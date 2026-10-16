r"""隧道帧协议测试（`shared/protocol.py`）。

## 为什么这个文件必须存在

`shared/protocol.py` 的模块 docstring 写着「帧编解码在此定义完毕并已被
单元测试覆盖」—— 而在本文件之前，整个仓库**没有任何一处** import 它
（`grep -rn "FrameParser\|encode_frame\|decode_frame\|pack_stream"` 只命中
它自己）。

这不是"少了几条测试"的问题。一期是局域网优先，协议层在运行时不参与，
所以它的正确性**没有第二条防线**：一个把 `REQ_END` 的 payload 当成
非空处理、或者把 `flags` 位算错的 bug，在一期跑一整年都不会有任何症状，
等二期接上云 Relay 才炸 —— 而那时问题会表现为"某些请求永远挂住"或
"大文件传到 8 MB 就卡死"，排查成本极高。

## 覆盖的三类东西

1. **常量与不变式** —— 帧类型数值一旦发布不得更改（协议兼容性），
   以及窗口/超时之间的相对关系。改错一个数字不会有任何编译错误。
2. **编解码往返** —— 每一种帧类型的 encode→decode 恒等。
3. **流式解析器的粘包/半包** —— 这是降级模式（SSE+POST）的唯一分帧
   手段，也是最容易写错的地方（O(n²) 切片、长度前缀不可信时的内存
   耗尽、半包状态残留）。
"""
from __future__ import annotations

import json
import os
import struct

import pytest

from shared.protocol import (
    CONN_CREDIT_WINDOW,
    CREDIT_DEADLOCK_TIMEOUT,
    CREDIT_THRESHOLD,
    CREDIT_WINDOW,
    CONN_DEADLOCK_TIMEOUT,
    FLAG_ENC,
    FLAG_RESERVED,
    HEADER_SIZE,
    HEARTBEAT_SEC,
    MAX_FRAME,
    PONG_TIMEOUT,
    REQ_ID_MASK,
    Frame,
    FrameParser,
    FrameType,
    ProtocolError,
    Transport,
    decode_frame,
    encode_frame,
    pack_req_id,
    pack_stream,
    unpack_req_id,
)

# --------------------------------------------------------------------------
# 1. 常量与协议不变式
# --------------------------------------------------------------------------


def test_frame_type_values_are_frozen() -> None:
    """★ 帧类型数值一旦发布不得更改。

    这是**线协议**：云端与电脑端是两个独立部署的进程，用户可能只升级
    一边。数值改了而另一边没改，表现为"所有请求都返回 unknown frame
    type"或更糟 —— 帧被当成另一种类型静默处理。

    所以这里把每个数值逐字写死。若将来真要改，必须同时改这里，改动
    会在 code review 里显式可见，而不是被一次"重排序"顺手带过。
    """
    assert FrameType.REQ_HEAD == 0x01
    assert FrameType.REQ_BODY == 0x02
    assert FrameType.REQ_END == 0x03
    assert FrameType.RES_HEAD == 0x04
    assert FrameType.RES_BODY == 0x05
    assert FrameType.RES_END == 0x06
    assert FrameType.ERROR == 0x07
    assert FrameType.CANCEL == 0x08
    assert FrameType.PING == 0x09
    assert FrameType.PONG == 0x0A
    assert FrameType.CREDIT == 0x0B
    assert FrameType.NOTIFY == 0x0C
    assert FrameType.HELLO == 0x0D
    assert FrameType.HELLO_ACK == 0x0E


def test_frame_type_values_are_unique_and_contiguous() -> None:
    """数值必须互不相同；且 0x01..0x0E 无空洞（便于日志按数组索引）。"""
    values = [int(t) for t in FrameType]
    assert len(set(values)) == len(values), "有重复的帧类型数值"
    assert values == list(range(1, len(values) + 1))


def test_fixed_length_types_are_exactly_the_empty_ones() -> None:
    """定长帧集合必须与"payload 恒为空"的语义完全一致。

    多一个：合法帧被拒（连接断掉）；少一个：非空 payload 被静默丢弃，
    对端永远等不到它期待的状态转移。
    """
    from shared.protocol import FIXED_LENGTH_TYPES

    assert FIXED_LENGTH_TYPES == {
        FrameType.REQ_END,
        FrameType.RES_END,
        FrameType.CANCEL,
        FrameType.PING,
        FrameType.PONG,
        FrameType.CREDIT,
    }
    # 带 JSON 体的帧绝不能进这个集合
    for t in (FrameType.REQ_HEAD, FrameType.RES_HEAD, FrameType.ERROR,
              FrameType.NOTIFY, FrameType.HELLO, FrameType.HELLO_ACK):
        assert t not in FIXED_LENGTH_TYPES


def test_priority_types_are_a_subset_of_all_types() -> None:
    """优先级帧必须是已知类型，且都应该是定长（要能插队立刻处理）。"""
    from shared.protocol import FIXED_LENGTH_TYPES, PRIORITY_TYPES

    assert PRIORITY_TYPES <= set(FrameType)
    assert PRIORITY_TYPES <= FIXED_LENGTH_TYPES, (
        "优先级帧带 payload 的话，插队处理就得先读缓冲，失去插队的意义")
    # 心跳与信用回补必须在里面：大文件传输会饿死它们
    assert FrameType.PING in PRIORITY_TYPES
    assert FrameType.PONG in PRIORITY_TYPES
    assert FrameType.CREDIT in PRIORITY_TYPES


def test_header_size_matches_struct() -> None:
    """HEADER_SIZE 必须与实际的 pack 格式一致（1 字节 type + 4 字节 req_id）。"""
    assert HEADER_SIZE == 5
    assert struct.calcsize(">BI") == HEADER_SIZE


def test_credit_threshold_is_smaller_than_window() -> None:
    """★ 回补阈值必须小于窗口。

    反过来的话，发送方在收到第一次回补之前就把窗口耗尽了 —— 而接收方
    的规则是"消费满 THRESHOLD 才回补"，于是双方都在等对方，形成死锁。
    这个错误在单元层面完全静默，只在真跑大文件时表现为"传到某个字节
    数就永久挂住"。
    """
    assert 0 < CREDIT_THRESHOLD < CREDIT_WINDOW
    assert CREDIT_THRESHOLD < CONN_CREDIT_WINDOW
    # 回补不该太频繁：每次回补都是一帧，阈值太小会让帧数爆炸
    assert CREDIT_THRESHOLD >= 64 * 1024


def test_pong_timeout_is_generously_larger_than_heartbeat() -> None:
    """★ PONG 超时必须明显大于心跳间隔。

    若只大一点点，一次正常的网络抖动（或手机切基站）就会判定连接已死
    并触发重连 —— 而重连期间所有在途请求都失败。设计取 3 倍。
    """
    assert PONG_TIMEOUT >= HEARTBEAT_SEC * 3


def test_deadlock_timeouts_are_ordered() -> None:
    """请求级死锁超时应小于连接级（先回收单个请求，别急着断整条连接）。"""
    assert 0 < CREDIT_DEADLOCK_TIMEOUT < CONN_DEADLOCK_TIMEOUT


def test_max_frame_is_the_documented_value() -> None:
    """★ max_frame 是 65536，**不是**"一个分片的大小"。

    设计文档初稿在这一点上自相矛盾：HELLO_ACK.max_frame = 65536（64 KB），
    而上传分片是 8 MB（局域网）/ 2 MB（云端）。一个 2 MB 的分片装不进
    一个 64 KB 的帧。

    docs/04-接口设计文档.md:387 给出的裁决是：**max_frame 约束的是单个
    隧道帧，不是单个 HTTP 分片** —— 一个 8 MB 的请求体分片在隧道内被
    切成一串 REQ_BODY 帧，每帧 payload ≤ 65530 字节，接收方按 req_id
    顺序拼接还原。`Content-Range` 描述的是 HTTP 层边界，帧边界与之无关。

    所以这里钉住的是"65536 这个值本身"以及它与 HEADER_SIZE 的关系，
    **不是**"能装下一个分片"。我第一版按后者写，跑出来才想起这条裁决。
    """
    assert MAX_FRAME == 65536, "max_frame 是线协议常量，改动要同步两端"
    assert MAX_FRAME > HEADER_SIZE
    # 一个 8 MB 分片需要的帧数（供参考，也防止有人把 MAX_FRAME 调成 0）
    assert (8 * 1024 * 1024) // (MAX_FRAME - HEADER_SIZE) == 128


# --------------------------------------------------------------------------
# 2. req_id / flags 位打包
# --------------------------------------------------------------------------


def test_req_id_roundtrip_without_flags() -> None:
    for rid in (0, 1, 2, 12345, REQ_ID_MASK):
        raw = pack_req_id(rid)
        assert unpack_req_id(raw) == (rid, 0)


def test_req_id_roundtrip_with_flags() -> None:
    """★ flags 与 req_id 必须能同时还原 —— 位不能互相污染。

    一期 flags 恒 0，所以这里的 bug 要等二期开 E2E 加密才暴露，
    而那时症状是"加密开关打开后 req_id 全部错乱"。
    """
    for rid in (0, 7, REQ_ID_MASK):
        for flags in (0, FLAG_ENC, FLAG_RESERVED, FLAG_ENC | FLAG_RESERVED):
            raw = pack_req_id(rid, flags)
            got_rid, got_flags = unpack_req_id(raw)
            assert got_rid == rid, f"rid 被 flags 污染：{got_rid} != {rid}"
            assert got_flags == flags, f"flags 丢失：{got_flags:#x} != {flags:#x}"


def test_req_id_and_flags_occupy_disjoint_bits() -> None:
    """req_id 用低 30 位、flags 用高 2 位，且打包结果必须落在 uint32 内。"""
    raw = pack_req_id(REQ_ID_MASK, FLAG_ENC | FLAG_RESERVED)
    assert raw == 0xFFFFFFFF
    assert 0 <= raw <= 0xFFFFFFFF
    # 低位与高位确实不重叠
    assert REQ_ID_MASK & (FLAG_ENC | FLAG_RESERVED) == 0


def test_pack_req_id_rejects_out_of_range() -> None:
    for bad in (-1, REQ_ID_MASK + 1, 1 << 31, 1 << 40):
        with pytest.raises(ProtocolError):
            pack_req_id(bad)


def test_pack_req_id_rejects_unknown_flags() -> None:
    """★ 未知 flags 必须报错，不能静默丢弃。

    静默丢弃意味着"二期加了新 flag，但一期实现把它抹掉后继续跑" ——
    对端以为该帧已加密，这边按明文处理，要么解析失败要么更糟。
    """
    for bad in (0x20000000, 0x00000001, 0x0000FFFF):
        with pytest.raises(ProtocolError):
            pack_req_id(0, bad)


# --------------------------------------------------------------------------
# 3. 编解码往返
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ftype", list(FrameType))
def test_every_frame_type_roundtrips(ftype: FrameType) -> None:
    """★ 每一种帧类型 encode→decode 必须恒等。

    参数化而不是循环，是为了让失败信息直接指出是哪一种帧坏了。
    """
    payload = b"" if ftype in __import__(
        "shared.protocol", fromlist=["FIXED_LENGTH_TYPES"]
    ).FIXED_LENGTH_TYPES else b"hello"
    raw = encode_frame(ftype, 42, payload)
    f = decode_frame(raw)
    assert f.type is ftype
    assert f.req_id == 42
    assert f.payload == payload
    assert f.flags == 0


def test_encode_accepts_bytes_str_and_dict() -> None:
    """三种 payload 形态都要能编码，且 dict 用紧凑分隔符。"""
    assert decode_frame(encode_frame(FrameType.REQ_BODY, 1, b"\x00\xff")).payload == b"\x00\xff"
    assert decode_frame(encode_frame(FrameType.REQ_HEAD, 1, "abc")).payload == b"abc"

    raw = encode_frame(FrameType.REQ_HEAD, 1, {"a": 1, "b": [2, 3]})
    body = raw[HEADER_SIZE:]
    assert b" " not in body, "JSON 应使用紧凑分隔符（省 8%~12% 字节）"
    assert json.loads(body.decode("utf-8")) == {"a": 1, "b": [2, 3]}


def test_encode_none_payload_is_empty() -> None:
    assert decode_frame(encode_frame(FrameType.REQ_HEAD, 1, None)).payload == b""


def test_encode_rejects_unsupported_payload_type() -> None:
    with pytest.raises(TypeError):
        encode_frame(FrameType.REQ_BODY, 1, 12345)  # type: ignore[arg-type]


def test_encode_rejects_payload_on_fixed_length_frame() -> None:
    """★ 定长帧带 payload 必须在**发送侧**就拒绝。

    若只靠接收侧检查，这个帧会先被发到网上 —— 而对端收到后关连接，
    表现是"某次操作之后连接就断了"，且本地没有任何日志指向原因。
    """
    for t in (FrameType.PING, FrameType.PONG, FrameType.CANCEL,
              FrameType.REQ_END, FrameType.RES_END, FrameType.CREDIT):
        with pytest.raises(ProtocolError) as ei:
            encode_frame(t, 1, b"x")
        assert t.name in str(ei.value)


def test_encode_rejects_oversized_payload() -> None:
    """★ 超限 payload 必须报错并提示调用方分片。

    静默发送的话，对端 FrameParser 会因 declared length 超限而毒化整个
    连接 —— 一个本可局部处理的错误变成断线。
    """
    with pytest.raises(ProtocolError) as ei:
        encode_frame(FrameType.REQ_BODY, 1, b"x" * (MAX_FRAME + 1))
    assert "max_frame" in str(ei.value).lower()


def test_encode_accepts_payload_exactly_at_limit() -> None:
    """边界：正好等于 MAX_FRAME 必须放行（不能把合法帧拒掉）。"""
    raw = encode_frame(FrameType.REQ_BODY, 1, b"x" * MAX_FRAME)
    assert decode_frame(raw).payload == b"x" * MAX_FRAME


def test_encode_preserves_flags() -> None:
    raw = encode_frame(FrameType.REQ_HEAD, 5, b"{}", flags=FLAG_ENC)
    f = decode_frame(raw)
    assert f.flags == FLAG_ENC
    assert f.req_id == 5
    assert f.enc is True


def test_frame_enc_is_false_by_default() -> None:
    assert Frame(FrameType.REQ_HEAD, 1).enc is False


# --------------------------------------------------------------------------
# 4. decode 的拒绝路径
# --------------------------------------------------------------------------


def test_decode_rejects_short_data() -> None:
    for n in range(HEADER_SIZE):
        with pytest.raises(ProtocolError) as ei:
            decode_frame(b"\x01" * n)
        assert "too short" in str(ei.value)


def test_decode_rejects_unknown_frame_type() -> None:
    """★ 未知类型必须报错，绝不能"跳过"或"当空帧处理"。

    线协议里多出来的类型只可能来自两种情况：对端版本更新（本端该
    提示升级）或链路被篡改。两种都不该被静默忽略。
    """
    for bad in (0x00, 0x0F, 0x10, 0x7F, 0xFF):
        raw = struct.pack(">BI", bad, 1)
        with pytest.raises(ProtocolError) as ei:
            decode_frame(raw)
        assert "unknown frame type" in str(ei.value).lower()


def test_decode_rejects_payload_on_fixed_length_frame() -> None:
    raw = struct.pack(">BI", int(FrameType.PING), 1) + b"oops"
    with pytest.raises(ProtocolError):
        decode_frame(raw)


def test_decode_strips_flags_from_req_id() -> None:
    """★ 解码后 req_id 必须是剥离 flags 的 30 位值。

    没剥离的话，二期开加密后所有 req_id 都带着 bit31，多路复用器会
    按完全不同的 key 找请求 —— 每个请求都"找不到对应响应"而超时。
    """
    raw = struct.pack(">BI", int(FrameType.REQ_HEAD), pack_req_id(9, FLAG_ENC)) + b"{}"
    f = decode_frame(raw)
    assert f.req_id == 9
    assert f.flags == FLAG_ENC


def test_decode_reserved_flag_is_read_but_ignored() -> None:
    """bit30 是保留位：读出来放在 flags 里，但不影响任何行为。"""
    raw = struct.pack(">BI", int(FrameType.REQ_HEAD), pack_req_id(3, FLAG_RESERVED)) + b"{}"
    f = decode_frame(raw)
    assert f.flags == FLAG_RESERVED
    assert f.enc is False, "保留位不该被当成加密标志"


# --------------------------------------------------------------------------
# 5. Frame.json
# --------------------------------------------------------------------------


def test_json_parses_object_payload() -> None:
    f = Frame(FrameType.REQ_HEAD, 1, b'{"method":"GET","path":"/x"}')
    assert f.json == {"method": "GET", "path": "/x"}


def test_json_returns_empty_dict_for_empty_payload() -> None:
    """空 payload 返回 {} 而不是报错 —— 定长帧也会被读 .json。"""
    assert Frame(FrameType.PING, 1).json == {}


def test_json_rejects_non_object() -> None:
    """★ 顶层必须是对象。

    允许数组的话，调用方 `f.json.get("status")` 会在数组上抛
    AttributeError，被 dispatch 的兜底 except 吞成 500 —— 而真正的原因
    （对端发了个数组）在日志里完全看不出来。
    """
    for body in (b"[1,2]", b'"str"', b"123", b"null", b"true"):
        with pytest.raises(ProtocolError) as ei:
            _ = Frame(FrameType.REQ_HEAD, 1, body).json
        assert "must be an object" in str(ei.value)


def test_json_rejects_malformed_json() -> None:
    with pytest.raises(ProtocolError) as ei:
        _ = Frame(FrameType.REQ_HEAD, 1, b"{not json").json
    assert "bad json" in str(ei.value).lower()


def test_json_rejects_invalid_utf8() -> None:
    """★ 非 UTF-8 字节必须变成 ProtocolError 而不是 UnicodeDecodeError。

    前者会被隧道层当成"对端实现有 bug → 关连接"处理，后者会一路冒泡
    成未捕获异常，在 asyncio 里表现为"Task exception was never retrieved"
    并且连接保持打开。
    """
    with pytest.raises(ProtocolError):
        _ = Frame(FrameType.REQ_HEAD, 1, b"\xff\xfe{}").json


def test_json_handles_non_ascii() -> None:
    """中文路径必须原样往返，且**不被转义成 \\uXXXX**（ensure_ascii=False）。

    转义会让每个汉字从 3 字节变成 6 字节 —— 库里的路径全是中文，
    这是实打实的带宽浪费。
    """
    raw = encode_frame(FrameType.REQ_HEAD, 1, {"path": "/相册/青甘环线"})
    assert decode_frame(raw).json == {"path": "/相册/青甘环线"}
    assert "相册".encode("utf-8") in raw, "非 ASCII 被转义了（浪费一倍字节）"
    assert b"\\u" not in raw


# --------------------------------------------------------------------------
# 6. Frame.__repr__ —— 日志安全
# --------------------------------------------------------------------------


def test_repr_does_not_dump_whole_payload() -> None:
    """★ repr 必须截断。

    隧道日志会打每一条帧；一条 8 MB 的 REQ_BODY 被完整 repr 会瞬间
    撑爆日志文件（而且这是最常见的帧）。
    """
    f = Frame(FrameType.REQ_BODY, 1, b"x" * 100000)
    r = repr(f)
    assert len(r) < 300, f"repr 太长了（{len(r)} 字符），会把日志撑爆"
    assert "REQ_BODY" in r
    assert "100000B" in r


def test_repr_falls_back_to_hex_for_binary() -> None:
    """二进制 payload 不能硬 decode —— 那会抛 UnicodeDecodeError 打断日志。"""
    r = repr(Frame(FrameType.REQ_BODY, 1, b"\xff\xfe\x00\x01"))
    assert "REQ_BODY" in r


def test_repr_shows_flags() -> None:
    assert "flags=0x80000000" in repr(Frame(FrameType.REQ_HEAD, 1, b"{}", FLAG_ENC))


# --------------------------------------------------------------------------
# 7. FrameParser —— 粘包 / 半包（降级模式的唯一分帧手段）
# --------------------------------------------------------------------------


def test_parser_single_frame() -> None:
    p = FrameParser()
    got = p.feed(pack_stream([Frame(FrameType.PING, 1)]))
    assert [(f.type, f.req_id) for f in got] == [(FrameType.PING, 1)]


def test_parser_handles_byte_by_byte_feed() -> None:
    """★ 逐字节喂入必须正确 —— 这是最极端的半包情形。

    网络栈实际会给出各种切分；只测"一次给完整帧"等于没测分帧逻辑。
    """
    frames = [Frame(FrameType.REQ_HEAD, 1, b'{"a":1}'),
              Frame(FrameType.REQ_BODY, 1, b"payload"),
              Frame(FrameType.REQ_END, 1)]
    blob = pack_stream(frames)
    p = FrameParser()
    got: list[Frame] = []
    for i in range(len(blob)):
        got += p.feed(blob[i:i + 1])
    assert len(got) == 3
    assert [f.type for f in got] == [f.type for f in frames]
    assert got[1].payload == b"payload"
    assert p.buffered == 0, "解析完后缓冲区必须清空"


def test_parser_handles_all_frames_in_one_feed() -> None:
    """★ 粘包：一次喂入多帧必须全部解出来。

    只返回第一帧的话，后面那些帧会永远留在缓冲区里 —— 而调用方
    已经"处理完这批数据"了，不会再来读，表现为随机丢帧。
    """
    frames = [Frame(FrameType.PING, i) for i in range(1, 6)]
    p = FrameParser()
    got = p.feed(pack_stream(frames))
    assert [f.req_id for f in got] == [1, 2, 3, 4, 5]


def test_parser_handles_split_across_arbitrary_boundaries() -> None:
    """以各种切分方式喂入同一段流，结果必须完全一致。"""
    frames = [Frame(FrameType.REQ_HEAD, 1, b'{"x":1}'),
              Frame(FrameType.RES_BODY, 1, b"y" * 100),
              Frame(FrameType.PING, 99)]
    blob = pack_stream(frames)

    expected = [f.type for f in frames]
    for step in (1, 2, 3, 5, 7, 13, 64, len(blob)):
        p = FrameParser()
        got: list[Frame] = []
        for i in range(0, len(blob), step):
            got += p.feed(blob[i:i + step])
        assert [f.type for f in got] == expected, f"step={step} 时解析结果不一致"
        assert p.buffered == 0, f"step={step} 时缓冲区有残留"


def test_parser_returns_empty_until_frame_complete() -> None:
    """半包时必须返回空列表，不能返回残缺帧。"""
    blob = pack_stream([Frame(FrameType.REQ_BODY, 1, b"abcdefgh")])
    p = FrameParser()
    assert p.feed(blob[:3]) == []
    assert p.feed(blob[3:6]) == []
    assert p.buffered > 0
    got = p.feed(blob[6:])
    assert len(got) == 1
    assert got[0].payload == b"abcdefgh"


def test_parser_empty_feed_is_noop() -> None:
    p = FrameParser()
    assert p.feed(b"") == []
    assert p.buffered == 0


def test_parser_poisons_on_oversized_declared_length() -> None:
    """★ 长度前缀不可信时必须毒化连接。

    这是**内存耗尽**防线：对端（或被篡改的链路）声明一个 4 GB 的长度，
    诚实等待的话会把整个进程吃光。毒化后永久拒绝继续解析。
    """
    p = FrameParser(max_frame=1024)
    huge = struct.pack(">I", 100 * 1024 * 1024)
    with pytest.raises(ProtocolError) as ei:
        p.feed(huge)
    assert "exceeds max_frame" in str(ei.value)
    assert p.poisoned is True
    # ★ 毒化必须持久：后续 feed 也要拒绝，不能"这一帧跳过就好了"
    with pytest.raises(ProtocolError) as ei2:
        p.feed(pack_stream([Frame(FrameType.PING, 1)]))
    assert "poisoned" in str(ei2.value).lower()


def test_parser_poisons_on_length_smaller_than_header() -> None:
    """★ 声明长度小于 5 必须毒化。

    不检查的话 `self._need = 4 + n` 会小于 4，于是 `self._need == 4`
    的分支再也进不去，解析器陷入死循环或永久错位。
    """
    # 每个坏值都要一个新的 parser：毒化之后后续 feed 报的是
    # "parser poisoned" 而不是长度本身的问题，混在一个循环里会掩盖真正的断言。
    for bad in (0, 1, 2, 3, 4):
        p = FrameParser()
        with pytest.raises(ProtocolError) as ei:
            p.feed(struct.pack(">I", bad))
        assert "header size" in str(ei.value), f"bad={bad} 报的是 {ei.value}"
        assert p.poisoned is True


def test_parser_accepts_length_exactly_at_max_frame() -> None:
    """边界：payload 正好等于 max_frame 必须放行。"""
    p = FrameParser(max_frame=64)
    blob = pack_stream([Frame(FrameType.REQ_BODY, 1, b"z" * 64)])
    got = p.feed(blob)
    assert len(got) == 1 and got[0].payload == b"z" * 64


def test_parser_rejects_length_one_over_max_frame() -> None:
    p = FrameParser(max_frame=64)
    blob = pack_stream([Frame(FrameType.REQ_BODY, 1, b"z" * 65)])
    with pytest.raises(ProtocolError):
        p.feed(blob)
    assert p.poisoned is True


def test_parser_does_not_slice_buffer_quadratically() -> None:
    """★ 缓冲区推进必须是 del buf[:n]，不能每帧重切整个缓冲区。

    用"喂入 N 帧后 buffered 归零 + 总字节数正确"来间接验证；真正
    防的是 O(n²) —— 那在 8 MB 分片流上会让 CPU 打满而带宽用不满。
    """
    p = FrameParser()
    frames = [Frame(FrameType.REQ_BODY, i, b"x" * 1000) for i in range(50)]
    got = p.feed(pack_stream(frames))
    assert len(got) == 50
    assert p.buffered == 0


def test_parser_survives_frame_boundary_at_buffer_end() -> None:
    """一帧恰好结束在 feed 的最后一个字节 —— 循环必须正确退出。"""
    p = FrameParser()
    blob = pack_stream([Frame(FrameType.PING, 1), Frame(FrameType.PING, 2)])
    got = p.feed(blob)
    assert len(got) == 2
    assert p.feed(b"") == []


def test_pack_stream_format_is_length_prefixed() -> None:
    """流格式：[4B 大端长度][5B 头 + body]，长度不含前缀本身。"""
    raw = encode_frame(FrameType.PING, 1)
    blob = pack_stream([Frame(FrameType.PING, 1)])
    (declared,) = struct.unpack(">I", blob[:4])
    assert declared == len(raw)
    assert blob[4:] == raw


def test_pack_stream_empty_list() -> None:
    assert pack_stream([]) == b""


def test_pack_stream_preserves_flags() -> None:
    p = FrameParser()
    got = p.feed(pack_stream([Frame(FrameType.REQ_HEAD, 1, b"{}", FLAG_ENC)]))
    assert got[0].flags == FLAG_ENC


def test_parser_roundtrip_of_all_types_in_one_stream() -> None:
    """★ 全部 14 种类型混在一条流里，逐 3 字节喂入。

    这是协议层的"全集回归"：任何类型的分帧长度算错都会在这里错位，
    而后面的帧全部解析失败（错位是自解释的，一眼能看出从哪个类型开始崩）。
    """
    from shared.protocol import FIXED_LENGTH_TYPES

    frames = [
        Frame(t, i + 1, b"" if t in FIXED_LENGTH_TYPES else b'{"k":%d}' % (i + 1))
        for i, t in enumerate(FrameType)
    ]
    blob = pack_stream(frames)
    p = FrameParser()
    got: list[Frame] = []
    for i in range(0, len(blob), 3):
        got += p.feed(blob[i:i + 3])
    assert [f.type for f in got] == [f.type for f in frames]
    assert [f.req_id for f in got] == [f.req_id for f in frames]
    assert [f.payload for f in got] == [f.payload for f in frames]


# --------------------------------------------------------------------------
# 8. Transport 抽象 —— "远程留接口"的核心
# --------------------------------------------------------------------------


def test_transport_is_runtime_checkable() -> None:
    """★ Transport 必须是 runtime_checkable。

    二期接云 Relay 时需要一个测试替身（进程内双工管道）。若不能
    `isinstance(obj, Transport)`，替身就只能是"看起来像"，而真正的
    实现漏掉一个方法要到运行时才炸。
    """

    class Good:
        async def send_bytes(self, raw: bytes) -> None: ...
        async def recv_bytes(self) -> bytes: ...
        async def close(self) -> None: ...

    class MissingClose:
        async def send_bytes(self, raw: bytes) -> None: ...
        async def recv_bytes(self) -> bytes: ...

    assert isinstance(Good(), Transport)
    assert not isinstance(MissingClose(), Transport)


def test_protocol_module_self_check_runs() -> None:
    """★ 模块的 `__main__` 自检必须真的通过。

    它是这个模块唯一的"可手动执行"验证，若哪天常量改动让它失败，
    说明协议往返已经坏了 —— 这里让它在 pytest 里也跑一遍。
    """
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    # ★ PYTHONIOENCODING 必须设成 utf-8。子进程默认按控制台代码页（本机
    # GBK）编码 stdout，脚本里的中文会变成乱码，于是"自检通过"这个断言
    # 永远不成立 —— 而 returncode 是 0，看起来像断言写错了而不是环境问题。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    r = subprocess.run(
        [sys.executable, str(repo / "shared" / "protocol.py")],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace", env=env,
    )
    assert r.returncode == 0, f"protocol.py 自检失败：{r.stdout}\n{r.stderr}"
    assert "自检通过" in r.stdout, f"自检输出异常：{r.stdout!r}"
