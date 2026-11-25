"""新设备配对的**电脑端批准**（P0-1，2026-11-24 渗透报告 A1.3）。

## 为什么要有这个模块

修之前，控制通道上的 `mint_client` 只要"云端问一句"就直接签一枚手机令牌：
拿到 `agent_token` 的假云端（渗透报告 A1.3）发一个控制帧，就换到了能读时间线
和原图的真令牌。云端被完全控制时，**没有任何一步**需要电脑前的人点头。

本模块把签发拆成三步，签发权留在电脑上：

1. `create_request()` —— 中继只能**发起**请求：记下（设备密钥、展示名、
   6 位核对码、有效期），返回 `pending`。**不签任何令牌。**
2. `decide()` —— 托盘（本机回环）列出待批准请求，用户核对手机上的
   核对码与设备指纹后点「批准」。只有这一步会生成 `client_id`。
3. `redeem()` —— 手机带着**同一个设备密钥**轮询：`pending` 时回核对码，
   `approved` 时才把令牌交给它，并**一次性**标记 `consumed`。

因此"中继单独发起申请"的最好结果是留下一条 `pending` 记录，永远换不到令牌。

## 为什么令牌不落库

批准时**不**生成令牌字符串，只记 `client_id` / `issued_at` / `token_expires_at`；
兑换时用 `issue_token(now=issued_at)` 重新算一遍。HMAC 令牌是纯函数，同样的
参数一定得到同样的串 —— 而把明文令牌写进 `data/photobridge.db` 等于把它
交给每一个能读这个文件的人（备份、网盘同步、用户自己拷走排障）。

## 为什么比对"核对码 + 指纹"

核对码由电脑端生成，经中继转给手机显示。中继若同时伪造一条自己的请求、
又把自己的核对码改成手机那枚，用户只比核对码就会批准错的对象。所以托盘上
同时显示 `sha256(设备密钥)` 的前 6 位：中继可以用别人的核对码，但没法让
自己的设备密钥算出同一个指纹。手机把这两项并排显示，用户对一眼即可。

## 已知边界（写清楚，不夸大）

* `device_key` 是手机自己在 localStorage 里生成的随机数，**没有硬件绑定**：
  清站点数据就换了一枚，等于换了一台"新设备"（这正是本模块想要的语义）。
* 中继仍能看到配对过程中的核对码与令牌（端到端保密是另一个立项，
  `docs/06` §13.3）。本模块只保证"新设备是否获得权限由电脑决定"。
* 用户不在电脑前时无法配对。这是**有意的**取舍：没有本地批准就等于
  把签发权交回云端，而这正是 A1.3 的成因。
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from typing import Any, Callable

from .identity import client_signing_secret

log = logging.getLogger(__name__)

__all__ = [
    "MAX_PENDING",
    "PAIR_EVENTS",
    "REQUEST_TTL",
    "PairError",
    "count_pending",
    "create_request",
    "decide",
    "fingerprint",
    "list_pending",
    "purge",
    "redeem",
    "set_notifier",
]

#: 请求的有效期（秒）。与配对码同一个量级：用户点「配对」之后应当**立刻**
#: 看到托盘提示，5 分钟是"他可能正在找鼠标"的余量。过期后必须重新发起 ——
#: 让一条无人处理的请求长期躺在那里，等于给中继一个随时可被批准的信箱。
REQUEST_TTL = 300.0

#: 同时挂起的请求上限。中继可以**无限次**发起请求（它本来就能转发任意控制
#: 帧），没有上限时托盘的待批准列表会被刷成一片，"偶发的一条"被淹没在噪声里。
#: 5 是"家里同时换手机"能想象到的上限；满了直接 429，先让用户清掉旧请求。
MAX_PENDING = 5

#: 事件类型。这张表是**审计**，不是业务状态：状态只回答"现在能不能兑换"，
#: 事件回答"谁在什么时候做过什么"，撤销、拒绝、兑换失败都要留痕。
PAIR_EVENTS = ("requested", "approved", "rejected", "denied",
               "consumed", "expired")

#: 请求 id / 设备密钥的形状。两者都只认小写十六进制：
#: * 统一小写是因为中继与手机各自可能大写一次，而 SQLite 的 TEXT 比较是
#:   逐字节的 —— "看起来一样但查不到"是这类系统里最难查的一类故障；
#: * 设备密钥固定 64 hex（32 字节），与 `crypto.getRandomValues(new Uint8Array(32))`
#:   一一对应；请求 id 固定 32 hex，与云端 `clients.id` 同宽。
_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_KEY_RE = re.compile(r"^[0-9a-f]{64}$")

_STATE_PENDING = "pending"
_STATE_APPROVED = "approved"
_STATE_REJECTED = "rejected"
_STATE_CONSUMED = "consumed"
_STATE_EXPIRED = "expired"

#: 通知钩子：托盘在启动时注册一个"有新的待批准请求"的回调。
#: 用模块级变量而不是把托盘 import 进来，是因为 `tray` 依赖 pystray 与
#: tkinter —— 服务端在没有桌面会话的机器上（计划任务、CI）也要能配对。
_notifier: Callable[[dict[str, Any]], None] | None = None


class PairError(Exception):
    """配对动作被拒。`code` 形如 `E50xx`，`status` 是回给云端的 HTTP 状态码。"""

    __slots__ = ("code", "message", "status")

    def __init__(self, code: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def set_notifier(fn: Callable[[dict[str, Any]], None] | None) -> None:
    """注册"有新的待批准请求"的通知函数（托盘用，可为 None）。"""
    global _notifier
    _notifier = fn


def fingerprint(device_key: str) -> str:
    """设备密钥的短指纹（6 hex），给用户肉眼比对用。

    取 sha256 的前 6 位而不是密钥本身的前 6 位：密钥是 bearer 形状的东西，
    屏幕上、日志里、用户拍的照片里都不该出现它的任何一段。
    """
    raw = str(device_key or "").strip().lower()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]


def _new_verify_code(db: Any, now: float) -> str:
    """生成一枚当前没被占用的 6 位核对码。

    冲突只可能发生在"同时挂着的几条请求之间"（概率 ~1e-6 × 5），但仍要重试：
    两行同码时用户在托盘上根本分不清哪条是手机上那台。
    """
    for _ in range(8):
        code = f"{secrets.randbelow(1_000_000):06d}"
        row = db.query_one(
            "SELECT 1 AS x FROM pair_requests"
            " WHERE state = ? AND verify_code = ? AND expires_at > ?",
            (_STATE_PENDING, code, now))
        if row is None:
            return code
    raise PairError("E5005", "核对码空间异常，请稍后重试", 503)


def _event(db: Any, event: str, *, request_id: str = "", client_id: str = "",
           device_key: str = "", device_name: str = "", source: str = "",
           result: str = "", now: float | None = None) -> None:
    """写一条配对事件。**任何失败都不能影响主流程**。

    审计写不进去时（磁盘满、库被锁）该继续签发还是该失败？继续：事件是
    "事后能不能查"，而拒绝签发是"用户现在能不能用" —— 让前者把后者拖下水
    只会把一次磁盘抖动放大成"新手机配不上对"。
    """
    try:
        db.execute(
            "INSERT INTO pair_events(ts, event, request_id, client_id,"
            " device_key, device_name, source, result)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (float(now if now is not None else time.time()), event,
             request_id[:32], client_id[:64], fingerprint(device_key),
             str(device_name or "")[:64], str(source or "")[:32],
             str(result or "")[:120]))
    except Exception:       # noqa: BLE001 - 见 docstring
        log.warning("写配对事件失败 event=%s", event, exc_info=True)


def _normalize(request_id: Any, device_key: Any) -> tuple[str, str]:
    rid = str(request_id or "").strip().lower()
    key = str(device_key or "").strip().lower()
    if not _ID_RE.match(rid):
        raise PairError("E5001", "配对请求 id 格式非法")
    if not _KEY_RE.match(key):
        raise PairError("E5001", "设备密钥格式非法（需 64 位十六进制）")
    return rid, key


def _row_to_dict(row: Any) -> dict[str, Any]:
    """把一行转成可 JSON 化的字典（**不含设备密钥本身**）。"""
    return {
        "request_id": str(row["request_id"]),
        "client_name": str(row["client_name"]),
        "fingerprint": fingerprint(str(row["device_key"])),
        "verify_code": str(row["verify_code"]),
        "state": str(row["state"]),
        "created_at": float(row["created_at"]),
        "expires_at": float(row["expires_at"]),
        "source": str(row["source"] or ""),
    }


def count_pending(db: Any, now: float | None = None) -> int:
    """当前还挂着的请求数（过期的先就地作废，避免虚高）。"""
    moment = float(now if now is not None else time.time())
    db.execute("UPDATE pair_requests SET state = ? WHERE state = ?"
               " AND expires_at <= ?", (_STATE_EXPIRED, _STATE_PENDING, moment))
    value = db.scalar("SELECT COUNT(*) AS n FROM pair_requests WHERE state = ?",
                      (_STATE_PENDING,), default=0)
    return int(value or 0)


def create_request(db: Any, *, device_id: str, device_key: str,
                   client_name: str = "", ttl_days: int = 90,
                   source: str = "cloud", request_id: str = "",
                   now: float | None = None) -> dict[str, Any]:
    """登记一条待批准的配对请求。**不签令牌、不写 devices 行。**

    返回 `{"state": "pending", "request_id", "verify_code", "fingerprint",
    "expires_at"}`。中继把 `verify_code`/`fingerprint` 转给手机显示，
    用户拿它和托盘上的那两行对一眼。
    """
    from shared import client_token as token_mod

    moment = float(now if now is not None else time.time())
    did = str(device_id or "").strip().lower()
    if not did:
        raise PairError("E5003", "本机没有云端设备 id，无法登记配对请求", 409)
    key = str(device_key or "").strip().lower()
    if not _KEY_RE.match(key):
        raise PairError("E5001", "设备密钥格式非法（需 64 位十六进制）")
    rid = str(request_id or "").strip().lower() or secrets.token_hex(16)
    if not _ID_RE.match(rid):
        raise PairError("E5001", "配对请求 id 格式非法")
    name = str(client_name or "").strip()[:64] or "手机"
    try:
        ttl = int(ttl_days)
    except (TypeError, ValueError) as exc:
        raise PairError("E5001", f"ttl_days 不是整数：{ttl_days!r}") from exc
    ttl = max(1, min(ttl, int(token_mod.MAX_TTL_DAYS)))

    code = _new_verify_code(db, moment)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE pair_requests SET state = ? WHERE state = ? AND expires_at <= ?",
            (_STATE_EXPIRED, _STATE_PENDING, moment))
        used = conn.execute(
            "SELECT COUNT(*) FROM pair_requests WHERE state = ?",
            (_STATE_PENDING,)).fetchone()[0]
        if int(used) >= MAX_PENDING:
            # 429 而不是 400：这是"现在不行、过一会儿或清掉旧的就行"，
            # 而中继把非 200 一律当失败处理，状态码只对日志与测试有意义。
            raise PairError("E5004", "待批准的配对请求过多，请先在电脑上处理",
                            429)
        exists = conn.execute(
            "SELECT 1 FROM pair_requests WHERE request_id = ?",
            (rid,)).fetchone()
        if exists is not None:
            raise PairError("E5001", "配对请求 id 重复")
        conn.execute(
            "INSERT INTO pair_requests(request_id, device_id, device_key,"
            " client_name, verify_code, ttl_days, created_at, expires_at,"
            " source, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (rid, did, key, name, code, ttl, moment,
             moment + REQUEST_TTL, str(source or "cloud")[:32], _STATE_PENDING))

    _event(db, "requested", request_id=rid, device_key=key,
           device_name=name, source=source, result="pending", now=moment)
    out = {"state": _STATE_PENDING, "request_id": rid, "verify_code": code,
           "fingerprint": fingerprint(key),
           "expires_at": moment + REQUEST_TTL, "client_name": name}
    log.info("收到配对请求 request=%s 来源=%s 名称=%s（等待本地批准）",
             rid, source, name)
    _notify(out)
    return out


def _notify(info: dict[str, Any]) -> None:
    fn = _notifier
    if fn is None:
        return
    try:
        fn(info)
    except Exception:       # noqa: BLE001 - 提醒失败不该让配对请求失败
        log.warning("配对提醒回调失败", exc_info=True)


def list_pending(db: Any, now: float | None = None) -> list[dict[str, Any]]:
    """托盘要展示的待批准请求（新的在前）。"""
    moment = float(now if now is not None else time.time())
    db.execute("UPDATE pair_requests SET state = ? WHERE state = ?"
               " AND expires_at <= ?", (_STATE_EXPIRED, _STATE_PENDING, moment))
    rows = db.query(
        "SELECT request_id, client_name, device_key, verify_code, state,"
        " created_at, expires_at, source FROM pair_requests"
        " WHERE state = ? ORDER BY created_at DESC", (_STATE_PENDING,))
    return [_row_to_dict(r) for r in rows]


def _load(db: Any, request_id: str) -> Any:
    return db.query_one(
        "SELECT request_id, device_id, device_key, client_name, verify_code,"
        " ttl_days, created_at, expires_at, issued_at, token_expires_at,"
        " decided_at, consumed_at, client_id, source, state"
        " FROM pair_requests WHERE request_id = ?", (request_id,))


def decide(db: Any, request_id: str, *, approve: bool,
           source: str = "local", device_key: str = "",
           now: float | None = None) -> dict[str, Any]:
    """批准或拒绝一条请求（只应由托盘/本机面板调用）。

    批准时生成 `client_id` 并记下 `issued_at`，**不生成令牌字符串**（见模块
    文档）。`device_key` 非空时要求与请求里的一致：将来若把批准入口挪到
    手机上，这一条就是"批准必须由同一台设备提交"的锚点。
    """
    moment = float(now if now is not None else time.time())
    rid = str(request_id or "").strip().lower()
    if not _ID_RE.match(rid):
        raise PairError("E5001", "配对请求 id 格式非法")

    issued: str | None = None
    stale = ""
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT state, expires_at, device_key, client_name, ttl_days"
            " FROM pair_requests WHERE request_id = ?", (rid,)).fetchone()
        if row is None:
            raise PairError("E5010", "配对请求不存在", 404)
        if str(row["device_key"]) != str(device_key or row["device_key"]).lower():
            raise PairError("E5005", "设备密钥不匹配", 403)
        state = str(row["state"])
        if state == _STATE_PENDING and float(row["expires_at"]) <= moment:
            conn.execute(
                "UPDATE pair_requests SET state = ? WHERE request_id = ?",
                (_STATE_EXPIRED, rid))
            state = _STATE_EXPIRED
        if state != _STATE_PENDING:
            # 409：这条请求已经不在"可批准"状态。重复点两次「批准」、
            # 或批准一个已经被手机兑换掉的请求，都落在这里。
            # ★ 这条 raise 必须在事务**提交之后**：在 `with` 里抛异常会回滚，
            # 上面那条"就地作废"的 UPDATE 也会被撤销，于是请求永远停在
            # pending —— 下次打开托盘它又回来了，用户点了又报错。
            stale = state
        elif approve:
            issued = secrets.token_hex(16)
            ttl = int(row["ttl_days"])
            conn.execute(
                "UPDATE pair_requests SET state = ?, decided_at = ?,"
                " client_id = ?, issued_at = ?, token_expires_at = ?"
                " WHERE request_id = ? AND state = ?",
                (_STATE_APPROVED, moment, issued, moment,
                 moment + ttl * 86400, rid, _STATE_PENDING))
        else:
            conn.execute(
                "UPDATE pair_requests SET state = ?, decided_at = ?"
                " WHERE request_id = ? AND state = ?",
                (_STATE_REJECTED, moment, rid, _STATE_PENDING))

    if stale:
        raise PairError("E5011", f"该请求当前状态为 {stale}，不能再处理", 409)

    event = "approved" if approve else "rejected"
    _event(db, event, request_id=rid, client_id=issued or "",
           device_name=str(row["client_name"]), source=source,
           result="批准" if approve else "拒绝", now=moment)
    log.info("配对请求 %s：%s（来源=%s）", rid, event, source)
    return {"request_id": rid, "state": event if approve else _STATE_REJECTED,
            "client_id": issued or ""}


def redeem(db: Any, request_id: str, device_key: str, *,
           now: float | None = None) -> dict[str, Any]:
    """手机轮询：`pending` 回核对码，`approved` 一次性换令牌。"""
    from shared import client_token as token_mod

    moment = float(now if now is not None else time.time())
    rid, key = _normalize(request_id, device_key)
    row = _load(db, rid)
    if row is None:
        raise PairError("E5010", "配对请求不存在", 404)

    stored_key = str(row["device_key"])
    if key != stored_key:
        # 设备密钥不对：**不**把请求作废（合法设备还要继续轮询），但要留痕。
        # 这是"中继拿着别人的 request_id 来偷令牌"的唯一形态。
        _event(db, "denied", request_id=rid, device_key=key, source="cloud",
               result="设备密钥不匹配", now=moment)
        log.warning("配对请求 %s：设备密钥不匹配，拒绝兑换", rid)
        raise PairError("E5005", "设备密钥不匹配", 403)

    state = str(row["state"])
    if state == _STATE_PENDING:
        if float(row["expires_at"]) <= moment:
            db.execute("UPDATE pair_requests SET state = ? WHERE request_id = ?",
                       (_STATE_EXPIRED, rid))
            _event(db, "expired", request_id=rid, device_key=key,
                   source="cloud", result="过期", now=moment)
            raise PairError("E5012", "配对请求已过期，请在电脑上重新发起", 410)
        return {"state": _STATE_PENDING, "request_id": rid,
                "verify_code": str(row["verify_code"]),
                "fingerprint": fingerprint(stored_key),
                "expires_at": float(row["expires_at"])}
    if state in (_STATE_REJECTED, _STATE_EXPIRED):
        raise PairError("E5012", "该配对请求已被拒绝或已过期，请重新发起",
                        403 if state == _STATE_REJECTED else 410)
    if state == _STATE_CONSUMED:
        raise PairError("E5013", "该配对请求已兑换过令牌", 409)
    if state != _STATE_APPROVED:
        raise PairError("E5011", f"配对请求状态异常：{state}", 409)

    ttl = int(row["ttl_days"])
    issued_at = float(row["issued_at"] or moment)
    token = token_mod.issue_token(
        client_signing_secret(db), client_id=str(row["client_id"]),
        device_id=str(row["device_id"]), ttl_days=ttl, now=issued_at,
        client_name=str(row["client_name"]))
    claimed = db.execute_rowcount(
        "UPDATE pair_requests SET state = ?, consumed_at = ?"
        " WHERE request_id = ? AND state = ?",
        (_STATE_CONSUMED, moment, rid, _STATE_APPROVED))
    if claimed != 1:
        # 两个轮询同时到达：后到的那个不该拿到第二枚令牌。
        raise PairError("E5013", "该配对请求已兑换过令牌", 409)
    _event(db, "consumed", request_id=rid, client_id=str(row["client_id"]),
           device_key=key, device_name=str(row["client_name"]), source="cloud",
           result="已兑换", now=moment)
    log.info("配对请求 %s 兑换成功 client=%s", rid, row["client_id"])
    return {"state": _STATE_APPROVED, "request_id": rid,
            "client_id": str(row["client_id"]), "token": token,
            "client_name": str(row["client_name"]),
            "expires_at": issued_at + ttl * 86400}


def purge(db: Any, now: float | None = None) -> int:
    """清理已完成/过期超过 7 天的请求，以及 90 天前的事件。

    返回删掉的请求行数。事件保留得比请求久：请求是可兑换的凭据，越早消失
    越好；事件是审计，用户过一个月回来问"那天谁配了我的电脑"要答得上来。
    """
    moment = float(now if now is not None else time.time())
    removed = db.execute_rowcount(
        "DELETE FROM pair_requests WHERE state != ? AND expires_at < ?"
        " AND COALESCE(consumed_at, decided_at, created_at) < ?",
        (_STATE_PENDING, moment, moment - 7 * 86400))
    db.execute("DELETE FROM pair_events WHERE ts < ?",
               (moment - 90 * 86400,))
    return int(removed)
