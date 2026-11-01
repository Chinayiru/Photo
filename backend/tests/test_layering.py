"""层次边界闸门（架构审查 c1cd076 M1/M2）。

审查指出两件事，这里各钉一条 AST 判据，防止以后再长回去：

1. **业务层不许依赖传输层**：`backend/app/handlers/**` 与 `backend/app/core/**`
   不得 import `backend.app.transports`。Range/MIME 这些协议无关的 HTTP 语义
   已经搬到 `backend/app/core/httpmedia.py`。
2. **不许出现 import 环**：`backend/app` 下所有模块构成的整图必须无环。
   ★ 判据是**全图**（含函数体内的延迟导入、含包 `__init__`），不是"某四个
   模块之间有没有互相 import"—— 后者漏掉的正是 ISSUE-073 那种写法：四组环
   全部由函数体内的延迟导入构成，顶层 import 一条都不违规，于是启动期不炸、
   现有测试全绿，而依赖图上确实是环。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "backend" / "app"
PKG = "backend.app"


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
        elif isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
    return out


def test_handlers_and_core_never_import_the_transport_layer() -> None:
    bad: list[str] = []
    for path in list((APP / "handlers").rglob("*.py")) + list((APP / "core").rglob("*.py")):
        for mod in _imports(path):
            if "transports" in mod:
                bad.append(f"{path.relative_to(REPO)} -> {mod}")
    assert not bad, "业务层依赖了传输层（M1）：\n" + "\n".join(bad)


def _top_level_imports(path: Path) -> set[str]:
    """只看模块**顶层**（不在函数/类体内）的相对导入目标。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in tree.body:  # 只遍历模块体，函数体里的延迟导入不算
        if isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[-1])
        elif isinstance(node, ast.ImportFrom) and node.level:
            out.update(a.name for a in node.names)
    return out


def test_identity_modules_do_not_import_each_other_at_module_level() -> None:
    """`auth` / `control` / `tunnel` / `guest` 之间的顶层 import 必须是单向的。

    这条是"退化的"快速判据：只有顶层 import 才算。真正的无环判据在下面
    `test_import_graph_has_no_cycles`，它把延迟导入也算进去。
    """
    names = ("auth", "control", "tunnel", "guest")
    edges: dict[str, set[str]] = {}
    for name in names:
        mods = _top_level_imports(APP / f"{name}.py")
        edges[name] = {m for m in mods if m in names and m != name}
    cycles = [
        f"{a} -> {b}" for a in names for b in edges[a]
        if a in edges.get(b, set())
    ]
    assert not cycles, "模块级循环导入（M2）：" + ", ".join(cycles)


# ---------------------------------------------------------------------------
# 全图无环（M2 的判据）
# ---------------------------------------------------------------------------


def _module_names() -> dict[str, Path]:
    """`backend.app.xxx.yyy` → 文件路径；包的 `__init__.py` 记为包名本身。"""
    out: dict[str, Path] = {}
    for path in APP.rglob("*.py"):
        parts = path.relative_to(APP).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
            if not parts:          # backend/app/__init__.py
                out[PKG] = path
                continue
        out[".".join((PKG, *parts))] = path
    return out


def _package_of(mod: str, level: int) -> list[str]:
    """按相对导入的层级算出基准包：`level=1` 是当前包，`level=2` 是上一级。"""
    parts = mod.split(".")[:-1]          # 当前模块所在的包
    if level > 1:
        parts = parts[: len(parts) - (level - 1)]
    return parts


def _resolve(name: str, known: set[str]) -> str | None:
    """把名字截到最近一个**真实存在**的模块（`core.ctx` 不存在时退到 `core`）。"""
    parts = name.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in known:
            return candidate
        parts.pop()
    return None


def _edges(names: dict[str, Path]) -> dict[str, set[str]]:
    known = set(names)
    graph: dict[str, set[str]] = {mod: set() for mod in known}
    for mod, path in names.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):        # ast.walk 覆盖函数体内的延迟导入
            targets: list[str] = []
            if isinstance(node, ast.ImportFrom):
                level = node.level or 0
                if not level:
                    targets = [node.module or ""]
                else:
                    base = _package_of(mod, level)
                    if node.module:
                        targets = [".".join((*base, node.module))]
                    else:
                        # `from . import guest` —— 被导入的是**子模块**
                        targets = [".".join((*base, a.name)) for a in node.names]
            elif isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            for target in targets:
                hit = _resolve(target, known)
                if hit and hit != mod:
                    graph[mod].add(hit)
    return graph


def _find_cycles(graph: dict[str, set[str]]) -> list[list[str]]:
    """DFS 找回边的路径；每个环只报一次（按规范化后的起点去重）。"""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {node: WHITE for node in graph}
    stack: list[str] = []
    found: list[list[str]] = []
    seen: set[frozenset[str]] = set()

    def visit(node: str) -> None:
        color[node] = GREY
        stack.append(node)
        for nxt in sorted(graph[node]):
            if color[nxt] == GREY:
                cycle = stack[stack.index(nxt):]
                key = frozenset(cycle)
                if key not in seen:
                    seen.add(key)
                    found.append([*cycle, nxt])
            elif color[nxt] == WHITE:
                visit(nxt)
        stack.pop()
        color[node] = BLACK

    for node in sorted(graph):
        if color[node] == WHITE:
            visit(node)
    return found


def test_import_graph_has_no_cycles() -> None:
    """`backend/app` 的整张 import 图必须无环（M2 的完成判据）。

    与上一条的区别：这里**不区分**顶层 import 与函数体内的延迟导入，也不
    限定四个模块——任何两个模块之间形成环都会失败，包括以后新增的模块。
    """
    names = _module_names()
    graph = _edges(names)
    cycles = _find_cycles(graph)
    assert not cycles, (
        "backend/app 存在 import 环（M2）：\n"
        + "\n".join("  " + " -> ".join(cycle) for cycle in cycles)
        + "\n解法：把环里共享的原语搬到一个更底层、不反向依赖的模块"
          "（如 backend/app/identity.py），而不是继续用函数内延迟导入掩盖。"
    )
