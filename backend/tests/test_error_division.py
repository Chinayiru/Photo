"""错误处理的分工（架构审查 F6）。

判据两条，分别钉住两端：

1. **后端**：错误体只含"机器读的"与"人读的"两类信息 —— `code` /
   `message`（英文标识符）/ `detail`（诊断）/ `retryable` / `request_id` /
   `user_hint`（简体中文）。不出现为某一块界面定制的字段（`friendly`、
   `toast`、`title`、`html`…）。后端不知道该弹 toast 还是红条。
2. **前端**：错误码 → 用户文案的表**只有一处**（`api/errorText.ts` 的
   `BY_CODE`），所有展示点都走 `errorText()`；`ApiError` 不再有
   `friendly` 这种"回落到 `message`/`code`"的快捷方式 —— 它正是
   2026-10 真机上把 `token_resource_mismatch` 甩到用户眼前的原因。

为什么值得两条测试而不是一条：F6 之前的状态是"表存在，但 7 个调用点
绕过它"。只测"表存在"会让这种绕过在全绿的情况下继续存在。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.app import errors

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"

#: 允许直接读 `userHint` 的位置：错误类型定义与文案收口本身。
ALLOWED_HINT_READERS = {"api/client.ts", "api/errorText.ts"}

#: 错误体里允许出现的键。加字段必须同时改这里、docs/04 §1.6 与前端
#: `ApiErrorBody` —— 三处对齐才算数。
ENVELOPE_KEYS = {"code", "message", "detail", "retryable", "request_id", "user_hint"}

#: 一个都不许出现：这些名字意味着后端开始替前端决定"怎么展示"。
UI_SHAPED = {"friendly", "toast", "toast_text", "title", "heading", "display",
             "html", "render", "icon", "color", "severity"}

#: 展示层必须走 `errorText()` 的调用点（数量是下限，防止被悄悄删掉）。
MUST_USE_ERROR_TEXT = {
    "views/ViewerView.vue": 4,
    "views/PairView.vue": 4,
    "stores/auth.ts": 1,
    "stores/system.ts": 1,
    "stores/viewer.ts": 1,
}


def _sources() -> list[Path]:
    out: list[Path] = []
    for pat in ("**/*.ts", "**/*.vue"):
        out.extend(p for p in SRC.glob(pat) if p.is_file())
    return out


def _code(path: Path) -> str:
    """去掉注释，只留会被执行的代码（注释里提到 `.friendly` 不算违规）。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text


def test_error_envelope_field_set_is_fixed() -> None:
    body = errors.AppError(errors.E1004).to_response("req-1")
    assert set(body) == {"error"}
    assert set(body["error"]) == ENVELOPE_KEYS, (
        "错误体字段变了：加/删字段要同时改 docs/04 §1.6、"
        "backend/app/errors.py:to_response() 与前端 ApiErrorBody"
    )
    assert not (set(body["error"]) & UI_SHAPED)


@pytest.mark.parametrize("code", sorted(errors.SPECS))
def test_message_is_machine_readable_and_hint_is_for_humans(code: str) -> None:
    """`message` 是给日志与断言的英文标识符，`user_hint` 是给人看的中文。

    两者一旦混用，前端就只能二选一：要么显示英文标识符（真机事故），
    要么把中文塞进 `message`（日志与断言开始依赖文案，改一个字就红）。
    """
    spec = errors.SPECS[code]
    assert re.fullmatch(r"[a-z0-9_]+", spec.message), (
        f"{code}.message 必须是英文小写标识符，实际是 {spec.message!r}"
    )
    assert re.search(r"[\u4e00-\u9fa5]", spec.user_hint), (
        f"{code}.user_hint 必须是中文，实际是 {spec.user_hint!r}"
    )
    assert not re.search(r"E\d{4}", spec.user_hint), (
        f"{code}.user_hint 里不该出现错误码编号：{spec.user_hint!r}"
    )
    assert spec.user_hint != spec.message


def test_frontend_has_exactly_one_code_to_text_table() -> None:
    holders = [
        p.relative_to(SRC).as_posix()
        for p in _sources() if "BY_CODE" in p.read_text(encoding="utf-8")
    ]
    assert holders == ["api/errorText.ts"], (
        f"错误码映射表只能有一处，实际出现在：{holders}"
    )

    # 除了那张表，任何文件都不许再写 `E1004: "..."` 这种键值对
    pat = re.compile(r'^\s*E\d{4}\s*:\s*"', re.M)
    extra = []
    for path in _sources():
        rel = path.relative_to(SRC).as_posix()
        if rel == "api/errorText.ts":
            continue
        if pat.search(_code(path)):
            extra.append(rel)
    assert not extra, f"这些文件又造了一张错误码文案表：{extra}"


def test_no_site_reads_friendly_or_user_hint() -> None:
    """`friendly` 已被删除；`userHint` 只能在 `errorText()` 里读。

    这两个口子就是 F6 的绕过路径：只要还能直接取，展示层就会有人
    "顺手"写成 `e.userHint || e.friendly`，而这句话在英文环境下
    会把技术串甩给用户。
    """
    bad: list[str] = []
    for path in _sources():
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED_HINT_READERS:
            continue
        for i, line in enumerate(_code(path).splitlines(), 1):
            if ".friendly" in line or ".userHint" in line:
                bad.append(f"{rel}:{i}: {line.strip()}")
    assert not bad, (
        "这些地方绕过了 errorText()，直接取后端/异常上的文案：\n" + "\n".join(bad)
    )


def test_display_sites_go_through_error_text() -> None:
    for rel, least in MUST_USE_ERROR_TEXT.items():
        src = _code(SRC / rel)
        got = src.count("errorText(")
        assert got >= least, (
            f"{rel} 只有 {got} 处 errorText()，至少应有 {least} 处 —— "
            f"有人把展示文案改回了 e.userHint/e.friendly/e.message"
        )
