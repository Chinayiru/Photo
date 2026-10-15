"""局域网 HTTP 传输：Starlette Request ↔ Ctx 的薄适配器。

## 职责边界

本模块**只做协议翻译**，不含任何业务判断：

    HTTP 请求 ──adapt_request──▶ Ctx ──Router──▶ Reply ──adapt_reply──▶ HTTP 响应

这样二期写 `cloud_tunnel.py` 时，只需把隧道帧翻译成同样的 Ctx，
业务代码一行都不用动（DP-4）。

## 为什么用 Starlette 而不是 FastAPI

FastAPI 的价值是 Pydantic 校验与自动文档，但我们的业务函数签名是 `(Ctx) -> Reply`，
FastAPI 的这两个能力都用不上，反而多一层依赖与一个会抛 422 的校验器
（接口文档明确要求不使用 422，统一转 400）。Starlette 只提供 ASGI 骨架，
正好够用。

## 为什么不用 `StreamingResponse` 处理大文件

Starlette 的 StreamingResponse 会把整个响应体当异步生成器消费，
这没问题；真正要小心的是**不要**在适配层把 Reply.body 读完再发送
—— 那会让 2GB 下载占用 2GB 内存。`_asgi_body` 保持流式转发。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Final, Iterable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route as StarletteRoute

from ..config import Config, check_bind_safety, local_ipv4_addresses
from ..core import Ctx, Reply, iter_body, json_reply
from ..errors import AppError, E1006, E3001, to_app_error
from .base import ServerTransport

log = logging.getLogger(__name__)

__all__ = ["LocalHttpTransport", "adapt_request", "adapt_reply"]

#: 单次从请求体读出的块大小。64KB 是 ASGI 常见实现（uvicorn/h11）的
#: 内部缓冲量级，与之对齐可以少一次拷贝。
CHUNK = 64 * 1024

#: 等待 uvicorn 真正开始监听的上限（秒）。
#: 局域网内绑定是毫秒级操作，10 秒还没起来说明端口被占用或权限不足，
#: 继续等只会让用户以为"启动中"。
STARTUP_TIMEOUT = 10.0

#: 交给 catch-all 路由的方法集合。
#: 必须列全并显式传给 Starlette —— 见 build_app() 里的说明，
#: 省略会让 Starlette 只放行 GET。
ALL_METHODS: Final[tuple[str, ...]] = (
    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS",
)


async def adapt_request(request: Request) -> Ctx:
    """Starlette Request → Ctx。

    注意 Starlette 的 `request.query_params` 是 MultiDict，
    同名参数会重复出现（`?tag=a&tag=b`），所以这里聚合成 list。
    """
    q: dict[str, list[str]] = {}
    for k in request.query_params:
        q.setdefault(k, []).extend(request.query_params.getlist(k))

    client_ip = ""
    if request.client is not None:
        client_ip = request.client.host or ""

    return Ctx(
        method=request.method,
        # request.url.path 已是**解码后**的路径（%E7%85%A7 → 照），
        # 业务层不需要再解码，也就不会出现二次解码漏洞。
        path=request.url.path,
        query=q,
        headers=dict(request.headers),
        body=_request_body(request),
        via="lan",
        client_ip=client_ip,
    )


async def _request_body(request: Request) -> AsyncIterator[bytes]:
    """把请求体做成异步字节流，**不预读**。

    用 `request.stream()` 而不是 `await request.body()`：
    后者会把整个 2GB 上传读进内存。
    """
    async for chunk in request.stream():
        if chunk:
            yield chunk


def _asgi_body(reply: Reply) -> AsyncIterator[bytes] | bytes | None:
    """把 Reply.body 转成 Starlette 能消费的形态。

    bytes 直接给（Starlette 会算 Content-Length 并一次性发送）；
    异步流保持流式（大文件不占内存）。
    """
    body = reply.body
    if body is None or isinstance(body, bytes):
        return body
    return _stream(body)


async def _stream(src: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    async for chunk in src:
        yield chunk


def adapt_reply(reply: Reply) -> Response:
    """Reply → Starlette Response。"""
    headers = reply.headers.as_dict()
    media_type = reply.media_type

    # 已经手动设过 Content-Type 的就不要重复设（Starlette 会追加成两个头）。
    headers.pop("content-type", None)

    if reply.body is None:
        # 204/304 不允许有正文。Starlette 的 Response 在 status 204 时
        # 不会自动去掉 Content-Length，这里显式清掉避免协议违规。
        headers.pop("content-length", None)
        return Response(content=None, status_code=reply.status,
                        headers=headers, media_type=media_type)

    if isinstance(reply.body, bytes):
        return Response(content=reply.body, status_code=reply.status,
                        headers=headers, media_type=media_type)

    return StreamingResponse(_stream(reply.body), status_code=reply.status,
                             headers=headers, media_type=media_type)


class LocalHttpTransport(ServerTransport):
    """基于 uvicorn 的局域网 HTTP 服务。

    ⚠️ 默认只绑回环（`127.0.0.1`）。要让手机连上必须显式配置
    `server.host`（如 `0.0.0.0`）且 `server.allow_lan=true`。
    理由：装好但还没配对的窗口期里，若默认绑 0.0.0.0，
    同网段的任何人（访客 Wi-Fi、公司网络）都能探测到 8787 并尝试访问照片库。
    开放局域网应当是一个**显式的配置动作**，而不是默认后果。
    """

    name = "local_http"

    def __init__(self, cfg: Config, router: Any, *,
                 host: str | None = None, port: int | None = None) -> None:
        self.cfg = cfg
        self.router = router
        self.host = host if host is not None else str(cfg.get("server.host", "127.0.0.1"))
        self.port = int(port if port is not None else cfg.get("server.port", 8787))
        self._server: Any = None
        self._task: asyncio.Task[None] | None = None
        self._app: Starlette | None = None
        self._actual_port = self.port
        self._started = asyncio.Event()

    # ------------------------------------------------------------------
    # ASGI 应用
    # ------------------------------------------------------------------

    def build_app(self) -> Starlette:
        """构造 Starlette 应用。

        只有一条 catch-all 路由：所有请求都交给 Router 处理。
        不把每条业务路由注册进 Starlette —— 那样就有两份路由表，
        两份一定会漂移（表现：局域网能访问、云端 404）。
        """
        if self._app is not None:
            return self._app

        async def endpoint(request: Request) -> Response:
            ctx = await adapt_request(request)
            reply = await self.router.dispatch(ctx)
            return adapt_reply(reply)

        self._app = Starlette(routes=[
            # path 用 "/{path:path}" 捕获任意深度。
            #
            # ⚠️ methods 必须显式列全，不能写 `methods=None` 或省略：
            # Starlette 对"函数端点 + methods=None"会**静默地只允许 GET**
            # （见 starlette/routing.py: `if methods is None: methods = ["GET"]`），
            # 于是 POST /api/echo 会先被 Starlette 挡成 405，根本到不了我们的
            # Router —— 表现是"所有写操作 405，读操作正常"，极难定位。
            StarletteRoute("/{path:path}", endpoint, methods=list(ALL_METHODS)),
        ])
        return self._app

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动 uvicorn。绑定失败时抛出，由调用方决定是否降级。"""
        import uvicorn

        ok, why = check_bind_safety(self.host, self.cfg)
        if not ok:
            raise AppError(E3001, f"绑定地址不安全：{why}")

        app = self.build_app()
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            # 关闭 uvicorn 自己的 access log：我们已有统一的结构化日志，
            # 两份日志会让人分不清哪份是权威。
            access_log=False,
            # 不用 uvicorn 的 reload/workers —— 我们是单进程带托盘的程序，
            # 多 worker 会让内存里的索引器状态分裂成多份。
            lifespan="off",
            # 大文件下载时不让 h11 提前断开。
            timeout_keep_alive=30,
        )
        self._server = uvicorn.Server(config)

        # uvicorn.Server.serve() 会一直阻塞，放到独立任务里。
        # ⚠️ 绝不能设 `config.loaded = True` 来"加速"启动：
        # uvicorn 的 config.load() 负责填 lifespan_class / http_protocol_class
        # 等属性，跳过它会在 serve() 里抛 AttributeError。
        #
        # 也不用 sleep 猜启动时长：端口被占用时 sleep 会让启动日志出现
        # "已启动"却连不上的假象。改为轮询 uvicorn 自己的 started 标志。
        self._task = asyncio.create_task(self._serve(), name="uvicorn")

        deadline = asyncio.get_running_loop().time() + STARTUP_TIMEOUT
        while not self._server.started:
            if self._task.done():
                # serve() 已退出：通常是端口被占用或权限不足。
                exc = self._task.exception()
                await self.stop()
                raise AppError(E3001, f"端口 {self.port} 无法监听：{exc!r}")
            if asyncio.get_running_loop().time() > deadline:
                await self.stop()
                raise AppError(E3001, f"HTTP 服务启动超时（端口 {self.port}）")
            await asyncio.sleep(0.02)

        # 端口写 0 时由系统分配，实际端口要从监听 socket 取回 ——
        # 否则 urls 会给出 http://127.0.0.1:0，二维码扫了也连不上。
        servers = getattr(self._server, "servers", None)
        if servers:
            try:
                self._actual_port = servers[0].sockets[0].getsockname()[1]
            except (IndexError, OSError, AttributeError):
                pass

        log.info("HTTP 服务已启动 %s", " ".join(self.urls) or f"{self.host}:{self._actual_port}")

    async def _serve(self) -> None:
        assert self._server is not None
        try:
            await self._server.serve()
        except Exception:  # noqa: BLE001
            log.exception("HTTP 服务异常退出")

    async def stop(self) -> None:
        """停止服务。幂等。

        不能只设 `should_exit = True` 就走：uvicorn 的 serve() 还要跑完
        shutdown（关监听 socket、等在途连接收尾），此时任务仍 pending。
        若调用方随即关闭事件循环，就会看到
        "Task was destroyed but it is pending!" —— 在真实程序里这意味着
        退出瞬间手机正在下载的照片会被硬切断。
        所以这里等它自然结束，超时才强制 cancel，且 cancel 后仍要 await
        一次让取消真正生效。
        """
        if self._server is not None:
            self._server.should_exit = True

        task = self._task
        self._task = None
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
                # 吞掉取消/异常，但必须 await 到任务真正结束，
                # 否则任务会一直挂着直到事件循环关闭。
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001
                # serve() 内部已把异常记进日志，这里不再重复抛。
                pass

        self._server = None
        log.info("HTTP 服务已停止")

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def port_actual(self) -> int:
        return self._actual_port

    @property
    def urls(self) -> list[str]:
        """手机可访问的地址列表。

        `0.0.0.0` 不是能给手机用的地址，必须换成真实网卡地址；
        同时过滤 Hyper-V/WSL/VMware 的虚拟网卡 —— 它们也在
        `local_ipv4_addresses()` 里，但手机连不上，显示出来只会让用户困惑。
        """
        if self.host in ("127.0.0.1", "localhost", "::1"):
            return [f"http://127.0.0.1:{self._actual_port}"]

        if self.host in ("0.0.0.0", "::"):
            ips = local_ipv4_addresses()
            if not ips:
                return []
            return [f"http://{ip}:{self._actual_port}" for ip in ips]

        return [f"http://{self.host}:{self._actual_port}"]

    def status(self) -> dict[str, object]:
        return {
            "transport": self.name,
            "host": self.host,
            "port": self._actual_port,
            "listening": bool(self._server is not None and
                              getattr(self._server, "started", False)),
            "urls": self.urls,
            "lan_enabled": bool(self.cfg.get("server.allow_lan", False)),
        }
