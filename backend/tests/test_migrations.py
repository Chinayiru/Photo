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


# --------------------------------------------------------------------------
# AC-5.10：devices 表补 expires_at 列
# --------------------------------------------------------------------------

# 与"devices 还没有 expires_at"时期一致的建表语句。
OLD_DEVICES = """
CREATE TABLE devices (
  id         TEXT PRIMARY KEY,
  name       TEXT,
  token_hash TEXT,
  last_seen  REAL,
  created_at REAL,
  revoked    INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0,1))
);
"""


def _make_old_db_with_devices(path: Path) -> None:
    """老库 + 一台**已配对**的设备（升级前签发的 token）。"""
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
    conn.executescript(OLD_DEVICES)
    conn.executescript(
        "INSERT INTO devices(id, name, token_hash, last_seen, created_at, revoked)"
        " VALUES ('dev1', '老婆的手机', 'hash1', 5.0, 1.0, 0);"
    )
    conn.commit()
    conn.close()


def test_migration_adds_devices_expires_at(tmp_path: Path) -> None:
    """★★ 老库必须补出 `expires_at` 列。

    不补的话 `_lookup_device` 的 SELECT 会抛
    `no such column: expires_at` —— 而那是**每个已配对用户在升级后
    发出的第一个请求**，表现为"升级后手机全部失联，重装才好"。
    这类 bug 只在"老库 + 新代码"下出现，用新库跑测试永远发现不了。
    """
    path = tmp_path / "old.db"
    _make_old_db_with_devices(path)

    db = Database(path)
    db.init()

    # 列存在，且 SQL 能跑通
    cols = {r[1] for r in db.query("PRAGMA table_info(devices)")}
    assert "expires_at" in cols
    row = db.query_one("SELECT id, expires_at FROM devices WHERE id = 'dev1'")
    assert row["id"] == "dev1"


def test_migration_old_devices_never_expire(tmp_path: Path) -> None:
    """★ 补出来的列必须是 NULL，不能被填上一个过期时刻。

    升级前签发的 token 从来没有过期概念。给它们安一个时刻，会让
    所有已配对的手机在某个时间点**集体掉线** —— 用户什么都没做，
    手机突然要重新配对。那比"永不触发过期"更糟。

    NULL 在本项目里就是"永不过期"（见 `require_client`）。
    """
    path = tmp_path / "old.db"
    _make_old_db_with_devices(path)

    db = Database(path)
    db.init()

    assert db.scalar("SELECT expires_at FROM devices WHERE id = 'dev1'") is None


def test_migration_devices_expires_index_exists(tmp_path: Path) -> None:
    """索引要在补完列之后建。

    ★ 不能写进 `SCHEMA`：`executescript(SCHEMA)` 跑在 `_migrate()`
    **之前**，那时老库还没有 expires_at 列，`CREATE INDEX` 会抛
    `no such column` 把启动整个打断。
    """
    path = tmp_path / "old.db"
    _make_old_db_with_devices(path)

    db = Database(path)
    db.init()

    names = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='devices'"
    )}
    assert "idx_devices_expires" in names


def test_devices_migration_is_idempotent(tmp_path: Path) -> None:
    """跑三次不能重复 ALTER（SQLite 会报 duplicate column name）。"""
    path = tmp_path / "old.db"
    _make_old_db_with_devices(path)

    for _ in range(3):
        db = Database(path)
        db.init()
        db.close()

    db = Database(path)
    db.init()
    cols = [r[1] for r in db.query("PRAGMA table_info(devices)")]
    assert cols.count("expires_at") == 1
    assert db.scalar("SELECT COUNT(*) FROM devices") == 1


def test_fresh_db_devices_has_expires_at(tmp_path: Path) -> None:
    """全新库直接就有这一列。"""
    db = Database(tmp_path / "new.db")
    db.init()
    db.execute(
        "INSERT INTO devices(id, name, token_hash, last_seen, created_at,"
        " revoked, expires_at) VALUES ('d', 'n', 'h', 1.0, 1.0, 0, 99.0)"
    )
    assert db.scalar("SELECT expires_at FROM devices WHERE id = 'd'") == 99.0
