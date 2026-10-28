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

from shared.client_token import looks_like_token
from shared.mux import MuxClosed
from shared.protocol import HEAD_TIMEOUT, TOTAL_TIMEOUT

from ..relay import control as cloud_control
from ..relay import netutil, security
from ..relay.devices import (
    DeviceRegistry,
    TunnelConnection,
    find_client_by_token,
    find_or_adopt_lan_token,
    record_traffic,
    touch_client,
)
from ..relay.limiter import (
    AUTH_FAIL_MAX,
    AUTH_FAIL_WINDOW,
    AUTH_RATE_PER_MIN,
    MAX_INFLIGHT_PER_DEVICE,
    MAX_INFLIGHT_TOTAL,
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

#: `ConcurrencyLimiter` 是按 key 分桶的，全局闸门就借一个固定 key。
#: 用 `"__all__"` 而不是 `""`：空串容易和"还没解析出设备 id"的路径混淆。
_INFLIGHT_ALL = "__all__"

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
    # ★ 这两个不能丢（审查 R-M5-03）：`retry-after` 是限流/排队时手机端
    #   唯一的退避依据；`x-upload-offset` 是分片上传 416 之后"服务端到底
    #   收到多少"的答案，丢了就只能从头重传整个文件。
    "retry-after",
    "vary",
    "x-content-type-options",
    "x-request-id",
    "x-upload-offset",
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

#: 认证失败的 401 体，**逐字段镜像电脑端**（`docs/01` AC-6.7、`docs/04`
#: §1.4.3 M7-023）：同一条路径经隧道与直连，手机看到的必须是同一段文案，
#: 否则 `api/client.ts` 按 code/message 分支处理的逻辑被迫分叉。
#:
#: 这四个字面量对应电脑端 `backend/app/errors.py` + `auth.py` 的输出：
#:   `{"code":"E2002","message":"client_token_invalid",
#:     "detail":"缺少凭证：Authorization 头"|"token 不存在",
#:     "retryable":false,"request_id":"…","user_hint":"配对已失效，请重新配对"}`
#:
#: ★ 只能是**固定常量**。把令牌、client_id、路径拼进 `detail` 就是在错误
#: 响应里外泄内部标识（上一轮 F11 的形状），而且会让"越权"与"不存在"
#: 变得可区分。
_DEVICE_AUTH_MESSAGE: str = "client_token_invalid"
_DEVICE_AUTH_HINT: str = "配对已失效，请重新配对"
_DEVICE_NO_TOKEN_DETAIL: str = "缺少凭证：Authorization 头"
_DEVICE_BAD_TOKEN_DETAIL: str = "token 不存在"

#: 云端 `/d/**` 上**不需要 client_token** 的路径（`docs/04` §1.2 与 §4.1.2）。
#:
#: 与电脑端 `backend/app/auth.py` 的 `PUBLIC_PATHS` 同一份语义，但**故意
#: 各写一份**：云端与电脑端是两套独立部署，共用一份常量会把"电脑端想
#: 加一个匿名端点"变成"云端也跟着放开一个"。两边不一致时的表现是
#: "探活通了但业务 401"或反过来，都只会出现在部署当天。
#:
#: * `/api/system/ping` —— 双通路状态机的探针，必须在**还没有令牌**时可用；
#: * `/api/pair` —— 配对本身就是"换令牌"的过程，此时不可能已有令牌。
#:
#: ★ 为什么 `POST /api/pair` 可以匿名而**根路径的 `/api/pair` 不可以**：
#: 这条路径带着 `/d/{device_id}` 前缀，而那个 id 是云端分配的 **128 位
#: 能力值**（32 位十六进制）。攻击者要爆破配对码，必须先知道某台设备的
#: id —— 猜不到，也无法枚举（云端不提供设备列表）。而如果在根路径上做
#: 一个"按码找设备"的接口，128 位这一层就没了，6 位数字（100 万种）
#: 就成了唯一屏障，配上公网可匿名访问，那是不可接受的。
#:
#: 放开的代价仍然要挡住：匿名 + 6 位码 → 云端必须自己限速（见
#: `PAIR_RATE_PER_MIN`），且把**真实来源 IP** 塞进帧里，让电脑端那层
#: `_PAIR_WINDOW_MAX` 按 IP 生效（否则所有人共用 `"cloud"` 一个桶）。
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/api/system/ping",
    "/api/pair",
})

#: 允许匿名的**非只读**方法。目前只有配对：它必须 POST。
#:
#: 单列一张表而不是"`/api/pair` 就放行所有方法"，是因为后者会让
#: `PUT/DELETE /d/{id}/api/pair` 也免鉴权进隧道 —— 虽然电脑端只有
#: POST 路由、其余会 405，但"白名单只管路径不管方法"是一条下次会被
#: 复制的坏模式（`docs/06` 规则 15 对 CORS 前缀匹配的批评同源）。
PUBLIC_POST_PATHS: frozenset[str] = frozenset({
    "/api/pair",
    # 游客兑换邀请：朋友点开链接时手里只有邀请码（pbg1.…），还没有会话令牌，
    # 而这一步本身就是在换令牌。与 `/api/pair` 同构，所以同样只放行 POST。
    "/api/guest/redeem",
})
PAIR_RATE_PATHS: frozenset[str] = frozenset({"/api/pair"})

#: 匿名探活的限流（每个来源 IP，每秒）。比鉴权路径的 120/s 宽，因为
#: 它不读库、不落盘，且状态机的探活频率天然更高。
PUBLIC_RATE_PER_SEC: int = 300

#: 匿名**配对**的限流（每个来源 IP，每分钟）。
#:
#: 比探活严格得多，因为配对码只有 6 位。10 次/分钟 = 每天最多约 1.4 万次
#: 猜测，命中率约 1.4%；而电脑端还有一层"每个码最多 5 次、锁定 10 分钟"，
#: 真正能到手的成功率远低于此。云端这一层的意义是**别让公网把家里的电脑
#: 拖进暴力破解**：每次猜测都要穿透隧道、落一次审计。
PAIR_RATE_PER_MIN: int = 10


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

    __slots__ = ("registry", "db", "rate", "public_rate", "pair_rate",
                 "inflight", "inflight_total", "auth_rate", "auth_fail",
                 "uploads", "meter")

    def __init__(self, registry: DeviceRegistry, db: Any) -> None:
        self.registry = registry
        self.db = db
        self.rate = RateLimiter(RATE_LIMIT_PER_SEC)
        # 匿名探活按来源 IP 计数。放得比鉴权路径宽（300/s）：探活本来
        # 就比业务请求频率高（双通路状态机 1.5 s 一次，加上手机切网时
        # 的重探），而且它不读库、不落盘，成本只有一次隧道往返。
        self.public_rate = RateLimiter(PUBLIC_RATE_PER_SEC)
        # 匿名配对：按分钟计。窗口取 60 秒、额度 PAIR_RATE_PER_MIN。
        # 单独一个桶而不是复用 `public_rate`：探活被限流会让状态条显示
        # "离线"，而配对被限流只该影响"试着配一台新手机"，两件事的
        # 用户体验完全不同，混在一起的症状是"点了几下之后连状态都不对了"。
        self.pair_rate = RateLimiter(PAIR_RATE_PER_MIN, window=60.0)
        self.inflight = ConcurrencyLimiter(MAX_INFLIGHT_PER_DEVICE)
        # ★ 审计 F03：单设备 64 之外还要一个**全局**上限。没有它时三台设备
        #   各挂 60 个慢速 `/raw` 就是 180 条并发隧道往返 + 同样多的文件句柄，
        #   2 vCPU 的云主机撑不住，受影响的是别人的隧道。
        self.inflight_total = ConcurrencyLimiter(MAX_INFLIGHT_TOTAL)
        # ★ 审计 F03：按来源 IP 限制"要验令牌的请求"与其中的失败。
        #   两个桶分开：正常浏览会把 `auth_rate` 用得比较满（一次缩略图潮
        #   就有几十个请求），但失败计数只该被真正的猜令牌推高。
        self.auth_rate = RateLimiter(AUTH_RATE_PER_MIN, window=60.0)
        self.auth_fail = RateLimiter(AUTH_FAIL_MAX, window=AUTH_FAIL_WINDOW)
        self.uploads = UploadLimiter()
        self.meter = TrafficMeter(db)

    def _accounts_match(self, a: str, b: str) -> bool:
        """两台设备是否属于同一个账号（只用于把 403 拆成 E2011/E2012）。

        ★ 只做"结论细分"，不做放行判断：调用点已经在拒绝分支里了，
          这个函数回答的只是"该说哪一种不对"。所以查不到账号、或
          测试替身根本没有 DB（`db=object()`）时一律按"不同账号"报，
          宁可让文案偏严，也不让一次诊断查询把拒绝变成 500。
        """
        q = getattr(self.db, "query_one", None)
        if q is None:
            return False
        try:
            mine = q("SELECT account_id FROM devices WHERE id = ?", (a,))
            theirs = q("SELECT account_id FROM devices WHERE id = ?", (b,))
        except Exception:  # noqa: BLE001 - 诊断失败不该改变"拒绝"这个结论
            return False
        if not mine or not theirs:
            return False
        return str(mine["account_id"]) == str(theirs["account_id"])

    async def handle(self, request: Request, device_id: str) -> Response:
        rid = request.headers.get("x-request-id") or ""

        # ---- 0 匿名探活 / 匿名配对 ----
        # `docs/04` §1.2 与 §4.1.2 都写死了一条：`/api/system/ping` 必须匿名，
        # 因为它在**通路选择阶段**被调用 —— 那时手机可能还没有令牌（首次使用），
        # 也可能令牌刚过期。如果云端要求令牌，云端通路就永远探测不通，
        # 双通路状态机会把所有"没配对的手机"判成 OFFLINE。
        #
        # 这样放开安全吗：`device_id` 是 32 位 hex（128 bit 能力值），猜不到；
        # ping 只回 device_id/device_name/version，与一期 CORS 审计里已经
        # 泄漏的那几个字段同集，不含任何照片信息。限流仍然生效（下面按 IP 计数）。
        #
        # `/api/pair` 同理但更严：它在把 6 位码换成令牌，所以走独立的
        # `pair_rate`（10 次/分钟/IP），且必须把真实来源 IP 带进帧里，
        # 让电脑端那层"每码 5 次"按 IP 生效。
        if _is_public(request):
            return await self._handle_public(request, device_id, rid)

        # ---- 1/2/3 鉴权 ----
        ip = _client_ip(request)
        # 先按来源 IP 记一次"鉴权尝试"。放在查库之前，因为它挡的正是
        # "用同一个来源 IP 反复送令牌"（审计 F03）。
        allowed, retry = self.auth_rate.hit(ip)
        if not allowed:
            return _error(429, "E4001", "请求太频繁", request_id=rid,
                          retry_after=max(1, int(retry)))

        token = _client_token(request)
        if not token:
            # ★ F08 / M7-023：**完全没带凭证时不自作回答、也不回源**。
            #
            #   两条约束同时成立才叫"对"：
            #   1. 同一条路径经隧道与直连必须给出同一段文案（`docs/01`
            #      AC-6.7、`docs/04` §1.4.3）。所以这里的 401 体逐字段
            #      镜像电脑端：`message` 用 `client_token_invalid`、`detail`
            #      用 `缺少凭证：Authorization 头`、`user_hint` 也照抄 ——
            #      不能用码表里那句中文 message（那是电脑端的 user_hint）。
            #   2. 不能为了"同形"把匿名请求转给电脑端。审计 F03 明确写了
            #      "形状检查必须在回源之前"：云端不认识的令牌会触发
            #      `_adopt_token_via_agent()`，一次隧道往返让家里的电脑替
            #      陌生人验签。匿名请求同理 —— 它连验签的资格都没有，不该
            #      占用电脑的一条在途额度（`tests/test_audit_f03_forward.py`
            #      的 `test_missing_token_is_rejected_without_asking_the_device`
            #      守的就是这条）。
            return self._reject_auth(ip, rid, detail=_DEVICE_NO_TOKEN_DETAIL)
        # ★ 审计 F03：形状检查必须在**回源之前**。云端不认识的令牌会触发
        #   `_adopt_token_via_agent()` —— 一次隧道往返，让家里的电脑替陌生人
        #   验签，验过还会往 `clients` 里插一行。`Bearer a` 这种垃圾值不该
        #   有资格把电脑变成验签器。
        if not looks_like_token(token):
            return self._reject_auth(ip, rid, clear_cookie=True)
        row = find_client_by_token(self.db, token)
        if row is None:
            # --- 局域网配对签发的令牌 ---
            #
            # 产品里手机只走 `POST /api/pair`（局域网），那枚令牌从没进过
            # 云端库。这里用 `devices.agent_token` 验签并收编（见
            # `find_or_adopt_lan_token` 的 docstring）；验不过仍然是 401。
            row = find_or_adopt_lan_token(self.db, token, device_id)
        if row is None:
            # 电脑端用**自己的**密钥签的令牌（2026-10-26 起默认）：云端没有
            # 密钥，只能请电脑判一次，判过再把哈希收编进 `clients`。
            row = await self._adopt_token_via_agent(token, device_id)
        if row is None:
            # 令牌死透了：顺手清掉浏览器里的死 cookie，否则 `<img>` 会
            # 用同一个死令牌一直 401（它发不出 Authorization 头）。
            return self._reject_auth(ip, rid, clear_cookie=True)
        if str(row["device_id"]) != device_id:
            # ★ 没有这一步，手机上任何一次配对都能读**别的**设备。
            # 令牌里的 device_id 是唯一可信的归属，路径里的只是它想访问的。
            #
            # ★ 审计 BUG-1：两种"不对"要分开报，否则只剩一句含糊的
            #   `token_resource_mismatch`：
            #     · 同一账号下的另一台电脑 → E2011（他大概点错了设备）
            #     · 另一个账号的电脑       → E2012（这枚令牌不是这个账号的）
            if self._accounts_match(str(row["device_id"]), device_id):
                return _error(403, "E2011", "配对不属于这台电脑",
                              request_id=rid)
            return _error(403, "E2012", "登录的账号无权访问这台电脑",
                          request_id=rid)
        client_id = str(row["id"])
        return await self._forward(request, device_id, rid,
                                   client_id=client_id,
                                   client_ip=_client_ip(request),
                                   rate_key=client_id)

    async def _forward(self, request: Request, device_id: str, rid: str, *,
                       client_id: str, client_ip: str,
                       rate_key: str) -> Response:
        """配额 + 并发闸门 + 回源。

        `rate_key` 是 client 级频率桶的键 —— 一律用 `client_id`，**不要**
        拿空串或占位值当键：那会把所有请求挤进同一个桶，一个人就能把别人
        的额度吃光。匿名请求根本走不到这里（在 `handle()` 里就被 401 挡掉
        了，见 M7-023 那段注释）。
        """
        # ---- 4 频率 ----
        if rate_key:
            ok, retry = self.rate.hit(rate_key)
            if not ok:
                return _error(429, "E4001", "请求太频繁", request_id=rid,
                              retry_after=max(1, int(retry)))
        # ★ 审计 F03：全局并发闸门在设备闸门**之前**。顺序反过来时，
        #   一台设备刷满自己 64 个额度后就不再碰全局计数，全局上限形同虚设。
        if not self.inflight_total.take(_INFLIGHT_ALL):
            log.warning("全局回源并发已满（%d），拒绝 device=%s",
                        MAX_INFLIGHT_TOTAL, device_id)
            return _error(429, "E4003", "云端回源并发已满，请稍后重试",
                          retry_after=1, request_id=rid)
        if not self.inflight.take(device_id):
            self.inflight_total.release(_INFLIGHT_ALL)
            return _error(429, "E4003", "请求太密集", retry_after=1,
                          request_id=rid)

        # ★ 审计 F06：额度必须**活到响应体传完**。原来这里是
        #   `try: return await self._relay(...) finally: release()` ——
        #   而 `_relay` 返回的是 StreamingResponse，`return` 那一刻响应体
        #   还在从隧道里往手机灌。于是手机上挂 64 个慢速 `/raw` 就能
        #   把在途计数刷回 0，再开 64 个，隧道任务与文件句柄无上限增长。
        #   现在把释放交给 `_release_with_body()`：非流式响应立刻释放，
        #   流式响应等生成器结束（正常结束、异常、客户端断开都算）。
        release = _Once(lambda: self._release_inflight(device_id))
        try:
            # 来源 IP 也要带进帧（不只是匿名配对那一条）：电脑端的审计
            # 记录与 `_rate_limit` 都读 `Ctx.client_ip`，缺了会全部落成
            # `"cloud"` 一个桶。
            resp = await self._relay(request, device_id, rid,
                                     client_id=client_id,
                                     client_ip=client_ip)
        except BaseException:
            release()
            raise
        return _release_with_body(resp, release)

    def _release_inflight(self, device_id: str) -> None:
        """还两个闸门的额度：设备一个、全局一个。

        两个必须在同一处还：全局额度是在设备闸门之前拿的（`handle` 里那个
        顺序是有原因的），任何一条只还其一的路径都会让全局计数慢慢漏高，
        最后表现成"没人访问但云端一直 429"。
        """
        self.inflight.release(device_id)
        self.inflight_total.release(_INFLIGHT_ALL)

    def _reject_auth(self, ip: str, rid: str, *,
                     clear_cookie: bool = False,
                     detail: str = _DEVICE_BAD_TOKEN_DETAIL) -> Response:
        """一次认证失败：先记账，超限改判 429，否则按原样 401。

        401 体逐字段镜像电脑端（`M7-023`）：`message`/`user_hint` 两处与
        电脑端 `backend/app/errors.py` 保持字面一致，只有 `detail` 随情形
        变（没带凭证 vs 令牌不可用）。`detail` 只会是下面两个常量之一，
        不拼接任何令牌、client_id 或路径。
        """
        allowed, retry = self.auth_fail.hit(ip)
        if not allowed:
            log.warning("来源 IP 认证失败过多 ip=%s", ip)
            return _error(429, "E4001", "认证失败次数过多，请稍后再试",
                          request_id=rid, retry_after=max(1, int(retry)))
        return _error(401, "E2002", _DEVICE_AUTH_MESSAGE, request_id=rid,
                      clear_cookie=clear_cookie, detail=detail,
                      user_hint=_DEVICE_AUTH_HINT)

    async def _adopt_token_via_agent(self, token: str,
                                     device_id: str) -> Any:
        """请电脑端验一枚云端不认识的令牌，并把结果收编进 `clients`。

        典型来源：手机在家连局域网、`POST /api/pair` 拿到的令牌 —— 云端
        没参与那次配对，所以库里没有它的哈希。以前靠"云端也算得出同一个
        HMAC 密钥"来验，而那正是数据库泄露就能伪造令牌的根源。

        验过之后必须落库：不落库的话这枚令牌在账号后台里是隐形的，
        用户看到"已配对 0 台手机"，点不了移除。
        """
        result = await cloud_control.call_agent(
            cloud_control.conn_for(self.registry, device_id), "verify_client",
            {"token": token, "device_id": device_id})
        if not result or not result.get("valid"):
            return None
        client_id = str(result.get("client_id") or "")[:64]
        expires_at = float(result.get("expires_at") or 0.0)
        if not client_id or expires_at <= time.time():
            return None
        name = str(result.get("name") or "").strip()[:64] or "手机"
        now = time.time()
        try:
            self.db.execute(
                "INSERT INTO clients(id, device_id, name, token_hash, last_seen,"
                " created_at, revoked, expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, ?)"
                " ON CONFLICT(id) DO UPDATE SET name = excluded.name,"
                "   token_hash = excluded.token_hash,"
                "   expires_at = excluded.expires_at",
                (client_id, device_id, name, security.hash_token(token), now,
                 now, expires_at))
        except Exception as exc:        # noqa: BLE001
            log.debug("收编局域网令牌失败：%s", type(exc).__name__)
            return None
        return find_client_by_token(self.db, token)

    async def _handle_public(self, request: Request, device_id: str,
                             rid: str) -> Response:
        """匿名转发（`GET /api/system/ping` 与 `POST /api/pair`）。

        与鉴权分支共用同一条转发路径，但限流键换成**来源 IP**：没有
        `client_id` 可用，而"不设限流"会让这个匿名端点变成免费的打点
        工具（每个请求都要穿透隧道，成本落在用户自己家里那台电脑上）。

        配对走**另一个**桶，而且额度小一个量级 —— 见 `PAIR_RATE_PER_MIN`
        的注释：6 位码 + 公网匿名 = 必须挡住。
        """
        ip = _client_ip(request)
        limiter = self.pair_rate if _is_pair(request) else self.public_rate
        ok, retry = limiter.hit(ip)
        if not ok:
            return _error(429, "E4001", "请求太频繁", request_id=rid,
                          retry_after=max(1, int(retry)))
        return await self._relay(request, device_id, rid, client_id="",
                                 client_ip=ip)

    async def _relay(self, request: Request, device_id: str, rid: str,
                     *, client_id: str, client_ip: str = "") -> Response:
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
                    client_ip=client_ip,
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
            # ★ 正常读完 vs 中途断（审查 R-M5-01）：只有正常读完才配 `_reap`。
            #   手机锁屏/返回导致生成器被取消时，`_reap` 只清本地状态、**不发
            #   CANCEL**，而电脑端的 handler 还在往一条没人读的响应里写，一直
            #   写到它自己的总超时（30 分钟）才放手 —— 期间那条隧道连接被占着。
            finished = False
            try:
                async for chunk in pending.iter_body(
                        idle_timeout=idle, total_timeout=total):
                    sent += len(chunk)
                    conn.bytes_out_today += len(chunk)
                    yield chunk
                finished = True
            except asyncio.CancelledError:
                # 客户端自己走了（手机锁屏、用户返回）：静默收尾是对的，
                # 此时往一个已经关掉的响应里写错误只会产生噪音。
                log.debug("手机取消响应体 device=%s sent=%d", device_id, sent)
                raise
            except asyncio.TimeoutError:
                # ★ 这里原来是 `log.info` 后静默返回 —— 而 `RES_HEAD` 早已
                #   把 200 发出去了，生成器正常结束在 chunked 传输下等于
                #   **一份合法的、完整的、短了的响应**。手机拿到半张照片，
                #   状态码是 200，所有层都无感。这是数据完整性问题。
                #   唯一能让客户端察觉的办法是**让连接异常终止**：
                #   抛出去 → Starlette 的 StreamingResponse 中断 → uvicorn
                #   不写结束块直接关连接 → 客户端拿到的是"响应不完整"，
                #   而不是"文件就这么长"。
                log.warning("电脑端响应体超时，中断传输 device=%s sent=%d",
                            device_id, sent)
                raise RuntimeError(
                    f"响应体超时，已发送 {sent} 字节，传输不完整") from None
            except MuxClosed as exc:
                log.warning("隧道断开，中断传输 device=%s sent=%d：%s",
                            device_id, sent, exc)
                raise RuntimeError(
                    f"隧道断开，已发送 {sent} 字节，传输不完整") from None
            except Exception:       # noqa: BLE001
                log.warning("响应体流转发中断 device=%s sent=%d",
                            device_id, sent, exc_info=True)
                raise
            finally:
                release()
                self.meter.add(device_id, bytes_in=up.total,
                               bytes_out=sent, requests=1)
                if finished:
                    _reap(pump, pending, conn, cancel)
                else:
                    # ★ 不能在**已取消**的生成器的 finally 里 `await`：取消传播
                    #   中任何 await 都会立刻再抛 CancelledError，`_abort` 半途
                    #   而废 —— CANCEL 帧发不出去，电脑端那条流继续写到它自己的
                    #   总超时，隧道连接一直占着。实测「打开再关掉」32 次之后，
                    #   新的合法订阅就开始 429。
                    #   改成独立任务：它与这次取消无关地跑完，异常只记日志。
                    _abort_task = asyncio.create_task(
                        _abort(conn, pending, cancel, pump))
                    _abort_task.add_done_callback(_swallow_abort_error)

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
           clear_cookie: bool = False, detail: str | None = None,
           user_hint: str | None = None) -> Response:
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
                                 request_id=request_id, detail=detail,
                                 user_hint=user_hint),
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

    只读方法 + 精确路径匹配；此外还放行 `PUBLIC_POST_PATHS` 里的
    非只读路径（目前只有配对）。刻意不用 `startswith`：`/api/system/ping/../info`
    这种在 Starlette 里不会路由到 info，但"前缀匹配"这个写法一旦被复制到
    别处就是一条越权路径（审计里 `docs/06` 规则 15 对 CORS 前缀匹配的
    批评是同一个道理）。
    """
    path = _inner_path(request)
    if request.method in ("GET", "HEAD"):
        return path in PUBLIC_PATHS
    if request.method == "POST":
        return path in PUBLIC_POST_PATHS
    return False


def _is_pair(request: Request) -> bool:
    """是不是一次匿名配对（决定用哪个限流桶）。"""
    return _inner_path(request) in PAIR_RATE_PATHS


def _client_ip(request: Request) -> str:
    """限流用的来源标识。

    判据在 `netutil`：只有当**直连对端**落在 `PB_CLOUD_TRUSTED_PROXIES`
    里时才采信 `X-Forwarded-For`。这个值同时喂两处 —— `public_rate` /
    `pair_rate` 的桶，以及塞进隧道帧的 `Ctx.client_ip`。二者都必须是真实
    客户端：对端若是 `127.0.0.1`（cloudflared / 80 端口转发）而这里直接
    取 `request.client.host`，全世界的手机就共用同一个桶，一台手机发几次
    请求就能让所有手机的探活一起 429（`docs/11` ISSUE-030）。
    """
    return netutil.client_ip(request)


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


def _swallow_abort_error(task: "asyncio.Task[None]") -> None:
    """`_abort` 是收尾动作：失败只记日志，不能冒泡成未处理的任务异常。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("取消订阅时向电脑端发 CANCEL 失败：%s", exc)


def _release_with_body(resp: Response, release: "_Once") -> Response:
    """把"还额度"这件事挂到响应体的生命周期上（审计 F06）。

    - 非流式响应（JSON 错误、304）：体已经完整在内存里，立即释放；
    - 流式响应：包一层生成器，`finally` 里释放 —— 正常结束、抛错、
      客户端中途断开（Starlette 会 `aclose()`）三种情况都会走到。
    """
    it = getattr(resp, "body_iterator", None)
    if it is None:
        release()
        return resp

    async def _wrapped() -> AsyncIterator[bytes]:
        try:
            async for chunk in it:
                yield chunk
        finally:
            release()

    resp.body_iterator = _wrapped()
    return resp


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
