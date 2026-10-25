"""客户端令牌的离线校验（二期）。

## 这份文件解决的是设计文档自己标记的那个缺口

`docs/04-接口设计文档.md:99-112` 写着：

> 云端 `clients` 表只存 `token_hash`，**电脑端拿不到任何可离线校验的材料**，
> 因此"本地校验"在协议上不成立。

一期之所以能跑，是因为手机**只连局域网**、配对码由电脑自己签发，令牌
本来就存在电脑的 `devices` 表里。二期手机走云端时，令牌是**云端**签发的
——电脑的 `devices` 表里没有这一行，于是第一次经隧道转发就会 401
（`require_client` 查不到 `token_hash`）。

三条路，各自的代价：

| 方案 | 代价 |
|---|---|
| 电脑端每次回源云端 introspect | 电脑断网时局域网直连也废了，与"局域网加速"直接矛盾 |
| Ed25519 JWT（文档推荐） | 需要一个 Ed25519 实现；`.venv` 里没有 `cryptography`，纯 Python 版要 ~100 行椭圆曲线运算 |
| **HMAC-SHA256 令牌（本文件）** | 两端共享同一把对称密钥 |

选第三条，理由是**这把密钥本来就已经由两端共享，而且云端只存了它的哈希**：

* 电脑端持有 `agent_token` 明文（`data/config.json` 的 `cloud.agent_token`）；
* 云端 `devices.agent_token` 列存的是 `sha256(agent_token)`；
* 于是 `key = sha256(agent_token)` 电脑端能算（对明文做一次 sha256），
  云端手上就有（那一列就是）。

信任边界：云端数据库中的这一列同时也是客户端令牌的签名密钥。
数据库泄露后，攻击者可以伪造新的客户端令牌，获得照片 API 访问权。
哈希本身不能直接冒充电脑建立隧道，因为云端会对提交的 agent_token
再次哈希。P1 仍把云端服务和数据库视为可信鉴权方；要抵抗云端被攻破，
需要另行设计本机授权与端到端鉴权，不能仅靠这份 HMAC 实现。
电脑端仍校验 `device_id`（`dev` claim），设备 A 的令牌在设备 B 上无效。

## 为什么不用 JWT 库

`requirements.txt` 里没有 `pyjwt`/`python-jose`，P1 也不该为了三个
字段的 JSON 引入一个依赖树。格式刻意做得和 JWT 一样（三段式、base64url、
不填充），将来要换 EdDSA 只需替换本文件的 `sign`/`verify` 两个函数。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from typing import Any

__all__ = [
    "TOKEN_PREFIX",
    "TOKEN_TTL_DAYS",
    "MAX_TTL_DAYS",
    "issue_token",
    "verify_token",
    "secret_for_agent_token",
    "TOKEN_MAX_LEN",
]

#: 令牌前缀。用 `pbc`（PhotoBridge Client）而不是 `eyJ`（JWT 的 base64 头），
#: 这样在日志/抓包里一眼就能把它和别的 JWT 区分开，也便于日志过滤规则匹配。
TOKEN_PREFIX = "pbc1"

#: 与一期 `backend/app/auth.py:106 TOKEN_TTL_DAYS` 保持一致。
TOKEN_TTL_DAYS = 90

#: "永不过期"用到的 ttl 天数（100 年）。
#:
#: 一期 `auth.token_ttl_days = 0` 的语义是**永不过期**，而 HMAC 令牌的
#: `exp` 是一个绝对时刻，没有"不含 exp"这种编码。用 100 年表示它：
#: 到 2126 年才会过期，而那时这张照片库早已不在（本项目的数据模型
#: 里没有"跨世纪"这个概念）。**不要**改成 `exp = 2**63`：`verify_token`
#: 会把 exp 转成 float 参与比较，极端值上 `int`/`float` 混算会抛。
MAX_TTL_DAYS = 36500

#: 令牌长度上限。三段式 + 32 字节 MAC 大约 150 字符，给到 1024，
#: 同时挡住"拿 1 MB 字符串当令牌"这种把 HMAC 当哈希炮灰的请求。
#:
#: ★ 为什么不是 512：`client_name` 允许 64 个**字符**，而一个 emoji 在
#: UTF-8 里占 4 字节、base64 后再膨胀 4/3。实测「64 个 😀」签出来的
#: 令牌是 559 字符 —— 卡在 512 会让"配对成功但之后每个请求都 401"，
#: 而那种故障没人会想到是名字太长导致的。1024 给同类输入留足余量，
#: 仍然远小于任何有意义的 DoS 面（一次 HMAC 只吃固定大小的密钥）。
TOKEN_MAX_LEN = 1024

_B64 = base64.urlsafe_b64encode
_UNB64 = base64.urlsafe_b64decode


def _b64e(raw: bytes) -> str:
    return _B64(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    """补回填充再解。非法 base64 抛 `ValueError`（调用方一律当验签失败）。"""
    pad = "=" * (-len(text) % 4)
    try:
        raw = _UNB64((text + pad).encode("ascii"))
        # 同一签名只能有一种文本表示；否则可改变 token_hash，绕开撤销记录。
        if _b64e(raw) != text:
            raise ValueError("non-canonical base64")
        return raw
    except (binascii.Error, UnicodeEncodeError, ValueError) as exc:
        raise ValueError("bad base64") from exc


def secret_for_agent_token(agent_token: str) -> str:
    """`agent_token` 明文 → 两端共享的 HMAC 密钥（hex 字符串）。

    云端那一侧不需要这个函数：它读的就是 `devices.agent_token` 列，
    值完全相同。写成函数是为了让电脑端有一处**唯一**的计算方式——
    两处各写一遍 `sha256(...).hexdigest()` 迟早会有一边多一个 `.strip()`。
    """
    if not agent_token:
        return ""
    return hashlib.sha256(agent_token.strip().encode("utf-8")).hexdigest()


def issue_token(secret: str, *, client_id: str, device_id: str,
                ttl_days: int = TOKEN_TTL_DAYS,
                now: float | None = None,
                client_name: str = "") -> str:
    """签发一枚绑定 `(client_id, device_id)` 的令牌。

    `client_name` 是可选的展示名（claim `nam`）。带上它的理由：电脑端
    第一次见到这枚令牌时本地 `devices` 表里没有这一行，若没有名字，
    `GET /api/devices` 里只能显示"未知设备"，而用户在手机丢了之后
    正是靠名字判断该移除哪一台。
    """
    if not secret:
        raise ValueError("secret 不能为空")
    issued = int(now if now is not None else time.time())
    payload = {
        "sub": client_id,
        "dev": device_id,
        "iat": issued,
        "exp": issued + int(ttl_days * 86400),
    }
    if client_name:
        # 截到 64 与云端 `clients.name` 的 CHECK 一致；不截会让一个
        # 超长名字把令牌顶到 TOKEN_MAX_LEN 之上，于是一枚刚签发的令牌
        # 立刻验不过（"配对成功但所有请求 401"是最难自查的一类故障）。
        payload["nam"] = str(client_name)[:64]
    body = _b64e(json.dumps(payload, ensure_ascii=False,
                            separators=(",", ":")).encode("utf-8"))
    signing_input = f"{TOKEN_PREFIX}.{body}"
    mac = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"),
                   hashlib.sha256).digest()
    return f"{signing_input}.{_b64e(mac)}"


def verify_token(secret: str, token: str, *, device_id: str | None = None,
                 now: float | None = None) -> dict[str, Any] | None:
    """校验令牌，成功返回 claims，失败返回 None。

    返回 None 而不抛异常：调用点（`require_client`）对所有失败都要给出
    同一个 401，把"为什么失败"分成不同异常只会诱使上层把它拼进响应体
    （那正是审计 F14 的形态）。
    """
    if not secret or not token or len(token) > TOKEN_MAX_LEN:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    signing_input = f"{parts[0]}.{parts[1]}"
    try:
        want = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"),
                        hashlib.sha256).digest()
        got = _b64d(parts[2])
    except (ValueError, UnicodeEncodeError):
        return None
    if not hmac.compare_digest(want, got):
        return None
    try:
        claims = json.loads(_b64d(parts[1]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    sub, dev = claims.get("sub"), claims.get("dev")
    exp = claims.get("exp")
    if not isinstance(sub, str) or not sub:
        return None
    if not isinstance(dev, str) or not dev:
        return None
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None
    if exp <= (now if now is not None else time.time()):
        return None
    # ★ 设备绑定校验。少了这一行，一台电脑的令牌能读另一台的照片。
    if device_id is not None and dev != device_id:
        return None
    return claims
