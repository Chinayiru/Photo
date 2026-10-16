"""测试隔离守卫 —— 确保测试永远不会碰用户的真实照片库。

## 为什么需要这个文件

开发期出过一次真实事故：`backend/tests/test_api_contract.py` 调用了
`build_state(cfg)` 而**没传 `db=`**。`build_state` 内部的 `get_db()`
是模块级单例，按磁盘上的 `data/config.json` 解析路径 ——
在开发机上那就是用户真实的 `D:\\Photos\\data\\photobridge.db`。

于是发生了两件事：

1. 测试夹具造的合成行（`2024/2024-05/a.jpg`、`2024/000.jpg`…）
   被 upsert 进了真实库；
2. 扫描结束时 `_mark_missing(root_id='library', seen={那 3 个合成文件})`
   把真实库的 **4915 行全部标成 `missing=1`** —— 而它们在磁盘上好好的。
   时间线只显示 `missing=0` 的行，用户打开 App 看到的是**空相册**。

单测全绿，因为测试自己的临时库一切正常。**没有任何一条断言能发现
"测试写到了别的库上"** —— 除非专门为它写一条。

## 这个文件做什么

三件事，从三个角度堵住：

1. `test_global_db_points_at_real_config` —— 记录"真实库路径是什么"，
   让后面的守卫知道要防谁。
2. `test_make_db_never_touches_real_db` —— 验证新 API `make_db(cfg)`
   的行为：按 cfg 解析路径，与全局单例无关。
3. `test_build_state_uses_cfg_db_when_not_passed` —— 验证 `build_state` 在
   "cfg 指向临时库但没传 db" 时**按 cfg 建库**，绝不回退到真实库。
   这是最关键的一条：它是那个事故的直接防线。
4. `test_scan_does_not_touch_other_databases` —— 端到端：在临时库上跑
   一次完整扫描 + 标记 missing，然后断言真实库的行数与 missing 计数
   **一个字节都没变**。

第 4 条是真正能抓住事故重演的那条 —— 前三条都是它失败时的定位辅助。

⚠️ 第 3 条曾经是断言 `raise RuntimeError`。改成"按 cfg 建库"是因为
报错方案让 `--config` 完全不可用（`serve()` 自己就不传 `db=`），
而 `--config` 指向另一个数据目录恰恰是第二次事故的场景。
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from backend.app import db as db_mod                                  # noqa: E402
from backend.app.config import (                                      # noqa: E402
    CONFIG_PATH,
    DATA_DIR,
    Config,
)
from backend.app.config import get as get_config                      # noqa: E402
from backend.app.db import Database, make_db                          # noqa: E402
from backend.app.indexer import Indexer                               # noqa: E402
from backend.app.server import build_state                            # noqa: E402
from backend.app.state import AppState                                # noqa: E402


def _real_db_path() -> Path:
    """全局单例（生产路径）解析出的数据库文件路径。

    不 import 私有变量，直接调 `get_db()` 再读它的 `.path` ——
    这样即使实现改了内部结构，这条守卫依然有效。
    """
    return Path(db_mod.get_db().path)


def _snapshot(path: Path) -> dict[str, int] | None:
    """某个库的"指纹"：行数 + missing 计数 + 最大 id。

    文件不存在时返回 None（说明这台机器上还没有真实库，
    守卫应该跳过而不是失败 —— 全新 clone 的仓库就是这样）。
    """
    if not path.is_file():
        return None
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(missing), 0), COALESCE(MAX(id), 0) "
            "FROM media"
        ).fetchone()
        return {"rows": row[0], "missing": row[1], "max_id": row[2]}
    except sqlite3.Error:
        return None
    finally:
        con.close()


def _make_photo(path: Path, taken: str = "2024:05:12 18:30:00") -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (64, 48), (180, 90, 40))
    ex = Image.Exif()
    ex.get_ifd(0x8769)[0x9003] = taken
    im.save(path, "JPEG", exif=ex)


@pytest.fixture()
def real_db_before():
    """记录真实库在测试前后的指纹，测试结束再比对。"""
    path = _real_db_path()
    before = _snapshot(path)
    yield path, before
    after = _snapshot(path)
    assert after == before, (
        f"测试改动了真实照片库！\n"
        f"  路径：{path}\n"
        f"  测试前：{before}\n"
        f"  测试后：{after}\n"
        f"测试必须用 Config(data_dir=tmp_path/...) + make_db(cfg) 造独立库，"
        f"绝不能落到全局单例上。"
    )


def _tmp_cfg(tmp_path: Path, library: Path) -> Config:
    return Config(
        {
            "server": {"host": "127.0.0.1", "port": 0, "allow_lan": False},
            "roots": [{
                "id": "library", "path": str(library), "label": "测试库",
                "mode": "readwrite", "enabled": True, "recursive": True,
            }],
            "auth": {"require_pairing": False},
            "index": {"scan_on_start": False, "watch": False},
        },
        path=tmp_path / "config.json",
        data_dir=tmp_path / "data",
    )


# ---------------------------------------------------------------------------
# 1. make_db 的行为
# ---------------------------------------------------------------------------

def test_make_db_uses_given_config_not_global(tmp_path, real_db_before) -> None:
    """`make_db(cfg)` 必须按 cfg 解析路径，与全局单例无关。"""
    real_path = _real_db_path()
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = _tmp_cfg(tmp_path, lib)

    db = make_db(cfg)
    assert Path(db.path).resolve() != real_path.resolve(), (
        "make_db 竟然解析到了真实库路径 —— 它就是为杜绝这件事而存在的"
    )
    assert Path(db.path).resolve() == Path(cfg.db_path).resolve()
    db.init()
    # 建好表且是空的：证明这是个全新独立的库
    assert db.scalar("SELECT COUNT(*) FROM media", default=-1) == 0
    db.close()


def test_make_db_does_not_touch_global_singleton(tmp_path, real_db_before) -> None:
    """造独立库不该改动全局单例指向的对象。"""
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = _tmp_cfg(tmp_path, lib)
    before = _real_db_path()
    make_db(cfg).init()
    assert _real_db_path() == before, "make_db 不该影响全局单例"


# ---------------------------------------------------------------------------
# 2. build_state 的防线
# ---------------------------------------------------------------------------

def test_build_state_uses_cfg_db_when_not_passed(tmp_path, real_db_before) -> None:
    """★ 事故的直接防线（第二版：从"报错"改为"造对"）。

    历史：这里最初断言 `build_state(cfg)` 会 `raise RuntimeError`。
    但那条防线有个更严重的问题 —— **它让 `--config` 根本无法工作**：
    `serve()` 自己就调 `build_state(cfg)` 不传 `db=`，于是任何用
    `--config` 指向另一个数据目录的启动都会直接崩掉。而"测试用
    `--config` 指向临时照片库、数据库却仍是真实库"正是第二次事故的
    根因，报错只是把崩溃从"污染数据"换成了"启动不了"，两者都不可接受。

    正确行为：`db is None` 时按 **cfg** 解析路径建库。这样
    `--config H:/tmp/x/cfg.json` 会连 `H:/tmp/x/data/photobridge.db`，
    与真实库毫无关系。关键断言不是"报没报错"，而是
    **真实库一个字节都没变**。
    """
    real_path = _real_db_path()
    if not real_path.is_file():
        pytest.skip("这台机器上没有真实库，无法验证防线（CI 上是正常的）")

    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = _tmp_cfg(tmp_path, lib)

    st = build_state(cfg)          # 故意不传 db=
    try:
        assert Path(st.db.path).resolve() == Path(cfg.db_path).resolve(), (
            "build_state 必须按 cfg 解析数据库路径，不能回退到全局单例"
        )
        assert Path(st.db.path).resolve() != real_path.resolve(), (
            "build_state 连到了真实照片库 —— 这就是那次事故的成因"
        )
    finally:
        st.db.close()


def test_build_state_still_uses_global_singleton_for_real_config(real_db_before) -> None:
    """反过来：cfg 就是磁盘上那份配置时，仍然复用全局单例。

    生产路径不该每次启动都新建一个 Database 对象 —— 同一个文件
    两个 Database 实例意味着两把写锁，WAL 下仍可能互相 SQLITE_BUSY。
    """
    real_path = _real_db_path()
    if not real_path.is_file():
        pytest.skip("这台机器上没有真实库")

    cfg = get_config()
    assert Path(cfg.db_path).resolve() == real_path.resolve(), (
        "前提：磁盘配置指向的正是真实库"
    )
    st = build_state(cfg)
    assert Path(st.db.path).resolve() == real_path.resolve()


def test_build_state_accepts_explicit_db(tmp_path, real_db_before) -> None:
    """传了 db= 就该正常工作，且用的是传进来的那个库。"""
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = _tmp_cfg(tmp_path, lib)
    db = make_db(cfg)
    db.init()

    st = build_state(cfg, db=db)
    assert st.db is db
    assert Path(st.db.path).resolve() == Path(cfg.db_path).resolve()


# ---------------------------------------------------------------------------
# 3. AppState 的防线
# ---------------------------------------------------------------------------

def test_app_state_with_cfg_only_uses_that_cfg(tmp_path, real_db_before) -> None:
    """只传 cfg 时，AppState 必须按 cfg 造库，不能落回全局单例。"""
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = _tmp_cfg(tmp_path, lib)

    st = AppState(cfg)            # 故意不传 db
    assert Path(st.db.path).resolve() == Path(cfg.db_path).resolve(), (
        "AppState(cfg) 落到了别的库上 —— 这正是把真实库标成 missing 的成因"
    )
    assert Path(st.db.path).resolve() != _real_db_path().resolve()


# ---------------------------------------------------------------------------
# 4. 端到端：完整扫描不能污染别的库
# ---------------------------------------------------------------------------

def test_full_scan_does_not_touch_other_databases(tmp_path, real_db_before) -> None:
    """★ 真正能抓住事故重演的一条。

    在临时库上跑一次完整扫描（含 `_mark_missing`），然后由夹具断言
    真实库的行数 / missing 计数 / 最大 id 完全没变。

    复现原事故的条件都齐了：临时库里有 3 个文件，而真实库里有几千行 ——
    如果 db 解析错了，`_mark_missing(root_id='library')` 会把真实库的
    行全部标成 missing=1，夹具立刻失败。
    """
    lib = tmp_path / "lib"
    _make_photo(lib / "2024" / "2024-05" / "a.jpg")
    _make_photo(lib / "2024" / "2024-05" / "b.jpg", "2024:05:12 18:30:01")
    _make_photo(lib / "青甘环线" / "敦煌.jpg", "2023:07:08 10:11:12")

    cfg = _tmp_cfg(tmp_path, lib)
    db = make_db(cfg)
    db.init()

    idx = Indexer(cfg, db)
    stats = asyncio.run(idx.scan_all())
    assert stats and stats[0].added == 3, f"临时库应索引 3 张：{stats}"

    # 再扫一次，这一次所有文件都"未变" —— 走的正是 `_mark_missing` 路径。
    # 原事故就发生在这一步之后。
    stats2 = asyncio.run(idx.scan_all())
    assert stats2[0].missing == 0, "临时库自己的文件不该被标 missing"

    # 临时库是干净的：3 行，全部 missing=0
    assert db.scalar("SELECT COUNT(*) FROM media") == 3
    assert db.scalar("SELECT SUM(missing) FROM media") == 0
    db.close()


def test_indexer_uses_injected_db_only(tmp_path, real_db_before) -> None:
    """索引器只该写它拿到的那个 db 对象。"""
    lib = tmp_path / "lib"
    _make_photo(lib / "x.jpg")
    cfg = _tmp_cfg(tmp_path, lib)
    db = make_db(cfg)
    db.init()

    idx = Indexer(cfg, db)
    assert idx.db is db
    asyncio.run(idx.scan_all())
    assert db.scalar("SELECT COUNT(*) FROM media") == 1
    # 明确确认没写到真实库：真实库的指纹由夹具比对
    db.close()


def test_database_class_never_defaults_to_real_path() -> None:
    """`Database` 的构造函数必须要求显式路径 —— 没有"默认库"这种概念。"""
    import inspect

    sig = inspect.signature(Database.__init__)
    param = sig.parameters.get("path")
    assert param is not None, "Database 必须收 path 参数"
    assert param.default is inspect.Parameter.empty, (
        "Database(path) 不能有默认值 —— 有默认值就意味着某个地方会"
        "悄悄连到真实库上"
    )


# ---------------------------------------------------------------------------
# 5. data_dir 必须跟着配置文件走（★ 第二个污染源的根因）
# ---------------------------------------------------------------------------

def test_data_dir_follows_config_path(tmp_path) -> None:
    """★ 真实事故的根因。

    `python -m backend.app.main --config H:/tmp/pbtest/cfg.json` 会从那个
    文件读 roots（指向测试照片库），但如果 `data_dir` 固定成安装目录下的
    `DATA_DIR`，数据库仍然是**用户真实库的索引**。于是测试扫描完自己的
    几个文件后，`_mark_missing(root_id='library')` 把真实库的 4915 行
    全部标成 missing —— 用户打开 App 看到空相册。

    修法：`data_dir` 跟着配置文件所在目录走。
    """
    cfg_path = tmp_path / "elsewhere" / "cfg.json"
    cfg_path.parent.mkdir(parents=True)
    cfg = Config({}, path=cfg_path)

    assert Path(cfg.data_dir) == cfg_path.parent / "data", (
        f"--config 指向 {cfg_path}，data_dir 却是 {cfg.data_dir} —— "
        f"这会连到别的库上去"
    )
    # 数据库必须落在配置旁边，而不是安装目录里
    assert Path(cfg.db_path).parent == cfg_path.parent / "data"
    assert Path(cfg.db_path).resolve() != Path(DATA_DIR, "photobridge.db").resolve()


def test_default_layout_is_unchanged() -> None:
    """默认布局（配置文件就在 DATA_DIR 里）下 data_dir 必须还是 DATA_DIR。

    这条是防回归：如果实现改成无脑 `parent / "data"`，
    默认路径会变成 `data/data/photobridge.db` —— 老用户升级后
    会看到一个空库（所有照片"消失"），是最糟糕的升级体验。
    """
    cfg = Config({}, path=CONFIG_PATH)
    assert Path(cfg.data_dir) == Path(DATA_DIR), (
        f"默认布局下 data_dir 变成了 {cfg.data_dir}，"
        f"老用户升级后会指向一个空库"
    )
    assert Path(cfg.db_path) == Path(DATA_DIR) / "photobridge.db"


def test_config_inside_data_named_dir_is_not_nested(tmp_path) -> None:
    """配置文件本身就在名为 data 的目录里 → 就地使用，不再拼一层。"""
    cfg_path = tmp_path / "mydata" / "data" / "config.json"
    cfg_path.parent.mkdir(parents=True)
    cfg = Config({}, path=cfg_path)
    assert Path(cfg.data_dir) == tmp_path / "mydata" / "data"


def test_explicit_data_dir_still_wins(tmp_path) -> None:
    """显式 data_dir 优先级最高 —— 测试夹具全靠它。"""
    cfg = Config({}, path=tmp_path / "cfg.json", data_dir=tmp_path / "custom")
    assert Path(cfg.data_dir) == tmp_path / "custom"


def test_load_never_points_at_another_projects_data(tmp_path, monkeypatch) -> None:
    """`load()` 读真实配置时，data_dir 必须是真实 DATA_DIR（不是 data/data）。"""
    from backend.app import config as cfgmod

    cfg = cfgmod.load()
    assert Path(cfg.data_dir) == Path(DATA_DIR)
    assert Path(cfg.db_path) == Path(DATA_DIR) / "photobridge.db"


# ---------------------------------------------------------------------------
# 首次运行：`--config` 不能被丢掉
# ---------------------------------------------------------------------------

def test_is_first_run_honours_given_config(tmp_path) -> None:
    """★ `is_first_run(cfg)` 必须看传进来的那份配置，而不是全局 CONFIG_PATH。

    这是"事故 2"的**另一半**。`data_dir` 修好之后，`--config` 已经能
    把数据库带到正确位置；但 `serve()` 里的 `is_first_run()` 仍只看
    默认的 `data/config.json`。当默认配置不存在（或 roots 为空）时它
    返回 True，`serve()` 随即调 `bootstrap_default_root()` 并用
    `get_config()` **把调用方传进来的 cfg 整个丢掉** —— 用户以为在用
    A 配置，实际跑的是自动探测出来的 B。

    实测：`--config` 指向只有 1 张图的临时库，程序却去索引了自动
    探测到的真实照片库，并在那份库上 `marked 2000 rows missing`。
    """
    from backend.app import config as cfgmod

    cfg_path = tmp_path / "elsewhere" / "cfg.json"
    cfg_path.parent.mkdir(parents=True)
    cfg = Config({"roots": [{"id": "library", "path": str(tmp_path),
                             "mode": "readwrite"}]}, path=cfg_path)

    assert cfgmod.is_first_run(cfg) is False, (
        "配置里明明有 roots，却被判成首次运行 —— "
        "serve() 会因此丢掉这份配置"
    )

    empty = Config({}, path=cfg_path)
    assert cfgmod.is_first_run(empty) is True, "roots 为空才算首次运行"


def test_bootstrap_writes_to_given_config_not_default(tmp_path, monkeypatch) -> None:
    """★ `bootstrap_default_root(cfg)` 必须写进 cfg 自己的路径并返回它。"""
    from backend.app import config as cfgmod

    cfg_path = tmp_path / "elsewhere" / "cfg.json"
    cfg_path.parent.mkdir(parents=True)
    cfg = Config({}, path=cfg_path)

    lib = tmp_path / "相册"
    lib.mkdir()
    monkeypatch.setattr(cfgmod, "_guess_library_paths", lambda: [(lib, "相册")])

    written: list[Path] = []
    monkeypatch.setattr(
        cfgmod, "_write_json",
        lambda target, raw, **kw: written.append(Path(target)),   # noqa: ARG005
    )

    out = cfgmod.bootstrap_default_root(cfg)

    assert out is cfg, "必须就地返回传进来的对象，而不是 get_config()"
    assert written == [cfg_path], (
        f"配置写到了 {written}，应该是调用方的 {cfg_path} —— "
        f"写错地方等于「探测结果」和「实际运行的配置」是两份东西"
    )
    assert [r.id for r in out.roots] == ["library"], (
        "改完 raw 必须让 roots 缓存失效，否则还是改之前的空列表"
    )
    assert str(out.roots[0].path) == str(lib)


def test_serve_does_not_discard_passed_config() -> None:
    """★ 结构回归：`serve()` 里不许出现 `cfg = get_config()` 覆盖入参。

    行为测试要真起服务，而这条约束本质是"别把入参换掉"，
    用 AST 直接看代码更稳（字符串匹配会被注释绊倒，
    见 `test_server_shutdown_passes_internal_drain_timeout` 的教训）。
    """
    import ast
    import inspect
    import textwrap

    from backend.app import server as server_mod

    tree = ast.parse(textwrap.dedent(inspect.getsource(server_mod.serve)))

    # 取函数体：跳过 docstring，找到第一个真正的语句
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef), "serve 应该是 async 函数"
    body = [n for n in fn.body
            if not (isinstance(n, ast.Expr)
                    and isinstance(n.value, ast.Constant))]

    # 找出所有 `cfg = get_config()` 形状的赋值
    bad: list[int] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Assign):
            continue
        if not (len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == "cfg"):
            continue
        v = node.value
        if isinstance(v, ast.Call) and (
            (isinstance(v.func, ast.Name) and v.func.id == "get_config")
            or (isinstance(v.func, ast.Attribute) and v.func.attr == "get_config")
        ):
            bad.append(node.lineno)

    assert not bad, (
        f"serve() 第 {bad} 行把 cfg 换成了 get_config() —— "
        f"这会丢掉 --config 传进来的配置"
    )

    # 首句赋值必须是 `cfg = cfg or get_config()` 这种保底形式
    first = body[0]
    assert isinstance(first, ast.Assign), (
        f"serve() 首句应是 cfg 的保底赋值，实际是 {type(first).__name__}"
    )
    assert isinstance(first.value, ast.BoolOp), (
        "serve() 首句应是 `cfg = cfg or get_config()`，"
        "而不是无条件覆盖入参"
    )

