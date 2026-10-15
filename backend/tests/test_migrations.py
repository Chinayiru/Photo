"""数据库迁移的测试。

`CREATE TABLE IF NOT EXISTS` 对已存在的表是**空操作** ——
后来新增的列、新增的 CHECK 约束对老库完全不生效。
用户升级程序后第一次配对就会 `CHECK constraint failed`，
而且是在"配对成功、写审计"那一刻，token 已经发出去了。

这类 bug 只在"老库 + 新代码"的组合下出现，
单元测试用新库跑永远不会发现。所以这里显式构造老库。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.app.db import Database

# 与 001_init_photobridge 时期一致的 audit_log（没有 pair/revoke）
OLD_AUDIT_LOG = """
CREATE TABLE audit_log (
  id          INTEGER PRIMARY KEY,
  ts          REAL NOT NULL,
  device_name TEXT,
  client_id   TEXT,
  action      TEXT NOT NULL
                CHECK (action IN ('upload','delete','move','rename','rescan','config')),
  rel_path    TEXT,
  detail      TEXT
);
"""


def _make_old_db(path: Path) -> None:
    """造一个"上一个版本"的库：只有旧 audit_log 和几行数据。"""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE schema_version (
          version    INTEGER PRIMARY KEY,
          applied_at REAL NOT NULL,
          name       TEXT NOT NULL,
          checksum   TEXT
        );
        INSERT INTO schema_version(version, applied_at, name, checksum)
          VALUES (1, 0, '001_init_photobridge', NULL);
    """)
    conn.executescript(OLD_AUDIT_LOG)
    conn.executescript(
        "INSERT INTO audit_log(ts, device_name, client_id, action, rel_path, detail)"
        " VALUES (1.0, 'PC', NULL, 'delete', 'a.jpg', '{}'),"
        "        (2.0, 'PC', NULL, 'upload', 'b.jpg', '{}');"
    )
    conn.commit()
    conn.close()


def test_migration_adds_pair_action(tmp_path: Path) -> None:
    """老库升级后必须能写 'pair' 动作。"""
    path = tmp_path / "old.db"
    _make_old_db(path)

    db = Database(path)
    db.init()

    # 迁移后能写入新动作（老 CHECK 会拒绝）
    db.execute(
        "INSERT INTO audit_log(ts, device_name, client_id, action, rel_path, detail)"
        " VALUES (?, ?, NULL, 'pair', '', '{}')",
        (3.0, "PC"),
    )
    assert db.scalar("SELECT COUNT(*) FROM audit_log") == 3


def test_migration_preserves_existing_rows(tmp_path: Path) -> None:
    """重建表必须搬走老数据。

    这张表是审计索引，丢掉等于事后无法追查"谁删了照片"。
    """
    path = tmp_path / "old.db"
    _make_old_db(path)

    db = Database(path)
    db.init()

    rows = db.query("SELECT action, rel_path FROM audit_log ORDER BY id")
    assert [r["action"] for r in rows] == ["delete", "upload"]
    assert [r["rel_path"] for r in rows] == ["a.jpg", "b.jpg"]


def test_migration_preserves_indexes(tmp_path: Path) -> None:
    """重建表会连带删掉索引，必须补建。"""
    path = tmp_path / "old.db"
    _make_old_db(path)

    db = Database(path)
    db.init()

    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='audit_log'"
    )}
    assert "idx_audit_ts" in names
    assert "idx_audit_action" in names
    assert "idx_audit_path" in names


def test_migration_is_idempotent(tmp_path: Path) -> None:
    """跑两次不能把表重建两次或报错。"""
    path = tmp_path / "old.db"
    _make_old_db(path)

    for _ in range(3):
        db = Database(path)
        db.init()
        db.close()

    db = Database(path)
    db.init()
    assert db.scalar("SELECT COUNT(*) FROM audit_log") == 2


def test_fresh_db_has_pair_action(tmp_path: Path) -> None:
    """全新库直接就有新 CHECK，不需要迁移。"""
    db = Database(tmp_path / "new.db")
    db.init()
    db.execute(
        "INSERT INTO audit_log(ts, device_name, client_id, action, rel_path, detail)"
        " VALUES (?, ?, NULL, 'revoke', '', '{}')",
        (1.0, "PC"),
    )
    assert db.scalar("SELECT COUNT(*) FROM audit_log") == 1


def test_pair_codes_table_exists(tmp_path: Path) -> None:
    db = Database(tmp_path / "new.db")
    db.init()
    db.execute(
        "INSERT INTO pair_codes(code_hash, device_id, device_name, created_at,"
        " expires_at, tries, locked_until, used_at)"
        " VALUES ('h', 'd', 'n', 1.0, 2.0, 0, NULL, NULL)"
    )
    assert db.scalar("SELECT COUNT(*) FROM pair_codes") == 1


def test_migration_survives_no_audit_log_table(tmp_path: Path) -> None:
    """极老的库可能连 audit_log 都没有（只有 schema_version）。"""
    path = tmp_path / "ancient.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY,"
        " applied_at REAL NOT NULL, name TEXT NOT NULL, checksum TEXT);"
    )
    conn.commit()
    conn.close()

    db = Database(path)
    db.init()   # 不能抛异常
    assert db.scalar("SELECT COUNT(*) FROM audit_log") == 0
