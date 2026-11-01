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
import re
import secrets
import time
from typing import Any, Final

from .core import Ctx
from .netguard import check_host
from shared import client_token as token_mod
from .errors import (
    E1002,
    E1003,
    E1005,
    E1022,
    E1023,
    E2002,
    E2003,
    E2010,
    E2004,
    E4004,
    AppError,
    Unauthorized,
)

from .identity import (
    CLOUD_TOKEN_TABLE,
    GUEST_COOKIE,
    SigningSecretUnavailable,
    _candidate_secrets,
    _cloud_cfg,
    cloud_device_id,
    _ensure_cloud_table,
    _signing_secret,
    hash_token,
    legacy_tunnel_secret,
    legacy_window_open,
    sync_revoked_clients,
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
    "sync_revoked_clients",
    "SigningSecretUnavailable",
    "auth_middleware",
    "PUBLIC_PATHS",
    "PANEL_PATHS",
    "panel_http_enabled",
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
    #: 配对页要在**没有任何令牌**时就把「家里 Wi-Fi / VPN / 云端」三条入口
    #: 摆出来，所以这个接口必须匿名。它只返回本机地址 —— 扫一遍网段就能
    #: 拿到的东西，藏起来没有意义（判据见 `entrances.py` 的模块注释）。
    "/api/system/entrances",
    "/api/pair",
    #: 游客兑换（ISSUE-063）：朋友点链接时**还没有任何凭证**，手上的
    #: 邀请码也不是令牌（`looks_like_token()` 会把它判成垃圾，正确地拦在
    #: 回源之前）。所以这个端点必须匿名 —— 它的安全依赖"邀请码是一次性
    #: 的 32 字节随机串 + 只在云端通路接受 + 按 IP 限速"。
    "/api/guest/redeem",
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


def _issue_client_token(now: float, client_id: str,
                        device_name: str | None, cfg: Any = None,
                        db: Any = None) -> str:
    """签发一枚 client_token，**配了云端时签成两端都能验的 HMAC 令牌**。

    ## 为什么局域网配对也必须签 HMAC 令牌

    产品里手机只有一条配对路径：在家连局域网、扫托盘二维码、
    `POST /api/pair` 换令牌（`frontend/src/stores/auth.ts` 只投递这一个
    接口，云端 `/api/devices/{id}/pair` 是"账号后台"用的，手机 UI 走不到）。
    而云端 `/d/**` 只认它自己 `clients` 表里的 `token_hash` —— 局域网配对
    签出来的随机串从没进过云端库，于是手机上拿到的令牌一出门就 401
    `E2002配对已失效`。

    实测（`build/_probe_lan_token_on_cloud.py`，真 uvicorn 全链路）：

        LAN  token→LAN    = [200] 时间线正常
        LAN  token→CLOUD  = [401] E2002 配对已失效，请重新配对

    结果是"在家能用、出门即失效"，而 P1 的全部价值就在出门那一段。

    ## 密钥从哪来（两端凭什么算出同一个值）

    `sha256(agent_token)`：云端手里是 `devices.agent_token` 那一列
    （`cloud/relay/db.py` 的 DDL 存的就是这个 sha256），电脑端手里是
    `cloud.agent_token` 明文，`token_mod.secret_for_agent_token()` 算一次
    就得到同一个 hex。**实测两端逐字节相同**，所以谁都不必回源。

    ## 没配云端时仍然回退到随机串

    一台纯局域网的机器没有 `agent_token`，也就没有可共享的密钥。
    此时照旧用 `new_token()`：本地库里有这一行，`_lookup_device` 查得到。
    不这样回退的话，`token_mod.issue_token` 会因 `secret` 为空抛
    `ValueError`，配对直接 500。

    ## 为什么 `token_ttl_days = 0` 要换成一个极大值

    一期把 0 解释成"永不过期"（`token_expiry` 返回 None）。HMAC 令牌的
    `exp` 是绝对时刻，没有"不含 exp"这种编码，只能用一个远超使用年限的
    天数表示 —— 用 `MAX_TTL_DAYS`（100 年）。
    """
    cfg = _cloud_cfg(cfg)
    secret = _signing_secret(db, cfg)
    if secret:
        device_id = cloud_device_id(cfg, "")
        if device_id:
            days = token_ttl_days(cfg)
            if days <= 0:
                days = token_mod.MAX_TTL_DAYS
            try:
                return token_mod.issue_token(
                    secret, client_id=client_id, device_id=device_id,
                    ttl_days=days, now=now,
                    client_name=str(device_name or ""))
            except (ValueError, TypeError):
                # 签名失败不该让配对整体失败：退回随机串，局域网照常可用，
                # 只是这枚令牌过不了云端（与修复前行为一致，是可控的降级）。
                log.warning("签发 HMAC 令牌失败，退回随机串", exc_info=True)
    return new_token()


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
    token = _issue_client_token(now, client_id, device_name, cfg, db)
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

        # ISSUE-034 ③：第一台完成配对的设备成为 owner，其后都是 viewer。
        #
        # 为什么这样分：配对码只能在本机面板上生成（`panel_pair_code` 只接受
        # 回环请求），所以"拿得到码"本身就等于"站在电脑前的人授权了这台设备"。
        # 第一台就是用户自己那部手机；以后再配的（家人的手机、临时设备）
        # 没有理由能吊销别人 —— 一台手机丢了，攻击者能做的应该止于
        # "删自己这台"，而不是把全家设备一次性踢下线（那需要用户跑到电脑前重配）。
        #
        # 判"有没有 owner"时带上 `revoked = 0`：owner 被吊销后必须有人能
        # 重新当 owner，否则"移除设备"这个功能会永久锁死。
        # 查询放在事务里，避免两个并发配对都判成"还没有 owner"。
        has_owner = db.query_one(
            "SELECT 1 AS present FROM devices WHERE role = 'owner' AND revoked = 0"
            " LIMIT 1"
        ) is not None
        role = "viewer" if has_owner else "owner"

        db.execute(
            "INSERT INTO devices(id, name, token_hash, last_seen, created_at,"
            "                    revoked, expires_at, role)"
            " VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (client_id, device_name or "未命名手机", hash_token(token), now, now,
             expires_at, role),
        )

    log.info("配对成功 client_id=%s name=%s role=%s", client_id, device_name, role)
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
    """按 token 查设备行。查不到返回 None。

    带 `role` 是 ISSUE-034 ③ 的需要：权限判断不能只看"是不是已配对"，
    还要看这台设备能不能执行不可逆操作（吊销别的设备）。
    """
    return db.query_one(
        "SELECT id, name, revoked, created_at, last_seen, expires_at, role"
        "  FROM devices WHERE token_hash = ?",
        (hash_token(token),),
    )


#: 云端令牌的本地缓存表名。**不改 `devices` 表**是有意的：
#: `devices` 的 `id` 是本地配对时生成的 16 位 hex，而云端令牌的
#: `client_id` 是云端生成的 32 位 hex，两者语义不同却能同名，
#: 混在一张表里会让"撤销一台设备"变成"两张表的行都可能被改"。


def _admit_guest(ctx: Ctx, db: Any, claims: dict[str, Any]) -> dict[str, Any]:
    """游客令牌 → 请求身份。三道判据：通路、会话、邀请。

    ★ 为什么通路要卡：游客进的是**云端链接**，而链接里的主机名/端口是
      公网可见的。同一条 wss 隧道同时承载局域网口径的请求（同一台电脑
      的 8799 也在监听），不卡通路就等于"邀请链接顺便开了这个人在你家
      Wi-Fi 里的出入口"。G2 要求"游客只能走云端"，这里就是那条线。
    """
    from . import guest as guest_mod
    from .core.ctx import CLOUD

    if ctx.via != CLOUD:
        log.warning("游客令牌来自非云端通路 via=%s（已拒绝）", ctx.via)
        raise Unauthorized("游客只能从云端入口访问")

    client_id = str(claims["sub"])
    invite_id = str(claims.get("inv") or "")
    ok, reason = guest_mod.session_active(db, client_id)
    if not ok:
        ctx.set_cookies.append((guest_mod.GUEST_COOKIE, "", 0))
        if reason == "revoked":
            raise AppError(E1023)
        raise AppError(E1022)
    guest_mod.touch_session(db, client_id)
    return {
        "id": client_id,
        "name": "游客",
        "role": "guest",
        "guest": True,
        "invite_id": invite_id,
        "expires_at": float(claims["exp"]),
    }


def require_owner(ctx: Ctx) -> dict[str, Any]:
    """主人专用接口的闸门（生成邀请、看会话、踢人）。

    ★ 用 403 E2003 而不是 401：游客的凭证是**有效的**，只是没这个权限。
      回 401 会让前端把游客踢回配对页（那里他也进不来，只能看到
      "邀请链接无效"），而正确表现是"你没这个权限"。
    """
    client = ctx.state.get("client")
    if not isinstance(client, dict):
        raise Unauthorized("缺少凭证")
    role = str(client.get("role") or "owner")
    if client.get("guest") or role == "guest":
        # ★ 这里只挡**游客**，不挡 `viewer`。理由是被实践推翻过一次：
        #   配对时只有第一台设备拿到 `role="owner"`，之后每台都是
        #   `viewer`（`backend/app/auth.py` 的配对分支按"是否已有 owner"
        #   决定）。把 `viewer` 一起挡掉的结果是**主人的第二部手机发不出
        #   邀请**——而"从手机云端页面点生成邀请"正是 BUG-1 的现场。
        #   `viewer` 的语义是"这台电脑上后来的只读客户端"，它仍然是主人
        #   拿本机配对码加进来的，与"外来的陌生人"不是一回事。
        #   跨设备/跨账号由 `E2011`/`E2012` 与 `create_invite` 里的
        #   `token_device_claim` 校验守着，那些才是真正的越权面。
        log.warning("拒绝游客调用主人接口 role=%s client=%s",
                    role, client.get("id"))
        raise AppError(E2010, "该操作只对这台电脑的主人开放")
    return client


def token_device_claim(ctx: Ctx, db: Any, cfg: Any = None) -> str:
    """取当前请求凭证里的 `dev` claim（**只验签，不比对设备**）。

    BUG-1 需要它：`require_client` 用"这台电脑的 id"去验签，设备不匹配
    时只会得到 None（→ 401 E2002），根本区分不出"设备不对"。这里把
    同一个凭证再解一次，拿它自己声明的设备 id，交给调用方比对并报
    E2011 —— 两条判断用的是同一枚令牌、同一组密钥，不存在"换个字段
    再试一次"的兜底。
    """
    token = ctx.bearer_token() or ctx.cookie_token("pb_token") or ""
    if not token:
        return ""
    for secret in _candidate_secrets(db, cfg):
        claims = token_mod.verify_token(secret, token, device_id=None)
        if claims is not None:
            return str(claims.get("dev") or "")
    return ""


def _verify_cloud_token(ctx: Ctx, db: Any, token: str) -> dict[str, Any] | None:
    """二期路径：验签云端签发的 HMAC 令牌，成功则落一行缓存并返回设备信息。

    ★ 验签通过**不等于**放行：还要让本地库记下这枚令牌，否则下面这个
    场景会静默失效 —— 用户在手机上点"移除这台设备"，云端把它标成
    revoked，但电脑永远不知道（它只验签、不查吊销）。所以这里先写
    缓存行，再让 `revoked` 由 `sync_revoked_clients()` 从云端拉回来。
    缓存行同时是"`GET /api/devices` 里能看到这台手机"的依据。

    ★ `expires_at` 用令牌自己的 `exp`，不用 `token_ttl_days` 配置：
    令牌是云端签的，它的寿命由云端决定，本地配置改了也不该让它变长。
    """
    cfg = ctx.app.cfg if ctx.app is not None else None
    try:
        candidates = _candidate_secrets(db, cfg)
    except SigningSecretUnavailable:
        # ★ 审计 M01：这里必须**失败关闭**。修好之前的行为是静默用旧密钥
        #   验签 —— 一把云端库里躺着明文的钥匙。宁可让手机 401（用户看到
        #   "配对已失效"，去看电脑日志就知道是密钥坏了），也不能让一个
        #   库泄露者签出的令牌在这台电脑上通过。
        log.warning("无权验签云端令牌：电脑端私有密钥不可用（已拒绝）")
        return None
    if not candidates:
        return None
    st = ctx.app
    # ★ 这里比对的必须是**云端**设备 id，不是本地那个 16 位 id。
    # 云端令牌的 `dev` claim 出自 `clients.device_id`，它引用的是
    # `devices.id`（32 位、云端分配）；本地 `state.device_id` 是电脑首次
    # 启动自己生成的 16 位串。混用会让每一枚云端令牌都验签失败，而症状
    # 是"配对成功、之后所有请求 401"——见 `tunnel.cloud_device_id` 的表。

    cfg = ctx.app.cfg if ctx.app is not None else None
    device_id = cloud_device_id(cfg, st.device_id if st is not None else "")
    claims = None
    for secret in candidates:
        claims = token_mod.verify_token(secret, token, device_id=device_id)
        if claims is not None:
            break
    if claims is None:
        return None
    # ★ ISSUE-063：游客令牌走独立分支。它不在本地 `devices` 表里，也
    #   **不能**写 `cloud_clients` —— 那张表是"这台手机被配对过"的记录，
    #   写进去会让 `GET /api/devices` 里凭空多出一堆游客。
    if str(claims.get("rl") or "owner") == "guest":
        return _admit_guest(ctx, db, claims)

    client_id = str(claims["sub"])
    name = str(claims.get("nam") or "")[:64] or "手机"
    exp = float(claims["exp"])
    now = time.time()
    try:
        local = db.query_one(
            "SELECT revoked, expires_at FROM devices WHERE id = ?", (client_id,))
        if local is not None and (local["revoked"] or (
                local["expires_at"] is not None and local["expires_at"] <= now)):
            return None
        _ensure_cloud_table(db)
        # ★ 撤销标记必须被 `DO UPDATE` 保留。
        #
        # 这里原来写的是 `expires_at = excluded.expires_at`，看起来天经地义
        # ——"令牌的寿命由令牌自己决定"。但那会让撤销**静默失效**：手机被
        # 移除后 `sync_revoked_clients()` 把 `expires_at` 打成 0，下一次
        # 请求进来验签通过，这行 upsert 就把 0 改回真实过期时间，撤销标记
        # 被自己抹掉了。于是"移除这台手机"只对它之后的那一个请求有效。
        #
        # 改成 `CASE WHEN 已撤销 THEN 0 ELSE 令牌 exp END`：撤销是**粘性**
        # 的，只有云端重新配对（新 client_id）才能拿到干净的行走这条路径。
        # `last_seen` 也不能在撤销后被刷新 —— 它是"这枚令牌最后用过"的
        # 依据，刷新等于说"它还在正常使用"。
        db.execute(
            f"INSERT INTO {CLOUD_TOKEN_TABLE}"
            "(client_id, device_id, name, expires_at, created_at, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(client_id) DO UPDATE SET"
            "   name = excluded.name,"
            "   expires_at = CASE WHEN"
            f"     {CLOUD_TOKEN_TABLE}.expires_at IS NOT NULL"
            f"     AND {CLOUD_TOKEN_TABLE}.expires_at <= 0"
            "   THEN 0 ELSE excluded.expires_at END,"
            f"   last_seen = CASE WHEN"
            f"     {CLOUD_TOKEN_TABLE}.expires_at IS NOT NULL"
            f"     AND {CLOUD_TOKEN_TABLE}.expires_at <= 0"
            f"   THEN {CLOUD_TOKEN_TABLE}.last_seen"
            "   ELSE excluded.last_seen END",
            (client_id, device_id, name, exp, now, now),
        )
    except Exception as e:  # noqa: BLE001 —— 缓存写失败不该让请求 500
        log.debug("写入云端令牌缓存失败 client_id=%s: %s", client_id, e)
        return None

    # 云端已撤销的令牌不能再放行。缓存行被 `sync_revoked_clients()`
    # 打了 `expires_at=0`，这里一次主键查询就够（比再算一遍 HMAC 便宜）。
    row = None
    try:
        row = db.query_one(
            f"SELECT expires_at FROM {CLOUD_TOKEN_TABLE} WHERE client_id = ?",
            (client_id,))
    except Exception:  # noqa: BLE001 —— 撤销状态不可用时拒绝访问
        log.warning("无法确认云端令牌撤销状态，拒绝访问 client_id=%s", client_id)
        return None
    if row is None or float(row["expires_at"] or 0) <= now:
        return None

    return {"id": client_id, "name": name, "cloud": True, "expires_at": exp}


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
    # Cookie authentication is a transport fallback only for browser APIs that
    # cannot attach Authorization. It must never authorize a state change.
    cookie_allowed = ctx.method.upper() in ("GET", "HEAD") and (
        ctx.path == "/api/events"
        or bool(re.fullmatch(
            r"/api/items/[0-9]+/(?:thumb|preview|raw)", ctx.path)))
    token = via_header
    if not token and cookie_allowed:
        token = ctx.cookie_token(CLIENT_COOKIE)
    if not token and cookie_allowed:
        # ★ 游客的 cookie 是**另一个名字**（`pb_guest`）。同一个浏览器
        #   既可能是主人又刚点过朋友的链接，共用一个 cookie 名会互相
        #   覆盖 —— 最坏的方向是游客身份被写进主人的 `pb_token`。
        token = ctx.cookie_token(GUEST_COOKIE)
    if not token:
        raise Unauthorized("缺少凭证：Authorization 头")

    row = _lookup_device(st.db, token)
    if row is None:
        # --- 二期路径：云端签发的 HMAC 令牌（`shared/client_token.py`）---
        #
        # 先查本地库再验签，顺序不能反：
        #   * 一期令牌（本地 `devices` 表里的行）校验是**一次索引查询**，
        #     而验签每次要做一次 HMAC —— 缩略图滚动时这是每秒几十次的
        #     差别，而绝大多数请求走的都是一期路径（局域网直连）；
        #   * 反过来的顺序（先验签）在**没配云端**的机器上每次都要先
        #     算一次空密钥短路，再落回查库，等于给常见路径加了一层。
        info = _verify_cloud_token(ctx, st.db, token)
        if info is not None and info.get("guest"):
            ctx.state["client"] = info
            ctx.state["via_cookie"] = not via_header
            if via_header:
                # 游客的 `<img>` / `<video>` 也要能带凭证，所以补的是
                # `pb_guest` —— 绝不能补到 `pb_token`（那会让浏览器从此
                # 以游客身份访问自己的库，症状是"突然只能看一个文件夹"）。
                ctx.set_cookies.append(
                    (GUEST_COOKIE, token,
                     cookie_max_age_for(info.get("expires_at"))))
            return info
        if info is not None:
            ctx.state["client"] = info
            ctx.state["via_cookie"] = not via_header
            if via_header:
                # 与一期同因（见下面那段"自愈路径"）：`<img>` 与
                # `EventSource` 发不了自定义头，必须让浏览器也拿到 cookie。
                # 云端令牌自带 `exp`，cookie 寿命按它算，不能按本地
                # `token_ttl_days` —— 否则 cookie 比令牌活得久，
                # 缩略图会在令牌过期后一直 401。
                ctx.set_cookies.append(
                    (CLIENT_COOKIE, token,
                     cookie_max_age_for(info.get("expires_at"))))
            return info
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

    info = {"id": row["id"], "name": row["name"], "role": row["role"] or "viewer"}
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

    ⚠️ **空 IP 不是回环**（ISSUE-018 / 审计 F11）。早先的实现把 `""` 也算
    进白名单，理由是"没有 IP 就当本机"。但 `Ctx.client_ip` 的默认值就是
    空串，任何一条**构造 `Ctx` 时忘了填 IP** 的代码路径（测试、二期云端
    派发、将来新增的传输层）都会让 `/panel/**` 静默变成公开端点 ——
    而它端出的是配对数、设备清单与审计日志。判不准的时候要拒绝，
    不是放行。
    """
    # 隧道中的 IP 是远端元数据，不能授予本机管理权限。
    if getattr(ctx, "via", "lan") != "lan":
        return False
    ip = (ctx.client_ip or "").strip().lower()
    if ip.startswith("::ffff:"):
        ip = ip[7:]
    if not ip:
        return False
    return ip in ("127.0.0.1", "::1", "localhost")


def panel_http_enabled(cfg: Any = None) -> bool:
    """`/panel/**` 是否挂在 HTTP 上。默认 False（见 `auth_middleware`）。"""
    if cfg is None:
        from .config import get as _get_cfg
        try:
            cfg = _get_cfg()
        except Exception:  # noqa: BLE001
            return False
    try:
        return bool(cfg.get("server.panel_http", False))
    except Exception:  # noqa: BLE001
        return False


#: 游客**允许**的非只读请求。全部在 `/api/uploads/` 之下：
#: init（POST）/ 分片（PATCH）/ 完成（POST）/ 中止（POST）。
#:
#: 白名单而不是黑名单：新增一个写接口时，默认对游客是**关闭**的。
#: 反过来（默认放行、逐个禁）在漏一条时就是"游客能删主人的照片"。
GUEST_WRITE_PREFIXES: tuple[str, ...] = ("/api/uploads/",)


def guest_may_write(method: str, path: str) -> bool:
    """游客这个请求能不能发。

    只读方法一律放行（读路径已按可见根收口，见 `handlers/common.py`）；
    写方法只认上传三件套。
    """
    m = (method or "").upper()
    if m in ("GET", "HEAD", "OPTIONS"):
        return True
    if m in ("POST", "PATCH"):
        return any(path.startswith(prefix) for prefix in GUEST_WRITE_PREFIXES)
    return False


def _enforce_audience(ctx: Ctx, client: dict[str, Any]) -> None:
    """游客只能碰 `audience="guest"` 的路由。

    为什么要单独一道，而不是只靠 `_enforce_guest_scope` 的路径黑名单：
    黑名单是"命名要挡的"，漏一个就是越权；白名单是"命名能用的"，漏一个
    只是功能少一点。2026-10-28 的隔离缺陷（游客的删除按钮点了没反应、
    主人反倒删不掉游客照片）就是黑名单 + 目录 mode 两处各自为政的结果。

    拒绝时回 404 —— 与"这个路径不存在"不可区分，不告诉游客这里有写接口。
    """
    if not client.get("guest"):
        return
    from .core.dispatch import active_router

    audience = "owner"
    router = active_router()
    if router is not None:
        try:
            audience = router.audience_of(ctx.method, ctx.path)
        except Exception:  # noqa: BLE001 —— 路由表异常不该变成 500
            audience = "owner"
    if audience != "guest":
        from .errors import NotFound

        log.warning("游客访问了主人专属路径，已按不存在处理：%s %s",
                    ctx.method, ctx.path)
        raise NotFound(ctx.path)


def _enforce_guest_scope(ctx: Ctx, client: dict[str, Any] | None) -> None:
    """游客越界就当场拒（403 E2003），并留一条审计级的告警。

    为什么放在中间件而不是各 handler 里：handler 有二十多个，
    漏一个就是一条越权路径；而"游客能做什么"是一条**全局性质**的规则。
    """
    if not client or client.get("role") != "guest":
        return
    if guest_may_write(ctx.method, ctx.path):
        return
    log.warning("游客越界被拒：%s %s", ctx.method, ctx.path)
    raise AppError(E2003, "游客只能查看与上传，不能修改或删除")


async def auth_middleware(ctx: Ctx, nxt: Any):
    """鉴权中间件。

    装配顺序很重要：它必须是**最内层**之一，让请求日志中间件能包住
    401 响应（否则鉴权失败不会出现在访问日志里，排障时看不到）。
    """
    st = ctx.app
    cfg = st.cfg if st is not None else None
    require_pairing = bool(cfg.auth.get("require_pairing", True)) if cfg else True

    path = ctx.path

    # --- Host 校验：挡住 DNS rebinding（审计 F1-c）---
    #
    # 必须放在**所有**分支之前。放在鉴权之后没有意义：攻击链的第一步
    # 是读 `/panel/pair-code`，而那条路本来就不需要 token。
    #
    # 代价是每个请求多一次本机网卡枚举（<0.1ms，见 netguard 的说明）。
    # 收益是把"恶意域名解析到 127.0.0.1 就等于本机"这条假设彻底打掉。
    denied = check_host(ctx.headers.get("host"), cfg)
    if denied is not None:
        from .errors import NotFound

        log.warning("%s %s 被 Host 校验拒绝：%s", ctx.method, path, denied)
        raise NotFound(path)

    # --- /panel/**：默认整条关闭（审计 F1-b）---
    #
    # 以前这里只判 `is_loopback(ctx)`。那道门在纸面上是"只允许本机"，
    # 实际含义是"允许本机上的**任何网页**"：恶意页面让浏览器去访问
    # `http://127.0.0.1:8787/panel/pair-code`，来源 IP 就是 127.0.0.1，
    # 与用户自己点面板没有任何可区分之处。于是攻击者不需要用户的配对码
    # ——它自己现取一个，再用 /api/pair 换 90 天有效的 token。
    #
    # 现在默认 `server.panel_http = false`：整条 prefix 404，连回环也拒。
    # 之所以敢默认关：**没有任何调用方**。托盘直接用 handler 函数（不走
    # HTTP），前端全仓库 `grep /panel` 零命中。需要脚本化取配对码时把
    # `server.panel_http` 打开即可，此时`check_bind_safety` 还会顺带
    # 禁止同时监听 0.0.0.0。
    for prefix in PANEL_PATHS:
        if path == prefix or path.startswith(prefix + "/"):
            from .errors import NotFound

            if not panel_http_enabled(cfg):
                log.info("拒绝访问 %s：server.panel_http 未开启", path)
                raise NotFound(path)
            if not is_loopback(ctx):
                # 返回 404 而不是 403：403 等于告诉扫描者"这个路径存在，
                # 只是你权限不够"，而 404 让他无法区分"没有这个功能"。
                log.warning("拒绝非本机访问 /panel：client_ip=%s", ctx.client_ip)
                raise NotFound(path)
            return await nxt(ctx)

    # --- 视频播放票（`?t=`）---
    #
    # `<video>` 由浏览器自己发请求：加不了 `Authorization`，cookie 这条路
    # 又被现实打过两次脸（ISSUE-033 / ISSUE-057）。所以已认证的 JS 先换一张
    # 5 分钟、只对单条目有效的票拼进播放地址（`streamticket.py`）。
    # 这里只负责"放行到处理器"：票的真伪在 `browse.stream_video` 里验，
    # 因为校验需要 `media_id`，而那是路由参数。
    # ⚠️ `ctx.query` 的值是 **list[str]**，直接 `get("t")` 判真会踩坑：
    #   `?t=`（空值）拿到的是 `[""]`，非空列表恒为真 —— 于是"带一个空票"
    #   就能绕过整个鉴权中间件。必须按字符串判空。
    ticket_q = (ctx.q("t") or "").strip()
    if ticket_q and path.startswith("/api/items/") \
            and path.endswith("/stream"):
        return await nxt(ctx)

    # --- 公开路径 ---
    if path in PUBLIC_PATHS:
        return await nxt(ctx)

    # --- 未启用配对：放行（仅供本机开发调试） ---
    if not require_pairing:
        return await nxt(ctx)

    client = require_client(ctx)
    # ★ 顺序：先按路由白名单（audience）判，再按路径黑名单（guest_scope）。
    # 反过来时，游客打 `/api/items/delete` 会先被 scope 拦成 403「游客只能查看
    # 与上传」，等于告诉对方"这个接口存在、只是你没权限"；先过 audience 就只
    # 剩 404，与"路径不存在"不可区分（硬约束 3）。
    _enforce_audience(ctx, client)
    _enforce_guest_scope(ctx, client)
    return await nxt(ctx)
