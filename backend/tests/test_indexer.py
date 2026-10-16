"""indexer.py 单元测试。

最重要的一组是 **junction 逃逸**：索引器自己遍历目录时必须跳过
junction/symlink，否则一个指向 C:\\ 的 junction 就能让索引器把整台
机器的图片扫进库里。`os.walk(followlinks=False)` 不防这个，
所以这里用真实 mklink 造出 junction 来验证。
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import indexer  # noqa: E402
from app.config import Config, RootConfig  # noqa: E402
from app.db import Database  # noqa: E402
from app.indexer import Indexer, ScanStats, _ext_of, _is_excluded_dir  # noqa: E402


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    d.init()
    yield d
    d.close()


def _cfg(tmp_path: Path, library: Path, *, mode: str = "readwrite",
         recursive: bool = True) -> Config:
    return Config({
        "roots": [{
            "id": "library", "path": str(library), "mode": mode,
            "recursive": recursive,
        }],
        "index": {"batch_size": 50},
    })


def _img(path: Path, size=(64, 48), color=(120, 80, 40),
         taken: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", size, color)
    kwargs = {}
    if taken:
        # ⚠️ DateTimeOriginal(0x9003) 必须写进 **Exif SubIFD(0x8769)**，
        # 不能直接挂顶层。Pillow 写顶层时 getexif().get(0x9003) 能读到，
        # 但那不是照片实际的存储结构，extract 会读不到（实测两者都踩过）。
        ex = Image.Exif()
        ex.get_ifd(0x8769)[0x9003] = taken
        kwargs["exif"] = ex.tobytes()
    im.save(path, "JPEG" if path.suffix.lower() in (".jpg", ".jpeg") else "PNG",
            **kwargs)
    return path


def _scan(cfg: Config, db: Database, thumbs=None,
          batch_size: int | None = None) -> ScanStats:
    kw = {} if batch_size is None else {"batch_size": batch_size}
    idx = Indexer(cfg, db, thumbs=thumbs, **kw)
    return asyncio.run(idx.scan_root(cfg.root_by_id("library")))


# --------------------------------------------------------------------------
# 扩展名与排除规则
# --------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "want"), [
    ("a.jpg", "jpg"),
    ("a.JPG", "jpg"),
    ("a.tar.gz", "gz"),
    ("noext", ""),
    ("a.jpg-lx2", "jpg-lx2"),   # ★ 小米回收站残留：必须不匹配任何白名单扩展名
    (".hidden", "hidden"),
])
def test_ext_of(name: str, want: str) -> None:
    assert _ext_of(name) == want


def test_jpg_lx2_is_not_indexed(tmp_path: Path, db: Database) -> None:
    """真实库里 22 个 .jpg-lx2 是小米相册回收站残留，不是图片。"""
    lib = tmp_path / "lib"
    _img(lib / "real.jpg")
    (lib / "recycle.jpg-lx2").write_bytes(b"\xff\xd8 not really a jpeg")
    cfg = _cfg(tmp_path, lib)

    stats = _scan(cfg, db)
    assert stats.added == 1
    assert stats.skipped_ext == 1
    assert db.scalar("SELECT COUNT(*) FROM media") == 1


def test_excluded_dirs_skipped(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    _img(lib / "ok.jpg")
    for d in ("@eaDir", ".git", "node_modules", ".photobridge_tmp", "$RECYCLE.BIN"):
        _img(lib / d / "inside.jpg")
    cfg = _cfg(tmp_path, lib)

    stats = _scan(cfg, db)
    assert stats.added == 1
    assert stats.skipped_dir == 5
    assert db.scalar("SELECT COUNT(*) FROM media") == 1


def test_photobridge_tmp_excluded(tmp_path: Path, db: Database) -> None:
    """上传临时区在库根下（同卷决策），必须排除，否则半截文件被索引。"""
    lib = tmp_path / "lib"
    _img(lib / "ok.jpg")
    _img(lib / ".photobridge_tmp" / "abc.part.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)
    assert db.scalar("SELECT COUNT(*) FROM media") == 1


def test_excluded_files_and_tmp(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    _img(lib / "ok.jpg")
    (lib / "Thumbs.db").write_bytes(b"x")
    (lib / "desktop.ini").write_bytes(b"x")
    _img(lib / "half.tmp")
    _img(lib / "~$draft.jpg")
    cfg = _cfg(tmp_path, lib)
    stats = _scan(cfg, db)
    assert stats.added == 1


def test_non_recursive(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    _img(lib / "top.jpg")
    _img(lib / "sub" / "deep.jpg")
    cfg = _cfg(tmp_path, lib, recursive=False)
    stats = _scan(cfg, db)
    assert stats.added == 1


# --------------------------------------------------------------------------
# ★ junction / symlink 逃逸（本文件最重要的一组）
# --------------------------------------------------------------------------

def _make_junction(link: Path, target: Path) -> bool:
    """用 mklink /J 创建 junction。不需要管理员权限。"""
    if os.name != "nt":
        return False
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and link.exists()


def test_junction_outside_library_is_not_followed(tmp_path: Path, db: Database) -> None:
    """★ 核心安全测试：库内的 junction 指向库外，索引器绝不能跟进去。

    `os.walk(followlinks=False)` 对 junction 无效（islink() 返回 False），
    一个指向 C:\\ 的 junction 就能把整台机器的图片扫进库。
    """
    outside = tmp_path / "outside"
    _img(outside / "secret.jpg")
    _img(outside / "nested" / "deep_secret.jpg")

    lib = tmp_path / "lib"
    _img(lib / "ok.jpg")
    link = lib / "escape"
    if not _make_junction(link, outside):
        pytest.skip("无法创建 junction（需要 Windows 且 mklink 可用）")

    # 前置确认：这个 junction 确实是 islink() 看不见的那种。
    # ⚠️ 必须用 os.lstat —— os.stat 会跟随 junction 返回目标目录的属性，
    # reparse 位就消失了（实测 stat 给 0x10，lstat 给 0x410）。
    st = os.lstat(link)
    assert not os.path.islink(link), "junction 不应被 islink 识别"
    assert getattr(st, "st_file_attributes", 0) & 0x400, "应有 reparse 位"

    cfg = _cfg(tmp_path, lib)
    stats = _scan(cfg, db)

    assert stats.added == 1, f"只应索引 ok.jpg，实际 {stats.added}"
    assert stats.skipped_link == 1
    names = {r["rel_path"] for r in db.query("SELECT rel_path FROM media")}
    assert names == {"ok.jpg"}, f"junction 逃逸！索引到了 {names}"


def test_junction_inside_library_also_skipped(tmp_path: Path, db: Database) -> None:
    """指向库**内**的 junction 同样跳过。

    规则保持一致比"聪明地判断是否越界"更好：一旦允许库内 junction，
    就会遇到循环链接（a -> b -> a）导致无限递归。
    """
    lib = tmp_path / "lib"
    _img(lib / "real" / "a.jpg")
    link = lib / "alias"
    if not _make_junction(link, lib / "real"):
        pytest.skip("无法创建 junction")

    cfg = _cfg(tmp_path, lib)
    stats = _scan(cfg, db)
    assert stats.added == 1
    assert stats.skipped_link == 1


def test_symlink_file_skipped(tmp_path: Path, db: Database) -> None:
    """符号链接**文件**同样跳过（它指向库外）。"""
    outside = tmp_path / "outside"
    _img(outside / "secret.jpg")
    lib = tmp_path / "lib"
    _img(lib / "ok.jpg")
    link = lib / "link.jpg"
    try:
        link.symlink_to(outside / "secret.jpg")
    except (OSError, NotImplementedError):
        pytest.skip("无权限创建符号链接")

    cfg = _cfg(tmp_path, lib)
    stats = _scan(cfg, db)
    assert stats.added == 1
    assert stats.skipped_link == 1


# --------------------------------------------------------------------------
# 索引正确性
# --------------------------------------------------------------------------

def test_basic_index(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    _img(lib / "a.jpg", (100, 50), taken="2024:05:12 18:30:00")
    _img(lib / "sub" / "b.png", (200, 100))
    cfg = _cfg(tmp_path, lib)

    stats = _scan(cfg, db)
    assert (stats.added, stats.errors) == (2, 0)

    row = db.query_one("SELECT * FROM media WHERE rel_path = 'a.jpg'")
    assert row["root_id"] == "library"
    assert row["file_name"] == "a.jpg"
    assert row["ext"] == "jpg"
    assert row["kind"] == "image"
    assert (row["width"], row["height"]) == (100, 50)
    assert row["taken_src"] == "exif"
    assert row["thumb_state"] == "pending"
    assert row["missing"] == 0

    # 子目录用 / 分隔（跨平台一致，前端不关心 Windows 的反斜杠）
    assert db.query_one("SELECT rel_path FROM media WHERE ext='png'")["rel_path"] == "sub/b.png"


def test_taken_at_fallback_chain(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    _img(lib / "exif.jpg", taken="2020:01:02 03:04:05")
    _img(lib / "IMG_20240512_183000.jpg")
    _img(lib / "IMG_0001.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    srcs = {r["rel_path"]: r["taken_src"]
            for r in db.query("SELECT rel_path, taken_src FROM media")}
    assert srcs["exif.jpg"] == "exif"
    assert srcs["IMG_20240512_183000.jpg"] == "filename"
    assert srcs["IMG_0001.jpg"] == "mtime"


def test_rescan_unchanged_is_noop(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    _img(lib / "b.jpg")
    cfg = _cfg(tmp_path, lib)

    s1 = _scan(cfg, db)
    s2 = _scan(cfg, db)
    assert s1.added == 2
    assert s2.added == 0 and s2.unchanged == 2
    assert db.scalar("SELECT COUNT(*) FROM media") == 2


def test_rescan_detects_mtime_change(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    p = _img(lib / "a.jpg", (64, 48))
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    # 换掉内容并改 mtime
    _img(p, (128, 96))
    os.utime(p, (time_now := 2_000_000_000, time_now))

    s2 = _scan(cfg, db)
    assert s2.updated == 1
    row = db.query_one("SELECT width, height, thumb_state, thumb_etag FROM media")
    assert (row["width"], row["height"]) == (128, 96)
    # 内容变了 → 缩略图必须作废重生成
    assert row["thumb_state"] == "pending"
    assert row["thumb_etag"] is None


def test_rescan_preserves_thumb_state_when_unchanged(tmp_path: Path, db: Database) -> None:
    """未修改的文件重新扫描时**不能**重置缩略图状态。

    否则每次启动程序都会重新生成 4915 张缩略图 —— 几分钟的 CPU 白烧。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)
    db.execute("UPDATE media SET thumb_state='ok', thumb_etag='sig'")

    _scan(cfg, db)
    row = db.query_one("SELECT thumb_state, thumb_etag FROM media")
    assert row["thumb_state"] == "ok"
    assert row["thumb_etag"] == "sig"


def test_missing_marked_not_deleted(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    a = _img(lib / "a.jpg")
    _img(lib / "b.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    a.unlink()
    s2 = _scan(cfg, db)

    assert s2.missing == 1
    assert db.scalar("SELECT COUNT(*) FROM media") == 2, "不能直接删行"
    assert db.scalar("SELECT missing FROM media WHERE rel_path='a.jpg'") == 1


def test_missing_file_returns_to_normal(tmp_path: Path, db: Database) -> None:
    """文件回来后 missing 必须重置为 0（用户拔了移动硬盘再插上）。"""
    lib = tmp_path / "lib"
    a = _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)
    data = a.read_bytes()
    a.unlink()
    _scan(cfg, db)
    assert db.scalar("SELECT missing FROM media") == 1

    a.write_bytes(data)
    os.utime(a, (2_000_000_000, 2_000_000_000))
    _scan(cfg, db)
    assert db.scalar("SELECT missing FROM media") == 0


def test_purge_missing(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    a = _img(lib / "a.jpg")
    _img(lib / "b.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)
    a.unlink()
    _scan(cfg, db)

    idx = Indexer(cfg, db)
    assert idx.purge_missing() == 1
    assert db.scalar("SELECT COUNT(*) FROM media") == 1


def test_missing_cleared_even_when_file_unchanged(tmp_path: Path, db: Database) -> None:
    """★ 文件没变（mtime/size 相同）但缺失标记为 1 → 重扫必须清掉。

    上面那条 `test_missing_file_returns_to_normal` 之所以能过，是因为它
    `os.utime()` 改了 mtime，于是走了"文件已修改"的正常分支。真正会卡住
    的是**文件一个字节都没变**的情况：

      `_load_known()` 会把 missing=1 的行也载入内存，而扫描循环原来是
      `if prev is not None and not self._changed(prev, st): continue`
      —— missing 不参与判断，于是这行被判为"未变"，直接跳过 upsert，
      `missing` 永远停在 1。文件明明在磁盘上，用户却一直看到"照片已丢失"。

    触发场景都是真的：移动硬盘拔了又插回来（驱动器号相同、mtime 不变）、
    某个根目录临时不可读导致 `_mark_missing` 误标、用户手动改过数据库。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)
    assert db.scalar("SELECT missing FROM media") == 0

    # 只把标记打脏，**不动文件的 mtime/size**
    db.execute("UPDATE media SET missing = 1")
    assert db.scalar("SELECT missing FROM media") == 1

    stats = _scan(cfg, db)

    assert db.scalar("SELECT missing FROM media") == 0, "文件没变也必须清掉缺失标记"
    assert stats.missing == 0, "本轮不该再把它标成丢失"


def test_missing_flag_does_not_break_unchanged_counting(tmp_path: Path, db: Database) -> None:
    """正常（missing=0）且未变的文件仍要计入 unchanged，不能每次全量重写。

    这是上一条的**反向保护**：修 missing 时最容易写成"永不跳过"，
    那会让每轮扫描都重新读 4915 张 EXIF，扫描从秒级变成分钟级。
    """
    lib = tmp_path / "lib"
    for i in range(5):
        _img(lib / f"{i}.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)

    stats = _scan(cfg, db)
    assert stats.unchanged == 5
    assert stats.updated == 0


def test_corrupt_file_does_not_abort_scan(tmp_path: Path, db: Database) -> None:
    """一个坏文件不能中断整轮扫描。"""
    lib = tmp_path / "lib"
    _img(lib / "good.jpg")
    (lib / "bad.jpg").write_bytes(b"not an image at all")
    _img(lib / "good2.jpg")
    cfg = _cfg(tmp_path, lib)

    stats = _scan(cfg, db)
    assert stats.added == 3, "坏文件也应入库（只是没有 EXIF）"
    assert db.scalar("SELECT COUNT(*) FROM media WHERE ext='jpg'") == 3
    row = db.query_one("SELECT taken_src FROM media WHERE file_name='bad.jpg'")
    assert row["taken_src"] == "mtime"


def test_batch_flush_across_boundaries(tmp_path: Path, db: Database) -> None:
    """批次边界（batch_size=50）必须正确分多次写入且不漏不重。"""
    lib = tmp_path / "lib"
    for i in range(123):
        _img(lib / f"p{i:03d}.jpg", (32, 32))
    cfg = _cfg(tmp_path, lib)
    stats = _scan(cfg, db)
    assert stats.added == 123
    assert db.scalar("SELECT COUNT(*) FROM media") == 123
    assert db.scalar("SELECT COUNT(DISTINCT rel_path) FROM media") == 123


def test_scan_empty_dir(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = _cfg(tmp_path, lib)
    stats = _scan(cfg, db)
    assert stats.added == 0 and stats.errors == 0


def test_scan_missing_root_does_not_crash(tmp_path: Path, db: Database) -> None:
    """根目录不存在时记录错误但**不能抛异常**（配置指向了拔掉的移动硬盘）。"""
    cfg = _cfg(tmp_path, tmp_path / "does_not_exist")
    stats = _scan(cfg, db)
    assert stats.errors >= 1
    assert stats.aborted is not None


def test_chinese_and_deep_paths(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    deep = lib / "青甘环线" / "2024年05月" / "a" / "b" / "c"
    _img(deep / "敦煌.jpg")
    cfg = _cfg(tmp_path, lib)
    stats = _scan(cfg, db)
    assert stats.added == 1
    rel = db.scalar("SELECT rel_path FROM media")
    assert rel == "青甘环线/2024年05月/a/b/c/敦煌.jpg"


# --------------------------------------------------------------------------
# 缩略图入队
# --------------------------------------------------------------------------

class _FakeThumbs:
    def __init__(self, accept: bool = True) -> None:
        self.ids: list[int] = []
        self.accept = accept

    def enqueue(self, media_id: int) -> bool:
        if self.accept:
            self.ids.append(media_id)
        return self.accept

    def enqueue_many(self, media_ids) -> int:
        n = 0
        for i in media_ids:
            if self.enqueue(i):
                n += 1
        return n


def test_thumbs_enqueued_for_new_files(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    for i in range(5):
        _img(lib / f"a{i}.jpg")
    cfg = _cfg(tmp_path, lib)
    fake = _FakeThumbs()
    stats = _scan(cfg, db, thumbs=fake)  # type: ignore[arg-type]
    assert stats.thumb_queued == 5
    assert len(fake.ids) == 5
    assert len(set(fake.ids)) == 5


def test_thumbs_not_requeued_when_queue_full(tmp_path: Path, db: Database) -> None:
    """队列满时丢弃是正确的：状态仍是 pending，下次扫描会补。"""
    lib = tmp_path / "lib"
    for i in range(3):
        _img(lib / f"a{i}.jpg")
    cfg = _cfg(tmp_path, lib)
    fake = _FakeThumbs(accept=False)
    stats = _scan(cfg, db, thumbs=fake)  # type: ignore[arg-type]
    assert stats.thumb_queued == 0
    assert db.scalar("SELECT COUNT(*) FROM media WHERE thumb_state='pending'") == 3


def test_pending_files_requeued_on_rescan(tmp_path: Path, db: Database) -> None:
    """上次没跑完的 pending 缩略图，重新扫描时要补入队。"""
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    _scan(cfg, db)                       # 第一次不带 thumbs，全部 pending

    fake = _FakeThumbs()
    stats = _scan(cfg, db, thumbs=fake)  # type: ignore[arg-type]
    assert stats.unchanged == 1
    assert stats.thumb_queued == 1


# --------------------------------------------------------------------------
# 单文件增量索引（上传后用）
# --------------------------------------------------------------------------

def test_index_one_file(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = _cfg(tmp_path, lib)
    idx = Indexer(cfg, db)
    root = cfg.root_by_id("library")

    p = _img(lib / "new.jpg", (100, 100))
    mid = idx.index_one_file(root, "new.jpg")
    assert mid is not None
    row = db.query_one("SELECT * FROM media WHERE id = ?", (mid,))
    assert row["rel_path"] == "new.jpg" and row["width"] == 100


def test_index_one_file_rejects_escape(tmp_path: Path, db: Database) -> None:
    """上传后的增量索引也必须过沙箱 —— 否则就是一条绕过校验的写库路径。"""
    lib = tmp_path / "lib"
    lib.mkdir()
    outside = _img(tmp_path / "outside.jpg")
    cfg = _cfg(tmp_path, lib)
    idx = Indexer(cfg, db)

    assert idx.index_one_file(cfg.root_by_id("library"), "../outside.jpg") is None
    assert db.scalar("SELECT COUNT(*) FROM media") == 0


def test_index_one_file_missing_returns_none(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = _cfg(tmp_path, lib)
    idx = Indexer(cfg, db)
    assert idx.index_one_file(cfg.root_by_id("library"), "nope.jpg") is None


# --------------------------------------------------------------------------
# 取消
# --------------------------------------------------------------------------

def test_cancel_stops_scan(tmp_path: Path, db: Database) -> None:
    """扫描中途取消必须尽快停下，而不是跑完全部文件。

    取消请求由外部（用户点"停止"、程序要退出）随时发出，因此这里
    在第一批写入后触发取消，模拟真实时序。
    """
    lib = tmp_path / "lib"
    for i in range(60):
        _img(lib / f"c{i:03d}.jpg", (16, 16))
    cfg = _cfg(tmp_path, lib)

    idx = Indexer(cfg, db, batch_size=5)
    original_flush = idx._flush
    calls = {"n": 0}

    def flush_and_cancel(batch):
        ids = original_flush(batch)
        calls["n"] += 1
        if calls["n"] == 1:
            idx.cancel()          # 第一批写完后请求取消
        return ids

    idx._flush = flush_and_cancel  # type: ignore[method-assign]

    stats = asyncio.run(idx.scan_root(cfg.root_by_id("library")))
    assert stats.aborted == "用户中止"
    assert stats.added < 60, f"应提前停止，实际写入 {stats.added}"


def test_cancel_is_reset_between_scans(tmp_path: Path, db: Database) -> None:
    """上一轮的取消不能影响下一轮。

    若不重置，用户点了"停止"之后再点"重新扫描"会立刻以"用户中止"
    结束，表现为"重新扫描没反应"。
    """
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    idx = Indexer(cfg, db)

    idx.cancel()
    s1 = asyncio.run(idx.scan_root(cfg.root_by_id("library")))
    assert s1.aborted == "用户中止" or s1.added == 1

    s2 = asyncio.run(idx.scan_root(cfg.root_by_id("library")))
    assert s2.aborted is None
    assert s2.unchanged == 1


def test_flush_error_does_not_lose_previous_batches(tmp_path: Path, db: Database) -> None:
    """某一批写库失败时，之前已写入的批次必须保留（不能整轮回滚）。"""
    lib = tmp_path / "lib"
    for i in range(12):
        _img(lib / f"d{i:02d}.jpg", (16, 16))
    cfg = _cfg(tmp_path, lib)
    idx = Indexer(cfg, db, batch_size=5)
    original_flush = idx._flush
    calls = {"n": 0}

    def flaky(batch):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated db failure")
        return original_flush(batch)

    idx._flush = flaky  # type: ignore[method-assign]
    stats = asyncio.run(idx.scan_root(cfg.root_by_id("library")))

    assert stats.aborted is not None
    assert db.scalar("SELECT COUNT(*) FROM media") == 5, "第一批应已落库"


def test_invalid_orientation_does_not_lose_whole_batch(
        tmp_path: Path, db: Database) -> None:
    """一个坏 EXIF 值不能让同批 500 张照片一起消失。

    真实库 D:\\Photos 里有 73 个文件写着 Orientation=0（畸形 EXIF），
    而 media 表有 CHECK(orientation BETWEEN 1 AND 8)。executemany 是全成或全败，
    所以坏值必须在校验层就被归零，否则整批 500 行一起失败，
    用户看到"扫描完成"但库里少 500 张照片。
    """
    lib = tmp_path / "lib"
    # 造一张 Orientation=0 的图（绕开 _img 的辅助函数，直接写畸形值）
    lib.mkdir(parents=True)
    im = Image.new("RGB", (32, 32), (10, 20, 30))
    ex = Image.Exif()
    ex[0x0112] = 0                      # Orientation=0，非法
    im.save(lib / "bad_orient.jpg", "JPEG", exif=ex.tobytes())
    for i in range(5):
        _img(lib / f"ok{i}.jpg", (16, 16))

    cfg = _cfg(tmp_path, lib)
    stats = _scan(cfg, db, batch_size=2)   # 故意让坏值和其他图同批

    assert stats.added == 6, "坏值照片也应入库（orientation 记为 NULL）"
    assert db.scalar("SELECT COUNT(*) FROM media") == 6
    assert db.scalar("SELECT COUNT(*) FROM media WHERE orientation IS NULL") == 6


def test_flush_degrades_to_per_row_on_integrity_error(
        tmp_path: Path, db: Database) -> None:
    """整批写入被 CHECK 拒绝时，必须逐行降级，只丢掉真正坏的行。"""
    lib = tmp_path / "lib"
    for i in range(4):
        _img(lib / f"g{i}.jpg", (16, 16))
    cfg = _cfg(tmp_path, lib)
    idx = Indexer(cfg, db, batch_size=4)

    good = idx._flush_batch  # 保留原始批量实现
    original_build = idx._build_row
    calls = {"n": 0}

    async def build_with_one_bad(root, rel, st, prev):
        row = await original_build(root, rel, st, prev)
        calls["n"] += 1
        if calls["n"] == 2:
            row = list(row)
            row[20] = 99          # orientation 列 → 触发 CHECK 失败
            row = tuple(row)
        return row

    idx._build_row = build_with_one_bad  # type: ignore[method-assign]
    stats = asyncio.run(idx.scan_root(cfg.root_by_id("library")))

    # 4 张里 3 张必须落库，1 张被跳过
    assert db.scalar("SELECT COUNT(*) FROM media") == 3
    assert stats.aborted is None, "单行坏值不应中止整轮扫描"
    assert good is not None


def test_stats_dict_and_summary(tmp_path: Path, db: Database) -> None:
    lib = tmp_path / "lib"
    _img(lib / "a.jpg")
    cfg = _cfg(tmp_path, lib)
    stats = _scan(cfg, db)
    d = stats.as_dict()
    assert d["root_id"] == "library" and d["added"] == 1
    assert "新增 1" in stats.summary()
    assert stats.elapsed >= 0
