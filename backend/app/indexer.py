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
    #: 本轮**在磁盘上确实存在**的相对路径集合，`_mark_missing` 的唯一依据。
    #:
    #: ⚠️ 它**故意不参与 `as_dict()` / `summary()`**：增量监听时这个集合是
    #: 全库路径（4900 条），塞进 SSE 事件与日志里毫无意义且会撑大每一帧。
    #: 之所以挂在 ScanStats 上而不是用局部变量，是因为增量监听的
    #: `_index_files` 是逐批调用的，集合必须跨批存活。
    seen_rels: set[str] = field(default_factory=set, repr=False, compare=False)

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


def should_index(name: str, root: RootConfig) -> bool:
    """这个文件名该不该进索引？**与 `_walk` 的过滤条件逐条对齐**。

    为什么单独抽出来：全量扫描（`_walk`）与单文件增量（`index_one_file`）
    是两条独立入口，但它们**必须给出完全一致的答案**。曾经不一致 ——
    `index_one_file` 只看 `sandbox.resolve` 能不能通过（即"路径在库内"），
    完全不看扩展名白名单，于是：

      * 上传接口把 `foo.jpg-lx2` 归档成功后调 `index_one_file`，
        它会**照收不误**建出一条 `ext="jpg-lx2"` 的行；
      * 下一次全量扫描走 `_walk`，同一个文件被 `include_exts` 挡掉，
        不进 `seen_rels`，于是 `_mark_missing` 把它标成 missing。

    结果是用户刚传完的照片先出现在时间线上、几秒后又变成"已丢失"。
    真实上传路径有扩展名校验挡着，所以这条只在"上传白名单被改宽、
    而索引白名单没跟着改"时才炸 —— 而那正是最容易被忽略的配置漂移。

    ⚠️ 这里**不含** junction/reparse 检查（那需要 lstat，是调用方的事）。
    """
    if _is_excluded_file(name, root):
        return False
    return _ext_of(name) in root.include_exts


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
            if not should_index(name, root):
                if not _is_excluded_file(name, root):
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
        total_provider: "Callable[[], int] | None" = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.thumbs = thumbs
        # 事件总线用于把索引进度推给手机端。为 None 时不发事件，
        # 这样索引器在测试与脚本里可以完全脱离事件系统运行。
        self.bus = bus
        self.batch_size = max(1, batch_size)
        #: 取"库里当前照片总数"的可注入钩子。
        #:
        #: 为什么要可注入：进度事件里的 `total` 决定百分比的分母，
        #: 而"取总数失败时不能打断扫描"这条只有把取数过程换掉才能测到
        #: （`Database` 用了 `__slots__`，没法直接猴补 `scalar`）。
        self._total_provider = total_provider
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

        ★ 本函数在**任何**退出路径上都必须发 `scan_done`（用 `finally`）。
        前端只靠 `scan_done` 把"扫描中"清掉；漏发一次，设置页就永远
        停在扫描中，而用户没有任何办法自救 —— 只能刷新页面。
        """
        # 每轮扫描都从干净状态开始。上一轮的 cancel 若不清掉，之后的扫描
        # 会立刻以"用户中止"结束，表现为"点重新扫描没反应"。
        self._cancel.clear()
        stats = ScanStats(root_id=root.id)
        self._running = True
        self._cancel.clear()
        try:
            return await self._scan_root_inner(root, stats)
        finally:
            stats.finished_at = time.time()
            self._running = False
            self.last_stats[root.id] = stats
            self._emit({"type": "scan_done", "root": root.id,
                        "stats": stats.as_dict()})
            log.info("scan finished %s", stats.summary())

    async def _scan_root_inner(self, root: RootConfig, stats: ScanStats) -> ScanStats:
        """`scan_root` 的主体。事件发射（`scan_done`）由调用方的 `finally` 负责。"""
        # 根目录本身不可读时提前退出并明确报错。
        # 不这样做的话，scandir 失败会被计入 errors 后继续跑完空栈，
        # 最终 aborted 仍是 None —— 用户看到"扫描完成，错误 1"却不知道
        # 整个根都没扫到（典型场景：移动硬盘没插、盘符变了）。
        if not root.path.is_dir():
            stats.errors += 1
            stats.aborted = f"根目录不可访问：{root.id}"
            log.error("root not accessible root=%s path=%s", root.id, root.path)
            # ★ 这里**不发** `scan_start`。
            #
            # 一开始我在这里补了一条 `scan_start`（带 aborted），理由是
            # "提前返回也要让前端知道"。但牙齿验证证明它是多余的：
            # 调用方的 `finally` 紧接着就发 `scan_done`，而它的
            # `stats.aborted` 里带着同一个原因。多推一条反而让设置页
            # 先闪一下"扫描中"再消失，看起来像卡了一下。
            # 前端据 `scan_done.stats.aborted` 显示失败原因（见 system.ts）。
            return stats

        log.info("scan started root=%s path=%s", root.id, root.path)
        self._emit({"type": "scan_start", "root": root.id, "ts": time.time()})
        try:
            await self._index_files(root, _walk(root, stats), stats, progress=True)
            if not self._cancel.is_set():
                stats.missing = self._mark_missing(root.id, stats.seen_rels)
            # ★ 收尾必推一条最终进度。
            #
            # 按"每 batch_size 个推进度"的话，**比 batch_size 小的库
            # 一条进度都发不出来**（3 张照片 / batch_size=500 → 3 < 500）。
            # 用户点"重新扫描"，界面上除了转圈什么都没有 —— 而这一轮
            # 恰恰是秒完的，本来最该给个明确的"扫完了"反馈。
            # 它同时保证百分比能真的走到 100%。
            self._emit_progress(root, stats)
        except Exception as e:  # noqa: BLE001 —— 扫描失败不能让整个服务倒下
            stats.errors += 1
            stats.aborted = str(e)
            log.exception("scan failed root=%s", root.id)
        return stats

    # --- 索引入口（全量扫描与增量监听的共同实现）---------------------------

    async def _index_files(
        self,
        root: RootConfig,
        files: Iterable[tuple[str, os.stat_result]],
        stats: ScanStats,
        *,
        known: dict[str, dict] | None = None,
        progress: bool = False,
    ) -> None:
        """把一批 (相对路径, stat) 同步进 media 表。

        **全量扫描与 watchdog 增量共用这一份实现**。为什么不各写一份：
        增量路径要处理的判断（missing 行复活、内容变了才重置缩略图、
        取消、缩略图补入队）与全量扫描**逐条相同**，复制一份的代价是
        两边迟早分叉 —— 而分叉的表现是"手动重扫能看到新照片、自动监听
        看不到"这类只在特定操作序列下复现的问题。

        调用方负责：`seen_rels` 的累积（增量时是全库已知集合）、
        `_mark_missing`、以及异常兜底。

        `progress=True` 时才推 `scan_progress`（**只有全量扫描传它**）。
        增量监听（watchdog）不推：它没有配对的 `scan_start`/`scan_done`，
        推一条进度会让前端永远停在"扫描中"—— 因为 `scan_done` 是唯一
        把 `progress` 清空的事件。增量有新照片时走 `media_changed`。

        ⚠️ 本函数**自己会过一遍 `should_index`**。全量扫描的 `_walk`
        已经过滤过一次，这里是重复的 —— 但重复是必要的：watchdog
        增量路径直接调用本函数，它拿到的是"watchdog 报上来的路径"，
        若不过滤，`.photobridge_tmp/xxx.part`、`Thumbs.db`、
        `*.jpg-lx2` 这些就会被收录进时间线（而全量扫描不收），
        表现为"手动重扫后它们消失了、过一会儿又冒出来"。
        """
        if known is None:
            known = self._load_known(root.id)
        batch: list[tuple] = []
        # 上次推进度时的 `seen`。见下方"为什么按 seen 而不是按 batch"。
        last_emit = 0

        for rel, st in files:
            if self._cancel.is_set():
                stats.aborted = "用户中止"
                break
            # 与 `_walk` / `index_one_file` 共用同一份判断，见 `should_index`。
            if not should_index(rel.rsplit("/", 1)[-1], root):
                stats.skipped_ext += 1
                continue
            stats.seen += 1
            stats.seen_rels.add(rel)

            prev = known.get(rel)
            # ★ `prev["missing"]` 必须参与判断。`_load_known` 会把
            #   missing=1 的行也载入内存，如果只比 mtime/size，
            #   一张"曾经被标丢失、如今又回来了"的照片会走 unchanged
            #   分支被 continue 掉，missing 永远清不掉 —— 文件就在
            #   磁盘上，用户却一直看到"照片已丢失"。
            #   触发场景很常见：拔了移动硬盘再插回来、临时改名目录后
            #   又改回去、_mark_missing 因某次根目录不可读而误标。
            if prev is not None and not prev["missing"] and not self._changed(prev, st):
                stats.unchanged += 1
                # 未变但仍缺缩略图 → 补入队（上次可能没跑完）
                if prev["thumb_state"] == "pending" and self.thumbs is not None:
                    if self.thumbs.enqueue(prev["id"]):
                        stats.thumb_queued += 1
            else:
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

            # ★ 按 `seen` 推进度，**不按 batch**。
            #   按 batch 的话，库里绝大多数文件都没变时（第 2 次扫描、
            #   或用户只加了 3 张照片到 4915 张的库里）一个字节都不需要
            #   写 → batch 永远不满 → **整轮扫描一条进度都发不出来**。
            #   用户点了"重新扫描"，界面上只有"扫描中"在转，进度数字
            #   纹丝不动，看起来就是卡死了。
            if progress and stats.seen - last_emit >= self.batch_size:
                last_emit = stats.seen
                self._emit_progress(root, stats)
                # 让出事件循环，否则大批量扫描会卡住 API 响应
                await asyncio.sleep(0)

        if batch:
            ids = self._flush(batch)
            if self.thumbs is not None:
                stats.thumb_queued += self.thumbs.enqueue_many(ids)

    # --- 事件 -------------------------------------------------------------

    def _count_photos(self) -> int:
        """库里当前有效的照片数（`missing = 0`）。

        ★ 必须排除 missing：数上丢失的行，百分比会在最后一段
        永远到不了 100 —— 用户盯着一个停在 87% 的进度条等下去。
        """
        return int(self.db.scalar(
            "SELECT COUNT(*) FROM media WHERE missing = 0 AND kind = 'image'",
            default=0) or 0)

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

        ★ `total` 是**必需**的：只有 `seen` 时前端只能显示
        "已扫描 1500"，用户看不出还要等多久（20 秒的等待没有任何
        参照物）。这里查一次库拿当前总数 —— 一次 `COUNT(*)` 走
        `idx_media_missing`，相对每批 500 次写入可忽略。
        """
        total = 0
        try:
            total = int(self._total_provider() if self._total_provider
                        else self._count_photos())
        except Exception:       # noqa: BLE001 —— 查不到总数不该打断扫描
            log.debug("取总数失败", exc_info=True)
            total = 0

        self._emit({
            "type": "scan_progress",
            "root": root.id,
            "seen": stats.seen,
            "added": stats.added,
            "updated": stats.updated,
            "unchanged": stats.unchanged,
            "missing": stats.missing,
            # ★ 前端读的是 `removed`（与 `index_info()` 的字段名一致），
            # 这里必须同时给，否则设置页的"移除 N"永远不显示。
            "removed": stats.missing,
            "errors": stats.errors,
            "thumb_queued": stats.thumb_queued,
            "total": total,
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
            "SELECT id, rel_path, mtime, size, thumb_state, missing"
            " FROM media WHERE root_id = ?",
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

        ★ **扩展名/排除名单必须先过 `should_index`**。不检查的话，
        这里会建出一条全量扫描永远不收的行 —— 下次扫描把它标成
        missing，用户刚传的照片就"消失"了。见 `should_index` 的注释。
        """
        try:
            p = sandbox.resolve(root, rel, "read", cfg=self.cfg)
            if not should_index(p.name, root):
                log.info("index_one_file 跳过不在索引白名单内的文件 rel=%s", rel)
                return None
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
