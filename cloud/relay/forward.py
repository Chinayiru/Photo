"""`/d/{device_id}/**` 转发（P1-6）：HTTP ⇄ 隧道帧的双向桥。

## 一条请求走过的十步

1. 从路径里取 `device_id`；
2. 校验客户端令牌（`Authorization: Bearer` 或 `pb_token` cookie），
   **并确认令牌绑定的设备就是路径里那台**（`docs/04` §1.2 的 E2003）；
3. 查吊销与过期（`find_client_by_token` 一并做掉）；
4. 限流（120 req/s）+ 并发（64）；
5. 从内存注册表里取隧道，没有就 503 `E3003`；
6. 分配 `req_id`，发 `REQ_HEAD`；
7. 边读 `request.stream()` 边发 `REQ_BODY`；
8. 发 `REQ_END`，等 `RES_HEAD`（30 秒，超时 504 `E3009`）；
9. 把 `RES_HEAD` 变成 HTTP 响应头，`RES_BODY` 逐块写进响应流；
10. `RES_END` 收尾；手机断开则发 `CANCEL`。

## 两个必须坚持的细节

**绝不 `await request.body()`。** 那样 2 GB 的上传会先在云端内存里
落成一个 2 GB 的 bytes 对象——AC-4.2 的 200 MB 上限从这里就崩了。
`request.stream()` 给的是 ASGI 的原始分块。

**错误帧不能变成 HTTP 200。** `docs/04` §4.5 给了 `E5xxx → E1/E3xxx`
的映射表，映射表的意义是：电脑端报"文件太大"时，手机端应该看到 413
而不是 500——否则前端的重试策略会退化成死循环（`client.ts` 只对
`retryable` 重试，一个被误标成可重试的 413 会无限重发同一个 2 GB）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from shared.mux import MuxClosed
from shared.protocol import HEAD_TIMEOUT, TOTAL_TIMEOUT

from ..relay.devices import (
    DeviceRegistry,
    TunnelConnection,
    find_client_by_token,
    find_or_adopt_lan_token,
    record_traffic,
    touch_client,
)
from ..relay.limiter import (
    MAX_INFLIGHT_PER_DEVICE,
    RATE_LIMIT_PER_SEC,
    ConcurrencyLimiter,
    RateLimiter,
    UploadLimiter,
)
from ..relay.reply import error_body
from ..relay.tunnel import forward_request

__all__ = [
    "Forwarder",
    "FORWARD_REQ_HEADERS",
    "FORWARD_RES_HEADERS",
    "E5_TO_HTTP",
    "PUBLIC_PATHS",
    "PUBLIC_RATE_PER_SEC",
    "TrafficMeter",
]

log = logging.getLogger(__name__)

#: 允许转发到电脑端的请求头（`docs/04` §1.3）。
#:
#: 白名单，不是黑名单。`Host`/`Cookie`/`Content-Length`/`Transfer-Encoding`/
#: `X-Forwarded-*` 一律**不进隧道**：
#:   * `Host` —— 电脑端的 netguard 只认自己的 Host，转发云的域名会让每个
#:     请求 404；
#:   * `Cookie` —— `pb_token` 是**云端**签发给手机的，电脑端不认；
#:   * `Content-Length`/`Transfer-Encoding` —— 由帧边界表达，转发会让
#:     电脑端按声明长度等一个永远不会到的剩余部分。
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

#: 允许回给手机的响应头。`Set-Cookie` 走独立字段（多值语义）。
FORWARD_RES_HEADERS: frozenset[str] = frozenset({
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

#: 隧道错误码 → HTTP 状态码（`docs/04` §4.5）。
#:
#: 为什么需要这张表：电脑端把"路由不存在"报成 `E5007`，而手机上必须看到
#: 404 —— 否则前端会把"这张照片没了"当成"网络抖动"而无限重试。
E5_TO_HTTP: dict[str, tuple[int, str, str]] = {
    "E5001": (400, "E1001", "帧格式错误"),
    "E5002": (500, "E3001", "电脑处理失败"),
    "E5003": (413, "E1006", "请求体过大"),
    "E5004": (403, "E1009", "该路径不允许访问"),
    "E5005": (403, "E2006", "该电脑已被移除"),
    "E5006": (500, "E3001", "协议版本不匹配"),
    "E5007": (404, "E1004", "内容不存在"),
    "E5008": (500, "E3001", "电脑处理失败"),
    "E5009": (504, "E3009", "电脑响应超时"),
    "E5010": (503, "E3008", "正在重新连接电脑"),
    "E5011": (499, "E1001", "请求已取消"),
    "E5012": (500, "E3001", "电脑处理失败"),
}

#: 攒够这么多或这么久就把流量写一次库。
#:
#: AC-3.15 要的是"双向流量都在 `traffic` 表里累计"，不是"每个请求写一次"。
#: 每请求写会让 sqlite 的全局写锁成为中继的瓶颈（120 req/s 就是 240 次
#: 写/秒）；只按字节阈值攒又会饿死"一小时只读一张图"的用户（一直不够
#: 阈值就永远不落库）。两个条件取先到者。
_FLUSH_BYTES: int = 1 << 20
_FLUSH_REQS: int = 32
_FLUSH_SEC: float = 30.0

#: 云端 `/d/**` 上**不需要 client_token** 的路径（`docs/04` §1.2 与 §4.1.2）。
#:
#: 与电脑端 `backend/app/auth.py` 的 `PUBLIC_PATHS` 同一份语义，但**故意
#: 各写一份**：云端与电脑端是两套独立部署，共用一份常量会把"电脑端想
#: 加一个匿名端点"变成"云端也跟着放开一个"。两边不一致时的表现是
#: "探活通了但业务 401"或反过来，都只会出现在部署当天。
#:
#: 只放 `/api/system/ping`：它是双通路状态机的探针，必须在**还没有
#: 令牌**时可用。
PUBLIC_PATHS: frozenset[str] = frozenset({"/api/system/ping"})

#: 匿名探活的限流（每个来源 IP，每秒）。比鉴权路径的 120/s 宽，因为
#: 它不读库、不落盘，且状态机的探活频率天然更高。
PUBLIC_RATE_PER_SEC: int = 300


class TrafficMeter:
    """按设备累计流量，攒够再落库。

    **只记成功转发过的请求**。被限流打回的 429 也占了带宽，但那部分
    字节小到没有统计意义，而把失败请求也算进 `req_count` 会让"今天用
    了多少次"变成一个看不出真假的数。
    """

    __slots__ = ("db", "_pending", "_since")

    def __init__(self, db: Any) -> None:
        self.db = db
        #: device_id → [bytes_in, bytes_out, req_count]
        self._pending: dict[str, list[int]] = {}
        self._since = time.monotonic()

    def add(self, device_id: str, *, bytes_in: int = 0,
            bytes_out: int = 0, requests: int = 1) -> None:
        slot = self._pending.get(device_id)
        if slot is None:
            slot = [0, 0, 0]
            self._pending[device_id] = slot
        slot[0] += max(0, int(bytes_in))
        slot[1] += max(0, int(bytes_out))
        slot[2] += max(0, int(requests))
        if (slot[0] + slot[1] >= _FLUSH_BYTES
                or slot[2] >= _FLUSH_REQS
                or time.monotonic() - self._since >= _FLUSH_SEC):
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            self._since = time.monotonic()
            return
        pending, self._pending = self._pending, {}
        self._since = time.monotonic()
        for device_id, (b_in, b_out, reqs) in pending.items():
            record_traffic(self.db, device_id, bytes_in=b_in,
                           bytes_out=b_out, requests=reqs)


class Forwarder:
    """`/d/{device_id}/**` 的处理逻辑（不含路由注册，便于单测）。

    路由注册在 `main.py`：`/d/{device_id}/{path:path}`。把"装配"和
    "逻辑"分开是因为测试想直接喂一个 `Request`，而不想启动整个
    Starlette 应用（也为了能把 `follow_redirects` 之类的框架行为排除
    在结论之外）。
    """

    __slots__ = ("registry", "db", "rate", "public_rate", "inflight",
                 "uploads", "meter")

    def __init__(self, registry: DeviceRegistry, db: Any) -> None:
        self.registry = registry
        self.db = db
        self.rate = RateLimiter(RATE_LIMIT_PER_SEC)
        # 匿名探活按来源 IP 计数。放得比鉴权路径宽（300/s）：探活本来
        # 就比业务请求频率高（双通路状态机 1.5 s 一次，加上手机切网时
        # 的重探），而且它不读库、不落盘，成本只有一次隧道往返。
        self.public_rate = RateLimiter(PUBLIC_RATE_PER_SEC)
        self.inflight = ConcurrencyLimiter(MAX_INFLIGHT_PER_DEVICE)
        self.uploads = UploadLimiter()
        self.meter = TrafficMeter(db)

    async def handle(self, request: Request, device_id: str) -> Response:
        rid = request.headers.get("x-request-id") or ""

        # ---- 0 匿名探活 ----
        # `docs/04` §1.2 与 §4.1.2 都写死了一条：`/api/system/ping` 必须匿名，
        # 因为它在**通路选择阶段**被调用 —— 那时手机可能还没有令牌（首次使用），
        # 也可能令牌刚过期。如果云端要求令牌，云端通路就永远探测不通，
        # 双通路状态机会把所有"没配对的手机"判成 OFFLINE。
        #
        # 这样放开安全吗：`device_id` 是 32 位 hex（128 bit 能力值），猜不到；
        # ping 只回 device_id/device_name/version，与一期 CORS 审计里已经
        # 泄漏的那几个字段同集，不含任何照片信息。限流仍然生效（下面按 IP 计数）。
        if _is_public(request):
            return await self._handle_public(request, device_id, rid)

        # ---- 1/2/3 鉴权 ----
        token = _client_token(request)
        if not token:
            return _error(401, "E2002", "缺少配对令牌", request_id=rid)
        row = find_client_by_token(self.db, token)
        if row is None:
            # --- 局域网配对签发的令牌 ---
            #
            # 产品里手机只走 `POST /api/pair`（局域网），那枚令牌从没进过
            # 云端库。这里用 `devices.agent_token` 验签并收编（见
            # `find_or_adopt_lan_token` 的 docstring）；验不过仍然是 401。
            row = find_or_adopt_lan_token(self.db, token, device_id)
        if row is None:
            # 令牌死透了：顺手清掉浏览器里的死 cookie，否则 `<img>` 会
            # 用同一个死令牌一直 401（它发不出 Authorization 头）。
            return _error(401, "E2002", "配对已失效，请重新配对",
                          request_id=rid, clear_cookie=True)
        if str(row["device_id"]) != device_id:
            # ★ 没有这一步，手机上任何一次配对都能读**别的**设备。
            # 令牌里的 device_id 是唯一可信的归属，路径里的只是它想访问的。
            return _error(403, "E2003", "无权访问该设备", request_id=rid)
        client_id = str(row["id"])

        # ---- 4 频率 ----
        ok, retry = self.rate.hit(client_id)
        if not ok:
            return _error(429, "E4001", "请求太频繁", request_id=rid,
                          retry_after=max(1, int(retry)))
        if not self.inflight.take(device_id):
            return _error(429, "E4003", "请求太密集", retry_after=1,
                          request_id=rid)

        try:
            return await self._relay(request, device_id, rid, client_id=client_id)
        finally:
            self.inflight.release(device_id)

    async def _handle_public(self, request: Request, device_id: str,
                             rid: str) -> Response:
        """匿名探活的转发（只可能是 `GET /api/system/ping`）。

        与鉴权分支共用同一条转发路径，但限流键换成**来源 IP**：没有
        `client_id` 可用，而"不设限流"会让这个匿名端点变成免费的打点
        工具（每个请求都要穿透隧道，成本落在用户自己家里那台电脑上）。
        """
        ok, retry = self.public_rate.hit(_client_ip(request))
        if not ok:
            return _error(429, "E4001", "请求太频繁", request_id=rid,
                          retry_after=max(1, int(retry)))
        return await self._relay(request, device_id, rid, client_id="")

    async def _relay(self, request: Request, device_id: str, rid: str,
                     *, client_id: str) -> Response:
        """把一次 HTTP 请求变成帧、再把帧变回 HTTP 响应。"""
        # ---- 5 在线判断 ----
        conn = self.registry.get(device_id)
        if conn is None:
            # 503 而不是 500：手机端的 `client.ts` 对 503 是"稍后重试"
            # 且不弹"出错了"，对 500 会弹错误页。电脑离线是常态
            # （合盖休眠），不该被当成故障。
            return _error(503, "E3003", "电脑离线", request_id=rid)

        upload_slot = False
        if _is_upload(request):
            if not self.uploads.take(device_id):
                return _error(429, "E4002", "同时上传的任务太多",
                              retry_after=3, request_id=rid)
            upload_slot = True

        try:
            # ---- 6/7/8 发帧 ----
            cancel = asyncio.Event()
            up = _ByteCounter()
            try:
                pending, pump = await forward_request(
                    conn,
                    method=request.method,
                    path=_inner_path(request),
                    query=_query_of(request),
                    headers=_req_headers(request),
                    body=_count_stream(request.stream(), up),
                    body_len=_content_length(request),
                    cancel=cancel,
                    total_timeout=_total_timeout(request),
                )
            except MuxClosed:
                return _error(503, "E3003", "电脑离线", request_id=rid)

            conn.inflight += 1
            release = _Once(lambda: setattr(
                conn, "inflight", max(0, conn.inflight - 1)))
            try:
                res = await pending.wait_head(timeout=HEAD_TIMEOUT)
            except asyncio.TimeoutError:
                await _abort(conn, pending, cancel, pump)
                release()
                self.meter.add(device_id, bytes_in=up.total, requests=1)
                return _error(504, "E3009", "电脑响应超时", request_id=rid)
            except MuxClosed:
                await _abort(conn, pending, cancel, pump)
                release()
                return _error(503, "E3003", "电脑离线", request_id=rid)

            if res.get("status") == 0 or "error" in res:
                await _abort(conn, pending, cancel, pump)
                release()
                self.meter.add(device_id, bytes_in=up.total, requests=1)
                return _from_frame_error(res, request_id=rid)

            if client_id:
                touch_client(self.db, client_id)
            return self._stream_response(
                request, conn, pending, res, pump, cancel, up, release)
        finally:
            if upload_slot:
                self.uploads.release(device_id)

    def _stream_response(self, request: Request, conn: TunnelConnection,
                         pending: Any, res: dict[str, Any],
                         pump: asyncio.Task[None] | None,
                         cancel: asyncio.Event,
                         up: "_ByteCounter",
                         release: "_Once") -> Response:
        device_id = conn.device_id
        status = _safe_int(res.get("status"), 200)
        headers, cookies = _res_headers(res)
        idle = _idle_timeout(request)
        total = _total_timeout(request)

        if status in (204, 304):
            # 无体响应不能挂 StreamingResponse 的生成器：Starlette 对 304
            # 会丢掉体，但生成器仍会被驱动一次，白白等一轮 `RES_END`。
            _reap(pump, pending, conn, cancel)
            release()
            self.meter.add(device_id, bytes_in=up.total, requests=1)
            return _mk_response(b"", status, headers, cookies)

        async def body_iter() -> AsyncIterator[bytes]:
            sent = 0
            try:
                async for chunk in pending.iter_body(
                        idle_timeout=idle, total_timeout=total):
                    sent += len(chunk)
                    conn.bytes_out_today += len(chunk)
                    yield chunk
            except asyncio.TimeoutError:
                log.info("手机等响应体超时 device=%s", device_id)
            except (MuxClosed, asyncio.CancelledError):
                pass
            except Exception:       # noqa: BLE001
                log.debug("响应体流转发中断", exc_info=True)
            finally:
                release()
                self.meter.add(device_id, bytes_in=up.total,
                               bytes_out=sent, requests=1)
                _reap(pump, pending, conn, cancel)

        media_type = headers.pop("content-type", None)
        resp = StreamingResponse(body_iter(), status_code=status,
                                 headers=headers, media_type=media_type)
        return _with_cookies(resp, cookies) if cookies else resp


# ---------------------------------------------------------------------------
# 帧收尾
# ---------------------------------------------------------------------------


def _reap(pump: asyncio.Task[None] | None, pending: Any,
          conn: TunnelConnection, cancel: asyncio.Event) -> None:
    """请求正常收尾：停掉体搬运并释放 req_id。

    为什么必须显式做：`Mux` 的 `_pending` 表是 req_id 的租约，不释放的话
    4096 个请求之后所有新请求都 `E5010`。正常路径上 `RES_END` 已经
    `_abort` 过一次（`finish` 是幂等的 `pop`），但手机提前断开时不会有
    `RES_END`——这正是 `docs/01` AC-4.4 说的"CANCEL 释放云+电脑资源"。
    """
    cancel.set()
    if pump is not None and not pump.done():
        pump.cancel()
    try:
        conn.mux.finish(pending.req_id)
    except Exception:       # noqa: BLE001
        pass


async def _abort(conn: TunnelConnection, pending: Any,
                 cancel: asyncio.Event,
                 pump: asyncio.Task[None] | None) -> None:
    """失败路径：告诉电脑端别干了，然后释放。

    先 `cancel()` 再 `finish()`：`finish` 只清本地状态，电脑端那边的
    handler 还在读一个它以为有 2 GB 的请求体——不发 `CANCEL` 的话，
    电脑端要等到自己的 `TOTAL_TIMEOUT`（30 分钟）才放手。
    """
    cancel.set()
    if pump is not None and not pump.done():
        pump.cancel()
    try:
        await conn.mux.cancel(pending.req_id, "relay_abort")
    except Exception:       # noqa: BLE001
        pass
    finally:
        try:
            conn.mux.finish(pending.req_id)
        except Exception:       # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 响应装配
# ---------------------------------------------------------------------------


def _mk_response(body: bytes, status: int, headers: dict[str, str],
                 cookies: list[str]) -> Response:
    resp = Response(body, status_code=status, headers=headers)
    for c in cookies:
        resp.raw_headers.append((b"set-cookie", c.encode("latin-1")))
    return resp


def _with_cookies(resp: Response, cookies: list[str]) -> Response:
    """把多条 `Set-Cookie` 逐条塞进 `raw_headers`。

    `Response.headers` 是 dict，同名头只能有一个值 —— 而 `/api/pair`
    会同时下发 `pb_token` 与 `pb_pair`，折叠掉一条的表现是"配对成功
    但下一张缩略图 401"。
    """
    for c in cookies:
        try:
            resp.raw_headers.append((b"set-cookie", c.encode("latin-1")))
        except UnicodeEncodeError:
            # 头值只能是 latin-1；中文 cookie 值只可能来自 bug，
            # 丢掉比让整个响应 500 好。
            log.debug("Set-Cookie 无法编码，已跳过")
    return resp


def _error(status: int, code: str, message: str, *,
           retry_after: int | None = None, request_id: str = "",
           clear_cookie: bool = False) -> Response:
    """云端的错误响应，**与电脑端逐字段同形**。

    形状一致不是审美问题：`docs/01` AC-6.7 要求同一路径在两条通路上
    返回同样的 JSON，而前端 `api/client.ts` 的 `ApiError` 只解析这一个
    形状。早先这里只回了 `{"error":{"code","message"}}`，缺 `retryable`
    时前端的自动重试全部失效（`retryable` 缺失等于 false）——云端说
    "电脑离线，稍后再试"，手机却当场弹错误页。
    """
    headers = {"content-type": "application/json; charset=utf-8",
               "cache-control": "no-store",
               "x-content-type-options": "nosniff"}
    if retry_after is not None:
        headers["retry-after"] = str(retry_after)
    if request_id:
        headers["x-request-id"] = request_id
    body = json.dumps(error_body(code, message=message,
                                 request_id=request_id),
                      ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    resp = Response(body, status_code=status, headers=headers)
    if clear_cookie:
        # 令牌已经没用了就别让浏览器继续带着它：`<img>` 只能靠 cookie，
        # 一个死 cookie 会让缩略图一直 401 而前端以为"网络抖动"。
        resp.raw_headers.append(
            (b"set-cookie",
             b"pb_token=; Path=/; Max-Age=0; SameSite=Lax; HttpOnly"))
    return resp


def _from_frame_error(res: dict[str, Any], *, request_id: str = "") -> Response:
    err = res.get("error") or {}
    if not isinstance(err, dict):
        err = {}
    code = str(err.get("code") or "E5002")
    status, out_code, message = E5_TO_HTTP.get(code,
                                              (500, "E3001", "电脑处理失败"))
    return _error(status, out_code, message, request_id=request_id)


def _res_headers(res: dict[str, Any]) -> tuple[dict[str, str], list[str]]:
    raw = res.get("headers")
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            lk = str(k).lower()
            if lk in FORWARD_RES_HEADERS and v is not None:
                out[lk] = str(v)
    cookies: list[str] = []
    for c in res.get("set_cookies") or ():
        if isinstance(c, str) and c:
            cookies.append(c)
    return out, cookies


# ---------------------------------------------------------------------------
# 请求装配
# ---------------------------------------------------------------------------


def _req_headers(request: Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk in FORWARD_REQ_HEADERS:
            out[lk] = v
    # 客户端令牌**必须**转成 Bearer 送给电脑端：电脑端的 auth 只认
    # `Authorization`，而手机用 `<img>` 取图时只能靠 cookie。云端已经
    # 验过一次，但仍然原样转发——电脑端必须自己再验一次，这是纵深防御：
    # 云端的注册表与令牌表若被攻破，电脑端不该跟着失守。
    token = _client_token(request)
    if token:
        out["authorization"] = f"Bearer {token}"
    return out


def _client_token(request: Request) -> str:
    raw = request.headers.get("authorization") or ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    cookie = request.cookies.get("pb_token")
    return (cookie or "").strip()


def _inner_path(request: Request) -> str:
    """把 `/d/<id>/api/timeline` 变成 `/api/timeline`。"""
    path = request.url.path
    prefix = "/d/"
    if not path.startswith(prefix):
        return path
    rest = path[len(prefix):]
    _, _, tail = rest.partition("/")
    return "/" + tail if tail else "/"


def _query_of(request: Request) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k in request.query_params:
        out.setdefault(k, []).extend(request.query_params.getlist(k))
    return out


def _content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if not raw:
        return None
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _is_upload(request: Request) -> bool:
    """是不是一个上传分片（决定要不要占上传并发额度）。"""
    path = request.url.path
    return request.method in ("POST", "PATCH") and "/api/uploads/" in path


def _is_public(request: Request) -> bool:
    """是不是一个**不需要 client_token** 的请求。

    只读方法 + 精确路径匹配。刻意不用 `startswith`：`/api/system/ping/../info`
    这种在 Starlette 里不会路由到 info，但"前缀匹配"这个写法一旦被复制到
    别处就是一条越权路径（审计里 `docs/06` 规则 15 对 CORS 前缀匹配的
    批评是同一个道理）。
    """
    if request.method not in ("GET", "HEAD"):
        return False
    return _inner_path(request) in PUBLIC_PATHS


def _client_ip(request: Request) -> str:
    """限流用的来源标识。

    不信任 `X-Forwarded-For`：云端前面是 Caddy，真值由 Caddy 覆写；
    而这个值在这里唯一的用途是"别让一个 IP 把匿名探活打成洪水"，
    伪造它只能让自己和别的伪造者挤在同一个桶里，最坏结果是他们互相
    限流——不构成对正常用户的影响。
    """
    client = request.client
    host = getattr(client, "host", "") or ""
    return host.strip() or "unknown"


def _safe_int(raw: Any, default: int) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return n if 100 <= n <= 599 else default


def _idle_timeout(request: Request) -> float:
    """响应体两块之间的最大间隔（`docs/04` §3.7）。

    SSE（`/api/events`）也是 60 秒：电脑端每 25 秒发一次心跳，60 秒
    足以判死，且不需要为它单独放宽——放宽会让"电脑端悄悄死了"变成
    "手机端挂着一个永不报错的连接"。
    """
    return 60.0


def _total_timeout(request: Request) -> float | None:
    """响应体总时长上限；SSE 无上限。

    `docs/04` §1.5 给最长的是原图 30 分钟（`TOTAL_TIMEOUT`）。而
    `GET /api/events` 是长连接事件流，任何总额上限都等于"每 30 分钟
    把手机的事件流掐一次"，用户看到的是"用久了收不到上传完成通知"。
    """
    if request.url.path.endswith("/api/events"):
        return None
    return TOTAL_TIMEOUT


class _ByteCounter:
    """数**上行**字节（手机→电脑）。

    数实际流过的字节，而不是手机声明的 `Content-Length`：声明值可以是
    假的（攻击者随便挑一个数字），而实际字节才是用户真正的流量成本。
    """

    __slots__ = ("total",)

    def __init__(self) -> None:
        self.total = 0


class _Once:
    """只执行一次的动作。

    `conn.inflight` 有两处会减（无体响应在装配时减一次、有体响应在
    生成器的 `finally` 里减一次），而生成器在被提前 `close()` 时
    两步都会跑到。少一个 `_Once` 就会把在途数减成 -1，最终表现为
    `inflight` 永远偏高——排查时看到"没有请求在跑但显示 3 个在途"。
    """

    __slots__ = ("fn", "_done")

    def __init__(self, fn: Any) -> None:
        self.fn = fn
        self._done = False

    def __call__(self) -> None:
        if self._done:
            return
        self._done = True
        try:
            self.fn()
        except Exception:       # noqa: BLE001
            log.debug("收尾动作失败", exc_info=True)


async def _count_stream(gen: AsyncIterator[bytes],
                        counter: _ByteCounter) -> AsyncIterator[bytes]:
    async for chunk in gen:
        counter.total += len(chunk)
        yield chunk
