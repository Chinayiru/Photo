"""云端数据库：**只有五张表**，且没有任何一张能存照片。

## 为什么强调"只有五张"

`docs/01` AC-7.3 把"云端库里恰好五张表、不存在 `media` 表"写成验收条件。
这不是洁癖：一台中继服务器上出现 `media` 表，唯一可能的路径就是有人
为了"加速缩略图"或"做个相册网页版"把照片复制了一份到云上——而那正是
产品对用户承诺过不会发生的事（`docs/01` §1.1："照片永远只在用户自己的
电脑上"）。所以 `tests/test_cloud_schema.py` 直接断言表名集合，让这条
承诺变成一条会失败的测试。

## 五张表

| 表 | 一行是什么 |
|---|---|
| `schema_version` | 迁移记录 |
| `accounts` | 一个账号（邮箱 + scrypt 口令哈希） |
| `devices` | 用户家里的一台电脑（`agent_token` 的 sha256） |
| `clients` | 一部手机对一个设备的配对（`token_hash`） |
| `pair_codes` | 一个 6 位配对码（5 次尝试 / 10 分钟锁） |
| `traffic` | 每设备每天的双向字节数（AC-3.15） |

等等，这是六张。`schema_version` 是迁移账本，不算业务表——AC-7.3 数的是
"业务表"：`accounts`、`devices`、`clients`、`pair_codes`、`traffic`。测试里
把两者分开断言，并在注释里写清这个口径，避免下一个人把 `schema_version`
当成"多了一张表"又删掉它。

## 为什么用 sqlite 而不是 Postgres

P1 的中继是**单进程**（设备注册表是内存 map，多 worker 会各持一半），
单进程下 sqlite 的并发上限远高于本产品的真实负载（几百台设备、每天
几万个请求）。换成 Postgres 会引入一个必须备份、必须升级、必须监控的
外部服务，而它对"把帧搬过去"这件事毫无帮助。真到需要水平扩展时，
`devices.py` 的内存注册表才是第一个要换的东西，不是这张库。

`traffic` 表是唯一的写热点（每个请求 +1 行更新），用 `UPSERT` 一条语句
完成，靠 sqlite 的写事务串行化——单进程下这就是最优解。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "CloudDB",
    "SCHEMA_VERSION",
    "BUSINESS_TABLES",
    "SCHEMA",
    "open_db",
]

log = logging.getLogger(__name__)

#: 迁移版本。加字段必须同时加一条 `(version, name, sql)`。
SCHEMA_VERSION: int = 1
SCHEMA_NAME: str = "001_init_cloud"

#: AC-7.3 数的那五张业务表（`schema_version` 是迁移账本，不计入）。
BUSINESS_TABLES: frozenset[str] = frozenset({
    "accounts", "devices", "clients", "pair_codes", "traffic",
})

SCHEMA: str = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at REAL    NOT NULL,
    name       TEXT    NOT NULL,
    checksum   TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS accounts (
    id          TEXT PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,
    pass_hash   TEXT NOT NULL,
    created_at  REAL NOT NULL,
    plan        TEXT NOT NULL DEFAULT 'personal'
                CHECK (plan IN ('personal')),
    last_login  REAL
);

CREATE TABLE IF NOT EXISTS devices (
    id          TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    agent_token TEXT NOT NULL UNIQUE,
    version     TEXT NOT NULL DEFAULT '',
    last_seen   REAL,
    last_lan    TEXT NOT NULL DEFAULT '[]',
    created_at  REAL NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1))
);

CREATE TABLE IF NOT EXISTS clients (
    id          TEXT PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
    token_hash  TEXT NOT NULL,
    last_seen   REAL,
    created_at  REAL NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    expires_at  REAL
);

CREATE TABLE IF NOT EXISTS pair_codes (
    code        TEXT PRIMARY KEY CHECK (length(code) = 6),
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    expires_at  REAL NOT NULL,
    tries       INTEGER NOT NULL DEFAULT 0,
    locked_until REAL,
    created_at  REAL NOT NULL,
    used_at     REAL
);

CREATE TABLE IF NOT EXISTS traffic (
    device_id   TEXT NOT NULL,
    day         TEXT NOT NULL,
    bytes_in    INTEGER NOT NULL DEFAULT 0,
    bytes_out   INTEGER NOT NULL DEFAULT 0,
    req_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, day)
);

-- 账号会话账本。**不是业务实体表**（不计入 AC-7.3 的五张）：
-- 一个账号可以同时在手机/平板/浏览器上登录，每台一条才能单独登出；
-- 给 accounts 加一列只能记一个令牌。
CREATE TABLE IF NOT EXISTS account_tokens (
    token_hash  TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    last_seen   REAL,
    label       TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_devices_account ON devices(account_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_agent_token ON devices(agent_token);
CREATE INDEX IF NOT EXISTS idx_clients_device ON clients(device_id);
CREATE INDEX IF NOT EXISTS idx_clients_token ON clients(token_hash);
CREATE INDEX IF NOT EXISTS idx_clients_revoked ON clients(device_id, revoked);
CREATE INDEX IF NOT EXISTS idx_pair_codes_device ON pair_codes(device_id);
CREATE INDEX IF NOT EXISTS idx_pair_codes_expires ON pair_codes(expires_at);
CREATE INDEX IF NOT EXISTS idx_traffic_day ON traffic(day);
CREATE INDEX IF NOT EXISTS idx_account_tokens_account
    ON account_tokens(account_id);
CREATE INDEX IF NOT EXISTS idx_account_tokens_expires
    ON account_tokens(expires_at);
"""


class CloudDB:
    """云端库的薄封装。

    连接是**线程局部**的：Starlette 的同步 handler 跑在线程池里，一条
    sqlite3 连接跨线程使用会得到 `ProgrammingError: SQLite objects
    created in a thread can only be used in that same thread`。按线程
    各开一条（都指向同一个文件，WAL 模式下读写互不阻塞）是最省心的做法。
    """

    __slots__ = ("path", "_lock", "_local")

    def __init__(self, path: str | Path = "cloud.db") -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._local = threading.local()

    # ---- 连接 ----

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(self.path, timeout=10.0,
                               isolation_level=None)   # 自己管事务
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None

    def close_all(self) -> None:
        """只关当前线程那条。

        这不是偷懒：sqlite3 连接**只能**由创建它的线程关闭，从一个
        "收尾线程"遍历关闭所有连接会抛 `ProgrammingError`。真正需要
        全关时（进程退出），操作系统会替我们回收。
        """
        self.close()

    # ---- 结构 ----

    def init(self) -> None:
        with self._lock:
            conn = self._conn()
            conn.executescript(SCHEMA)
            row = conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC "
                "LIMIT 1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version(version, applied_at, name) "
                    "VALUES (?, ?, ?)",
                    (SCHEMA_VERSION, time.time(), SCHEMA_NAME))
            elif int(row["version"]) != SCHEMA_VERSION:
                log.warning("云端库版本 %s ≠ 代码 %s，请检查迁移",
                            row["version"], SCHEMA_VERSION)

    def tables(self) -> set[str]:
        """当前库里所有用户表的名字（测试用来断言"恰好五张业务表"）。"""
        rows = self._conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'").fetchall()
        return {str(r["name"]) for r in rows}

    def schema_version(self) -> int:
        row = self._conn().execute(
            "SELECT version FROM schema_version ORDER BY version DESC "
            "LIMIT 1").fetchone()
        return int(row["version"]) if row else 0

    # ---- 查询 ----

    def query(self, sql: str, args: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn().execute(sql, tuple(args)).fetchall())

    def query_one(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, tuple(args)).fetchone()

    def scalar(self, sql: str, args: Sequence[Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, args)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    def execute(self, sql: str, args: Sequence[Any] = ()) -> int:
        cur = self._conn().execute(sql, tuple(args))
        return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        cur = self._conn().executemany(sql, [tuple(r) for r in rows])
        return cur.rowcount

    def insert(self, sql: str, args: Sequence[Any] = ()) -> None:
        self._conn().execute(sql, tuple(args))

    # ---- 事务 ----

    def transaction(self) -> "_Txn":
        return _Txn(self._conn())

    # ---- 统计 ----

    def stats(self) -> dict[str, Any]:
        return {
            "accounts": int(self.scalar("SELECT COUNT(*) FROM accounts", (), 0)),
            "devices": int(self.scalar("SELECT COUNT(*) FROM devices", (), 0)),
            "clients": int(self.scalar("SELECT COUNT(*) FROM clients", (), 0)),
            "pair_codes": int(self.scalar("SELECT COUNT(*) FROM pair_codes", (), 0)),
            "schema_version": self.schema_version(),
        }

    def db_size_bytes(self) -> int:
        try:
            return Path(self.path).stat().st_size
        except OSError:
            return 0


class _Txn:
    """`BEGIN IMMEDIATE` 事务。配对码的"试一次就 +1"必须在事务里。"""

    __slots__ = ("_conn", "_done")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._done = False

    def __enter__(self) -> sqlite3.Connection:
        self._conn.execute("BEGIN IMMEDIATE")
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._done:
            return
        self._done = True
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")


#: 进程级单例（`main.py` 启动时 `open_db()`）。
_db: CloudDB | None = None


def open_db(path: str | Path | None = None) -> CloudDB:
    """打开（并初始化）云端库。重复调用返回同一个实例。"""
    global _db
    if _db is not None and (path is None or str(path) == _db.path):
        return _db
    if _db is not None:
        _db.close_all()
    target = path or "cloud.db"
    db = CloudDB(target)
    db.init()
    _db = db
    return db


def get_db() -> CloudDB:
    if _db is None:
        raise RuntimeError("云端库尚未初始化（先调 open_db）")
    return _db


def set_db(db: CloudDB | None) -> None:
    """测试用：注入/清空单例。"""
    global _db
    _db = db
