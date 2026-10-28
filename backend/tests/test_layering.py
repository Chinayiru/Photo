"""层次边界闸门（架构审查 c1cd076 M1/M2）。

审查指出两件事，这里各钉一条 AST 判据，防止以后再长回去：

1. **业务层不许依赖传输层**：`backend/app/handlers/**` 与 `backend/app/core/**`
   不得 import `backend.app.transports`。Range/MIME 这些协议无关的 HTTP 语义
   已经搬到 `backend/app/core/httpmedia.py`。
2. **不许出现模块级循环导入**：`auth` / `control` / `tunnel` / `guest` 之间
   的顶层 import 必须是单向的。函数内延迟导入允许（现状如此），但模块顶部
   再互相 import 一次就会把环变成启动期硬依赖。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "backend" / "app"


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
    """`auth` / `control` / `tunnel` / `guest` 之间的顶层 import 必须是单向的。"""
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
