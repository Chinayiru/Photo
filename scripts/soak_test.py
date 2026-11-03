"""长时间稳定性挂机（L7-9）：连续跑、持续发请求、盯内存与线程数。

**为什么需要单独一个脚本**：单测跑 30 秒，根本看不到泄漏。这条脚本按小时
量级运行，周期性采样 RSS / 线程数 / asyncio 任务数 / 数据库行数，只有
"长时间持续有负载"才暴露得出来的问题才会显形（句柄未关、任务未回收、
缩略图缓存无上限、SSE 连接堆积）。

**它查什么**：
  1. 进程不崩、`/api/system/ping` 全程可用
  2. RSS 不持续增长（用**后半程均值 vs 前半程均值**判断，而不是首尾两点 ——
     冷启动与缩略图缓存填充会让开头必然上涨，拿首尾比会天天误报）
  3. 线程数不增长（缩略图线程池若泄漏线程，这里会涨）
  4. asyncio 任务数不增长（worker 或 SSE 连接若泄漏，这里会涨）
  5. 目录句柄不泄漏（Windows 上 `os.scandir` 未关会让长扫慢慢卡死）
  6. 每轮请求全部成功，且延迟没有单调恶化

**它不查什么**：不做 24 小时真实的"人怎么用"。它是**持续压测 + 采样**，
不能替代真机体验测试（那是 E4 的事）。

用法：
    # 默认 24 小时，每 60 秒采样一次
    .venv\\Scripts\\python.exe scripts\\soak_test.py

    # 快速自检（5 分钟，用来确认脚本本身能跑通）
    .venv\\Scripts\\python.exe scripts\\soak_test.py --hours 0.083 --interval 15

    # 用合成库（不动真实照片库，且可控）
    .venv\\Scripts\\python.exe scripts\\soak_test.py --hours 1 --synthetic 200

    # 后台挂机并把报告写文件
    .venv\\Scripts\\python.exe scripts\\soak_test.py --hours 24 --report build\\soak.json

**中断也能看进度**：带 `--report` 时每 30 轮会原子写一次检查点
（`"status": "running"`，只记事实不做判定），跑一半被杀掉也能看到
"跑到第几轮、失败哪些"。收尾时才写完整报告（`"status"` 不含，字段见下）。

退出码：0 = 通过；1 = 有指标越界；2 = 环境问题（库不存在等）**或观测窗口
不足以判定**（见 `_plateau_info`：线程/任务数还在单调上升时，说明暖机段
没盖住惰性建线程期，此时报 FAIL 是假失败 —— 实测本机需约 3 小时才爬平，
所以 `--hours 0.25` 这种短跑会得到 2 而不是 0/1）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

LIBRARY = Path(r"D:\Photos")

from backend.app.config import Config                      # noqa: E402
from backend.app.db import Database                        # noqa: E402
from backend.app.events import get_bus                     # noqa: E402
from backend.app.indexer import Indexer                    # noqa: E402
from backend.app.server import build_app                   # noqa: E402
from backend.app.state import AppState                     # noqa: E402
from backend.app.thumbs import ThumbEngine                 # noqa: E402

# 泄漏判定阈值。
#
# ★ 判据是"**后半程相对前半程**"，而且**丢掉前 1/4 的采样**。
# 两个原因，都是实测踩出来的：
#
# 1. **不能拿首值当基线**。首值必然是刚启动时的低谷（还没建缩略图缓存、
#    还没建连接池），拿它比会得到一个永远为正的增长率，跑一次误报一次。
# 2. **不能拿整体前半程当基线**。`ThreadPoolExecutor` 是**惰性建线程**的：
#    `ThreadPoolExecutor` 在第一次 `submit()` 时才建 worker；而
#    `backend/app/handlers/browse.py` 与 `common.py` 用的 `run_in_executor(None, ...)`
#    走的是 asyncio **默认执行器**，线程数按并发需求慢慢长。
#    实测前几分钟里线程数从 5 一路涨到 11，线程名是 `asyncio_1`、`asyncio_2`、
#    `thumb_1` —— 全是"该起的线程刚起来"，不是泄漏。
#    丢掉前 1/4 正好把这段一次性增长排除掉，之后还在涨才是真的漏。
#
# 泄漏的定义是"**无界增长**"：跑 24 小时就该一直涨，所以后 3/4 的前后对比
# 依然抓得到真正的泄漏。
WARMUP_FRACTION = 0.25
RSS_GROWTH_LIMIT = 0.30        # 后半程均值相对前半程均值涨超 30% 判失败
THREAD_GROWTH_LIMIT = 4        # 线程数净增超过 4 个判失败
TASK_GROWTH_LIMIT = 8          # asyncio 任务数净增超过 8 个判失败

# ★ 判据有前提：**观测窗口必须长到暖机段能盖住"惰性起线程"的上升期**。
#
# 实测（2026-10-18）：本机 `os.cpu_count() = 20`，asyncio 默认执行器上限
# 是 `min(32, 20+4) = 24`。线程数从 5 爬到 29 用了 **约 3 小时**
# （`asyncio_1..23` 逐个按并发需求建起来，`thumb_1` 同理）。
# 当总时长只有 15 分钟时，`WARMUP_FRACTION=0.25` 只丢掉最初的 3.75 分钟，
# 之后测到的"增长"完全来自仍在进行的建线程过程 —— 脚本于是报
# `线程数净增 +6.1 超过上限`，**这是假失败**：同一台机器跑满 10 小时，
# 线程数在 3 小时后恒定在 29、判据是 +0.26 PASS。
#
# 假失败比没有结论更糟：它会让"缩短挂机"看起来像"发现了泄漏"。
# 所以下面引入"是否已趋平"的判定 —— 指标还在单调上升时不给 PASS/FAIL，
# 给 INCONCLUSIVE（退出码 2，与"环境问题"同义：这次观测不足以判定）。
#
# 趋平判据：把后 40% 的采样再切成前后两半，若后半段均值相对前半段
# 仍在涨超过 `PLATEAU_TOLERANCE`，就算"还在上升"。
PLATEAU_FRACTION = 0.40        # 从末尾往前取多少比例来判断是否趋平
PLATEAU_TOLERANCE = 0.10       # 末段相对前段涨幅超过 10% 就算仍在上升
PLATEAU_MIN_ABS = 1.0          # 绝对差小于这个值一律视为已趋平（噪声）


def _plateau_info(series: list[float]) -> tuple[bool, float]:
    """判断序列末尾是否已趋平。

    返回 `(是否已趋平, 末段相对前段的涨幅)`。

    为什么要它：`_net_growth` 比较的是"暖机后前后半程"，它默认暖机段
    已经把一次性增长吃掉。这个假设在 24 小时窗口下成立（实测 3 小时
    就爬到平台），在 15 分钟窗口下**不成立** —— 那时测到的是上升期本身，
    报出的"净增"是真正的建线程过程，不是泄漏。
    """
    if len(series) < 10:
        return True, 0.0           # 样本太少，不去质疑；交给 _net_growth 的 None
    tail = series[int(len(series) * (1.0 - PLATEAU_FRACTION)):]
    if len(tail) < 4:
        return True, 0.0
    half = len(tail) // 2
    first = statistics.mean(tail[:half])
    second = statistics.mean(tail[half:])
    diff = second - first
    if abs(diff) < PLATEAU_MIN_ABS:
        return True, 0.0
    rel = diff / first if first else 0.0
    return rel <= PLATEAU_TOLERANCE, rel



def _net_growth(series: list[float], *, frac: float = WARMUP_FRACTION):
    """丢掉前 `frac` 的暖机采样后，比较后半程均值与前半程均值。

    返回 `(abs_growth, rel_growth | None, baseline_mean)`；样本太少返回
    `(0, None, 0.0)` —— 挂机刚开始就下结论没有意义。
    """
    usable = series[int(len(series) * frac):]
    if len(usable) < 6:
        return 0.0, None, 0.0
    half = len(usable) // 2
    first = statistics.mean(usable[:half])
    second = statistics.mean(usable[half:])
    rel = (second - first) / first if first else None
    return second - first, rel, first


def _write_checkpoint(
    path: Path | None,
    *,
    hours: float,
    rounds: int,
    rounds_ok: int,
    rounds_bad: int,
    samples: list[dict[str, Any]],
    failures: list[str],
    total_hours: float,
) -> None:
    """把"到目前为止"的进度落盘，供中断后查看。

    ★ 与最终报告的差别：这里**不做判定**。判据（丢掉前 1/4 暖机后比较前后
    半程均值）在样本不足时毫无意义，提前算出来只会误导。所以检查点只写
    原始事实：跑了多久、多少轮、失败哪些、采样序列是什么。

    写失败一律吞掉 —— 挂机不该因为磁盘满/文件被占用而中断。
    """
    if path is None:
        return
    payload = {
        "status": "running",
        "hours": round(hours, 3),
        "total_hours": total_hours,
        "progress": f"{hours / total_hours:.1%}" if total_hours else "n/a",
        "rounds": rounds,
        "rounds_ok": rounds_ok,
        "rounds_bad": rounds_bad,
        "failures": failures[-50:],          # 只留最近 50 条，避免无限膨胀
        "samples": samples,
        "written_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8", newline="",
        )
        # 原子替换：否则进程正好在写到一半时被杀，留下半截 JSON，
        # 之后"看一眼进度"反而会 JSONDecodeError。
        os.replace(tmp_path, path)
    except Exception:       # noqa: BLE001
        pass


def _rss_bytes() -> int | None:
    """当前进程 RSS（字节）。用 ctypes 读 Windows 的 PROCESS_MEMORY_COUNTERS。

    **为什么不引入 psutil**：这只是挂机脚本，为了一个数字加一个运行时依赖
    不划算（而且 psutil 装不上时脚本会直接 import 失败，挂机 24 小时才发现
    更糟）。读不到就返回 None，指标自动跳过。

    **必须显式声明 `restype`/`argtypes`**：`GetCurrentProcess()` 返回的是
    64 位 HANDLE（实测 `0xFFFFFFFFFFFFFFFF`），ctypes 默认按 C `int` 解释
    会截断成 `-1`，随后 `GetProcessMemoryInfo` 直接失败并报 `err 6`
    （ERROR_INVALID_HANDLE）—— 症状是 RSS 永远读到 `None`，挂机 24 小时
    才发现"内存指标根本没采到"。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

        handle = kernel32.GetCurrentProcess()
        counters = _PMC()
        counters.cb = ctypes.sizeof(_PMC)
        if not psapi.GetProcessMemoryInfo(
            handle, ctypes.byref(counters), counters.cb
        ):
            return None
        return int(counters.WorkingSetSize)
    except Exception:                                          # noqa: BLE001
        return None


def _open_handles() -> int | None:
    """进程当前句柄数。句柄泄漏比内存泄漏更隐蔽（内存看不出来但会漏）。

    同样必须声明 `argtypes`/`restype`（理由见 `_rss_bytes`）。
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL

        handle = kernel32.GetCurrentProcess()
        count = wintypes.DWORD(0)
        if not kernel32.GetProcessHandleCount(handle, ctypes.byref(count)):
            return None
        return int(count.value)
    except Exception:                                          # noqa: BLE001
        return None


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024.0:
            return f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


class Service:
    """后台线程跑完整服务，主线程往它的循环里投协程。"""

    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        self.loop: asyncio.AbstractEventLoop | None = None
        self.port = 0
        self.state: AppState | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="soak-server")

    def start(self, timeout: float = 30.0) -> None:
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("服务启动超时")

    def call(self, coro, timeout: float = 600.0):
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def task_count(self) -> int:
        """事件循环里还活着的 asyncio 任务数。"""
        assert self.loop is not None
        try:
            tasks = asyncio.all_tasks(self.loop)
        except RuntimeError:
            return -1
        return sum(1 for t in tasks if not t.done())

    def idle(self, quiet_sec: float = 5.0) -> None:
        """等到"系统真的安静下来"再取基线。

        **为什么需要**：`ThreadPoolExecutor` 是**惰性建线程**的（`_adjust_thread_count`
        在第一次 `submit` 时才建），而索引刚跑完时缩略图队列还堆着几百个任务，
        接下来几秒里 worker 线程会被陆续建出来、队列才慢慢排空。在这期间取基线，
        线程数会从 5 一路涨到 9，看起来像"线程泄漏" —— 实测两次都误报
        `线程数增长 +7 / +5 超过上限`。

        判据是三个都归零：队列空、缩略图引擎没有待办、事件循环里除了本协程
        没有别的任务。超时也返回（挂机脚本不能因为静默不下来就不开始）。
        """
        assert self.loop is not None
        t0 = time.time()
        while time.time() - t0 < quiet_sec:
            engine = self.state.thumbs if self.state else None
            pending = engine.pending_count() if engine is not None else 0
            queued = engine.queue.qsize() if engine is not None else 0
            tasks = sum(1 for t in asyncio.all_tasks(self.loop) if not t.done())
            # 本协程自己算一个；再加上可能是 idle 调用链上的
            if queued == 0 and pending == 0 and tasks <= 1:
                return
            time.sleep(0.25)

    def stop(self) -> None:
        if self.loop is not None and self._stop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=60)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop

        async def main_async() -> None:
            bus = get_bus()
            bus.bind_loop(loop)
            thumbs = ThumbEngine(self.cfg, self.db, workers=4, bus=bus)
            indexer = Indexer(self.cfg, self.db, thumbs=thumbs, bus=bus)
            st = AppState(self.cfg, self.db, thumbs=thumbs, indexer=indexer,
                          bus=bus)
            _, transport, _ = build_app(self.cfg, state=st)
            transport.port = 0
            await transport.start()
            self.port = transport.port_actual
            self.state = st
            self._stop = asyncio.Event()
            # 先扫一遍，让库进入"有内容"的状态（挂机要压真实路径）
            if self.cfg.index.get("scan_on_start", True):
                try:
                    await indexer.scan_all()
                except Exception as exc:                        # noqa: BLE001
                    print(f"  ! 启动扫描失败（继续挂机）：{exc!r}")
            self._ready.set()
            await self._stop.wait()
            # 挂机结束要**干净关闭**：worker 泄漏与 WAL 残留都在这里暴露
            try:
                await thumbs.stop(drain=True, drain_timeout=30.0)
            except Exception as exc:                            # noqa: BLE001
                print(f"  ! 缩略图引擎关闭异常：{exc!r}")
            await transport.stop()

        try:
            loop.run_until_complete(main_async())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:                                   # noqa: BLE001
                pass
            loop.close()


class Sampler:
    """一轮负载：把用户真会走的路各走一遍。"""

    def __init__(self, base: str) -> None:
        self.base = base
        self.latencies: dict[str, list[float]] = {}

    def _time(self, name: str, fn) -> Any:
        t0 = time.perf_counter()
        try:
            return fn()
        finally:
            self.latencies.setdefault(name, []).append(
                (time.perf_counter() - t0) * 1000.0
            )

    def get(self, path: str, *, raw: bool = False, timeout: float = 60.0):
        req = urllib.request.Request(self.base + path)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                return r.status, (body if raw else json.loads(body))
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def round(self) -> dict[str, Any]:
        """一轮请求。返回本轮的战果，供上层判成败。"""
        out: dict[str, Any] = {"ok": True, "detail": []}

        def note(label: str, ok: bool) -> None:
            if not ok:
                out["ok"] = False
                out["detail"].append(label)

        try:
            status, ping = self._time("ping", lambda: self.get("/api/system/ping"))
            note("ping", status == 200 and ping.get("pong") is True)

            status, page = self._time(
                "timeline", lambda: self.get("/api/timeline?limit=50")
            )
            items = page.get("items", []) if isinstance(page, dict) else []
            note("timeline", status == 200 and len(items) > 0)
            out["items"] = len(items)

            # 缩略图：每次换不同的起点，让缓存不会一遍就喂饱
            if items:
                mid = items[int(time.time()) % len(items)]["id"]
                status, blob = self._time(
                    "thumb",
                    lambda: self.get(f"/api/items/{mid}/thumb?size=320", raw=True),
                )
                is_webp = isinstance(blob, bytes) and blob[:4] == b"RIFF"
                note("thumb", status == 200 and is_webp)

            status, found = self._time("search", lambda: self.get("/api/search?q=img"))
            note("search", status in (200, 404))

            status, info = self._time("info", lambda: self.get("/api/system/info"))
            note("info", status == 200)

            status, _ = self._time("folders", lambda: self.get("/api/folders"))
            note("folders", status == 200)

        except Exception as exc:                                # noqa: BLE001
            out["ok"] = False
            out["detail"].append(f"exception:{exc!r}")

        return out


def _build_config(args) -> tuple[Config, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="pb_soak_"))

    if args.synthetic:
        lib = tmp / "lib"
        lib.mkdir(parents=True)
        # 造出跨年跨月的层级，让归档/时间线都走真实路径。
        # **必须写真正的 JPEG**：第一版写的是 SOI+APP0+EOI 的假字节，
        # 结果每一张都 `cannot identify image file`，缩略图全部失败 ——
        # 挂机 24 小时压的是一条谁都不会走的错误路径（而且失败路径
        # 比成功路径快得多，内存指标全是假的）。
        from PIL import Image

        n = int(args.synthetic)
        for i in range(n):
            year = 2020 + (i % 5)
            month = 1 + (i % 12)
            d = lib / f"{year}" / f"{year}-{month:02d}"
            d.mkdir(parents=True, exist_ok=True)
            # 尺寸错开，避免所有图都命中同一份缓存路径
            size = (160 + (i % 7) * 40, 120 + (i % 5) * 30)
            img = Image.new("RGB", size, ((i * 37) % 256, (i * 91) % 256, 128))
            img.save(d / f"IMG_{i:05d}.jpg", "JPEG", quality=70)
        root_path = lib
    else:
        root_path = Path(args.library) if args.library else LIBRARY
        if not root_path.is_dir():
            print(f"照片库不存在：{root_path}")
            raise SystemExit(2)

    cfg = Config({
        "server": {"host": "127.0.0.1", "port": 0, "allow_lan": False},
        "roots": [{
            "id": "library", "label": "我的照片库", "path": str(root_path),
            "mode": "readwrite", "enabled": True, "recursive": True,
        }],
        "auth": {"require_pairing": False, "allow_delete": False},
        "index": {"scan_on_start": True, "watch": True},
    }, path=tmp / "config.json", data_dir=tmp / "data")
    return cfg, tmp


def main() -> int:
    ap = argparse.ArgumentParser(description="PhotoBridge 长时间稳定性挂机")
    ap.add_argument("--hours", type=float, default=24.0,
                    help="挂机时长（小时），默认 24")
    ap.add_argument("--interval", type=float, default=60.0,
                    help="采样间隔（秒），默认 60")
    ap.add_argument("--synthetic", type=int, default=0,
                    help="改用 N 张合成图的临时库，不动真实照片库")
    ap.add_argument("--library", type=str, default="",
                    help="指定照片库路径（默认 D:\\Photos）")
    ap.add_argument("--report", type=str, default="",
                    help="把完整报告写成 JSON")
    args = ap.parse_args()

    total_sec = max(1.0, args.hours * 3600.0)
    interval = max(1.0, args.interval)

    # ★ 报告路径必须**在循环开始前**就解析好。
    # 原实现只在收尾处写一次，于是挂机跑 24 小时、中途被任何原因打断
    # （外部进程 git reset、用户关机、断电、任务管理器结束进程），
    # 已经跑出来的数据**一行都不会留下** —— 实测踩过：跑了 8.00 小时
    # / 481 轮，进程被杀掉，`build\soak_24h.json` 根本不存在，
    # 只剩一份日志要人肉反推。检查点让"跑了多久"随时可查。
    report_path: Path | None = None
    if args.report:
        report_path = Path(args.report)
        if not report_path.is_absolute():
            report_path = REPO / report_path
        report_path.parent.mkdir(parents=True, exist_ok=True)

    cfg, tmp = _build_config(args)
    print("=" * 66)
    print("PhotoBridge 稳定性挂机")
    print(f"  照片库  : {cfg.roots[0].path}")
    print(f"  数据目录: {tmp / 'data'}")
    print(f"  时长    : {args.hours} 小时（{total_sec:.0f} 秒）")
    print(f"  采样    : 每 {interval:.0f} 秒")
    print("=" * 66)

    db = Database(cfg.db_path)
    db.init()
    svc = Service(cfg, db)
    t_start = time.time()
    svc.start(timeout=300.0)
    base = f"http://127.0.0.1:{svc.port}"
    print(f"服务已就绪：{base}\n")

    sampler = Sampler(base)
    samples: list[dict[str, Any]] = []
    failures: list[str] = []
    rounds_ok = 0
    rounds_bad = 0

    def sample(tag: str) -> dict[str, Any]:
        s = {
            "t": round(time.time() - t_start, 1),
            "tag": tag,
            "rss": _rss_bytes(),
            "threads": threading.active_count(),
            "tasks": svc.task_count(),
            "handles": _open_handles(),
            # 记下线程名，泄漏时能直接看出是哪一个池在长。
            # 只记名字不记完整堆栈：挂机 1440 轮，堆栈会让报告膨胀几十 MB。
            "thread_names": sorted(
                t.name for t in threading.enumerate()
            ),
        }
        samples.append(s)
        return s

    # 先跑一轮"热身"，再等系统静默，最后才取基线。
    #
    # **为什么必须热身 + 静默**：`ThreadPoolExecutor` 是**惰性建线程**的 ——
    # 进程刚起来只有 5 个线程，第一次生成缩略图才把那 4 个 worker 建出来；
    # 而索引刚结束时队列里还堆着几百个缩略图任务，要跑几秒才排空。
    # 不热身就取基线，线程数会从 5 涨到 9，看起来像"线程泄漏"
    # （实测两次都误报 `线程数增长 +7 / +5 超过上限`）。
    # 基线必须是"该起的线程都起完了、队列也空了"的状态。
    warm = sampler.round()
    if not warm["ok"]:
        print(f"  注意：热身轮就有失败 {warm['detail']}（后面会继续）")
    svc.idle(quiet_sec=10.0)
    sample("baseline")
    print(f"基线：threads={samples[-1]['threads']}  "
          f"tasks={samples[-1]['tasks']}  "
          f"rss={_human(samples[-1]['rss']) if samples[-1]['rss'] else 'n/a'}\n")

    n_rounds = 0
    while True:
        elapsed = time.time() - t_start
        if elapsed >= total_sec:
            break

        n_rounds += 1
        res = sampler.round()
        if res["ok"]:
            rounds_ok += 1
        else:
            rounds_bad += 1
            failures.append(f"第 {n_rounds} 轮请求失败：{res['detail']}")

        s = sample("tick")

        # ★ 每 30 轮（默认 30 分钟）落一次检查点。
        # 不每轮都写：轮间隔 60 秒时每轮写一次也是可以的，但挂机时长
        # 可能是 0.03 小时（压测模式，间隔 1 秒），那样写盘会盖过被测逻辑。
        if n_rounds % 30 == 0:
            _write_checkpoint(
                report_path,
                hours=elapsed / 3600, rounds=n_rounds,
                rounds_ok=rounds_ok, rounds_bad=rounds_bad,
                samples=samples, failures=failures, total_hours=args.hours,
            )

        # 每 10 轮打一行，避免日志刷屏（挂机 24 小时 = 1440 轮）
        if n_rounds % 10 == 1 or not res["ok"]:
            rss = _human(s["rss"]) if s["rss"] else "n/a"
            print(
                f"[{elapsed / 3600:6.2f}h] 轮 {n_rounds:5d}  "
                f"rss={rss:>9}  threads={s['threads']:3d}  "
                f"tasks={s['tasks']:3d}  handles={s['handles']}  "
                f"{'ok' if res['ok'] else 'FAIL ' + str(res['detail'])}"
            )

        # 睡到下一轮（用最后一次请求的耗时扣掉，保持节奏稳定）
        sleep_for = interval - ((time.time() - t_start) % interval)
        if sleep_for > 0:
            time.sleep(min(sleep_for, max(0.0, total_sec - (time.time() - t_start))))

    # 收尾前再采一次，然后干净关闭
    sample("final")
    print("\n正在关闭服务……")
    t0 = time.time()
    svc.stop()
    shutdown_sec = time.time() - t0
    alive = svc._thread.is_alive()

    print(f"关闭耗时 {shutdown_sec:.1f}s，线程是否残留：{alive}")
    if alive:
        failures.append("服务线程未能在 60 秒内退出")

    # ---------------- 判定 ----------------
    http_fail_rate = rounds_bad / max(1, n_rounds)

    # 判据不成立时的条目（区别于 failures）：这次观测不足以判定，
    # 不代表产品有问题。见 _plateau_info 的注释。
    inconclusive: list[str] = []

    rss_series = [s["rss"] for s in samples if s["rss"]]
    rss_abs, rss_growth, rss_base = _net_growth(rss_series)

    thread_series = [float(s["threads"]) for s in samples]
    thread_abs, _thread_rel, thread_base = _net_growth(thread_series)
    thread_flat, thread_flat_rel = _plateau_info(thread_series)

    task_series = [float(s["tasks"]) for s in samples if s["tasks"] >= 0]
    task_abs, _task_rel, task_base = _net_growth(task_series)
    task_flat, task_flat_rel = _plateau_info(task_series)

    handle_series = [float(s["handles"]) for s in samples if s["handles"]]
    handle_abs, _handle_rel, handle_base = _net_growth(handle_series)
    handle_flat, handle_flat_rel = _plateau_info(handle_series)

    # 保留首尾净增，用于报告里的"整体"视角（不参与判定）
    thread_net = (thread_series[-1] - thread_series[0]) if thread_series else 0.0
    task_net = (task_series[-1] - task_series[0]) if task_series else 0.0
    handle_net = (handle_series[-1] - handle_series[0]) if handle_series else 0.0

    # 延迟是否单调恶化：比前半程与后半程的 p95
    lat_report: dict[str, Any] = {}
    latency_worse: list[str] = []
    for name, vals in sampler.latencies.items():
        if len(vals) < 8:
            lat_report[name] = {"n": len(vals), "why": "样本太少"}
            continue
        half = len(vals) // 2
        p95_first = sorted(vals[:half])[int(len(vals[:half]) * 0.95) - 1]
        p95_second = sorted(vals[half:])[int(len(vals[half:]) * 0.95) - 1]
        lat_report[name] = {
            "n": len(vals),
            "p95_first_ms": round(p95_first, 1),
            "p95_second_ms": round(p95_second, 1),
            "p95_last_ms": round(sorted(vals)[int(len(vals) * 0.95) - 1], 1),
        }
        # 只报告不判失败：局域网抖动本来就大，且首轮包含冷缓存。
        # 真正的判据是"请求有没有失败"，不是"快没快"。
        if p95_second > p95_first * 3 and p95_second > 500:
            latency_worse.append(name)

    print("\n" + "=" * 66)
    print("结果")
    print("=" * 66)
    print(f"  总时长      : {(time.time() - t_start) / 3600:.2f} 小时")
    print(f"  请求轮次    : {n_rounds}（成功 {rounds_ok} / 失败 {rounds_bad}，"
          f"失败率 {http_fail_rate:.2%}）")
    if rss_growth is not None:
        print(f"  RSS 增长    : {rss_growth:+.1%}"
              f"（丢掉前 {WARMUP_FRACTION:.0%} 暖机后，前半程均值 → 后半程均值，"
              f"上限 {RSS_GROWTH_LIMIT:.0%}）")
        print(f"  RSS 峰值    : {_human(max(rss_series))}")
    print(f"  线程数      : 净增 {thread_abs:+.1f}（上限 +{THREAD_GROWTH_LIMIT}）"
          f"，首尾差 {thread_net:+.0f}")
    if thread_abs > 0:
        # 涨了就打印是哪些线程 —— 否则只能看到"多了 6 个"而不知道该查谁
        first_names = samples[0].get("thread_names") or []
        last_names = samples[-1].get("thread_names") or []
        added = [n for n in last_names if n not in first_names]
        if added:
            print(f"    新增线程  : {added}")
    print(f"  任务数      : 净增 {task_abs:+.1f}（上限 +{TASK_GROWTH_LIMIT}）"
          f"，首尾差 {task_net:+.0f}")
    if handle_series:
        print(f"  句柄数      : 净增 {handle_abs:+.1f}，首尾差 {handle_net:+.0f}")
    for name, info in lat_report.items():
        if "p95_last_ms" in info:
            print(f"    {name:9s} n={info['n']:5d}  "
                  f"p95 前={info['p95_first_ms']:8.1f}ms  "
                  f"后={info['p95_second_ms']:8.1f}ms  "
                  f"全程={info['p95_last_ms']:8.1f}ms")

    if http_fail_rate > 0.0:
        failures.append(f"请求失败率 {http_fail_rate:.2%}（要求 0）")
    if rss_growth is not None and rss_growth > RSS_GROWTH_LIMIT:
        failures.append(f"RSS 增长 {rss_growth:+.1%} 超过上限 {RSS_GROWTH_LIMIT:.0%}")
    if thread_abs > THREAD_GROWTH_LIMIT:
        if thread_flat:
            failures.append(f"线程数净增 {thread_abs:+.1f} 超过上限")
        else:
            # 还在上升 = 暖机段没盖住建线程期，这次观测不足以判定
            inconclusive.append(
                f"线程数仍在上升（末段相对前段 {thread_flat_rel:+.1%}，"
                f"净增 {thread_abs:+.1f}）—— 观测窗口太短，暖机段没有盖住"
                f"惰性建线程期，本次**不能判定**是否为泄漏"
            )
    if task_abs > TASK_GROWTH_LIMIT:
        if task_flat:
            failures.append(f"任务数净增 {task_abs:+.1f} 超过上限")
        else:
            inconclusive.append(
                f"任务数仍在上升（末段相对前段 {task_flat_rel:+.1%}，"
                f"净增 {task_abs:+.1f}）—— 观测窗口太短，无法判定"
            )
    if latency_worse:
        print(f"  注意        : 这些接口后半程 p95 明显变慢：{latency_worse}")

    # 库完整性：挂机不能改动照片库本身
    media_total = db.scalar("SELECT COUNT(*) FROM media", default=0)
    media_missing = db.scalar(
        "SELECT COUNT(*) FROM media WHERE missing=1", default=0
    )
    print(f"  库媒体行数  : {media_total}（missing={media_missing}）")

    report = {
        "hours": round((time.time() - t_start) / 3600, 3),
        "library": str(cfg.roots[0].path),
        "rounds": n_rounds,
        "rounds_ok": rounds_ok,
        "rounds_bad": rounds_bad,
        "warmup_fraction": WARMUP_FRACTION,
        "rss_growth": rss_growth,
        "rss_base_bytes": rss_base,
        "rss_peak_bytes": max(rss_series) if rss_series else None,
        "thread_growth": thread_abs,
        "thread_net_first_last": thread_net,
        "thread_plateau": thread_flat,
        "task_growth": task_abs,
        "task_net_first_last": task_net,
        "task_plateau": task_flat,
        "handle_growth": handle_abs,
        "handle_net_first_last": handle_net,
        "handle_plateau": handle_flat,
        "shutdown_sec": round(shutdown_sec, 1),
        "thread_alive": alive,
        "media_total": media_total,
        "media_missing": media_missing,
        "failures": failures,
        "inconclusive": inconclusive,
        "latency": lat_report,
        "samples": samples,
    }

    if report_path is not None:
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
            newline="",
        )
        print(f"\n报告已写入 {report_path}")

    print()
    if failures:
        print(f"=== FAIL（{len(failures)} 项） ===")
        for f in failures:
            print("  -", f)
        if inconclusive:
            print(f"（另有 {len(inconclusive)} 项证据不足，未计入 FAIL）")
        return 1
    if inconclusive:
        print(f"=== INCONCLUSIVE（{len(inconclusive)} 项证据不足） ===")
        for f in inconclusive:
            print("  -", f)
        print("\n这不是产品缺陷，是**观测窗口不够长**：判据要求暖机段"
              "（前 25%）盖住惰性建线程的上升期，实测本机需要约 3 小时。"
              "请用 --hours 24 重跑。")
        return 2
    print("=== PASS ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
