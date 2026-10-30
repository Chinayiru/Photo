"""配对窗口（二维码窗口）**不得置顶**，且关窗必须显式取消置顶。

## 为什么单独钉这一条

`_show_qr_window` 早先在创建时 `root.attributes("-topmost", True)`，而且
**全生命周期没有任何一处把它设回 False**：窗口一弹出就压在所有程序之上，
切到浏览器、文件管理器照样挡着，只能手动关掉。这不是功能错，是窗口行为错，
所以用「源码结构 + 真开窗口读属性」两条一起钉住：

1. 源码里不许再出现 `attributes("-topmost", True)`（注释里提到不算）；
2. 真开一个窗口，`root.attributes("-topmost")` 必须是 0；
3. 关窗出口（`_close`）必须先 `False` 再 `destroy`，且 × / Alt+F4 也走它。

第 2/3 条需要桌面会话，没有就跳过（CI 里通常没有）。
"""

from __future__ import annotations

import inspect
import threading

import pytest

from backend.app import tray

URL = "https://abc.trycloudflare.com/d/" + "a" * 32 + "/"


# --------------------------------------------------------------------------
# 源码结构（任何环境都跑）
# --------------------------------------------------------------------------

def _code_lines(src: str) -> list[str]:
    """去掉注释行，只留真正会执行的代码。"""
    out = []
    for line in src.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(stripped)
    return out


def test_source_never_sets_topmost_true() -> None:
    src = inspect.getsource(tray._show_qr_window)
    bad = [ln for ln in _code_lines(src)
           if "topmost" in ln and '"-topmost", True' in ln.replace("'", '"')]
    assert not bad, f"二维码窗口不许再置顶，发现：{bad}"


def test_close_clears_topmost_before_destroy() -> None:
    """关窗出口必须「先 False 再 destroy」。"""
    src = inspect.getsource(tray._show_qr_window)
    i_false = src.find('"-topmost", False')
    assert i_false > 0, "关窗路径里找不到显式取消置顶的代码"
    i_destroy_after = src.find("root.destroy()", i_false)
    assert i_destroy_after > i_false, "取消置顶必须发生在 destroy 之前"


def test_all_close_paths_go_through_close() -> None:
    """「关闭」按钮、右上角 ×、异常兜底都要经过同一个出口。"""
    src = inspect.getsource(tray._show_qr_window)
    assert 'command=_close' in src, "「关闭」按钮必须调用 _close()"
    assert 'protocol("WM_DELETE_WINDOW", _close)' in src, "× / Alt+F4 也必须是 _close()"
    assert 'locals().get("_close")' in src, "finally 兜底也要走 _close()"


# --------------------------------------------------------------------------
# 真开窗口（需要桌面会话）
# --------------------------------------------------------------------------

def _have_display() -> bool:
    try:
        import tkinter as tk

        root = tk.Tk()
    except Exception:  # pragma: no cover - 无桌面会话
        return False
    root.destroy()
    return True


@pytest.mark.skipif(not _have_display(), reason="需要桌面会话才能开窗口")
def test_window_is_not_topmost_and_closes_cleanly() -> None:
    done = threading.Event()
    seen: dict[str, object] = {}

    def on_ready(ui) -> None:
        def step() -> None:
            root = ui["root"]
            # 刚打开：必须是普通窗口（不是置顶）
            seen["topmost_open"] = int(root.attributes("-topmost"))
            # 人为把它设成置顶，再走关窗出口，验证出口会把它清掉
            root.attributes("-topmost", True)
            seen["topmost_forced"] = int(root.attributes("-topmost"))
            ui["_close"]()
            try:
                root.winfo_exists()
                seen["destroyed"] = False
            except Exception:
                seen["destroyed"] = True
            done.set()

        ui["root"].after(200, step)

    tray._show_qr_window("PhotoBridge 置顶测试", URL, "111111",
                         ["192.0.2.10"], None, on_ready=on_ready)
    assert done.wait(20), "窗口没有在 20 秒内完成打开/关闭"
    assert seen["topmost_open"] == 0, f"窗口打开时仍是置顶：{seen}"
    assert seen["topmost_forced"] == 1, "这条测试的前提没成立（没能人为置顶）"
    assert seen["destroyed"] is True, f"关窗没有真正销毁：{seen}"
