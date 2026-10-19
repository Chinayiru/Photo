"""隧道多路复用器 —— 云端 Relay 与电脑端 Agent 共享的**同一份实体**。

## 为什么放在 `shared/` 而不是各写一份

`docs/09` §9.6 说得很明白：`protocol.py` 是两端共享的同一份物理文件，
所以"帧格式不存在漂移的窗口期"。`req_id` 分配、`pending` 表、信用窗口、
超时清扫这些**状态机**比帧格式更容易漂移——两端各写一份的话，会出现
"云端认为窗口是连接级、电脑端认为是请求级"这种只在压力下才暴露的 bug
（`docs/04` §2.6 专门用一整节纠正了这个歧义）。

因此本模块与 `protocol.py` 一样：**纯 asyncio、无 I/O、无 WebSocket 依赖**，
两端的传输实现只提供一个 `sender(frame_bytes)` 协程和一个 `feed(frame)` 入口。

## 角色对称

同一个 `Mux` 同时支持两个方向：

* **发起侧**（云端转手机请求 / 电脑端主动请求）：`begin()` → `wait_head()`
  → `iter_body()`。响应体用 `asyncio.Queue` 而不是 bytes，所以 2 GB 下载
  不会被缓冲（`docs/04` §2.3.5 的硬要求）。
* **响应侧**（电脑端处理 REQ_*）：`on_request` 回调 → `res_head()` /
  `res_body()` / `res_end()`。

## 信用（CREDIT）语义与 `docs/04` 的差异

`docs/04` §2.3.10 规定 CREDIT 帧带 12 字节 payload（window/consumed/flags），
但 `shared/protocol.py` 把 CREDIT 放进 `FIXED_LENGTH_TYPES`
（`backend/tests/test_protocol.py:279` 断言带 payload 必须报错）。
两边冲突时以**已冻结并被 68 条用例钉住的协议文件**为准：本模块的 CREDIT
是空 payload，语义是"**把对端发送信用重置回满窗口**"。

这不会放大内存：接收方只有在**已消费 ≥ 窗口一半**时才回 CREDIT，所以
发送方最多多拿到半个窗口的额度（8 MB → 峰值 ≤ 12 MB，仍远低于
`docs/01` AC-1.4 的 200 MB 上限）。少掉的只是"精确到字节的对账"，
而那个数字在排障时由 `bytes_in/bytes_out` 统计提供，不靠 CREDIT。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncIterator, Awaitable, Callable, Sequence

from .protocol import (
    CONN_CREDIT_WINDOW,
    CREDIT_DEADLOCK_TIMEOUT,
    CREDIT_THRESHOLD,
    CREDIT_WINDOW,
    HEAD_TIMEOUT,
    IDLE_TIMEOUT,
    MAX_FRAME,
    SWEEP_INTERVAL,
    TOTAL_TIMEOUT,
    Frame,
    FrameType,
    ProtocolError,
    TooManyRequests,
    encode_frame,
)

__all__ = [
    "MAX_ACTIVE",
    "MAX_BODY_CHUNK",
    "InboundRequest",
    "Mux",
    "MuxClosed",
    "PendingRequest",
    "ReqIdAllocator",
]

log = logging.getLogger(__name__)

#: 同时活跃的 req_id 上限（`docs/03` §2.5.2）。30 位空间是 10 亿，
#: 限到 4096 是为了让"对端开了几万个请求不释放"这种错误尽早暴露成
#: 一个 429，而不是把内存慢慢吃光。
MAX_ACTIVE: int = 4096

#: 单帧 BODY 的 payload 上限。比 `MAX_FRAME` 小 6 字节，留出帧头余量，
#: 让"payload + 5 字节头"恰好不超过 `MAX_FRAME` —— `docs/04` §2.3.2
#: 写的 65530 就是这个数的十进制（65536 - 6）。
MAX_BODY_CHUNK: int = MAX_FRAME - 6

#: 响应侧 BODY 队列深度。刻意留得很浅：队列只是"读取协程"与"写手机
#: 协程"之间的交接点，真正的背压由 CREDIT 窗口施加。队列越深，
#: 内存峰值越大而吞吐不变。
_BODY_QUEUE_MAX = 64


class MuxClosed(Exception):
    """连接已关闭，不能再发起请求。"""


def now() -> float:
    return time.monotonic()


# --------------------------------------------------------------------------
# req_id 分配
# --------------------------------------------------------------------------


class ReqIdAllocator:
    """`docs/03` §2.5.2：单调递增 +1，回绕到 1，跳过仍在活跃的 id。

    为什么不用随机/哈希：`req_id` 会出现在日志与抓包里，单调递增让
    "第 3 个请求丢了"这类问题一眼能看出来。为什么跳过活跃 id：
    30 位空间要 10 亿次请求才会回绕，但回绕那一刻若撞上长连接上的
    大文件下载，帧就会串到别人的响应里——症状是"照片内容对不上"。
    """

    __slots__ = ("_next", "_active", "_max_active")

    def __init__(self, max_active: int = MAX_ACTIVE) -> None:
        self._next = 1
        self._active: set[int] = set()
        self._max_active = max_active

    @property
    def active(self) -> int:
        return len(self._active)

    def alloc(self) -> int:
        if len(self._active) >= self._max_active:
            raise TooManyRequests(
                f"活跃请求数已达上限 {self._max_active}，请稍后重试")
        start = self._next
        rid = start
        while True:
            if rid not in self._active:
                self._active.add(rid)
                # 0 是保留值（连接级 CREDIT/心跳），回绕时跳过。
                self._next = rid + 1 if rid < 0x3FFFFFFF else 1
                return rid
            rid = rid + 1 if rid < 0x3FFFFFFF else 1
            if rid == start:      # pragma: no cover —— 需要 4096 个连续占用
                raise TooManyRequests("req_id 空间已耗尽")

    def release(self, rid: int) -> None:
        self._active.discard(rid)


# --------------------------------------------------------------------------
# 信用窗口（连接级，所有 req_id 共享）
# --------------------------------------------------------------------------


class _CreditGate:
    """发送侧信用 + 接收侧回补。

    ★ 窗口是**连接级**的（`docs/04` §2.6 的关键澄清）。按 req_id 各给
    8 MB 的话，30 个并发缩略图 = 240 MB，1 GB 内存的 VPS 直接 OOM——
    与"流控是为了省内存"的设计意图完全相反。
    """

    __slots__ = ("_window", "_credit", "_cond", "_recv_unacked",
                 "_last_progress", "_stalled", "_sent_total", "_recv_total")

    def __init__(self, window: int = CREDIT_WINDOW) -> None:
        self._window = int(window)
        self._credit = int(window)
        self._cond = asyncio.Condition()
        self._recv_unacked = 0
        self._last_progress = now()
        self._stalled = False
        self._sent_total = 0
        self._recv_total = 0

    @property
    def credit(self) -> int:
        return self._credit

    @property
    def sent_total(self) -> int:
        return self._sent_total

    @property
    def recv_total(self) -> int:
        return self._recv_total

    async def take(self, n: int) -> None:
        """等到至少有 `n` 字节额度再扣除。

        `n` 恒 ≤ `MAX_BODY_CHUNK`（65530），远小于窗口，所以"等整块"
        不会把自己饿死。等的过程中若超过 `CREDIT_DEADLOCK_TIMEOUT`
        没有收到任何 CREDIT，判为对端卡死 —— 这正是 `E5010`。
        """
        if n <= 0:
            return
        async with self._cond:
            while self._credit < n:
                if self._stalled:
                    raise MuxClosed("信用窗口长时间未回补（E5010）")
                try:
                    await asyncio.wait_for(
                        self._cond.wait(), timeout=CREDIT_DEADLOCK_TIMEOUT)
                except asyncio.TimeoutError:
                    self._stalled = True
                    raise MuxClosed(
                        f"等待 CREDIT 超过 {CREDIT_DEADLOCK_TIMEOUT:.0f}s（E5010）"
                    ) from None
                if self._stalled:
                    raise MuxClosed("信用窗口长时间未回补（E5010）")
            self._credit -= n
            self._sent_total += n

    async def grant(self) -> None:
        """收到对端 CREDIT：把额度重置回满窗口。

        为什么是"重置"而不是"累加"（见模块 docstring）：帧协议把
        CREDIT 的 payload 冻结成空，能表达的最小语义就是"我有进展"。
        重置只会多给，不会少给，因此不可能造成发送方永久卡住。
        """
        async with self._cond:
            self._credit = self._window
            self._last_progress = now()
            self._cond.notify_all()

    def on_recv(self, n: int) -> int:
        """记接收字节，返回"该不该回 CREDIT"（1 = 该回，0 = 不用）。"""
        self._recv_unacked += n
        self._recv_total += n
        if self._recv_unacked >= max(CREDIT_THRESHOLD, self._window // 2):
            self._recv_unacked = 0
            return 1
        return 0

    async def wake(self) -> None:
        async with self._cond:
            self._cond.notify_all()


# --------------------------------------------------------------------------
# 请求对象
# --------------------------------------------------------------------------


class PendingRequest:
    """发起侧的一个在途请求。`docs/03` §2.5.3 的结构，去掉 E2E 字段。"""

    __slots__ = ("req_id", "created_at", "deadline", "head", "body",
                 "cancelled", "_head_done", "_body_done", "_last_body_at")

    def __init__(self, req_id: int, *, timeout: float | None = TOTAL_TIMEOUT) -> None:
        self.req_id = req_id
        self.created_at = now()
        #: `None` = **没有总额上限**，只给 `GET /api/events`（SSE）用。
        #: 它按定义永不结束，给它任何总额上限都等于"每 30 分钟把手机的
        #: 事件流掐一次"。用 `inf` 而不是 `None` 是为了让清扫器的
        #: `t > p.deadline` 保持单一比较、不必到处判 None。
        self.deadline = (self.created_at + timeout
                         if timeout is not None else float("inf"))
        #: 第一个 RES_HEAD / ERROR 的结果（dict）。用 Future 而不是队列：
        #: 头部只会来一次，Future 的"只能 set 一次"正好把它钉住。
        self.head: asyncio.Future[dict[str, Any]] = (
            asyncio.get_event_loop().create_future())
        self.body: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=_BODY_QUEUE_MAX)
        self.cancelled = asyncio.Event()
        self._head_done = False
        self._body_done = False
        self._last_body_at = self.created_at

    # ---- 发起侧消费 ----

    async def wait_head(self, timeout: float = HEAD_TIMEOUT) -> dict[str, Any]:
        """等首个 RES_HEAD。超时抛 `asyncio.TimeoutError`（映射 E5009）。"""
        return await asyncio.wait_for(asyncio.shield(self.head), timeout)

    async def iter_body(self, *, idle_timeout: float = IDLE_TIMEOUT,
                        total_timeout: float | None = TOTAL_TIMEOUT
                        ) -> AsyncIterator[bytes]:
        """把响应体当作异步流消费。

        两层超时（`docs/04` §3.7）：`idle_timeout` 是两块之间的间隔，
        `total_timeout` 是兜底总额。**没有总额上限**会在对端"每 59 秒
        吐 1 字节"时永远挂着；`docs/04` 说原图可以很久，所以总额给到
        30 分钟——2 GB @ 1.5 MB/s 约 22 分钟。

        `total_timeout=None` 只给一种响应开：`GET /api/events`（SSE）。
        它**按定义**永不结束，给它任何总额上限都等于"每 30 分钟把手机
        的事件流掐一次"，而手机端只能表现为"偶尔莫名重连"。它的存活
        判据仍然是 `idle_timeout`（SSE 心跳 25 秒一帧，见
        `backend/app/events.py:51`），所以死连接照样 60 秒内被发现。
        """
        start = now()
        while True:
            if total_timeout is not None and start + total_timeout < now():
                raise asyncio.TimeoutError("响应体总时长超限")
            try:
                chunk = await asyncio.wait_for(
                    self.body.get(), timeout=idle_timeout)
            except asyncio.TimeoutError:
                raise asyncio.TimeoutError(
                    f"{idle_timeout:.0f}s 内没有收到新的 RES_BODY") from None
            if chunk is None:
                return
            yield chunk

    # ---- 内部：由 Mux 调用 ----

    def set_head(self, payload: dict[str, Any]) -> None:
        if not self.head.done():
            self._head_done = True
            self.head.set_result(payload)

    def push_body(self, chunk: bytes) -> None:
        self._last_body_at = now()
        self.body.put_nowait(chunk)

    def finish_body(self) -> None:
        if not self._body_done:
            self._body_done = True
            self.body.put_nowait(None)


class InboundRequest:
    """响应侧（电脑端）收到的一个请求：头部 + 请求体流。"""

    __slots__ = ("req_id", "head", "body", "cancelled", "created_at",
                 "body_len", "received")

    def __init__(self, req_id: int, head: dict[str, Any]) -> None:
        self.req_id = req_id
        self.head = head
        self.body: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=_BODY_QUEUE_MAX)
        self.cancelled = asyncio.Event()
        raw_len = head.get("body_len")
        self.body_len: int = int(raw_len) if isinstance(raw_len, int) else -1
        self.received = 0
        self.created_at = now()

    async def iter_body(self) -> AsyncIterator[bytes]:
        while True:
            chunk = await self.body.get()
            if chunk is None:
                return
            yield chunk


# --------------------------------------------------------------------------
# Mux
# --------------------------------------------------------------------------

#: 响应侧回调：拿到头部就开始干活。返回值忽略（错误由 mux 统一处理）。
RequestHandler = Callable[[InboundRequest], Awaitable[None]]
#: 通知回调：收到 NOTIFY 帧（云 → 手机推送）。
NotifyHandler = Callable[[dict[str, Any]], None]


class Mux:
    """一条隧道连接上的多路复用器。

    ★ 单读协程 + 单写协程（`docs/03` §2.5.4）。读由调用方驱动
    （`feed()` 由 WebSocket 接收循环调用），写在这里串行化：
    WebSocket 的 `send()` 在多任务下并发调用会交错成半帧，帧头与
    payload 分家的结果是对端 `decode_frame` 抛 `frame too short`。

    ★ PRIORITY_TYPES（PING/PONG/CREDIT/CANCEL）**不进队列**、不参与
    信用等待：大文件传输会把 `send_credit` 耗尽，若心跳也要排队，
    连接会在传 2 GB 的中途被自己判死（`docs/03` §2.7.1）。
    """

    def __init__(self, sender: Callable[[bytes], Awaitable[None]], *,
                 window: int = CREDIT_WINDOW,
                 role: str = "mux") -> None:
        self._send_raw = sender
        self._alloc = ReqIdAllocator()
        self._pending: dict[int, PendingRequest] = {}
        self._inbound: dict[int, InboundRequest] = {}
        self._credit = _CreditGate(window)
        self._send_lock = asyncio.Lock()
        self._closed = False
        #: 底层发送失败过一次（socket 已关、ASGI 消息已终结）。见 `_send_frame`。
        self._failed = False
        self._sweeper: asyncio.Task[None] | None = None
        self._role = role

        #: 响应侧回调（电脑端设置）。云端转发侧不设置。
        self.on_request: RequestHandler | None = None
        #: NOTIFY 回调。
        self.on_notify: NotifyHandler | None = None
        #: 心跳：对端 PING 序号回显，本地收到 PONG 时记时间。
        self.last_frame_at: float = now()
        self.last_pong_at: float = now()
        self._ping_seq = 0
        self.bytes_out = 0
        self.bytes_in = 0

    # ---- 生命周期 ----

    async def start(self) -> None:
        if self._sweeper is None:
            self._sweeper = asyncio.create_task(
                self._sweep_loop(), name=f"mux-sweep-{self._role}")

    async def close(self, reason: str = "") -> None:
        """关闭：所有在途请求立刻以失败结束，绝不让调用方永久等待。

        这也是 `docs/01` AC-4.4（CANCEL 释放云+电脑资源）的兜底：
        连接断了以后没有任何帧会来，任何还在 `await` 的协程都必须被
        主动唤醒，否则云端的 HTTP 响应流会挂到手机超时（表现为
        "断网后页面转圈 5 分钟"）。
        """
        if self._closed:
            return
        self._closed = True
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        err = {"status": 0, "error": {"code": "E5010",
                                      "message": f"tunnel closed: {reason or 'closed'}"}}
        for rid, p in list(self._pending.items()):
            p.set_head(err)
            p.finish_body()
            self._alloc.release(rid)
        self._pending.clear()
        for rid, req in list(self._inbound.items()):
            req.cancelled.set()
            req.body.put_nowait(None)
        self._inbound.clear()
        await self._credit.wake()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def stats(self) -> dict[str, int]:
        return {
            "active": len(self._pending) + len(self._inbound),
            "credit": self._credit.credit,
            "bytes_out": self.bytes_out,
            "bytes_in": self.bytes_in,
        }

    # ---- 发送 ----

    async def _send_frame(self, ftype: FrameType, req_id: int = 0,
                          payload: Any = None) -> None:
        if self._closed and ftype not in (FrameType.ERROR, FrameType.CANCEL):
            raise MuxClosed("连接已关闭")
        data = encode_frame(ftype, req_id, payload)
        failed: Exception | None = None
        async with self._send_lock:
            # ★ `_send_raw` 抛出的任何异常（socket 已关、ASGI 消息在
            # `websocket.close` 之后）都必须翻译成 `MuxClosed`。
            #
            # 不翻译的后果是**连接级故障漏成请求级 500**：底层 WebSocket
            # 的 `RuntimeError: Unexpected ASGI message 'websocket.send',
            # after sending 'websocket.close'` 会一路穿过 `forward_request`
            # → `Forwarder._relay`（它只 `except MuxClosed`）→ Starlette 的
            # ServerErrorMiddleware，最终手机拿到 500 而不是 503 E3003。
            # 触发窗口正是"电脑端刚掉线、注册表里那条连接还在"的瞬间。
            #
            # 只对 `MuxClosed` 放行：调用方（如握手拒绝路径）可能靠它判分支，
            # 把它包成新的异常会改变类型。
            if self._failed:
                raise MuxClosed("连接已失效")
            try:
                await self._send_raw(data)
            except MuxClosed:
                raise
            except Exception as exc:  # noqa: BLE001 —— 见上
                self._failed = True
                failed = exc
        if failed is not None:
            log.warning("隧道发送失败，标记连接失效：%s", failed)
            # 立刻失败所有在途请求，而不是等读循环发现对端已断。
            await self.close(f"send failed: {failed}")
            raise MuxClosed(f"连接已失效：{failed}") from failed
        self.bytes_out += len(data)

    async def send_ping(self) -> None:
        self._ping_seq = (self._ping_seq % 0x3FFFFFFF) + 1
        await self._send_frame(FrameType.PING, self._ping_seq)

    async def send_credit(self) -> None:
        await self._send_frame(FrameType.CREDIT, 0)

    # ---- 发起侧 ----

    async def open(self, head: dict[str, Any], *,
                   timeout: float | None = TOTAL_TIMEOUT) -> PendingRequest:
        """发 `REQ_HEAD` 并返回在途请求，**请求体由调用方后续送**。

        为什么需要这个而不是只用 `begin()`：转发侧必须**先拿到 req_id**
        才能开始等 `RES_HEAD`，而请求体可能很大（2 GB 上传）。`begin()`
        把"发头 + 发完体 + 发尾"揉成一次调用，意味着转发侧在整段上传
        期间都拿不到 `PendingRequest`，也就无法把电脑端的早期响应
        （例如 400 参数错）及时回给手机——手机要等自己把 2 GB 传完才
        知道"这个文件扩展名不支持"。

        用法：`p = await mux.open(head)` → `await mux.send_body(p, chunk)`
        … → `await mux.end_body(p)`。
        """
        if self._closed:
            raise MuxClosed("连接已关闭")
        rid = self._alloc.alloc()
        p = PendingRequest(rid, timeout=timeout)
        self._pending[rid] = p
        try:
            await self._send_frame(FrameType.REQ_HEAD, rid, head)
        except BaseException:
            self._abort(rid)
            raise
        return p

    async def send_body(self, p: PendingRequest, chunk: bytes) -> None:
        """送一块请求体（自动按 `MAX_BODY_CHUNK` 切片并过信用窗）。"""
        if not chunk:
            return
        if p.cancelled.is_set():
            raise MuxClosed("请求已被取消")
        await self._send_body_chunk(p.req_id, chunk)

    async def end_body(self, p: PendingRequest) -> None:
        """请求体送完：发 `REQ_END`。"""
        try:
            await self._send_frame(FrameType.REQ_END, p.req_id)
        except MuxClosed:
            self._abort(p.req_id)
            raise

    async def begin(self, head: dict[str, Any],
                    body: AsyncIterator[bytes] | None = None, *,
                    timeout: float | None = TOTAL_TIMEOUT) -> PendingRequest:
        """发 REQ_HEAD（+ 可选的 REQ_BODY 流）+ REQ_END，返回在途请求。

        ★ 请求体边读边发：手机上传 2 GB 时，云端不能先把整个体读完
        （`docs/04` §3.4 的硬要求）。`body` 是调用方的 ASGI 流。

        转发侧请改用 `open()/send_body()/end_body()`：它们能在发体的
        同时等响应头，见 `open()` 的说明。
        """
        p = await self.open(head, timeout=timeout)
        try:
            if body is not None:
                async for chunk in body:
                    await self.send_body(p, chunk)
            await self.end_body(p)
        except BaseException:
            self._abort(p.req_id)
            raise
        return p

    async def _send_body_chunk(self, rid: int, chunk: bytes) -> None:
        """按 `MAX_BODY_CHUNK` 切片后逐片发，每片先过信用窗。"""
        view = memoryview(chunk)
        for off in range(0, len(view), MAX_BODY_CHUNK):
            piece = bytes(view[off:off + MAX_BODY_CHUNK])
            await self._credit.take(len(piece))
            await self._send_frame(FrameType.REQ_BODY, rid, piece)

    async def cancel(self, rid: int, reason: str = "client_aborted") -> None:
        """通知对端停止（`docs/04` §2.3.8）。幂等。

        ★ `CANCEL` 的 payload **必须为空**：`shared/protocol.py` 把它放进
        `FIXED_LENGTH_TYPES`，`encode_frame` 见到任何 payload 都抛
        `ProtocolError`。第一版把 `reason` 编进 payload，异常被下面的
        `except` 吞掉 ⇒ 这一帧**从来没发出去过**，`cancel()` 退化成
        "只清本地状态"。症状是 AC-4.4 名存实亡：手机断开上传后，电脑端
        仍在读一个它以为有 2 GB 的请求体，直到 idle 超时才放手。

        `reason` 因此只进日志，不上线。要让它上线就得改协议文件，
        而协议是两端共享且已被 22 条用例钉住的 —— 代价大于收益。
        """
        if rid not in self._pending and rid not in self._inbound:
            return
        try:
            await self._send_frame(FrameType.CANCEL, rid)
        except (MuxClosed, ProtocolError):
            return
        log.debug("发送 CANCEL rid=%s 原因=%s", rid, reason)
        p = self._pending.get(rid)
        if p is not None:
            p.cancelled.set()
            p.set_head({"status": 0,
                        "error": {"code": "E5011", "message": "cancelled"}})
            p.finish_body()
            # ★ 必须 `_abort`：只置 head 不释放的话，`_pending` 里会永远
            # 留着一个 req_id。每取消一次泄漏一个，4096 次之后
            # `ReqIdAllocator.alloc()` 抛 TooManyRequests，**整条隧道的
            # 所有新请求一起 429**，且只能重启进程恢复。
            # 触发路径很常见：手机上取消一次上传、或页面切走导致连接断开，
            # `cloud/relay/forward.py` 就会对那个 req_id 调一次 `cancel()`。
            self._abort(rid)

    def _abort(self, rid: int) -> None:
        p = self._pending.pop(rid, None)
        if p is not None:
            p.finish_body()
        self._inbound.pop(rid, None)
        self._alloc.release(rid)

    def finish(self, rid: int) -> None:
        """请求正常结束：释放槽位。"""
        self._abort(rid)

    # ---- 响应侧（电脑端）----

    async def res_head(self, rid: int, status: int,
                       headers: dict[str, str], *,
                       body_len: int | None = None,
                       not_modified: bool = False,
                       set_cookies: Sequence[str] = ()) -> None:
        """发 `RES_HEAD`。

        ★ `set_cookies` 是**独立字段**，不是 `headers` 里的一个键。
        `headers` 在帧里是 JSON 对象（一名一值），把两条 cookie 塞进
        同一个键只能是"覆盖"或"拼成 `['a','b']` 这样的假值"，两种都会
        让另一条 cookie 静默失效——而 `<img>` 标签只能靠 cookie 鉴权
        （发不出 `Authorization` 头），失效的表现是"登录成功但缩略图
        全 401"。这里用 JSON 数组保住多值语义，由云端按多行还原。
        """
        payload = {
            "status": int(status),
            "headers": {str(k).lower(): str(v) for k, v in headers.items()},
            "body_len": body_len,
            "enc": None,
            "not_modified": bool(not_modified),
        }
        if set_cookies:
            payload["set_cookies"] = [str(c) for c in set_cookies]
        await self._send_frame(FrameType.RES_HEAD, rid, payload)

    async def res_body(self, rid: int, chunk: bytes) -> None:
        view = memoryview(chunk)
        for off in range(0, len(view), MAX_BODY_CHUNK):
            piece = bytes(view[off:off + MAX_BODY_CHUNK])
            # 响应侧**不主动等信用**：等不到就说明手机那边卡住了，
            # 而阻塞在这里会把整个隧道的读取协程堵死（所有 req_id
            # 一起完蛋）。改为"信用不足就抛"，由上层发 CANCEL。
            await self._credit.take(len(piece))
            await self._send_frame(FrameType.RES_BODY, rid, piece)

    async def res_end(self, rid: int) -> None:
        await self._send_frame(FrameType.RES_END, rid)
        self._abort(rid)

    async def res_error(self, rid: int, code: str, message: str, *,
                        fatal: bool = False) -> None:
        await self._send_frame(FrameType.ERROR, rid,
                              {"code": code, "message": message,
                               "fatal": bool(fatal)})
        self._abort(rid)

    async def notify(self, topic: str, data: dict[str, Any], *,
                     client_id: str | None = None) -> None:
        await self._send_frame(FrameType.NOTIFY, 0,
                              {"topic": topic, "data": data,
                               "ts": time.time(), "client_id": client_id})

    # ---- 接收 ----

    async def feed(self, frame: Frame) -> None:
        """处理一条入站帧。任何格式问题都收敛成"失败该请求"，不抛出去。

        `docs/04` §2.3.7：`ERROR` 终止**一个** `req_id`；帧格式错误才是
        `fatal`（整条连接不可继续）。把两者混为一谈的后果是——一张照片
        的 response 头解析失败会让整条隧道重连，手机上表现为"看图时
        所有缩略图一起白掉"。
        """
        self.last_frame_at = now()
        self.bytes_in += len(frame.payload)
        ftype = frame.type
        rid = frame.req_id

        if ftype in (FrameType.PING, FrameType.PONG, FrameType.CREDIT,
                     FrameType.CANCEL):
            # 优先级帧也要挡 `MuxClosed`：`PING` 会就地回 `PONG`，而对端
            # 可能刚好在这条帧到达的同时断开（读循环还没来得及发现）。
            # 让它冒出去会打断调用方的读循环，把"对端正常断开"变成
            # 一条异常日志。
            try:
                await self._feed_priority(frame)
            except MuxClosed:
                return
            return

        try:
            if ftype == FrameType.REQ_HEAD:
                await self._on_req_head(rid, frame.json)
            elif ftype == FrameType.REQ_BODY:
                await self._on_req_body(rid, frame.payload)
            elif ftype == FrameType.REQ_END:
                await self._on_req_end(rid)
            elif ftype == FrameType.RES_HEAD:
                self._on_res_head(rid, frame.json)
            elif ftype == FrameType.RES_BODY:
                await self._on_res_body(rid, frame.payload)
            elif ftype == FrameType.RES_END:
                self._on_res_end(rid)
            elif ftype == FrameType.ERROR:
                await self._on_error(rid, frame.json)
            elif ftype == FrameType.NOTIFY:
                if self.on_notify is not None:
                    self.on_notify(frame.json)
            elif ftype in (FrameType.HELLO, FrameType.HELLO_ACK):
                # 握手帧只在隧道建立阶段有意义，运行期收到说明对端实现
                # 有问题；忽略而不是断连（断连会让一次误发打死整条隧道）。
                log.debug("运行期收到握手帧 %s，已忽略", ftype.name)
        except ProtocolError as exc:
            log.warning("帧解析失败 %s rid=%s：%s", ftype.name, rid, exc)
            await self._fail(rid, "E5002", "帧内容解析失败")

    async def _feed_priority(self, frame: Frame) -> None:
        ftype = frame.type
        if ftype == FrameType.PING:
            await self._send_frame(FrameType.PONG, frame.req_id)
        elif ftype == FrameType.PONG:
            self.last_pong_at = now()
        elif ftype == FrameType.CREDIT:
            await self._credit.grant()
        elif ftype == FrameType.CANCEL:
            rid = frame.req_id
            p = self._pending.get(rid)
            if p is not None:
                p.cancelled.set()
                p.set_head({"status": 0,
                            "error": {"code": "E5011",
                                      "message": "peer cancelled"}})
                p.finish_body()
                self._abort(rid)
            req = self._inbound.get(rid)
            if req is not None:
                req.cancelled.set()
                req.body.put_nowait(None)

    async def _on_req_head(self, rid: int, head: dict[str, Any]) -> None:
        if self.on_request is None:
            await self._send_frame(
                FrameType.ERROR, rid,
                {"code": "E5004", "message": "peer is not a server", "fatal": False})
            return
        req = InboundRequest(rid, head)
        self._inbound[rid] = req
        # 不 await 回调：处理器要边读体边处理，若在这里 await，读循环
        # 就停了，`REQ_BODY` 永远收不到 —— 经典的自锁。
        asyncio.create_task(self._run_handler(req), name=f"tunnel-req-{rid}")

    async def _run_handler(self, req: InboundRequest) -> None:
        try:
            assert self.on_request is not None
            await self.on_request(req)
        except asyncio.CancelledError:
            raise
        except MuxClosed:
            log.debug("mux 已关闭，放弃请求 rid=%s", req.req_id)
        except Exception:      # noqa: BLE001 —— 处理器异常不能打死隧道
            log.exception("隧道请求处理失败 rid=%s", req.req_id)
            try:
                await self.res_error(req.req_id, "E5008", "本地处理失败")
            except (MuxClosed, ProtocolError):
                pass
        finally:
            self._inbound.pop(req.req_id, None)

    async def _on_req_body(self, rid: int, data: bytes) -> None:
        req = self._inbound.get(rid)
        if req is None:
            return
        if req.body_len >= 0 and req.received + len(data) > req.body_len:
            await self.res_error(rid, "E5003", "请求体超出声明长度")
            return
        req.received += len(data)
        if self._credit.on_recv(len(data)):
            await self.send_credit()
        await req.body.put(data)

    async def _on_req_end(self, rid: int) -> None:
        req = self._inbound.get(rid)
        if req is not None:
            req.body.put_nowait(None)

    def _on_res_head(self, rid: int, head: dict[str, Any]) -> None:
        p = self._pending.get(rid)
        if p is not None:
            p.set_head(head)

    async def _on_res_body(self, rid: int, data: bytes) -> None:
        p = self._pending.get(rid)
        if p is None:
            return
        if self._credit.on_recv(len(data)):
            await self.send_credit()
        await p.body.put(data)

    def _on_res_end(self, rid: int) -> None:
        p = self._pending.get(rid)
        if p is not None:
            p.finish_body()
        self._abort(rid)

    async def _on_error(self, rid: int, payload: dict[str, Any]) -> None:
        p = self._pending.get(rid)
        if p is not None:
            p.set_head({"status": 0, "error": payload})
            p.finish_body()
            self._abort(rid)
            return
        req = self._inbound.get(rid)
        if req is not None:
            req.cancelled.set()
            req.body.put_nowait(None)
            self._inbound.pop(rid, None)

    async def _fail(self, rid: int, code: str, message: str) -> None:
        p = self._pending.get(rid)
        if p is not None:
            p.set_head({"status": 0, "error": {"code": code, "message": message}})
            p.finish_body()
            self._abort(rid)
            return
        req = self._inbound.get(rid)
        if req is not None:
            await self.res_error(rid, code, message)

    # ---- 超时清扫 ----

    async def _sweep_loop(self) -> None:
        """`docs/03` §2.5.5：每 5 秒扫一次，清掉超时的在途请求。

        为什么必须有：`HEAD_TIMEOUT` 只在 `wait_head()` 处生效，而
        调用方可能因为"手机已经断开"而再也不会 await 它 —— 那个
        `req_id` 就永远留在 `pending` 里，4096 个之后所有新请求 429。
        """
        try:
            while True:
                await asyncio.sleep(SWEEP_INTERVAL)
                self._sweep_once()
        except asyncio.CancelledError:
            raise

    def _sweep_once(self) -> None:
        t = now()
        for rid, p in list(self._pending.items()):
            if t > p.deadline:
                log.debug("请求超时清理 rid=%s", rid)
                p.set_head({"status": 0,
                            "error": {"code": "E5009",
                                      "message": "请求总时长超限"}})
                p.finish_body()
                self._abort(rid)
                asyncio.create_task(self._cancel_quietly(rid))

    async def _cancel_quietly(self, rid: int) -> None:
        # 同 `cancel()`：payload 必须为空（FIXED_LENGTH_TYPES）。
        try:
            await self._send_frame(FrameType.CANCEL, rid)
        except (MuxClosed, ProtocolError):
            pass
