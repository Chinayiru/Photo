"""事件总线测试。

要点：队列满时丢最旧、订阅者清理、跨线程发布、SSE 帧格式。
这些都是"只在真实使用中才会暴露"的问题：单测里订阅者只开一个、
事件只发几条、也从不跨线程，永远看不到。
"""
from __future__ import annotations

import asyncio
import json
import threading

import pytest

from backend.app.events import (
    QUEUE_MAX,
    EventBus,
    format_sse,
    stream_events,
)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# 帧格式
# --------------------------------------------------------------------------


def test_format_sse_shape():
    raw = format_sse({"type": "ping", "ts": 1.5})
    text = raw.decode("utf-8")
    assert text.startswith("event: ping\n")
    assert text.endswith("\n\n")
    body = text.split("data: ", 1)[1].strip()
    assert json.loads(body) == {"type": "ping", "ts": 1.5}


def test_format_sse_keeps_chinese_readable():
    """中文文件名不该在线上变成 \\uXXXX（流量是手机的真实成本）。"""
    raw = format_sse({"type": "media_changed", "name": "敦煌.jpg"})
    assert "敦煌".encode("utf-8") in raw
    assert b"\\u6566" not in raw


def test_format_sse_explicit_event_name():
    raw = format_sse({"type": "scan_done"}, event_name="done")
    assert raw.startswith(b"event: done\n")


# --------------------------------------------------------------------------
# 发布订阅
# --------------------------------------------------------------------------


def test_publish_delivers_to_all():
    bus = EventBus()
    a, b = bus.subscribe(), bus.subscribe()
    assert bus.publish({"type": "ping"}) == 2
    assert a.queue.qsize() == 1
    assert b.queue.qsize() == 1


def test_unsubscribe_stops_delivery():
    bus = EventBus()
    a = bus.subscribe()
    bus.unsubscribe(a)
    assert bus.publish({"type": "ping"}) == 0


def test_queue_full_drops_oldest_not_newest():
    """慢客户端（手机切后台、JS 被冻结）不能把内存吃光。

    丢最旧的而不是拒绝新的：进度事件里最新的值才是用户想看的，
    "1200/4915" 比 "1100/4915" 更有用。
    """
    bus = EventBus()
    sub = bus.subscribe()
    for i in range(QUEUE_MAX + 10):
        bus.publish({"type": "scan_progress", "seen": i})
    assert sub.queue.qsize() == QUEUE_MAX
    assert sub.dropped == 10
    # 队首是最早保留下来的那批，队尾一定是最新值
    last = None
    while not sub.queue.empty():
        last = sub.queue.get_nowait()
    assert last["seen"] == QUEUE_MAX + 9


def test_publish_never_raises():
    """发布绝不能因为某个订阅者异常而中断调用方（上传/索引）的流程。"""
    bus = EventBus()

    class Boom:
        def put_nowait(self, item):
            raise RuntimeError("boom")

    bus._subs.add(Boom())          # type: ignore[arg-type]
    assert bus.publish({"type": "ping"}) == 0


def test_publish_threadsafe_from_other_thread():
    """executor 与 watchdog 回调都在别的线程里 —— 直接 put 不会唤醒协程。"""
    async def main():
        bus = EventBus()
        bus.bind_loop(asyncio.get_running_loop())
        sub = bus.subscribe()

        def worker():
            bus.publish_threadsafe({"type": "media_changed", "id": 7})

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        item = await asyncio.wait_for(sub.queue.get(), timeout=2.0)
        return item

    assert _run(main())["id"] == 7


def test_publish_threadsafe_without_loop_is_noop():
    bus = EventBus()
    bus.publish_threadsafe({"type": "ping"})       # 不该抛


# --------------------------------------------------------------------------
# SSE 流
# --------------------------------------------------------------------------


def test_stream_events_sends_hello_first():
    async def main():
        bus = EventBus()
        gen = stream_events(bus, heartbeat=5.0, hello={"device_id": "d1"})
        first = await gen.__anext__()
        await gen.aclose()
        return first

    first = _run(main())
    text = first.decode("utf-8")
    assert text.startswith("event: hello\n")
    assert '"device_id":"d1"' in text


def test_stream_events_unsubscribes_on_close():
    """客户端断开必须清理订阅者，否则每开一次设置页泄漏一个。"""
    async def main():
        bus = EventBus()
        gen = stream_events(bus, heartbeat=5.0)
        await gen.__anext__()
        assert bus.subscribers == 1
        await gen.aclose()
        return bus.subscribers

    assert _run(main()) == 0


def test_stream_events_emits_heartbeat():
    """没有心跳时，手机切网络后进度条会永远停在原处且无任何错误。"""
    async def main():
        bus = EventBus()
        gen = stream_events(bus, heartbeat=0.05)
        await gen.__anext__()                      # hello
        second = await gen.__anext__()             # 心跳
        await gen.aclose()
        return second

    text = _run(main()).decode("utf-8")
    assert text.startswith("event: ping\n")


def test_stream_events_forwards_published_event():
    async def main():
        bus = EventBus()
        gen = stream_events(bus, heartbeat=5.0)
        await gen.__anext__()
        bus.publish({"type": "scan_progress", "seen": 42})
        frame = await gen.__anext__()
        await gen.aclose()
        return frame

    text = _run(main()).decode("utf-8")
    assert text.startswith("event: scan_progress\n")
    assert '"seen":42' in text


def test_close_wakes_subscribers():
    async def main():
        bus = EventBus()
        gen = stream_events(bus, heartbeat=30.0)
        await gen.__anext__()
        bus.close()
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    _run(main())


# --------------------------------------------------------------------------
# 索引器 / 缩略图引擎接入
# --------------------------------------------------------------------------


def test_indexer_emits_scan_events(tmp_path):
    """索引器在 scan_start / scan_progress / scan_done 都要发事件。

    注意：`scan_progress` 只在**写完一批有变化的行**之后才发。全部
    文件都未变化的第二轮扫描不会发进度事件 —— 这符合预期，因为
    没有进度可言。测试必须用"首次扫描"或造出变化。
    """
    from PIL import Image

    from backend.app.config import Config
    from backend.app.db import Database
    from backend.app.indexer import Indexer

    lib = tmp_path / "lib"
    lib.mkdir()
    for i in range(5):
        Image.new("RGB", (40, 30), (10 * i, 40, 90)).save(lib / f"{i}.jpg")

    cfg = Config(
        {"roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}]},
        path=tmp_path / "config.json",
        data_dir=tmp_path / "data",
    )
    db = Database(tmp_path / "t.db")
    db.init()
    bus = EventBus()
    sub = bus.subscribe()
    idx = Indexer(cfg, db, batch_size=2, bus=bus)

    _run(idx.scan_all())

    types = []
    while not sub.queue.empty():
        types.append(sub.queue.get_nowait()["type"])

    assert types[0] == "scan_start"
    assert "scan_progress" in types
    assert types[-1] == "scan_done"
    db.close()


def test_indexer_without_bus_does_not_fail(tmp_path):
    """bus=None 时索引器必须完全正常工作（测试与脚本都这么用）。"""
    from PIL import Image

    from backend.app.config import Config
    from backend.app.db import Database
    from backend.app.indexer import Indexer

    lib = tmp_path / "lib"
    lib.mkdir()
    Image.new("RGB", (10, 10)).save(lib / "a.jpg")
    cfg = Config(
        {"roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}]},
        path=tmp_path / "config.json",
        data_dir=tmp_path / "data",
    )
    db = Database(tmp_path / "t.db")
    db.init()
    stats = _run(Indexer(cfg, db).scan_all())
    assert stats[0].added == 1
    db.close()


# --------------------------------------------------------------------------
# AC-3.13：上传完成必须推 upload_done
#
# ★ 这一组的理由：`upload_done` 从 L1 起就在 `EVENT_TYPES` 白名单里，
#   但全仓**零处** `.publish(` 发过它 —— 白名单里一行死字符串。
#   只断言"白名单里有这个类型"是测不出这个缺陷的（`test_events.py`
#   原来就有这样的用例，全绿）。必须走完整 handler 再验帧。
# --------------------------------------------------------------------------

def _upload_state(tmp_path, db, bus):
    """造一个带 bus 的 AppState，并把 uploads 路由挂起来。"""
    import asyncio as _aio

    from backend.app import uploads as _up
    from backend.app.config import Config
    from backend.app.core import Ctx, Router, json_reply
    from backend.app.handlers import uploads as h_up
    from backend.app.state import AppState

    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = Config({
        "app": {"mode": "readwrite"},
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}],
        "auth": {"require_pairing": False},
        "upload": {"subdir": "", "chunk_mb_lan": 1, "max_file_mb": 8,
                   "session_ttl_hours": 24, "dedup": True},
    }, path=tmp_path / "data" / "config.json", data_dir=tmp_path / "data")
    st = AppState(cfg=cfg, db=db, bus=bus)

    r = Router()
    r.add("POST", "/api/uploads/init", h_up.init_upload)
    r.add("PATCH", "/api/uploads/{upload_id}", h_up.patch_upload)
    r.add("POST", "/api/uploads/{upload_id}/complete", h_up.complete_upload)
    return st, r, lib, _up


def _png_bytes(w: int = 8, h: int = 8) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (12, 200, 90)).save(buf, format="PNG")
    return buf.getvalue()


def test_upload_done_is_published(tmp_path):
    """★ 完整走一遍 init → PATCH → complete，总线里必须出现 upload_done。

    这是 AC-3.13 的唯一有效断言方式：直接在 handler 里搜 `.publish(`
    证明不了它真的被调用（可能在某个提前 return 后面）。
    """
    import asyncio as _aio
    import json as _json

    from backend.app.core import Ctx
    from backend.app.db import Database

    bus = EventBus()
    sub = bus.subscribe()
    db = Database(tmp_path / "t.db")
    db.init()
    st, router, lib, _ = _upload_state(tmp_path, db, bus)

    payload = _png_bytes()

    def _make_ctx(method, path, *, body=b"", headers=None):
        async def _gen():
            if body:
                yield body

        return Ctx(method=method, path=path, headers=headers or {},
                   params={"upload_id": uid} if uid else {},
                   client_ip="192.168.0.50", app=st, body=_gen())

    async def _body(reply):
        if reply.body is None:
            return b""
        if isinstance(reply.body, (bytes, bytearray)):
            return bytes(reply.body)
        out = []
        async for c in reply.body:
            if c:
                out.append(c)
        return b"".join(out)

    uid = ""

    async def _flow():
        nonlocal uid
        r = await router.dispatch(_make_ctx(
            "POST", "/api/uploads/init",
            body=_json.dumps({
                "name": "新照片.png", "size": len(payload),
                "root_id": "library",
            }).encode()))
        data = _json.loads((await _body(r)).decode())
        uid = data["upload_id"]
        assert data.get("exists") is not True

        r = await router.dispatch(_make_ctx(
            "PATCH", f"/api/uploads/{uid}", body=payload,
            headers={"Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}"}))
        assert r.status == 206

        r = await router.dispatch(_make_ctx(
            "POST", f"/api/uploads/{uid}/complete", body=b""))
        assert r.status in (200, 201)
        return r

    asyncio.run(_flow())

    types = []
    while not sub.queue.empty():
        types.append(sub.queue.get_nowait())
    done = [e for e in types if e["type"] == "upload_done"]
    assert done, f"总线上没有 upload_done：{[t['type'] for t in types]}"
    assert done[0]["upload_id"] == uid
    db.close()


def test_upload_done_bus_absent_does_not_fail(tmp_path):
    """`st.bus is None` 时上传必须照常成功（脚本与单测都这么装配）。"""
    import asyncio as _aio
    import json as _json

    from backend.app.core import Ctx
    from backend.app.db import Database

    db = Database(tmp_path / "t.db")
    db.init()
    st, router, lib, _ = _upload_state(tmp_path, db, None)
    payload = _png_bytes()

    async def _body(reply):
        if reply.body is None:
            return b""
        if isinstance(reply.body, (bytes, bytearray)):
            return bytes(reply.body)
        out = []
        async for c in reply.body:
            if c:
                out.append(c)
        return b"".join(out)

    state = {"uid": ""}

    async def _flow():
        r = await router.dispatch(Ctx(
            method="POST", path="/api/uploads/init", headers={}, params={},
            client_ip="192.168.0.50", app=st,
            body=_gen(_json.dumps({"name": "b.png", "size": len(payload),
                                   "root_id": "library"}).encode())))
        state["uid"] = _json.loads((await _body(r)).decode())["upload_id"]
        uid = state["uid"]
        r = await router.dispatch(Ctx(
            method="PATCH", path=f"/api/uploads/{uid}", params={"upload_id": uid},
            headers={"Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}"},
            client_ip="192.168.0.50", app=st, body=_gen(payload)))
        assert r.status == 206
        r = await router.dispatch(Ctx(
            method="POST", path=f"/api/uploads/{uid}/complete",
            params={"upload_id": uid}, headers={},
            client_ip="192.168.0.50", app=st, body=_gen(b"")))
        assert r.status in (200, 201)

    asyncio.run(_flow())
    db.close()


def _gen(data: bytes):
    async def _it():
        if data:
            yield data
    return _it()

def test_subscriber_cap_reports_e4006_before_streaming():
    """订阅数满时必须**在开流之前**报 429，而不是让第 33 条连接把线程耗光。

    真实故障场景有三种：手机浏览器切后台重连、用户开了很多标签页、有人
    拿脚本刷 `/api/events`。前两种要给出可读的原因，第三种必须挡住。
    """
    from backend.app.errors import AppError
    from backend.app.events import MAX_SUBSCRIBERS, ensure_capacity

    bus = EventBus()
    subs = [bus.subscribe() for _ in range(MAX_SUBSCRIBERS)]

    with pytest.raises(AppError) as ei:
        ensure_capacity(bus)
    assert ei.value.spec.code == "E4006"

    # 腾出一个名额就应当立刻恢复可用
    bus.unsubscribe(subs.pop())
    ensure_capacity(bus)
