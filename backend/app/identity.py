"""身份原语的**最底层**窄接口：签名密钥、令牌哈希、云端设备 id、撤销同步。

## 为什么单独一个模块（M2）

`auth` / `control` / `tunnel` / `guest` 四个模块互相需要对方的几样小东西：
`control` 要 `auth.hash_token`，`auth` 要 `control.client_signing_secret`，
`tunnel` 要 `auth.sync_revoked_clients`，`auth` 又要 `tunnel.cloud_device_id`，
`guest` 签会话令牌要 `auth._signing_secret`，`auth` 又要 `guest.GUEST_COOKIE`。
函数内延迟 import 让启动期不炸，但依赖图上仍是环 —— 环里任何一处改动都可能
变成"运行时才炸，而且只有某条分支会走到"。

这里把这些**不依赖任何业务模块**的原语搬进一个只依赖 `shared` 的模块，
四个模块一律单向依赖它，环消失（门禁见 `backend/tests/test_layering.py`）。
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Any, Final

from shared import client_token as token_mod

log = logging.getLogger(__name__)

#: 令牌签名密钥在本地 `settings` 表里的键名。
SECRET_KEY = "client_signing_secret"

#: 云端令牌缓存表名（`docs/04` §3.6）。
CLOUD_TOKEN_TABLE: Final = "cloud_clients"

#: 游客令牌的 cookie 名。与主人的 `pb_token` 分开：**同一个浏览器可能既是
#: 主人又刚点过朋友的链接**，共用一个 cookie 名就会互相覆盖，症状是
#: "回到自己的页面变成游客"（或者反过来，最坏的情况是游客拿到主人身份）。
GUEST_COOKIE: Final = "pb_guest"

def cloud_device_id(cfg: Any = None, fallback: str = "") -> str:
    """这台电脑在**云端**的设备 id（`cloud.device_id`）。

    ## 为什么不能直接用 `state.device_id`

    两个 id 是完全不同的东西，混用会让整条链路在握手阶段就断：

    | | 生成者 | 形态 | 用途 |
    |---|---|---|---|
    | `state.device_id` | 电脑自己（首次启动 `uuid4().hex[:16]`） | 16 hex | 手机上"这是不是我配对过的那台电脑" |
    | `cloud.device_id` | 云端（`scripts/cloud_init.py`，32 hex） | 32 hex | 云端 `devices.id`，`/d/{device_id}/**` 的路径段 |

    云端 `tunnel.py:_handshake` 会校验 `HELLO.device_id` == `agent_token`
    所属设备（`docs/04` §2.4 明确要求"必须校验"，否则 A 的令牌能冒充
    B 上线），而 `agent_token` 是绑在云端那行 `devices.id` 上的。所以
    HELLO 必须报**云端 id**。

    `cloud.device_id` 没配时退回本地 id：这样"云端登记的 id 恰好等于
    本地 id"的部署（例如手工插行）仍然能连，而不是直接连不上。
    """
    if cfg is not None:
        raw = str(cfg.get("cloud.device_id", "") or "").strip()
        if raw:
            return raw
    return fallback

def hash_token(token: str) -> str:
    """token 的存储形式。无盐 sha256 足够（token 是高熵随机数）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

def _looks_like_secret(value: str) -> bool:
    """64 位小写 hex 才算数 —— 手改坏了就重新生成，别拿坏值去签名。"""
    return (len(value) == 64
            and all(ch in "0123456789abcdef" for ch in value))

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

class SigningSecretUnavailable(RuntimeError):
    """电脑端私有签名密钥读不出来（审计 M01）。

    ★ 这个异常存在的唯一目的是**不让调用方回退**。以前这里是
    `except Exception: return legacy_tunnel_secret(cfg)`：一旦
    `settings.client_signing_secret` 读失败（库损坏、迁移半途、磁盘只读），
    验签就会"降级"到那枚**云端库里也有明文**的旧密钥 —— 正好是
    ISSUE-049 修掉的那条攻击链，而且降级时没有任何日志。

    现在拆成两条互不影响的路径：私有密钥坏了 = 拒绝认证 + 告警；旧密钥
    只在 `devices.allow_legacy_until` 显式窗口内、作为**独立候选**参与
    验签（`_candidate_secrets()` 里那一段），两者绝不互相顶替。
    """

def _cloud_cfg(cfg: Any = None) -> Any:
    """取当前配置。`require_client` 只拿得到 `ctx`，配置只能从这里拿。"""
    if cfg is not None:
        return cfg
    from .config import get as _get_cfg

    try:
        return _get_cfg()
    except Exception:  # noqa: BLE001 —— 配置坏了不该让鉴权抛别的异常
        return None

def legacy_tunnel_secret(cfg: Any = None) -> str:
    """**旧**令牌的 HMAC 密钥 = `sha256(agent_token)`。

    ★ 2026-10-26 起新签发的令牌**不再**用它（见 `control.client_signing_secret()`）：
    这个值在云端库里明文躺着，等于把签名私钥放进了云端数据库 ——
    库一泄露就能离线签出任意手机令牌。这里保留只为让 90 天有效期的
    老令牌自然过期，别再往这条路上加东西。

    没配 `cloud.agent_token` 时返回空串，调用方据此跳过云端令牌校验
    —— 一台根本没用过云端的电脑不该因为"表里有个不认识的令牌"而
    多走一次 HMAC 计算（那是每个缩略图请求一次）。
    """
    cfg = _cloud_cfg(cfg)
    if cfg is None:
        return ""
    raw = str(cfg.get("cloud.agent_token", "") or "").strip()
    if not raw:
        return ""
    return token_mod.secret_for_agent_token(raw)

def _signing_secret(db: Any = None, cfg: Any = None) -> str:
    """签发令牌用的 HMAC 密钥：**电脑端私有**（`settings.client_signing_secret`）。

    `db is None` 时退回旧密钥：有几个调用点（如 `_tunnel_secret()` 的老
    用法）拿不到数据库句柄。这种回退是过渡期的妥协 —— 真正的修复是把
    db 一路传下去，`control.mint_client` / `_verify_cloud_token` 都已经做到了。

    ★ 审计 M01：`db is not None` 时**任何**读/生成失败都抛
    `SigningSecretUnavailable`，不再静默换成旧密钥。拿不到私有密钥就是
    "这台电脑现在没法判断令牌真假"，正确处置是拒绝并告警，不是换一把
    攻击者也有的钥匙。
    """
    if db is not None:
        try:
            return client_signing_secret(db)
        except Exception as exc:    # noqa: BLE001 - 任何失败都不能降级
            log.warning("私有签名密钥不可用，拒绝云端令牌验签：%s", exc,
                        exc_info=True)
            raise SigningSecretUnavailable("私有签名密钥不可用") from exc
    return legacy_tunnel_secret(cfg)

def _candidate_secrets(db: Any = None, cfg: Any = None) -> list[str]:
    """验签时要试的密钥列表：新密钥在前，旧密钥只在窗口期内兜底。

    ★ 审计 M01：私有密钥不可用时**抛异常**，不返回"只有旧密钥"的列表。
    否则调用方的 for 循环会拿旧密钥验签成功，等于悄悄降级。
    """
    out: list[str] = []
    fresh = _signing_secret(db, cfg)
    if fresh:
        out.append(fresh)
    legacy = legacy_tunnel_secret(cfg)
    if legacy and legacy not in out and legacy_window_open(db):
        out.append(legacy)
    return out

def legacy_window_open(db: Any = None) -> bool:
    """`devices.allow_legacy_until` 还没过期吗？（默认关闭）

    旧密钥 = `sha256(agent_token)`，它在云端库里明文躺着。只要我们还拿它
    验签，"云端库泄露 → 伪造成任意手机令牌"这条攻击链就还在。所以默认
    不接受，只在运维显式开的窗口期内接受（`pbctl allow-legacy <天数>`）。
    """
    if db is None:
        return False
    try:
        row = db.query_one(
            "SELECT MAX(allow_legacy_until) AS until FROM devices")
    except Exception:               # noqa: BLE001 - 老库没有这一列
        return False
    until = 0.0
    try:
        until = float(row["until"]) if row is not None and row["until"] else 0.0
    except (TypeError, ValueError):
        return False
    return until > time.time()

def _ensure_cloud_table(db: Any) -> None:
    """首次用云端令牌时建缓存表（老库升级上来时没有这张表）。"""
    db.execute(
        f"CREATE TABLE IF NOT EXISTS {CLOUD_TOKEN_TABLE} ("
        "  client_id  TEXT PRIMARY KEY,"
        "  device_id  TEXT NOT NULL,"
        "  name       TEXT,"
        "  expires_at REAL,"
        "  created_at REAL NOT NULL,"
        "  last_seen  REAL"
        ")"
    )

def sync_revoked_clients(st: Any, revoked_ids: list[str],
                         *, access_token: str = "") -> int:
    """把云端撤销列表落到本地缓存（`docs/04` §3.6）。

    用 `expires_at = 0` 表示"已撤销"而不是删行：删了之后再验签会
    **重新插入**一行干净记录，撤销就失效了（令牌本身没过期，签名也
    还是对的）。这是本条链路唯一容易写错的地方。

    ## 为什么还要动本地 `devices` 表

    局域网配对现在也签发 HMAC 令牌（见 `_issue_client_token`），而那枚
    令牌**同时**会在本地 `devices` 表留一行（`consume_pair_code` 写的）。
    `require_client` 是先查本地库、查不到才验签的，所以这类令牌走的是
    `_lookup_device` 那条路 —— 只写 `cloud_clients` 的话，用户在账号
    后台点"移除这台手机"，云端通路立刻 403，但**局域网通路仍然放行**，
    而用户以为自己已经把手机摘掉了。

    两张表的 `id` 是同一个值（`consume_pair_code` 用同一个 `client_id`
    同时写两处），所以按 id 直接落 `revoked = 1` 即可。

    返回实际影响的缓存行数。
    """
    if not revoked_ids:
        return 0

    device_id = cloud_device_id(st.cfg, st.device_id)
    now = time.time()
    _ensure_cloud_table(st.db)
    n = 0
    for cid in revoked_ids:
        key = str(cid or "").strip()
        if not key:
            continue
        n += int(st.db.execute_rowcount(
            f"INSERT INTO {CLOUD_TOKEN_TABLE}"
            " (client_id, device_id, expires_at, created_at) VALUES (?, ?, 0, ?)"
            " ON CONFLICT(client_id) DO UPDATE SET expires_at = 0"
            f" WHERE {CLOUD_TOKEN_TABLE}.expires_at IS NULL"
            f" OR {CLOUD_TOKEN_TABLE}.expires_at > 0",
            (key, device_id, now)))
        # 幂等：已经 revoked 的行不重复计数（rowcount 只数真正改到的行）。
        n += int(st.db.execute_rowcount(
            "UPDATE devices SET revoked = 1 WHERE id = ? AND revoked = 0",
            (key,)))
    if n:
        log.info("从云端同步到 %d 台被撤销的手机", n)
    return n
