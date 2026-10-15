"""SQLite 存储层：连接管理、schema、便捷查询。"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Sequence

from . import config

_local = threading.local()
_write_lock = threading.RLock()

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;
PRAGMA temp_store=MEMORY;
PRAGMA cache_size=-32000;

CREATE TABLE IF NOT EXISTS roots (
  id          TEXT PRIMARY KEY,
  path        TEXT NOT NULL,
  label       TEXT,
  mode        TEXT NOT NULL DEFAULT 'readonly',
  recursive   INTEGER NOT NULL DEFAULT 1,
  created_at  REAL
);

CREATE TABLE IF NOT EXISTS media (
  id          INTEGER PRIMARY KEY,
  root_id     TEXT NOT NULL,
  rel_path    TEXT NOT NULL,
  file_name   TEXT NOT NULL,
  ext         TEXT NOT NULL,
  kind        TEXT NOT NULL,
  size        INTEGER NOT NULL,
  mtime       REAL NOT NULL,
  sha256      TEXT,
  taken_at    REAL NOT NULL,
  taken_src   TEXT,
  width       INTEGER,
  height      INTEGER,
  duration    REAL,
  camera      TEXT,
  lens        TEXT,
  iso         INTEGER,
  fnum        REAL,
  exposure    REAL,
  focal       REAL,
  gps_lat     REAL,
  gps_lon     REAL,
  orientation INTEGER,
  live_pair   TEXT,
  is_live     INTEGER NOT NULL DEFAULT 0,
  thumb_state TEXT NOT NULL DEFAULT 'pending',
  missing     INTEGER NOT NULL DEFAULT 0,
  indexed_at  REAL,
  UNIQUE(root_id, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_media_timeline ON media(taken_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_media_root     ON media(root_id, rel_path);
CREATE INDEX IF NOT EXISTS idx_media_sha      ON media(sha256);
CREATE INDEX IF NOT EXISTS idx_media_kind     ON media(kind, taken_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_missing  ON media(missing);

CREATE TABLE IF NOT EXISTS tags (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  color TEXT
);
CREATE TABLE IF NOT EXISTS media_tags (
  media_id INTEGER NOT NULL,
  tag_id   INTEGER NOT NULL,
  PRIMARY KEY(media_id, tag_id)
);

CREATE TABLE IF NOT EXISTS albums (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  cover_media_id INTEGER,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS album_items (
  album_id INTEGER NOT NULL,
  media_id INTEGER NOT NULL,
  sort     INTEGER,
  PRIMARY KEY(album_id, media_id)
);

CREATE TABLE IF NOT EXISTS upload_sessions (
  id            TEXT PRIMARY KEY,
  root_id       TEXT NOT NULL,
  target_rel    TEXT NOT NULL,
  temp_path     TEXT NOT NULL,
  total_size    INTEGER NOT NULL,
  received      INTEGER NOT NULL DEFAULT 0,
  chunk_size    INTEGER NOT NULL,
  sha256        TEXT,
  declared_name TEXT,
  taken_at      REAL,
  created_at    REAL,
  expires_at    REAL,
  status        TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS devices (
  id         TEXT PRIMARY KEY,
  name       TEXT,
  token_hash TEXT NOT NULL,
  last_seen  REAL,
  created_at REAL,
  revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS job_state (
  key   TEXT PRIMARY KEY,
  value TEXT
);
"""


def db_path() -> str:
    return str(config.data_path("photobridge.db"))


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path(), timeout=30.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def close_current() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        conn.close()
        _local.conn = None


def init_db() -> None:
    with _write_lock:
        conn = connect()
        conn.executescript(SCHEMA)
        conn.commit()


def query(sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    cur = connect().execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return rows


def query_one(sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    cur = connect().execute(sql, params)
    row = cur.fetchone()
    cur.close()
    return row


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    with _write_lock:
        conn = connect()
        cur = conn.execute(sql, params)
        conn.commit()
        rid = cur.lastrowid
        cur.close()
        return int(rid or 0)


def executemany(sql: str, seq: Iterable[Sequence[Any]]) -> None:
    with _write_lock:
        conn = connect()
        conn.executemany(sql, seq)
        conn.commit()


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    with _write_lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def scalar(sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
    row = query_one(sql, params)
    if row is None:
        return default
    return row[0]


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_setting(key: str, default: str | None = None) -> str | None:
    v = scalar("SELECT value FROM settings WHERE key=?", (key,))
    return default if v is None else str(v)


def stats() -> dict[str, Any]:
    total = scalar("SELECT COUNT(*) FROM media WHERE missing=0", (), 0)
    images = scalar("SELECT COUNT(*) FROM media WHERE missing=0 AND kind='image'", (), 0)
    videos = scalar("SELECT COUNT(*) FROM media WHERE missing=0 AND kind='video'", (), 0)
    pending = scalar("SELECT COUNT(*) FROM media WHERE thumb_state='pending'", (), 0)
    return {
        "total": total,
        "images": images,
        "videos": videos,
        "thumbs_pending": pending,
        "db_size": _db_size(),
        "ts": time.time(),
    }


def _db_size() -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = config.data_path("photobridge.db" + suffix)
        if p.exists():
            total += p.stat().st_size
    return total
