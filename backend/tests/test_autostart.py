"""开机自启的回归测试（ISSUE-042）。

## 为什么这些用例值得存在

这条链路上真正的故障不是"写错了命令"，而是**没有任何人写**：
`set_autostart()` 存在了几个月，全仓却没有一个调用点，于是注册表里
从来没有 `PhotoBridge` 这一项 —— 用户开机后程序不在，而代码看起来"功能齐全"。

第二个故障更隐蔽：`-m backend.app.main` 依赖工作目录，而注册表 `Run` 键
拉起进程时工作目录是 `C:\\Windows\\System32`。注册表里那句话看着完全正常，
实际启动必失败（找不到 `backend` 包），且**没有任何报错会送到用户眼前**。

所以这里断言的是"命令与工作目录无关"和"启动时会自愈"。
"""
from __future__ import annotations

import pytest

from backend.app import autostart


class _Key:
    """最小 winreg 键对象：读写都落在一个 dict 上。"""

    def __init__(self, store: dict[str, str]) -> None:
        self.store = store

    def __enter__(self) -> "_Key":
        return self

    def __exit__(self, *_: object) -> bool:
        return False


class _FakeWinreg:
    """只实现 `autostart` 用到的那几个函数。"""

    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    # 读路径
    def OpenKey(self, _root: str, _path: str, _reserved: int = 0,
                _access: int = 0) -> _Key:
        return _Key(self.store)

    # 写路径
    def CreateKeyEx(self, _root: str, _path: str, _reserved: int = 0,
                    _access: int = 0) -> _Key:
        return _Key(self.store)

    def QueryValueEx(self, key: _Key, name: str) -> tuple[str, int]:
        if name not in key.store:
            raise FileNotFoundError(name)
        return key.store[name], self.REG_SZ

    def SetValueEx(self, key: _Key, name: str, _reserved: int, _type: int,
                   value: str) -> None:
        key.store[name] = value

    def DeleteValue(self, key: _Key, name: str) -> None:
        if name not in key.store:
            raise FileNotFoundError(name)
        del key.store[name]


@pytest.fixture()
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> _FakeWinreg:
    fake = _FakeWinreg()
    monkeypatch.setattr(autostart, "_winreg", lambda: fake)
    return fake


def test_command_is_absolute_and_cwd_independent() -> None:
    """注册表里那条命令不能依赖工作目录。

    `Run` 键没有"起始位置"，Windows 从 `C:\\Windows\\System32` 拉起它。
    所以既不能出现 `-m backend.app.main`（要靠当前目录进 `sys.path`），
    也不能出现相对路径 —— 两种都会"命令执行了、进程立刻退出"。
    """
    cmd = autostart.autostart_command()

    assert "-m backend.app.main" not in cmd
    assert "run_tray.py" in cmd
    # 入口脚本必须是绝对路径（含盘符或 UNC 前缀）
    assert '":\\' in cmd or cmd.count('"') >= 4
    assert cmd.strip().endswith('"')


def test_enable_writes_value(fake_registry: _FakeWinreg) -> None:
    assert autostart.is_enabled() is False

    assert autostart.set_autostart(True) is True
    assert fake_registry.store[autostart.VALUE_NAME] == autostart.autostart_command()
    assert autostart.is_enabled() is True


def test_disable_removes_value(fake_registry: _FakeWinreg) -> None:
    autostart.set_autostart(True)

    assert autostart.set_autostart(False) is True
    assert autostart.VALUE_NAME not in fake_registry.store
    assert autostart.is_enabled() is False


def test_disable_is_idempotent(fake_registry: _FakeWinreg) -> None:
    """本来就没有这一项时，取消自启也算成功（用户点两次不该报错）。"""
    assert autostart.set_autostart(False) is True


def test_status_marks_stale_command(fake_registry: _FakeWinreg) -> None:
    """旧形态（`-m` 版）留在注册表里时，状态必须报 `stale`。

    这一项是自愈的判据：`ensure_enabled()` 看到 stale 就重写，
    否则"用户换了目录/换了打包方式"之后自启会永远静默失效。
    """
    fake_registry.store[autostart.VALUE_NAME] = '"C:\\py\\pythonw.exe" -m backend.app.main --tray'

    st = autostart.status()

    assert st["enabled"] is True
    assert st["stale"] is True
    assert st["expected"] == autostart.autostart_command()


def test_ensure_enabled_heals_stale_value(fake_registry: _FakeWinreg) -> None:
    fake_registry.store[autostart.VALUE_NAME] = '"C:\\py\\pythonw.exe" -m backend.app.main --tray'

    st = autostart.ensure_enabled(True)

    assert st["enabled"] is True
    assert st["stale"] is False
    assert fake_registry.store[autostart.VALUE_NAME] == autostart.autostart_command()


def test_ensure_enabled_writes_when_missing(fake_registry: _FakeWinreg) -> None:
    """★ 这条是整个 ISSUE-042 的核心：启动时发现没有就必须写进去。"""
    assert autostart.is_enabled() is False

    st = autostart.ensure_enabled(True)

    assert st["enabled"] is True
    assert autostart.VALUE_NAME in fake_registry.store


def test_ensure_enabled_false_removes(fake_registry: _FakeWinreg) -> None:
    autostart.set_autostart(True)

    st = autostart.ensure_enabled(False)

    assert st["enabled"] is False
    assert autostart.VALUE_NAME not in fake_registry.store


def test_ensure_enabled_is_cheap_when_already_correct(
        fake_registry: _FakeWinreg, monkeypatch: pytest.MonkeyPatch) -> None:
    """已经写对时不该再写一遍（避免每次启动都动注册表）。"""
    autostart.set_autostart(True)

    calls: list[bool] = []
    monkeypatch.setattr(autostart, "set_autostart",
                        lambda enabled=True: calls.append(enabled) or True)

    autostart.ensure_enabled(True)

    assert calls == []


def test_no_registry_support_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 Windows（`winreg` 导入失败）时状态要如实报"不支持"，且不抛异常。"""
    monkeypatch.setattr(autostart, "_winreg", lambda: None)

    assert autostart.is_enabled() is False
    assert autostart.set_autostart(True) is False
    st = autostart.ensure_enabled(True)
    assert st["supported"] is False
    assert st["enabled"] is False
