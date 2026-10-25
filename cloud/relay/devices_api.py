"""设备与手机（客户端）管理 API（`docs/04` §3.3-§3.6）。

## 一条必须先说清楚的边界

云端**不存任何照片**，`devices` 表也只存"这台电脑叫什么、上次什么时候
出现、agent_token 的哈希"。所有"设备在线"的判断都来自**内存注册表**，
不是 `last_seen`。

> 若用 `now - last_seen < 60` 推断在线，隧道正常但空闲 61 秒的电脑会被
> 误报离线，前端就会降级到局域网探测——而用户其实一直连着云。
> `docs/04` §3.3 的补充说明也是同一句。

## 为什么 `device_id` 用 32 位 hex

它出现在 URL 路径里（`/d/{device_id}/api/**`）。32 位 hex 既够随机
（128 bit），又不含需要 URL 编码的字符，且大小写归一化后可以直接比较。
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from ..relay import control as cloud_control
from ..relay.auth import require_account
from ..relay.db import CloudDB
from ..relay.devices import (
    DeviceRegistry,
    today_utc,
    traffic_today,
)
from ..relay.reply import json_error, json_reply, no_content

__all__ = [
    "DEVICE_ID_RE",
    "list_devices",
    "device_status",
    "delete_client",
    "revoked_clients",
    "device_public",
    "require_owned_device",
    "owner_of_device",
]

#: `device_id` / `client_id` 的形状（`docs/04` §3.7：32 位 hex）。
DEVICE_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: 详细流量统计最多回溯多少天（`docs/04` §3.4：`1 ≤ days ≤ 90`）。
_MAX_DAYS: int = 90


def _norm_id(raw: Any) -> str:
    """归一化 id：小写 + 去空白。非法返回空串（调用方一律回 404）。"""
    if not isinstance(raw, str):
        return ""
    value = raw.strip().lower()
    return value if DEVICE_ID_RE.match(value) else ""


# ---------------------------------------------------------------------------
# 归属校验
# ---------------------------------------------------------------------------


def owner_of_device(db: CloudDB, device_id: str) -> str | None:
    """设备属于哪个账号；不存在返回 None。

    ★ `device_id` 不存在与"存在但不属于你"在**所有对外接口上必须返回
    同一个 404**（`docs/04` §3.4 的安全要点）。否则攻击者可以拿一个
    候选 id 列表探测哪些设备存在——虽然 128 bit 猜不到，但探测本身
    也不需要猜：一个泄露的日志里就可能有一批 id。
    """
    row = db.query_one("SELECT account_id FROM devices WHERE id = ?",
                       (device_id,))
    return str(row["account_id"]) if row is not None else None


def require_owned_device(request: Request, db: CloudDB,
                         device_id: str) -> tuple[dict[str, Any], Any] | None:
    """`(账号, 设备行)`；鉴权或归属失败返回 None。"""
    account = require_account(request, db)
    if account is None:
        return None
    raw = _norm_id(device_id)
    if not raw:
        return None
    row = db.query_one(
        "SELECT id, account_id, name, version, last_seen, last_lan, "
        "created_at, revoked FROM devices WHERE id = ?", (raw,))
    if row is None:
        return None
    if str(row["account_id"]) != account["id"]:
        return None
    return account, row


# ---------------------------------------------------------------------------
# 设备视图
# ---------------------------------------------------------------------------


def device_public(db: CloudDB, registry: DeviceRegistry,
                  row: Any) -> dict[str, Any]:
    """`docs/04` §3.3 的 `devices[]` 元素。"""
    device_id = str(row["id"])
    conn = registry.get(device_id)
    try:
        last_lan = json.loads(row["last_lan"] or "[]")
    except (TypeError, ValueError):
        last_lan = []
    if not isinstance(last_lan, list):
        last_lan = []
    clients_count = int(db.scalar(
        "SELECT COUNT(*) FROM clients WHERE device_id = ? AND revoked = 0",
        (device_id,)) or 0)
    return {
        "id": device_id,
        "name": row["name"],
        # ★ 在线状态只来自内存 map，绝不用 last_seen 推断。
        "online": conn is not None,
        "version": row["version"] or None,
        "last_seen": row["last_seen"],
        "last_lan": [str(x) for x in last_lan][:16],
        "created_at": float(row["created_at"]),
        "revoked": bool(int(row["revoked"] or 0)),
        "clients_count": clients_count,
        "traffic_today": traffic_today(db, device_id),
        "indexed_count": _indexed_count(conn),
        "tunnel_mode": "ws" if conn is not None else None,
    }


def _indexed_count(conn: Any) -> int | None:
    """最近一次 HELLO 上报的索引条数（进程内存，重启后 None）。

    故意不落库：它是"这台电脑现在有多少张照片"，会随扫描变化，落库就
    多一份可能过期的真相。重启后返回 None，前端显示"未知"而不是一个
    陈旧的数字。
    """
    if conn is None:
        return None
    value = getattr(conn, "indexed_count", None)
    return int(value) if isinstance(value, int) else None


# ---------------------------------------------------------------------------
# 请求处理
# ---------------------------------------------------------------------------


async def list_devices(request: Request, db: CloudDB,
                       registry: DeviceRegistry) -> Response:
    account = require_account(request, db)
    if account is None:
        return json_error("E2001", request_id=_rid(request))
    include_revoked = (request.query_params.get("include_revoked") or "")
    include = include_revoked.strip().lower() in ("1", "true", "yes")
    sql = ("SELECT id, account_id, name, version, last_seen, last_lan, "
           "created_at, revoked FROM devices WHERE account_id = ?")
    args: list[Any] = [account["id"]]
    if not include:
        sql += " AND revoked = 0"
    # `last_seen IS NULL` 排前面还是后面：SQLite 里 NULL 在 DESC 时排最前，
    # 而"从未连上过"的电脑排第一反而显眼——就是它连不上，该被看见。
    sql += " ORDER BY last_seen DESC, created_at DESC"
    rows = db.query(sql, args)
    return json_reply({
        "devices": [device_public(db, registry, r) for r in rows],
        "server_time": time.time(),
    }, request_id=_rid(request))


async def device_status(request: Request, db: CloudDB,
                        registry: DeviceRegistry,
                        device_id: str) -> Response:
    found = require_owned_device(request, db, device_id)
    if found is None:
        return json_error("E1004", request_id=_rid(request))
    _account, row = found
    did = str(row["id"])

    days = _days(request.query_params.get("days"))
    traffic = _traffic_series(db, did, days)
    total_in = sum(int(t["bytes_in"]) for t in traffic)
    total_out = sum(int(t["bytes_out"]) for t in traffic)

    conn = registry.get(did)
    tunnel = None
    if conn is not None:
        mux = getattr(conn, "mux", None)
        tunnel = {
            "mode": "ws",
            "session_id": conn.session_id,
            "connected_at": conn.connected_at,
            "active_reqs": _active_reqs(mux),
            "credit_used_in": int(getattr(mux, "bytes_in", 0) or 0),
            "credit_used_out": int(getattr(mux, "bytes_out", 0) or 0),
        }

    clients = [
        {"id": str(c["id"]), "name": c["name"],
         "last_seen": c["last_seen"], "created_at": float(c["created_at"]),
         "revoked": bool(int(c["revoked"] or 0))}
        for c in db.query(
            "SELECT id, name, last_seen, created_at, revoked FROM clients "
            "WHERE device_id = ? ORDER BY created_at DESC", (did,))
    ]
    return json_reply({
        "device": device_public(db, registry, row),
        "traffic": traffic,
        "traffic_total": {"bytes_in": total_in, "bytes_out": total_out},
        "tunnel": tunnel,
        "clients": clients,
    }, request_id=_rid(request))


async def delete_client(request: Request, db: CloudDB,
                        device_id: str, client_id: str) -> Response:
    """`DELETE /api/devices/{id}/clients/{cid}`：撤销一台手机。"""
    if require_account(request, db) is None:
        return json_error("E2001", request_id=_rid(request))
    did, cid = _norm_id(device_id), _norm_id(client_id)
    if not did or not cid:
        return json_error("E1004", request_id=_rid(request))
    # 一次 JOIN 同时判"设备属于我"与"手机属于这台设备"：
    # 分开查会让"设备不是我的"返回 204（因为 UPDATE 影响 0 行），
    # 攻击者据此可以探测设备是否存在。
    row = db.query_one(
        "SELECT c.id FROM clients AS c JOIN devices AS d "
        "ON d.id = c.device_id WHERE c.id = ? AND c.device_id = ? "
        "AND d.account_id = ?", (cid, did, _account_id(request, db) or ""))
    if row is None:
        return json_error("E1004", request_id=_rid(request))
    db.execute("UPDATE clients SET revoked = 1 WHERE id = ?", (cid,))
    # 立刻推给电脑端（`docs/06` §13.2：轮询兜底仍然保留，但用户点"移除"
    # 的那一下就应该生效）。控制通道是尽力而为：电脑离线时这里什么也不做，
    # 等它上线后由 `GET /api/clients/revoked` 补齐。
    registry = getattr(getattr(request.app, "state", None), "registry", None)
    await cloud_control.call_agent(
        cloud_control.conn_for(registry, did), "revoke_clients", {"ids": [cid]})
    return no_content()


def revoked_clients(db: CloudDB, device_id: str) -> list[dict[str, Any]]:
    """该设备下已撤销的手机列表（供电脑端轮询，`docs/04` §3.6）。

    ★ 不做"增量"（没有 `revoked_at` 列）：撤销是**单调**的操作，电脑端
    拿到全量列表后自己求差集即可。加一列时间戳会把"什么时候撤的"变成
    又一份要维护的真相，而它唯一的用途是省一次全量比较——这张表最多
    几十行。
    """
    return [
        {"id": str(r["id"]), "name": r["name"],
         "revoked": True, "created_at": float(r["created_at"])}
        for r in db.query(
            "SELECT id, name, created_at FROM clients "
            "WHERE device_id = ? AND revoked = 1 ORDER BY created_at DESC",
            (device_id,))
    ]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _account_id(request: Request, db: CloudDB) -> str | None:
    account = require_account(request, db)
    return account["id"] if account is not None else None


def _days(raw: str | None) -> int:
    try:
        n = int(raw or 7)
    except (TypeError, ValueError):
        return 7
    return max(1, min(_MAX_DAYS, n))


def _traffic_series(db: CloudDB, device_id: str, days: int
                    ) -> list[dict[str, Any]]:
    """最近 `days` 天的流量，按 day 升序，**缺的天补 0**。

    补 0 是给前端画折线图用的：缺的日期直接跳过会让"停用三天"看起来
    像"连续三天都有流量"。
    """
    since = _day_offset_utc(-(days - 1))
    rows = db.query(
        "SELECT day, bytes_in, bytes_out FROM traffic "
        "WHERE device_id = ? AND day >= ? ORDER BY day ASC",
        (device_id, since))
    got = {str(r["day"]): r for r in rows}
    out: list[dict[str, Any]] = []
    for i in range(days):
        day = _day_offset_utc(-(days - 1 - i))
        r = got.get(day)
        out.append({
            "day": day,
            "bytes_in": int(r["bytes_in"]) if r is not None else 0,
            "bytes_out": int(r["bytes_out"]) if r is not None else 0,
        })
    return out


def _day_offset_utc(offset_days: int) -> str:
    """相对今天（UTC）偏移若干天的 `YYYY-MM-DD`。"""
    day = today_utc()
    try:
        y, m, d = (int(x) for x in day.split("-"))
    except (ValueError, TypeError):
        return day
    import datetime as _dt
    try:
        base = _dt.date(y, m, d) + _dt.timedelta(days=offset_days)
    except (ValueError, OverflowError):
        return day
    return base.isoformat()


def _active_reqs(mux: Any) -> int:
    if mux is None:
        return 0
    pending = getattr(mux, "_pending", None)
    if isinstance(pending, dict):
        return len(pending)
    return 0


def _rid(request: Request) -> str:
    return request.headers.get("x-request-id") or ""
