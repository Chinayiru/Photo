"""协议无关的业务核心。

- `ctx.py`     ：请求/响应中间表示（Ctx / Reply）
- `dispatch.py`：路由表 + 统一异常收敛 + 统一错误响应

这一层不 import starlette / fastapi / websockets，只依赖标准库与 errors。
它是"两条通路一套 API"的落点：局域网 HTTP 与二期云端隧道都只是
把各自的协议翻译成 Ctx，然后调用同一个 Router。
"""
from __future__ import annotations

from .ctx import (
    CLOUD,
    LAN,
    Body,
    Ctx,
    Headers,
    Reply,
    bytes_reply,
    empty_reply,
    file_reply,
    iter_body,
    json_reply,
    new_request_id,
    stream_reply,
    text_reply,
)
from .dispatch import Route, Router

__all__ = [
    "CLOUD",
    "LAN",
    "Body",
    "Ctx",
    "Headers",
    "Reply",
    "Route",
    "Router",
    "bytes_reply",
    "empty_reply",
    "file_reply",
    "iter_body",
    "json_reply",
    "new_request_id",
    "stream_reply",
    "text_reply",
]
