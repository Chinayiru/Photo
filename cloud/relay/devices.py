"""设备注册表（内存）+ 令牌查询（库）。

## 为什么注册表在内存里，而**不能**查库

`docs/10` §2.3 把这条写成依赖规则："`tunnel.py`（云）→ `devices.py`，查的
是**内存 map**，不得查库"。原因有两层：

1. **性能**：一个手机打开相册会同时发几十个请求（缩略图），每个请求都
   要判断"这台电脑在线吗"。查 sqlite 是几十微秒乘以几百并发——而这些
   信息每 60 秒就可能变一次，缓存它的收益远大于成本。
2. **正确性**：库里**没有**"在线"这个字段，也不该有。一台电脑断电时不会
   有机会更新任何字段，任何落库的 `online` 标记都会永远停在 `1`。唯一
   权威的在线信号是"这条 WebSocket 还开着"，而那只存在于本进程内存里。

这直接推出一个部署约束：**中继必须单进程**（`uvicorn --workers 1`）。
两个 worker 会各持一半注册表，手机连到 A、电脑连到 B 时表现为"电脑离线"
而实际在线。`main.py` 的启动日志会把这条打出来。

## 令牌查询为什么要单独包一层

`agent_token`/`client_token` 的校验会被 `/agent/ws` 与 `/d/**` 两条路径
调用，且都发生在**每个请求**上。把它们放在一个地方，才能保证"看 token
时永远同时看 revoked 与 expires_at"——漏掉一处的表现是"撤销了还能用"，
而那是最难在测试里复现的一类 bug（因为要撤销之后才发现）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from ..relay.db import CloudDB
from . import security
from shared import client_token as _shared_token

__all__ = [
    "TunnelConnection",
    "DeviceRegistry",
    "find_device_by_agent_token",
    "find_client_by_token",
    "find_or_adopt_lan_token",
    "touch_device",
    "touch_client",
    "record_traffic",
    "today_utc",
    "MAX_INFLIGHT",
]

log = logging.getLogger(__name__)

#: 电脑端局域网配对签发的令牌前缀（`shared/client_token.py:TOKEN_PREFIX`）。
#: 只对带这个前缀的令牌走"验签 + 收编"回退，随机串一律不进那条路径
#: —— 否则每一枚野令牌都要做一次 HMAC，等于给未鉴权请求开了个 CPU 面。
_TOKEN_PREFIX = "pbc1"

#: 单设备并发隧道请求上限（`docs/04` §3.3：超出回 429 E4003）。
#:
#: 为什么是 64 而不是更大：一个手机屏幕最多同时显示几十个缩略图，64 已经
#: 覆盖"滚得很快"的情形；再大只会让一台手机把另一台的带宽挤干。
MAX_INFLIGHT: int = 64


def today_utc() -> str:
    """`traffic.day` 的格式（`docs/05` §2.2：UTC `YYYY-MM-DD`）。

    用 UTC 而不是本地时区：云端可能部署在任何地方，而"这一天的流量"
    是给用户看账单的，跨时区时以服务器时区切分会出现"同一天算了两次"。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class TunnelConnection:
    """一条活着的电脑端隧道。**只在内存里**。"""

    device_id: str
    account_id: str
    ws: Any
    mux: Any
    session_id: str
    connected_at: float = field(default_factory=time.time)
    version: str = ""
    lan_ips: list[str] = field(default_factory=list)
    caps: list[str] = field(default_factory=list)
    name: str = ""
    #: 最近一次 `HELLO` 上报的照片条数。**故意不落库**（见
    #: `devices_api._indexed_count` 的说明）：它随扫描变化，落库就多一份
    #: 可能过期的真相。`devices_api` 读的正是这个名字——两边必须一致，
    #: 否则用户在看到"已索引 4900 张"之前会先看到"未知"。
    indexed_count: int | None = None
    #: 当前打到这台设备上的在途 HTTP 请求数（转发侧增减）。
    inflight: int = 0
    #: 今天的双向字节（进程内计数；落库在 `record_traffic`，按需批量写）。
    bytes_in_today: int = 0
    bytes_out_today: int = 0
    last_frame_at: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        """给 `/api/devices/{id}/status` 与日志用的安全视图（不含 mux/ws）。"""
        return {
            "device_id": self.device_id,
            "name": self.name,
            "version": self.version,
            "session_id": self.session_id,
            "connected_at": self.connected_at,
            "online_sec": round(time.time() - self.connected_at, 1),
            "inflight": self.inflight,
            "lan_ips": list(self.lan_ips),
            "caps": list(self.caps),
            "bytes_in_today": self.bytes_in_today,
            "bytes_out_today": self.bytes_out_today,
        }


class DeviceRegistry:
    """`device_id → TunnelConnection`。

    ## 重连时的两种竞态，以及它们各自怎么解

    1. **新连接到达时旧连接还开着**（电脑端网络抖动，旧的 WSS 半死不活）。
       新连接必须赢：`register()` 覆盖映射并返回旧连接，由调用方关掉它。
       反过来（拒绝新连接）会让电脑端永远连不回来——它无法知道旧的其实
       已经死了。
    2. **旧连接随后自己断开**（TCP 超时终于触发）。此时它调 `unregister`
       时，映射里已经是新连接了。所以 `unregister` 必须比较**身份**而不是
       只按 device_id 删——否则一次迟到的清理会把新连接从映射里踢掉，
       表现为"刚连上就显示离线，然后每 60 秒抖一次"。
    """

    __slots__ = ("_map", "_lock")

    def __init__(self) -> None:
        self._map: dict[str, TunnelConnection] = {}
        self._lock = asyncio.Lock()

    async def register(self, conn: TunnelConnection) -> TunnelConnection | None:
        """登记一条新连接。返回被顶掉的旧连接（调用方负责关闭）。"""
        async with self._lock:
            old = self._map.get(conn.device_id)
            self._map[conn.device_id] = conn
        if old is not None and old is not conn:
            log.info("设备 %s 重连，顶掉旧会话 %s",
                     conn.device_id, old.session_id)
            return old
        return None

    async def unregister(self, device_id: str,
                         conn: TunnelConnection | None = None) -> bool:
        """注销。**按身份**匹配，避免迟到的清理踢掉新连接。"""
        async with self._lock:
            current = self._map.get(device_id)
            if current is None:
                return False
            if conn is not None and current is not conn:
                return False
            del self._map[device_id]
            return True

    def get(self, device_id: str) -> TunnelConnection | None:
        return self._map.get(device_id)

    def count(self) -> int:
        return len(self._map)

    def all(self) -> list[TunnelConnection]:
        return list(self._map.values())

    def stats(self) -> dict[str, Any]:
        return {
            "online": len(self._map),
            "devices": [c.public() for c in self._map.values()],
        }


# ---------------------------------------------------------------------------
# 令牌查询（库）
# ---------------------------------------------------------------------------


def find_device_by_agent_token(db: CloudDB, token: str) -> sqlite3.Row | None:
    """按 `agent_token` 找设备。

    ★ 先把 token 哈希再过索引：`devices.agent_token` 上存的是 sha256
    （`docs/05` §2.2），明文 token 绝不进 SQL 语句——否则它会出现在
    sqlite 的错误信息与 `EXPLAIN` 里，也就出现在日志里。
    """
    if not isinstance(token, str) or not token:
        return None
    digest = security.hash_token(token)
    if not digest:
        return None
    return db.query_one(
        "SELECT id, account_id, name, agent_token, version, last_seen, "
        "last_lan, created_at, revoked FROM devices WHERE agent_token = ?",
        (digest,))


def find_client_by_token(db: CloudDB, token: str) -> sqlite3.Row | None:
    """按 `client_token` 找配对，并**同时**校验 revoked 与 expires_at。

    为什么把两件事放一个函数：`/d/**` 的每个请求都要走这里，若调用方
    自己判 revoked，那么"某个新加的接口忘了判"就是时间问题；而撤销
    失效的后果是"用户以为移除了手机，别人还能看他的相册"。
    """
    if not isinstance(token, str) or not token:
        return None
    digest = security.hash_token(token)
    if not digest:
        return None
    row = db.query_one(
        "SELECT id, device_id, name, token_hash, last_seen, created_at, "
        "revoked, expires_at FROM clients WHERE token_hash = ?", (digest,))
    if row is None:
        return None
    if int(row["revoked"] or 0) != 0:
        return None
    expires = row["expires_at"]
    if expires is not None and float(expires) <= time.time():
        return None
    return row


def find_or_adopt_lan_token(db: CloudDB, token: str,
                            device_id: str) -> sqlite3.Row | None:
    """接受**电脑端在局域网配对时签发**的 HMAC 令牌，并把它收编进 `clients`。

    ## 为什么需要这条回退

    产品里手机只有一条配对路径：在家连局域网、扫托盘二维码、
    `POST /api/pair`（`frontend/src/stores/auth.ts` 只投递这一个接口；
    云端 `/api/devices/{id}/pair` 是账号后台用的，手机 UI 走不到）。
    只查 `clients.token_hash` 的话，那枚令牌从没进过云端库，于是
    "在家能用、出门 401 E2002"——而 P1 的全部价值就在出门那一段。
    实测见 `build/_probe_lan_token_on_cloud.py`。

    ## 凭什么能验

    `client_token` 的 HMAC 密钥就是 `sha256(agent_token)`，而
    `devices.agent_token` 存的正是这个值（`cloud/relay/db.py` 的 DDL）。
    两端算出来逐字节相同，所以云端**不需要回源电脑**就能验签 ——
    这正是 `shared/client_token.py` 存在的理由。

    设备从哪来：用**路径里的** `device_id` 去查那台设备的 `agent_token`，
    再用令牌自带的 `dev` claim 反查一遍（`verify_token(device_id=...)`
    会拒掉不一致的）。所以为 A 签的令牌在 B 的路径上无效。

    ## 为什么要收编进 `clients`

    不收编的话这枚令牌在账号后台里是隐形的：用户看到"已配对 0 台手机"，
    点不了移除，而令牌还在用。落一行之后
    `DELETE /api/devices/{id}/clients/{cid}` 与
    `GET /api/clients/revoked` 才覆盖得到它（电脑端靠后者把撤销拉回本地）。

    ## 收编之后仍然要判撤销

    `INSERT ... ON CONFLICT(id) DO NOTHING` 而不是 `DO UPDATE`：一行已经
    被标成 `revoked=1` 时，重放同一枚令牌不该把它改回 `revoked=0`
    —— 那正是审计里"撤销只生效一次"那类缺陷的形态。
    """
    if not isinstance(token, str) or not token:
        return None
    if not token.startswith(_TOKEN_PREFIX):
        return None
    dev = str(device_id or "").strip().lower()
    if not dev:
        return None

    row = db.query_one(
        "SELECT id, agent_token, revoked FROM devices WHERE id = ?", (dev,))
    if row is None or int(row["revoked"] or 0) != 0:
        return None
    secret = str(row["agent_token"] or "")
    if not secret:
        return None

    claims = _shared_token.verify_token(secret, token, device_id=dev)
    if claims is None:
        return None

    client_id = str(claims.get("sub") or "")[:64]
    if not client_id:
        return None
    name = str(claims.get("nam") or "").strip()[:64] or "手机"
    expires = float(claims.get("exp") or 0.0) or None
    now = time.time()
    try:
        db.execute(
            "INSERT INTO clients(id, device_id, name, token_hash, last_seen,"
            " created_at, revoked, expires_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 0, ?)"
            " ON CONFLICT(id) DO NOTHING",
            (client_id, dev, name, security.hash_token(token), now, now, expires))
    except sqlite3.Error:
        # 收编失败不该让请求挂掉：令牌本身验签已经通过了。日志里
        # 只记 client_id，不记令牌本身（`scripts/check_lint.py` 规则 4）。
        log.warning("局域网令牌收编失败 client_id=%s", client_id)
        return None

    cached = db.query_one(
        "SELECT id, device_id, name, token_hash, last_seen, created_at, "
        "revoked, expires_at FROM clients WHERE id = ?", (client_id,))
    if cached is None or int(cached["revoked"] or 0) != 0:
        return None
    exp = cached["expires_at"]
    if exp is not None and float(exp) <= now:
        return None
    return cached


#: 同一个设备/客户端的 `last_seen` 最多每 60 秒写一次。
_TOUCH_INTERVAL: float = 60.0


def touch_device(db: CloudDB, device_id: str, *, now: float | None = None,
                 lan_ips: Iterable[str] | None = None,
                 version: str | None = None) -> None:
    """更新 `devices.last_seen`（节流），并顺带落 `last_lan` / `version`。

    ## 为什么 `last_lan` 必须落库

    `last_lan`（`docs/04` §3.3）的唯一用途是：手机在云端配好对之后，
    **回到家里时要能用它直接连局域网**（`docs/03` §5.1 的 `DIRECT` 通路）。
    它只能从 `HELLO` 里来 —— 云端根本不知道电脑的私网地址。不落库的话，
    云端配对响应里的 `lan_ips` 永远是空数组，手机就永远只有云端一条路，
    在家里也绕公网跑（`docs/07` 的 `TC-TUNNEL-FUNC-002` 抓的就是这一点）。

    ## 为什么 `version` 也落

    `devices.version` 在 `cloud/relay/db.py` 的 DDL 里就是"电脑端上报的
    版本"，`docs/04` §3.3 的 `devices[]` 也把它列为字段。不落库时它永远是
    `NULL`，运维看不到"哪台电脑还在跑旧版本"——那正是升级时要看的东西。
    与 `last_lan` 同理，二者都只在握手时才有值，错过就再也拿不到。

    ## 为什么仍然节流

    一台电脑每秒几十帧会让 `devices` 表每帧写一次——sqlite 的写事务是
    全局串行的，这会把中继的吞吐压到几百 req/s。`last_seen` 的用途是
    "这台电脑多久没出现了"，60 秒精度完全够用。`lan_ips` / `version`
    只在**改变时**才写，所以稳态下不会带来额外写入。
    """
    ts = time.time() if now is None else now
    try:
        db.execute(
            "UPDATE devices SET last_seen = ? WHERE id = ? "
            "AND (last_seen IS NULL OR last_seen < ?)",
            (ts, device_id, ts - _TOUCH_INTERVAL))
        if lan_ips is not None:
            payload = json.dumps([str(x)[:64] for x in list(lan_ips)[:16]],
                                 ensure_ascii=False)
            # 只在变化时写：同一台电脑重连几十次，地址一般不变
            db.execute(
                "UPDATE devices SET last_lan = ? WHERE id = ? "
                "AND (last_lan IS NULL OR last_lan != ?)",
                (payload, device_id, payload))
        if version:
            text = str(version)[:32]
            db.execute(
                "UPDATE devices SET version = ? WHERE id = ? "
                "AND (version IS NULL OR version != ?)",
                (text, device_id, text))
    except sqlite3.Error:
        log.debug("更新 last_seen 失败 device=%s", device_id, exc_info=True)


def touch_client(db: CloudDB, client_id: str, *, now: float | None = None) -> None:
    ts = time.time() if now is None else now
    try:
        db.execute(
            "UPDATE clients SET last_seen = ? WHERE id = ? "
            "AND (last_seen IS NULL OR last_seen < ?)",
            (ts, client_id, ts - _TOUCH_INTERVAL))
    except sqlite3.Error:
        log.debug("更新客户端 last_seen 失败 client=%s", client_id,
                  exc_info=True)


def record_traffic(db: CloudDB, device_id: str, *, bytes_in: int = 0,
                   bytes_out: int = 0, requests: int = 1,
                   day: str | None = None) -> None:
    """累加当天流量（AC-3.15）。

    **方向约定**（容易记反，写在这里）：`bytes_in` = 手机→电脑的上行
    （上传照片），`bytes_out` = 电脑→手机的下行（看照片）。从云端的角度
    看正好相反，但对用户来说"上传/下载"才是他理解的词，所以按用户的
    方向命名，账单一目了然。

    单条 `UPSERT`：读改写分两条语句会丢更新（两个请求同时读到旧值）。
    """
    if bytes_in <= 0 and bytes_out <= 0 and requests <= 0:
        return
    try:
        db.execute(
            "INSERT INTO traffic(device_id, day, bytes_in, bytes_out, "
            "req_count) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(device_id, day) DO UPDATE SET "
            "bytes_in = bytes_in + excluded.bytes_in, "
            "bytes_out = bytes_out + excluded.bytes_out, "
            "req_count = req_count + excluded.req_count",
            (device_id, day or today_utc(), max(0, int(bytes_in)),
             max(0, int(bytes_out)), max(0, int(requests))))
    except sqlite3.Error:
        # 记账失败绝不能影响转发：一次写失败换来一个 500 是本末倒置。
        log.debug("写 traffic 失败 device=%s", device_id, exc_info=True)


def traffic_today(db: CloudDB, device_id: str) -> dict[str, int]:
    row = db.query_one(
        "SELECT bytes_in, bytes_out, req_count FROM traffic "
        "WHERE device_id = ? AND day = ?", (device_id, today_utc()))
    if row is None:
        return {"bytes_in": 0, "bytes_out": 0, "req_count": 0}
    return {
        "bytes_in": int(row["bytes_in"] or 0),
        "bytes_out": int(row["bytes_out"] or 0),
        "req_count": int(row["req_count"] or 0),
    }
