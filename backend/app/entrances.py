"""候选入口地址：把「手机该填哪个地址」从用户的记忆里搬到页面上。

## 为什么需要这个模块

配对页上有两处要用户手填地址：局域网的「电脑地址」、云端的「云端链接」。
手填这一步是**真实的第一道门槛** —— 用户不知道家里电脑的 IP，更不知道
Tailscale 会给它分配另一个地址，于是"我明明装好了却连不上"。

这个模块把本机所有**手机真能连上**的地址枚举出来，让前端做成可点即填。

## 三条判据（都是踩过的坑）

1. **只列真能连的**：`config.local_ipv4_addresses()` 已经把 VMware/WSL 这类
   "永远连不上的虚拟网卡"滤掉了，本模块复用它，不再自己发明一套。
2. **组网地址单独一组**：Tailscale 的 `100.64.0.0/10` 与 Hamachi 的 `25.0.0.0/8`
   属于虚拟网卡，但它们**手机真能连**（前提是手机装了同一个组网客户端），
   所以不能跟局域网混在一组里 —— 用户要知道"在外面用哪条"。
3. **Radmin 的 `26.0.0.0/8` 刻意不列**：手机端没有官方客户端，列出来只会
   引导用户去连一个连不上的地址。它的 Host 校验仍然放行（见 `netguard.py`）。

## 与安全的关系

本模块输出的都是**本机地址**，不含令牌、不含设备口令。接口因此可以匿名
（`/api/system/entrances` 在 `auth.PUBLIC_PATHS` 里）：未配对的人本来就能
从局域网里扫到这台机器的 IP，藏起来没有意义；反过来，配对页必须能在
**没有任何令牌**的状态下拿到它。
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Any, Final, Iterable

__all__ = [
    "MESH_NETWORKS",
    "LAN_LABEL",
    "MESH_LABEL",
    "CLOUD_LABEL",
    "classify",
    "collect",
    "mesh_ipv4_addresses",
]

#: 虚拟组网网段 → 给用户看的标签。
#:
#: `100.64.0.0/10` 是 CGNAT 段，Tailscale 固定用其中的地址；
#: `25.0.0.0/8` 是 Hamachi 的历史段。两者都**排除** `26.0.0.0/8`（Radmin）。
MESH_NETWORKS: Final[tuple[tuple[ipaddress.IPv4Network, str], ...]] = (
    (ipaddress.ip_network("100.64.0.0/10"), "Tailscale"),
    (ipaddress.ip_network("25.0.0.0/8"), "Hamachi"),
)

LAN_LABEL: Final = "家里 Wi-Fi（同一个路由器下）"
MESH_LABEL: Final = "VPN（在外面用这个）"
CLOUD_LABEL: Final = "云端入口（手机没开 VPN 时用）"


def _host_ipv4() -> list[str]:
    """本机主机名解析到的全部 IPv4（顺序稳定、去重、去掉回环）。

    实测这台机器会返回 Tailscale 的 `100.103.255.39` 与 Radmin 的
    `26.26.192.40` —— 组网网卡也在这个列表里，所以组网地址不需要另找来源。
    """
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addr = info[4][0]
            if not addr or addr.startswith("127.") or addr in found:
                continue
            found.append(addr)
    except OSError:  # pragma: no cover —— 主机名解析失败时退化为"没有"
        return []
    return found


def _mesh_network_of(addr: str) -> str:
    """地址属于哪个组网网段；不属于就返回空串。"""
    try:
        ip = ipaddress.IPv4Address(addr)
    except ValueError:
        return ""
    for net, name in MESH_NETWORKS:
        if ip in net:
            return name
    return ""


def mesh_ipv4_addresses() -> list[str]:
    """本机的组网地址（Tailscale / Hamachi），顺序按 `_host_ipv4()`。"""
    return [a for a in _host_ipv4() if _mesh_network_of(a)]


def _url(addr: str, port: int, scheme: str) -> str:
    return f"{scheme}://{addr}:{int(port)}/"


def classify(
    *,
    lan: Iterable[str],
    mesh: Iterable[str],
    port: int,
    scheme: str = "http",
) -> dict[str, Any]:
    """把地址分成「局域网」与「组网」两组，各自带上可点的 URL。

    纯函数（不碰网络、不碰配置），测试直接喂列表。
    """
    lan_items = [
        {"label": LAN_LABEL, "host": f"{a}:{int(port)}", "url": _url(a, port, scheme)}
        for a in lan
    ]
    mesh_items = [
        {
            "label": f"{MESH_LABEL} · {_mesh_network_of(a) or 'mesh'}",
            "host": f"{a}:{int(port)}",
            "url": _url(a, port, scheme),
        }
        for a in mesh
    ]
    return {"port": int(port), "lan": lan_items, "vpn": mesh_items, "cloud": None}


def collect(*, cfg: Any, port: int, scheme: str = "http") -> dict[str, Any]:
    """装配接口返回值：局域网 + 组网（云端由调用方补 `cloud` 字段）。"""
    from .config import local_ipv4_addresses

    try:
        lan_ips = list(local_ipv4_addresses())
    except Exception:  # pragma: no cover —— 枚举失败不该让首页 500
        lan_ips = []
    mesh_ips = mesh_ipv4_addresses()
    # 组网地址有时也会被 `local_ipv4_addresses()` 认成"可用"（它按源地址路由
    # 探测），所以这里做一次去重，避免同一条地址出现两次。
    lan_only = [a for a in lan_ips if a not in mesh_ips]
    return classify(lan=lan_only, mesh=mesh_ips, port=port, scheme=scheme)
