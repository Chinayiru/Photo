"""传输层的公共契约。

## 一期：局域网直连；二期：云端隧道

设计原则 DP-4「两条通路，一套 API」要求业务代码对通路无感知。
因此这里定义两个东西：

1. `ServerTransport` —— 把 `Router` 暴露给手机的统一接口。
   一期由 `local_http.LocalHttpTransport` 实现（uvicorn 监听 8787）；
   二期加一个 `cloud_tunnel.CloudTunnelTransport`（主动外连 wss://云/agent/ws），
   两者对 `Router` 的调用方式完全相同。

2. `parse_range` / `content_type_for` —— 两条通路都要用的 HTTP 语义。
   放在这里而不是 `local_http` 里，是因为二期隧道里手机发的仍然是
   `GET /api/items/1/raw` 加一个 `Range: bytes=...` 头，
   隧道只是搬运字节，语义完全一致。

## 为什么 Range 必须在一期就做

`/api/items/{id}/raw` 要能下 2GB 的原图。若不支持 Range，
手机在 4G 下下到 95% 断网就得从头再来 —— 而 2GB 在 5Mbps 下要 55 分钟。
这不是"优化项"，是"能不能用"的问题。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


__all__ = [
    "ServerTransport",
    "RangeNotSatisfiable",
    "parse_range",
    "content_type_for",
    "EXTRA_MIME",
    "DANGEROUS_EXTS",
    "DEFAULT_MIME",
]


# ★ 这些符号的实现在 `core/httpmedia.py`（协议无关的 HTTP 语义）；
#   这里 re-export 只为兼容旧导入路径，新代码请从 `..core.httpmedia` 取。
from ..core.httpmedia import (
    RangeNotSatisfiable,
    EXTRA_MIME,
    DEFAULT_MIME,
    DANGEROUS_EXTS,
    content_type_for,
    parse_range,
    # 兼容旧导入路径：	ransports/__init__.py 从这里取这三个名字。
    # ruff 的 F401 会想把它们删掉（本文件内部没用），删了就 ImportError
    # —— docs/11 记着这次事故（自动 --fix 打挂 import）。
    range_length,  # noqa: F401
    content_range_header,  # noqa: F401
    unsatisfied_range_header,  # noqa: F401
)

@runtime_checkable
class ServerTransport(Protocol):
    """把 Router 暴露给手机的统一接口。

    一期实现：`local_http.LocalHttpTransport`（uvicorn 监听，局域网直连）。
    二期实现：`cloud_tunnel.CloudTunnelTransport`（主动外连云，无需公网 IP）。

    两者对 Router 的调用完全一致，差别只在"字节从哪来、往哪去"。
    """

    name: str

    async def start(self) -> None:
        """开始接受请求。必须在 `app.state` 就绪后调用。"""
        ...

    async def stop(self) -> None:
        """停止接受请求并释放端口/连接。必须幂等。"""
        ...

    @property
    def urls(self) -> list[str]:
        """可供手机访问的地址，用于托盘显示与二维码。

        局域网实现返回 `http://192.168.x.x:8787`（已过滤虚拟网卡）；
        隧道实现返回 `https://域名/d/{device_id}`。
        """
        ...

    def status(self) -> dict[str, object]:
        """供 `/api/system/info` 与托盘显示的状态摘要。"""
        ...
