"""迁移记账（`schema_version` 逐版一条 + checksum）的契约。

## 为什么值得单测

`docs/05 §7.1` 承诺了两件事，而这两件事在早期实现里其实都不成立（外部审查
Q-01 点出来的）：

1. **能看出升级历史**：每一行代表一次已应用的迁移。早期实现只写一行
   `SCHEMA_VERSION = 1`，`checksum` 恒为 NULL —— 换句话说"迁移了几次、哪天
   跑的"根本查不到。
2. **能发现迁移被改过**：`checksum` 对不上就在启动时报错。早期实现里这一列
   永远是 NULL，等于没有这道闸门。

现在的实现：迁移仍写在 `backend/app/db.py` 里（有些步骤必须由 Python 表达），
但记账照文档来 —— 每版一条记录，`checksum` 取**该迁移函数源码**的
sha256 前 16 位。

## 老库怎么办

线上已有库只有"一行 version=1、checksum 为 NULL"。这里必须有**补记**分支：
否则所有存量用户升级后启动即失败（checksum NULL ≠ 实际值 → 被判漂移）。
补记只发生一次，之后走正常校验。
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.app import db as dbmod
from backend.app.db import (
    MIGRATIONS,
    SCHEMA,
    SCHEMA_VERSION,
    Database,
    MigrationDrift,
    _migration_checksum,
)


def test_ledger_versions_are_strictly_increasing() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(set(versions)), "版本号必须唯一且递增"
    assert versions[0] == 1, "基线必须是 1"
    assert SCHEMA_VERSION == versions[-1], "SCHEMA_VERSION 必须等于最后一版"


def test_fresh_database_records_every_migration(tmp_path) -> None:
    db = Database(tmp_path / "fresh.db")
    db.init()
    rows = db.query(
        "SELECT version, name, checksum, applied_at FROM schema_version "
        "ORDER BY version")
    assert [r["version"] for r in rows] == [m.version for m in MIGRATIONS]
    assert [r["name"] for r in rows] == [m.name for m in MIGRATIONS]
    for row, mig in zip(rows, MIGRATIONS, strict=True):
        assert row["checksum"], f"{mig.name} 没有记 checksum"
        assert row["checksum"] == _migration_checksum(mig.apply)
        assert len(row["checksum"]) == 16
        assert row["applied_at"] > 0
    assert db.schema_version() == SCHEMA_VERSION
    db.close()


def test_init_is_idempotent_and_does_not_duplicate_rows(tmp_path) -> None:
    db = Database(tmp_path / "twice.db")
    db.init()
    first = db.scalar("SELECT COUNT(*) FROM schema_version")
    db.init()
    db.init()
    assert db.scalar("SELECT COUNT(*) FROM schema_version") == first
    assert db.schema_version() == SCHEMA_VERSION
    db.close()


def test_editing_an_applied_migration_refuses_to_start(tmp_path) -> None:
    """checksum 非空且对不上 → 拒绝启动（docs/05 §7.1 的硬要求）。"""
    db = Database(tmp_path / "drift.db")
    db.init()
    db.execute(
        "UPDATE schema_version SET checksum = 'deadbeefdeadbeef' WHERE version = 2")
    with pytest.raises(MigrationDrift) as ei:
        db.init()
    msg = str(ei.value)
    assert "002" in msg and "不可修改" in msg
    db.close()


def test_legacy_null_checksum_is_backfilled_not_treated_as_drift(tmp_path) -> None:
    """老库只有一行 (1, NULL)：补记 checksum，不许判成漂移。"""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(str(path))
    raw.executescript(SCHEMA)
    raw.execute(
        "INSERT INTO schema_version(version, applied_at, name, checksum) "
        "VALUES (1, 0, '001_init_photobridge', NULL)")
    raw.commit()
    raw.close()

    db = Database(path)
    db.init()                                  # 不许抛 MigrationDrift

    row = db.query_one("SELECT checksum, name FROM schema_version WHERE version = 1")
    assert row["checksum"] == _migration_checksum(MIGRATIONS[0].apply)
    assert db.scalar("SELECT COUNT(*) FROM schema_version") == len(MIGRATIONS)
    assert db.schema_version() == SCHEMA_VERSION
    db.close()


def test_recorded_checksums_match_the_migration_bodies() -> None:
    """写死的 checksum 必须跟得上函数体（**这是防漂移的 CI 闸门**）。

    运行期比对的是"账本里那一行"与"`MIGRATIONS` 里的常量"，两次都不读源码 ——
    因为 PyInstaller 冻结后没有 `.py` 文件（`verify_exe.py` 实测过
    `OSError: could not get source code`）。所以"定义改了没改常量"只能在这里拦。
    """
    for m in MIGRATIONS:
        assert m.checksum == _migration_checksum(m.apply), (
            f"{m.name} 的函数体改过了，但 checksum 常量没更新："
            f"应为 {_migration_checksum(m.apply)}")
        assert len(m.checksum) == 16


def test_migrate_does_not_read_source_at_runtime(tmp_path, monkeypatch) -> None:
    """冻结产物里没有源码：`_migrate()` 一碰 `inspect.getsource` 就崩。

    这条用例就是在复现那次事故（exe 双击即退出，日志里是 OSError）。把
    `inspect.getsource` 换成必炸的实现，`init()` 仍必须成功。
    """
    import inspect as real_inspect

    def boom(*_a, **_kw):
        raise OSError("could not get source code")

    monkeypatch.setattr(real_inspect, "getsource", boom)
    db = Database(tmp_path / "frozen_like.db")
    db.init()
    assert db.schema_version() == SCHEMA_VERSION
    db.close()


def test_a_missing_version_is_applied_on_next_start(tmp_path, monkeypatch) -> None:
    """账本比库里少一版（模拟"新版本带了新迁移"）→ 下次启动补上。"""
    db = Database(tmp_path / "increment.db")
    db.init()
    db.execute("DELETE FROM schema_version WHERE version = ?", (SCHEMA_VERSION,))

    order: list[int] = []
    real = MIGRATIONS[-1]

    def _spy(conn) -> None:
        order.append(real.version)
        real.apply(conn)

    monkeypatch.setattr(
        dbmod, "MIGRATIONS",
        MIGRATIONS[:-1] + (dbmod.Migration(real.version, real.name,
                                           real.checksum, _spy),))
    # _migrate 里引用的是模块级 MIGRATIONS，monkeypatch 生效
    db.init()
    assert order == [real.version], "缺失的那一版应当被执行且只执行一次"
    assert db.schema_version() == SCHEMA_VERSION
    db.close()
