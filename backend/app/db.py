"""PhotoBridge 电脑端 SQLite 数据层。

权威 DDL：docs/05-数据库设计文档.md §3.2（已用 SQLite 3.49 实跑验证）。

三个必须做对的点（都来自 docs/05 §10 的实测结论）：

1. **每个新连接都要重设 PRAGMA**。`busy_timeout` 是**连接级**的，默认 0。
   只在 init_db() 里设一次，之后每个新连接都是 0 → 多线程写入会**立刻**
   抛 `SQLITE_BUSY` 而不是等待。FastAPI 的同步 handler 跑在线程池里，
   所以这里用线程本地连接，且每条连接都跑一遍 PRAGMA。

2. **不用 `roots` 表**。根目录配置在 config.json 里，两张表存同一份配置
   必然双写不一致（docs/05 §10.2 ④ 的裁决）。

3. **时间线游标分页的索引顺序必须是 `(taken_at DESC, id DESC)`**，
   与 ORDER BY 逐字一致，否则退化成 TEMP B-TREE 排序。
   实测：无索引 1.197 ms/次，有索引 0.071 ms/次（快 16.8 倍）。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

from . import config as cfgmod
from .errors import E3002, AppError

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 建表脚本
# --------------------------------------------------------------------------

SCHEMA_VERSION: Final[int] = 1
SCHEMA_NAME: Final[str] = "001_init_photobridge"

SCHEMA: Final[str] = """
-- ============================================================
-- schema_version：迁移基线
-- ============================================================
CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at REAL    NOT NULL,
  name       TEXT    NOT NULL,
  checksum   TEXT
);

-- ============================================================
-- media：媒体元数据（核心表）
-- ============================================================
CREATE TABLE IF NOT EXISTS media (
  id          INTEGER PRIMARY KEY,               -- = rowid，自增，永不复用
  root_id     TEXT    NOT NULL,                  -- 根标识，如 'library'
  rel_path    TEXT    NOT NULL,                  -- 相对根，统一 '/' 分隔
  file_name   TEXT    NOT NULL,                  -- 文件名（含扩展名）
  ext         TEXT    NOT NULL,                  -- 扩展名，小写，不含点
  kind        TEXT    NOT NULL DEFAULT 'image'
                CHECK (kind IN ('image','video')),  -- 视频见 ISSUE-055
  size        INTEGER NOT NULL CHECK (size >= 0),
  mtime       REAL    NOT NULL,                  -- 文件系统 mtime（Unix 秒）
  sha256      TEXT,                              -- 内容哈希；索引时默认不填
  taken_at    REAL    NOT NULL,                  -- ★ 排序主键（Unix 秒）
  taken_src   TEXT    NOT NULL DEFAULT 'mtime'
                CHECK (taken_src IN ('exif','filename','mtime')),
  width       INTEGER CHECK (width  IS NULL OR width  > 0),
  height      INTEGER CHECK (height IS NULL OR height > 0),
  camera      TEXT,
  lens        TEXT,
  iso         INTEGER CHECK (iso IS NULL OR iso > 0),
  fnum        REAL    CHECK (fnum IS NULL OR fnum > 0),
  exposure    REAL    CHECK (exposure IS NULL OR exposure > 0),  -- 秒
  focal       REAL    CHECK (focal IS NULL OR focal > 0),        -- mm
  gps_lat     REAL    CHECK (gps_lat IS NULL OR (gps_lat >= -90  AND gps_lat <= 90)),
  gps_lon     REAL    CHECK (gps_lon IS NULL OR (gps_lon >= -180 AND gps_lon <= 180)),
  orientation INTEGER CHECK (orientation IS NULL OR orientation BETWEEN 1 AND 8),
  -- ★★ 视频（kind='video'）专用；图片行为 NULL ★★
  --
  -- 为什么放在 media 表而不是另开 media_video：时间线必须能"一条 SQL
  -- 按时间倒序混排图片与视频"。分表就得 UNION，而 UNION 会让
  -- `idx_media_timeline` 的 ORDER BY 优化失效（SQLite 对复合查询的
  -- 排序只能落临时 B 树），5000 行就会明显卡；而且分页游标要跨两张表
  -- 做键集扫描，复杂度换来的是零收益。
  --
  -- 四列都是可空的：老库里全是 NULL，代码里一律按"未知"处理。
  duration    REAL    CHECK (duration IS NULL OR duration >= 0),  -- 秒
  vcodec      TEXT,                              -- 'h264'/'hevc'/'vp9'/'av1'
  acodec      TEXT,                              -- 'aac'/'opus'/...
  container   TEXT,                              -- 'mp4'/'mov'/'webm'/'mkv'/'avi'
  thumb_state TEXT    NOT NULL DEFAULT 'pending'
                CHECK (thumb_state IN ('pending','ok','failed')),
  thumb_etag  TEXT,                              -- 缩略图内容哈希（用于强 ETag + URL 版本参数）
  missing     INTEGER NOT NULL DEFAULT 0 CHECK (missing IN (0,1)),
  indexed_at  REAL,
  UNIQUE(root_id, rel_path)
);

-- ★★ 核心性能索引：与 ORDER BY taken_at DESC, id DESC 逐字一致 ★★
CREATE INDEX IF NOT EXISTS idx_media_timeline
  ON media(taken_at DESC, id DESC);

-- 去重/秒传：sha256 命中时快速定位（部分索引，索引时大部分行为 NULL）
CREATE INDEX IF NOT EXISTS idx_media_sha
  ON media(sha256) WHERE sha256 IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_media_camera
  ON media(camera, taken_at DESC) WHERE camera IS NOT NULL;

-- 待生成缩略图队列。必须是三列全索引：实测部分索引在 pending 占比
-- 10% / 1% / 0.1% 时都不被采用，仍是 SCAN media。
CREATE INDEX IF NOT EXISTS idx_media_thumbs
  ON media(thumb_state, missing, id);

-- 按天分组。⚠️ 表达式索引要求查询里的表达式**逐字节一致**：
-- 这里用 UTC 天；若要按本地天分组必须另建一个把偏移量写死的索引，
-- 两者不能互相替代（docs/05 §3.5.4 的"时区陷阱"）。
CREATE INDEX IF NOT EXISTS idx_media_taken_day
  ON media(date(taken_at, 'unixepoch'));

-- ============================================================
-- media_fts：FTS5 全文索引（外部内容表模式）
-- ============================================================
CREATE VIRTUAL TABLE IF NOT EXISTS media_fts USING fts5(
  file_name,
  rel_path,
  camera,
  content='media',
  content_rowid='id',
  tokenize="unicode61 remove_diacritics 2 tokenchars '_-.'"
);

CREATE TRIGGER IF NOT EXISTS media_fts_ai AFTER INSERT ON media BEGIN
  INSERT INTO media_fts(rowid, file_name, rel_path, camera)
  VALUES (new.id, new.file_name, new.rel_path, new.camera);
END;

CREATE TRIGGER IF NOT EXISTS media_fts_ad AFTER DELETE ON media BEGIN
  INSERT INTO media_fts(media_fts, rowid, file_name, rel_path, camera)
  VALUES ('delete', old.id, old.file_name, old.rel_path, old.camera);
END;

CREATE TRIGGER IF NOT EXISTS media_fts_au AFTER UPDATE ON media BEGIN
  INSERT INTO media_fts(media_fts, rowid, file_name, rel_path, camera)
  VALUES ('delete', old.id, old.file_name, old.rel_path, old.camera);
  INSERT INTO media_fts(rowid, file_name, rel_path, camera)
  VALUES (new.id, new.file_name, new.rel_path, new.camera);
END;

-- ============================================================
-- upload_sessions：分片上传会话
-- ============================================================
CREATE TABLE IF NOT EXISTS upload_sessions (
  id            TEXT    PRIMARY KEY,             -- uuid4 hex，32 位
  root_id       TEXT    NOT NULL,
  target_rel    TEXT    NOT NULL,                -- 临时区相对路径
  final_rel     TEXT,                            -- ★ 归档后的相对路径（complete 时填）
  temp_path     TEXT    NOT NULL,                -- 临时文件的绝对路径
  total_size    INTEGER NOT NULL CHECK (total_size > 0),
  received      INTEGER NOT NULL DEFAULT 0 CHECK (received >= 0),
  chunk_size    INTEGER NOT NULL,                -- 服务端权威分片大小
  sha256        TEXT,                            -- 客户端声明的哈希
  declared_name TEXT    NOT NULL,                -- 原始文件名
  taken_at      REAL,                            -- 客户端声明的拍摄时间（参考）
  created_at    REAL    NOT NULL,
  expires_at    REAL    NOT NULL,
  status        TEXT    NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','completed','aborted','expired')),
  media_id      INTEGER,                         -- complete 后的 media.id
  dedup         INTEGER CHECK (dedup IS NULL OR dedup IN (0,1)),
  completed_at  REAL,
  -- ★ 会话归属的客户端（devices.id = client_id）。见 ISSUE-034 ②。
  --
  -- 这一列不是"锦上添花的元数据"，而是会话复用的**身份**部分：
  -- 复用条件原来只有 root_id + 文件名 + 总大小 + sha256，而两台手机
  -- 传同名同大小文件是完全正常的事（连拍导出、微信同名图）。少了
  -- client_id，第二台手机的 init 会拿到第一台的 upload_id，继续往
  -- 别人的半成品临时文件里写 —— 结果是**静默的数据损坏**：两边都
  -- 以为成功了，归档出来是一个字节都不对的混合文件。
  --
  -- 允许 NULL 是给升级前遗留的会话用的（它们本来就没有归属信息，
  -- 只能按"谁都不可复用"处理，见 uploads.create_session）。
  client_id     TEXT,
  CHECK (received <= total_size),
  CHECK (length(id) = 32)
);

CREATE INDEX IF NOT EXISTS idx_uploads_status  ON upload_sessions(status, expires_at);
CREATE INDEX IF NOT EXISTS idx_uploads_created ON upload_sessions(created_at);

-- ============================================================
-- settings：运行时可变状态（键值）
-- ============================================================
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- ============================================================
-- audit_log：写操作审计（主记录是 data/audit.log 文件，本表是索引）
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
  id          INTEGER PRIMARY KEY,
  ts          REAL NOT NULL,
  device_name TEXT,
  client_id   TEXT,
  action      TEXT NOT NULL
                CHECK (action IN ('upload','delete','move','rename','rescan',
                                  'config','pair','revoke','upload_reject')),
  rel_path    TEXT,
  detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_path   ON audit_log(rel_path, ts DESC);

-- ============================================================
-- devices：本机已配对的手机（一期为局域网配对，二期用于吊销缓存）
-- ============================================================
CREATE TABLE IF NOT EXISTS devices (
  id         TEXT PRIMARY KEY,                   -- = client_id
  name       TEXT,
  token_hash TEXT,
  last_seen  REAL,
  created_at REAL,
  revoked    INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0,1)),
  -- token 的绝对过期时刻（AC-5.10）。NULL 表示"永不过期" ——
  -- 老库升级上来的行就是 NULL，强行给它们安一个过期时间会让所有
  -- 已配对的手机在某天突然集体掉线，那是比"永不发过期"更糟的故障。
  expires_at REAL,
  -- ★ 设备角色：owner 可以吊销任意设备，viewer 只能看到（脱敏后的）
  -- 设备列表、只能吊销自己。见 ISSUE-034 ③。
  --
  -- 为什么需要它：`/api/devices` 原来对**任何**已配对设备开放，
  -- 包括 DELETE。于是"捡到/泄露一部手机"就等于拿到了全家的设备
  -- 开关 —— 攻击者可以一键把桌上电脑、其他人的手机全部踢下线，
  -- 而重新配对必须有人站在电脑前点配对码。这是不可逆的破坏，
  -- 比"看到设备列表"严重一个量级，所以必须区分权限。
  --
  -- 默认 viewer（最小权限）：新列在 `CREATE TABLE IF NOT EXISTS` 里
  -- 只对新库生效，老库由 _migrate() 补列 + 回填。
  role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('owner','viewer'))
);

-- ⚠️ 不能在这里建 idx_devices_expires。老库的 devices 表还没有
-- expires_at 列，而 executescript(SCHEMA) 跑在 _migrate() **之前** ——
-- CREATE INDEX 会抛 "no such column: expires_at"，把启动整个打断。
-- 索引改在 _migrate() 里补完列之后建。

-- ============================================================
-- pair_codes：局域网配对码（一期）
--
-- 云端方案里这张表在云上；一期不走云，电脑端自己就是签发方，
-- 因此码也必须存在电脑本地。
--
-- ⚠️ 只存 **code_hash** 而不是明文码：这台电脑的数据库会被
-- 备份到网盘、被同步工具复制、被用户拷走排查问题。明文码在手，
-- 任何人都能在 5 分钟有效期内换到一枚 90 天有效的 client_token。
-- 6 位数字的熵很低（100 万种），但配合"5 分钟过期 + 错 5 次锁 10 分钟"
-- 且只能从局域网发起，暴力枚举在时效内不可行。
-- ============================================================
CREATE TABLE IF NOT EXISTS pair_codes (
  code_hash   TEXT PRIMARY KEY,
  device_id   TEXT NOT NULL,
  device_name TEXT,
  created_at  REAL NOT NULL,
  expires_at  REAL NOT NULL,
  tries       INTEGER NOT NULL DEFAULT 0,
  locked_until REAL,
  used_at     REAL
);

CREATE INDEX IF NOT EXISTS idx_pair_codes_expires ON pair_codes(expires_at);
CREATE INDEX IF NOT EXISTS idx_pair_codes_device  ON pair_codes(device_id, used_at);

-- ============================================================
-- albums / tags：一期建表不暴露接口，二期启用
-- ============================================================
CREATE TABLE IF NOT EXISTS albums (
  id             INTEGER PRIMARY KEY,
  name           TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 64),
  cover_media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
  created_at     REAL NOT NULL,
  sort           INTEGER
);

CREATE TABLE IF NOT EXISTS album_items (
  album_id INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
  media_id INTEGER NOT NULL REFERENCES media(id)  ON DELETE CASCADE,
  sort     INTEGER,
  added_at REAL,
  PRIMARY KEY (album_id, media_id)
);

CREATE INDEX IF NOT EXISTS idx_album_items_media ON album_items(media_id);

CREATE TABLE IF NOT EXISTS tags (
  id    INTEGER PRIMARY KEY,
  name  TEXT NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 32),
  color TEXT
);

CREATE TABLE IF NOT EXISTS media_tags (
  media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
  tag_id   INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
  PRIMARY KEY (media_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_media_tags_tag ON media_tags(tag_id);
"""


#: media 表的 `CREATE TABLE` 文本，从 `SCHEMA` 里**就地取出**。
#:
#: 为什么取出而不是再写一份：老库升级时要重建 media 表（SQLite 改不了
#: CHECK 约束），重建用的 DDL 与新建库用的 DDL 必须完全一致。手写两份的
#: 必然结果是"新装的库能存视频、升级上来的库插入视频报 CHECK 失败" ——
#: 而且只在用户升级后扫描到第一个视频时才暴露。
_MEDIA_DDL_RE: Final[re.Pattern[str]] = re.compile(
    r"CREATE TABLE IF NOT EXISTS media \(.*?\n\);", re.DOTALL
)


def media_ddl(table: str = "media") -> str:
    """`SCHEMA` 里 media 表的建表语句（可改表名，供迁移重建用）。"""
    m = _MEDIA_DDL_RE.search(SCHEMA)
    if m is None:  # pragma: no cover —— 只会在有人删掉 SCHEMA 里那张表时发生
        raise RuntimeError("SCHEMA 里找不到 media 表定义")
    return m.group(0).replace(
        "CREATE TABLE IF NOT EXISTS media", f"CREATE TABLE {table}", 1
    )


#: 迁移重建 media 时要按名搬过去的列。顺序无关，但**必须是老库真实存在的**
#: 列，否则 `INSERT ... SELECT` 会报 `no such column`。视频四列（duration
#: /vcodec/acodec/container）故意不在内 —— 它们就是这次新加的，老库没有。
_MEDIA_COPY_COLUMNS: Final[tuple[str, ...]] = (
    "id", "root_id", "rel_path", "file_name", "ext", "kind", "size", "mtime",
    "sha256", "taken_at", "taken_src", "width", "height", "camera", "lens",
    "iso", "fnum", "exposure", "focal", "gps_lat", "gps_lon", "orientation",
    "thumb_state", "thumb_etag", "missing", "indexed_at",
)


# --------------------------------------------------------------------------
# 连接管理
# --------------------------------------------------------------------------


class Database:
    """线程本地连接的 SQLite 封装。

    为什么用线程本地而不是单连接 + 锁：WAL 模式下**读不阻塞写**，
    多个读连接可以真正并行。用一把大锁把并发全序列化，
    会让"索引器在写 + 手机在翻页"互相卡住。

    为什么不用连接池库：标准库 sqlite3 的 connect 本身很便宜（微秒级），
    而连接池引入的借还逻辑和失效检测是净负担。
    """

    __slots__ = ("path", "_local", "_write_lock", "_initialized", "_conns")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._initialized = False
        # 所有线程建过的连接。**必须记着**：SQLite 只在"最后一个连接关闭"
        # 时才 checkpoint 并删除 -wal / -shm。只关当前线程那条的话，
        # 缩略图 worker、watchdog 线程留下的连接会一直挂着，
        # 进程退出后 data/ 里就留着 photobridge.db-wal（实测 1.5 MB）。
        self._conns: list[sqlite3.Connection] = []

    # --- 连接 -------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """取本线程的连接，必要时新建并设好 PRAGMA。"""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            return conn

        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.path),
            timeout=30.0,                 # 与 busy_timeout 一致
            isolation_level=None,         # 自己管事务，不用 sqlite3 的隐式 BEGIN
            check_same_thread=False,      # 配合线程本地使用
        )
        conn.row_factory = sqlite3.Row
        self._apply_pragmas(conn)
        self._local.conn = conn
        with self._write_lock:
            self._conns.append(conn)
        return conn

    def _apply_pragmas(self, conn: sqlite3.Connection) -> None:
        """每条新连接都必须跑一遍。

        busy_timeout 是连接级且默认 0 —— 漏设的话并发写入会立刻
        SQLITE_BUSY 而不是等待，这正是 docs/05 §10.2 ⑳ 指出的遗留 bug。
        """
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")        # 持久化设置，但重复执行无害
        cur.execute("PRAGMA synchronous=NORMAL")      # WAL 下 NORMAL 已能防断电丢库
        cur.execute("PRAGMA busy_timeout=30000")      # ★ 连接级，必须每次设
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA temp_store=MEMORY")
        cur.execute("PRAGMA cache_size=-16000")       # 约 16 MB 页缓存
        cur.execute("PRAGMA mmap_size=268435456")     # 256 MB 内存映射
        cur.close()

    def close(self) -> None:
        """关闭**本线程**的连接（可重复调用）。"""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
            with self._write_lock:
                try:
                    self._conns.remove(conn)
                except ValueError:
                    pass

    def close_all(self) -> int:
        """关闭所有线程建过的连接，返回关闭条数。**退出时调用。**

        ## 为什么必须有个 close_all

        `close()` 只关当前线程那条，这是对的 —— 线程本地连接本来就
        该由各自线程管。但进程退出时没有人替缩略图 worker、watchdog
        线程收尾，它们的连接会跟着解释器一起被回收，而 SQLite 的
        "最后连接关闭 → checkpoint → 删 -wal/-shm" 这一步**不会执行**。

        实测症状：托盘"退出"后 `data/` 里留着
        `photobridge.db-wal`（1.5 MB 量级）与 `photobridge.db-shm`。
        下次启动 SQLite 会把 WAL 合回去（数据不丢），但：

        * A-11 验收项（"退出后无 WAL/SHM 残留"）永远不可能通过；
        * 用户在"退出后能不能直接拷走 data/ 备份"时会被 -wal 误导；
        * 单文件数据库看起来像多文件，与文档承诺不符。

        关完再 `PRAGMA wal_checkpoint(TRUNCATE)` 一次，把 WAL 内容
        落回主库并 truncate 到 0 字节（此时已经没别的连接持有 WAL）。
        """
        with self._write_lock:
            conns, self._conns = self._conns, []
        closed = 0
        for conn in conns:
            try:
                conn.close()
                closed += 1
            except Exception:      # noqa: BLE001
                pass
        # 本线程那条如果没被 close() 过，也一并清掉引用
        self._local.conn = None
        try:
            # 用一个临时连接做一次 TRUNCATE checkpoint（此时已无其他连接）
            tmp = sqlite3.connect(str(self.path), timeout=5.0,
                                  isolation_level=None)
            try:
                tmp.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                tmp.close()
        except Exception:          # noqa: BLE001
            # 库被别的进程占着、文件被删了……都不该让退出失败
            pass
        return closed

    # --- 初始化 -----------------------------------------------------------

    def init(self) -> None:
        """建表 + 迁移 + 记录版本。幂等，启动时调用一次。"""
        with self._write_lock:
            conn = self.connect()
            conn.executescript(SCHEMA)
            self._migrate(conn)
            row = conn.execute(
                "SELECT version FROM schema_version WHERE version = ?", (SCHEMA_VERSION,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_version(version, applied_at, name, checksum) "
                    "VALUES (?, ?, ?, NULL)",
                    (SCHEMA_VERSION, time.time(), SCHEMA_NAME),
                )
            self._initialized = True

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """就地迁移。

        ⚠️ 这里处理的都是 `CREATE TABLE IF NOT EXISTS` **改不动**的东西。
        表已存在时那条语句是空操作，所以后来新增的 CHECK 约束
        （例如 audit_log 新增 'pair'/'revoke' 动作）对老库完全不生效 ——
        用户升级后第一次配对就会 `CHECK constraint failed`，
        而且是发生在"配对成功、写审计"那一刻，token 已经发出去了。

        SQLite 不支持修改 CHECK，只能重建表。这张表只是索引
        （主记录是 data/audit.log），所以搬数据没有丢失风险。
        """
        # --- devices：补 expires_at 列（AC-5.10）---
        #
        # `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，所以新增的
        # 列在老库上永远不会出现。不补的话 `_lookup_device` 的 SELECT
        # 会抛 `no such column: expires_at` —— 而那是每个已配对用户在
        # 升级后发出的第一个请求，表现为"升级后手机全部失联"。
        #
        # 补出来的列值全是 NULL。NULL 在本项目里明确表示"永不过期"，
        # 这正是升级用户的既有语义：不该因为软件更新就让他们掉线。
        # 新配对的设备一律写入具体时刻。
        cols = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
        if cols and "expires_at" not in cols:
            log.info("迁移 devices：新增 expires_at 列")
            conn.execute("ALTER TABLE devices ADD COLUMN expires_at REAL")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_devices_expires ON devices(expires_at)")

        # --- devices：补 role 列（ISSUE-034 ③）---
        #
        # `ADD COLUMN ... NOT NULL DEFAULT 'viewer'` 是 SQLite 允许的
        # （带非 NULL 默认值即可），老库原有的设备行会被填成 viewer。
        # 但**只填 viewer 是不够的**：家里可能已经配对了好几台设备，
        # 升级后没有任何一台是 owner，于是"吊销设备"这个功能对所有人
        # 都失效了 —— 包括那台站在电脑前、理应拥有权限的机器。
        # 所以补列后要把最早配对、且未被吊销的那台回填成 owner：
        # 它按定义就是"第一次在电脑前配对的那部手机"。
        if cols and "role" not in cols:
            log.info("迁移 devices：新增 role 列（最早配对的设备回填为 owner）")
            conn.execute(
                "ALTER TABLE devices ADD COLUMN role TEXT NOT NULL DEFAULT 'viewer'")
            conn.execute(
                "UPDATE devices SET role = 'owner' WHERE id = ("
                "  SELECT id FROM devices WHERE revoked = 0"
                "   ORDER BY created_at ASC, id ASC LIMIT 1)")

        # --- devices：补 allow_legacy_until 列（ISSUE-049）---
        #
        # 2026-10-26 之前，手机令牌由云端用 `sha256(agent_token)` 签 ——
        # 那个值就在云端库里，泄露即可伪造令牌。现在签发权回到电脑端
        # （`control.client_signing_secret()`），旧密钥**默认不再接受**。
        #
        # 默认 0 = 关闭。这不是苛刻而是必须：只要还接受旧密钥，"云端库泄露
        # 就能伪造令牌"这条边界就依然是敞开的。确实需要迁移老手机时，
        # 用 `python -m backend.pbctl allow-legacy 7` 开一个限时窗口，
        # 让老令牌在这段时间里继续能用，之后自动关回去。
        if cols and "allow_legacy_until" not in cols:
            log.info("迁移 devices：新增 allow_legacy_until 列（默认关闭旧密钥）")
            conn.execute(
                "ALTER TABLE devices ADD COLUMN allow_legacy_until "
                "REAL NOT NULL DEFAULT 0")

        # --- upload_sessions：补 client_id 列（ISSUE-034 ②）---
        #
        # 补出来是 NULL，语义是"归属未知"。这类历史会话不可被任何
        # 客户端复用（见 uploads.create_session）：既然搞不清它是谁的，
        # 就别把它交给别人 —— 宁可多花一次上传，也不能拼出一个混合文件。
        # 未完成的旧会话会自然过期（session_ttl_hours），不需要主动清理。
        up_cols = {r[1] for r in conn.execute("PRAGMA table_info(upload_sessions)")}
        if up_cols and "client_id" not in up_cols:
            log.info("迁移 upload_sessions：新增 client_id 列")
            conn.execute("ALTER TABLE upload_sessions ADD COLUMN client_id TEXT")

        # --- media：放开 kind='video'，补视频四列（ISSUE-055）---
        #
        # `kind` 原来是 `CHECK (kind IN ('image'))`。CHECK 改不了，只能
        # 重建表。这是本项目**唯一一次重建主表**，所以三条纪律写在这里：
        #
        # ① 重建用的 DDL 从 `SCHEMA` 里取（`media_ddl()`），不另写一份；
        # ② 只按名搬已有列，新列留 NULL（NULL = 未知，代码里当"没探测过"）；
        # ③ 重建后重新执行一遍 `SCHEMA`（索引与触发器随 DROP TABLE 消失），
        #    再 `rebuild` 一次 FTS —— media_fts 是外部内容表，
        #    内容行没变但索引条目被扰动过。
        #
        # 失败后果提醒：不迁移的话，**新库**一切正常，**升级的库**在扫描到
        # 第一个视频时插入失败（CHECK constraint failed），表现为"视频
        # 一个都不显示"。
        mrow = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='media'"
        ).fetchone()
        if mrow and mrow[0] and "'video'" not in mrow[0]:
            log.info("迁移 media：放开 kind='video' 并补视频四列")
            cols = ", ".join(_MEDIA_COPY_COLUMNS)
            # ⚠️ `media_tags.media_id` / `albums.cover_media_id` 上有
            # `REFERENCES media(id)`。连接是 `foreign_keys=ON`，直接
            # DROP 父表会把**标签行按 CASCADE 一起删掉**（用户丢的是标签，
            # 而且没有任何报错）。所以重建期间必须临时关掉。
            conn.execute("PRAGMA foreign_keys=OFF")
            try:
                conn.executescript(
                    "BEGIN;"
                    + media_ddl("media_new")
                    + f"INSERT INTO media_new({cols}) SELECT {cols} FROM media;"
                    + "DROP TABLE media;"
                    + "ALTER TABLE media_new RENAME TO media;"
                    + "COMMIT;"
                )
            finally:
                conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO media_fts(media_fts) VALUES('rebuild')")

        # --- audit_log：补齐 pair/revoke/upload_reject 动作 ---
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'"
        ).fetchone()
        if row and row[0] and "'pair'" not in row[0]:
            log.info("迁移 audit_log：新增 pair/revoke 动作")
            conn.executescript(
                "ALTER TABLE audit_log RENAME TO audit_log_old;"
                "CREATE TABLE audit_log ("
                "  id          INTEGER PRIMARY KEY,"
                "  ts          REAL NOT NULL,"
                "  device_name TEXT,"
                "  client_id   TEXT,"
                "  action      TEXT NOT NULL"
                "                CHECK (action IN ('upload','delete','move','rename',"
                "                                  'rescan','config','pair','revoke',"
                "                                  'upload_reject')),"
                "  rel_path    TEXT,"
                "  detail      TEXT"
                ");"
                "INSERT INTO audit_log(id, ts, device_name, client_id, action,"
                "                      rel_path, detail)"
                "  SELECT id, ts, device_name, client_id, action, rel_path, detail"
                "    FROM audit_log_old;"
                "DROP TABLE audit_log_old;"
                "CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_log(ts DESC);"
                "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, ts DESC);"
                "CREATE INDEX IF NOT EXISTS idx_audit_path   ON audit_log(rel_path, ts DESC);"
            )

    @property
    def initialized(self) -> bool:
        return self._initialized

    def schema_version(self) -> int:
        row = self.query_one("SELECT MAX(version) AS v FROM schema_version")
        return int(row["v"]) if row and row["v"] is not None else 0

    # --- 查询 -------------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        cur = self.connect().execute(sql, params)
        try:
            return cur.fetchall()
        finally:
            cur.close()

    def query_one(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        cur = self.connect().execute(sql, params)
        try:
            return cur.fetchone()
        finally:
            cur.close()

    def scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = (), default: Any = None) -> Any:
        row = self.query_one(sql, params)
        if row is None:
            return default
        v = row[0]
        return default if v is None else v

    def iterate(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = (), *, arraysize: int = 256
    ) -> Iterator[sqlite3.Row]:
        """流式读取，用于全表扫描（索引器）。不会把结果全读进内存。"""
        cur = self.connect().execute(sql, params)
        try:
            while True:
                rows = cur.fetchmany(arraysize)
                if not rows:
                    return
                yield from rows
        finally:
            cur.close()

    # --- 写入 -------------------------------------------------------------

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        with self._write_lock:
            return self.connect().execute(sql, params)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        with self._write_lock:
            return self.connect().executemany(sql, seq)

    def execute_rowcount(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> int:
        return self.execute(sql, params).rowcount

    def insert_returning_id(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> int:
        cur = self.execute(sql, params)
        return int(cur.lastrowid or 0)

    # --- 事务 -------------------------------------------------------------

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """显式事务。

        `immediate=True` 用 BEGIN IMMEDIATE 立刻取写锁。WAL 下如果先读后写，
        SQLite 需要把读事务升级为写事务，此时若有另一个写者就会
        返回 SQLITE_BUSY 且**无法靠 busy_timeout 重试**（升级失败是立即的）。
        索引器批量写入必须用 IMMEDIATE 避免这个陷阱。
        """
        conn = self.connect()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    # --- settings 便捷方法 ------------------------------------------------

    def set_setting(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, _encode_setting(value)),
        )

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return default if row is None else _decode_setting(row["value"])

    def all_settings(self) -> dict[str, Any]:
        return {r["key"]: _decode_setting(r["value"]) for r in self.query("SELECT key, value FROM settings")}

    # --- 统计 -------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        total = self.scalar("SELECT COUNT(*) FROM media WHERE missing = 0", default=0)
        pending = self.scalar(
            "SELECT COUNT(*) FROM media WHERE thumb_state = 'pending' AND missing = 0", default=0
        )
        failed = self.scalar(
            "SELECT COUNT(*) FROM media WHERE thumb_state = 'failed' AND missing = 0", default=0
        )
        missing = self.scalar("SELECT COUNT(*) FROM media WHERE missing = 1", default=0)
        bytes_total = self.scalar("SELECT COALESCE(SUM(size),0) FROM media WHERE missing = 0", default=0)
        oldest = self.scalar("SELECT MIN(taken_at) FROM media WHERE missing = 0")
        newest = self.scalar("SELECT MAX(taken_at) FROM media WHERE missing = 0")
        return {
            "media_total": int(total),
            "thumb_pending": int(pending),
            "thumb_failed": int(failed),
            "missing": int(missing),
            "bytes_total": int(bytes_total),
            "taken_at_min": oldest,
            "taken_at_max": newest,
            "db_bytes": self.db_size_bytes(),
        }

    def db_size_bytes(self) -> int:
        """数据库总占用（含 WAL 与 shm）。

        注意用 `Path(str(path) + "-wal")` 而不是 `with_suffix("-wal")`：
        `Path("photobridge.db").with_suffix("-wal")` 会得到 "photobridge-wal"
        （把 .db 当后缀换掉），恰好不对。docs/05 §10.3 ⑳ 指出的正是这个坑。
        """
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            try:
                total += p.stat().st_size
            except OSError:
                pass
        return total

    def vacuum(self) -> None:
        """整理碎片。会短暂持有排他锁，只应在托盘"维护"里手动触发。"""
        with self._write_lock:
            self.connect().execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.connect().execute("VACUUM")


# --------------------------------------------------------------------------
# settings 值的编解码
# --------------------------------------------------------------------------


def _encode_setting(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


def _decode_setting(raw: str) -> Any:
    import json

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw          # 兼容手工写入的裸字符串


# --------------------------------------------------------------------------
# 模块级单例
# --------------------------------------------------------------------------

_db: Database | None = None
_db_lock = threading.Lock()


def get_db(reload: bool = False) -> Database:
    """取全局数据库句柄（按 `data/config.json` 解析出的**真实**路径）。

    ⚠️ 生产代码用它；**测试与脚本绝不能用**。

    它按配置文件解析路径，而 `data/config.json` 在开发机上指向用户
    真实的照片库索引。开发期曾因此出过一次事故：测试夹具构造
    `Config(data_dir=tmp)` 后只把 `cfg` 传下去、没传 `db`，
    落到这里的 `get_db()` 拿到真实库，于是
    ① 合成测试行被写进真实库；② 测试库只有 3 个文件，扫描后
    `_mark_missing` 把真实库的 4915 行全标成"文件已丢失"，
    用户的时间线变成空白。

    所以新增了 `make_db(cfg)`：**永远按传入的 cfg 解析路径**，
    测试与脚本一律用它，从源头上不可能再落到真实库。
    """
    global _db
    if _db is None or reload:
        with _db_lock:
            if _db is None or reload:
                cfg = cfgmod.load()
                _db = Database(cfg.db_path)
    return _db


def make_db(cfg: Config) -> Database:
    """按给定配置造一个**独立**的数据库句柄（不碰全局单例）。

    与 `get_db()` 的区别只有一点，但这一点至关重要：路径来自传入的
    `cfg` 而不是磁盘上的 `data/config.json`。测试用
    `Config({...}, data_dir=tmp_path/"data")` 构造配置后调它，
    就绝无可能写到用户真实的照片库里。
    """
    return Database(cfg.db_path)


def init_db() -> Database:
    """创建/迁移数据库并返回句柄。启动时调用一次。"""
    db = get_db()
    try:
        db.init()
    except sqlite3.OperationalError as e:
        # E3002 的语义是"稍后重试"；启动期锁冲突通常意味着另一个实例在跑
        raise AppError(E3002, f"数据库初始化失败: {e}") from e
    return db


def close_db() -> None:
    global _db
    if _db is not None:
        _db.close()
        _db = None


__all__ = ["Database", "SCHEMA", "SCHEMA_NAME", "SCHEMA_VERSION", "close_db",
           "get_db", "init_db", "make_db"]
