"""watcher.py 单元测试。

## 为什么这个文件必须存在

`config.index.watch` 的默认值是 `True`，`docs/01-需求规格说明书.md:395`
把它写成验收标准 AC-2.15：

    直接在电脑上往库目录拷入 1 张新图，30 秒内手机时间线可见（无需手动重扫）

而在这之前 `backend/app/` 里**没有任何 watchdog 实现**。也就是说这条
验收标准长期无法通过，但配置项、依赖声明、设计文档全都摆在那里 ——
没有测试就没人会发现"配置说了但没做"。

## 本文件的重点

监听器最危险的失败模式不是"不工作"，而是**安静地破坏数据**：

* `_index_upserts` 若误调 `_mark_missing`，库里 4900 行会被一次性
  全标成"已丢失"（见 `test_upsert_never_marks_unrelated_rows_missing`）；
* 忽略名单若与索引器不一致，临时区的 `.part` 半截文件会被收录进
  时间线（见 `test_rel_of_shares_ignore_rules_with_indexer`）；
* 防抖若写死模块常量，`index.debounce_sec` 就成了摆设，且
  `stats()` 报出的数字是假的（见 `test_debounce_sec_comes_from_config`）。

这些都不会抛异常、不会有日志，只会让用户看到错误的数据。
"""
from __future__ import annotations

import asyncio
import os
import queue
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import watcher as watcher_mod  # noqa: E402
from app.config import Config, RootConfig  # noqa: E402
from app.db import Database  # noqa: E402
from app.indexer import Indexer  # noqa: E402
from app.watcher import WatchCollector, Watcher, _rel_of, build_watcher  # noqa: E402


# --------------------------------------------------------------------------
# 夹具与工具
# --------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    d.init()
    yield d
    d.close()


def _cfg(tmp_path: Path, library: Path, *, watch: bool = True,
         debounce: float | None = None, mode: str = "readwrite") -> Config:
    index: dict[str, object] = {"watch": watch, "batch_size": 50}
    if debounce is not None:
        index["debounce_sec"] = debounce
    return Config({
        "roots": [{"id": "library", "path": str(library), "mode": mode,
                   "recursive": True}],
        "index": index,
    }, path=tmp_path / "data" / "config.json", data_dir=tmp_path / "data")


def _img(path: Path, size=(64, 48), color=(120, 80, 40)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(
        path, "JPEG" if path.suffix.lower() in (".jpg", ".jpeg") else "PNG")
    return path


def _scan(cfg: Config, db: Database, thumbs=None) -> object:
    idx = Indexer(cfg, db, thumbs=thumbs)
    return asyncio.run(idx.scan_root(cfg.root_by_id("library")))


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


def _run(coro):
    """在独立事件循环里跑一个协程（测试是同步的）。"""
    return asyncio.run(coro)


# ==========================================================================
# 一、WatchCollector：防抖与合并
# ==========================================================================

def test_same_path_events_are_merged_into_one_pending() -> None:
    """★ 一个文件写 10 次只该产生 1 个待办。

    不去重的代价很具体：手机上传一张照片，Windows 会连发
    created → modified → modified …，每个都索引一遍就是 10 轮
    EXIF 抽取 + 10 次 UPSERT。
    """
    root = RootConfig(id="r", path=Path("."), mode="readwrite")
    c = WatchCollector(debounce_sec=0.0)
    for _ in range(10):
        c.on_event("created", root, "a.jpg")
    assert c.depth == 1
    ready = c.take_ready()
    assert len(ready) == 1
    assert ready[0][:2] == ("created", "a.jpg")


def test_debounce_pushes_due_into_the_future() -> None:
    """静默期未过时不能取出 —— 否则就是"每来一个事件索引一次半截文件"。"""
    root = RootConfig(id="r", path=Path("."), mode="readwrite")
    c = WatchCollector(debounce_sec=30.0)
    c.on_event("modified", root, "a.jpg")
    assert c.depth == 1
    assert c.take_ready() == [], "静默期未过就取出来了"


def test_debounce_restarts_on_each_event() -> None:
    """静默期以**最后一次**事件起算，不是第一次。"""
    root = RootConfig(id="r", path=Path("."), mode="readwrite")
    c = WatchCollector(debounce_sec=0.4)
    c.on_event("modified", root, "a.jpg")
    time.sleep(0.25)
    c.on_event("modified", root, "a.jpg")      # 重新计时
    time.sleep(0.25)
    # 距第一次已 0.5s（> 0.4），但距最后一次只有 0.25s（< 0.4）
    assert c.take_ready() == [], "静默期必须从最后一次事件起算"
    time.sleep(0.25)
    assert len(c.take_ready()) == 1


def test_delete_is_never_debounced() -> None:
    """★ 删除立刻可处理。

    用户删了照片，手机上应马上看到"已丢失"，而不是等 2 秒。
    更关键的是删除没有"后续事件"可以合并，防抖只会让状态停在错误值上：
    文件已经不在了，库里仍是正常状态，此时点开就是一张打不开的图。
    """
    root = RootConfig(id="r", path=Path("."), mode="readwrite")
    c = WatchCollector(debounce_sec=3600.0)
    c.on_event("deleted", root, "a.jpg")
    ready = c.take_ready()
    assert len(ready) == 1 and ready[0][0] == "deleted"


def test_delete_wins_over_pending_create() -> None:
    """先建后删（同一路径）→ 最终动作必须是 deleted。

    反过来的顺序也测了：先删后建 → created。合并规则是"后者覆盖前者"，
    取到的必须是**最后一个**动作，否则会出现"照片已删除，时间线上
    却又出现一张打不开的图"。
    """
    root = RootConfig(id="r", path=Path("."), mode="readwrite")
    c = WatchCollector(debounce_sec=0.0)
    c.on_event("created", root, "a.jpg")
    c.on_event("deleted", root, "a.jpg")
    assert c.take_ready()[0][0] == "deleted"

    c.on_event("deleted", root, "b.jpg")
    c.on_event("created", root, "b.jpg")
    assert c.take_ready()[0][0] == "created"


def test_deletes_are_ordered_before_upserts() -> None:
    """★ 删除优先出队。

    用户对"删了没反应"最敏感；而且 upsert 会做 EXIF 抽取（慢），
    排在前面会让删除等上几秒。
    """
    root = RootConfig(id="r", path=Path("."), mode="readwrite")
    c = WatchCollector(debounce_sec=0.0)
    for i in range(5):
        c.on_event("created", root, f"up{i}.jpg")
    c.on_event("deleted", root, "gone.jpg")
    ready = c.take_ready()
    assert ready[0][:2] == ("deleted", "gone.jpg"), f"顺序错了：{[r[:2] for r in ready]}"


def test_take_ready_respects_limit() -> None:
    """单轮最多 limit 个：用户一次性拷 3000 张进来时不能一口气占住事件循环。"""
    root = RootConfig(id="r", path=Path("."), mode="readwrite")
    c = WatchCollector(debounce_sec=0.0)
    for i in range(500):
        c.on_event("created", root, f"a{i}.jpg")
    assert len(c.take_ready(limit=200)) == 200
    assert c.depth == 300, "取出的必须从待办里移除，否则会无限重复处理"
    assert len(c.take_ready(limit=200)) == 200
    assert len(c.take_ready(limit=200)) == 100


def test_empty_rel_is_ignored() -> None:
    root = RootConfig(id="r", path=Path("."), mode="readwrite")
    c = WatchCollector()
    c.on_event("created", root, "")
    assert c.depth == 0


def test_on_event_never_raises() -> None:
    """★ 在 watchdog 线程里抛异常会**杀掉监听线程**。

    那之后整个监听静默失效（没有任何日志、服务照常跑），用户只能
    靠"照片不再自动出现"察觉。所以 on_event 必须自己吞掉所有异常。
    """
    c = WatchCollector(debounce_sec=0.0)
    root = RootConfig(id="r", path=Path("."), mode="readwrite")
    # 破坏内部状态，让 `len(self._pending)` 这一行自己抛 TypeError。
    # 这不是"人为制造不可能的场景"：`_pending` 是普通属性，
    # 任何一次不小心把它赋成 None/列表的改动都会走到这里，
    # 而代价是整个监听线程死掉。
    c._pending = None            # type: ignore[assignment]
    c.on_event("created", root, "a.jpg")      # 不应抛出
    c._pending = {}                           # 恢复，免得影响其他断言


def test_max_pending_evicts_but_never_drops_deletes() -> None:
    """★ 待办表满了也不能丢删除事件。

    为什么需要上限：用户把整个 `D:\\照片备份`（十几万文件）拖进库目录。
    但**丢谁**是有讲究的 —— 丢掉的 upsert 下次全量扫描一定会收录；
    丢掉的 deleted 则没有任何后续触发点，库里会长期留着一行指向
    已不存在的文件（点开就是打不开的图，且永远不会自愈）。
    """
    root = RootConfig(id="r", path=Path("."), mode="readwrite")
    c = WatchCollector(debounce_sec=0.0)
    # 先灌满洪水（都是 upsert），最后再补一个删除。
    for i in range(watcher_mod.MAX_PENDING + 50):
        c.on_event("created", root, f"flood{i}.jpg")
    c.on_event("deleted", root, "important.jpg")

    assert c.depth <= watcher_mod.MAX_PENDING
    assert c.dropped > 0
    ready = [r[:2] for r in c.take_ready(limit=watcher_mod.MAX_PENDING)]
    assert ("deleted", "important.jpg") in ready, "删除事件被洪水挤掉了"


def test_eviction_never_drops_delete_even_when_deletes_are_old() -> None:
    """★ 删除事件**即使是最早登记的**也不能被挤掉。

    反例：库里有 2 万个待办，其中最早的一个是删除。若按"due 最小
    优先丢弃"，它正好是 due 最小的（删除恒为"现在"）—— 于是一次
    洪水就把最该保留的事件丢掉了。
    """
    root = RootConfig(id="r", path=Path("."), mode="readwrite")
    c = WatchCollector(debounce_sec=0.0)
    c.on_event("deleted", root, "oldest_delete.jpg")
    for i in range(watcher_mod.MAX_PENDING * 2):
        c.on_event("created", root, f"flood{i}.jpg")
    assert "oldest_delete.jpg" in c._pending, "最早的删除事件被丢弃了"


def test_eviction_drops_newest_upserts_first() -> None:
    """腾空间时留旧丢新：让积压的旧事件先被处理完，队列整体在推进。

    注意 `dropped` 不是 1 而是**一批**：每次插入只丢一个的话，每次
    插入都要遍历整个待办表（`min()` over 20000 项），而 `on_event`
    跑在事件循环线程里 —— 实测 2 万待办 + 4 万次插入要 36 秒，
    期间手机端所有请求超时。改成攒一批一次丢（见 EVICT_FRACTION）。
    """
    root = RootConfig(id="r", path=Path("."), mode="readwrite")
    c = WatchCollector(debounce_sec=0.0)
    c._pending.clear()
    # 手工填满，due 递增，便于判断谁被丢
    for i in range(watcher_mod.MAX_PENDING):
        c._pending[f"a{i}.jpg"] = watcher_mod._Pending(
            action="created", due=float(i), root=root)
    c.on_event("created", root, "newcomer.jpg")

    assert "a0.jpg" in c._pending, "最旧的不该被丢"
    assert "newcomer.jpg" in c._pending, "刚来的事件不该被丢"
    # 一次腾出一批（1/EVICT_FRACTION），不是精确的一个。
    assert c.dropped >= 1
    assert c.depth <= watcher_mod.MAX_PENDING
    # 丢的是最新的那些：due 最大的已经不在表里。
    assert f"a{watcher_mod.MAX_PENDING - 1}.jpg" not in c._pending


# ==========================================================================
# 二、_rel_of：安全边界
# ==========================================================================

def _root_for(lib: Path, *, mode: str = "readwrite") -> RootConfig:
    return RootConfig(id="library", path=lib, mode=mode, recursive=True)


def test_rel_of_normalizes_backslashes(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    _img(lib / "sub" / "a.jpg")
    root = _root_for(lib)
    assert _rel_of(root, str(lib / "sub" / "a.jpg")) == "sub/a.jpg"


def test_rel_of_rejects_outside_path(tmp_path: Path) -> None:
    """库外路径一律丢弃。"""
    lib = tmp_path / "lib"
    lib.mkdir()
    outside = tmp_path / "outside.jpg"
    _img(outside)
    assert _rel_of(_root_for(lib), str(outside)) is None


def test_rel_of_rejects_dotdot(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    assert _rel_of(_root_for(lib), str(lib / ".." / "x.jpg")) is None


def test_rel_of_shares_ignore_rules_with_indexer(tmp_path: Path) -> None:
    """★★ 忽略名单必须与索引器**共用同一份判断**。

    这是本模块最容易出错的地方：watchdog 报的是原始文件系统事件，
    它会忠实地报告 `<库根>/.photobridge_tmp/xxx.part`、`Thumbs.db`、
    `~$xxx.docx`、`*.jpg-lx2`。若这里用另一套规则，就会出现
    "扫描不收录、监听却收录"的分叉 —— 用户时间线上多出一堆半截
    上传文件和系统缩略图缓存。

    这里逐个验证：凡是索引器不收的，监听也必须不收。
    """
    lib = tmp_path / "lib"
    (lib / watcher_mod.sandbox.TMP_DIRNAME).mkdir(parents=True)
    _img(lib / "ok.jpg")
    # 一个白名单外的扩展名（小米回收站残留）
    (lib / "ghost.jpg-lx2").write_bytes(b"x")
    (lib / "Thumbs.db").write_bytes(b"x")
    (lib / "~$doc.docx").write_bytes(b"x")
    (lib / "note.tmp").write_bytes(b"x")

    root = _root_for(lib)
    assert _rel_of(root, str(lib / "ok.jpg")) == "ok.jpg"
    for bad in ("ghost.jpg-lx2", "Thumbs.db", "~$doc.docx", "note.tmp"):
        assert _rel_of(root, str(lib / bad)) is None, f"{bad} 不该被监听收录"
    assert _rel_of(
        root, str(lib / watcher_mod.sandbox.TMP_DIRNAME / "x.part")) is None


def test_rel_of_skips_directory_events(tmp_path: Path) -> None:
    """目录的创建/删除不改变 media 表（里面的文件各自有事件）。"""
    lib = tmp_path / "lib"
    (lib / "newdir").mkdir(parents=True)
    assert _rel_of(_root_for(lib), str(lib / "newdir")) is None


def test_rel_of_blocks_junction_escape(tmp_path: Path) -> None:
    """★ watchdog 的 recursive 会跟随 junction，报出库外路径。

    `ReadDirectoryChangesW` 与 `os.walk` 一样不看 reparse 位。一个放在
    库根下、指向 `C:\\` 的 junction，就能让监听器把整台机器的图片
    报成"库内新增"。`_rel_of` 里的 `sandbox.resolve` 逐段 lstat
    检查 reparse 位，是唯一的拦截点。
    """
    if os.name != "nt":
        pytest.skip("junction 只在 Windows 上可测")
    outside = tmp_path / "outside"
    _img(outside / "secret.jpg")
    lib = tmp_path / "lib"
    lib.mkdir()
    link = lib / "escape"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not link.exists():
        pytest.skip("无法创建 junction")

    root = _root_for(lib)
    assert _rel_of(root, str(link / "secret.jpg")) is None, \
        "junction 逃逸：库外的文件被当成了库内文件"


def test_rel_of_accepts_readonly_root(tmp_path: Path) -> None:
    """只读库也必须能监听 —— 监听是"读"操作。

    若这里错用了 need="write"，只读根上的照片就永远不会自动出现，
    而用户看到的是"我明明开了监听"。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    assert _rel_of(_root_for(lib, mode="readonly"), str(lib / "a.jpg")) == "a.jpg"


# ==========================================================================
# 三、配置读取：watch 开关与 debounce_sec
# ==========================================================================

def test_watch_false_disables_everything(tmp_path: Path, db: Database) -> None:
    """`index.watch=false` 时 start() 是空操作，且如实报告未运行。"""
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib, watch=False)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    assert w.enabled is False
    w.start()
    assert w.running is False
    assert w.stats()["running"] is False
    _run(w.stop())


def test_watch_true_enables(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib, watch=True)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    assert w.enabled is True


def test_debounce_sec_comes_from_config(tmp_path: Path, db: Database) -> None:
    """★ `index.debounce_sec` 必须真的生效，而不是被模块常量覆盖。

    曾经的写法是收集器内部直接 `now + DEBOUNCE_SEC`（模块常量 2.0），
    而 `Watcher._debounce_sec` 从配置读 —— 于是 `stats()` 报 0.5、
    实际生效 2.0。用户把 debounce_sec 调大以减少大文件重复索引，
    不会有任何效果，且没有任何报错。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib, debounce=7.5)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    assert w.stats()["debounce_sec"] == 7.5
    assert w._collector.debounce_sec == 7.5, "收集器没拿到配置值"

    # 真的影响行为：7.5 秒内取不出来
    root = cfg.root_by_id("library")
    w._collector.on_event("created", root, "a.jpg")
    assert w._collector.take_ready() == []


@pytest.mark.parametrize("bad", [-1, -0.5, "abc", None, [], {}])
def test_bad_debounce_falls_back_to_default(tmp_path: Path, db: Database,
                                            bad: object) -> None:
    """非法值退默认：负数会把"到期时刻"算到过去，字符串会让比较抛异常。

    ⚠️ **0 不在这里**，它是合法值（"不防抖、立即处理"）——
    见 `test_zero_debounce_means_process_immediately`。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    cfg.raw["index"]["debounce_sec"] = bad
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    assert w.stats()["debounce_sec"] == watcher_mod.DEBOUNCE_SEC


def test_zero_debounce_means_process_immediately(tmp_path: Path, db: Database) -> None:
    """`debounce_sec = 0` 是"立即处理"，必须原样生效而不是被改成 2.0。

    用户明确要求不防抖时，悄悄加 2 秒延迟是错的；而且 `stats()` 会
    报 2.0，看起来像"我配的 0 没生效"。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib, debounce=0.0)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    assert w.stats()["debounce_sec"] == 0.0
    root = cfg.root_by_id("library")
    w._collector.on_event("created", root, "a.jpg")
    assert len(w._collector.take_ready()) == 1, "0 秒防抖应立即可取"


def test_stats_reports_real_debounce(tmp_path: Path, db: Database) -> None:
    """报出来的必须是**实际生效**的值，不能是"重新读一遍配置"。"""
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib, debounce=3.0)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    assert w.stats()["debounce_sec"] == w._collector.debounce_sec


def test_build_watcher_never_returns_none(tmp_path: Path, db: Database) -> None:
    """`build_watcher` 不返回 None，这样 server.py 里少一处可忘的分支。"""
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = _cfg(tmp_path, lib, watch=False)
    assert build_watcher(cfg, db, indexer=None) is not None


# ==========================================================================
# 四、_mark_missing_rows：只标真的不在的
# ==========================================================================

def _watcher(cfg: Config, db: Database, *, bus=None) -> Watcher:
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db), bus=bus)
    w._roots = [cfg.root_by_id("library")]      # 绕过 start()，直接喂根
    return w


def test_mark_missing_only_marks_vanished_files(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    _img(lib / "keep.jpg")
    _img(lib / "gone.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)
    assert db.scalar("SELECT COUNT(*) FROM media WHERE missing = 0") == 2

    (lib / "gone.jpg").unlink()
    w = _watcher(cfg, db)
    w._mark_deleted([("deleted", "gone.jpg", cfg.root_by_id("library"))])

    assert db.scalar("SELECT missing FROM media WHERE rel_path='gone.jpg'") == 1
    assert db.scalar("SELECT missing FROM media WHERE rel_path='keep.jpg'") == 0
    assert w.stats()["missing"] == 1


def test_mark_missing_skips_file_that_still_exists(tmp_path: Path, db: Database) -> None:
    """★ "保存文件"在 Windows 上是**先删后建**，会触发 deleted 事件。

    那一瞬间文件确实不存在，但紧接着的 created 会把它建回来。
    如果收到 deleted 就无条件标 missing，用户会看到照片闪一下
    变灰再变回来 —— 而且这个中间态可能被正在下拉刷新的手机抓到。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    w = _watcher(cfg, db)
    w._mark_deleted([("deleted", "a.jpg", cfg.root_by_id("library"))])
    assert db.scalar("SELECT missing FROM media") == 0, \
        "文件还在，不该标 missing"
    assert w.stats()["missing"] == 0


def test_mark_missing_does_not_touch_disk(tmp_path: Path, db: Database) -> None:
    """★ 监听器**只改库、绝不删文件**。

    这是整条链路上最不能出错的一点：一个把用户的照片删掉的"监听器"
    是不可接受的。事件说"这个路径没了"，我们相信它并更新索引，
    但绝不去动磁盘 —— 用户的文件可能只是被临时移走（移动硬盘拔了、
    换了个目录），下次扫描会把它改回 missing=0。
    """
    lib = tmp_path / "lib"
    p = _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    w = _watcher(cfg, db)
    w._mark_deleted([("deleted", "a.jpg", cfg.root_by_id("library"))])
    assert p.exists(), "监听器删掉了用户的照片！"
    assert p.read_bytes()[:2] == b"\xff\xd8"


def test_mark_missing_ignores_unknown_paths(tmp_path: Path, db: Database) -> None:
    """库外的 rel 不该产生任何 UPDATE（沙箱拦住）。"""
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)
    w = _watcher(cfg, db)
    w._mark_deleted([("deleted", "../escape.jpg", cfg.root_by_id("library"))])
    assert db.scalar("SELECT COUNT(*) FROM media WHERE missing = 1") == 0


# ==========================================================================
# 五、增量索引：复用 Indexer，绝不误标 missing
# ==========================================================================

def test_upsert_indexes_new_file(tmp_path: Path, db: Database) -> None:
    """AC-2.15 的最小闭环：往库目录拷一张图 → 库里出现这一行。"""
    lib = tmp_path / "lib"
    _img(lib / "first.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    _img(lib / "copied_in.jpg", (80, 60))
    w = _watcher(cfg, db)
    root = cfg.root_by_id("library")
    _run(w._index_upserts([("created", "copied_in.jpg", root)]))

    assert db.scalar("SELECT COUNT(*) FROM media") == 2
    assert db.scalar(
        "SELECT COUNT(*) FROM media WHERE rel_path='copied_in.jpg'") == 1
    assert w.stats()["indexed"] == 1


def test_upsert_never_marks_unrelated_rows_missing(tmp_path: Path, db: Database) -> None:
    """★★★ 本文件最重要的一条。

    `_index_upserts` 处理的是"本轮事件涉及的几个文件"。如果它顺手调用了
    `_mark_missing(root.id, stats.seen_rels)`，而 `seen_rels` 里只有这
    几个文件，那么**库里其余全部行会被一次性标成"已丢失"**。

    真实场景：库里有 4915 张照片，用户往库目录拷进 1 张新图 →
    监听器处理这 1 个文件 → 4915 张全部变灰。这个 bug 不会抛异常、
    不会有日志，只会让用户以为照片被删光了。
    """
    lib = tmp_path / "lib"
    for i in range(50):
        _img(lib / f"a{i}.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)
    assert db.scalar("SELECT COUNT(*) FROM media WHERE missing = 0") == 50

    _img(lib / "new.jpg", (90, 70))
    w = _watcher(cfg, db)
    root = cfg.root_by_id("library")
    _run(w._index_upserts([("created", "new.jpg", root)]))

    missing = db.scalar("SELECT COUNT(*) FROM media WHERE missing = 1")
    assert missing == 0, f"增量索引误标了 {missing} 行为已丢失"
    assert db.scalar("SELECT COUNT(*) FROM media") == 51


def test_upsert_revives_missing_row(tmp_path: Path, db: Database) -> None:
    """★ 被标 missing 的文件回来了，监听必须把标记清掉。

    路径：文件被临时移走 → deleted 事件标 missing → 用户又移回来 →
    created 事件。此时文件 mtime/size 可能**一个字节都没变**，而
    `_load_known()` 会载入 missing=1 的行；若 `_index_files` 的判断
    不含 `prev["missing"]`，这行会走 unchanged 分支被跳过，missing
    永远停在 1 —— 文件就在磁盘上，用户却一直看到"照片已丢失"。
    """
    lib = tmp_path / "lib"
    p = _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    # 模拟"被移走 → 标 missing"（不动文件，只打脏标记，等价于
    # 移动硬盘拔掉又插回来：驱动器号相同、mtime 不变）
    db.execute("UPDATE media SET missing = 1")
    assert db.scalar("SELECT missing FROM media") == 1

    w = _watcher(cfg, db)
    root = cfg.root_by_id("library")
    _run(w._index_upserts([("created", "a.jpg", root)]))

    assert db.scalar("SELECT missing FROM media") == 0, "缺失标记没被清掉"
    assert p.exists()


def test_upsert_skips_vanished_file(tmp_path: Path, db: Database) -> None:
    """created 事件可能早于写入完成，也可能文件已被下一步操作删掉。

    此时应记 skipped 而不是抛异常 —— 随后的 created 会重新触发。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    w = _watcher(cfg, db)
    root = cfg.root_by_id("library")
    _run(w._index_upserts([("created", "never_existed.jpg", root)]))
    assert w.stats()["skipped"] == 1
    assert w.stats()["indexed"] == 0


def test_upsert_ignores_ignored_extensions(tmp_path: Path, db: Database) -> None:
    """即使事件漏过了 `_rel_of` 的过滤，索引层也必须再挡一次。"""
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    (lib / "ghost.jpg-lx2").write_bytes(b"not an image")
    w = _watcher(cfg, db)
    root = cfg.root_by_id("library")
    _run(w._index_upserts([("created", "ghost.jpg-lx2", root)]))
    assert db.scalar("SELECT COUNT(*) FROM media") == 1


def test_upsert_does_not_requeue_unchanged_thumbs(tmp_path: Path, db: Database) -> None:
    """未变的文件不能每来一次事件就重排一次缩略图。

    ⚠️ 前置条件是"缩略图**已经生成**"。若它还是 pending，重排是
    **正确行为**（`_index_files` 里有一条专门补入队的分支：上次
    可能没跑完）。所以这里先把状态改成 ok，只测"内容没变"这一条。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)

    class _T:
        def __init__(self) -> None:
            self.n = 0

        def enqueue(self, media_id: int) -> bool:
            self.n += 1
            return True

        def enqueue_many(self, ids) -> int:
            return sum(1 for i in ids if self.enqueue(i))

    t = _T()
    _scan(cfg, db, thumbs=t)
    db.execute("UPDATE media SET thumb_state = 'ok'")
    first = t.n

    w = build_watcher(cfg, db, indexer=Indexer(cfg, db, thumbs=t))
    w._roots = [cfg.root_by_id("library")]
    root = cfg.root_by_id("library")
    _run(w._index_upserts([("modified", "a.jpg", root)]))
    assert t.n == first, "内容没变、缩略图已就绪，却重排了缩略图"


def test_upsert_requeues_pending_thumbs(tmp_path: Path, db: Database) -> None:
    """★ 反向保护：缩略图还是 pending 时必须补入队。

    这条与上一条是一对。上一版测试断言"任何情况下都不重排"，
    它错了 —— 上次没跑完的 pending 缩略图若不补入队，那张照片
    在手机上就**永远是灰格子**，而且没有任何后续触发点。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)                      # 不带 thumbs → 全部 pending
    assert db.scalar("SELECT thumb_state FROM media") == "pending"

    class _T:
        def __init__(self) -> None:
            self.ids: list[int] = []

        def enqueue(self, media_id: int) -> bool:
            self.ids.append(media_id)
            return True

        def enqueue_many(self, ids) -> int:
            return sum(1 for i in ids if self.enqueue(i))

    t = _T()
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db, thumbs=t))
    w._roots = [cfg.root_by_id("library")]
    root = cfg.root_by_id("library")
    _run(w._index_upserts([("modified", "a.jpg", root)]))
    assert len(t.ids) == 1, "pending 缩略图没被补入队，用户会一直看到灰格子"


# ==========================================================================
# 六、事件发布：让手机端不用手动刷新
# ==========================================================================

def test_media_changed_published_on_new_file(tmp_path: Path, db: Database) -> None:
    """AC-2.15 的"无需手动重扫"依赖这条事件。

    没有它，用户要手动下拉刷新才能看到新照片 —— 那就不叫"自动"了。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    bus = _FakeBus()
    w = _watcher(cfg, db, bus=bus)
    root = cfg.root_by_id("library")
    _img(lib / "new.jpg", (70, 50))
    _run(w._index_upserts([("created", "new.jpg", root)]))

    kinds = [e["type"] for e in bus.events]
    assert "media_changed" in kinds, f"没发事件：{kinds}"
    ev = [e for e in bus.events if e["type"] == "media_changed"][0]
    assert ev["added"] == 1 and ev["root"] == "library"


def test_no_event_when_nothing_changed(tmp_path: Path, db: Database) -> None:
    """未变的文件不发事件 —— 否则手机端会被无意义的事件刷屏。"""
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    bus = _FakeBus()
    w = _watcher(cfg, db, bus=bus)
    root = cfg.root_by_id("library")
    _run(w._index_upserts([("modified", "a.jpg", root)]))
    assert [e["type"] for e in bus.events] == []


def test_bus_failure_does_not_break_indexing(tmp_path: Path, db: Database) -> None:
    """事件总线炸了不能影响索引 —— 照片进库比"推送通知"重要得多。"""
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    class _Boom:
        def publish(self, event: dict) -> None:
            raise RuntimeError("bus down")

    w = _watcher(cfg, db, bus=_Boom())
    root = cfg.root_by_id("library")
    _img(lib / "new.jpg", (70, 50))
    _run(w._index_upserts([("created", "new.jpg", root)]))
    assert db.scalar("SELECT COUNT(*) FROM media") == 2


# ==========================================================================
# 七、生命周期：可重复调用、优雅降级
# ==========================================================================

def test_start_stop_start_is_idempotent(tmp_path: Path, db: Database) -> None:
    """start/stop 可重复调用（托盘里"暂停/恢复监听"会用到）。"""
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)

    async def _scenario() -> None:
        w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
        w.start()
        w.start()                      # 重复 start 不该起第二个 Observer
        assert len(w._observers) == 1
        await w.stop()
        await w.stop()                 # 重复 stop 不该炸
        w.start()
        assert w.running is True
        await w.stop()

    _run(_scenario())


def test_start_without_indexer_is_noop(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    w = build_watcher(cfg, db, indexer=None)
    w.start()
    assert w.running is False
    _run(w.stop())


def test_start_with_missing_root_is_noop(tmp_path: Path, db: Database) -> None:
    """根目录不存在（移动硬盘没插）时不能崩，也不能报"正在监听"。"""
    cfg = _cfg(tmp_path, tmp_path / "not_mounted")
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    w.start()
    assert w.running is False
    _run(w.stop())


def test_stop_cancels_flush_task(tmp_path: Path, db: Database) -> None:
    """★ stop 必须取消事件循环协程。

    漏掉它的话，服务退出后协程还在跑，`_flush_loop` 会继续查一个
    已关闭的数据库连接（SQLite ProgrammingError），而且进程无法退出。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)

    async def _scenario() -> None:
        w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
        w.start()
        task = w._task
        assert task is not None
        await w.stop()
        assert task.cancelled() or task.done()
        assert w._task is None

    _run(_scenario())


def test_flush_loop_survives_exception(tmp_path: Path, db: Database) -> None:
    """★ 处理循环不能因一次异常退出。

    退出后的表现是"过一会儿就再也不自动收录了"，而服务照常运行、
    没有任何可见错误 —— 用户完全无从判断发生了什么。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib, debounce=0.0)

    async def _scenario() -> None:
        w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
        w._roots = [cfg.root_by_id("library")]
        w._running = True
        calls = {"n": 0}
        real = Watcher._process_ready

        async def _boom(self) -> None:      # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first pass explodes")
            await real(self)

        # ⚠️ 不能 `w._process_ready = _boom`：Watcher 用 `__slots__`
        # （没有 __dict__），实例属性赋值会抛 AttributeError。
        # 必须打**类**上的补丁，退出时还原。
        Watcher._process_ready = _boom      # type: ignore[method-assign]
        try:
            _img(lib / "new.jpg", (70, 50))
            w._collector.on_event("created", cfg.root_by_id("library"), "new.jpg")

            task = asyncio.create_task(w._flush_loop())
            await asyncio.sleep(watcher_mod.FLUSH_INTERVAL_SEC * 3)
            alive = not task.done()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        finally:
            Watcher._process_ready = real   # type: ignore[method-assign]
            await w.stop()

        assert alive, "处理循环因一次异常退出了"
        assert w.stats()["errors"] >= 1, "异常没被计数"

    _run(_scenario())


def test_disabled_watcher_methods_are_noop(tmp_path: Path, db: Database) -> None:
    """`index.watch=false` 时所有方法都是空操作，不抛异常。"""
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib, watch=False)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))

    async def _scenario() -> None:
        w.start()
        w.start()
        await w.stop()
        await w.stop()

    _run(_scenario())
    assert w.stats()["running"] is False
    assert w.stats()["roots"] == []


# ==========================================================================
# 八、队列：watchdog 线程 → 事件循环
# ==========================================================================

def test_drain_queue_moves_events_into_collector(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib, debounce=0.0)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    w._roots = [cfg.root_by_id("library")]

    w._events.put_nowait(("created", "library", "a.jpg"))
    w._events.put_nowait(("modified", "library", "b.jpg"))
    w._drain_queue()

    assert w.stats()["events"] == 2
    assert w._collector.depth == 2


def test_drain_queue_counts_unknown_root_as_skipped(tmp_path: Path, db: Database) -> None:
    """根目录在事件产生后被移除时，事件必须被丢弃而不是崩掉。"""
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    w._roots = []
    w._events.put_nowait(("created", "library", "a.jpg"))
    w._drain_queue()
    assert w.stats()["skipped"] == 1
    assert w.stats()["events"] == 0


def test_event_queue_is_bounded(tmp_path: Path, db: Database) -> None:
    """队列必须有上限，且满了要丢事件而不是阻塞 watchdog 线程。

    阻塞在 `put_nowait` 上会让 watchdog 的派发线程停住，操作系统
    缓冲随即填满并**丢弃**事件 —— 那才是真正无法恢复的情况。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    assert w._events.maxsize == watcher_mod.MAX_EVENTS
    for i in range(watcher_mod.MAX_EVENTS):
        w._events.put_nowait(("created", "library", f"a{i}.jpg"))
    assert w._events.full()
    with pytest.raises(queue.Full):
        w._events.put_nowait(("created", "library", "overflow.jpg"))


def test_handler_pushes_absolute_path_as_relative(tmp_path: Path, db: Database) -> None:
    """事件处理器把绝对路径转成 (action, root_id, rel) 三元组。

    ★ root_id 必须随事件传下去：多根（且有嵌套）时下游无法从 rel
    反推它属于哪个根 —— 只能靠"再 resolve 一遍所有根"来猜，
    那是 O(根数) 次 lstat 且对嵌套根会猜错。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    handler = watcher_mod._make_handler(
        w._collector, cfg.root_by_id("library"), w._events)

    class _Ev:
        def __init__(self, src: str, dest: str = "", is_directory: bool = False):
            self.src_path = src
            self.dest_path = dest
            self.is_directory = is_directory

    handler.on_created(_Ev(str(lib / "a.jpg")))
    handler.on_moved(_Ev(str(lib / "a.jpg"), str(lib / "b.jpg")))

    got = []
    while not w._events.empty():
        got.append(w._events.get_nowait())
    assert ("created", "library", "a.jpg") in got
    # 移动必须报两次：源消失 + 目标出现
    assert ("deleted", "library", "a.jpg") in got
    assert ("created", "library", "b.jpg") in got


def test_handler_ignores_directory_events(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    (lib / "sub").mkdir(parents=True)
    cfg = _cfg(tmp_path, lib)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    handler = watcher_mod._make_handler(
        w._collector, cfg.root_by_id("library"), w._events)

    class _Ev:
        def __init__(self, src: str):
            self.src_path = src
            self.dest_path = ""
            self.is_directory = True

    handler.on_created(_Ev(str(lib / "sub")))
    assert w._events.empty()


def test_handler_never_raises_on_garbage(tmp_path: Path, db: Database) -> None:
    """watchdog 线程里抛异常会杀掉监听 —— 处理器必须自己吞掉一切。"""
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    handler = watcher_mod._make_handler(
        w._collector, cfg.root_by_id("library"), w._events)

    class _Bad:
        @property
        def src_path(self):
            raise RuntimeError("bad event")

    handler.on_created(_Bad())        # 不应抛出
    handler.on_deleted(object())      # 缺属性也不应抛出


# ==========================================================================
# 九、端到端：真实文件系统事件（AC-2.15）
# ==========================================================================

def test_real_filesystem_event_end_to_end(tmp_path: Path, db: Database) -> None:
    """★★ AC-2.15 的真实闭环：往库目录拷一张图，监听器自动把它索引进去。

    前面所有用例都是"手工调 on_event / _index_upserts"。这一条不同：
    它启动**真的 watchdog Observer**，用真的文件复制触发操作系统事件，
    再等事件循环把它处理完。中间任何一环接错（忘了 schedule、
    recursive 没开、_rel_of 把路径判成库外、flush 循环没跑）
    都会让这条失败。

    ⚠️ 故意不依赖 `index.debounce_sec` 的默认 2 秒：这里设为 0.1 秒，
    让用例在 ~1 秒内结束而不是稳定地花 3 秒。
    """
    lib = tmp_path / "lib"
    _img(lib / "seed.jpg")
    cfg = _cfg(tmp_path, lib, debounce=0.1)
    _scan(cfg, db)
    assert db.scalar("SELECT COUNT(*) FROM media") == 1

    async def _scenario() -> None:
        w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
        w.start()
        assert w.running, "监听没起来"
        try:
            # 真的写一个文件进去（等价于用户从 U 盘拷一张照片）
            _img(lib / "copied.jpg", (96, 72))
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                if db.scalar(
                    "SELECT COUNT(*) FROM media WHERE rel_path='copied.jpg'"
                ):
                    return
            pytest.fail("15 秒内没有自动收录新照片（AC-2.15 不通过）")
        finally:
            await w.stop()

    _run(_scenario())


def test_real_delete_event_marks_missing_end_to_end(tmp_path: Path, db: Database) -> None:
    """真的删掉一个文件 → 监听器把该行标成 missing（而不是从库里删掉）。

    为什么是"标 missing"而不是"删行"：用户的文件可能只是被临时移走，
    删行会连带丢掉缩略图缓存、收藏、相册归属等一切用户数据。
    """
    lib = tmp_path / "lib"
    _img(lib / "seed.jpg")
    p = _img(lib / "doomed.jpg")
    cfg = _cfg(tmp_path, lib, debounce=0.1)
    _scan(cfg, db)
    assert db.scalar("SELECT COUNT(*) FROM media") == 2

    async def _scenario() -> None:
        w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
        w.start()
        try:
            p.unlink()
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                if db.scalar(
                    "SELECT missing FROM media WHERE rel_path='doomed.jpg'"
                ):
                    return
            pytest.fail("15 秒内没有标记为已丢失")
        finally:
            await w.stop()

    _run(_scenario())
    assert db.scalar("SELECT COUNT(*) FROM media") == 2, "行不该被删掉"


def test_real_tmp_part_file_is_not_indexed(tmp_path: Path, db: Database) -> None:
    """★ 真实场景：上传临时区的 .part 文件绝不能被监听器收录。

    这是"忽略名单分叉"最容易出事的路径 —— 用户正拿着手机传一张 5 MB
    的照片，电脑端监听器看到 `.photobridge_tmp/xxx.part` 不断变大，
    若收录了它，时间线上就会出现一张打不开的半截图。
    """
    lib = tmp_path / "lib"
    _img(lib / "seed.jpg")
    cfg = _cfg(tmp_path, lib, debounce=0.1)
    _scan(cfg, db)

    async def _scenario() -> None:
        w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
        w.start()
        try:
            tmp = lib / watcher_mod.sandbox.TMP_DIRNAME
            tmp.mkdir(exist_ok=True)
            (tmp / "abc.part").write_bytes(b"\xff\xd8" + b"x" * 4096)
            await asyncio.sleep(1.5)       # 足够越过防抖 + 一次 flush
            return
        finally:
            await w.stop()

    _run(_scenario())
    assert db.scalar("SELECT COUNT(*) FROM media") == 1
    assert db.scalar(
        "SELECT COUNT(*) FROM media WHERE rel_path LIKE '%part%'") == 0


def test_real_junction_created_in_library_is_not_followed(
        tmp_path: Path, db: Database) -> None:
    """★ 运行期造一个指向库外的 junction，监听器绝不能跟进去。

    静态扫描有 junction 测试（`test_indexer.py`），但监听走的是
    `ReadDirectoryChangesW` 另一条代码路径 —— 它同样不看 reparse 位。
    这条验证 `_rel_of` 的 `sandbox.resolve` 真的在事件路径上生效。
    """
    if os.name != "nt":
        pytest.skip("junction 只在 Windows 上可测")
    outside = tmp_path / "outside"
    _img(outside / "secret.jpg")
    lib = tmp_path / "lib"
    _img(lib / "seed.jpg")
    cfg = _cfg(tmp_path, lib, debounce=0.1)
    _scan(cfg, db)

    link = lib / "escape"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not link.exists():
        pytest.skip("无法创建 junction")

    async def _scenario() -> None:
        w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
        w.start()
        try:
            # 通过 junction 往里写一个新文件，触发库外路径的事件
            _img(link / "leaked.jpg", (70, 50))
            await asyncio.sleep(1.5)
            return
        finally:
            await w.stop()

    _run(_scenario())
    assert db.scalar(
        "SELECT COUNT(*) FROM media WHERE rel_path LIKE '%leaked%'") == 0, \
        "junction 逃逸：库外的照片被监听器收录了"
    assert db.scalar("SELECT COUNT(*) FROM media") == 1


# ==========================================================================
# 十、接线回归（AST）：server.py / state.py 的结构不能退化
# ==========================================================================

def _server_src() -> str:
    return (Path(__file__).resolve().parent.parent / "app" / "server.py"
            ).read_text(encoding="utf-8")


def test_serve_starts_watcher() -> None:
    """`serve()` 必须启动监听器 —— 否则 AC-2.15 永远不会通过。"""
    import ast

    tree = ast.parse(_server_src())
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "serve")
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    names = {ast.unparse(c.func) for c in calls}
    assert "st.watcher.start" in names, f"serve() 没启动监听：{sorted(names)}"


def test_watcher_starts_after_scan() -> None:
    """★ 监听必须在**启动扫描之后**启动。

    反过来的话，扫描期间产生的事件会与扫描抢同一批行：监听先按单文件
    路径打 missing=1（或插入一行），扫描的 `_mark_missing` 又重算一遍。
    结果虽然最终一致，但中间态会让手机端看到闪烁。
    """
    import ast

    tree = ast.parse(_server_src())
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "serve")

    def _line_of(pred) -> int:
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and pred(ast.unparse(n.func)):
                return n.lineno
        return -1

    scan = _line_of(lambda s: s == "_startup_scan")
    watch = _line_of(lambda s: s == "st.watcher.start")
    assert scan > 0, "serve() 里找不到启动扫描"
    assert watch > 0, "serve() 里找不到启动监听"
    assert watch > scan, "监听必须在启动扫描之后启动"


def test_shutdown_stops_watcher_first() -> None:
    """★ `_shutdown` 里监听必须**最先**停。

    它是唯一一个在别的线程里跑、并且会主动发起索引写入的组件。
    先停数据库再停监听，监听的回调会撞上已关闭的连接（SQLite 抛
    ProgrammingError，而它发生在 watchdog 线程里，堆栈看不出原因）。
    """
    import ast

    tree = ast.parse(_server_src())
    fn = next(n for n in tree.body
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "_shutdown")
    order = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            s = ast.unparse(n.func)
            if s in ("st.watcher.stop", "transport.stop"):
                order.append((n.lineno, s))
    order.sort()
    assert order, "找不到停止调用"
    assert order[0][1] == "st.watcher.stop", f"停止顺序不对：{order}"


def test_build_state_constructs_watcher() -> None:
    """`build_state` 必须构造监听器并交给 AppState。"""
    import ast

    tree = ast.parse(_server_src())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "build_state")
    src = ast.unparse(fn)
    assert "build_watcher(" in src, "build_state 没构造监听器"
    assert "watcher=watcher" in src, "监听器没交给 AppState"


def test_system_info_exposes_watch() -> None:
    """`/api/system/info` 必须暴露真实监听状态。

    这条字段存在的理由就是"配置说了但没做"曾经长期没人发现 ——
    接口里不报状态，用户与我们都无从判断。
    """
    p = (Path(__file__).resolve().parent.parent / "app" / "handlers"
         / "system.py")
    assert '"watch": st.watch_info()' in p.read_text(encoding="utf-8")


def test_watch_info_reports_disabled_without_watcher(tmp_path: Path) -> None:
    """没有监听器时如实报"未运行"，而不是假装 watch=true 就在监听。"""
    from app.state import AppState

    cfg = _cfg(tmp_path, tmp_path / "lib")
    db = Database(tmp_path / "t.db")
    db.init()
    try:
        st = AppState(cfg, db)
        info = st.watch_info()
        assert info["enabled"] is False
        assert info["running"] is False
        assert info["reason"]
    finally:
        db.close()


def test_watch_info_reports_running(tmp_path: Path, db: Database) -> None:
    from app.state import AppState

    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    w = build_watcher(cfg, db, indexer=Indexer(cfg, db))
    st = AppState(cfg, db, watcher=w)
    info = st.watch_info()
    assert info["enabled"] is True
    assert info["running"] is False        # 还没 start
    assert info["debounce_sec"] == watcher_mod.DEBOUNCE_SEC
