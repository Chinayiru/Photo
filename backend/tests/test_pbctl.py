"""`backend/pbctl.py` 的行为：开窗/关窗只改一列，且不改别的行。"""
from __future__ import annotations

import time
import uuid

import pytest

from backend import pbctl
from backend.app import db as db_mod


@pytest.fixture()
def opened(tmp_path, monkeypatch):
    database = db_mod.Database(tmp_path / "photobridge.db")
    database.init()
    live_id, dead_id = uuid.uuid4().hex, uuid.uuid4().hex
    now = time.time()
    database.execute(
        "INSERT INTO devices(id, name, token_hash, revoked, created_at, role)"
        " VALUES(?, '手机A', 'x', 0, ?, 'owner')", (live_id, now))
    database.execute(
        "INSERT INTO devices(id, name, token_hash, revoked, created_at, role)"
        " VALUES(?, '旧机B', 'y', 1, ?, 'viewer')", (dead_id, now))
    monkeypatch.setattr(pbctl, "_open_db", lambda: (database, None))
    yield database, live_id, dead_id
    database.close()


def _legacy(database, did: str) -> float:
    row = database.query_one(
        "SELECT allow_legacy_until FROM devices WHERE id = ?", (did,))
    return float(row["allow_legacy_until"])


def test_allow_legacy_opens_window_only_for_live_devices(opened) -> None:
    database, live_id, dead_id = opened
    before = time.time()
    assert pbctl.main(["allow-legacy", "7"]) == 0
    assert _legacy(database, live_id) > before + 6.9 * 86400
    # 已撤销的设备不该再拿到窗口（否则"移除手机"就不算数了）
    assert _legacy(database, dead_id) == 0.0


def test_close_legacy_zeroes_every_window(opened) -> None:
    database, live_id, _dead = opened
    assert pbctl.main(["allow-legacy", "3"]) == 0
    assert _legacy(database, live_id) > 0
    assert pbctl.main(["close-legacy"]) == 0
    assert _legacy(database, live_id) == 0.0


def test_allow_legacy_rejects_non_positive_days(opened) -> None:
    database, live_id, _dead = opened
    assert pbctl.main(["allow-legacy", "0"]) == 2
    assert _legacy(database, live_id) == 0.0