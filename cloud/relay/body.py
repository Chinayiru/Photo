"""账号和配对接口的 JSON 读取上限；上传分片使用独立的流式路径。"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request

JSON_BODY_LIMIT = 64 * 1024


class RequestBodyTooLarge(Exception):
    """请求体超过账号接口允许的大小。"""


async def read_json_object(request: Request) -> dict[str, Any] | None:
    """按实际读取的字节数限额，覆盖没有 Content-Length 的分块请求。"""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > JSON_BODY_LIMIT:
                raise RequestBodyTooLarge
        except ValueError:
            return None
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > JSON_BODY_LIMIT:
            raise RequestBodyTooLarge
        raw.extend(chunk)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return data if isinstance(data, dict) else None
