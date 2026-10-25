"""电脑端隧道客户端（P1-5）：主动外连云端，永不放弃重连。

## 一句话

家里的电脑没有公网 IP，所以**由它拨出去**——一条到 `wss://<域名>/agent/ws`
的长连接。手机在外面访问云端，云端把请求帧塞进这条连接，电脑处理完再把
响应帧塞回来。这样路由器的 NAT 完全不需要配置。

## 这个文件负责的四件事

1. **握手**：`HELLO` / `HELLO_ACK`，并在 ACK 里拿到心跳间隔与窗口大小。
2. **重连**：指数退避 `1s → 2s → … → 60s`，带 ±20% 抖动，**永不放弃**
   （`docs/11` Q-021）。常驻服务放弃重连等于静默死亡：用户在外面看到
   "电脑离线"，而他家里的电脑其实好好开着。
3. **心跳**：每 `HEARTBEAT_SEC` 发一帧 `PING`。用应用层心跳而不是
   WebSocket 控制帧，因为 Nginx/Caddy 默认**不转发**控制帧，而心跳要
   穿过的正是它们。
4. **桥接**：把帧灌进 `shared.mux.Mux`，由它多路复用；请求交给
   `TunnelDispatcher`（业务代码零改动的落点）。

## 为什么用"收到任何帧"判活而不是"收到 PONG"

手机上翻相册时隧道上永远是满的：一帧 `RES_BODY` 紧接着下一帧。如果
判活条件是"每 75 秒必须收到一个 PONG"，一次 2 GB 的下载会把 PONG 挤在
几十万个 BODY 后面（哪怕只是排队），连接就会在大文件传到一半时被自己
判死——症状是"下载大视频总是失败，小图片没问题"。`docs/04` §2.3.9 的
原话是"存活判据是收到任何帧"，这里照做。

## 断线时在途请求怎么办

`Mux.close()` 会把每个在途 `req_id` 的 head 设成错误、body 塞 `None`，
所以云端每个挂着的 HTTP 响应都会**立刻**收到 `E5011` 而不是等超时。
这一步不能省：隧道断了以后没有任何帧会来，不主动唤醒的话，手机上的
每个 `<img>` 都要等 20 秒才显示占位图，一百个缩略图就是"页面卡死"。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import secrets
import ssl
import time
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from shared.mux import Mux, MuxClosed
from shared.protocol import (
    CREDIT_V2_CAP,
    HEARTBEAT_SEC,
    PONG_TIMEOUT,
    RECONNECT_BASE,
    RECONNECT_JITTER,
    RECONNECT_MAX,
    FrameType,
    wants_credit_v2,
)

log = logging.getLogger(__name__)

__all__ = [
    "TunnelClient",
    "cloud_device_id",
    "next_backoff",
    "public_base_for",
    "get_tunnel",
    "set_tunnel",
]

#: 本期协议版本（`docs/04` §2.4）。
PROTO_VERSION: int = 1

#: 握手超时：发出 HELLO 后多久没收到 HELLO_ACK 就重连。
HELLO_ACK_TIMEOUT: float = 30.0

#: 连续这么多秒没有任何入站帧，判定连接已死（`PONG_TIMEOUT` = 75s
#: 是"两个心跳间隔 + 余量"，这里直接用协议常量）。
DEAD_AFTER: float = PONG_TIMEOUT

#: 稳定连接多久之后把重连次数清零（`docs/04` §2.5：30 秒）。
STABLE_RESET_SEC: float = 30.0

#: 撤销列表的同步周期（`docs/04` §3.6 建议 10 分钟）。
#:
#: 为什么是 10 分钟而不是每秒：`GET /api/clients/revoked` 返回的是**全量**
#: （撤销是单调操作，云端没有 `revoked_at` 列，做不了增量），一次要读
#: 这台电脑名下所有已撤销的配对行。实际最多几十行，但 10 分钟一次的
#: 抖动足以把"用户移除手机"到"手机真的读不到照片"的窗口压到可接受
#: 范围，同时不给云端加一份每秒轮询。
REVOKE_SYNC_SEC: float = 600.0


def next_backoff(attempt: int, *, jitter: float | None = None) -> float:
    """指数退避 + 抖动（`docs/03` §2.5.6）。

    抖动是给"云端重启后几百台电脑同时重连"准备的：没有抖动时它们会
    在同一秒扎堆重连，把刚起来的云端再打死一次（thundering herd）。

    ★ 抖动值用 `secrets` 而不是 `random`。严格说抖动的质量无关安全
    （即使完全可预测，退避仍然单调递增），但 `scripts/check_lint.py`
    规则 19 禁止 `backend/app/**` 出现 `random.*` —— 那条规则保护的是
    配对码与令牌。**给它开"仅此处例外"的缺口会让整条规则失效**：
    下一个需要随机数的人会照着这里的先例绕过它，而其中就会有人是
    在生成凭据。用 `secrets.randbelow` 的代价是每 60 秒最多一次系统
    熵读取，在这里可以忽略。

    `jitter` 参数只为测试可复现（传 0.0 = 无抖动）。
    """
    base = min(RECONNECT_BASE * (2 ** attempt), RECONNECT_MAX)
    if jitter is None:
        # randbelow(2*N+1) - N ⇒ 均匀落在 [-N, N] 的整数分之一
        span = int(RECONNECT_JITTER * 1000)
        frac = (secrets.randbelow(2 * span + 1) - span) / 1000.0
    else:
        frac = jitter
    return base * (1.0 + frac)


def cloud_device_id(cfg: Any = None, fallback: str = "") -> str:
    """这台电脑在**云端**的设备 id（`cloud.device_id`）。

    ## 为什么不能直接用 `state.device_id`

    两个 id 是完全不同的东西，混用会让整条链路在握手阶段就断：

    | | 生成者 | 形态 | 用途 |
    |---|---|---|---|
    | `state.device_id` | 电脑自己（首次启动 `uuid4().hex[:16]`） | 16 hex | 手机上"这是不是我配对过的那台电脑" |
    | `cloud.device_id` | 云端（`scripts/cloud_init.py`，32 hex） | 32 hex | 云端 `devices.id`，`/d/{device_id}/**` 的路径段 |

    云端 `tunnel.py:_handshake` 会校验 `HELLO.device_id` == `agent_token`
    所属设备（`docs/04` §2.4 明确要求"必须校验"，否则 A 的令牌能冒充
    B 上线），而 `agent_token` 是绑在云端那行 `devices.id` 上的。所以
    HELLO 必须报**云端 id**。

    `cloud.device_id` 没配时退回本地 id：这样"云端登记的 id 恰好等于
    本地 id"的部署（例如手工插行）仍然能连，而不是直接连不上。
    """
    if cfg is not None:
        raw = str(cfg.get("cloud.device_id", "") or "").strip()
        if raw:
            return raw
    return fallback


def public_base_for(base_url: str, device_id: str) -> str:
    """把 `cloud.base_url` 变成手机端可用的基址 `https://host/d/<id>`。

    为什么要算这一步：手机端只需要**一个基址**，路径部分与局域网完全
    相同（`docs/09` §9.6 机制②）。而 `cloud.base_url` 的形态有四种常见
    写法（带不带协议、带不带尾斜杠、带不带 `/d/` 前缀、带不带端口），
    每种都在真实部署里出现过。这里统一吃掉，避免"配了却连不上，而
    日志里那句话看起来完全正常"。
    """
    raw = (base_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = parts.netloc or parts.path.strip("/")
    if not host:
        return ""
    scheme = parts.scheme or "https"
    base_path = parts.path.rstrip("/")
    # 用户可能把 `/d/<id>` 也写进配置里（从另一台设备抄过来的地址）。
    # 重复拼接会得到 `/d/<id>/d/<id>`，云端会 404，所以先剥掉。
    if base_path.startswith("/d/"):
        base_path = ""
    return urlunsplit((scheme, host, f"{base_path}/d/{device_id}", "", ""))


class TunnelClient:
    """一条到云端的隧道。`start()` 后自行维持，`stop()` 幂等。"""

    __slots__ = ("cfg", "_dispatcher", "_base_url", "_agent_token",
                 "_task", "_stop", "_mux", "_ws", "_state", "_attempt",
                 "_connected_at", "_last_error", "_session_id", "_next_wake",
                 "_peer_heartbeat", "_device_id", "_revoke_task",
                 "_credit_v2")

    def __init__(self, cfg: Any, *, dispatcher: Any, base_url: str,
                 agent_token: str) -> None:
        self.cfg = cfg
        self._dispatcher = dispatcher
        self._base_url = base_url
        self._agent_token = agent_token
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._mux: Mux | None = None
        self._ws: Any = None
        self._attempt = 0
        self._connected_at: float | None = None
        self._last_error: str | None = None
        self._session_id: str | None = None
        self._next_wake: float | None = None
        self._peer_heartbeat: float = HEARTBEAT_SEC
        #: 云端是否支持带 payload 的 `CREDIT`（`CREDIT_V2_CAP`）。握手时定。
        self._credit_v2: bool = False
        self._device_id = ""
        self._revoke_task: asyncio.Task[None] | None = None
        self._state: str = "idle"

    # --- 生命周期 ---

    async def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="tunnel-client")

    async def stop(self) -> None:
        """幂等停止。先置停止位再取消，避免"刚连上又被重连"。"""
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._close_conn("stopped")

    # --- 对外状态（`/api/system/info` 的 tunnel 段读这里）---

    def public_base(self) -> str:
        dev = self._device_id
        if not dev:
            return ""
        return public_base_for(self._base_url, dev)

    def status(self) -> dict[str, Any]:
        return {
            "mode": "ws" if self._state == "connected" else "offline",
            "cloud_url": str(self.cfg.get("cloud.base_url", "") or ""),
            "connected": self._state == "connected",
            "session_id": self._session_id,
            "connected_at": self._connected_at,
            "last_error": self._last_error,
            "attempt": self._attempt,
            "next_retry_in": (
                round(max(0.0, (self._next_wake or 0) - time.monotonic()), 1)
                if self._state == "backoff" else None),
            "heartbeat_sec": self._peer_heartbeat,
            "public_base": self.public_base(),
        }

    # --- 主循环 ---

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:      # noqa: BLE001 —— 重连循环不能死
                self._last_error = _short_error(exc)
                log.warning("隧道连接失败（第 %d 次）：%s", self._attempt + 1, exc)
            await self._close_conn("loop")
            if self._stop.is_set():
                return
            delay = next_backoff(self._attempt)
            self._attempt = min(self._attempt + 1, 16)
            self._state = "backoff"
            self._next_wake = time.monotonic() + delay
            log.info("隧道将在 %.1f 秒后重连（第 %d 次）", delay, self._attempt + 1)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except asyncio.TimeoutError:
                pass

    async def _connect_once(self) -> None:
        from websockets.asyncio.client import connect

        uri = self._ws_uri()
        if not uri:
            raise RuntimeError("cloud.base_url 为空")
        self._state = "connecting"
        log.info("正在连接隧道 %s", uri)
        # compression=None：帧里是已经压缩过的 webp/jpeg 字节，再套一层
        # permessage-deflate 只会让 CPU 翻倍而体积不变；更糟的是它会让
        # 云端必须整体解压才能拿到帧边界（破坏"不缓冲"）。
        ssl_ctx = ssl.create_default_context() if uri.startswith("wss") else None
        async with connect(
            uri,
            additional_headers={"Authorization": f"Bearer {self._agent_token}"},
            compression=None,
            open_timeout=15.0,
            ping_interval=None,     # 用应用层 PING（见模块 docstring 第 3 条）
            max_size=None,          # 一条 WS 消息 = 一帧，我们自己控 MAX_FRAME
            ssl=ssl_ctx,
        ) as ws:
            self._ws = ws
            await self._handshake(ws)
            self._state = "connected"
            self._connected_at = time.time()
            # ★ 这里**不能**清 `self._session_id`：`_handshake` 刚把它从
            # `HELLO_ACK` 里解出来，清掉会让 `status()` 永远报
            # `session_id: null`，排障时无法把云端的
            # "隧道上线 device=… session=…" 日志和电脑端对上。
            self._last_error = None
            log.info("隧道已建立 device=%s base=%s",
                     self._device_id, self.public_base())
            # 撤销列表同步（`docs/04` §3.6）：连上就跑一次，之后每
            # `REVOKE_SYNC_SEC` 一次。为什么在隧道进程里做而不是单开
            # 一个定时器：这条链路的唯一用途就是"云端撤销后电脑端也
            # 踢掉"，而电脑端有没有网、能不能连上云端,与隧道状态完全
            # 一致 —— 隧道通的时候才可能拉到列表。
            self._revoke_task = asyncio.create_task(
                self._revoke_loop(), name="tunnel-revoke")
            try:
                await self._pump(ws)
            finally:
                task, self._revoke_task = self._revoke_task, None
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass

    def _ws_uri(self) -> str:
        raw = (self._base_url or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = "https://" + raw
        parts = urlsplit(raw)
        scheme = "wss" if parts.scheme in ("https", "wss") else "ws"
        # ★ 审计 F03：不许把"公网 http 基址"悄悄降级成 ws://。明文
        #   websocket 会把 `Authorization: Bearer <agent 令牌>` 和整条照片
        #   流转发在公网上，路径上任何一跳都能读走。只有回环地址（本机自测、
        #   局域网调试）允许明文。
        if scheme == "ws":
            hn = (parts.hostname or "").lower()
            loopback = hn in ("localhost", "::1") or hn.startswith("127.")
            if not loopback:
                raise RuntimeError(
                    "cloud.base_url 必须使用 https://（明文 ws:// 会把 agent "
                    f"令牌和照片暴露给中间人）：{raw}"
                )
        host = parts.netloc
        if not host:
            return ""
        path = parts.path.rstrip("/")
        if path.startswith("/d/"):
            path = ""
        return urlunsplit((scheme, host, f"{path}/agent/ws", "", ""))

    async def _handshake(self, ws: Any) -> None:
        """发 HELLO，等 HELLO_ACK。失败即抛，由主循环重连。"""
        from shared.protocol import decode_frame, encode_frame

        st = _state_get()
        self._device_id = cloud_device_id(
            self.cfg, st.device_id if st is not None else "")
        hello = {
            "device_id": self._device_id,
            "name": st.device_name if st is not None else "PhotoBridge",
            "version": st.version() if st is not None else "0.1.0",
            "proto": PROTO_VERSION,
            "lan_ips": list(st.cfg_server_ips()) if st is not None else [],
            "caps": ["image", "notify", "credit", CREDIT_V2_CAP],
            "started_at": st.started_at if st is not None else time.time(),
        }
        try:
            hello["roots"] = [
                {"id": r.get("id"), "label": r.get("label"),
                 "mode": r.get("mode"), "count": r.get("count")}
                for r in (st.roots_info(with_disk=False) if st is not None else [])
            ]
            hello["indexed_count"] = int(
                (st.index_info() or {}).get("indexed", 0) or 0)
        except Exception:      # noqa: BLE001 —— HELLO 缺字段不该断开
            log.debug("HELLO 补充字段失败", exc_info=True)

        await ws.send(encode_frame(FrameType.HELLO, 1, hello))
        raw = await asyncio.wait_for(ws.recv(), timeout=HELLO_ACK_TIMEOUT)
        if isinstance(raw, str):        # pragma: no cover
            raw = raw.encode("utf-8")
        frame = decode_frame(raw)
        if frame.type is FrameType.ERROR:
            payload = frame.json
            raise RuntimeError(
                f"云端拒绝握手：{payload.get('code')} {payload.get('message')}")
        if frame.type is not FrameType.HELLO_ACK:
            raise RuntimeError(f"握手期望 HELLO_ACK，实收 {frame.type.name}")
        ack = frame.json
        hb = ack.get("heartbeat_sec")
        if isinstance(hb, (int, float)) and 5.0 <= float(hb) <= 120.0:
            self._peer_heartbeat = float(hb)
        self._session_id = ack.get("session_id")
        # ★ 只有云端也声明了 `credit_v2` 才发带 payload 的 CREDIT。
        #   老云端把 CREDIT 当定长帧，带 payload 会让它 `decode_frame` 抛
        #   `ProtocolError` 直接断连 —— 症状是"传大文件到一半隧道断"。
        server_caps = ack.get("server_caps")
        self._credit_v2 = wants_credit_v2(server_caps)
        log.info("握手完成 session=%s proto=%s hb=%.0fs credit_v2=%s",
                 self._session_id, ack.get("proto"), self._peer_heartbeat,
                 self._credit_v2)

    async def _pump(self, ws: Any) -> None:
        """读循环 + 心跳 + 判活。三个协程，谁先结束谁负责拆连接。"""
        mux = Mux(lambda data: ws.send(data), role="agent",
                  credit_v2=self._credit_v2)
        # `on_request` 只收一个参数（`RequestHandler`），而
        # `TunnelDispatcher.handle` 需要知道往哪条 mux 上回帧。用
        # `partial` 把**这一条**连接绑进去，而不是让 dispatcher 存一个
        # `_mux`：重连瞬间新旧两条连接可能并存，存单例会让旧请求的
        # 响应写到新连接上（对端只会看到一堆无法归属的 req_id）。
        mux.on_request = functools.partial(self._dispatcher.handle, mux)
        self._mux = mux
        await mux.start()

        stop = asyncio.Event()
        tasks = [
            asyncio.create_task(self._read_loop(ws, mux, stop), name="tunnel-read"),
            asyncio.create_task(self._heartbeat_loop(mux, stop),
                                name="tunnel-hb"),
        ]
        try:
            await stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            for t in tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
            await mux.close("pump end")
            self._mux = None
            # 连满 30 秒才算"稳定"，之后才清零退避次数。刚连上就被踢
            # 时清零会让重连变成 1s/1s/1s 的忙等，把云端再打一遍。
            if (self._connected_at is not None
                    and time.time() - self._connected_at >= STABLE_RESET_SEC):
                self._attempt = 0
            self._connected_at = None

    async def _read_loop(self, ws: Any, mux: Mux, stop: asyncio.Event) -> None:
        from shared.protocol import decode_frame

        try:
            while True:
                raw = await ws.recv()
                if isinstance(raw, str):    # pragma: no cover —— 只发二进制
                    raw = raw.encode("utf-8")
                try:
                    frame = decode_frame(raw)
                except Exception as exc:    # noqa: BLE001
                    # 帧都解不开说明这条连接已经不可信（半帧/错位）。
                    # 立刻断开重连，而不是带着错位的流继续解析 ——
                    # 后者会把后续字节解释成任意帧，包括别人的响应。
                    log.warning("收到无法解码的帧，断开重连：%s", exc)
                    return
                await mux.feed(frame)
        except asyncio.CancelledError:
            raise
        except Exception as exc:            # noqa: BLE001
            self._last_error = _short_error(exc)
            log.info("隧道读循环结束：%s", exc)
        finally:
            stop.set()

    async def _heartbeat_loop(self, mux: Mux, stop: asyncio.Event) -> None:
        interval = max(5.0, self._peer_heartbeat)
        try:
            while True:
                await asyncio.sleep(interval)
                if mux.closed:
                    return
                # 判活看"最后一次收到任何帧"，不是"最后一次 PONG"。
                silent = time.monotonic() - mux.last_frame_at
                if silent > DEAD_AFTER:
                    log.warning("隧道 %.0f 秒无入站帧，判定失效", silent)
                    return
                await mux.send_ping()
        except asyncio.CancelledError:
            raise
        except Exception as exc:            # noqa: BLE001
            log.info("隧道心跳结束：%s", exc)
        finally:
            stop.set()

    async def _revoke_loop(self) -> None:
        """每 `REVOKE_SYNC_SEC` 拉一次云端撤销列表（`docs/04` §3.6）。

        ★ 为什么必须存在：二期令牌是云端签发的 HMAC 令牌，电脑端**只验签
        不查库**（它没有云端 `clients` 表）。用户在手机上点"移除这台
        设备"后云端把行标成 `revoked=1`，但令牌本身还没到期、签名也
        仍然正确 —— 不同步的话，被移除的手机能**继续读到全部照片**，
        直到 90 天后令牌自然过期（AC-5.8 直接不成立）。

        失败只记日志：这是"尽力而为的收敛"，隧道断了下一轮再试。
        """
        while True:
            await asyncio.sleep(REVOKE_SYNC_SEC)
            try:
                await self._sync_revoked()
            except asyncio.CancelledError:
                raise
            except Exception as exc:        # noqa: BLE001
                log.debug("同步撤销列表失败：%s", _short_error(exc))

    async def _sync_revoked(self) -> None:
        """`GET {base}/api/clients/revoked`，把结果灌进本地缓存。"""
        import json
        import urllib.request

        base = self.public_base()
        if not base:
            return
        url = base.rsplit("/d/", 1)[0] + "/api/clients/revoked"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._agent_token}"})
        # 走线程池：`urlopen` 是阻塞调用，直接在事件循环里跑会让
        # 隧道上一整批缩略图请求跟着卡住（最长 10 秒）。
        raw = await asyncio.wait_for(
            asyncio.to_thread(_urlopen_json, req), timeout=15.0)
        ids = raw.get("clients") if isinstance(raw, dict) else None
        if not isinstance(ids, list):
            return
        revoked = [str(c.get("id") or "") for c in ids if isinstance(c, dict)]
        st = _state_get()
        if st is None:
            return
        from .auth import sync_revoked_clients

        sync_revoked_clients(st, revoked)

    async def _close_conn(self, reason: str) -> None:
        ws, self._ws = self._ws, None
        mux, self._mux = self._mux, None
        if mux is not None:
            await mux.close(reason)
        if ws is not None:
            try:
                await ws.close()
            except Exception:      # noqa: BLE001
                pass
        self._state = "idle"


def _short_error(exc: BaseException) -> str:
    """把异常压成一行短文案。

    不写 `traceback.format_exc()`：这个值会经 `/api/system/info` 发给手机，
    而审计 F14 的规则是"错误响应不得含堆栈"。异常类型 + 首行足够定位
    （"ConnectionRefusedError: [WinError 10061]"）。
    """
    name = type(exc).__name__
    msg = str(exc).replace("\n", " ").strip()
    if len(msg) > 160:
        msg = msg[:157] + "..."
    return f"{name}: {msg}" if msg else name


def _state_get() -> Any:
    from .state import get_state

    return get_state()


def _urlopen_json(req: Any) -> dict[str, Any]:
    """`urlopen` + `json.loads`，在线程池里跑（见 `_sync_revoked`）。"""
    import json
    import urllib.request

    with urllib.request.urlopen(req, timeout=15.0) as resp:  # noqa: S310
        body = resp.read(1 << 20)
    data = json.loads(body.decode("utf-8"))
    return data if isinstance(data, dict) else {}


# --------------------------------------------------------------------------
# 进程级单例（托盘/`/api/system/info` 需要读到它）
# --------------------------------------------------------------------------

_tunnel: TunnelClient | None = None


def get_tunnel() -> "TunnelClient | None":
    return _tunnel


def set_tunnel(client: "TunnelClient | None") -> None:
    global _tunnel
    _tunnel = client
