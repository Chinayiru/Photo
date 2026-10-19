"""配对码（`docs/04` §3.5、`docs/03` §3.5）。

## 谁生成、谁显示、谁校验

| 环节 | 谁做 | 为什么 |
|---|---|---|
| 生成码 | **云端**（`POST /api/devices/{id}/paircode`，`agent_token` 鉴权） | 手机必须把码交给云端换令牌；云端不认识这个码就无法校验 |
| 显示码 | 电脑端托盘 | 二维码是"电脑展示、手机扫"，链路最短 |
| 校验码 | 云端（`POST /api/devices/{id}/pair`，`account_token` 鉴权） | 云端是码的权威副本 |

## 两道独立的闸门

1. **每码 5 次**（`pair_codes.tries` + `locked_until`）——防止有人在
   5 分钟窗口内枚举那 100 万个组合。
2. **每设备 5 次 / 每 IP 20 次**（进程内）——因为攻击者可以不断请求
   新码来重置第 1 道闸门。第 1 道只保护"这一个码"，第 2 道保护"这台
   设备"。

两道都要，少一道就有一条现成的绕过路径。选 20 而不是 5（IP 维度）
的理由与电脑端一致：一家人共用一个出口 IP，一个人输错三次就锁住
全家说不过去；4 倍阈值仍然把爆破速率压到 20 次/10 分钟。

## 为什么码用 `secrets` 而不是 `random`

配对码是**唯一**能换来 90 天照片访问权的东西。`random` 的 Mersenne
Twister 在观察到 624 个输出后可以完全预测；而攻击者能观察到的正是
"每次生成/失败的码"。`secrets.randbelow` 走的是 `os.urandom`。
（`scripts/check_lint.py` 规则 19 就是因为这类问题而存在的。）
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from ..relay import security
from ..relay.auth import require_account
from ..relay.db import CloudDB
from ..relay.devices import find_device_by_agent_token
from ..relay.reply import json_error, json_reply
from shared import client_token as shared_token

__all__ = [
    "PAIR_LOCK_SECONDS",
    "issue_pair_code",
    "redeem_pair_code",
    "purge_expired_codes",
]

#: 码有效期（`docs/04` §3.5：5 分钟）。
PAIR_LOCK_SECONDS: float = security.PAIR_LOCK_SECONDS

#: 每设备失败次数达到这个值就锁 10 分钟。
_DEVICE_MAX_TRIES: int = security.PAIR_MAX_TRIES
#: 每 IP 阈值放宽 4 倍（一家人共用出口 IP）。
_IP_MAX_TRIES: int = security.PAIR_MAX_TRIES * 4
#: 进程内尝试计数。`(kind, key) -> [count, locked_until]`
_ATTEMPTS: dict[tuple[str, str], list[float]] = {}


# ---------------------------------------------------------------------------
# 发码（电脑端调用）
# ---------------------------------------------------------------------------


async def issue_pair_code(request: Request, db: CloudDB) -> Response:
    """`POST /api/devices/{id}/paircode`。

    ★ 鉴权用 **`agent_token`**（`docs/03` §3.5.4），不是 `account_token`：
    发码的是电脑自己（托盘要立刻显示），而电脑手里只有 `agent_token`。
    用账号令牌会强迫电脑保存用户的邮箱密码，那是更糟的设计。
    """
    device = _device_by_agent(request, db)
    if device is None:
        return json_error("E2002", request_id=_rid(request))
    device_id = str(device["id"])
    if int(device["revoked"] or 0):
        return json_error("E2006", request_id=_rid(request))

    now = time.time()
    # 同一台电脑上一枚还没过期的码直接作废：托盘上只该显示一枚有效的码，
    # 留着旧码等于把一个仍然可用的凭据留在屏幕上（用户截图求助时会带上）。
    db.execute(
        "DELETE FROM pair_codes WHERE device_id = ? AND used_at IS NULL",
        (device_id,))
    code = _new_unique_code(db)
    db.execute(
        "INSERT INTO pair_codes(code, device_id, expires_at, tries, "
        "created_at) VALUES (?, ?, ?, 0, ?)",
        (code, device_id, now + security.PAIR_CODE_TTL, now))
    return json_reply({
        "code": code,
        "device_id": device_id,
        "device_name": device["name"],
        "expires_in": int(security.PAIR_CODE_TTL),
        "expires_at": now + security.PAIR_CODE_TTL,
    }, status=201, request_id=_rid(request))


def _new_unique_code(db: CloudDB) -> str:
    """生成一个当前不冲突的 6 位码。

    冲突概率是 100 万分之一，但 `code` 是主键——撞上会直接抛
    `IntegrityError` 让整个请求 500。重试 8 次够了：连续 8 次撞上
    说明"未使用的码已经有几十万枚"，那才是真问题。
    """
    for _ in range(8):
        code = security.new_pair_code()
        row = db.query_one("SELECT 1 AS x FROM pair_codes WHERE code = ?",
                           (code,))
        if row is None:
            return code
    raise RuntimeError("配对码空间即将耗尽，请清理过期的 pair_codes")


# ---------------------------------------------------------------------------
# 换令牌（手机调用）
# ---------------------------------------------------------------------------


async def redeem_pair_code(request: Request, db: CloudDB,
                           device_id: str) -> Response:
    """`POST /api/devices/{id}/pair`（`docs/04` §3.5）。"""
    account = require_account(request, db)
    if account is None:
        return json_error("E2001", request_id=_rid(request))

    did = (device_id or "").strip().lower()
    device = db.query_one(
        "SELECT id, account_id, name, revoked, last_lan, agent_token "
        "FROM devices WHERE id = ?", (did,))
    # ★ 不存在 / 不属于我 → 同一个 404（不泄露设备存在性）。
    if device is None or str(device["account_id"]) != account["id"]:
        return json_error("E1004", request_id=_rid(request))
    if int(device["revoked"] or 0):
        return json_error("E2006", request_id=_rid(request))

    body = await _json_object(request)
    if body is None:
        return json_error("E1001", message="请求体必须是 JSON 对象",
                          request_id=_rid(request))
    code = body.get("code")
    client_name = body.get("client_name")
    if not isinstance(code, str) or not security.is_valid_pair_code(code):
        return json_error("E1001", message="配对码必须是 6 位数字",
                          request_id=_rid(request))
    if not isinstance(client_name, str) or not (1 <= len(client_name.strip()) <= 64):
        return json_error("E1001", message="手机名称需为 1-64 字符",
                          request_id=_rid(request))
    client_name = client_name.strip()

    ip = _client_ip(request)
    locked = _check_locked(did, ip)
    if locked is not None:
        return json_error("E4004", request_id=_rid(request),
                          retry_after=max(1, int(locked)))

    now = time.time()
    row = db.query_one(
        "SELECT code, device_id, expires_at, tries, locked_until, used_at "
        "FROM pair_codes WHERE code = ?", (code.strip(),))
    if row is None:
        _bump_failure(did, ip)
        return json_error("E1002", message="配对码不存在",
                          request_id=_rid(request))
    if str(row["device_id"]) != did:
        # 码是真的，但不是这台电脑的。不回 404 而回 403 —— 用户手里
        # 那个码确实存在，让他去"另一台电脑上重新生成"，而不是以为打错了。
        _bump_failure(did, ip)
        return json_error("E1016", message="该配对码属于另一台电脑",
                          request_id=_rid(request))
    if row["used_at"] is not None:
        _bump_failure(did, ip)
        return json_error("E1005", message="配对码已被使用",
                          request_id=_rid(request))
    lock_until = row["locked_until"]
    if lock_until is not None and float(lock_until) > now:
        return json_error("E4004", request_id=_rid(request),
                          retry_after=int(float(lock_until) - now) + 1)
    if float(row["expires_at"]) <= now:
        _bump_failure(did, ip)
        return json_error("E1003", message="配对码已过期",
                          request_id=_rid(request))

    # 走到这里就成功。**先用条件 UPDATE 抢下这个码**，再签发令牌：
    # 两条并发请求同时通过上面的检查是可能的（同一个码在两台手机上
    # 同时点），条件 UPDATE 的影响行数就是那把锁。
    client_id = security.new_device_id()
    expires_at = now + security.CLIENT_TOKEN_TTL_DAYS * 86400
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE pair_codes SET used_at = ? WHERE code = ? "
            "AND used_at IS NULL", (now, code.strip()))
        if cur.rowcount != 1:
            return json_error("E1005", message="配对码已被使用",
                              request_id=_rid(request))
        token = _issue_client_token(device, client_id, client_name, now,
                                    expires_at)
        conn.execute(
            "INSERT INTO clients(id, device_id, name, token_hash, "
            "last_seen, created_at, revoked, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (client_id, did, client_name, security.hash_token(token),
             now, now, expires_at))

    _clear_failures(did, ip)
    import json as _json
    try:
        lan_ips = _json.loads(device["last_lan"] or "[]")
    except (TypeError, ValueError):
        lan_ips = []
    if not isinstance(lan_ips, list):
        lan_ips = []
    # 令牌只在**这一次**响应里出现（库里只有哈希）。前端必须立刻存进
    # localStorage，否则只能重新配对。
    return json_reply({
        "client_token": token,
        "client_id": client_id,
        "device_id": did,
        "device_name": device["name"],
        "expires_at": expires_at,
        "lan_ips": [str(x) for x in lan_ips][:16],
        "server_public_key": None,      # 二期 E2E 预留
    }, request_id=_rid(request))


def _issue_client_token(device: Any, client_id: str, client_name: str,
                        now: float, expires_at: float) -> str:
    """签发一枚可被**电脑端离线验签**的 client_token（`shared/client_token.py`）。

    ## 为什么不能再用 `security.new_token()`（32 字节随机串）

    一期令牌是在电脑上签发的，电脑自己 `devices` 表里就有那一行，所以
    "查库比对 sha256"能成立。二期手机是**云端**发令牌，电脑的库里没有
    这一行，于是手机第一次走进隧道就会被 `require_client` 判 401
    （`docs/04-接口设计文档.md:99-112` 把这个缺口写在了明面上）。

    电脑端不能为此回源云端：用户之所以要"局域网直连"，就是因为不想让
    每次滑动时间线都经过公网。断网时回源失败 = 局域网也废了。

    于是换成 HMAC 令牌：密钥是 `sha256(agent_token)`，云端手里就是
    `devices.agent_token` 那一列，电脑端手里是 `agent_token` 明文，
    两边算出来同一个值。电脑端 `require_client` 先查本地库（一期令牌
    兼容），查不到再验签（二期令牌）——两条路并存，老配对不受影响。

    ## 过期时间

    令牌自带的 `exp` 与 `clients.expires_at` 必须**同源**（都用同一个
    `expires_at` 算），否则会出现"云端认为还有效、电脑认为已过期"，
    表现是令牌有效期内随机 401。这里用 `expires_at` 反推天数。
    """
    secret = str(device["agent_token"] or "")
    ttl_days = max(1, int((expires_at - now) // 86400))
    return shared_token.issue_token(
        secret, client_id=client_id, device_id=str(device["id"]),
        ttl_days=ttl_days, now=now, client_name=client_name)


def purge_expired_codes(db: CloudDB, *, older_than: float = 86400.0) -> int:
    """清掉过期一天以上的码。

    不设定时任务：每次发码时顺手清一次，成本是一条 DELETE，而"过期码
    越积越多"这件事没有任何外部信号会被发现。
    """
    cutoff = time.time() - older_than
    try:
        # `CloudDB.execute()` 已经返回 `cur.rowcount`，这里**不能**调
        # `execute_rowcount`——`CloudDB` 没有那个方法（那是电脑端
        # `backend/app/db.py:Database` 的 API）。第一版写错了，症状是
        # 每小时 housekeeping 打一行 `清理过期配对码失败
        # AttributeError: 'CloudDB' object has no attribute
        # 'execute_rowcount'`，而过期码从来没被清掉。
        return db.execute(
            "DELETE FROM pair_codes WHERE expires_at < ?", (cutoff,))
    except sqlite3.Error:       # pragma: no cover —— 清理失败不影响发码
        return 0


# ---------------------------------------------------------------------------
# 尝试计数
# ---------------------------------------------------------------------------


def _check_locked(device_id: str, ip: str) -> float | None:
    now = time.time()
    for key in (("dev", device_id), ("ip", ip)):
        slot = _ATTEMPTS.get(key)
        if slot is not None and slot[1] > now:
            return slot[1] - now
    return None


def _bump_failure(device_id: str, ip: str) -> None:
    now = time.time()
    _bump(("dev", device_id), _DEVICE_MAX_TRIES, now)
    _bump(("ip", ip), _IP_MAX_TRIES, now)


def _bump(key: tuple[str, str], limit: int, now: float) -> None:
    slot = _ATTEMPTS.get(key)
    if slot is None or slot[1] <= now:
        slot = [0.0, 0.0]
        _ATTEMPTS[key] = slot
    slot[0] += 1
    if slot[0] >= limit:
        slot[0] = 0.0
        slot[1] = now + PAIR_LOCK_SECONDS
        _gc_attempts(now)


def _clear_failures(device_id: str, ip: str) -> None:
    _ATTEMPTS.pop(("dev", device_id), None)
    _ATTEMPTS.pop(("ip", ip), None)


def _gc_attempts(now: float) -> None:
    """顺手清掉早就解锁的计数，避免被大量 IP 撑大内存。"""
    if len(_ATTEMPTS) < 256:
        return
    stale = [k for k, v in _ATTEMPTS.items() if v[1] and v[1] <= now]
    for k in stale:
        _ATTEMPTS.pop(k, None)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _device_by_agent(request: Request, db: CloudDB) -> Any:
    raw = request.headers.get("authorization") or ""
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()
    if not token:
        return None
    return find_device_by_agent_token(db, token)


def _client_ip(request: Request) -> str:
    client = request.client
    return (client.host if client is not None else "") or "unknown"


def _rid(request: Request) -> str:
    return request.headers.get("x-request-id") or ""


async def _json_object(request: Request) -> dict[str, Any] | None:
    import json

    try:
        raw = await request.body()
    except Exception:       # noqa: BLE001
        return None
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None
