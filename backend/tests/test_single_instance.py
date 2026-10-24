"""单实例守卫的回归测试（ISSUE-043）。

## 为什么需要单实例

托盘程序没有主窗口。用户第二次双击图标时看不到任何反馈，于是很容易
连点几次 —— 每次都可能起一个**新的后台进程**（只要端口恰好没冲突），
各自扫索引、各自跑缩略图、各自持有 SQLite 写锁。用户那边的现象是
"点了退出，托盘图标没了，任务管理器里还躺着一堆 python"。

判定方式：锁文件记端口 + `GET /api/system/ping` 探活。这里把探活替换成
假响应，避免测试真的去开端口。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app import single_instance


class _Cfg:
    """`single_instance` 只用到 `data_dir` 与 `server["port"]`。"""

    def __init__(self, data_dir: Path, port: int = 8787) -> None:
        self.data_dir = data_dir
        self.server = {"port": port}


@pytest.fixture()
def cfg(tmp_path: Path) -> _Cfg:
    return _Cfg(tmp_path)


def _fake_probe(body: dict[str, object] | None):
    def probe(port: int, timeout: float = 0.0) -> dict[str, object] | None:
        return body

    return probe


def test_acquire_writes_lock(cfg: _Cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(single_instance, "_probe", _fake_probe(None))

    got, running = single_instance.acquire(cfg)

    assert got is True
    assert running is None
    lock = single_instance.lock_path(cfg)
    assert lock.is_file()
    payload = json.loads(lock.read_text(encoding="utf-8"))
    assert payload["port"] == 8787
    assert isinstance(payload["pid"], int)


def test_second_instance_is_rejected(cfg: _Cfg,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """已有实例答得上 `/api/system/ping` 时，第二个进程必须停手。

    注意断言里的端口是**配置里的 8787**，不是锁文件里的 9000 —— 锁文件可能
    是上一次运行留下的过期数据，拿它当端口去探活会误判（见 `find_running`
    的注释：一个过期锁曾经把启动拦死）。
    """
    single_instance.lock_path(cfg).write_text(
        json.dumps({"pid": 4321, "port": 9000}), encoding="utf-8")
    monkeypatch.setattr(
        single_instance, "_probe",
        _fake_probe({"pong": True, "device_id": "abc"}))

    got, running = single_instance.acquire(cfg)

    assert got is False
    assert running is not None
    assert running["port"] == 8787
    assert running["url"] == "http://127.0.0.1:8787"
    assert running["pid"] == 4321
    assert running["device_id"] == "abc"


def test_stale_lock_is_overwritten(cfg: _Cfg,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """断电/强杀留下的过期锁不能把用户永远挡在门外。"""
    single_instance.lock_path(cfg).write_text(
        json.dumps({"pid": 999999, "port": 8787}), encoding="utf-8")
    monkeypatch.setattr(single_instance, "_probe", _fake_probe(None))

    got, running = single_instance.acquire(cfg)

    assert got is True
    assert running is None
    payload = json.loads(single_instance.lock_path(cfg).read_text(encoding="utf-8"))
    assert payload["port"] == 8787


def test_malformed_lock_is_ignored(cfg: _Cfg) -> None:
    single_instance.lock_path(cfg).write_text("{not json", encoding="utf-8")

    assert single_instance.read_lock(cfg) is None


def test_stale_lock_port_does_not_hijack_startup(
        cfg: _Cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    """过期锁里记着老端口时，判定仍以**配置端口**为准。

    真实事故：锁文件留着 `port=8799`（上一次测试实例），而 8799 上恰好还有
    另一个 PhotoBridge 在跑 → 新进程探活成功、判定"已在运行"、安静退出，
    用户看到的是"双击没反应"。所以探活必须打自己要绑的那个端口。
    """
    single_instance.lock_path(cfg).write_text(
        json.dumps({"pid": 1, "port": 8799}), encoding="utf-8")
    seen: list[int] = []

    def probe(port: int, timeout: float = 0.0):
        seen.append(port)
        return {"pong": True}

    monkeypatch.setattr(single_instance, "_probe", probe)

    info = single_instance.find_running(cfg)

    assert seen == [8787]
    assert info is not None and info["port"] == 8787


def test_unhealthy_response_is_not_running(cfg: _Cfg,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """端口上有人答 200，但不是 PhotoBridge（没有 `pong`/`ok`）→ 当没在跑。

    典型场景：8787 被别的软件占着。此时不该阻止启动 —— 后面绑定端口会失败
    并给出"端口被占"的明确错误，那比"以为程序已经在跑"清楚得多。
    """
    single_instance.lock_path(cfg).write_text(
        json.dumps({"pid": 7, "port": 8787}), encoding="utf-8")
    monkeypatch.setattr(single_instance, "_probe",
                        _fake_probe({"something": "else"}))

    assert single_instance.find_running(cfg) is None


def test_release_removes_lock(cfg: _Cfg, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(single_instance, "_probe", _fake_probe(None))
    single_instance.acquire(cfg)

    single_instance.release(cfg)

    assert not single_instance.lock_path(cfg).exists()
    # 再释放一次也不能炸（退出路径会走两遍）
    single_instance.release(cfg)


def test_unwritable_data_dir_does_not_block_start(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """数据目录不可写（例如程序被装进 Program Files）时宁可允许启动。"""
    blocked = _Cfg(tmp_path / "nope")
    monkeypatch.setattr(single_instance, "_probe", _fake_probe(None))
    monkeypatch.setattr(Path, "write_text",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))

    got, running = single_instance.acquire(blocked)

    assert got is True
    assert running is None
