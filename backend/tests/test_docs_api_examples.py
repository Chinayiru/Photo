"""文档里的 JSON 示例必须与实现返回的字段一致（F19 漂移的防线）。

## 为什么需要这条测试

2026-10-20 的审计 F19 指出上传响应字段与文档漂移。字段表改正了，
但**示例 JSON 被漏掉了** —— `docs/04` §4.3 的四个示例里还写着
`"total": 4194304` 和 `"percent": 0.5`，而实现返回的是 `total_size`
且**从来没有 `percent` 这个字段**。

这类漂移特别难靠人眼发现，因为它藏在代码块里：字段表在上一屏写着
「没有 percent」，示例在下一屏写着 `"percent": 0.5`，读的人会照着示例写代码。

## 这条测试怎么工作

不比对"所有文档"，只比对**能机械化比对的部分**：

  · 按小节（`#### 4.3.1` / `#### 4.3.2` / `#### 4.3.3`）切出每个接口的正文；
  · 每个小节里抽 ```json 代码块，取顶层键；
  · 与实现里同名 handler 的返回键比对。

★ 一开始按 `upload_id` 前缀在全文里捞示例 —— 那是错的：隧道帧的示例
（`{"method":"PATCH","path":"/api/uploads/1d6e8f0a..."}`）里也含同一个 id，
于是它被当成"上传响应示例"，报出一堆 `method`/`headers`/`body_len` 漂移。
**必须先按小节划范围，再按块取键。**

## 两个刻意收窄的地方

  · `init_upload` / `patch_upload` 返回的是变量（`payload` / `result`），
    AST 抓不到字面量。对它们只做负向断言（文档里不许出现已知不存在的
    字段），不去猜实现 —— 猜错了测试会自己变成噪声源。
  · 只比顶层键。嵌套对象（`root`、`progress`）的形状差异属于设计表述，
    不是接口契约，钉住它只会让每次正常重构都变红。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "04-接口设计文档.md"
HANDLER = REPO / "backend" / "app" / "handlers" / "uploads.py"

#: 曾经漂移过、实现里根本不存在的字段。
BANNED_FIELDS = {"percent"}

#: 小节 → handler 名。只列**返回字面量字典**的 handler。
SECTIONS: dict[str, str] = {
    "#### 4.3.3": "get_upload",
}


def _section(doc: str, heading: str) -> str:
    """切出 `heading` 到下一个同级或更高级标题之间的正文。

    ★ 必须跳过代码围栏内的内容：`docs/04` 的 bash 示例里有
    `# 分配 upload_id` 这类注释，行首也是 `#`。第一版按 `\\n#` 找标题，
    结果 §4.3.2 被切在代码块中间、长度变成 0 —— 后续断言于是全部
    "通过"（空集合的差集是空集）。**空正文必须当成失败，不能当成干净。**
    """
    lines = doc.split("\n")
    start = None
    for i, ln in enumerate(lines):
        if ln.startswith(heading):
            start = i
            break
    assert start is not None, "文档里找不到小节 %s" % heading
    level = len(heading) - len(heading.lstrip("#"))

    in_fence = False
    for i in range(start + 1, len(lines)):
        ln = lines[i]
        stripped = ln.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if ln.startswith("#"):
            n = len(ln) - len(ln.lstrip("#"))
            if n <= level:
                return "\n".join(lines[start + 1 : i])
    return "\n".join(lines[start + 1 :])


def _json_blocks(text: str) -> list[str]:
    """抽出所有 ```json 代码块的内容（不含围栏行）。"""
    blocks: list[str] = []
    cur: list[str] | None = None
    for line in text.split("\n"):
        stripped = line.strip()
        if cur is None:
            if stripped == "```json":
                cur = []
        elif stripped.startswith("```"):
            blocks.append("\n".join(cur))
            cur = None
        else:
            cur.append(line)
    return blocks


def _top_level_keys(block: str) -> set[str]:
    """只取第一层键名。

    不做完整 JSON 解析：示例里有注释（如 `// 手机上传`）会让严格解析失败，
    而我们要的只是"顶层出现了哪些字段名"。
    """
    keys: set[str] = set()
    depth = 0
    in_str = False
    esc = False
    pending: list[str] = []
    for i, ch in enumerate(block):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                if depth == 1 and pending and block[i + 1 :].lstrip().startswith(":"):
                    keys.add("".join(pending))
                pending = []
            else:
                pending.append(ch)
        else:
            if ch == '"':
                in_str = True
                pending = []
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
    return keys


def _impl_keys(fn_name: str) -> set[str]:
    """从 handler 里 AST 抽 `json_reply({...})` 或 `payload = {...}` 的字面量键。"""
    tree = ast.parse(HANDLER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == fn_name:
            found: set[str] = set()
            for sub in ast.walk(node):
                if (
                    isinstance(sub, ast.Call)
                    and isinstance(sub.func, ast.Name)
                    and sub.func.id == "json_reply"
                    and sub.args
                    and isinstance(sub.args[0], ast.Dict)
                ):
                    found |= {
                        k.value
                        for k in sub.args[0].keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
            if found:
                return found
    return set()


# ---------------------------------------------------------------------------
# 正向：文档示例的字段必须是实现真的返回的
# ---------------------------------------------------------------------------


def test_section_of_get_upload_matches_impl() -> None:
    """§4.3.3 的响应示例，顶层键必须在 `get_upload` 的返回字典里。"""
    impl = _impl_keys("get_upload")
    assert impl, "AST 没抽到 get_upload 的返回键，抽取器坏了？"

    doc = DOC.read_text(encoding="utf-8")
    body = _section(doc, "#### 4.3.3")
    blocks = _json_blocks(body)
    assert blocks, "§4.3.3 里没有 json 示例了，文档结构变了？"

    extra: list[str] = []
    for b in blocks:
        for k in sorted(_top_level_keys(b) - impl):
            extra.append("%s（示例：%s）" % (k, b.strip().split("\n")[0][:48]))
    assert not extra, "§4.3.3 示例用了实现没返回的字段：\n  " + "\n  ".join(extra)


def test_impl_keys_look_right() -> None:
    """抽取器本身的健全性检查 —— 别让它在实现改名后静默返回空集。"""
    impl = _impl_keys("get_upload")
    assert {"upload_id", "offset", "received", "total_size", "status"} <= impl
    assert "total" not in impl, "实现里出现了 total? 那文档的 total_size 才是错的"
    assert "percent" not in impl


# ---------------------------------------------------------------------------
# 负向：曾经漂移过的字段不许回来
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("banned", sorted(BANNED_FIELDS))
def test_banned_field_never_appears_in_docs(banned: str) -> None:
    """`percent` 在实现里根本不存在，文档里任何地方都不许出现。

    2026-10-20 的 F19 修复把字段表改对了，却漏掉了四个示例 ——
    字段表说"没有"，同一小节的示例写着 `"percent": 0.5`。
    """
    doc = DOC.read_text(encoding="utf-8")
    hits = [
        (i + 1, ln.strip())
        for i, ln in enumerate(doc.split("\n"))
        if '"%s"' % banned in ln
    ]
    assert not hits, "docs/04 里仍有不存在的字段 %r：\n%s" % (
        banned,
        "\n".join("  %d: %s" % h for h in hits),
    )


def test_upload_examples_use_total_size_not_total() -> None:
    """上传响应示例必须写 `total_size`。

    `total` 是索引进度（`index.progress`）与媒体统计的字段，不是上传的。
    在 §4.3.2 / §4.3.3 里出现 `total` 就是把两个接口混了。
    """
    doc = DOC.read_text(encoding="utf-8")
    for heading in ("#### 4.3.2", "#### 4.3.3"):
        body = _section(doc, heading)
        for b in _json_blocks(body):
            keys = _top_level_keys(b)
            # 隧道帧示例与请求示例不带 upload_id 之外的响应字段，跳过
            if "upload_id" not in keys:
                continue
            assert "total" not in keys, "%s 示例里出现了 total：\n%s" % (
                heading,
                b.strip()[:300],
            )
            assert "total_size" in keys, "%s 示例缺 total_size：\n%s" % (
                heading,
                b.strip()[:300],
            )


def test_patch_example_shows_the_renewed_expires_at() -> None:
    """§4.3.2 的响应示例必须体现续期字段（F18 的可见效果）。"""
    doc = DOC.read_text(encoding="utf-8")
    body = _section(doc, "#### 4.3.2")
    assert body.strip(), "§4.3.2 切出来是空的 —— 切分器坏了"
    joined = "\n".join(_json_blocks(body))
    assert joined.strip(), "§4.3.2 里没有 json 示例"
    assert "expires_at" in joined, "§4.3.2 的示例没有 expires_at"
    assert "chunk_size" in joined, "§4.3.2 的示例没有 chunk_size"


def test_section_slicing_is_not_trivially_empty() -> None:
    """切分器的健全性：每个小节都要有实际内容。

    第一版按 `\\n#` 找下一个标题，被代码块里的 `# 注释` 骗到，
    §4.3.2 被切成 0 长度 —— 而「没有 json 示例」这类断言在空集合上是
    **恒真**的。这条用例专门盯住那个形态。
    """
    doc = DOC.read_text(encoding="utf-8")
    for heading, min_len in (
        ("#### 4.3.0", 200),
        ("#### 4.3.1", 500),
        ("#### 4.3.2", 2000),
        ("#### 4.3.3", 2000),
        ("#### 4.3.4", 500),
    ):
        body = _section(doc, heading)
        assert len(body) >= min_len, "%s 只切出 %d 字符（至少应有 %d）" % (
            heading,
            len(body),
            min_len,
        )
