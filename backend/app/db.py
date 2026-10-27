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

import hashlib
import inspect
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final

from . import config as cfgmod
from .errors import E3002, AppError

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# 建表脚本
# --------------------------------------------------------------------------

#: 基线迁移名。`schema_version` 的第一行用它，之后每版一条（docs/05 §7.1）。
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
  -- moov 是否在 mdat 之前（1=是，0=否，NULL=未知/没探过）。0 表示未做
  -- faststart：播放器得先读到文件尾才能起播，跨网络时表现为长时间转圈。
  faststart   INTEGER CHECK (faststart IS NULL OR faststart IN (0,1)),
  -- 上传者（ISSUE-063 G4）：游客只能看到自己上传的。主人自己上传的为空串，
  -- 因为"主人"不是一根可以跨设备比较的身份 —— 主人本来就该看到全部。
  uploader_client_id TEXT NOT NULL DEFAULT '',
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

-- 「按文件夹」浏览：按 (根, 相对路径) 定位一层目录。
-- 没有它时 `WHERE root_id = ? AND rel_path LIKE 'x/%' AND rel_path NOT LIKE
-- 'x/%/%'` 只能 SCAN media（5000 行时约 3ms，看着不慢，但目录分页是
-- 每翻一页扫一次、且随库增长线性变差）。有了它 EXPLAIN 走
-- `SEARCH media USING INDEX idx_media_root_dir (root_id=? AND rel_path>? AND rel_path<?)`。
-- ⚠️ 它**不能**替 timeline 索引：ORDER BY 仍是 taken_at，SQLite 会在这里
-- 取到候选行后做一次有界排序（开销正比于该目录条目数，与总库大小无关）。
CREATE INDEX IF NOT EXISTS idx_media_root_dir
  ON media(root_id, rel_path);

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
                                  'config','pair','revoke','upload_reject',
                                  'guest_invite','guest_enter','guest_upload',
                                  'guest_revoke','guest_denied')),
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
-- guest_invites / guest_sessions / guest_upload_log：游客访问（ISSUE-063）
--
-- 为什么三张表而不是往 devices 里塞 role='guest'：
--   `devices` 的语义是"我有权限的手机"，它会进「我的设备」列表、参与令牌撤销
--   与 last_seen 统计。游客是**别人**的手机，撤一次邀请不该出现在
--   "已配对设备"里，也不该继承那套"每台设备一枚长命令牌"的心智。
--
-- 为什么邀请与会话分开：
--   邀请（guest_invites）是**链接**，一次性、可撤销；会话（guest_sessions）是
--   **浏览器**，一个链接换一根，各自有 TTL。合并成一张表就没法表达
--   "链接已用掉但会话还在跑"（G11 解释 A）。
-- ============================================================
-- >>> guest-schema（由 guest_schema() 原样取出供迁移 9 复用，勿手工移除标记）
CREATE TABLE IF NOT EXISTS guest_invites (
  id              TEXT PRIMARY KEY,              -- 32 hex
  device_id       TEXT NOT NULL,                 -- 哪台电脑（绑定，不可换）
  token_hash      TEXT NOT NULL UNIQUE,          -- sha256(明文 token)，明文不落库
  label           TEXT NOT NULL DEFAULT '',      -- 主人给的备注，如"给老王"
  created_at      REAL NOT NULL,
  expires_at      REAL NOT NULL,                 -- 从**生成时刻**起算（G12）
  first_used_at   REAL,                          -- 首次兑换时间；非空即"已使用"
  revoked_at      REAL,
  guest_client_id TEXT,                          -- 首次兑换时绑定的会话
  created_via     TEXT NOT NULL DEFAULT 'lan'
                    CHECK (created_via IN ('lan','cloud'))
);

CREATE INDEX IF NOT EXISTS idx_guest_invites_expires ON guest_invites(expires_at);

CREATE TABLE IF NOT EXISTS guest_sessions (
  client_id  TEXT PRIMARY KEY,                   -- 32 hex，等于令牌的 sub
  token_hash TEXT NOT NULL UNIQUE,
  invite_id  TEXT NOT NULL REFERENCES guest_invites(id) ON DELETE CASCADE,
  device_id  TEXT NOT NULL,
  label      TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  expires_at REAL NOT NULL,
  last_seen  REAL,
  revoked_at REAL
);

CREATE INDEX IF NOT EXISTS idx_guest_sessions_invite
  ON guest_sessions(invite_id);

-- 游客上传台账：用于「每日字节配额」与「谁传了什么」的审计追溯。
-- media_id 置空是**故意**的：游客删不了文件，但主人可能把媒体记录清掉，
-- 台账要留下"某天某人传过多大一个文件"这件事本身。
CREATE TABLE IF NOT EXISTS guest_upload_log (
  id         INTEGER PRIMARY KEY,
  client_id  TEXT NOT NULL,
  root_id    TEXT NOT NULL,
  media_id   INTEGER REFERENCES media(id) ON DELETE SET NULL,
  rel_path   TEXT NOT NULL,
  size       INTEGER NOT NULL CHECK (size >= 0),
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_guest_upload_client
  ON guest_upload_log(client_id, created_at);
-- <<< guest-schema

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


#: 游客三表的建表语句，从 `SCHEMA` 里**就地取出**（与 `media_ddl()` 同理）。
#:
#: 为什么不在迁移里再写一份 DDL：新库走 `executescript(SCHEMA)`、老库走迁移，
#: 两条路必须是同一段文本。分开写就会漂移，而漂移只在"老库升级"时暴露。
_GUEST_SCHEMA_RE: Final[re.Pattern[str]] = re.compile(
    r"-- >>> guest-schema.*?\n(.*?)-- <<< guest-schema", re.DOTALL
)


def guest_schema() -> str:
    """`SCHEMA` 里游客三张表的建表语句（供迁移 9 复用）。"""
    m = _GUEST_SCHEMA_RE.search(SCHEMA)
    if m is None:  # pragma: no cover —— 只会在有人删掉标记时发生
        raise RuntimeError("SCHEMA 里找不到 guest-schema 标记")
    return m.group(1)


#: audit_log 的建表语句，同样就地取出：迁移 11 重建表时 CHECK 必须与新库一致，
#: 否则会出现"新库能记 guest_upload、升级库记不进去"的静默丢审计。
_AUDIT_DDL_RE: Final[re.Pattern[str]] = re.compile(
    r"CREATE TABLE IF NOT EXISTS audit_log \(.*?\n\);", re.DOTALL
)


def audit_ddl(table: str = "audit_log") -> str:
    """`SCHEMA` 里 audit_log 的建表语句（可改表名，供迁移重建用）。"""
    m = _AUDIT_DDL_RE.search(SCHEMA)
    if m is None:  # pragma: no cover
        raise RuntimeError("SCHEMA 里找不到 audit_log 表定义")
    return m.group(0).replace(
        "CREATE TABLE IF NOT EXISTS audit_log", f"CREATE TABLE {table}", 1
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
# 迁移账本
# --------------------------------------------------------------------------
#
# 口径与 docs/05 §7.1 一致：`schema_version` 表里**每一行**代表一次已应用的
# 迁移，`checksum` 是那次迁移**定义文本**的 sha256 前 16 位；改了就启动即报错。
#
# 为什么不铺 `migrations/*.sql`（05 §7.2 的旧写法）：本项目的迁移有几处只能由
# Python 表达 —— 重建 `media` 表要临时关 `foreign_keys`（否则 `media_tags` 会
# 被 CASCADE 连带删掉）、重建后还要重跑 `SCHEMA` 并 `rebuild` 一次 FTS。
# SQL 文件写不出"顺带把索引和全文索引一起修好"。所以迁移就地写在 db.py 里，
# 但**记账方式一字不差**：每版一条、带 checksum、顺序执行、版本号严格递增。


class MigrationDrift(RuntimeError):
    """已应用的迁移被改动过（checksum 对不上），拒绝启动。"""


@dataclass(frozen=True)
class Migration:
    """一次迁移：版本号 + 名字 + 定义摘要 + 执行体。

    `checksum` 是**构建期算好写进来的**常量，不是运行时算的：PyInstaller 打包后
    没有 `.py` 源文件，`inspect.getsource()` 会抛 `OSError: could not get source
    code`（`scripts/verify_exe.py` 实测抓到过）。所以摘要必须在还有源码的时候算，
    运行期只做"账本 vs 常量"的比对。
    """

    version: int
    name: str
    checksum: str
    apply: Callable[[sqlite3.Connection], None]


def _migration_checksum(fn: Callable[[sqlite3.Connection], None]) -> str:
    """迁移定义文本的 sha256 前 16 位（**仅开发期使用**，冻结产物里没有源码）。

    取**源码**而不是取"执行过的 SQL"：同一个迁移在新库上会走到建列分支、
    在老库上走幂等分支，取执行内容会让同一份定义算出不同 checksum。

    `test_migration_ledger.py` 用它来校验 `MIGRATIONS` 里写死的 `checksum`
    常量有没有跟上函数体；运行期不再调用它。
    """
    src = inspect.getsource(fn)
    norm = "\n".join(line.rstrip() for line in src.splitlines() if line.strip())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _m_001_init_photobridge(conn: sqlite3.Connection) -> None:  # noqa: ARG001
    """基线：建表脚本就是 `SCHEMA`，`init()` 已经 `executescript` 过了。

    这里只留一个空实现，作用是**在账本里占住 version 1**，让"从哪个版本起"
    有据可查。早期版本的库里这一行已经存在（checksum 为 NULL），会被补记。
    """


def _m_002_devices_expires_at(conn: sqlite3.Connection) -> None:
    """devices 补 expires_at 列（AC-5.10）。

    `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，所以新增的列在老库上
    永远不会出现。不补的话 `_lookup_device` 的 SELECT 会抛
    `no such column: expires_at` —— 而那是每个已配对用户在升级后发出的第一个
    请求，表现为"升级后手机全部失联"。

    补出来的列值全是 NULL。NULL 在本项目里明确表示"永不过期"，这正是升级用户
    的既有语义：不该因为软件更新就让他们掉线。新配对的设备一律写入具体时刻。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
    if cols and "expires_at" not in cols:
        log.info("迁移 devices：新增 expires_at 列")
        conn.execute("ALTER TABLE devices ADD COLUMN expires_at REAL")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_devices_expires ON devices(expires_at)")


def _m_003_devices_role_owner(conn: sqlite3.Connection) -> None:
    """devices 补 role 列（ISSUE-034 ③），并把最早配对的设备回填为 owner。

    `ADD COLUMN ... NOT NULL DEFAULT 'viewer'` 是 SQLite 允许的（带非 NULL
    默认值即可），老库原有的设备行会被填成 viewer。但**只填 viewer 是不够的**：
    家里可能已经配对了好几台设备，升级后没有任何一台是 owner，于是"吊销设备"
    这个功能对所有人都失效了 —— 包括那台站在电脑前、理应拥有权限的机器。
    所以补列后要把最早配对、且未被吊销的那台回填成 owner：它按定义就是
    "第一次在电脑前配对的那部手机"。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
    if cols and "role" not in cols:
        log.info("迁移 devices：新增 role 列（最早配对的设备回填为 owner）")
        conn.execute(
            "ALTER TABLE devices ADD COLUMN role TEXT NOT NULL DEFAULT 'viewer'")
        conn.execute(
            "UPDATE devices SET role = 'owner' WHERE id = ("
            "  SELECT id FROM devices WHERE revoked = 0"
            "   ORDER BY created_at ASC, id ASC LIMIT 1)")


def _m_004_devices_allow_legacy_until(conn: sqlite3.Connection) -> None:
    """devices 补 allow_legacy_until 列（ISSUE-049）。

    2026-10-26 之前，手机令牌由云端用 `sha256(agent_token)` 签 —— 那个值就在
    云端库里，泄露即可伪造令牌。现在签发权回到电脑端
    （`control.client_signing_secret()`），旧密钥**默认不再接受**。

    默认 0 = 关闭。这不是苛刻而是必须：只要还接受旧密钥，"云端库泄露就能伪造
    令牌"这条边界就依然是敞开的。确实需要迁移老手机时，用
    `python -m backend.pbctl allow-legacy 7` 开一个限时窗口。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(devices)")}
    if cols and "allow_legacy_until" not in cols:
        log.info("迁移 devices：新增 allow_legacy_until 列（默认关闭旧密钥）")
        conn.execute(
            "ALTER TABLE devices ADD COLUMN allow_legacy_until "
            "REAL NOT NULL DEFAULT 0")


def _m_005_upload_sessions_client_id(conn: sqlite3.Connection) -> None:
    """upload_sessions 补 client_id 列（ISSUE-034 ②）。

    补出来是 NULL，语义是"归属未知"。这类历史会话不可被任何客户端复用
    （见 uploads.create_session）：既然搞不清它是谁的，就别把它交给别人 ——
    宁可多花一次上传，也不能拼出一个混合文件。未完成的旧会话会自然过期
    （session_ttl_hours），不需要主动清理。
    """
    up_cols = {r[1] for r in conn.execute("PRAGMA table_info(upload_sessions)")}
    if up_cols and "client_id" not in up_cols:
        log.info("迁移 upload_sessions：新增 client_id 列")
        conn.execute("ALTER TABLE upload_sessions ADD COLUMN client_id TEXT")


def _m_006_media_kind_video(conn: sqlite3.Connection) -> None:
    """media 放开 `kind='video'`，补视频四列（ISSUE-055）。

    `kind` 原来是 `CHECK (kind IN ('image'))`。CHECK 改不了，只能重建表。
    这是本项目**唯一一次重建主表**，所以三条纪律写在这里：
    ① 重建用的 DDL 从 `SCHEMA` 里取（`media_ddl()`），不另写一份；
    ② 只按名搬已有列，新列留 NULL（NULL = 未知，代码里当"没探测过"）；
    ③ 重建后重新执行一遍 `SCHEMA`（索引与触发器随 DROP TABLE 消失），
       再 `rebuild` 一次 FTS —— media_fts 是外部内容表，内容行没变但索引
       条目被扰动过。

    失败后果提醒：不迁移的话，**新库**一切正常，**升级的库**在扫描到第一个
    视频时插入失败（CHECK constraint failed），表现为"视频一个都不显示"。
    """
    mrow = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='media'"
    ).fetchone()
    if not (mrow and mrow[0] and "'video'" not in mrow[0]):
        return
    log.info("迁移 media：放开 kind='video' 并补视频四列")
    cols = ", ".join(_MEDIA_COPY_COLUMNS)
    # ⚠️ `media_tags.media_id` / `albums.cover_media_id` 上有
    # `REFERENCES media(id)`。连接是 `foreign_keys=ON`，直接 DROP 父表会把
    # **标签行按 CASCADE 一起删掉**（用户丢的是标签，而且没有任何报错）。
    # 所以重建期间必须临时关掉。
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


def _m_007_media_faststart(conn: sqlite3.Connection) -> None:
    """media 补 faststart 列（V0 诊断：moov 在不在 mdat 之前）。

    为什么单独一列而不是塞进 detail JSON：播放页要按它决定"要不要提示用户
    会等很久"，查询时不能为此再打开文件。
    """
    mcols = {r[1] for r in conn.execute("PRAGMA table_info(media)")}
    if mcols and "faststart" not in mcols:
        log.info("迁移 media：新增 faststart 列")
        conn.execute("ALTER TABLE media ADD COLUMN faststart INTEGER")


def _m_008_audit_log_actions(conn: sqlite3.Connection) -> None:
    """audit_log 补齐 pair/revoke/upload_reject 动作（CHECK 只能重建）。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'"
    ).fetchone()
    if not (row and row[0] and "'pair'" not in row[0]):
        return
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


def _m_009_guest_tables(conn: sqlite3.Connection) -> None:
    """新建游客三表（ISSUE-063）。新库由 `SCHEMA` 建，老库在这里补。"""
    conn.executescript(guest_schema())


def _m_010_media_uploader_client_id(conn: sqlite3.Connection) -> None:
    """media 增 `uploader_client_id`（G4：游客只看自己上传的）+ 过滤索引。

    索引必须建在**补完列之后**：`executescript(SCHEMA)` 跑在迁移之前，把索引
    写进 `SCHEMA` 会让老库启动时报 `no such column`（`idx_devices_expires`
    就是这么踩过的，见 `SCHEMA` 里那段注释）。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(media)")}
    if "uploader_client_id" not in cols:
        log.info("迁移 media：新增 uploader_client_id 列")
        conn.execute(
            "ALTER TABLE media ADD COLUMN uploader_client_id TEXT NOT NULL"
            " DEFAULT ''"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_media_uploader"
        "  ON media(root_id, uploader_client_id, taken_at DESC, id DESC)"
    )


def _m_011_audit_log_guest_actions(conn: sqlite3.Connection) -> None:
    """audit_log 补齐 guest_* 动作（CHECK 约束只能重建表）。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'"
    ).fetchone()
    if not (row and row[0] and "'guest_invite'" not in row[0]):
        return
    log.info("迁移 audit_log：新增 guest_* 动作")
    ddl = audit_ddl("audit_log").rstrip().rstrip(";")
    conn.executescript(
        "ALTER TABLE audit_log RENAME TO audit_log_old;"
        + ddl
        + ";"
        "INSERT INTO audit_log(id, ts, device_name, client_id, action,"
        "                      rel_path, detail)"
        "  SELECT id, ts, device_name, client_id, action, rel_path, detail"
        "    FROM audit_log_old;"
        "DROP TABLE audit_log_old;"
        "CREATE INDEX IF NOT EXISTS idx_audit_ts     ON audit_log(ts DESC);"
        "CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, ts DESC);"
        "CREATE INDEX IF NOT EXISTS idx_audit_path   ON audit_log(rel_path, ts DESC);"
    )


#: 全部迁移，**按版本号严格递增**。新增迁移只能往后追加，已发布的迁移禁止修改
#: （checksum 会拦住）。
MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(1, SCHEMA_NAME, "8f0f1582f8a1a059", _m_001_init_photobridge),
    Migration(2, "002_devices_expires_at", "a479c4622c4dcf5d",
              _m_002_devices_expires_at),
    Migration(3, "003_devices_role_owner", "31f1096acc1ced12",
              _m_003_devices_role_owner),
    Migration(4, "004_devices_allow_legacy_until", "3a203f55017f4c74",
              _m_004_devices_allow_legacy_until),
    Migration(5, "005_upload_sessions_client_id", "e070870d954ac8ff",
              _m_005_upload_sessions_client_id),
    Migration(6, "006_media_kind_video", "bb3dd43e2feec9c3",
              _m_006_media_kind_video),
    Migration(7, "007_media_faststart", "17a3caceac967213",
              _m_007_media_faststart),
    Migration(8, "008_audit_log_actions", "fbcd4a3c839e07a1",
              _m_008_audit_log_actions),
    Migration(9, "009_guest_tables", "67f9ec25e570c954", _m_009_guest_tables),
    Migration(10, "010_media_uploader_client_id", "bfccd1785448bf2f",
              _m_010_media_uploader_client_id),
    Migration(11, "011_audit_log_guest_actions", "57e00ff588f9b20a",
              _m_011_audit_log_guest_actions),
)

#: 当前 schema 版本 = 账本里最后一版。`init()` 跑完后 `schema_version()` 应等于它。
SCHEMA_VERSION: Final[int] = MIGRATIONS[-1].version


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
        """建表 + 按账本执行迁移。幂等，启动时调用一次。"""
        with self._write_lock:
            conn = self.connect()
            conn.executescript(SCHEMA)
            self._migrate(conn)
            self._initialized = True

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """按账本执行未应用的迁移，并校验已应用迁移的定义没被改过。

        为什么要账本而不是"再跑一遍所有幂等语句"：后者能补列，但补不出
        **顺序与时间**（哪天真出的问题、从哪个版本升上来的都查不到），
        也发现不了"有人回头改了迁移定义"。这两条正是 docs/05 §7.1 承诺的。

        ⚠️ 这里**不能**调 `_migration_checksum(m.apply)`：那要读源码，而
        PyInstaller 打出来的 exe 没有 `.py` 文件（实测 `OSError: could not get
        source code`，一启动就崩）。摘要由 `MIGRATIONS` 里的常量提供。
        """
        applied = {
            int(r[0]): r[1]
            for r in conn.execute("SELECT version, checksum FROM schema_version")
        }
        for m in MIGRATIONS:
            want = m.checksum
            if m.version in applied:
                got = applied[m.version]
                if got is None:
                    # 老库只写了基线那一行、checksum 为 NULL（早期实现）。
                    # 补记定义摘要，不当作漂移 —— 否则所有存量用户升级即启动失败。
                    conn.execute(
                        "UPDATE schema_version SET checksum = ?, name = ? "
                        "WHERE version = ?", (want, m.name, m.version))
                    log.info("迁移记账补齐：%03d %s → %s", m.version, m.name, want)
                elif got != want:
                    raise MigrationDrift(
                        f"迁移 {m.version:03d} {m.name} 的定义已被修改"
                        f"（账本 {got}，实际 {want}）。已应用的迁移不可修改，"
                        "请新建一个版本号更大的迁移。")
                continue
            log.info("执行迁移 %03d %s", m.version, m.name)
            m.apply(conn)
            conn.execute(
                "INSERT INTO schema_version(version, applied_at, name, checksum) "
                "VALUES (?, ?, ?, ?)",
                (m.version, time.time(), m.name, want),
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


__all__ = ["MIGRATIONS", "Database", "Migration", "MigrationDrift", "SCHEMA",
           "SCHEMA_NAME", "SCHEMA_VERSION", "close_db", "get_db", "init_db",
           "make_db"]
