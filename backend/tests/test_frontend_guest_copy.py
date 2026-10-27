"""「复制链接」失败 + 链接不可见（ISSUE-065）的回归判据。

## 这一批为什么只能用源码判据

仓库没有 JS 测试运行器（与 `test_frontend_hardening.py` 同一套做法），而这两
个缺陷恰好都是**"本地看不出来、真机才暴露"**的类型：

* 复制：`navigator.clipboard` 在 `http://127.0.0.1` 下**存在**（回环算安全
  上下文），在手机的 `http://192.0.2.10:8799` 下是 `undefined`。所以
  "在我机器上能复制"不能证明任何事，只有"代码里有没有 `execCommand` 兜底"
  能证明。
* 链接：`it.link` 在列表响应里**根本不存在**（服务端只存 `token_hash`），
  页面若无条件渲染它就只能是空 —— 于是断言必须落在"模板里有没有真的把
  文本渲染出来"和"拿不到时是否如实说明"。

真机侧的取证另有一份：`build/_probe_clip_guest.py`（Chrome CDP，LAN IP 下
`isSecureContext=false` / `navigator.clipboard=undefined` 的实测）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"
PANEL = SRC / "components" / "GuestPanel.vue"
CLIP = SRC / "api" / "clipboard.ts"


def _src_files() -> list[Path]:
    out: list[Path] = []
    for pat in ("*.ts", "*.vue"):
        out.extend(SRC.rglob(pat))
    return out


def _code(text: str) -> str:
    """去掉注释后的源码。

    注释里提 `navigator.clipboard` 是**必须允许**的 —— 这个仓库正是靠注释
    记下"为什么不能用它"。判违规的只能是真的调用。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"\s*//[^\n\"]*$", "", text, flags=re.M)


# ------------------------------------------------------------ 唯一实现

def test_clipboard_lives_in_exactly_one_module() -> None:
    """全仓库只能有一处 clipboard 实现（硬约束 3）。

    `GuestPanel.vue` 原来就是自己写了一份 `navigator.clipboard.writeText`，
    于是 `execCommand` 兜底永远轮不到 —— 这就是 bug 的成因。
    """
    offenders = [
        p.relative_to(ROOT).as_posix()
        for p in _src_files()
        if p != CLIP and "navigator.clipboard" in _code(p.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"这些文件绕过了 clipboard.ts：{offenders}"


def test_panel_uses_the_shared_copy_helper() -> None:
    text = PANEL.read_text(encoding="utf-8")
    assert 'from "../api/clipboard"' in text
    assert "await copyText(" in text
    assert "navigator.clipboard" not in _code(text)


# ------------------------------------------------------------ 兜底顺序

def test_copy_text_falls_back_synchronously_without_the_async_api() -> None:
    """`navigator.clipboard` 不存在时必须**不 await** 直接走 `execCommand`。

    原因：`execCommand("copy")` 只有在**用户手势的同步调用栈**里才算数。
    如果先 `await` 一个必然失败的东西，兜底就跑到微任务里去了，安卓上会
    静默失败（`copy()` 返回 false，用户看到的还是"复制失败"）。
    """
    text = CLIP.read_text(encoding="utf-8")
    body = text.split("export async function copyText", 1)[1].split("\n}", 1)[0]
    # 取 clipboard 之后必须**先判空**，再在同一个分支里同步 return 兜底
    assert "clip && typeof clip.writeText" in body
    assert "return legacyCopy(text)" in body
    # 兜底调用不许被 await 包着（await legacyCopy 没有意义，也说明写错了）
    assert "await legacyCopy" not in body
    # execCommand 的调用点存在，且是一次真实调用而不是注释
    assert re.search(r'ok = document\.execCommand\("copy"\)', text)


def test_legacy_copy_keeps_the_textarea_in_layout() -> None:
    """`display:none` / `visibility:hidden` 的 textarea 选不中（安卓）。"""
    text = CLIP.read_text(encoding="utf-8")
    body = _code(text.split("function legacyCopy", 1)[1])
    assert 'ta.style.opacity = "0"' in body
    assert 'display = "none"' not in body
    assert "visibility" not in body


# ------------------------------------------------------------ 失败不静默

def test_failed_copy_tells_the_user_what_to_do() -> None:
    text = PANEL.read_text(encoding="utf-8")
    assert "复制失败" in text
    assert "长按" in text
    # 失败时把链接摊开，用户才有东西可长按
    assert "revealAll()" in text


def test_success_shows_a_prefix_so_the_user_can_verify() -> None:
    """D2：复制成功后 toast 带前 32 字符。"""
    text = PANEL.read_text(encoding="utf-8")
    assert "已复制：" in text
    assert "text.slice(0, 32)" in text


# ------------------------------------------------------------ 链接必须可见

def test_link_is_rendered_as_visible_text() -> None:
    text = PANEL.read_text(encoding="utf-8")
    # 真的把文本插进 DOM（插值或 v-text 都算），而不是只放一个"复制"按钮
    assert re.search(r"\{\{\s*\n?\s*expanded\.has\(created\.invite_id\)", text)
    assert re.search(r"\{\{\s*\n?\s*expanded\.has\(it\.invite_id\)", text)
    assert "maskLink(" in text and "linkOf(" in text


def test_link_style_is_selectable_and_not_hidden() -> None:
    text = PANEL.read_text(encoding="utf-8")
    css = text.split(".link-show {", 1)[1].split("}", 1)[0]
    assert "user-select: text" in css
    assert "-webkit-user-select: text" in css
    assert "display: none" not in css
    assert "opacity: 0" not in css
    # 长链接必须能换行，否则手机上一行放不下、又被裁掉
    assert "break-all" in css or "overflow-wrap" in css


def test_created_link_is_kept_for_the_panel_and_the_browser() -> None:
    """生成响应里的 `link` 不能丢：内存一份（本次会话）+ localStorage 一份。"""
    text = PANEL.read_text(encoding="utf-8")
    assert "created.value = {" in text
    assert "saveLink(r.invite_id, r.link)" in text
    assert "pb.guestLinks" in text


def test_rows_without_a_known_link_say_so_instead_of_copying_undefined() -> None:
    """列表拿不到明文时，如实说明，绝不渲染一个"复制 undefined"的按钮。"""
    text = PANEL.read_text(encoding="utf-8")
    assert 'v-if="linkOf(it)"' in text
    assert "链接只在生成时显示" in text


# ------------------------------------------------------------ 后端事实

def test_list_endpoint_really_cannot_return_the_link() -> None:
    """守住前提：库里只存 `token_hash`，所以列表**不可能**给回明文。

    这条不是"测着玩"：如果哪天有人给 `guest_invites` 加了明文列，上面那套
    "只在生成时可见"的界面就需要重新设计 —— 让测试把这件事喊出来。
    """
    db_py = (ROOT / "backend" / "app" / "db.py").read_text(encoding="utf-8")
    block = db_py.split("CREATE TABLE IF NOT EXISTS guest_invites", 1)[1].split(");", 1)[0]
    assert "token_hash" in block
    assert "token " not in block.replace("token_hash", "")
