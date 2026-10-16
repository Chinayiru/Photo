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
from typing import Any, Final

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
    "TOKEN_TTL_KEY",
    "token_ttl_days",
    "token_expiry",
    "cookie_max_age",
    "cookie_max_age_for",
    "COOKIE_MAX_AGE",
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

#: client_token 有效期的**默认值**（天）。AC-5.10 要求 90 天，
#: 且明写"过期策略可配置"—— 实际值由 `auth.token_ttl_days` 决定。
#:
#: 为什么仍然要有个过期：手机丢失后，用户很可能想不起来去电脑上点
#: "移除这台手机"。定期失效是最后一道兜底。
TOKEN_TTL_DAYS: int = 90

#: 配置键。`0` 表示永不过期。
TOKEN_TTL_KEY: Final = "auth.token_ttl_days"


def token_ttl_days(cfg: Any = None) -> int:
    """读 `auth.token_ttl_days`，非法值退回默认 90。

    `cfg` 为 None 时用 `config.get()` 取当前配置 —— 这让 `require_client`
    这类只拿得到 `ctx` 的路径也能读到配置，不必把 cfg 层层透传。

    ★ 负数按 0（永不过期）处理而不是当非法值退回 90：用户显式写了
    负数是明显想关掉过期，"退回 90"会让他以为设置没生效。
    """
    if cfg is None:
        from .config import get as _get_cfg
        try:
            cfg = _get_cfg()
        except Exception:       # noqa: BLE001 —— 配置未初始化时用默认值
            return TOKEN_TTL_DAYS
    raw = cfg.get(TOKEN_TTL_KEY, TOKEN_TTL_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        log.warning("auth.token_ttl_days 非法：%r，退回 %d 天",
                    raw, TOKEN_TTL_DAYS)
        return TOKEN_TTL_DAYS
    return max(0, days)


def token_expiry(now: float, cfg: Any = None) -> float | None:
    """签发时刻 `now` 对应的绝对过期时间；永不过期返回 `None`。"""
    days = token_ttl_days(cfg)
    return None if days <= 0 else now + days * 86400


#: 浏览器对 cookie `Max-Age` 的实际上限是 400 天（RFC 6265bis 建议），
#: 超过会被夹到 400 天。用它表示"永不过期的 token"。
COOKIE_MAX_AGE: Final = 400 * 86400


def cookie_max_age_for(exp: float | None, now: float | None = None) -> int:
    """按绝对过期时刻算出 cookie 该活多久（秒）。`None` = 永不过期。

    刚配对完还没有设备行时用这个（`expires_at` 刚算出来，还没 INSERT）。
    """
    if exp is None:
        return COOKIE_MAX_AGE
    return max(1, int(exp - (now if now is not None else time.time())))


def cookie_max_age(row: Any, now: float | None = None) -> int:
    """据设备行算出 cookie 该活多久（秒）。

    ★ 不能像原来那样硬编码 `90 天`。两个方向都会出错：

      * token 还剩 1 天而 cookie 写 90 天 → cookie 活得比 token 长，
        浏览器一直带着一枚服务端必然拒绝的凭证，每次都是 401 再清掉，
        用户看到的是缩略图间歇性变灰块。
      * token 永不过期（`expires_at is NULL`，老库升级上来的行）
        而 cookie 只写 90 天 → 90 天后 `<img>` 突然全部灰掉。
        JS 请求能靠 Bearer 头自愈，但用户看到的是"照片全灰了"。
    """
    return cookie_max_age_for(row["expires_at"] if row is not None else None, now)


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


def _penalize_active_codes(db: Any, now: float) -> None:
    """把一次**猜错**记到当前活跃的配对码上。

    ## 为什么需要这个函数（否则 AC-5.5 是一句空话）

    `consume_pair_code` 是按 `code_hash` 主键查行的，而**猜错的码在表里
    没有行** —— 于是 `row is None` 直接走到 E1002 返回，`tries` 永远
    没有机会递增。全仓唯一会写 `tries` 的地方曾是测试自己
    （`UPDATE pair_codes SET tries = ?` 直接造前置状态），所以
    "错 5 次锁 10 分钟"看起来有实现、测试也全绿，实际上在生产里
    一次都没生效过：攻击者可以无限次猜。

    计数必须挂在**某个对猜错也存在的行**上。正确的那枚码就是这样的行：
    用户点"添加手机"时托盘生成它，攻击者攻击的也正是它。

    ## 为什么递增**所有**活跃码而不是最新那一枚

    `docs/06-安全设计文档.md` §8.2 场景 B 自己指出了按 code 锁定的绕过：
    若同时有 N 枚活跃码，攻击者可以每枚试 5 次，预算变成 5N 次。递增
    全部活跃码让这 N 枚**共享**同一个 5 次预算，绕过随之关闭。
    正常使用下同时只有一枚活跃码（`panel_pair_code` 每次都会先
    `revoke_all_codes`），所以这个加强不改变正常行为。

    ⚠️ 这确实带来一个 DoS 面（攻击者故意错 5 次就能锁住用户 10 分钟），
    该残余风险在 `docs/06` 的 R-3 里已被显式接受，且用户点托盘
    "重新生成配对码"可立即解除 —— 新码的 `tries` 从 0 开始。
    """
    # 已锁定的活跃码优先报 429：比 E1002「码不存在」更接近真实原因，
    # 也让攻击者无法靠"继续猜"把锁定状态刷成别的错误码。
    row = db.query_one(
        "SELECT locked_until FROM pair_codes"
        " WHERE used_at IS NULL AND expires_at >= ?"
        " ORDER BY created_at DESC LIMIT 1",
        (now,),
    )
    if row is None:
        # 没有任何活跃码可记（用户还没生成，或已全部过期）。
        # 这种情况由 handlers/auth.py 的按 IP 限速兜底。
        return
    locked_until = row["locked_until"]
    if locked_until is not None and locked_until > now:
        raise AppError(E4004, f"pair code locked until {locked_until:.0f}")

    db.execute(
        "UPDATE pair_codes SET tries = tries + 1"
        " WHERE used_at IS NULL AND expires_at >= ?",
        (now,),
    )
    # 到量就落锁。`tries >= ?` 用更新后的值判断，所以这次 UPDATE 与上一条
    # 合起来等于"第 5 次失败击锁"。SQLite 的 UPDATE 里不能"用更新后的
    # tries 去算 locked_until"再写回同一列，故必须分成两条语句。
    rows = db.execute_rowcount(
        "UPDATE pair_codes SET locked_until = ?"
        " WHERE used_at IS NULL AND tries >= ?"
        "   AND (locked_until IS NULL OR locked_until <= ?)",
        (now + PAIR_LOCK_SECONDS, PAIR_MAX_TRIES, now),
    )
    if rows:
        # 锁定是这次尝试造成的 → 回 429 而不是 400「码不存在」。
        # 前端据此显示"请 10 分钟后再试"，而 400 会让用户以为码输错了
        # 而继续重输，每一次重输都在延长/刷新这个锁定。
        raise AppError(E4004, "pair code tries exhausted")


def consume_pair_code(
    db: Any, code: str, *, device_name: str | None = None, cfg: Any = None,
) -> tuple[str, str]:
    """校验并消费一个配对码，成功时返回 `(client_id, token)`。

    失败一律抛 `AppError`，且**错误码要能指导用户下一步动作**：
    码不存在 → 让用户核对电脑上显示的数字；码过期 → 让用户去电脑上
    重新生成；被锁 → 告诉用户等 10 分钟或直接重新生成（后者更快）。

    ⚠️ 校验顺序不能反：**先判锁定，再判过期，最后判尝试次数**。
    反过来的话攻击者可以用一个已锁定的码继续刷 `tries`，
    虽然结果都是拒绝，但会让"错误次数"统计失去意义。

    ⚠️ 猜错的码**查不到行**，所以它走的是 `_penalize_active_codes`
    而不是本函数里的 `tries` 分支 —— 详见该函数的 docstring，
    那里记着"错 5 次锁 10 分钟"曾经完全没生效的结构性原因。
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
        # 猜错的码在表里没有行，所以计数只能记到"当前活跃的那枚码"上
        # （见 `_penalize_active_codes`）。审计不在这里写：本模块是纯逻辑层，
        # 审计统一由 handlers 层负责，免得两处都写造成重复记录。
        _penalize_active_codes(db, now)
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
    # AC-5.10：过期时刻**必须落库**。
    #
    # 原来这里只把 `now + 90 天` 拼进响应体给客户端看，库里一个字段都
    # 没有 —— 于是 token 永远有效，而客户端拿到的 `expires_at` 只是
    # 一个装饰性的数字。用户手机丢了、又没想起来点"移除设备"，
    # 那枚 token 就永久开门。
    expires_at = token_expiry(now, cfg)

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
            "INSERT INTO devices(id, name, token_hash, last_seen, created_at,"
            "                    revoked, expires_at)"
            " VALUES (?, ?, ?, ?, ?, 0, ?)",
            (client_id, device_name or "未命名手机", hash_token(token), now, now,
             expires_at),
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
        "SELECT id, name, revoked, created_at, last_seen, expires_at"
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

    # AC-5.10：token 到期必须拒绝，并且**要把失效的 cookie 清掉**。
    #
    # 不清 cookie 的话浏览器会一直带着它重试：用户重新配对拿到新 token
    # 后，`<img>` 仍优先发旧 cookie，缩略图永远 401，而 JS 请求用的是
    # 新 Bearer 头所以一切正常 —— 症状是"时间线有照片但全是灰块"，
    # 用户完全无从理解，也无法自助恢复。（与 401 E2002 那条同因。）
    #
    # `expires_at is None` 表示永不过期：老库升级上来的行就是这个值。
    # 强行给它们安一个过期时刻会让所有已配对的手机在某个时刻集体
    # 掉线 —— 那是比"永不触发过期"更糟的故障。
    exp = row["expires_at"]
    if exp is not None and exp <= time.time():
        ctx.set_cookies.append((CLIENT_COOKIE, "", 0))
        raise AppError(E2002, f"client token expired at {exp:.0f}")

    if via_header:
        # ★ 自愈路径：客户端还在用 Authorization 头（老版本前端、
        # 或刚配对完还没刷新页面）。顺手补一个 cookie，让同一浏览器
        # 里的 `<img>` / `EventSource` 立刻能用，无需用户重新配对。
        ctx.set_cookies.append(
            (CLIENT_COOKIE, token, cookie_max_age(row)))

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
