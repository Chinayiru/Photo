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


def _secret(db: Any) -> str:
    from . import control

    return control.client_signing_secret(db)


def _sig(secret: str, media_id: int, exp: int) -> str:
    msg = f"{_PREFIX}:{media_id}:{exp}".encode()
    digest = hmac.new(secret.encode(), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def mint(db: Any, media_id: int, *, ttl: float = STREAM_TICKET_TTL,
         now: float | None = None) -> str:
    """签发一张票：`st1.<exp>.<sig>`。

    票里**不**放 client_id：票只回答"能不能读这一条"，不回答"你是谁"。
    调用方（`stream_ticket` 处理器）已经在鉴权中间件后面，能拿票的人
    本来就是这条设备的合法用户。
    """
    exp = int((now if now is not None else time.time()) + ttl)
    return f"{_PREFIX}.{exp}.{_sig(_secret(db), int(media_id), exp)}"


def verify(db: Any, ticket: str, media_id: int, *,
           now: float | None = None) -> bool:
    """票是否有效，且确实是给这一条签的。

    任何解析失败都返回 `False`（不抛异常）：调用方只关心"能不能放行"，
    而畸形票与过期票的处理完全一样 —— 都是让前端重新换一张。
    """
    parts = str(ticket or "").strip().split(".")
    if len(parts) != 3 or parts[0] != _PREFIX:
        return False
    try:
        exp = int(parts[1])
    except ValueError:
        return False
    if exp <= int(now if now is not None else time.time()):
        return False
    expect = _sig(_secret(db), int(media_id), exp)
    return hmac.compare_digest(expect, parts[2])
