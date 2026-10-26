"""视频：容器识别、元数据探测、抽帧。

## 为什么单独一个模块

一份视频要回答三个不同层次的问题，它们的代价差三个数量级：

| 问题 | 依据 | 代价 |
|---|---|---|
| 这是不是视频？什么容器？ | 文件头 64 字节 | 一次 open + 64 字节 |
| 多长？什么编码？能不能播？ | ffprobe | 一次子进程，10 ms–10 s |
| 封面长什么样？ | ffmpeg 解一帧 | 一次子进程 + 一次解码，50 ms–5 s |

把它们混在一起写，第一个问题就会退化成"跑一次 ffprobe" —— 于是扫描
一个 5000 文件的库要起 5000 个子进程。所以：**索引期只做第一层**
（`sniff_container`），第二层在索引器里按需跑（`probe`，有超时与并发闸门），
第三层只在缩略图队列里跑（`extract_frame`）。

## 子进程的三条硬要求（docs/06 新增约束）

1. **参数数组，不过 shell**：文件名里可能有空格、引号、`&`、`;`
   （真实相册里就有 `VID 2024 & 生日.mp4`）。任何字符串拼接都是一次
   命令注入。
2. **显式超时并杀进程**：`subprocess.run(timeout=...)` 到点会 kill 掉，
   而不是让一个 4 GB 的坏文件把工作线程占住一小时。
3. **限并发**：抽帧是真解码，2 个并发就能吃满 2 vCPU。闸门放在这里
   而不是调用方，是为了让"索引器、缩略图队列、按需预览"三条路径共用
   同一个上限 —— 各写一份的话，最坏情况是 3 条路径各开 2 个，直接
   打满 CPU 并让手机端所有请求超时。

## 关于 ffmpeg 二进制

不进 git（体积 80 MB+，且与本仓库的"可复现构建"约定冲突）。查找顺序见
`find_bin()`：配置 → 环境变量 → 程序目录旁的 `tools/ffmpeg/` → PATH。
找不到时抛 `FFmpegMissing`，调用方必须**明确报错**，不能静默降级成
"没有封面" —— 用户会以为视频里没有画面。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

log = logging.getLogger(__name__)

__all__ = [
    "VIDEO_EXTS_INDEX",
    "VIDEO_EXTS_UPLOAD",
    "IMAGE_BRANDS",
    "VIDEO_BRANDS",
    "MAGIC_HEAD_BYTES",
    "FFmpegMissing",
    "ProbeResult",
    "sniff_container",
    "container_of_ext",
    "check_container",
    "find_bin",
    "probe",
    "extract_frame",
    "is_playable",
    "PLAYABLE_CONTAINERS",
    "PLAYABLE_VCODECS",
    "row_kind",
]


def row_kind(row: Any) -> str:
    """media 行的类型（`"image"` / `"video"`），缺列一律当图片。

    ★ 宽容是有原因的：`media.kind` 是加视频时才有的列，而缩略图引擎、
      维护脚本、测试里的假行都可能只有一部分列。直接 `row["kind"]`
      在 dict 上抛 `KeyError`、在老 Row 上抛 `IndexError`，而这些异常
      会被上层的通用 `except` 吞成"这张缩略图生成失败" —— 症状是
      "所有缩略图都不见了"，根因只是一次取列。
    """
    try:
        keys = row.keys()
    except AttributeError:
        return "image"
    try:
        if "kind" in keys:
            return str(row["kind"] or "image")
    except (IndexError, KeyError):
        return "image"
    return "image"

#: 索引期收哪些视频扩展名（D1：mp4/mov/webm/mkv/avi）。
#: 刻意不含 `3gp/wmv/flv/mts`：容器识别与浏览器播放都没有稳定答案，
#: 收进来只会让用户点开看到"不支持"。
VIDEO_EXTS_INDEX: Final[frozenset[str]] = frozenset(
    {"mp4", "m4v", "mov", "webm", "mkv", "avi"}
)

#: 上传期收哪些（D1：只收浏览器与现代手机能直接播的三个家族）。
#: `mkv/avi` 可索引但不可上传 —— 用户传上来自己也播不了。
VIDEO_EXTS_UPLOAD: Final[frozenset[str]] = frozenset({"mp4", "m4v", "mov", "webm"})

#: ISO-BMFF（mp4/mov）的 brand。⚠️ 这个盒子**也是** HEIC/AVIF 的容器，
#: 所以必须先排除图片 brand，否则一张 `.heic` 会被当成 `.mp4`。
IMAGE_BRANDS: Final[frozenset[bytes]] = frozenset(
    {b"avif", b"avis", b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}
)
VIDEO_BRANDS: Final[frozenset[bytes]] = frozenset(
    {b"isom", b"iso2", b"iso4", b"iso5", b"mp41", b"mp42", b"avc1",
     b"dash", b"M4V ", b"m4v ", b"M4A ", b"qt  ", b"3gp4", b"3gp5"}
)

#: 读文件头的字节数。EBML 的 DocType（"webm"/"matroska"）在头部 40 字节内
#: 才稳定，32 不够。
MAGIC_HEAD_BYTES: Final[int] = 64

#: 扩展名 → 可接受的容器集合。扩展名与容器不一致时拒绝（约束 3）。
_EXT_CONTAINERS: Final[dict[str, frozenset[str]]] = {
    "mp4": frozenset({"mp4"}),
    "m4v": frozenset({"mp4"}),
    "mov": frozenset({"mov", "mp4"}),
    "webm": frozenset({"webm"}),
    "mkv": frozenset({"mkv"}),
    "avi": frozenset({"avi"}),
}

#: 浏览器能直接播的容器。mkv/avi 不在内（D2：宁可不播也不要白屏）。
PLAYABLE_CONTAINERS: Final[frozenset[str]] = frozenset({"mp4", "mov", "webm"})

#: 浏览器能直接播的视频编码。H.265/HEVC 明确不在内：Chrome 桌面基本不支持。
PLAYABLE_VCODECS: Final[frozenset[str]] = frozenset(
    {"h264", "vp8", "vp9", "av1", "theora"}
)

#: 抽帧与探测的超时（秒）。见模块文档第 2 条。
PROBE_TIMEOUT: Final[float] = 10.0
FRAME_TIMEOUT: Final[float] = 30.0

#: 默认并发（约束：建议 2）。可由 `video.max_concurrency` 覆盖。
DEFAULT_CONCURRENCY: Final[int] = 2


class FFmpegMissing(RuntimeError):
    """找不到 ffmpeg/ffprobe 可执行文件。

    这是**部署错误**，不是数据错误：调用方应当把整个视频功能标记为不可用
    并给出可执行的修复提示，而不是逐条视频报"封面生成失败"。
    """


@dataclass(slots=True)
class ProbeResult:
    """一次探测的结论。失败时 `ok=False` 且 `error` 非空。"""

    ok: bool
    container: str = ""
    duration: float = 0.0          # 秒；未知为 0
    width: int = 0
    height: int = 0
    vcodec: str = ""               # 'h264' / 'hevc' / 'vp9' / 'av1' / ...
    acodec: str = ""
    error: str = ""

    def as_db(self) -> dict[str, Any]:
        """写进 media 表的字段（宽高为 0 时写 NULL，避免触发 CHECK > 0）。"""
        return {
            "duration": self.duration if self.duration > 0 else None,
            "width": self.width if self.width > 0 else None,
            "height": self.height if self.height > 0 else None,
            "vcodec": self.vcodec or None,
            "acodec": self.acodec or None,
            "container": self.container or None,
        }


# --------------------------------------------------------------------------
# 第一层：容器识别（只读 64 字节）
# --------------------------------------------------------------------------

def sniff_container(head: bytes) -> str | None:
    """从文件头判定容器：'mp4' / 'mov' / 'webm' / 'mkv' / 'avi'，否则 None。

    ⚠️ `ftyp` 即 ISO-BMFF，图片（HEIC/AVIF）也用它。所以**先查图片 brand**，
    命中就返回 None —— 一箱 iPhone 的 HEIC 不能被当成视频索引，
    否则每张照片都会多起一次 ffprobe，扫描时间翻几倍。
    """
    if len(head) < 12:
        return None

    # --- ISO-BMFF：4 字节 size + 'ftyp' + brand ---
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in IMAGE_BRANDS:
            return None
        if brand == b"qt  ":
            return "mov"
        if brand in VIDEO_BRANDS:
            # `qt` 之外的 BMFF 视频一律按 mp4 处理；mov 与 mp4 的差别只
            # 在 brand，解码路径相同，浏览器对两者的支持也几乎一致。
            return "mp4"
        # 未知 brand：可能是厂商私有的 mp4（如 Sony 的 `XAVC`），仍按
        # 容器结构归为 mp4 —— 后面 ffprobe 会给最终答案。
        return "mp4"

    # --- EBML（webm / mkv）---
    if head[0:4] == b"\x1a\x45\xdf\xa3":
        # DocType 是 EBML 里的一个 UTF-8 字符串元素（ID 0x4282）。
        # 在头部 64 字节内按字节串找即可：这个元素总在 Segment 之前。
        if b"webm" in head[:MAGIC_HEAD_BYTES]:
            return "webm"
        if b"matroska" in head[:MAGIC_HEAD_BYTES]:
            return "mkv"
        # 结构是 EBML 但看不出 DocType：按 webm 处理（浏览器更可能播），
        # ffprobe 会给出 `format_name`，最终以它为准。
        return "webm"

    # --- RIFF：AVI（RIFF 也被 wav/webp 用，必须看第三个四字节）---
    if head[0:4] == b"RIFF" and head[8:12] in (b"AVI ", b"AVIX"):
        return "avi"

    return None


def container_of_ext(ext: str) -> frozenset[str]:
    """扩展名期望的容器集合（空集 = 不认识这个扩展名）。"""
    return _EXT_CONTAINERS.get(ext.lower().lstrip("."), frozenset())


def check_container(ext: str, head: bytes) -> tuple[bool, str]:
    """扩展名与容器是否一致。返回 `(一致吗, 识别到的容器)`。

    与图片的 `uploads.check_magic` 同一条原则：扩展名是用户可控的字符串，
    容器结构来自文件本身。改了名的 `.mkv` 叫 `.mp4` 必须拒 —— 否则
    手机端会拿到 `Content-Type: video/mp4` 去解一个 Matroska，
    表现是"点了播放一直转圈"，而不是一句人话。
    """
    actual = sniff_container(head[:MAGIC_HEAD_BYTES]) or ""
    if not actual:
        return False, ""
    return actual in container_of_ext(ext), actual


# --------------------------------------------------------------------------
# 可执行文件查找
# --------------------------------------------------------------------------

_BIN_CACHE: dict[str, str | None] = {}
_BIN_LOCK = threading.Lock()


def _app_dirs() -> list[Path]:
    """可能放着 `tools/ffmpeg/` 的目录。

    打包后是 `_MEIPASS`；源码运行是仓库根，以及仓库里的 `build/`
    （开发机把二进制放在 `build/tools/ffmpeg/`，`build/` 整体不入库）。
    """
    out: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        out.append(Path(meipass))
    # 源码运行：本文件在 backend/app/ 下，仓库根是上两级
    root = Path(__file__).resolve().parents[2]
    out.extend([root, root / "build"])
    return out


def find_bin(name: str, *, configured_dir: str = "") -> str:
    """找 ffmpeg / ffprobe。找不到抛 `FFmpegMissing`（附可执行的修复提示）。

    查找顺序（先近后远，因为用户自己的覆盖应当优先）：

      1. 显式配置 `video.ffmpeg_dir`
      2. 环境变量 `PHOTOBRIDGE_FFMPEG_DIR`
      3. 程序目录旁 `tools/ffmpeg/<name>.exe`
      4. PATH
    """
    key = f"{name}\x00{configured_dir}"
    with _BIN_LOCK:
        if key in _BIN_CACHE:
            cached = _BIN_CACHE[key]
            if cached is None:
                raise FFmpegMissing(_missing_hint(name))
            return cached

    exe = f"{name}.exe" if os.name == "nt" else name
    candidates: list[Path] = []
    for d in (configured_dir, os.environ.get("PHOTOBRIDGE_FFMPEG_DIR", "")):
        if d:
            candidates.append(Path(d) / exe)
    for base in _app_dirs():
        # 两种铺法都认：官方压缩包解出来的 `bin/` 子目录，以及直接把
        # exe 拷到 `tools/ffmpeg/` 下的扁平铺法。
        candidates.append(base / "tools" / "ffmpeg" / exe)
        candidates.append(base / "tools" / "ffmpeg" / "bin" / exe)
    found = ""
    for cand in candidates:
        if cand.is_file():
            found = str(cand)
            break
    if not found:
        found = shutil.which(name) or ""

    with _BIN_LOCK:
        _BIN_CACHE[key] = found or None
    if not found:
        raise FFmpegMissing(_missing_hint(name))
    return found


def _missing_hint(name: str) -> str:
    return (
        f"找不到 {name}：视频功能需要 ffmpeg。请把它放到程序的 "
        f"tools/ffmpeg/ 目录下，或在配置里设置 video.ffmpeg_dir。"
    )


def reset_bin_cache() -> None:
    """测试用：清掉可执行文件查找缓存（换配置后必须清）。"""
    with _BIN_LOCK:
        _BIN_CACHE.clear()


# --------------------------------------------------------------------------
# 并发闸门
# --------------------------------------------------------------------------

_SLOTS: threading.BoundedSemaphore | None = None
_SLOTS_N = 0
_SLOTS_LOCK = threading.Lock()


def _slots(limit: int = DEFAULT_CONCURRENCY) -> threading.BoundedSemaphore:
    """全局解码闸门。第一次调用时按 `limit` 建，之后复用。

    为什么是模块级而不是每实例一个：索引器与缩略图队列跑在不同的线程池里，
    各自建闸门等于"并发上限 × 2"。
    """
    global _SLOTS, _SLOTS_N
    with _SLOTS_LOCK:
        if _SLOTS is None or _SLOTS_N != limit:
            _SLOTS = threading.BoundedSemaphore(max(1, int(limit)))
            _SLOTS_N = limit
        return _SLOTS


def set_concurrency(limit: int) -> None:
    """设置并发上限（配置变化时调用）。已在跑的任务不受影响。"""
    _slots(limit)


# --------------------------------------------------------------------------
# 子进程调用
# --------------------------------------------------------------------------

def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    """跑一个子进程，带超时与"不弹控制台窗口"。

    `stdin=DEVNULL` 是必须的：ffmpeg 会读 stdin 的按键（交互式覆盖询问），
    在 Windows 服务/托盘进程里 stdin 是无效句柄，读它会直接卡住。
    """
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "check": False,
    }
    if os.name == "nt":
        # 0x08000000 = CREATE_NO_WINDOW：否则每次抽帧都在托盘程序旁边
        # 闪一下黑框。
        kwargs["creationflags"] = 0x08000000
    return subprocess.run(argv, **kwargs)  # noqa: S603 —— 参数数组，无 shell


def probe(
    path: str | Path, *, timeout: float = PROBE_TIMEOUT, ffmpeg_dir: str = "",
) -> ProbeResult:
    """用 ffprobe 读元数据。**任何失败都只返回 ok=False，不抛异常。**

    不抛异常的理由：一张坏视频不该中断整轮扫描，也不该让缩略图 worker
    崩掉。唯一抛的是 `FFmpegMissing` —— 那是部署问题，不是数据问题。
    """
    exe = find_bin("ffprobe", configured_dir=ffmpeg_dir)
    argv = [
        exe, "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        "-i", str(path),
    ]
    with _slots():
        try:
            cp = _run(argv, timeout=timeout)
        except subprocess.TimeoutExpired:
            log.warning("ffprobe 超时（%.0fs）：%s", timeout, path)
            return ProbeResult(ok=False, error="探测超时")
        except OSError as exc:
            return ProbeResult(ok=False, error=f"无法启动 ffprobe：{exc}")

    if cp.returncode != 0:
        err = (cp.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        return ProbeResult(ok=False, error=err[-1] if err else f"exit {cp.returncode}")
    try:
        data = json.loads((cp.stdout or b"{}").decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        return ProbeResult(ok=False, error="ffprobe 输出不是 JSON")

    return _parse_probe(data)


def _parse_probe(data: dict[str, Any]) -> ProbeResult:
    """把 ffprobe 的 JSON 压成我们要的那几个字段。"""
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    if not video:
        return ProbeResult(ok=False, error="没有视频轨")

    duration = 0.0
    for raw in (fmt.get("duration"), video.get("duration")):
        try:
            duration = max(duration, float(raw))
        except (TypeError, ValueError):
            continue

    return ProbeResult(
        ok=True,
        container=_container_from_format(str(fmt.get("format_name") or "")),
        duration=duration,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        vcodec=str(video.get("codec_name") or "").lower(),
        acodec=str(audio.get("codec_name") or "").lower(),
    )


def _container_from_format(format_name: str) -> str:
    """`mov,mp4,m4a,3gp,3g2,mj2` → 'mp4'；`matroska,webm` → 'mkv'。"""
    names = {n.strip() for n in format_name.split(",") if n.strip()}
    if not names:
        return ""
    if "matroska" in names:
        # webm 是 matroska 的子集，ffprobe 对两者都报 `matroska,webm`。
        return "webm" if "webm" in names else "mkv"
    if "avi" in names:
        return "avi"
    if names & {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}:
        return "mov" if "mov" in names and "mp4" not in names else "mp4"
    return sorted(names)[0]


def is_playable(*, container: str, vcodec: str) -> bool:
    """能不能直接交给 `<video>` 播（D2：按容器 + 编码判，不只看扩展名）。

    `hevc`（H.265）必须判不可播：iPhone 默认录 HEVC，而 Chrome 桌面**不**支持
    它 —— 这正是"我手机上明明能看"的典型来源。
    """
    return container in PLAYABLE_CONTAINERS and vcodec in PLAYABLE_VCODECS


def extract_frame(
    src: str | Path,
    dst: Path,
    *,
    at: float = 1.0,
    timeout: float = FRAME_TIMEOUT,
    ffmpeg_dir: str = "",
) -> Path:
    """抽一帧到 `dst`（JPEG）。失败回退首帧，再失败抛 `RuntimeError`。

    D3：默认取第 1 秒。理由：手机视频常有 0.3–0.8 秒的黑场/对焦，
    首帧经常是一张纯黑图；而 `-ss 1` 对 ffmpeg 是"先跳到关键帧再解"，
    代价几乎为零。1 秒取不到（视频短于 1 秒、或文件截断）时回退 0，
    再失败就让调用方用占位图。

    ⚠️ `-ss` 必须放在 `-i` **之前**（快速定位，不解全片）。
    """
    exe = find_bin("ffmpeg", configured_dir=ffmpeg_dir)
    dst.parent.mkdir(parents=True, exist_ok=True)
    last = ""
    for seek in (at, 0.0):
        argv = [
            exe, "-hide_banner", "-loglevel", "error",
            "-nostdin", "-ss", f"{seek:.3f}", "-i", str(src),
            "-frames:v", "1", "-an", "-sn", "-dn",
            "-q:v", "3", "-y", "-f", "image2", str(dst),
        ]
        with _slots():
            try:
                cp = _run(argv, timeout=timeout)
            except subprocess.TimeoutExpired:
                last = f"抽帧超时（{timeout:.0f}s）"
                log.warning("ffmpeg 抽帧超时 seek=%.1fs：%s", seek, src)
                continue
            except OSError as exc:
                raise RuntimeError(f"无法启动 ffmpeg：{exc}") from exc
        if cp.returncode == 0 and dst.is_file() and dst.stat().st_size > 0:
            return dst
        err = (cp.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        last = err[-1] if err else f"exit {cp.returncode}"
        log.info("抽帧失败 seek=%.1fs %s：%s", seek, src, last)
    raise RuntimeError(f"抽帧失败：{last or '未知原因'}")
