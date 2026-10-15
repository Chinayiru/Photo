"""分片上传测试。

覆盖重点（按风险排序）：

1. **绝不覆盖已有照片** —— R-004，本项目唯一可能造成永久数据丢失的缺陷。
2. **秒传不会丢照片** —— 索引里那条记录指向的文件已被手工删除时，
   秒传必须失效，否则"旧文件没了 + 新字节被丢弃" = 照片永久消失。
3. **并发 PATCH 不损坏文件** —— 手机端超时重试与正常请求撞车是最常见的。
4. **乱序分片必须报错** —— 静默接受会让文件中间留下空洞，
   表现为灰条或打不开，而 sha256 永远对不上。
5. **崩溃后 offset 以磁盘为准** —— DB 偏大会跳过一段。
"""
from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path

import pytest

from backend.app import uploads
from backend.app.config import Config
from backend.app.db import Database
from backend.app.errors import AppError


LIB = "lib0000000000001"


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    d = tmp_path / "lib"
    d.mkdir()
    return d


def _cfg(tmp_path: Path, library: Path, *, mode: str = "readwrite", **upload) -> Config:
    up = {
        "subdir": "手机上传",
        "chunk_mb_lan": 1,          # 测试用 1 MB，避免造几 MB 数据
        "max_file_mb": 4,           # 4 MB 上限，方便测超限
        "session_ttl_hours": 24,
        "dedup": True,
    }
    up.update(upload)
    return Config(
        {
            "roots": [
                {
                    "id": "library",
                    "path": str(library),
                    "label": "测试库",
                    "mode": mode,
                    "enabled": True,
                    "recursive": True,
                }
            ],
            "upload": up,
        },
        path=tmp_path / "config.json",
        data_dir=tmp_path / "data",
    )


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    d.init()
    yield d
    d.close()


def _make(db: Database, cfg: Config, data: bytes = b"hello world", name: str = "a.jpg", **kw):
    return uploads.create_session(db, cfg, name=name, size=len(data), **kw)


def _send(
    db: Database,
    cfg: Config,
    uid: str,
    data: bytes,
    *,
    offset: int = 0,
    total: int | None = None,
    declared_total: int | None = None,
):
    """发一个分片。

    `total` 是 Content-Range 里声明的**文件总大小**（默认等于会话大小）；
    `declared_total` 单独用来构造"声明的总大小与会话不符"的场景。
    """
    end = offset + len(data) - 1
    if declared_total is None:
        sess = uploads.get_session(db, uid)
        declared_total = int(sess["total_size"]) if total is None else total
    return uploads.append_chunk(
        db, cfg, uid, start=offset, end=end, total=declared_total, data=data
    )


# --------------------------------------------------------------------------
# create_session
# --------------------------------------------------------------------------


def test_create_session_writes_temp_inside_library(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg)
    assert r["created"] is True
    assert r["exists"] is False
    assert r["offset"] == 0
    # 临时文件必须在库根下（同卷才能 os.replace 原子落盘）
    sess = uploads.get_session(db, r["upload_id"])
    assert Path(sess["temp_path"]).parent.name == ".photobridge_tmp"
    assert Path(sess["temp_path"]).exists()


def test_extension_not_allowed_is_rejected(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    for bad in ("a.mp4", "a.jpg-lx2", "a.txt", "a.exe"):
        with pytest.raises(AppError) as ei:
            _make(db, cfg, name=bad)
        assert ei.value.code == "E1017", bad


def test_size_over_limit_rejected(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    with pytest.raises(AppError) as ei:
        uploads.create_session(db, cfg, name="a.jpg", size=5 * 1024 * 1024)
    assert ei.value.code == "E1006"


def test_readonly_root_rejects_upload(tmp_path, library, db):
    cfg = _cfg(tmp_path, library, mode="readonly")
    with pytest.raises(AppError) as ei:
        _make(db, cfg)
    assert ei.value.code == "E1015"


def test_duplicate_init_reuses_session(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    a = _make(db, cfg, name="x.jpg")
    b = _make(db, cfg, name="x.jpg")
    assert b["created"] is False
    assert b["exists"] is False
    assert b["upload_id"] == a["upload_id"]


def test_open_session_limit(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    for i in range(uploads.MAX_OPEN_SESSIONS):
        _make(db, cfg, name=f"{i}.jpg")
    with pytest.raises(AppError) as ei:
        _make(db, cfg, name="overflow.jpg")
    assert ei.value.code == "E4002"


# --------------------------------------------------------------------------
# append_chunk
# --------------------------------------------------------------------------


def test_sequential_chunks_accumulate(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    data = b"0123456789"
    r = _make(db, cfg, data)
    uid = r["upload_id"]
    out = _send(db, cfg, uid, data[:4])
    assert out["offset"] == 4 and out["complete"] is False
    out = _send(db, cfg, uid, data[4:], offset=4)
    assert out["offset"] == 10 and out["complete"] is True
    sess = uploads.get_session(db, uid)
    assert Path(sess["temp_path"]).read_bytes() == data


def test_out_of_order_chunk_returns_416_with_offset(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, b"abcdefgh")
    uid = r["upload_id"]
    _send(db, cfg, uid, b"abcd")
    with pytest.raises(AppError) as ei:
        _send(db, cfg, uid, b"gh", offset=6)
    assert ei.value.code == "E1011"
    assert ei.value.status == 416
    # 必须告诉客户端"现在该从哪继续"，否则它只能从头重传
    assert ei.value.headers["X-Upload-Offset"] == "4"
    assert "bytes=0-3" in ei.value.headers["Range"]


def test_duplicate_chunk_is_idempotent(tmp_path, library, db):
    """客户端没收到上次响应就重发整片 —— 必须成功且不重复写。"""
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, b"0123456789")
    uid = r["upload_id"]
    _send(db, cfg, uid, b"0123")
    again = _send(db, cfg, uid, b"0123")
    assert again["duplicate"] is True
    assert again["offset"] == 4
    sess = uploads.get_session(db, uid)
    assert Path(sess["temp_path"]).read_bytes() == b"0123"   # 没有被写成 8 字节


def test_chunk_length_mismatch_rejected(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, b"0123456789")
    uid = r["upload_id"]
    with pytest.raises(AppError):
        # 声明 0-5（6 字节）却只给 3 字节
        uploads.append_chunk(db, cfg, uid, start=0, end=5, total=10, data=b"abc")


def test_total_mismatch_rejected(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, b"0123456789")
    with pytest.raises(AppError):
        _send(db, cfg, r["upload_id"], b"0123", declared_total=999)


def test_reconcile_offset_prefers_disk(tmp_path, library, db):
    """崩溃可能发生在写完文件与更新 DB 之间：DB 说 4，磁盘上其实有 8。"""
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, b"0123456789")
    uid = r["upload_id"]
    _send(db, cfg, uid, b"01234567")
    sess = uploads.get_session(db, uid)
    db.execute("UPDATE upload_sessions SET received = 4 WHERE id = ?", (uid,))
    fixed = uploads._reconcile_offset(db, uploads.get_session(db, uid))
    assert fixed == 8
    # 并且写回了 DB
    assert int(uploads.get_session(db, uid)["received"]) == 8
    assert Path(sess["temp_path"]).read_bytes() == b"01234567"


def test_concurrent_chunks_do_not_corrupt(tmp_path, library, db):
    """两个线程同时写不同分片：必须串行化，最终内容完整。

    不加锁时两个线程都读到 received=0，各自 seek(0) 写入，
    后写的覆盖先写的，文件只剩后半段。
    """
    cfg = _cfg(tmp_path, library)
    chunk = b"a" * 100
    data = chunk + b"b" * 100 + b"c" * 100
    r = _make(db, cfg, data)
    uid = r["upload_id"]

    barrier = threading.Barrier(4)      # 3 个线程 + 主线程，凑齐才一起冲
    errors: list[Exception] = []

    def send(off: int, payload: bytes) -> None:
        barrier.wait()
        try:
            _send(db, cfg, uid, payload, offset=off)
        except Exception as exc:            # noqa: BLE001
            errors.append(exc)

    ts = [
        threading.Thread(target=send, args=(0, chunk)),
        threading.Thread(target=send, args=(100, b"b" * 100)),
        threading.Thread(target=send, args=(200, b"c" * 100)),
    ]
    for t in ts:
        t.start()
    barrier.wait()          # 主线程也参与，确保三个线程真的同时冲
    for t in ts:
        t.join()

    # 后两片可能因顺序不对被拒（这是正确行为），但文件绝不能是拼错的
    sess = uploads.get_session(db, uid)
    got = Path(sess["temp_path"]).read_bytes()
    assert got == data[: len(got)]        # 永远是正确的前缀，不是覆盖出来的乱序
    assert len(got) >= 100


def test_oversized_chunk_rejected(tmp_path, library, db):
    cfg = _cfg(tmp_path, library, chunk_mb_lan=1)
    big = b"x" * (1024 * 1024 + 4096)
    r = _make(db, cfg, big)
    with pytest.raises(AppError) as ei:
        uploads.append_chunk(
            db, cfg, r["upload_id"], start=0, end=len(big) - 1, total=len(big), data=big
        )
    assert ei.value.code == "E1007"


# --------------------------------------------------------------------------
# complete_session
# --------------------------------------------------------------------------


def test_complete_places_file_and_returns_rel(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    data = b"photo-bytes"
    r = _make(db, cfg, data, name="IMG_1234.jpg", declared_taken_at=1715520600.0)
    _send(db, cfg, r["upload_id"], data)
    out = uploads.complete_session(db, cfg, r["upload_id"])
    assert out["dedup"] is False
    rel = out["rel_path"]
    assert rel.startswith("手机上传/2024/2024-05/")
    assert rel.endswith(".jpg")
    assert (library / rel).read_bytes() == data
    # 临时文件已消失
    assert not Path(uploads.get_session(db, r["upload_id"])["temp_path"]).exists()
    assert uploads.get_session(db, r["upload_id"])["status"] == "completed"


def test_complete_is_idempotent(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    data = b"abc"
    r = _make(db, cfg, data)
    _send(db, cfg, r["upload_id"], data)
    first = uploads.complete_session(db, cfg, r["upload_id"])
    second = uploads.complete_session(db, cfg, r["upload_id"])
    assert second["already_completed"] is True
    assert second["rel_path"] == first["rel_path"]
    # 只落了一份
    assert len(list((library / "手机上传").rglob("*.jpg"))) == 1


def test_complete_incomplete_returns_offset(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    data = b"0123456789"
    r = _make(db, cfg, data)
    _send(db, cfg, r["upload_id"], b"0123")
    with pytest.raises(AppError) as ei:
        uploads.complete_session(db, cfg, r["upload_id"])
    assert ei.value.code == "E1014"
    assert ei.value.headers["X-Upload-Offset"] == "4"


def test_complete_sha256_mismatch_keeps_temp(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    data = b"real-bytes"
    r = _make(db, cfg, data)
    _send(db, cfg, r["upload_id"], data)
    with pytest.raises(AppError) as ei:
        uploads.complete_session(db, cfg, r["upload_id"], sha256="0" * 64)
    assert ei.value.code == "E1013"
    # 保留临时文件，客户端才能只重传坏掉的部分
    assert Path(uploads.get_session(db, r["upload_id"])["temp_path"]).exists()


def test_complete_sha256_match_ok(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    data = b"real-bytes"
    r = _make(db, cfg, data)
    _send(db, cfg, r["upload_id"], data)
    out = uploads.complete_session(
        db, cfg, r["upload_id"], sha256=hashlib.sha256(data).hexdigest()
    )
    assert out["sha256"] == hashlib.sha256(data).hexdigest()


def test_never_overwrites_existing_photo(tmp_path, library, db):
    """R-004：两张不同照片落在同一时间戳，谁都不能覆盖谁。"""
    cfg = _cfg(tmp_path, library)
    when = 1715520600.0
    rels = []
    for i in range(3):
        data = f"photo-{i}".encode()
        r = _make(db, cfg, data, name=f"p{i}.jpg", declared_taken_at=when)
        _send(db, cfg, r["upload_id"], data)
        rels.append(uploads.complete_session(db, cfg, r["upload_id"])["rel_path"])
    assert len(set(rels)) == 3
    for i, rel in enumerate(rels):
        assert (library / rel).read_bytes() == f"photo-{i}".encode()


# --------------------------------------------------------------------------
# 去重 / 秒传
# --------------------------------------------------------------------------


def _seed_media(db: Database, library: Path, rel: str, data: bytes) -> int:
    p = library / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    st = p.stat()
    return db.insert_returning_id(
        "INSERT INTO media (root_id, rel_path, file_name, ext, kind, size, mtime,"
        " sha256, taken_at, taken_src, width, height, indexed_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "library", rel, p.name, "jpg", "image", st.st_size, st.st_mtime,
            hashlib.sha256(data).hexdigest(), 1715520600.0, "exif", 100, 100, 0.0,
        ),
    )


def test_instant_upload_hit(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    data = b"already-there"
    digest = hashlib.sha256(data).hexdigest()
    mid = _seed_media(db, library, "2024/2024-05/20240512_213000.jpg", data)
    r = uploads.create_session(db, cfg, name="new.jpg", size=len(data), sha256=digest)
    assert r["exists"] is True
    assert r["media_id"] == mid
    assert r["offset"] == len(data)
    assert r["upload_id"] is None


def test_instant_upload_skipped_when_file_gone(tmp_path, library, db):
    """★ 索引里有记录但磁盘上文件已被手工删除 —— 秒传必须失效。

    否则：旧文件没了 + 新字节被丢弃 = 照片永久消失（R-004）。
    """
    cfg = _cfg(tmp_path, library)
    data = b"ghost-bytes"
    digest = hashlib.sha256(data).hexdigest()
    _seed_media(db, library, "2024/2024-05/20240512_213000.jpg", data)
    (library / "2024/2024-05/20240512_213000.jpg").unlink()

    r = uploads.create_session(db, cfg, name="new.jpg", size=len(data), sha256=digest)
    assert r["exists"] is False
    assert r["upload_id"] is not None


def test_instant_upload_skipped_when_size_differs(tmp_path, library, db):
    """sha256 命中但库里那份大小不同 —— 同名文件被换成别的内容，不能复用。"""
    cfg = _cfg(tmp_path, library)
    data = b"real"
    digest = hashlib.sha256(data).hexdigest()
    _seed_media(db, library, "2024/2024-05/a.jpg", data)
    # 磁盘上换成更长的内容（索引未更新）
    (library / "2024/2024-05/a.jpg").write_bytes(b"real-and-more")
    r = uploads.create_session(db, cfg, name="new.jpg", size=len(data), sha256=digest)
    assert r["exists"] is False


def test_complete_dedup_without_client_sha256(tmp_path, library, db):
    """客户端没带 sha256 时，complete 阶段仍要去重（并发上传靠这道兜底）。"""
    cfg = _cfg(tmp_path, library)
    data = b"same-photo"
    _seed_media(db, library, "2024/2024-05/x.jpg", data)
    r = _make(db, cfg, data, name="dup.jpg")
    _send(db, cfg, r["upload_id"], data)
    out = uploads.complete_session(db, cfg, r["upload_id"])
    assert out["dedup"] is True
    assert out["media_id"] is not None
    # 没有落第二份
    assert len(list((library / "手机上传").rglob("*.jpg"))) == 0


def test_dedup_disabled_config_still_works(tmp_path, library, db):
    cfg = _cfg(tmp_path, library, dedup=False)
    data = b"same"
    digest = hashlib.sha256(data).hexdigest()
    _seed_media(db, library, "2024/2024-05/y.jpg", data)
    r = uploads.create_session(db, cfg, name="n.jpg", size=len(data), sha256=digest)
    assert r["exists"] is False


# --------------------------------------------------------------------------
# abort / purge
# --------------------------------------------------------------------------


def test_abort_removes_temp_and_keeps_row(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, b"data")
    sess = uploads.get_session(db, r["upload_id"])
    uploads.abort_session(db, cfg, r["upload_id"])
    assert not Path(sess["temp_path"]).exists()
    row = db.query_one("SELECT status FROM upload_sessions WHERE id = ?", (r["upload_id"],))
    assert row["status"] == "aborted"       # 行保留，用户能查"为什么少了一张"


def test_abort_completed_session_rejected(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    data = b"d"
    r = _make(db, cfg, data)
    _send(db, cfg, r["upload_id"], data)
    uploads.complete_session(db, cfg, r["upload_id"])
    with pytest.raises(AppError):
        uploads.abort_session(db, cfg, r["upload_id"])


def test_expired_session_rejected(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, b"data")
    db.execute("UPDATE upload_sessions SET expires_at = 1.0 WHERE id = ?", (r["upload_id"],))
    with pytest.raises(AppError) as ei:
        uploads.get_session(db, r["upload_id"])
    assert ei.value.code == "E1012"


def test_purge_expired_removes_temp_files(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, b"data")
    sess = uploads.get_session(db, r["upload_id"])
    temp = Path(sess["temp_path"])
    db.execute("UPDATE upload_sessions SET expires_at = 1.0 WHERE id = ?", (r["upload_id"],))
    n = uploads.purge_expired(db, cfg=cfg)
    assert n == 1
    assert not temp.exists()


def test_unknown_session_404(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    with pytest.raises(AppError) as ei:
        uploads.get_session(db, "nope")
    assert ei.value.status == 404


# --------------------------------------------------------------------------
# 安全
# --------------------------------------------------------------------------


def test_temp_path_is_not_injectable(tmp_path, library, db):
    """upload_id 会拼进文件路径 —— 必须只允许白名单字符。"""
    from backend.app import sandbox

    cfg = _cfg(tmp_path, library)
    root = cfg.root_by_id("library")
    for bad in ("../../evil", "..", "a/b", "a\\b", "", "x" * 40):
        with pytest.raises(AppError):
            sandbox.tmp_path(root, bad)


def test_audit_log_written_on_complete(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    data = b"audited"
    r = _make(db, cfg, data)
    _send(db, cfg, r["upload_id"], data)
    uploads.complete_session(db, cfg, r["upload_id"])
    text = cfg.audit_log_path.read_text(encoding="utf-8")
    assert "upload" in text
    assert "\t" in text          # TAB 分隔，便于 awk
