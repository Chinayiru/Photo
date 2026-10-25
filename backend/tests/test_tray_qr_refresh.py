"""二维码窗口的「刷新二维码和配对码」按钮。

配对码 5 分钟过期，二维码里编着码；能在窗口里刷一下是基本要求，但这条
路径跨了三个执行上下文（Tk 线程 → 工作线程 → `root.after`），任何一环
写错都表现为"点了没反应"，而窗口本身看起来完全正常。所以这里真的把
窗口开起来、点一下按钮、断言码变了 —— 没有桌面会话时跳过。
"""

from __future__ import annotations

import threading

import pytest

from backend.app import tray

URL = "https://abc.trycloudflare.com/d/" + "a" * 32 + "/"
NEW_URL = "https://xyz.trycloudflare.com/d/" + "a" * 32 + "/"


def _have_display() -> bool:
    try:
        import tkinter as tk

        root = tk.Tk()
    except Exception:  # pragma: no cover - 无桌面会话
        return False
    root.destroy()
    return True


@pytest.mark.skipif(not _have_display(), reason="需要桌面会话才能开窗口")
def test_refresh_button_replaces_code_and_status() -> None:
    done = threading.Event()
    seen: dict[str, str] = {}

    def on_ready(ui) -> None:
        def step_check() -> None:
            seen["code"] = str(ui["code_label"].cget("text"))
            seen["status"] = str(ui["status"].cget("text"))
            seen["state_code"] = str(ui["state"]["code"])
            try:
                ui["root"].destroy()
            finally:
                done.set()

        def step_click() -> None:
            ui["refresh_btn"].invoke()
            # 刷新走工作线程，给它足够时间回来
            ui["root"].after(1500, step_check)

        ui["root"].after(200, step_click)

    tray._show_qr_window(
        "PhotoBridge 刷新测试", URL, "111111", ["192.0.2.10"], None,
        refetch=lambda: ("222222", NEW_URL, ["192.0.2.10"], []),
        on_ready=on_ready,
    )
    assert done.wait(20), "窗口没有在 20 秒内完成刷新（可能是点击没接上）"
    assert seen["code"] == "222222"
    assert seen["state_code"] == "222222", "复制按钮用的还是旧码"
    assert seen["status"].startswith("已刷新")


@pytest.mark.skipif(not _have_display(), reason="需要桌面会话才能开窗口")
def test_refresh_failure_is_reported_not_silent() -> None:
    done = threading.Event()
    seen: dict[str, str] = {}

    def on_ready(ui) -> None:
        def step_check() -> None:
            seen["status"] = str(ui["status"].cget("text"))
            seen["code"] = str(ui["code_label"].cget("text"))
            try:
                ui["root"].destroy()
            finally:
                done.set()

        def step_click() -> None:
            ui["refresh_btn"].invoke()
            ui["root"].after(1500, step_check)

        ui["root"].after(200, step_click)

    tray._show_qr_window(
        "PhotoBridge 刷新失败测试", URL, "333333", ["192.0.2.10"], None,
        refetch=lambda: None,          # 模拟"生成失败"
        on_ready=on_ready,
    )
    assert done.wait(20), "窗口没有在 20 秒内响应"
    assert seen["code"] == "333333", "失败时不该把旧码抹掉"
    assert "失败" in seen["status"]
