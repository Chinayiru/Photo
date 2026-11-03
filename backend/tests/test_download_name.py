"""下载文件名净化（`backend/app/handlers/common.py:download_filename`）。

## 这些用例为什么存在

`GET /api/items/{id}/raw?download=1&name=<任意字符串>` 里的 `name`
**原样进入** `download_filename()`（`browse.py:677`），再由
`filename_disposition()` 写进 `Content-Disposition`。它不是磁盘上的路径，
所以沙箱管不到它；沙箱对 `rel_path` 的重解析点/bidi 检查**不会**顺带覆盖
这条入参（防御审计 D4-4 / D3-1）。

带 U+202E（RLO）的文件名会让客户端把

    photo<U+202E>gnp.exe   显示成    photoexe.png

—— 用户点了"下载原图"，存到本地却是个可执行文件的名字。所以这里
与 `sandbox.strip_bidi()` 共用同一集合剥掉它。

⚠️ 边界（别扩大成"清除所有零宽字符"）：U+200B–200D 与 U+FEFF 是
中文/阿拉伯文排版的正常组合字符，`sandbox` 有意不禁止，这里也不剥。
"""
from __future__ import annotations

import pytest

from backend.app.handlers.common import download_filename, filename_disposition

#: 真实码点，**不能**写成 `"\\u202e"`（那是 6 个可见字符，不是控制符）。
RLO = chr(0x202E)
LRO = chr(0x202D)
PDF = chr(0x202C)
LRI = chr(0x2066)
PDI = chr(0x2069)
RLM = chr(0x200F)


@pytest.mark.parametrize("bad", [RLO, LRO, PDF, LRI, PDI, RLM])
def test_bidi_control_is_stripped(bad: str) -> None:
    got = download_filename(f"photo{bad}gnp.exe", "fallback.jpg")
    assert bad not in got
    assert got == "photognp.exe"


def test_disposition_header_never_carries_bidi() -> None:
    """头里两个字段（ASCII 兜底与 `filename*`）都必须干净。"""
    name = download_filename(f"photo{RLO}gnp.exe", "fallback.jpg")
    header = filename_disposition(name, attachment=True)
    assert header.startswith("attachment; ")
    assert "%E2%80%AE" not in header.upper()
    assert RLO not in header


def test_zwsp_and_bom_are_left_alone() -> None:
    """U+200B / U+FEFF 属正常组合字符，不剥（避免毁掉真实文件名）。"""
    got = download_filename(f"照\u200b片\ufeff.jpg", "fallback.jpg")
    assert got == "照\u200b片\ufeff.jpg"


def test_name_that_is_all_bidi_falls_back() -> None:
    """全被剥掉后不能返回空名（否则头里是 `filename=""`）。"""
    assert download_filename(RLO + PDF, "fallback.jpg") == "download"


def test_crlf_and_separators_still_stripped() -> None:
    """原有净化不能因为新增 bidi 处理而退化。"""
    got = download_filename("a\r\nb/c\\d:e*f?g\"h<i>j|k.jpg", "x.jpg")
    assert got == "abcdefghijk.jpg"


def test_chinese_name_survives() -> None:
    assert download_filename("青海湖 2026.jpg", "x.jpg") == "青海湖 2026.jpg"


def test_length_is_capped() -> None:
    assert len(download_filename("x" * 500 + ".jpg", "y.jpg")) == 180


def test_fallback_used_when_name_missing() -> None:
    assert download_filename(None, "real.jpg") == "real.jpg"
    assert download_filename("   ", "real.jpg") == "real.jpg"
