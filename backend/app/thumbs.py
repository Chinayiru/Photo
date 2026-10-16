"""缩略图引擎。

缩略图是全系统**唯一的高 CPU 操作**，也是首屏体验的决定因素。三条设计红线：

1. **永远不为了看一张图而解码原图**。两级尺寸（320 网格 / 1600 查看器）全部
   预生成并落盘缓存，请求路径只做文件读取。
2. **必须处理 EXIF Orientation**。手机竖拍的照片在 EXIF 里是横的，不转正
   90% 的照片躺着显示。
3. **必须限制并发**。4 核机器被 8 个 Pillow 任务打满后，隧道心跳会被饿死，
   手机端表现为"图片转圈然后断线"。

另有一个容易低估的风险：一张 100MP 照片解码后是 288MB RGB，4 个 worker
同时处理就是 1.1GB，直接 OOM。见 `_open_downscaled` 的三层防护。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Iterable, Literal

from PIL import Image

from . import sandbox
from .config import Config
from .exif import _prepare_pillow

if TYPE_CHECKING:  # pragma: no cover
    from .db import Database

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 两级尺寸。320 给网格，1600 给查看器。
#: 1600 是权衡结果：再大在手机屏上无差别（手机宽约 1080 物理像素），
#: 再小则双指放大后糊。设计文档 §5.1。
THUMB_SIZES: Final[tuple[int, ...]] = (320, 1600)

#: WebP 质量。78 是实测的拐点：再高体积线性涨而肉眼无差别。
THUMB_QUALITY: Final[int] = 78

#: 解码时先缩到这个尺寸以内，再精缩到目标尺寸。
DRAFT_MAX: Final[int] = 2048

#: 失败重试退避（秒）。3 次后标记 failed，不再自动重试。
RETRY_BACKOFF: Final[tuple[float, ...]] = (5.0, 30.0, 300.0)

#: Pillow 的 WEBP 编码 method。4 是速度/体积的平衡点；
#: 6 能再省 5% 体积但慢一倍，对 4915 张的首次索引不划算。
WEBP_METHOD: Final[int] = 4

ThumbState = Literal["pending", "ok", "failed"]

#: EXIF Orientation → Pillow 变换。
#:
#: ⚠️ 方向语义最容易搞反：Orientation=6 表示"需要顺时针转 90° 才正"，
#: 而 Pillow 的 ROTATE_270 是逆时针 270°（等价于顺时针 90°）。
#: 写成 ROTATE_90 会让竖拍照片转成倒的。
#:
#: 用 transpose 而非 rotate：transpose 是 90° 整数倍的无损像素重排，
#: rotate 会插值并引入模糊。
ORIENTATION_OPS: Final[dict[int, Image.Transpose]] = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,   # ★ 手机竖拍最常见
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


# --------------------------------------------------------------------------
# 缓存路径
# --------------------------------------------------------------------------

def thumb_key(root_id: str, rel_path: str) -> str:
    """缩略图缓存的稳定键。

    ⚠️ 用 sha1(root_id + rel_path) 而不是直接用原路径，三个理由：
      ① Windows 未开长路径时上限 260 字符，hash 后固定 40 字符，永不超限；
      ② 避免中文/特殊字符在不同文件系统下的编码差异；
      ③ 取前 2 位分目录，单目录文件数从 4900 降到约 76，NTFS 遍历更快。
    代价是无法从缓存路径反推原图 —— 不需要，DB 里有映射。
    """
    return hashlib.sha1(f"{root_id}\x00{rel_path}".encode("utf-8")).hexdigest()


def thumb_path(cfg: Config, root_id: str, rel_path: str, size: int) -> Path:
    """缓存文件路径：data/thumbs/{root_id}/{hash 前 2 位}/{hash}_{size}.webp"""
    h = thumb_key(root_id, rel_path)
    return cfg.data_path("thumbs", root_id, h[:2], f"{h}_{size}.webp")


def source_signature(mtime: float, size: int) -> str:
    """源文件的"内容指纹"，用于缓存失效判断与 ETag。

    ⚠️ 不用 sha256 全文件哈希：4915 张照片全量哈希要读几十 GB，首次索引
    从 3 分钟变成 30 分钟，而 mtime+size 的组合在"用户手工替换了同名文件"
    这个真实场景下已经足够（替换必然改 mtime）。

    ⚠️ 精度用**微秒**而不是整秒。整秒会漏掉真实场景：用户在文件管理器里
    复制一张同名照片覆盖原图，两个文件的 mtime 很可能落在**同一秒**内，
    于是签名不变 → 手机缓存一年且不再校验 → 用户永远看旧图。
    微秒是 NTFS 时间戳的实际分辨率（100ns），仍比 sha256 便宜几个数量级。
    浮点安全性：mtime≈1.7e9 秒 × 1e6 ≈ 1.7e15，远在 float64 精确整数
    范围（2^53≈9e15）之内，不会丢精度。
    """
    return f"{int(mtime * 1_000_000)}-{size}"


# --------------------------------------------------------------------------
# 纯函数部分（可脱离 asyncio 测试）
# --------------------------------------------------------------------------

def _apply_orientation(im: Image.Image, orientation: int | None) -> Image.Image:
    op = ORIENTATION_OPS.get(orientation or 1)
    if op is not None:
        im = im.transpose(op)
    return im


def _open_downscaled(path: str | Path, target: int) -> Image.Image:
    """打开图片并尽量在解码阶段就缩小，控制峰值内存。

    三层防护：
      ① JPEG draft —— 让 libjpeg 直接按 1/2、1/4、1/8 输出，内存降到 1/64。
         对 PNG/HEIC 无效（Pillow 自动忽略），所以还需要 ②③。
      ② MAX_IMAGE_PIXELS —— 拒绝声明超大尺寸的恶意图（见 exif.py）。
      ③ reduce() —— 先整数倍降采样再精缩，大倍率缩放的峰值内存与耗时都更低。
    """
    im = Image.open(path)
    try:
        if im.format == "JPEG":
            im.draft("RGB", (DRAFT_MAX, DRAFT_MAX))
        factor = max(1, min(im.width, im.height) // (target * 2))
        if factor >= 2:
            im = im.reduce(factor)
    except Exception:  # noqa: BLE001 —— 优化失败不影响正确性，继续用原图
        log.debug("downscale shortcut failed for %s", path, exc_info=True)
    return im


def generate_thumb(
    src: str | Path,
    dst: Path,
    size: int,
    *,
    quality: int = THUMB_QUALITY,
    orientation: int | None = None,
) -> tuple[int, int]:
    """同步生成一张缩略图并原子落盘。返回 (宽, 高)。

    抛出的异常由调用方（ThumbEngine）决定是否重试。
    """
    _prepare_pillow()
    with _open_downscaled(src, size) as im:
        im = _apply_orientation(im, orientation)
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGB")
        # thumbnail 保证等比且**不会放大**。resize 会把 200x200 的图
        # 插值成 1600x1600 —— 浪费带宽且更糊。
        im.thumbnail((size, size), Image.Resampling.LANCZOS)
        w, h = im.size

        dst.parent.mkdir(parents=True, exist_ok=True)
        # ⚠️ 临时名必须**进程内也唯一**：只用 pid 的话，同一进程里两个协程
        # 同时为同一张图生成缩略图（索引器后台入队 + 用户点开触发按需生成，
        # 这是最常见的并发场景）会写到同一个 .tmp 路径上，互相截断，
        # 最后 os.replace 出去的可能是一张半截图 —— 而且它会被当作成功结果
        # 写进 thumb_state='ok'，之后永远不再重新生成。
        tmp = dst.with_name(f"{dst.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            # ⚠️ exif=b"" 是必须的。变换已经烘焙进像素了，若再把原图的
            # Orientation 标签写进 WebP，浏览器会**再转一次**，照片就反了。
            im.save(tmp, "WEBP", quality=quality, method=WEBP_METHOD, exif=b"")
            os.replace(tmp, dst)          # 原子落盘，避免半截 WebP 被缓存
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    return w, h


def _generate_thumb_kw(
    src: str, dst: Path, size: int, quality: int, orientation: int | None,
) -> tuple[int, int]:
    """run_in_executor 不接受关键字参数，用它把位置参数转成关键字参数。

    直接 `functools.partial` 也行，但 partial 会在异常回溯里丢掉函数名，
    排查缩略图失败时更难读。多一个两行的包装函数换取可读的回溯是值得的。
    """
    return generate_thumb(src, dst, size, quality=quality, orientation=orientation)


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------

@dataclass(slots=True)
class ThumbJob:
    media_id: int
    attempts: int = 0


class ThumbEngine:
    """后台缩略图生成器。

    设计取舍：**不用线程池直接跑，而是 asyncio 队列 + 信号量 + 线程池**。
    队列让"索引进度"和"缩略图进度"解耦（索引器不必等缩略图），
    信号量保证同时最多 `workers` 个 Pillow 任务在跑（不饿死心跳），
    线程池让 Pillow 的阻塞调用不卡住事件循环。
    """

    def __init__(
        self,
        cfg: Config,
        db: "Database",
        *,
        workers: int = 4,
        queue_size: int = 2000,
        bus=None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        #: 事件总线，用于把"缩略图生成进度"推给手机端。为 None 时不发事件。
        self.bus = bus
        self.workers = max(1, workers)
        self.queue: asyncio.Queue[ThumbJob] = asyncio.Queue(maxsize=queue_size)
        self._pool = ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="thumb")
        self._sem = asyncio.Semaphore(self.workers)
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        #: 是否已完成回收（worker 已取消、线程池已关闭）。与 `_running` 分开：
        #: `_running=False` 只是"不再接新活"，回收是在 stop() 里做的，
        #: 两者之间有一个可被取消的 await —— 见 stop() 的长注释。
        self._stopped = False
        #: 统计，供 /api/system/info 与托盘显示
        self.done = 0
        self.failed = 0
        self.skipped = 0

    # --- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        if self._stopped:
            # 线程池已 shutdown，再起 worker 会抛 RuntimeError 或静默不干活。
            raise RuntimeError("ThumbEngine 已停止，不能重新启动（请新建实例）")
        self._running = True
        for i in range(self.workers):
            self._tasks.append(asyncio.create_task(self._worker(i), name=f"thumb-{i}"))
        log.info("thumb engine started: %d workers, sizes=%s", self.workers, THUMB_SIZES)

    async def stop(self, *, drain: bool = False,
                   drain_timeout: float | None = None) -> None:
        """停止引擎。可重复调用；**保证 worker 与线程池一定被回收**。

        `drain=True` 时先把手头的队列跑完 —— 关机/退出时用，避免用户看到
        "进度 95% 然后全没了"。

        ★ 为什么 drain 的超时**长在函数内部**（`drain_timeout`），
        而不是由调用方用 `asyncio.wait_for(stop(), ...)` 从外面掐：

        从外面掐会 **cancel 这个协程**。若回收写在 `await queue.join()`
        之后（早期实现），取消会让回收整段跳过 —— 而 `_running` 已经置为
        False，于是第二次 stop() 直接 early-return，worker 与线程池
        **永远不会被回收**。实测后果：

        - 事件循环关闭时刷出 4 条 `Task was destroyed but it is pending!`
          （`thumb-0`..`thumb-3` 卡在 `queue.get()` / `_generate_one()`）；
        - 托盘退出看起来"卡住了"，只能去任务管理器杀进程（强杀留下 WAL）。

        曾经试过用 `asyncio.shield` 保护回收段，但那只是把问题往后推：
        `wait_for` 超时后 shield 的内层任务继续跑，而调用方已经走完
        `_shutdown` 并让 `asyncio.run` 关闭事件循环，那个还在跑的任务
        照样被销毁。**唯一可靠的做法是不让取消发生** —— 自己带超时，
        超时只是停止等待，回收在同一协程里继续走完。

        `finally` 保证即使 drain 抛异常也会回收。
        """
        if self._stopped:
            return
        self._running = False
        try:
            if drain:
                if drain_timeout is None:
                    await self.queue.join()
                else:
                    try:
                        await asyncio.wait_for(self.queue.join(),
                                               timeout=drain_timeout)
                    except asyncio.TimeoutError:
                        log.info(
                            "缩略图队列未在 %.0f 秒内跑完（剩余 %d 个），"
                            "剩余任务将在下次启动时补做",
                            drain_timeout, self.queue.qsize())
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        """取消 worker、关线程池。幂等，且不依赖 `_running`。"""
        if self._stopped:
            return
        self._stopped = True
        # 队列里可能还有没跑完的任务（drain 超时或根本没 drain 时会）。
        # worker 被取消后没人再调 task_done()，留着未平衡的计数会让
        # 后续任何 queue.join() 永久挂起。
        while not self.queue.empty():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self.queue.task_done()
        for t in self._tasks:
            if not t.done():
                t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)
        log.info("thumb engine stopped (done=%d failed=%d skipped=%d)",
                 self.done, self.failed, self.skipped)

    # --- 入队 -------------------------------------------------------------

    def enqueue(self, media_id: int) -> bool:
        """非阻塞入队。队列满时返回 False，由调用方决定丢弃还是等待。

        首次索引 4915 张时会瞬间灌满队列。**丢弃是正确的**：缩略图是
        惰性可补的，`thumb_state` 仍是 pending，下次扫描或用户点开时重入队。
        阻塞索引器去等缩略图才是错的（会让首次索引从 3 分钟变 20 分钟）。
        """
        try:
            self.queue.put_nowait(ThumbJob(media_id))
            return True
        except asyncio.QueueFull:
            log.debug("thumb queue full, dropping media_id=%s", media_id)
            return False

    def enqueue_many(self, media_ids: Iterable[int]) -> int:
        return sum(1 for mid in media_ids if self.enqueue(mid))

    # --- worker -----------------------------------------------------------

    async def _worker(self, idx: int) -> None:
        while True:
            job = await self.queue.get()
            try:
                # ★ 用返回值区分"真的生成了"与"跳过了"。只看"没抛异常"
                # 会把 missing 与缓存命中也算成 done，统计里的进度条
                # 会比实际生成数虚高，用户看到"100% 完成"却有图打不开。
                if await self._generate_one(job.media_id):
                    self.done += 1
                else:
                    self.skipped += 1
            except asyncio.CancelledError:
                self.queue.task_done()
                raise
            except Exception as e:  # noqa: BLE001 —— 一个坏文件不能拖垮 worker
                await self._on_failure(job, e)
            finally:
                self.queue.task_done()

    async def _generate_one(self, media_id: int) -> bool:
        """生成一张图的所有尺寸。返回 True 表示真的做了工作，False 表示跳过。

        ★ 新鲜度判定只看**磁盘**：源文件与缓存文件各自的 mtime，
        完全不依赖 media 表的 `thumb_etag`。

        为什么不能用 DB 的 etag 做判定：DB 的 mtime/size 由索引器写入，
        而用户可以在"程序没跑"或"两次扫描之间"替换照片。这期间
        `thumb_etag == sig`（两边都是旧值）会成立，于是每一张旧缓存
        都被当成新鲜的跳过，最后 `_set_state(ok, 旧sig)` 把 DB 也
        标记成"已生成" —— 用户永远看到旧图，而且重扫也修不好，
        因为重扫前 etag 已经等于 sig 了。

        改用"缓存文件是否比源文件新"之后，这条路径自带自愈能力：
        任何一次替换都会被下一次请求/重扫发现，不需要 DB 参与。
        """
        row = self.db.query_one(
            "SELECT id, root_id, rel_path, mtime, size, orientation,"
            "       thumb_state, thumb_etag, missing"
            "  FROM media WHERE id = ?",
            (media_id,),
        )
        if row is None or row["missing"]:
            return False

        root = self.cfg.root_by_id(row["root_id"])
        # ★ 过沙箱。缩略图读取也必须走唯一入口 —— 索引器写库时的路径
        # 可能来自更早的配置，配置改了之后旧行仍然指向库外。
        src = sandbox.resolve(root, row["rel_path"], "read", cfg=self.cfg)

        try:
            src_mtime = src.stat().st_mtime
        except OSError:
            return False

        paths = {s: thumb_path(self.cfg, row["root_id"], row["rel_path"], s)
                 for s in THUMB_SIZES}

        def _fresh(p: Path) -> bool:
            """缓存文件存在且不比源文件旧。"""
            try:
                return p.stat().st_mtime >= src_mtime
            except OSError:
                return False

        if all(_fresh(p) for p in paths.values()):
            return False

        loop = asyncio.get_running_loop()
        async with self._sem:
            for size, dst in paths.items():
                if _fresh(dst):
                    continue
                await loop.run_in_executor(
                    self._pool, _generate_thumb_kw,
                    str(src), dst, size, THUMB_QUALITY, row["orientation"],
                )
        self._set_state(media_id, "ok", source_signature(src_mtime, row["size"]))
        # 只报"还剩多少"，不报"刚生成了哪张" —— 后者会让手机端
        # 为每一张图重排一次时间线，4915 张下来页面直接卡死。
        self._emit({
            "type": "thumb_progress",
            "done": self.done,
            "failed": self.failed,
            "pending": self.pending_count(),
        })
        return True

    async def _on_failure(self, job: ThumbJob, exc: Exception) -> None:
        job.attempts += 1
        if job.attempts >= len(RETRY_BACKOFF) + 1:
            self._set_state(job.media_id, "failed", None)
            self.failed += 1
            log.warning("thumb permanently failed media_id=%s: %s", job.media_id, exc)
            return
        delay = RETRY_BACKOFF[job.attempts - 1]
        log.info("thumb retry %d/%d media_id=%s in %.0fs",
                 job.attempts, len(RETRY_BACKOFF), job.media_id, delay)
        loop = asyncio.get_running_loop()
        loop.call_later(delay, self._requeue, job)

    def _requeue(self, job: ThumbJob) -> None:
        """重试入队。队列满时放弃 —— 状态仍是 pending，下次扫描会再试。"""
        if not self._running:
            return
        try:
            self.queue.put_nowait(job)
        except asyncio.QueueFull:
            log.debug("retry dropped (queue full) media_id=%s", job.media_id)

    def _emit(self, event: dict) -> None:
        """发布事件，永不抛异常（缩略图进度不该影响生成本身）。"""
        if self.bus is None:
            return
        try:
            self.bus.publish(event)
        except Exception:       # noqa: BLE001
            log.debug("缩略图事件发布失败", exc_info=True)

    def _set_state(self, media_id: int, state: ThumbState, etag: str | None) -> None:
        try:
            self.db.execute(
                "UPDATE media SET thumb_state = ?, thumb_etag = ? WHERE id = ?",
                (state, etag, media_id),
            )
        except Exception:  # noqa: BLE001 —— 写状态失败不该中断生成
            log.warning("failed to persist thumb_state media_id=%s", media_id,
                        exc_info=True)

    # --- 对外查询 ---------------------------------------------------------

    async def ensure(self, media_id: int) -> bool:
        """按需生成（handler 调用）。返回 True 表示真的生成了。

        与后台 worker 共用同一条 `_generate_one` 路径和同一个信号量：
        走独立实现的话，两处对"什么算已生成"的判断迟早会不一致，
        表现为"后台说已完成、点开却 404"。
        """
        return await self._generate_one(media_id)

    def pending_count(self) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM media WHERE thumb_state = 'pending' AND missing = 0",
            default=0) or 0)

    def failed_count(self) -> int:
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM media WHERE thumb_state = 'failed' AND missing = 0",
            default=0) or 0)

    def queue_depth(self) -> int:
        return self.queue.qsize()

    def stats(self) -> dict[str, int]:
        return {
            "done": self.done,
            "failed": self.failed,
            "skipped": self.skipped,
            "queued": self.queue_depth(),
            "pending": self.pending_count(),
            "failed_total": self.failed_count(),
            "workers": self.workers,
        }

    def reset_failed(self) -> int:
        """把 failed 重置为 pending，供托盘"重试失败的缩略图"。"""
        n = self.db.execute_rowcount(
            "UPDATE media SET thumb_state = 'pending' WHERE thumb_state = 'failed'"
        )
        self.failed = 0
        return n


# --------------------------------------------------------------------------
# 占位图（thumb_state='failed' 时返回，避免时间线出现破图）
# --------------------------------------------------------------------------

_PLACEHOLDER_CACHE: dict[int, bytes] = {}


def placeholder_webp(size: int) -> bytes:
    """生成灰色占位图。返回 WebP 字节。

    为什么返回占位图而不是 404/500：时间线上一个破图会让用户以为
    "照片丢了"，而实际只是缩略图没生成出来。占位图 + 可点击重试更好。
    """
    if size in _PLACEHOLDER_CACHE:
        return _PLACEHOLDER_CACHE[size]
    import io

    im = Image.new("RGB", (size, size), (58, 58, 62))
    buf = io.BytesIO()
    im.save(buf, "WEBP", quality=60, method=2)
    data = buf.getvalue()
    _PLACEHOLDER_CACHE[size] = data
    return data


__all__ = [
    "DRAFT_MAX",
    "ORIENTATION_OPS",
    "RETRY_BACKOFF",
    "THUMB_QUALITY",
    "THUMB_SIZES",
    "ThumbEngine",
    "ThumbJob",
    "generate_thumb",
    "placeholder_webp",
    "source_signature",
    "thumb_key",
    "thumb_path",
]
