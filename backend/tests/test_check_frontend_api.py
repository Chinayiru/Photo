"""前后端接口一致性检查器（`scripts/check_frontend_api.py`）的回归。

## 为什么这条测试必须存在

2026-10-20 手动跑了一遍 `scripts/check_frontend_api.py`，它**退出码 1**：

```
✗ 前端调用了不存在的接口：/api/**
    出现在：frontend\\src\\api\\transport.ts
```

而 `/api/**` 只出现在 `transport.ts` 第 10 行的**模块 docstring** 里
（「两条通路走完全相同的 `/api/**` 路径」）—— 那是一句通配描述，
不是调用。检查器把注释当代码扫，于是判成「前端调了不存在的接口」。

真正的问题不在这个假阳性本身，而在于：**没有任何东西在跑这个检查器**。
没有测试引用它，也没有 CI 入口（仓库里连 `.github/` 都没有）。
一条永远不会被执行的检查，红了几天也没人知道 —— 它提供的安全感是假的。

所以本文件做三件事：
  a. 在真实仓库上跑，断言 exit 0（现状干净）；
  b. 钉住"注释里的路径不算调用"这个判据，并用**变异**证明它真的在起作用；
  c. 钉住它的另一半职责 —— 真实的路径漂移仍然必须被抓到（不能为了
     少报错把判据放宽到什么都不报）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_frontend_api.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_frontend_api", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CFA = _load()


# ---------------------------------------------------------------------------
# (a) 真实仓库必须干净
# ---------------------------------------------------------------------------


def test_real_repo_has_no_drift() -> None:
    """底线：当前仓库的前端路径全都能在后端路由表里找到。"""
    assert CFA.main() == 0


def test_routes_and_paths_are_both_non_empty() -> None:
    """两条腿都不能是空的 —— 空集合比对永远通过。"""
    routes = CFA._route_patterns()
    paths = CFA._frontend_paths()
    assert len(routes) >= 20, "后端路由只解析出 %d 条，解析器坏了？" % len(routes)
    assert len(paths) >= 15, "前端路径只解析出 %d 条，扫描器坏了？" % len(paths)


# ---------------------------------------------------------------------------
# (b) 注释里的路径不算调用（本次修复的判据）
# ---------------------------------------------------------------------------


def test_docstring_wildcard_is_not_a_call() -> None:
    """`/api/**` 只在注释里 —— 它不该出现在扫出来的路径集合里。

    这是回归的核心断言。它红了就说明 `_strip_comments()` 失效了
    （或者有人真在代码里写了 `/api/**`，那也确实该红）。
    """
    text = (REPO / "frontend" / "src" / "api" / "transport.ts").read_text(encoding="utf-8")
    assert "/api/**" in text, "前提变了：注释里已经没有这个通配写法"
    stripped = CFA._strip_comments(text)
    assert "/api/**" not in stripped, "注释没有被剥掉"


def test_strip_comments_keeps_line_numbers() -> None:
    """剥注释必须保行数 —— 否则报错里的行号全是错的。"""
    src = 'const a = 1; // tail\n/* multi\nline */\nconst b = "/api/x";\n'
    out = CFA._strip_comments(src)
    assert out.count("\n") == src.count("\n")
    assert len(out) == len(src), "长度必须一一对应，否则列号也会偏"
    assert "/api/x" in out, "字符串里的路径不能被当成注释吃掉"


def test_strip_comments_does_not_eat_urls() -> None:
    """`https://...` 里的 `//` 不是注释起点。

    这一条如果错了，后果比假阳性更严重：`//` 把后半行吃掉，
    同一行后面真正的 `/api/...` 调用会**静默消失**，真漂移就查不到了。
    """
    src = 'const u = "https://example.com/d/abc/api/timeline";\n'
    out = CFA._strip_comments(src)
    assert "https://example.com/d/abc/api/timeline" in out


def test_strip_comments_handles_template_literals() -> None:
    """反引号模板串里的 `${...}` 要原样留下，供 `_normalize()` 归一化。"""
    src = "const p = `/api/items/${id}/thumb`;\n"
    out = CFA._strip_comments(src)
    assert "/api/items/${id}/thumb" in out


def test_mutation_stripping_restores_the_false_positive() -> None:
    """★ 变异：把 `_strip_comments` 换成恒等函数，假阳性必须回来。

    不看这一条，(b) 的前面几条都可能只是"断言写对了但判据没生效"。
    实测：2026-10-20 应用此变异后 `_frontend_paths()` 立刻多出
    `/api/**`，`main()` 返回 1。
    """
    original = CFA._strip_comments
    try:
        CFA._strip_comments = lambda s: s  # type: ignore[assignment]
        paths = CFA._frontend_paths()
        assert "/api/**" in paths, "恒等函数下应当重新出现假阳性"
        assert CFA.main() == 1, "假阳性存在时 main() 必须报错"
    finally:
        CFA._strip_comments = original  # type: ignore[assignment]
    # 还原之后必须恢复干净
    assert "/api/**" not in CFA._frontend_paths()
    assert CFA.main() == 0


# ---------------------------------------------------------------------------
# (c) 真漂移仍然要被抓到
# ---------------------------------------------------------------------------


def test_real_drift_is_still_caught(monkeypatch, tmp_path: Path) -> None:
    """把一条真实调用改成不存在的路径，`main()` 必须报错。

    这是防止"为了不报假阳性，把判据放宽到什么都不报"。
    """
    fake = tmp_path / "fake.ts"
    fake.write_text('const r = await fetch("/api/definitely-not-a-route");\n', encoding="utf-8")

    original = CFA._frontend_paths
    try:
        monkeypatch.setattr(
            CFA,
            "_frontend_paths",
            lambda: {"/api/definitely-not-a-route": [str(fake)]},
        )
        assert CFA.main() == 1
    finally:
        CFA._frontend_paths = original  # type: ignore[assignment]


def test_shape_ignores_param_names() -> None:
    """参数名不同不算漂移（`{id}` vs `{media_id}`），只比形状。"""
    assert CFA._shape("/api/items/{id}/thumb") == "/api/items/*/thumb"
    assert CFA._shape("/api/items/{media_id}/thumb") == "/api/items/*/thumb"


def test_normalize_strips_query_and_template() -> None:
    """查询串与 `${...}` 都要归一化，否则每条带参数的路径都会报漂移。"""
    assert CFA._normalize("/api/items/5/thumb?size=320&v=abc") == "/api/items/5/thumb"
    assert CFA._normalize("/api/items/${id}/thumb") == "/api/items/{}/thumb"
    assert CFA._normalize("/api/timeline/") == "/api/timeline"


def test_intentionally_unused_entries_all_carry_a_reason() -> None:
    """「故意不用」清单里每条都必须写明理由。

    否则它会变成漂移的垃圾桶 —— 谁都能加一行让检查通过。
    """
    for path, why in CFA.INTENTIONALLY_UNUSED.items():
        assert path.startswith("/api/") or path.startswith("/panel/"), path
        assert len(why.strip()) >= 10, "%s 的理由太短：%r" % (path, why)
