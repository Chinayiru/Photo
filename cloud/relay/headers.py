"""安全响应头与访问日志（纯 ASGI 中间件，不依赖任何反代）。

## 为什么头必须由应用自己发

2026-10-23 之前这些头写在 `cloud/Caddyfile` 里。部署从 Caddy 换成
Cloudflare quick tunnel（`docs/11` ISSUE-028）之后，Caddy 就不在请求路径上
了 —— 头随之消失，而**没有任何测试会发现**（`docs/07` 的 TC-XSS-SEC-009
断言的是"响应里有 CSP"，但它只在 Docker+Caddy 拓扑下跑过）。
教训与 ISSUE-027 同型：**安全属性必须由唯一在路径上的那一层保证**。

同一份中间件也挂到电脑端的静态伺服上（`backend/app/static.py` 复用
`PAGE_HEADERS`），于是"页面从局域网来"和"页面从云端来"拥有同一套头。

## CSP 为什么长这样

* 从最严的 `default-src`（取值 `'none'`）起步：没显式允许的一律不许。
* `script-src 'self'`：产物是外链 `<script type="module" src="./assets/…">`，
  页面里没有内联脚本，所以**不需要** `'unsafe-inline'`（那一项等于把 XSS
  防线拆掉一半）。
* `style-src 'self' 'unsafe-inline'`：Vite 生产产物把样式打成外链 CSS，
  但 Vue 的 `:style` / CSS `v-bind()` 会写 `el.style`，部分浏览器实现把它
  算作内联样式。这一项保留 `'unsafe-inline'` 是**有意的取舍**：样式注入
  的危害远小于脚本注入，而白屏的代价是产品不可用。
* `img-src … blob: data:`：原图走 `URL.createObjectURL`（`blob:`）、占位图
  走 `data:`。云端页面**不再**放行 `http://*:8787`：`https://域名` 上的页面
  去取 `http://192.168…` 是混合内容，浏览器在 CSP 之前就拦掉了，留着那条
  通配只会让人以为"云端页面能直连局域网"（ISSUE-034）。局域网页面由电脑端
  自己伺服、用自己那份策略（`backend/app/static.py` → `csp_for(lan_port=…)`）。
* `connect-src 'self' https:`：云端页面只跟**自己**（`/d/<id>/api/**`）说话。
  需要额外来源时由 `PB_CLOUD_PAGE_CONNECT` 点名（逗号分隔的 origin）。
* `frame-ancestors 'none'` + `X-Frame-Options: DENY`：点击劫持。实测过
  这个面的代价 —— 攻击者用透明 iframe 把"删除照片"按钮盖在"领取奖品"
  下面，一次点击就是不可逆的批量删除。
* `form-action 'none'` / `base-uri 'none'`：页面不发原生表单、也不用
  `<base>`，禁掉可以拦一类"改 base 劫持相对路径"的攻击。
"""
from __future__ import annotations

from . import netutil

import logging
import time
import uuid
from typing import Any

# 头的定义在 `shared/security_headers.py`：云端伺服（本模块 + `pages.py`）
# 与电脑端伺服（`backend/app/static.py`）是**同一个页面**的两条路径，必须
# 发同一套头。这里只做再导出，方便云端内部按 `headers.PAGE_HEADERS` 取用。
from shared.security_headers import COMMON_HEADERS, PAGE_HEADERS, csp_for

__all__ = [
    "PAGE_HEADERS",
    "COMMON_HEADERS",
    "AccessLogMiddleware",
    "CorsMiddleware",
    "SecurityHeadersMiddleware",
    "csp_for",
]

log = logging.getLogger("pb.cloud.access")


class _Peer:
    """`netutil` 只读 `request.client.host`，这里给个最小替身。"""

    __slots__ = ("host",)

    def __init__(self, host: str) -> None:
        self.host = host


class _ScopeView:
    """把 ASGI scope 包成 `netutil` 认识的 request。

    中间件跑在路由之前，手上只有 scope，拿不到 Starlette 的 `Request`；
    而 `netutil` 只用到 `.client` 与 `.headers` 两个属性，所以用鸭子类型
    适配，不去伪造一个带 receive 的 Request。
    """

    __slots__ = ("client", "headers")

    def __init__(self, scope: dict) -> None:
        peer = scope.get("client")
        self.client = _Peer(str(peer[0]) if peer else "")
        self.headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in (scope.get("headers") or [])
        }


def _is_https(scope: dict) -> bool:
    """这次请求是不是 HTTPS。

    只看 `scope["scheme"]` 不够：`uvicorn.run(proxy_headers=False)` 之后，
    经 cloudflared 进来的请求**永远**是 `http`，于是 HSTS 一次都发不出去
    （2026-10-23 线上实测就是这个症状：https 首页也没有 HSTS）。所以要读
    `x-forwarded-proto`，但**只在对面是我们自己的代理时**才读 —— 否则
    任何人加一个头就能让明文连接被 HSTS 记住，把用户锁在 https 上。
    """
    if scope.get("scheme") == "https":
        return True
    view = _ScopeView(scope)
    if not netutil.is_trusted_proxy(view):
        return False
    proto = (view.headers.get("x-forwarded-proto") or "").split(",")[0]
    return proto.strip().lower() == "https"


class SecurityHeadersMiddleware:
    """给每个 HTTP 响应补安全头；HTTPS 请求额外补 HSTS。

    纯 ASGI 而不是 `BaseHTTPMiddleware`：后者会把响应包进一个 anyio 任务，
    对**流式响应**（`/d/**` 的隧道下载、SSE）会多一层背压，实测过它的
    症状是"大文件下载到一半停住"。

    `page_connect` 是这个部署**额外**允许页面连的来源（`PB_CLOUD_PAGE_CONNECT`）。
    不配就只允许 `'self' https:` —— 中继页面永远不需要直连局域网，理由见
    模块文档里 `img-src` 那条。
    """

    def __init__(self, app: Any, *,
                 page_connect: tuple[str, ...] | list[str] = ()) -> None:
        self.app = app
        self.page_connect = tuple(s for s in page_connect if s)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_https = _is_https(scope)
        page_csp = csp_for(extra_connect=list(self.page_connect),
                           allow_lan_http=False)

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                lowered = {k.decode("latin-1").lower() for k, _ in headers}
                ctype = ""
                for key, value in headers:
                    if key.decode("latin-1").lower() == "content-type":
                        ctype = value.decode("latin-1").lower()
                        break
                wanted = list(COMMON_HEADERS)
                if ctype.startswith("text/html"):
                    wanted.append(("content-security-policy", page_csp))
                if is_https:
                    wanted.append(("strict-transport-security",
                                   "max-age=15552000"))
                for key, value in wanted:
                    if key in lowered:
                        continue
                    headers.append((key.encode("latin-1"),
                                    value.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, _send)


class CorsMiddleware:
    """给**中继根**上的接口补 CORS 头（`/d/**` 由 `main.py` 自己贴）。

    ## 为什么根路径也要

    `cors.py` 的白名单解决的是"页面从局域网来、却要调云端的 `/d/**`"。
    同一条真实路径上还有两个**根路径**调用：

    | 页面从哪来 | 调什么 | 跨源 |
    |---|---|---|
    | `http://192.0.2.10:8787/` | `POST https://域名/api/auth/login` | **是** |
    | `http://192.0.2.10:8787/` | `POST https://域名/api/devices/{id}/pair` | **是** |

    局域网入口上用户仍然要能账号登录（ISSUE-046 修订）、要能用自动配对，
    而这两条在 `main.py` 里是**独立路由**、原先一个 CORS 头都没有 ——
    浏览器把响应丢掉，前端只看到 `Failed to fetch`，被当成"密码错了"。

    ## 白名单与凭据

    与 `cors.py` 同一条判据：精确白名单（`PB_CLOUD_CORS_ORIGINS`）+ 私有/
    回环字面量来源。**不发** `Access-Control-Allow-Credentials`，也不回显
    任意 `Origin`。
    """

    def __init__(self, app: Any, *, allowed: Any = ()) -> None:
        self.app = app
        self.allowed = frozenset(allowed or ())

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from .cors import cors_headers, preflight_headers

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in (scope.get("headers") or [])
        }
        origin = headers.get("origin", "")
        if not origin:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        # `/d/**` 自己会贴（`main.py` 的 `_with_cors`），这里只管中继根。
        if path.startswith("/d/"):
            await self.app(scope, receive, send)
            return

        # 预检：就地回 204。三个条件缺一不可（与 `cors.is_preflight` 同判据）。
        if (str(scope.get("method", "")).upper() == "OPTIONS"
                and headers.get("access-control-request-method")):
            from starlette.responses import Response

            extra = preflight_headers(origin, allowed=self.allowed)
            await Response(status_code=204, headers=extra)(
                scope, receive, send)
            return

        extra = cors_headers(origin, allowed=self.allowed)

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start" and extra:
                out = list(message.get("headers") or [])
                have = {k.decode("latin-1").lower() for k, _ in out}
                for key, value in extra.items():
                    if key.lower() in have:
                        continue
                    out.append((key.encode("latin-1"), value.encode("latin-1")))
                message = dict(message, headers=out)
            await send(message)

        await self.app(scope, receive, _send)


class HostAllowlistMiddleware:
    """Reject HTTP requests whose Host is not an explicitly configured name."""

    def __init__(self, app: Any, *, allowed_hosts: list[str] | tuple[str, ...]) -> None:
        self.app = app
        configured = {_normalize_host(h) for h in allowed_hosts if h}
        self.allowed = configured | {"localhost", "127.0.0.1", "::1", "testserver"}

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = next((v.decode("latin-1") for k, v in scope.get("headers", [])
                    if k.lower() == b"host"), "")
        if _normalize_host(raw) not in self.allowed:
            from starlette.responses import JSONResponse

            request_id = uuid.uuid4().hex[:16]
            response = JSONResponse({
                "error": {
                    "code": "E1004",
                    "message": "not_found",
                    "detail": None,
                    "request_id": request_id,
                }
            }, status_code=404, headers={"cache-control": "no-store",
                                        "x-request-id": request_id})
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class RequestIdMiddleware:
    """Ensure every relay response carries a non-empty correlation id."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = list(scope.get("headers") or [])
        raw = next((v.decode("latin-1") for k, v in headers
                    if k.lower() == b"x-request-id"), "")
        if not raw or len(raw) > 64 or not raw.isascii() \
                or not all(ch.isalnum() or ch in "-_" for ch in raw):
            raw = uuid.uuid4().hex[:16]
            headers = [(k, v) for k, v in headers
                       if k.lower() != b"x-request-id"]
            headers.append((b"x-request-id", raw.encode("ascii")))
            scope = dict(scope, headers=headers)

        async def send_with_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers") or [])
                if not any(k.lower() == b"x-request-id" for k, _ in response_headers):
                    response_headers.append((b"x-request-id", raw.encode("ascii")))
                    message = dict(message, headers=response_headers)
            await send(message)

        await self.app(scope, receive, send_with_id)


def _normalize_host(value: str) -> str:
    from urllib.parse import urlsplit

    raw = (value or "").strip().lower().rstrip(".")
    if not raw:
        return ""
    try:
        return (urlsplit("//" + raw).hostname or "").rstrip(".")
    except ValueError:
        return ""


class AccessLogMiddleware:
    """只记 `method + path + status`，**绝不记 query string**。

    为什么不用 uvicorn 的 access log：它把整行请求写进日志，而本产品的
    配对入口是 `GET /d/<id>/?code=NNNNNN`、搜索是 `/api/search?q=…` ——
    前者是**一次性登录凭据**（300 秒内可被重放），后者是用户隐私。日志
    文件又会原样躺在服务器的 `logs\relay.log` 里（`docs/11` ISSUE-032）。

    静态资源（`/assets/*`、`/`）不记：它们量大且没有排查价值。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.monotonic()
        status = {"code": 0}

        async def _send(message: dict) -> None:
            if message["type"] == "http.response.start":
                status["code"] = int(message.get("status", 0))
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            path = str(scope.get("path") or "")
            code = status["code"]
            interesting = (path.startswith("/api/") or path.startswith("/agent/")
                           or code >= 400)
            if interesting:
                log.info("%s %s -> %s (%.0fms)",
                         scope.get("method", "?"), path, code,
                         (time.monotonic() - started) * 1000.0)
