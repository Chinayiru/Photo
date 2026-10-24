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

#: 一个最小但合法的 JPEG 头（SOI + APP0）。
#: complete 阶段现在会校验魔术字节（docs/06 §6.1、docs/10 规则 S6），
#: 所以凡是要走完 complete 的用例，负载必须"看起来真的是 JPEG" ——
#: 用 `b"hello world"` 这类任意字节会被拒，那正是校验生效的证明。
JPEG_HEAD = b"\xff\xd8\xff\xe0"


def _img(payload: bytes) -> bytes:
    """把任意负载包成一份合法的 JPEG 字节流。

    补足到 32 字节：`sniff_kind` 需要至少 12 字节才能判定
    （ISO-BMFF 的 brand 在偏移 8~12），比这短的一律拒绝 ——
    真实图片没有小于 12 字节的，但测试里 `_img(b"d")` 只有 6 字节。
    """
    blob = JPEG_HEAD + payload
    return blob + b"\x00" * max(0, 32 - len(blob))


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
    data = _img(b"photo-bytes")
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
    data = _img(b"abc")
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
    data = _img(b"real-bytes")
    r = _make(db, cfg, data)
    _send(db, cfg, r["upload_id"], data)
    with pytest.raises(AppError) as ei:
        uploads.complete_session(db, cfg, r["upload_id"], sha256="0" * 64)
    assert ei.value.code == "E1013"
    # 保留临时文件，客户端才能只重传坏掉的部分
    assert Path(uploads.get_session(db, r["upload_id"])["temp_path"]).exists()


def test_complete_sha256_match_ok(tmp_path, library, db):
    cfg = _cfg(tmp_path, library)
    data = _img(b"real-bytes")
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
        data = _img(f"photo-{i}".encode())
        r = _make(db, cfg, data, name=f"p{i}.jpg", declared_taken_at=when)
        _send(db, cfg, r["upload_id"], data)
        rels.append(uploads.complete_session(db, cfg, r["upload_id"])["rel_path"])
    assert len(set(rels)) == 3
    for i, rel in enumerate(rels):
        assert (library / rel).read_bytes() == _img(f"photo-{i}".encode())


# --------------------------------------------------------------------------
# 魔术字节（docs/06 §6.1、docs/10 规则 S6）
# --------------------------------------------------------------------------


#: `docs/07` TC-UPLOAD-SEC-008 ~ 015 的样本：(用例号, 文件名, 内容)
_MASQUERADES: list[tuple[str, str, bytes]] = [
    ("TC-UPLOAD-SEC-008", "fake_executable.jpg", b"MZ\x90\x00\x03" + b"\x00" * 64),
    ("TC-UPLOAD-SEC-009", "elf.jpg", b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64),
    ("TC-UPLOAD-SEC-010", "script.jpg", b"#!/bin/sh\nrm -rf /" + b"\x00" * 64),
    ("TC-UPLOAD-SEC-011", "svg_payload.png",
     b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>'),
    ("TC-UPLOAD-SEC-012", "html_payload.jpg", b"<html><script>alert(1)</script></html>"),
    ("TC-UPLOAD-SEC-013", "doc.jpg", b"%PDF-1.4\n" + b"\x00" * 64),
    ("TC-UPLOAD-SEC-014", "zip.jpg", b"PK\x03\x04" + b"\x00" * 64),
    ("TC-UPLOAD-SEC-015", "evil.php.jpg", b"<?php system($_GET[0]); ?>" + b"\x00" * 64),
]


@pytest.mark.parametrize("case,name,payload", _MASQUERADES, ids=[c for c, _, _ in _MASQUERADES])
def test_magic_mismatch(tmp_path, library, db, case, name, payload):
    """★ 伪装文件必须被拒、不落盘、临时文件清理。

    这是 `docs/06` 规则 6 点名的用例（`tests/test_uploads.py::test_magic_mismatch`）。
    此前 `uploads.py` 里**根本没有**魔术字节校验，这 8 个样本全部被接受并
    归档进了真实照片库（R-014 的缓解措施④是一条不存在的防线）。
    """
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, payload, name=name)
    _send(db, cfg, r["upload_id"], payload)
    temp = Path(uploads.get_session(db, r["upload_id"])["temp_path"])

    with pytest.raises(AppError) as ei:
        uploads.complete_session(db, cfg, r["upload_id"])
    assert ei.value.code == "E1017", case

    # ① 不落盘：归档目录下不能出现任何文件
    up = library / "手机上传"
    assert not list(up.rglob("*")) if up.exists() else True
    # ② 临时文件被清理（与 sha256 不符相反：伪装文件的字节重传一万遍还是 exe）
    assert not temp.exists()
    # ③ 会话被标记 aborted，不能停在 open 让人以为还能续传
    row = db.query_one("SELECT status FROM upload_sessions WHERE id = ?", (r["upload_id"],))
    assert row["status"] == "aborted"


def test_magic_rejects_disguised_video_as_jpg(tmp_path, library, db):
    """MP4（ftypmp42）改名 .jpg —— brand 不在图片白名单内。"""
    cfg = _cfg(tmp_path, library)
    payload = b"\x00\x00\x00\x20ftypmp42" + b"\x00" * 64
    r = _make(db, cfg, payload, name="video.jpg")
    _send(db, cfg, r["upload_id"], payload)
    with pytest.raises(AppError) as ei:
        uploads.complete_session(db, cfg, r["upload_id"])
    assert ei.value.code == "E1017"


def test_magic_rejects_riff_that_is_not_webp(tmp_path, library, db):
    """RIFF 只是容器：WAV 也是 RIFF。只查 `RIFF` 会放它进来。"""
    cfg = _cfg(tmp_path, library)
    payload = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 64
    r = _make(db, cfg, payload, name="sound.webp")
    _send(db, cfg, r["upload_id"], payload)
    with pytest.raises(AppError) as ei:
        uploads.complete_session(db, cfg, r["upload_id"])
    assert ei.value.code == "E1017"


def test_magic_rejects_short_garbage(tmp_path, library, db):
    """短到无法判定的一律拒绝 —— 不能因为"看不出是坏的"就放行。"""
    cfg = _cfg(tmp_path, library)
    payload = b"ab"
    r = _make(db, cfg, payload, name="tiny.jpg")
    _send(db, cfg, r["upload_id"], payload)
    with pytest.raises(AppError) as ei:
        uploads.complete_session(db, cfg, r["upload_id"])
    assert ei.value.code == "E1017"


def test_magic_accepts_valid_jpeg_and_png(tmp_path, library, db):
    """真正的图片必须照常通过 —— 校验不能误杀。"""
    cfg = _cfg(tmp_path, library)
    for name, head in (
        ("ok.jpg", b"\xff\xd8\xff\xe1" + b"\x00" * 64),
        ("ok.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
        ("ok.gif", b"GIF89a" + b"\x00" * 64),
        ("ok.webp", b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 64),
        ("ok.heic", b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64),
    ):
        r = _make(db, cfg, head, name=name)
        _send(db, cfg, r["upload_id"], head)
        out = uploads.complete_session(db, cfg, r["upload_id"])
        assert out["dedup"] is False, name
        assert (library / out["rel_path"]).read_bytes() == head


def test_magic_audit_records_reject(tmp_path, library, db):
    """拒绝要留痕：用户问"我传的那张怎么没进库"时，审计里得答得上来。"""
    cfg = _cfg(tmp_path, library)
    payload = b"MZ\x90\x00" + b"\x00" * 64
    r = _make(db, cfg, payload, name="bad.jpg")
    _send(db, cfg, r["upload_id"], payload)
    with pytest.raises(AppError):
        uploads.complete_session(db, cfg, r["upload_id"])
    text = cfg.audit_log_path.read_text(encoding="utf-8")
    assert "upload_reject" in text
    assert "magic_mismatch" in text


def test_sniff_kind_units():
    """`sniff_kind` 的边界：单测直接覆盖，不依赖上传链路。"""
    assert uploads.sniff_kind(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == "jpeg"
    assert uploads.sniff_kind(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4) == "png"
    assert uploads.sniff_kind(b"GIF87a" + b"\x00" * 6) == "gif"
    assert uploads.sniff_kind(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00") == "webp"
    assert uploads.sniff_kind(b"\x00\x00\x00\x18ftypheic") == "heic"
    assert uploads.sniff_kind(b"\x00\x00\x00\x18ftypavif") == "avif"
    # 无法识别的
    assert uploads.sniff_kind(b"MZ\x90\x00" + b"\x00" * 8) is None
    assert uploads.sniff_kind(b"ab") is None
    assert uploads.sniff_kind(b"") is None
    # RIFF 但不是 WEBP
    assert uploads.sniff_kind(b"RIFF\x00\x00\x00\x00WAVE") is None


def test_magic_check_not_bypassed_by_double_extension(tmp_path, library, db):
    """`evil.php.jpg`：扩展名是 .jpg，内容必须也是 JPEG 才算数。"""
    cfg = _cfg(tmp_path, library)
    payload = b"<?php system($_GET[0]); ?>" + b"\x00" * 64
    r = _make(db, cfg, payload, name="evil.php.jpg")
    _send(db, cfg, r["upload_id"], payload)
    with pytest.raises(AppError) as ei:
        uploads.complete_session(db, cfg, r["upload_id"])
    assert ei.value.code == "E1017"


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
    data = _img(b"same-photo")
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
    data = _img(b"d")
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
    data = _img(b"audited")
    r = _make(db, cfg, data)
    _send(db, cfg, r["upload_id"], data)
    uploads.complete_session(db, cfg, r["upload_id"])
    text = cfg.audit_log_path.read_text(encoding="utf-8")
    assert "upload" in text
    assert "\t" in text          # TAB 分隔，便于 awk


def test_chunk_size_switches_by_transport(tmp_path: Path) -> None:
    """★ AC-6.8：分片大小必须随通路切换。

    `chunk_mb_wan` 此前是死配置 —— 声明了、写进了验收标准，但代码里
    零读取。二期接上隧道后分片会静静地仍是 8 MB，而云隧道下 RTT 高，
    丢一片要重传 8 MB 而不是 2 MB。这条用例把"二期会忘"变成"改不动"。
    """
    from backend.app.uploads import _chunk_size

    def _cfg(transport: str) -> Config:
        data = tmp_path / f"d_{transport}"
        data.mkdir(exist_ok=True)
        lib = tmp_path / f"lib_{transport}"
        lib.mkdir(exist_ok=True)
        return Config({
            "app": {"mode": "readwrite"},
            "server": {"transport": transport},
            "roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}],
        }, path=data / "config.json", data_dir=data)

    assert _chunk_size(_cfg("local_http")) == 8 * 1024 * 1024
    # 二期通路（名字先定成 relay，见 config.py 的注释）
    assert _chunk_size(_cfg("relay")) == 2 * 1024 * 1024, \
        "非局域网通路必须用 WAN 档分片"


def test_chunk_size_floor_is_respected(tmp_path: Path) -> None:
    """用户把分片设成 0（或负数）时不能算出 0 —— 那会让上传死循环。

    分片大小 0 的实现会让 `offset += len(chunk)` 永不前进，
    表现是"上传卡在 0%"，而且没有任何错误。
    """
    from backend.app.uploads import _chunk_size

    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    cfg = Config({
        "app": {"mode": "readwrite"},
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}],
        "upload": {"chunk_mb_lan": 0},
    }, path=data / "config.json", data_dir=data)
    assert _chunk_size(cfg) == 256 * 1024


# --------------------------------------------------------------------------
# 会话额度：废弃会话必须能被回收（审计 F6）
#
# 这一节的每一条都对应一个"用户再也传不上任何东西"的真实死法。
# `MAX_OPEN_SESSIONS = 3`，而 `open_session_count()` 数的是所有
# `status='open'` 的行 —— 只要有三条**永远不会有进展**的会话，全站上传
# 就被 429 锁死，用户看到的是"同时上传的任务太多"，而一个都没在传。
# --------------------------------------------------------------------------


def _age_session(db: Database, uid: str, *, created_ago: float) -> None:
    """把会话的 created_at 往前挪，模拟"这是很久以前创建的"。"""
    import time as _t

    db.execute(
        "UPDATE upload_sessions SET created_at = ? WHERE id = ?",
        (_t.time() - created_ago, uid),
    )


def test_quota_counts_only_live_sessions(tmp_path, library, db):
    """占满额度后，新的 init 必须被 429 拒 —— 这是前提，别测漏了。"""
    cfg = _cfg(tmp_path, library)
    for i in range(uploads.MAX_OPEN_SESSIONS):
        _make(db, cfg, name=f"a{i}.jpg")
    assert uploads.open_session_count(db) == uploads.MAX_OPEN_SESSIONS
    with pytest.raises(AppError) as ei:
        _make(db, cfg, name="blocked.jpg")
    assert ei.value.code == "E4002"


def test_stale_init_session_is_reclaimed(tmp_path, library, db):
    """只 init 过、一个字节没收到、超过 STALE_INIT_SECONDS 的会话会被回收。

    这是最常见的死法：用户在相册里选了 3 张，app 起了 3 个会话就崩了
    （或用户直接切走）。这三个空会话会永久占满额度。
    """
    cfg = _cfg(tmp_path, library)
    uids = [_make(db, cfg, name=f"a{i}.jpg")["upload_id"] for i in range(3)]
    assert uploads.open_session_count(db) == 3

    # 刚建完不算废弃 —— 正常客户端 init 完立刻开始传
    assert uploads.reclaim_stale(db) == 0
    assert uploads.open_session_count(db) == 3

    for uid in uids:
        _age_session(db, uid, created_ago=uploads.STALE_INIT_SECONDS + 60)
    assert uploads.reclaim_stale(db) == 3
    assert uploads.open_session_count(db) == 0

    # 额度回来了：新上传必须能成功
    assert _make(db, cfg, name="after.jpg")["created"] is True


def test_session_with_bytes_is_not_reclaimed_too_early(tmp_path, library, db):
    """"已经收到字节"的会话不能按 10 分钟判死 —— 那是续传中的会话。"""
    cfg = _cfg(tmp_path, library)
    uid = _make(db, cfg, data=b"x" * 100)["upload_id"]
    _send(db, cfg, uid, b"x" * 10)
    _age_session(db, uid, created_ago=uploads.STALE_INIT_SECONDS + 60)

    # created_at 很旧，但临时文件刚写过 → 不算废弃
    assert uploads.reclaim_stale(db) == 0
    sess = uploads.get_session(db, uid)
    assert sess["status"] == "open"


def test_idle_session_is_reclaimed_after_idle_window(tmp_path, library, db):
    """传了一半、临时文件超过 IDLE_RECLAIM_SECONDS 没被碰过 → 回收，但**不删文件**。

    不删文件是刻意的：用户回来点重试走 `_reconcile_offset` 还能接着传。
    这里要验的正是"额度回来了"和"数据还在"这两件事同时成立。
    """
    import os as _os
    import time as _t

    cfg = _cfg(tmp_path, library)
    uid = _make(db, cfg, data=b"x" * 100)["upload_id"]
    _send(db, cfg, uid, b"x" * 10)
    temp = Path(uploads.get_session(db, uid)["temp_path"])

    old = _t.time() - uploads.IDLE_RECLAIM_SECONDS - 60
    _os.utime(temp, (old, old))

    assert uploads.reclaim_stale(db) == 1
    assert uploads.open_session_count(db) == 0
    # ★ 临时文件必须还在 —— 回收与否只影响名额，不能连累用户已传的字节
    assert temp.is_file()
    assert temp.stat().st_size == 10


def test_session_whose_temp_file_vanished_is_reclaimed(tmp_path, library, db):
    """临时文件被系统清理掉了：这个会话永远 complete 不了，必须让位。"""
    cfg = _cfg(tmp_path, library)
    uid = _make(db, cfg, data=b"x" * 100)["upload_id"]
    _send(db, cfg, uid, b"x" * 10)
    Path(uploads.get_session(db, uid)["temp_path"]).unlink()

    assert uploads.reclaim_stale(db) == 1
    assert uploads.open_session_count(db) == 0


def test_purge_runs_before_quota_check(tmp_path, library, db):
    """过期会话必须在**计数之前**被清掉，否则它自己会把清理入口堵死。

    早先的顺序是 `if count >= MAX: raise` 然后才 `purge_expired()` ——
    过期会话明明已经过了 expires_at，用户却一直收到 429，直到 24 小时后
    别的路径触发清理。这条用例钉住的是**顺序**，不是清理逻辑本身
    （清理逻辑在 `test_purge_expired_*` 里）。
    """
    import time as _t

    cfg = _cfg(tmp_path, library)
    for i in range(uploads.MAX_OPEN_SESSIONS):
        _make(db, cfg, name=f"a{i}.jpg")
    # 全部置成"已过期但还没被清"
    db.execute(
        "UPDATE upload_sessions SET expires_at = ? WHERE status = 'open'",
        (_t.time() - 10,),
    )
    assert uploads.open_session_count(db) == 0   # 计数本来就不含过期行
    # 但行还在，且 purge 之前它们仍占 status='open'
    assert db.scalar(
        "SELECT COUNT(*) FROM upload_sessions WHERE status = 'open'") == 3

    # 关键：这次 init 必须成功（内部先 purge 再计数）
    r = _make(db, cfg, name="fresh.jpg")
    assert r["created"] is True
    # 那三条被 purge 置成 expired
    assert db.scalar(
        "SELECT COUNT(*) FROM upload_sessions WHERE status = 'expired'") == 3


def test_reclaim_never_raises_on_broken_row(tmp_path, library, db):
    """回收过程绝不能因为某一行坏掉而影响别人。"""
    cfg = _cfg(tmp_path, library)
    good = _make(db, cfg, name="good.jpg")["upload_id"]
    bad = _make(db, cfg, name="bad.jpg")["upload_id"]
    db.execute("UPDATE upload_sessions SET temp_path = ? WHERE id = ?",
               ("", bad))
    _age_session(db, bad, created_ago=uploads.STALE_INIT_SECONDS + 60)
    _age_session(db, good, created_ago=uploads.STALE_INIT_SECONDS + 60)

    # 不抛，且两条都被回收（空路径那条走"文件不存在"分支）
    assert uploads.reclaim_stale(db) == 2


def test_abort_releases_quota_immediately(tmp_path, library, db):
    """`DELETE /api/uploads/{id}` 之后额度必须立刻回来（前端 F6 的另一半）。"""
    cfg = _cfg(tmp_path, library)
    uids = [_make(db, cfg, name=f"a{i}.jpg")["upload_id"] for i in range(3)]
    for uid in uids:
        assert uploads.abort_session(db, cfg, uid) is True
    assert uploads.open_session_count(db) == 0
    assert _make(db, cfg, name="after-abort.jpg")["created"] is True


# --------------------------------------------------------------------------
# 未知 root_id 必须是 4xx（审计 F7）
#
# 客户端传了个不存在的 root id 是**请求错误**，不是服务器故障。
# 早先 Config.root_by_id() 抛的是普通 ConfigError（继承 Exception），
# 而 errors.EXC_MAP 里没有它 → 归一成 Internal(None) → 500
# "服务器开小差"，用户完全不知道自己拼错了参数。
# --------------------------------------------------------------------------


def test_unknown_root_id_is_400_not_500(tmp_path, library, db):
    """`create_session` 抛出来的原始异常经 `to_app_error` 必须变成 400。

    ★ 为什么这里要显式调 `to_app_error` 而不是断言 `pytest.raises(AppError)`：
    `uploads.create_session()` 是**服务层**，它抛出的是领域异常
    （`UnknownRootError`），归一成 HTTP 错误码是 dispatch 层的职责
    （`backend/app/errors.py` 的 `to_app_error`）。直接断言 AppError
    会让人误以为服务层就该产出 HTTP 语义 —— 那是上一版的错误模型，
    也正是 F7 之前"ConfigError 落不进 EXC_MAP 就变 500"的温床。
    """
    from backend.app.config import UnknownRootError
    from backend.app.errors import to_app_error

    cfg = _cfg(tmp_path, library)
    with pytest.raises(UnknownRootError):
        uploads.create_session(db, cfg, name="a.jpg", size=10, root_id="nope")

    err = to_app_error(UnknownRootError("未知的根目录 id: 'nope'"))
    assert err.status == 400, f"未知 root 应为 400，实际 {err.status}"
    assert err.spec.code == "E1001"


def test_unknown_root_error_does_not_leak_path(tmp_path, library, db):
    """归一后响应体里不能出现磁盘路径 —— docs/04 §6 的硬约束。

    ★ 反向验证：把同一段消息里塞进库路径，归一后必须**看不见**它。
    只断言"正常输入不泄漏"是不够的 —— 那种断言在 `detail=str(exc)`
    的实现下也能过，因为正常输入的消息里本来就没有路径。
    """
    from backend.app.config import UnknownRootError
    from backend.app.errors import to_app_error

    leaky = UnknownRootError(f"未知的根目录 {library}")
    err = to_app_error(leaky)
    text = f"{err.detail or ''}{err.spec.message}{err!r}"
    assert str(library) not in text, f"响应里泄漏了库路径：{text}"
    assert str(tmp_path) not in text


def test_unknown_root_is_a_distinct_exception(tmp_path, library):
    """类型要分得开：请求错（UnknownRootError）vs 配置坏（ConfigError）。

    `Config.is_first_run()` 靠 `except ConfigError: return True` 判断
    "配置里没有可用根"。如果 UnknownRootError 不是 ConfigError 的
    子类而是并列类型，那两处语义就乱了 —— 但反过来，如果 root_by_id
    在"没有启用任何根"时也抛 UnknownRootError，首次运行判定就会失效。
    所以这里把两个方向都钉住。
    """
    from backend.app.config import ConfigError, UnknownRootError

    cfg = _cfg(tmp_path, library)
    assert issubclass(UnknownRootError, ConfigError)
    with pytest.raises(UnknownRootError):
        cfg.root_by_id("nope")
    # 正常 id 不受影响
    assert cfg.root_by_id("library").id == "library"


def test_root_by_id_none_uses_default(tmp_path, library):
    cfg = _cfg(tmp_path, library)
    assert cfg.root_by_id(None).id == "library"


# --------------------------------------------------------------------------
# 会话归属：client_id（ISSUE-034 ②）
# --------------------------------------------------------------------------
#
# 危害不是"多一条会话"，而是**静默数据损坏**：两台手机传同名同大小文件
# （连拍导出、微信同名图、两个人的 IMG_0001.jpg）时，第二台会接到第一台
# 的会话上，从它的 offset 继续往后写。两边都收到"成功"，归档出来的却是
# 两个文件前后半段拼接成的混合体 —— 而且 sha256 还能对上（它校验的是
# 拼接后的临时文件）。


def test_two_clients_do_not_share_a_session(tmp_path, library, db):
    """同签名（名+大小+sha256）但不同设备 → 必须是两条独立会话。"""
    cfg = _cfg(tmp_path, library)
    data = _img(b"same bytes")
    a = _make(db, cfg, data=data, name="IMG_0001.jpg",
              sha256=hashlib.sha256(data).hexdigest(), client_id="phoneA")
    b = _make(db, cfg, data=data, name="IMG_0001.jpg",
              sha256=hashlib.sha256(data).hexdigest(), client_id="phoneB")

    assert a["created"] is True
    assert b["created"] is True, "两台设备复用了同一个上传会话"
    assert a["upload_id"] != b["upload_id"]
    # 各自有独立的临时文件：不会互相覆盖
    sa = uploads.get_session(db, a["upload_id"], client_id="phoneA")
    sb = uploads.get_session(db, b["upload_id"], client_id="phoneB")
    assert sa["temp_path"] != sb["temp_path"]
    assert db.scalar("SELECT COUNT(*) FROM upload_sessions") == 2


def test_same_client_reuses_its_own_session(tmp_path, library, db):
    """同一台设备重发 init（弱网重试）仍然复用 —— 这是原功能，别改坏。"""
    cfg = _cfg(tmp_path, library)
    data = _img(b"retry")
    a = _make(db, cfg, data=data, name="a.jpg", client_id="phoneA")
    _send(db, cfg, a["upload_id"], data[:5])
    b = _make(db, cfg, data=data, name="a.jpg", client_id="phoneA")

    assert b["created"] is False
    assert b["upload_id"] == a["upload_id"]
    assert b["offset"] == 5, "复用会话时不能重置已收字节"


def test_other_client_session_is_404_not_403(tmp_path, library, db):
    """拿到别人的 upload_id → 404。

    403 等于确认"这个 uid 真的存在"，而 `GET /api/uploads/{uid}` 会回
    `declared_name` —— 确认存在之后剩下的就是猜大小、猜文件名。
    upload_id 是 32 位 hex，本来无法枚举，不能用 403 把它变成可探测的。
    """
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, data=_img(b"x"), name="a.jpg", client_id="phoneA")

    with pytest.raises(AppError) as ei:
        uploads.get_session(db, r["upload_id"], client_id="phoneB")
    assert ei.value.status == 404
    # 自己的仍然拿得到
    assert uploads.get_session(db, r["upload_id"], client_id="phoneA")["id"] == r["upload_id"]


def test_legacy_session_without_client_id_is_nobodys(tmp_path, library, db):
    """升级前的历史行（client_id IS NULL）谁的都不是 —— 宁可让人重传。

    让它匹配上空 client_id 才是危险的：那等于"身份未知的调用方可以写进
    任意历史会话"，而这正是要修的洞。
    """
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, data=_img(b"legacy"), name="old.jpg", client_id="phoneA")
    db.execute("UPDATE upload_sessions SET client_id = NULL WHERE id = ?",
               (r["upload_id"],))

    with pytest.raises(AppError) as ei:
        uploads.get_session(db, r["upload_id"], client_id="phoneA")
    assert ei.value.status == 404
    # 也不会被"未知归属"的 init 复用（否则两台手机都能抢到它）
    again = _make(db, cfg, data=_img(b"legacy"), name="old.jpg")
    assert again["created"] is True


def _ctx_for(st, uid: str, client_id: str, *, method: str = "GET",
             headers: dict | None = None, body: bytes | None = None):
    """构造一个只带 `ctx.state["client"]` 的 Ctx。

    正常请求里这个键由 `auth.require_client()` 在中间件里写入 ——
    测试直接塞进去，等价于"这台设备已经通过鉴权"。
    """
    from backend.app.core import Ctx

    async def _stream():
        if body:
            yield body

    ctx = Ctx(method=method, path=f"/api/uploads/{uid}",
              headers=headers or {}, params={"upload_id": uid}, app=st,
              body=_stream() if body is not None else None)
    ctx.state["client"] = {"id": client_id, "name": "test", "role": "viewer"}
    return ctx


def test_http_resume_of_foreign_session_is_404(tmp_path, library, db):
    """PATCH/GET 别人的 uid → 404（不是 403、更不是 500）。"""
    import asyncio

    from backend.app.handlers import uploads as uh
    from backend.app.state import AppState

    cfg = _cfg(tmp_path, library)
    st = AppState(cfg, db)
    r = _make(db, cfg, data=_img(b"secret"), name="a.jpg", client_id="phoneA")
    uid = r["upload_id"]

    with pytest.raises(AppError) as ei:
        asyncio.run(uh.get_upload(_ctx_for(st, uid, "phoneB")))
    assert ei.value.status == 404

    with pytest.raises(AppError) as ei:
        asyncio.run(uh.patch_upload(_ctx_for(
            st, uid, "phoneB", method="PATCH",
            headers={"content-range": "bytes 0-5/12"}, body=b"abcdef",
        )))
    assert ei.value.status == 404

    # 自己的请求照常
    reply = asyncio.run(uh.get_upload(_ctx_for(st, uid, "phoneA")))
    assert reply.status == 200


# --------------------------------------------------------------------------
# 文件名清洗（2026-10-23 审计）
# --------------------------------------------------------------------------
#
# `declared_name` 会被写进 `audit_log` 表、`data/audit.log`、以及
# `log.info("新建上传会话 %s")` —— 一条带 \r\n 的文件名就能伪造出完整的
# 假日志行，把"谁传了什么"这条追查线索变成攻击者可写的文本。


def _stored_name(db, uid: str) -> str:
    return db.query_one("SELECT declared_name FROM upload_sessions WHERE id = ?",
                        (uid,))["declared_name"]


def test_declared_name_crlf_is_cleaned(tmp_path, library, db, caplog):
    """CRLF 不能进库、也不能进日志。"""
    cfg = _cfg(tmp_path, library)
    # 伪造行的位置刻意放在中间：这样清洗后仍是合法文件名，
    # 能证明"只是去掉了危险字符"而不是"整条被拒"。
    tainted = "evil\r\n2026-10-23 INFO 新建上传会话 admin：删库成功.jpg"
    with caplog.at_level("INFO"):
        r = _make(db, cfg, data=_img(b"crlf"), name=tainted)

    stored = _stored_name(db, r["upload_id"])
    assert "\r" not in stored and "\n" not in stored
    # 日志同样必须是**单行**：一条带换行的文件名会伪造出完整的假日志行
    logged = [rec.getMessage() for rec in caplog.records]
    assert logged, "没有记录新建会话的日志"
    for line in logged:
        assert "\r" not in line and "\n" not in line, f"日志被换行注入了：{line!r}"
    assert stored.startswith("evil") and stored.endswith(".jpg")


def test_declared_name_control_chars_are_cleaned(tmp_path, library, db):
    """NUL / DEL / 其他 C0 控制符一律去掉。"""
    cfg = _cfg(tmp_path, library)
    weird = "ph\x00oto\x1f.jpg\x7f"
    r = _make(db, cfg, data=_img(b"ctl"), name=weird)
    stored = _stored_name(db, r["upload_id"])
    assert stored == "photo.jpg"


@pytest.mark.parametrize("bidi", ["\u202a", "\u202e", "\u2066", "\u2069"])
def test_declared_name_bidi_is_cleaned(tmp_path, library, db, bidi):
    """BIDI 控制符能让扩展名在界面上"看起来"是另一个。

    U+202E（RLO）之后的一段会被反向渲染：`photo\u202eexe.jpg` 在界面上
    显示的结尾是 `jpg`，而真实文件名结尾是 `jpg`、中间夹着 `exe` ——
    用户以为自己拿到一张图，实际看到的名字与他以为的不是同一个字符串。
    文件名后面还要进时间线、上传列表、电脑端托盘通知，每一处都依赖
    "看到的名字可信"，所以控制符必须在入库前就消失。
    （扩展名本身还有 `check_extension` 白名单兜着，BIDI 骗不了落盘，
    但骗得了**人**。）
    """
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, data=_img(b"bidi"), name=f"IMG{bidi}_0001.jpg")
    stored = _stored_name(db, r["upload_id"])
    assert bidi not in stored
    assert stored == "IMG_0001.jpg", "显示出来的名字必须与实际名字一致"


def test_declared_name_trailing_dots_and_spaces_are_cleaned(tmp_path, library, db):
    """Win32 建文件会静默吞掉结尾的点/空格，声明名与真实名就会不一致。"""
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, data=_img(b"dots"), name="  photo.jpg .  ")
    assert _stored_name(db, r["upload_id"]) == "photo.jpg"


def test_leading_dot_file_name_is_kept(tmp_path, library, db):
    """只去尾部的点：`.hidden.jpg` 是合法且常见的名字，不能一起砍掉。"""
    cfg = _cfg(tmp_path, library)
    r = _make(db, cfg, data=_img(b"hidden"), name=".hidden.jpg")
    assert _stored_name(db, r["upload_id"]) == ".hidden.jpg"


@pytest.mark.parametrize("name", [
    "CON", "con.txt", "CON.tar.jpg", "NUL.jpg", "aux.JPG",
    "COM1.jpg", "com9.png", "LPT1", "lpt9.jpg",
])
def test_windows_reserved_names_are_rejected(tmp_path, library, db, name):
    """Windows 设备保留名必须拒绝（E1001/400），而不是"清洗成别的名字"。

    猜用户想叫什么名字不如让他重命名：`CON.jpg` 清洗成 `_CON.jpg` 之后，
    用户看到的是一个他没起过的名字，问题被藏起来了。
    """
    cfg = _cfg(tmp_path, library)
    with pytest.raises(AppError) as ei:
        _make(db, cfg, data=_img(b"reserved"), name=name)
    assert ei.value.status == 400
    assert ei.value.code == "E1001"
    assert db.scalar("SELECT COUNT(*) FROM upload_sessions") == 0


def test_reserved_lookalike_names_are_allowed(tmp_path, library, db):
    """`console.jpg` / `NULLA.jpg` 不能被误伤（只比整套名字，不判前缀）。"""
    cfg = _cfg(tmp_path, library)
    for name in ("console.jpg", "NULLA.jpg", "auxiliary.jpg", "lpt10.jpg"):
        r = _make(db, cfg, data=_img(b"ok"), name=name)
        assert _stored_name(db, r["upload_id"]) == name
        # MAX_OPEN_SESSIONS 只有 3：逐个结束，避免撞上并发上限
        uploads.abort_session(db, cfg, r["upload_id"])


def test_name_that_cleans_to_nothing_is_rejected(tmp_path, library, db):
    """整串都是控制字符/空白 → 400，而不是建一条空名字的会话。"""
    cfg = _cfg(tmp_path, library)
    for name in ("\x00\x01\x1f", "\u202e\u202a", "   ", " .. "):
        with pytest.raises(AppError) as ei:
            _make(db, cfg, data=_img(b"empty"), name=name)
        assert ei.value.status == 400, name
    assert db.scalar("SELECT COUNT(*) FROM upload_sessions") == 0
