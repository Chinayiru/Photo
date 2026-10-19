"""云端中继进程装配（P1-1..P1-7 的入口）。

## 一条请求在这里怎么走

```
手机 ──https──> Caddy ──> Starlette
                          ├─ /api/**        云端自有（账号、设备、配对）
                          ├─ /agent/ws      电脑端的 wss 长连接
                          └─ /d/{dev}/**    透明转发进隧道
```

`/api/**` 与 `/d/**` 互不干扰：前者在路由表里是**具体路径**，后者带
`{device_id}` 前缀，Starlette 按注册顺序匹配，`/api/devices` 永远先命中
自己的 handler（`docs/04` §3.9 的"路由优先级"）。

## 为什么不 CORS

云端与 PWA 同源（都是 `https://域名`），跨源请求一个都不需要。云端
因此**不安装**任何 CORS 中间件——一期审计 F1 的教训是"默认关掉比
白名单配错更安全"。手机要是真需要从别的源访问，那是要重新设计的事，
不该由一行 `allow_origins=["*"]` 悄悄开启。

## 为什么所有响应都带 `X-PB-Tunnel`

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
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute

from shared.protocol import MAX_FRAME

from ..relay.auth import login, logout, me
from ..relay.db import CloudDB, open_db
from ..relay.devices import DeviceRegistry, find_device_by_agent_token
from ..relay.devices_api import (
    delete_client,
    device_status,
    list_devices,
    revoked_clients,
)
from ..relay.forward import Forwarder
from ..relay.pairing import issue_pair_code, purge_expired_codes, redeem_pair_code
from ..relay.reply import json_error, json_reply
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
               hosts: list[str] | None = None) -> Starlette:
    """造一个云端应用。测试里直接 `TestClient(create_app(db))`。"""
    database = db if db is not None else open_db()
    database.init()
    registry = DeviceRegistry()
    tunnel = TunnelServer(database, registry, host_allowlist=hosts or _hosts())
    forwarder = Forwarder(registry, database)

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
        # `path` 是 `/d/{id}/` 之后剩下的部分，forwarder 自己从
        # `request.url.path` 剥前缀，这里用不上它。
        device_id = str(request.path_params.get("device_id") or "")
        response = await forwarder.handle(request, device_id)
        # `docs/04` §1.2：这两个头**只**出现在 `/d/**` 的响应上，而且必须
        # 加在错误响应上——"电脑离线"正是最需要它的一次（否则手机只能看到
        # 一个 503，分不清是云端挂了还是电脑合盖了）。
        response.headers["X-PB-Tunnel"] = "relay"
        response.headers["X-PB-Device-Online"] = (
            "1" if registry.get(device_id) is not None else "0")
        return response

    routes = [
        Route("/api/health", health, methods=["GET"]),
        Route("/api/auth/login", _bind(login, database), methods=["POST"]),
        Route("/api/auth/logout", _bind(logout, database), methods=["POST"]),
        Route("/api/auth/me", _bind(me, database), methods=["GET"]),
        Route("/api/devices", _bind(list_devices, database, registry),
              methods=["GET"]),
        Route("/api/devices/{device_id}/status",
              _bind(device_status, database, registry), methods=["GET"]),
        Route("/api/devices/{device_id}/pair",
              _bind(redeem_pair_code, database), methods=["POST"]),
        Route("/api/devices/{device_id}/paircode",
              _bind(issue_pair_code, database), methods=["POST"]),
        Route("/api/devices/{device_id}/clients/{client_id}",
              _bind(delete_client, database), methods=["DELETE"]),
        Route("/api/clients/revoked", revoke_list, methods=["GET"]),
        WebSocketRoute("/agent/ws", agent_ws),
        # ★ `/d/{device_id}/{path:path}` 必须放在 `/api/*` **之后**：
        # Starlette 按顺序匹配，把通配放前面会让 `/api/devices` 变成
        # "device_id 为 devices 的转发"。
        Route("/d/{device_id}/{path:path}", forward,
              methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE",
                       "OPTIONS"]),
    ]

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # ★ 只在这里起后台任务。同时传 `on_startup=[...]` 会让应用启动时
        # 跑两遍（Starlette 把 on_startup 与 lifespan 都执行），于是流量
        # 每秒被落库两次——统计翻倍，而且没人看得出来。
        if not _hosts():
            log.warning("PB_CLOUD_HOSTS 未设置：Host 头不做校验。"
                        "生产部署**必须**设成自己的域名")
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

    return Starlette(routes=routes, lifespan=lifespan)


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
    logging.basicConfig(
        level=os.environ.get("PB_CLOUD_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s")
    log.info("云端中继启动 %s:%s max_frame=%s", host, port, MAX_FRAME)
    # ★ 必须单进程（`workers=1`）：设备注册表是**内存 map**，多 worker
    # 会让"电脑连在 worker A、手机请求打到 worker B"变成常态，表现是
    # 随机 503。真要横向扩展，注册表得换 Redis，那是另一个设计。
    uvicorn.run(create_app(), host=host, port=port, log_level="info",
                ws="websockets", proxy_headers=True)


if __name__ == "__main__":
    main()
