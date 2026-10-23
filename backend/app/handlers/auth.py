"""配对与设备管理接口。

一期（局域网直连）的配对流程：

```
电脑托盘                        手机浏览器
   │                               │
   │ POST /panel/pair-code (仅本机) │
   │ ← 6 位码 + 二维码              │
   │                               │
   │            扫码 / 手输 6 位码  │
   │ ←── POST /api/pair {code} ────┤
   │ ─── {client_token, device} ──→│ 存入 localStorage
   │                               │
   │ ←── GET /api/timeline ────────┤ Authorization: Bearer <token>
```

为什么 `POST /api/pair` 不需要任何鉴权（它本身就是为了拿 token）：
它靠的是"必须持有 5 分钟内有效的 6 位码"这个前提，以及
"错 5 次锁 10 分钟"的限速。这两条合起来使在线枚举不可行
（5 分钟内最多 5 次尝试，命中概率 5/1000000）。

⚠️ 但不能让它变成"无限次尝试不同码"的入口：`consume_pair_code`
对不存在的码也返回 E1002，而针对**不存在的码**无法计数（没有行可更新）。
因此这里额外加一层**按来源 IP 的滑动窗口限速** —— 它防不住换 IP 的
攻击者，但能挡住脚本对同一台电脑的暴力扫描，而后者才是真实威胁
（局域网内只有有限的几台设备）。
"""
from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any, Deque

from .. import audit
from ..auth import (
    CLIENT_COOKIE,
    PAIR_CODE_TTL,
    TOKEN_TTL_DAYS,
    cookie_max_age_for,
    token_expiry,
    consume_pair_code,
    generate_pair_code,
    is_loopback,
    pair_code_info,
    purge_expired_codes,
    require_client,
    revoke_all_codes,
    store_pair_code,
)
from ..core import Ctx, Reply, empty_reply, json_reply
from ..errors import E1001, E1015, E2003, E4001, AppError, NotFound, Unauthorized

log = logging.getLogger(__name__)

__all__ = [
    "pair",
    "me",
    "list_devices",
    "revoke_device",
    "panel_pair_code",
    "panel_devices",
    "panel_audit",
]

#: 按来源 IP 的配对尝试限速：10 分钟内最多 20 次。
#:
#: 20 次而不是 5 次，是因为"5 次"已经由配对码本身的锁定机制保证了；
#: 这一层是为了拦住"换不同码持续扫"的脚本 —— 它对每个码都是第一次尝试，
#: 配对码的计数器帮不上忙。
_PAIR_WINDOW_SEC = 600.0
_PAIR_WINDOW_MAX = 20
_pair_attempts: dict[str, Deque[float]] = {}


def _rate_limit(ctx: Ctx) -> None:
    ip = (ctx.client_ip or "unknown").lower()
    now = time.time()
    q = _pair_attempts.setdefault(ip, deque())
    while q and now - q[0] > _PAIR_WINDOW_SEC:
        q.popleft()
    if len(q) >= _PAIR_WINDOW_MAX:
        log.warning("配对尝试过于频繁 ip=%s", ip)
        raise AppError(E4001, f"pair attempts from {ip}")
    q.append(now)
    # 清理空队列，避免长期运行后字典无限增长（每个访问过的 IP 一条）
    if len(_pair_attempts) > 512:
        for k in [k for k, v in _pair_attempts.items() if not v]:
            _pair_attempts.pop(k, None)


def _st(ctx: Ctx) -> Any:
    st = ctx.app
    if st is None:  # pragma: no cover
        raise RuntimeError("AppState 未注入 ctx.app")
    return st


def _client_name(ctx: Ctx) -> str:
    """从 X-PB-Client 或 User-Agent 里取一个能认出来的名字。

    用户在两台手机上各配一次后，托盘列表里如果两行都叫"未知设备"，
    就没法判断该撤销哪一个 —— 撤销错了还得重新配对。
    """
    raw = ctx.header("x-pb-client") or ctx.header("user-agent") or ""
    raw = raw.strip()
    if not raw:
        return "未命名手机"
    # 只要前 48 个字符：User-Agent 动辄 150+ 字符，整串存进去
    # 在托盘列表里显示会被截断成一堆版本号，反而认不出设备。
    return raw[:48]


# --------------------------------------------------------------------------
# 手机端
# --------------------------------------------------------------------------

async def pair(ctx: Ctx) -> Reply:
    """`POST /api/pair` —— 用配对码换 client_token。"""
    _rate_limit(ctx)
    st = _st(ctx)

    body = await ctx.json()
    code = str(body.get("code") or "").strip()
    # 允许用户输入 "042 817" 或带空格的形式：手机上照着电脑屏幕
    # 输入 6 位数字时，分组输入是很自然的动作。
    code = code.replace(" ", "").replace("-", "")
    if not code.isdigit() or len(code) != 6:
        raise AppError(E1001, f"配对码格式非法 len={len(code)}")

    client_id, token = consume_pair_code(
        st.db, code, device_name=_client_name(ctx), cfg=st.cfg,
    )

    now = time.time()
    audit.record(
        st.db, st.cfg, audit.ACTION_PAIR,
        device=st.device_name, client_id=client_id,
        detail={"device_name": _client_name(ctx)},
    )

    r = json_reply({
        "client_token": token,
        "client_id": client_id,
        "device_id": st.device_id,
        "device_name": st.device_name,
        "expires_at": token_expiry(now, st.cfg),
        "lan_ips": st.cfg_server_ips(),
        "server_time": now,
    }, status=201)
    # ★ 配对时同时下发 cookie：浏览器原生 `<img>` / `EventSource`
    # 带不了 Authorization 头，缩略图只能靠 cookie 才能过鉴权。
    # 前端仍然拿到明文 token（上面响应体里），两条通路并行：
    # JS 请求走 Bearer 头，`<img src>` 走 cookie。
    # ★ 这里**不能**写 `max(1, token_ttl_days(cfg)) * 86400`：
    # `token_ttl_days` 为 0（永不过期）时那个表达式算出来是 1 天，
    # 于是"永不过期"的 token 配了一个一天就消失的 cookie ——
    # 第二天缩略图全灰而 JS 请求正常。
    r.set_cookie(CLIENT_COOKIE, token,
                 max_age=cookie_max_age_for(token_expiry(now, st.cfg), now))
    return r


async def me(ctx: Ctx) -> Reply:
    """`GET /api/clients/me` —— 手机冷启动时确认自己是谁、该连哪里。

    这个接口存在的理由是**探活**：PWA 冷启动时手机不知道电脑的 IP
    有没有变（DHCP 续租、用户换了路由器）。先打一个已知地址，
    通了就拿回最新的 `lan_ips`，之后所有请求都用它。
    """
    st = _st(ctx)
    info = require_client(ctx)
    return json_reply({
        "client_id": info["id"],
        "client_name": info["name"],
        "device_id": st.device_id,
        "device_name": st.device_name,
        "lan_ips": st.cfg_server_ips(),
        "server_time": time.time(),
    })


# --------------------------------------------------------------------------
# 设备管理（手机端或托盘）
# --------------------------------------------------------------------------

async def list_devices(ctx: Ctx) -> Reply:
    """`GET /api/devices` —— 已配对的手机列表。

    ISSUE-034 ③：**任何**已配对设备都能看到列表（手机端要能回答"这台是我吗、
    家里还有哪些设备连着"，这既是配对确认也是排障信息），但 `viewer` 拿到
    的是**脱敏**版本：

      * 不含 `role`：否则拿到列表的人可以直接知道哪台是 owner，
        从而知道该重点偷哪台 —— 权限信息不该描述别的设备；
      * 不含 `created_at`（配对时刻）：那是对端设备的运维细节，
        只对"管理"这件事有用，而管理是本机面板/owner 的事。

    权衡说明：保留 `name` 与 `last_seen` 是有意的 —— 没有它们列表就没法用
    （用户看"这台小米14是上周配的，那台叫iPhone的是我老婆的"），而且
    这些信息对攻击者的价值远低于"能一键吊销全部设备"。
    """
    st = _st(ctx)
    me = require_client(ctx)
    is_owner = me.get("role") == "owner"
    rows = st.db.query(
        "SELECT id, name, created_at, last_seen, revoked, role FROM devices"
        " ORDER BY created_at DESC"
    )
    items = []
    for r in rows:
        item: dict[str, Any] = {
            "client_id": r["id"],
            "name": r["name"],
            "last_seen": r["last_seen"],
            "revoked": bool(r["revoked"]),
        }
        if is_owner:
            item["created_at"] = r["created_at"]
            item["role"] = r["role"] or "viewer"
        items.append(item)
    return json_reply({"items": items})


async def revoke_device(ctx: Ctx) -> Reply:
    """`DELETE /api/devices/{client_id}` —— 移除一台手机。

    ⚠️ 不删行，只置 `revoked=1`。删行会让 `audit_log.client_id` 变成
    指向不存在记录的悬空引用，事后追查"是谁删了这张照片"就断了线索。

    ★ ISSUE-034 ③：只允许 owner 或本机回环调用。吊销是**不可逆**操作 ——
    被吊销的设备必须有人亲自走到电脑前重新生成配对码，所以"任何一台
    手机令牌泄露就能把全家设备踢下线"是不可接受的破坏面。

    允许设备吊销**自己**是刻意保留的：那是"退出登录/移除本机"的语义，
    最坏结果不过是它自己失去访问权，没有可被滥用的方向
    （而且用户换手机时正需要它）。非 owner 想吊销别人则是 403 E2003，
    不是 404：目标 uid 是由调用方给出的，回 404 并不能隐藏什么，
    反而会让真正的权限问题表现成"设备不见了"这种误导性症状。
    """
    st = _st(ctx)
    me = require_client(ctx)
    client_id = ctx.params["client_id"]

    is_self = client_id == me["id"]
    if not is_self and me.get("role") != "owner" and not is_loopback(ctx):
        log.warning("拒绝越权吊销：client_id=%s role=%s", me["id"], me.get("role"))
        raise AppError(E2003, "只有本机或主设备可以移除其它设备")

    row = st.db.query_one("SELECT id FROM devices WHERE id = ?", (client_id,))
    if row is None:
        raise NotFound(client_id)

    st.db.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (client_id,))
    audit.record(
        st.db, st.cfg, audit.ACTION_REVOKE,
        device=st.device_name, client_id=client_id,
    )
    r = empty_reply(204)
    if client_id == me["id"]:
        # 手机自己撤销自己（或托盘通过手机页面撤销）：必须清 cookie。
        # 不清的话 `<img>` 仍带着有效期的旧 cookie 请求，虽然服务端
        # 会因 revoked=1 回 403，但浏览器会缓存失败状态直到用户
        # 手动清站点数据 —— 重新配对后旧 cookie 依然在，缩略图
        # 继续 403 而 JS 请求正常，症状是"照片全是灰块"。
        r.clear_cookie(CLIENT_COOKIE)
    return r


# --------------------------------------------------------------------------
# 本地管理面板（仅 127.0.0.1）
# --------------------------------------------------------------------------

async def panel_pair_code(ctx: Ctx) -> Reply:
    """`POST /panel/pair-code` —— 生成新的 6 位配对码。

    ⚠️ 只允许本机调用。`auth_middleware` 已经拦了 `/panel/**`，
    这里再判一次是**纵深防御**：中间件的路径前缀判断一旦写错
    （例如把 `PANEL_PATHS` 改成空元组、或路径大小写差异），
    这个函数是最后一道闸。安全相关的检查宁可重复。
    """
    if not is_loopback(ctx):
        log.warning("拒绝非本机生成配对码 client_ip=%s", ctx.client_ip)
        raise NotFound(ctx.path)

    st = _st(ctx)
    revoke_all_codes(st.db)
    purge_expired_codes(st.db)

    code = generate_pair_code()
    store_pair_code(st.db, code, st.device_id, st.device_name)
    return json_reply({
        "code": code,
        "device_id": st.device_id,
        "device_name": st.device_name,
        "expires_in": int(PAIR_CODE_TTL),
        "expires_at": time.time() + PAIR_CODE_TTL,
        "lan_ips": st.cfg_server_ips(),
    }, status=201)


async def panel_devices(ctx: Ctx) -> Reply:
    """`GET /panel/devices` —— 托盘展示的设备列表（免 token，仅本机）。"""
    if not is_loopback(ctx):
        raise NotFound(ctx.path)
    st = _st(ctx)
    now = time.time()
    rows = st.db.query(
        "SELECT id, name, created_at, last_seen, revoked, expires_at FROM devices"
        " ORDER BY created_at DESC"
    )
    return json_reply({
        "device_id": st.device_id,
        "device_name": st.device_name,
        "items": [{
            "client_id": r["id"],
            "name": r["name"],
            "created_at": r["created_at"],
            "last_seen": r["last_seen"],
            "revoked": bool(r["revoked"]),
            # AC-5.10：过期时刻要能看出来。`None` = 永不过期
            # （老库升级上来的行，或 `auth.token_ttl_days = 0`）。
            # 只回一个布尔"是否已过期"的话用户无法提前处理
            # ——"这台手机 3 天后掉线"和"已经掉线了"需要完全不同的动作。
            "expires_at": r["expires_at"],
            "expired": bool(r["expires_at"] is not None
                            and r["expires_at"] <= now),
        } for r in rows],
    })


async def panel_audit(ctx: Ctx) -> Reply:
    """`GET /panel/audit?limit=100` —— 读审计日志尾部。

    直接读 `data/audit.log` 文件而不是 `audit_log` 表：文件是**主记录**
    （先写文件并 fsync，再写表），表只是索引。排查"照片怎么没了"
    时必须看最原始的那份，表的写入失败是刻意被容忍的。

    实现已合并到 `backend/app/audit.py` —— 此前这里、`handlers/browse.py`
    与 `uploads.py` 各有一份互不一致的写实现（其中只有一份 fsync、
    只有一份写表），详见该模块的 docstring。
    """
    if not is_loopback(ctx):
        raise NotFound(ctx.path)
    st = _st(ctx)
    limit = ctx.q_int("limit", 100, minimum=1, maximum=1000)
    items, truncated = audit.read_entries(st.cfg, limit=limit)
    return json_reply({"items": items, "truncated": truncated})
