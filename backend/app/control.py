"""隧道控制通道：云端 → 电脑端的最小内部控制面（`POST /.pb/control`）。

## 为什么需要它

审计 2026-10-26 的两条「已知安全边界」（`docs/06` §13）都源于同一件事：
**电脑端把自己的签名能力借给了云端**。

二期令牌的 HMAC 密钥是 `sha256(agent_token)`，而云端库里 `devices.agent_token`
存的**正是这个值** —— 所以"云端数据库泄露"等于"令牌签名私钥泄露"，攻击者
可以离线签出任意手机令牌，电脑端照样验签通过。

要真正消掉它，密钥只能待在电脑上。于是签发动作必须搬回电脑端：云端配对
成功时**请**电脑签一枚令牌（本模块的 `mint_client`），云端只保存 `sha256(令牌)`。

同理，局域网配对签出来的令牌云端从没见过（它没参与那次配对），于是云端
在遇到一枚自己不认识的令牌时，同样请电脑端验一下（`verify_client`），
验过再把哈希记下来 —— 这样"在家配对、出门可用"不受影响，而云端手里
始终没有签名密钥。

第三个用途是撤销：`revoke_clients` 让云端在用户点"移除这台手机"的**那一下**
就把撤销推进来，不必等 10 分钟一轮的轮询（轮询保留作兜底）。

## 威胁模型（写清楚，别夸大）

本通道只走**已经通过 `agent_token` 鉴权**的那条隧道。因此：

* 拿到云端数据库文件（备份、磁盘残留）的攻击者：**签不出**令牌 —— 库里没有密钥。
* 完全控制正在运行的云端、且电脑在线：仍可以命令电脑签一枚令牌。这一条
  **没有被消掉**，要靠端到端鉴权（手机与电脑之间做 PAKE / 口令证明）才能挡，
  记在 `docs/06` §13.3 与本文件末尾的"未做到"里。

## 协议

`POST /.pb/control`，请求体 `{"action": "...", "payload": {...}}`，响应体
`{"ok": true, ...}` 或 `{"ok": false, "code": "E500x", "message": "..."}`。

| action | 用途 | 成功返回 |
|---|---|---|
| `ping` | 探活/自检 | `device_id` `device_name` `version` |
| `mint_client` | 签一枚手机令牌 | `client_id` `token` `expires_at` `name` |
| `verify_client` | 验一枚云端不认识的令牌 | `client_id` `name` `expires_at` 或 `valid=false` |
| `revoke_clients` | 立即撤销 | `revoked`（生效条数） |

★ 体大小上限 4 KiB：这条通道只传 JSON 小对象，**永远不传照片**。没有上限
的话，一条 `Content-Length: 2GB` 的控制帧就能让电脑端先吃满内存再报错。
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

#: 模块日志。控制通道的失败**必须**留痕：这条链路平时安静，出问题时
#: 要么是"云端在命令电脑签令牌"（要能查），要么是"密钥坏了"（要能告警）。
log = logging.getLogger(__name__)

#: 控制通道路径。`/api/**` 白名单之外**唯一**放行的一条（见
#: `backend/app/transports/cloud_tunnel.py::_clean_path`），因此手机无论
#: 怎么构造 URL 都到不了这里：云端给手机的路由前缀是 `/d/<id>/api/…`。
CONTROL_PATH = "/.pb/control"

#: 请求体上限。
MAX_BODY = 4096

#: 令牌签名密钥在本地 `settings` 表里的键名。
SECRET_KEY = "client_signing_secret"

#: 允许的动作。
ACTIONS = ("ping", "mint_client", "verify_client", "revoke_clients")


class ControlError(Exception):
    """控制请求被拒。`code` 形如 `E5001`，`status` 是回复的 HTTP 状态码。"""

    __slots__ = ("code", "message", "status")

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def client_signing_secret(db: Any) -> str:
    """取（首次调用时生成）电脑端独有的令牌签名密钥。

    ★ 这是整条链路上**唯一**一份能签手机令牌的材料，它只存在于电脑的
    `settings` 表里，从不上传、不进日志、不进任何隧道帧。云端拿到过的
    只有 `sha256(token)`。

    32 字节随机（`secrets.token_hex(32)`）+ 只在需要时生成：老库升级上来
    时第一次配对会自动补上，不需要迁移脚本。
    """
    row = db.query_one(
        "SELECT value FROM settings WHERE key = ?", (SECRET_KEY,))
    current = str(row["value"]) if row is not None and row["value"] else ""
    if _looks_like_secret(current):
        return current
    fresh = secrets.token_hex(32)
    db.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SECRET_KEY, fresh))
    return fresh


def _looks_like_secret(value: str) -> bool:
    """64 位小写 hex 才算数 —— 手改坏了就重新生成，别拿坏值去签名。"""
    return (len(value) == 64
            and all(ch in "0123456789abcdef" for ch in value))


def parse_request(raw: bytes) -> tuple[str, dict[str, Any]]:
    """把控制请求体解析成 `(action, payload)`；格式不对抛 `ControlError`。"""
    if len(raw) > MAX_BODY:
        raise ControlError("E5002", f"控制请求体过大（{len(raw)} > {MAX_BODY}）", 413)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ControlError("E5001", f"控制请求体不是合法 JSON：{exc}") from exc
    if not isinstance(obj, dict):
        raise ControlError("E5001", "控制请求体必须是 JSON 对象")
    action = str(obj.get("action") or "").strip()
    if action not in ACTIONS:
        raise ControlError("E5001", f"未知控制动作：{action!r}")
    payload = obj.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ControlError("E5001", "payload 必须是 JSON 对象")
    return action, payload


def handle(state: Any, cfg: Any, action: str, payload: dict[str, Any],
           *, now: float | None = None) -> dict[str, Any]:
    """执行一个控制动作，返回要写回隧道的字典。"""
    moment = float(now if now is not None else time.time())
    if action == "ping":
        return {
            "device_id": _local_device_id(state, cfg),
            "device_name": str(getattr(state, "device_name", "") or ""),
            "version": _version(),
        }
    if action == "mint_client":
        return _mint_client(state, cfg, payload, moment)
    if action == "verify_client":
        return _verify_client(state, cfg, payload)
    if action == "revoke_clients":
        return _revoke_clients(state, payload)
    raise ControlError("E5001", f"未知控制动作：{action!r}")


def _version() -> str:
    from . import __version__ as version

    return str(version)


def _local_device_id(state: Any, cfg: Any) -> str:
    from .tunnel import cloud_device_id

    return cloud_device_id(cfg, str(getattr(state, "device_id", "") or ""))


def _mint_client(state: Any, cfg: Any, payload: dict[str, Any],
                 now: float) -> dict[str, Any]:
    """给云端配对签一枚手机令牌（云端只留 `sha256(token)`）。"""
    from shared import client_token as token_mod

    from .auth import hash_token

    device_id = _local_device_id(state, cfg)
    if not device_id:
        # 没配云端就没有 `dev` claim 可写。云端会把它当成"这台上线异常"，
        # 比签出一枚永远验不过的令牌要好 —— 后者表现为"配对成功但全站 401"。
        raise ControlError("E5003", "本机没有云端设备 id，无法签发云端令牌", 409)

    name = str(payload.get("name") or "").strip()[:64] or "手机"
    try:
        ttl_days = int(payload.get("ttl_days") or 90)
    except (TypeError, ValueError) as exc:
        raise ControlError("E5001", f"ttl_days 不是整数：{payload.get('ttl_days')!r}") from exc
    ttl_days = max(1, min(ttl_days, int(token_mod.MAX_TTL_DAYS)))

    # 32 hex：云端 `devices_api.DEVICE_ID_RE` 只认这个宽度（16 hex 会被
    # 归一成空串 —— 症状是配对成功、撤销 404）。
    client_id = secrets.token_hex(16)
    token = token_mod.issue_token(
        client_signing_secret(state.db), client_id=client_id,
        device_id=device_id, ttl_days=ttl_days, now=now, client_name=name)
    expires_at = now + ttl_days * 86400

    # ★ 这里**不**写本地 `devices` 行：手机第一次用这枚令牌时，
    # `auth._verify_cloud_token()` 的 upsert 会把行补上（含 role 判定与撤销粘性）。
    # 在这里再写一遍有两个坏处：一是配对码抢码失败时留下孤儿行，
    # 二是“谁决定 role”变成两处实现。
    return {"client_id": client_id, "token": token, "name": name,
            "expires_at": expires_at}


def _verify_client(state: Any, cfg: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """验一枚**云端从没见过**的令牌（典型来源：局域网配对）。

    只验签、不落库：写本地行是 `auth._verify_cloud_token()` 的职责，它已经
    把这套 upsert 的撤销粘性处理对了，这里再写一遍迟早会分叉。
    """
    from shared import client_token as token_mod

    device_id = _local_device_id(state, cfg)
    token = payload.get("token")
    if not isinstance(token, str) or not token:
        raise ControlError("E5001", "缺少 token")
    if not device_id:
        return {"valid": False, "reason": "no_device_id"}

    for secret in _candidate_secrets(state):
        claims = token_mod.verify_token(secret, token, device_id=device_id)
        if claims is not None:
            break
    else:
        return {"valid": False, "reason": "bad_signature"}
    return {
        "valid": True,
        "client_id": str(claims.get("sub") or "")[:64],
        "name": str(claims.get("nam") or "").strip()[:64] or "手机",
        "expires_at": float(claims.get("exp") or 0.0),
    }


def _revoke_clients(state: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """立刻撤销（`docs/06` §13.2 的延迟问题）。"""
    from .auth import sync_revoked_clients

    ids = payload.get("ids")
    if not isinstance(ids, list):
        raise ControlError("E5001", "ids 必须是数组")
    wanted = [str(x).strip()[:64] for x in ids if str(x or "").strip()]
    if len(wanted) > 200:
        # 撤销是逐条 UPDATE，一次性推 10 万条会把电脑端卡住；云端分批推。
        raise ControlError("E5002", "一次最多撤销 200 条", 413)
    return {"revoked": sync_revoked_clients(state, wanted)}


def _candidate_secrets(state: Any) -> list[str]:
    """新密钥优先；旧密钥只在显式开的迁移窗口期内兜底（ISSUE-049）。

    旧密钥是 `sha256(agent_token)`，云端库里就有 —— 接受它等于把签名权
    又放回云端。默认关，`pbctl allow-legacy <天数>` 才开。

    ★ 审计 M01：`client_signing_secret()` 读不出来时**整体失败**，绝不
    退化成"只试旧密钥"。那等于把 ISSUE-049 修掉的攻击链重新接上，而且
    退化时一眼看不出来（照样能验签成功）。
    """
    from .auth import legacy_tunnel_secret, legacy_window_open

    try:
        fresh = client_signing_secret(state.db)
    except Exception as exc:        # noqa: BLE001 - 任何失败都不能降级
        log.warning("控制通道：私有签名密钥不可用，拒绝验签：%s", exc,
                    exc_info=True)
        raise ControlError("E5001", "签名密钥不可用，已拒绝校验", 500) from exc
    out = [fresh]
    legacy = legacy_tunnel_secret(getattr(state, "cfg", None))
    if legacy and legacy not in out and legacy_window_open(state.db):
        out.append(legacy)
    return out
