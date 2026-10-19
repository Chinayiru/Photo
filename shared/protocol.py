"""PhotoBridge 隧道帧协议 —— 云端与电脑端共享的唯一源。

本文件是一期「局域网优先」策略下为二期远程隧道预留的接口层。
一期局域网 HTTP 通路**不经过**本模块（直接走 ASGI 请求/响应），
但传输层抽象（Transport）与帧编解码在此定义完毕，并由
`backend/tests/test_protocol.py` 的 68 条用例覆盖（帧类型数值冻结、
位打包、编解码往返、拒绝路径、粘包/半包分帧、内存耗尽防线）。
二期接入云 Relay 时业务代码零改动（设计文档 DP-4「两条通路，一套 API」）。

权威设计：docs/03-详细设计说明书.md §2
"""
from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Protocol, runtime_checkable

# --------------------------------------------------------------------------
# 帧类型
# --------------------------------------------------------------------------


class FrameType(IntEnum):
    """隧道帧类型。数值一旦发布不得更改（协议兼容性）。"""

    REQ_HEAD = 0x01   # 云→电脑  UTF-8 JSON {method, path, query, headers, enc?}
    REQ_BODY = 0x02   # 云→电脑  上传分片原始字节
    REQ_END = 0x03    # 云→电脑  空
    RES_HEAD = 0x04   # 电脑→云  UTF-8 JSON {status, headers}
    RES_BODY = 0x05   # 电脑→云  响应体分片原始字节
    RES_END = 0x06    # 电脑→云  空
    ERROR = 0x07      # 双向     UTF-8 JSON {code, message, detail?}
    CANCEL = 0x08     # 双向     空（req_id 即目标）
    PING = 0x09       # 双向     空；req_id 承载非零序号
    PONG = 0x0A       # 双向     空；req_id 回显 PING 序号
    # 双向 12 字节 {window, consumed, flags}；req_id 必须为 0（`docs/04` §2.5.3）。
    # ★ 一度被实现成"空 payload + 收到即重置满窗"，那是**错的**：空 payload
    #   表达不了"消费者只消费了 10 KB"，发送方只能拿到整个窗口，于是
    #   (1) 慢消费者（10 KB/s 看图）被当成死连接，(2) 多次回补会让发送方
    #   无限领先消费者。payload 里的 window 就是"本次归还多少字节"。
    CREDIT = 0x0B
    NOTIFY = 0x0C     # 电脑→云→手机 UTF-8 JSON {topic, data}
    # --- 以下两个类型为对 docs/03 §2.2.2 与 §2.9 不一致的修正 ---
    # §2.9 的握手时序使用了 HELLO / HELLO_ACK，但 §2.2.2 的类型表未定义它们。
    # 补在此处（而非复用 REQ_HEAD），因为握手发生在多路复用器建立之前，
    # 且 HELLO 需要独立的校验与拒绝路径（token 无效 → 直接 close）。
    HELLO = 0x0D      # 电脑→云  UTF-8 JSON {device_id, name, version, lan_ips, caps}
    HELLO_ACK = 0x0E  # 云→电脑  UTF-8 JSON {server_time, heartbeat_sec, max_frame, credit_window}


# 定长帧：payload 必须为空。收到非空 payload 记 warning 并忽略。
# ★ `CREDIT` **不在**此列（`docs/04` §2.5.3 规定它带 12 字节 payload）。
#   曾经的实现把它冻结成定长帧，代价是两个真实缺陷：
#   ① 慢消费者（10 KB/s 看图）永远凑不满回补阈值，发送方 30 秒后按
#      `E5010` 判死，手机收到**半张照片且状态码仍是 200**；
#   ② `grant()` 只能"重置回满窗"，无法表达"只消费了 10 KB"，
#      于是对端可以无限领先一个追不上它的消费者。
FIXED_LENGTH_TYPES: frozenset[FrameType] = frozenset(
    {
        FrameType.REQ_END,
        FrameType.RES_END,
        FrameType.CANCEL,
        FrameType.PING,
        FrameType.PONG,
    }
)

# 必须在收到后立即处理、不得排队等业务处理的帧（§2.7.1：大文件传输会饿死心跳）
PRIORITY_TYPES: frozenset[FrameType] = frozenset(
    {FrameType.PING, FrameType.PONG, FrameType.CREDIT, FrameType.CANCEL}
)

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

HEADER_SIZE = 5                     # type(1B) + req_id(4B)
MAX_FRAME = 65536                   # 单个逻辑帧 payload 上限，由 HELLO_ACK 下发

REQ_ID_MASK = 0x3FFFFFFF            # 低 30 位
FLAG_ENC = 0x80000000               # bit31：payload 已 E2E 加密【二期】
FLAG_RESERVED = 0x40000000          # bit30：保留，读取时必须忽略

# 流控（§2.6.2）
CREDIT_WINDOW = 8 * 1024 * 1024         # 每方向每 req_id 初始窗口 8 MB
CONN_CREDIT_WINDOW = 16 * 1024 * 1024   # 连接级总窗口 16 MB
CREDIT_THRESHOLD = 512 * 1024           # 消费满 512 KB 回补一次
CREDIT_DEADLOCK_TIMEOUT = 30.0          # 单 req_id 信用耗尽后无回补则强制关闭
CONN_DEADLOCK_TIMEOUT = 60.0            # 整连接无 CREDIT 进展则断连重连

#: `CREDIT` 帧的 payload 长度（`docs/04` §2.5.3）：window(4) + consumed(4) + flags(4)。
CREDIT_PAYLOAD_SIZE = 12
_CREDIT_STRUCT = struct.Struct(">III")

#: 能力标识：带 12 字节 payload 的 `CREDIT`（按**消费量**归还信用）。
#:
# ★ 这是一个**线上协议变更**。老实现把 `CREDIT` 冻在 `FIXED_LENGTH_TYPES`
#   里，`decode_frame()` 看到 payload 就抛 `ProtocolError` —— 所以"新电脑端
#   连老云端"会在第一条 CREDIT 上直接断连，症状是"传大文件到一半隧道断"。
#   因此握手时要交换这个能力位：只有两端都有，才发带 payload 的 CREDIT；
#   否则退回空帧（老语义"重置满窗"，慢消费者仍会被误杀，但不比从前更坏）。
CREDIT_V2_CAP = "credit_v2"


def wants_credit_v2(peer_caps: object) -> bool:
    """对端能力列表里是否声明了 `credit_v2`。

    两端**都必须**用它判断，而不是各自写一次 `CAP in caps` —— 这个判断
    的错法（`not in`、把 `caps` 当字符串做子串匹配）在单侧写错时只表现为
    "升级后传大文件到一半隧道断"，很难归因。一次实现 + 两端调用，让一条
    用例就能覆盖两个方向。

    `peer_caps` 允许是任何东西（`None`/字符串/含非字符串的列表），因为它
    直接来自对端发来的 JSON：`"credit_v2" in "xcredit_v2x"` 这种子串匹配
    会误判，所以类型不对时一律当作不支持。
    """
    if not isinstance(peer_caps, (list, tuple, set, frozenset)):
        return False
    return any(c == CREDIT_V2_CAP for c in peer_caps)

# 超时（§2.5.5）
HEAD_TIMEOUT = 30.0                 # 发 REQ_HEAD 到收 RES_HEAD/ERROR
IDLE_TIMEOUT = 60.0                 # 两次 RES_BODY 之间的最大间隔
TOTAL_TIMEOUT = 1800.0              # 兜底：2 GB @1.5 MB/s ≈ 22 分钟
SWEEP_INTERVAL = 5.0

# 心跳与重连（§2.7）
HEARTBEAT_SEC = 25.0                # 小于多数 NAT/代理空闲超时（30–60s）
PONG_TIMEOUT = HEARTBEAT_SEC * 3    # 75s
RECONNECT_BASE = 1.0
RECONNECT_MAX = 60.0
RECONNECT_JITTER = 0.20

# 流适配层长度前缀（降级模式用，§2.4）
_LEN = struct.Struct(">I")
_HEADER = struct.Struct(">BI")      # 大端 uint8 + uint32


class ProtocolError(Exception):
    """帧格式非法。收到此异常说明对端实现有 bug 或链路被篡改，应关闭连接。"""


class TooManyRequests(Exception):
    """req_id 池耗尽（活跃请求超过 MAX_ACTIVE）。"""


# --------------------------------------------------------------------------
# req_id 与 flags 的位打包（§2.2.3）
# --------------------------------------------------------------------------


def pack_req_id(req_id: int, flags: int = 0) -> int:
    """把 30 位 req_id 与 flags 打包成 uint32。"""
    if not 0 <= req_id <= REQ_ID_MASK:
        raise ProtocolError(f"req_id {req_id} out of 30-bit range")
    if flags & ~(FLAG_ENC | FLAG_RESERVED):
        raise ProtocolError(f"illegal flags 0x{flags:08X}")
    return (req_id & REQ_ID_MASK) | flags


def unpack_req_id(raw: int) -> tuple[int, int]:
    """从 uint32 拆出 (req_id, flags)。"""
    return raw & REQ_ID_MASK, raw & (FLAG_ENC | FLAG_RESERVED)


# --------------------------------------------------------------------------
# Frame
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Frame:
    type: FrameType
    req_id: int                     # 已剥离 flags 的 30 位值
    payload: bytes = b""
    flags: int = 0                  # F_ENC 等，一期恒 0

    @property
    def json(self) -> dict[str, Any]:
        """把 payload 当 JSON 解析。仅 HEAD/ERROR/NOTIFY/HELLO 使用。"""
        if not self.payload:
            return {}
        try:
            obj = json.loads(self.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ProtocolError(f"bad json payload: {e}") from e
        if not isinstance(obj, dict):
            raise ProtocolError(f"json payload must be an object, got {type(obj).__name__}")
        return obj

    @property
    def enc(self) -> bool:
        """payload 是否已 E2E 加密【二期，一期恒 False】。"""
        return bool(self.flags & FLAG_ENC)

    def __repr__(self) -> str:  # 日志友好，避免打印整个二进制体
        n = len(self.payload)
        head = self.payload[:64]
        try:
            preview = head.decode("utf-8")
        except UnicodeDecodeError:
            preview = head.hex()
        if n > 64:
            preview += f"…(+{n - 64}B)"
        return f"<Frame {self.type.name} rid={self.req_id} flags=0x{self.flags:X} {n}B {preview!r}>"


# --------------------------------------------------------------------------
# 编解码（§2.3.2）
# --------------------------------------------------------------------------


def encode_frame(
    type: FrameType,
    req_id: int = 0,
    payload: bytes | str | dict[str, Any] | None = None,
    flags: int = 0,
) -> bytes:
    """把一条逻辑帧编码成 WebSocket 二进制消息。

    payload 允许 bytes / str / dict：HEAD、ERROR、NOTIFY 三种帧永远是 JSON，
    直接传 dict 比调用方自己 dumps 更不容易出错。
    """
    if payload is None:
        body = b""
    elif isinstance(payload, bytes):
        body = payload
    elif isinstance(payload, str):
        body = payload.encode("utf-8")
    elif isinstance(payload, dict):
        # separators 去空格：JSON 头平均省 8%~12% 字节，且确定性输出便于测试断言
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    else:
        raise TypeError(f"unsupported payload type: {type(payload)!r}")

    if type in FIXED_LENGTH_TYPES and body:
        raise ProtocolError(f"{type.name} must have empty payload, got {len(body)}B")
    if len(body) > MAX_FRAME:
        raise ProtocolError(
            f"payload {len(body)} exceeds max_frame {MAX_FRAME}; "
            f"caller must split into REQ_BODY/RES_BODY chunks"
        )

    return _HEADER.pack(int(type), pack_req_id(req_id, flags)) + body


def decode_frame(data: bytes) -> Frame:
    """把一条完整的 WebSocket 二进制消息解码成 Frame。

    前置条件：data 恰好是一条完整帧（WebSocket 已保证消息边界）。
    半包/粘包是 FrameParser 的职责（见下）。
    """
    if len(data) < HEADER_SIZE:
        raise ProtocolError(f"frame too short: {len(data)} < {HEADER_SIZE}")
    raw_type, raw_rid = _HEADER.unpack_from(data, 0)
    try:
        ftype = FrameType(raw_type)
    except ValueError:
        raise ProtocolError(f"unknown frame type: 0x{raw_type:02X}") from None
    rid, flags = unpack_req_id(raw_rid)
    payload = data[HEADER_SIZE:]
    if ftype in FIXED_LENGTH_TYPES and payload:
        raise ProtocolError(f"{ftype.name} must have empty payload, got {len(payload)}B")
    if ftype is FrameType.CREDIT and payload and len(payload) != CREDIT_PAYLOAD_SIZE:
        raise ProtocolError(
            f"CREDIT payload must be {CREDIT_PAYLOAD_SIZE}B, got {len(payload)}B")
    return Frame(type=ftype, req_id=rid, payload=payload, flags=flags)


def encode_credit(window: int, consumed: int = 0, flags: int = 0) -> bytes:
    """打包 `CREDIT` 的 12 字节 payload（`docs/04` §2.5.3）。

    `window` 是**本次归还的字节数**（不是绝对窗口大小），`consumed` 是
    累计已消费量（仅用于排障，收端不依赖它）。
    """
    return _CREDIT_STRUCT.pack(max(0, min(int(window), 0xFFFFFFFF)),
                               max(0, min(int(consumed), 0xFFFFFFFF)),
                               int(flags) & 0xFFFFFFFF)


def decode_credit(payload: bytes) -> tuple[int, int, int]:
    """解开 `CREDIT` payload → `(window, consumed, flags)`。

    空 payload 视为 0：老版本对端会把 `CREDIT` 发成空帧（那是**曾经**的
    实现），收端必须还能跑 —— 但这里返回 0 会让发送方**不**获得信用，
    而非像老代码那样凭空重置满窗。宁可慢一次，也不静默超发。
    """
    if not payload:
        return 0, 0, 0
    if len(payload) != CREDIT_PAYLOAD_SIZE:
        raise ProtocolError(
            f"CREDIT payload must be {CREDIT_PAYLOAD_SIZE}B, got {len(payload)}B")
    return _CREDIT_STRUCT.unpack(payload)


# --------------------------------------------------------------------------
# 流式解析器（§2.4）—— 降级模式 SSE+POST 用；一期已实现并测试
# --------------------------------------------------------------------------


class FrameParser:
    """流式帧解析器：处理粘包与半包。

    输入流格式：[4B len][5B header + body] [4B len][5B header + body] ...

    为什么需要它：WebSocket 是消息边界协议，理论上不需要分帧；但降级模式
    （SSE + POST 长轮询）是纯字节流，必然粘包/半包。协议层的正确性必须
    不依赖传输层。

    缓冲区策略：
      * self._buf 是 bytearray，追加 O(1) 摊还；
      * 仅在成功解析一帧后才 del self._buf[:n] 推进读指针，
        绝不每帧重新切片整个缓冲区（否则 O(n²)）；
      * 用 self._need 记录"还差多少字节才够一帧"，避免重复解析头部。
    """

    __slots__ = ("_buf", "_need", "_max_frame", "_bad")

    def __init__(self, max_frame: int = MAX_FRAME) -> None:
        self._buf = bytearray()
        self._need = 4                  # 先要 4 字节长度前缀
        self._max_frame = max_frame
        self._bad = False

    def feed(self, data: bytes) -> list[Frame]:
        """喂入一段字节，返回本次能完整解析出的所有帧（可能为空）。"""
        if self._bad:
            raise ProtocolError("parser poisoned by previous oversized frame")
        if data:
            self._buf += data
        out: list[Frame] = []

        while True:
            if len(self._buf) < self._need:
                return out              # 半包：等下次 feed

            if self._need == 4:         # 刚够长度前缀
                (n,) = _LEN.unpack_from(self._buf, 0)
                if n < HEADER_SIZE:
                    self._bad = True
                    raise ProtocolError(f"declared length {n} < header size {HEADER_SIZE}")
                if n - HEADER_SIZE > self._max_frame:
                    # 毒化：长度不可信，继续读会吃光内存
                    self._bad = True
                    raise ProtocolError(
                        f"declared length {n} exceeds max_frame {self._max_frame}"
                    )
                self._need = 4 + n
                continue

            # 够一整帧了
            frame_bytes = bytes(self._buf[4 : self._need])
            del self._buf[: self._need]
            self._need = 4
            out.append(decode_frame(frame_bytes))
            # 循环继续：缓冲区里可能还粘着下一帧

    @property
    def buffered(self) -> int:
        """当前缓冲区字节数。持续 > 1 MB 说明对端异常，应告警。"""
        return len(self._buf)

    @property
    def poisoned(self) -> bool:
        return self._bad


def pack_stream(frames: list[Frame]) -> bytes:
    """把若干帧编码成带长度前缀的字节流（降级模式发送侧用）。"""
    parts: list[bytes] = []
    for f in frames:
        raw = encode_frame(f.type, f.req_id, f.payload, f.flags)
        parts.append(_LEN.pack(len(raw)))
        parts.append(raw)
    return b"".join(parts)


# --------------------------------------------------------------------------
# 传输层抽象（§2.8）—— 这是"远程留接口"的核心
# --------------------------------------------------------------------------


@runtime_checkable
class Transport(Protocol):
    """隧道传输抽象。

    一期实现：无（局域网走 HTTP，不经过隧道）。
    二期实现：WsTransport（WebSocket）、SseTransport（降级模式）。
    测试实现：MemoryTransport（进程内双工管道，见 backend/tests）。

    Mux / FlowController / 业务逻辑只依赖本协议，不依赖任何具体网络库，
    因此二期接入云 Relay 时改动面仅为一个工厂函数。
    """

    async def send_bytes(self, raw: bytes) -> None: ...

    async def recv_bytes(self) -> bytes: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------
# 自检：模块被直接运行时验证基本往返
# --------------------------------------------------------------------------

if __name__ == "__main__":  # pragma: no cover
    for ft in FrameType:
        if ft is FrameType.CREDIT:
            payload = encode_credit(4096, 8192)
        else:
            payload = b"" if ft in FIXED_LENGTH_TYPES else b"x"
        f = Frame(ft, 7, payload)
        assert decode_frame(encode_frame(ft, 7, f.payload)) == f, ft
    assert decode_credit(decode_frame(
        encode_frame(FrameType.CREDIT, 0, encode_credit(1, 2, 3))).payload
    ) == (1, 2, 3)
    p = FrameParser()
    blob = pack_stream([Frame(FrameType.PING, 1), Frame(FrameType.PONG, 1)])
    got: list[Frame] = []
    for i in range(0, len(blob), 3):        # 逐 3 字节喂，强制走半包路径
        got += p.feed(blob[i : i + 3])
    assert [f.type for f in got] == [FrameType.PING, FrameType.PONG], got
    print(f"protocol.py 自检通过：{len(list(FrameType))} 种帧类型，粘包/半包解析正常")
