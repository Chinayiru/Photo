"""短视频播放票（`?t=`）：给 `<video>` 用的**短期、单条目、有效期内可复用**的播放凭证。

## 为什么需要它

`<video src="...">` 由**浏览器自己**发请求，页面 JS 完全插不上手，因此：

- 加不了 `Authorization: Bearer`；
- 唯一的替代是 cookie，而 cookie 这条路已经被现实打过两次脸
  （ISSUE-033：`Secure` 写死在 http 页面上被丢弃；ISSUE-057：客户端
  `credentials: "omit"` 把服务端 `Set-Cookie` 直接扔了）。症状都一样：
  `<video>` 每 1.5 秒重试一次、次次 `401 E2002`，用户看到"永远在转圈"。

所以这里走**显式换票**：已认证的 JS 先换一张 5 分钟、只对**单个条目**
有效的票，再把 `?t=<票>` 拼进播放地址。

## 一次性还是可复用

**可复用**（有效期内）。`<video>` 播一条片子会发多次请求：起播一次、
拖动进度一次、网络抖动重试一次。若"用过即废"，第二次 Range 就是 401，
用户看到的现象与"没有票"完全一样。所以票是"短期通行证"，
不是"一次性入场券"——`docs/04` 的契约也只规定「单条目绑定 + 300 秒有效」。

## 踢出之后呢

「可复用」不等于「谁拿着都能一直用」：票里签进了签票人自己的 `client_id`，
验票时若该 client 有游客会话行、而这条会话已经不在有效状态（被踢出、邀请被
撤销、过期），票立即失效 —— 不必等满 300 秒。主人自己的票不受影响：主人
没有 `guest_sessions` 行，那套规则不适用于它。

## 与「把 token 塞进 URL」的区别（后者在 `storage.ts` 里被否决）

1. **绑定条目**：票只对签发它的 `media_id` 有效，泄漏了也打不开别的照片；
2. **短命**：默认 300 秒，到期即失效（有效期内可重复请求，见上节），
   不像 90 天的配对令牌会进浏览器历史、
   服务器访问日志、`Referer`；
3. **可轮换**：换一次票不牵动任何持久凭证，用户端不需要重新配对。

签名用 `control.client_signing_secret`（ISSUE-049 之后**只存在电脑上**的
密钥）：云端即使被拖库也伪造不出一张票。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

#: 票的有效期（秒）。够播完一条视频，短到泄漏也不值得利用。
STREAM_TICKET_TTL: float = 300.0

#: 票的前缀。带版本号是为了将来换签名方案时能一眼看出旧票。
_PREFIX = "st1"

#: 没有 client 线索时占位用的段值（`-` 不是合法 base64url 字符，不会与真值冲突）。
_NO_CLIENT = "-"


def _secret(db: Any) -> str:
    from . import control

    return control.client_signing_secret(db)


def _sig(secret: str, media_id: int, exp: int, client_id: str = "") -> str:
    msg = f"{_PREFIX}:{media_id}:{exp}:{client_id}".encode()
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _encode_client(client_id: str) -> str:
    if not client_id:
        return _NO_CLIENT
    raw = base64.urlsafe_b64encode(client_id.encode()).decode().rstrip("=")
    return raw


def _decode_client(seg: str) -> str | None:
    if seg == _NO_CLIENT:
        return ""
    try:
        pad = "=" * (-len(seg) % 4)
        return base64.urlsafe_b64decode(seg + pad).decode()
    except (ValueError, UnicodeDecodeError, base64.binascii.Error):
        return None


def _guest_session_gone(db: Any, client_id: str, now: float) -> bool:
    """这个客户端的游客会话是否已经不能用（被踢出/撤销/过期）。

    ★ 先看有没有会话行：没有行说明这个 client 不是游客（主人自己的手机），
    不该拿游客那套规则去卡它。有行就必须「当场仍有效」—— 票的 300 秒寿命
    是给 `<video>` 连发的几次 Range 用的，不是给被踢出的人续命用的。
    """
    row = db.query_one(
        "SELECT 1 FROM guest_sessions WHERE client_id = ?", (client_id,))
    if row is None:
        return False
    from . import guest

    ok, _reason = guest.session_active(db, client_id, now)
    return not ok


def mint(db: Any, media_id: int, *, client_id: str = "",
         ttl: float = STREAM_TICKET_TTL, now: float | None = None) -> str:
    """签发一张票：`st1.<exp>.<client>.<sig>`。

    **票里带 client_id**（base64url，无线索时是 `-`）：票要回答两个问题 ——
    "能不能读这一条"和"这张票的主人还在不在"。只答前一个的话，主人把游客
    踢出去以后，对方手里那张票还能再读 5 分钟（ISSUE-090）。client_id 本身
    是 32 位 hex、不是凭证，签进签名段只用于事后比对，不构成新的泄漏面。
    """
    exp = int((now if now is not None else time.time()) + ttl)
    cid = str(client_id or "")
    sig = _sig(_secret(db), int(media_id), exp, cid)
    return f"{_PREFIX}.{exp}.{_encode_client(cid)}.{sig}"


def verify(db: Any, ticket: str, media_id: int, *,
           now: float | None = None) -> bool:
    """票是否有效，确实是给这一条签的，且**签发者现在仍有权读**。

    任何解析失败都返回 `False`（不抛异常）：调用方只关心"能不能放行"，
    而畸形票与过期票的处理完全一样 —— 都是让前端重新换一张。
    """
    at = float(now if now is not None else time.time())
    parts = str(ticket or "").strip().split(".")
    if len(parts) == 3 and parts[0] == _PREFIX:
        # 旧格式（不带 client）：只在升级后的 5 分钟内还会收到，
        # 它没绑人，只能做条目绑定 + 过期这两项检查。
        cid = ""
        try:
            exp = int(parts[1])
        except ValueError:
            return False
        if exp <= int(at):
            return False
        return hmac.compare_digest(_sig(_secret(db), int(media_id), exp, ""),
                                   parts[2])
    if len(parts) != 4 or parts[0] != _PREFIX:
        return False
    try:
        exp = int(parts[1])
    except ValueError:
        return False
    if exp <= int(at):
        return False
    cid = _decode_client(parts[2])
    if cid is None:
        return False
    if not hmac.compare_digest(_sig(_secret(db), int(media_id), exp, cid),
                               parts[3]):
        return False
    return not (cid and _guest_session_gone(db, cid, at))
