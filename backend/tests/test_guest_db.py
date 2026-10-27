"""游客访问的数据库层（ISSUE-063 G1 / G4 / G13 / G14）。

## 这批用例在钉什么

1. **三张表必须在**：`guest_invites`（链接）、`guest_sessions`（浏览器）、
   `guest_upload_log`（台账）。缺任何一张，对应的功能就只能靠内存状态 ——
   重启即失效，而 `docs/11` G13 明确要求"状态在库里，不在内存"。
2. **老库升上来也要有**：新库走 `executescript(SCHEMA)`，老库走迁移 9/10/11。
   两条路必须是同一段 DDL（`guest_schema()` 从 `SCHEMA` 里取），这里用
   "造一个 v8 老库再升级"的用例把它钉死 —— 这类"新装能用、升级不能用"的
   缺陷只在存量用户身上出现，最晚被发现。
3. **审计动作要对得上**：`guest_*` 五个动作同时写进 `SCHEMA` 的 CHECK 与
   `audit.ACTIONS`。写一个 CHECK 不接受的动作会抛 `IntegrityError`，而写审计表
   失败是被容忍的（只记日志）→ 记录静默消失。
4. **`uploader_client_id` 是过滤依据**（G4「只看自己上传的」）：列必须
   `NOT NULL DEFAULT ''`（主人上传为空串），并且有 `(root_id, uploader_client_id,
   taken_at DESC, id DESC)` 的索引 —— 没有它，游客翻页会退化成全表扫描。
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.app import audit
from backend.app.db import SCHEMA_VERSION, Database, guest_schema


def _fresh(tmp_path) -> Database:
    db = Database(tmp_path / "guest.db")
    db.init()
    return db


def _insert_audit(db, action: str) -> None:
    """直接写一行审计：既有 CHECK 不接受就抛 IntegrityError。"""
    db.execute(
        "INSERT INTO audit_log(ts, client_id, action, rel_path, detail)"
        " VALUES (?, 'c1', ?, '', '{}')", (1.0, action))


# ---------------------------------------------------------------- 表与列

def test_guest_tables_exist(tmp_path) -> None:
    db = _fresh(tmp_path)
    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'guest%'")}
    assert names == {"guest_invites", "guest_sessions", "guest_upload_log"}


def test_media_has_uploader_column_with_empty_default(tmp_path) -> None:
    db = _fresh(tmp_path)
    cols = {r["name"]: r for r in db.query("PRAGMA table_info(media)")}
    assert "uploader_client_id" in cols, "G4 的过滤依据不在表里"
    col = cols["uploader_client_id"]
    assert col["notnull"] == 1
    assert col["dflt_value"] == "''", "主人上传没有上传者，默认必须是空串"

    db.execute(
        "INSERT INTO media(root_id, rel_path, file_name, ext, size, mtime,"
        " taken_at) VALUES ('guest', 'a.jpg', 'a.jpg', 'jpg', 1, 0, 0)")
    row = db.query_one("SELECT uploader_client_id FROM media WHERE rel_path='a.jpg'")
    assert row["uploader_client_id"] == ""


def test_uploader_filter_index_exists(tmp_path) -> None:
    db = _fresh(tmp_path)
    sql = db.scalar(
        "SELECT sql FROM sqlite_master WHERE type='index'"
        " AND name='idx_media_uploader'")
    assert sql and "uploader_client_id" in sql
    # 顺序必须与游标分页的 ORDER BY 一致，否则索引用不上
    assert "taken_at DESC, id DESC" in sql

    plan = " | ".join(
        r["detail"] for r in db.query(
            "EXPLAIN QUERY PLAN SELECT id FROM media"
            " WHERE root_id='guest' AND uploader_client_id='x'"
            " ORDER BY taken_at DESC, id DESC LIMIT 30"))
    assert "idx_media_uploader" in plan, plan


def test_schema_version_covers_the_guest_migrations() -> None:
    assert SCHEMA_VERSION >= 11


# ---------------------------------------------------------------- 约束

def test_invite_token_hash_is_unique(tmp_path) -> None:
    db = _fresh(tmp_path)
    sql = ("INSERT INTO guest_invites(id, device_id, token_hash, created_at,"
           " expires_at) VALUES (?, 'dev', 'same', 0, 1)")
    db.execute(sql, ("id1",))
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(sql, ("id2",))
    assert db.scalar("SELECT COUNT(*) FROM guest_invites") == 1


def test_session_rows_follow_their_invite(tmp_path) -> None:
    db = _fresh(tmp_path)
    db.execute(
        "INSERT INTO guest_invites(id, device_id, token_hash, created_at,"
        " expires_at) VALUES ('inv1', 'dev', 'h1', 0, 1)")
    db.execute(
        "INSERT INTO guest_sessions(client_id, token_hash, invite_id, device_id,"
        " created_at, expires_at) VALUES ('c1', 's1', 'inv1', 'dev', 0, 1)")
    db.execute("DELETE FROM guest_invites WHERE id='inv1'")
    assert db.scalar("SELECT COUNT(*) FROM guest_sessions") == 0, \
        "邀请撤销后会话必须跟着走（ON DELETE CASCADE）"


def test_upload_log_survives_media_deletion(tmp_path) -> None:
    db = _fresh(tmp_path)
    db.execute(
        "INSERT INTO media(root_id, rel_path, file_name, ext, size, mtime,"
        " taken_at) VALUES ('guest', 'b.jpg', 'b.jpg', 'jpg', 5, 0, 0)")
    mid = db.scalar("SELECT MAX(id) FROM media")
    db.execute(
        "INSERT INTO guest_upload_log(client_id, root_id, media_id, rel_path,"
        " size, created_at) VALUES ('c1', 'guest', ?, 'b.jpg', 5, 0)", (mid,))
    db.execute("DELETE FROM media WHERE id = ?", (mid,))
    row = db.query_one("SELECT media_id, size FROM guest_upload_log")
    assert row["media_id"] is None, "台账要留下「传过多大一个文件」，但允许媒体记录消失"
    assert row["size"] == 5


# ---------------------------------------------------------------- 审计动作

def test_guest_actions_are_in_both_the_check_and_the_python_set(tmp_path) -> None:
    db = _fresh(tmp_path)
    sql = db.scalar(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'")
    for action in ("guest_invite", "guest_enter", "guest_upload",
                   "guest_revoke", "guest_denied"):
        assert f"'{action}'" in sql, f"audit_log 的 CHECK 不认 {action}"
        assert action in audit.ACTIONS, f"audit.ACTIONS 漏了 {action}"
    # 真的能写进去（CHECK 骗不过去）
    _insert_audit(db, audit.ACTION_GUEST_ENTER)
    assert db.scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action='guest_enter'") == 1


# ---------------------------------------------------------------- 老库升级

def _make_v8_database(path) -> None:
    """造一个"游客功能之前"的库：没有 guest 表/列，audit_log 也不认 guest_*。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            "CREATE TABLE schema_version (version INTEGER PRIMARY KEY,"
            " applied_at REAL, name TEXT, checksum TEXT);"
            "CREATE TABLE media (id INTEGER PRIMARY KEY, root_id TEXT NOT NULL,"
            " rel_path TEXT NOT NULL, file_name TEXT NOT NULL, ext TEXT NOT NULL,"
            " kind TEXT NOT NULL DEFAULT 'image', size INTEGER NOT NULL,"
            " mtime REAL NOT NULL, sha256 TEXT, taken_at REAL NOT NULL,"
            " taken_src TEXT NOT NULL DEFAULT 'mtime', width INTEGER,"
            " height INTEGER, camera TEXT, lens TEXT, iso INTEGER, fnum REAL,"
            " exposure REAL, focal REAL, gps_lat REAL, gps_lon REAL,"
            " orientation INTEGER, duration REAL, vcodec TEXT, acodec TEXT,"
            " container TEXT, faststart INTEGER,"
            " thumb_state TEXT NOT NULL DEFAULT 'pending', thumb_etag TEXT,"
            " missing INTEGER NOT NULL DEFAULT 0, indexed_at REAL,"
            " UNIQUE(root_id, rel_path));"
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY, ts REAL NOT NULL,"
            " device_name TEXT, client_id TEXT, action TEXT NOT NULL"
            "  CHECK (action IN ('upload','delete','move','rename','rescan',"
            "                    'config','pair','revoke','upload_reject')),"
            " rel_path TEXT, detail TEXT);"
            # 老库里已经有一条媒体和一条审计，升级不能把它们弄丢
            "INSERT INTO media(root_id, rel_path, file_name, ext, size, mtime,"
            " taken_at) VALUES ('library', 'old.jpg', 'old.jpg', 'jpg', 9, 0, 0);"
            "INSERT INTO audit_log(ts, action, rel_path)"
            " VALUES (1.0, 'upload', 'old.jpg');"
        )
        for v in range(1, 9):
            conn.execute(
                "INSERT INTO schema_version(version, applied_at, name, checksum)"
                " VALUES (?, 0, ?, NULL)", (v, f"00{v}_legacy"))
        conn.commit()
    finally:
        conn.close()


def test_upgrade_from_v8_adds_everything_and_keeps_data(tmp_path) -> None:
    path = tmp_path / "old.db"
    _make_v8_database(path)

    db = Database(path)
    db.init()

    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'guest%'")}
    assert names == {"guest_invites", "guest_sessions", "guest_upload_log"}

    cols = {r["name"] for r in db.query("PRAGMA table_info(media)")}
    assert "uploader_client_id" in cols

    assert db.scalar("SELECT COUNT(*) FROM media") == 1
    assert db.scalar("SELECT COUNT(*) FROM audit_log") == 1
    assert db.schema_version() == SCHEMA_VERSION

    # 升级后能写 guest_* 审计（迁移 11 真的重建了 CHECK）
    _insert_audit(db, audit.ACTION_GUEST_INVITE)


def test_upgrade_is_idempotent(tmp_path) -> None:
    path = tmp_path / "old.db"
    _make_v8_database(path)
    for _ in range(2):
        db = Database(path)
        db.init()
        db.close()
    db = Database(path)
    db.init()
    assert db.schema_version() == SCHEMA_VERSION
    assert db.scalar("SELECT COUNT(*) FROM schema_version") == SCHEMA_VERSION


def test_guest_schema_is_extracted_from_the_single_source() -> None:
    """迁移 9 用的 DDL 必须就是 `SCHEMA` 里那一段（不许有两份）。"""
    body = guest_schema()
    assert "CREATE TABLE IF NOT EXISTS guest_invites" in body
    assert "CREATE TABLE IF NOT EXISTS guest_sessions" in body
    assert "CREATE TABLE IF NOT EXISTS guest_upload_log" in body
    assert body.strip().endswith(";")
