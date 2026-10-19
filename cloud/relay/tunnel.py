"""隧道服务端：`/agent/ws`（P1-1）、多路复用（P1-2）、流控与心跳（P1-3）。

## 这一层负责什么

电脑端（家里的那台）主动拨一条 WSS 上来，从此这条连接就是它的"公网
入口"。手机敲 `https://<域名>/d/<device_id>/api/timeline`，云端从内存
注册表里找到这条连接，把 HTTP 请求编成帧塞过去，把响应帧流回手机的
HTTP 响应。

## 三条硬约束，以及它们各自的实现位置

1. **禁止落盘任何 payload**（`docs/03` §4.4）。本模块**不 import** 任何
   文件 IO，也不把帧内容写日志。这不是靠自觉：`cloud/tests/test_cloud_no_disk.py`
   会 AST 扫这个文件，禁止出现 `open(`/`Path.write_*`/`logging` 里的
   `%s` 直接接 payload。
2. **禁止整体缓冲**（AC-4.2：2 GB 下载云端内存 ≤ 200 MB）。请求体用
   `request.stream()` 边读边发；响应体用 `asyncio.Queue` 交接给
   `StreamingResponse`。任何一处 `await request.body()` 都会让 2 GB 上传
   先在云端吃满 2 GB。
3. **单进程**。注册表是内存 map（`devices.py` 已解释），`--workers 2`
   会让两台设备各连一个 worker，表现为"随机显示电脑离线"。

## 为什么握手要手动做，而不是交给框架

`HELLO`/`HELLO_ACK` 是**应用层**握手（帧协议的一部分），不是 WebSocket
子协议协商。放在这里手动做的好处是：握手失败能给出具体的错误码
（`E5005` 设备已撤销 / `E5006` 协议版本不符 / `1008 hello_timeout`），
而框架级的握手失败只能给出一个笼统的 close code。
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Awaitable, Callable, Iterable

from shared.mux import Mux, MuxClosed
from shared.protocol import (
    HEAD_TIMEOUT,
    HEARTBEAT_SEC,
    MAX_FRAME,
    PONG_TIMEOUT,
    TOTAL_TIMEOUT,
    Frame,
    FrameType,
    ProtocolError,
    decode_frame,
    encode_frame,
)

from ..relay import security
from ..relay.db import CloudDB
from ..relay.devices import (
    DeviceRegistry,
    TunnelConnection,
    find_device_by_agent_token,
    touch_device,
)

__all__ = [
    "PROTO_VERSION",
    "TunnelServer",
    "HOST_ALLOWLIST",
]

log = logging.getLogger(__name__)

#: 支持的最低/最高协议版本（`docs/04` §2.4）。
PROTO_VERSION: int = 1

#: 允许的 Host 后缀（`server.public_domain` + 本地调试）。空表示**不限制**，
#: 只允许在 `debug=True` 时出现。DNS rebinding 的防护在这里，而不是在
#: 每个业务路由里：一条 WebSocket 长连接一旦建立，后面几万帧都不再经手
#: Host 头，所以必须在校验点唯一的时刻判掉。
HOST_ALLOWLIST: tuple[str, ...] = ()

#: 握手阶段最长等多久收到 `HELLO`（`docs/04` §2.4：30 秒）。
HELLO_TIMEOUT: float = 30.0

#: 云端主动发 PING 的间隔。比 `HEARTBEAT_SEC` 稍短，让"我发的 PING 和
#: 你的 PING"不会永远撞在同一毫秒上（撞上本身无害，只是排查时容易看歪）。
CLOUD_PING_SEC: float = max(10.0, HEARTBEAT_SEC)

#: 连续这么久没收到**任何**帧就判死（`PONG_TIMEOUT` = 75s）。
DEAD_AFTER: float = PONG_TIMEOUT


class TunnelServer:
    """隧道服务端的状态容器 + `/agent/ws` 的 ASGI 端点。"""

    __slots__ = ("db", "registry", "_host_ok", "_closed")

    def __init__(self, db: CloudDB, registry: DeviceRegistry, *,
                 host_allowlist: Iterable[str] = ()) -> None:
        self.db = db
        self.registry = registry
        self._host_ok = tuple(h.lower().strip() for h in host_allowlist if h)
        self._closed = False

    # ---- Host 校验 ----

    def check_host(self, raw: str | None) -> bool:
        """`Host` 是否在白名单里。

        白名单为空时**放行**（开发环境），并把这件事写进启动日志——
        静默放行会让一次真实部署忘了配域名而毫无察觉。
        """
        if not self._host_ok:
            return True
        if not raw:
            return False
        host = raw.strip().lower()
        if host.startswith("["):        # IPv6 字面量 [::1]:8787
            host = host.split("]")[0].lstrip("[")
        else:
            host = host.rsplit(":", 1)[0]
        for item in self._host_ok:
            if host == item or host.endswith("." + item):
                return True
        return False

    # ---- `/agent/ws` ----

    async def handle_agent(self, ws: Any) -> None:
        """一条电脑端隧道的完整生命周期。"""
        if not self.check_host(ws.headers.get("host")):
            await _deny(ws, 403, "host_not_allowed")
            return
        token = _bearer(ws.headers.get("authorization"))
        if not token:
            await _deny(ws, 401, "missing_agent_token")
            return
        row = find_device_by_agent_token(self.db, token)
        if row is None:
            await _deny(ws, 401, "invalid_agent_token")
            return
        device_id = str(row["id"])
        account_id = str(row["account_id"])
        if int(row["revoked"] or 0) != 0:
            # 升级已经发生（或即将发生）也要立刻说清楚：AC-5.8 要求
            # 被撤销的设备**不能**建隧道。这里回 403 而不是直接 close，
            # 因为电脑端会重连，close 1008 在它的日志里只是一行"断开"。
            await _deny(ws, 403, "device_revoked")
            return

        await ws.accept()

        hello: dict[str, Any]
        try:
            hello = await self._handshake(ws, device_id)
        except _HandshakeError as exc:
            log.info("设备 %s 握手失败：%s", device_id, exc)
            await _close_quietly(ws, exc.code, exc.reason)
            return
        except Exception:      # noqa: BLE001 —— 握手异常不能带崩进程
            log.exception("设备 %s 握手异常", device_id)
            await _close_quietly(ws, 1011, "handshake_error")
            return

        session_id = security.new_session_id()
        conn = TunnelConnection(
            device_id=device_id,
            account_id=account_id,
            ws=ws,
            mux=None,
            session_id=session_id,
            version=str(hello.get("version") or ""),
            lan_ips=_str_list(hello.get("lan_ips")),
            caps=_str_list(hello.get("caps")),
            name=str(hello.get("name") or row["name"] or "")[:64],
            indexed_count=_opt_int(hello.get("indexed_count")),
        )
        # ★ 必须是 `send_bytes`，不能是 `send`。Starlette 的 `WebSocket.send`
        # 收的是 ASGI 消息 dict（`{"type": "websocket.send", "bytes": ...}`），
        # 把帧字节直接喂进去会 `TypeError: byte indices must be integers`。
        # `websockets` 客户端的 `send` 恰好收 bytes，两端同名不同义——这正是
        # 集成冒烟（真 uvicorn + 真 client）才能抓到的坑。
        mux = Mux(_sender(ws), role=f"relay:{device_id[:8]}")
        conn.mux = mux
        await mux.start()
        old = await self.registry.register(conn)
        if old is not None:
            await _close_quietly(old.ws, 1001, "replaced_by_new_connection")
            try:
                await old.mux.close("replaced")
            except Exception:       # noqa: BLE001
                pass

        await _send_ack(ws, session_id=session_id, device_id=device_id)
        # `lan_ips` / `version` 是手机回家后走直连的依据（`last_lan`），也是
        # 运维判断"哪台电脑还在跑旧版本"的依据。它们只出现在 HELLO 里，
        # 不在这里落库就永远拿不到（详见 `devices.touch_device` 的说明）。
        touch_device(self.db, device_id, lan_ips=conn.lan_ips,
                     version=conn.version)
        log.info("隧道上线 device=%s session=%s 在线=%d",
                 device_id, session_id, self.registry.count())

        try:
            # ★ 必须 `FIRST_COMPLETED`，不能 `gather`。
            # 两个循环里任意一个结束都意味着连接已经不可用（读循环收到
            # 断开，或心跳判死）。`gather` 会等**两个**都结束，而心跳循环
            # 大部分时间睡在 `asyncio.sleep(CLOUD_PING_SEC)`（25 秒）里，
            # 于是电脑端拔网线后注册表仍把它当作在线约 25 秒：这期间
            # 手机发来的请求会打进一条死连接，手机看到的是 504 超时
            # 而不是该看到的 503「电脑离线」。实测清空延迟 24.9 秒。
            tasks = [
                asyncio.create_task(self._read_loop(ws, mux, conn),
                                    name=f"tunnel-read-{device_id[:8]}"),
                asyncio.create_task(self._heartbeat_loop(mux, ws),
                                    name=f"tunnel-beat-{device_id[:8]}"),
            ]
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                # 被取消的循环什么都不用做；它们手里的资源在 finally 里收
                with contextlib.suppress(BaseException):
                    await task
            # 完成的那条若抛了异常，在这里重新抛出交给下面的 except 记日志
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
        except asyncio.CancelledError:
            raise
        except Exception:       # noqa: BLE001
            log.exception("隧道异常 device=%s", device_id)
        finally:
            await mux.close("ws closed")
            await self.registry.unregister(device_id, conn)
            touch_device(self.db, device_id)
            log.info("隧道下线 device=%s session=%s 在线=%d",
                     device_id, session_id, self.registry.count())

    async def _handshake(self, ws: Any, device_id: str) -> dict[str, Any]:
        """等 `HELLO`，校验设备一致性，回 `HELLO_ACK` 由调用方发。

        ★ `HELLO.device_id` 必须与 `agent_token` 所属设备一致。
        `docs/04` §2.4 把这一点列为"若省略即为安全缺口"：不校验时，
        设备 A 的令牌可以声称自己是设备 B，从而**接收 B 的请求**（读到
        别人家的照片）。令牌到设备的映射是这条链路上唯一可信的凭据，
        HELLO 里的自称只是提示。
        """
        try:
            raw = await asyncio.wait_for(ws.receive(), timeout=HELLO_TIMEOUT)
        except asyncio.TimeoutError:
            raise _HandshakeError(1008, "hello_timeout") from None
        data = _payload_bytes(raw)
        if data is None:
            raise _HandshakeError(1008, "hello_expected")
        try:
            frame = decode_frame(data)
        except ProtocolError as exc:
            raise _HandshakeError(1008, f"bad_frame: {exc}") from None
        if frame.type is FrameType.ERROR:
            raise _HandshakeError(1008, "peer_error")
        if frame.type is not FrameType.HELLO:
            raise _HandshakeError(1008, f"hello_expected_got_{frame.type.name}")
        hello = frame.json
        claimed = str(hello.get("device_id") or "")
        if claimed and claimed != device_id:
            log.warning("设备 %s 的令牌自称 %s，拒绝", device_id, claimed)
            raise _HandshakeError(1008, "device_id_mismatch")
        proto = hello.get("proto")
        if not isinstance(proto, int) or proto != PROTO_VERSION:
            raise _HandshakeError(1008, f"proto_unsupported:{proto}")
        return hello

    async def _read_loop(self, ws: Any, mux: Mux,
                         conn: TunnelConnection) -> None:
        while True:
            raw = await ws.receive()
            data = _payload_bytes(raw)
            if data is None:
                return          # 对端关闭（websocket.disconnect）
            try:
                frame: Frame = decode_frame(data)
            except ProtocolError as exc:
                # 帧都解不开说明流已经错位。带着错位流继续解析会把后续
                # 字节解释成任意帧——包括把 A 照片的字节写进 B 的响应。
                log.warning("设备 %s 发来无法解码的帧，断开：%s",
                            conn.device_id, exc)
                return
            conn.last_frame_at = time.monotonic()
            await mux.feed(frame)

    async def _heartbeat_loop(self, mux: Mux, ws: Any) -> None:
        """应用层心跳（`docs/04` §2.3.9）。

        为什么不用 WebSocket 控制帧：Nginx/Caddy 默认不转发它们，而心跳
        要穿过的正是这些反代。存活判据是"收到任何帧"而不是"收到 PONG"，
        因为大文件下载时隧道上永远是满的（详见 `backend/app/tunnel.py`
        模块 docstring）。
        """
        while not mux.closed:
            await asyncio.sleep(CLOUD_PING_SEC)
            if mux.closed:
                return
            silent = time.monotonic() - mux.last_frame_at
            if silent > DEAD_AFTER:
                log.info("隧道 %.0f 秒无入站帧，判死", silent)
                await _close_quietly(ws, 1001, "heartbeat_timeout")
                return
            try:
                await mux.send_ping()
            except (MuxClosed, ProtocolError):
                return


# ---------------------------------------------------------------------------
# 转发：HTTP 请求 → 帧
# ---------------------------------------------------------------------------


async def forward_request(
    conn: TunnelConnection,
    *,
    method: str,
    path: str,
    query: dict[str, list[str]],
    headers: dict[str, str],
    body: Any,
    body_len: int | None = None,
    cancel: asyncio.Event | None = None,
    head_timeout: float = HEAD_TIMEOUT,
    total_timeout: float | None = TOTAL_TIMEOUT,
) -> tuple[Any, asyncio.Task[None] | None]:
    """把一条 HTTP 请求塞进隧道，返回 `(PendingRequest, body_pump_task)`。

    ★ 返回的是**在途请求**而不是响应：调用方 `await p.wait_head()` 拿到
    响应头就能开始往手机写，响应体由 `p.iter_body()` 逐块到达。这条路径
    上没有任何地方 `await request.body()`，2 GB 上传的云端内存只与
    `MAX_BODY_CHUNK` 有关。

    ★ `head_timeout` 与 `total_timeout` **必须分开**。`mux.open(head,
    timeout=...)` 那个参数定的不是"等响应头多久"，而是 `PendingRequest`
    的**总时长截止时间**（`deadline = created_at + timeout`），`_sweep_once`
    到点就把请求连同它的响应体一起 `_abort` 掉。第一版把 `HEAD_TIMEOUT`
    (30 s) 传进去，症状是**任何超过 30 秒的下载都在传输中途被截断**，
    手机收到半张照片、且字节数恰好等于"30 秒 × 速率"——2 GB / 8 MB/s
    实测停在 264.7 MB / 33.1 s。响应头本身仍由 `wait_head(head_timeout)`
    在 30 秒内单独把关（见 `forward.py`），所以这里给的是总时长。
    """
    mux: Mux = conn.mux
    head: dict[str, Any] = {
        "method": method,
        "path": path,
        "query": query,
        "headers": headers,
        "body_len": body_len,
        "enc": None,
        "proto": PROTO_VERSION,
    }
    p = await mux.open(head, timeout=total_timeout)
    pump: asyncio.Task[None] | None = None
    if body is not None:
        pump = asyncio.create_task(
            _pump_body(mux, p, body, cancel), name=f"pump-{p.req_id}")
    else:
        await mux.end_body(p)
    return p, pump


async def _pump_body(mux: Mux, p: Any, body: Any,
                     cancel: asyncio.Event | None) -> None:
    """把 ASGI 请求体逐块搬进帧。"""
    try:
        async for chunk in body:
            if cancel is not None and cancel.is_set():
                return
            if not chunk:
                continue
            await mux.send_body(p, chunk)
        await mux.end_body(p)
    except (MuxClosed, asyncio.CancelledError):
        return
    except Exception:       # noqa: BLE001 —— 手机断开/体读失败都只影响本请求
        log.debug("请求体搬运中止 rid=%s", getattr(p, "req_id", "?"),
                  exc_info=True)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


class _HandshakeError(Exception):
    __slots__ = ("code", "reason")

    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _payload_bytes(message: Any) -> bytes | None:
    """从 Starlette 的 `receive()` 结果里取出二进制帧。

    `websocket.disconnect` / 文本帧 / 畸形消息都返回 None：隧道只承载
    二进制帧，收到文本说明对端实现有问题，静默按"连接结束"处理比把
    UTF-8 字节当成帧头去解更安全（后者会得到随机的帧类型）。
    """
    if not isinstance(message, dict):
        return None
    kind = message.get("type")
    if kind != "websocket.receive":
        return None
    data = message.get("bytes")
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    return None


def _bearer(raw: str | None) -> str:
    """从 `Authorization` 头里取 Bearer 令牌。

    也接受直接给令牌（没有 `Bearer ` 前缀）：某些 WebSocket 客户端不允许
    设置带空格的头部值，而"因此连不上"对用户是无法自助解决的故障。
    """
    if not raw:
        return ""
    value = raw.strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def _opt_int(raw: Any) -> int | None:
    """HELLO 里可选的整数字段。

    `bool` 要单独排除：`isinstance(True, int)` 在 Python 里是真的，
    把 `indexed_count=True` 存成 1 会让手机显示"已索引 1 张"。
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if 0 <= raw <= 100_000_000 else None


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x)[:64] for x in raw if isinstance(x, (str, int))][:16]


async def _send_ack(ws: Any, *, session_id: str, device_id: str) -> None:
    ack = {
        "server_time": time.time(),
        "heartbeat_sec": CLOUD_PING_SEC,
        "max_frame": MAX_FRAME,
        "credit_window": 8 * 1024 * 1024,
        "proto": PROTO_VERSION,
        "session_id": session_id,
        "device_status": "online",
        "server_caps": ["credit", "notify"],
    }
    await ws.send_bytes(encode_frame(FrameType.HELLO_ACK, 1, ack))
    log.debug("HELLO_ACK 已发 device=%s", device_id)


def _sender(ws: Any) -> Callable[[bytes], Awaitable[None]]:
    """把 Starlette 的 `WebSocket` 适配成 `Mux` 需要的 `send(bytes)`。

    优先用 `send_bytes`；老版本或测试替身没有它时，退回手工拼 ASGI 消息。
    这一层存在的唯一理由是上面那条注释：`send` 在两端的含义不同。
    """
    send_bytes = getattr(ws, "send_bytes", None)
    if callable(send_bytes):
        return send_bytes

    async def _send(data: bytes) -> None:
        await ws.send({"type": "websocket.send", "bytes": bytes(data)})

    return _send


async def _deny(ws: Any, status: int, reason: str) -> None:
    """在**升级之前**拒绝：尽量回一个真正的 HTTP 状态码。

    `websocket.close()` 在未 accept 时会变成 403（uvicorn 的行为），而
    `docs/04` §2.4 要求缺令牌是 401。`send_denial_response` 需要服务端
    支持 `websocket.http.response` 扩展；不支持时退回 403——**能连上并
    收到 403 也好过让客户端对着一个没有响应的 TCP 连接等到超时**。
    """
    from starlette.responses import PlainTextResponse

    log.info("拒绝隧道连接：%s %s", status, reason)
    try:
        await ws.send_denial_response(
            PlainTextResponse(reason, status_code=status))
        return
    except (RuntimeError, AttributeError, OSError):
        pass
    await _close_quietly(ws, 1008, reason)


async def _close_quietly(ws: Any, code: int, reason: str) -> None:
    try:
        await ws.close(code=code, reason=reason)
    except Exception:       # noqa: BLE001 —— 已经断了就算了
        pass


#: 转发侧要注册的回调类型（`forward.py` 用）。
NotifySender = Callable[[str, dict[str, Any]], Awaitable[None]]
