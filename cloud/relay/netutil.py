"""客户端来源地址：谁是真客户端，谁只是中间那一跳。

## 为什么单独一个模块

限流（`limiter.py`）、配对码锁定（`pairing.py`）、登录闸门（`auth.py`）、
隧道帧里的 `Ctx.client_ip`（`forward.py`）都读"客户端 IP"。四处各写一遍
`request.client.host` 的后果在 2026-10-23 的安全审计里被实测出来了：部署
拓扑是 `公网 → cloudflared / 80 端口转发 → 127.0.0.1:8000`，于是**每一台
手机都是 127.0.0.1**，全世界的请求挤在同一个限流桶里 —— 攻击者每分钟发
10 个假登录就能把主人锁在门外（`docs/11` ISSUE-030）。

## 判据

* `request.client.host` 是**直连对端**，它不能被伪造（TCP 三次握手决定）。
* `X-Forwarded-For` 是**对端写的**，只有在"对端是我们自己的代理"时才可信。
* 所以：对端 ∈ `PB_CLOUD_TRUSTED_PROXIES` → 从 `X-Forwarded-For` **右往左**
  找第一个不在受信段的地址（最右边是最后一跳代理写的，最左边才是客户端
  能随便写的）。对端不在受信段 → 直接用对端，`X-Forwarded-For` 一个字都
  不看。

★ **默认受信集为空**。这是刻意的：默认信任任何东西等于把限流交给攻击者
配置。要用 cloudflared / 80 端口转发时，必须在部署脚本里显式写
`PB_CLOUD_TRUSTED_PROXIES=127.0.0.1,::1`（见 `cloud/deploy.ps1`）。

★ 反过来，受信集为空 + 前面有代理时，所有请求会共用一个桶（`127.0.0.1`）。
那是**安全**的失败方向（宁可大家一起排队，也不让一个人伪造一千个身份），
代价是可用性 —— 所以 `run_relay.ps1` 必须把它设对，并且 `main.py` 启动时
在没有受信代理却检测到对端全是回环的情况下打一条 warning。
"""
from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from typing import Any

__all__ = [
    "TRUSTED_ENV",
    "client_ip",
    "is_trusted_proxy",
    "peer_ip",
    "rate_key",
    "trusted_networks",
]

#: 受信代理的来源地址（逗号分隔的 IP 或 CIDR）。
TRUSTED_ENV = "PB_CLOUD_TRUSTED_PROXIES"

#: 取不到地址时的占位。它与任何真实地址都不相等，因此不会与某个真实
#: 客户端共享桶 —— 但也意味着"取不到"这一点是可观测的（日志里能 grep）。
UNKNOWN = "unknown"


@lru_cache(maxsize=8)
def _parse(text: str) -> tuple[Any, ...]:
    """把环境变量的内容解析成网段元组。坏值**跳过**而不是抛异常。

    配置写错（`127.0.0.1 127.0.0.2`、多了个空格、IPv6 少了括号）时，
    进程必须还能起来：起不来的话线上就是 502，而写错一个字符的人正在
    回家的路上。
    """
    nets: list[Any] = []
    for chunk in text.split(","):
        item = chunk.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue
    return tuple(nets)


def trusted_networks() -> tuple[Any, ...]:
    """当前配置的受信代理网段（空元组 = 谁都不信）。"""
    return _parse(os.environ.get(TRUSTED_ENV, "").strip())


def _addr(host: str) -> Any:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _in_networks(host: str, nets: tuple[Any, ...]) -> bool:
    addr = _addr(host)
    if addr is None:
        return False
    for net in nets:
        try:
            if addr in net:
                return True
        except TypeError:           # IPv4 地址对 IPv6 网段：不算命中
            continue
    return False


def peer_ip(request: Any) -> str:
    """直连对端地址（不可伪造的那一个）。"""
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return str(host or "") or UNKNOWN


def is_trusted_proxy(request: Any) -> bool:
    """对端是不是我们自己的代理（它写的 `X-Forwarded-For` 才可信）。"""
    return _in_networks(peer_ip(request), trusted_networks())


def client_ip(request: Any) -> str:
    """真实客户端地址。见模块 docstring 的判据。"""
    peer = peer_ip(request)
    nets = trusted_networks()
    if not nets or not _in_networks(peer, nets):
        return peer
    raw = request.headers.get("x-forwarded-for") or ""
    hops = [h.strip() for h in raw.split(",") if h.strip()]
    # 从右往左：最右边是最后一跳代理追加的（可信），最左边是客户端能
    # 随便写的（不可信）。跳过受信链上的每一跳，第一个不受信的就是客户端。
    for hop in reversed(hops):
        if _in_networks(hop, nets):
            continue
        addr = _addr(hop)
        if addr is None:
            continue
        return str(addr)
    return peer


def rate_key(request: Any) -> str:
    """限流桶的 key。

    与 `client_ip()` 的区别只有一个：取不到地址时不要和真实地址撞桶。
    目前两者等价，单独留一个函数的理由是"限流的 key"将来还要掺别的维度
    （比如 `client_ip + email` 的双桶），到那时改这里而不是改四个调用点。
    """
    return client_ip(request)
