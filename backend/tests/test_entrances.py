"""候选入口地址（`backend/app/entrances.py` 与 `/api/system/entrances`）。

## 这批用例在守什么

配对页上那两处「电脑地址 / 云端链接」是用户最容易卡住的地方：他不知道家里
电脑今天被 DHCP 分到哪个 IP，也不知道 Tailscale 会给它分另一个地址。接口
把这些地址算出来，页面做成可点即填。

判据三条：
1. **只列真能连的**：组网地址单独一组（在外面临时用），虚拟网卡（VMware/WSL）
   一个都不能出现在局域网组里 —— 否则手机去连一个永远连不上的地址。
2. **Radmin 的 `26/8` 不进组网组**：手机端没有官方客户端。
3. **匿名可读**：配对页在拿到令牌之前就要渲染，这个接口不能要鉴权；它只回
   本机地址（扫一遍网段就能拿到的东西）。
"""
from __future__ import annotations

import pytest

from backend.app import entrances


def test_classify_splits_lan_and_mesh_into_two_groups() -> None:
    out = entrances.classify(
        lan=["192.0.2.10"], mesh=["100.103.255.39"], port=8799
    )
    assert [i["url"] for i in out["lan"]] == ["http://192.0.2.10:8799/"]
    assert [i["url"] for i in out["vpn"]] == ["http://100.103.255.39:8799/"]
    assert out["port"] == 8799
    assert out["cloud"] is None
    # 组网那条必须自己说明白「在外面用这个」，否则用户不知道选哪条
    assert "在外面用这个" in out["vpn"][0]["label"]


def test_classify_marks_which_mesh_product_the_address_belongs_to() -> None:
    out = entrances.classify(
        lan=[], mesh=["100.64.1.2", "25.3.4.5"], port=8787
    )
    labels = [i["label"] for i in out["vpn"]]
    assert "Tailscale" in labels[0]
    assert "Hamachi" in labels[1]


def test_radmin_range_is_not_offered_as_a_mesh_entry() -> None:
    """`26.0.0.0/8` 是 Radmin：手机端没有客户端，列出来只会误导。"""
    assert entrances.mesh_ipv4_addresses.__doc__  # 契约说明在位
    assert all(
        name not in ("Radmin",) for _net, name in entrances.MESH_NETWORKS
    )
    assert not entrances._mesh_network_of("26.26.192.40")  # noqa: SLF001


def test_mesh_detection_recognises_cgnat_and_hamachi() -> None:
    assert entrances._mesh_network_of("100.103.255.39") == "Tailscale"  # noqa: SLF001
    assert entrances._mesh_network_of("25.1.2.3") == "Hamachi"  # noqa: SLF001
    assert entrances._mesh_network_of("192.0.2.10") == ""  # noqa: SLF001


def test_collect_does_not_list_the_same_address_twice(monkeypatch) -> None:
    """`local_ipv4_addresses()` 有时会把组网地址也认成可用（按源地址路由探测）。"""
    monkeypatch.setattr(
        "backend.app.config.local_ipv4_addresses",
        lambda: ["192.0.2.10", "100.103.255.39"],
    )
    monkeypatch.setattr(
        entrances, "mesh_ipv4_addresses", lambda: ["100.103.255.39"]
    )
    out = entrances.collect(cfg={}, port=8799)
    assert [i["url"] for i in out["lan"]] == ["http://192.0.2.10:8799/"]
    assert [i["url"] for i in out["vpn"]] == ["http://100.103.255.39:8799/"]


def test_collect_survives_address_enumeration_failure(monkeypatch) -> None:
    def boom() -> list[str]:
        raise OSError("no adapters")

    monkeypatch.setattr("backend.app.config.local_ipv4_addresses", boom)
    monkeypatch.setattr(entrances, "mesh_ipv4_addresses", lambda: [])
    out = entrances.collect(cfg={}, port=8799)
    assert out["lan"] == [] and out["vpn"] == []


def test_endpoint_is_anonymous_and_uses_the_port_we_entered_from() -> None:
    """端口取 Host 头——用户就是从那个地址打开页面的。"""
    body = _call(host="100.103.255.39:9123")
    assert body["port"] == 9123
    assert set(body) >= {"port", "lan", "vpn", "cloud"}


def test_endpoint_falls_back_to_configured_port_without_host_header() -> None:
    assert _call(host="")["port"] == 8799


def test_entrances_path_is_public() -> None:
    from backend.app import auth

    assert "/api/system/entrances" in auth.PUBLIC_PATHS


def _call(*, host: str) -> dict:
    """把 handler 跑起来（不启服务器），返回解析后的 JSON 体。"""
    import asyncio
    import json

    from backend.app.core import Ctx, Headers
    from backend.app.handlers import system as h_system

    class _App:
        cfg = {"server.host": "0.0.0.0", "server.port": 8799}

    headers = Headers({"host": host} if host else {})
    ctx = Ctx(method="GET", path="/api/system/entrances", headers=headers, app=_App())
    reply = asyncio.run(h_system.entrances(ctx))
    assert reply.status == 200
    return json.loads(reply.body.decode("utf-8"))
