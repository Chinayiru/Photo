"""云端中继进程装配（P1-1..P1-7 的入口）。

## 一条请求在这里怎么走

```
手机 ──https──> Caddy ──> Starlette
                          ├─ /api/**        云端自有（账号、设备、配对）
                          ├─ /agent/ws      电脑端的 wss 长连接
                          ├─ /d/{dev}/api/**透明转发进隧道
                          ├─ /d/{dev}/**    手机端页面与静态资源
                          └─ /**            手机端页面与静态资源（根路径入口）
```

`/api/**` 与 `/d/**` 互不干扰：前者在路由表里是**具体路径**，后者带
`{device_id}` 前缀，Starlette 按注册顺序匹配，`/api/devices` 永远先命中
自己的 handler（`docs/04` §3.9 的"路由优先级"）。

## 为什么云端也要发页面

P1 的目标是"人在外面也能看照片"（`docs/01` G-1），而人出门后手里只有
域名。云端只要不发页面，`https://域名/` 就是一个 404，整条隧道在产品上
**不可达** —— 隧道通着、`/d/{id}/api/**` 全 200，但用户打不开一个能点的
界面。实测过这个缺口（`build/_probe_cloud_entry.py`）：

```
GET /              [404] ct='text/plain' 'Not Found'
GET /index.html    [404] ct='text/plain' 'Not Found'
GET /d/{id}/api/system/ping  [200]
```

页面本体在 `pages.py`。

## 为什么现在**需要** CORS 了

原来这里写着"云端与 PWA 同源，跨源请求一个都不需要"。那句话的前提是
"页面永远从云端加载"，而真实路径有两条：

| 页面从哪来 | 调云端的请求 | 是否跨源 |
|---|---|---|
| `https://域名/d/<id>/` | `https://域名/d/<id>/api/**` | 同源 |
| `http://192.0.2.10:8787/` | `https://域名/d/<id>/api/**` | **跨源** |

第二行才是最常见的：用户在家里扫码配对（页面由电脑端伺服），带着这个
已打开的页面出门，局域网探活失败、状态机回退到云端 —— 此时每一个请求
都是跨源 `fetch`，没有 CORS 头就会被浏览器在响应到达 JS 之前丢掉。云端
因此按**局域网源白名单**（`cors.py`）回 CORS 头，绝不回显任意 `Origin`
（一期审计 F1 的教训）。

## 为什么所有 `/d/**` 响应都带 `X-PB-Tunnel`

`/d/**` 的响应来自家里那台电脑，但它是经云端拼出来的。带上这个头，
排查"到底是云端丢字节还是电脑端没发"时，看响应头就知道请求确实
穿过了隧道，而不用去翻云端日志（云端日志里没有 payload，翻不到）。
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute

from shared.protocol import MAX_FRAME

from ..relay.auth import login, logout, me
from ..relay.cors import cors_headers, is_preflight, origin_of, preflight_headers
from ..relay.db import CloudDB, open_db
from ..relay.devices import DeviceRegistry, find_device_by_agent_token
from ..relay.devices_api import (
    client_me,
    delete_client,
    device_status,
    list_devices,
    revoked_clients,
    refresh_client_token,
)
from ..relay import netutil
from ..relay.forward import Forwarder
from ..relay.headers import (
    AccessLogMiddleware,
    HostAllowlistMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from ..relay.pages import StaticPages
from ..relay.pairing import issue_pair_code, purge_expired_codes, redeem_pair_code
from ..relay.reply import json_error, json_reply, no_content
from ..relay.tunnel import TunnelServer

__all__ = ["create_app", "CLOUD_VERSION"]

log = logging.getLogger("pb.cloud")

#: 云端版本号。与电脑端的 `0.1.0` 分开：云端与电脑可以独立升级，
#: 合成一个版本号会让"手机上看到的版本"永远说不清是哪一端的。
CLOUD_VERSION = "0.1.0"

#: 流量落库间隔（`docs/03` §3.6.2 的 30 秒）。
FLUSH_INTERVAL = 30.0

#: 过期配对码清理间隔（一小时一次就够，码的有效期只有 5 分钟）。
PURGE_INTERVAL = 3600.0

_STARTED_AT = time.time()


# ---------------------------------------------------------------------------
# 应用装配
# ---------------------------------------------------------------------------


def create_app(db: CloudDB | None = None, *,
               hosts: list[str] | None = None,
               dist: str | None = None) -> Starlette:
    """造一个云端应用。测试里直接 `TestClient(create_app(db))`。

    `dist` 是手机端构建产物目录；`None` 时用 `pages.DEFAULT_DIST`
    （仓库根的 `frontend/dist`）。测试要指到临时目录时显式传。
    """
    database = db if db is not None else open_db()
    database.init()
    registry = DeviceRegistry()
    configured_hosts = hosts if hosts is not None else _hosts()
    tunnel = TunnelServer(database, registry, host_allowlist=configured_hosts)
    forwarder = Forwarder(registry, database)
    pages = StaticPages(dist if dist is not None else _web_dir())

    async def health(request: Request) -> Response:
        """`GET /api/health`（匿名，`docs/04` §3.7）。

        只说"进程活着、几台电脑在线"。**不报**数据库路径、磁盘余量、
        账号数量——这个接口是给监控探针用的，而监控探针不需要知道
        这些，公网上任何一个扫描器也不需要。
        """
        return json_reply({
            "ok": True,
            "version": CLOUD_VERSION,
            "uptime_sec": round(time.time() - _STARTED_AT, 1),
            "online_devices": registry.count(),
        })

    async def agent_ws(websocket: Any) -> None:
        await tunnel.handle_agent(websocket)

    async def revoke_list(request: Request) -> Response:
        """`GET /api/clients/revoked`（`agent_token`，`docs/04` §3.6）。

        电脑端每 10 分钟拉一次，用来把"在云端已撤销"的手机在本地也
        踢掉。走 `agent_token` 而不是 `account_token`：这台电脑没有
        用户的账号口令。
        """
        row = find_device_by_agent_token(
            database, _bearer(request.headers.get("authorization")))
        if row is None:
            return json_error("E2002", request_id=_rid(request))
        return json_reply({
            "device_id": str(row["id"]),
            "clients": revoked_clients(database, str(row["id"])),
            "server_time": time.time(),
        }, request_id=_rid(request))

    async def forward(request: Request) -> Response:
        # 路径参数从 `request.path_params` 取（同 `_bind` 的理由：Starlette
        # 的 `request_response` 是 `await f(request)`，不注入关键字参数）。
        device_id = str(request.path_params.get("device_id") or "")

        # ---- ① CORS 预检：就地回 204，一个字节都不进隧道 ----
        # 预检**不带** `Authorization`（浏览器规范如此），转发进去只会被
        # 电脑端判 401，于是真实请求根本不会发出、前端看到"预检失败"。
        if is_preflight(request):
            return _preflight(request)

        # ---- ② 非 `/api/**` 的一律由云端自己服务页面 ----
        # ★ 这里绝不能落进 forwarder：`/d/{id}/**` 是**公开**前缀
        #   （id 只是 128 位能力值，不是凭证），把 `index.html`、
        #   `assets/*`、以及任何拼错的路径都转发进隧道等于让公网免费
        #   驱动用户家里的电脑；而电脑端的 `_clean_path` 本来就只放行
        #   `/api/**`，转发过去必然 403 —— 症状是"云端页面 403"。
        inner = _inner_path(request)
        if not inner.startswith("/api/"):
            tail = _tail_after_device(request)
            return _with_cors(request, await pages.serve(request, tail))

        response = await forwarder.handle(request, device_id)
        # `docs/04` §1.2：这两个头**只**出现在 `/d/**` 的响应上，而且必须
        # 加在错误响应上——"电脑离线"正是最需要它的一次（否则手机只能看到
        # 一个 503，分不清是云端挂了还是电脑合盖了）。
        response.headers["X-PB-Tunnel"] = "relay"
        response.headers["X-PB-Device-Online"] = (
            "1" if registry.get(device_id) is not None else "0")
        return _with_cors(request, response)

    async def pages_root(request: Request) -> Response:
        """根路径入口：`https://域名/`（`pages.StaticPages.__call__`）。

        也要过一遍 CORS：页面本身是同源加载的，但**子资源**在
        `Origin` 存在时（比如页面被局域网源 iframe 嵌入、或 PWA 的
        `manifest` 被抓取）同样有跨源判定的问题，回一条白名单头比
        事后排查"为什么 manifest 拿不到"便宜得多。

        ★ `/api/**` 与 `/agent/**` 上来了也不交给页面：这两个前缀是
        接口，命中它们说明**接口不存在**（路由表里没有）。给一份
        `index.html` 会变成 `200 text/html`，前端 `JSON.parse` 炸掉、
        还会让"接口没实现"看起来像"接口返回了怪东西"（`docs/04`
        规划的 `/api/clients/me` 就是这种状态）。
        """
        raw = request.url.path
        if raw == "/api" or raw.startswith("/api/") or raw.startswith("/agent/"):
            return _with_cors(request, json_error("E1004", request_id=_rid(request)))
        if is_preflight(request):
            return _preflight(request)
        return _with_cors(request, await pages(request))

    routes = [
        Route("/api/health", health, methods=["GET"]),
        Route("/api/auth/login", _bind(login, database), methods=["POST"]),
        Route("/api/auth/logout", _bind(logout, database), methods=["POST"]),
        Route("/api/auth/me", _bind(me, database), methods=["GET"]),
        Route("/api/devices", _bind(list_devices, database, registry),
              methods=["GET"]),
        Route("/api/devices/{device_id}/status",
              _bind(device_status, database, registry), methods=["GET"]),
        # registry 是给「账号登录自动配对」用的：那条路要先确认电脑在线。
        Route("/api/devices/{device_id}/pair",
              _bind(redeem_pair_code, database, registry), methods=["POST"]),
        Route("/api/devices/{device_id}/paircode",
              _bind(issue_pair_code, database), methods=["POST"]),
        Route("/api/devices/{device_id}/clients/{client_id}",
              _bind(delete_client, database), methods=["DELETE"]),
        Route("/api/clients/revoked", revoke_list, methods=["GET"]),
        Route("/api/clients/me", _bind(client_me, database, registry), methods=["GET"]),
        Route("/api/clients/token/refresh",
              _bind(refresh_client_token, database, registry), methods=["POST"]),
        WebSocketRoute("/agent/ws", agent_ws),
        # ★ `/d/{device_id}/{path:path}` 必须放在 `/api/*` **之后**：
        # Starlette 按顺序匹配，把通配放前面会让 `/api/devices` 变成
        # "device_id 为 devices 的转发"。
        # ★ 它必须同时收 OPTIONS：跨源预检要在这里就地回，收不到就得
        #   进 405，而浏览器把"预检失败"报成 `Failed to fetch`。
        Route("/d/{device_id}/{path:path}", forward,
              methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE",
                       "OPTIONS"]),
        # `/d/{id}` 没有尾斜杠时上面那条匹配不到（`{path:path}` 至少要吃
        # 一个字符），而用户从二维码里拿到的地址、以及 `public_base`
        # 的拼接结果都可能是这个形状。补一条只服务 index 的。
        Route("/d/{device_id}", forward,
              methods=["GET", "HEAD", "OPTIONS"]),
        # 根路径的页面入口放最后：它是通配，注册早了会把 `/api/**`
        # 和 `/agent/ws` 全截走。
        Route("/{path:path}", pages_root,
              methods=["GET", "HEAD"]),
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # ★ 只在这里起后台任务。同时传 `on_startup=[...]` 会让应用启动时
        # 跑两遍（Starlette 把 on_startup 与 lifespan 都执行），于是流量
        # 每秒被落库两次——统计翻倍，而且没人看得出来。
        if not configured_hosts:
            log.warning("PB_CLOUD_HOSTS 未设置：仅允许本机测试 Host，公网 Host 将被拒绝")
        # 受信代理没配 = 所有人共用 `127.0.0.1` 一个限流桶（cloudflared
        # 与 80 端口转发都从回环连入）。这个失败模式不会报错，只会让
        # "一个人刷满登录次数 → 所有人登不上"，所以必须在启动时说一次。
        if not netutil.trusted_networks():
            log.warning("PB_CLOUD_TRUSTED_PROXIES 未设置：%s 里所有请求"
                        "共用同一个限流桶（登录 10 次/10 分钟、匿名 "
                        "300 次/秒）。前面有 cloudflared 时**必须**设成 "
                        "`127.0.0.1/32` —— 见 docs/11 ISSUE-030",
                        netutil.TRUSTED_ENV)
        task = asyncio.create_task(_housekeeping(database, forwarder),
                                   name="cloud-housekeeping")
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):   # noqa: BLE001
                pass
            forwarder.meter.flush()
            database.close_all()

    # 中间件顺序 = 从外到内。`AccessLogMiddleware` 在最外层才能记到
    # "整个请求花了多久"，`SecurityHeadersMiddleware` 必需在最外层之内
    # 但早于业务：它要改写 `http.response.start`，而任何内层中间件都可能
    # 提前结束响应（例如 CORS 预检的 204）。
    middleware = [
        Middleware(AccessLogMiddleware),
        Middleware(SecurityHeadersMiddleware),
        Middleware(HostAllowlistMiddleware, allowed_hosts=configured_hosts),
        Middleware(RequestIdMiddleware),
    ]

    async def http_error(request: Request, exc: HTTPException) -> Response:
        if exc.status_code == 404:
            response = json_error("E1004", request_id=_rid(request))
        elif exc.status_code == 405:
            response = json_error("E1018", request_id=_rid(request))
        else:
            response = json_error("E3001", request_id=_rid(request))
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    # 把 registry 挂到 app.state 上：测试要能「伪造一台在线电脑」才测得了
    # 自动配对（真实场景里它由 `/agent/ws` 注册）。不放这里的话，测试要么
    # 起真隧道（重），要么去摸闭包变量（脆）。
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan,
                    exception_handlers={404: http_error, 405: http_error})
    app.state.registry = registry
    app.state.db = database
    return app


def _bind(fn: Any, *deps: Any) -> Any:
    """把 `(request, *deps, device_id=...)` 形状的 handler 变成 Starlette 的 `(request)`。

    为什么用闭包而不是给每个 handler 加默认参数：Starlette 会按 URL
    参数名注入 `device_id`，多出来的默认参数会被当成"URL 参数"去填，
    缺了就直接 500。闭包让 handler 的签名保持纯净，也让这些函数
    能在测试里被单独 `await`。

    ★ 路径参数只能从 `request.path_params` 取。Starlette 的
    `request_response()` 是 `await f(request)`——它**不会**把 URL 变量
    作为关键字参数传进来（那是 FastAPI 的 `Depends` 注入层做的事）。
    早期版本这里读的是闭包自己的 `**path_params`，永远是空字典，
    于是所有带 `{device_id}` 的路由都 `TypeError: missing argument`。
    只有真跑一次 uvicorn 才发现得了：直接 `await fn(req, db, device_id=..)`
    的单元测试全都通过。
    """
    async def endpoint(request: Request) -> Response:
        params = request.path_params
        # 只传 handler 真的收的参数。`issue_pair_code` 是从 `agent_token`
        # 反查设备 id 的（不信任 URL 里的那一段），签名里因此没有
        # `device_id`；无条件塞关键字会 `TypeError: unexpected keyword`。
        wanted = _accepts(fn)
        kwargs: dict[str, Any] = {
            name: params[name]
            for name in ("device_id", "client_id", "path")
            if name in params and name in wanted
        }
        return await fn(request, *deps, **kwargs)

    return endpoint


def _accepts(fn: Any) -> frozenset[str]:
    """`fn` 接受哪些关键字参数（只关心三个路径参数名）。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):     # pragma: no cover —— 内建/C 函数
        return frozenset()
    names = {"device_id", "client_id", "path"}
    return frozenset(
        name for name, p in sig.parameters.items()
        if name in names
        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    )


# ---------------------------------------------------------------------------
# 后台任务
# ---------------------------------------------------------------------------


async def _housekeeping(db: CloudDB, forwarder: Any) -> None:
    """流量落库 + 过期码清理。

    两条都不该失败退出：它们失败的唯一后果是"统计少一点"，而让这个
    协程死掉会让云端悄悄地永远不再统计。
    """
    last_purge = 0.0
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        try:
            forwarder.meter.flush()
        except Exception:       # noqa: BLE001
            log.exception("流量落库失败")
        if time.time() - last_purge >= PURGE_INTERVAL:
            last_purge = time.time()
            try:
                purge_expired_codes(db)
            except Exception:   # noqa: BLE001
                log.exception("清理过期配对码失败")


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _hosts() -> list[str]:
    raw = os.environ.get("PB_CLOUD_HOSTS", "") or ""
    return [h.strip().lower() for h in raw.split(",") if h.strip()]


def _web_dir() -> str | None:
    """手机端构建产物目录（`PB_CLOUD_WEB_DIR` 优先，否则 `pages.DEFAULT_DIST`）。

    给环境变量是为了容器外测试与"把页面放在别处"的部署；正常部署里
    Dockerfile 把 `frontend/dist` 复制进镜像，不设这个变量。
    """
    raw = os.environ.get("PB_CLOUD_WEB_DIR", "").strip()
    return raw or None


def _cors_origins() -> frozenset[str]:
    """配置里显式允许的**额外**跨源来源（逗号分隔）。

    默认空集：局域网/回环源本来就由 `cors.is_lan_origin()` 放行，
    这个变量只用于"页面挂在某个公网域名下"的部署。
    """
    raw = os.environ.get("PB_CLOUD_CORS_ORIGINS", "") or ""
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def _with_cors(request: Request, response: Response) -> Response:
    """按来源白名单给响应补 CORS 头（不命中就原样返回）。"""
    extra = cors_headers(origin_of(request), allowed=_cors_origins())
    for key, value in extra.items():
        response.headers[key] = value
    return response


def _preflight(request: Request) -> Response:
    """就地回一个 204 预检响应。

    ★ 白名单不命中时也回 204，但**不带**任何 `Access-Control-*` 头 ——
    浏览器据此拒绝真实请求，而我们在日志里看不到"有人试过"。回 403
    会让跨源失败与"云端拒绝服务"混在一起，前端无法区分。
    """
    headers = preflight_headers(origin_of(request), allowed=_cors_origins())
    return Response(status_code=204, headers=headers)


def _inner_path(request: Request) -> str:
    """`/d/<id>/api/timeline` → `/api/timeline`（与 `forward._inner_path` 同义）。

    这里必须自己有一份：`main.forward` 要在**决定是否转发之前**判断
    路径，而那一步不能依赖 `Forwarder` 的私有函数。
    """
    path = request.url.path
    if not path.startswith("/d/"):
        return path
    _, _, tail = path[len("/d/"):].partition("/")
    return "/" + tail if tail else "/"


def _tail_after_device(request: Request) -> str:
    """`/d/<id>/assets/x.js` → `assets/x.js`（页面服务要的相对路径）。"""
    path = request.url.path
    if not path.startswith("/d/"):
        return path.lstrip("/")
    _, _, tail = path[len("/d/"):].partition("/")
    return tail


def _bearer(raw: str | None) -> str:
    value = (raw or "").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


def _rid(request: Request) -> str:
    return request.headers.get("x-request-id") or ""


def main() -> None:
    """`python -m cloud.relay.main`（也由 `scripts/cloud_run.py` 调用）。"""
    import uvicorn

    host = os.environ.get("PB_CLOUD_HOST", "127.0.0.1")
    port = int(os.environ.get("PB_CLOUD_PORT", "8000"))
    # 库的位置。容器里必须指到挂载卷上（`/data/cloud.db`），否则
    # `cloud.db` 会落在工作目录 `/app` 里 —— 容器一重建，账号、设备、
    # 配对码、流量统计全部归零，而"重建容器"是升级时的常规动作。
    db_path = os.environ.get("PB_CLOUD_DB", "").strip()
    logging.basicConfig(
        level=os.environ.get("PB_CLOUD_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    log.info("云端中继启动 %s:%s max_frame=%s db=%s",
             host, port, MAX_FRAME, db_path or "cloud.db（相对工作目录）")
    # ★ 必须单进程（`workers=1`）：设备注册表是**内存 map**，多 worker
    # 会让"电脑连在 worker A、手机请求打到 worker B"变成常态，表现是
    # 随机 503。真要横向扩展，注册表得换 Redis，那是另一个设计。
    # ★ 受信代理**默认空**（谁都不信），并且 uvicorn 自己的
    # `--proxy-headers` 一律**关掉**。
    #
    # 旧默认值是 `172.16.0.0/12`（假设前面是 Docker 里的 Caddy）。实际部署
    # 是 Windows + Cloudflare quick tunnel + 80 端口转发，两者都从
    # `127.0.0.1` 连入 —— 那个默认值让 uvicorn **丢弃**真实的
    # `X-Forwarded-For`，于是所有手机共用 `127.0.0.1` 一个限流桶：
    # 一个人刷 10 次假登录，主人自己也登不上（10 次/10 分钟）。
    #
    # 反过来把默认值改成 127.0.0.1「看起来」能修好，但那会让**任何能
    # 直连 8000 的人**伪造 X-Forwarded-For 绕开全部限流。
    #
    # 所以判据统一收到 `netutil`：它先看**直连对端**（不可伪造），只有
    # 对端落在 `PB_CLOUD_TRUSTED_PROXIES` 里才去读 XFF，并且从右往左
    # 跳过受信跳。uvicorn 的 ProxyHeadersMiddleware 会**改写
    # `scope["client"]`**，那会让 `netutil` 再也看不到真实对端（也就
    # 无从判断"这一跳该不该信"），所以这里必须关掉它，只留一条判据。
    #
    # ★ 顺序陷阱：把 `PB_CLOUD_TRUSTED_PROXIES` 设成 127.0.0.1 **同时**
    #   还让 8000 直接对外（绕过 cloudflared）＝ 任何人都能伪造。
    #   正确做法是先只留隧道，再设受信代理。
    uvicorn.run(create_app(open_db(db_path or None)), host=host, port=port,
                log_level="info", ws="websockets",
                proxy_headers=False,
                # **不**回 `Server: uvicorn`：这条指纹告诉扫描器"这里是个
                # 现成的 Python 服务"，配合版本差异就是一份漏洞清单。
                server_header=False,
                # 访问日志由 `AccessLogMiddleware` 接管：uvicorn 的 access
                # log 会把 `?code=NNNNNN`（一次性配对凭据）和用户搜索词
                # 原样写进 `relay.log`（`docs/11` ISSUE-032）。
                access_log=False)


if __name__ == "__main__":
    main()
