"""托盘生命周期的回归测试（ISSUE-043 的"退出必须真的退"部分）。

## 为什么这两条判据值得钉住

**① 点"退出"之后进程必须消失。** 托盘程序由 `pythonw.exe` 启动，没有控制台
也没有主窗口：用户点"退出"、图标消失，如果进程还在，他既看不到、也不知道
该不该去任务管理器强杀。真实反馈就是"后台经常残留"。

优雅路径（`stop.set()` → `serve()` 返回 → `_shutdown()` 停监听/停监听线程/
drain 缩略图队列最多 20 秒/checkpoint WAL）绝大多数时候几十毫秒就完成，
但只要其中一步卡住，用户就永远看不到那个进程退出。所以 `_on_quit()` 里挂了
一个 15 秒的看门狗。

⚠️ 测试里**绝不能让它真的跑起来**：`_force_exit()` 调的是 `os._exit(0)`，
真跑一次会把 pytest 自己杀掉（现象是"测试输出到一半就没了、退出码 0"）。
所以这里把 `threading.Timer` 换成只记录不执行的假计时器，再单独测
`_force_exit()` 的行为（用假的 `os`）。

**② `pythonw` 下 `sys.stdout is None` 会把启动直接打死。**
`uvicorn.Config.__init__` 会 `dictConfig` 自己的日志格式器，那个格式器要读
`sys.stdout.isatty()` —— `None` 上取属性就是 `AttributeError`，被 dictConfig
包一层后变成 `ValueError: Unable to configure formatter 'default'`。
这条错误里一个字都没提到控制台，而 pythonw 又不留窗口，于是现象是
"开机后程序根本没启动"。`scripts/run_tray.py::ensure_std_streams()` 负责补上。
"""
from __future__ import annotations

import importlib.util
import sys as _sys
from pathlib import Path
from typing import Any

import pytest

from backend.app import tray


class _Loop:
    def __init__(self) -> None:
        self.ran: list[str] = []

    def call_soon_threadsafe(self, fn) -> None:
        self.ran.append(getattr(fn, "__name__", "?"))
        fn()


class _Stop:
    def __init__(self) -> None:
        self.called = False

    def set(self) -> None:
        self.called = True


class _FakeOS:
    """替掉 `tray.os`，把 `_exit` 记下来（绝不能真把测试进程干掉）。"""

    def __init__(self) -> None:
        self.codes: list[int] = []

    def _exit(self, code: int) -> None:       # noqa: SLF001 —— 就是被测的名字
        self.codes.append(code)


class _Timer:
    def __init__(self, interval: float, fn) -> None:
        self.interval = interval
        self.fn = fn
        self.daemon = False
        self.started = False

    def start(self) -> None:
        self.started = True


class _FakeThreading:
    """只提供 `Timer`：托盘退出路径上只用到它。"""

    def __init__(self) -> None:
        self.timers: list[_Timer] = []

    def Timer(self, interval: float, fn) -> _Timer:      # noqa: N802
        timer = _Timer(interval, fn)
        self.timers.append(timer)
        return timer


def _bare_controller() -> tray.TrayController:
    """不走 `__init__`：这里只测退出路径，不需要真的建图标。"""
    ctrl = tray.TrayController.__new__(tray.TrayController)
    ctrl._loop = _Loop()          # type: ignore[attr-defined]
    ctrl._stop = _Stop()          # type: ignore[attr-defined]
    ctrl._icon = None             # type: ignore[attr-defined]
    return ctrl


def test_quit_signals_stop_and_arms_watchdog(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_threading = _FakeThreading()
    monkeypatch.setattr(tray, "threading", fake_threading)

    ctrl = _bare_controller()
    ctrl._on_quit()

    assert ctrl._stop.called is True          # type: ignore[attr-defined]
    assert len(fake_threading.timers) == 1
    timer = fake_threading.timers[0]
    assert timer.started is True
    assert timer.daemon is True
    assert timer.fn is tray._force_exit
    assert timer.interval == pytest.approx(tray._QUIT_WATCHDOG_SECONDS)


def test_force_exit_exits_with_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_os = _FakeOS()
    monkeypatch.setattr(tray, "os", fake_os)

    tray._force_exit()

    assert fake_os.codes == [0]


def test_quit_without_loop_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    """`on_ready` 还没跑过（服务起不来）时点退出，也不能抛异常。"""
    fake_threading = _FakeThreading()
    monkeypatch.setattr(tray, "threading", fake_threading)
    ctrl = tray.TrayController.__new__(tray.TrayController)
    ctrl._loop = None             # type: ignore[attr-defined]
    ctrl._stop = None             # type: ignore[attr-defined]
    ctrl._icon = None             # type: ignore[attr-defined]

    ctrl._on_quit()

    assert len(fake_threading.timers) == 1     # 看门狗照样要挂上


def _load_run_tray():
    path = Path(__file__).resolve().parents[2] / "scripts" / "run_tray.py"
    spec = importlib.util.spec_from_file_location("pb_run_tray", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeSys:
    """冒充 `sys`：只有 `stdout/stderr` 是 None，其余透传真的 sys。"""

    def __init__(self) -> None:
        self.stdout: Any = None
        self.stderr: Any = None
        self.path = list(_sys.path)

    def __getattr__(self, name: str) -> Any:
        return getattr(_sys, name)


def test_run_tray_fills_missing_std_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pythonw.exe` 下 `sys.stdout/stderr` 是 `None`，入口必须先补上。

    不补的后果：`uvicorn` 建日志格式器时 `sys.stdout.isatty()` 抛
    `AttributeError`，外层报 `ValueError: Unable to configure formatter
    'default'` —— 一个跟"没有控制台"毫无字面关联的错误，直接把启动打死。
    """
    mod = _load_run_tray()
    fake_sys = _FakeSys()
    monkeypatch.setattr(mod, "sys", fake_sys)

    mod.ensure_std_streams()

    assert fake_sys.stdout is not None
    assert fake_sys.stderr is not None
    # 只要求"能写、没关"：Windows 上 `open(os.devnull)` 得到的其实是 CON 类
    # 字符设备，`isatty()` 返回 True（不能拿它当判据），但 uvicorn 只是拿
    # isatty 决定要不要上色，够用。
    fake_sys.stdout.write("")
    fake_sys.stderr.write("")
    assert fake_sys.stdout.closed is False
    fake_sys.stdout.close()
    fake_sys.stderr.close()


def test_run_tray_bootstrap_is_cwd_independent(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """引导逻辑必须把仓库根放进 `sys.path`，与工作目录无关。

    注册表 `Run` 键拉起进程时工作目录是 `C:\\Windows\\System32`；
    只要这条成立，"开机自启"才可能真的生效。
    """
    mod = _load_run_tray()
    monkeypatch.chdir(tmp_path)

    mod.bootstrap()

    assert str(Path(mod.__file__).resolve().parents[1]) in mod.sys.path
