"""系统类接口：/api/system/**。

这些接口是手机端启动时的"握手"：先 ping 确认连对了电脑，再拉 info 渲染首页。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ..core import Ctx, Reply, json_reply
from ..errors import E1001, BadRequest
from ..state import AppState

log = logging.getLogger(__name__)

__all__ = ["ping", "info", "stats"]


def _state(ctx: Ctx) -> AppState:
    st = getattr(ctx, "app", None)
    if st is None:  # pragma: no cover —— 装配错误，宁可 500 也不要 AttributeError
        raise RuntimeError("AppState 未注入 Ctx（server 装配遗漏）")
    return st


async def ping(ctx: Ctx) -> Reply:
    """存活探测。

    带 device_id 是刻意的：手机切换 Wi-Fi 后可能连到**另一台**也跑着
    PhotoBridge 的电脑（或同一台电脑的旧实例）。手机记住配对的 device_id，
    ping 回来不一致就提示"连到了另一台电脑"，而不是把别人的照片显示出来。
    """
    st = _state(ctx)
    return json_reply({
        "pong": True,
        "ts": time.time(),
        "device_id": st.device_id,
        "device_name": st.device_name,
        "version": st.version(),
        "via": ctx.via,
    })


async def info(ctx: Ctx) -> Reply:
    """手机端首页所需的全部元信息（一次请求拿全，减少弱网往返）。"""
    st = _state(ctx)
    cfg = st.cfg
    sizes = st.thumb_sizes()

    return json_reply({
        "app": {
            "name": st.app_name(),
            "version": st.version(),
            "build": str(cfg.get("app.build", "")),
            "started_at": st.started_at,
            "uptime_sec": round(st.uptime_sec(), 1),
        },
        # 一期只有局域网，tunnel 恒为 offline；字段先留着，
        # 二期接上云端隧道后这里会变成 "ws" 而前端无需改动。
        "tunnel": {
            "mode": "offline",
            "cloud_url": str(cfg.get("cloud.base_url", "")),
            "connected": False,
            "session_id": None,
            "connected_at": None,
            "last_error": None,
        },
        "device": {
            "device_id": st.device_id,
            "device_name": st.device_name,
        },
        "roots": st.roots_info(),
        "index": st.index_info(),
        "watch": st.watch_info(),
        "thumbs": {
            "cache_bytes": _thumb_cache_bytes(st),
            "cache_files": _thumb_cache_files(st),
            "sizes": list(sizes),
            "quality": int(cfg.get("thumbs.quality", 78)),
            "format": str(cfg.get("thumbs.format", "webp")),
        },
        "server": {
            "host": str(cfg.get("server.host", "127.0.0.1")),
            "port": int(cfg.get("server.port", 8787)),
            "lan_ips": _lan_ips(),
            "bind_scope": "private",
        },
        "limits": st.limits_info(),
        # 电脑端外壳的状态。手机端在"设置 → 电脑端"里展示，
        # 让用户知道自启有没有生效（他没法直接看电脑的注册表）。
        "host_app": _host_app_info(),
        "time": {
            "server_time": time.time(),
            "tz_offset_minutes": int(cfg.tz_offset_minutes),
        },
    })


async def stats(ctx: Ctx) -> Reply:
    """运维统计。给"设置"页的诊断信息用，也是排障时的第一手数据。"""
    st = _state(ctx)
    db = st.db

    def _count(sql: str, *params: Any) -> int:
        return int(db.scalar(sql, params, default=0) or 0)

    total = _count("SELECT COUNT(*) FROM media WHERE missing = 0 AND kind = 'image'")
    images = _count("SELECT COUNT(*) FROM media WHERE missing = 0 AND kind = 'image'")
    missing = _count("SELECT COUNT(*) FROM media WHERE missing = 1")
    pending = _count("SELECT COUNT(*) FROM media WHERE thumb_state='pending' AND missing=0")
    ok = _count("SELECT COUNT(*) FROM media WHERE thumb_state='ok' AND missing=0")
    failed = _count("SELECT COUNT(*) FROM media WHERE thumb_state='failed' AND missing=0")
    open_uploads = _count(
        "SELECT COUNT(*) FROM upload_sessions WHERE status IN ('open','uploading')")

    return json_reply({
        "media": {
            "total": total,
            "images": images,
            "videos": 0,          # 一期不索引视频
            "missing": missing,
            "by_kind": {"image": images},
        },
        "thumbs": {
            "pending": pending,
            "ok": ok,
            "failed": failed,
            "cache_bytes": _thumb_cache_bytes(st),
            "cache_files": _thumb_cache_files(st),
        },
        "db": _db_stats(st),
        "uploads": {
            "open_sessions": open_uploads,
            "received_bytes": _count(
                "SELECT COALESCE(SUM(received),0) FROM upload_sessions"
                " WHERE status IN ('open','uploading')"),
        },
        "ts": time.time(),
    })


# --------------------------------------------------------------------------
# 内部统计
# --------------------------------------------------------------------------

def _thumb_cache_bytes(st: AppState) -> int:
    """缩略图缓存占用。用户会关心它涨到多大（可以安全删除，会自动重建）。"""
    total = 0
    root = st.cfg.data_path("thumbs")
    if not root.exists():
        return 0
    for p in root.rglob("*.webp"):
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return total


def _thumb_cache_files(st: AppState) -> int:
    root = st.cfg.data_path("thumbs")
    if not root.exists():
        return 0
    n = 0
    for _ in root.rglob("*.webp"):
        n += 1
    return n


def _db_stats(st: AppState) -> dict[str, Any]:
    db = st.db
    out: dict[str, Any] = {"size_bytes": db.db_size_bytes()}
    for key, sql in (
        ("page_count", "PRAGMA page_count"),
        ("freelist_count", "PRAGMA freelist_count"),
        ("page_size", "PRAGMA page_size"),
    ):
        try:
            out[key] = int(db.scalar(sql, default=0) or 0)
        except Exception:  # noqa: BLE001
            out[key] = None
    return out


def _lan_ips() -> list[str]:
    from ..config import local_ipv4_addresses
    return list(local_ipv4_addresses())


def _host_app_info() -> dict[str, Any]:
    """电脑端外壳（打包形态 / 托盘 / 开机自启）的状态。

    任何一项取不到都返回保守值而不是抛异常 —— 这是展示型字段，
    它失败不该让整个 `/api/system/info` 500（那会让手机端首页白屏）。
    """
    import sys

    out: dict[str, Any] = {
        "packaged": bool(getattr(sys, "frozen", False)),
        "tray": False,
        "autostart": {"supported": False, "enabled": False, "stale": False},
    }
    try:
        from ..autostart import status as autostart_status

        out["autostart"] = autostart_status()
    except Exception:       # noqa: BLE001
        pass
    try:
        from ..tray import is_tray_running

        out["tray"] = is_tray_running()
    except Exception:       # noqa: BLE001
        pass
    return out
