"""`POST /api/items/move` 与 `POST /api/items/rename` 的测试。

这两个接口是**唯一会改动用户照片在磁盘上位置**的接口（删除走回收站、
上传只新增），所以测试的重点不是"正常路径能跑通"，而是：

1. **不丢数据**：任何失败路径下源文件必须还在（跨卷复制中途失败尤其）；
2. **不覆盖**：目标已有同名文件时绝不能盖掉它 —— 这是唯一可能造成
   **永久数据丢失**的软件缺陷（见 docs/11 的 R-004）；
3. **不越界**：`..`、绝对路径、路径分隔符必须被拒；
4. **不留假记录**：库里 rel_path 必须与磁盘一致，否则下次扫描标 missing。

## 为什么用真实文件而不是 mock

移动的核心风险全在文件系统语义里（Windows 跨卷 `os.replace` 抛
WinError 17、`O_EXCL` 的原子性、大小写不敏感）。mock 掉之后测的只是
"我的 mock 被调用过"，而真 bug 恰恰在 mock 里看不见。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.core import Ctx, Router  # noqa: E402
from app.db import Database  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.handlers import browse  # noqa: E402
from app.state import AppState  # noqa: E402


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture()
def library(tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    lib.mkdir()
    return lib


def _img(path: Path, size=(80, 60), color=(200, 100, 50),
         taken: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", size, color)
    kw = {}
    if taken:
        ex = Image.Exif()
        ex.get_ifd(0x8769)[0x9003] = taken       # 必须写 SubIFD
        kw["exif"] = ex.tobytes()
    im.save(path, "JPEG", **kw)
    return path


def _state(tmp_path: Path, library: Path, *, mode: str = "readwrite",
           allow_move: bool = True) -> AppState:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    cfg = Config({
        "app": {"mode": mode},
        "roots": [{"id": "library", "path": str(library), "mode": mode}],
        "thumbs": {"sizes": [320, 1600]},
        "auth": {"allow_delete": True, "delete_to_recycle_bin": True,
                 "allow_move": allow_move},
        "index": {"batch_size": 50},
    }, path=data / "config.json", data_dir=data)
    db = Database(cfg.db_path)
    db.init()
    return AppState(cfg, db)


def _router() -> Router:
    r = Router()
    for method, pattern, handler in [
        ("POST", "/api/items/move", browse.move_items),
        ("POST", "/api/items/rename", browse.rename_item),
        ("GET", "/api/items/{media_id}", browse.get_item),
    ]:
        r.add(method, pattern, handler)
    return r


def _ctx(st: AppState, method: str, path: str, *, query: dict | None = None,
         body: bytes | None = None, params: dict | None = None) -> Ctx:
    q: dict[str, list[str]] = {}
    for k, v in (query or {}).items():
        q[k] = v if isinstance(v, list) else [str(v)]

    async def _stream():
        if body:
            yield body

    return Ctx(method=method, path=path, query=q, headers={},
               body=_stream() if body is not None else None,
               params=params or {}, app=st)


def _call(st: AppState, method: str, path: str, **kw):
    return asyncio.run(_router().dispatch(_ctx(st, method, path, **kw)))


def _json(reply) -> dict:
    body = reply.body
    if not isinstance(body, bytes):
        body = asyncio.run(_drain(body))
    return json.loads(body)


async def _drain(agen) -> bytes:
    out = bytearray()
    async for chunk in agen:
        out.extend(chunk)
    return bytes(out)


def _seed(st: AppState, library: Path) -> dict[str, int]:
    """造 3 张照片并索引，返回 `{名字: media_id}`。"""
    from app.indexer import Indexer

    _img(library / "2024" / "2024-05" / "a.jpg", taken="2024:05:12 18:30:00")
    _img(library / "2024" / "2024-05" / "b.jpg", taken="2024:05:12 18:30:01")
    _img(library / "2024" / "2024-05" / "c.jpg", taken="2024:05:12 18:30:02")
    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))
    rows = st.db.query("SELECT id, file_name FROM media")
    return {r["file_name"]: int(r["id"]) for r in rows}


def _rel(st: AppState, mid: int) -> str:
    return st.db.scalar("SELECT rel_path FROM media WHERE id=?", (mid,))


def _body(**kw) -> bytes:
    return json.dumps(kw).encode("utf-8")


def _audit_lines(st: AppState) -> list[str]:
    p = st.cfg.data_path("audit.log")
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln]


# --------------------------------------------------------------------------
# 移动：正常路径
# --------------------------------------------------------------------------

def test_move_relocates_file_and_updates_db(tmp_path, library) -> None:
    """最基本的：文件真的动了，库里 rel_path 跟着变，内容没坏。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids["a.jpg"]
    before = (library / "2024" / "2024-05" / "a.jpg").read_bytes()

    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=[mid], target_dir="2024/精选")))

    assert b["failed"] == []
    assert len(b["moved"]) == 1
    assert b["moved"][0]["renamed"] is False
    assert b["moved"][0]["to"] == "2024/精选/a.jpg"
    assert b["target_dir"] == "2024/精选"
    dst = library / "2024" / "精选" / "a.jpg"
    assert dst.exists()
    assert dst.read_bytes() == before, "移动不能改变文件内容"
    assert not (library / "2024" / "2024-05" / "a.jpg").exists()
    assert _rel(st, mid) == "2024/精选/a.jpg"


def test_move_creates_target_dir_recursively(tmp_path, library) -> None:
    """目标目录不存在时要 mkdir -p，而不是报错。

    手机端"新建相册并移入"是同一个动作，分成两步会让前端状态很难处理。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=[ids["a.jpg"]], target_dir="a/b/c/d")))
    assert b["failed"] == []
    assert (library / "a" / "b" / "c" / "d" / "a.jpg").exists()


def test_move_batch_of_many(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=list(ids.values()), target_dir="全部")))
    assert len(b["moved"]) == 3
    assert b["failed"] == []
    assert len(list((library / "全部").iterdir())) == 3


def test_move_to_root_dir(tmp_path, library) -> None:
    """target_dir 为空串表示库根 —— 不能因为"空"就当成参数缺失。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=[ids["a.jpg"]], target_dir="")))
    assert b["failed"] == []
    assert (library / "a.jpg").exists()


def test_move_to_same_dir_is_skipped_not_error(tmp_path, library) -> None:
    """目标就是当前目录 → 静默成功。

    手机端"全选后移动到当前相册"是常见误操作；报错会让用户以为
    自己弄坏了什么，而实际什么都没发生。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=[ids["a.jpg"]], target_dir="2024/2024-05")))
    assert b["moved"] == []
    assert b["skipped"] == [ids["a.jpg"]]
    assert b["failed"] == []
    assert (library / "2024" / "2024-05" / "a.jpg").exists()


def test_move_writes_audit_with_from_and_to(tmp_path, library) -> None:
    """审计必须同时有 from 和 to —— 只记一个无法回答"它去哪了"。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    _call(st, "POST", "/api/items/move",
          body=_body(ids=[ids["a.jpg"]], target_dir="新家"))
    lines = [ln for ln in _audit_lines(st) if "\tmove\t" in ln]
    assert len(lines) == 1
    detail = json.loads(lines[0].split("\t")[4])
    assert detail["from"] == "2024/2024-05/a.jpg"
    assert detail["to"] == "新家/a.jpg"
    assert detail["media_id"] == ids["a.jpg"]


# --------------------------------------------------------------------------
# 移动：冲突处理（唯一可能永久丢数据的路径）
# --------------------------------------------------------------------------

def test_move_conflict_rename_never_overwrites(tmp_path, library) -> None:
    """★ 默认 on_conflict=rename：目标已存在同名文件时**绝不能覆盖**。

    这是唯一可能造成永久数据丢失的软件缺陷（docs/11 R-004）。
    断言两侧内容都还在且各自可辨认 —— 只断言"文件存在"是不够的，
    覆盖后文件同样存在，只是内容变成了另一个。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids["a.jpg"]
    # 在目标目录放一个同名但内容不同的文件（库里还不知道它）
    other = _img(library / "2024" / "精选" / "a.jpg", color=(0, 0, 255))
    original_bytes = other.read_bytes()

    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=[mid], target_dir="2024/精选")))

    assert b["failed"] == []
    assert len(b["moved"]) == 1
    assert b["moved"][0]["renamed"] is True
    assert b["moved"][0]["to"] == "2024/精选/a_1.jpg"
    # 原有的那个文件一个字节都没变
    assert other.read_bytes() == original_bytes, "★ 覆盖了目标文件 = 永久丢数据"
    assert (library / "2024" / "精选" / "a_1.jpg").exists()
    assert _rel(st, mid) == "2024/精选/a_1.jpg"


def test_move_conflict_skip(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    _img(library / "dest" / "a.jpg")
    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=[ids["a.jpg"]], target_dir="dest",
                               on_conflict="skip")))
    assert b["skipped"] == [ids["a.jpg"]]
    assert b["moved"] == []
    assert (library / "2024" / "2024-05" / "a.jpg").exists(), "跳过时源文件必须原地不动"


def test_move_conflict_fail(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    _img(library / "dest" / "a.jpg")
    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=[ids["a.jpg"]], target_dir="dest",
                               on_conflict="fail")))
    assert len(b["failed"]) == 1
    assert b["failed"][0]["code"] == "E3012"
    assert (library / "2024" / "2024-05" / "a.jpg").exists()


def test_move_conflict_rename_avoids_all_taken_names(tmp_path, library) -> None:
    """已有 a.jpg、a_1.jpg、a_2.jpg 时必须跳到 a_3.jpg。

    只试一次 `_1` 的实现会在这里把 a_1 覆盖掉。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    for n in ("a.jpg", "a_1.jpg", "a_2.jpg"):
        _img(library / "dest" / n)
    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=[ids["a.jpg"]], target_dir="dest")))
    assert b["moved"][0]["to"] == "dest/a_3.jpg"


def test_move_conflict_rename_sees_unindexed_files_on_disk(tmp_path, library) -> None:
    """★ 磁盘上有但库里没有的文件也必须被避开。

    库里查"已占用名字"只能覆盖已索引的文件；用户手动往库里拖一张
    照片而扫描还没跑到时，库查不到它 —— 此时必须退回文件系统探测，
    否则会把它覆盖掉。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    _img(library / "dest" / "a.jpg")       # 磁盘上有，库里没有（未扫描）
    assert st.db.scalar(
        "SELECT COUNT(*) FROM media WHERE rel_path = ?", ("dest/a.jpg",)) == 0

    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=[ids["a.jpg"]], target_dir="dest")))
    assert b["failed"] == []
    assert b["moved"][0]["to"] == "dest/a_1.jpg"
    assert (library / "dest" / "a.jpg").exists()
    assert (library / "dest" / "a_1.jpg").exists()


def test_move_batch_with_identical_names_does_not_collide_within_batch(
    tmp_path, library
) -> None:
    """★ 同一批里两个不同目录的同名文件移到一起时，第二个必须改名。

    这是最容易被忽略的冲突：两次 `dst.exists()` 检查之间没有任何
    同步，第一张搬完之前第二张看到的目标还是"不存在"。
    """
    st = _state(tmp_path, library)
    _img(library / "x" / "same.jpg")
    _img(library / "y" / "same.jpg")
    from app.indexer import Indexer
    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))
    ids = [int(r["id"]) for r in st.db.query("SELECT id FROM media ORDER BY id")]

    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=ids, target_dir="merged")))

    assert b["failed"] == []
    names = sorted(p.name for p in (library / "merged").iterdir())
    assert names == ["same.jpg", "same_1.jpg"], names
    assert len(b["moved"]) == 2


# --------------------------------------------------------------------------
# 移动：参数校验与越界
# --------------------------------------------------------------------------

def test_move_rejects_dotdot(tmp_path, library) -> None:
    """★ `..` 必须 400，且不能有任何文件被移动。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "POST", "/api/items/move",
              body=_body(ids=[ids["a.jpg"]], target_dir="../outside"))
    assert r.status == 400
    assert (library / "2024" / "2024-05" / "a.jpg").exists()
    assert not (tmp_path / "outside").exists()


def test_move_rejects_absolute_path(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "POST", "/api/items/move",
              body=_body(ids=[ids["a.jpg"]], target_dir="C:/Windows"))
    assert r.status == 400


def test_move_rejects_bad_params(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    for body in (
        {"ids": [], "target_dir": "x"},
        {"ids": "notalist", "target_dir": "x"},
        {"ids": [1], "target_dir": 123},
        {"ids": [1], "target_dir": "x", "on_conflict": "explode"},
        {"ids": [1], "target_dir": "x", "on_conflict": "skip"},   # ok
    ):
        r = _call(st, "POST", "/api/items/move", body=_body(**body))
        if body.get("on_conflict") == "skip":
            assert r.status == 200, body
        else:
            assert r.status == 400, body


def test_move_rejects_too_many_ids(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    r = _call(st, "POST", "/api/items/move",
              body=_body(ids=list(range(1, 202)), target_dir="x"))
    assert r.status == 400


def test_move_readonly_root_is_403(tmp_path, library) -> None:
    """只读模式下移动必须 403，而不是"移到一半发现不行"。"""
    st = _state(tmp_path, library, mode="readonly")
    ids = _seed(st, library)
    r = _call(st, "POST", "/api/items/move",
              body=_body(ids=[ids["a.jpg"]], target_dir="x"))
    assert r.status == 403
    assert _json(r)["error"]["code"] == "E1015"
    assert (library / "2024" / "2024-05" / "a.jpg").exists()


def test_move_unknown_id_goes_to_failed_not_500(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=[999999, ids["a.jpg"]], target_dir="ok")))
    assert len(b["failed"]) == 1
    assert b["failed"][0]["code"] == "E1004"
    assert len(b["moved"]) == 1, "一项失败不该中断整批"


def test_move_missing_row_goes_to_failed(tmp_path, library) -> None:
    """库里标了 missing 的行不能移动（文件已经不在那儿了）。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids["a.jpg"]
    (library / "2024" / "2024-05" / "a.jpg").unlink()
    st.db.execute("UPDATE media SET missing = 1 WHERE id = ?", (mid,))
    b = _json(_call(st, "POST", "/api/items/move",
                    body=_body(ids=[mid], target_dir="ok")))
    assert len(b["failed"]) == 1
    assert b["moved"] == []


# --------------------------------------------------------------------------
# 移动：跨卷降级（唯一允许的非原子操作）
# --------------------------------------------------------------------------

def test_cross_volume_move_copies_then_deletes(tmp_path, library,
                                               monkeypatch) -> None:
    """★ 跨卷时 `os.replace` 抛 WinError 17 → 必须降级为复制+校验+删源。

    直接调 `_move_one_sync` 并让 `os.replace` 假装跨卷失败，这样
    在没有第二个卷的 CI 机器上也能覆盖这条路径 —— 而它是**唯一**
    允许的非原子操作，也最容易写错（漏删源、或复制没完成就删源）。
    """
    real_replace = os.replace
    calls: list[tuple[str, str]] = []

    def fake_replace(src, dst):
        calls.append((str(src), str(dst)))
        # 第一次（src → dst）假装跨卷；后续（tmp → dst）正常
        if str(src).endswith(".jpg") and len(calls) == 1:
            raise OSError(18, "Invalid cross-device link")
        return real_replace(src, dst)

    monkeypatch.setattr(browse.os, "replace", fake_replace)

    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids["a.jpg"]
    src = library / "2024" / "2024-05" / "a.jpg"
    dst = library / "dest" / "a.jpg"
    dst.parent.mkdir(parents=True)
    before = src.read_bytes()

    browse._move_one_sync(src, dst, size=src.stat().st_size)

    assert dst.read_bytes() == before
    assert not src.exists(), "降级路径必须删掉源文件"
    assert not (dst.parent / "a.jpg.pbmove").exists(), "临时文件必须清理"


def test_cross_volume_failure_keeps_source_intact(tmp_path, library,
                                                  monkeypatch) -> None:
    """★ 跨卷复制中途失败时：源文件**一个字节都不能少**，半成品要删掉。

    这是"移动"最危险的时刻 —— 若实现是"先删源再复制"或者不清理
    半成品，用户会同时失去两边或得到一个截断的文件。
    """
    real_replace = os.replace
    state = {"first": True}

    def fake_replace(src, dst):
        if state["first"]:
            state["first"] = False
            raise OSError(18, "Invalid cross-device link")
        return real_replace(src, dst)

    def boom(src, dst, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(browse.os, "replace", fake_replace)
    monkeypatch.setattr(browse.shutil, "copy2", boom)

    st = _state(tmp_path, library)
    ids = _seed(st, library)
    src = library / "2024" / "2024-05" / "a.jpg"
    dst = library / "dest" / "a.jpg"
    dst.parent.mkdir(parents=True)
    before = src.read_bytes()

    with pytest.raises(OSError):
        browse._move_one_sync(src, dst, size=src.stat().st_size)

    assert src.exists(), "★ 失败时源文件必须还在"
    assert src.read_bytes() == before
    assert not dst.exists()
    assert not (dst.parent / "a.jpg.pbmove").exists()


def test_cross_volume_size_mismatch_does_not_delete_source(tmp_path, library,
                                                           monkeypatch) -> None:
    """★ 复制后大小不符 → 拒绝删源。

    这是防"磁盘写满导致文件被截断，而源文件已经被删掉"的最后一道闸。
    """
    real_replace = os.replace
    state = {"first": True}

    def fake_replace(src, dst):
        if state["first"]:
            state["first"] = False
            raise OSError(18, "Invalid cross-device link")
        return real_replace(src, dst)

    def short_copy(src, dst, **kw):
        Path(dst).write_bytes(b"truncated")

    monkeypatch.setattr(browse.os, "replace", fake_replace)
    monkeypatch.setattr(browse.shutil, "copy2", short_copy)

    st = _state(tmp_path, library)
    ids = _seed(st, library)
    src = library / "2024" / "2024-05" / "a.jpg"
    dst = library / "dest" / "a.jpg"
    dst.parent.mkdir(parents=True)

    with pytest.raises(AppError):
        browse._move_one_sync(src, dst, size=src.stat().st_size)

    assert src.exists()
    assert not dst.exists()
    assert not (dst.parent / "a.jpg.pbmove").exists()


def test_same_volume_move_is_atomic_rename(tmp_path, library, monkeypatch) -> None:
    """同卷时**必须**走 `os.replace`（原子），不能一律复制+删源。

    复制+删源在同卷上是纯粹的倒退：慢一倍，还多一个"中途断电就丢"
    的窗口。
    """
    seen: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(browse.os, "replace", spy)
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    src = library / "2024" / "2024-05" / "a.jpg"
    dst = library / "dest" / "a.jpg"
    dst.parent.mkdir(parents=True)
    browse._move_one_sync(src, dst, size=src.stat().st_size)
    assert seen == ["replace"]


def test_move_one_refuses_to_overwrite_existing_target(tmp_path, library) -> None:
    """★ 底层函数自己也必须拒绝覆盖（不能只靠调用方的 exists() 检查）。

    `exists()` 与 `replace()` 之间存在 TOCTOU 窗口；把不覆盖的保证
    放在最底层，即使上层判断出错也丢不了数据。
    """
    src = tmp_path / "src.jpg"
    dst = tmp_path / "dst.jpg"
    src.write_bytes(b"AAAA")
    dst.write_bytes(b"BBBB")
    with pytest.raises(FileExistsError):
        browse._move_one_sync(src, dst, size=4)
    assert src.read_bytes() == b"AAAA"
    assert dst.read_bytes() == b"BBBB"


# --------------------------------------------------------------------------
# 改名
# --------------------------------------------------------------------------

def test_rename_updates_file_and_db(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids["a.jpg"]
    before = (library / "2024" / "2024-05" / "a.jpg").read_bytes()

    b = _json(_call(st, "POST", "/api/items/rename",
                    body=_body(id=mid, new_name="假期合影.jpg")))

    assert b["from"] == "2024/2024-05/a.jpg"
    assert b["to"] == "2024/2024-05/假期合影.jpg"
    assert (library / "2024" / "2024-05" / "假期合影.jpg").read_bytes() == before
    assert not (library / "2024" / "2024-05" / "a.jpg").exists()
    assert _rel(st, mid) == "2024/2024-05/假期合影.jpg"
    assert st.db.scalar("SELECT file_name FROM media WHERE id=?", (mid,)) == "假期合影.jpg"


def test_rename_rejects_path_separator(tmp_path, library) -> None:
    """★ 名字里带分隔符 = 想借改名做移动，必须 400。

    这一条必须在沙箱之前拦 —— 沙箱对 `a/b.jpg` 是合法的（它就是
    为路径设计的），放过它等于绕开了移动接口的冲突语义与审计。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    for bad in ("sub/x.jpg", "..\\x.jpg", "a\\b.jpg"):
        r = _call(st, "POST", "/api/items/rename",
                  body=_body(id=ids["a.jpg"], new_name=bad))
        assert r.status == 400, bad
    assert (library / "2024" / "2024-05" / "a.jpg").exists()


def test_rename_rejects_non_image_extension(tmp_path, library) -> None:
    """★ 扩展名必须仍在白名单 —— 否则能把照片改成 .exe。

    双重危害：①用户在资源管理器里双击就运行；②它会立刻从时间线
    消失（索引器按扩展名过滤），看起来像"照片丢了"。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    for bad in ("evil.exe", "evil.bat", "evil.jpg-lx2", "noext"):
        r = _call(st, "POST", "/api/items/rename",
                  body=_body(id=ids["a.jpg"], new_name=bad))
        assert r.status == 400, bad
    assert (library / "2024" / "2024-05" / "a.jpg").exists()


def test_rename_accepts_uppercase_extension(tmp_path, library) -> None:
    """`.JPG` 是合法的（白名单比对要小写化）。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    b = _json(_call(st, "POST", "/api/items/rename",
                    body=_body(id=ids["a.jpg"], new_name="SHOT.JPG")))
    assert b["to"].endswith("SHOT.JPG")


def test_rename_rejects_windows_reserved_name(tmp_path, library) -> None:
    """Windows 保留名由沙箱统一拒绝（CON/PRN/AUX/NUL/COM1…）。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "POST", "/api/items/rename",
              body=_body(id=ids["a.jpg"], new_name="CON.jpg"))
    assert r.status == 400
    assert (library / "2024" / "2024-05" / "a.jpg").exists()


def test_rename_normalizes_trailing_space_and_rejects_trailing_dot(
    tmp_path, library
) -> None:
    """首尾空白被**规范化掉**而不是报错；结尾的点必须拒。

    两种处理不同，理由不同：
    - 结尾**空格**：手机输入法很容易带出来，报"路径不合法"对用户毫无
      帮助。先 strip 再校验，保证"校验的名字"就是"实际创建的名字"
      （沙箱之所以拒绝结尾空格，正是怕 Windows 静默去掉导致两者不一致；
      我们提前 strip 就不存在这个不一致）。
    - 结尾**点**：`x.jpg.` 会被解析成"扩展名为空"，本来就不该通过
      白名单；而且 Windows 同样会静默去掉它。必须拒。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)

    b = _json(_call(st, "POST", "/api/items/rename",
                    body=_body(id=ids["a.jpg"], new_name="  x.jpg  ")))
    assert b["to"] == "2024/2024-05/x.jpg"
    assert (library / "2024" / "2024-05" / "x.jpg").exists()

    for bad in ("y.jpg.", "y.jpg. "):
        r = _call(st, "POST", "/api/items/rename",
                  body=_body(id=ids["a.jpg"], new_name=bad))
        assert r.status == 400, repr(bad)


def test_rename_conflict_is_409_not_overwrite(tmp_path, library) -> None:
    """★ 同目录已有同名文件 → 409，绝不覆盖。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    other = library / "2024" / "2024-05" / "b.jpg"
    before = other.read_bytes()
    r = _call(st, "POST", "/api/items/rename",
              body=_body(id=ids["a.jpg"], new_name="b.jpg"))
    assert r.status == 409
    assert other.read_bytes() == before
    assert (library / "2024" / "2024-05" / "a.jpg").exists()


def test_rename_conflict_uses_target_exists_code(tmp_path, library) -> None:
    """★ 冲突要用 E3012（"目标位置已有同名文件"），不能用 E1012。

    `Conflict` 的默认 spec 是 E1012，它的 user_hint 是
    「上传已结束，请重新开始」—— 手机端会把这句话原样显示给用户，
    而用户刚刚只是在改名。这个断言看着琐碎，但它是唯一能挡住
    "用默认 spec" 的地方。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "POST", "/api/items/rename",
              body=_body(id=ids["a.jpg"], new_name="b.jpg"))
    assert r.status == 409
    body = json.loads(r.body if isinstance(r.body, bytes) else b"{}")
    assert body["error"]["code"] == "E3012"
    # user_hint 不能提到上传
    assert "上传" not in body["error"].get("user_hint", "")


def test_rename_same_name_is_idempotent_and_not_audited(tmp_path, library) -> None:
    """改成同一个名字 → 成功但不写审计。

    否则用户"点了一下没反应"会把审计日志刷满，真正重要的记录被淹没。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    b = _json(_call(st, "POST", "/api/items/rename",
                    body=_body(id=ids["a.jpg"], new_name="a.jpg")))
    assert b["from"] == b["to"]
    assert [ln for ln in _audit_lines(st) if "\trename\t" in ln] == []


def test_rename_writes_audit(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    _call(st, "POST", "/api/items/rename",
          body=_body(id=ids["a.jpg"], new_name="new.jpg"))
    lines = [ln for ln in _audit_lines(st) if "\trename\t" in ln]
    assert len(lines) == 1
    detail = json.loads(lines[0].split("\t")[4])
    assert detail["from"].endswith("a.jpg")
    assert detail["to"].endswith("new.jpg")


def test_rename_readonly_is_403(tmp_path, library) -> None:
    st = _state(tmp_path, library, mode="readonly")
    ids = _seed(st, library)
    r = _call(st, "POST", "/api/items/rename",
              body=_body(id=ids["a.jpg"], new_name="x.jpg"))
    assert r.status == 403
    assert (library / "2024" / "2024-05" / "a.jpg").exists()


def test_rename_unknown_id_is_404(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    r = _call(st, "POST", "/api/items/rename",
              body=_body(id=999999, new_name="x.jpg"))
    assert r.status == 404


def test_rename_rejects_bad_params(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    for body in (
        {"id": "abc", "new_name": "x.jpg"},
        {"id": ids["a.jpg"], "new_name": ""},
        {"id": ids["a.jpg"], "new_name": "   "},
        {"id": ids["a.jpg"]},
        {"id": ids["a.jpg"], "new_name": "x" * 300},
    ):
        r = _call(st, "POST", "/api/items/rename", body=_body(**body))
        assert r.status == 400, body


def test_rename_missing_file_is_404(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids["a.jpg"]
    (library / "2024" / "2024-05" / "a.jpg").unlink()
    st.db.execute("UPDATE media SET missing = 1 WHERE id = ?", (mid,))
    r = _call(st, "POST", "/api/items/rename",
              body=_body(id=mid, new_name="x.jpg"))
    assert r.status == 404


# --------------------------------------------------------------------------
# 改名：taken_at 重算
# --------------------------------------------------------------------------

def test_rename_recomputes_taken_at_when_it_came_from_filename(
    tmp_path, library
) -> None:
    """★ 来源是文件名时，改名后必须按 EXIF → 文件名 → mtime 重算。

    文件名 `20230101_120000.jpg` 提供 taken_at，改成无日期的名字后
    该来源失效 → 必须回退到 mtime。若实现忘了重算，这条记录会一直
    挂着一个"文件名里已经不存在的时刻"，用户看到排序莫名其妙。
    """
    st = _state(tmp_path, library)
    # 不带 EXIF 的图，文件名里带日期
    src = _img(library / "20230101_120000.jpg", color=(10, 20, 30))
    from app.indexer import Indexer
    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))
    mid = int(st.db.scalar("SELECT id FROM media WHERE file_name=?", ("20230101_120000.jpg",)))
    old_src = st.db.scalar("SELECT taken_src FROM media WHERE id=?", (mid,))
    assert old_src == "filename", "前置条件：来源必须是文件名"

    b = _json(_call(st, "POST", "/api/items/rename",
                    body=_body(id=mid, new_name="无日期.jpg")))

    assert b["taken_src"] != "filename", "文件名里的日期没了，来源必须改变"
    assert b["taken_at"] == pytest.approx(os.stat(library / "无日期.jpg").st_mtime, abs=1.0)
    assert st.db.scalar("SELECT taken_src FROM media WHERE id=?", (mid,)) == b["taken_src"]


def test_rename_keeps_exif_taken_at_when_filename_source_lost(
    tmp_path, library
) -> None:
    """★ 改名丢掉文件名日期时，若 EXIF 里有 DateTimeOriginal 必须用它。

    这是最容易写错的地方：简单实现会直接退回 mtime，于是一张本来
    有准确拍摄时间的照片掉到"文件修改时间"那个时刻，在时间线上
    跑到错误的位置。正确做法是走 `exif.resolve_taken_at` 走完整顺序。
    """
    st = _state(tmp_path, library)
    # EXIF 日期 + 文件名日期都存在 → 索引时 EXIF 优先，来源是 exif，
    # 改名后 taken_at 必须原样保留（不能被"文件名日期没了"影响）。
    src = _img(library / "20230101_120000.jpg", taken="2024:05:12 18:30:00")
    from app.indexer import Indexer
    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))
    mid = int(st.db.scalar("SELECT id FROM media WHERE file_name=?", ("20230101_120000.jpg",)))
    assert st.db.scalar("SELECT taken_src FROM media WHERE id=?", (mid,)) == "exif"
    before = st.db.scalar("SELECT taken_at FROM media WHERE id=?", (mid,))

    b = _json(_call(st, "POST", "/api/items/rename",
                    body=_body(id=mid, new_name="无日期.jpg")))
    assert b["taken_src"] == "exif"
    assert b["taken_at"] == pytest.approx(before, abs=0.001)


def test_rename_falls_back_to_exif_when_filename_date_was_the_source(
    tmp_path, library, monkeypatch
) -> None:
    """★ 来源是 filename 时，重算必须**先试 EXIF**再退 mtime。

    构造方式：先把一张无 EXIF 的图按文件名日期入库（来源 filename），
    改名后给文件补上 EXIF 日期，再断言重算结果用了 EXIF 而不是 mtime。
    直接退回 mtime 的实现会在这里失败 —— 而"有 EXIF 却用了 mtime"
    正是会让照片在时间线上跳位置的那类 bug。
    """
    st = _state(tmp_path, library)
    src = _img(library / "20230101_120000.jpg", color=(10, 20, 30))
    from app.indexer import Indexer
    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))
    mid = int(st.db.scalar(
        "SELECT id FROM media WHERE file_name=?", ("20230101_120000.jpg",)))
    assert st.db.scalar("SELECT taken_src FROM media WHERE id=?", (mid,)) == "filename"

    # 改名时让 extract_sync 返回一个 EXIF 日期，模拟"文件里本来就有
    # DateTimeOriginal，只是当初文件名也解析出了日期才没轮到它"。
    from app import exif as exif_mod

    fake = exif_mod.ExifInfo(taken_at=1715509800.0, has_exif=True)
    monkeypatch.setattr(exif_mod, "extract_sync", lambda p: fake)

    b = _json(_call(st, "POST", "/api/items/rename",
                    body=_body(id=mid, new_name="无日期.jpg")))

    assert b["taken_src"] == "exif", "有 EXIF 日期时必须用它，不能退回 mtime"
    assert b["taken_at"] == pytest.approx(1715509800.0, abs=0.001)


def test_rename_keeps_taken_at_when_source_is_exif(tmp_path, library) -> None:
    """来源是 EXIF 时改名不该改变 taken_at（EXIF 还在文件里）。"""
    st = _state(tmp_path, library)
    src = _img(library / "shot.jpg", taken="2024:05:12 18:30:00")
    from app.indexer import Indexer
    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))
    mid = int(st.db.scalar("SELECT id FROM media WHERE file_name=?", ("shot.jpg",)))
    before = st.db.scalar("SELECT taken_at FROM media WHERE id=?", (mid,))
    b = _json(_call(st, "POST", "/api/items/rename",
                    body=_body(id=mid, new_name="renamed.jpg")))
    assert b["taken_src"] == "exif"
    assert b["taken_at"] == pytest.approx(before, abs=0.001)


# --------------------------------------------------------------------------
# 跨接口一致性
# --------------------------------------------------------------------------

def test_moved_item_is_still_visible_and_consistent(tmp_path, library) -> None:
    """★ 移动后这条记录必须仍然"自洽"：库里 rel_path 指向的文件真的在。

    不自洽的表现是：用户看到缩略图（缓存还在），点开却报"文件不存在"，
    而下次扫描会把它标 missing —— 从用户视角就是"照片莫名其妙丢了"。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids["a.jpg"]
    _call(st, "POST", "/api/items/move",
          body=_body(ids=[mid], target_dir="相册A"))
    rel = _rel(st, mid)
    assert rel == "相册A/a.jpg"
    assert (library / rel).exists()
    b = _json(_call(st, "GET", f"/api/items/{mid}", params={"media_id": str(mid)}))
    assert b["rel_path"] == "相册A/a.jpg"
    assert b["name"] == "a.jpg"
    assert b["missing"] is False


def test_renamed_item_is_still_visible_and_consistent(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids["a.jpg"]
    _call(st, "POST", "/api/items/rename",
          body=_body(id=mid, new_name="改名了.jpg"))
    rel = _rel(st, mid)
    assert (library / rel).exists()
    b = _json(_call(st, "GET", f"/api/items/{mid}", params={"media_id": str(mid)}))
    assert b["name"] == "改名了.jpg"


def test_move_and_rename_do_not_leak_absolute_path(tmp_path, library) -> None:
    """错误响应里不能出现绝对路径（跨接口的脱敏要求）。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "POST", "/api/items/move",
              body=_body(ids=[ids["a.jpg"]], target_dir="../escape"))
    text = r.body if isinstance(r.body, bytes) else asyncio.run(_drain(r.body))
    assert str(library).encode() not in text
    assert b"C:\\\\" not in text and b"E:\\\\" not in text


# --------------------------------------------------------------------------
# auth.allow_move —— 第 4 个死配置的回归
# --------------------------------------------------------------------------

def test_move_disabled_by_config(tmp_path: Path) -> None:
    """★ `auth.allow_move = false` 必须真的挡住移动。

    这个键此前只在 `config.py` 里声明、代码路径零读取 —— 用户关掉它
    之后一切照旧，界面上没有任何区别，只能以为自己设错了。
    """
    library = tmp_path / "lib"
    (library / "2024").mkdir(parents=True)
    st = _state(tmp_path, library, allow_move=False)
    ids = _seed(st, library)
    mid = ids["a.jpg"]
    before = _rel(st, mid)

    r = _call(st, "POST", "/api/items/move",
              body=_body(ids=[mid], target_dir="", on_conflict="rename"))
    assert r.status == 403, "allow_move=false 时移动仍然成功"
    assert _json(r)["error"]["code"] == "E1015"
    assert _rel(st, mid) == before, "被拒绝的请求竟然改了库"


def test_rename_disabled_by_config(tmp_path: Path) -> None:
    """改名同样受 `auth.allow_move` 管 —— 两个动作都是"改变照片的位置/名字"。

    分成两个键会让"我明明关了"变成需要读文档才知道的细节，
    而用户的心理模型只有"允许改动我的照片库 / 不允许"。
    """
    library = tmp_path / "lib"
    (library / "2024").mkdir(parents=True)
    st = _state(tmp_path, library, allow_move=False)
    ids = _seed(st, library)
    mid = ids["a.jpg"]
    before = _rel(st, mid)

    r = _call(st, "POST", "/api/items/rename",
              body=_body(id=mid, new_name="zzz.jpg"))
    assert r.status == 403, "allow_move=false 时改名仍然成功"
    assert _json(r)["error"]["code"] == "E1015"
    assert _rel(st, mid) == before


def test_move_allowed_by_default(tmp_path: Path) -> None:
    """默认（配置里不写这个键）必须是允许 —— 与 `config.py` 的默认一致。

    ★ 这条用例防的是"加了开关之后默认值接反"：`allow_delete` 是安全默认
    False（删照片不可逆地改变用户数据），而移动是可撤销的日常操作，
    默认 True。两者接反的话所有用户都会突然不能移动照片。
    """
    library = tmp_path / "lib"
    (library / "2024").mkdir(parents=True)
    st = _state(tmp_path, library)
    # 把键删掉，模拟"用户配置文件里根本没这一项"（老库升级上来就是这样）
    st.cfg.raw["auth"].pop("allow_move", None)
    ids = _seed(st, library)
    mid = ids["a.jpg"]

    r = _call(st, "POST", "/api/items/move",
              body=_body(ids=[mid], target_dir="2024", on_conflict="rename"))
    assert r.status == 200, f"默认应当允许移动，却返回 {r.status}"
