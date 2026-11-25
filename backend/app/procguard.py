"""媒体子进程的护栏：时间、内存、可写目录。

## 为什么需要这一层

`ffmpeg` / `ffprobe` 吃的是**用户文件**。用户文件里可以有什么，事先不知道：

* 一个 8K 的 HEVC，解码一帧就能吃掉 2 GB —— 32 位进程直接 OOM，整机开始
  换页，用户看到的是"电脑卡死了"，不是"这张封面没生成"。
* 一个构造过的容器，让 demuxer 在解析头里申请一块声明的巨大缓冲区。
* 一个文件名里带 `..\\..\\config.json` 的"视频"（虽然上层已经过了路径
  沙箱，但子进程本身不该有能力写程序目录）。

所以每条外部命令都在这里统一出口：**有超时、有内存上限、有专用工作目录**。
进程一旦越界，死的是子进程，不是托盘程序。

## 三件事分别怎么落地

| 约束 | Windows | POSIX |
|---|---|---|
| 内存 | Job Object `JOB_OBJECT_LIMIT_PROCESS_MEMORY` | `setrlimit(RLIMIT_AS)` in `preexec_fn` |
| 超时后连子孙一起杀 | `taskkill /T /F` | `killpg`（`start_new_session=True`） |
| 可写目录 | `cwd` + `TMP/TEMP` 指向 scratch | 同上 |

## 边界（写清楚，别让人以为这是沙箱）

这**不是**沙箱。同一账户下，子进程仍然能读写这个账户能读写的任何东西 ——
`cwd` 只是"默认落点"，不是"只能落这里"。真正的隔离要把进程降权或放进
容器里，那属于部署形态，不是这一层能承诺的事。这里能做的是：
**跑飞了会被掐死、吃太多会被掐死、默认不会把中间文件丢进程序目录**。

对应整改单 P2：「让媒体处理进程无权改写程序、配置和凭据」当中，
"默认落点 + 越界即杀"这半截；降权那半截写在 `docs/06` §13.4 的残余风险里。
"""
from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_MEMORY_MB",
    "ProcResult",
    "ProcTimeout",
    "SpawnedProc",
    "memory_mb",
    "open_guarded",
    "run",
    "scratch_dir",
]

#: 单个媒体子进程的内存上限（MB）。1536 足够解一个 4K 关键帧 + 缩放，
#: 又远小于"把一台 8 GB 的笔记本拖进交换"的量。
DEFAULT_MEMORY_MB = 1536

#: 环境变量覆盖（部署时可以按机器调，不必改代码）。
MEMORY_ENV = "PB_MEDIA_MEMORY_MB"


class ProcTimeout(RuntimeError):
    """子进程超时，且已经被连同子孙一起杀掉。"""


class ProcResult:
    """`run()` 的返回值（只留我们真的会用到的字段）。"""

    __slots__ = ("returncode", "stdout", "stderr", "elapsed")

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes,
                 elapsed: float) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.elapsed = elapsed

    def __repr__(self) -> str:  # pragma: no cover - 只为调试好看
        return (f"ProcResult(rc={self.returncode}, "
                f"out={len(self.stdout)}B, err={len(self.stderr)}B, "
                f"{self.elapsed:.2f}s)")


def memory_mb() -> int:
    """当前生效的内存上限（MB）。"""
    raw = str(os.environ.get(MEMORY_ENV) or "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_MEMORY_MB


def scratch_dir() -> Path:
    """媒体子进程的专用工作目录。

    放在系统临时目录下的固定名字里，**不放在程序目录**：这样即便 ffmpeg
    按"当前目录"写中间文件，也不会落在 `backend/`、`data/` 或配置旁边。
    目录不存在就建；建不出来退回系统临时目录（宁可能用，不要因此让
    整条缩略图链路停摆）。
    """
    base = Path(tempfile.gettempdir()) / "photobridge-media"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:      # pragma: no cover - 磁盘满/权限异常
        return Path(tempfile.gettempdir())
    return base


# ---------------------------------------------------------------------------
# Windows：Job Object
# ---------------------------------------------------------------------------

if os.name == "nt":  # pragma: no cover - 平台分支
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount",
            "OtherOperationCount", "ReadTransferCount",
            "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _make_job(limit_mb: int) -> Any:
        """建一个带内存上限的 Job Object；失败返回 None（不阻断主流程）。"""
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        info.ProcessMemoryLimit = int(limit_mb) * 1024 * 1024
        ok = _kernel32.SetInformationJobObject(
            handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            _kernel32.CloseHandle(handle)
            return None
        return handle

    def _attach(handle: Any, pid: int) -> None:
        if not handle:
            return
        proc = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                                     False, int(pid))
        if not proc:
            return
        try:
            _kernel32.AssignProcessToJobObject(handle, proc)
        finally:
            _kernel32.CloseHandle(proc)

    def _close(handle: Any) -> None:
        if handle:
            _kernel32.CloseHandle(handle)

    def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
        """杀整棵树。`/T` 是关键：ffmpeg 自己不派生子进程，但 job 里可能有。"""
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=0x08000000)
        except Exception:       # noqa: BLE001
            pass
        try:
            proc.kill()
        except Exception:       # noqa: BLE001
            pass

    def _popen_kwargs(limit_mb: int, cwd: Path) -> dict[str, Any]:
        return {
            "stdin": subprocess.DEVNULL,
            # 0x08000000 = CREATE_NO_WINDOW：否则每次抽帧都在托盘程序旁边闪黑框
            "creationflags": 0x08000000,
            "cwd": str(cwd),
        }

    def _after_spawn(proc: subprocess.Popen[bytes], limit_mb: int) -> Any:
        handle = _make_job(limit_mb)
        pid = getattr(proc, "pid", None)
        if handle and isinstance(pid, int) and pid > 0:
            try:
                _attach(handle, pid)
            except Exception:       # noqa: BLE001
                # attach 失败（进程已经退出、权限不够）不影响"能跑"：
                # 少一层内存上限，好过整条缩略图链路停摆
                pass
        return handle
else:  # POSIX
    def _popen_kwargs(limit_mb: int, cwd: Path) -> dict[str, Any]:
        limit = int(limit_mb) * 1024 * 1024

        def _limit() -> None:          # pragma: no cover - 在子进程里跑
            import resource

            try:
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
            except (ValueError, OSError):
                pass

        return {
            "stdin": subprocess.DEVNULL,
            "cwd": str(cwd),
            # 新会话：超时后 killpg 才能一次收掉整棵进程树
            "start_new_session": True,
            "preexec_fn": _limit,
        }

    def _after_spawn(proc: subprocess.Popen[bytes], limit_mb: int) -> Any:
        return None

    def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:       # noqa: BLE001
            try:
                proc.kill()
            except Exception:   # noqa: BLE001
                pass

    def _close(handle: Any) -> None:
        return None


class SpawnedProc:
    """已经挂上护栏的 `Popen`。

    转码那条路要边跑边读进度，不能等它结束，所以必须暴露 Popen 本身；
    但也必须把 job 句柄一起带出来，否则 `finally` 里没人关它（Windows 上
    `KILL_ON_JOB_CLOSE` 靠关闭句柄来保证"程序没了子进程也死"）。
    """

    __slots__ = ("proc", "_handle", "elapsed")

    def __init__(self, proc: subprocess.Popen[bytes], handle: Any) -> None:
        self.proc = proc
        self._handle = handle
        self.elapsed = time.monotonic()

    def kill(self) -> None:
        _kill_tree(self.proc)

    def close(self) -> None:
        _close(self._handle)
        self._handle = None

    def __enter__(self) -> SpawnedProc:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def open_guarded(argv: list[str], *, limit_mb: int | None = None,
                 cwd: Path | None = None, **popen_kwargs: Any) -> SpawnedProc:
    """起一个带护栏的子进程（调用方自己管生命周期）。"""
    limit = int(limit_mb if limit_mb else memory_mb())
    work = cwd or scratch_dir()
    kwargs = _popen_kwargs(limit, work)
    kwargs.update(popen_kwargs)
    # 中间文件默认落在工作目录：ffmpeg 会看 TMPDIR/TEMP
    env = dict(os.environ)
    env.setdefault("TMPDIR", str(work))
    env["TMP"] = str(work)
    env["TEMP"] = str(work)
    kwargs["env"] = env
    proc = subprocess.Popen(argv, **kwargs)      # noqa: S603 —— 参数数组，无 shell
    try:
        handle = _after_spawn(proc, limit)
    except Exception:       # noqa: BLE001
        _kill_tree(proc)
        raise
    return SpawnedProc(proc, handle)


def run(argv: list[str], *, timeout: float, limit_mb: int | None = None,
        cwd: Path | None = None) -> ProcResult:
    """跑一个子进程，超时/超内存就掐死，返回它的输出。

    超时抛 `ProcTimeout`（调用方据此写"转码超时"这类用户可读的结论）；
    其余情况一律返回 `ProcResult`，**不按退出码抛异常** —— 上层要看
    stderr 的最后一行来决定怎么解释这次失败。
    """
    started = time.monotonic()
    with open_guarded(argv, limit_mb=limit_mb, cwd=cwd,
                      stdout=subprocess.PIPE, stderr=subprocess.PIPE) as guard:
        proc = guard.proc
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            guard.kill()
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:      # pragma: no cover
                out, err = b"", b""
            raise ProcTimeout(f"子进程超时（{timeout:.0f}s），已终止") from None
    return ProcResult(proc.returncode if proc.returncode is not None else -1,
                      out or b"", err or b"", time.monotonic() - started)
