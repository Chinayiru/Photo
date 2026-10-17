"""路由表与统一异常收敛。

## 为什么不用 FastAPI 的 APIRouter

FastAPI 的路由绑定 `Request`/`Response` 与 Pydantic 模型，业务函数就再也
没法在隧道上复用了。这里的 `Router` 只认识 `Ctx`/`Reply`，
两条通路各自套一个薄适配器即可（DP-4）。

## 异常收敛为什么必须集中在一处

接口文档 §6 要求：响应 message 里**绝不能**出现绝对路径、异常类名、堆栈、
SQL 片段、库内文件名、库版本。靠每个处理函数自己 `try/except` 是做不到的
—— 4915 张照片里总有一张会触发某个没预料到的 OSError，
而 `str(OSError)` 在 Windows 上**恰好包含绝对路径**（`D:\\Photos\\...`）。

所以：所有异常只在一个地方被翻译成响应，且只使用 `ErrorSpec` 里的固定文案，
`detail` 只进日志。
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Pattern

from ..errors import (
    E1004,
    E1018,
    AppError,
    to_app_error,
)
from .ctx import Ctx, Reply, json_reply, new_request_id

log = logging.getLogger(__name__)

__all__ = ["Route", "Router", "Handler", "Middleware", "NotFoundRoute", "ECHO_ORIGIN"]

#: `enable_cors(allow_origin=...)` 的特殊值：**回显请求自己的 Origin**。
#: 用它当默认值，而不是 `"*"` —— 见 `enable_cors` 的文档。
ECHO_ORIGIN: str = "echo"

#: 处理函数签名：接收 Ctx，返回 Reply。
Handler = Callable[[Ctx], Awaitable[Reply]]
Fallback = Callable[[Ctx], Awaitable[Reply]]
#: 中间件签名：接收 (Ctx, next)，返回 Reply。
Middleware = Callable[[Ctx, Callable[[Ctx], Awaitable[Reply]]], Awaitable[Reply]]

_PARAM_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass(slots=True)
class Route:
    """一条路由。

    `pattern` 用 `{name}` 占位（如 `/api/items/{id}/thumb`），
    编译成带命名组的正则。手写正则而不是引入 `path-to-regexp` 之类的依赖：
    需求只有"整段匹配的命名参数"，一个正则就够。
    """

    method: str
    pattern: str
    handler: Handler
    name: str = ""
    regex: Pattern[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # 把 {id} 变成 (?P<id>[^/]+)。
        # 用 [^/]+ 而不是 .+ ：路径参数不应跨段，否则 /api/items/{id}
        # 会匹配到 /api/items/1/thumb，把多余的部分吞进 id。
        parts: list[str] = []
        pos = 0
        for m in _PARAM_RE.finditer(self.pattern):
            parts.append(re.escape(self.pattern[pos:m.start()]))
            parts.append(f"(?P<{m.group(1)}>[^/]+)")
            pos = m.end()
        parts.append(re.escape(self.pattern[pos:]))
        # 末尾可选的斜杠：手机端/浏览器有时会带，不该因此 404。
        self.regex = re.compile("^" + "".join(parts) + "/?$")

    def match(self, path: str) -> dict[str, str] | None:
        m = self.regex.match(path)
        return m.groupdict() if m else None

    def __repr__(self) -> str:
        return f"Route({self.method} {self.pattern})"


class NotFoundRoute(Exception):
    """内部信号：路径没匹配到任何路由（用于区分 404 与 405）。"""


class Router:
    """路由集合 + 中间件 + 统一异常收敛。

    典型用法：

        router = Router()

        @router.get("/api/system/ping", name="ping")
        async def ping(ctx: Ctx) -> Reply:
            return json_reply({"ok": True})

        reply = await router.dispatch(ctx)

    `dispatch` 保证**永不抛异常**（除非调用方自己取消），
    任何异常都会变成合法的错误响应 —— 服务器崩在半个响应体上
    比返回一个 500 更糟。
    """

    __slots__ = ("_routes", "_by_method", "_exact", "_middleware", "_cors",
                 "_on_error", "_fallback", "name", "app")

    def __init__(self, name: str = "router", *, app: Any = None) -> None:
        self.name = name
        #: 共享依赖容器（AppState）。**在这里注入而不是让处理函数
        #: 去 import 模块级单例**：处理函数 `from ..state import
        #: get_state` 会让测试无法注入临时库与临时配置，也让"同一进程
        #: 里两个不同配置"（二期云端派发）变得不可能。
        self.app = app
        self._routes: list[Route] = []
        # 精确路径索引：绝大多数请求（缩略图、分片）都会命中，
        # 线性扫 40 条正则虽然不慢，但缩略图瀑布流一秒可能几百个请求。
        self._exact: dict[tuple[str, str], Route] = {}
        self._by_method: dict[str, list[Route]] = {}
        self._middleware: list[Middleware] = []
        self._cors: dict[str, Any] | None = None
        self._on_error: Callable[[Ctx, BaseException], None] | None = None
        self._fallback: Fallback | None = None

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def add(self, method: str, pattern: str, handler: Handler,
            *, name: str = "") -> Route:
        r = Route(method.upper(), pattern, handler, name or handler.__name__)
        self._routes.append(r)
        if "{" not in pattern:
            self._exact[(r.method, pattern.rstrip("/") or "/")] = r
        self._by_method.setdefault(r.method, []).append(r)
        return r

    def get(self, pattern: str, *, name: str = ""):
        return self._decorator("GET", pattern, name)

    def post(self, pattern: str, *, name: str = ""):
        return self._decorator("POST", pattern, name)

    def put(self, pattern: str, *, name: str = ""):
        return self._decorator("PUT", pattern, name)

    def patch(self, pattern: str, *, name: str = ""):
        return self._decorator("PATCH", pattern, name)

    def delete(self, pattern: str, *, name: str = ""):
        return self._decorator("DELETE", pattern, name)

    def head(self, pattern: str, *, name: str = ""):
        return self._decorator("HEAD", pattern, name)

    def _decorator(self, method: str, pattern: str, name: str):
        def wrap(fn: Handler) -> Handler:
            self.add(method, pattern, fn, name=name)
            return fn
        return wrap

    # ------------------------------------------------------------------
    # 中间件
    # ------------------------------------------------------------------

    def use(self, mw: Middleware) -> Middleware:
        """注册中间件。**先注册的先执行**（洋葱模型，同 Express/Django）。

        即：`use(a)` 然后 `use(b)` → 执行顺序 a-in → b-in → 处理函数
        → b-out → a-out。先注册的在外层，能看到后注册的所有效果。
        """
        self._middleware.append(mw)
        return mw

    def enable_cors(self, *, allow_origin: str = ECHO_ORIGIN,
                    allow_headers: str = "Authorization, Content-Type, Content-Range, X-Request-Id",
                    expose_headers: str = "ETag, Content-Range, Content-Length, X-Request-Id",
                    max_age: int = 86400) -> None:
        """启用 CORS。

        局域网直连时手机页面与 API 同源，本不需要 CORS。
        但二期云端隧道下页面来自 https://域名，API 也走同域，同样同源。
        真正需要它的是开发期：Vite dev server 在 5173，API 在 8787，
        不同端口就是跨源，不加这个连不上。

        ★ `allow_origin` 的默认值是 `ECHO_ORIGIN`（`"echo"`），不是 `"*"`：

          `"echo"`（默认）：请求带 `Origin` → 回显该源；不带 → **不发**
              `Access-Control-Allow-Origin`。这是唯一同时满足"开发期跨源
              能用"与"响应里绝不出现通配符"的取值。

          `"*"`：字面通配符，**必须显式传**。本服务的鉴权走 `Authorization`
              头与 cookie，且从不发 `Access-Control-Allow-Credentials`，
              所以通配符在语义上并不比回显更宽松；但它会让"允许任何源"
              这句话出现在响应头里，审计与渗透测试都会标成问题 —— 不当默认。

          其他字符串：固定值，只放行那一个源（二期云端域名可走这里）。

        ## 回显为什么安全

        回显本身不授予任何权限："这个源能否读到响应"由浏览器按自己的源
        比对决定，回显只对发起请求的那个源有效。第三方站点想借受害者
        浏览器读走数据，需要 `Access-Control-Allow-Credentials: true`
        —— 我们从不发这个头（全仓库仅在 `frontend/src/api/events.ts` 的
        注释里提到）。没有凭据时攻击者本来就无需 CORS 即可发请求，
        回显不增加任何攻击面。

        规则 15（`docs/06-安全设计文档.md` §10）的要求因此落在两点：
        默认值不写 `*`，且无 `Origin` 时干脆不发这个头。
        """
        self._cors = {
            "origin": allow_origin,
            "headers": allow_headers,
            "expose": expose_headers,
            "max_age": str(max_age),
        }

    def set_fallback(self, fn: "Fallback") -> None:
        """注册兜底处理函数，只在**所有路由都没匹配上**时调用。

        用于静态前端：手机访问 `/` 或任何前端路由时，由它返回
        `frontend/dist` 里的文件。它**不是**中间件 —— 中间件会包住
        每一个 API 请求，而这个只该在 404 那一刻介入。

        兜底函数自己抛 `AppError(E1004)` 就等价于"确实没有这个路径"，
        仍然返回标准的 404 错误体。
        """
        self._fallback = fn

    def on_error(self, fn: Callable[[Ctx, BaseException], None]) -> None:
        """注册错误回调，用于审计/告警。回调自身抛错会被吞掉。"""
        self._on_error = fn

    # ------------------------------------------------------------------
    # 匹配
    # ------------------------------------------------------------------

    def match(self, method: str, path: str) -> tuple[Route, dict[str, str]]:
        """返回 (路由, 路径参数)。匹配不到抛 NotFoundRoute。"""
        method = method.upper()
        # HEAD 复用 GET 的处理函数：HEAD 的语义就是"GET 但不返回体"，
        # 让每个 GET 再写一遍 HEAD 处理函数只会引入不一致。
        lookup = "GET" if method == "HEAD" else method
        clean = path.rstrip("/") or "/"

        r = self._exact.get((lookup, clean))
        if r is not None:
            return r, {}

        for r in self._by_method.get(lookup, ()):
            params = r.match(path)
            if params is not None:
                return r, params

        raise NotFoundRoute(f"{method} {path}")

    def allowed_methods(self, path: str) -> list[str]:
        """路径存在但方法不对时，用于生成 Allow 头（405 必须带）。"""
        out: list[str] = []
        for r in self._routes:
            if r.match(path) is not None:
                out.append(r.method)
        if "GET" in out and "HEAD" not in out:
            out.append("HEAD")
        return sorted(set(out))

    # ------------------------------------------------------------------
    # 派发
    # ------------------------------------------------------------------

    async def dispatch(self, ctx: Ctx) -> Reply:
        """执行请求，**永不抛出**（除 asyncio.CancelledError）。"""
        if not ctx.request_id:
            ctx.request_id = new_request_id()
        # 补装配 AppState。不覆盖已有值，这样隧道通路可以自己塞
        # 一个（将来同一进程服务两个库时用得上）。
        if ctx.app is None and self.app is not None:
            ctx.app = self.app

        # 先建 Ctx 后建 Reply，保证即使中间件崩了也能带上 request_id ——
        # 用户报障时唯一能用来在日志里定位的就是它。
        try:
            reply = await self._run(ctx)
        except Exception as exc:  # noqa: BLE001 —— 这里就是要兜住一切
            reply = self._error_reply(ctx, exc)

        if self._cors is not None:
            self._apply_cors(ctx, reply)

        # 鉴权中间件（`auth.require_client`）在拿到 Reply **之前** 就跑完了，
        # 它只能把待下发的 cookie 记在 ctx 上。这里统一搬运一次 ——
        # 放在错误响应之后、写 X-Request-Id 之前，任何路径都不会漏掉
        # Set-Cookie（包括"旧前端仍用 Bearer 头、顺手补一个 cookie"的自愈路径）。
        if ctx.set_cookies:
            reply.cookies.extend(ctx.set_cookies)

        reply.headers.setdefault("X-Request-Id", ctx.request_id)
        # 明确禁用 MIME 嗅探：照片库里的内容是用户提供的字节，
        # 如果浏览器把某个 .jpg 嗅探成 HTML，就变成了存储型 XSS。
        reply.headers.setdefault("X-Content-Type-Options", "nosniff")

        if ctx.method.upper() == "HEAD":
            # HEAD 不返回体，但保留 Content-Length（告知大小是 HEAD 的意义）。
            reply.body = None

        log.info("%s %s -> %s %.1fms", ctx.method, ctx.path, reply.status,
                 ctx.elapsed_ms)
        return reply

    async def _run(self, ctx: Ctx) -> Reply:
        try:
            route, params = self.match(ctx.method, ctx.path)
        except NotFoundRoute:
            allowed = self.allowed_methods(ctx.path)
            if allowed:
                # 路径存在、方法不对 → 405 而不是 404。
                # 报 404 会让调试的人去查路径拼写，方向完全错了。
                err = AppError(E1018)
                err.headers = {"Allow": ", ".join(allowed)}
                raise err
            if self._fallback is not None:
                # 静态前端兜底。放在 405 判断**之后**：某个路径确实
                # 注册了别的 HTTP 方法时，应该老实回 405，而不是让
                # 前端外壳用 200 把问题盖住。
                #
                # ⚠️ 兜底**故意不经过中间件**。前端外壳（HTML/JS/CSS）
                # 必须匿名可取 —— 它就是配对页本身，要求 token 才能
                # 拿到它就成了死锁。dist 里只有我们自己构建的代码，
                # 没有任何用户数据；真正的照片仍然走 /api/** 并照常鉴权。
                #
                # 但 `/api/**` 与 `/panel/**` **绝不兜底**：接口路径拼错时
                # 必须拿到 404 错误体。让前端外壳接管会返回 200 + HTML，
                # 前端只会报"JSON 解析失败"，排查方向完全错。
                clean = ctx.path.rstrip("/") or "/"
                if clean.startswith(("/api/", "/panel/")) or clean in ("/api", "/panel"):
                    raise AppError(E1004) from None
                return await self._fallback(ctx)
            raise AppError(E1004) from None

        ctx.params = params

        # 洋葱模型：middleware[0] 在最内层（最靠近处理函数）。
        handler: Callable[[Ctx], Awaitable[Reply]] = route.handler
        for mw in reversed(self._middleware):
            handler = _wrap_mw(mw, handler)

        return await handler(ctx)

    def _error_reply(self, ctx: Ctx, exc: BaseException) -> Reply:
        app_exc = to_app_error(exc)
        # detail 只进日志。响应体里用固定文案，杜绝路径/堆栈/SQL 泄漏。
        # 可预期错误（AppError）不打堆栈 —— 手机在弱网下重试几次就能刷出
        # 几百行无害回溯，真正的问题会被埋掉。
        log.error("%s %s 失败 code=%s detail=%s", ctx.method, ctx.path,
                  app_exc.code, app_exc.detail or type(exc).__name__,
                  exc_info=not isinstance(exc, AppError))
        if self._on_error is not None:
            try:
                self._on_error(ctx, exc)
            except Exception:  # noqa: BLE001
                log.warning("on_error 回调自身失败", exc_info=True)

        headers = dict(app_exc.headers or {})
        reply = json_reply(app_exc.to_response(ctx.request_id),
                           status=app_exc.status, headers=headers)
        # 5xx 不该被任何缓存保存：缓存住一个 500 会让用户长时间打不开照片。
        if app_exc.status >= 500:
            reply.headers["Cache-Control"] = "no-store"
        return reply

    def _apply_cors(self, ctx: Ctx, reply: Reply) -> None:
        """把 CORS 头写进响应。

        `allow_origin` 有三种语义（见 `enable_cors`）：

        * `ECHO_ORIGIN`：有 Origin 就回显，没有就**一个都不发**；
        * `"*"`：显式通配符，原样发；
        * 其他固定值：原样发，并声明 `Vary: Origin`，因为同一个 URL
          对不同源给出的是不同结果。
        """
        assert self._cors is not None
        configured = self._cors["origin"]
        origin = ctx.headers.get("origin")

        if configured == ECHO_ORIGIN:
            if not origin:
                # 没有 Origin 的请求（curl、<img>、顶层导航）不参与 CORS，
                # 发任何 Allow-Origin 都没有意义，发 "*" 更是白白广播。
                return
            reply.headers["Access-Control-Allow-Origin"] = origin
            reply.headers["Vary"] = "Origin"
        else:
            reply.headers["Access-Control-Allow-Origin"] = configured
            if configured != "*":
                # 固定白名单值时响应对所有请求都一样，本来不必 Vary；
                # 但一旦以后改成按 Origin 分支就会漏掉，加上更稳妥。
                reply.headers["Vary"] = "Origin"

        reply.headers["Access-Control-Allow-Headers"] = self._cors["headers"]
        reply.headers["Access-Control-Expose-Headers"] = self._cors["expose"]
        reply.headers["Access-Control-Max-Age"] = self._cors["max_age"]

    # ------------------------------------------------------------------
    # 自省
    # ------------------------------------------------------------------

    @property
    def routes(self) -> tuple[Route, ...]:
        return tuple(self._routes)

    def bind_app(self, app: Any) -> None:
        """注入共享依赖容器。装配顺序上 Router 可能先于 AppState 建好。"""
        self.app = app

    def describe(self) -> list[str]:
        """给启动日志和 /api/system/info 用的路由清单。"""
        return [f"{r.method:6} {r.pattern}" for r in
                sorted(self._routes, key=lambda r: (r.pattern, r.method))]


def _wrap_mw(mw: Middleware, nxt: Callable[[Ctx], Awaitable[Reply]]):
    async def call(ctx: Ctx) -> Reply:
        return await mw(ctx, nxt)
    return call
