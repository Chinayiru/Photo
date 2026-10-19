"""`shared/mux.py` 的单元测试：多路复用、流控、超时、取消。

## 为什么这些用例必须存在

`docs/07` §5.1 的 29 条隧道用例里，有一半是**协议层**的：req_id 不串台、
credit 不死锁、CANCEL 真的释放资源。这些性质用"跑一遍端到端"是测不出来的
—— 端到端只会告诉你"这次没出错"，而多路复用的 bug 恰恰是概率性的。

所以这里用一个 `_Loopback` 把 `Mux` 的两端接在同一个事件循环里
（一端 `sender` 直接 `feed` 给另一端），可以在**单进程、无 socket、无
sleep** 的条件下精确构造出"第 65 个请求"、"credit 归零后停 31 秒"、
"响应在请求体还没发完时就到了"这类时序。

## 有意不测的东西

不测 `encode_frame/decode_frame` —— 那是 `test_protocol.py` 的地盘，
重复测试只会让两处断言一起漂。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from shared.mux import (                                          # noqa: E402
    MAX_ACTIVE,
    MAX_BODY_CHUNK,
    InboundRequest,
    Mux,
    MuxClosed,
    PendingRequest,
    ReqIdAllocator,
)
from shared.protocol import (                                     # noqa: E402
    CREDIT_DEADLOCK_TIMEOUT,
    HEAD_TIMEOUT,
    FrameType,
    TooManyRequests,
    decode_frame,
    encode_frame,
)


# ---------------------------------------------------------------------------
# ReqIdAllocator：单调、回绕、跳过活跃
# ---------------------------------------------------------------------------

def test_allocator_is_monotonic_from_one() -> None:
    """req_id 从 1 开始单调递增 —— 0 是连接级保留值。"""
    a = ReqIdAllocator()
    assert [a.alloc() for _ in range(5)] == [1, 2, 3, 4, 5]


def test_allocator_skips_active_ids_after_wrap() -> None:
    """回绕时跳过仍活跃的 id，绝不复用（复用会让帧串到别人的响应里）。"""
    a = ReqIdAllocator()
    a._next = 0x3FFFFFFF          # 手动逼近回绕点
    first = a.alloc()             # 0x3FFFFFFF
    second = a.alloc()            # 回绕到 1
    assert first == 0x3FFFFFFF
    assert second == 1
    third = a.alloc()
    assert third == 2
    a.release(second)
    # 回绕之后的分配不会回头去捡 1（_next 只前进），这是刻意的：
    # 捡旧 id 会让"迟到的旧帧"撞上新请求。
    assert a.alloc() == 3


def test_allocator_raises_when_saturated() -> None:
    """活跃数达上限时抛 `TooManyRequests`（映射到云端 429 E4003）。"""
    a = ReqIdAllocator(max_active=3)
    for _ in range(3):
        a.alloc()
    with pytest.raises(TooManyRequests):
        a.alloc()
    assert a.active == 3


def test_allocator_release_frees_the_slot() -> None:
    a = ReqIdAllocator(max_active=1)
    rid = a.alloc()
    a.release(rid)
    assert a.active == 0
    assert a.alloc() == rid + 1


# ---------------------------------------------------------------------------
# 环回装置
# ---------------------------------------------------------------------------

class _Loopback:
    """把两个 `Mux` 的 `sender` 互接到对方的 `feed`。

    `delay_left` 用来制造"响应比请求体先到"的时序：左侧发帧时先
    `await asyncio.sleep(delay)`，右侧（电脑端）因此有机会在左侧还在
    `send_body()` 时就回 `RES_HEAD`。
    """

    def __init__(self, *, delay_left: float = 0.0,
                 delay_right: float = 0.0,
                 window: int = 8 * 1024 * 1024) -> None:
        self.delay_left = delay_left
        self.delay_right = delay_right
        self.left: Mux | None = None
        self.right: Mux | None = None
        self.left_frames = 0
        self.right_frames = 0
        self._window = window

    def build(self) -> tuple[Mux, Mux]:
        async def to_right(data: bytes) -> None:
            self.left_frames += 1
            if self.delay_left:
                await asyncio.sleep(self.delay_left)
            await self.right.feed(decode_frame(data))    # type: ignore[union-attr]

        async def to_left(data: bytes) -> None:
            self.right_frames += 1
            if self.delay_right:
                await asyncio.sleep(self.delay_right)
            await self.left.feed(decode_frame(data))     # type: ignore[union-attr]

        self.left = Mux(to_right, window=self._window, role="client")
        self.right = Mux(to_left, window=self._window, role="server")
        return self.left, self.right


async def _echo_handler(req: InboundRequest, *, tag: str = "") -> None:
    """把请求体原样回显，并在头部带上 req_id —— 用来验"不串台"。"""
    mux: Mux = _ECHO_MUX[req.req_id]                     # type: ignore[index]
    body = bytearray()
    async for chunk in req.iter_body():
        body.extend(chunk)
    await mux.res_head(req.req_id, 200, {"content-type": "application/octet-stream"},
                       body_len=len(body))
    if body:
        await mux.res_body(req.req_id, bytes(body))
    await mux.res_end(req.req_id)


#: `on_request` 只有 `InboundRequest` 一个参数，拿不到 mux。
#: 用一个模块级映射补上（只在测试里这么干，生产侧用 functools.partial）。
_ECHO_MUX: dict[int, Mux] = {}


def _silent_mux() -> Mux:
    """一个"发出去了但永远不回"的对端。

    超时/清扫/关闭这三类用例**必须**用这个而不是 `_Loopback`：
    `_Loopback` 的另一端若没设 `on_request`，`_on_req_head` 会立刻回
    `ERROR "peer is not a server"`，于是请求瞬间失败，超时路径根本
    走不到（第一版就踩了这个坑，四条用例全部假红）。
    """
    async def sink(_data: bytes) -> None:
        return

    return Mux(sink, role="silent")


# ---------------------------------------------------------------------------
# 多路复用：不串台
# ---------------------------------------------------------------------------

def _run(coro) -> object:
    return asyncio.run(coro)


def test_ten_concurrent_requests_do_not_cross_talk() -> None:
    """TC-TUNNEL-FUNC-003 / AC-1.3：10 个并发请求各自拿回自己的字节。

    这是整条隧道最要紧的性质。串台的表现不是报错，而是"照片内容
    对不上"——用户会以为是缓存问题，实际上两路响应被拼到了同一个
    req_id 上。
    """
    async def main() -> list[bytes]:
        lb = _Loopback()
        left, right = lb.build()

        async def on_request(req: InboundRequest) -> None:
            _ECHO_MUX[req.req_id] = right
            await _echo_handler(req)

        right.on_request = on_request

        async def one(i: int) -> bytes:
            payload = f"payload-{i}-".encode() * 100 + bytes([i])
            p = await left.begin({"method": "POST", "path": "/echo"},
                                 _body_stream([payload]))
            head = await p.wait_head(timeout=5)
            assert head["status"] == 200, head
            out = bytearray()
            async for chunk in p.iter_body(idle_timeout=5):
                out.extend(chunk)
            left.finish(p.req_id)
            return bytes(out)

        return await asyncio.gather(*(one(i) for i in range(10)))

    results = _run(main())
    assert len(results) == 10
    for i, got in enumerate(results):
        want = f"payload-{i}-".encode() * 100 + bytes([i])
        assert got == want, f"第 {i} 路响应被串台：{got[:40]!r}"


async def _body_stream(chunks):
    for c in chunks:
        yield c


def test_slow_request_does_not_block_fast_one() -> None:
    """TC-TUNNEL-FUNC-004：慢请求不阻塞快请求，且各自仍匹配自己的 req_id。"""
    async def main() -> list[str]:
        lb = _Loopback()
        left, right = lb.build()
        order: list[str] = []

        async def on_request(req: InboundRequest) -> None:
            _ECHO_MUX[req.req_id] = right
            name = req.head.get("name", "?")
            if name == "slow":
                await asyncio.sleep(0.05)
            async for _ in req.iter_body():
                pass
            await right.res_head(req.req_id, 200, {"x-name": name}, body_len=0)
            await right.res_end(req.req_id)
            order.append(name)

        right.on_request = on_request

        async def one(name: str) -> str:
            p = await left.begin({"method": "GET", "path": "/x", "name": name},
                                 _body_stream([]))
            head = await p.wait_head(timeout=5)
            left.finish(p.req_id)
            return str(head["headers"].get("x-name"))

        got = await asyncio.gather(one("slow"), one("fast"))
        assert got == ["slow", "fast"], got
        assert order == ["fast", "slow"], "快的必须先完成"
        return order

    _run(main())


def test_head_arrives_before_request_body_finishes() -> None:
    """`open()` + `send_body()` 的存在理由：电脑端可以提前回 4xx。

    手机上传 2 GB 时，扩展名不支持之类的错误必须在第一块就回给手机，
    而不是等它把 2 GB 传完。这条用例钉住的就是这个时序。
    """
    async def main() -> tuple[int, bool]:
        lb = _Loopback()
        left, right = lb.build()
        right_head: list[int] = []

        async def on_request(req: InboundRequest) -> None:
            _ECHO_MUX[req.req_id] = right
            # 只读第一块就下结论（模拟 sniff 到 .txt）
            first = await req.body.get()
            assert first is not None
            await right.res_head(req.req_id, 400,
                                 {"content-type": "application/json"},
                                 body_len=2)
            await right.res_body(req.req_id, b"[]")
            await right.res_end(req.req_id)
            right_head.append(400)

        right.on_request = on_request

        p = await left.open({"method": "POST", "path": "/upl"})
        await left.send_body(p, b"x" * 1024)
        # 关键：还没 end_body 就已经能拿到响应头
        head = await p.wait_head(timeout=2)
        left.finish(p.req_id)
        return int(head["status"]), bool(right_head)

    status, seen = _run(main())
    assert (status, seen) == (400, True)


def test_set_cookies_keeps_multi_value_semantics() -> None:
    """两条 cookie 必须各自到达，不能被压成 `"['a', 'b']"`。

    `<img>` 标签发不出 `Authorization` 头，只能靠 cookie 鉴权；
    压扁的表现是"登录成功但缩略图全 401"。
    """
    async def main() -> list[str]:
        lb = _Loopback()
        left, right = lb.build()

        async def on_request(req: InboundRequest) -> None:
            _ECHO_MUX[req.req_id] = right
            await right.res_head(req.req_id, 200, {}, body_len=0,
                                 set_cookies=["a=1; Path=/",
                                              "b=2; Path=/; HttpOnly"])
            await right.res_end(req.req_id)

        right.on_request = on_request
        p = await left.begin({"method": "GET", "path": "/pair"},
                             _body_stream([]))
        head = await p.wait_head(timeout=5)
        left.finish(p.req_id)
        return list(head.get("set_cookies") or [])

    cookies = _run(main())
    assert cookies == ["a=1; Path=/", "b=2; Path=/; HttpOnly"], cookies


# ---------------------------------------------------------------------------
# 流控
# ---------------------------------------------------------------------------

def test_credit_gate_take_and_grant() -> None:
    """额度用尽后 `take` 阻塞，收到 `grant()` 立刻放行。"""
    from shared.mux import _CreditGate

    async def main() -> list[int]:
        gate = _CreditGate(window=1024)
        await gate.take(1024)
        assert gate.credit == 0
        blocked = asyncio.ensure_future(gate.take(512))
        await asyncio.sleep(0)
        assert not blocked.done(), "额度不足时不许直接放行"
        await gate.grant()
        await asyncio.wait_for(blocked, timeout=1)
        return [gate.credit]

    _run(main())


def test_credit_deadlock_raises_instead_of_hanging() -> None:
    """TC-TUNNEL-EDGE-004：对端永不回 CREDIT 时必须抛，不能挂死。

    挂死的后果是云端的 HTTP 响应流永远不结束，手机上的转圈一直转到
    用户放弃 —— 而服务端一条日志都没有。
    """
    from shared.mux import _CreditGate

    async def main() -> str:
        gate = _CreditGate(window=64)
        await gate.take(64)
        # 把 30 秒的等待压缩掉：直接改判定用的常量不会影响已实例化的对象，
        # 所以这里靠 monkeypatch 之外的手段不现实 —— 用极短窗口 +
        # 直接调用内部超时路径（真实时间是 30 s，太慢）。
        import shared.mux as m

        orig = m.CREDIT_DEADLOCK_TIMEOUT
        m.CREDIT_DEADLOCK_TIMEOUT = 0.05
        try:
            try:
                await gate.take(1)
            except MuxClosed as exc:
                return str(exc)
            return "NO-RAISE"
        finally:
            m.CREDIT_DEADLOCK_TIMEOUT = orig

    out = _run(main())
    assert out != "NO-RAISE", "信用窗口耗尽后必须抛 MuxClosed（E5010）"
    assert "E5010" in out, out


def test_receiver_acknowledges_after_threshold() -> None:
    """接收侧攒够半个窗口就回一次 CREDIT（不能每块都回，否则帧数翻倍）。"""
    from shared.mux import _CreditGate

    gate = _CreditGate(window=8 * 1024 * 1024)
    assert gate.on_recv(1024) == 0
    assert gate.on_recv(4 * 1024 * 1024) == 1        # 恰好到一半
    assert gate.on_recv(1024) == 0


# ---------------------------------------------------------------------------
# 超时 / 取消 / 关闭
# ---------------------------------------------------------------------------

def test_head_timeout_is_reported_not_hung() -> None:
    """对端完全不回 `RES_HEAD` 时，`wait_head` 超时抛出（映射 E5009）。"""
    async def main() -> str:
        left = _silent_mux()
        p = await left.open({"method": "GET", "path": "/x"})

        async def consume() -> None:
            try:
                await p.wait_head(timeout=0.05)
            except asyncio.TimeoutError:
                return
            pytest.fail("应当超时")

        await consume()
        await left.close("test")
        return "ok"

    assert _run(main()) == "ok"


def test_sweeper_cleans_abandoned_requests() -> None:
    """没人再 `await` 的在途请求必须被清扫协程释放，否则 4096 个后全站 429。"""
    async def main() -> int:
        import shared.mux as m

        left = _silent_mux()
        orig = m.SWEEP_INTERVAL
        m.SWEEP_INTERVAL = 0.02
        try:
            await left.start()
            p = await left.open({"method": "GET", "path": "/x"}, timeout=0.05)
            assert left.stats["active"] == 1
            await asyncio.sleep(0.2)
            assert left.stats["active"] == 0, "清扫器没释放超时请求"
            # head 必须已被置为失败，否则调用方会永远等下去
            head = await p.wait_head(timeout=1)
            assert head["status"] == 0 and head["error"]["code"] == "E5009"
        finally:
            m.SWEEP_INTERVAL = orig
            await left.close("done")
        return 0

    assert _run(main()) == 0


def test_cancel_from_peer_releases_resources() -> None:
    """TC-TUNNEL-FUNC-009 / AC-4.4：收到 CANCEL 后两侧都不再持有该 req_id。

    方向要搞对：**发起侧**（云端）在手机断开时调 `cancel()`，帧发到
    **响应侧**（电脑端），响应侧据此置 `cancelled` 并结束请求体队列。
    电脑端自己 `cancel()` 只发帧、不置本地位（否则就成了"自己取消自己"）。
    """
    async def main() -> tuple[int, int, bool]:
        lb = _Loopback()
        left, right = lb.build()
        started = asyncio.Event()
        saw_cancel = asyncio.Event()

        async def on_request(req: InboundRequest) -> None:
            started.set()
            await req.cancelled.wait()
            saw_cancel.set()
            # handler 从 cancelled 醒来后必须能靠 None 哨兵退出读循环
            got = await req.body.get()
            assert got is None, "取消时请求体队列必须收到 None 哨兵"

        right.on_request = on_request
        p = await left.open({"method": "GET", "path": "/big"})
        await asyncio.wait_for(started.wait(), timeout=2)
        assert left.stats["active"] == 1 and right.stats["active"] == 1

        # 手机断开 → 云端（left）发 CANCEL
        await left.cancel(p.req_id, "client_gone")
        await asyncio.wait_for(saw_cancel.wait(), timeout=2)
        head = await p.wait_head(timeout=1)
        assert head["error"]["code"] == "E5011", head
        await asyncio.sleep(0.05)
        active_left = left.stats["active"]
        active_right = right.stats["active"]
        await left.close("done")
        await right.close("done")
        return active_left, active_right, True

    assert _run(main()) == (0, 0, True)


def test_close_wakes_every_pending_request() -> None:
    """连接断了以后没有任何帧会来，所有在途请求必须立刻失败返回。

    否则云端 HTTP 响应流会挂到手机超时（"断网后转圈 5 分钟"）。
    """
    async def main() -> list[str]:
        left = _silent_mux()
        pending = [await left.open({"method": "GET", "path": f"/{i}"})
                   for i in range(3)]
        assert left.stats["active"] == 3
        await left.close("socket closed")
        assert left.stats["active"] == 0, "close 必须清空在途表"
        codes = []
        for p in pending:
            head = await p.wait_head(timeout=1)
            codes.append(str((head.get("error") or {}).get("code")))
        return codes

    assert _run(main()) == ["E5010", "E5010", "E5010"]


def test_body_oversize_is_rejected() -> None:
    """声明长度 10 却发来 11 字节 → E5003，且该 req_id 立刻结束。"""
    async def main() -> str:
        lb = _Loopback()
        left, right = lb.build()
        got: list[str] = []

        async def on_request(req: InboundRequest) -> None:
            _ECHO_MUX[req.req_id] = right
            async for _ in req.iter_body():
                pass

        right.on_request = on_request
        p = await left.open({"method": "POST", "path": "/u", "body_len": 10})
        await left.send_body(p, b"x" * 20)
        head = await p.wait_head(timeout=2)
        got.append(str((head.get("error") or {}).get("code")))
        left.finish(p.req_id)
        await left.close("x")
        await right.close("x")
        return got[0]

    assert _run(main()) == "E5003"


def test_max_body_chunk_fits_in_one_frame() -> None:
    """一块 body 必须能装进一帧：`MAX_BODY_CHUNK + 5` 不得超 `MAX_FRAME`。"""
    from shared.protocol import HEADER_SIZE, MAX_FRAME

    assert MAX_BODY_CHUNK + HEADER_SIZE <= MAX_FRAME
    # 顺带钉住"一整块 body 只产生一帧"，否则大文件帧数会翻倍
    data = encode_frame(FrameType.REQ_BODY, 7, b"y" * MAX_BODY_CHUNK)
    assert len(data) <= MAX_FRAME
    assert decode_frame(data).payload == b"y" * MAX_BODY_CHUNK


def test_active_cap_matches_docs() -> None:
    """MAX_ACTIVE=4096、HEAD_TIMEOUT=30、CREDIT_DEADLOCK_TIMEOUT=30（文档口径）。"""
    assert MAX_ACTIVE == 4096
    assert HEAD_TIMEOUT == 30.0
    assert CREDIT_DEADLOCK_TIMEOUT == 30.0


# ---------------------------------------------------------------------------
# 底层发送失败：必须翻译成 MuxClosed，不能漏成请求级 500
# ---------------------------------------------------------------------------

def test_send_failure_becomes_mux_closed() -> None:
    """`_send_raw` 抛非 `MuxClosed` 异常时必须翻译成 `MuxClosed`。

    ★ 这是集成冒烟才抓到的坑（`build/_p1_e2e.py`）。真实栈是：

        starlette/websockets.py send_bytes
        uvicorn/protocols/websockets/websockets_impl.py asgi_send
        RuntimeError: Unexpected ASGI message 'websocket.send',
                      after sending 'websocket.close' or response
                      already completed.

    这个 `RuntimeError` 一路穿过 `forward_request` → `Forwarder._relay`
    （它只 `except MuxClosed`）→ Starlette 的 ServerErrorMiddleware，
    最终手机拿到 **500 Internal Server Error** 而不是 503 E3003。
    触发窗口正是"电脑端刚掉线、注册表里那条连接还在"的瞬间。

    这里用一个总是抛 `RuntimeError` 的 sender 精确复现，不需要 socket。
    """
    async def main() -> tuple[str, bool, bool]:
        async def broken(_data: bytes) -> None:
            raise RuntimeError("Unexpected ASGI message 'websocket.send'")

        mux = Mux(broken, role="broken")
        try:
            await mux.open({"method": "GET", "path": "/x"})
        except MuxClosed as exc:
            first = str(exc)
        else:
            first = "NO-RAISE"
        # 失败过的连接必须被标记，后续帧立刻失败（不再去撞同一个错误）
        try:
            await mux.open({"method": "GET", "path": "/y"})
        except MuxClosed:
            second = True
        else:
            second = False
        # 且必须已经自我关闭：在途请求全部被唤醒，不能挂死
        return first, mux.closed, second

    first, closed, second = _run(main())
    assert first != "NO-RAISE", "底层发送异常必须翻译成 MuxClosed"
    assert "连接已失效" in first, first
    assert closed, "发送失败后 Mux 必须自我关闭，否则在途请求会挂到手机超时"
    assert second, "失效连接上的后续发送必须立刻失败"


def test_send_failure_fails_inflight_requests() -> None:
    """发送失败后，**已经在途**的请求必须立刻拿到失败而不是永远等待。

    只抛异常不 `close()` 的话，先 `open()` 成功、随后连接坏掉的请求会
    卡在 `wait_head()` 上，直到 30 秒 HEAD 超时才变成 504 —— 而真实的
    原因（连接已断）被完全掩盖。
    """
    async def main() -> str:
        calls = 0

        async def flaky(_data: bytes) -> None:
            nonlocal calls
            calls += 1
            if calls > 1:            # 第一条 REQ_HEAD 放行，之后全坏
                raise RuntimeError("socket closed")

        mux = Mux(flaky, role="flaky")
        p = await mux.open({"method": "GET", "path": "/a"})
        # 触发一次失败发送（CREDIT 也在 `_send_frame` 上）
        with pytest.raises(MuxClosed):
            await mux.send_credit()
        head = await p.wait_head(timeout=1)
        return str((head.get("error") or {}).get("code"))

    assert _run(main()) == "E5010"


def test_priority_frame_send_failure_is_swallowed() -> None:
    """入站 PING 就地回 PONG 时若连接已断，不能把异常抛进读循环。

    读循环的调用方（`cloud/relay/tunnel.py:_read_loop`）把异常当成
    "对端发来坏帧"并 `return`，于是**对端正常断开**会被记成一条
    warning 级日志，排障时指向错误的方向。
    """
    async def main() -> bool:
        async def broken(_data: bytes) -> None:
            raise RuntimeError("already closed")

        mux = Mux(broken, role="broken2")
        await mux.feed(decode_frame(encode_frame(FrameType.PING, 5)))
        return True

    assert _run(main()) is True


class _FakeConn:
    """`forward_request` 只用到 `conn.mux`，别的一概不碰。"""

    __slots__ = ("mux",)

    def __init__(self, mux: Mux) -> None:
        self.mux = mux


def test_forward_request_survives_past_head_timeout() -> None:
    """`cloud/relay/tunnel.py:forward_request` 的响应不能被头部超时截断。

    这是把 2 GB 传输截断在 264.7 MB 的那个 bug 的**端到端**回归。修法是把
    `mux.open(timeout=...)` 的参数从 `head_timeout` 换成 `total_timeout`；
    这条用例用一个"响应比头部超时慢很多、但远在总时长内"的对端来证明它。

    `head_timeout` 取 0.05 s、对端拖 0.4 s 才回头——若仍用头部超时当
    deadline，清扫器会在 0.05 s 后 `_abort`，`iter_body()` 拿不到任何
    字节（甚至连 head 都拿不到）。
    """
    async def main() -> tuple[int, bytes]:
        import shared.mux as m

        # ★ 必须把清扫周期压到毫秒级。默认是 5 秒，而这里只拖 0.4 秒 ——
        #   清扫器还没跑到第一轮，响应体就已经送完了，于是**用错了 timeout
        #   也照样通过**（变异测试实测：换回 head_timeout 时这条用例仍然绿）。
        orig = m.SWEEP_INTERVAL
        m.SWEEP_INTERVAL = 0.02
        lb = _Loopback()
        left, right = lb.build()

        async def on_request(req: InboundRequest) -> None:
            await asyncio.sleep(0.4)
            await right.res_head(req.req_id, 200, {}, body_len=5)
            await right.res_body(req.req_id, b"hello")
            await right.res_end(req.req_id)

        right.on_request = on_request
        try:
            await left.start()
            await right.start()
            from cloud.relay.tunnel import forward_request

            p, pump = await forward_request(
                _FakeConn(left),
                method="GET",
                path="/api/items/1/raw",
                query={},
                headers={},
                body=None,
                body_len=None,
                head_timeout=0.05,
                total_timeout=10.0,
            )
            head = await p.wait_head(timeout=5.0)
            data = bytearray()
            async for chunk in p.iter_body(idle_timeout=5.0,
                                           total_timeout=10.0):
                data.extend(chunk)
            if pump is not None:
                await pump
            return int(head.get("status") or 0), bytes(data)
        finally:
            m.SWEEP_INTERVAL = orig
            await left.close("done")
            await right.close("done")

    status, body = _run(main())
    assert status == 200, status
    assert body == b"hello", body


def test_forward_request_gives_sse_no_deadline() -> None:
    """`/api/events` 走 `total_timeout=None` 时 `deadline` 必须是无穷。

    否则清扫器每 30 分钟把手机的事件流掐一次，期间的 `upload_done`
    推送（AC-3.13）全部丢失。这条与 `forward.py:_total_timeout()` 的
    `端swith('/api/events')` 判断配套。
    """
    async def main() -> float:
        lb = _Loopback()
        left, right = lb.build()

        async def on_request(req: InboundRequest) -> None:
            return           # 永不应答，模拟 SSE 长连接

        right.on_request = on_request
        try:
            await left.start()
            await right.start()
            from cloud.relay.tunnel import forward_request

            p, _ = await forward_request(
                _FakeConn(left),
                method="GET",
                path="/api/events",
                query={},
                headers={},
                body=None,
                body_len=None,
                head_timeout=30.0,
                total_timeout=None,
            )
            return p.deadline - p.created_at
        finally:
            await left.close("done")
            await right.close("done")

    assert _run(main()) == float("inf")


def test_open_timeout_becomes_request_deadline() -> None:
    """`open(timeout=...)` 定的是**请求总时长**，`_sweep_once` 按它掐断。

    这是 2 GB 传输被截断的真根因：`cloud/relay/tunnel.py:forward_request`
    原本把 `head_timeout`(30 s) 当总时长传给 `mux.open()`，而 `open()`
    用它算 `PendingRequest.deadline`，清扫器到点就把请求连同响应体一起
    `_abort`。症状是**任何超过 30 秒的下载都在中途断掉**，收到的字节数
    恰好等于"30 秒 x 速率"——2 GB @ 8 MB/s 实测停在 264.7 MB / 33.1 s。

    这里不测"30 秒以后还在流"（那需要真的等），而是钉住不变式本身：
    `deadline` 必须由 `open()` 的参数决定，且 `None` 表示没有上限。
    """
    async def main() -> tuple[float, float, float]:
        mux = _silent_mux()
        short = await mux.open({"method": "GET", "path": "/a"}, timeout=30.0)
        longy = await mux.open({"method": "GET", "path": "/b"}, timeout=1800.0)
        forever = await mux.open({"method": "GET", "path": "/c"}, timeout=None)
        spans = (short.deadline - short.created_at,
                 longy.deadline - longy.created_at,
                 forever.deadline - forever.created_at)
        await mux.close("done")
        return spans

    short_span, long_span, forever_span = _run(main())
    assert abs(short_span - 30.0) < 1.0, short_span
    assert abs(long_span - 1800.0) < 1.0, long_span
    assert forever_span == float("inf"), forever_span


def test_sse_deadline_is_unbounded() -> None:
    """`GET /api/events` 走 `total_timeout=None`，清扫器永不掐它。

    `iter_body(total_timeout=None)` 是 SSE 的唯一入口。若 `open()` 仍拿
    30 秒算 `deadline`，手机上表现为"每半小时事件流莫名重连一次"——
    而重连本身会漏掉期间的 `upload_done` 推送（AC-3.13）。
    """
    async def main() -> bool:
        import shared.mux as m

        orig = m.SWEEP_INTERVAL
        m.SWEEP_INTERVAL = 0.02
        # 必须用 `_silent_mux`：`_Loopback` 的对端若没设 `on_request`，
        # 会立刻回 ERROR "peer is not a server"，请求瞬间消失。
        left = _silent_mux()
        try:
            await left.start()
            p = await left.open({"method": "GET", "path": "/api/events"},
                                timeout=None)
            assert p.deadline == float("inf")
            await asyncio.sleep(0.2)
            assert left.stats["active"] == 1, "无上限的请求被清扫器掐掉了"
            return True
        finally:
            m.SWEEP_INTERVAL = orig
            await left.close("done")

    assert _run(main()) is True


# ---------------------------------------------------------------------------
# 响应体队列满：两条历史上真的会"打死别的东西"的路径
# ---------------------------------------------------------------------------
#
# `body` 是 `asyncio.Queue(maxsize=_BODY_QUEUE_MAX=64)`。手机读得慢时它会
# 填满，而**填满之后的两个分支原来都会抛 `asyncio.QueueFull`**：
#
#   * `finish_body()` 里 `put_nowait(None)` —— 结束标记进不去 ⇒ 消费者
#     永远等在 `iter_body()` 里；更糟的是它从 `_sweep_once()` 与 `close()`
#     里被调用，抛出去会**打死清扫协程**或**让连接关不掉**。
#   * `_on_res_body()` 里 `await body.put(data)` 没有上限 ⇒ 堵住的是
#     **整条连接共享的读循环**，PING/CREDIT/CANCEL 一起收不到，云端
#     心跳（`DEAD_AFTER=75s`）会把整条隧道判死。
#
# 这两条都不是"慢一点"，是"一张没看完的图让所有请求一起失败"。

def _sender() -> tuple[object, list[bytes]]:
    """`Mux` 的构造参数是一个 `async def send(bytes)` 回调。"""
    sent: list[bytes] = []

    async def sink(data: bytes) -> None:
        sent.append(data)

    return sink, sent


def test_finish_body_never_raises_when_queue_is_full() -> None:
    """结束标记进不去满队列时不能抛，也**不能**为此丢掉一块数据。

    丢数据等于把一张照片截断，而调用方会把它当完整响应发给手机 ——
    比"多等一会儿"严重得多。所以契约是：不抛、数据一块不少、
    消费者仍然能收尾。
    """
    async def main() -> None:
        import shared.mux as m
        sink, _ = _sender()
        mux = Mux(sink, role="probe")
        p = PendingRequest(1, timeout=None)
        mux._pending[1] = p
        for _ in range(m._BODY_QUEUE_MAX):
            p.push_body(b"x" * 10)

        p.finish_body()                     # 原来这里抛 QueueFull

        # 数据一块都不能少；结束标记挤不进去是允许的（由 `_body_done` 兜）
        assert p.body.qsize() == m._BODY_QUEUE_MAX, (
            f"丢数据了：{p.body.qsize()} / {m._BODY_QUEUE_MAX}")
        assert p._body_done is True

        # 消费者仍然能收尾 —— 不依赖队列里那个 None
        import time as _t
        t0 = _t.monotonic()
        got = [c async for c in p.iter_body(idle_timeout=5.0)]
        elapsed = _t.monotonic() - t0
        assert len(got) == m._BODY_QUEUE_MAX
        assert all(c == b"x" * 10 for c in got)
        # ★ 必须**立刻**收尾，而不是先干等一个 idle_timeout 再靠超时分支
        #   兜底。两者对用户都不卡死，但白等 5 秒会让每个满队列响应的
        #   收尾都慢一拍 —— 而且掩盖了"队列空 + 已结束"这类正常情况。
        assert elapsed < 1.0, f"收尾白等了 {elapsed:.2f}s（idle_timeout=5s）"

    _run(main())


def test_sweeper_survives_full_body_queue(tmp_path: Path) -> None:
    """清扫协程不能被 `QueueFull` 打死 —— 死了之后没有任何东西清理超时。

    `req_id` 泄漏的后果是 4096 个之后所有请求 429，**而隧道看起来还连着**。
    """
    async def main() -> None:
        import shared.mux as m
        sink, _ = _sender()
        mux = Mux(sink, role="probe")
        old = m.SWEEP_INTERVAL
        m.SWEEP_INTERVAL = 0.01
        task = asyncio.create_task(mux._sweep_loop())
        try:
            p1 = PendingRequest(1, timeout=0.0)
            mux._pending[1] = p1
            for _ in range(m._BODY_QUEUE_MAX):
                p1.push_body(b"x" * 10)
            await asyncio.sleep(0.08)
            assert not task.done(), (
                f"清扫协程死了：{task.exception()!r}")

            p2 = PendingRequest(2, timeout=0.0)
            mux._pending[2] = p2
            await asyncio.sleep(0.15)
            assert mux._pending.get(2) is None, "清扫协程还活着但不再清理超时请求"
        finally:
            m.SWEEP_INTERVAL = old
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):      # noqa: BLE001
                pass

    _run(main())


def test_close_survives_full_body_queue() -> None:
    """`close()` 必须能关掉连接，即使某个响应体队列是满的。"""
    async def main() -> None:
        import shared.mux as m
        sink, _ = _sender()
        mux = Mux(sink, role="probe")
        p = PendingRequest(1, timeout=None)
        mux._pending[1] = p
        for _ in range(m._BODY_QUEUE_MAX):
            p.push_body(b"x" * 10)

        await mux.close("probe")             # 原来这里抛 QueueFull
        assert mux.closed is True

    _run(main())


def test_slow_consumer_does_not_wedge_the_read_loop() -> None:
    """消费者不取数据时，读循环必须自己脱身，而不是永久堵住。

    堵住的读循环收不到 PING/CREDIT/CANCEL —— 那不只是这个请求慢，
    是整条隧道被判死。所以投递有上限，超时就放弃**这一个**请求。
    """
    async def main() -> None:
        import shared.mux as m
        sink, sent = _sender()
        mux = Mux(sink, role="probe")
        p = PendingRequest(1, timeout=None)
        mux._pending[1] = p
        for _ in range(m._BODY_QUEUE_MAX):
            p.push_body(b"x" * 10)          # 队列满，下一次 put 必然阻塞

        old = m._BODY_PUT_TIMEOUT
        m._BODY_PUT_TIMEOUT = 0.2
        try:
            from shared.protocol import Frame
            task = asyncio.create_task(
                mux.feed(Frame(m.FrameType.RES_BODY, 1, b"y" * 100)))
            await asyncio.sleep(0.8)
            assert task.done(), "读循环被一个满队列永久堵住了"
        finally:
            m._BODY_PUT_TIMEOUT = old
            if not task.done():
                task.cancel()

        # 该请求必须以可重试的错误结束，而不是无声消失
        head = p.head.result()
        assert head["status"] == 0
        assert head["error"]["code"] == "E5008"

    _run(main())
