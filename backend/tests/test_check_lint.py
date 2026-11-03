"""编码规范检查器（scripts/check_lint.py）的双向验证。

## 为什么这条测试必须存在

`docs/11` 的 Q-023 记着一次真实事故：魔术字节校验的"通过判据"是
`grep -c "sniff_kind" uploads.py >= 1` —— **函数名出现就算过**。
于是伪装成 JPEG 的 PE 可执行文件被照单全收、归档进了真实照片库。
符号存在性不是行为，一条只在"符号在不在"上做文章的判据，会在
实现被删掉之后继续通过。

`scripts/check_lint.py` 把 20 条规则改成了 AST 判据。但**检查器本身
也会犯同样的错**：如果它写的判据从来没抓到过任何东西（判据空转），
它照样报"✅ 通过"。开发过程中确实发生过两次：

  * 规则 2 按 `_call_name(call)` 找 send2trash，而它在
    `handlers/browse.py:796` 是函数内 import、作为参数传给
    `run_in_executor`，AST 里根本不是 Call —— 于是永远报"找不到"。
  * 规则 20 按 `r.get(path)` 解析路由，而项目用的是
    `r.add("GET", path, ...)` —— 于是一条路由都解析不出来。

所以本测试对每条规则都做**三件事**：

  a. 在真实仓库上跑，断言 0 违规（现状是干净的）；
  b. 在临时目录里造一棵最小项目树，注入**一个已知坏样本**，
     断言该规则**恰好报出它**、且报的行号对得上；
  c. 对"正向断言"类规则（如规则 1 要求业务文件必须调
     `sandbox.resolve`），注入"删掉那处调用"的版本，断言它报红。

只做 (a) 的检查器和 grep 没有区别。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_lint.py"


def _load() -> "object":
    """按路径加载 scripts/check_lint.py（`scripts/` 不是包，不能 import）。"""
    spec = importlib.util.spec_from_file_location("check_lint", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_lint"] = mod
    spec.loader.exec_module(mod)
    return mod


CL = _load()


class Tree:
    """临时项目树：`backend/app/<rel>` + 可选的前端文件。

    `Ctx` 的 `repo` / `app` / `frontend` 都是可覆盖字段（就是为了这个），
    所以规则能在一棵假仓库上跑，不去碰真实的 backend/app。
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.app = root / "backend" / "app"
        self.app.mkdir(parents=True, exist_ok=True)
        self.frontend = root / "frontend" / "src"

    def write(self, rel: str, code: str) -> None:
        p = self.app / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code, encoding="utf-8")

    def front(self, rel: str, code: str) -> None:
        p = self.frontend / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code, encoding="utf-8")

    def ctx(self):
        return CL.Ctx(repo=self.root, app=self.app, frontend=self.frontend)

    def run(self, rule: int):
        return CL.RULES[rule][1](self.ctx())


# ---------------------------------------------------------------------------
# (a) 真实仓库必须干净
# ---------------------------------------------------------------------------


def test_real_repo_is_clean() -> None:
    """现状 0 违规 —— 这是底线，不是全部。"""
    findings = CL.run()
    assert findings == [], "\n".join(f.render() for f in findings)


def test_every_rule_is_registered() -> None:
    """每条都要有实现，且编号连续 —— 少一条就少守一个洞。"""
    assert sorted(CL.RULES) == list(range(1, len(CL.RULES) + 1))


def test_all_rules_have_nonempty_titles() -> None:
    for n, (title, _fn) in CL.RULES.items():
        assert title.strip(), f"规则 {n} 没有标题"


# ---------------------------------------------------------------------------
# (b) 每条规则都必须能抓到注入的坏样本
# ---------------------------------------------------------------------------


def test_rule_1_catches_path_concatenation(tmp_path: Path) -> None:
    """规则 1：业务代码里出现 `root.path / rel` 必须报。"""
    t = Tree(tmp_path)
    t.write("handlers/browse.py", (
        "def get(root, rel):\n"
        "    return root.path / rel\n"      # ← 第 2 行：绕过沙箱
    ))
    findings = t.run(1)
    # 报两条：这条 `.path /` 拼路径，以及"这个业务文件根本没调 sandbox.resolve"。
    # 两个方向都该报 —— 一处拼路径的文件不可能同时也是"有沙箱校验"的。
    assert {f.line for f in findings} == {0, 2}, [f.render() for f in findings]
    assert any(f.line == 2 and "拼路径" in f.message for f in findings)


def test_rule_1_catches_missing_sandbox_call(tmp_path: Path) -> None:
    """规则 1：业务文件完全不调 sandbox.resolve 也要报。"""
    t = Tree(tmp_path)
    t.write("indexer.py", "def walk(p):\n    return p.iterdir()\n")
    findings = t.run(1)
    assert len(findings) == 1
    assert "sandbox.resolve" in findings[0].message


def test_rule_1_allows_sandbox_resolve_usage(tmp_path: Path) -> None:
    """规则 1：正确用法必须放行 —— 否则这条规则只会被忽略。"""
    t = Tree(tmp_path)
    t.write("indexer.py", (
        "def walk(root, rel):\n"
        "    p = sandbox.resolve(root, rel, 'read')\n"
        "    return p.stat()\n"
    ))
    assert t.run(1) == []


def test_rule_2_catches_plain_unlink_of_user_data(tmp_path: Path) -> None:
    """规则 2：用户照片直接 os.unlink 必须报。"""
    t = Tree(tmp_path)
    t.write("handlers/browse.py", (
        "def tidy(photo):\n"
        "    os.unlink(photo)\n"           # ← 第 2 行：用户数据真删
    ))
    findings = t.run(2)
    assert any(f.line == 2 for f in findings), [f.render() for f in findings]
    assert any("send2trash" in f.message for f in findings)


def test_rule_2_catches_missing_send2trash_entirely(tmp_path: Path) -> None:
    """规则 2：全后端没有 send2trash，必须报"删除不可恢复"。"""
    t = Tree(tmp_path)
    t.write("handlers/browse.py", "def f():\n    return 1\n")
    findings = t.run(2)
    assert any("send2trash" in f.message for f in findings)


def test_rule_2_accepts_trash_via_run_in_executor(tmp_path: Path) -> None:
    """规则 2：`run_in_executor(None, send2trash, p)` 这种形态必须认。

    这正是实现在 `handlers/browse.py:796` 的写法 —— send2trash 是函数内
    import 的名字、被当作参数传递，AST 里**不是 Call**。按 Call 找会漏。
    """
    t = Tree(tmp_path)
    t.write("handlers/browse.py", (
        "async def trash(loop, root, rel):\n"
        "    from send2trash import send2trash\n"
        "    path = sandbox.resolve(root, rel, 'delete')\n"
        "    await loop.run_in_executor(None, send2trash, str(path))\n"
    ))
    assert t.run(2) == []


def test_rule_3_catches_value_interpolation(tmp_path: Path) -> None:
    """规则 3：把值插进 SQL 必须报。"""
    t = Tree(tmp_path)
    t.write("db.py", (
        "def q(db, name):\n"
        "    return db.query(f\"SELECT * FROM media WHERE name = '{name}'\")\n"
    ))
    findings = t.run(3)
    assert any(f.line == 2 for f in findings), [f.render() for f in findings]


def test_rule_3_allows_placeholder_only_interpolation(tmp_path: Path) -> None:
    """规则 3：只拼 `?` 占位符必须放行（4 处批量 IN 都是这个写法）。"""
    t = Tree(tmp_path)
    t.write("indexer.py", (
        "def q(db, part):\n"
        "    placeholders = ','.join(['(?,?)'] * len(part))\n"
        "    flat = [x for pair in part for x in pair]\n"
        "    return db.execute(\n"
        "        f'SELECT id FROM media WHERE (root_id, rel_path) IN (VALUES {placeholders})',\n"
        "        flat,\n"
        "    ).fetchall()\n"
    ))
    assert t.run(3) == []


def test_rule_4_catches_logging_a_token(tmp_path: Path) -> None:
    """规则 4：把令牌值打进日志必须报。"""
    t = Tree(tmp_path)
    t.write("auth.py", (
        "def f(log, client):\n"
        "    log.info('pairing %s', client.token)\n"   # ← 第 2 行：泄漏
    ))
    findings = t.run(4)
    assert any(f.line == 2 for f in findings), [f.render() for f in findings]


def test_rule_4_allows_uppercase_constants(tmp_path: Path) -> None:
    """规则 4：`TOKEN_TTL_DAYS` 这种常量名不算泄漏。

    真实代码 `auth.py:129` 就是 `log.warning("... %r ...", raw,
    TOKEN_TTL_DAYS)` —— 它命中了 "TOKEN" 字样，但值是 24。
    """
    t = Tree(tmp_path)
    t.write("auth.py", (
        "def f(log, raw):\n"
        "    log.warning('token_ttl 非法：%r', raw)\n"
        "    log.warning('退回 %d 天', TOKEN_TTL_DAYS)\n"
    ))
    assert t.run(4) == []


def test_rule_5_catches_v_html(tmp_path: Path) -> None:
    """规则 5：前端出现 v-html 必须报（存储型 XSS 的唯一入口）。"""
    t = Tree(tmp_path)
    t.front("views/Bad.vue", '<template>\n  <div v-html="evil"></div>\n</template>\n')
    findings = t.run(5)
    assert any(f.line == 2 for f in findings), [f.render() for f in findings]


def test_rule_6_catches_missing_magic_check(tmp_path: Path) -> None:
    """规则 6：uploads.py 里没有魔术字节校验必须报。"""
    t = Tree(tmp_path)
    t.write("uploads.py", "def complete():\n    return 1\n")
    findings = t.run(6)
    assert findings, "规则 6 没报 —— 判据空转"
    assert any("魔术字节" in f.message or "sniff" in f.message or "magic" in f.message
               for f in findings), [f.render() for f in findings]


def test_rule_7_catches_missing_pixel_limit(tmp_path: Path) -> None:
    """规则 7：没设 MAX_IMAGE_PIXELS 必须报。"""
    t = Tree(tmp_path)
    t.write("exif.py", "def parse():\n    return 1\n")
    assert t.run(7), "规则 7 没报 —— 判据空转"


def test_rule_8_catches_save_without_exif(tmp_path: Path) -> None:
    """规则 8：源图对象 save() 不传 exif= 必须报。"""
    t = Tree(tmp_path)
    t.write("thumbs.py", (
        "def gen(src, dst, size):\n"
        "    with open(src) as fh:\n"
        "        im = Image.open(fh)\n"
        "    im.save(dst, 'WEBP', quality=80)\n"   # ← 漏了 exif=b""
    ))
    findings = t.run(8)
    assert any("exif" in f.message for f in findings), [f.render() for f in findings]


def test_rule_8_ignores_placeholder_images(tmp_path: Path) -> None:
    """规则 8：现画的占位图不受约束（它没有源图可继承 EXIF）。

    真实代码 `thumbs.py:547` 是 `Image.new("RGB", ...).save(buf, "WEBP")`，
    而且它和缩略图**共用了变量名 `im`** —— 按文件级收集变量名会串味。
    """
    t = Tree(tmp_path)
    t.write("thumbs.py", (
        "def gen(src, dst):\n"
        "    with open(src) as fh:\n"
        "        im = Image.open(fh)\n"
        "    im.save(dst, 'WEBP', exif=b'')\n"
        "\n"
        "def placeholder(buf, size):\n"
        "    im = Image.new('RGB', (size, size), (58, 58, 62))\n"
        "    im.save(buf, 'WEBP', quality=60, method=2)\n"
    ))
    assert t.run(8) == []


def test_rule_9_catches_token_in_url(tmp_path: Path) -> None:
    """规则 9：构造带令牌的 URL 必须报。"""
    t = Tree(tmp_path)
    t.write("auth.py", "def f(base, tok):\n    return base + '?token=' + tok\n")
    findings = t.run(9)
    assert any(f.line == 2 for f in findings), [f.render() for f in findings]


def test_rule_10_catches_dynamic_need(tmp_path: Path) -> None:
    """规则 10：need 来自变量必须报（客户端能把读声明成删）。"""
    t = Tree(tmp_path)
    t.write("handlers/browse.py", (
        "def f(root, rel, op):\n"
        "    return sandbox.resolve(root, rel, op)\n"
    ))
    findings = t.run(10)
    assert any(f.line == 2 for f in findings), [f.render() for f in findings]


def test_rule_10_allows_literal_need(tmp_path: Path) -> None:
    """规则 10：字面量 need 放行。"""
    t = Tree(tmp_path)
    t.write("handlers/browse.py", (
        "def f(root, rel):\n"
        "    return sandbox.resolve(root, rel, 'read')\n"
    ))
    assert t.run(10) == []


def test_rule_11_catches_cross_dir_replace(tmp_path: Path) -> None:
    """规则 11：跨目录 os.replace 且无卷比较/EXDEV 降级必须报。"""
    t = Tree(tmp_path)
    t.write("thumbs.py", (
        "def move(a, b):\n"
        "    os.replace(a, b)\n"
    ))
    findings = t.run(11)
    assert any(f.line == 2 for f in findings), [f.render() for f in findings]


def test_rule_11_accepts_exdev_fallback(tmp_path: Path) -> None:
    """规则 11：EXDEV 探测降级是合法写法（`_move_one_sync` 就是这么写的）。"""
    t = Tree(tmp_path)
    t.write("handlers/browse.py", (
        "def move(src, dst):\n"
        "    try:\n"
        "        os.replace(src, dst)\n"
        "        return\n"
        "    except OSError as exc:\n"
        "        if getattr(exc, 'winerror', None) != 17 and exc.errno != 18:\n"
        "            raise\n"
    ))
    assert t.run(11) == []


def test_rule_11_accepts_same_dir_with_name(tmp_path: Path) -> None:
    """规则 11：`tmp = dst.with_name(...)` 后 replace 天然同卷。"""
    t = Tree(tmp_path)
    t.write("thumbs.py", (
        "def gen(dst):\n"
        "    tmp = dst.with_name(dst.name + '.tmp')\n"
        "    os.replace(tmp, dst)\n"
    ))
    assert t.run(11) == []


def test_rule_12_catches_truncating_open(tmp_path: Path) -> None:
    """规则 12：`open(..., "wb")` 会覆盖同名照片，必须报。"""
    t = Tree(tmp_path)
    t.write("archive.py", (
        "def place(dst):\n"
        "    return open(dst, 'wb')\n"
    ))
    findings = t.run(12)
    assert any(f.line == 2 for f in findings), [f.render() for f in findings]


def test_rule_13_catches_missing_root_safety(tmp_path: Path) -> None:
    """规则 13：没有 check_root_safety 必须报（data 会被当照片）。"""
    t = Tree(tmp_path)
    t.write("config.py", "class C:\n    @property\n    def roots(self):\n        return []\n")
    findings = t.run(13)
    assert any("check_root_safety" in f.message for f in findings)


def test_rule_13_catches_defined_but_unused(tmp_path: Path) -> None:
    """规则 13：定义了却不在 roots 里调用 —— 等于没有。"""
    t = Tree(tmp_path)
    t.write("config.py", (
        "def check_root_safety(r, c):\n"
        "    return True, 'ok'\n"
        "\n"
        "class C:\n"
        "    @property\n"
        "    def roots(self):\n"
        "        return []\n"
    ))
    findings = t.run(13)
    assert any("没在 roots" in f.message for f in findings), [f.render() for f in findings]


def test_rule_14_catches_unsanitized_error(tmp_path: Path) -> None:
    """规则 14：to_response 不走 sanitize 必须报。"""
    t = Tree(tmp_path)
    t.write("errors.py", (
        "class E:\n"
        "    def to_response(self, request_id):\n"
        "        return {'detail': self.detail, 'request_id': request_id}\n"
    ))
    findings = t.run(14)
    assert any("sanitize" in f.message for f in findings)


def test_rule_15_catches_wildcard_cors_default(tmp_path: Path) -> None:
    """规则 15：`allow_origins` 的默认值是 `*` 必须报。

    审计 F1 的原始写法是 `allow_origin: str = "*"`（单数、关键字默认值），
    旧判据 `grep 'allow_origins=["*"]'` 连碰都没碰到它 —— 这就是
    "判据合规、行为敞开"的典型。
    """
    t = Tree(tmp_path)
    t.write("core/dispatch.py", (
        "def enable_cors(self, *, allow_origins: Iterable[str] | str = '*') -> None:\n"
        "    self._cors = {'origins': allow_origins}\n"
    ))
    findings = t.run(15)
    assert any("allow_origins" in f.message for f in findings), \
        [f.render() for f in findings]


def test_rule_15_demands_cors_off_by_default(tmp_path: Path) -> None:
    """规则 15：空白名单时必须 `self._cors = None`，不许选"更安全的回显"。"""
    t = Tree(tmp_path)
    t.write("core/dispatch.py", (
        "def enable_cors(self, *, allow_origins=()) -> None:\n"
        "    # 没有 self._cors = None，等价于总是启用某种策略\n"
        "    self._cors = {'origins': frozenset(allow_origins)}\n"
    ))
    findings = t.run(15)
    assert any("_cors = None" in f.message for f in findings), \
        [f.render() for f in findings]


def test_rule_15_rejects_prefix_matching(tmp_path: Path) -> None:
    """规则 15：用 startswith 比对 Origin 必须报（子域可绕过）。"""
    t = Tree(tmp_path)
    t.write("core/dispatch.py", (
        "def enable_cors(self, *, allow_origins=()) -> None:\n"
        "    if not allow_origins:\n"
        "        self._cors = None\n"
        "        return\n"
        "    self._cors = {'origins': frozenset(allow_origins)}\n"
        "\n"
        "def _apply_cors(self, ctx, r):\n"
        "    origin = ctx.headers.get('origin')\n"
        "    allowed = self._cors['origins']\n"
        "    if origin and any(origin.startswith(a) for a in allowed):\n"
        "        r.set_header('Access-Control-Allow-Origin', origin)\n"
        "        return\n"
        "    return\n"
    ))
    findings = t.run(15)
    assert any("startswith" in f.message for f in findings), \
        [f.render() for f in findings]


def test_rule_15_demands_early_return_for_unknown_origin(tmp_path: Path) -> None:
    """规则 15：不在白名单也必须一个头都不发（不能只跳过 ACAO）。"""
    t = Tree(tmp_path)
    t.write("core/dispatch.py", (
        "def enable_cors(self, *, allow_origins=()) -> None:\n"
        "    if not allow_origins:\n"
        "        self._cors = None\n"
        "        return\n"
        "    self._cors = {'origins': frozenset(allow_origins)}\n"
        "\n"
        "def _apply_cors(self, ctx, r):\n"
        "    origin = ctx.headers.get('origin')\n"
        "    allowed = self._cors['origins']\n"
        "    if origin and origin in allowed:\n"
        "        r.set_header('Access-Control-Allow-Origin', origin)\n"
        "    r.set_header('Access-Control-Allow-Headers', 'Authorization')\n"
    ))
    findings = t.run(15)
    assert any("return" in f.message for f in findings), \
        [f.render() for f in findings]


def test_rule_16_catches_missing_executor(tmp_path: Path) -> None:
    """规则 16：没有专用执行器必须报（解码会阻塞事件循环）。"""
    t = Tree(tmp_path)
    t.write("thumbs.py", "def gen(src):\n    return Image.open(src)\n")
    findings = t.run(16)
    assert findings, "规则 16 没报 —— 判据空转"


def test_rule_17_catches_missing_fsync(tmp_path: Path) -> None:
    """规则 17：审计日志不 fsync 必须报。"""
    t = Tree(tmp_path)
    t.write("audit.py", (
        "def write(path, line):\n"
        "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
        "    os.write(fd, line)\n"
    ))
    findings = t.run(17)
    assert any("fsync" in f.message for f in findings), [f.render() for f in findings]


def test_rule_17_catches_path_leak_into_other_module(tmp_path: Path) -> None:
    """规则 17：别的模块引用审计日志路径必须报（可能被当照片吐出去）。"""
    t = Tree(tmp_path)
    t.write("audit.py", (
        "def write(path, line):\n"
        "    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)\n"
        "    os.write(fd, line)\n"
        "    os.fsync(fd)\n"
    ))
    t.write("static.py", "LOG = 'audit.log'\n")
    findings = t.run(17)
    assert any("非审计模块" in f.message for f in findings), [f.render() for f in findings]


def test_rule_18_catches_non_loopback_default(tmp_path: Path) -> None:
    """规则 18：默认 host 不是回环必须报。

    注意 `DEFAULT_CONFIG: Final[...] = {...}` 是 **AnnAssign**，
    只匹配 `ast.Assign` 会永远取不到值 —— 这条测试就是防那个的。
    """
    t = Tree(tmp_path)
    t.write("config.py", (
        "from typing import Final, Any\n"
        "DEFAULT_CONFIG: Final[dict[str, Any]] = {\n"
        "    'server': {'host': '0.0.0.0', 'port': 8787},\n"
        "}\n"
        "def check_bind_safety(h, c=None):\n"
        "    return True, 'ok'\n"
    ))
    findings = t.run(18)
    assert any("不是回环" in f.message for f in findings), [f.render() for f in findings]


def test_rule_18_catches_missing_gates(tmp_path: Path) -> None:
    """规则 18：check_bind_safety 少一道门必须报。"""
    t = Tree(tmp_path)
    t.write("config.py", (
        "from typing import Final, Any\n"
        "DEFAULT_CONFIG: Final[dict[str, Any]] = {'server': {'host': '127.0.0.1'}}\n"
        "def check_bind_safety(h, c=None):\n"
        "    return True, 'ok'\n"      # 三道门一道都没有
    ))
    findings = t.run(18)
    assert any("门控" in f.message for f in findings), [f.render() for f in findings]


def test_rule_19_catches_random_for_credentials(tmp_path: Path) -> None:
    """规则 19：用 random 生成凭据必须报。"""
    t = Tree(tmp_path)
    t.write("auth.py", (
        "import random\n"
        "def code():\n"
        "    return random.randint(100000, 999999)\n"
    ))
    findings = t.run(19)
    assert any(f.line == 3 for f in findings), [f.render() for f in findings]


def test_rule_20_catches_unparsed_routes(tmp_path: Path) -> None:
    """规则 20：路由一条都解析不出来时必须报"空判据"。

    这正是开发中踩到的坑：项目用 `r.add("GET", path, ...)`，而第一版
    按 `r.get(path)` 解析 —— 解析出 0 条，规则静默通过。
    """
    t = Tree(tmp_path)
    t.write("routers/__init__.py", "def build_router(r):\n    return r\n")
    t.write("server.py", "def serve():\n    router.use(auth_middleware)\n")
    findings = t.run(20)
    assert any("空判据" in f.message for f in findings), [f.render() for f in findings]


def test_rule_20_catches_panel_without_default_off(tmp_path: Path) -> None:
    """规则 20：`/panel` 只有 `is_loopback` 一道门必须报（审计 F1-b）。

    本机浏览器发起的请求来源 IP 就是 127.0.0.1，与用户本人无从区分 ——
    所以"仅回环"单独一道门等于没有门。
    """
    t = Tree(tmp_path)
    t.write("routers/__init__.py", (
        "def build_router(r):\n"
        "    r.add('POST', '/panel/pair-code', h.pair, name='p')\n"
    ))
    t.write("server.py", "def serve():\n    router.use(auth_middleware)\n")
    t.write("auth.py", (
        "from .netguard import check_host\n"
        "PANEL_PATHS = ('/panel',)\n"
        "def is_loopback(c):\n"
        "    return True\n"
        "def middleware(c):\n"
        "    return check_host(c.headers.get('host'))\n"
    ))
    findings = t.run(20)
    assert any("panel" in f.message.lower() for f in findings), \
        [f.render() for f in findings]


def test_rule_20_catches_unwired_host_guard(tmp_path: Path) -> None:
    """规则 20：`netguard.py` 写了却没人调用必须报（死代码等于不存在）。"""
    t = Tree(tmp_path)
    t.write("routers/__init__.py", (
        "def build_router(r):\n"
        "    r.add('GET', '/api/system/ping', h.ping, name='p')\n"
    ))
    t.write("server.py", "def serve():\n    router.use(auth_middleware)\n")
    t.write("auth.py", "PANEL_PATHS = ('/panel',)\ndef is_loopback(c):\n    return True\n")
    t.write("netguard.py", "def check_host(raw, cfg=None):\n    return None\n")
    findings = t.run(20)
    assert any("check_host" in f.message for f in findings), \
        [f.render() for f in findings]


def test_rule_20_parses_r_add_style_routes(tmp_path: Path) -> None:
    """规则 20：`r.add("GET", path, ...)` 必须被解析到（否则判据空转）。"""
    t = Tree(tmp_path)
    t.write("routers/__init__.py", (
        "def build_router(r):\n"
        "    r.add('GET', '/api/system/ping', h.ping, name='p')\n"
        "    r.add('POST', '/api/pair', h.pair, name='a')\n"
        "    r.add('GET', '/api/timeline', h.timeline, name='t')\n"
    ))
    t.write("server.py", "def serve():\n    router.use(auth_middleware)\n")
    t.write("auth.py", (
        "from .netguard import check_host\n"
        "PANEL_PATHS = ('/panel',)\n"
        "def panel_http_enabled(cfg):\n"
        "    return False\n"
        "def is_loopback(c):\n"
        "    return True\n"
        "def middleware(c):\n"
        "    return check_host(c.headers.get('host'))\n"
    ))
    assert t.run(20) == []


# ---------------------------------------------------------------------------
# 规则 21：库内内容必须经沙箱句柄读取
# ---------------------------------------------------------------------------


def test_rule_21_flags_a_new_path_open(tmp_path: Path) -> None:
    """业务模块里新写一处 `open(path)` 就要报 —— 这才是护栏的意义。"""
    t = Tree(tmp_path)
    t.write("handlers/page.py", (
        "def sniff(path):\n"
        "    with open(path, 'rb') as fh:\n"
        "        return fh.read(8)\n"
    ))
    found = t.run(21)
    assert [(f.line, "open()" in f.message) for f in found] == [(2, True)]


def test_rule_21_flags_path_read_bytes(tmp_path: Path) -> None:
    """`p.read_bytes()` 同样是"按路径打开"，不能漏。"""
    t = Tree(tmp_path)
    t.write("video.py", (
        "def probe(p):\n"
        "    return p.read_bytes()[:8]\n"
        "def probe2(p):\n"
        "    return p.read_bytes()[:8]\n"
    ))
    found = t.run(21)
    # 基线已清零：两处都要报，一处都不能漏
    assert [f.line for f in found] == [2, 4]


def test_rule_21_accepts_the_sandbox_handle(tmp_path: Path) -> None:
    """走 `sandbox.open_verified()` 的不报 —— 那才是规则要求的写法。"""
    t = Tree(tmp_path)
    t.write("indexer.py", (
        "from . import sandbox\n"
        "def head(path):\n"
        "    with sandbox.open_verified(path, 'rb') as fh:\n"
        "        return fh.read(32)\n"
    ))
    assert t.run(21) == []


def test_rule_21_accepts_an_already_open_handle(tmp_path: Path) -> None:
    """`Image.open(fh)` 是"用句柄"，不是"按路径打开" —— 不能误报。"""
    t = Tree(tmp_path)
    t.write("exif.py", (
        "from . import sandbox\n"
        "from PIL import Image\n"
        "def read(path):\n"
        "    with sandbox.open_verified(path) as fh:\n"
        "        return Image.open(fh).size\n"
    ))
    assert t.run(21) == []


def test_rule_21_baseline_is_accounted(tmp_path: Path) -> None:
    """存量记账机制还在：往基线里填 1，同一个文件就只报第 2 处。"""
    t = Tree(tmp_path)
    t.write("exif.py", (
        "def a(p):\n"
        "    return open(p, 'rb').read(8)\n"
        "def b(p):\n"
        "    return open(p, 'rb').read(8)\n"
    ))
    assert CL._HANDLE_BASELINE == {}
    CL._HANDLE_BASELINE["exif.py"] = 1
    try:
        assert [f.line for f in t.run(21)] == [4]
    finally:
        CL._HANDLE_BASELINE.clear()


def test_rule_21_skips_infrastructure(tmp_path: Path) -> None:
    """配置/日志/库这些模块读的是自己的文件，不在"库内媒体"范围内。"""
    t = Tree(tmp_path)
    t.write("config.py", "def load(p):\n    return open(p, 'rb').read()\n")
    t.write("sandbox.py", "def rd(p):\n    return p.read_bytes()\n")
    assert t.run(21) == []


def test_rule_21_ignores_os_open(tmp_path: Path) -> None:
    """`os.open()` 返回 fd，本身不是"按路径读内容"，禁掉它等于禁掉句柄化的实现。"""
    t = Tree(tmp_path)
    t.write("handlers/reserve.py", (
        "import os\n"
        "def make(dst):\n"
        "    os.close(os.open(dst, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))\n"
    ))
    assert t.run(21) == []

# ---------------------------------------------------------------------------
# CLI 行为
# ---------------------------------------------------------------------------


def test_cli_list_prints_every_rule(capsys: pytest.CaptureFixture[str]) -> None:
    """规则清单必须与 `RULES` 一致 —— 加规则时忘了登记会被这条逮住。"""
    assert CL.main(["--list"]) == 0
    out = capsys.readouterr().out
    for n in sorted(CL.RULES):
        assert f"{n:2d}" in out or f"{n} " in out


def test_cli_rejects_unknown_rule(capsys: pytest.CaptureFixture[str]) -> None:
    assert CL.main(["--rule=99"]) == 2
    assert "未知规则编号" in capsys.readouterr().err


def test_cli_json_output_is_parseable() -> None:
    import json
    import subprocess

    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPT), "--json"],
        capture_output=True, text=True, encoding="utf-8", cwd=str(REPO),
    )
    payload = json.loads(r.stdout)
    assert isinstance(payload, list)
    assert r.returncode == (1 if payload else 0)
