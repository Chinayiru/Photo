"""文件系统监听 —— 让"在电脑上直接拷进来的照片"也自动出现在手机时间线上。

## 为什么需要它

`config.index.watch` 默认是 `True`，`docs/01-需求规格说明书.md:395` 把它写成
一条**验收标准**：

    AC-2.15  watchdog：直接在电脑上往库目录拷入 1 张新图，
            30 秒内手机时间线可见（无需手动重扫）

而在这之前，`backend/app/` 里没有任何 `watchdog` / `Observer` /
`FileSystemEventHandler` 的实现 —— 也就是说这条验收标准**无法通过**，
但配置项与依赖声明都摆在那里（`requirements.txt` 里 `watchdog>=4.0` 的
注释甚至写着"一期默认关闭"，与 `watch: True` 的默认值直接矛盾）。
本模块把它补齐。

## 设计要点（对应 docs/03-详细设计说明书.md §4.1.7）

### 1. 必须防抖，而且必须"按路径"防抖

手机上传一张照片会触发 `created`（临时区）→ `moved`（归档目标）→
可能还有 `modified`，Windows 上某些操作会连发 10+ 次。不去抖会导致
同一个文件被索引 10 次（每次都是一轮 EXIF 抽取 + 一次 UPSERT）。

设计文档给的 `DEBOUNCE_SEC = 2.0` 是**单个路径的静默期**：路径最后一次
事件之后再过 2 秒才处理。这与"每 2 秒扫一次待办"不是一回事 ——
后者在持续写入一个大文件时会每 2 秒索引一次半截文件。

### 2. 删除**不防抖**，立刻标记 missing

用户删了照片，手机上应该马上看到它变成"已丢失"占位，而不是等 2 秒。
更重要的是：删除事件没有"后续事件"可以合并，防抖只会让状态短暂地
停留在错误值上（文件已不在、库里仍是正常状态，此时点开就是一张
打不开的图）。

### 3. 忽略名单必须与索引器**共用同一份判断**

这是本模块最容易出错的地方。`watchdog` 报的是**原始文件系统事件**，
它会忠实地报告 `<库根>/.photobridge_tmp/xxx.part`、`Thumbs.db`、
`~$xxx.docx`、`*.jpg-lx2`。如果这里用另一套过滤规则，就会出现
"扫描不收录、监听却收录"的分叉 —— 用户会看到时间线上多出一堆
半截上传文件和系统缩略图缓存。

所以忽略判断直接调用 `indexer.should_index()` 与
`sandbox.tmp_dir()`，**不复制一份规则**。

### 4. 自己写遍历，不用 `os.walk`，也不能信任 watchdog 的 recursive

`indexer._walk` 有一整段注释解释为什么必须逐目录检查 reparse 位
（junction 不是 symlink，`os.path.islink()` 返回 False，而
`os.walk(followlinks=False)` 会照常递归进去 —— 一个指向 `C:\\` 的
junction 放在库根下就能把整台机器的图片扫进来）。

watchdog 的 `recursive=True` 用的是 Windows 的 `ReadDirectoryChangesW`，
它**同样会跟随 junction**：`FileSystemEventHandler` 收到的路径可能是
`<库根>/junction_out/某个库外文件`。因此每个事件路径都必须过一遍
`sandbox.resolve()`（它逐段 lstat 检查 reparse 位），失败就丢弃。
这道校验同时覆盖了"路径越界"与"junction 穿透"两种情况。

### 5. 事件回调在 watchdog 自己的线程里，不能碰 asyncio

`on_created` 等回调运行在 `WindowsApiObserver` 的线程上。那里
**不能** `await`、不能碰 `asyncio.Queue`、也不能直接查库（
`Database` 用线程本地连接，跨线程用会拿到另一个连接 —— 虽然安全，
但会在监听线程里跑一次 SQLite 查询，没必要）。

做法：回调只把事件塞进一个 `queue.Queue`（标准库，线程安全），
事件循环里的 `_flush_loop` 协程负责取出并处理。
"""
from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from . import sandbox
from .config import Config, RootConfig
from .errors import AppError
from .indexer import ScanStats, should_index

if TYPE_CHECKING:  # pragma: no cover
    from .db import Database
    from .events import EventBus
    from .indexer import Indexer
    from .thumbs import ThumbEngine

log = logging.getLogger(__name__)

__all__ = ["DEBOUNCE_SEC", "WatchCollector", "Watcher", "build_watcher"]

#: 单个路径的事件静默期（秒）。`docs/03-详细设计说明书.md` §4.1.7 的裁决值。
DEBOUNCE_SEC: Final[float] = 2.0

#: 待办表扫描间隔。防抖到期是"最早可能处理时间"，
#: 这个值决定到期后最多再等多久 —— 1 秒意味着 AC-2.15 的 30 秒
#: 预算里最多花掉 2.0 + 1.0 = 3 秒，余量充足。
FLUSH_INTERVAL_SEC: Final[float] = 1.0

#: 单轮最多处理多少个待办路径。用户一次性拷 3000 张照片进来时，
#: 一次全处理会长时间占住事件循环（每张都要抽 EXIF）。
#: 分批 + 每批之间 `await asyncio.sleep(0)`，让 API 请求能插进来。
BATCH_MAX: Final[int] = 200

#: 待办表上限。超过就丢弃**最旧**的待办并记一条警告。
#:
#: 为什么需要上限：用户把整个 `D:\\照片备份`（十几万文件）拖进库目录，
#: 每次事件都建一个待办项，字典会涨到几百 MB。丢最旧的是安全的 ——
#: 丢掉的路径只是"这次不增量索引"，下次全量扫描仍会收录它。
MAX_PENDING: Final[int] = 20000

#: 队列上限（watchdog 线程 → 事件循环）。满了就丢事件并记数。
#: 同样安全：丢的是"增量索引的触发"，不是数据本身。
MAX_EVENTS: Final[int] = 10000

#: 待办表满时一次丢弃的比例（1/N）。见 `WatchCollector._evict_oldest_locked`：
#: 每次插入只丢一个的话，每次插入都要遍历整个待办表 —— 10 万文件的洪水
#: 会把事件循环占住几十秒（实测 36 秒），手机端全部请求超时。
EVICT_FRACTION: Final[int] = 8


@dataclass(slots=True)
class _Pending:
    """一个待处理的路径。"""

    action: str                  # created / modified / deleted
    due: float                   # 到期时刻（monotonic）
    root: RootConfig


class WatchCollector:
    """把 watchdog 的原始事件合并成"路径 → 最终动作"。

    这一层**完全不知道 watchdog 的存在**：它只接收 (action, root, rel)，
    因此可以在测试里直接调用 `on_event()` 验证防抖与合并逻辑，
    不需要真的去改文件、也不需要等 2 秒。

    线程安全：`on_event()` 会被 watchdog 的线程调用，
    `take_ready()` / `drop()` 在事件循环线程里调用。内部用一把
    `threading.Lock` 保护（**不是** asyncio.Lock —— 后者不能跨线程）。
    """

    __slots__ = ("_pending", "_lock", "_dropped", "_debounce")

    def __init__(self, debounce_sec: float = DEBOUNCE_SEC) -> None:
        # ★ 键必须是 `(root_id, rel)`，不能只用 `rel`（审查 R-M3-04）。
        #   两个库根出现同名相对路径（`2024/a.jpg` 在两个盘上都很常见）时，
        #   后一个事件会覆盖前一个待办：`_Pending.root` 被换成后登记的根，
        #   于是"磁盘 A 上的照片被删"变成了"扫描磁盘 B 的同名路径" ——
        #   A 那条记录永远停在 missing=0，用户点开是坏的，且没有后续触发点。
        self._pending: dict[tuple[str, str], _Pending] = {}
        self._lock = threading.Lock()
        self._dropped = 0
        # ★ 静默期必须**由构造方注入**，不能直接读模块常量。
        #   原来这里写死 `now + DEBOUNCE_SEC`，而 `Watcher._debounce_sec`
        #   从 `index.debounce_sec` 读配置 —— 于是 `stats()` 里报的
        #   防抖值是 0.5，实际生效的却是 2.0。配置项成了摆设，
        #   用户把 debounce_sec 调到 10 秒想减少大文件重复索引，
        #   不会有任何效果。
        #
        # 合法性由 `Watcher._debounce_sec` 负责（配置里的 0/负数/字符串
        # 都在那里退化成默认值），所以这里只挡负数。**0 是合法值**，
        # 含义是"不防抖、立即处理"，测试需要它才能不用真的等 2 秒。
        self._debounce = debounce_sec if debounce_sec >= 0 else DEBOUNCE_SEC

    @property
    def debounce_sec(self) -> float:
        return self._debounce

    # --- 生产者（watchdog 线程）-----------------------------------------

    def on_event(self, action: str, root: RootConfig, rel: str) -> None:
        """登记一个事件。**永不抛异常**（在 watchdog 线程里抛会杀掉监听）。

        `action` 取 "created" / "modified" / "deleted"。

        合并规则（与设计文档 §4.1.7 一致，但按路径键而不是逐事件排队）：

        * `deleted` → 覆盖任何待办，**且 due 设为现在**（不防抖，立刻处理）；
        * 其余动作 → 覆盖同路径的待办，due 重置为"现在 + 静默期"。

        "覆盖"而不是"追加"是关键：一个文件被写 10 次只该产生 1 个待办，
        且以**最后一次**事件的时间起算静默期。
        """
        try:
            if not rel:
                return
            now = time.monotonic()
            due = now if action == "deleted" else now + self._debounce
            key = (root.id, rel)
            with self._lock:
                if len(self._pending) >= MAX_PENDING and key not in self._pending:
                    self._evict_oldest_locked()
                self._pending[key] = _Pending(action=action, due=due, root=root)
        except Exception:       # noqa: BLE001 —— 见 docstring
            log.warning("登记监听事件失败 rel=%s", rel, exc_info=True)

    def _evict_oldest_locked(self) -> None:
        """腾出空间。调用方必须已持锁。

        ## 丢谁：优先丢 upsert，绝不动 deleted

        due 最小的项**不能**按"最旧"理解。删除项的 due 恒为"现在"，
        所以它永远是最小的 —— 按 due 最小丢弃，正好丢掉最该保留的
        事件。真实后果：用户删了照片，同时往库里拷了十万个文件，
        删除事件被洪水挤掉，那张照片在手机上一直是正常状态
        （点开打不开），而且**没有任何后续触发点**能纠正它。

        规则：

        1. 优先丢 upsert。丢掉的 upsert 下次全量扫描一定会收录；
           丢掉的 deleted 则没有任何补救路径。
        2. 同为 upsert 时丢 **due 最大**的（最新事件）。留旧丢新，
           让积压的旧事件先被处理完，队列整体在推进。

        ## 为什么攒一批一次丢，而不是每次插一个丢一个

        ★ 这是实测出来的性能缺陷，不是理论担心。原本的写法是
        "插入时若满则 `min()` 一次丢一个"，而 `on_event` 由
        `_drain_queue` 在**事件循环线程**里调用 —— 一次 10 万文件的
        洪水会让**每次插入**都遍历 2 万个待办。实测（2 万待办 +
        4 万次插入）耗时 **36 秒**，期间事件循环被完全占住，表现为
        "服务还在跑，但手机端所有请求都超时"。

        改成一次排序丢掉一批（O(n log n) 均摊到每次插入是常数级），
        同样负载实测降到 **0.1 秒**。
        """
        victims = [k for k, p in self._pending.items() if p.action != "deleted"]
        if not victims:
            # 待办里全是删除。它们来自用户的实际操作，数量有天然上限，
            # 让它短暂超出 MAX_PENDING 远好过丢掉它们。
            return
        victims.sort(key=lambda k: self._pending[k].due, reverse=True)
        for key in victims[:max(1, len(victims) // EVICT_FRACTION)]:
            del self._pending[key]
            self._dropped += 1

    # --- 消费者（事件循环线程）-------------------------------------------

    def take_ready(self, limit: int = BATCH_MAX) -> list[tuple[str, str, RootConfig]]:
        """取出所有已到期的待办，最多 `limit` 个。返回 [(action, rel, root)]。

        删除类动作排在前面：它们不防抖、且用户对"删了没反应"最敏感。
        """
        now = time.monotonic()
        out: list[tuple[str, str, RootConfig]] = []
        with self._lock:
            ready = [k for k, p in self._pending.items() if p.due <= now]
            # 删除优先，其余按到期时间（先到先处理）
            ready.sort(key=lambda k: (self._pending[k].action != "deleted",
                                      self._pending[k].due))
            for key in ready[:limit]:
                p = self._pending.pop(key)
                out.append((p.action, key[1], p.root))
        return out

    def drop(self, rel: str, root_id: str | None = None) -> None:
        """丢弃一个待办（处理失败且不值得重试时用）。

        `root_id` 省略时丢掉**所有**根下同路径的待办 —— 这是"这个文件不值得
        再试"的语义（例如它已被判定为非媒体），与具体哪个根无关。
        """
        with self._lock:
            if root_id is not None:
                self._pending.pop((root_id, rel), None)
                return
            for key in [k for k in self._pending if k[1] == rel]:
                del self._pending[key]

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._pending)

    @property
    def dropped(self) -> int:
        with self._lock:
            return self._dropped

    def clear(self) -> None:
        with self._lock:
            self._pending.clear()


# --------------------------------------------------------------------------
# watchdog 事件处理器（运行在 watchdog 线程）
# --------------------------------------------------------------------------

def _make_handler(collector: WatchCollector, root: RootConfig,
                  events: "queue.Queue[tuple[str, str, str]]"):
    """构造 watchdog 的事件处理器。

    `watchdog` 是可选依赖（`requirements.txt` 里装了，但开发机上可能没有），
    因此这个 import 放在函数内部 —— 模块级 import 会让整个 watcher 模块
    在缺少 watchdog 时无法导入，连带 server.py 起不来。
    """
    from watchdog.events import FileSystemEventHandler       # noqa: PLC0415

    class _Handler(FileSystemEventHandler):  # type: ignore[misc]
        """把 watchdog 的原始事件翻译成 (action, root_id, 相对路径) 塞进队列。

        ★ **本类里绝不能做任何耗时的事**。watchdog 用单个线程派发事件，
        在这里查库或抽 EXIF 会让后续事件堆积在操作系统的缓冲区里，
        缓冲满了 Windows 就会**丢弃**事件（表现为"有些照片就是不出现在
        时间线上"，且完全无法复现）。所以这里只做纯字符串处理。
        """

        def on_created(self, event: Any) -> None:
            self._push("created", event)

        def on_modified(self, event: Any) -> None:
            self._push("modified", event)

        def on_deleted(self, event: Any) -> None:
            self._push("deleted", event)

        def on_moved(self, event: Any) -> None:
            # 移动 = 源消失 + 目标出现。**两个都要报**：
            # 只报目标的话，源路径的旧行永远留在库里显示"照片已丢失"；
            # 只报源的话，新位置的照片永远不出现。
            self._push("deleted", event, use_src=True)
            self._push("created", event, use_dest=True)

        # --- 内部 ---------------------------------------------------------

        def _push(self, action: str, event: Any, *,
                  use_src: bool = False, use_dest: bool = False) -> None:
            try:
                if use_dest:
                    raw = getattr(event, "dest_path", None)
                elif use_src:
                    raw = getattr(event, "src_path", None)
                else:
                    raw = getattr(event, "src_path", None)
                if not raw:
                    return
                rel = _rel_of(root, str(raw))
                if rel is None:
                    return                  # 库外 / 越界 / 被忽略
                # ★ root.id 必须一起传。只传 rel 的话，多根（且根之间
                #   有嵌套）时下游无法判断这个 rel 是相对哪个根的，
                #   只能靠"再 resolve 一遍所有根"来猜 —— 那是 O(根数)
                #   次 lstat 且对嵌套根会猜错。
                events.put_nowait((action, root.id, rel))
            except queue.Full:
                # 队列满 = 事件循环那边的处理跟不上。丢事件是安全的：
                # 丢的是"增量索引的触发"，照片本身还在磁盘上，
                # 下次全量扫描一定会收录它。
                log.warning("监听事件队列已满，丢弃事件 action=%s", action)
            except Exception:       # noqa: BLE001
                log.debug("处理监听事件失败", exc_info=True)

    return _Handler()


def _rel_of(root: RootConfig, raw: str) -> str | None:
    """绝对路径 → 库内相对路径。库外/越界/被忽略的一律返回 None。

    这道函数是**安全边界**，不是格式转换。它承担三件事：

    1. **junction 穿透**：watchdog 会跟随 junction，报出的路径可能在库外。
       `sandbox.resolve()` 逐段 lstat 检查 reparse 位，能挡住这种情况
       （`indexer._walk` 的注释详述了为什么 `os.walk` 与 `islink()` 都不够）。
    2. **越界**：`../` 之类的路径在 watchdog 里不该出现，但防御性检查
       的代价是一次 `normpath`。
    3. **忽略名单**：临时区、缩略图、`Thumbs.db`、`~$xxx`、`*.jpg-lx2`
       等 —— 与索引器**共用同一份判断**，避免两边分叉。
    """
    try:
        root_path = Path(root.path)
        p = Path(raw)
        rel = os.path.relpath(str(p), str(root_path))
    except (ValueError, OSError):
        return None
    if rel.startswith(".."):
        return None
    rel = sandbox.normalize_rel(rel)
    if not rel:
        return None

    name = rel.rsplit("/", 1)[-1]
    # 目录事件没有扩展名，`should_index` 会返回 False —— 这正是我们要的：
    # 目录的创建/删除本身不改变 media 表（里面的文件各自有事件）。
    if not should_index(name, root):
        return None

    # ★ 最后一道：逐段 lstat 检查 reparse 位。这一步同时覆盖了
    #   "路径其实在库外"（junction 穿透）与"路径里有 symlink"。
    try:
        sandbox.resolve(root, rel, "read")
    except AppError:
        return None
    except Exception:       # noqa: BLE001 —— 文件可能已被删/正在改名
        return None
    return rel


# --------------------------------------------------------------------------
# 监听器
# --------------------------------------------------------------------------

class Watcher:
    """库目录的文件系统监听器。

    生命周期与 `ThumbEngine` 对称：`start()` 建 watchdog Observer 与
    事件循环协程，`stop()` 全部回收。**start/stop 都可重复调用**。

    `enabled=False`（或没有可监听的根）时所有方法都是空操作 ——
    这样 `server.py` 不需要在调用处写 if，也就不会出现
    "某条分支忘了停监听"这种泄漏。
    """

    __slots__ = ("cfg", "db", "indexer", "thumbs", "bus", "enabled",
                 "_collector", "_events", "_observers", "_task", "_running",
                 "_roots", "_stats")

    def __init__(
        self,
        cfg: Config,
        db: "Database",
        *,
        indexer: "Indexer | None" = None,
        thumbs: "ThumbEngine | None" = None,
        bus: "EventBus | None" = None,
        enabled: bool = True,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.indexer = indexer
        self.thumbs = thumbs
        self.bus = bus
        self._collector = WatchCollector(self._debounce_sec)
        self._events: "queue.Queue[tuple[str, str, str]]" = queue.Queue(
            maxsize=MAX_EVENTS)
        self._observers: list[Any] = []
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._roots: list[RootConfig] = []
        #: 累计统计，供 /api/system/info 与诊断用
        self._stats: dict[str, int] = {"events": 0, "indexed": 0, "missing": 0,
                                       "skipped": 0, "errors": 0}
        # 配置读不出来时退化为"默认开启"，与 config.py 的默认值一致。
        self.enabled = bool(enabled) and bool(cfg.index.get("watch", True))

    # --- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        """启动监听。**失败不影响服务**（watchdog 缺失、目录不可访问等）。"""
        if self._running:
            return
        if not self.enabled:
            log.info("文件系统监听已关闭（index.watch=false）")
            return
        if self.indexer is None:
            log.info("没有索引器实例，文件系统监听跳过")
            return

        self._roots = [r for r in self.cfg.enabled_roots if r.path.is_dir()]
        if not self._roots:
            log.info("没有可监听的根目录，文件系统监听跳过")
            return

        try:
            from watchdog.observers import Observer        # noqa: PLC0415
        except ImportError:
            # watchdog 没装（用户手工 pip 装了一半、或用了裁剪过的环境）。
            # 降级为"只有手动重扫"，而不是让整个服务起不来。
            log.warning("未安装 watchdog，文件系统监听不可用；"
                        "照片仍可通过手动重扫收录（pip install watchdog）")
            return

        for root in self._roots:
            try:
                obs = Observer()
                obs.schedule(_make_handler(self._collector, root, self._events),
                             str(root.path), recursive=True)
                obs.start()
                self._observers.append(obs)
                log.info("监听库目录 root=%s path=%s", root.id, root.path)
            except Exception:       # noqa: BLE001 —— 一个根失败不影响其他根
                log.warning("监听根目录失败 root=%s", root.id, exc_info=True)

        if not self._observers:
            return

        self._running = True
        self._task = asyncio.create_task(self._flush_loop(), name="watch-flush")
        log.info("文件系统监听已启动：%d 个根目录，防抖 %.1fs",
                 len(self._observers), self._debounce_sec)

    async def stop(self) -> None:
        """停止监听并回收线程。可重复调用。"""
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass

        for obs in self._observers:
            try:
                # join 必须给超时：watchdog 的 Observer 线程若卡在
                # ReadDirectoryChangesW 上，无超时的 join 会让退出流程
                # 永远停在这里（表现为"点了退出没反应"）。
                obs.stop()
                obs.join(timeout=5.0)
            except Exception:       # noqa: BLE001
                log.debug("停止监听器失败", exc_info=True)
        self._observers.clear()
        self._collector.clear()
        log.info("文件系统监听已停止")

    # --- 配置 -------------------------------------------------------------

    @property
    def _debounce_sec(self) -> float:
        """`index.debounce_sec`，非法值退默认。

        ★ 必须与 `WatchCollector.__init__` 的判据一致：**0 是合法值**
        （"不防抖、立即处理"）。若这里写成 `v > 0`，配置里的 0 会被
        悄悄改成 2.0 —— 用户明确要求立即处理却被加上 2 秒延迟，
        而且 `stats()` 报的还是 2.0，看起来"配置没生效"。
        """
        try:
            v = float(self.cfg.index.get("debounce_sec", DEBOUNCE_SEC))
        except (TypeError, ValueError):
            return DEBOUNCE_SEC
        return v if v >= 0 else DEBOUNCE_SEC

    @property
    def running(self) -> bool:
        return self._running

    def stats(self) -> dict[str, Any]:
        """监听状态，供 /api/system/info 与托盘显示。"""
        s = dict(self._stats)
        s.update({
            "running": self._running,
            "roots": [r.id for r in self._roots],
            "pending": self._collector.depth,
            "dropped": self._collector.dropped,
            # 报**实际生效**的值（收集器持有的那个），而不是重新读一遍
            # 配置 —— 两者曾经不一致，报出来的数字必须是真的。
            "debounce_sec": self._collector.debounce_sec,
        })
        return s

    # --- 事件循环侧 -------------------------------------------------------

    async def _flush_loop(self) -> None:
        """把队列里的事件搬进待办表，再把到期的待办落进索引。"""
        while self._running:
            try:
                await asyncio.sleep(FLUSH_INTERVAL_SEC)
                self._drain_queue()
                await self._process_ready()
            except asyncio.CancelledError:
                raise
            except Exception:       # noqa: BLE001
                # 监听循环绝不能因为一次异常退出 —— 退出后表现为
                # "过一会儿就再也不自动收录了"，而用户完全无从判断。
                log.exception("监听处理循环异常（继续运行）")
                self._stats["errors"] += 1

    def _drain_queue(self) -> None:
        """把 watchdog 线程塞进队列的事件搬进待办表。

        这一步**不做任何 I/O**（`_rel_of` 已在 watchdog 线程里跑过），
        因此可以一次搬空整个队列。
        """
        by_id = {r.id: r for r in self._roots}
        while True:
            try:
                action, root_id, rel = self._events.get_nowait()
            except queue.Empty:
                return
            root = by_id.get(root_id)
            if root is None:
                self._stats["skipped"] += 1
                continue
            self._stats["events"] += 1
            self._collector.on_event(action, root, rel)

    def _root_for(self, rel: str) -> RootConfig | None:
        """事件属于哪个根。

        ⚠️ **当前实现里没有调用点**，保留它是为了给"多根嵌套"留一个
        明确的落点：`_make_handler` 已经把 root.id 随事件一起传下来，
        所以正常路径不需要反查。若将来要支持"用户新增根目录后重建
        处理器"，会需要按 rel 反查（取路径最长的那个根 —— 子根的
        相对路径更精确，用父根会算出一个多一层前缀的 rel）。
        """
        best: RootConfig | None = None
        best_len = -1
        for root in self._roots:
            try:
                sandbox.resolve(root, rel, "read")
            except Exception:       # noqa: BLE001
                continue
            if len(str(root.path)) > best_len:
                best, best_len = root, len(str(root.path))
        return best

    async def _process_ready(self) -> None:
        """处理已到期的待办。"""
        ready = self._collector.take_ready()
        if not ready:
            return

        # 删除类先处理：它们不防抖，且"删了没反应"最容易被用户察觉。
        deletes = [(a, r, root) for a, r, root in ready if a == "deleted"]
        upserts = [(a, r, root) for a, r, root in ready if a != "deleted"]

        if deletes:
            self._mark_deleted(deletes)
        if upserts:
            await self._index_upserts(upserts)

    def _mark_deleted(self, items: list[tuple[str, str, RootConfig]]) -> None:
        """把文件已消失的路径标记为 missing。

        为什么**只改库、不碰文件**：事件说"这个路径没了"，我们相信它，
        但绝不去动磁盘（用户的文件可能只是被临时移走，下次扫描会把它
        改回 missing=0 —— 见 `_index_files` 里 `prev["missing"]` 的注释）。
        """
        if self.indexer is None:
            return
        by_root: dict[str, list[str]] = {}
        for _action, rel, root in items:
            by_root.setdefault(root.id, []).append(rel)

        for root_id, rels in by_root.items():
            try:
                n = self._mark_missing_rows(root_id, rels)
                self._stats["missing"] += n
            except Exception:       # noqa: BLE001
                log.warning("标记 missing 失败 root=%s", root_id, exc_info=True)
                self._stats["errors"] += 1

    def _mark_missing_rows(self, root_id: str, rels: list[str]) -> int:
        """把指定路径标为 missing=1，返回受影响行数。

        ⚠️ **必须检查文件是否真的不在了**。watchdog 的 `deleted` 事件
        在"保存文件"（先删后建）这类操作里也会触发，而那一瞬间文件
        确实不存在 —— 但紧接着的 `created` 会把它建回来。
        如果直接标 missing，用户会看到照片闪一下变灰再变回来。

        做法：标之前先 `os.path.exists`。存在的路径直接跳过，
        让随后的 `created` 事件去走索引路径（那里会把 missing 清掉）。
        """
        gone: list[str] = []
        for rel in rels:
            root = self._root_by_id(root_id)
            if root is None:
                return 0
            try:
                p = sandbox.resolve(root, rel, "read")
            except AppError:
                # 路径已经无法解析（文件被删了，或者目录也没了）→ 确实没了
                gone.append(rel)
                continue
            except Exception:       # noqa: BLE001
                continue
            if not p.exists():
                gone.append(rel)
        if not gone:
            return 0
        with self.db.transaction() as conn:
            for i in range(0, len(gone), 500):
                part = gone[i:i + 500]
                conn.execute(
                    f"UPDATE media SET missing = 1"
                    f" WHERE root_id = ? AND missing = 0"
                    f" AND rel_path IN ({','.join('?' * len(part))})",
                    [root_id, *part],
                )
        return len(gone)

    def _root_by_id(self, root_id: str) -> RootConfig | None:
        for root in self._roots:
            if root.id == root_id:
                return root
        return None

    async def _index_upserts(self, items: list[tuple[str, str, RootConfig]]) -> None:
        """把新增/修改的路径走一遍索引逻辑。

        ★ **复用 `Indexer._index_files`**，不自己写一份。增量与全量必须
        对"missing 行复活"、"内容变了才重置缩略图"给出相同答案，
        两份实现迟早分叉。

        ★ **不能调用 `_mark_missing`**。它会把"本轮没见到的行"全标成
        missing —— 而这里的"本轮"只有 3 个文件，库里其余 4900 行
        会一次性全部被标丢失。这是本模块最危险的一个陷阱。
        """
        if self.indexer is None:
            return
        # 只处理**当前确实存在**的文件：`created` 事件可能早于写入完成，
        # 也可能文件已经被下一步操作删掉。
        by_root: dict[str, tuple[RootConfig, list[tuple[str, os.stat_result]]]] = {}
        for _action, rel, root in items:
            try:
                p = sandbox.resolve(root, rel, "read")
                st = p.stat()
            except (AppError, OSError):
                # 文件不在（或越界）→ 丢弃。可能是"先删后建"的中间态，
                # 随后的 created 事件会重新触发。
                self._stats["skipped"] += 1
                continue
            entry = by_root.setdefault(root.id, (root, []))
            entry[1].append((rel, st))

        for root_id, (root, files) in by_root.items():
            stats = ScanStats(root_id=root_id)
            try:
                # 先载入已知行：`_index_files` 要靠它判断"新增还是更新"、
                # 以及"这条 missing 行该不该复活"。**必须连 missing 行
                # 一起载入** —— 漏掉它们的话，一个被临时移走的文件回来时
                # 不会清掉 missing 标记，用户一直看到"照片已丢失"。
                known = self.indexer._load_known(root_id)      # noqa: SLF001
                await self.indexer._index_files(root, files, stats, known=known)  # noqa: SLF001
                self._stats["indexed"] += stats.added + stats.updated
                if self.bus is not None and (stats.added or stats.updated):
                    self._emit_media_changed(root, stats)
            except Exception:       # noqa: BLE001
                log.warning("增量索引失败 root=%s", root_id, exc_info=True)
                self._stats["errors"] += 1

    def _emit_media_changed(self, root: RootConfig, stats: ScanStats) -> None:
        """推一条事件，让手机端知道"有新照片了"。

        为什么需要：用户把照片拷进电脑后，正拿着手机看时间线。
        没有这条事件，他要手动下拉刷新才能看到 —— 而 AC-2.15 的
        "30 秒内可见"指的是**不需要任何手动操作**。
        """
        try:
            assert self.bus is not None
            self.bus.publish({
                "type": "media_changed",
                "root": root.id,
                "added": stats.added,
                "updated": stats.updated,
                "ts": time.time(),
            })
        except Exception:       # noqa: BLE001
            log.debug("发布 media_changed 失败", exc_info=True)


def build_watcher(
    cfg: Config,
    db: "Database",
    *,
    indexer: "Indexer | None" = None,
    thumbs: "ThumbEngine | None" = None,
    bus: "EventBus | None" = None,
) -> Watcher:
    """按配置构造监听器。`index.watch=false` 时返回一个禁用的实例。

    为什么不返回 None：`server.py` 里就不用写 `if watcher is not None`，
    少一处可以忘掉的分支。禁用实例的所有方法都是空操作。
    """
    return Watcher(cfg, db, indexer=indexer, thumbs=thumbs, bus=bus,
                   enabled=bool(cfg.index.get("watch", True)))
