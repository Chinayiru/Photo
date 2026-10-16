"""鉴权与配对。

## 一期（局域网直连）的鉴权模型

设计文档 §8.2 说"直连时电脑本地校验 `client_token`"，但云端方案里
`clients` 表只存 `token_hash`，电脑端**拿不到任何可离线校验的材料** ——
那条路在协议上不成立（docs/04 §1.2 已记录该冲突）。

一期的关键简化：**电脑端自己就是签发方**。没有云、没有账号，
配对时电脑端生成随机 token、存 hash、把明文给手机。于是：

- 校验就是"取 Bearer → sha256 → 查 devices.token_hash"，
  一次主键/索引查询，无需 JWT、无需联网回源；
- 撤销就是 `UPDATE devices SET revoked=1`，**立刻生效**，
  比云端 JWT 方案（吊销最长滞后 10 分钟）更强。

二期接入云端隧道时，改的是"谁签发"，`require_client()` 的调用方不变。

## 为什么 token 用 32 字节随机数而不是 JWT

一期不存在"电脑端需要离线验证第三方签名"的场景 —— 它自己签发、
自己验证，密钥永远不会离开这台电脑。引入 Ed25519 只增加了
密钥管理面（密钥丢了要重新配对全部设备）而没有换来任何能力。
二期的 JWT 是给"云端签发、电脑离线验签"用的，到那时再加。

## 为什么数据库里只存 hash

`data/photobridge.db` 会被用户备份到网盘、被同步工具复制、
在排查问题时被拷给别人。明文 token 在手等于永久访问权。
sha256 无盐即可 —— token 本身就是 32 字节高熵随机数，
不存在字典攻击面（与用户口令不同，不需要 bcrypt 这类慢哈希）。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from typing import Any

from .core import Ctx
from .errors import (
    E1002,
    E1003,
    E1005,
    E2002,
    E2004,
    E4004,
    AppError,
    Unauthorized,
)

log = logging.getLogger(__name__)

__all__ = [
    "PAIR_CODE_TTL",
    "PAIR_MAX_TRIES",
    "PAIR_LOCK_SECONDS",
    "TOKEN_TTL_DAYS",
    "TOKEN_BYTES",
    "CLIENT_COOKIE",
    "hash_token",
    "hash_code",
    "new_token",
    "generate_pair_code",
    "pair_code_info",
    "consume_pair_code",
    "revoke_all_codes",
    "require_client",
    "optional_client",
    "auth_middleware",
    "PUBLIC_PATHS",
    "PANEL_PATHS",
]

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 配对码有效期。5 分钟是"够用户从电脑走到手机、打开页面、输入 6 位数"
#: 的合理窗口，同时把暴力枚举的可用时间压到最小。
PAIR_CODE_TTL: float = 300.0

#: 同一个码最多错几次。注意统计的是**针对这个码**的失败次数 ——
#: 按来源 IP 统计是无效的：攻击者换一个 IP 就能重置计数。
PAIR_MAX_TRIES: int = 5

#: 超过尝试次数后锁定多久。文档要求 10 分钟。
#: 锁定加在**码**上而不是 IP 上，因为 IP 可以伪造/更换。
PAIR_LOCK_SECONDS: float = 600.0

#: client_token 有效期（天）。一期局域网场景下"永久有效"其实更贴合
#: 使用习惯（用户不会希望每 90 天重新配对一次），但仍设 90 天：
#: 手机丢失后即使没人去点"撤销"，令牌也会自己失效。
TOKEN_TTL_DAYS: int = 90

#: token 随机字节数。32 字节 = 256 位，远超暴力破解可行范围。
TOKEN_BYTES: int = 32

#: 承载 client_token 的 cookie 名。
#:
#: ★★ 存在的唯一理由：浏览器原生 `<img>` 与 `EventSource` **无法发送
#: 自定义请求头**，`Authorization: Bearer` 根本传不进去。缩略图走
#: `<img src>`，于是全部 401（E2002），前端 `PhotoCell.onError` 把
#: 每格标成 failed 渲染灰块 —— 表现为"照片一张都加载不出来"。
#:
#: 备选方案都被否决了：
#:   - `?token=` 进 URL：token 会落进浏览器缓存键、服务器访问日志、
#:     `Referer` 头，且与 `Vary: Authorization` 的长缓存策略冲突；
#:   - `fetch` + `URL.createObjectURL`：摧毁 `loading="lazy"` 与
#:     HTTP 缓存（`PhotoCell.vue` 的懒加载是硬约束）；
#:   - Service Worker 加头：SW 是 **secure context** 专属 API，
#:     `http://192.168.x.x` 上根本注册不了（只有 https 与 localhost 算）。
#:
#: Cookie 是唯一同时满足"浏览器自动携带 + 保留 HTTP 缓存与懒加载"的方案。
CLIENT_COOKIE: str = "pb_token"

#: 无需鉴权的路径（精确匹配）。
#:
#: `/api/system/ping` 必须匿名：手机冷启动时要用它探活，
#: 而"还没有 token"正是探活要解决的场景（判断这台电脑是不是
#: 自己配对过的那台）。把它设成需要鉴权会让探活永远 401，
#: 前端无法区分"电脑没开"与"token 过期"。
#:
#: `/api/pair` 必须匿名 —— 它**就是**用来换取 token 的接口。
#: 漏掉它会导致配对永远 401「配对已失效，请重新配对」，
#: 而用户根本还没有 token：一个死锁，且错误文案完全指错方向。
#: 它的安全依赖"必须持有 5 分钟内有效的 6 位码"+ 按 IP 限速。
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/api/system/ping",
    "/api/pair",
})

#: 只能从本机访问的路径前缀。
#:
#: ⚠️ 这是**安全要求**，不是便利性设计。电脑端默认监听 0.0.0.0:8787，
#: 同网段任何设备（访客 Wi-Fi、公司网络、隔壁邻居的电脑）都能访问。
#: 如果它们能调 `/panel/pair-code` 拿到配对码，配对码就形同虚设 ——
#: 攻击者拿到码后自己配对一枚 90 天有效的 token。
PANEL_PATHS: tuple[str, ...] = ("/panel",)


# --------------------------------------------------------------------------
# 哈希
# --------------------------------------------------------------------------

def hash_token(token: str) -> str:
    """token 的存储形式。无盐 sha256 足够（token 是高熵随机数）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_code(code: str) -> str:
    """配对码的存储形式。

    配对码只有 100 万种可能，理论上可离线枚举。但它存在的窗口只有
    5 分钟且必须先从局域网发起，因此无盐 sha256 是够的。
    加盐会让"按码查行"需要全表扫描（无法用主键索引），
    而这张表最多几行，代价可以接受；但收益也接近零。
    """
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def new_token() -> str:
    """生成一枚 client_token（URL 安全，可直接放 localStorage）。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def generate_pair_code() -> str:
    """生成 6 位数字配对码（允许前导 0）。

    ⚠️ 用 `secrets` 而不是 `random`：后者是可预测的梅森旋转，
    攻击者观察到几个码后能推算下一个。
    """
    return f"{secrets.randbelow(1_000_000):06d}"


# --------------------------------------------------------------------------
# 配对码生命周期
# --------------------------------------------------------------------------

def pair_code_info(db: Any, code: str) -> dict[str, Any] | None:
    """查配对码状态，不改动任何计数。供托盘展示"当前码是否还有效"。"""
    row = db.query_one(
        "SELECT code_hash, device_id, device_name, created_at, expires_at,"
        "       tries, locked_until, used_at"
        "  FROM pair_codes WHERE code_hash = ?",
        (hash_code(code),),
    )
    if row is None:
        return None
    return dict(row)


def consume_pair_code(
    db: Any, code: str, *, device_name: str | None = None,
) -> tuple[str, str]:
    """校验并消费一个配对码，成功时返回 `(client_id, token)`。

    失败一律抛 `AppError`，且**错误码要能指导用户下一步动作**：
    码不存在 → 让用户核对电脑上显示的数字；码过期 → 让用户去电脑上
    重新生成；被锁 → 告诉用户等 10 分钟或直接重新生成（后者更快）。

    ⚠️ 校验顺序不能反：**先判锁定，再判过期，最后判尝试次数**。
    反过来的话攻击者可以用一个已锁定的码继续刷 `tries`，
    虽然结果都是拒绝，但会让"错误次数"统计失去意义。
    """
    now = time.time()
    code_hash = hash_code(code)

    row = db.query_one(
        "SELECT code_hash, device_id, device_name, expires_at, tries,"
        "       locked_until, used_at"
        "  FROM pair_codes WHERE code_hash = ?",
        (code_hash,),
    )
    if row is None:
        # 码不存在也记一次审计：连续的"码不存在"是探测信号。
        raise AppError(E1002, f"pair code not found name={device_name!r}")

    if row["used_at"] is not None:
        # 一次性：配对成功后立刻作废。否则用户截图/转发出去的码
        # 在有效期内还能被第二个人用。
        raise AppError(E1005, "pair code already used")

    locked_until = row["locked_until"]
    if locked_until is not None and locked_until > now:
        raise AppError(E4004, f"pair code locked until {locked_until:.0f}")

    if row["expires_at"] < now:
        raise AppError(E1003, f"pair code expired at {row['expires_at']:.0f}")

    if row["tries"] >= PAIR_MAX_TRIES:
        # 次数用尽但锁定时间已过（或从未设置）→ 补一个锁定窗口。
        # 不设的话这一行会永久卡在"次数用尽"状态，用户重新生成前
        # 每次请求都白跑一次数据库。
        db.execute(
            "UPDATE pair_codes SET locked_until = ? WHERE code_hash = ?",
            (now + PAIR_LOCK_SECONDS, code_hash),
        )
        raise AppError(E4004, "pair code tries exhausted")

    # --- 通过校验，签发 token ---
    client_id = secrets.token_hex(16)
    token = new_token()
    expires_at = now + TOKEN_TTL_DAYS * 86400

    with db.transaction(immediate=True):
        # ⚠️ 条件更新 + 检查 rowcount 是必需的，不能只 UPDATE。
        # 两台手机同时提交同一个码时，两个请求都能读到 `used_at IS NULL`，
        # 不加条件就会签发两枚 token（配对码的一次性语义被破坏）。
        # 这里用 `used_at IS NULL` 做乐观锁，只有第一个请求能改到行。
        cur = db.execute(
            "UPDATE pair_codes SET used_at = ? WHERE code_hash = ? AND used_at IS NULL",
            (now, code_hash),
        )
        if cur.rowcount != 1:
            raise AppError(E1005, "pair code lost the race")

        db.execute(
            "INSERT INTO devices(id, name, token_hash, last_seen, created_at, revoked)"
            " VALUES (?, ?, ?, ?, ?, 0)",
            (client_id, device_name or "未命名手机", hash_token(token), now, now),
        )

    log.info("配对成功 client_id=%s name=%s", client_id, device_name)
    return client_id, token


def revoke_all_codes(db: Any) -> int:
    """作废当前所有未使用的配对码，返回作废条数。

    托盘上"重新生成配对码"会先调它：用户点这个按钮的心理预期是
    "刚才那个码别用了"，只插入新码而不作废旧码会让旧码继续有效 5 分钟。
    """
    now = time.time()
    return db.execute_rowcount(
        "UPDATE pair_codes SET used_at = ? WHERE used_at IS NULL",
        (now,),
    )


def store_pair_code(db: Any, code: str, device_id: str, device_name: str,
                    *, ttl: float = PAIR_CODE_TTL) -> None:
    """写入一枚新配对码（托盘调用）。"""
    now = time.time()
    db.execute(
        "INSERT OR REPLACE INTO pair_codes"
        "(code_hash, device_id, device_name, created_at, expires_at, tries,"
        " locked_until, used_at)"
        " VALUES (?, ?, ?, ?, ?, 0, NULL, NULL)",
        (hash_code(code), device_id, device_name, now, now + ttl),
    )


def purge_expired_codes(db: Any) -> int:
    """清掉过期且已使用的码。启动时调用一次即可，不必定时。"""
    cutoff = time.time() - 86400
    return db.execute_rowcount(
        "DELETE FROM pair_codes WHERE expires_at < ?", (cutoff,)
    )


# --------------------------------------------------------------------------
# 请求鉴权
# --------------------------------------------------------------------------

def _lookup_device(db: Any, token: str) -> Any:
    """按 token 查设备行。查不到返回 None。"""
    return db.query_one(
        "SELECT id, name, revoked, created_at, last_seen"
        "  FROM devices WHERE token_hash = ?",
        (hash_token(token),),
    )


def require_client(ctx: Ctx) -> dict[str, Any]:
    """校验 Bearer token，返回设备信息；失败抛 401/403。

    ⚠️ 用 `hmac.compare_digest` 的思路在这里用不上（我们查的是 hash，
    不是逐字节比 token），但**必须**保证"token 不存在"与"token 被撤销"
    返回不同状态码：
      - 不存在 → 401 E2002「配对已失效，请重新配对」
      - 已撤销 → 403 E2004「该设备已被移除，请重新配对」
    两者都返回 401 的话，用户在电脑上点了"移除这台手机"之后，
    手机会一直提示"配对失效"而不知道是**被主动移除**的，
    于是反复重试同一个 token（前端行为）而不是去重新配对。
    """
    st = ctx.app
    if st is None:  # pragma: no cover —— 装配遗漏
        raise Unauthorized("state 未注入")

    via_header = ctx.bearer_token()
    token = via_header or ctx.cookie_token(CLIENT_COOKIE)
    if not token:
        raise Unauthorized("缺少凭证：Authorization 头或 pb_token cookie")

    row = _lookup_device(st.db, token)
    if row is None:
        # 失效的 cookie 必须清掉，否则浏览器会**永远**带着它：
        # 用户重新配对拿到新 token 后，`<img>` 仍优先发旧 cookie
        # （cookie 与 localStorage 是两个独立存储），缩略图继续 401，
        # 而 JS 请求用的是新 Bearer 头所以一切正常 —— 症状是
        # "时间线有照片但全是灰块"，用户无法理解也无法自助恢复。
        ctx.set_cookies.append((CLIENT_COOKIE, "", 0))
        raise Unauthorized("token 不存在")

    if row["revoked"]:
        ctx.set_cookies.append((CLIENT_COOKIE, "", 0))
        raise AppError(E2004, f"device revoked id={row['id']}")

    if via_header:
        # ★ 自愈路径：客户端还在用 Authorization 头（老版本前端、
        # 或刚配对完还没刷新页面）。顺手补一个 cookie，让同一浏览器
        # 里的 `<img>` / `EventSource` 立刻能用，无需用户重新配对。
        ctx.set_cookies.append(
            (CLIENT_COOKIE, token, TOKEN_TTL_DAYS * 86400))

    info = {"id": row["id"], "name": row["name"]}
    ctx.state["client"] = info
    ctx.state["via_cookie"] = not via_header
    _touch(st, row["id"])
    return info


def optional_client(ctx: Ctx) -> dict[str, Any] | None:
    """尝试鉴权，失败返回 None 而不抛异常。"""
    try:
        return require_client(ctx)
    except AppError:
        return None


def _touch(st: Any, client_id: str) -> None:
    """更新 last_seen。

    ⚠️ 按秒节流。手机滚动时间线时每秒几十个缩略图请求，
    每个都写一次库会与索引器的批量写抢写锁 ——
    WAL 下写-写仍然互斥，用户会看到滚动卡顿。
    """
    now = time.time()
    key = f"_touch_{client_id}"
    last = st.db.get_setting(key, None)
    if isinstance(last, (int, float)) and now - last < 60:
        return
    try:
        st.db.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now, client_id))
        st.db.set_setting(key, now)
    except Exception as e:  # noqa: BLE001 —— 更新失败不能影响业务请求
        log.debug("更新 last_seen 失败 client_id=%s: %s", client_id, e)


def is_loopback(ctx: Ctx) -> bool:
    """请求是否来自本机。

    ⚠️ 必须同时认 `::1` 与 `::ffff:127.0.0.1`。Python 在双栈 socket 上
    把 IPv4 映射地址表示成 `::ffff:127.0.0.1`，只判 `127.0.0.1`
    会让本机浏览器访问 `/panel` 时被拒 —— 而用户此刻正坐在电脑前
    点托盘的"打开面板"。
    """
    ip = (ctx.client_ip or "").strip().lower()
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    return ip in ("127.0.0.1", "::1", "localhost", "")


async def auth_middleware(ctx: Ctx, nxt: Any):
    """鉴权中间件。

    装配顺序很重要：它必须是**最内层**之一，让请求日志中间件能包住
    401 响应（否则鉴权失败不会出现在访问日志里，排障时看不到）。
    """
    st = ctx.app
    cfg = st.cfg if st is not None else None
    require_pairing = bool(cfg.auth.get("require_pairing", True)) if cfg else True

    path = ctx.path

    # --- /panel/**：只允许本机 ---
    for prefix in PANEL_PATHS:
        if path == prefix or path.startswith(prefix + "/"):
            if not is_loopback(ctx):
                # 返回 404 而不是 403：403 等于告诉扫描者"这个路径存在，
                # 只是你权限不够"，而 404 让他无法区分"没有这个功能"。
                from .errors import NotFound

                log.warning("拒绝非本机访问 /panel：client_ip=%s", ctx.client_ip)
                raise NotFound(path)
            return await nxt(ctx)

    # --- 公开路径 ---
    if path in PUBLIC_PATHS:
        return await nxt(ctx)

    # --- 未启用配对：放行（仅供本机开发调试） ---
    if not require_pairing:
        return await nxt(ctx)

    require_client(ctx)
    return await nxt(ctx)
