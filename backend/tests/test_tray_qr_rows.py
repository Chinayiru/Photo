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
