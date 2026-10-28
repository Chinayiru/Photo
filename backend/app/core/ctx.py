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

#: SQLite 的 INTEGER 是**有符号 64 位**。路径参数最终都会绑进 `WHERE id = ?`，
#: 超出这个区间的 Python 整数会让 `sqlite3` 抛 `OverflowError`（→ 500）。
INT64_MIN: int = -(2 ** 63)
INT64_MAX: int = 2 ** 63 - 1


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
    #: 本次响应要下发的 cookie：`(name, value, max_age_seconds)`。
    #:
    #: ★ 为什么挂在 `Ctx` 上而不是直接写 `Reply`：鉴权发生在**拿到
    #: Reply 之前**（`auth.require_client` 在中间件里跑，处理函数才
    #: 负责造 Reply）。`require_client` 需要"发现客户端还在用
    #: Authorization 头就顺手补一个 cookie"来自愈老版本前端，
    #: 而它手上根本没有 Reply 对象。让 dispatch 在收尾时统一搬运。
    set_cookies: list[tuple[str, str, int]] = field(default_factory=list)
    #: 应用级依赖（cfg/db/thumbs/indexer 的容器，类型是 app.state.AppState）。
    #: 放在 Ctx 上而不是让处理函数 `from ..state import get_state`：
    #: 后者会让处理函数隐式依赖进程级单例，测试无法注入临时库，
    #: 且二期云端派发时"同一个进程里两个不同配置"就再也做不到了。
    #: 这里用 Any 是为了保持 core 层不依赖上层（core 必须能被云端复用）。
    app: Any = None

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

    def cookies(self) -> dict[str, str]:
        """解析 Cookie 头。无 Cookie 或格式非法时返回空 dict。

        不引入 `http.cookies`：它的 `SimpleCookie` 在遇到非法字符时会
        **静默丢弃整条** Cookie 头（而不是丢掉那一个键值对），
        于是一个无关的坏 cookie 就能让鉴权失败，且完全没有日志线索。
        这里逐段解析，坏段跳过、好段保留。
        """
        raw = self.headers.get("cookie", "")
        if not raw:
            return {}
        out: dict[str, str] = {}
        for part in raw.split(";"):
            name, sep, value = part.partition("=")
            if not sep:
                continue
            name = name.strip()
            if not name:
                continue
            out[name] = value.strip().strip('"')
        return out

    def cookie_token(self, name: str) -> str | None:
        """从 Cookie 里取 token。"""
        v = self.cookies().get(name, "").strip()
        return v or None

    def bearer_token(self) -> str | None:
        """从 Authorization 头取 Bearer token。

        不用 302 重定向传 token：重定向会让 Authorization 头丢失
        （接口文档 §2.3 明确不使用 301/302）。

        ★ 只认头，**不**回落到 Cookie —— 调用方（`auth.require_client`）
        自己决定要不要接受 Cookie。把回落藏在这里会让
        `test_core.py:489` 那种"有没有凭证"的判断出现两种口径。
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

    async def json(self, limit: int = 1024 * 1024,
                   *, require_json: bool = True) -> dict[str, Any]:
        """解析 JSON 请求体。空体返回 {}，非法 JSON 抛 400。

        ★ `require_json=True` 时会校验 `Content-Type` 是 JSON（审计 F1 第 4 条）。

        ## 为什么这不是"洁癖"，而是攻击链的一环

        CORS 把请求分成两类：`GET`/`HEAD`/`POST` 且 Content-Type 属于
        `text/plain`、`application/x-www-form-urlencoded`、
        `multipart/form-data` 的，是**简单请求**，浏览器不发预检。
        于是 `POST /api/pair` 只要声明 `Content-Type: text/plain`，
        就能带着一段 JSON 文本跨源发出去 —— 而服务端只看 body 不看头，
        照样解析成功并返回一个新 token。

        预检本来是"带 `Authorization` 或 `application/json` 的跨源请求
        会被浏览器先拦一下"这道屏障的落点；服务端不校验 Content-Type，
        这道屏障就形同不存在（审计报告 §2.3 明确说明：攻击者不需要
        用户的任何凭据，它自己 mint 一个）。

        多一个请求头对合法客户端零成本 —— 它们本来就都发
        `application/json`（`frontend/src/api/client.ts:312`）；而且
        `application/json` 这个 Content-Type 本身就会触发预检，
        跨源攻击者**恰恰做不到**。

        `require_json=False` 留给"body 是 JSON 但头不是"的历史调用方
        （目前没有；保留这个开关是为了将来接隧道协议时不被迫改这里）。
        """
        from ..errors import BadRequest

        if require_json:
            ctype = (self.headers.get("content-type") or "").strip().lower()
            # 空头 = 没有 body 语义上的声明。此时若 body 非空，只可能是
            # 手工构造的请求，按"不是 JSON"处理。
            if ctype and not _is_json_content_type(ctype):
                raise BadRequest(
                    f"Content-Type 必须是 application/json（当前 {ctype!r}）")
            if not ctype and (self.headers.get("content-length") or "").strip() not in ("", "0"):
                raise BadRequest("缺少 Content-Type 头（应为 application/json）")

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

    def param_int(self, name: str, *, minimum: int = INT64_MIN, maximum: int = INT64_MAX) -> int:
        """取路径参数并转 int，失败抛 400（而不是 500）。

        ★ 必须挡住 int64 之外的整数（ISSUE-018 / 审计 F8）。Python 的 `int`
        是任意精度，`int("99999999999999999999")` 一点问题都没有，但 SQLite
        的 INTEGER 只有 64 位：这个值一旦绑定进 `WHERE id = ?`，`sqlite3`
        会抛 `OverflowError: Python int too large to convert to SQLite INTEGER`，
        而 `OverflowError` 不在 `errors.EXC_MAP` 里 → 500。

        客户端只要拼一个 `/api/items/9…9/raw` 就能把"不存在"变成"服务器错误"，
        顺带把日志刷满。取值范围必须是路径参数契约的一部分。
        """
        from ..errors import BadRequest

        raw = self.params.get(name, "")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise BadRequest(f"路径参数 {name} 必须是整数") from None
        if value < minimum or value > maximum:
            raise BadRequest(f"路径参数 {name} 超出取值范围")
        return value

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

    #: 待下发的 cookie：`(name, value, max_age_seconds)`。
    #:
    #: ★★ 为什么单独放一个列表而不是塞进 `headers`：
    #: `headers` 是"头名 → 单值"的映射，同一个 `Set-Cookie` 写两次
    #: 只有最后一个能活下来；而登录/续期/清除三种场景经常要同时下发
    #: 两个 cookie。更关键的是，把两个 cookie 用逗号拼进**一个**
    #: `Set-Cookie` 行是**非法**的 —— RFC 6265 明确要求每个
    #: `Set-Cookie` 独立成行，浏览器会把拼接后的整串当成一个畸形
    #: cookie 丢掉。这个失败**完全静默**：响应 200、body 正常、
    #: 只有缩略图 401。
    cookies: list[tuple[str, str, int]] = field(default_factory=list)

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

    def set_cookie(self, name: str, value: str, *, max_age: int) -> "Reply":
        """记录一个待下发的 cookie。

        ★ 追加到 `self.cookies`（`(name, value, max_age)` 三元组），
        **不是**写 `headers`：同名头在 `Headers` 里只能有一个值，
        两个 cookie 会互相覆盖；而把它们用逗号拼成一行又违反
        RFC 6265，浏览器会静默丢弃整串。
        适配层（`transports/local_http.py: adapt_reply`）用
        `cookie_header_value()` 把每一项还原成独立的一行 `Set-Cookie`。

        `Path` 固定 `/`、`HttpOnly` 固定开启、`SameSite` 固定 `Lax`：
          - `Secure` 在一期**必须关**：局域网是明文 HTTP，浏览器会
            直接丢弃带 Secure 的 cookie，症状是"缩略图仍然 401"；
          - `SameSite=Strict` 会让用户从别的 App 点分享链接进来时
            **不携带 cookie**，表现为"点分享链接进去显示未配对"。
        """
        self.cookies.append((name, value, max_age))
        return self

    def clear_cookie(self, name: str) -> "Reply":
        """下发一个立即过期的 cookie。

        ⚠️ `Max-Age=0` 与"不下发"是两回事：只清 localStorage 而不清
        cookie 时，手机在电脑上点了"移除这台设备"之后刷新页面，
        `<img>` 仍会带着旧 cookie 拿到 200（缩略图照常显示），
        用户会以为撤销没生效。
        """
        return self.set_cookie(name, "", max_age=0)

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


def _is_json_content_type(ctype: str) -> bool:
    """`application/json` / `application/ld+json` / `application/vnd.x+json` 都算。

    不追求严格：真正的判据是"浏览器会不会把它当简单请求"。凡是
    `application/*` 里以 `+json` 结尾的变体，浏览器都不把它归入那三个
    简单类型，所以都该放行。`text/json` 也放行（有些老客户端这么发），
    它在预检语义上属于简单请求 —— 但那个类型**没人用**，且放行它
    需要显式拼这个字符串，不会悄无声息地打开。
    """
    base = ctype.split(";", 1)[0].strip()
    return base == "application/json" or base.endswith("+json") or base == "text/json"


def cookie_header_value(name: str, value: str, max_age: int, *,
                        path: str = "/", http_only: bool = True,
                        secure: bool = False) -> str:
    """把 cookie 的语义值拼成一行 `Set-Cookie` 的值（不含头名）。

    ★ 放在 core 层而不是各适配器里各写一遍：二期加云端隧道适配器时，
    只要忘了拼 `HttpOnly` 或 `SameSite`，安全属性就**静默**降级 ——
    没有报错、没有测试失败，只是 cookie 变得能被 JS 读到。

    不引入 `http.cookies.SimpleCookie`：它输出 `Set-Cookie` 的方式
    依赖调用方再 `.output()`，且遇到非法字符时**静默丢整条**。
    这里的输入全部来自我们自己的常量与 `secrets.token_urlsafe()`，
    无需转义；但仍显式拒绝 `;`/CR/LF，因为 `value` 来自数据库。
    """
    v = str(value)
    if any(c in v for c in (";", "\r", "\n")):
        raise ValueError(f"cookie 值含非法字符 name={name!r}")
    parts = [f"{name}={v}", f"Path={path}", f"Max-Age={int(max_age)}",
             "SameSite=Lax"]
    if http_only:
        parts.append("HttpOnly")
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def empty_reply(status: int = 204) -> Reply:
    """无正文响应。204 不带 Content-Length（RFC 7230 明确禁止）。"""
    return Reply(status=status, body=None)


def stream_reply(chunks: AsyncIterator[bytes], media_type: str,
                 status: int = 200, *,
                 length: int | None = None,
                 headers: Mapping[str, str] | None = None) -> Reply:
    """流式响应。

    传 length 时会带上 Content-Length，让客户端能显示下载进度；
    不传则用分块传输。缩略图知道大小（文件 stat），原图也大多知道。

    headers 用于附加协议头 —— 206 分片下载必须带 Content-Range，
    没有它就只是"一个碰巧少了几百字节的 200"，客户端无法判断续传位置。
    """
    r = Reply(status=status, body=chunks, media_type=media_type)
    r.headers["Content-Type"] = media_type
    if length is not None:
        r.headers["Content-Length"] = str(length)
    for k, v in (headers or {}).items():
        r.headers[k] = v
    return r


def file_reply(chunks: AsyncIterator[bytes], media_type: str, length: int,
               status: int = 200, *,
               filename: str | None = None,
               headers: Mapping[str, str] | None = None) -> Reply:
    """文件下载响应。

    ⚠️ 不使用 301/302 重定向到文件 URL：重定向会丢 Authorization 头，
    而照片是私有的，不能靠"URL 猜不到"来保护。
    """
    r = stream_reply(chunks, media_type, status, length=length, headers=headers)
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
