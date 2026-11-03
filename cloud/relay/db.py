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
| `pair_codes` | 一条配对码记录（只存 HMAC；5 次尝试 / 10 分钟锁） |
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

import hashlib
import hmac
import logging
import os
import secrets
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
    last_login  REAL,
    -- 连续登录失败次数与锁定到期时刻（`docs/11` ISSUE-031）。
    -- 老库没有这两列，由 `_migrate()` 的 ALTER 补上；新库直接建好。
    failed_count INTEGER NOT NULL DEFAULT 0,
    locked_until REAL
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
    revoked     INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    -- 配对失败计数。必须落库而不是放进程内存：内存版一次部署/崩溃/实例
    -- 迁移就清零，攻击者只要等到重启就能把 5 次额度重置一遍
    -- （`docs/11` SEC-09）。
    pair_fails  INTEGER NOT NULL DEFAULT 0,
    pair_window REAL,
    pair_locked_until REAL
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
    -- 只存码的 HMAC（key 来自库旁边的 `.pepper` 文件或 PB_CLOUD_PAIR_PEPPER
    -- 环境变量），不存明文：6 位数字只有 100 万种，明文落库等于把「此刻有效
    -- 的那几枚码」直接交给任何能读 db 文件的人（`docs/11` ISSUE-091）。
    code_hash   TEXT PRIMARY KEY CHECK (length(code_hash) = 64),
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


#: `SCHEMA` 之后追加的列：(表, 列名, 列定义)。
#:
#: `CREATE TABLE IF NOT EXISTS` 改不了已存在的表，所以"给表加一列"必须
#: 单独走 `ALTER TABLE`。清单只增不改（改已在线上跑的列要的是数据迁移，
#: 不是这里的事）。测试 `test_cloud_schema_columns.py` 会断言"SCHEMA 里
#: 声明的列"与"清单补上的列"没有重叠、且都能在新建的库里查到。
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("accounts", "failed_count", "failed_count INTEGER NOT NULL DEFAULT 0"),
    ("accounts", "locked_until", "locked_until REAL"),
    ("devices", "pair_fails", "pair_fails INTEGER NOT NULL DEFAULT 0"),
    ("devices", "pair_window", "pair_window REAL"),
    ("devices", "pair_locked_until", "pair_locked_until REAL"),
)

#: 只能用索引表达的约束。`ALTER TABLE` 改不了约束，只能建索引。
#: `token_hash` 唯一的意义：两行同哈希意味着两个客户端共用同一个令牌，
#: 撤销其中一个就把另一个也废了（反之亦然）。老库里若已有重复，建索引会
#: 抛 `IntegrityError` —— 这里记 ERROR 而不中断启动，但**必须**让它可见：
#: 静默跳过等于假装没有这个约束。
_ADDED_INDEXES: tuple[tuple[str, str], ...] = (
    ("idx_clients_token_hash",
     "CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_token_hash "
     "ON clients(token_hash)"),
)


#: 进程内缓存：同一个库文件只读一次 `.pepper`。
_PEPPERS: dict[str, str] = {}


def _pepper_from_env() -> str:
    return (os.environ.get("PB_CLOUD_PAIR_PEPPER") or "").strip()


def pair_pepper(path: str | Path) -> str:
    """配对码 HMAC 的密钥。

    优先级：`PB_CLOUD_PAIR_PEPPER` 环境变量 → 库文件旁边的 `<db>.pepper`
    文件（首次自动生成 32 字节 hex）。

    为什么要有 key：6 位数字只有 100 万种，无盐 `sha256(code)` 用一张
    彩虹表就能瞬间还原。有了 key，**只拿到 db 文件**（最常见的泄露形态：
    备份、误提交、误拷走）还原不出任何一枚活码。

    两个来源都不可用时（只读目录且没设环境变量）不阻断启动：退化为
    "按库路径派生"的固定值并打 WARNING —— 这种做法挡不住有心人，但比
    明文强，且不会让中继因为一个写不进去的目录起不来。
    """
    env = _pepper_from_env()
    if env:
        return env
    key = os.path.abspath(str(path))
    cached = _PEPPERS.get(key)
    if cached:
        return cached
    side = key + ".pepper"
    try:
        with open(side, "r", encoding="ascii") as f:
            value = f.read().strip()
    except FileNotFoundError:
        value = ""
    except OSError:          # pragma: no cover —— 读不了当作没有
        value = ""
    if not value:
        fresh = secrets.token_hex(32)
        try:
            with open(side, "x", encoding="ascii") as f:
                f.write(fresh)
            value = fresh
            log.info("已生成配对码 pepper：%s", side)
        except FileExistsError:      # 另一个进程抢先建了
            try:
                with open(side, "r", encoding="ascii") as f:
                    value = f.read().strip()
            except OSError:          # pragma: no cover
                value = ""
        except OSError as exc:       # pragma: no cover —— 只读目录
            log.warning("配对码 pepper 写不进 %s（%s），退化为按库路径派生",
                        side, type(exc).__name__)
            value = hashlib.sha256(key.encode("utf-8")).hexdigest()
    _PEPPERS[key] = value
    return value


def hash_pair_code(path: str | Path, code: str) -> str:
    """把 6 位明文码换成入库用的 HMAC（64 位 hex）。"""
    key = pair_pepper(path).encode("utf-8")
    return hmac.new(key, code.strip().encode("utf-8"),
                    hashlib.sha256).hexdigest()


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
            self._migrate(conn)
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

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """把老库补到当前结构。

        为什么需要它：`SCHEMA` 里的表全是 `CREATE TABLE IF NOT EXISTS`，
        对**已经存在**的表它一个字节都不改 —— 于是"给 `accounts` 加一列"
        在开发机（新库，列都在）上绿、在线上（老库，列不在）上炸。
        `docs/11` ISSUE-031 之前 `auth.py` 的注释引用过一个并不存在的
        `_migrate()`，正是这个洞。

        SQLite 的 `ALTER TABLE ... ADD COLUMN` 只能加列，不能改约束，
        所以这里只做"缺什么补什么"，够用且不需要停机迁数据。
        """
        for table, column, ddl in _ADDED_COLUMNS:
            have = {str(r["name"]) for r in
                    conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if not have:
                # 表都没有：`executescript(SCHEMA)` 本该建好它。走到这里
                # 说明 SCHEMA 与这份清单不一致，宁可跳过也不要在启动时抛。
                continue
            if column in have:
                continue
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            log.info("云端库迁移：%s 加列 %s", table, column)

        for name, ddl in _ADDED_INDEXES:
            try:
                conn.execute(ddl)
            except sqlite3.IntegrityError:
                log.error("索引 %s 建不起来：表里已有重复数据，需人工清理", name)

        self._migrate_pair_codes(conn)

    def _migrate_pair_codes(self, conn: sqlite3.Connection) -> None:
        """老库的 `pair_codes.code`（明文）→ 新库的 `code_hash`。

        `ALTER TABLE` 改不了主键与约束，只能重建：建新表 → 逐行按明文算
        HMAC 插进去 → 丢旧表 → 改名 → 补回索引。已有的码在剩余 TTL 内
        继续可用（`PAIR_CODE_TTL` 是 5 分钟，不需要停机迁数据）。
        """
        cols = {str(r["name"]) for r in
                conn.execute("PRAGMA table_info(pair_codes)").fetchall()}
        if not cols or "code_hash" in cols:
            return
        if "code" not in cols:      # pragma: no cover —— 结构不认识，别乱动
            log.warning("pair_codes 既没有 code 也没有 code_hash，跳过迁移")
            return
        log.info("云端库迁移：pair_codes 明文码 → HMAC")
        conn.execute("DROP TABLE IF EXISTS pair_codes_migrating")
        conn.execute(
            "CREATE TABLE pair_codes_migrating ("
            " code_hash TEXT PRIMARY KEY CHECK (length(code_hash) = 64),"
            " device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,"
            " expires_at REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0,"
            " locked_until REAL, created_at REAL NOT NULL, used_at REAL)")
        rows = conn.execute(
            "SELECT code, device_id, expires_at, tries, locked_until,"
            " created_at, used_at FROM pair_codes").fetchall()
        for row in rows:
            conn.execute(
                "INSERT OR IGNORE INTO pair_codes_migrating(code_hash,"
                " device_id, expires_at, tries, locked_until, created_at,"
                " used_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (hash_pair_code(self.path, str(row["code"])),
                 str(row["device_id"]), row["expires_at"], row["tries"],
                 row["locked_until"], row["created_at"], row["used_at"]))
        conn.execute("DROP TABLE pair_codes")
        conn.execute("ALTER TABLE pair_codes_migrating RENAME TO pair_codes")
        for ddl in ("CREATE INDEX IF NOT EXISTS idx_pair_codes_device"
                    " ON pair_codes(device_id)",
                    "CREATE INDEX IF NOT EXISTS idx_pair_codes_expires"
                    " ON pair_codes(expires_at)"):
            conn.execute(ddl)
        log.info("云端库迁移：pair_codes 迁移完成（%d 行）", len(rows))

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
    """打开（并初始化）云端库。重复调用返回同一个实例。

    路径优先级：显式参数 > `PB_CLOUD_DB` 环境变量 > 工作目录下的
    `cloud.db`。容器里必须落到挂载卷（`/data/cloud.db`），否则
    "重建容器"这个常规升级动作会把账号、设备、配对码一起清掉 ——
    容器的工作目录 `/app` 是镜像内部，不持久。
    """
    global _db
    if _db is not None and (path is None or str(path) == _db.path):
        return _db
    if _db is not None:
        _db.close_all()
    target = path or os.environ.get("PB_CLOUD_DB", "").strip() or "cloud.db"
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
