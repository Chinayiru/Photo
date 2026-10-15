"""服务端事件推送（SSE）。

用途：手机端在设置页看索引进度、在时间线上看到"刚传完的照片"。
为什么用 SSE 而不是 WebSocket：

* 一期只需要**服务端 → 客户端**单向推送，SSE 就是这个语义；
* SSE 走普通 HTTP，同一套 Ctx/Reply 中间表示直接复用，
  二期接云隧道时帧格式不用动（隧道里它只是一条长响应）；
* WebSocket 需要额外的握手与帧处理，而二期隧道**自己**就是
  WebSocket，两个 WebSocket 叠在一起会让排障困难一倍。

事件格式（每行一个 JSON，SSE 的 data 字段）：

    event: progress
    data: {"type":"scan_progress","root":"library","seen":1200,...}

设计要点：

1. **订阅者队列有上限**（`QUEUE_MAX`）。慢客户端（手机切到后台、
   系统冻结 JS）不读数据时，无上限队列会把内存吃光。满了就丢弃
   最旧的事件 —— 进度类事件天然可丢（下一条会带上最新值）。
2. **心跳**（`HEARTBEAT_SEC` = 25 秒）。没有心跳时，手机切网络、
   中间设备静默断开都不会立刻被发现，用户看到进度条永远停在
   原处却没有任何错误。
3. **发布是非阻塞的**。`publish()` 在请求处理协程里被调用，
   绝不能因为某个客户端读得慢而卡住上传或索引。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

__all__ = [
    "QUEUE_MAX",
    "HEARTBEAT_SEC",
    "EventBus",
    "get_bus",
    "set_bus",
    "format_sse",
]

#: 单个订阅者的待发事件上限。满了丢最旧的（进度事件天然可丢）。
QUEUE_MAX: int = 256

#: 心跳间隔。必须小于常见中间设备的空闲超时（多数是 60 秒）。
HEARTBEAT_SEC: float = 25.0

#: 事件类型白名单。写错类型名不会被发现，直到前端某个面板永远不刷新。
EVENT_TYPES = frozenset({
    "hello",
    "ping",
    "scan_start",
    "scan_progress",
    "scan_done",
    "thumb_progress",
    "upload_done",
    "media_changed",
    "index_changed",
})


def format_sse(event: dict[str, Any], *, event_name: str | None = None) -> bytes:
    """把事件编码成 SSE 帧。

    `ensure_ascii=False`：中文文件名（"敦煌.jpg"）不该在线上变成
    一串 \\uXXXX —— 那会让每条事件从 30 字节涨到 120 字节，
    而流量是手机在蜂窝网络下的真实成本。
    """
    name = event_name or str(event.get("type") or "message")
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\ndata: {body}\n\n".encode("utf-8")


class _Subscriber:
    __slots__ = ("queue", "created_at", "dropped")

    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=QUEUE_MAX)
        self.created_at = time.time()
        self.dropped = 0


class EventBus:
    """极简发布订阅。

    只在**单进程内**工作。一期是单进程；二期若要把电脑端拆成多进程
    （比如把索引器单独放一个进程），这里要换成 Unix socket 或
    multipart 中转 —— 但现在就上那套复杂度不划算。
    """

    __slots__ = ("_subs", "_loop", "published")

    def __init__(self) -> None:
        self._subs: set[_Subscriber] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.published = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """记录事件循环，供跨线程 `publish_threadsafe` 使用。

        允许传 None：同步上下文（脚本、不跑事件循环的测试）里调用时
        没有 running loop 可绑，此时 `publish` 照常工作，
        只有 `publish_threadsafe` 变成静默 no-op（见它的实现）。
        """
        self._loop = loop

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def subscribe(self) -> _Subscriber:
        sub = _Subscriber()
        self._subs.add(sub)
        return sub

    def unsubscribe(self, sub: _Subscriber) -> None:
        self._subs.discard(sub)

    def publish(self, event: dict[str, Any]) -> int:
        """把事件投给所有订阅者，**永不阻塞、永不抛异常**。

        返回实际投递的订阅者数（用于测试与排障）。
        """
        self.published += 1
        delivered = 0
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                # 丢最旧的一条再放新的：进度事件里最新的值才是用户
                # 想看的，"1200/4915" 比 "1100/4915" 更有用。
                try:
                    sub.queue.get_nowait()
                    sub.queue.put_nowait(event)
                    sub.dropped += 1
                    delivered += 1
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    sub.dropped += 1
            except Exception:       # noqa: BLE001
                # 一个坏掉的订阅者绝不能拖垮发布方 —— publish() 是在
                # 上传与索引的协程里被调用的，抛出去就是"某个手机端的
                # 连接异常导致另一台手机传不完照片"。
                log.debug("事件投递失败", exc_info=True)
        return delivered

    def publish_threadsafe(self, event: dict[str, Any]) -> None:
        """从非事件循环线程（executor、watchdog 回调）发布。

        直接调 `publish()` 会改 asyncio.Queue，而它**不是线程安全**的：
        从别的线程 put 进去，等待中的协程可能永远收不到唤醒信号。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self.publish, event)
        except RuntimeError:        # 循环正在关闭
            log.debug("publish_threadsafe 失败：事件循环已关闭", exc_info=True)

    def close(self) -> None:
        """唤醒所有订阅者让它们结束（服务退出时调用）。"""
        for sub in list(self._subs):
            try:
                sub.queue.put_nowait(None)
            except asyncio.QueueFull:
                pass
        self._subs.clear()

    def stats(self) -> dict[str, Any]:
        return {"subscribers": len(self._subs), "published": self.published}


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus


def set_bus(bus: EventBus | None) -> None:
    global _bus
    _bus = bus


async def stream_events(
    bus: EventBus,
    *,
    heartbeat: float = HEARTBEAT_SEC,
    hello: dict[str, Any] | None = None,
) -> AsyncIterator[bytes]:
    """把事件总线变成 SSE 字节流。

    首帧立刻发一条 `hello`：手机端要在 UI 上显示"已连接"，
    若等第一个真实事件才发，用户可能盯着"连接中…"几十秒。
    """
    sub = bus.subscribe()
    try:
        yield format_sse(
            {"type": "hello", "ts": time.time(), **(hello or {})}, event_name="hello"
        )
        while True:
            try:
                item = await asyncio.wait_for(sub.queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                yield format_sse({"type": "ping", "ts": time.time()}, event_name="ping")
                continue
            if item is None:
                break
            yield format_sse(item)
    finally:
        # 客户端断开（协程被取消）时也会走到这里。不清理的话，
        # 每开一次设置页就泄漏一个订阅者，事件循环里越积越多。
        bus.unsubscribe(sub)
