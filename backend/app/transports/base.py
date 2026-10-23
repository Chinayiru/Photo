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

import mimetypes
from typing import ClassVar, Protocol, runtime_checkable

from ..core import Ctx, Reply

__all__ = [
    "ServerTransport",
    "RangeNotSatisfiable",
    "parse_range",
    "content_type_for",
    "EXTRA_MIME",
    "DANGEROUS_EXTS",
    "DEFAULT_MIME",
]


class RangeNotSatisfiable(Exception):
    """Range 头无法满足（越界或格式非法）→ 调用方应回 416。"""

    #: 调用方统一用的 HTTP 状态码。`backend/app/handlers/browse.py` 里
    #: `except RangeNotSatisfiable: return _reply(416, ...)`，测试也读这个属性；
    #: 写在一处，避免"改了一边忘了另一边"。
    status: ClassVar[int] = 416

    def __init__(self, size: int) -> None:
        super().__init__(f"range not satisfiable for size {size}")
        self.size = size


#: 扩展名 → MIME 的补充表。
# 系统 mimetypes 库对 heic/avif 的识别依赖注册表，跨机器不一致，
# 而这两种格式正是 iPhone 与安卓新机的主力格式，必须写死。
EXTRA_MIME: dict[str, str] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "heic": "image/heic",
    "heif": "image/heif",
    "avif": "image/avif",
    # 二期视频
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "mkv": "video/x-matroska",
}

DEFAULT_MIME = "application/octet-stream"

#: 绝不按原类型返回的扩展名。
# `application/xhtml+xml` 会被浏览器**当作 HTML 渲染并执行脚本**，
# 而照片库里的文件是用户提供的字节 —— 库里混进一个 .xhtml 就是存储型 XSS。
# SVG 同理：直接导航到 image/svg+xml 时内嵌 <script> 会在本站源下执行。
# 注意 `guess_type` 对 xhtml 返回的不是 text/*，只靠"排除 text/"挡不住。
DANGEROUS_EXTS: frozenset[str] = frozenset({
    "html", "htm", "xhtml", "xht", "shtml", "svg", "svgz", "xml", "xsl", "xslt",
    "js", "mjs", "cjs", "jsx", "ts", "mhtml", "mht", "hta", "vbs", "wsf",
    "php", "phtml", "asp", "aspx", "jsp", "cgi", "pl", "sh", "bat", "cmd", "ps1",
})


def content_type_for(ext: str) -> str:
    """扩展名（不含点，任意大小写）→ MIME 类型。

    先查写死的表，再退到系统 mimetypes。**永远不返回可执行类型**
    （text/* 与 DANGEROUS_EXTS 一律降级为 octet-stream）：
    照片库里的内容是用户提供的字节，被当作 HTML/SVG 渲染并执行脚本，
    就是本站源下的存储型 XSS。这是 `nosniff` 之外的第二道防线。
    """
    e = ext.lower().lstrip(".")
    if e in DANGEROUS_EXTS:
        return DEFAULT_MIME
    hit = EXTRA_MIME.get(e)
    if hit:
        return hit
    guess, _ = mimetypes.guess_type(f"x.{e}")
    if guess and not guess.startswith("text/"):
        return guess
    return DEFAULT_MIME


def _is_ascii_digits(value: str) -> bool:
    """是否全是 **ASCII** 数字。

    ★ 不能只用 `str.isdigit()`：它对 Unicode 数字同样返回 True ——
    `"²".isdigit()`、全角 `"１２３".isdigit()`、`"٣".isdigit()` 都是 True，
    但 `int("²")` 会抛 `ValueError`。于是 `Range: bytes=²-` 不再是
    "格式不合法 → 416"，而是 `ValueError` 逃出 handler → **500**。

    这类 500 比看上去严重：它不需要任何凭证之外的东西，一个 `curl`
    就能稳定复现，而且 `/api/items/{id}/raw` 是每个图片请求都会走的路径。
    RFC 7233 的 range 语法里只允许 DIGIT（0x30-0x39），`isascii()` 恰好
    表达的正是这个约束。
    """
    return value.isascii() and value.isdigit()


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """解析 `Range: bytes=...`，返回**闭区间** (start, end)；无 Range 返回 None。

    支持的形式（手机浏览器与下载器实际会发的）：

    - `bytes=0-`        → 从 0 到末尾（断点续传最常见的形态）
    - `bytes=500-999`   → 指定区间
    - `bytes=-500`      → **最后** 500 字节（注意：这不是"从 0 到 500"）
    - `bytes=0-0`       → 只要第 1 个字节（探测文件类型/是否存在）

    不支持多区间（`bytes=0-1,5-9`）：返回 416 让客户端改用单区间。
    多区间响应体是 `multipart/byteranges`，实现复杂而实际收益极小
    —— 手机端播放器/下载器都用单区间。

    ⚠️ `start >= size` 必须抛 RangeNotSatisfiable（回 416 + `Content-Range: bytes */size`），
    不能静默返回 200 全文件：那会让客户端以为"续传成功了"却拿到重复数据。
    """
    if not header:
        return None
    h = header.strip()
    if not h.lower().startswith("bytes="):
        return None
    spec = h[6:].strip()
    if not spec or "," in spec:
        # 多区间不支持；空 spec 是畸形头。
        raise RangeNotSatisfiable(size)

    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()

    if not first:
        # `-500`：最后 500 字节。
        # 这是最容易写错的分支：把 "-500" 当成 0..500 是经典 bug。
        if not _is_ascii_digits(last):
            raise RangeNotSatisfiable(size)
        n = int(last)
        if n <= 0:
            raise RangeNotSatisfiable(size)
        n = min(n, size)
        if n == 0:
            raise RangeNotSatisfiable(size)
        return size - n, size - 1

    if not _is_ascii_digits(first):
        raise RangeNotSatisfiable(size)
    start = int(first)
    if start >= size:
        raise RangeNotSatisfiable(size)

    if not last:
        return start, size - 1
    if not _is_ascii_digits(last):
        raise RangeNotSatisfiable(size)
    end = int(last)
    if end < start:
        raise RangeNotSatisfiable(size)
    # `bytes=0-99999` 而文件只有 100 字节 → 钳到末尾而不是报错
    # （RFC 7233 允许，且手机下载器常发一个"足够大"的区间）。
    return start, min(end, size - 1)


def range_length(rng: tuple[int, int]) -> int:
    """闭区间的字节数。"""
    start, end = rng
    return end - start + 1


def content_range_header(rng: tuple[int, int], size: int) -> str:
    """206 响应的 Content-Range 头。"""
    start, end = rng
    return f"bytes {start}-{end}/{size}"


def unsatisfied_range_header(size: int) -> str:
    """416 响应的 Content-Range 头（RFC 7233 要求 `bytes */size`）。"""
    return f"bytes */{size}"


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
