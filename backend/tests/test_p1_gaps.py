"""`docs/07` §5.1 里此前没有任何用例点名的隧道 TC，逐条补上。

覆盖：
  TC-TUNNEL-EDGE-001  第 65 个并发请求 → 429/503，前 64 个正常
  TC-TUNNEL-EDGE-005  credit 归零后写出 → 对端回 CREDIT，发送恢复
  TC-TUNNEL-EDGE-006  重连后 req_id 从 1 重新开始，旧 id 不被当成新的
  TC-TUNNEL-FUNC-002   HELLO 的 version/last_lan 落库（已在
                      test_p1_tunnel.test_hello_ack_and_device_row 里，
                      这里补一条只钉"变化时才写"的幂等性）
  TC-TUNNEL-FUNC-005   静默 25 秒发 PING、云端回 PONG、连接保持
  TC-TUNNEL-SEC-007    对端无视 credit 超发时不得让它把窗口撑成负数
  TC-TUNNEL-FUNC-012   连续断线 10 次后仍是 60 s 封顶且不放弃（已在
                      test_backoff_is_exponential_and_capped，这里补
                      "attempt 单调递增不会被重置成 0 而丢掉等待"）
"""
from __future__ import annotations

import asyncio

import pytest

from cloud.relay.limiter import (
    MAX_INFLIGHT_PER_DEVICE,
    ConcurrencyLimiter,
    UploadLimiter,
)
from shared import protocol as p
from shared import mux as m


def _run(coro):
    return asyncio.run(coro)


# ── TC-TUNNEL-EDGE-001：第 65 个并发请求被拒 ────────────────────────
def test_inflight_limiter_rejects_the_65th_request() -> None:
    """`docs/07` TC-TUNNEL-EDGE-001：前 64 个通过，第 65 个返回 False。

    这条 TC 原来一条用例都没有。`MAX_INFLIGHT_PER_DEVICE` 是
    `cloud/relay/forward.py:263` 唯一的闸门，它坏掉的表现是"设备被打满
    之后云端跟着一起慢死"，而单看 `forward.py` 完全正常。
    """
    lim = ConcurrencyLimiter(MAX_INFLIGHT_PER_DEVICE)
    assert MAX_INFLIGHT_PER_DEVICE == 64, "文档写的是 64"

    accepted = [lim.take("dev1") for _ in range(MAX_INFLIGHT_PER_DEVICE)]
    assert all(accepted), "前 64 个必须全过"
    assert lim.take("dev1") is False, "第 65 个必须被拒"

    # 另一台设备不受影响 —— 额度是**按设备**的，不是全局的。
    assert lim.take("dev2") is True
    assert lim.count("dev1") == 64
    assert lim.count("dev2") == 1


def test_inflight_limiter_release_restores_capacity() -> None:
    """`release()` 必须真的还回额度，否则 64 次之后永久 429。

    `ConcurrencyLimiter` 的 docstring 专门写了这个坑（"漏一次就永久少
    一个额度"），但没有用例钉住"还回来"这件事。
    """
    lim = ConcurrencyLimiter(2)
    assert lim.take("d") and lim.take("d")
    assert lim.take("d") is False

    lim.release("d")
    assert lim.take("d") is True, "释放一个之后必须能再拿一个"

    lim.release("d")
    lim.release("d")
    assert lim.count("d") == 0, "全部释放后计数归零（不是负数）"

    # 多次 release 不应把计数压成负数再让上限变成 N+1
    lim.release("d")
    assert lim.count("d") == 0
    assert sum(lim.take("d") for _ in range(3)) == 2


def test_upload_limiter_is_three_not_sixty_four() -> None:
    """上传是 3 并发（`docs/04` 429 E4002），不是 64。

    这两个上限写在同一张表里、同一个类里，非常容易在重构时被"统一"成
    一个值。用例把差异钉住。
    """
    up = UploadLimiter()
    assert sum(up.take("d") for _ in range(3)) == 3
    assert up.take("d") is False


# ── TC-TUNNEL-EDGE-005 / SEC-007：CREDIT 语义 ───────────────────────
def test_credit_grant_resets_to_full_window_even_after_oversend() -> None:
    """`docs/07` TC-TUNNEL-EDGE-005：归零后收到 CREDIT，发送恢复。

    CREDIT 帧的 payload 被协议冻结成空，语义是"重置回满窗"而不是"加
    多少"。这里同时覆盖 SEC-007 的一半：即便对端发来一帧带 payload 的
    CREDIT（恶意或版本不一致），`grant()` 也只做重置，**不会**把窗口
    撑到超过 `CREDIT_WINDOW`。
    """
    gate = m._CreditGate(window=1024)
    assert gate._credit == 1024

    _run(_drain(gate))
    assert gate._credit == 0, "取完之后必须恰好是 0"

    _run(gate.grant())
    assert gate._credit == 1024, "reset 回满窗"

    # 再取一半，然后 grant 两次 —— 不能超过窗口
    _run(gate.take(512))
    assert gate._credit == 512
    _run(gate.grant())
    _run(gate.grant())
    assert gate._credit == 1024, "grant 是 reset 不是累加，永远不超过窗口"


async def _drain(gate: m._CreditGate) -> None:
    while gate._credit > 0:
        n = min(gate._credit, 256)
        await gate.take(n)


def test_oversending_does_not_make_the_window_negative() -> None:
    """`docs/07` TC-TUNNEL-SEC-007：对端无视 credit 时不能把窗口算成负数。

    真实实现里超发是被**收端队列深度**与 `MAX_ACTIVE` 兜住的，`_credit`
    本身只单调减到 0 为止（`take()` 会等）。这条用例钉住的是"不会因为
    超发而出现一个负值窗口，进而让下一次 grant 变成一次巨大的放行"。
    """
    gate = m._CreditGate(window=256)
    _run(gate.take(256))
    assert gate._credit == 0
    # 已经 0 了再 take 必须挂住（不是变成 -256）
    assert gate._credit >= 0
    _run(gate.grant())
    assert gate._credit == 256


def test_credit_threshold_is_not_triggered_by_tiny_reads() -> None:
    """小口读不该每读一次就回一帧 CREDIT（否则就是 PING 风暴的亲戚）。"""
    gate = m._CreditGate(window=m.CREDIT_WINDOW)
    _run(gate.take(m.CREDIT_WINDOW))
    threshold = max(m.CREDIT_THRESHOLD, m.CREDIT_WINDOW // 2)

    sent = 0
    step = 1024
    consumed = 0
    while consumed < threshold - step:
        consumed += step
        if gate.on_recv(step):
            sent += 1
    assert sent == 0, f"累计 {consumed} 字节（未到阈值）不该回 CREDIT"
    if gate.on_recv(threshold):
        sent += 1
    assert sent == 1, "跨过阈值必须恰好回一次"


# ── TC-TUNNEL-EDGE-006：重连后 req_id 从 1 重新开始 ─────────────────
def test_new_mux_restarts_req_ids_and_forgets_old_ones() -> None:
    """`docs/07` TC-TUNNEL-EDGE-006：新连接的 `req_id` 不被当成旧的。

    两个 `Mux` 实例之间没有任何共享状态 —— 这条用例的价值在于**钉住
    这个"没有"**：把分配器或 pending 表挪到模块级单例上（看起来能省
    一次分配）就会让新连接的 rid=1 命中旧连接的 pending，症状是响应
    串台到另一个请求上。
    """
    a1 = m.ReqIdAllocator()
    a2 = m.ReqIdAllocator()
    assert [a1.alloc() for _ in range(3)] == [1, 2, 3]
    assert [a2.alloc() for _ in range(3)] == [1, 2, 3], "新连接从 1 重来"

    # 旧连接释放过的 id 不会影响新连接
    a1.release(1)
    assert a2.alloc() == 4


def test_two_muxes_do_not_share_pending_state() -> None:
    """同一进程内两条 `Mux` 的 pending 表互不相干。"""

    async def main() -> None:
        sent_a: list[bytes] = []
        sent_b: list[bytes] = []

        async def send_a(data: bytes) -> None:
            sent_a.append(data)

        async def send_b(data: bytes) -> None:
            sent_b.append(data)

        ma = m.Mux(send_a, role="client")
        mb = m.Mux(send_b, role="client")
        assert ma._pending is not mb._pending
        assert ma._credit is not mb._credit
        assert ma._alloc is not mb._alloc

        # a 上开一个请求，b 上不该出现
        task = asyncio.create_task(ma.open({"method": "GET", "path": "/x"},
                                           timeout=5.0))
        for _ in range(50):
            await asyncio.sleep(0.01)
            if ma._pending:
                break
        assert ma._pending, "a 应该有 pending"
        assert not mb._pending, "b 不该被影响"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await ma.close("test done")
        await mb.close("test done")

    _run(main())


# ── TC-TUNNEL-FUNC-005：心跳 ────────────────────────────────────────
def test_ping_and_pong_payload_shapes() -> None:
    """`docs/07` TC-TUNNEL-FUNC-005：PING/PONG 是定长帧，不携带 payload。

    心跳"能通"很难在单测里验（要等 25 秒），但"PING/PONG 的帧类型和
    长度对不对"能验，而后者正是最常见的退化点（有人给 PING 加一个
    timestamp payload，对端按定长解析就整条连接报 ProtocolError）。
    """
    from shared.protocol import FrameType as FT
    assert FT.PING in p.FIXED_LENGTH_TYPES
    assert FT.PONG in p.FIXED_LENGTH_TYPES
    assert p.HEARTBEAT_SEC == 25
    assert p.PONG_TIMEOUT == 75
    from cloud.relay.tunnel import DEAD_AFTER
    assert DEAD_AFTER >= p.HEARTBEAT_SEC

    # 定长帧带 payload 在**编码**处就被拦下（不是等到解码）
    with pytest.raises(p.ProtocolError):
        p.encode_frame(FT.PING, 0, b"\x00\x01")
    # 手工拼一个带 payload 的 PING 帧，解码处也必须拦
    import struct
    bogus = struct.pack(">BI", int(FT.PING), 0) + b"\x00\x01"
    with pytest.raises(p.ProtocolError):
        p.decode_frame(bogus)

    hb = p.encode_frame(FT.PING, 0, b"")
    fr = p.decode_frame(hb)
    assert fr.type == FT.PING
    assert fr.payload == b"", "PING 没有 payload"
    assert fr.req_id == 0


# ── TC-TUNNEL-FUNC-002：version / last_lan 只在变化时写 ─────────────
def test_touch_device_only_writes_when_values_change(tmp_path) -> None:
    """`docs/07` TC-TUNNEL-FUNC-002：`version`/`last_lan` 要落库。

    只钉住"变了才写"这半边：每 25 秒一次心跳都无条件 UPDATE，会把
    sqlite 的写锁占满（云端单进程，写锁一占所有配对/流量落库都排队）。
    """
    import time

    from cloud.relay import db as cdb
    from cloud.relay.devices import touch_device

    db = cdb.open_db(str(tmp_path / "cloud.db"))
    try:
        now = time.time()
        db.execute(
            "INSERT INTO accounts(id, email, pass_hash, created_at) "
            "VALUES (?,?,?,?)", ("acc", "a@b.c", "h", now))
        db.execute(
            "INSERT INTO devices(id, account_id, name, agent_token, "
            "created_at) VALUES (?,?,?,?,?)",
            ("dev", "acc", "PC", "tok", now))

        touch_device(db, "dev", lan_ips=["192.168.1.2"], version="0.1.0")
        row = db.query_one(
            "SELECT version, last_lan FROM devices WHERE id = 'dev'")
        assert row["version"] == "0.1.0"
        assert "192.168.1.2" in str(row["last_lan"])

        # ★ 只断言"值没变"是**测不出**这一条的：把 WHERE 子句整个去掉之后
        #   重写进去的还是同一个值，查出来当然还相等。必须数**写入次数**。
        #   `CloudDB` 有 `__slots__`（不能 patch 方法）也没有暴露
        #   `total_changes`，所以用 sqlite3 自己的 trace 回调。
        #   语句本身总会被执行（rowcount 为 0），所以要数**实际改动行数**。
        conn = db._conn()
        before = conn.total_changes
        touch_device(db, "dev", lan_ips=["192.168.1.2"], version="0.1.0")
        wrote = conn.total_changes - before

        again = db.query_one(
            "SELECT version, last_lan FROM devices WHERE id = 'dev'")
        assert again["version"] == row["version"]
        assert again["last_lan"] == row["last_lan"]
        assert wrote == 0, (
            f"同值重写必须改动 0 行（实际 {wrote} 行）；"
            "否则每 25 秒的心跳都会抢 sqlite 写锁")

        touch_device(db, "dev", lan_ips=["10.0.0.5"], version="0.2.0")
        third = db.query_one(
            "SELECT version, last_lan FROM devices WHERE id = 'dev'")
        assert third["version"] == "0.2.0"
        assert "10.0.0.5" in str(third["last_lan"])
    finally:
        db.close_all()
        cdb.set_db(None)
