"""句柄级打开原语：与 `sandbox` 无关的最底层实现。

为什么单独一个模块：`backend/app/video.py` 被 `config` 反向依赖
（`config` 需要视频扩展名表），因此它**不能** import `sandbox`
（`sandbox` 又 import `config`，会成环 —— `backend/tests/test_layering.py`
明确禁止用函数内延迟导入掩盖）。但 `video` 读容器头时同样需要
"打开之后复核身份"这一步，所以把这几个纯 os 级别的原语放这里，
`sandbox` 与 `video` 各自引用，不复制逻辑。

本模块只依赖标准库：不 import `config`、不 import `errors`，
抛出的异常也是 `OSError` 的子类（`HandleMismatch`），由调用方决定
翻译成什么对外错误。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import IO


class HandleMismatch(OSError):
    """打开的句柄与"现在的路径"指向的对象不一致。

    `reason`：
      * ``mismatch`` —— 路径被换成了另一个文件（junction/symlink 换位）；
      * ``lost``     —— 句柄在、路径没了（被删/被改名）。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def strip_win_prefix(path: str) -> str:
    """剥掉 realpath 可能带上的 Windows 扩展长度前缀。

    ★ 这里修的是一个**只在并发下出现、且会拒绝合法请求**的 Windows 坑。

    `os.path.realpath()` 在 Windows 上调用 `nt._getfinalpathname()`，
    它返回两种形态的路径：

      * `C:\\Users\\...\\lib\\a.jpg`        （普通）
      * `\\\\?\\C:\\Users\\...\\lib\\a.jpg`  （扩展长度前缀）

    哪种形态取决于系统当时的内部状态 —— 实测同一进程内并发调用时
    **同一个路径会随机返回两者之一**，且删除挂起的文件还会返回
    `\\\\?\\C:\\$Extend\\$Deleted\\...`。而 `os.path.commonpath` 见到
    `\\\\?\\c:\\...` 会把它当成 UNC（服务器名是 `?`），于是判为
    "different drive" 并抛 ValueError。

    后果：两台手机同时上传照片时，**有一半请求被沙箱拒绝**，
    而照片本身完全合法。更糟的是错误文案（"不在同一磁盘分区"）
    会把人引向磁盘分区去排查，与真实原因毫无关系。

    前缀剥离是无损的：`\\\\?\\C:\\x` 与 `C:\\x` 指同一个文件。
    """
    p = path
    if p.startswith("\\\\?\\"):
        p = p[4:]
        # `\\?\UNC\server\share` 剥掉前缀后要补回前导 `\\`
        if p[:4].upper() == "UNC\\":
            p = "\\\\" + p[4:]
    elif p.startswith("\\\\.\\"):
        p = p[4:]
    return p


def norm_final(path: str) -> str:
    """可比较形式：剥前缀 + normcase。

    normcase 单独用在**比较**上（Windows 大小写不敏感），
    不能用在返回值上 —— 会把真实磁盘上的大小写抹掉。
    """
    return os.path.normcase(strip_win_prefix(path))


def identity(st: os.stat_result) -> tuple[int, int]:
    """文件身份。Windows 上也给 st_ino（NTFS 文件索引），可用来比对。"""
    return (int(getattr(st, "st_dev", 0)), int(getattr(st, "st_ino", 0)))


def assert_same_path(again: Path, path: Path) -> None:
    """复核第一步：重新解析出来的路径必须与刚才校验的完全一致。"""
    if norm_final(str(again)) != norm_final(str(path)):
        raise HandleMismatch("mismatch")


def assert_identity(fd: int, path: Path) -> None:
    """复核第二步：句柄指向的必须是 `lstat(path)` 看到的那个文件。

    `st_ino` 在 NTFS 上是文件索引，junction 换位后指向的是另一个文件，
    两者不等 → 拒绝，不读一个字节。
    """
    try:
        st_fd = os.fstat(fd)
        st_path = os.lstat(path)
    except OSError:
        # 句柄在、路径没了（被删/被换名）：句柄本身仍然可用，但"路径语义"
        # 已经不成立。拒绝比继续读更安全 —— 调用方会按 404 处理。
        raise HandleMismatch("lost") from None
    if identity(st_fd) != identity(st_path):
        raise HandleMismatch("mismatch")


class PathContractError(ValueError):
    """违反了「文件访问层只接受绝对路径」的契约。

    为什么用异常而不是就地转换：`os.open("a.jpg")` 会静默按**进程 CWD**
    解析。CWD 在托盘、计划任务、PyInstaller 三种启动方式下完全不同，
    于是"同一个相对路径"会指向三个不同的文件，而失败时症状是"照片找不到"
    或"打开了另一张照片"—— 后者尤其糟：校验过的是 A，读的是 B。

    相对→绝对的转换**只允许**发生在 `sandbox.resolve()` 这一处（它同时做
    库根归属校验）。这里收到相对路径说明调用链漏了一步，必须当场炸。
    """

    def __init__(self, path: object) -> None:
        super().__init__(
            f"文件访问层只接受绝对路径，收到相对路径：{path!r}。"
            "相对路径请先经 sandbox.resolve(root, rel, op) 转换。"
        )


def require_absolute(path: str | Path) -> Path:
    """校验并返回绝对路径；违反契约直接抛 `PathContractError`。

    只做"是不是绝对"这一件事，不做 `resolve()`（不碰符号链接、不查库根）——
    那是 `sandbox.resolve()` 的职责。这里再"顺手转换"一次，就等于把
    唯一转换点又拆成两处。
    """
    p = Path(path)
    if not p.is_absolute():
        raise PathContractError(path)
    return p


#: `open_verified()` 支持的模式。故意只开放这三个 —— 需要 `O_TRUNC`
#: 或 `O_EXCL` 的写入必须走 `sandbox.open_write()`（那里有完整的 root 校验）。
VERIFIED_FLAGS: dict[str, int] = {
    "rb": os.O_RDONLY,
    "r+b": os.O_RDWR,
    "ab": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
}


def open_verified(path: str | Path, mode: str = "rb", *,
                  buffering: int = 0) -> IO[bytes]:
    """打开一个**不在库根下**的文件，并复核它不是被换位来的。

    与 `sandbox.open_read()` 的区别：不做 root 归属校验（调用方给的是缓存
    文件、上传临时文件，或已经过 `resolve()` 的库内路径），但仍然做
    "打开之后再核对身份"这一步 —— 也就是消除 TOCTOU 的那两条：重解析后
    路径一致、句柄 identity 与 `lstat(path)` 一致。

    ## 输入契约（F-03）

    只接受**绝对路径**。相对路径一律抛 `PathContractError`，不做任何
    "看起来像相对路径就拼 CWD"的猜测 —— 那样等于把转换点复制到这里。

    异常：`PathContractError`（违反了上面的契约）、`FileNotFoundError`
    （不存在，调用方按缺失处理）、`OSError`、`HandleMismatch`（复核失败）。
    """
    flags = VERIFIED_FLAGS.get(mode)
    if flags is None:
        raise ValueError(f"open_verified 不支持的模式：{mode!r}")
    p = require_absolute(path)
    try:
        fd = os.open(str(p), flags | getattr(os, "O_BINARY", 0), 0o644)
    except FileNotFoundError:
        raise
    except OSError:
        raise
    try:
        assert_same_path(Path(os.path.realpath(p)), p)
        assert_identity(fd, p)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, mode, buffering=buffering)


__all__ = [
    "HandleMismatch", "PathContractError", "VERIFIED_FLAGS",
    "assert_identity", "assert_same_path", "identity",
    "norm_final", "open_verified", "require_absolute", "strip_win_prefix",
]
