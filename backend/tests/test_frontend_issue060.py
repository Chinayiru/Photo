"""ISSUE-060 前端行为：两种浏览方式 / 上传多选上限 / 从搜索结果下钻文件夹。

## 为什么用"读源码"来测前端

这个仓库没有 JS 测试运行器（见 `test_frontend_hardening.py` 的说明），
而这三件事都是**契约**级别的约束，用字符串判据足够把它们钉住：

- 目录模式下**只能**打 `/api/folders/media` —— 一旦有人图省事复用
  `/api/items`，目录模式就会变成"按目录过滤但拿全库分页"，用户看到的是
  空目录旁边躺着别的文件夹的照片。
- 搜索页的"只看这个文件夹"必须走**事件**通知 App 切 Tab，不能自己
  `location.href` 或反向 import App 的 ref（会成环）。
- 上传队列上限只允许有一个数字，`200`；散落在两处的常量迟早不一致。

字符串判据的弱点是"改个写法就失效"，所以每条断言都钉的是**行为锚点**
（接口路径、事件名、常量名、可见文案），不是格式化细节。
"""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _read(rel: str) -> str:
    return (SRC / rel).read_text(encoding="utf-8")


# ------------------------------------------------------------------ A 两种浏览

def test_timeline_store_has_two_modes_and_switches_cleanly() -> None:
    src = _read("stores/timeline.ts")
    assert re.search(r'export type BrowseMode = "all" \| "folder"', src)
    assert "export function setMode(" in src
    # 换模式必须清空当前列表并立刻重取（reload），不能"就地换接口"
    # —— 否则会拿着旧模式的光标去问新模式，页码语义完全不同
    body = src[src.index("export function setMode("):]
    body = body[: body.index("\n}\n")]
    assert 'changeFolder("", { reload: true })' in body


def test_folder_mode_uses_the_folder_endpoint() -> None:
    src = _read("stores/timeline.ts")
    assert 'api.get<TimelineResponse>("/api/folders/media"' in src
    # 全库时间线走 /api/timeline，两个入口不能混成一个
    assert 'api.get<TimelineResponse>("/api/timeline"' in src


def test_open_folder_and_crumbs_are_exported() -> None:
    src = _read("stores/timeline.ts")
    assert "export function openFolder(" in src
    assert "export const crumbs" in src
    # 根段必须存在（label 叫"全部"），否则进了深层目录就回不到顶层
    body = src[src.index("export const crumbs"):]
    body = body[: body.index("});")]
    assert 'label: "全部"' in body


def test_timeline_view_renders_the_mode_switch() -> None:
    view = _read("views/TimelineView.vue")
    assert ">全部图片</button>" in view
    assert ">按文件夹</button>" in view


# ------------------------------------------------------------------ C 搜索结果下钻

def test_search_results_can_jump_into_a_folder() -> None:
    search = _read("views/SearchView.vue")
    assert 'new CustomEvent("pb:goto-folder"' in search
    assert "dirStats" in search


def test_app_owns_the_tab_switch_for_goto_folder() -> None:
    app = _read("App.vue")
    assert 'window.addEventListener("pb:goto-folder"' in app
    assert 'window.removeEventListener("pb:goto-folder"' in app
    body = app[app.index("function onGotoFolder("):]
    body = body[: body.index("\n}\n")]
    # 切 Tab + 下钻，一样都不能少
    assert "openFolder(dir)" in body
    assert 'tab.value = "timeline"' in body
    # 空目录串要当没发生：否则用户会被踢回根层
    assert 'dir === ""' in body


# ------------------------------------------------------------------ B 上传上限

def test_upload_queue_cap_is_two_hundred_and_explained() -> None:
    src = _read("stores/upload.ts")
    assert "export const MAX_QUEUE = 200;" in src
    assert "MAX_QUEUE" in src[src.index("MAX_QUEUE = 200;") + 1:], "上限没被用上"
    # 上限拒绝必须有可说的一句话（用户不知道"为什么没加进去"就会反复点）
    assert re.search(r"MAX_QUEUE[^\n]*\n[^\n]*(提示|最多|上限)", src) or "超过" in src


def test_upload_view_accepts_multiple_files() -> None:
    view = _read("views/UploadView.vue")
    assert "multiple" in view
