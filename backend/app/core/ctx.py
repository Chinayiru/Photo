"""协议无关的请求/响应中间表示。

## 为什么需要这一层

设计原则 DP-4「两条通路，一套 API」：手机既可能通过局域网 HTTP 直连电脑，
也可能（二期）通过云端隧道中继。如果业务逻辑直接写 `starlette.Request`
或直接读隧道帧，那么两条通路就要写两份业务代码 —— 两份代码一定会漂移，
而"局域网能用、远程不能用"这种 bug 极难排查。

所以业务处理函数只认识这里的 `Ctx` / `Reply`：

    async def timeline(ctx: Ctx) -> Reply: ...

两侧各有一个**薄适配器**把真实协议翻译成 Ctx：

- `transports/local_http.py`：Starlette Request  → Ctx，Reply → Response
- `transports/cloud_tunnel.py`（二期）：帧流 → Ctx，Reply → 帧流

适配器只做翻译，不含任何业务判断。这样二期接隧道时业务代码零改动。

## 与 Starlette 的边界

`Ctx` 刻意不暴露 `Request` 对象，也不暴露 `asyncio` 之外的东西。
响应体是 `AsyncIterator[bytes]`，因此缩略图、原图、分片上传
都能流式处理，不需要先把整个文件读进内存 —— 2GB 的原始视频
（二期）读进内存会直接把进程打爆。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Mapping, MutableMapping

__all__ = [
    "Headers",
    "Ctx",
    "Reply",
    "Body",
    "json_reply",
    "text_reply",
    "bytes_reply",
    "empty_reply",
    "stream_reply",
    "file_reply",
    "iter_body",
    "new_request_id",
    "LAN",
    "CLOUD",
]

#: 通路标识。只用于日志与诊断，**业务代码不应据此分支**。
LAN = "lan"
CLOUD = "cloud"

Body = bytes | AsyncIterator[bytes] | None


def new_request_id() -> str:
    """生成请求 id，用于把日志、错误响应、审计记录串起来。

    取 uuid4 前 16 位十六进制：足够避免碰撞（生日界在 2^32 次请求量级），
    又比完整 uuid 短一半，日志里好读。
    """
    return uuid.uuid4().hex[:16]


class Headers(MutableMapping[str, str]):
    """大小写不敏感的头部映射。

    HTTP 头部按 RFC 7230 是大小写不敏感的，但 `dict` 不是。
    用普通 dict 存头部，`h["etag"]` 与 `h["ETag"]` 会得到不同结果，
    而 Starlette、浏览器、curl 三者的书写习惯各不相同。
    内部统一转小写存储，读取时也转小写。
    """

    __slots__ = ("_d",)

    def __init__(self, items: Mapping[str, str] | Iterable[tuple[str, str]] = ()) -> None:
        self._d: dict[str, str] = {}
        src = items.items() if isinstance(items, Mapping) else items
        for k, v in src:
            self._d[str(k).lower()] = str(v)

    def __getitem__(self, k: str) -> str:
        return self._d[k.lower()]

    def __setitem__(self, k: str, v: str) -> None:
        self._d[k.lower()] = str(v)

    def __delitem__(self, k: str) -> None:
        del self._d[k.lower()]

    def __iter__(self):
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, k: object) -> bool:
        return isinstance(k, str) and k.lower() in self._d

    def get(self, k: str, default: Any = None) -> Any:  # type: ignore[override]
        return self._d.get(k.lower(), default)

    def setdefault(self, k: str, default: str = "") -> str:  # type: ignore[override]
        return self._d.setdefault(k.lower(), default)

    def pop(self, k: str, default: Any = None) -> Any:  # type: ignore[override]
        return self._d.pop(k.lower(), default)

    def copy(self) -> "Headers":
        h = Headers()
        h._d = dict(self._d)
        return h

    def as_dict(self) -> dict[str, str]:
        return dict(self._d)

    def __repr__(self) -> str:
        return f"Headers({self._d!r})"


@dataclass(slots=True)
class Ctx:
    """一次请求的协议无关表示。

    字段说明：

    - `path` 是**已解码**的路径（`/api/items/12/thumb`），不含查询串。
    - `query` 的值是 list：`?tag=a&tag=b` 是合法的，取第一个用 `q()`。
    - `headers` 键为小写。
    - `body` 是异步字节流。**不要无条件读完** —— 上传可以到 2GB。
    - `via` 是通路标识，只用于日志与诊断。
    - `params` 由路由匹配填充路径参数（`{id}`）。
    - `state` 供中间件与处理函数之间传递少量数据（如已认证的设备）。
    """

    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    headers: Headers = field(default_factory=Headers)
    body: Body = None
    via: str = LAN
    client_ip: str = ""
    request_id: str = field(default_factory=new_request_id)
    started_at: float = field(default_factory=time.time)
    params: dict[str, str] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """把构造时传入的普通 dict 提升为大小写不敏感的 Headers。

        不这样做的话，`Ctx(headers={"Authorization": "Bearer x"})` 里的
        `ctx.headers.get("authorization")` 会返回 None —— 大小写不敏感
        只在**构造后**才成立，而调用方几乎总是直接传 dict。
        这会让鉴权中间件静默失效（表现为"带了 token 仍然 401"）。
        """
        if not isinstance(self.headers, Headers):
            self.headers = Headers(self.headers or {})

    # ---- 查询参数便捷读取 ----
    # 这些 helper 存在的意义是**统一校验语义**：接口文档规定 limit 超出范围
    # 要钳制而不是报错，但 order=asc 要报 400（不静默忽略）。
    # 如果每个处理函数各写一遍 int(...)，这两条规则一定会被写歪。

    def q(self, name: str, default: str | None = None) -> str | None:
        """取查询参数第一个值。"""
        vals = self.query.get(name)
        return vals[0] if vals else default

    def q_all(self, name: str) -> list[str]:
        return list(self.query.get(name, ()))

    def q_int(self, name: str, default: int, *, minimum: int, maximum: int) -> int:
        """取整型查询参数，超出范围**钳制**（不报错）。

        钳制而非报错是有意的：手机端滚动加载时可能传 limit=1000，
        返回 500 条比返回 400 让用户卡住要好。
        非法文本（`limit=abc`）仍会报错 —— 那是客户端 bug，静默纠正会掩盖它。
        """
        from ..errors import BadRequest

        raw = self.q(name)
        if raw is None or raw == "":
            return default
        try:
            n = int(raw)
        except (TypeError, ValueError):
            raise BadRequest(f"参数 {name} 必须是整数") from None
        return max(minimum, min(maximum, n))

    def q_bool(self, name: str, default: bool = False) -> bool:
        raw = self.q(name)
        if raw is None:
            return default
        return raw.lower() in ("1", "true", "yes", "on")

    def q_float(self, name: str) -> float | None:
        from ..errors import BadRequest

        raw = self.q(name)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise BadRequest(f"参数 {name} 必须是数字") from None

    # ---- 头部便捷读取 ----

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def bearer_token(self) -> str | None:
        """从 Authorization 头取 Bearer token。

        不用 302 重定向传 token：重定向会让 Authorization 头丢失
        （接口文档 §2.3 明确不使用 301/302）。
        """
        raw = self.headers.get("authorization", "")
        if not raw:
            return None
        parts = raw.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip() or None
        return None

    # ---- 请求体 ----

    async def body_bytes(self, limit: int = 8 * 1024 * 1024) -> bytes:
        """把请求体整体读进内存，超过 limit 抛 413。

        只用于小请求体（JSON 元数据、分片上传的单个分片）。
        **绝不要**用它读整文件上传。
        """
        from ..errors import E1006, AppError

        if self.body is None:
            return b""
        if isinstance(self.body, bytes):
            if len(self.body) > limit:
                raise AppError(E1006)
            return self.body
        buf = bytearray()
        async for chunk in self.body:
            buf.extend(chunk)
            if len(buf) > limit:
                # 边读边判：等读完再判的话，2GB 上传会先把内存吃满。
                raise AppError(E1006)
        return bytes(buf)

    async def json(self, limit: int = 1024 * 1024) -> dict[str, Any]:
        """解析 JSON 请求体。空体返回 {}，非法 JSON 抛 400。"""
        from ..errors import BadRequest

        raw = await self.body_bytes(limit)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BadRequest("请求体不是合法 JSON") from None
        if not isinstance(data, dict):
            raise BadRequest("请求体必须是 JSON 对象")
        return data

    def param_int(self, name: str) -> int:
        """取路径参数并转 int，失败抛 400（而不是 500）。"""
        from ..errors import BadRequest

        raw = self.params.get(name, "")
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise BadRequest(f"路径参数 {name} 必须是整数") from None

    @property
    def elapsed_ms(self) -> float:
        return (time.time() - self.started_at) * 1000.0

    def __repr__(self) -> str:
        return f"Ctx({self.method} {self.path} via={self.via} id={self.request_id})"


@dataclass(slots=True)
class Reply:
    """一次响应的协议无关表示。

    `body` 可以是 bytes（小响应）、异步字节流（文件/缩略图）、或 None。
    流式响应让 2GB 文件的下载内存占用与文件大小无关。
    """

    status: int = 200
    headers: Headers = field(default_factory=Headers)
    body: Body = None
    media_type: str | None = None

    # ---- 便捷设置 ----

    def set(self, name: str, value: str) -> "Reply":
        self.headers[name] = value
        return self

    def set_etag(self, etag: str, *, weak: bool = False) -> "Reply":
        """设置 ETag。传入的应是**裸值**，这里负责加引号。

        统一加引号很重要：调用方手写 `f'"{v}"'` 时很容易漏掉或写错，
        而 ETag 格式错误会让浏览器的条件请求直接失效（每次都重下）。
        """
        v = etag if etag.startswith('"') or etag.startswith('W/"') else f'"{etag}"'
        if weak and not v.startswith("W/"):
            v = "W/" + v
        self.headers["ETag"] = v
        return self

    def set_cache(self, max_age: int, *, immutable: bool = False,
                  private: bool = False) -> "Reply":
        parts = ["private" if private else "public", f"max-age={max_age}"]
        if immutable:
            parts.append("immutable")
        self.headers["Cache-Control"] = ", ".join(parts)
        return self

    def set_length(self, n: int) -> "Reply":
        self.headers["Content-Length"] = str(n)
        return self

    @property
    def is_stream(self) -> bool:
        return self.body is not None and not isinstance(self.body, bytes)

    @property
    def is_empty(self) -> bool:
        return self.body is None or self.body == b""

    def __repr__(self) -> str:
        kind = "stream" if self.is_stream else (
            f"{len(self.body)}B" if isinstance(self.body, bytes) else "empty")
        return f"Reply({self.status} {self.media_type or '-'} {kind})"


# --------------------------------------------------------------------------
# 响应构造器
# --------------------------------------------------------------------------

_JSON_SEP = (",", ":")
# 紧凑分隔符：手机端在弱网下每次请求省下的字节都是真实的。
# 用 ensure_ascii=False 让中文按 UTF-8 原样输出（转义成 \uXXXX 会大 3 倍）。


def json_reply(data: Any, status: int = 200, *,
               headers: Mapping[str, str] | None = None) -> Reply:
    """构造 JSON 响应。

    ⚠️ 成功响应**不套信封**：顶层永远直接是对象或 `{items: [...]}`。
    只有错误才用 `{"error": {...}}`（接口文档 §2.2）。
    """
    body = json.dumps(data, ensure_ascii=False, separators=_JSON_SEP).encode("utf-8")
    r = Reply(status=status, body=body, media_type="application/json; charset=utf-8")
    r.headers["Content-Type"] = r.media_type
    r.headers["Content-Length"] = str(len(body))
    for k, v in (headers or {}).items():
        r.headers[k] = v
    return r


def text_reply(text: str, status: int = 200) -> Reply:
    body = text.encode("utf-8")
    r = Reply(status=status, body=body, media_type="text/plain; charset=utf-8")
    r.headers["Content-Type"] = r.media_type
    r.headers["Content-Length"] = str(len(body))
    return r


def bytes_reply(data: bytes, media_type: str, status: int = 200) -> Reply:
    r = Reply(status=status, body=data, media_type=media_type)
    r.headers["Content-Type"] = media_type
    r.headers["Content-Length"] = str(len(data))
    return r


def empty_reply(status: int = 204) -> Reply:
    """无正文响应。204 不带 Content-Length（RFC 7230 明确禁止）。"""
    return Reply(status=status, body=None)


def stream_reply(chunks: AsyncIterator[bytes], media_type: str,
                 status: int = 200, *,
                 length: int | None = None) -> Reply:
    """流式响应。

    传 length 时会带上 Content-Length，让客户端能显示下载进度；
    不传则用分块传输。缩略图知道大小（文件 stat），原图也大多知道。
    """
    r = Reply(status=status, body=chunks, media_type=media_type)
    r.headers["Content-Type"] = media_type
    if length is not None:
        r.headers["Content-Length"] = str(length)
    return r


def file_reply(chunks: AsyncIterator[bytes], media_type: str, length: int,
               status: int = 200, *,
               filename: str | None = None) -> Reply:
    """文件下载响应。

    ⚠️ 不使用 301/302 重定向到文件 URL：重定向会丢 Authorization 头，
    而照片是私有的，不能靠"URL 猜不到"来保护。
    """
    r = stream_reply(chunks, media_type, status, length=length)
    if filename:
        # 文件名可能是中文，必须用 RFC 5987 编码，否则某些浏览器会乱码。
        from urllib.parse import quote

        r.headers["Content-Disposition"] = (
            f"inline; filename*=UTF-8''{quote(filename)}")
    return r


async def iter_body(reply: Reply) -> AsyncIterator[bytes]:
    """统一把 Reply.body 变成异步字节流，供各适配器消费。"""
    body = reply.body
    if body is None:
        return
    if isinstance(body, bytes):
        if body:
            yield body
        return
    async for chunk in body:
        if chunk:
            yield chunk
