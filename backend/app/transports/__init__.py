"""传输层：把 Router 暴露给手机的不同方式。

一期只实现 `local_http`（局域网直连，手机连同一 Wi-Fi 即可用，不需要 VPS）。
二期加 `cloud_tunnel`（电脑主动外连云端，手机在任何网络都能访问）。

新增一条通路**不需要改业务代码** —— 只需实现 `base.ServerTransport`。
"""
from __future__ import annotations

from .base import (
    DEFAULT_MIME,
    EXTRA_MIME,
    RangeNotSatisfiable,
    ServerTransport,
    content_range_header,
    content_type_for,
    parse_range,
    range_length,
    unsatisfied_range_header,
)

__all__ = [
    "DEFAULT_MIME",
    "EXTRA_MIME",
    "RangeNotSatisfiable",
    "ServerTransport",
    "content_range_header",
    "content_type_for",
    "parse_range",
    "range_length",
    "unsatisfied_range_header",
]
