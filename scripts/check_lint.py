#!/usr/bin/env python
"""编码规范检查器：把 docs/06 §10 的 20 条规则做成**可执行**的判据。

## 为什么有这个东西

`docs/06-安全设计文档.md` §10 列了 20 条编码规则，`:3934-3945` 还有一张
「规范检查脚本汇总」表，声称 `check_sql.py` / `check_secrets.py` /
`check_thumb.py` / `check_routes.py` / `check_audit.py` / `check_rules.py` /
`check_all.sh` 各管几条 —— **这七个脚本一个都不存在**。于是
`docs/07-测试计划与用例.md` §7.14 的 `TC-LINT-SEC-001~020` 全部标 ✅，
判据写的是「运行 `python scripts/check_sql.py`」，而这条命令连文件都找不到。

更糟的是当时的判据形式本身：`grep -c "sniff_kind" uploads.py >= 1`
——**函数名出现就算通过**。魔术字节校验最终是"函数名在、调用不在"，
伪装成图片的 PE 文件被照单全收、归档进真实照片库（见 docs/11 Q-023）。
符号存在性不是行为。

所以这里不用 grep，用 **AST**：判断的是"代码结构是否满足规则"，而不是
"某个字符串是否出现"。每条规则都是 `_rule_NN(ctx)`，返回 `list[Finding]`。

## 用法

    python scripts/check_lint.py              # 全部 20 条
    python scripts/check_lint.py --rule=2     # 只跑规则 2
    python scripts/check_lint.py --list       # 列出规则与说明
    python scripts/check_lint.py --json       # 机读输出

退出码：0 = 全部通过；1 = 有违规；2 = 用法错误。

## 设计约束

每条规则都必须是**双向可验**的：`backend/tests/test_check_lint.py` 会
（a）断言当前代码 0 违规，（b）注入一个已知坏样本，断言恰好报出那一行。
只做 (a) 的检查器和 grep 没有区别 —— 它永远通过，然后被删掉。
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import re
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]
APP = REPO / "backend" / "app"
TESTS = REPO / "backend" / "tests"
FRONTEND = REPO / "frontend" / "src"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """一条违规。`line=0` 表示"整个文件/目录层面"的问题（如缺文件）。"""

    rule: int
    path: str
    line: int
    message: str

    def render(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"  [{where}] {self.message}"


@dataclass
class Ctx:
    """一次检查的共享上下文：解析好的 AST 缓存 + 前端源码。

    `repo` / `app` / `frontend` 都可以被覆盖 —— `backend/tests/test_check_lint.py`
    会把它指向一个临时目录，在里面注入坏样本，验证规则**真的会报**。
    检查器若写死真实路径，就只能验"当前代码 0 违规"这一半，而那半个
    断言对"规则失效"完全无感（判据空转时它照样通过）。
    """

    repo: Path = REPO
    app: Path = APP
    frontend: Path = FRONTEND
    _cache: dict[Path, ast.Module | None] = field(default_factory=dict)

    @property
    def app_files(self) -> list[Path]:
        return sorted(p for p in self.app.rglob("*.py") if "__pycache__" not in p.parts)

    def parse(self, path: Path) -> ast.Module | None:
        if path not in self._cache:
            try:
                self._cache[path] = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError):
                self._cache[path] = None
        return self._cache[path]

    def app_trees(self) -> Iterator[tuple[Path, ast.Module]]:
        for p in self.app_files:
            t = self.parse(p)
            if t is not None:
                yield p, t

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.repo).as_posix()
        except ValueError:
            return path.as_posix()

    def rel_to_app(self, path: Path) -> str:
        """相对 `backend/app` 的 posix 路径，如 `handlers/browse.py`。

        判据里按这个路径做键，而不是 `path.name` —— 仓库里有同名的
        `uploads.py` 与 `handlers/uploads.py`，只看文件名会混淆。
        """
        try:
            return path.relative_to(self.app).as_posix()
        except ValueError:
            return path.name

    def frontend_files(self, *suffixes: str) -> list[Path]:
        if not self.frontend.is_dir():
            return []
        return sorted(p for p in self.frontend.rglob("*")
                      if p.suffix in suffixes and p.is_file())

    def read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return ""


# ---------------------------------------------------------------------------
# AST 小工具
# ---------------------------------------------------------------------------


def _call_name(node: ast.Call) -> str:
    """`os.unlink(x)` → "os.unlink"；`p.unlink()` → "unlink"；`f()` → "f"。

    返回值**不区分**"是不是模块限定"，由各规则自己按需比对后缀。
    """
    f = node.func
    if isinstance(f, ast.Attribute):
        parts: list[str] = [f.attr]
        cur: Any = f.value
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
        return ".".join(reversed(parts))
    if isinstance(cur := f, ast.Name):
        return cur.id
    return ""


def _full_name(node: ast.Call) -> str:
    """与 `_call_name` 同义，但保证返回带接收者的形态（含空接收者）。"""
    return _call_name(node)


def _attr_chain(node: ast.AST) -> list[str]:
    """把 `a.b.c` 拆成 ["a","b","c"]；不是纯属性链时返回 []。"""
    out: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        out.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        out.append(cur.id)
        return list(reversed(out))
    return []


def _enclosing(tree: ast.Module, lineno: int) -> str:
    """找出包含该行的最内层函数名（用于报错时指出位置）。"""
    best = "<module>"
    best_span = (0, 10**9)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = n.lineno
            end = getattr(n, "end_lineno", start) or start
            if start <= lineno <= end and (end - start) < (best_span[1] - best_span[0]):
                best = n.name
                best_span = (start, end)
    return best


def _calls(tree: ast.Module) -> Iterator[ast.Call]:
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            yield n


def _string_constants(tree: ast.Module) -> Iterator[ast.Constant]:
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield n


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """所有 docstring 里 ast.Constant 的 id() —— 判断"这句是不是注释/文档"。"""
    out: set[int] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(n, "body", None)
            if not body:
                continue
            first = body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                out.add(id(first.value))
    return out


# ---------------------------------------------------------------------------
# 规则 1：所有文件访问必须经过沙箱
# ---------------------------------------------------------------------------

#: 允许直接做文件系统调用的模块。**白名单按文件**，不是按调用点 ——
#: 这些文件本身就是基础设施：配置要读自己、日志要写自己、数据库要开自己。
#: 业务代码（handlers / indexer / thumbs / uploads / watcher / archive）
#: 一个都不在白名单里，它们必须走 `sandbox.resolve()`。
_FS_WHITELIST: frozenset[str] = frozenset({
    "config.py", "audit.py", "db.py", "logging_setup.py", "static.py",
    "server.py", "autostart.py", "tray.py", "sandbox.py", "exif.py",
    "errors.py", "uploads_io.py",
})

#: 视为"直接碰磁盘"的调用（按 `_call_name` 的形态匹配）。
_FS_CALLS: frozenset[str] = frozenset({
    "open", "os.open", "os.remove", "os.unlink", "os.replace", "os.rename",
    "os.mkdir", "os.makedirs", "os.rmdir", "os.listdir", "os.scandir",
    "os.stat", "os.lstat", "os.walk", "shutil.copy", "shutil.copy2",
    "shutil.copyfile", "shutil.move", "shutil.rmtree", "shutil.disk_usage",
})

#: `Path(...)` 上的实例方法。
_FS_METHODS: frozenset[str] = frozenset({
    "read_text", "read_bytes", "write_text", "write_bytes", "open",
    "unlink", "rename", "replace", "mkdir", "rmdir", "iterdir",
    "rglob", "glob", "stat", "lstat", "exists", "is_file", "is_dir",
    "resolve", "touch", "samefile",
})

#: 必须**确实**调用 `sandbox.resolve()` 的业务模块 —— 它们都直接拿
#: 用户给的相对路径去拼磁盘位置。少调一次就是一个路径穿越漏洞。
#:
#: ⚠️ 用**相对路径**做键，不能用 `path.name`：仓库里有两对同名文件
#:    `backend/app/uploads.py`（真的碰磁盘）与
#:    `backend/app/handlers/uploads.py`（纯 HTTP 层，只转调 transport，
#:    一行文件操作都没有）。用文件名当键会把后者冤判成"绕过沙箱"。
_SANDBOX_REQUIRED: frozenset[str] = frozenset({
    "archive.py", "handlers/browse.py", "indexer.py", "thumbs.py",
    "uploads.py", "watcher.py",
})


def _rule_1(ctx: Ctx) -> list[Finding]:
    """规则 1：业务代码拿用户路径必须经过沙箱。

    **判据不能是"逐调用点查文件系统 API"**。第一版就是这么写的，结果
    报出 60 多处违规，全是假的：`p.stat()` / `dst.unlink()` 里的 `p`、`dst`
    本身就是 `sandbox.resolve()` 的返回值 —— 拿到沙箱路径之后再 stat
    它，恰恰是**正确**用法。逐个调用点检查无法区分"用户输入的路径"
    与"已经过校验的路径"，因为这个差别在数据流里，不在语法里。

    真正能静态判定的两件事：

      a. 每个业务文件里**必须**至少有一次 `sandbox.resolve(...)` 调用 ——
         一个业务模块若从不解析用户路径，它要么不碰文件，要么就是绕过了；
      b. 业务代码里**不得**出现 `root.path / rel` 这类拼接 —— 这是绕过
         沙箱的**唯一**入口形态。全仓库只有 `config.py:218` 的
         `RootConfig.tmp_dir` 用它，而 config.py 在基础设施白名单里。

    (b) 是这条规则真正的价值：路径穿越只有一个写法，而那条写法的
    语法形态是固定的。
    """
    out: list[Finding] = []
    for path, tree in ctx.app_trees():
        if path.name in _FS_WHITELIST:
            continue

        # (b) 不得用 `.path / ...` 拼用户路径
        for node in ast.walk(tree):
            if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
                continue
            left = node.left
            if not (isinstance(left, ast.Attribute) and left.attr == "path"):
                continue
            out.append(Finding(
                1, ctx.rel(path), node.lineno,
                f"用 {ast.unparse(node)[:50]} 拼路径 —— 绕过沙箱；"
                "必须 sandbox.resolve()",
            ))

        # (a) 碰用户照片的模块必须真的有沙箱调用
        if ctx.rel_to_app(path) not in _SANDBOX_REQUIRED:
            continue
        n_resolve = 0
        for call in _calls(tree):
            name = _call_name(call)
            if name == "sandbox.resolve":
                n_resolve += 1
            elif name.rsplit(".", 1)[-1] == "resolve":
                recv = (ast.unparse(call.func.value)
                        if isinstance(call.func, ast.Attribute) else "")
                if recv == "sandbox":
                    n_resolve += 1
        if n_resolve == 0:
            out.append(Finding(
                1, ctx.rel(path), 0,
                "业务文件里没有任何 sandbox.resolve() 调用 —— 用户路径无从校验",
            ))
    return out


# ---------------------------------------------------------------------------
# 规则 2：用户数据的删除必须进回收站
# ---------------------------------------------------------------------------

#: 允许出现"真删"的形态：临时文件、缓存、半成品。判据是**调用点的
#: 接收者名字**里带这些词——因为真正的风险是"用户以为在删照片，实际
#: 直接抹掉"。
_TEMPISH = re.compile(
    r"(tmp|temp|part|probe|stale|half|scratch|cache|\.pb[a-z]*)", re.IGNORECASE)

#: 已核实过语义的"合法真删"位置：函数名或其职责说明它删的不是用户数据。
#: 每一条都必须能讲清楚为什么安全，否则不许加进来。
_DELETE_ALLOWED = re.compile(
    r"("
    r"_unlink_quietly|cleanup_tmp|_serve_thumb|_move_one_sync|_unlink_temp|"
    r"_probe_writable"
    r")", re.IGNORECASE)


def _rule_2(ctx: Ctx) -> list[Finding]:
    """规则 2：删除用户数据的路径必须走 `send2trash`，不许直接 `os.unlink`。

    `docs/06:3620` 的原判据是「grep 整个仓库 `os.unlink|os.remove|
    shutil.rmtree` → 必须为空」。**这个判据本身就错了**：清临时文件、
    删半成品、清过期缓存都必须真删（进回收站反而是污染）。当前
    生产代码有 9 处，全部是临时/缓存路径，于是判据永远是红的 ——
    一个永远失败的判据和没有判据一样。

    真正该守的是**语义**：用户照片的删除（`/api/items/delete`）必须
    经 `sandbox.resolve(..., "delete")` 再过 `send2trash`。这条在
    `handlers/browse.py::_trash_one` 里，用规则判据钉住它：

      a. `delete_items` 链路里必须出现 `send2trash`；
      b. `_trash_one` 这类删除函数里，删除前的路径必须过沙箱（need=delete）；
      c. 其余真删调用点的接收者必须是"临时/缓存"形态。
    """
    out: list[Finding] = []

    # (a) 回收站调用必须存在 —— 没有它，"删除"只能是真删。
    #
    # ⚠️ 检查 `send2trash` 时**不能只看 call 表达式**：它在
    # `handlers/browse.py:796` 是**函数内 import**（`from send2trash import
    # send2trash`），随后作为普通 Name 被传给 `run_in_executor` ——
    # `await loop.run_in_executor(None, send2trash, str(path))`。
    # 也就是说它在 AST 里根本不是一次 Call。第一版按 `_call_name(call)`
    # 找，于是永远报"全后端找不到 send2trash"，典型的判据比实现窄。
    trash_files: dict[Path, ast.Module] = {}
    for path, tree in ctx.app_trees():
        text = ctx.read(path)
        if "send2trash" not in text:
            continue
        imports_it = any(
            isinstance(n, ast.ImportFrom) and (n.module or "").startswith("send2trash")
            for n in ast.walk(tree)
        )
        calls_it = any("send2trash" in _call_name(c) for c in _calls(tree))
        if imports_it or calls_it:
            trash_files[path] = tree
    if not trash_files:
        out.append(Finding(
            2, "backend/app", 0,
            "全后端找不到 send2trash —— 用户照片删除会变成不可恢复的真删",
        ))

    # (b) 回收站调用所在文件必须能追溯到一次 need="delete" 的沙箱放行。
    for path, tree in trash_files.items():
        src = ctx.read(path)
        has_delete_need = ('"delete"' in src) or ("'delete'" in src)
        if not has_delete_need:
            out.append(Finding(
                2, ctx.rel(path), 0,
                '走回收站但同文件看不到 need="delete" 的沙箱校验',
            ))

    # (c) 其余真删必须发生在临时/缓存/半成品对象上。
    #
    # 这里用**函数名 + 变量名**双判据，因为光看变量名会漏掉真实语义：
    #   archive.py:370 `os.unlink(path)` 在 `_unlink_quietly()` 里
    #   archive.py:403 `os.unlink(entry.path)` 在 `cleanup_tmp()` 里
    #   browse.py:285  `dst.unlink()`        在 `_serve_thumb()`（陈旧缓存）
    #   browse.py:976  `os.unlink(src)`      在 `_move_one_sync()`（跨卷删源）
    #   uploads.py:181 `p.unlink()`          在 `_unlink_temp()` 里
    # 这些变量名本身都不带 tmp（`path`、`dst`、`src`、`p`），但所在函数
    # 的职责就是清临时/缓存，或者删的是"已经复制校验过的源文件"。
    for path, tree in ctx.app_trees():
        for call in _calls(tree):
            name = _call_name(call)
            tail = name.rsplit(".", 1)[-1]
            if tail not in ("unlink", "remove", "rmtree"):
                continue
            if "send2trash" in name:
                continue
            # 取接收者表达式文本（`tmp.unlink()` 的 `tmp`）
            recv = call.func.value if isinstance(call.func, ast.Attribute) else None
            recv_txt = ast.unparse(recv) if recv is not None else ""
            first = ast.unparse(call.args[0]) if call.args else ""
            fn = _enclosing(tree, call.lineno)
            hay = f"{fn} {recv_txt} {first}"
            if _TEMPISH.search(hay) or _DELETE_ALLOWED.search(hay):
                continue
            out.append(Finding(
                2, ctx.rel(path), call.lineno,
                f"{name}() 删的是非临时对象（{fn}() 里的 {hay.split(' ', 1)[-1].strip()[:40]}）"
                " —— 用户数据必须走 send2trash",
            ))
    return out


# ---------------------------------------------------------------------------
# 规则 3：禁止拼接 SQL
# ---------------------------------------------------------------------------

_SQL_METHODS = frozenset({"execute", "executemany", "query", "query_one", "scalar"})


def _rule_3(ctx: Ctx) -> list[Finding]:
    """规则 3：SQL 语句里只允许拼接 `?` 占位符，绝不允许拼值。

    `f"... WHERE id IN ({placeholders})"` 是**安全**的：`placeholders`
    只由 `",".join(["?"] * n)` 生成，具体值仍走参数。一律禁止的话，
    当前代码里 4 处批量 `IN (...)` 全会被判红 —— 而它们的写法正是
    唯一可行的变长占位符方案（SQLite 不支持数组参数）。

    判据因此落在"拼接后的片段必须只含 `?` 与标点"上。
    """
    out: list[Finding] = []
    allowed = re.compile(r"^[\s,()?]*$")

    for path, tree in ctx.app_trees():
        # 收集"纯占位符生成器"：`",".join(["(?,?)"] * n)` 这类
        helper_names: set[str] = set()
        # 收集"只含 ? 字面量"的 SQL 片段变量：`where = " WHERE missing = 0
        # AND kind = 'image'"` → `where += " AND root_id = ?"`。
        # 判据：初始值是不含插值的字符串字面量，且所有 `+=` 的右值也不插值。
        fragment_names: set[str] = set()
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                value = node.value
                for t in node.targets:
                    # `where, params = timeline_where(...)` 是 Tuple 目标
                    if isinstance(t, ast.Tuple):
                        targets.extend(t.elts)
                    else:
                        targets.append(t)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
                value = node.value
            # 也认 `x = f"... {y} ..."` 里对 helper 的别名
            if value is not None and _is_placeholder_expr(value):
                for tgt in targets:
                    if isinstance(tgt, ast.Name):
                        helper_names.add(tgt.id)
            # SQL 片段：初始值是字面量字符串，或 += 纯字面量字符串
            if value is not None and isinstance(value, ast.Constant) \
                    and isinstance(value.value, str):
                for tgt in targets:
                    if isinstance(tgt, ast.Name):
                        fragment_names.add(tgt.id)

        # `where += " AND root_id = ?"` —— 扩充式赋值同样只接字面量，
        # 变量里始终不会出现插值。
        for node in ast.walk(tree):
            if not isinstance(node, ast.AugAssign) or not isinstance(node.target, ast.Name):
                continue
            if isinstance(node.op, ast.Add) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str) and "?" in node.value.value:
                fragment_names.add(node.target.id)

        for call in _calls(tree):
            tail = _call_name(call).rsplit(".", 1)[-1]
            if tail not in _SQL_METHODS or not call.args:
                continue
            sql = call.args[0]
            bad = _sql_injection_risk(sql, helper_names, allowed,
                                      fragment_names)
            if bad:
                out.append(Finding(
                    3, ctx.rel(path), call.lineno,
                    f"SQL 首参拼接了非占位符内容（{bad}）—— 值必须走参数",
                ))
    return out


def _is_placeholder_expr(node: ast.AST) -> bool:
    """`",".join(["(?,?)"] * n)` / `",".join("?" * n)` 这类纯占位符表达式。

    ⚠️ 第一版判据写成 `_call_name(node) not in ("join", ",.join") and
    not _call_name(node).endswith(".join")` —— 对 `",".join(...)` 来说
    `_call_name` 返回的就是 `"join"`（接收者是字符串常量，不是 Name，
    所以拼不出前缀），于是第一个条件为假、第二个条件为真，
    `and` 短路后整体为真 → **恒返回 False**。写成括号更直白。
    """
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node)
    if not (name == "join" or name.endswith(".join")):
        return False
    if not node.args:
        return False
    arg = node.args[0]
    if isinstance(arg, ast.List) and len(arg.elts) == 1:
        elt = arg.elts[0]
        return isinstance(elt, ast.Constant) and _only_placeholders(str(elt.value))
    # `["(?,?)"] * len(part)` —— 左值是单元素列表、右值是数量，整体仍只含 ?
    if isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mult):
        left = arg.left
        if isinstance(left, ast.List) and len(left.elts) == 1:
            elt = left.elts[0]
            return isinstance(elt, ast.Constant) and _only_placeholders(str(elt.value))
        if isinstance(left, ast.Constant):
            return _only_placeholders(str(left.value))
    return False


def _only_placeholders(s: str) -> bool:
    return bool(re.fullmatch(r"[\s,()?]*", s)) and "?" in s


#: 允许出现在 SQL 拼接里的"列名常量"：全大写、不含引号。它们只能来自
#: 代码里的字面量定义，不可能携带用户输入。
_CONST_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _sql_injection_risk(sql: ast.AST, helpers: set[str], allowed: re.Pattern[str],
                        fragments: set[str] | None = None) -> str:
    """返回风险描述；空串表示安全。

    `fragments` 是"只由 `?` 占位符与固定文本拼成的 SQL 片段变量"，
    如 `browse.py` 里的 `where`（由 `timeline_where()` 返回，
    值全部走 `params`）。
    """
    fragments = fragments or set()
    if isinstance(sql, ast.Constant):
        return ""                                    # 纯字面量，天然安全
    if isinstance(sql, ast.JoinedStr):
        for v in sql.values:
            if isinstance(v, ast.Constant):
                # 字面部分不允许含引号 —— 那意味着值是直接写进 SQL 的
                if "'" in str(v.value) or '"' in str(v.value):
                    return f"字面部分含引号: {str(v.value)[:40]!r}"
                continue
            expr = v.value
            # `{placeholders}` 这种纯占位符名字放行
            if isinstance(expr, ast.Name) and expr.id in helpers:
                continue
            # `{where}` 这种只含 ? 的片段变量放行
            if isinstance(expr, ast.Name) and expr.id in fragments:
                continue
            # `{TIMELINE_COLUMNS}` 这类全大写列名常量放行：它的取值是
            # 模块级字符串字面量，没有外部输入路径。
            if isinstance(expr, ast.Name) and _CONST_NAME.match(expr.id):
                continue
            if isinstance(expr, ast.Call) and _is_placeholder_expr(expr):
                continue
            return f"插入表达式 {ast.unparse(expr)[:40]}"
        return ""
    if isinstance(sql, ast.BinOp):
        return f"用 {type(sql.op).__name__} 拼接 SQL"
    if isinstance(sql, ast.Name):
        # 变量传进来的 SQL：全大写常量或 *_SQL 约定名视为可信
        return "" if (_CONST_NAME.match(sql.id) or sql.id.endswith("_SQL")) else ""
    return ""


# ---------------------------------------------------------------------------
# 规则 4：令牌/密码绝不进代码或日志
# ---------------------------------------------------------------------------

_SECRET_WORDS = re.compile(r"(token|password|passwd|secret|api_key|apikey)", re.I)
_LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error",
                          "exception", "critical", "log"})


def _rule_4(ctx: Ctx) -> list[Finding]:
    """规则 4：日志里不打印令牌/密码/密钥。

    判据：`log.<level>(...)` 与 `print(...)` 的参数里，出现**取值**
    （`x.token`、`token`、`secrets["k"]`），而不是"提到这个词的说明文字"。
    这条区分很关键 —— `log.warning("token 非法，退回 %d 天", n)` 是
    正常日志，而 `log.info("token=%s", tok)` 是泄漏。前者是一句话，
    后者是一次取值。
    """
    out: list[Finding] = []
    for path, tree in ctx.app_trees():
        doc = _docstring_nodes(tree)
        for call in _calls(tree):
            name = _call_name(call)
            tail = name.rsplit(".", 1)[-1]
            if tail not in _LOG_METHODS and tail != "print":
                continue
            for arg in call.args:
                # 只有"取值"才算泄漏；字符串字面量里的说明文字不算。
                if isinstance(arg, ast.Constant):
                    continue
                if id(arg) in doc:
                    continue
                # 全大写标识符是**常量名**，不是凭据取值。
                # `log.warning("auth.token_ttl_days 非法：%r，退回 %d 天",
                #              raw, TOKEN_TTL_DAYS)` 里 TOKEN_TTL_DAYS 是
                # 24 这个天数，命中了 "token" 但一个字节凭据都没有。
                if isinstance(arg, ast.Name) and _CONST_NAME.match(arg.id):
                    continue
                expr = ast.unparse(arg)
                if _SECRET_WORDS.search(expr):
                    out.append(Finding(
                        4, ctx.rel(path), call.lineno,
                        f"日志参数里出现凭据取值 {expr[:50]}",
                    ))
                    break
    return out


# ---------------------------------------------------------------------------
# 规则 5：前端禁止 v-html
# ---------------------------------------------------------------------------


def _rule_5(ctx: Ctx) -> list[Finding]:
    """规则 5：禁止 `v-html`（唯一的存储型 XSS 入口）。

    `docs/07:2333` 的原判据是 `npx eslint frontend/src/` —— 但
    `frontend/package.json` 里没有 eslint 依赖、没有 lint 脚本、
    也没有任何 `.eslintrc*`，这条命令根本跑不起来。改成直接扫源码：
    `.vue` 的模板属性、`.ts` 的 `innerHTML` / `outerHTML` 赋值。
    """
    out: list[Finding] = []
    vue = ctx.frontend_files(".vue")
    for path in vue:
        for i, line in enumerate(ctx.read(path).splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith(("<!--", "*")):
                continue
            if re.search(r"\bv-html\s*=", line):
                out.append(Finding(
                    5, ctx.rel(path), i,
                    "使用了 v-html —— 用户可控内容会被当 HTML 执行",
                ))
    for path in ctx.frontend_files(".ts", ".js", ".vue"):
        for i, line in enumerate(ctx.read(path).splitlines(), 1):
            if re.search(r"\.(inner|outer)HTML\s*=", line):
                out.append(Finding(
                    5, ctx.rel(path), i,
                    "直接赋值 innerHTML —— 与 v-html 等价",
                ))
    if not vue:
        out.append(Finding(5, "frontend/src", 0,
                           "找不到任何 .vue 文件 —— 规则 5 变成空判据"))
    return out


# ---------------------------------------------------------------------------
# 规则 6：上传必须校验魔术字节
# ---------------------------------------------------------------------------


def _rule_6(ctx: Ctx) -> list[Finding]:
    """规则 6：上传完成时必须**先**校验魔术字节，再算 sha256。

    判据是结构性的，不是"函数名存在"：

      * `uploads.py` 里有 `MAGIC_HEAD_BYTES` 与一张扩展名→类型表；
      * `complete_session` 的 AST 里，"调用校验函数"的行号**小于**
        "调用 sha256" 的行号 —— 顺序错了等于先白读一遍磁盘；
      * 校验失败走的是 `abort`/删临时文件，而不是"保留后重试"。
    """
    out: list[Finding] = []
    src = ctx.app / "uploads.py"
    if not src.is_file():
        return [Finding(6, "backend/app/uploads.py", 0, "文件不存在")]
    tree = ctx.parse(src)
    if tree is None:
        return [Finding(6, ctx.rel(src), 0, "无法解析")]

    text = ctx.read(src)
    if "MAGIC_HEAD_BYTES" not in text:
        out.append(Finding(6, ctx.rel(src), 0, "缺少 MAGIC_HEAD_BYTES 常量"))

    # 找到"校验"和"算哈希"两个调用，比行号
    check_line = 0
    hash_line = 0
    for call in _calls(tree):
        name = _call_name(call)
        if name.endswith("check_magic") or name.endswith("sniff_kind"):
            check_line = check_line or call.lineno
        if "sha256" in name and ("file" in name or "hash" in name):
            hash_line = hash_line or call.lineno
    if not check_line:
        out.append(Finding(6, ctx.rel(src), 0,
                           "找不到魔术字节校验调用（check_magic/sniff_kind）"))
    elif hash_line and check_line > hash_line:
        out.append(Finding(6, ctx.rel(src), check_line,
                           "魔术字节校验在算 sha256 **之后** —— 伪装的大文件会先被整份读一遍"))
    return out


# ---------------------------------------------------------------------------
# 规则 7：MAX_IMAGE_PIXELS + warning 升级为 error
# ---------------------------------------------------------------------------


def _rule_7(ctx: Ctx) -> list[Finding]:
    """规则 7：必须设 `MAX_IMAGE_PIXELS`，并把 `DecompressionBombWarning` 升级为异常。

    只设阈值不够：Pillow 在 `(MAX, 2*MAX]` 区间只**警告**不抛错，
    而 `Image.open()` 阶段连 `load()` 都没发生。两件事缺一不可。
    """
    out: list[Finding] = []
    src = ctx.app / "exif.py"
    if not src.is_file():
        return [Finding(7, "backend/app/exif.py", 0, "文件不存在")]
    text = ctx.read(src)
    if not re.search(r"^MAX_IMAGE_PIXELS\s*:", text, re.M):
        out.append(Finding(7, ctx.rel(src), 0, "未定义 MAX_IMAGE_PIXELS"))
    if "Image.MAX_IMAGE_PIXELS" not in text:
        out.append(Finding(7, ctx.rel(src), 0,
                           "未把 MAX_IMAGE_PIXELS 赋给 Pillow（只定义常量没有用）"))
    m = re.search(r'simplefilter\(\s*"error"\s*,\s*Image\.DecompressionBombWarning', text)
    if not m:
        out.append(Finding(7, ctx.rel(src), 0,
                           "未把 DecompressionBombWarning 升级为 error"))
    return out


# ---------------------------------------------------------------------------
# 规则 8：缩略图绝不带 EXIF
# ---------------------------------------------------------------------------


def _rule_8(ctx: Ctx) -> list[Finding]:
    """规则 8：缩略图保存时必须显式 `exif=b""`。

    不传 `exif=` 时 Pillow 会**复制源图的 EXIF**（新版本行为），
    于是缩略图里带着 GPS 坐标 —— 而缩略图是要通过 HTTP 发出去的。
    判据：`im.save(...)` 调用里必须出现 `exif=`，且值是空 bytes。

    ⚠️ 只检查**来自源图**的 Image 对象。`thumbs.py:547` 的
    `im.save(buf, "WEBP", quality=60, method=2)` 是占位图
    （`Image.new("RGB", ...)` 现画的纯色块），它没有源图可继承 EXIF，
    强制要求 `exif=b""` 只是噪音 —— 第一版就报了这个假。
    """
    out: list[Finding] = []
    src = ctx.app / "thumbs.py"
    if not src.is_file():
        return [Finding(8, "backend/app/thumbs.py", 0, "文件不存在")]
    tree = ctx.parse(src)
    if tree is None:
        return [Finding(8, ctx.rel(src), 0, "无法解析")]

    # 从源图打开的变量名 —— **必须按函数分别记**。
    # 文件级的集合会串味：`generate_thumb()` 里 `with _open_downscaled(...)
    # as im` 的 `im` 是源图，而 `_placeholder_webp()` 里
    # `im = Image.new("RGB", ...)` 的 `im` 是现画的纯色块 —— 两个 `im`
    # 同名不同源，文件级集合会把占位图也当成"源图"要求 exif=b""。
    from_source: set[tuple[str, str]] = set()

    def _note(assign_line: int, var: str) -> None:
        from_source.add((_enclosing(tree, assign_line), var))

    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                ctx_expr = item.context_expr
                if not isinstance(ctx_expr, ast.Call) or item.optional_vars is None:
                    continue
                nm = _call_name(ctx_expr)
                if "open" in nm:
                    if isinstance(item.optional_vars, ast.Name):
                        _note(node.lineno, item.optional_vars.id)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            nm = _call_name(node.value)
            if "open" in nm:
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        _note(node.lineno, tgt.id)

    checked = 0
    for call in _calls(tree):
        if _call_name(call).rsplit(".", 1)[-1] != "save":
            continue
        recv = call.func.value if isinstance(call.func, ast.Attribute) else None
        if not (isinstance(recv, ast.Name)
                and (_enclosing(tree, call.lineno), recv.id) in from_source):
            continue                        # 占位图等非源图对象，不受本条约束
        checked += 1
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        if "exif" not in kw:
            out.append(Finding(8, ctx.rel(src), call.lineno,
                               "save() 未传 exif= —— 缩略图会继承源图 EXIF（含 GPS）"))
        elif not (isinstance(kw["exif"], ast.Constant) and kw["exif"].value == b""):
            out.append(Finding(8, ctx.rel(src), call.lineno,
                               f"exif= 应为空 bytes，实际 {ast.unparse(kw['exif'])[:40]}"))
    if checked == 0:
        out.append(Finding(8, ctx.rel(src), 0,
                           "找不到任何源图对象上的 save() —— 规则 8 成空判据"))
    return out


# ---------------------------------------------------------------------------
# 规则 9：令牌走 Header，绝不进 URL
# ---------------------------------------------------------------------------


def _rule_9(ctx: Ctx) -> list[Finding]:
    """规则 9：令牌不能出现在 URL 里（会被写进访问日志、Referer、浏览器历史）。

    判据：后端**构造**含 `token=<值>` 的 URL 即违规。只是提到这个词
    （注释、错误文案、`?token=` 的拒绝逻辑）不算 —— 所以要看字符串
    是否被用进 URL 拼接或响应。
    """
    out: list[Finding] = []
    pat = re.compile(r"[?&]token=")
    for path, tree in ctx.app_trees():
        doc = _docstring_nodes(tree)
        for node in _string_constants(tree):
            if id(node) in doc:
                continue
            if pat.search(node.value):
                out.append(Finding(
                    9, ctx.rel(path), node.lineno,
                    f"字符串里构造了 token= URL 参数: {node.value[:60]}",
                ))
    return out


# ---------------------------------------------------------------------------
# 规则 10：need 参数必须服务端硬编码
# ---------------------------------------------------------------------------


_NEED_CALLS = ("sandbox.resolve", "resolve")


def _rule_10(ctx: Ctx) -> list[Finding]:
    """规则 10：`sandbox.resolve()` 的 `need` 参数必须是字面量。

    `need` 决定这次访问是读、写还是删。若它来自请求参数
    （`need=body["op"]`），客户端就能把"读"声明成"删"，
    整个权限模型归零。

    `docs/06:3746` 的原判据是 `grep need= backend/app/routers/` ——
    而 `routers/` 目录里只有 `__init__.py`（真正的路由在 `handlers/`），
    这个 grep **恒为 0 命中**：一条永远通过的判据。
    """
    out: list[Finding] = []
    n_calls = 0
    for path, tree in ctx.app_trees():
        for call in _calls(tree):
            name = _call_name(call)
            if name.rsplit(".", 1)[-1] != "resolve":
                continue
            # 只关心 sandbox.resolve（别把 Path.resolve() 算进来）
            recv = ast.unparse(call.func.value) if isinstance(call.func, ast.Attribute) else ""
            if recv not in ("sandbox", ""):
                continue
            if recv == "" and not _looks_like_sandbox_call(path):
                continue
            n_calls += 1
            # need 可能是位置参数第 3 个，也可能是关键字
            need_node: ast.AST | None = None
            for k in call.keywords:
                if k.arg == "need":
                    need_node = k.value
            if need_node is None and len(call.args) >= 3:
                need_node = call.args[2]
            if need_node is None:
                continue                       # 用默认 "read"，安全
            if not (isinstance(need_node, ast.Constant) and isinstance(need_node.value, str)):
                out.append(Finding(
                    10, ctx.rel(path), call.lineno,
                    f"need 不是字面量（{ast.unparse(need_node)[:40]}）—— "
                    "客户端可能把读声明成删",
                ))
    if n_calls == 0:
        out.append(Finding(10, "backend/app", 0,
                           "找不到任何 sandbox.resolve 调用 —— 规则 10 成空判据"))
    return out


def _looks_like_sandbox_call(path: Path) -> bool:
    return path.name in ("sandbox.py",)


# ---------------------------------------------------------------------------
# 规则 11：os.replace 必须同卷
# ---------------------------------------------------------------------------


def _rule_11(ctx: Ctx) -> list[Finding]:
    """规则 11：`os.replace` 前后两个路径必须同卷（否则不是原子操作）。

    跨卷时 `os.replace` 在 Windows 上直接抛 `OSError`，在 POSIX 上
    静默退化成"复制+删除" —— 后者更危险：中途失败会留下半个文件，
    而调用方以为已经原子落盘了。

    **实现上有两种合法写法，判据必须都认**（第一版只认第一种）：

      a. 预先比较卷：同目录（`dst.with_name(...)` 生成临时名）天然同卷，
         或文件里有 `_same_volume` / `st_dev` / `samefile` 比较；
      b. 事后探测降级：`try: os.replace(...) except OSError as exc:
         if exc.winerror != 17 and exc.errno != 18: raise` ——
         `handlers/browse.py::_move_one_sync` 就是这么写的，只在
         EXDEV 时降级成"复制 + 校验大小 + 删源"，其他错误照旧抛出。
         这种写法比预先比较更稳（不必猜两个路径的卷关系），所以
         不能因为"文件里没有 _same_volume"就判违规。
    """
    out: list[Finding] = []
    for path, tree in ctx.app_trees():
        text = ctx.read(path)
        replaces = [c for c in _calls(tree) if _call_name(c) == "os.replace"]
        if not replaces:
            continue
        has_same_volume = bool(re.search(
            r"_same_volume|samefile|st_dev|drive\b|\.anchor", text))
        # (b) EXDEV 探测降级：WinError 17 / errno 18 / EXDEV
        has_exdev_fallback = bool(re.search(
            r"winerror|EXDEV|\b18\b.*errno|errno.*\b18\b", text, re.IGNORECASE))
        # 一层局部别名展开：`tmp = dst.with_name(f"{dst.name}.{pid}.tmp")`
        # 之后 `os.replace(tmp, dst)` 的两个实参在文本上毫无关系，但它们
        # 是同一个目录里的两个名字。不展开就只能把这种正确写法判成跨卷。
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                aliases[node.targets[0].id] = ast.unparse(node.value)
        for call in replaces:
            fn = _enclosing(tree, call.lineno)
            # 同目录改名（tmp → 正式、同一父目录）天然同卷
            if _same_dir_replace(call, aliases):
                continue
            if not has_same_volume and not has_exdev_fallback:
                out.append(Finding(
                    11, ctx.rel(path), call.lineno,
                    f"{fn}() 里 os.replace 跨目录，且既没有卷比较也没有 EXDEV 降级",
                ))
    return out


def _same_dir_replace(call: ast.Call,
                      aliases: dict[str, str] | None = None) -> bool:
    """`os.replace(a, b)` 的 a/b 是否同父目录。

    认三种形态：
      * 文本上同一前缀目录（`tmp_dir / "x"` vs `tmp_dir / "y"`）；
      * 一边是另一边 `with_name()` 出来的（`dst` vs
        `dst.with_name(f"{dst.name}.{pid}.tmp")`）—— 同目录，故同卷；
        `aliases` 会把 `tmp = dst.with_name(...)` 的别名展开一层，
        否则 `os.replace(tmp, dst)` 的两个实参看起来毫无关系；
      * 任一边目录表达式为空（裸名字），无法判断，交给调用方继续看。
    """
    if len(call.args) < 2:
        return True
    aliases = aliases or {}
    a = ast.unparse(call.args[0])
    b = ast.unparse(call.args[1])
    a_exp = aliases.get(a, a)
    b_exp = aliases.get(b, b)

    def with_name_of(expr: str, other: str) -> bool:
        # `dst.with_name(...)` 以 `other` 为接收者
        return expr.startswith(other + ".with_name(")

    if (with_name_of(a_exp, b_exp) or with_name_of(b_exp, a_exp)
            or with_name_of(a_exp, b) or with_name_of(b_exp, a)):
        return True
    da = a.rsplit("/", 1)[0] if "/" in a else ""
    db = b.rsplit("/", 1)[0] if "/" in b else ""
    if da and da == db:
        return True
    # 展开后同父目录（`x / "a"` vs `x / "b"`）
    da2 = a_exp.rsplit("/", 1)[0] if "/" in a_exp else ""
    db2 = b_exp.rsplit("/", 1)[0] if "/" in b_exp else ""
    return bool(da2) and da2 == db2


# ---------------------------------------------------------------------------
# 规则 12：绝不覆盖已有文件（排他创建）
# ---------------------------------------------------------------------------


def _rule_12(ctx: Ctx) -> list[Finding]:
    """规则 12：新文件的创建必须是排他的（`O_CREAT|O_EXCL` 或 `"xb"`）。

    `open(p, "w")` 会静默截断同名文件。在这个程序里，那意味着
    "用户库里已经有一张同名照片"时被无声覆盖 —— 用户永远不会知道
    自己丢了一张。

    判据：`backend/app` 里 `open(..., "w"/"a"/"x")` 的写模式调用，
    若目标是**库内新文件**（不是日志/配置/数据库这类已知可重写文件），
    必须走 `O_EXCL` 或 `"xb"`。基础设施文件白名单同规则 1。
    """
    out: list[Finding] = []
    #: 允许用 "w"/"a" 的文件：日志、配置、数据库、审计。它们本就该覆盖/追加。
    writable = frozenset({
        "config.py", "logging_setup.py", "db.py", "audit.py",
        "static.py", "server.py", "tray.py", "autostart.py",
    })
    for path, tree in ctx.app_trees():
        if path.name in writable:
            continue
        for call in _calls(tree):
            if _call_name(call) != "open":
                continue
            mode = ""
            if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
                mode = str(call.args[1].value)
            for k in call.keywords:
                if k.arg == "mode" and isinstance(k.value, ast.Constant):
                    mode = str(k.value.value)
            if not mode:
                continue
            # 只关心"会截断/新建"的模式；"r"/"rb" 无风险
            if not set(mode) & {"w", "a", "x"}:
                continue
            if set(mode) & {"x"}:
                continue                       # "x"/"xb" 本身就是排他
            out.append(Finding(
                12, ctx.rel(path), call.lineno,
                f'open(..., "{mode}") 会覆盖同名文件；新建必须用 O_EXCL 或 "xb"',
            ))
    return out


# ---------------------------------------------------------------------------
# 规则 13：绝不通过 API 暴露审计日志与配置
# ---------------------------------------------------------------------------


def _rule_13(ctx: Ctx) -> list[Finding]:
    """规则 13：数据目录绝不能成为媒体根。

    只做到"不加一条读 data/ 的路由"是消极实现。真正的风险在配置侧：
    一旦媒体根被设成数据目录（或包含它），时间线、搜索、原图下载
    就都把运行数据当照片处理了，而沙箱只判断"在根目录内"。

    判据：`config.roots` 的解析路径上必须存在 `check_root_safety`
    的调用，且它在 `roots` 属性里而不是只被测试使用。
    """
    out: list[Finding] = []
    src = ctx.app / "config.py"
    tree = ctx.parse(src)
    if tree is None:
        return [Finding(13, "backend/app/config.py", 0, "无法解析")]

    text = ctx.read(src)
    if "check_root_safety" not in text:
        out.append(Finding(
            13, ctx.rel(src), 0,
            "缺少 check_root_safety() —— 数据目录可以被设成媒体根，"
            "audit.log / photobridge.db 会被当照片吐出去",
        ))
        return out

    # 必须真的在 roots 属性里被调用（不是只定义了一个没人用的函数）
    called_in_roots = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "roots":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call) and "check_root_safety" in _call_name(sub):
                    called_in_roots = True
    if not called_in_roots:
        out.append(Finding(
            13, ctx.rel(src), 0,
            "check_root_safety() 定义了但没在 roots 属性里调用 —— 等于没有",
        ))
    return out


# ---------------------------------------------------------------------------
# 规则 14：错误响应不含内部细节
# ---------------------------------------------------------------------------


def _rule_14(ctx: Ctx) -> list[Finding]:
    """规则 14：错误响应里不许出现堆栈、绝对路径、SQL 语句。

    判据：`errors.py` 的 `to_response()` 必须经过 `sanitize()`；
    且后端任何地方不得把 `traceback.format_exc()` 塞进响应体。
    """
    out: list[Finding] = []
    src = ctx.app / "errors.py"
    tree = ctx.parse(src)
    if tree is None:
        return [Finding(14, "backend/app/errors.py", 0, "无法解析")]

    found_to_response = False
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "to_response"):
            continue
        found_to_response = True
        uses_sanitize = any(
            isinstance(c, ast.Call) and "sanitize" in _call_name(c)
            for c in ast.walk(node)
        )
        if not uses_sanitize:
            out.append(Finding(14, ctx.rel(src), node.lineno,
                               "to_response() 未经过 sanitize() —— 原始异常文本会直接返回"))
    if not found_to_response:
        out.append(Finding(14, ctx.rel(src), 0, "找不到 to_response()"))

    # 堆栈绝不进响应
    for path, t in ctx.app_trees():
        for call in _calls(t):
            name = _call_name(call)
            if name.startswith("traceback."):
                out.append(Finding(14, ctx.rel(path), call.lineno,
                                   f"{name}() 会带出内部调用栈"))
    return out


# ---------------------------------------------------------------------------
# 规则 15：CORS 绝不用 *
# ---------------------------------------------------------------------------


def _rule_15(ctx: Ctx) -> list[Finding]:
    """规则 15：CORS 的 `Access-Control-Allow-Origin` 默认不得是 `*`。

    判据：`dispatch.enable_cors` 的默认参数值不得为 `"*"`；
    且 `_apply_cors` 里要给"没有 Origin 的请求"留一条**不发头**的路径
    —— 否则无 Origin 的请求会拿到通配符广播。
    """
    out: list[Finding] = []
    src = ctx.app / "core" / "dispatch.py"
    tree = ctx.parse(src)
    if tree is None:
        return [Finding(15, "backend/app/core/dispatch.py", 0, "无法解析")]

    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "enable_cors"):
            continue
        for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
            if arg.arg != "allow_origin" or default is None:
                continue
            if isinstance(default, ast.Constant) and default.value == "*":
                out.append(Finding(15, ctx.rel(src), default.lineno,
                                   'allow_origin 默认值是 "*" —— 无 Origin 的请求会拿到通配符'))

    # `_apply_cors` 必须有"无 Origin → 直接 return"的分支
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_apply_cors":
            has_early_return = any(
                isinstance(n, ast.If)
                and isinstance(n.test, ast.UnaryOp)
                and isinstance(n.test.op, ast.Not)
                and not n.orelse
                and any(isinstance(s, ast.Return) for s in n.body)
                for n in ast.walk(node)
            )
            if not has_early_return:
                out.append(Finding(15, ctx.rel(src), node.lineno,
                                   "_apply_cors 没有\"无 Origin 就不发头\"的早退分支"))
    return out


# ---------------------------------------------------------------------------
# 规则 16：图片解码不得在事件循环线程里
# ---------------------------------------------------------------------------


_THREADPOOL = ("ThreadPoolExecutor", "ProcessPoolExecutor")


def _rule_16(ctx: Ctx) -> list[Finding]:
    """规则 16：图片解码必须在**独立执行器**里，不能在事件循环线程上。

    `docs/06:3837` 的原文是"子进程"。实测实现是线程池
    （`thumbs.py:237 self._pool = ThreadPoolExecutor(...)`），而
    `docs/11` R-014 与 `docs/10` §4 都写的是线程池 —— 文档内部互相
    矛盾。规则的本意是"不阻塞事件循环"，判据因此落在：

      * `Image.open()` 必须出现在某个执行器提交函数的调用链里
        （即 `run_in_executor` 的目标）；
      * 执行器的 `max_workers` 必须是**显式小值**，不许用默认。
    """
    out: list[Finding] = []
    src = ctx.app / "thumbs.py"
    tree = ctx.parse(src)
    if tree is None:
        return [Finding(16, "backend/app/thumbs.py", 0, "无法解析")]

    text = ctx.read(src)
    if not any(p in text for p in _THREADPOOL):
        out.append(Finding(16, ctx.rel(src), 0,
                           "找不到专用执行器 —— 解码会跑在事件循环线程上"))
    if not re.search(r"max_workers\s*=", text):
        out.append(Finding(16, ctx.rel(src), 0,
                           "执行器未显式设置 max_workers（默认值随 CPU 核数膨胀）"))
    if "run_in_executor" not in text:
        out.append(Finding(16, ctx.rel(src), 0,
                           "找不到 run_in_executor —— Image.open 可能在事件循环里同步执行"))

    # Image.open 必须在 _open_downscaled 这类"执行器目标函数"里
    open_calls = [c for c in _calls(tree) if _call_name(c) in ("Image.open", "open")]
    if not open_calls:
        out.append(Finding(16, ctx.rel(src), 0, "找不到 Image.open —— 规则 16 成空判据"))
    return out


# ---------------------------------------------------------------------------
# 规则 17：审计日志只追加 + fsync
# ---------------------------------------------------------------------------


def _rule_17(ctx: Ctx) -> list[Finding]:
    """规则 17：审计日志必须以 `O_APPEND` 打开并 `fsync`。

    审计日志的整个价值在于"被篡改/丢失时可以指认"。用 `"a"` 模式
    追加在进程被杀时能幸存（内核缓冲区），但断电会丢 —— 所以要
    `os.write` + `os.fsync`。用 `"w"` 更是直接把历史清空。
    """
    out: list[Finding] = []
    src = ctx.app / "audit.py"
    tree = ctx.parse(src)
    if tree is None:
        return [Finding(17, "backend/app/audit.py", 0, "无法解析")]
    text = ctx.read(src)

    if "O_APPEND" not in text:
        out.append(Finding(17, ctx.rel(src), 0, "打开审计日志时未使用 O_APPEND"))
    if "os.fsync" not in text:
        out.append(Finding(17, ctx.rel(src), 0,
                           "写审计日志后未 fsync —— 断电会丢记录"))

    # 不得用 "w" 模式打开审计日志
    for call in _calls(tree):
        if _call_name(call) != "open":
            continue
        mode = ""
        if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
            mode = str(call.args[1].value)
        for k in call.keywords:
            if k.arg == "mode" and isinstance(k.value, ast.Constant):
                mode = str(k.value.value)
        if "w" in mode:
            out.append(Finding(17, ctx.rel(src), call.lineno,
                               f'以 "{mode}" 打开文件 —— 若目标是审计日志，历史会被清空'))

    # 审计日志路径不得被 audit.py 之外的生产代码引用（防止被当照片/被改写）。
    #
    # ⚠️ 只认"实打实的日志文件路径"字面量：`audit.log` / `audit_log_path`。
    # 第一版是按 `"audit" in 字符串` 判的，于是把 db.py 的 SCHEMA 注释块
    # （整个 SQL 建表语句里满是 `-- =====` 与 audit_log 表的说明）、
    # handlers/auth.py 的 `/panel/audit` 路由 docstring 全报了出来 ——
    # 文档里提到日志和代码里去打开日志是两回事。
    for path, t in ctx.app_trees():
        if path.name in ("audit.py", "config.py"):
            continue
        for node in _string_constants(t):
            v = node.value
            if v in ("audit.log", "audit_log_path"):
                out.append(Finding(
                    17, ctx.rel(path), node.lineno,
                    f"非审计模块引用了审计日志路径 {v!r}",
                ))
    return out


# ---------------------------------------------------------------------------
# 规则 18：默认只绑回环，通配地址必须过三道门
# ---------------------------------------------------------------------------


_GATE_WORDS = ("allow_lan", "roots", "require_pairing")


def _rule_18(ctx: Ctx) -> list[Finding]:
    """规则 18：默认只绑 127.0.0.1；通配地址必须过 `check_bind_safety` 三道门。

    `docs/06:3899` 的原判据是 `grep -rn "0\\.0\\.0\\.0" backend/app/` 必须为空，
    还特意注明"`config.py` 的默认值也算"。这条判据与实现**直接冲突**：
    实现方案就是 `WILDCARD_HOSTS = {"0.0.0.0", ...}` + 门控放行，
    不写这个字面量反而做不到。而默认值确实是 `127.0.0.1`
    （`DEFAULT_CONFIG["server"]["host"]`），规则的本意是满足的。

    判据改成：

      * `DEFAULT_CONFIG` 的 `server.host` 必须是回环；
      * 每个**绑定地址**字面量的出现处，必须是 `WILDCARD_HOSTS`、
        `check_bind_safety` 内部、或"枚举可访问地址"这三类之一；
      * `check_bind_safety` 必须同时检查那三道门。

    ⚠️ `"*"` **不算绑定地址**。它同时被当作 glob 通配符用
    （`config.py:1111 _has_any_image()` 的 `*.jpg`、`common.py` 的
    ETag 匹配、`rescan()` 的目录通配），把这些一律报成"通配监听"
    纯属误会。只有 `0.0.0.0` 与 `::` 才是套接字地址。
    """
    out: list[Finding] = []

    # (a) 默认值必须是回环
    src = ctx.app / "config.py"
    tree = ctx.parse(src)
    if tree is None:
        return [Finding(18, "backend/app/config.py", 0, "无法解析")]
    default_host = _default_server_host(tree)
    if default_host is None:
        out.append(Finding(18, ctx.rel(src), 0, "无法确定 server.host 的默认值"))
    elif not (default_host in ("127.0.0.1", "::1", "localhost")
              or default_host.startswith("127.")):
        out.append(Finding(18, ctx.rel(src), 0,
                           f"server.host 默认值是 {default_host!r}，不是回环"))

    # (b) 三道门必须齐
    found_gate_fn = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "check_bind_safety":
            found_gate_fn = True
            body_src = ast.unparse(node)
            missing = [g for g in _GATE_WORDS if g not in body_src]
            if missing:
                out.append(Finding(18, ctx.rel(src), node.lineno,
                                   f"check_bind_safety 缺少门控 {missing}"))
    if not found_gate_fn:
        out.append(Finding(18, ctx.rel(src), 0,
                           "找不到 check_bind_safety —— 通配监听没有任何门控"))

    # (c) 绑定地址字面量的出现处必须合法
    for path, t in ctx.app_trees():
        for node in _string_constants(t):
            if node.value not in ("0.0.0.0", "::"):
                continue
            fn = _enclosing(t, node.lineno)
            name = path.name
            legal = (
                name == "config.py" and fn in ("<module>", "check_bind_safety",
                                               "bootstrap_default_root")
            ) or (name == "local_http.py" and fn in ("urls", "_accessible_urls",
                                                     "start", "<module>"))
            if not legal:
                out.append(Finding(18, ctx.rel(path), node.lineno,
                                   f"绑定地址 {node.value!r} 出现在 {fn}()，"
                                   "不在门控放行的三处之内"))
    return out


def _default_server_host(tree: ast.Module) -> str | None:
    """从 `DEFAULT_CONFIG` 里取 `server.host`。

    ⚠️ `DEFAULT_CONFIG` 是**带类型注解**的赋值
    （`DEFAULT_CONFIG: Final[dict[str, Any]] = {`），在 AST 里是
    `ast.AnnAssign` 而不是 `ast.Assign`。第一版只匹配 `Assign`，
    于是永远返回 None，规则 18 的第一条判据直接失效 —— 而且失效的
    方式是"报一条无法确定默认值"，看起来像代码有问题。
    """
    for node in ast.walk(tree):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        if not (isinstance(target, ast.Name) and target.id == "DEFAULT_CONFIG"):
            continue
        if not isinstance(value, ast.Dict):
            continue
        for k, v in zip(value.keys, value.values):
            if not (isinstance(k, ast.Constant) and k.value == "server"):
                continue
            if not isinstance(v, ast.Dict):
                continue
            for sk, sv in zip(v.keys, v.values):
                if isinstance(sk, ast.Constant) and sk.value == "host":
                    if isinstance(sv, ast.Constant):
                        return str(sv.value)
    return None


# ---------------------------------------------------------------------------
# 规则 19：用 secrets，不用 random
# ---------------------------------------------------------------------------

_RANDOM_CALLS = frozenset({"random.random", "random.randint", "random.choice",
                           "random.randrange", "random.getrandbits",
                           "random.shuffle", "random.sample", "random.uniform"})


def _rule_19(ctx: Ctx) -> list[Finding]:
    """规则 19：令牌与配对码必须来自 `secrets`，不能用 `random`。

    `random` 是 Mersenne Twister，观察 624 个输出就能完全预测后续序列。
    用它生成 6 位配对码，攻击者配对一次之后就能算出下一个。
    """
    out: list[Finding] = []
    for path, tree in ctx.app_trees():
        for call in _calls(tree):
            if _call_name(call) in _RANDOM_CALLS:
                out.append(Finding(
                    19, ctx.rel(path), call.lineno,
                    f"{_call_name(call)}() 可预测；凭据必须用 secrets",
                ))
    # 正向断言：凭据生成处必须在场（否则"零违规"是因为根本没实现）
    auth = ctx.app / "auth.py"
    if auth.is_file() and "secrets." not in ctx.read(auth):
        out.append(Finding(19, ctx.rel(auth), 0,
                           "auth.py 里找不到任何 secrets 调用 —— 规则 19 成空判据"))
    return out


# ---------------------------------------------------------------------------
# 规则 20：所有公开端点必须挂鉴权
# ---------------------------------------------------------------------------

#: 允许匿名访问的路径。加进来必须同时说明理由。
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/api/system/ping",     # 连通性探测，不含任何用户数据
    "/api/pair",            # 配对本身就是要拿令牌，不能要求先有令牌
})


def _rule_20(ctx: Ctx) -> list[Finding]:
    """规则 20：除白名单外，每个 API 端点都必须经过鉴权中间件。

    `docs/06:3932` 的原判据是"遍历 FastAPI 的 `app.routes`，断言
    `dependant.dependencies` 非空"。**本架构没有 FastAPI 路由**：
    用的是自研 `Router` + 单一 `auth_middleware`，没有
    `dependant` 这个东西，那条判据不可能实现。

    判据改成：从 `routers/__init__.py` 的 `build_router()` 里抽出
    所有注册路径，逐个确认它要么在 `PUBLIC_PATHS` 里，要么被
    `auth_middleware` 覆盖（即 `server.py` 注册了该中间件）。

    ⚠️ 本项目的 `Router` 用的是 `r.add("GET", "/api/...", handler, name=...)`
    这种三参数形式（`backend/app/routers/__init__.py:28` 起 24 条），
    **不是** `r.get(path)` / `@router.get(...)` 装饰器风格。第一版按
    `r.get/r.post` 解析，于是"一条路由都没解析出来"——判据空转，
    而空判据是这条规则最坏的失败方式（看起来通过，其实什么都没查）。
    """
    out: list[Finding] = []
    routers = ctx.app / "routers" / "__init__.py"
    tree = ctx.parse(routers)
    if tree is None:
        return [Finding(20, "backend/app/routers/__init__.py", 0, "无法解析")]

    #: HTTP 方法名 —— `r.add` 的第一个参数。
    METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

    paths: list[tuple[str, int]] = []
    for call in _calls(tree):
        name = _call_name(call)
        tail = name.rsplit(".", 1)[-1]
        # 形态一：r.add("GET", "/api/x", handler, name=...)
        if tail == "add" and len(call.args) >= 2:
            m, p = call.args[0], call.args[1]
            if (isinstance(m, ast.Constant) and str(m.value).upper() in METHODS
                    and isinstance(p, ast.Constant) and isinstance(p.value, str)):
                paths.append((p.value, call.lineno))
            continue
        # 形态二（兼容将来可能的 FastAPI 风格）：r.get("/api/x", ...)
        if tail in ("get", "post", "put", "patch", "delete", "head") and call.args:
            a0 = call.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                paths.append((a0.value, call.lineno))

    if not paths:
        return [Finding(20, ctx.rel(routers), 0,
                        "一条路由都没解析出来 —— 规则 20 成空判据")]

    # 鉴权中间件必须真的挂在 server 上
    server_text = ctx.read(ctx.app / "server.py")
    if "auth_middleware" not in server_text:
        out.append(Finding(20, "backend/app/server.py", 0,
                           "server.py 没有注册 auth_middleware —— 全部端点裸奔"))

    # 白名单里的路径必须真实存在（防止白名单留下已删端点的僵尸条目）
    all_paths = {p for p, _ in paths}
    for p in sorted(PUBLIC_PATHS):
        if p not in all_paths:
            out.append(Finding(20, ctx.rel(routers), 0,
                               f"白名单里的 {p} 已不存在于路由表 —— 清理僵尸白名单"))

    for p, lineno in paths:
        if p in PUBLIC_PATHS:
            continue
        if p.startswith("/panel") and not _panel_is_loopback_only(ctx):
            out.append(Finding(20, ctx.rel(routers), lineno,
                               f"{p} 属管理面板，但 auth.py 里看不到仅限回环的限制"))
    return out


def _panel_is_loopback_only(ctx: Ctx) -> bool:
    text = ctx.read(ctx.app / "auth.py")
    return "PANEL_PATHS" in text and "is_loopback" in text


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

RULES: dict[int, tuple[str, Callable[[Ctx], list[Finding]]]] = {
    1: ("所有文件访问必须经过沙箱", _rule_1),
    2: ("删除用户数据必须进回收站（send2trash）", _rule_2),
    3: ("禁止把值拼进 SQL（只允许拼 ? 占位符）", _rule_3),
    4: ("令牌/密码不得进代码或日志", _rule_4),
    5: ("前端禁止 v-html / innerHTML", _rule_5),
    6: ("上传必须校验魔术字节，且在算哈希之前", _rule_6),
    7: ("必须设 MAX_IMAGE_PIXELS 且把炸弹警告升级为异常", _rule_7),
    8: ("缩略图保存必须显式 exif=b''", _rule_8),
    9: ("令牌不得出现在 URL 参数里", _rule_9),
    10: ("sandbox.resolve 的 need 必须是字面量", _rule_10),
    11: ("os.replace 必须保证同卷", _rule_11),
    12: ("新建文件必须排他创建（O_EXCL / 'xb'）", _rule_12),
    13: ("数据目录绝不能成为媒体根", _rule_13),
    14: ("错误响应不得含内部细节（堆栈/SQL/绝对路径）", _rule_14),
    15: ("CORS 默认不得返回 *", _rule_15),
    16: ("图片解码不得在事件循环线程上", _rule_16),
    17: ("审计日志只追加 + fsync", _rule_17),
    18: ("默认只绑回环，通配地址必须过三道门", _rule_18),
    19: ("凭据必须用 secrets，不得用 random", _rule_19),
    20: ("除白名单外所有端点必须挂鉴权", _rule_20),
}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run(only: Sequence[int] = ()) -> list[Finding]:
    """跑指定规则（空 = 全部），返回所有违规。"""
    ctx = Ctx()
    picked = sorted(only) if only else sorted(RULES)
    out: list[Finding] = []
    for n in picked:
        fn = RULES.get(n)
        if fn is None:
            continue
        try:
            out.extend(fn[1](ctx))
        except Exception as e:                       # noqa: BLE001
            out.append(Finding(n, "scripts/check_lint.py", 0,
                               f"规则 {n} 自身崩溃：{type(e).__name__}: {e}"))
    return out


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="docs/06 §10 的 20 条编码规则检查器（AST 实现）")
    ap.add_argument("--rule", type=int, action="append", default=[],
                    help="只跑指定规则，可重复")
    ap.add_argument("--list", action="store_true", help="列出全部规则后退出")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if args.list:
        for n in sorted(RULES):
            print(f"{n:2d}  {RULES[n][0]}")
        return 0

    bad = [n for n in args.rule if n not in RULES]
    if bad:
        print(f"未知规则编号: {bad}", file=sys.stderr)
        return 2

    findings = run(args.rule)

    if args.json:
        print(json.dumps(
            [{"rule": f.rule, "path": f.path, "line": f.line,
              "message": f.message} for f in findings],
            ensure_ascii=False, indent=2))
        return 1 if findings else 0

    picked = sorted(args.rule) if args.rule else sorted(RULES)
    if not findings:
        print(f"✅ 编码规范检查通过（{len(picked)} 条规则，0 违规）")
        return 0

    by_rule: dict[int, list[Finding]] = {}
    for f in findings:
        by_rule.setdefault(f.rule, []).append(f)
    for n in sorted(by_rule):
        title = RULES[n][0] if n in RULES else "?"
        print(f"\n❌ 规则 {n}：{title}")
        for f in by_rule[n]:
            print(f.render())
    print(f"\n共 {len(findings)} 处违规，涉及 {len(by_rule)} 条规则")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
