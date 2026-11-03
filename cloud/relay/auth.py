"""账号与账号令牌（P1-7 的一小半：登录/登出/me）。

## 为什么 `account_token` 不是 JWT

`docs/04` §1.2 建议 `client_token` 用 Ed25519 JWT，理由是"电脑端要能
离线验签"。`account_token` 没有这个需求——它只在云端用，云端就是唯一的
验证方。所以这里用最简单、最不会写错的东西：

* 32 字节 `secrets.token_urlsafe` 作为令牌；
* 库里只存 `sha256`（`accounts.token_hash` 是后加的列，见 `_migrate`）；
* 每次请求一次索引查询。

不引入 `cryptography` 依赖的另一个原因很实在：一期只装了 starlette +
websockets + pillow，加一个带 C 扩展的密码学库会让 PyInstaller 打包体积
与失败面都变大，而 Ed25519 的收益（电脑端离线验签）在 P1 里还用不上
——P1 的云端通路是**云端验完再转发**，电脑端验的是同一个令牌但不需要
独立验签能力。

> 这条决策记在 `docs/11` 的 Q-030 里。将来做 E2E 时再上 JWT。

## `account_token` 存哪里

`accounts` 表在原 DDL 里没有令牌列（`docs/05` §2.2 只定义了账号与
密码哈希）。这里加一张表而不是加一列：一个账号可以同时在手机、平板、
电脑浏览器上登录，每台一条记录才能单独登出。
"""
from __future__ import annotations

import time
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from ..relay import netutil, security
from ..relay.body import RequestBodyTooLarge, read_json_object
from ..relay.db import CloudDB
from ..relay.limiter import RateLimiter
from ..relay.reply import json_error, json_reply, no_content

__all__ = [
    "ACCOUNT_TOKEN_TTL_DAYS",
    "login",
    "logout",
    "me",
    "require_account",
    "create_account",
]

#: `account_token` 有效期（`docs/04` §1.2：短期，建议 7 天）。
ACCOUNT_TOKEN_TTL_DAYS: int = 7

#: 单 IP 登录尝试：10 次 / 10 分钟（`docs/04` §3.1）。
_LOGIN_LIMIT: int = 10
_LOGIN_WINDOW: float = 600.0

#: 单邮箱连续失败 5 次后锁 15 分钟（`docs/11` ISSUE-031）。
#:
#: 为什么除了按 IP 限流还要按**账号**计数：按 IP 的桶永远能被"换 IP"绕开，
#: 而一个账号的口令空间是固定的。反过来，按账号计数会带来"攻击者故意输错
#: 把主人锁在门外"的风险 —— 所以锁定时间取短（15 分钟）且**响应与密码错误
#: 完全一致**（E2005），不泄露"这个邮箱存在且被锁了"。
_ACCOUNT_MAX_FAILS: int = 5
_ACCOUNT_LOCK_SECONDS: float = 900.0

#: 进程内登录限流器。多 worker 部署会各自计数 ⇒ 必须单进程（见 main.py）。
_login_limiter = RateLimiter(_LOGIN_LIMIT, _LOGIN_WINDOW)

#: 按邮箱的限流器。与 `_login_limiter` 是**两个独立桶**：IP 桶防"一个来源
#: 猛撞"，邮箱桶防"一堆来源撞同一个账号"。
_email_limiter = RateLimiter(_LOGIN_LIMIT, _LOGIN_WINDOW)



# ---------------------------------------------------------------------------
# 账号
# ---------------------------------------------------------------------------


def create_account(db: CloudDB, email: str, password: str) -> dict[str, Any]:
    """建账号。**只给运维脚本与测试用**——公开注册接口不在 P1 范围。

    为什么不做注册接口：这是个"自己的照片自己看"的产品，账号是随
    服务一起建好的（`scripts/cloud_init.py`），开放注册只会带来垃圾账号
    与"谁都能占个邮箱"的问题，而没有任何用户体验上的收益。
    """
    email = (email or "").strip().lower()
    if not security.EMAIL_RE.match(email):
        raise ValueError("邮箱格式不正确")
    if not isinstance(password, str) or not (8 <= len(password) <= 128):
        raise ValueError("密码长度需在 8-128 之间")
    now = time.time()
    account_id = security.new_device_id()      # 32 位 hex，与 device_id 同规格
    pass_hash = security.hash_password(password)
    db.execute(
        "INSERT INTO accounts(id, email, pass_hash, created_at, plan) "
        "VALUES (?, ?, ?, ?, 'personal')",
        (account_id, email, pass_hash, now))
    return {"id": account_id, "email": email, "plan": "personal",
            "created_at": now}


def _issue_token(db: CloudDB, account_id: str, label: str) -> tuple[str, float]:
    token = security.new_token()
    now = time.time()
    expires = now + ACCOUNT_TOKEN_TTL_DAYS * 86400
    db.execute(
        "INSERT INTO account_tokens(token_hash, account_id, created_at, "
        "expires_at, label) VALUES (?, ?, ?, ?, ?)",
        (security.hash_token(token), account_id, now, expires, label[:64]))
    return token, expires


# ---------------------------------------------------------------------------
# 请求处理
# ---------------------------------------------------------------------------


async def login(request: Request, db: CloudDB) -> Response:
    """`POST /api/auth/login`（`docs/04` §3.1）。"""
    ip = _client_ip(request)
    allowed, retry = _login_limiter.hit(ip)
    if not allowed:
        return json_error("E4001", request_id=_rid(request),
                          retry_after=max(1, int(retry)))

    try:
        body = await _json_object(request)
    except RequestBodyTooLarge:
        return json_error("E1006", request_id=_rid(request))
    if body is None:
        return json_error("E1001", message="请求体必须是 JSON 对象",
                          request_id=_rid(request))
    email = body.get("email")
    password = body.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        return json_error("E1001", message="缺少 email 或 password",
                          request_id=_rid(request))
    email = email.strip().lower()
    if not (3 <= len(email) <= 254) or "@" not in email:
        return json_error("E1001", message="邮箱格式不正确",
                          request_id=_rid(request))
    if not (1 <= len(password) <= 256):
        return json_error("E1001", message="密码长度不正确",
                          request_id=_rid(request))

    # 按邮箱的第二道桶。放在字段校验之后、查库之前：它统计的是"对某个
    # 账号的尝试次数"，不含畸形请求。
    allowed_email, retry_email = _email_limiter.hit(email)
    if not allowed_email:
        return json_error("E4001", request_id=_rid(request),
                          retry_after=max(1, int(retry_email)))

    row = db.query_one(
        "SELECT id, email, pass_hash, created_at, plan, last_login, "
        "failed_count, locked_until FROM accounts WHERE email = ?", (email,))
    # ★ 账号不存在与密码错误必须走同一个分支，且耗时相近。
    # `verify_password` 在 stored 非法时立刻返回 False（微秒级），而正常
    # 校验要 30-80ms —— 直接对比会暴露"这个邮箱存在"。所以这里用一段
    # 固定的假哈希把时间对齐。
    stored = row["pass_hash"] if row is not None else _FAKE_HASH
    ok = security.verify_password(password, stored)
    now = time.time()
    locked_until = float(row["locked_until"] or 0.0) if row is not None else 0.0
    if row is not None and locked_until > now:
        # 锁定期间**照样校验口令**（上面的 `ok` 已经算过），但一律按失败
        # 处理：既不让锁定期变成"口令正确就能进"，也不让响应形态与
        # "密码错"有区别。
        return json_error("E2005", request_id=_rid(request))
    if row is None or not ok:
        if row is not None:
            _note_login_failure(db, str(row["id"]),
                                int(row["failed_count"] or 0), now)
        return json_error("E2005", request_id=_rid(request))

    if int(row["failed_count"] or 0) or locked_until:
        db.execute("UPDATE accounts SET failed_count = 0, locked_until = NULL "
                   "WHERE id = ?", (str(row["id"]),))

    account_id = str(row["id"])
    label = body.get("client_label")
    token, expires = _issue_token(db, account_id,
                                  label if isinstance(label, str) else "")
    db.execute("UPDATE accounts SET last_login = ? WHERE id = ?",
               (time.time(), account_id))
    devices_count = int(db.scalar(
        "SELECT COUNT(*) FROM devices WHERE account_id = ? AND revoked = 0",
        (account_id,)) or 0)
    return json_reply({
        "account_token": token,
        "expires_at": expires,
        "account": {"id": account_id, "email": row["email"],
                    "plan": row["plan"],
                    "created_at": float(row["created_at"])},
        "devices_count": devices_count,
    }, request_id=_rid(request))


async def logout(request: Request, db: CloudDB) -> Response:
    """`POST /api/auth/logout`（`docs/04` §3.2）：幂等 204。"""
    token = _bearer(request)
    if not token:
        return json_error("E2001", request_id=_rid(request))
    digest = security.hash_token(token)
    if not digest:
        return json_error("E2001", request_id=_rid(request))
    # 幂等：令牌本来就不存在也回 204。回 401 会让"重复点登出"的
    # 界面弹一个错误，而用户的心智里它已经登出了。
    db.execute("DELETE FROM account_tokens WHERE token_hash = ?", (digest,))
    return no_content()


async def me(request: Request, db: CloudDB) -> Response:
    """`GET /api/auth/me`：给前端判"要不要显示账号页"。"""
    account = require_account(request, db)
    if account is None:
        return json_error("E2001", request_id=_rid(request))
    devices_count = int(db.scalar(
        "SELECT COUNT(*) FROM devices WHERE account_id = ? AND revoked = 0",
        (account["id"],)) or 0)
    return json_reply({"account": account, "devices_count": devices_count},
                      request_id=_rid(request))


def require_account(request: Request, db: CloudDB) -> dict[str, Any] | None:
    """校验 `account_token`，返回账号信息（不含哈希）。失败返回 None。"""
    token = _bearer(request)
    if not token:
        return None
    digest = security.hash_token(token)
    if not digest:
        return None
    row = db.query_one(
        "SELECT t.token_hash, t.expires_at, a.id, a.email, a.created_at, "
        "a.plan FROM account_tokens AS t "
        "JOIN accounts AS a ON a.id = t.account_id "
        "WHERE t.token_hash = ?", (digest,))
    if row is None:
        return None
    expires = row["expires_at"]
    if expires is not None and float(expires) <= time.time():
        return None
    try:
        db.execute(
            "UPDATE account_tokens SET last_seen = ? WHERE token_hash = ? "
            "AND (last_seen IS NULL OR last_seen < ?)",
            (time.time(), digest, time.time() - 60.0))
    except Exception:       # noqa: BLE001 —— 走时更新失败不能影响鉴权
        pass
    return {"id": str(row["id"]), "email": row["email"],
            "plan": row["plan"], "created_at": float(row["created_at"])}


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

#: `scrypt` 的一个合法哈希，用于把"账号不存在"的耗时对齐到"密码错"。
_FAKE_HASH: str = security.hash_password(
    "photo-bridge-timing-equalizer-not-a-real-password")


def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization") or ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return raw.strip() if raw and " " not in raw else ""


def _rid(request: Request) -> str:
    return request.headers.get("x-request-id") or ""


def _client_ip(request: Request) -> str:
    """取客户端 IP。

    ★ 判据全在 `netutil`：只有当**直连对端**落在 `PB_CLOUD_TRUSTED_PROXIES`
    里时才看 `X-Forwarded-For`。部署拓扑是 `公网 → cloudflared / 80 端口
    转发 → 127.0.0.1:8000`，若一律取 `request.client.host`，所有手机都会
    共用 `127.0.0.1` 一个限流桶（`docs/11` ISSUE-030：攻击者每分钟 10 个假
    登录就能把主人锁在门外）；若一律信 `X-Forwarded-For`，攻击者换个头发
    一个假 IP 就能把 10 次/10 分钟变成无限次。
    """
    return netutil.client_ip(request)


def _note_login_failure(db: CloudDB, account_id: str, failed: int,
                        now: float) -> None:
    """记一次口令错误；达到阈值就把账号锁一段时间。

    `failed_count` 在锁定时清零：否则"锁定到期后再错一次"会立刻重新触发
    锁定，账号会变成永久锁死（用户唯一的自救办法是改库）。
    """
    failed += 1
    if failed >= _ACCOUNT_MAX_FAILS:
        db.execute("UPDATE accounts SET failed_count = 0, locked_until = ? "
                   "WHERE id = ?", (now + _ACCOUNT_LOCK_SECONDS, account_id))
        return
    db.execute("UPDATE accounts SET failed_count = ? WHERE id = ?",
               (failed, account_id))


async def _json_object(request: Request) -> dict[str, Any] | None:
    """读 JSON 对象，**不校验 Content-Type**。

    与电脑端 `ctx.json()` 的行为保持一致：客户端可能发
    `text/plain`（为了避开 CORS 预检），而这里拒掉它只会让某些前端
    莫名其妙地 400。安全边界不在 Content-Type 上，而在随后的字段校验。
    """
    return await read_json_object(request)
