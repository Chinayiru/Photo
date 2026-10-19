"""二维码的**主入口**必须是云端（ISSUE-024）。

## 这个测试防的是什么

P1 之前托盘二维码编的是局域网地址（`http://192.168.x.x:8787/?code=…`）。
而 localStorage 与 cookie 都按 **origin** 隔离：

* 在家扫码 → 令牌落在 `http://192.0.2.10:8787` 这个 origin；
* 出门打开 `https://域名/d/<id>/` → 那个 origin 的 localStorage 是空的
  → 手机回到配对页 → 而 6 位码要人站在电脑边上看（人已出门）。

浏览器实测（同一个 profile、同一台"手机"）见 `docs/11` ISSUE-024：
`[2] 出门：用同一个 profile 以云端地址打开浏览器 → keys=[] → 配对页`。

所以二维码必须直接指云端入口。**这条测试对着行为断言，不对着字符串**：
它真的调 `TrayController._new_pair_code()`，看二维码 URL 里编的到底是
哪个 origin —— 因为"能力已实现、调用点没接上"正是这个仓库反复踩的坑
（`test_tray.TestTrayWiring` 的 docstring 就是在说这件事）。

## 为什么不用 monkeypatch 替 `get_tunnel`

`_cloud_entry()` 里 `from .tunnel import get_tunnel` 是函数内导入，patch
模块属性对已导入的引用无效。所以这里直接 `tunnel.set_tunnel(fake)` ——
那就是生产代码唯一读写这个单例的入口，测的路径与真实完全一致。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest

from backend.app import tunnel as tunnel_mod
from backend.app.tray import TrayController

LAN_BASE = "http://192.0.2.10:8787"
CLOUD_BASE = "https://pb.example.com/d/" + "a" * 32


class _FakeTunnel:
    """只实现 `_cloud_entry` 用到的那两个方法。"""

    __slots__ = ("base", "connected")

    def __init__(self, base: str, connected: bool = True) -> None:
        self.base = base
        self.connected = connected

    def public_base(self) -> str:
        return self.base

    def status(self) -> dict[str, Any]:
        return {"connected": self.connected}


class _FakeTransport:
    __slots__ = ("urls",)

    def __init__(self, urls: list[str]) -> None:
        self.urls = urls


def _make(transport_urls: list[str] | None = None) -> TrayController:
    """造一个不碰 pystray / tkinter 的 TrayController。"""
    from backend.app.config import Config

    cfg = Config({})
    tray = TrayController(cfg)
    tray._transport = _FakeTransport(
        transport_urls if transport_urls is not None else [LAN_BASE])
    return tray


@pytest.fixture()
def state(tmp_path: Any):
    """一个真实 AppState（有 devices 表，`_new_pair_code` 要写配对码）。"""
    from backend.app.config import Config
    from backend.app.db import make_db
    from backend.app.server import build_state

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    cfg = Config({"server": {"host": "127.0.0.1", "port": 0, "allow_lan": False},
                  "roots": [], "index": {"scan_on_start": False, "watch": False}},
                 path=data / "config.json", data_dir=data)
    db = make_db(cfg)
    db.init()
    return build_state(cfg, db=db)


@pytest.fixture(autouse=True)
def _clean_tunnel():
    tunnel_mod.set_tunnel(None)
    yield
    tunnel_mod.set_tunnel(None)


class TestCloudEntryIsThePrimaryQrTarget:
    """隧道连着时，二维码必须编云端入口。"""

    def test_qr_points_at_cloud_when_tunnel_is_up(self, state) -> None:
        tunnel_mod.set_tunnel(_FakeTunnel(CLOUD_BASE))
        tray = _make()
        tray._state = state

        got = tray._new_pair_code()
        assert got is not None
        code, url, _ips = got

        u = urlparse(url)
        assert f"{u.scheme}://{u.netloc}" == "https://pb.example.com", (
            f"二维码应指向云端 origin，实际是 {url!r} —— "
            "否则用户出门后打开域名会回到一个空白的配对页（ISSUE-024）"
        )
        assert u.path.startswith("/d/"), f"云端入口必须带 /d/<id> 前缀，实际 {u.path!r}"
        assert parse_qs(u.query)["code"] == [code]
        assert parse_qs(u.query)["did"], "云端入口同样要带 device_id（AC-5.2）"

    def test_qr_falls_back_to_lan_when_tunnel_is_down(self, state) -> None:
        """隧道没连上时必须退回局域网，不能编一个打不开的云端地址。"""
        tunnel_mod.set_tunnel(_FakeTunnel(CLOUD_BASE, connected=False))
        tray = _make()
        tray._state = state

        got = tray._new_pair_code()
        assert got is not None
        _code, url, _ips = got
        assert url.startswith(LAN_BASE), (
            f"隧道未连接时二维码必须退回局域网地址，实际 {url!r}"
        )

    def test_qr_falls_back_when_no_tunnel_singleton(self, state) -> None:
        """一期形态（根本没启用云端）：必须还能正常出码。"""
        tray = _make()
        tray._state = state
        got = tray._new_pair_code()
        assert got is not None
        assert got[1].startswith(LAN_BASE)

    def test_cloud_entry_empty_when_public_base_is_blank(self, state) -> None:
        """`public_base()` 为空串时不能编出 `/?code=…` 这种残缺地址。"""
        tunnel_mod.set_tunnel(_FakeTunnel(""))
        tray = _make()
        assert tray._cloud_entry() == ""

    def test_cloud_entry_survives_status_raising(self, state) -> None:
        """状态端点抛异常时托盘不能崩（托盘任何功能都不该让服务挂掉）。"""

        class _Boom(_FakeTunnel):
            def status(self) -> dict[str, Any]:
                raise RuntimeError("boom")

        tunnel_mod.set_tunnel(_Boom(CLOUD_BASE))
        tray = _make()
        assert tray._cloud_entry() == ""

    def test_cloud_entry_survives_public_base_raising(self, state) -> None:
        class _Boom(_FakeTunnel):
            def public_base(self) -> str:
                raise RuntimeError("boom")

        tunnel_mod.set_tunnel(_Boom(CLOUD_BASE))
        tray = _make()
        assert tray._cloud_entry() == ""


class TestAltUrls:
    """窗口里要列出局域网地址（两个 origin 各自独立配对）。"""

    def test_lan_urls_are_listed_when_qr_is_cloud(self, state) -> None:
        tray = _make()
        alt = tray._alt_urls_for(CLOUD_BASE + "/?code=123456")
        assert alt == [LAN_BASE + "/"], (
            "二维码走云端时，窗口必须明文列出局域网地址，"
            "否则用户想在家直连时无从下手"
        )

    def test_no_alt_when_qr_is_already_lan(self, state) -> None:
        tray = _make()
        assert tray._alt_urls_for(LAN_BASE + "/?code=123456") == []

    def test_alt_urls_are_unique_and_slash_terminated(self, state) -> None:
        tray = _make([LAN_BASE, LAN_BASE + "/", "http://10.0.0.5:8787"])
        alt = tray._alt_urls_for(CLOUD_BASE + "/")
        # 尾斜杠差异不该导致同一个地址出现两次 —— 用户照着打会以为有两个入口
        assert len(alt) == len(set(alt)), alt
        assert all(u.endswith("/") for u in alt), alt


class TestWiringIsActuallyUsed:
    """`_on_show_qr` 真的把 alt 传下去了吗（源码级，防"函数写了没人调"）。"""

    def test_on_show_qr_passes_alt_urls(self) -> None:
        import inspect

        src = inspect.getsource(TrayController._on_show_qr)
        assert "_alt_urls_for(url)" in src, (
            "_on_show_qr 必须把备选地址传给 _show_qr_window，"
            "否则用户在窗口里看不到局域网地址"
        )
        assert "alt_urls" in src or "_alt_urls_for" in src
