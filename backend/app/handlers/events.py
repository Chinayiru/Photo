"""SSE 事件流的路由处理函数。"""
from __future__ import annotations

import logging
import time

from ..core import Ctx, Reply, stream_reply
from ..events import get_bus, stream_events

log = logging.getLogger(__name__)

__all__ = ["events"]


async def events(ctx: Ctx) -> Reply:
    """`GET /api/events` —— 服务端事件推送（SSE）。

    参数：
      * `last_event_id`（可选）：只做记录。一期不做补发 —— 事件全是
        进度类，错过就错过，下一条带的是最新值。真正的"补发"需要
        服务端保存事件历史，而历史里包含文件名，等于在内存里留一份
        照片清单，收益不抵风险。

    响应头里的三条都不能省：
      * `Cache-Control: no-store` —— 否则中间设备会把这个长响应缓存住；
      * `X-Accel-Buffering: no` —— 反向代理（二期云端的 Caddy/Nginx）
        默认会攒够 4KB 才转发，SSE 会变成"每 30 秒蹦一批"；
      * `Connection: keep-alive` —— 明确告诉客户端这是长连接。
    """
    st = getattr(ctx, "app", None)
    bus = getattr(st, "bus", None) if st is not None else None
    if bus is None:
        bus = get_bus()

    hello = {
        "device_id": getattr(st, "device_id", None) if st is not None else None,
        "via": ctx.via,
    }
    if ctx.q_bool("once", False):
        # 测试与排障用：只发 hello 就结束，不必等 25 秒心跳。
        from ..core import bytes_reply

        from ..events import format_sse

        reply = bytes_reply(
            format_sse({"type": "hello", "ts": time.time(), **hello}, event_name="hello"),
            "text/event-stream; charset=utf-8",
        )
    else:
        reply = stream_reply(
            stream_events(bus, hello=hello),
            "text/event-stream; charset=utf-8",
        )

    # ⚠️ 三个头必须对**两个分支都设**。只在流式分支设的话，`?once=1`
    # 返回的 hello 帧会被中间设备缓存住，下次请求拿到的是上一次的
    # hello（device_id 是旧的），排查时完全看不出问题出在哪。
    reply.set("Cache-Control", "no-store")
    reply.set("X-Accel-Buffering", "no")
    reply.set("Connection", "keep-alive")
    return reply
