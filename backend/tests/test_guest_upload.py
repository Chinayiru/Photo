"""游客上传（ISSUE-063 / S4）。

## 这批用例在守什么

游客能"添加"，但添加这件事有四个必须成立的前提，缺一个就会变成事故：

1. **字节只进游客根**。主人自己的目录结构（`2024/2024-05`）是主人的资产，
   朋友发来的图混进去就是数据污染 —— 而且主人扫描时报"怎么多了张不认识的照片"
   根本查不出来源。
2. **不看客户端说的 root/target_dir**。游客端的请求体是不可信的：说
   `root_id=<主人根>` 也必须落到游客根。
3. **关掉秒传**。去重命中会把 `media_id` 与 `rel_path` 回给调用方，
   等于告诉游客"这个哈希在你的库里已经有一张，路径是 xx"。
4. **`uploader_client_id` 必须写**。G4「游客只看自己上传的」整条链路
   都靠这一列筛选；不写就是所有游客互相看见对方发的图。

## 为什么用真文件

归档、索引、魔术字节校验、`guest_upload_log` 的外键行为都是与真实文件系统
/ SQLite 打交道的逻辑。mock 掉之后测的只是"我的 mock 被调用过"。
"""
from __future__ import annotations

import hashlib
import secrets
import time
from pathlib import Path

import pytest

from backend.app import guest, uploads
from backend.app.config import Config
from backend.app.db import Database
from backend.app.errors import AppError
from backend.app.indexer import Indexer

OWNER_ROOT = "lib0000000000001"
GUEST_ROOT = "gst0000000000001"
CLIENT = "c" * 32

#: 最小合法 JPEG（SOI + APP0），补齐到 32 字节让 `sniff_kind` 能判。
JPEG = b"\xff\xd8\xff\xe0" + b"payload" + b"\x00" * 24


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    owner = tmp_path / "lib"
    owner.mkdir()
    gdir = tmp_path / "guests"
    gdir.mkdir()
    return Config(
        {
            "roots": [
                {"id": OWNER_ROOT, "path": str(owner), "label": "主人的库",
                 "mode": "readwrite", "recursive": True},
                {"id": GUEST_ROOT, "path": str(gdir), "label": "游客区",
                 "mode": "upload", "visibility": "guest", "recursive": True},
            ],
            "upload": {
                "subdir": "手机上传",
                "chunk_mb_lan": 1,
                "max_file_mb": 4,
                "session_ttl_hours": 24,
                "dedup": True,
            },
        },
        path=tmp_path / "config.json",
        data_dir=tmp_path / "data",
    )


@pytest.fixture()
def db(cfg: Config) -> Database:
    d = Database(cfg.db_path)
    d.init()
    return d


def _push(db: Database, cfg: Config, payload: bytes, *, name: str = "a.jpg",
          guest_upload: bool = True, client_id: str = CLIENT,
          **kw) -> dict:
    """把一份字节完整传完（init → 一片 → complete）。"""
    sess = uploads.create_session(
        db, cfg, name=name, size=len(payload), sha256=hashlib.sha256(payload).hexdigest(),
        client_id=client_id, guest=guest_upload, **kw,
    )
    uid = sess["upload_id"]
    assert uid, f"init 未返回 upload_id：{sess}"
    # `end` 是**闭区间**（`end - start + 1` 才是长度），传 len 会多算一个字节。
    uploads.append_chunk(db, cfg, uid, start=0, end=len(payload) - 1,
                         total=len(payload), data=payload)
    return uploads.complete_session(
        db, cfg, uid, indexer=Indexer(cfg, db), guest=guest_upload,
    )


# ------------------------------------------------------------------ 归档位置

def test_guest_bytes_land_in_the_guest_root_only(cfg: Config, db: Database) -> None:
    out = _push(db, cfg, JPEG)
    assert out["rel_path"].startswith("游客上传/"), out["rel_path"]

    row = db.query_one("SELECT root_id, rel_path, uploader_client_id FROM media "
                       "WHERE id = ?", (out["media_id"],))
    assert row["root_id"] == GUEST_ROOT
    assert row["uploader_client_id"] == CLIENT

    # 主人根下一个字节都不该多出来
    owner_files = [p for p in Path(cfg.root_by_id(OWNER_ROOT).path).rglob("*")
                   if p.is_file()]
    assert owner_files == []


def test_guest_cannot_choose_a_different_root(cfg: Config, db: Database) -> None:
    """请求体里说 `root_id=<主人根>` 也没用 —— 游客的目标根是写死的。"""
    out = _push(db, cfg, JPEG, root_id=OWNER_ROOT, target_dir="手机上传")
    row = db.query_one("SELECT root_id, rel_path FROM media WHERE id = ?",
                       (out["media_id"],))
    assert row["root_id"] == GUEST_ROOT
    assert row["rel_path"].startswith("游客上传/")


def test_guest_upload_without_a_guest_root_is_refused(cfg: Config) -> None:
    base = Path(cfg.root_by_id(OWNER_ROOT).path).parent
    lonely = Config(
        {"roots": [{"id": OWNER_ROOT, "path": str(cfg.root_by_id(OWNER_ROOT).path),
                    "mode": "readwrite"}],
         "upload": {"subdir": "手机上传", "chunk_mb_lan": 1, "max_file_mb": 4}},
        path=base / "c2.json", data_dir=base / "d2",
    )
    d = Database(lonely.db_path)
    d.init()
    with pytest.raises(AppError) as exc:
        uploads.create_session(d, lonely, name="a.jpg", size=len(JPEG), guest=True)
    assert "游客" in str(exc.value) or "E1012" in str(exc.value)


def test_view_only_guest_root_refuses_uploads_with_a_clear_reason(tmp_path: Path) -> None:
    """`readonly` 游客根（只看不给发）要给出 "改 mode" 的提示，而不是路径错误。"""
    (tmp_path / "lib").mkdir()
    (tmp_path / "guests").mkdir()
    c = Config(
        {"roots": [
            {"id": OWNER_ROOT, "path": str(tmp_path / "lib"), "mode": "readwrite"},
            {"id": GUEST_ROOT, "path": str(tmp_path / "guests"),
             "mode": "readonly", "visibility": "guest"},
        ], "upload": {"subdir": "手机上传", "chunk_mb_lan": 1, "max_file_mb": 4}},
        path=tmp_path / "c.json", data_dir=tmp_path / "data",
    )
    d = Database(c.db_path)
    d.init()
    with pytest.raises(AppError) as exc:
        uploads.create_session(d, c, name="a.jpg", size=len(JPEG), guest=True)
    assert "upload" in str(exc.value)

# ------------------------------------------------------------------ 秒传

def test_guest_uploads_are_never_deduplicated(cfg: Config, db: Database) -> None:
    """同一份字节传两次要有两条媒体行：去重会泄露路径，而且第二张会"没传"。"""
    first = _push(db, cfg, JPEG, name="a.jpg")
    second = _push(db, cfg, JPEG, name="b.jpg")
    assert first["media_id"] != second["media_id"]
    assert second["rel_path"] != first["rel_path"]
    n = db.query_one("SELECT COUNT(*) AS n FROM media WHERE root_id = ?", (GUEST_ROOT,))
    assert n["n"] == 2


def test_owner_upload_is_unchanged(cfg: Config, db: Database) -> None:
    """主人上传仍是老行为：进 `upload.subdir`、`uploader_client_id` 为空串。"""
    out = _push(db, cfg, JPEG, guest_upload=False, client_id="")
    row = db.query_one("SELECT root_id, rel_path, uploader_client_id FROM media "
                       "WHERE id = ?", (out["media_id"],))
    assert row["root_id"] == OWNER_ROOT
    assert row["rel_path"].startswith("手机上传/")
    assert row["uploader_client_id"] == ""


# ------------------------------------------------------------------ 记账

def test_upload_log_keeps_the_receipt_after_media_is_deleted(cfg: Config,
                                                             db: Database) -> None:
    out = _push(db, cfg, JPEG)
    db.execute("DELETE FROM media WHERE id = ?", (out["media_id"],))
    row = db.query_one("SELECT client_id, media_id, size FROM guest_upload_log "
                       "WHERE rel_path = ?", (out["rel_path"],))
    assert row is not None, "媒体行删了，游客上传的流水不该跟着消失"
    assert row["media_id"] is None       # ON DELETE SET NULL
    assert row["client_id"] == CLIENT
    assert row["size"] == len(JPEG)


def test_guest_upload_is_audited(cfg: Config, db: Database) -> None:
    out = _push(db, cfg, JPEG)
    row = db.query_one("SELECT action, rel_path FROM audit_log "
                       "WHERE action = 'guest_upload' AND rel_path = ?",
                       (out["rel_path"],))
    assert row is not None


# ------------------------------------------------------------------ 邀请/会话上限

def test_invite_limits_are_enforced(cfg: Config, db: Database) -> None:
    """同时有效邀请有上限：否则一张截图就能造出无数个入口。"""
    for i in range(guest.MAX_ACTIVE_INVITES):
        guest.create_invite(db, device_id="d" * 32, label=f"g{i}", minutes=60, via="lan")
    with pytest.raises(AppError):
        guest.create_invite(db, device_id="d" * 32, label="overflow", minutes=60, via="lan")


def test_online_guest_cap_is_enforced(cfg: Config, db: Database) -> None:
    """在线游客总量有上限：每个会话都是一条常驻隧道订阅。"""
    holder = guest.create_invite(db, device_id="d" * 32, label="holder",
                                 minutes=600, via="cloud")
    now = time.time()
    for _ in range(guest.MAX_ONLINE_GUESTS):
        cid = secrets.token_hex(16)
        db.execute(
            "INSERT INTO guest_sessions(client_id, token_hash, invite_id, device_id,"
            " label, created_at, expires_at, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, guest.hash_token(secrets.token_urlsafe(24)), holder["invite_id"],
             "d" * 32, "g", now, now + 600, now),
        )
    fresh = guest.create_invite(db, device_id="d" * 32, label="fresh",
                                minutes=60, via="cloud")
    with pytest.raises(AppError):
        guest.redeem(db, token=fresh["token"], device_id="d" * 32, via="cloud")


def test_expired_invite_cannot_be_redeemed(cfg: Config, db: Database) -> None:
    inv = guest.create_invite(db, device_id="d" * 32, label="g", minutes=5, via="cloud")
    db.execute("UPDATE guest_invites SET expires_at = ? WHERE id = ?",
               (time.time() - 1, inv["invite_id"]))
    with pytest.raises(AppError):
        guest.redeem(db, token=inv["token"], device_id="d" * 32, via="cloud")
