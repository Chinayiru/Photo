"""按需转码：把「这台手机解不了」的视频变成 h264 + aac。

## 为什么需要它（V2）

V1 只是「能播的直接播、不能播的提示」。但业主库里的主力素材是 **iPhone 直出的
HEVC**（本机 15 条里 11 条），而安卓 Chrome / 桌面 Chrome 大量机型**没有 HEVC
解码器**。对用户来说，那 11 条视频就是"永远看不了" —— 提示再多也没用。

转码能彻底解决，但有代价，所以这里的边界写得很死：

| 维度 | 取值 | 理由 |
|---|---|---|
| 触发 | **只有用户点了「转码播放」才做** | 转码是重活（一个 4K 片段能吃满 CPU 几分钟）。自动转码会在用户翻列表时偷偷占满机器 |
| 并发 | 默认 **1**（`video.transcode_concurrency`） | 与封面抽取共用 CPU；转码本身已经是多线程，再叠并发只会互相拖慢 |
| 输出 | `h264 / yuv420p / CRF 23 / 长边 ≤ 1280 / aac 128k / +faststart` | yuv420p 是所有浏览器的交集；faststart 让起播不用等整段；长边 1280 在手机屏上看不出差别，体积却是 4K 的几分之一 |
| 存放 | `data/transcodes/<id>-<mtime>-<size>.mp4` | 名字里带源文件的 mtime 与字节数：源文件一变，旧产物自动失效（不用扫描、不用清理逻辑判断） |
| 清理 | 总量上限（`video.transcode_cache_mb`，默认 2048）按 **atime 最旧**淘汰 | 用户看过的会留下，很久没看的先走。超限时删到 80% 以下，避免"每转一个删一个"的抖动 |

## 不做的事

- 不覆盖、不移动源文件（源文件只读）；
- 不做"高品质存档转码"——产物是**播放用的临时文件**，不是备份；
- 不在云端做：转码必须在电脑上（源文件在那里，云盘中继只流转字节）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "STATE_QUEUED",
    "STATE_RUNNING",
    "STATE_READY",
    "STATE_FAILED",
    "TRANSCODE_CRF",
    "TRANSCODE_MAX_EDGE",
    "TranscodeJob",
    "Transcoder",
    "cache_dir",
    "parse_progress",
    "plan_args",
    "target_name",
    "sweep",
]

#: CRF 23 是 x264 的默认甜点：肉眼看不出和源的区别，体积约为无损的几分之一。
TRANSCODE_CRF: int = 23
#: 长边上限。手机竖屏 1080x1920 → 720x1280；横屏 4K → 1280x720。
TRANSCODE_MAX_EDGE: int = 1280
#: 音频码率。语音/环境声够用，音乐也不至于明显发闷。
TRANSCODE_ABITRATE: str = "128k"
#: 编码预设。veryfast 在 2 vCPU 上大约是 realtime 的 3–6 倍速，质量与
#: medium 的差距在这个用途上看不出来。
TRANSCODE_PRESET: str = "veryfast"

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_READY = "ready"
STATE_FAILED = "failed"

#: 缓存上限的默认值与淘汰目标比例。
DEFAULT_CACHE_MB: int = 2048
_EVICT_TO = 0.8


def cache_dir(cfg: Any) -> Path:
    """转码产物目录（`data/transcodes`），不存在就建。"""
    d = cfg.data_path("transcodes")
    d.mkdir(parents=True, exist_ok=True)
    return d


def target_name(media_id: int, mtime: float | None, size: int | None) -> str:
    """产物文件名。

    ★ 名字里必须带「源文件指纹」：`<id>-<mtime 整数>-<字节数>.mp4`。
    这样源文件被替换（同名不同内容）时，新名字自动不同 —— 不需要扫描、
    不需要在数据库里记版本，也不存在"改了源文件却还在放旧转码"的问题。
    """
    mt = int(mtime or 0)
    sz = int(size or 0)
    return f"{int(media_id)}-{mt}-{sz}.mp4"


def plan_args(ffmpeg: str, src: str | Path, dst: str | Path, *,
              duration: float | None = None,
              max_edge: int = TRANSCODE_MAX_EDGE) -> list[str]:
    """拼 ffmpeg 参数。

    * `-map 0:v:0` 只取第一路视频（手机视频常带数据流/时间码轨）；
    * `-map 0:a:0?` 末尾的 `?` = 没有音频也不要失败；
    * `-vf scale='min(MAX,iw)':-2`：只缩不放（`min`），`-2` 保证偶数（h264 要求）；
    * `-movflags +faststart`：把 moov 挪到文件头，否则手机上起播要等整段；
    * `-progress pipe:1`：进度走 stdout，供 `parse_progress` 读。
    """
    vf = f"scale='min({int(max_edge)},iw)':-2"
    argv = [
        ffmpeg,
        "-hide_banner", "-loglevel", "error",
        "-nostdin",
        "-y",
        "-i", str(src),
        "-map", "0:v:0",
        "-map", "0:a:0?",
        "-c:v", "libx264",
        "-preset", TRANSCODE_PRESET,
        "-crf", str(TRANSCODE_CRF),
        "-pix_fmt", "yuv420p",
        "-vf", vf,
        "-c:a", "aac",
        "-b:a", TRANSCODE_ABITRATE,
        "-ac", "2",
        "-movflags", "+faststart",
        "-f", "mp4",
        "-progress", "pipe:1",
        "-nostats",
        str(dst),
    ]
    return argv


def parse_progress(line: str, duration: float | None) -> float | None:
    """从 `-progress` 的一行里取出完成度（0.0–1.0）；读不到就返回 None。

    ffmpeg 的 `out_time_ms` 名字骗人：它的单位其实是**微秒**
    （历史上写成 ms，改不动了）。这里按微秒算。
    """
    text = (line or "").strip()
    if not text or "=" not in text:
        return None
    key, _, value = text.partition("=")
    key = key.strip()
    value = value.strip()
    if key == "progress" and value == "end":
        return 1.0
    if key not in ("out_time_us", "out_time_ms"):
        return None
    if not duration or duration <= 0:
        return None
    try:
        us = int(value)
    except ValueError:
        return None
    return max(0.0, min(1.0, us / 1_000_000.0 / duration))


def sweep(directory: Path, budget_bytes: int, *, protect: set[str] | None = None) -> int:
    """把目录总量压到预算以内，返回删掉的字节数。

    按 **atime** 淘汰：用户最近看过的那几条留下。`protect` 里的文件名不删
    （正在转码/正在播放的产物）。预算 ≤ 0 视为"不清理"。
    """
    if budget_bytes <= 0 or not directory.is_dir():
        return 0
    keep = protect or set()
    entries: list[tuple[float, int, Path]] = []
    total = 0
    for path in directory.iterdir():
        if not path.is_file() or path.suffix != ".mp4":
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        total += st.st_size
        entries.append((max(st.st_atime, st.st_mtime), st.st_size, path))
    if total <= budget_bytes:
        return 0

    target = int(budget_bytes * _EVICT_TO)
    freed = 0
    for _atime, size, cached in sorted(entries):
        if total - freed <= target:
            break
        if cached.name in keep:
            continue
        try:
            cached.unlink()
        except OSError as exc:                    # 正在被播放器占用等情况
            log.debug("转码产物删不掉（可能正被读取）%s: %s", cached.name, exc)
            continue
        freed += size
        log.info("转码缓存淘汰 %s（%.1f MB）", cached.name, size / 1048576)
    return freed


@dataclass
class TranscodeJob:
    """一次转码的公开状态（接口直接序列化它）。"""

    media_id: int
    state: str = STATE_QUEUED
    progress: float = 0.0
    path: str = ""
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    def as_json(self) -> dict[str, Any]:
        size = 0
        if self.path:
            try:
                size = os.path.getsize(self.path)
            except OSError:
                size = 0
        return {
            "media_id": self.media_id,
            "state": self.state,
            "progress": round(self.progress, 3),
            "size": size,
            "error": self.error,
        }


class Transcoder:
    """管转码任务：每个 media_id 至多一个在跑，产物落 `data/transcodes`。

    线程模型：`start()` 起一个 worker 线程；真正的并发闸门是 `_slots`
    （默认 1），所以"点了三条视频"只会排队，不会把机器打满。
    """

    def __init__(self, cfg: Any, *, concurrency: int = 1,
                 cache_mb: int = DEFAULT_CACHE_MB) -> None:
        self._cfg = cfg
        self._slots = threading.BoundedSemaphore(max(1, int(concurrency)))
        self._lock = threading.Lock()
        self._jobs: dict[int, TranscodeJob] = {}
        self._cache_bytes = max(0, int(cache_mb)) * 1048576
        self._procs: dict[int, subprocess.Popen[bytes]] = {}

    # ---------------------------------------------------------------- 查询

    def get(self, media_id: int) -> TranscodeJob | None:
        with self._lock:
            return self._jobs.get(int(media_id))

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for j in self._jobs.values()
                       if j.state in (STATE_QUEUED, STATE_RUNNING))

    # ---------------------------------------------------------------- 启动

    def start(self, *, media_id: int, src: Path, duration: float | None,
              ffmpeg: str) -> TranscodeJob:
        """开始（或复用）一次转码。已就绪的产物直接复用。"""
        media_id = int(media_id)
        try:
            st = src.stat()
        except OSError as exc:
            job = TranscodeJob(media_id=media_id, state=STATE_FAILED,
                               error=f"源文件不可读：{exc}")
            with self._lock:
                self._jobs[media_id] = job
            return job

        out = cache_dir(self._cfg) / target_name(media_id, st.st_mtime, st.st_size)
        with self._lock:
            current = self._jobs.get(media_id)
            if current is not None and current.state in (STATE_QUEUED,
                                                          STATE_RUNNING):
                return current
            if out.is_file() and out.stat().st_size > 0:
                job = TranscodeJob(media_id=media_id, state=STATE_READY,
                                   progress=1.0, path=str(out),
                                   finished_at=time.time())
                self._jobs[media_id] = job
                self._touch(out)
                return job
            job = TranscodeJob(media_id=media_id, state=STATE_QUEUED,
                               path=str(out))
            self._jobs[media_id] = job

        t = threading.Thread(target=self._work, args=(job, src, out, duration,
                                                     ffmpeg),
                             name=f"transcode-{media_id}", daemon=True)
        t.start()
        return job

    # ---------------------------------------------------------------- 执行

    def _work(self, job: TranscodeJob, src: Path, out: Path,
              duration: float | None, ffmpeg: str) -> None:
        with self._slots:
            job.state = STATE_RUNNING
            # 同目录改名才能在 ntfs/ext4 上都保证原子（跨卷 os.replace 会退化成"复制+删"，见 docs/06）
            tmp = out.with_name(f"{out.stem}.part.mp4")
            argv = plan_args(ffmpeg, src, tmp)
            log.info("转码开始 media_id=%s → %s", job.media_id, out.name)
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            except OSError as exc:
                job.state = STATE_FAILED
                job.error = f"启动 ffmpeg 失败：{exc}"
                job.finished_at = time.time()
                return
            with self._lock:
                self._procs[job.media_id] = proc
            try:
                assert proc.stdout is not None
                for raw in proc.stdout:
                    pct = parse_progress(raw.decode("utf-8", "replace"), duration)
                    if pct is not None:
                        job.progress = pct
                rc = proc.wait(timeout=3600)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = -1
                job.error = "转码超时（超过 60 分钟）"
            except Exception as exc:              # noqa: BLE001
                proc.kill()
                rc = -1
                job.error = f"转码失败：{exc}"
            finally:
                with self._lock:
                    self._procs.pop(job.media_id, None)
                job.finished_at = time.time()

            if rc == 0 and tmp.is_file() and tmp.stat().st_size > 0:
                try:
                    os.replace(tmp, out)          # 原子：播放器永远看不到半个文件
                except OSError as exc:
                    job.state = STATE_FAILED
                    job.error = f"产物落盘失败：{exc}"
                    return
                job.state = STATE_READY
                job.progress = 1.0
                job.path = str(out)
                log.info("转码完成 media_id=%s %.1f MB",
                         job.media_id, out.stat().st_size / 1048576)
                self._sweep()
            else:
                job.state = STATE_FAILED
                stderr = b""
                if proc.stderr is not None:
                    try:
                        stderr = proc.stderr.read() or b""
                    except Exception:             # noqa: BLE001
                        stderr = b""
                tail = stderr.decode("utf-8", "replace").strip().splitlines()
                job.error = job.error or (tail[-1] if tail else f"ffmpeg 退出码 {rc}")
                try:
                    tmp.unlink()
                except OSError:
                    pass
                log.warning("转码失败 media_id=%s：%s", job.media_id, job.error)

    # ---------------------------------------------------------------- 辅助

    def _sweep(self) -> None:
        with self._lock:
            protect = {Path(j.path).name for j in self._jobs.values()
                       if j.path}
        try:
            sweep(cache_dir(self._cfg), self._cache_bytes, protect=protect)
        except OSError as exc:                    # pragma: no cover
            log.debug("转码缓存清理失败：%s", exc)

    @staticmethod
    def _touch(path: Path) -> None:
        """读一次 = 刷新 atime，配合 LRU 淘汰（`noatime` 挂载上也有效）。"""
        try:
            os.utime(path, None)
        except OSError:
            pass
