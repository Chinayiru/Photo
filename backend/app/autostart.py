"""开机自启（Windows 注册表 Run 键）。

## 为什么用注册表 Run 而不是启动文件夹 / 计划任务

| 方案 | 优点 | 缺点 |
|---|---|---|
| `HKCU\\...\\Run` | 当前用户即可写、无需管理员、卸载时容易删干净 | 只在用户登录后启动 |
| 启动文件夹快捷方式 | 同上，可视化 | 用户容易误删；`.lnk` 需要 COM 才能创建 |
| 计划任务 | 能在登录前启动 | 需要管理员权限；用户难以自行关闭 |
| 系统服务 | 最稳 | 需要管理员；且**照片库是用户目录，服务账户读不到** |

照片库在用户的 `D:\\Photos`，服务账户（LocalSystem）访问
用户目录会遇到权限问题；而"电脑开机但用户没登录"这个场景对
私人相册没有意义 —— 手机要连的时候，人就在电脑旁边。
所以 Run 键是最合适的，且**不需要管理员权限**。

## 可移植性

`set_autostart()` 接受一个可执行路径。开发时是
`pythonw.exe -m backend.app.main`，打包后是 `PhotoBridge.exe`。
两种形态都写成一条命令行，注册表里就是一行字符串。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["RUN_KEY", "VALUE_NAME", "autostart_command", "is_enabled",
           "set_autostart", "disable_autostart", "ensure_enabled", "status"]

#: 每用户生效，不需要管理员权限。
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

#: 注册表里的值名。用户能在"任务管理器 → 启动"里看到它。
VALUE_NAME = "PhotoBridge"


def _pythonw() -> str:
    """找 `pythonw.exe`（无控制台的 Python）。

    用 `python.exe` 的话每次开机都会闪一个黑框 —— 托盘程序不该这样。
    找不到就退回 `python.exe`（功能一样，只是多一个窗口）。
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        return str(exe)
    cand = exe.with_name("pythonw.exe")
    if cand.is_file():
        return str(cand)
    return str(exe)


def autostart_command() -> str:
    """生成写进注册表的那条命令行。

    * 冻结（打包成 exe）→ `"C:\\...\\PhotoBridge.exe" --tray`
    * 开发态 → `"C:\\...\\pythonw.exe" "D:\\Photos\\scripts\\run_tray.py"`

    ## 为什么开发态不再是 `-m backend.app.main`

    ★ 注册表 `Run` 键**没有"起始位置"这一栏**：Windows 拉起它时工作目录是
    `C:\\Windows\\System32`。而 `python -m backend.app.main` 依赖
    `sys.path[0] = 当前目录` —— 在 System32 里根本没有 `backend` 包，
    结果是"命令被执行了、进程立刻退出"，注册表里看着一切正常，自启却永不生效。

    所以这里改成指向 `scripts/run_tray.py`：它自己把仓库根插进 `sys.path`，
    与工作目录彻底解耦（`scripts/cloud_run.py` 在云端是同一套路子）。
    """
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        return f'"{exe}" --tray'

    repo = Path(__file__).resolve().parents[2]
    entry = repo / "scripts" / "run_tray.py"
    return f'"{_pythonw()}" "{entry}"'


def _winreg():
    try:
        import winreg       # noqa: PLC0415

        return winreg
    except ImportError:
        return None


def is_enabled() -> bool:
    """当前是否已设置开机自启。非 Windows 或读不到注册表时返回 False。"""
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except FileNotFoundError:
        return False
    except OSError:
        log.debug("读取自启注册表失败", exc_info=True)
        return False


def _current_value() -> str | None:
    winreg = _winreg()
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except (FileNotFoundError, OSError):
        return None


def set_autostart(enabled: bool = True) -> bool:
    """设置或取消开机自启，返回是否成功。

    ⚠️ **幂等且会自愈**：已存在但命令行变了（用户把程序移到了
    别的目录、或从开发态换成了 exe）时会覆盖成新值。
    否则用户移动目录后自启会静默失效 —— 表现是"以前能自启，
    现在不行了"，而他完全不知道是注册表里那条旧路径的问题。
    """
    winreg = _winreg()
    if winreg is None:
        log.warning("非 Windows 平台，不支持注册表开机自启")
        return False

    want = autostart_command()
    if not enabled:
        return disable_autostart()

    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, want)
        log.info("已设置开机自启：%s", want)
        return True
    except OSError:
        log.exception("设置开机自启失败")
        return False


def disable_autostart() -> bool:
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        log.info("已取消开机自启")
        return True
    except FileNotFoundError:
        return True         # 本来就没有，视为成功
    except OSError:
        log.exception("取消开机自启失败")
        return False


def status() -> dict[str, object]:
    """给 `/api/system/info` 与设置页用的状态。"""
    value = _current_value()
    return {
        "supported": _winreg() is not None,
        "enabled": bool(value),
        "command": value,
        "expected": autostart_command(),
        "stale": bool(value) and value != autostart_command(),
    }


def ensure_enabled(want: bool | None = None) -> dict[str, object]:
    """启动时把自启**写成当前该有的样子**，返回最新状态。

    ## 为什么需要"每次启动都写一遍"

    ★ 这条链路上原来缺的正是"谁来写"：`set_autostart()` 存在，但全仓没有
    任何调用点（只有 `status()` 被 `/api/system/info` 用来显示）。结果是
    注册表里从来没有 `PhotoBridge` 这一项 —— 用户在任务管理器里找不到它，
    开机自然也不会启动。

    ★ 顺便自愈：用户把程序从"开发态"换成打包版、或移动了目录之后，
    注册表里那条旧命令会指向不存在的路径。每次启动都按当前形态重写一遍，
    这类"以前能自启、现在不行"的故障就不会再发生。

    `want=None` 时取默认行为（开启）。Windows 之外、或注册表不可读写时
    只返回状态，不报错 —— 自启失败不该阻止程序启动。
    """
    if _winreg() is None:
        return status()
    if want is None:
        want = True
    if want:
        if not is_enabled() or _current_value() != autostart_command():
            set_autostart(True)
    else:
        if is_enabled():
            disable_autostart()
    return status()

