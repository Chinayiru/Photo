"""云端隧道适配器（P1-4）：隧道帧 ⇄ `Ctx` / `Reply`。

## 这个文件为什么存在

`docs/09` §9.6 说二期接入的落点是"把隧道帧还原成 `Ctx`，调用**同一个
Router**，再把 `Reply` 编回帧"。这句话在这里变成两个类：

* `TunnelDispatcher` —— 电脑端收到一个 `REQ_HEAD` 后，构造 `Ctx` 交给
  `router.dispatch()`，然后把 `Reply` 流式编回 `RES_HEAD/RES_BODY/RES_END`。
  它**不碰任何协议细节**：不解析 HTTP、不读 socket，只做数据结构搬运。
* `CloudTunnelTransport` —— `server.transport = "cloud_tunnel"` 时被
  `build_transport()` 造出来。它**同时**启动局域网监听与隧道客户端：
  一期的手机还在家里走 Wi-Fi 直连，二期只是在旁边多开一条云端通路。
  "双通路"不是前端的发明，它是电脑端真的同时在两条路上服务。

## 三条必须守住的边界

1. **业务代码零改动**：`handlers/**` 只认 `Ctx`，这里构造的 `Ctx` 与
   `local_http.adapt_request()` 造出来的**逐字段等价**（除了 `via`）。
   不等价的地方就是"云端能访问、局域网 404"这类幽灵故障的来源。
2. **不转发 Host**：`docs/04` §1.3 把 `Host` 列进禁止转发的头。这不只是
   礼仪问题——电脑端的 `netguard.check_host()` 会拒绝任何非本机 Host，
   转发手机的 Host（`pb.example.com`）会让**每一个**隧道请求 404。
   同时这也让 DNS rebinding 防护在云端通路天然成立。
3. **`client_ip` 绝不能留空**：`auth.is_loopback()` 把空串当回环
   （审计 F11），空 `client_ip` 会让 `/panel/**` 在云端通路上"看起来
   来自本机"。这里用 `cloud` 这个非 IP 字面值兜底，它既不是回环也不会
   被误认成某个真实网段。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from shared.mux import MuxClosed
from shared.protocol import ProtocolError

from ..core.ctx import (
    CLOUD,
    Ctx,
    Headers,
    Reply,
    iter_body,
)
from ..core.dispatch import Router
from ..errors import AppError
from . import ServerTransport, register
from .local_http import LocalHttpTransport

__all__ = ["CloudTunnelTransport", "TunnelDispatcher", "PATH_MAX", "ALLOWED_METHODS"]

log = logging.getLogger(__name__)

#: `REQ_HEAD.path` 的长度上限（`docs/04` §2.3.1）。
PATH_MAX: int = 2048

#: 隧道允许承载的方法。与 `local_http.ALL_METHODS` 保持一致 ——
#: 少一个 `OPTIONS` 会让前端的跨源预检在云端通路上静默失败。
ALLOWED_METHODS: frozenset[str] = frozenset({
    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS",
})

#: 从 `REQ_HEAD.headers` 里接受的请求头（小写）。
#:
#: 白名单而不是黑名单：隧道是"手机 → 云 → 电脑"的三段路，任何一段的
#: 中间人都可能往里塞头，而电脑端的 `auth` 只信任自己解析出来的东西。
#: 关键的三条被**刻意排除**：
#:   * `host`    —— 见模块 docstring 第 2 条；
#:   * `cookie`  —— 云端 cookie 是云端的，不该出现在电脑端的鉴权上下文里；
#:   * `x-forwarded-*` / `forwarded` —— 电脑端不信任它们（审计 F1），
#:     转发只会让"来源不可伪造"这条不变式在日志里看起来被违反了。
FORWARD_REQ_HEADERS: frozenset[str] = frozenset({
    "accept",
    "accept-language",
    "authorization",
    "content-type",
    "content-range",
    "if-none-match",
    "if-modified-since",
    "range",
    "user-agent",
    "x-pb-client",
    "x-request-id",
})

#: 回给手机的响应头（小写）。`content-length` 由适配器自己按实际字节数
#: 决定，不让 handler 的声明值透传 —— 声明值与实际不符时，手机端会
#: 卡在"等剩余字节"直到超时。
FORWARD_RES_HEADERS: frozenset[str] = frozenset({
    # `allow` 不是可有可无的：电脑端对"路径存在、方法不对"回 405 +
    # `Allow: GET, HEAD`，丢掉它之后手机只知道 405、不知道**该用什么
    # 方法**，调试的人会去怀疑路径拼写。
    "allow",
    "accept-ranges",
    "cache-control",
    "content-disposition",
    "content-range",
    "content-type",
    "etag",
    "last-modified",
    "vary",
    "x-content-type-options",
    "x-request-id",
})


def _clean_path(raw: Any) -> str:
    """校验并归一化 `REQ_HEAD.path`。非法直接抛 `E5004`。

    `docs/04` §2.3.1 的约束：必须以 `/api/` 开头、禁 `..`、长度 ≤ 2048。
    这三条针对的是**云端**（它必须拒绝任何越权路径，不能指望电脑端兜底），
    所以电脑端再判一次——两端判同一个函数，云端被绕过时电脑端仍然安全。
    """
    if not isinstance(raw, str) or not raw:
        raise _PathRejected("缺少 path")
    if len(raw) > PATH_MAX:
        raise _PathRejected(f"path 过长（{len(raw)} > {PATH_MAX}）")
    if "\\" in raw or "\x00" in raw:
        raise _PathRejected("path 含反斜杠或 NUL")
    if ".." in raw.split("?")[0].split("/"):
        raise _PathRejected("path 含 ..")
    # 只允许 /api/** ：/panel/** 是电脑本机才有的面板，静态资源不在隧道上
    # 承载（前端由云端自己发，见 cloud/relay/main.py）。
    if not (raw == "/api" or raw.startswith("/api/")):
        raise _PathRejected(f"path 不在 /api 下：{raw!r}")
    return raw


class _PathRejected(Exception):
    """路径被拒（映射成错误帧 `E5004`）。"""


def _split_path_query(raw: str) -> tuple[str, dict[str, list[str]]]:
    """把 `REQ_HEAD.path`（可能带 `?`）拆成路径与查询。

    为什么隧道帧里 path 可能带 `?`：`docs/04` §2.3.1 允许多种实现，
    而"把原始 target 一股脑塞进 path"是最省事的做法。这里两种都接受，
    因为**宽容对端比纠正对端便宜**——只要两端最终都得到同一个 `Ctx`。
    """
    if "?" not in raw:
        return raw, {}
    path, _, qs = raw.partition("?")
    query: dict[str, list[str]] = {}
    for pair in qs.split("&"):
        if not pair:
            continue
        k, _, v = pair.partition("=")
        if k:
            query.setdefault(k, []).append(v)
    return path, query


class TunnelDispatcher:
    """把一条隧道请求交给 Router，再把 Reply 编回帧。

    ★ 签名是 `handle(mux, req)`。`shared.mux` 的回调只传 `req`
    （`RequestHandler = Callable[[InboundRequest], Awaitable[None]]`），
    所以装配处必须 `functools.partial(dispatcher.handle, mux)` 把 mux
    绑进去——这比让 dispatcher 自己存一个 `_mux` 更安全：重连时新旧
    两条连接的请求可能短暂并存，存单例会让旧请求的响应被写到新连接上。
    """

    __slots__ = ("router", "cfg")

    def __init__(self, router: Router, cfg: Any = None) -> None:
        self.router = router
        self.cfg = cfg

    async def handle(self, mux: Any, req: Any) -> None:
        """处理一个 `InboundRequest`（`shared.mux` 的回调）。"""
        head = req.head
        rid = req.req_id
        try:
            path, query = _split_path_query(_clean_path(head.get("path")))
        except _PathRejected as exc:
            await mux.res_error(rid, "E5004", f"path not allowed: {exc}", fatal=False)
            return

        method = str(head.get("method") or "GET").upper()
        if method not in ALLOWED_METHODS:
            await mux.res_error(rid, "E5001", f"method not allowed: {method}")
            return

        raw_query = head.get("query")
        if isinstance(raw_query, dict):
            # 云端可能把查询放进独立字段（string 或 string[]）。
            for k, v in raw_query.items():
                if not isinstance(k, str) or not k:
                    continue
                if isinstance(v, list):
                    query[k] = [str(x) for x in v]
                elif v is not None:
                    query[k] = [str(v)]

        headers = Headers()
        raw_headers = head.get("headers")
        if isinstance(raw_headers, dict):
            for k, v in raw_headers.items():
                lk = str(k).lower()
                if lk in FORWARD_REQ_HEADERS and v is not None:
                    headers[lk] = str(v)
        if "content-type" not in headers:
            ct = head.get("content_type")
            if isinstance(ct, str) and ct:
                headers["content-type"] = ct

        ctx = Ctx(
            method=method,
            path=path,
            query=query,
            headers=headers,
            body=self._body_iter(req),
            via=CLOUD,
            client_ip=str(head.get("client_ip") or "cloud"),
        )

        reply = await self.router.dispatch(ctx)
        await self._send_reply(mux, rid, reply)

    @staticmethod
    async def _body_iter(req: Any) -> AsyncIterator[bytes]:
        async for chunk in req.iter_body():
            yield chunk

    async def _send_reply(self, mux: Any, rid: int, reply: Reply) -> None:
        """把 `Reply` 流式写回帧。**不整块缓冲**。

        `docs/01` AC-4.2 要求 2 GB 下载时云端内存 ≤ 200 MB。这条约束在
        电脑端就开始了：这里一旦 `b"".join(...)`，2 GB 原图会先在电脑端
        吃满 2 GB —— 云端再省也没用。

        ★ 错误只能在**发 head 之前**报告。`res_error` 发完 ERROR 帧就
        `_abort(rid)` 释放槽位，此后同一 rid 的 `res_body` 会变成向一个
        已回收的 req_id 写字节——最坏情况是撞上刚分配到同一 id 的新请求，
        把一张照片的字节灌进另一张照片的响应里。所以这里先把 head 的
        内容全部算完再发；发完 head 之后出任何错都改用 CANCEL（对端会
        把已收到的部分当成截断响应丢弃），绝不再发 ERROR。
        """
        head_sent = False
        try:
            length = _body_length(reply)
            headers: dict[str, str] = {}
            for k, v in reply.headers.items():
                lk = k.lower()
                if lk in FORWARD_RES_HEADERS:
                    headers[lk] = str(v)
            if reply.media_type and "content-type" not in headers:
                headers["content-type"] = reply.media_type
            # Set-Cookie 走独立字段：手机端的 `<img>` 依赖 cookie 鉴权
            # （`<img>` 发不了 Authorization 头），两条 cookie 被折叠成
            # 一条就等于其中一条静默失效。
            cookies = [
                _cookie_line(name, value, max_age)
                for name, value, max_age in reply.cookies
            ]
            not_modified = reply.status == 304
            await mux.res_head(rid, reply.status, headers,
                               body_len=length, not_modified=not_modified,
                               set_cookies=cookies)
            head_sent = True
            if not_modified or reply.status == 204:
                await mux.res_end(rid)
                return
            async for chunk in iter_body(reply):
                await mux.res_body(rid, chunk)
            await mux.res_end(rid)
        except (MuxClosed, asyncio.CancelledError):
            # 连接没了 / 上层要求停：已经没有任何可写的对端。
            raise
        except (AppError, ValueError) as exc:
            log.warning("隧道响应编码失败 rid=%s：%s", rid, exc)
            await self._abort_reply(mux, rid, head_sent, "E5002", "响应编码失败")
        except Exception:      # noqa: BLE001 —— 不能让一条坏响应杀掉隧道
            log.exception("隧道响应编码异常 rid=%s", rid)
            await self._abort_reply(mux, rid, head_sent, "E5002", "本地处理失败")

    @staticmethod
    async def _abort_reply(mux: Any, rid: int, head_sent: bool,
                           code: str, message: str) -> None:
        """head 已发 → CANCEL；head 未发 → ERROR。理由见 `_send_reply`。"""
        try:
            if head_sent:
                await mux.cancel(rid, code)
            else:
                await mux.res_error(rid, code, message)
        except (MuxClosed, ProtocolError):
            pass


def _int_or_none(raw: Any) -> int | None:
    """把 `Content-Length` 这样的头值转成 int；坏值当"不知道"。

    流式响应的长度只能从 handler 自己写的 `Content-Length` 里读（
    `file_reply` 会给），而 `Reply.headers` 是 `Headers`（大小写不敏感），
    值可能是 `None`、空串、甚至被上游改坏的非数字 —— 任何一种都不该
    让一条正常的缩略图响应变成 500。
    """
    if raw is None:
        return None
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _body_length(reply: Reply) -> int | None:
    """本次响应体的字节数；未知返回 None（手机端按分块处理）。

    bytes 体按实际长度算，**不信** handler 声明的 `Content-Length`：
    声明值与实际不符时，手机端会卡在"等剩余字节"直到超时，而 304/204
    本来就没有体（RFC 7230 禁止 204 带 Content-Length）。
    """
    if reply.status in (204, 304):
        return None
    body = reply.body
    if isinstance(body, (bytes, bytearray)):
        return len(body)
    return _int_or_none(reply.headers.get("content-length"))


def _cookie_line(name: str, value: str, max_age: int) -> str:
    """与 `core.ctx.cookie_header_value` 同形（这里不 import 是为了
    让 `set-cookie` 以"数组里的一个字符串"形态进 JSON，而不是被
    Headers 的 dict 语义折叠掉）。"""
    from ..core.ctx import cookie_header_value

    return cookie_header_value(name, value, max_age)


class CloudTunnelTransport(ServerTransport):
    """局域网 + 云隧道的复合通路。

    ## 为什么不二选一

    一期的手机在家里走 Wi-Fi 直连（延迟 ~5 ms，不消耗云流量），二期
    只是"出门也能用"。把局域网关掉才能开云隧道的设计，等于要求用户
    "出门前改配置"——而 `docs/03` §5.1 的双通路状态机明确假设**两条路
    同时可用**，由手机自己探测择优。

    ## 启动顺序

    先起局域网（失败了就整体失败，因为它是主通路），再起隧道客户端
    （失败只记日志、继续重连：云端挂了不该让家里的相册变成打不开）。
    这正是 `docs/09` DoD 第 5 条"杀电脑端重启后隧道 60 s 内自动恢复"
    要求的韧性方向。
    """

    name = "cloud_tunnel"

    def __init__(self, cfg: Any, router: Router) -> None:
        self.cfg = cfg
        self._lan = LocalHttpTransport(cfg, router)
        self._dispatcher = TunnelDispatcher(router, cfg)
        self._client: Any = None
        self._started = False

    # --- ServerTransport ---

    async def start(self) -> None:
        from ..tunnel import TunnelClient, set_tunnel

        await self._lan.start()
        base = str(self.cfg.get("cloud.base_url", "") or "").strip()
        token = str(self.cfg.get("cloud.agent_token", "") or "").strip()
        if not base or not token:
            log.warning(
                "server.transport=cloud_tunnel 但 cloud.base_url / "
                "cloud.agent_token 未配置，云端通路不启动（局域网正常）")
            set_tunnel(None)
            self._started = True
            return
        self._client = TunnelClient(
            self.cfg,
            dispatcher=self._dispatcher,
            base_url=base,
            agent_token=token,
        )
        # ★ 必须登记到进程级单例，否则 `GET /api/system/info` 的
        #   `tunnel` 段永远报 offline（`handlers/system.py:_tunnel_info`
        #   读的就是 `get_tunnel()`），手机端也就永远学不到云端基址
        #   `public_base` —— 表现是"手机在家能用、出门全灰"。
        set_tunnel(self._client)
        await self._client.start()
        self._started = True

    async def stop(self) -> None:
        from ..tunnel import set_tunnel

        if self._client is not None:
            await self._client.stop()
            self._client = None
        # 先清单例再停局域网：顺序反过来的话，停机过程中若有请求打到
        # `/api/system/info`，会读到一个已经 `stop()` 的客户端（`mode`
        # 仍报 `ws` 但所有请求都会失败）。
        set_tunnel(None)
        await self._lan.stop()
        self._started = False

    @property
    def urls(self) -> list[str]:
        urls = list(self._lan.urls)
        if self._client is not None:
            cloud = self._client.public_base()
            if cloud:
                urls.append(cloud)
        return urls

    def status(self) -> dict[str, Any]:
        st = self._lan.status()
        st["transport"] = self.name
        st["tunnel"] = (
            self._client.status() if self._client is not None
            else {"mode": "disabled", "connected": False}
        )
        return st


def _register() -> None:
    register("cloud_tunnel", CloudTunnelTransport)
    # `config.py` 的注释里把二期通路称作 "relay"（一期就写上的名字）。
    # 两个名字指向同一个工厂：改配置的人不必去读源码才知道该填哪个。
    register("relay", CloudTunnelTransport)


_register()
