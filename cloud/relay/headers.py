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
* `img-src … blob: data: http://*:8787`：原图走 `URL.createObjectURL`
  （`blob:`）、占位图走 `data:`、双通路下手机可能直接从局域网
  `http://192.168.x.x:8787` 取缩略图。
* `connect-src 'self' http://*:8787 https:`：页面可能由局域网伺服、却要
  调云端中继（跨源 `fetch`），而中继域名是运行时才知道的（quick tunnel
  每次重启都变）。收紧到具体域名需要把域名喂进伺服层，见 `docs/11`
  ISSUE-034 的后续项。
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
from typing import Any

# 头的定义在 `shared/security_headers.py`：云端伺服（本模块 + `pages.py`）
# 与电脑端伺服（`backend/app/static.py`）是**同一个页面**的两条路径，必须
# 发同一套头。这里只做再导出，方便云端内部按 `headers.PAGE_HEADERS` 取用。
from shared.security_headers import COMMON_HEADERS, PAGE_HEADERS, csp_for

__all__ = [
    "PAGE_HEADERS",
    "COMMON_HEADERS",
    "AccessLogMiddleware",
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
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_https = _is_https(scope)

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
                    wanted.extend(PAGE_HEADERS[len(COMMON_HEADERS):])
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
