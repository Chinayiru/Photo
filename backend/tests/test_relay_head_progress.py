"""上传的 `RES_HEAD` 必须按"无进展"计时，而不是按总时长。

## 这条测试在守什么

`cloud/relay/forward.py:_relay` 曾经这样等响应头：

    res = await pending.wait_head(timeout=HEAD_TIMEOUT)   # 30 s

对 GET 没问题 —— 电脑端处理完立刻回 `RES_HEAD`。但**上传的响应头要等整片
收完**：`backend/app/handlers/uploads.py:149` 用 `ctx.body_bytes()` 把整片
读进内存、落盘、fsync，然后才返回 201/206。于是"发 REQ_HEAD → 收 RES_HEAD"
之间的时长 ≈ 这一片的传输时长。

WAN 分片是 2 MiB（`backend/app/config.py:209`），云出口 1 Mbps ≈ 122 KiB/s，
光"云 → 电脑"就要约 17 秒；手机上行更慢时 2 MiB 轻松越过 30 秒。旧实现
把这个**明明还在流动**的请求判成 504 `E3009`，而它在 `backend/app/errors.py`
里是 `retryable=True` —— 客户端会把整片重传，弱网下越传越慢，重传还可能
对着已落盘的 offset 撞 416。

判据：**只要上行字节还在增加就续期**；连续 30 秒没有新字节才算卡死。
GET 没有请求体（`up.total` 恒为 0），行为与旧实现一字不差。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from starlette.requests import Request
from starlette.responses import Response

from cloud.relay import forward as fwd

DEV = "a" * 32


def _run(coro):
    return asyncio.run(coro)


class _Pending:
    """假 `PendingRequest`：第 `head_after` 次等待时给出响应头。

    `on_wait` 用来模拟"上行还在动"——每次等待前把计数器加一点，
    等价于 pump 又往隧道里写了若干字节。
    """

    def __init__(self, *, head_after: int | None, on_wait=None) -> None:
        self.calls = 0
        self.head_after = head_after
        self.on_wait = on_wait

    async def wait_head(self, timeout: float = 30.0) -> dict:
        self.calls += 1
        if self.on_wait is not None:
            self.on_wait()
        if self.head_after is not None and self.calls >= self.head_after:
            return {"status": 200, "headers": []}
        await asyncio.sleep(timeout)
        raise asyncio.TimeoutError


def _counter(total: int = 0) -> fwd._ByteCounter:
    up = fwd._ByteCounter()
    up.total = total
    return up


# ---------------------------------------------------------------- 单元判据

def test_head_that_arrives_immediately_is_untouched() -> None:
    up = _counter()
    p = _Pending(head_after=1)
    res = _run(fwd._await_head_with_progress(p, up, timeout=0.05, slice_=0.01))
    assert res["status"] == 200
    assert p.calls == 1


def test_stalled_upload_still_times_out() -> None:
    """一个字节都不动 = 真卡死，必须照旧报 504。"""
    up = _counter()
    p = _Pending(head_after=None)
    start = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        _run(fwd._await_head_with_progress(p, up, timeout=0.05, slice_=0.01))
    assert time.monotonic() - start >= 0.05


def test_progress_extends_the_wait_past_the_nominal_timeout() -> None:
    """慢但在动：总时长超过 30 s（这里是 0.05 s）也必须放行。"""
    up = _counter()
    p = _Pending(head_after=10, on_wait=lambda: setattr(up, "total", up.total + 4096))
    start = time.monotonic()
    res = _run(fwd._await_head_with_progress(p, up, timeout=0.05, slice_=0.01))
    assert res["status"] == 200
    # 10 次等待 × 0.01 s ≈ 0.1 s，已明显越过 0.05 s 的窗口 —— 说明是
    # "有进展就续期"，而不是把常量调大了。
    assert time.monotonic() - start > 0.05
    assert p.calls > 1


def test_progress_that_stops_is_still_a_stall() -> None:
    """先动后停：停了 0.05 s 之后仍然要超时，不能无限续。"""
    up = _counter()
    state = {"n": 0}

    def _bump() -> None:
        state["n"] += 1
        if state["n"] <= 3:
            up.total += 4096

    p = _Pending(head_after=None, on_wait=_bump)
    start = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        _run(fwd._await_head_with_progress(p, up, timeout=0.05, slice_=0.01))
    assert time.monotonic() - start >= 0.05


# ---------------------------------------------------------------- 接线判据

def _req(path: str, *, method: str = "GET") -> Request:
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "https", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "",
        "headers": [(b"host", b"pb.example.com")],
        "client": ("203.0.113.7", 51000), "server": ("pb.example.com", 443),
    })


class _Conn:
    device_id = DEV
    inflight = 0


class _Registry:
    def get(self, device_id: str):
        return _Conn() if device_id == DEV else None


def test_relay_wires_the_progress_aware_wait(monkeypatch) -> None:
    """`_relay` 必须用进度判据 —— 换回裸 `wait_head` 时这条会红。"""
    monkeypatch.setattr(fwd, "_head_timeout", lambda _request: 0.05)
    monkeypatch.setattr(fwd, "_HEAD_PROGRESS_SLICE", 0.01)

    # `up` 是 `_relay` 的局部变量，只有截住 `_ByteCounter` 才能往同一个
    # 计数器里加字节（等价于 pump 又写了一段）。
    made: list = []

    class _Tracked(fwd._ByteCounter):
        def __init__(self) -> None:
            super().__init__()
            made.append(self)

    monkeypatch.setattr(fwd, "_ByteCounter", _Tracked)

    p = _Pending(head_after=8,
                 on_wait=lambda: setattr(made[-1], "total", made[-1].total + 4096))

    async def _forward(*a, **kw):
        return p, None

    monkeypatch.setattr(fwd, "forward_request", _forward)
    monkeypatch.setattr(fwd.Forwarder, "_stream_response",
                        lambda self, *a, **kw: Response(b"{}", status_code=200))

    f = fwd.Forwarder(_Registry(), db=object())
    resp = _run(f._relay(_req(f"/d/{DEV}/api/uploads/x", method="PATCH"),
                         DEV, "rid-1", client_id=""))
    assert resp.status_code == 200
    assert made and made[-1].total > 0
    assert p.calls >= 8, "应该分段等待了多次；只等一次说明没用进度判据"


def test_relay_returns_E3009_when_nothing_moves(monkeypatch) -> None:
    monkeypatch.setattr(fwd, "_head_timeout", lambda _request: 0.05)
    monkeypatch.setattr(fwd, "_HEAD_PROGRESS_SLICE", 0.01)

    up = fwd._ByteCounter()
    p = _Pending(head_after=None)

    async def _forward(*a, **kw):
        return p, None

    monkeypatch.setattr(fwd, "forward_request", _forward)

    class _Conn2(_Conn):
        calls = 0

        async def send(self, frame) -> None:
            _Conn2.calls += 1

    class _Reg2:
        def get(self, device_id: str):
            return _Conn2()

    monkeypatch.setattr(fwd.Forwarder, "_stream_response",
                        lambda self, *a, **kw: Response(b"{}", status_code=200))

    f = fwd.Forwarder(_Reg2(), db=object())
    resp = _run(f._relay(_req(f"/d/{DEV}/api/uploads/x", method="PATCH"),
                         DEV, "rid-2", client_id=""))
    assert resp.status_code == 504
    assert json.loads(resp.body)["error"]["code"] == "E3009"

# ---------------------------------------------------------------- 分级预算

@pytest.mark.parametrize("path,seconds", [
    (f"/d/{DEV}/api/system/ping", 5.0),
    (f"/d/{DEV}/api/system/info", 10.0),
    (f"/d/{DEV}/api/items/12/thumb", 30.0),
    (f"/d/{DEV}/api/items/12/preview", 60.0),
    (f"/d/{DEV}/api/items/12/raw", 30.0),
    (f"/d/{DEV}/api/uploads/abc", 120.0),
    (f"/d/{DEV}/api/index/rescan", 10.0),
    (f"/d/{DEV}/api/whatever", 30.0),
])
def test_head_timeout_is_graded_by_path(path: str, seconds: float) -> None:
    """docs/04 §3.7 的分级表：上传 120 s、预览 60 s、ping 5 s……"""
    assert fwd._head_timeout(_req(path, method="PATCH")) == seconds
