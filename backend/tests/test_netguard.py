"""Host 头校验（`backend/app/netguard.py`）：本机地址与攻击域名的边界。

## 这批用例为什么存在

手机经**组网**（Radmin `26/8`、Tailscale `100.64/10`）直连时，请求里的 Host
就是那个组网地址。最初的实现只认 `config.local_ipv4_addresses()` 枚举的地址，
而那个函数为了"别把手机路由不到的虚拟网卡印进二维码"主动滤掉了组网网卡 ——
于是组网入口的表现是：首页能打开（静态资源不校验 Host），所有 `/api/*`
一律 404，日志里写着

    Host 头 '26.26.192.40' 不属于本机（防 DNS rebinding）

首页能开是这里最容易骗人的地方：用户看到配对页，就会认为"网络是通的"。

## 判据的核心

DNS rebinding 的攻击形态是「用**攻击者的域名**做 Host，让请求落到 127.0.0.1」。
域名不可能是私网/组网网段的字面量，所以放行这些字面量对那条路零影响；
反过来，收紧到"必须精确等于枚举出来的地址"则会误伤组网与手工配置的地址。
"""
from __future__ import annotations

import pytest

from backend.app import netguard


@pytest.mark.parametrize("host", [
    "26.26.192.40",            # Radmin VPN 的真实地址（实测机型）
    "26.26.192.40:8799",       # 带端口
    "25.1.2.3",                # Hamachi
    "100.64.0.7",              # CGNAT：Tailscale/ZeroTier
    "100.127.255.254",
    "10.1.2.3",
    "172.20.0.1",
    "10.20.30.40",
    "169.254.10.20",
    "127.0.0.1",
    "[fd00::2]:8799",          # IPv6 ULA
    "[fe80::1]:8799",          # IPv6 链路本地
    "[::1]:8799",
    "localhost:8799",
])
def test_local_literals_and_names_are_allowed(host: str) -> None:
    assert netguard.is_host_allowed(host) is True


@pytest.mark.parametrize("host", [
    "evil.example.com",        # 典型 rebinding 域名
    "evil.example.com:8799",
    "8.8.8.8",                 # 公网字面量：不是本机地址
    "1.1.1.1:8799",
    "2001:db8::1",             # 公网文档段
    "not-a-host",              # 无点的主机名：可能是内网 DNS 名，但不是本机
    "",
])
def test_foreign_hosts_are_rejected(host: str) -> None:
    assert netguard.is_host_allowed(host) is False


def test_check_host_returns_none_for_mesh_address() -> None:
    assert netguard.check_host("26.26.192.40:8799") is None


def test_check_host_explains_rejection() -> None:
    msg = netguard.check_host("evil.example.com")
    assert msg is not None
    assert "evil.example.com" in msg
    assert "DNS rebinding" in msg


def test_missing_host_is_not_this_layers_business() -> None:
    """没有 Host 头的请求（内部 dispatch、隧道适配器）不归这层管。"""
    assert netguard.check_host(None) is None
    assert netguard.check_host("") is None


def test_out_of_range_octets_are_rejected() -> None:
    assert netguard.is_host_allowed("26.26.192.999") is False
    assert netguard.is_host_allowed("26.26.192") is False


def test_wildcard_configuration_disables_the_check() -> None:
    class _Cfg:
        def get(self, key, default=None):
            if key == netguard.TRUSTED_HOSTS_KEY:
                return ["*"]
            return default

    assert netguard.is_host_allowed("anything.example", _Cfg()) is True
