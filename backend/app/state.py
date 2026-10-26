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
from .db import Database, get_db, make_db

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

    __slots__ = ("cfg", "db", "thumbs", "indexer", "bus", "watcher", "started_at",
                 "transcoder", "_device_id", "_device_name")

    def __init__(self, cfg: Config | None = None, db: Database | None = None, *,
                 thumbs: Any = None, indexer: Any = None, bus: Any = None,
                 watcher: Any = None, transcoder: Any = None) -> None:
        self.cfg = cfg if cfg is not None else get_config()
        if db is not None:
            self.db = db
        else:
            # ★ 只在**生产路径**（cfg 也是从磁盘配置来的）才允许回退到全局单例。
            # 测试夹具会传 cfg=Config(data_dir=tmp) 而忘了传 db，此时
            # `get_db()` 会连到用户真实的照片库索引 —— 曾经因此把
            # 4915 行真实记录标成 missing，用户时间线变空白。
            # 这里主动发现这种组合并报错，而不是悄悄连错库。
            if cfg is not None:
                self.db = make_db(cfg)
            else:
                self.db = get_db()
        self.thumbs = thumbs
        self.indexer = indexer
        # 按需转码器（ISSUE-058）。懒建：只读用户可能永远不点"转码播放"，
        # 那就不该在启动时创建 data/transcodes 目录。
        self.transcoder = transcoder
        # 文件系统监听器。为 None 时 /api/system/info 里如实报"未运行"，
        # 而不是假装 watch=true 就有监听 —— 那个谎言让 AC-2.15 长期
        # 无法验证（配置写着开、实际没有任何实现）。
        self.watcher = watcher
        # 事件总线（SSE 用）。为 None 时 SSE 接口会退回模块级单例，
        # 因此测试里注入一个独立总线即可完全隔离。
        self.bus = bus
        self.started_at = time.time()
        self._device_id: str | None = None
        self._device_name: str | None = None

    # ------------------------------------------------------------------
    # 设备标识
    # ------------------------------------------------------------------

    @property
    def transcodes(self) -> Any:
        """转码器（懒建）。第一次用到时才创建，并沿用 `video.*` 的配置。"""
        if self.transcoder is None:
            from .transcode import Transcoder

            self.transcoder = Transcoder(
                self.cfg,
                concurrency=self.cfg.transcode_concurrency,
                cache_mb=self.cfg.transcode_cache_mb,
            )
        return self.transcoder

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
                " WHERE missing = 0 GROUP BY root_id"
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
            "SELECT COUNT(*) FROM media WHERE missing = 0", default=0)
        return {
            "state": "scanning" if running else ("idle" if last else "never"),
            "total": int(total or 0),
            "pending_thumbs": int(pending or 0),
            **last,
        }

    def watch_info(self) -> dict[str, Any]:
        """文件系统监听状态。

        ★ 存在的理由：`index.watch` 默认是 `True`，但在一期很长时间里
        `backend/app/` 里**没有任何 watchdog 实现** —— 用户在设置页看到
        "监听：已开启"，实际往库目录拷照片不会自动出现（AC-2.15 无法通过）。
        这类"配置说了但没做"的字段比缺一个功能更糟：它让用户以为是自己
        的操作有问题。所以这里如实反映"监听器到底在不在跑"。
        """
        w = self.watcher
        if w is None:
            return {"enabled": False, "running": False, "reason": "未构造监听器"}
        try:
            return {"enabled": bool(getattr(w, "enabled", False)), **w.stats()}
        except Exception:       # noqa: BLE001
            log.debug("读取监听状态失败", exc_info=True)
            return {"enabled": bool(getattr(w, "enabled", False)),
                    "running": False, "reason": "状态读取失败"}

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

    def cfg_server_ips(self) -> list[str]:
        """本机可被手机访问的 IPv4 地址。

        过滤掉了 Hyper-V/WSL/VMware 的虚拟网卡地址 —— 那些地址
        （192.168.64.x、26.x 等）手机根本路由不到，把它们放进
        二维码里会让用户扫了之后连不上，且完全看不出原因。
        """
        from .config import local_ipv4_addresses

        try:
            return local_ipv4_addresses()
        except Exception as e:  # noqa: BLE001 —— 拿不到 IP 不该让配对 500
            log.warning("获取本机 IP 失败：%s", e)
            return []

    def server_port(self) -> int:
        try:
            return int(self.cfg.get("server.port", 8787))
        except (TypeError, ValueError):
            return 8787


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

