"""归档：把上传上来的照片落到库里的最终位置。

## 目录布局

    <库根>/<upload.subdir>/YYYY/YYYY-MM/YYYYMMDD_HHMMSS[_序号].jpg

例：`D:\\Photos\\手机上传\\2024\\2024-05\\20240512_183000.jpg`

为什么按 年/年-月 两级：全部平铺的话 4900 张已经让资源管理器卡顿，
5 年后是 2 万多张；按 年/年-月/日 三级则 5 年下来 1800 个目录，
浏览时反而要点很多层才看到照片。

## 三个必须做对的点

1. **按本地时间归档，不是 UTC。** `taken_at` 是 UTC 时间戳，
   晚上 23:30 拍的照片在 UTC+8 下是次日 15:30。用 UTC 归档会把
   一整晚的照片挪到下个月甚至下一年，用户按日期找照片时找不到。
   `datetime.fromtimestamp()` 用的就是本机时区，这里依赖它而不是
   自己加偏移量 —— 用户改时区后行为自动跟着改。

2. **占位与落地都必须原子。** 先 `os.open(..., O_CREAT | O_EXCL)`
   原子地占住文件名，再 `os.replace` 把临时文件换上去。
   用 `exists()` 检查再写是 TOCTOU：两台上传同时通过检查、同时写，
   后写的覆盖先写的，**用户的照片静默消失一张**。
   这是唯一可能造成永久数据丢失的软件缺陷（风险登记册 R-004）。

3. **临时文件必须与库同卷。** `os.replace` 在 Windows 上跨卷
   直接抛 `OSError [WinError 17]`（EXDEV），"原子落盘"不可能实现。
   所以临时区是 `<库根>/.photobridge_tmp/` 而不是 `data/uploads_tmp/`。
   这里显式检查并给出可读的错误，而不是把 WinError 17 抛给用户。
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .config import RootConfig, TMP_DIRNAME
from .errors import AppError, ArchiveError, E1001, SandboxError
from . import sandbox

log = logging.getLogger(__name__)

#: 同一秒内的第 2 张开始追加 `_1`、`_2`……第一张不带序号。
#: 这样绝大多数文件名是干净的 `20240512_183000.jpg`。
SEQ_START = 1

#: 序号上限。超过之后改用毫秒（`_123`），因为 4 位数序号
#: 会让"按文件名排序"与"按时间排序"不再一致。
SEQ_MAX = 999

#: 候选名上限。**必须有上限**：若目标目录出现某种"永远 FileExistsError
#: 但文件又不存在"的异常状态（网络盘、杀软隔离区），没有上限就是死循环，
#: 上传接口永远不返回。
MAX_ATTEMPTS = 2000

#: 归档文件名的**主干**允许的字符（扩展名之前的部分）。
#: 时间戳与序号之外不允许任何东西 —— 原始文件名（可能是用户随手
#: 打的、含 emoji 或 `:`）不参与最终命名，它记录在
#: upload_sessions.declared_name 与 audit_log.detail 里。
_ALLOWED_STEM_CHARS = frozenset("0123456789_")

#: 扩展名允许的字符。只放字母数字 —— `.jpg` 合法，`.jpg-lx2` 不合法
#: （那是小米相册回收站的残留扩展名，不是真图片）。
_ALLOWED_EXT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def archive_dir(taken_at: float) -> str:
    """`YYYY/YYYY-MM`，按**本地时区**。"""
    dt = datetime.fromtimestamp(taken_at)
    return f"{dt.year:04d}/{dt.year:04d}-{dt.month:02d}"


def archive_name(taken_at: float, ext: str, seq: int = 0, *, ms: int | None = None) -> str:
    """构造归档文件名。

    seq=0 表示第一个候选（不带序号）。ms 非 None 时改用毫秒消歧。
    """
    dt = datetime.fromtimestamp(taken_at)
    stem = f"{dt:%Y%m%d_%H%M%S}"
    if ms is not None:
        stem = f"{stem}_{ms:03d}"
        if seq:
            stem = f"{stem}_{seq}"
    elif seq:
        stem = f"{stem}_{seq}"
    return f"{stem}{normalize_ext(ext)}"


def normalize_ext(ext: str) -> str:
    """扩展名统一小写并带点。

    `.JPG` / `jpg` / `.jpg` 三种写法都要收敛到 `.jpg`。
    不收敛的话同一个目录里会同时出现 `.JPG` 和 `.jpg`，
    按扩展名筛选时会漏掉一半（Windows 大小写不敏感所以看不出来，
    但导出到 U 盘 / 传到手机 / 用 Python 处理时就会漏）。
    """
    e = (ext or "").strip().lower()
    if not e:
        return ""
    if not e.startswith("."):
        e = "." + e
    return e


def candidate_names(
    taken_at: float, ext: str, *, limit: int = MAX_ATTEMPTS
) -> Iterator[str]:
    """依次产出候选文件名，从最干净的形式开始。"""
    yield archive_name(taken_at, ext)
    for seq in range(SEQ_START, SEQ_MAX + 1):
        if seq > limit:
            return
        yield archive_name(taken_at, ext, seq)
    # 同一秒超过 999 张（现实中不可能，但代码路径必须存在且会终止）
    dt = datetime.fromtimestamp(taken_at)
    for ms in range(1000):
        for seq in range(0, 3):
            if SEQ_MAX + 1 + ms * 3 + seq > limit:
                return
            yield archive_name(taken_at, ext, seq, ms=dt.microsecond // 1000 + ms)


def validate_rel_path(rel_path: str) -> str:
    """确认这是本模块生成的、安全的相对路径。

    文件名部分只允许 `[0-9_.]`。这不是多余的洁癖：归档路径会被
    拼进 `sandbox.resolve()` 与 SQL，而 `subdir` 来自配置、理论上
    可以是用户手改的任意字符串。多一道字符白名单，越界就退化成
    一个明确的 400 而不是把 `..` 带进文件系统调用。
    """
    rel = (rel_path or "").replace("\\", "/").strip("/")
    if not rel:
        raise AppError(E1001, "归档路径为空")
    name = rel.rsplit("/", 1)[-1]
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        raise AppError(E1001, "归档文件名缺少扩展名")
    bad = {c for c in stem if c not in _ALLOWED_STEM_CHARS}
    if bad:
        raise AppError(E1001, f"归档文件名含非法字符：{''.join(sorted(bad))}")
    bad_ext = {c for c in ext if c not in _ALLOWED_EXT_CHARS}
    if bad_ext:
        raise AppError(E1001, f"归档扩展名含非法字符：{''.join(sorted(bad_ext))}")
    return rel


def subdir_prefix(subdir: str | None = None) -> str:
    """`upload.subdir` 转成安全的相对前缀（可能为空串）。"""
    raw = (subdir if subdir is not None else "") or ""
    raw = raw.replace("\\", "/").strip().strip("/")
    if not raw:
        return ""
    # 逐段做与沙箱同源的形态与字符检查，但**不要求目录已存在** ——
    # 归档时要新建目录，此时它当然还不存在。
    for seg in raw.split("/"):
        if not seg or seg in (".", ".."):
            raise AppError(E1001, "上传子目录非法")
        if len(seg) > sandbox.MAX_SEGMENT:
            raise AppError(E1001, "上传子目录名过长")
        # `:` 单独判：沙箱的 ILLEGAL_CHARS 里没有它（它在段级检查里
        # 按 NTFS 数据流单独拦），而这里没有段级检查那一步。
        # 漏掉的话配置里写 `upload.subdir = "a:b"` 会拼出
        # `a:b/2024/...`，Windows 把 `a:b` 当数据流，落盘位置与
        # 预期完全不同 —— 而且**不报错**。
        if ":" in seg:
            raise AppError(E1001, "上传子目录含非法字符")
        bad = sandbox.ILLEGAL_CHARS.intersection(seg)
        if bad or seg.endswith((" ", ".")):
            # 结尾空格/点必须拦：Windows 会静默去掉它们，于是
            # "校验时的名字"与"实际创建的名字"不同 —— 这正是
            # "校验通过但落到别处"的经典绕过。
            # （整串首尾的空白已在上面 strip 掉，配置里手滑多打
            #  一个空格应该被容忍，所以 `"a "` 会被接受。）
            raise AppError(E1001, "上传子目录含非法字符")
        if seg.split(".", 1)[0].upper() in sandbox.RESERVED_NAMES:
            raise AppError(E1001, "上传子目录是系统保留名")
    if len(raw) > sandbox.MAX_RELPATH // 2:
        raise AppError(E1001, "上传子目录过长")
    return raw


def build_rel_path(
    taken_at: float,
    ext: str,
    *,
    subdir: str | None = None,
    seq: int = 0,
) -> str:
    """算出归档相对路径（不做冲突消解，不碰文件系统）。"""
    prefix = subdir_prefix(subdir)
    parts = [p for p in (prefix, archive_dir(taken_at), archive_name(taken_at, ext, seq)) if p]
    return validate_rel_path("/".join(parts))


def _same_volume(a: Path, b: Path) -> bool:
    """粗判两个路径是否同卷。

    用盘符比较而不是 `os.stat().st_dev`：临时文件与目标目录此时
    都可能还不存在，stat 拿不到。盘符比较对"同一台机器上的两个盘"
    是充分的，而跨卷错误恰恰只在这种情况下发生。
    盘符为空（UNC 路径 `\\\\server\\share`）时无法判断，放行让
    `os.replace` 自己去失败 —— UNC 上的 rename 本来就依赖服务器实现。
    """
    da, db_ = a.drive.upper(), b.drive.upper()
    if not da or not db_:
        return True
    return da == db_


def reserve(root: RootConfig, rel_path: str, *, cfg=None) -> Path:
    """原子地占住一个文件名，返回已创建的空文件路径。

    `O_CREAT | O_EXCL` 是文件系统级别的"若不存在则创建"，
    两个并发上传只有一个能成功，另一个拿到 FileExistsError。
    """
    rel = validate_rel_path(rel_path)
    try:
        dst = sandbox.resolve(root, rel, "write", cfg=cfg)
    except SandboxError as exc:
        raise _as_exists_or_raise(exc) from None
    parent = dst.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveError(f"无法创建归档目录：{exc!r}") from exc
    # 目录刚建出来，重新解析一次以确保它仍在根内且不是 reparse point
    try:
        dst = sandbox.resolve(root, rel, "write", cfg=cfg)
    except SandboxError as exc:
        raise _as_exists_or_raise(exc) from None
    try:
        fd = os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o644)
    except FileExistsError:
        raise
    except PermissionError:
        # ★ 名字"正忙"会伪装成权限错误。候选名刚被别的线程 os.replace
        # 覆盖、或正处在 delete-pending 状态时，Windows 的
        # ERROR_ACCESS_DENIED 被 Python 映射成 PermissionError，
        # 而不是 FileExistsError。把它当权限问题抛出去，会让并发
        # 上传随机失败（实测 12 线程挂 2 个），用户看到"保存失败"。
        # 目录本身不可写的情况由 place() 开头的探测提前拦掉。
        raise FileExistsError(f"name busy: {rel_path}") from None
    except OSError as exc:
        raise ArchiveError(f"无法创建归档文件：{exc!r}") from exc
    os.close(fd)
    return dst


#: 按目录串行化"选名 → 占位 → 换入"。仅用于进程内多线程/多事件循环
#: 场景（索引器线程 + 上传、pytest 里每个线程一个 asyncio.run）。
#: 单事件循环下这段同步代码本来就不会被打断，但显式加锁让
#: "同一目录不会有两次并发选名"成为代码保证而不是时序巧合。
_dir_locks: dict[str, threading.Lock] = {}
_dir_locks_guard = threading.Lock()


def _dir_lock(key: str) -> threading.Lock:
    with _dir_locks_guard:
        lock = _dir_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _dir_locks[key] = lock
        return lock


def _probe_writable(directory: Path) -> None:
    """确认目录真的可写，并给出比 PermissionError 有用的错误。

    没有这一步，目录不可写（只读盘、被别的程序独占）时会在候选名
    循环里连撞 2000 次 PermissionError，最后报"无法找到可用文件名"，
    与真实原因毫无关系。
    """
    probe = directory / f".pb_write_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o644)
    except OSError as exc:
        raise ArchiveError(f"归档目录不可写：{exc!r}") from exc
    os.close(fd)
    try:
        os.unlink(probe)
    except OSError:
        log.debug("清理探测文件失败：%s", probe.name, exc_info=True)


def _as_exists_or_raise(exc: SandboxError) -> Exception:
    """把"文件正在被删除"翻译成"这个名字现在被占着"。

    并发落盘时多个线程从同一个基准名开始往序号上走。线程 A 刚
    `os.replace` 覆盖掉候选名 `_2.jpg` 的那一瞬间，Windows 会把该
    路径解析到 NTFS 的 `$Extend\\$Deleted` 暂存区（`nt._getfinalpathname`
    的中间状态）。此时线程 B 正解析同一个候选名，于是拿到
    `file_being_deleted`。

    这不是越界，也不是错误 —— 它的真实含义就是"这个名字现在用不了"，
    与 FileExistsError 完全等价。把它当错误抛出去会让**并发上传随机
    失败**（实测 12 个线程里挂 3 个），而用户看到的是"保存失败"。
    """
    if exc.rule == "file_being_deleted":
        return FileExistsError(str(exc))
    return exc


async def place(
    src: Path,
    root: RootConfig,
    taken_at: float,
    ext: str,
    *,
    subdir: str | None = None,
    cfg=None,
) -> tuple[str, Path]:
    """把 `src`（临时文件）落到库里，返回 `(rel_path, 最终绝对路径)`。

    这是**上传流程的最后一公里**，也是唯一会移动用户数据的函数。
    失败时保证：库里不会留下半截文件、`src` 原样保留（调用方可重试）。
    """
    src = Path(src)
    if not src.is_file():
        raise ArchiveError(f"临时文件不存在：{src.name}")

    prefix = subdir_prefix(subdir)
    dir_rel = "/".join(p for p in (prefix, archive_dir(taken_at)) if p)
    dir_abs = sandbox.resolve(root, dir_rel, "write", cfg=cfg)
    try:
        dir_abs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveError(f"无法创建归档目录：{exc!r}") from exc
    _probe_writable(dir_abs)

    if not _same_volume(src, dir_abs):
        # 与其让用户看到 `OSError [WinError 17]`，不如直接说清怎么办
        raise ArchiveError(
            "临时文件与照片库不在同一个磁盘分区，无法原子落盘"
        )

    last_exc: Exception | None = None
    with _dir_lock(str(dir_abs)):
        for attempt, name in enumerate(candidate_names(taken_at, ext)):
            if attempt >= MAX_ATTEMPTS:
                break
            rel = validate_rel_path(f"{dir_rel}/{name}")
            try:
                dst = reserve(root, rel, cfg=cfg)
            except FileExistsError:
                continue
            except ArchiveError as exc:
                last_exc = exc
                break
            try:
                os.replace(src, dst)
            except OSError as exc:
                # 必须清掉占位文件，否则库里留下一个 0 字节的"照片"，
                # 它会被索引器收录，用户在时间线上看到一张打不开的图。
                _unlink_quietly(dst)
                last_exc = exc
                continue
            log.info("归档 %s -> %s", src.name, rel)
            return rel, dst

    raise ArchiveError(f"归档失败，无法为文件找到可用名：{last_exc!r}")


def _unlink_quietly(path: Path) -> None:
    try:
        os.unlink(path)
    except OSError:
        log.debug("清理占位文件失败：%s", path.name, exc_info=True)


def cleanup_tmp(root: RootConfig, *, older_than: float | None = None) -> int:
    """清掉临时区里的残留文件（进程被杀、上传中断留下的）。

    `older_than` 是绝对时间戳（time.time() 口径）。给 None 表示
    清掉全部 —— 只在启动时调用，此时不可能有活跃上传。
    """
    import time as _time

    tmp = root.tmp_dir
    if not tmp.is_dir():
        return 0
    removed = 0
    now = _time.time()
    try:
        entries = list(os.scandir(tmp))
    except OSError:
        log.warning("无法读取临时目录：%s", TMP_DIRNAME, exc_info=True)
        return 0
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            if older_than is not None and entry.stat(follow_symlinks=False).st_mtime >= older_than:
                continue
            if older_than is None and now - entry.stat(follow_symlinks=False).st_mtime < 60:
                # 启动清理也不动刚写进来的文件：用户可能刚好在
                # 程序重启前一秒开始传照片，删掉就要重传几百 MB。
                continue
            os.unlink(entry.path)
            removed += 1
        except OSError:
            log.debug("清理临时文件失败：%s", entry.name, exc_info=True)
    if removed:
        log.info("清理临时文件 %d 个", removed)
    return removed


__all__ = [
    "MAX_ATTEMPTS",
    "SEQ_MAX",
    "SEQ_START",
    "archive_dir",
    "archive_name",
    "build_rel_path",
    "candidate_names",
    "cleanup_tmp",
    "normalize_ext",
    "place",
    "reserve",
    "subdir_prefix",
    "validate_rel_path",
]
