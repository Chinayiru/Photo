"""二维码窗口里的"入口行"：扫码地址 + 在家直连地址。

窗口能点能复制这件事本身靠人眼验收，但"列哪几条、按什么顺序、标签叫什么"
是纯逻辑，容易在后面的改动里悄悄退化（例如把备选入口去重去掉，窗口里就会
出现两条一模一样的地址）。
"""

from __future__ import annotations

from backend.app.tray import _entry_rows

URL = "https://abc.trycloudflare.com/d/" + "a" * 32 + "/"
LAN = "http://192.0.2.10:8799/"
MESH = "http://26.26.192.40:8799/"


def test_only_primary_when_no_alternatives() -> None:
    assert _entry_rows(URL, None) == [("扫码地址", URL)]
    assert _entry_rows(URL, []) == [("扫码地址", URL)]


def test_alternatives_are_labelled_and_ordered() -> None:
    rows = _entry_rows(URL, [LAN, MESH])
    assert rows[0] == ("扫码地址", URL)
    assert rows[1] == ("在家直连", LAN)
    assert rows[2] == ("其他入口", MESH)


def test_duplicates_and_blanks_are_dropped() -> None:
    rows = _entry_rows(URL, ["", "   ", URL, LAN, LAN])
    assert rows == [("扫码地址", URL), ("在家直连", LAN)]


def test_primary_never_repeated_as_alternative() -> None:
    rows = _entry_rows(URL, [URL])
    assert len(rows) == 1


# --------------------------------------------------------------------------
# 地址框：必须"有内容"且"改不了"
#
# 曾经把 `state="readonly"` 写在构造参数里，之后 `insert()` 被静默丢弃，
# 窗口弹出但每条地址都是空白的（用户原话："你把扫码地址给屏蔽了"）。
# 这条测试盯着那个顺序。
# --------------------------------------------------------------------------

import pytest


@pytest.fixture(scope="module")
def tkroot():
    """整模块共用一个 Tk 根窗口。

    用完 `destroy()` 再新建第二个根，在这台机器上会抛 `TclError`
    （`tkinter._default_root` 仍指着已销毁的解释器），于是第二条测试会被
    静默 skip 掉 —— 那等于没测。共用同一个根最省事，也快。
    """
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except Exception:  # pragma: no cover - 无桌面会话时才走
        pytest.skip("没有可用的图形会话")
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:  # pragma: no cover
        pass


def test_url_entry_contains_the_address(tkroot) -> None:
    from backend.app.tray import _make_url_entry

    assert _make_url_entry(tkroot, URL, primary=True).get() == URL


def test_url_entry_is_readonly_but_visible(tkroot) -> None:
    from backend.app.tray import _make_url_entry

    ent = _make_url_entry(tkroot, LAN, primary=False)
    assert ent.get() == LAN
    assert str(ent.cget("state")) == "readonly"
    # 长地址不能被截断（宽 78 是上限，短地址按长度给）
    long_ent = _make_url_entry(tkroot, "https://x/" + "b" * 60, primary=True)
    assert int(long_ent.cget("width")) >= 60
