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

from ..auth import (
    PAIR_CODE_TTL,
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
from ..errors import E1001, E1015, E4001, AppError, NotFound, Unauthorized

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
        st.db, code, device_name=_client_name(ctx),
    )

    now = time.time()
    _audit(st, "pair", client_id, {"device_name": _client_name(ctx)})

    return json_reply({
        "client_token": token,
        "client_id": client_id,
        "device_id": st.device_id,
        "device_name": st.device_name,
        "expires_at": now + 90 * 86400,
        "lan_ips": st.cfg_server_ips(),
        "server_time": now,
    }, status=201)


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
    """`GET /api/devices` —— 已配对的手机列表。"""
    st = _st(ctx)
    require_client(ctx)
    rows = st.db.query(
        "SELECT id, name, created_at, last_seen, revoked FROM devices"
        " ORDER BY created_at DESC"
    )
    return json_reply({
        "items": [{
            "client_id": r["id"],
            "name": r["name"],
            "created_at": r["created_at"],
            "last_seen": r["last_seen"],
            "revoked": bool(r["revoked"]),
        } for r in rows],
    })


async def revoke_device(ctx: Ctx) -> Reply:
    """`DELETE /api/devices/{client_id}` —— 移除一台手机。

    ⚠️ 不删行，只置 `revoked=1`。删行会让 `audit_log.client_id` 变成
    指向不存在记录的悬空引用，事后追查"是谁删了这张照片"就断了线索。
    """
    st = _st(ctx)
    require_client(ctx)
    client_id = ctx.params["client_id"]

    row = st.db.query_one("SELECT id FROM devices WHERE id = ?", (client_id,))
    if row is None:
        raise NotFound(client_id)

    st.db.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (client_id,))
    _audit(st, "revoke", client_id, {})
    return empty_reply(204)


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
    rows = st.db.query(
        "SELECT id, name, created_at, last_seen, revoked FROM devices"
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
        } for r in rows],
    })


async def panel_audit(ctx: Ctx) -> Reply:
    """`GET /panel/audit?limit=100` —— 读审计日志尾部。

    直接读 `data/audit.log` 文件而不是 `audit_log` 表：文件是**主记录**
    （先写文件并 fsync，再写表），表只是索引。排查"照片怎么没了"
    时必须看最原始的那份，表的写入失败是刻意被容忍的。
    """
    if not is_loopback(ctx):
        raise NotFound(ctx.path)
    st = _st(ctx)
    limit = ctx.q_int("limit", 100, minimum=1, maximum=1000)

    path = st.cfg.audit_log_path
    if not path.exists():
        return json_reply({"items": [], "truncated": False})

    # 从文件尾部倒着读，避免把 10MB 日志整个读进内存。
    # 每行约 120 字节，limit 行取 400 倍字节数留足余量。
    raw = _tail(path, max_bytes=max(8192, limit * 400))
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    items = []
    for line in lines[-limit:]:
        parts = line.split("\t")
        items.append({
            "ts": parts[0] if len(parts) > 0 else "",
            "action": parts[1] if len(parts) > 1 else "",
            "device": parts[2] if len(parts) > 2 else "",
            "path": parts[3] if len(parts) > 3 else "",
            "detail": parts[4] if len(parts) > 4 else "",
        })
    items.reverse()   # 最新的在最上面
    return json_reply({"items": items, "truncated": len(lines) > limit})


def _tail(path: Any, *, max_bytes: int) -> str:
    """读文件尾部若干字节，并按 UTF-8 容错解码。

    ⚠️ 从中间截断可能切断一个多字节汉字，直接 decode 会抛
    UnicodeDecodeError 让整个面板 500。用 `errors="replace"` 兜住。
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
    except OSError as e:
        log.warning("读取审计日志失败：%s", e)
        return ""
    text = data.decode("utf-8", "replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]   # 丢掉可能残缺的首行
    return text


def _audit(st: Any, action: str, rel_path: str, detail: dict) -> None:
    """写审计日志。文件是主记录，写表失败只记日志。

    复用 handlers.common 的实现会引入循环导入（common 依赖 state），
    这里的逻辑与它一致：TAB 分隔，便于 awk/grep 事后追查。
    """
    import json

    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())
    line = (f"{ts}\t{action}\t{st.device_name}\t{rel_path}\t"
            f"{json.dumps(detail, ensure_ascii=False)}\n")
    try:
        path = st.cfg.audit_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:  # pragma: no cover
        log.warning("写审计日志文件失败：%s", e)

    try:
        st.db.execute(
            "INSERT INTO audit_log(ts, device_name, client_id, action, rel_path, detail)"
            " VALUES (?, ?, NULL, ?, ?, ?)",
            (time.time(), st.device_name, action, rel_path,
             json.dumps(detail, ensure_ascii=False)),
        )
    except Exception as e:  # noqa: BLE001 —— 审计写库失败不能影响业务
        log.debug("写审计日志表失败：%s", e)
