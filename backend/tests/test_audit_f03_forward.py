"""审计 F03：云端**回源之前**就该拦住的东西。

## 这批用例在守什么

云端中继是家里那台电脑的"门卫"，而门卫本身的成本很低——真正贵的是**放
进去**之后：一次隧道往返（打洞未成时还要绕 DERP 中继）、一个文件句柄、
一段被占住的并发额度。所以三条线都必须挡在回源之前：

1. **令牌形状**：`pbc1.` + base64 + 长度的检查必须自己做。云端不认识的
   令牌会被 `_adopt_token_via_agent()` 拿去问电脑——不先做形状检查，任何人
   送 `Bearer a` 都能让家里的电脑跑一次验签。
2. **按来源 IP 的尝试/失败计数**：一枚 32 字节随机令牌猜不动，但"用同一
   个 IP 无限次试"这件事不该被免费提供。
3. **全局并发上限**：单设备 64 个额度管不住"三台设备各挂 60 个慢速 /raw"，
   而云主机只有 2 vCPU。

判据的写法：**看回源次数**（`rig.agent.calls`）而不是只看状态码。401/429
谁都能返回，`calls == 0` 才证明这次攻击没花掉电脑端一毫秒。

## 为什么不是 async 测试

仓库里没装 `pytest-asyncio`（其余用例清一色是同步函数内部 `asyncio.run`），
这里跟着用同一种写法，不为一批用例引入插件依赖。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from starlette.requests import Request
from starlette.responses import Response

from cloud.relay import forward as fwd
from cloud.relay.limiter import ConcurrencyLimiter

DEV = "a" * 32
CLIENT = "c" * 32


def _run(coro):
    return asyncio.run(coro)


class _Agent:
    """记录"被问到电脑端"的次数——这就是回源成本。"""

    def __init__(self) -> None:
        self.calls = 0
        self.tokens: list[str] = []


class _Registry:
    """`Forwarder` 只在公开路径与回源里用到它，这里只要存在即可。"""


class _Rig:
    """一个不连库、不回源的 `Forwarder` + 它的可观测状态。"""

    def __init__(self) -> None:
        # `Forwarder` 用了 `__slots__`，测试状态不能挂在它身上
        self.f = fwd.Forwarder(_Registry(), db=object())
        self.agent = _Agent()
        self.row: dict | None = None
        self.relay_calls = 0

    def handle(self, path: str, *, token: str | None = None,
               ip: str = "203.0.113.7", method: str = "GET"):
        return _run(self.f.handle(_req(path, token=token, ip=ip,
                                       method=method), DEV))


@pytest.fixture()
def rig(monkeypatch):
    rig_ = _Rig()

    # 挂到类上就变成绑定方法，第一个参数是 `Forwarder` 自己
    async def _adopt(_self, token: str, device_id: str):
        rig_.agent.calls += 1
        rig_.agent.tokens.append(token)
        return None

    async def _relay(*a, **kw):
        rig_.relay_calls += 1
        return Response(b"{}", status_code=200,
                        headers={"content-type": "application/json"})

    monkeypatch.setattr(fwd.Forwarder, "_adopt_token_via_agent", _adopt)
    monkeypatch.setattr(fwd.Forwarder, "_relay", _relay)
    monkeypatch.setattr(fwd, "find_client_by_token",
                        lambda db, tok: rig_.row)
    monkeypatch.setattr(fwd, "find_or_adopt_lan_token",
                        lambda db, tok, dev: None)
    return rig_


def _req(path: str, *, token: str | None = None,
         ip: str = "203.0.113.7", method: str = "GET") -> Request:
    headers = [(b"host", b"pb.example.com")]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    return Request({
        "type": "http", "http_version": "1.1", "method": method,
        "scheme": "https", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "headers": headers,
        "client": (ip, 51000), "server": ("pb.example.com", 443),
    })


def _body(resp) -> dict:
    return json.loads(resp.body)


def _err(resp) -> dict:
    return _body(resp)["error"]


GOOD = "pbc1." + "A" * 44 + "." + "B" * 43


# ---------------------------------------------------------------- 形状检查

@pytest.mark.parametrize("junk", [
    "a",                    # 短到不值得问
    "Bearer",               # 只有前缀
    "pbc1",                 # 少了点和载荷
    "pbc1.",                # 载荷为空
    "pbc1." + "!" * 60,     # 非 base64 字符
    "pbc1." + "A" * 3,      # 载荷太短
    "pbc1." + "A" * 44,     # 只有两段（缺签名）
    "pbc1.." + "A" * 44,    # 中间段为空
    "pbc1." + "A" * 6000,   # 载荷长到不合理
])
def test_malformed_token_never_reaches_the_device(rig, junk) -> None:
    resp = rig.handle(f"/d/{DEV}/api/timeline", token=junk)
    assert resp.status_code == 401
    assert _err(resp)["code"] == "E2002"
    assert rig.agent.calls == 0, "垃圾令牌不该让电脑端跑一次验签"
    assert rig.relay_calls == 0


def test_missing_token_is_rejected_without_asking_the_device(rig) -> None:
    resp = rig.handle(f"/d/{DEV}/api/timeline")
    assert resp.status_code == 401
    assert rig.agent.calls == 0


def test_well_formed_but_unknown_token_does_ask_once(rig) -> None:
    """形状对了就必须照旧问电脑端——别把形状检查做成"凡是可疑就拒"。"""
    resp = rig.handle(f"/d/{DEV}/api/timeline", token=GOOD)
    assert resp.status_code == 401
    assert rig.agent.calls == 1
    assert rig.agent.tokens == [GOOD]


# ------------------------------------------------------- 按来源 IP 的计数

def test_auth_attempts_are_capped_per_source_ip(rig) -> None:
    """尝试计数与失败计数是**两个桶**，这里只验证尝试桶会关门。

    把窗口缩到 3 次而不是跑满 600 次：跑满会先撞上"单令牌 120/s"那条线，
    就分不清到底哪条线在挡了。阈值本身在 `test_thresholds_are_sane` 里钉。
    """
    from cloud.relay.limiter import RateLimiter

    rig.f.auth_rate = RateLimiter(3, window=60.0)
    rig.row = {"id": CLIENT, "device_id": DEV}
    for _ in range(3):
        assert rig.handle(f"/d/{DEV}/api/timeline", token=GOOD).status_code == 200
    resp = rig.handle(f"/d/{DEV}/api/timeline", token=GOOD)
    assert resp.status_code == 429
    assert _err(resp)["code"] == "E4001"
    assert resp.headers.get("retry-after")
    assert rig.relay_calls == 3, "被限流的请求不该再敲隧道"


def test_attempt_cap_is_per_ip_not_global(rig) -> None:
    """一个 IP 被限流后，别的 IP 还得能正常访问。"""
    from cloud.relay.limiter import RateLimiter

    rig.f.auth_rate = RateLimiter(2, window=60.0)
    rig.row = {"id": CLIENT, "device_id": DEV}
    for _ in range(3):
        rig.handle(f"/d/{DEV}/api/timeline", token=GOOD, ip="198.51.100.9")
    resp = rig.handle(f"/d/{DEV}/api/timeline", token=GOOD, ip="198.51.100.10")
    assert resp.status_code == 200


def test_repeated_failures_flip_to_429_for_the_same_ip(rig) -> None:
    seen = []
    for _ in range(fwd.AUTH_FAIL_MAX + 1):
        seen.append(rig.handle(f"/d/{DEV}/api/timeline", token=GOOD).status_code)
    assert seen[:fwd.AUTH_FAIL_MAX] == [401] * fwd.AUTH_FAIL_MAX
    assert seen[-1] == 429


# ------------------------------------------------------------- 并发上限

def test_global_cap_rejects_even_when_the_device_has_room(rig) -> None:
    """全局额度用光时，设备自己的 64 个额度还全空也必须拒。"""
    rig.f.inflight_total = ConcurrencyLimiter(1)
    assert rig.f.inflight_total.take(fwd._INFLIGHT_ALL)
    rig.row = {"id": CLIENT, "device_id": DEV}

    resp = rig.handle(f"/d/{DEV}/api/timeline", token=GOOD)
    assert resp.status_code == 429
    assert _err(resp)["code"] == "E4003"
    assert "云端" in _err(resp)["message"]
    assert rig.relay_calls == 0, "并发满了就不该再去敲隧道"


def test_device_cap_rejection_gives_the_global_slot_back(rig) -> None:
    """设备闸门拒绝时，先拿到的全局额度必须还回去。"""
    rig.f.inflight = ConcurrencyLimiter(1)
    rig.row = {"id": CLIENT, "device_id": DEV}
    assert rig.f.inflight.take(DEV)  # 把设备额度占满

    resp = rig.handle(f"/d/{DEV}/api/timeline", token=GOOD)
    assert resp.status_code == 429
    assert _err(resp)["message"] == "请求太密集"
    assert rig.f.inflight_total.count(fwd._INFLIGHT_ALL) == 0, "全局额度漏了"


def test_both_slots_are_released_after_a_successful_relay(rig) -> None:
    rig.row = {"id": CLIENT, "device_id": DEV}
    resp = rig.handle(f"/d/{DEV}/api/timeline", token=GOOD)
    assert resp.status_code == 200
    assert rig.relay_calls == 1
    assert rig.f.inflight.count(DEV) == 0
    assert rig.f.inflight_total.count(fwd._INFLIGHT_ALL) == 0


def test_slots_are_released_when_the_relay_raises(rig, monkeypatch) -> None:
    """隧道中途炸了也要把两个额度还回去，否则额度越用越少。"""
    rig.row = {"id": CLIENT, "device_id": DEV}

    async def _boom(*a, **kw):
        raise RuntimeError("tunnel died")

    monkeypatch.setattr(fwd.Forwarder, "_relay", _boom)
    with pytest.raises(RuntimeError):
        rig.handle(f"/d/{DEV}/api/timeline", token=GOOD)
    assert rig.f.inflight.count(DEV) == 0
    assert rig.f.inflight_total.count(fwd._INFLIGHT_ALL) == 0


def test_token_for_another_device_is_still_403(rig) -> None:
    """形状检查不能把归属校验（`row["device_id"] != 路径`) 挤掉。"""
    rig.row = {"id": CLIENT, "device_id": "b" * 32}
    resp = rig.handle(f"/d/{DEV}/api/timeline", token=GOOD)
    assert resp.status_code == 403
    assert _err(resp)["code"] == "E2003"


# --------------------------------------------------------------- 阈值本身

def test_thresholds_are_sane() -> None:
    from cloud.relay import limiter

    # 全局必须比单设备大得多，否则一台设备就能把全局额度吃干
    assert limiter.MAX_INFLIGHT_TOTAL >= limiter.MAX_INFLIGHT_PER_DEVICE * 4
    # 正常手机的缩略图潮（20–60 个请求/分钟，快滑到 100+）不该被误伤
    assert limiter.AUTH_RATE_PER_MIN >= 300
    # 失败计数要容得下"重新配对前那一刻"的一两次旧令牌失败
    assert limiter.AUTH_FAIL_MAX >= 5
    assert limiter.AUTH_FAIL_WINDOW >= 60
