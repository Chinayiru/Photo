"""界面文案收口（ISSUE-069）：错误码 / 英文串不许出现在用户眼前。

## 为什么这条要成为测试

真机实测过的原话（游客点「回收站」）：

    提示条上写着 token_resource_mismatch

`ApiError.friendly` 的第二个候选是 `message`，而 `message` 是错误码的英文
名。所以只要后端某条路径没填 `user_hint`，或者某个调用点写成
`e instanceof Error ? e.message : "兜底"`，技术串就直接上屏。这类退化不会
让任何功能失效，只会让用户看到一串看不懂的英文 —— 功能测试抓不到。

判据只有两条：
1. 展示层一律走 `errorText()`；
2. 前端源码里除 `errorText()`/`client.ts` 自身，不许再出现"把 `.message`
   直接当界面文案"的写法。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "frontend" / "src"

#: 允许出现 `.message` 的地方：错误类型的定义与文案收口本身。
ALLOWED = {"api/client.ts", "api/errorText.ts"}


def _sources() -> list[Path]:
    out = []
    for pat in ("**/*.ts", "**/*.vue"):
        out.extend(p for p in SRC.glob(pat) if p.is_file())
    return out


def _code(path: Path) -> str:
    """去掉注释与模板里的说明文字，只留会被执行的代码。"""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text


def test_error_text_module_exists_and_never_returns_a_code() -> None:
    src = (SRC / "api" / "errorText.ts").read_text(encoding="utf-8")
    assert "export function errorText(" in src
    # 兜底里若出现 e.code / e.message，就等于又把技术串放回界面
    body = src.split("export function errorText(")[1]
    assert "return e.code" not in body
    assert "return e.detail" not in body


def test_no_display_site_shows_a_raw_message() -> None:
    bad: list[str] = []
    pat = re.compile(r"(toastError|toastWarn|\.value\s*=|task\.error\s*=)[^;\n]*\.message")
    for path in _sources():
        rel = path.relative_to(SRC).as_posix()
        if rel in ALLOWED:
            continue
        for i, line in enumerate(_code(path).splitlines(), 1):
            if pat.search(line):
                bad.append(f"{rel}:{i}: {line.strip()}")
    assert not bad, "这些地方把异常的 message 直接当文案了：\n" + "\n".join(bad)


def test_guest_write_rejection_has_its_own_wording() -> None:
    src = (SRC / "api" / "errorText.ts").read_text(encoding="utf-8")
    assert "游客只能查看和上传" in src


def test_wire_rename_and_move_go_through_error_text() -> None:
    viewer = _code(SRC / "views" / "ViewerView.vue")
    for op in ("改名失败", "移动失败", "删除失败", "下载失败"):
        assert f'errorText(e, "{op}")' in viewer, op
