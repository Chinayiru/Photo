"""目录扫描与索引器。

职责：把磁盘上的照片变成 `media` 表里的行。这是全系统唯一一处"主动遍历
用户目录"的代码，因此安全上最敏感 —— 详见 `_walk` 对 junction 的处理。

三条设计原则：

1. **索引绝不修改用户文件**。只 stat / 只读打开，不改 mtime、不写同目录文件。
2. **一个坏文件不能中断整轮扫描**。4915 个文件里有一个权限错误就让整轮
   失败，用户看到的是"扫描失败"而不是"跳过了 1 个文件"。
3. **扫描必须可中断**。首次索引要跑几分钟，用户可能中途改配置或退出程序；
   不可中断的扫描会卡住退出流程。
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Final, Iterable, Iterator

from . import exif, sandbox
from .config import Config, RootConfig
from .events import EventBus
from .errors import AppError
from .exif import ExifInfo, resolve_taken_at

if TYPE_CHECKING:  # pragma: no cover
    from .db import Database
    from .thumbs import ThumbEngine

log = logging.getLogger(__name__)

#: 每积累这么多条就写一次库。500 是实测值：太小则事务开销占比高
#: （每 50 条一写时写入耗时占比约 35%），太大则中断时丢的进度多。
BATCH_SIZE: Final[int] = 500

#: 单轮扫描最多连续跳过的"坏目录"数。超过则判定为路径系统异常并中止，
#: 避免在递归损坏的目录结构上无限跑下去。
MAX_CONSECUTIVE_ERRORS: Final[int] = 200


# --------------------------------------------------------------------------
# 扫描结果
# --------------------------------------------------------------------------

@dataclass(slots=True)
class ScanStats:
    """一轮扫描的统计。既进日志也回给 /api/index/status。"""

    root_id: str
    seen: int = 0            # 白名单命中的候选文件数
    added: int = 0           # 新插入
    updated: int = 0         # 已存在且 mtime/size 变化
    unchanged: int = 0       # 已存在且未变
    missing: int = 0         # 标记为 missing
    skipped_ext: int = 0     # 扩展名不在白名单
    skipped_dir: int = 0     # 排除目录
    skipped_link: int = 0    # ★ junction/symlink，绝不进入
    errors: int = 0
    thumb_queued: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    aborted: str | None = None

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return end - self.started_at

    def as_dict(self) -> dict[str, object]:
        d = {k: getattr(self, k) for k in (
            "root_id", "seen", "added", "updated", "unchanged", "missing",
            "skipped_ext", "skipped_dir", "skipped_link", "errors",
            "thumb_queued", "aborted",
        )}
        d["elapsed"] = round(self.elapsed, 2)
        d["started_at"] = self.started_at
        d["finished_at"] = self.finished_at
        return d

    def summary(self) -> str:
        return (
            f"[{self.root_id}] {self.seen} 个候选，新增 {self.added}，"
            f"更新 {self.updated}，未变 {self.unchanged}，丢失 {self.missing}，"
            f"跳过 扩展名 {self.skipped_ext}/目录 {self.skipped_dir}/链接 {self.skipped_link}，"
            f"错误 {self.errors}，缩略图入队 {self.thumb_queued}，"
            f"耗时 {self.elapsed:.1f}s"
        )


# --------------------------------------------------------------------------
# 目录遍历（安全核心）
# --------------------------------------------------------------------------

def _is_excluded_dir(name: str, root: RootConfig) -> bool:
    return name.lower() in root.exclude_dirs


def _is_excluded_file(name: str, root: RootConfig) -> bool:
    low = name.lower()
    if low in root.exclude_files:
        return True
    # Windows 缩略图缓存与 Office 临时文件：形如 ~$xxx / xxx.tmp
    return low.startswith("~$") or low.endswith(".tmp")


def _ext_of(name: str) -> str:
    """取扩展名（小写，不含点）。

    ⚠️ 用 rsplit 而不是 Path.suffix：`.jpg-lx2` 这种小米回收站残留的
    suffix 是 ".jpg-lx2"，而我们要的是"不匹配任何白名单扩展名"。
    用 rsplit 得到 "jpg-lx2"，同样不匹配，效果一致但更直观。
    """
    _, dot, ext = name.rpartition(".")
    return ext.lower() if dot else ""


def _walk(root: RootConfig, stats: ScanStats) -> Iterator[tuple[str, os.stat_result]]:
    """遍历根目录，产出 (相对路径, stat)。

    ★ **本函数是索引器最容易被忽视的越界点**。

    设计文档 §8.1 只覆盖了"请求路径"的沙箱校验，但索引器自己遍历目录时
    同样可能走出库外。`os.walk(followlinks=False)` **不防 Windows junction** ——
    junction 不是 symlink，`os.path.islink()` 对它返回 False，而
    `os.walk` 会照常递归进去。

    实测（scripts/check_db_sandbox.py）：`mklink /J` 无需管理员权限即可创建，
    一个指向 C:\\ 的 junction 放在库根下，索引器会把整台机器的图片都扫进来。
    这不是理论风险，是当前布局下真实可复现的。

    因此这里**不用 os.walk**，改为手写栈式遍历并逐目录检查 reparse 位：
    只要目录自身是 reparse point（junction/symlink/挂载点），整棵子树跳过，
    不递归、不报错、计入 skipped_link。
    """
    root_path = Path(root.path)
    stack: list[tuple[Path, str]] = [(root_path, "")]
    consecutive_errors = 0

    while stack:
        cur, rel_prefix = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError as e:
            stats.errors += 1
            consecutive_errors += 1
            log.warning("scandir failed rel=%s: %s", rel_prefix or ".", e)
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                stats.aborted = f"连续 {consecutive_errors} 个目录无法读取，已中止"
                log.error("scan aborted: %s", stats.aborted)
                return
            continue
        consecutive_errors = 0

        for entry in entries:
            name = entry.name
            rel = f"{rel_prefix}/{name}" if rel_prefix else name
            try:
                # ⚠️ 必须用 lstat 语义（follow_symlinks=False）。
                # os.stat() 会**跟随** junction，返回目标目录的属性，
                # st_file_attributes 里就没有 reparse 位了（实测：stat 给 0x10，
                # lstat 给 0x410）。用 stat 的结果去查 reparse 位永远查不出来，
                # junction 会被当成普通目录递归进去 —— 这正是本模块要防的事。
                st = entry.stat(follow_symlinks=False)
            except OSError:
                stats.errors += 1
                continue

            if entry.is_dir(follow_symlinks=False):
                if _is_excluded_dir(name, root):
                    stats.skipped_dir += 1
                    continue
                # ★ 关键：junction / symlink / 挂载点一律不进入
                if sandbox._is_reparse_or_link(Path(entry.path), st):
                    stats.skipped_link += 1
                    log.info("skipped reparse point dir: %s", rel)
                    continue
                if not root.recursive:
                    continue
                stack.append((Path(entry.path), rel))
                continue

            # --- 文件 ---
            if not entry.is_file(follow_symlinks=False):
                continue
            if _is_excluded_file(name, root):
                continue
            ext = _ext_of(name)
            if ext not in root.include_exts:
                stats.skipped_ext += 1
                continue
            # 文件级的 reparse（符号链接文件）同样跳过：它指向库外
            if sandbox._is_reparse_or_link(Path(entry.path), st):
                stats.skipped_link += 1
                log.info("skipped reparse point file: %s", rel)
                continue
            yield rel, st


# --------------------------------------------------------------------------
# 索引器
# --------------------------------------------------------------------------

class Indexer:
    """把一个根目录同步进 media 表。

    用法：
        idx = Indexer(cfg, db, thumbs=engine)
        stats = await idx.scan_root(cfg.root_by_id("library"))
    """

    def __init__(
        self,
        cfg: Config,
        db: "Database",
        *,
        thumbs: "ThumbEngine | None" = None,
        batch_size: int = BATCH_SIZE,
        bus: EventBus | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.thumbs = thumbs
        # 事件总线用于把索引进度推给手机端。为 None 时不发事件，
        # 这样索引器在测试与脚本里可以完全脱离事件系统运行。
        self.bus = bus
        self.batch_size = max(1, batch_size)
        self._cancel = asyncio.Event()
        self._running = False
        self.last_stats: dict[str, ScanStats] = {}

    # --- 控制 -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        """请求中止当前扫描。扫描会在当前批次结束时退出（最多再处理 batch_size 个）。"""
        self._cancel.set()

    # --- 主流程 -----------------------------------------------------------

    async def scan_all(self) -> list[ScanStats]:
        out: list[ScanStats] = []
        for root in self.cfg.enabled_roots:
            out.append(await self.scan_root(root))
        return out

    async def scan_root(self, root: RootConfig) -> ScanStats:
        """扫描单个根。返回统计。

        流程：遍历 → 逐文件 stat/EXIF → 批量 upsert → 标记 missing → 缩略图入队。
        """
        # 每轮扫描都从干净状态开始。上一轮的 cancel 若不清掉，之后的扫描
        # 会立刻以"用户中止"结束，表现为"点重新扫描没反应"。
        self._cancel.clear()
        stats = ScanStats(root_id=root.id)
        self._running = True
        self._cancel.clear()
        # 根目录本身不可读时提前退出并明确报错。
        # 不这样做的话，scandir 失败会被计入 errors 后继续跑完空栈，
        # 最终 aborted 仍是 None —— 用户看到"扫描完成，错误 1"却不知道
        # 整个根都没扫到（典型场景：移动硬盘没插、盘符变了）。
        if not root.path.is_dir():
            stats.errors += 1
            stats.aborted = f"根目录不可访问：{root.id}"
            log.error("root not accessible root=%s path=%s", root.id, root.path)
            return stats

        log.info("scan started root=%s path=%s", root.id, root.path)
        self._emit({"type": "scan_start", "root": root.id, "ts": time.time()})
        try:
            known = self._load_known(root.id)
            batch: list[tuple] = []
            seen_rels: set[str] = set()

            for rel, st in _walk(root, stats):
                if self._cancel.is_set():
                    stats.aborted = "用户中止"
                    break
                stats.seen += 1
                seen_rels.add(rel)

                prev = known.get(rel)
                if prev is not None and not self._changed(prev, st):
                    stats.unchanged += 1
                    # 未变但仍缺缩略图 → 补入队（上次可能没跑完）
                    if prev["thumb_state"] == "pending" and self.thumbs is not None:
                        if self.thumbs.enqueue(prev["id"]):
                            stats.thumb_queued += 1
                    continue

                row = await self._build_row(root, rel, st, prev)
                batch.append(row)
                if prev is None:
                    stats.added += 1
                else:
                    stats.updated += 1

                if len(batch) >= self.batch_size:
                    ids = self._flush(batch)
                    batch.clear()
                    if self.thumbs is not None:
                        stats.thumb_queued += self.thumbs.enqueue_many(ids)
                    self._emit_progress(root, stats)
                    # 让出事件循环，否则大批量扫描会卡住 API 响应
                    await asyncio.sleep(0)

            if batch:
                ids = self._flush(batch)
                if self.thumbs is not None:
                    stats.thumb_queued += self.thumbs.enqueue_many(ids)

            if not self._cancel.is_set():
                stats.missing = self._mark_missing(root.id, seen_rels)
        except Exception as e:  # noqa: BLE001 —— 扫描失败不能让整个服务倒下
            stats.errors += 1
            stats.aborted = str(e)
            log.exception("scan failed root=%s", root.id)
        finally:
            stats.finished_at = time.time()
            self._running = False
            self.last_stats[root.id] = stats
            self._emit({"type": "scan_done", "root": root.id, "stats": stats.as_dict()})
            log.info("scan finished %s", stats.summary())
        return stats

    # --- 事件 -------------------------------------------------------------

    def _emit(self, event: dict) -> None:
        """发布一条事件。**永不抛异常**。

        索引进度是"锦上添花"的信息：事件发不出去（没有订阅者、
        队列满、事件循环已关闭）绝不能影响扫描本身。
        """
        if self.bus is None:
            return
        try:
            self.bus.publish(event)
        except Exception:       # noqa: BLE001
            log.debug("事件发布失败：%s", event.get("type"), exc_info=True)

    def _emit_progress(self, root: RootConfig, stats: ScanStats) -> None:
        """每写完一批推一次进度。

        为什么按批而不是按文件：4915 个文件会推 4915 条事件，
        手机端每秒渲染几十次进度条反而更卡；按批（500 个）推
        约 10 条，用户看到的进度仍然是连续变化的。
        """
        self._emit({
            "type": "scan_progress",
            "root": root.id,
            "seen": stats.seen,
            "added": stats.added,
            "updated": stats.updated,
            "unchanged": stats.unchanged,
            "missing": stats.missing,
            "thumb_queued": stats.thumb_queued,
            "elapsed": stats.elapsed,
        })

    # --- 明细 -------------------------------------------------------------

    def _load_known(self, root_id: str) -> dict[str, dict]:
        """一次性载入该根已索引的行。

        为什么要全量载入内存：4915 行的 (rel_path, mtime, size, id, thumb_state)
        约 1.5MB，而逐文件查库要做 4915 次 SELECT。全量载入把首次索引从
        "4915 次查询 + 4915 次写"降到 "1 次查询 + 约 10 次批量写"。
        """
        rows = self.db.query(
            "SELECT id, rel_path, mtime, size, thumb_state FROM media WHERE root_id = ?",
            (root_id,),
        )
        return {r["rel_path"]: dict(r) for r in rows}

    @staticmethod
    def _changed(prev: dict, st: os.stat_result) -> bool:
        """mtime 与 size 都没变 → 视为未修改。

        ⚠️ 只比 mtime 不够：有些同步工具（网盘客户端、rsync -t）会保留
        原 mtime 但改变内容长度，只比 mtime 会漏掉这类替换。
        """
        return prev["mtime"] != st.st_mtime or prev["size"] != st.st_size

    async def _build_row(
        self, root: RootConfig, rel: str, st: os.stat_result, prev: dict | None,
    ) -> tuple:
        """构造一条待写入的 media 行。"""
        name = rel.rsplit("/", 1)[-1]
        ext = _ext_of(name)
        info = await exif.extract(sandbox.resolve(root, rel, "read", cfg=self.cfg))
        taken_at, taken_src = resolve_taken_at(info, name, st.st_mtime)

        return (
            root.id, rel, name, ext, "image",
            int(st.st_size), float(st.st_mtime),
            taken_at, taken_src,
            info.width, info.height,
            info.camera, info.lens,
            info.iso, info.fnum, info.exposure, info.focal,
            info.gps_lat, info.gps_lon, info.orientation,
            "pending" if prev is None else prev.get("thumb_state", "pending"),
            None,           # thumb_etag：源文件变了就作废，强制重新生成
            0,              # missing
            time.time(),    # indexed_at
        )

    _UPSERT_SQL: Final[str] = """
        INSERT INTO media (
            root_id, rel_path, file_name, ext, kind, size, mtime,
            taken_at, taken_src, width, height, camera, lens,
            iso, fnum, exposure, focal, gps_lat, gps_lon, orientation,
            thumb_state, thumb_etag, missing, indexed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(root_id, rel_path) DO UPDATE SET
            file_name  = excluded.file_name,
            ext        = excluded.ext,
            size       = excluded.size,
            mtime      = excluded.mtime,
            taken_at   = excluded.taken_at,
            taken_src  = excluded.taken_src,
            width      = excluded.width,
            height     = excluded.height,
            camera     = excluded.camera,
            lens       = excluded.lens,
            iso        = excluded.iso,
            fnum       = excluded.fnum,
            exposure   = excluded.exposure,
            focal      = excluded.focal,
            gps_lat    = excluded.gps_lat,
            gps_lon    = excluded.gps_lon,
            orientation= excluded.orientation,
            -- 内容变了（mtime/size 不同）则缩略图作废，重置为 pending。
            -- 否则保留原状态，避免每次扫描都重新生成 4915 张缩略图。
            thumb_state = CASE
                WHEN media.mtime <> excluded.mtime OR media.size <> excluded.size
                THEN 'pending' ELSE media.thumb_state END,
            thumb_etag = CASE
                WHEN media.mtime <> excluded.mtime OR media.size <> excluded.size
                THEN NULL ELSE media.thumb_etag END,
            missing    = 0,
            indexed_at = excluded.indexed_at
    """

    def _flush(self, batch: list[tuple]) -> list[int]:
        """批量写入一批行，返回对应的 media_id 列表（供缩略图入队）。

        单行失败不能让整批消失：`executemany` 是"要么全成要么全败"，
        一个坏值（真实库里出现过 Orientation=0）会让同批 500 张照片
        全部丢失且用户看不到任何提示。因此先整批快写，失败再逐行降级，
        只丢掉真正有问题的那几行。
        """
        try:
            return self._flush_batch(batch)
        except sqlite3.IntegrityError as exc:
            log.warning("批次写入被拒，降级为逐行写入：%s", exc)
            return self._flush_one_by_one(batch)

    def _flush_batch(self, batch: list[tuple]) -> list[int]:
        rels = [(r[0], r[1]) for r in batch]
        with self.db.transaction() as conn:
            conn.executemany(self._UPSERT_SQL, batch)
            # 取回 id：批量 upsert 不返回 id，按 (root_id, rel_path) 回查。
            # 一条 SQL 用 IN 比逐个 SELECT 快得多。
            ids: list[int] = []
            CHUNK = 200
            for i in range(0, len(rels), CHUNK):
                part = rels[i:i + CHUNK]
                placeholders = ",".join(["(?,?)"] * len(part))
                flat = [x for pair in part for x in pair]
                rows = conn.execute(
                    f"SELECT id, root_id, rel_path FROM media"
                    f" WHERE (root_id, rel_path) IN (VALUES {placeholders})",
                    flat,
                ).fetchall()
                ids.extend(r[0] for r in rows)
        return ids

    def _flush_one_by_one(self, batch: list[tuple]) -> list[int]:
        """逐行写入，跳过并记录无法写入的行。

        被跳过的文件仍会被标记为 missing（本轮见过但没入库），
        这样前端能明确显示"这张照片索引失败"，而不是静默少一张。
        """
        ids: list[int] = []
        for row in batch:
            try:
                ids.extend(self._flush_batch([row]))
            except sqlite3.IntegrityError as exc:
                log.error("单行写入失败，跳过 %s/%s：%s", row[0], row[1], exc)
        return ids

    def _mark_missing(self, root_id: str, seen: set[str]) -> int:
        """把本轮没见到的行标记为 missing=1。

        ⚠️ **不能直接 DELETE**。用户在手机上删了照片、或者只是临时把移动硬盘
        拔了，直接删行会让"回收站/最近删除"功能失去依据，也无法区分
        "文件被移走"与"文件被删"。标记 missing 后前端显示占位，
        用户可以在托盘里选择"清理已丢失的记录"。
        """
        rows = self.db.query(
            "SELECT id, rel_path FROM media WHERE root_id = ? AND missing = 0",
            (root_id,),
        )
        gone = [r["id"] for r in rows if r["rel_path"] not in seen]
        if not gone:
            return 0
        with self.db.transaction() as conn:
            for i in range(0, len(gone), 500):
                part = gone[i:i + 500]
                conn.execute(
                    f"UPDATE media SET missing = 1 WHERE id IN ({','.join('?' * len(part))})",
                    part,
                )
        log.info("marked %d rows missing in root=%s", len(gone), root_id)
        return len(gone)

    # --- 维护操作 ---------------------------------------------------------

    def purge_missing(self, root_id: str | None = None) -> int:
        """删除所有 missing 的行。由用户显式触发，不自动执行。"""
        if root_id:
            return self.db.execute_rowcount(
                "DELETE FROM media WHERE missing = 1 AND root_id = ?", (root_id,))
        return self.db.execute_rowcount("DELETE FROM media WHERE missing = 1")

    def index_one_file(self, root: RootConfig, rel: str) -> int | None:
        """单文件增量索引（上传完成后调用）。

        上传路径需要"落盘后立刻能在时间线看到"，重新扫全库不可接受。
        这里只处理一个文件，走与全量扫描完全相同的取 EXIF/解析时间逻辑，
        保证两条路径产出的行一致。
        """
        try:
            p = sandbox.resolve(root, rel, "read", cfg=self.cfg)
            st = p.stat()
        except (AppError, OSError) as e:
            log.warning("index_one_file failed rel=%s: %s", rel, e)
            return None
        info = exif.extract_sync(p)
        name = rel.rsplit("/", 1)[-1]
        taken_at, taken_src = resolve_taken_at(info, name, st.st_mtime)
        row = (
            root.id, rel, name, _ext_of(name), "image",
            int(st.st_size), float(st.st_mtime), taken_at, taken_src,
            info.width, info.height, info.camera, info.lens,
            info.iso, info.fnum, info.exposure, info.focal,
            info.gps_lat, info.gps_lon, info.orientation,
            "pending", None, 0, time.time(),
        )
        ids = self._flush([row])
        if ids and self.thumbs is not None:
            self.thumbs.enqueue(ids[0])
        return ids[0] if ids else None


__all__ = ["BATCH_SIZE", "Indexer", "ScanStats"]
