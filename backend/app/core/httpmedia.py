"""HTTP 表示层里与**协议无关**的那部分：MIME 表、Range 解析、Content-Range 头。

## 为什么在 `core` 而不是 `transports`

架构审查（`架构审查报告-c1cd076.md` M1）指出：handler 直接从
`transports.base` 取 Range/MIME，等于业务层依赖某个传输适配器的实现，
换传输层（局域网 HTTP / 云隧道 / 将来的别的）时业务代码也可能要跟着改，
与 `docs/03` §「handler 只接收协议无关的 Ctx、返回 Reply」冲突。

这些东西与"怎么传输"无关，只是 HTTP 的字节语义，所以放 `core`。
`transports.base` 仍在 `__all__` 里 re-export 同名符号，避免一次性改动
炸一片；新代码请直接从这里取。

★ 反过来也要守住：`core` 不许 import `transports`（有 AST 闸门用例盯着）。
"""
from __future__ import annotations

import mimetypes

__all__ = [
    "DANGEROUS_EXTS",
    "DEFAULT_MIME",
    "EXTRA_MIME",
    "RangeNotSatisfiable",
    "content_range_header",
    "content_type_for",
    "parse_range",
    "range_length",
    "unsatisfied_range_header",
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
