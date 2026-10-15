"""应用级共享状态：把 cfg / db / thumbs / indexer 组装成一个对象传给处理函数。

## 为什么不用模块级单例

处理函数需要访问数据库、缩略图引擎、索引器。若每个模块各自 `from .db import
get_db`，测试就没法注入一个临时库（只能改环境变量或 monkeypatch 模块属性，
两者都容易泄漏到其他测试）。

这里显式传一个 `AppState`，测试构造自己的实例即可，互不干扰。
"""
from __future__ import annotations

import logging
import os
import platform
import socket
import time
import uuid
from typing import Any

from .config import Config, get as get_config
from .db import Database, get_db

log = logging.getLogger(__name__)

__all__ = ["AppState", "get_state", "set_state"]

#: settings 表里存 device_id 的键名。
#: device_id 必须**持久**：手机靠它确认"这确实是我的那台电脑"，
#: 每次重启都换一个的话，手机每次都要重新配对。
_SETTING_DEVICE_ID = "device_id"
_SETTING_DEVICE_NAME = "device_name"
_SETTING_STARTED_AT = "server_started_at"


class AppState:
    """一次进程生命周期内的共享依赖。"""

    __slots__ = ("cfg", "db", "thumbs", "indexer", "started_at", "_device_id",
                 "_device_name")

    def __init__(self, cfg: Config | None = None, db: Database | None = None, *,
                 thumbs: Any = None, indexer: Any = None) -> None:
        self.cfg = cfg if cfg is not None else get_config()
        self.db = db if db is not None else get_db()
        self.thumbs = thumbs
        self.indexer = indexer
        self.started_at = time.time()
        self._device_id: str | None = None
        self._device_name: str | None = None

    # ------------------------------------------------------------------
    # 设备标识
    # ------------------------------------------------------------------

    @property
    def device_id(self) -> str:
        """本机稳定标识。首次调用时生成并落库。"""
        if self._device_id is None:
            v = self.db.get_setting(_SETTING_DEVICE_ID)
            if not v:
                v = uuid.uuid4().hex[:16]
                self.db.set_setting(_SETTING_DEVICE_ID, v)
                log.info("生成 device_id=%s", v)
            self._device_id = str(v)
        return self._device_id

    @property
    def device_name(self) -> str:
        """人类可读的机器名，显示在手机端"我的设备"里。

        用 hostname 而不是 IP：IP 会变（DHCP/换网络），hostname 不会，
        用户看到"DESKTOP-ABC"才能确认连的是哪台电脑。
        """
        if self._device_name is None:
            v = self.db.get_setting(_SETTING_DEVICE_NAME)
            if not v:
                v = _default_device_name()
                self.db.set_setting(_SETTING_DEVICE_NAME, v)
            self._device_name = str(v)
        return self._device_name

    # ------------------------------------------------------------------
    # 供 /api/system/info 用的聚合视图
    # ------------------------------------------------------------------

    def uptime_sec(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def mode(self) -> str:
        return str(self.cfg.get("app.mode", "readonly"))

    def version(self) -> str:
        return str(self.cfg.get("app.version", "0.1.0"))

    def app_name(self) -> str:
        return str(self.cfg.get("app.name", "PhotoBridge"))

    def roots_info(self, *, with_disk: bool = True) -> list[dict[str, Any]]:
        """每个根的 id/label/path/mode/数量/磁盘余量。

        数量用一次 GROUP BY 拿全，避免每个根一次查询（3 个根就是 3 次扫描）。
        """
        counts: dict[str, int] = {}
        try:
            for row in self.db.query(
                "SELECT root_id, COUNT(*) AS n FROM media"
                " WHERE missing = 0 AND kind = 'image' GROUP BY root_id"
            ):
                counts[str(row["root_id"])] = int(row["n"])
        except Exception:  # noqa: BLE001 —— 统计失败不该让 /info 整体 500
            log.exception("统计根目录数量失败")

        out: list[dict[str, Any]] = []
        for r in self.cfg.roots:
            item: dict[str, Any] = {
                "id": r.id,
                "label": r.label,
                "path": str(r.path),
                "mode": r.mode,
                "recursive": r.recursive,
                "count": counts.get(r.id, 0),
                "enabled": r.enabled,
            }
            if with_disk:
                item["disk_free"], item["disk_total"] = _disk_usage(r.path)
            out.append(item)
        return out

    def index_info(self) -> dict[str, Any]:
        """索引进度。首次索引 4915 张约 20 秒，用户需要看到进度。"""
        running = bool(getattr(self.indexer, "running", False))
        last: dict[str, Any] = {}
        stats = getattr(self.indexer, "last_stats", None)
        if stats:
            # last_stats 是 {root_id: ScanStats}
            total_seen = sum(int(getattr(s, "seen", 0)) for s in stats.values())
            total_added = sum(int(getattr(s, "added", 0)) for s in stats.values())
            total_updated = sum(int(getattr(s, "updated", 0)) for s in stats.values())
            total_missing = sum(int(getattr(s, "missing", 0)) for s in stats.values())
            elapsed = max((float(getattr(s, "elapsed", 0.0) or 0.0)
                           for s in stats.values()), default=0.0)
            finished = max((float(getattr(s, "finished_at", 0.0) or 0.0)
                            for s in stats.values()), default=0.0)
            last = {
                "scanned": total_seen,
                "added": total_added,
                "updated": total_updated,
                "removed": total_missing,
                "duration_sec": round(elapsed, 3),
                "last_scan_at": finished or None,
            }

        pending = self.db.scalar(
            "SELECT COUNT(*) FROM media WHERE thumb_state = 'pending' AND missing = 0",
            default=0,
        )
        total = self.db.scalar(
            "SELECT COUNT(*) FROM media WHERE missing = 0 AND kind = 'image'", default=0)
        return {
            "state": "scanning" if running else ("idle" if last else "never"),
            "total": int(total or 0),
            "pending_thumbs": int(pending or 0),
            **last,
        }

    def limits_info(self) -> dict[str, Any]:
        return {
            "max_file_bytes": int(self.cfg.get("upload.max_file_mb", 2048)) * 1024 * 1024,
            "chunk_size": int(self.cfg.get("upload.chunk_mb_lan", 8)) * 1024 * 1024,
            "upload_concurrency": 3,
            "session_ttl_hours": int(self.cfg.get("upload.session_ttl_hours", 24)),
        }

    def thumb_sizes(self) -> tuple[int, ...]:
        raw = self.cfg.thumbs.get("sizes") or [320, 1600]
        try:
            sizes = tuple(sorted({int(s) for s in raw}))
        except (TypeError, ValueError):
            log.warning("thumbs.sizes 配置非法（%r），回退默认值", raw)
            return (320, 1600)
        return sizes or (320, 1600)


def _default_device_name() -> str:
    try:
        h = socket.gethostname()
    except OSError:
        h = ""
    if h:
        return h
    return platform.node() or "PhotoBridge"


def _disk_usage(path: Any) -> tuple[int | None, int | None]:
    """返回 (剩余字节, 总字节)。取不到时为 (None, None)。

    磁盘满会让上传失败，用户需要在"设置"里提前看到余量。
    """
    try:
        u = os.statvfs(str(path))  # type: ignore[attr-defined]
        return u.f_bavail * u.f_frsize, u.f_blocks * u.f_frsize
    except AttributeError:
        pass
    except OSError:
        return None, None
    try:
        import shutil
        usage = shutil.disk_usage(str(path))
        return usage.free, usage.total
    except OSError:
        return None, None


# --------------------------------------------------------------------------
# 进程级当前实例（供 server/main 与托盘使用；测试自己构造 AppState）
# --------------------------------------------------------------------------

_state: AppState | None = None


def get_state() -> AppState:
    global _state
    if _state is None:
        _state = AppState()
    return _state


def set_state(state: AppState | None) -> None:
    global _state
    _state = state
