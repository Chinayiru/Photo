"""外部审计 F03 / F05 / F06 的回归（2026-10-26）。

三条都是"限制/遮掩写在了纸上，代码里没有"：

- **F03**（CWE-319）部署脚本默认公网 `http://`，电脑端还把它降级成
  `ws://` —— agent 令牌和照片全程明文。现在：部署不给默认值、电脑端
  只允许回环地址用明文。
- **F05**（CWE-532）托盘把 6 位配对码原样写进日志，反代访问日志又把
  `?code=` 原样写盘。现在：码不落日志，反代用 filter 遮盖。
- **F06**（CWE-770）云端每设备 64 个在途额度在**响应头到手**时就还了，
  响应体还在传 —— 慢读 64 条就能无限续。现在额度活到响应体结束。
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from starlette.responses import JSONResponse, StreamingResponse  # noqa: E402

from cloud.relay.forward import _Once, _release_with_body  # noqa: E402
from cloud.relay.limiter import ConcurrencyLimiter  # noqa: E402


async def _drain(it) -> bytes:
    out = bytearray()
    async for chunk in it:
        out.extend(chunk)
    return bytes(out)


# ------------------------------------------------------------------ F06

def test_streaming_slot_is_held_until_body_is_consumed():
    """★ F06 的核心判据：响应头返回后额度**仍然占着**。"""
    lim = ConcurrencyLimiter(1)
    assert lim.take("dev") is True
    release = _Once(lambda: lim.release("dev"))

    async def gen():
        yield b"first"
        yield b"second"

    wrapped = _release_with_body(StreamingResponse(gen()), release)

    # 还没开始传体：额度必须还是占着的，否则第 2 个慢读请求就能进来
    assert lim.take("dev") is False, "响应头一到就还了额度（F06 回归）"

    body = asyncio.run(_drain(wrapped.body_iterator))
    assert body == b"firstsecond"
    assert lim.take("dev") is True, "响应体传完了额度却没还"


def test_streaming_slot_released_when_body_raises():
    lim = ConcurrencyLimiter(1)
    assert lim.take("dev") is True
    release = _Once(lambda: lim.release("dev"))

    async def gen():
        yield b"partial"
        raise RuntimeError("隧道断了")

    wrapped = _release_with_body(StreamingResponse(gen()), release)
    with pytest.raises(RuntimeError):
        asyncio.run(_drain(wrapped.body_iterator))
    assert lim.take("dev") is True, "异常路径把额度漏掉了"


def test_non_streaming_response_releases_immediately():
    """JSON/304 这类无体响应没有生成器可挂，必须当场释放。"""
    lim = ConcurrencyLimiter(1)
    assert lim.take("dev") is True
    release = _Once(lambda: lim.release("dev"))

    _release_with_body(JSONResponse({"ok": True}), release)
    assert lim.take("dev") is True, "无体响应没有释放额度"


def test_handle_no_longer_releases_on_return():
    """源码级断言：`handle()` 里不许再有"return 后立刻 release"。"""
    src = (ROOT / "cloud" / "relay" / "forward.py").read_text(encoding="utf-8")
    assert "_release_with_body(resp, release)" in src
    assert "finally:\n            self.inflight.release(device_id)" not in src
    # `_relay` 内部的 `conn.inflight`（每连接计数）保持原样，别误伤
    assert 'conn, "inflight"' in src


# ------------------------------------------------------------------ F03

def _tunnel(base_url: str):
    from app.tunnel import TunnelClient

    return TunnelClient(None, dispatcher=None, base_url=base_url,
                        agent_token="t")


@pytest.mark.parametrize("base", [
    "http://203.0.113.10:8000",
    "http://example.com",
    "ws://1.2.3.4:8000",
])
def test_agent_refuses_plaintext_websocket_to_non_loopback(base):
    """公网明文基址必须直接拒（连都不许连），不能"降级成 ws:// 照用"。"""
    with pytest.raises(RuntimeError) as ei:
        _tunnel(base)._ws_uri()
    assert "https" in str(ei.value)


def test_agent_never_yields_plaintext_ws_for_a_public_host():
    """没写 scheme 的 `1.2.3.4:8000` 会被补成 https（于是 wss），
    关键是**不许**产出 `ws://` —— 明文握手一条都不许发生。"""
    assert not _tunnel("203.0.113.10:8000")._ws_uri().startswith("ws://")


@pytest.mark.parametrize("base,expect", [
    ("https://relay.example.com", "wss://relay.example.com/agent/ws"),
    ("relay.example.com", "wss://relay.example.com/agent/ws"),
    ("https://relay.example.com/d/abc123", "wss://relay.example.com/agent/ws"),
    ("http://127.0.0.1:8900", "ws://127.0.0.1:8900/agent/ws"),
    ("http://localhost:8900", "ws://localhost:8900/agent/ws"),
])
def test_agent_accepts_https_and_loopback_http(base, expect):
    assert _tunnel(base)._ws_uri() == expect


def test_deploy_script_has_no_public_http_default():
    src = (ROOT / "cloud" / "deploy.ps1").read_text(encoding="utf-8")
    assert '[string]$BaseUrl    = ""' in src, "部署脚本又给了个默认基址"
    assert "http://203.0.113.10:8000" not in src, "默认值还在"


def test_no_historical_password_in_shipped_sources():
    """审计点名的历史明文口令不许再出现在**会被部署/执行的源码**里。

    （测试文件与 `docs/` 里提到这段历史是允许的：口令已于 2026-10-26
    轮换失效，记录保留反而有用。）
    """
    roots = [ROOT / "backend" / "app", ROOT / "cloud", ROOT / "scripts"]
    offenders = []
    for base in roots:
        for p in base.rglob("*"):
            if not p.is_file() or p.suffix not in (".py", ".ps1", ".cmd"):
                continue
            if "tests" in p.parts or p.parent.name == "tests":
                continue
            if "PbCloud2026" in p.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, f"这些文件里还留着历史口令：{offenders}"


# ------------------------------------------------------------------ F05

def test_tray_does_not_log_the_pair_code():
    src = (ROOT / "backend" / "app" / "tray.py").read_text(encoding="utf-8")
    assert "生成配对码 %s" not in src, "配对码又被写进日志了"
    assert "码本身不落日志" in src


@pytest.mark.parametrize("rel", ["cloud/Caddyfile",
                                 "cloud/Caddyfile.windows-domain"])
def test_reverse_proxy_log_redacts_pair_code(rel):
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "replace code REDACTED" in src, f"{rel} 还在原样记录 ?code="
    # 也不许再有裸 `format console` 指令（那种写法会把整个 URI 带上）
    bare = [ln.strip() for ln in src.splitlines()
            if ln.strip() == "format console"]
    assert not bare, f"{rel} 里还有裸 format console"
