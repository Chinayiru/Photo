"""系统类接口：/api/system/**。

这些接口是手机端启动时的"握手"：先 ping 确认连对了电脑，再拉 info 渲染首页。
"""
from __future__ import annotations

import ipaddress

import asyncio
import logging
import ntpath
import os
import threading
import time
from pathlib import Path
from typing import Any, Final

from ..config import WILDCARD_HOSTS
from ..core import Ctx, Reply, json_reply
from ..errors import E1001, BadRequest
from ..state import AppState
from . import common

log = logging.getLogger(__name__)

__all__ = ["ping", "info", "stats", "entrances"]


def _masked_path(value: object) -> str:
    """Keep a directory hint without exposing the complete host path."""
    raw = str(value or "").replace("/", "\\")
    drive, tail = ntpath.splitdrive(raw)
    # A UNC share name is sensitive too; only preserve a local drive letter.
    if len(drive) != 2 or drive[1] != ":":
        drive = ""
    parts = [part for part in tail.split("\\") if part]
    if not parts:
        return drive or "..."
    leaf = parts[-1]
    return f"{drive}\\...\\{leaf}" if drive else f"...\\{leaf}"


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


def _public_base(cfg: Any) -> str:
    """`https://域名/d/<云端 device_id>`，没配云端或没配 device_id 时为空串。

    ★ 为什么不直接取 `client.status()["public_base"]`：那个值在**第一次
    握手成功之后**才有（`TunnelClient._device_id` 是在 `_handshake` 里
    填的）。但手机恰恰是在家里走局域网配对时学会云端基址的 —— 那时
    隧道可能正断着，而"断着"不该导致手机永远学不到云端地址（否则
    用户出门后才发现用不了，回家再配对一次）。

    所以这里从**配置**算。配置里的 `cloud.device_id` 是部署时
    `scripts/cloud_init.py` 写进去的，与握手时用的是同一个值
    （`backend/app/tunnel.py:cloud_device_id` 优先读配置、读不到才回退
    本地 id）。
    """
    try:
        from ..tunnel import cloud_device_id, public_base_for

        dev = cloud_device_id(cfg, "")
        if not dev:
            return ""
        return public_base_for(str(cfg.get("cloud.base_url", "") or ""), dev)
    except Exception:      # noqa: BLE001 —— 状态端点绝不能因此 500
        log.debug("计算云端基址失败", exc_info=True)
        return ""


def _tunnel_info(cfg: Any) -> dict[str, Any]:
    """隧道状态。

    ★ 从进程级单例读，而不是从配置推断。二期之前这里恒为 offline；
    接上隧道后，同一个字段名与同一组成员的含义保持不变，前端零改动
    （`docs/09` §9.6 的"业务代码零改动"包括这类"只读状态"端点）。

    没有隧道时仍返回同样的键，值取自配置 —— 手机端据此显示"云端未启用"
    而不是"未知"：两者的区别在于用户能不能自己修好。
    """
    empty = {
        "mode": "offline",
        "cloud_url": str(cfg.get("cloud.base_url", "")),
        "connected": False,
        "session_id": None,
        "connected_at": None,
        "last_error": None,
        # 手机端靠它学会自己的云端基址（`https://域名/d/<id>`）。
        # 为什么由电脑端上报而不是让用户手输：这个 URL 里的 `<id>` 是
        # **云端** device_id，与电脑本地 device_id 是两个不同的值
        # （`backend/app/tunnel.py:cloud_device_id`），用户拿不到；
        # 而电脑端在 HELLO 时就已经知道自己该出现在哪个前缀下。
        "public_base": _public_base(cfg),
    }
    try:
        from ..tunnel import get_tunnel

        client = get_tunnel()
    except Exception:      # noqa: BLE001 —— 状态端点绝不能因此 500
        return empty
    if client is None:
        return empty
    try:
        st = client.status()
    except Exception:      # noqa: BLE001
        log.debug("读取隧道状态失败", exc_info=True)
        return empty
    for k, v in empty.items():
        st.setdefault(k, v)
    if not st.get("public_base"):
        st["public_base"] = _public_base(cfg)
    return st


def _bind_scope(host: str) -> str:
    """从**实际监听地址**推导绑定范围。

    之前这里是一个硬编码的字符串 `"private"`，与 `server.host` 毫无关系：
    用户为了手机能连上把 `server.host` 改成 `0.0.0.0`，设置页照样显示
    "仅私有网络" —— 这是把"已经对外开放"说成"没对外开放"，正好反了。
    反向也一样糟：绑在 `127.0.0.1` 时不说清"只有本机能连"，用户会一直
    怀疑手机或防火墙有问题（`docs/11` SEC-17）。

    取值：

    * `loopback` —— 只有本机能连（`127.0.0.1` / `::1` / `localhost`）
    * `private`  —— 私有网段与"公网不可达"的段（RFC1918 / CGNAT / 链路本地 /
      IPv6 ULA / 保留段）
    * `public`   —— 某个具体公网地址
    * `any`      —— `0.0.0.0` / `::`，所有网卡
    * `custom`   —— 认不出的写法（主机名等），不做猜测
    """
    value = (host or "").strip().strip("[]").lower()
    if value in ("", "localhost", "127.0.0.1", "::1"):
        return "loopback"
    # 字面量只准出现在 `config.WILDCARD_HOSTS`（规则 18 的门控放行处）：
    # 这里写成 `in WILDCARD_HOSTS` 而不是再抄一遍 "0.0.0.0"/"::"，否则
    # `scripts/check_lint.py` 的规则 18 会判"绑定地址出现在 _bind_scope()"，
    # 而这不是绑定，只是分类。
    if value in WILDCARD_HOSTS:
        return "any"
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return "custom"
    if ip.is_loopback:
        return "loopback"
    # `is_private` 在 3.12 里不覆盖 100.64.0.0/10（CGNAT）这类"不可全局路由"
    # 的段，而它们恰恰是运营商大内网 —— 手机看到的网段就在这里。用
    # `not is_global` 兜住：只要是"公网到不了"的地址，就按私有处理。
    if ip.is_private or ip.is_link_local or not ip.is_global:
        return "private"
    return "public"


async def info(ctx: Ctx) -> Reply:
    """手机端首页所需的全部元信息（一次请求拿全，减少弱网往返）。

    ⚠️ 游客只看到**自己那个根**，且拿不到局域网 IP / 托盘状态 / 监视器状态：
    这些是主人的机器画像（家里网段、盘符、目录树），与"看照片"无关。
    """
    st = _state(ctx)
    guest = common.is_guest(ctx)
    visible = set(common.visible_root_ids(ctx, st))
    cfg = st.cfg
    host = str(cfg.get("server.host", "127.0.0.1"))
    sizes = st.thumb_sizes()
    cache_files, cache_bytes = await _thumb_cache_stats(st)

    return json_reply({
        "app": {
            "name": st.app_name(),
            "version": st.version(),
            "build": str(cfg.get("app.build", "")),
            "started_at": st.started_at,
            "uptime_sec": round(st.uptime_sec(), 1),
        },
        # 二期接上云端隧道后这里会变成 "ws" 而前端无需改动；
        # 一期/未配置时恒为 offline（见 _tunnel_info）。
        "tunnel": _tunnel_info(cfg),
        "device": {
            "device_id": st.device_id,
            "device_name": st.device_name,
        },
        "roots": [
            {**root, "path": _masked_path(root.get("path"))}
            for root in st.roots_info()
            if not guest or root.get("id") in visible
        ],
        "index": st.index_info(),
        # 视频能力（ISSUE-055）。`ffmpeg` 为空串 = 没找到可执行文件，
        # 那台机器上视频全是占位图；用户需要知道"这是缺依赖，不是文件坏了"。
        "video": _video_info(cfg),
        "watch": {} if guest else st.watch_info(),
        "thumbs": {
            "cache_bytes": cache_bytes,
            "cache_files": cache_files,
            "sizes": list(sizes),
            "quality": int(cfg.get("thumbs.quality", 78)),
            "format": str(cfg.get("thumbs.format", "webp")),
        },
        "server": {
            "host": host,
            "port": int(cfg.get("server.port", 8787)),
            "lan_ips": [] if guest else _lan_ips(),
            # 由实际监听地址推导，不是常量（SEC-17）
            "bind_scope": _bind_scope(host),
        },
        "limits": st.limits_info(),
        # 电脑端外壳的状态。手机端在"设置 → 电脑端"里展示，
        # 让用户知道自启有没有生效（他没法直接看电脑的注册表）。
        "host_app": {} if guest else _host_app_info(),
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

    # 游客的数字只能是"他自己那个根"的：把全库总量报回去等于告诉他
    # "这台电脑上有 5000 张照片"，与"只让他看这次旅行的相册"相悖。
    guest = common.is_guest(ctx)
    visible = common.visible_root_ids(ctx, st)
    scope = " AND root_id = ?" if guest and visible else ""
    args: tuple[Any, ...] = (visible[0],) if scope else ()

    images = _count(
        "SELECT COUNT(*) FROM media WHERE missing = 0 AND kind = 'image'" + scope, *args)
    videos = _count(
        "SELECT COUNT(*) FROM media WHERE missing = 0 AND kind = 'video'" + scope, *args)
    total = images + videos
    missing = _count(
        "SELECT COUNT(*) FROM media WHERE missing = 1" + scope, *args)
    pending = _count(
        "SELECT COUNT(*) FROM media WHERE thumb_state='pending' AND missing=0" + scope, *args)
    ok = _count(
        "SELECT COUNT(*) FROM media WHERE thumb_state='ok' AND missing=0" + scope, *args)
    failed = _count(
        "SELECT COUNT(*) FROM media WHERE thumb_state='failed' AND missing=0" + scope, *args)
    open_uploads = _count(
        "SELECT COUNT(*) FROM upload_sessions WHERE status IN ('open','uploading')")
    cache_files, cache_bytes = await _thumb_cache_stats(st)

    return json_reply({
        "media": {
            "total": total,
            "images": images,
            "videos": videos,
            "missing": missing,
            "by_kind": {"image": images, "video": videos},
        },
        "thumbs": {
            "pending": pending,
            "ok": ok,
            "failed": failed,
            "cache_bytes": cache_bytes,
            "cache_files": cache_files,
        },
        "db": {} if guest else _db_stats(st),
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
    """缩略图缓存占用。用户会关心它涨到多大（可以安全删除，会自动重建）。

    ⚠️ 已不再直接遍历目录，改由 `_thumb_cache_stats()` 一次算出两列。
    保留这个函数只为兼容（它现在走同一份 memo，不会再多跑一遍遍历）。
    """
    return _thumb_cache_stats_sync(st)[1]


def _thumb_cache_files(st: AppState) -> int:
    """缓存文件数。同上：与 `_thumb_cache_bytes` 共用同一次遍历。"""
    return _thumb_cache_stats_sync(st)[0]


#: 缓存统计的 memo 有效期（秒）。
#:
#: 60 秒的依据：这个数字是给"设置 → 存储"看的趋势量，用户不会指望它
#: 秒级精确；而它背后是**整目录遍历**，代价与照片数量线性相关。
_THUMB_CACHE_TTL: Final[float] = 60.0

#: `(算出的 monotonic 时刻, 文件数, 总字节)`，None 表示还没算过。
#: 模块级而不是挂在 `AppState` 上：它是纯展示用的派生数据，丢了只是重算
#: 一次；挂到 AppState 会让它跟着状态对象的生命周期走，反而要处理"配置/库根
#: 换了之后 memo 失效"的问题。
_thumb_cache_memo: tuple[str, float, int, int] | None = None

#: 保证同一时刻只有**一个**线程在遍历目录。
#:
#: ★ memo 之外还必须有一把锁：memo 只挡"算完之后"的重复调用，
#: 挡不住"冷启动瞬间涌入的一批请求" —— 它们会各自发现 memo 为空，
#: 然后各跑一遍遍历（5 个并发 = 5 倍系统调用，正是要消除的那个问题）。
#: 第一个线程持锁算，其余线程在锁上等，等到时 memo 已就绪直接返回。
_thumb_cache_lock = threading.Lock()


def _scan_thumb_cache(root: Path) -> tuple[int, int]:
    """一次遍历同时算出 `(文件数, 总字节)`。

    用 `os.scandir` 递归而不是两次 `rglob("*.webp")`：
      * 原来 `cache_bytes` 与 `cache_files` 各跑一次 rglob + 逐文件 stat，
        等于把同一棵树遍历两遍、把同一个 stat 做两遍。10 万张照片（两级
        尺寸 = 20 万个文件）时一次请求就是 40 万次系统调用，而这两个数字
        只是"设置"页的一行展示 —— 手机端点开该页即可把事件循环占满，
        连 SSE 推送都停摆（自我 DoS）。
      * `scandir` 的 `DirEntry.stat()` 在 Windows 上直接取自
        `FindFirstFile`/`FindNextFile` 已经返回的数据，不需要额外 syscall；
        `rglob` + `Path.stat()` 每条要单独再问一次文件系统。
    """
    files = 0
    total = 0
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif (entry.name.lower().endswith(".webp")
                              and entry.is_file(follow_symlinks=False)):
                            files += 1
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        # 单个文件被删/被占用不该让整次统计失败
                        continue
        except OSError:
            continue
    return files, total


def _thumb_cache_stats_sync(st: AppState) -> tuple[int, int]:
    """带 60 秒 TTL 的 `(文件数, 总字节)`。阻塞调用，只能在 executor 里跑。"""
    global _thumb_cache_memo

    # ★ memo 的键必须包含 root。只按时间缓存的话，同一进程里换了库
    # （测试换 tmp 目录、或用户改了 data 目录）会把**另一个目录**的
    # 数字当成自己的返回，而且 60 秒内都错 —— 表现是"缓存文件数是 0"。
    root = st.cfg.data_path("thumbs")
    key = str(root)

    now = time.monotonic()
    memo = _thumb_cache_memo
    if memo is not None and memo[0] == key and now - memo[1] < _THUMB_CACHE_TTL:
        return memo[2], memo[3]

    with _thumb_cache_lock:
        # 二次检查：等锁期间别的线程可能已经算完了
        now = time.monotonic()
        memo = _thumb_cache_memo
        if (memo is not None and memo[0] == key
                and now - memo[1] < _THUMB_CACHE_TTL):
            return memo[2], memo[3]

        files, total = _scan_thumb_cache(root) if root.exists() else (0, 0)
        _thumb_cache_memo = (key, time.monotonic(), files, total)
        return files, total


async def _thumb_cache_stats(st: AppState) -> tuple[int, int]:
    """`(文件数, 总字节)` —— 遍历放 executor，不占事件循环。

    返回顺序与 `_thumb_cache_stats_sync` 一致：**先文件数、后总字节**。
    这两个值类型都是 int，调用处解包反了不会报错，只会让"设置 → 存储"里
    的两行数字互换（而且请求照样 200）—— 所以顺序写在这里当契约。

    即便命中 memo，走的也是 executor：这个函数在 `info` / `stats` 的
    返回组装路径上，而 memo 过期的那一次调用仍然是**几万次系统调用**，
    直接在协程里跑会把整个事件循环（含 SSE 心跳）卡住若干秒。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _thumb_cache_stats_sync, st)


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


def _video_info(cfg: Any) -> dict[str, Any]:
    """视频能力：开关 + ffmpeg/ffprobe 是否找得到。

    拿不到就返回保守值（`available: False`）而不是抛 —— 与
    `_host_app_info` 同理：展示字段失败不该让首页白屏。
    """
    out: dict[str, Any] = {
        "enabled": bool(getattr(cfg, "video_enabled", False)),
        "available": False,
        "ffmpeg": "",
        "ffprobe": "",
        "exts": [],
    }
    try:
        from .. import video as videomod

        out["exts"] = sorted(videomod.VIDEO_EXTS_INDEX)
        if not out["enabled"]:
            return out
        ff = videomod.find_bin("ffmpeg", configured_dir=str(cfg.video.get("ffmpeg_dir") or ""))
        fp = videomod.find_bin("ffprobe", configured_dir=str(cfg.video.get("ffmpeg_dir") or ""))
        out["ffmpeg"], out["ffprobe"] = ff, fp
        out["available"] = bool(ff and fp)
    except Exception:       # noqa: BLE001
        pass
    return out


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

        status = autostart_status()
        # Command lines may contain the Windows user name and full executable path.
        status["command"] = None
        status["expected"] = None
        out["autostart"] = status
    except Exception:       # noqa: BLE001
        pass
    try:
        from ..tray import is_tray_running

        out["tray"] = is_tray_running()
    except Exception:       # noqa: BLE001
        pass
    return out

# --------------------------------------------------------------------------
# 候选入口地址
# --------------------------------------------------------------------------

def _served_port(ctx: Ctx, cfg: Any) -> int:
    """这次请求是从哪个端口进来的。

    用户就是从这个地址打开页面的，所以 Host 头里的端口最可信；取不到才退回
    配置（`--port` 启动时配置里未必同步）。
    """
    host = (ctx.header("host") or "").strip()
    if host:
        tail = host.rpartition(":")[2]
        if tail.isdigit() and tail.isascii():
            return int(tail)
    try:
        return int(cfg.get("server.port", 8787) or 8787)
    except (TypeError, ValueError):
        return 8787


async def entrances(ctx: Ctx) -> Reply:
    """候选入口地址（匿名）。

    配对页拿它把「家里 Wi-Fi / VPN / 云端」三条路都摆成可点即填，用户不必
    知道家里电脑的 IP。匿名是**必须**的：配对页在拿到令牌之前就要渲染。
    """
    from .. import entrances as entrances_mod

    st = _state(ctx)
    cfg = st.cfg
    data = entrances_mod.collect(cfg=cfg, port=_served_port(ctx, cfg))
    base = _public_base(cfg)
    data["cloud"] = (
        {"label": entrances_mod.CLOUD_LABEL, "host": base.split("://", 1)[-1],
         "url": f"{base.rstrip('/')}/"}
        if base else None
    )
    return json_reply(data)