"""云端 -> 电脑端的内部控制通道（`POST /.pb/control`）。

对端实现见 `backend/app/control.py`。这里只做一件事：把 JSON 送进那条已经
通过 `agent_token` 鉴权的隧道，再把 JSON 拿回来。

## 为什么云端需要它

审计 2026-10-26 的两条「已知安全边界」（`docs/06` §13）根子都在
"云端替电脑签名"：令牌的 HMAC 密钥就是 `devices.agent_token` 那一列，
库一泄露就能离线签出任意手机令牌。现在签发搬回电脑端：

* 配对成功 -> `mint_client`，云端只存 `sha256(token)`；
* 遇到自己不认识的令牌（局域网配对签的）-> `verify_client`，让电脑判；
* 用户移除手机 -> `revoke_clients`，立刻推过去，不等 10 分钟轮询。

代价：这三件事都要求电脑在线。配对本来就要求在线（配对码 / 自动配对都以
"设备在线"为前置条件），撤销推送失败也只是退回轮询，所以没有真正的
可用性损失。

## 为什么不抛异常

控制通道是**尽力而为**的增强路径。让它抛会把一次本来成功的配对变成 500，
而用户看到的是"配对失败"，排查方向全错。调用方一律按"返回 None = 这次
增强没生效"处理。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .tunnel import TunnelConnection, forward_request

#: 控制通道路径（与电脑端 `backend/app/control.py` 的常量一致）。
CONTROL_PATH = "/.pb/control"

#: 控制调用最多等这么久。比 `HEAD_TIMEOUT`(30 s) 短：控制消息是内网级的
#: 小对象，等 10 秒还不回来说明那条隧道实际已经废了，再等只会把用户的
#: 配对请求一起拖死。
CONTROL_TIMEOUT = 10.0

log = logging.getLogger("pb.cloud.control")


async def call_agent(conn: TunnelConnection | None, action: str,
                     payload: dict[str, Any] | None = None, *,
                     timeout: float = CONTROL_TIMEOUT) -> dict[str, Any] | None:
    """向电脑端发一条控制命令，返回它的 JSON（失败一律返回 None）。"""
    if conn is None:
        return None
    raw = json.dumps({"action": action, "payload": payload or {}},
                     ensure_ascii=False).encode("utf-8")

    async def _body() -> Any:
        # `forward_request()` 要的是**异步可迭代对象**，不是 bytes：它起一个
        # pump 任务把块逐帧塞进隧道。喂 bytes 会让 pump 立刻停下，电脑端
        # 永远等不到请求体 —— 症状是 10 秒后 `mint_client` 报 TimeoutError
        # （配对 503 而配对码没被消费）。控制消息不超过几百字节，一块发完。
        yield raw
    pending = None
    pump = None
    try:
        pending, pump = await forward_request(
            conn, method="POST", path=CONTROL_PATH, query={},
            headers={"content-type": "application/json"},
            body=_body(), body_len=len(raw), head_timeout=timeout,
            total_timeout=timeout)
        head = await pending.wait_head(timeout)
        # `wait_head()` 回的是 RES_HEAD 的 payload（dict），不是状态码。
        status = int(head.get("status") or 0)
        if status != 200:
            log.debug("控制命令 %s 返回 %s", action, status)
            return None
        chunks: list[bytes] = []
        async for chunk in pending.iter_body():
            chunks.append(bytes(chunk))
        raw = b"".join(chunks)
        if not raw:
            return None
        obj = json.loads(raw.decode("utf-8"))
    except Exception as exc:        # noqa: BLE001 - 见模块 docstring
        log.debug("控制命令 %s 失败：%s", action, type(exc).__name__)
        return None
    finally:
        if pump is not None:
            try:
                pump.cancel()
            except Exception:       # noqa: BLE001
                pass
        if pending is not None:
            try:
                pending.close()
            except Exception:       # noqa: BLE001
                pass
    if not isinstance(obj, dict) or not obj.get("ok"):
        return None
    return obj


def conn_for(registry: Any, device_id: str) -> TunnelConnection | None:
    """取某台设备当前的隧道连接；不在线返回 None。"""
    try:
        return registry.get(device_id)
    except Exception:               # noqa: BLE001
        return None
