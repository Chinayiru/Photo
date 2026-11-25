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
| `ping` | 探活/自检 | `{"ok": true}` —— **只有这个字段** |
| `request_pair` | 云端**申请**一次配对（不签发） | `state=pending` `request_id` `verify_code` `fingerprint` |
| `pair_status` | 手机轮询：待批准 / 已批准换令牌 | `state` / `client_id` `token` `expires_at` |
| `mint_client` | **已停用**（A1.3 的原始入口） | 恒为 403 `E5015` |
| `verify_client` | 验一枚云端不认识的令牌 | `client_id` `name` `expires_at` 或 `valid=false` |
| `revoke_clients` | 立即撤销 | `revoked`（生效条数） |

★ 2026-11-24 渗透报告 A1.3：`mint_client` 原来"云端问一句就签"。现在签发要
先 `request_pair` 挂起，由托盘的本地批准（`backend/app/pairing.py`）决定
`client_id`，手机再凭**同一枚设备密钥**到 `pair_status` 兑换。中继单独发起的
申请只能停在 `pending`，换不到令牌。

★ `ping` 只回 `ok`（A1.2）：原先它无凭据地回 `device_id`/`device_name`/`version`，
一条假云端连接就能读到这台电脑的身份与版本。探活是"你还活着吗"，管理动作
必须分开并有各自的权限 —— 家里电脑的版本号不该是公网可查的元数据。

★ 体大小上限 4 KiB：这条通道只传 JSON 小对象，**永远不传照片**。没有上限
的话，一条 `Content-Length: 2GB` 的控制帧就能让电脑端先吃满内存再报错。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from .identity import (
    client_signing_secret,
    cloud_device_id,
    legacy_tunnel_secret,
    legacy_window_open,
    sync_revoked_clients,
)

#: 模块日志。控制通道的失败**必须**留痕：这条链路平时安静，出问题时
#: 要么是"云端在命令电脑签令牌"（要能查），要么是"密钥坏了"（要能告警）。
log = logging.getLogger(__name__)

#: 控制通道路径。`/api/**` 白名单之外**唯一**放行的一条（见
#: `backend/app/transports/cloud_tunnel.py::_clean_path`），因此手机无论
#: 怎么构造 URL 都到不了这里：云端给手机的路由前缀是 `/d/<id>/api/…`。
CONTROL_PATH = "/.pb/control"

#: 请求体上限。
MAX_BODY = 4096

#: 允许的动作。`mint_client` 留着只为**明确拒绝**（见 `handle`）：
#: 从白名单里删掉会把它变成 `E5001 未知控制动作`，而"这个动作存在但被停用"
#: 与"你写错了动作名"是两件事，日志里必须分得开。
ACTIONS = ("ping", "request_pair", "pair_status", "mint_client",
           "refresh_client", "verify_client", "revoke_clients")


class ControlError(Exception):
    """控制请求被拒。`code` 形如 `E5001`，`status` 是回复的 HTTP 状态码。"""

    __slots__ = ("code", "message", "status")

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


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
           *, now: float | None = None, remote: str = "") -> dict[str, Any]:
    """执行一个控制动作，返回要写回隧道的字典。

    `remote` 是云端转进来的客户端来源（隧道帧的 `client_ip`），只用于事件
    留痕，**不参与鉴权**：能走到这里的对端已经过了 `agent_token` 握手，
    而客户端自报的转发头在云端就被 `netutil.client_ip()` 丢掉了。
    """
    moment = float(now if now is not None else time.time())
    if action == "ping":
        return {"ok": True}
    if action == "request_pair":
        return _request_pair(state, cfg, payload, moment, remote)
    if action == "pair_status":
        return _pair_status(state, payload, moment)
    if action == "mint_client":
        raise ControlError(
            "E5015",
            "签发令牌需要电脑端批准：请改用 request_pair，并在托盘确认", 403)
    if action == "refresh_client":
        return _refresh_client(state, cfg, payload, moment)
    if action == "verify_client":
        return _verify_client(state, cfg, payload)
    if action == "revoke_clients":
        return _revoke_clients(state, payload)
    raise ControlError("E5001", f"未知控制动作：{action!r}")


def _version() -> str:
    from . import __version__ as version

    return str(version)


def _local_device_id(state: Any, cfg: Any) -> str:

    return cloud_device_id(cfg, str(getattr(state, "device_id", "") or ""))


def _request_pair(state: Any, cfg: Any, payload: dict[str, Any], now: float,
                  remote: str) -> dict[str, Any]:
    """云端**申请**一次配对（不签发任何令牌）。

    A1.3 的原始入口是 `mint_client`：一条控制帧换一枚真令牌。现在同样的
    调用只能得到一条 `pending` 记录 —— 签发要等托盘的本地批准。
    """
    from . import pairing as pairing_mod

    device_id = _local_device_id(state, cfg)
    if not device_id:
        raise ControlError("E5003", "本机没有云端设备 id，无法登记配对请求", 409)
    wanted = str(payload.get("device_id") or "").strip().lower()
    if wanted and wanted != device_id:
        # 云端说"给设备 X 发请求"，而本机是 Y：要么配置漂移，要么有人在
        # 拿别人的 device_id 试探。两种都该拒绝，而不是静默按本机 id 记。
        raise ControlError("E5003", "控制请求的 device_id 与本机不一致", 409)
    try:
        return pairing_mod.create_request(
            state.db, device_id=device_id,
            device_key=payload.get("device_key"),
            client_name=str(payload.get("name") or ""),
            ttl_days=payload.get("ttl_days") or 90,
            source="cloud" if remote else "cloud-local",
            request_id=str(payload.get("request_id") or ""),
            now=now)
    except pairing_mod.PairError as exc:
        raise ControlError(exc.code, exc.message, exc.status) from exc


def _pair_status(state: Any, payload: dict[str, Any],
                 now: float) -> dict[str, Any]:
    """手机轮询：待批准时回核对码，已批准时**一次性**换令牌。"""
    from . import pairing as pairing_mod

    try:
        return pairing_mod.redeem(
            state.db, str(payload.get("request_id") or ""),
            str(payload.get("device_key") or ""), now=now)
    except pairing_mod.PairError as exc:
        # ★ "被拒绝 / 已过期 / 已被兑换"是**正常状态**，不是控制通道故障：
        #   云端要把这条结论原样转给手机（"电脑上点了拒绝，别再轮询了"），
        #   如果在这里抛 ControlError，`call_agent()` 只会看到非 200 而返回
        #   None，手机端就只能看到"电脑不在线"这一种含糊症状。
        #
        # E5005（设备密钥不符）也归到这里：它是"这台手机带着错误的密钥来
        # 兑换"，对手机来说是**终态**（本地存储被清空、换了标签页、换了
        # 设备都会这样），必须让它重新配对，而不是一直显示"电脑不在线"。
        # 申请本身不作废，合法手机随后仍然能兑换（见 `pairing.redeem`）。
        if exc.code in ("E5005", "E5012", "E5013"):
            reason = exc.message
            if exc.code == "E5005":
                reason = "这台手机的配对密钥与申请不一致，请在手机上重新配对"
            return {"state": "rejected", "reason": reason}
        raise ControlError(exc.code, exc.message, exc.status) from exc


def _refresh_client(state: Any, cfg: Any, payload: dict[str, Any],
                    now: float) -> dict[str, Any]:
    """Rotate an active owner's token while preserving its client identity."""
    from shared import client_token as token_mod

    old_token = payload.get("token")
    wanted_id = str(payload.get("client_id") or "")
    device_id = _local_device_id(state, cfg)
    if not isinstance(old_token, str) or not wanted_id or not device_id:
        raise ControlError("E5001", "续期请求无效")
    claims = None
    for secret in _candidate_secrets(state):
        claims = token_mod.verify_token(secret, old_token, device_id=device_id,
                                        now=now)
        if claims is not None:
            break
    if (claims is None or str(claims.get("sub") or "") != wanted_id
            or str(claims.get("rl") or "owner") == "guest"):
        raise ControlError("E5001", "续期令牌无效", 401)
    # ★ 续期必须受原设备的权限状态约束：被移除的手机不能靠续期"复活"。
    # 令牌有效期 90 天，没有这一条的话撤销与续期的竞争里，撤销会输给
    # 一台拿着旧令牌的机器 —— 它每秒重试，而撤销只生效一次。
    if _client_revoked(state.db, wanted_id):
        raise ControlError("E5004", "该设备已被移除，不能续期", 403)
    name = str(claims.get("nam") or "手机")[:64]
    ttl_days = int(token_mod.TOKEN_TTL_DAYS)
    token = token_mod.issue_token(
        secret, client_id=wanted_id, device_id=device_id, ttl_days=ttl_days,
        now=now, client_name=name)
    return {"client_id": wanted_id, "token": token,
            "name": name, "expires_at": now + ttl_days * 86400}


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

    ids = payload.get("ids")
    if not isinstance(ids, list):
        raise ControlError("E5001", "ids 必须是数组")
    wanted = [str(x).strip()[:64] for x in ids if str(x or "").strip()]
    if len(wanted) > 200:
        # 撤销是逐条 UPDATE，一次性推 10 万条会把电脑端卡住；云端分批推。
        raise ControlError("E5002", "一次最多撤销 200 条", 413)
    return {"revoked": sync_revoked_clients(state, wanted)}


def _client_revoked(db: Any, client_id: str) -> bool:
    """这枚 client 是否已被移除（查本地 `devices` 行与云端令牌缓存）。

    `cloud_clients` 是懒建的（第一次收到云端令牌才有这张表），所以先看一眼
    `sqlite_master` —— 直接在空库上查它会抛 `no such table`，而"表不存在"
    与"库坏了"必须分开：前者是正常的首次状态，后者应当拒绝续期。
    """
    if not client_id:
        return True
    row = db.query_one("SELECT revoked FROM devices WHERE id = ?", (client_id,))
    if row is not None and int(row["revoked"] or 0):
        return True
    try:
        exists = db.query_one(
            "SELECT 1 AS x FROM sqlite_master"
            " WHERE type = 'table' AND name = 'cloud_clients'")
        if exists is None:
            return False
        cache = db.query_one(
            "SELECT expires_at FROM cloud_clients WHERE client_id = ?",
            (client_id,))
    except Exception:       # noqa: BLE001 - 读不出来就别放行
        log.warning("续期时读取撤销状态失败，按已移除处理", exc_info=True)
        return True
    # 撤销在缓存里写成 `expires_at = 0`（`identity.sync_revoked_clients`）。
    return cache is not None and float(cache["expires_at"] or 0) <= 0


def _candidate_secrets(state: Any) -> list[str]:
    """新密钥优先；旧密钥只在显式开的迁移窗口期内兜底（ISSUE-049）。

    旧密钥是 `sha256(agent_token)`，云端库里就有 —— 接受它等于把签名权
    又放回云端。默认关，`pbctl allow-legacy <天数>` 才开。

    ★ 审计 M01：`client_signing_secret()` 读不出来时**整体失败**，绝不
    退化成"只试旧密钥"。那等于把 ISSUE-049 修掉的攻击链重新接上，而且
    退化时一眼看不出来（照样能验签成功）。
    """

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
