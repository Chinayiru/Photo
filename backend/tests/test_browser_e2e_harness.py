"""`scripts/verify_browser_e2e.py` 的装置自愈能力（静态断言）。

## 为什么需要这个文件

这个脚本的价值全在"它在真实浏览器里跑"。装置自己出问题时最危险的结果
不是红，是**假绿**：

* `[4q]` 扫码自动配对那一步，`CDP Page.navigate` 偶发**永久不回** —— 实测
  （`build/_probe_e2e_hang2.py`）目标标签页发出 `Page.frameStartedNavigating`
  之后就再没有下文，而同一时刻服务端 0.01 秒回了 200、同浏览器另外 5 个
  同源标签页 `evaluate` 全部正常、`Page.getNavigationHistory` 里根本没有那条
  URL（导航从未提交）。那是**那一个标签页**的命运，不是应用的行为。没有
  自愈时整轮跑挂在等待里，什么都验证不到。
* 自愈的代价是"换一个标签页"。而 `Emulation.setDeviceMetricsOverride`、
  `Emulation.setTouchEmulationEnabled`、`Page.addScriptToEvaluateOnNewDocument`
  都是**按标签页**生效的。恢复后不重放的话，后面的断言会以桌面视口、
  无触摸、且 `window.__errors` 根本不存在（读成 `undefined` → 数组为空）的
  页面上继续跑 —— 每一条都"通过"，但没有一条在验证真实手机形态。

第二条尤其隐蔽：它不是报错，是断言对象消失。所以这里不看"恢复逻辑写了
没有"，而是钉住"**那些按标签页生效的设置一律走 `remember()`**"。

## 为什么是 AST 而不是 grep

`scripts/verify_browser_e2e.py` 的注释里大量讨论这些 CDP 方法名（包括
讨论"为什么不能用 `send()`"）。文本匹配会被自己的注释骗到，AST 只看得见
真实的调用。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

SCRIPT = REPO / "scripts" / "verify_browser_e2e.py"

# 只有这些是"按标签页生效、换标签页就丢"的。`Network.*`、`Runtime.*`
# 之类同样按标签页生效，但它们在脚本里是在需要时才现设的（限速、断网），
# 且都在自愈之后重新执行，不属于"设一次用到底"的持久设置。
DURABLE_METHODS = frozenset({
    "Emulation.setDeviceMetricsOverride",
    "Emulation.setTouchEmulationEnabled",
    "Page.addScriptToEvaluateOnNewDocument",
})


def _tree() -> ast.Module:
    return ast.parse(SCRIPT.read_text(encoding="utf-8"))


def _attr_chain(node: ast.AST) -> str:
    """把 `a.b.c` 还原成 "a.b.c"（只处理纯属性链）。"""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    return ".".join(reversed(parts))


def _call_name(call: ast.Call) -> str:
    return _attr_chain(call.func)


def test_per_tab_emulation_goes_through_remember() -> None:
    """所有按标签页生效的设置必须走 `remember()`，不能走 `send()`。

    `remember()` 会把它记进 `_durable`，换标签页后由 `_replay_durable()`
    重新执行。直接 `send()` 的那些在 `recover()` 之后就不存在了。
    """
    offenders: list[str] = []
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        # 形如 `br.send("Emulation.setDeviceMetricsOverride", ...)`
        if not name.endswith(".send"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value in DURABLE_METHODS:
                    offenders.append(f"{arg.value} 用了 send() 而非 remember()")
    assert not offenders, (
        "换标签页后会静默丢失这些设置（断言对象消失，表现为假绿）：\n  "
        + "\n  ".join(sorted(set(offenders))))


def test_remember_actually_records_into_durable() -> None:
    """`remember()` 必须真的写进 `_durable`，否则等于普通 `send()`。"""
    src = SCRIPT.read_text(encoding="utf-8")
    tree = _tree()
    found: ast.FunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "remember":
            found = node
            break
    assert found is not None, "`Browser.remember()` 不见了"
    body = ast.get_source_segment(src, found) or ""
    assert "_durable.append" in body, (
        "`remember()` 没有把设置记进 `_durable` —— 换标签页后不会重放")
    assert "continue" not in body or "_durable" in body


def _func(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    return None


def _calls_in(node: ast.AST, method: str) -> list[ast.Call]:
    """`node` 里所有 `<something>.<method>(...)` 调用。

    ★ 必须走 AST：这些函数的 body 注释与 docstring 里大量讨论方法名
    （`goto()` 的注释就写着"详见 `recover()` 的注释"）。文本匹配会被
    自己的注释骗过 —— 本次变异验证实测到过：把自愈整段删掉之后，
    `"recover(" in body` 仍然为真。
    """
    out: list[ast.Call] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and _attr_chain(sub.func).endswith(
                "." + method):
            out.append(sub)
    return out


def test_recover_replays_injects_into_live_document() -> None:
    """恢复出来的标签页要补注入一次性脚本。

    `Page.addScriptToEvaluateOnNewDocument` 只对**之后**的文档生效，而
    `recover()` 走的是 `PUT /json/new?<url>` —— 那个文档在 CDP 连上之前就
    已经提交了。不补的话 `window.__errors` 在恢复后的页面上不存在，
    "前端没有任何未捕获异常"那条断言会读一个 `undefined` 然后通过。
    """
    tree = _tree()
    fn = _func(tree, "recover")
    assert fn is not None, "`Browser.recover()` 不见了"
    # 必须遍历 `_durable` 并把注入脚本 evaluate 进当前文档
    assert _calls_in(fn, "evaluate"), (
        "`recover()` 没有把注入脚本 evaluate 进当前文档：恢复后的页面上"
        " `window.__errors`/`window.__sse` 会不存在，相关断言读 undefined")
    src = SCRIPT.read_text(encoding="utf-8")
    body = ast.get_source_segment(src, fn) or ""
    assert "addScriptToEvaluateOnNewDocument" in body, (
        "`recover()` 里没有针对注入脚本的特殊处理")


def test_goto_self_heals_instead_of_blocking_forever() -> None:
    """`goto()` 必须给 `Page.navigate` 短超时并调用 `recover()`。

    否则那个"偶发永久不回"会把整轮跑挂在 120 秒的等待里 —— 实测发生过
    多次（`build/_b70.log`、`build/_b72.log`，都是 P1 之前就有的）。
    """
    tree = _tree()
    fn = _func(tree, "goto")
    assert fn is not None, "`Browser.goto()` 不见了"
    assert _calls_in(fn, "recover"), "`goto()` 没有调用 `recover()` 自愈"
    nav = _calls_in(fn, "send")
    nav_call = None
    for call in nav:
        for arg in call.args:
            if isinstance(arg, ast.Constant) and arg.value == "Page.navigate":
                nav_call = call
    assert nav_call is not None, "`goto()` 里没有 `Page.navigate` 调用"
    kw = {k.arg: k.value for k in nav_call.keywords if k.arg}
    assert "_timeout" in kw, (
        "`goto()` 的 `Page.navigate` 没给短超时，卡死时会白等 120 秒")
    timeout = kw["_timeout"]
    assert isinstance(timeout, ast.Constant), "_timeout 必须是字面量"
    assert float(timeout.value) < 60.0, (
        f"`goto()` 的 `Page.navigate` 超时 {timeout.value}s 太长 ——"
        " 卡死时该尽快换标签页，而不是陪它等")


def test_recover_closes_the_stuck_target() -> None:
    """恢复后要关掉卡死的那个标签页，否则每自愈一次就漏一个。"""
    tree = _tree()
    fn = _func(tree, "recover")
    assert fn is not None
    # 在字符串字面量里找 `/json/close/`，不做全文匹配（注释里也提到过）
    hits = [
        n.value for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and "/json/close/" in n.value
    ]
    assert hits, "`recover()` 没有关闭卡死的标签页（漏 target）"
