"""分片上传：会话管理、断点续传、去重、归档。

## 为什么必须分片

手机在 4G 上传一张 8 MB 照片，中途切 Wi-Fi、进电梯、锁屏都可能
断掉。整文件 PUT 断了就要从头再来，用户传 200 张照片会反复重传。
分片后每片独立成功，断点只丢当前这一片。

## 三个必须做对的点

1. **临时文件必须在库根下**（`<库根>/.photobridge_tmp/{id}.part`），
   不是 `data/uploads_tmp/`。`os.replace` 只有同卷才原子；库在 E 盘、
   data 在 H 盘时跨卷 replace 抛 `OSError [WinError 17]`，
   "原子落盘"不可能实现（docs/05 §10.1 ①）。

2. **秒传（去重）命中前必须确认目标文件真的还在磁盘上。**
   DB 里 `missing=0` 只说明"上次扫描时还在"。用户手工删了照片、
   索引器还没重扫时，sha256 依然能命中 —— 此时如果直接丢弃刚上传的
   字节并回答"已存在"，那张照片就**永久消失**了：旧文件被手工删了，
   新文件被丢弃了。这是本项目唯一可能造成永久数据丢失的路径
   （风险登记册 R-004），所以 dedup 之前一定 stat 一次。

3. **收到多少字节以磁盘为准，不以 DB 为准。** 崩溃可能发生在
   "写完文件、还没更新 DB"之间。此时 DB 的 received 比文件小，
   客户端按 DB 值续传会**覆盖掉已写入的部分**（seek 回去重写），
   虽然内容相同不算错，但更糟的反向情况（DB 比文件大）会让
   续传位置超出文件末尾，写出一个中间有空洞的文件 ——
   空洞在照片上表现为灰条，且校验和永远对不上。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Final

from . import archive, audit, sandbox
from .config import Config, RootConfig
from .db import Database
from .errors import (
    AppError,
    ArchiveError,
    BadRequest,
    Conflict,
    E1001,
    E1004,
    E1006,
    E1007,
    E1010,
    E1011,
    E1012,
    E1013,
    E1014,
    E1017,
    E3010,
    E4002,
    NotFound,
    SandboxError,
)

log = logging.getLogger(__name__)

#: 单个上传会话最多允许的未完成会话数。手机端并发 3 条已经能跑满
#: 家用上行；再多只会让每个都变慢，且电脑端要同时维持多个临时文件。
MAX_OPEN_SESSIONS: Final[int] = 3

#: 读文件算 sha256 的分块大小。
HASH_CHUNK: Final[int] = 1024 * 1024

#: 允许上传的扩展名由 config.ALLOWED_IMAGE_EXTS 决定，这里不重复定义。

#: 每会话一把锁，串行化"读 offset → 写文件 → 更新 DB"。
#: 不加锁时两个并发 PATCH（手机端超时重试与正常请求撞车）会读到
#: 同一个 offset，各自 seek 到同一位置写入，后写的覆盖先写的，
#: 最终文件比声明的短，而两边都返回 206 成功 —— 直到 complete
#: 才发现"文件未传完"，用户完全不知道哪一片出了问题。
#:
#: 用 threading.Lock 而不是 asyncio.Lock：写入路径全是同步文件
#: 操作，实际并发来自 uvicorn 的线程池与 executor，asyncio.Lock
#: 在这两个世界里都不生效。
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def session_lock(upload_id: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(upload_id)
        if lock is None:
            lock = threading.Lock()
            _locks[upload_id] = lock
        return lock


def _drop_lock(upload_id: str) -> None:
    with _locks_guard:
        _locks.pop(upload_id, None)


# --------------------------------------------------------------------------
# 会话读取
# --------------------------------------------------------------------------


def get_session(db: Database, upload_id: str) -> dict[str, Any]:
    """取会话，不存在/已过期/状态不对时抛对应错误。"""
    row = db.query_one("SELECT * FROM upload_sessions WHERE id = ?", (upload_id,))
    if row is None:
        raise NotFound("上传会话不存在或已清理")
    sess = dict(row)
    if sess["status"] == "expired" or sess["expires_at"] < time.time():
        if sess["status"] == "open":
            db.execute(
                "UPDATE upload_sessions SET status = 'expired' WHERE id = ?", (upload_id,)
            )
        raise AppError(E1012, "上传会话已过期，请重新开始")
    if sess["status"] in ("aborted",):
        raise AppError(E1012, "上传已取消")
    return sess


def root_of(cfg: Config, sess: dict[str, Any]) -> RootConfig:
    root = cfg.root_by_id(sess["root_id"])
    if root is None:
        raise AppError(E1012, "媒体根目录已被移除，上传无法继续")
    return root


def open_session_count(db: Database) -> int:
    row = db.query_one(
        "SELECT COUNT(*) AS n FROM upload_sessions WHERE status = 'open' AND expires_at > ?",
        (time.time(),),
    )
    return int(row["n"]) if row else 0


def purge_expired(db: Database, *, cfg: Config | None = None) -> int:
    """清理过期会话及其临时文件。

    只删文件不删行会让 `upload_sessions` 无限增长；只删行不删文件
    会让临时区在用户放弃的每张照片上留一个几 MB 的 .part，
    几个月后占掉几个 GB 而用户完全不知道。
    """
    now = time.time()
    rows = db.query(
        "SELECT id, root_id, temp_path FROM upload_sessions "
        "WHERE status = 'open' AND expires_at <= ?",
        (now,),
    )
    removed = 0
    for r in rows:
        _unlink_temp(r["temp_path"])
        db.execute("UPDATE upload_sessions SET status = 'expired' WHERE id = ?", (r["id"],))
        _drop_lock(r["id"])
        removed += 1
    # 已结束的会话只留 7 天：它们承载"这张照片是从哪台设备传的"
    # 追溯信息，但不需要永远保留。
    old = db.query(
        "SELECT id, temp_path FROM upload_sessions "
        "WHERE status != 'open' AND created_at < ?",
        (now - 7 * 86400,),
    )
    for r in old:
        _unlink_temp(r["temp_path"])
        db.execute("DELETE FROM upload_sessions WHERE id = ?", (r["id"],))
    if removed:
        log.info("清理过期上传会话 %d 个", removed)
    return removed


def _unlink_temp(temp_path: str | None) -> None:
    if not temp_path:
        return
    try:
        p = Path(temp_path)
        if p.is_file():
            p.unlink()
    except OSError:
        log.debug("清理临时文件失败：%s", temp_path, exc_info=True)


# --------------------------------------------------------------------------
# 去重
# --------------------------------------------------------------------------


def _dedup_on(cfg: Config) -> bool:
    """`upload.dedup` 开关。

    关掉时连"秒传"也一并关掉：只关 complete 阶段的去重会让 init 阶段
    仍然回答"已存在"，用户以为没传而实际上服务端什么都没做。
    """
    return bool(cfg.upload.get("dedup", True))


def find_duplicate(
    db: Database, cfg: Config, sha256: str | None, *, root_id: str | None = None
) -> dict[str, Any] | None:
    """按 sha256 找已有照片，**并确认文件真的还在磁盘上**。

    确认这一步是数据安全的关键：`missing=0` 只说明"上次扫描时还在"。
    用户手工删掉照片、索引器还没重扫时 sha256 依然命中，此时若直接
    丢弃刚上传的字节并回答"已存在"，那张照片就永久消失了 ——
    旧文件被手工删了，新文件被丢弃了。

    返回 None 表示"没有可复用的副本，照常收下"。
    """
    if not sha256:
        return None
    sql = (
        "SELECT id, root_id, rel_path, file_name, size, taken_at FROM media "
        "WHERE sha256 = ? AND missing = 0"
    )
    params: list[Any] = [sha256]
    if root_id:
        sql += " AND root_id = ?"
        params.append(root_id)
    sql += " ORDER BY id ASC LIMIT 4"      # 多取几条：第一条可能已不在磁盘上
    for row in db.query(sql, params):
        root = cfg.root_by_id(row["root_id"])
        if root is None:
            continue
        try:
            p = sandbox.resolve(root, row["rel_path"], "read", cfg=cfg)
            st = os.stat(p)
        except (SandboxError, OSError):
            log.warning("去重目标不可用，继续检查下一个：%s", row["rel_path"])
            continue
        if st.st_size != row["size"]:
            # 同名文件被换成别的内容：不能复用
            log.warning("去重目标大小不符，跳过：%s", row["rel_path"])
            continue
        return {
            "media_id": int(row["id"]),
            "root_id": row["root_id"],
            "rel_path": row["rel_path"],
            "file_name": row["file_name"],
            "size": int(row["size"]),
        }
    return None


# --------------------------------------------------------------------------
# 创建会话
# --------------------------------------------------------------------------


def check_extension(cfg: Config, name: str) -> str:
    """校验扩展名并返回规范形式（小写、带点）。"""
    ext = archive.normalize_ext(os.path.splitext(name)[1])
    if not ext or ext.lstrip(".") not in cfg.upload["allowed_exts"]:
        raise AppError(E1017, f"不支持的文件格式：{ext or '无扩展名'}")
    return ext


# --------------------------------------------------------------------------
# 魔术字节校验（docs/06 §6.1、docs/10 规则 S6）
# --------------------------------------------------------------------------

#: 文件头 → 真实类型。`(kind, magic, offset)`。
#: 只看扩展名是不够的：扩展名是**用户可控的字符串**，而文件内容不是。
#: 把 `payload.exe` 改名成 `photo.jpg` 就能绕过纯扩展名白名单。
_MAGIC: Final[list[tuple[str, bytes, int]]] = [
    ("jpeg", b"\xff\xd8\xff", 0),
    ("png", b"\x89PNG\r\n\x1a\n", 0),
    ("gif", b"GIF87a", 0),
    ("gif", b"GIF89a", 0),
    ("webp", b"RIFF", 0),
    ("bmp", b"BM", 0),
    ("tiff", b"II*\x00", 0),
    ("tiff", b"MM\x00*", 0),
]

#: ISO-BMFF（HEIC/AVIF）的 brand：偏移 8 处。偏移 4 必须是 `ftyp`。
_BMFF_BRANDS: Final[frozenset[bytes]] = frozenset({
    b"avif", b"avis", b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1",
})

#: 扩展名 → 期望的类型。只列白名单内的扩展名。
_EXT_KIND: Final[dict[str, str]] = {
    ".jpg": "jpeg", ".jpeg": "jpeg", ".jpe": "jpeg",
    ".png": "png", ".gif": "gif", ".webp": "webp", ".bmp": "bmp",
    ".tif": "tiff", ".tiff": "tiff",
    ".avif": "avif", ".heic": "heic", ".heif": "heic",
}

#: 读文件头用的字节数。JPEG 的 SOI 在偏移 0，BMFF 的 brand 在偏移 12，
#: 32 字节对白名单里的所有格式都够。
MAGIC_HEAD_BYTES: Final[int] = 32


def sniff_kind(head: bytes) -> str | None:
    """从文件头判定真实类型，无法识别返回 None。

    注意 **不能** 靠"Pillow 能不能打开"来判断：真实库里 22 个 `.jpg-lx2`
    的真实内容是 PNG，Pillow 打得开，但它们不该进照片库（docs/07 F7）。
    """
    if len(head) < 12:
        # 白名单里的所有格式，最短的合法文件头也有 12 字节可供判定；
        # 比这更短的不可能是正常照片，直接拒。
        return None
    for kind, magic, off in _MAGIC:
        if head[off:off + len(magic)] == magic:
            if kind == "webp":
                # RIFF 只是容器（wav/avi 也是 RIFF），必须再看偏移 8 的 WEBP。
                # 少了这一步，一个改名为 .webp 的 wav 会被放进去。
                if head[8:12] != b"WEBP":
                    continue
            return kind
    if head[4:8] == b"ftyp" and head[8:12] in _BMFF_BRANDS:
        return "avif" if head[8:12] == b"avif" else "heic"
    return None


def check_magic(cfg: Config, name: str, head: bytes) -> str:
    """扩展名 + 魔术字节双重校验，返回规范扩展名。

    与 `check_extension` 的区别：那个只看名字，这个看内容。
    `head` 至少要有 `MAGIC_HEAD_BYTES` 字节（不足按截断处理，
    短到无法判定一律拒绝）。
    """
    ext = check_extension(cfg, name)
    declared = _EXT_KIND.get(ext)
    actual = sniff_kind(head[:MAGIC_HEAD_BYTES])
    if declared is None or actual is None or actual != declared:
        # 统一用 E1017（"不支持的文件格式"）对外，不新增错误码 ——
        # 手机端对这两种情况的处理完全一样（让用户换一张图重传），
        # 而区分码会把 docs/04 的错误表契约改宽。审计里留 `reason`
        # 区分：排查时能看出是"扩展名不对"还是"内容伪装"。
        raise AppError(
            E1017,
            f"文件内容与扩展名不符（声明 {ext}，实际 {actual or '无法识别'}）",
        )
    return ext


def create_session(
    db: Database,
    cfg: Config,
    *,
    name: str,
    size: int,
    root_id: str | None = None,
    sha256: str | None = None,
    mtime: float | None = None,
    target_dir: str | None = None,
    declared_taken_at: float | None = None,
    client_chunk_size: int | None = None,
) -> dict[str, Any]:
    """创建或复用上传会话。

    返回的 dict 里 `created` 标明是新会话还是复用了已有会话 ——
    两者都返回 200/201 给客户端，但语义不同（复用时不重置已收字节）。
    """
    if not isinstance(name, str) or not name.strip():
        raise BadRequest("缺少文件名")
    name = name.strip()
    if len(name) > 255:
        raise BadRequest("文件名过长")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise BadRequest("文件大小必须为正整数")
    max_bytes = int(cfg.upload["max_file_mb"]) * 1024 * 1024
    if size > max_bytes:
        raise AppError(E1006, f"文件超过 {cfg.upload['max_file_mb']} MB 上限")

    ext = check_extension(cfg, name)

    root = cfg.root_by_id(root_id) if root_id else None
    if root is None:
        root = cfg.root_by_id(cfg.default_root_id)
    if root is None:
        raise AppError(E1012, "尚未配置媒体根目录")
    if not root.can_write:
        from .errors import ReadonlyError

        raise ReadonlyError("当前媒体根为只读，无法上传")

    if sha256 is not None:
        sha256 = sha256.strip().lower() or None
        if sha256 and (len(sha256) != 64 or not all(c in "0123456789abcdef" for c in sha256)):
            raise BadRequest("sha256 格式不正确")

    # 子目录：显式指定优先，否则用配置里的 upload.subdir
    subdir = archive.subdir_prefix(
        target_dir if target_dir is not None else cfg.upload.get("subdir")
    )

    # --- 1) 秒传：init 阶段就判，能省掉全部字节 ---
    dup = find_duplicate(db, cfg, sha256, root_id=root.id) if _dedup_on(cfg) else None
    if dup is not None:
        log.info("秒传命中：%s -> media#%d", name, dup["media_id"])
        return {
            "created": False,
            "exists": True,
            "upload_id": None,
            "offset": size,
            "chunk_size": _chunk_size(cfg),
            "media_id": dup["media_id"],
            "expires_at": None,
            "root_id": root.id,
            "final_rel": dup["rel_path"],
            "dedup": True,
            "subdir": subdir,
            "ext": ext,
        }

    # --- 2) 同一文件的重复 init：复用会话，不重置已收字节 ---
    existing = db.query_one(
        "SELECT * FROM upload_sessions WHERE status = 'open' AND expires_at > ? "
        "AND root_id = ? AND declared_name = ? AND total_size = ? "
        "AND (sha256 IS ? OR sha256 = ?) "
        "ORDER BY created_at DESC LIMIT 1",
        (time.time(), root.id, name, size, sha256, sha256),
    )
    if existing is not None:
        sess = dict(existing)
        received = _reconcile_offset(db, sess)
        return {
            "created": False,
            "exists": False,
            "upload_id": sess["id"],
            "offset": received,
            "chunk_size": int(sess["chunk_size"]),
            "media_id": None,
            "expires_at": float(sess["expires_at"]),
            "root_id": root.id,
            "final_rel": None,
            "dedup": False,
            "subdir": subdir,
            "ext": ext,
        }

    # --- 3) 并发上限 ---
    if open_session_count(db) >= MAX_OPEN_SESSIONS:
        raise AppError(E4002, "同时上传的任务太多，请等前面的传完")

    purge_expired(db)

    upload_id = uuid.uuid4().hex
    temp = sandbox.tmp_path(root, upload_id)
    now = time.time()
    ttl = float(cfg.upload["session_ttl_hours"]) * 3600.0
    chunk = _chunk_size(cfg)

    try:
        temp.parent.mkdir(parents=True, exist_ok=True)
        # O_CREAT|O_EXCL：绝不覆盖可能存在的同名临时文件。
        # upload_id 是随机 32 位 hex，撞车概率可忽略；但真撞上时
        # 覆盖会破坏另一个正在进行的上传。
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o644)
        os.close(fd)
    except FileExistsError:
        raise Conflict("上传会话冲突，请重试") from None
    except OSError as exc:
        raise AppError(E3010, f"无法创建临时文件：{exc!r}") from exc

    db.execute(
        "INSERT INTO upload_sessions(id, root_id, target_rel, final_rel, temp_path, "
        " total_size, received, chunk_size, sha256, declared_name, taken_at, "
        " created_at, expires_at, status, media_id, dedup, completed_at) "
        "VALUES (?, ?, ?, NULL, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'open', NULL, NULL, NULL)",
        (
            upload_id, root.id, subdir, str(temp), size, chunk,
            sha256, name, declared_taken_at, now, now + ttl,
        ),
    )
    log.info("新建上传会话 %s：%s（%d 字节）", upload_id, name, size)
    return {
        "created": True,
        "exists": False,
        "upload_id": upload_id,
        "offset": 0,
        "chunk_size": chunk,
        "media_id": None,
        "expires_at": now + ttl,
        "root_id": root.id,
        "final_rel": None,
        "dedup": False,
        "subdir": subdir,
        "ext": ext,
    }


def _chunk_size(cfg: Config) -> int:
    """服务端权威分片大小。

    局域网与远程不同：云隧道下 RTT 高，8 MB 分片丢一片要重传 8 MB，
    2 MB 分片只重传 2 MB。

    ★ `chunk_mb_wan` 此前是**死配置**：`config.py` 声明了它、
    AC-6.8 也把它写进了验收标准，但代码里零读取 —— 二期接上隧道后
    分片大小会静静地仍是 8 MB，而验收测试抓包才会发现。
    这里按 `server.transport` 选：非局域网通路一律用 WAN 档。
    一期只有 `local_http`，所以永远走 LAN 分支；这个分支存在的意义
    是让二期不要忘记改这里。

    为什么按 transport 而不是按"客户端声称的网络类型"：客户端不可信，
    而且它自己往往也判断不出来（手机连着的 Wi-Fi 背后可能是 4G 中继）。
    服务端知道自己在用哪条通路。
    """
    transport = str(cfg.get("server.transport", "local_http") or "local_http")
    key = "chunk_mb_lan" if transport == "local_http" else "chunk_mb_wan"
    mb = cfg.upload.get(key)
    if mb is None:
        # 配置里只写了另一个档位（用户手改过配置文件）→ 用它，
        # 总比退回硬编码的 8 更贴近用户意图。
        mb = cfg.upload.get("chunk_mb_lan", 8)
    try:
        mb = int(mb)
    except (TypeError, ValueError):
        mb = 8
    return max(256 * 1024, mb * 1024 * 1024)


def _reconcile_offset(db: Database, sess: dict[str, Any]) -> int:
    """以**磁盘实际大小**为准修正 offset。

    DB 与文件不一致只可能来自崩溃（写完文件、没来得及更新 DB）。
    以 DB 为准会让续传位置错位：DB 偏小时客户端会 seek 回去重写
    （内容相同，无害但浪费），DB 偏大时会跳过一段，文件中间留下
    空洞 —— 在照片上表现为灰条，且 sha256 永远对不上，
    用户反复重传都失败，却没有任何提示指向真正的原因。
    """
    temp = sess.get("temp_path")
    actual = 0
    if temp:
        try:
            actual = os.path.getsize(temp)
        except OSError:
            actual = 0
    actual = min(actual, int(sess["total_size"]))
    if actual != int(sess["received"]):
        log.warning(
            "会话 %s 的 offset 与磁盘不一致（DB=%s 磁盘=%s），以磁盘为准",
            sess["id"], sess["received"], actual,
        )
        db.execute("UPDATE upload_sessions SET received = ? WHERE id = ?", (actual, sess["id"]))
    return actual


# --------------------------------------------------------------------------
# 写入分片
# --------------------------------------------------------------------------


def append_chunk(
    db: Database,
    cfg: Config,
    upload_id: str,
    *,
    start: int,
    end: int,
    total: int,
    data: bytes,
) -> dict[str, Any]:
    """把一个分片追加到临时文件。

    只支持**顺序追加**。乱序回 409 + 当前 offset，让客户端自己对齐 ——
    支持随机写需要预分配与空洞管理，而手机端上传本来就是顺序的，
    为不存在的场景增加复杂度不划算（docs/03 §4.3）。
    """
    with session_lock(upload_id):
        return _append_chunk_locked(
            db, cfg, upload_id, start=start, end=end, total=total, data=data
        )


def _append_chunk_locked(
    db: Database,
    cfg: Config,
    upload_id: str,
    *,
    start: int,
    end: int,
    total: int,
    data: bytes,
) -> dict[str, Any]:
    sess = get_session(db, upload_id)
    if sess["status"] != "open":
        raise AppError(E1012, "上传已结束")

    if total != int(sess["total_size"]):
        raise BadRequest("分片声明的总大小与上传会话不符")

    received = _reconcile_offset(db, sess)

    if start < received and end < received:
        # 整片都已收到：这是重试（客户端没收到上次的响应就重发了）。
        # 幂等返回成功，不重复写入 —— 重复写会让文件变长。
        return {
            "upload_id": upload_id, "offset": received, "received": received,
            "total_size": int(sess["total_size"]), "complete": received >= int(sess["total_size"]),
            "duplicate": True,
        }
    if start > received or (start < received <= end):
        # 跳号，或与已收部分部分重叠（无法安全拼接）
        raise AppError(
            E1011,
            f"分片顺序异常：期望 {received}，收到 {start}",
            headers={"Range": f"bytes=0-{max(received - 1, 0)}", "X-Upload-Offset": str(received)},
        )

    expected_len = end - start + 1
    if len(data) != expected_len:
        raise BadRequest(f"分片长度与 Content-Range 不符：声明 {expected_len}，实际 {len(data)}")
    if len(data) > int(sess["chunk_size"]) + 1024:
        raise AppError(E1007, f"分片超过 {sess['chunk_size']} 字节上限")

    root = root_of(cfg, sess)
    temp = Path(sess["temp_path"])
    # 再走一次沙箱：temp_path 来自 DB，理论上可被改；且库根配置
    # 可能在两次请求之间被改成只读。
    try:
        temp = sandbox.tmp_path(root, upload_id)
    except SandboxError as exc:
        raise AppError(E1012, f"临时路径不可用：{exc.rule}") from None

    try:
        with open(temp, "r+b") as fh:
            fh.seek(received)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            new_offset = fh.tell()
    except FileNotFoundError:
        raise AppError(E1012, "临时文件已丢失，请重新开始上传") from None
    except OSError as exc:
        if getattr(exc, "errno", None) == 28:      # ENOSPC
            raise AppError(E3010, "电脑磁盘空间不足") from None
        raise AppError(E3010, f"写入临时文件失败：{exc!r}") from exc

    db.execute(
        "UPDATE upload_sessions SET received = ? WHERE id = ?", (new_offset, upload_id)
    )
    complete = new_offset >= int(sess["total_size"])
    return {
        "upload_id": upload_id,
        "offset": new_offset,
        "received": new_offset,
        "total_size": int(sess["total_size"]),
        "complete": complete,
        "duplicate": False,
    }


# --------------------------------------------------------------------------
# 完成
# --------------------------------------------------------------------------


def hash_file(path: Path, *, chunk: int = HASH_CHUNK) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def complete_session(
    db: Database,
    cfg: Config,
    upload_id: str,
    *,
    indexer=None,
    thumbs=None,
    sha256: str | None = None,
    taken_at: float | None = None,
) -> dict[str, Any]:
    """校验 → 去重 → 归档 → 索引。

    顺序不能换：**先写盘再更新 DB**。反过来若归档成功但 DB 更新失败，
    下次扫描会把文件收进来，照片还在，只是少一条会话记录；
    若 DB 先更新而归档失败，库里会有一条指向不存在文件的记录，
    用户在时间线上看到一张永远打不开的图。
    """
    sess = get_session(db, upload_id)
    if sess["status"] == "completed":
        # 客户端没收到响应就重试了 complete。幂等返回，不能报错 ——
        # 报错会让手机以为上传失败，而照片其实已经进库了。
        return {
            "upload_id": upload_id,
            "media_id": sess["media_id"],
            "rel_path": sess["final_rel"],
            "dedup": bool(sess["dedup"]),
            "already_completed": True,
            "sha256": sess["sha256"],
        }
    if sess["status"] != "open":
        raise AppError(E1012, "上传已结束")

    root = root_of(cfg, sess)
    temp = Path(sess["temp_path"])
    total = int(sess["total_size"])

    if not temp.is_file():
        raise AppError(E1012, "临时文件已丢失，请重新开始上传")
    actual = os.path.getsize(temp)
    if actual != total:
        raise AppError(
            E1014,
            f"文件未传完：已收 {actual}/{total}",
            headers={"X-Upload-Offset": str(actual)},
        )

    # --- 魔术字节：必须在算哈希**之前** ---
    # 一个改名为 .jpg 的 exe 可能有几百 MB，先算 sha256 再拒绝等于白读
    # 一整遍磁盘；读 32 字节就能判掉。失败时**删临时文件并置 aborted**，
    # 与 sha256 不符的处理**相反** —— 后者保留临时文件让客户端重传坏分片
    # （内容是对的，只是传输坏了），而伪装文件的字节再传一万遍也还是 exe。
    try:
        with open(temp, "rb") as fh:
            head = fh.read(MAGIC_HEAD_BYTES)
        check_magic(cfg, sess["declared_name"], head)
    except AppError:
        _unlink_temp(str(temp))
        db.execute(
            "UPDATE upload_sessions SET status = 'aborted', completed_at = ? "
            "WHERE id = ?",
            (time.time(), upload_id),
        )
        _drop_lock(upload_id)
        _audit(db, cfg, audit.ACTION_UPLOAD_REJECT, sess["declared_name"], {
            "upload_id": upload_id,
            "declared_name": sess["declared_name"],
            "size": actual,
            "reason": "magic_mismatch",
        })
        log.warning(
            "上传被拒（魔术字节不符）：%s（%d 字节）",
            sess["declared_name"], actual,
        )
        raise

    # --- 哈希：服务端自己算，不信任客户端声明 ---
    digest = hash_file(temp)
    declared = (sha256 or sess["sha256"] or "").strip().lower() or None
    if declared and declared != digest:
        # 客户端算错（读文件时被别的程序占用）或传错文件。
        # 此时**保留临时文件**让客户端能重传缺失的分片，
        # 但不能进库 —— 进库后照片内容与用户以为的不是同一张。
        raise AppError(E1013, f"文件校验失败：期望 {declared[:12]}…，实际 {digest[:12]}…")

    # --- 去重（第二次判）：覆盖"客户端没带 sha256"与并发上传 ---
    dup = find_duplicate(db, cfg, digest, root_id=root.id) if _dedup_on(cfg) else None
    if dup is not None and dup["size"] == total:
        _unlink_temp(str(temp))
        db.execute(
            "UPDATE upload_sessions SET status = 'completed', received = total_size, "
            "sha256 = ?, final_rel = ?, media_id = ?, dedup = 1, completed_at = ? "
            "WHERE id = ?",
            (digest, dup["rel_path"], dup["media_id"], time.time(), upload_id),
        )
        log.info("complete 阶段去重命中：%s -> media#%d", sess["declared_name"], dup["media_id"])
        _audit(db, cfg, audit.ACTION_UPLOAD, dup["rel_path"], {
            "upload_id": upload_id, "dedup": True, "size": total,
        })
        return {
            "upload_id": upload_id,
            "media_id": dup["media_id"],
            "rel_path": dup["rel_path"],
            "dedup": True,
            "already_completed": False,
            "sha256": digest,
        }

    # --- 归档 ---
    # ⚠️ 归档目录由**照片自己的 EXIF 拍摄时间**决定，不是上传时间。
    # 手机相册里翻出一张 2019 年的老照片补传，它应该落进 2019 年目录；
    # 用上传时间会让整个库的年份结构变成"什么时候传的"，与照片内容
    # 完全脱节，用户按年份找照片时永远找不到。
    when = taken_at or sess["taken_at"]
    if when is None:
        # init 时客户端没带 declared_taken_at：自己从临时文件读 EXIF。
        when = _read_taken_at(temp, sess["declared_name"])
    if when is None:
        # 连 EXIF 都没有（截图、社交软件导出的图）才退回 mtime。
        try:
            when = os.path.getmtime(temp)
        except OSError:
            when = time.time()
    try:
        rel_path, dst = _place_sync(temp, root, cfg, when, sess["declared_name"], sess["target_rel"])
    except ArchiveError as exc:
        raise AppError(E3010, str(exc)) from exc

    size = dst.stat().st_size

    # --- 索引（单文件增量） ---
    media_id: int | None = None
    if indexer is not None:
        try:
            media_id = indexer.index_one_file(root, rel_path)
        except Exception:      # noqa: BLE001
            # 索引失败不能让上传失败：文件已经安全落盘了。
            # 报错会让用户重传几百 MB，而照片其实就在库里，
            # 下次全量扫描一定会收录它。
            log.exception("上传后索引失败，等待下次扫描：%s", rel_path)

    # 把刚算出来的 sha256 写进 media 行。
    #
    # 这一步是**秒传能生效的前提**：索引器的 hash_on_index 默认关闭
    # （对 4915 张已有照片全量哈希会把首次索引从 20 秒拖到十几分钟），
    # 所以扫描出来的行 sha256 全是 NULL。而上传路径本来就必须算一次
    # 哈希来校验完整性 —— 白算的哈希顺手存下来，下次同一张照片再传
    # 就能命中秒传。不存的话"重复上传同一张照片"永远走完整流程，
    # 而这正是用户最常做的事（换手机重装 App、相册全量同步）。
    if media_id is not None and digest:
        try:
            db.execute("UPDATE media SET sha256 = ? WHERE id = ?", (digest, media_id))
        except Exception:      # noqa: BLE001
            log.debug("回写 sha256 失败：%s", rel_path, exc_info=True)

    db.execute(
        "UPDATE upload_sessions SET status = 'completed', received = total_size, "
        "sha256 = ?, final_rel = ?, media_id = ?, dedup = 0, completed_at = ? "
        "WHERE id = ?",
        (digest, rel_path, media_id, time.time(), upload_id),
    )
    _drop_lock(upload_id)
    log.info("上传完成 %s -> %s（%d 字节）", sess["declared_name"], rel_path, size)
    _audit(db, cfg, audit.ACTION_UPLOAD, rel_path, {
        "upload_id": upload_id, "dedup": False, "size": size,
        "declared_name": sess["declared_name"],
    })
    return {
        "upload_id": upload_id,
        "media_id": media_id,
        "rel_path": rel_path,
        "dedup": False,
        "already_completed": False,
        "sha256": digest,
    }


def _read_taken_at(path: Path, file_name: str) -> float | None:
    """从临时文件里读拍摄时间，读不到返回 None。

    优先级与索引器完全一致（EXIF → 文件名 → 交给调用方用 mtime 兜底），
    这样"上传归档"与"全量扫描"对同一张照片算出同一个时间。
    两边不一致时，用户会看到照片在时间线上的位置和它所在的目录对不上。

    这里**只读 EXIF 头**（不 decode 像素），一张 20MB 的 HEIC 约 30ms，
    比走一遍 Pillow 完整解码快两个数量级。
    """
    from . import exif as exif_mod

    try:
        info = exif_mod.extract_sync(path)
    except Exception:      # noqa: BLE001
        log.debug("读 EXIF 失败，退回 mtime：%s", path, exc_info=True)
        return None
    ts, src = exif_mod.resolve_taken_at(info, file_name, os.path.getmtime(path))
    # resolve_taken_at 的 mtime 兜底值不算"读到了拍摄时间" —— 那是
    # 调用方自己也会做的事，返回 None 让调用链只有一处 mtime 逻辑。
    return ts if src != "mtime" else None


def _place_sync(
    temp: Path, root: RootConfig, cfg: Config, when: float, name: str, subdir: str | None) -> tuple[str, Path]:
    """归档（同步）。在 executor 里跑，避免阻塞事件循环。"""
    ext = archive.normalize_ext(os.path.splitext(name)[1])
    return asyncio_run_sync(archive.place(temp, root, when, ext, subdir=subdir, cfg=cfg))


def asyncio_run_sync(coro):
    """在**已在运行**的事件循环之外执行协程。

    `archive.place` 是 async（签名如此，因为二期归档要经隧道），
    但它内部全是同步文件操作。这里不能用 `asyncio.run()` ——
    complete_session 本身就在事件循环里被调用，嵌套 asyncio.run
    会抛 "asyncio.run() cannot be called from a running event loop"。
    用私有事件循环执行，语义等价且不依赖调用方是否在协程里。
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


async def complete_session_async(
    db: Database, cfg: Config, upload_id: str, *, indexer=None, thumbs=None,
    sha256: str | None = None, taken_at: float | None = None,
) -> dict[str, Any]:
    """异步包装：把重活（哈希、归档）丢进线程池。

    一张 20 MB 的 HEIC 算 sha256 约 40 ms，归档涉及 mkdir + rename；
    直接同步跑会在这段时间里冻住事件循环，其他手机的时间线请求
    全部卡住 —— 上传是"用户主动触发且可等待"的操作，
    不该让浏览照片的人一起等。
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None,
        lambda: complete_session(
            db, cfg, upload_id, indexer=indexer, thumbs=thumbs,
            sha256=sha256, taken_at=taken_at,
        ),
    )


# --------------------------------------------------------------------------
# 取消
# --------------------------------------------------------------------------


def abort_session(db: Database, cfg: Config, upload_id: str) -> bool:
    """取消上传：删临时文件、标记 aborted。

    行保留（不 DELETE）：用户放弃上传这件事本身是要能查的 ——
    "我明明传了 50 张，怎么只有 48 张"这类问题的答案就在这些行里。
    """
    sess = get_session(db, upload_id)
    if sess["status"] == "completed":
        raise AppError(E1012, "上传已完成，无法取消")
    _unlink_temp(sess["temp_path"])
    db.execute(
        "UPDATE upload_sessions SET status = 'aborted', completed_at = ? WHERE id = ?",
        (time.time(), upload_id),
    )
    _drop_lock(upload_id)
    log.info("上传已取消 %s", upload_id)
    return True


# --------------------------------------------------------------------------
# 审计
# --------------------------------------------------------------------------


def _audit(db: Database, cfg: Config, action: str, rel_path: str,
           detail: dict[str, Any]) -> None:
    """写审计。已合并到 `backend/app/audit.py`（此前这里有第三份实现）。

    ⚠️ `upload_sessions` 表**没有**记录 client_id / device_name
    （只有 id/root_id/target_rel/temp_path/declared_name 等），
    所以上传这条审计线索**无法回答"哪台手机传的"**。
    这在一期（只有用户自己的设备）影响有限，但值得记下来：
    要补就得给 upload_sessions 加列，那是 schema 迁移。
    """
    audit.record(db, cfg, action, rel_path=rel_path, detail=detail)


__all__ = [
    "HASH_CHUNK",
    "MAGIC_HEAD_BYTES",
    "MAX_OPEN_SESSIONS",
    "abort_session",
    "append_chunk",
    "check_extension",
    "check_magic",
    "complete_session",
    "complete_session_async",
    "create_session",
    "find_duplicate",
    "get_session",
    "hash_file",
    "open_session_count",
    "purge_expired",
    "root_of",
    "sniff_kind",
]
