"""游客访问（ISSUE-063）：邀请、会话、令牌。

## 三样东西，别混

| 对象 | 存在哪 | 生命周期 | 谁看得见 |
|---|---|---|---|
| **邀请链接** | 明文只在 `POST /api/guest/invites` 的响应里出现一次；库里存 `sha256` | 到 `expires_at`；被兑换一次即失效（G11 解释 A） | 主人（`GET /api/guest/invites`） |
| **会话** | `guest_sessions`（`client_id` = 令牌的 `sub`） | `min(邀请剩余有效期, 2h)`，可被主人踢（G13） | 主人（`GET /api/guest/sessions`） |
| **令牌** | 和主人令牌同一个格式（`pbc1.…`），多两个 claim：`role=guest`、`inv` | 同上 | 游客的浏览器（cookie `pb_guest` / Bearer） |

为什么邀请码与令牌分开：邀请码是**一次性**的，令牌是**可重复用**的。
合成一个东西就必须在"链接转发给别人先点了"时给出含糊的语义（G11 选 A 的
理由）；分开之后"链接已用"与"会话还在"可以同时成立，各自判定。

## 为什么令牌走 `pbc1` 而不是新格式

`auth._verify_cloud_token()` 已经有一整套验签 + 设备绑定 + 撤销缓存逻辑，
新增一套格式就要在 `auth.py` 里分叉出第二条验签路径 —— 而"两条路径里
有一条忘了校验 `dev`"就是跨设备越权。加 claim 的代价只有几行。

`role` 缺省 `owner`：老令牌（没有这个 claim）继续当主人令牌用，不需要
任何迁移；反过来，凡是 `role=guest` 的令牌**必须**同时带 `inv`，
否则无法回答"这枚令牌属于哪次邀请"（撤销和过期都靠它）。
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import time
from typing import Any, Final

from shared import client_token as token_mod

from .errors import AppError, E1020, E1021, E1022, E1023, E1024

log = logging.getLogger(__name__)

__all__ = [
    "GUEST_COOKIE",
    "GUEST_MAX_TTL",
    "MAX_ACTIVE_INVITES",
    "MAX_ONLINE_GUESTS",
    "GUEST_SUBDIR",
    "clamp_minutes",
    "create_invite",
    "list_invites",
    "revoke_invite",
    "redeem",
    "session_active",
    "issue_session_token",
    "list_sessions",
    "kick_session",
    "invite_state",
]

#: 游客令牌的 cookie 名。与主人的 `pb_token` 分开：**同一个浏览器可能既是
#: 主人又刚点过朋友的链接**，共用一个 cookie 名就会互相覆盖，症状是
#: "回到自己的页面变成游客"（或者反过来，最坏的情况是游客拿到主人身份）。
GUEST_COOKIE: Final = "pb_guest"

#: 会话 TTL 上限（G13）：`min(邀请剩余有效期, 2 小时)`。
GUEST_MAX_TTL: Final = 7200.0

#: G14：有效邀请 ≤ 5，在线游客 ≤ 10。
MAX_ACTIVE_INVITES: Final = 3   # 默认值；实际由 config.guest_max_online 传入
MAX_ONLINE_GUESTS: Final = 3    # 默认值；实际由 config.guest_max_online 传入

#: G12：自定义分钟数钳制区间（5 分钟 ~ 7 天）。
MIN_MINUTES: Final = 5
MAX_MINUTES: Final = 10080

#: G5：游客上传固定落 `游客上传/<YYYY-MM>/`，不跟主人的 `upload.subdir` 走。
GUEST_SUBDIR: Final = "游客上传"

#: 邀请码明文前缀。带前缀是为了在日志/聊天记录里**一眼认出**这是邀请码
#: （而不是某个配对码或令牌），排障时不必逐个去比对哈希。
_INVITE_PREFIX: Final = "pbg1."


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


#: 邀请/兑换请求体的上限（字节）。
#:
#: `handlers/guest.redeem` 用 `ctx.json(limit=GUEST_BODY_LIMIT)` 读 body ——
#: 这里曾经漏定义，于是兑换请求直接 500/E3001（审计 07f03d 的发证链缺陷）。
#: 邀请码本身只有一百多字节，给到和 uploads/complete 同档的 16 KB 足够。
GUEST_BODY_LIMIT = 16 * 1024


def _now() -> float:
    return time.time()


def hash_token(token: str) -> str:
    """明文码 → 库里存的值。用 sha256 而非 PBKDF2：明文是 32 字节随机数，
    不存在字典攻击空间（和 6 位配对码那种"必须慢哈希"的情形不同）。"""
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


def new_invite_token() -> str:
    return _INVITE_PREFIX + secrets.token_urlsafe(32)


def clamp_minutes(minutes: Any) -> int:
    """把用户给的有效期钳到 `[5, 10080]`；超上限**报错**而不是静默改成 7 天。

    静默钳制的失败模式很坏：主人以为链接有 30 天，实际 7 天就失效，
    朋友在第八天点开看到"链接已过期"，而主人坚信自己发的是 30 天。
    """
    try:
        n = int(minutes)
    except (TypeError, ValueError):
        raise AppError(E1020, "有效期必须是分钟数") from None
    if n < MIN_MINUTES or n > MAX_MINUTES:
        raise AppError(
            E1020, f"有效期必须在 {MIN_MINUTES}–{MAX_MINUTES} 分钟之间")
    return n


# ---------------------------------------------------------------------------
# 邀请
# ---------------------------------------------------------------------------


def invite_state(row: Any, now: float | None = None) -> str:
    """`active` / `used` / `expired` / `revoked`（顺序即判定优先级）。

    `used` 排在 `expired` 之前：一个"用掉了又过期了"的邀请，对主人最有用的
    信息是"它被用过了"（说明有人的会话可能还在跑）。
    """
    t = now if now is not None else _now()
    if row["revoked_at"]:
        return "revoked"
    if row["first_used_at"]:
        return "used"
    if float(row["expires_at"]) <= t:
        return "expired"
    return "active"


def _active_invites(conn: sqlite3.Connection | Any, now: float) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM guest_invites"
        " WHERE revoked_at IS NULL AND first_used_at IS NULL AND expires_at > ?",
        (now,)).fetchone()
    return int(row[0] if isinstance(row, tuple) else row["n"])


def create_invite(db: Any, *, device_id: str, label: str, minutes: int,
                  via: str, max_active: int = MAX_ACTIVE_INVITES) -> dict[str, Any]:
    """生成一条邀请。返回**唯一一次**能看到明文的字典。

    `max_active` 由调用方从配置传入（`Config.guest_max_online`）。条数判定与
    插入在同一个 `BEGIN IMMEDIATE` 事务里：并发生成时只有一个能过线。
    """
    now = _now()
    minutes = clamp_minutes(minutes)
    cap = max(1, int(max_active))
    with db.transaction() as conn:
        if _active_invites(conn, now) >= cap:
            raise AppError(
                E1021,
                f"最多同时保留 {cap} 条未使用的邀请，请先在电脑上撤销一条",
            )
        invite_id = secrets.token_hex(16)
        token = new_invite_token()
        expires_at = now + minutes * 60
        conn.execute(
            "INSERT INTO guest_invites(id, device_id, token_hash, label,"
            " created_at, expires_at, created_via)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (invite_id, device_id, hash_token(token),
             str(label or "")[:64], now, expires_at, via),
        )
    log.info("生成游客邀请 id=%s 有效期=%d 分钟 via=%s", invite_id, minutes, via)
    return {
        "invite_id": invite_id,
        "token": token,
        "label": str(label or "")[:64],
        "created_at": now,
        "expires_at": expires_at,
        "minutes": minutes,
    }


def list_invites(db: Any, *, include_revoked: bool = False) -> list[dict[str, Any]]:
    """邀请列表。**默认不返回已撤销的条目**。

    ★ 过滤放在查询层而不是前端：撤销是终态、不可恢复，撤销项对主人没有操作
      价值；但留在响应里让前端 `v-if` 隐藏，等于仍然把"这条存在过"发给浏览器。
    ★ 记录本身**保留**（`revoked_at` + 审计 `guest_revoke`）：撤销要留痕，物理
      删除会断审计链。排障时用 `include_revoked=True`。
    """
    now = _now()
    # 两条**字面量** SQL，不拼字符串：`check_lint` 规则 3 禁止把非占位符内容拼进
    # SQL 首参（拼接是注入的唯一入口，哪怕是内部常量也不开口子）。
    if include_revoked:
        rows = db.query(
            "SELECT id, label, created_at, expires_at, first_used_at, revoked_at,"
            " guest_client_id, created_via FROM guest_invites"
            " ORDER BY created_at DESC")
    else:
        rows = db.query(
            "SELECT id, label, created_at, expires_at, first_used_at, revoked_at,"
            " guest_client_id, created_via FROM guest_invites"
            " WHERE revoked_at IS NULL ORDER BY created_at DESC")
    out = []
    for r in rows:
        out.append({
            "invite_id": r["id"],
            "label": r["label"],
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
            "first_used_at": r["first_used_at"],
            "revoked_at": r["revoked_at"],
            "guest_client_id": r["guest_client_id"],
            "created_via": r["created_via"],
            "state": invite_state(r, now),
        })
    return out


def revoke_invite(db: Any, invite_id: str) -> bool:
    """撤销一条邀请，**连带终止**它名下的会话（G13「撤销即刻失效」）。"""
    now = _now()
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE guest_invites SET revoked_at = ?"
            " WHERE id = ? AND revoked_at IS NULL", (now, invite_id))
        if cur.rowcount == 0:
            return False
        conn.execute(
            "UPDATE guest_sessions SET revoked_at = ?"
            " WHERE invite_id = ? AND revoked_at IS NULL", (now, invite_id))
    log.info("撤销游客邀请 id=%s（含其会话）", invite_id)
    return True


# ---------------------------------------------------------------------------
# 兑换（一次性）
# ---------------------------------------------------------------------------


def _session_ttl(invite_expires_at: float, now: float) -> float:
    return min(float(invite_expires_at) - now, GUEST_MAX_TTL)


def redeem(db: Any, *, token: str, device_id: str, via: str,
           max_online: int = MAX_ONLINE_GUESTS) -> dict[str, Any]:
    """用邀请码换一个会话。一次性：第二次调用报"已被使用"（G11 A）。"""
    now = _now()
    th = hash_token(token)
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT id, device_id, label, expires_at, first_used_at, revoked_at"
            " FROM guest_invites WHERE token_hash = ?", (th,)).fetchone()
        if row is None:
            raise AppError(E1020, "邀请链接无效，请向主人索取新的链接")
        if row["revoked_at"]:
            raise AppError(E1023, "该邀请已被主人撤销")
        if row["first_used_at"]:
            # 已经换过会话 → G11 A：链接失效，但**已有会话照旧有效**。
            raise AppError(E1022, "该链接已被使用，请向主人索取新的链接")
        if float(row["expires_at"]) <= now:
            raise AppError(E1021, "该链接已过期，请向主人索取新的链接")

        ttl = _session_ttl(row["expires_at"], now)
        if ttl <= 0:  # pragma: no cover —— 上面已判过期
            raise AppError(E1021, "该链接已过期")

        # ★ 计数与插入必须落在**同一个** BEGIN IMMEDIATE 事务里。先查数量再
        #   插入的两步写法，在"两个游客几乎同时兑换"时会双双读到 2、双双插入，
        #   最后 4 人在线。IMMEDIATE 立刻取写锁，后者排队等前者提交后再读，
        #   于是被下面这句挡下。
        online = conn.execute(
            "SELECT COUNT(*) AS n FROM guest_sessions"
            " WHERE revoked_at IS NULL AND expires_at > ?", (now,)).fetchone()
        cap = max(1, int(max_online))
        if int(online["n"]) >= cap:
            # 消息里**不带人数**：还没进门的人没资格知道主人家里有几个客人。
            raise AppError(E1024, "当前访问人数已满，请稍后再试")

        client_id = secrets.token_hex(16)
        session_token = issue_session_token(
            db, client_id=client_id, invite_id=row["id"],
            device_id=device_id, ttl=ttl, now=now)
        conn.execute(
            "INSERT INTO guest_sessions(client_id, token_hash, invite_id,"
            " device_id, label, created_at, expires_at, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (client_id, hash_token(session_token), row["id"], device_id,
             row["label"] or "", now, now + ttl, now))
        # 一次性：占位。用条件更新而不是"先 SELECT 再 UPDATE"，
        # 两个请求同时 Redeem 时只有一个能把 first_used_at 抢到非空。
        cur = conn.execute(
            "UPDATE guest_invites SET first_used_at = ?, guest_client_id = ?"
            " WHERE id = ? AND first_used_at IS NULL", (now, client_id, row["id"]))
        if cur.rowcount == 0:
            raise AppError(E1022, "该链接已被使用，请向主人索取新的链接")
    log.info("游客兑换成功 invite=%s client=%s via=%s", row["id"], client_id, via)
    return {
        "client_id": client_id,
        "invite_id": row["id"],
        "label": row["label"] or "",
        "token": session_token,
        "expires_at": now + ttl,
    }


# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------


def session_active(db: Any, client_id: str,
                   now: float | None = None) -> tuple[bool, str]:
    """会话是否还能用。返回 `(ok, reason)`，reason ∈ ok/unknown/revoked/
    expired/invite_revoked/invite_expired。

    ★ 每次请求都查（G13）：只看 cookie 的 TTL 不够 —— 主人撤销邀请后，
    游客浏览器里的 cookie 仍然"没过期"，接口会一直放行到 2 小时后。
    状态在库里，所以重启程序也不影响判断。
    """
    t = now if now is not None else _now()
    row = db.query_one(
        "SELECT s.expires_at, s.revoked_at, i.expires_at AS inv_exp,"
        " i.revoked_at AS inv_rev"
        "  FROM guest_sessions s JOIN guest_invites i ON i.id = s.invite_id"
        " WHERE s.client_id = ?", (client_id,))
    if row is None:
        return False, "unknown"
    if row["revoked_at"] or row["inv_rev"]:
        return False, "revoked"
    if float(row["expires_at"]) <= t:
        return False, "expired"
    if float(row["inv_exp"]) <= t:
        return False, "invite_expired"
    return True, "ok"


def issue_session_token(db: Any, *, client_id: str, invite_id: str,
                        device_id: str, ttl: float,
                        now: float | None = None) -> str:
    """签一枚 `role=guest` 的令牌。密钥与主人令牌**同一把**
    （`client_signing_secret`，从不上传云端），所以云端库泄露也签不出令牌。"""
    from .auth import _signing_secret

    secret = _signing_secret(db, None)
    return token_mod.issue_token(
        secret,
        client_id=client_id,
        device_id=device_id,
        ttl_days=max(ttl, 60.0) / 86400.0,
        now=now if now is not None else _now(),
        client_name="游客",
        role="guest",
        invite_id=invite_id,
    )


def list_sessions(db: Any) -> list[dict[str, Any]]:
    now = _now()
    rows = db.query(
        "SELECT s.client_id, s.invite_id, s.label, s.created_at, s.expires_at,"
        " s.last_seen, s.revoked_at, i.label AS inv_label"
        "  FROM guest_sessions s JOIN guest_invites i ON i.id = s.invite_id"
        " ORDER BY s.created_at DESC")
    out = []
    for r in rows:
        out.append({
            "client_id": r["client_id"],
            "invite_id": r["invite_id"],
            "label": r["inv_label"] or r["label"] or "",
            "created_at": r["created_at"],
            "expires_at": r["expires_at"],
            "last_seen": r["last_seen"],
            "revoked_at": r["revoked_at"],
            "online": bool(
                not r["revoked_at"] and float(r["expires_at"]) > now),
        })
    return out


def kick_session(db: Any, client_id: str) -> bool:
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE guest_sessions SET revoked_at = ?"
            " WHERE client_id = ? AND revoked_at IS NULL", (_now(), client_id))
        return cur.rowcount > 0


def touch_session(db: Any, client_id: str) -> None:
    """更新 `last_seen`（「主人能看到谁在线」的依据）。失败不影响请求。"""
    try:
        db.execute("UPDATE guest_sessions SET last_seen = ? WHERE client_id = ?",
                   (_now(), client_id))
    except Exception as e:  # noqa: BLE001
        log.debug("更新游客 last_seen 失败 client=%s: %s", client_id, e)
