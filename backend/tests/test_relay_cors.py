"""中继根路径的 CORS 与「谁来答预检」的回归判据。

## 为什么需要这一组

2026-10-28 的全量测试（`report/PhotoBridge-4b3d1e4-全流程与安全测试报告.md`）
留下两条未闭合项，都是"页面开着 A、请求打到 B"引出的：

* **跨源配对被浏览器拦掉**：页面从 `http://192.168.x.x:8787/` 加载、用户点
  「云端」入口时，配对请求打到 `https://域名/d/<id>/api/pair` —— 那是跨源，
  而中继只在 `/d/**` 的路由里贴 CORS 头，根路径 `/api/**`（账号登录、
  `/api/devices`）一个头都不贴。于是"局域网页面 → 云端账号登录"整条路走不通。
* **预检没人答**：`OPTIONS /api/auth/login` 会落到业务路由上，被答成
  `405 method_not_allowed`。浏览器要求预检必须 2xx，否则真实请求根本不发。

修法（`cloud/relay/headers.py:CorsMiddleware`）：根路径也按同一套局域网源
白名单贴头，预检就地回 204，**一个字节都不进隧道**（预检不带 `Authorization`，
转发进去只会被判 401，前端看到的是"预检失败"）。

## 顺带钉住的两条边界

* 绝不回显任意 `Origin`（`https://evil.example` 一个头都不给）。
* 绝不发 `Access-Control-Allow-Credentials`：发了之后浏览器才允许跨源带上
  `pb_token` cookie，而"局域网里任何一个能投毒的页面"就能带着用户的 cookie
  读相册。前端 `client.ts` 也是显式 `credentials: "omit"`，两边一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from _fake_control import FAKE                      # noqa: E402
from cloud.relay import control as cloud_control    # noqa: E402
from cloud.relay import db as cloud_db              # noqa: E402
from cloud.relay import main as cloud_main         # noqa: E402

LAN_ORIGIN = "http://192.0.2.10:8787"
PUBLIC_ORIGIN = "https://surplus-merger-jacket-ins.trycloudflare.com"


def _dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        "<!doctype html><html><body><div id=\"app\"></div></body></html>",
        encoding="utf-8")
    (root / "assets" / "index-abc.js").write_text("export {};\n",
                                                 encoding="utf-8")
    return root


def _app(tmp_path: Path, *, dist: Path | None = None):
    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    database.init()
    return cloud_main.create_app(
        database, hosts=[], dist=str(dist) if dist is not None else None)


@pytest.fixture(autouse=True)
def _fake_agent(monkeypatch):
    """控制通道换成本地替身，用例不必起真隧道。"""
    FAKE.calls.clear()
    FAKE.online = True
    monkeypatch.setattr(cloud_control, "call_agent", FAKE)


# ------------------------------------------------------------------ 预检


def test_relay_root_preflight_is_answered_with_204(tmp_path: Path) -> None:
    """`OPTIONS /api/<anything>` + 局域网源 → 204，且带全套预检头。

    判据是 2xx 而不是"有 Access-Control-* 头"：浏览器直接看状态码。
    """
    client = TestClient(_app(tmp_path), client=("192.168.0.9", 5000))
    response = client.options("/api/auth/login", headers={
        "origin": LAN_ORIGIN,
        "access-control-request-method": "POST",
        "access-control-request-headers": "authorization,content-type",
    })
    assert response.status_code == 204, response.text
    assert response.headers["access-control-allow-origin"] == LAN_ORIGIN
    assert "origin" in response.headers["vary"].lower()
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "authorization" in response.headers["access-control-allow-headers"]
    # ★ 绝不发：发了才允许跨源带 cookie。
    assert "access-control-allow-credentials" not in response.headers


def test_relay_root_preflight_drops_unknown_origin(tmp_path: Path) -> None:
    """公网源 → 仍是 204（浏览器据此拒发真实请求），但一个 CORS 头都不给。"""
    client = TestClient(_app(tmp_path), client=("198.51.100.7", 5000))
    response = client.options("/api/devices", headers={
        "origin": "https://evil.example",
        "access-control-request-method": "GET",
    })
    assert response.status_code == 204
    assert "access-control-allow-origin" not in response.headers


def test_plain_options_without_acrm_is_not_a_preflight(tmp_path: Path) -> None:
    """普通 `OPTIONS`（查 `Allow`）不该被当成预检吞掉。

    只看方法就会把这类请求也答成 204，客户端拿不到 `Allow`，排查时完全摸不着。
    """
    client = TestClient(_app(tmp_path), client=("198.51.100.8", 5000))
    response = client.options("/api/health", headers={"origin": LAN_ORIGIN})
    assert response.status_code != 204


# -------------------------------------------------------------- 真实请求


def test_relay_root_real_request_gets_cors_headers(tmp_path: Path) -> None:
    """局域网源打中继根 `/api/health` → 真的带上 `Access-Control-Allow-Origin`。

    这条对应的现场：页面从 `http://192.168.x.x:8787/` 加载，用户点「云端」
    入口做账号登录 —— 请求打到中继根，没有这个头浏览器就把响应丢掉。
    """
    client = TestClient(_app(tmp_path), client=("192.168.0.9", 5000))
    response = client.get("/api/health", headers={"origin": LAN_ORIGIN})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == LAN_ORIGIN
    assert "etag" in response.headers["access-control-expose-headers"].lower() \
        or "content-range" in response.headers["access-control-expose-headers"].lower()


def test_relay_root_never_echoes_public_origin(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path), client=("198.51.100.9", 5000))
    response = client.get("/api/health", headers={"origin": "https://evil.example"})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_relay_cors_env_adds_explicit_origin(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """`PB_CLOUD_CORS_ORIGINS` 是"额外点名"，不是替代局域网白名单。"""
    monkeypatch.setenv("PB_CLOUD_CORS_ORIGINS", PUBLIC_ORIGIN)
    client = TestClient(_app(tmp_path), client=("198.51.100.10", 5000))
    for origin, expected in ((PUBLIC_ORIGIN, True), (LAN_ORIGIN, True),
                             ("https://evil.example", False)):
        response = client.get("/api/health", headers={"origin": origin})
        header = response.headers.get("access-control-allow-origin")
        assert (header is not None) is expected, (origin, header)


# ------------------------------------------------- 中继页面的 CSP（不放开明文局域网）


def test_relay_page_csp_has_no_lan_http_wildcard(tmp_path: Path) -> None:
    """中继伺服的页面不带 `http://*:8787`。

    那条通配在 `https://域名` 的页面上用不上（混合内容在 CSP 之前就被拦），
    留着只会让人以为"云端页面能直连局域网" —— 排查时会一直往错的方向找。
    """
    client = TestClient(_app(tmp_path, dist=_dist(tmp_path)),
                        client=("198.51.100.11", 5000))
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    csp = response.headers["content-security-policy"]
    assert "http://*:8787" not in csp
    for directive in ("connect-src 'self' https:", "img-src 'self' blob: data:",
                      "frame-ancestors 'none'", "object-src 'none'"):
        assert directive in csp, (directive, csp)


def test_relay_page_csp_names_extra_connect_from_env(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`PB_CLOUD_PAGE_CONNECT` 点名的来源要落在 `connect-src` **里**。

    ★ 早先的实现是 `csp + " " + add`：整串尾多出一个不属于任何指令的 token，
    浏览器直接忽略 —— 看起来改了 CSP，实际一次都没放行。
    """
    monkeypatch.setenv("PB_CLOUD_PAGE_CONNECT", "https://photos.example.com")
    client = TestClient(_app(tmp_path, dist=_dist(tmp_path)),
                        client=("198.51.100.12", 5000))
    csp = client.get("/").headers["content-security-policy"]
    connect = [p for p in csp.split(";") if p.strip().startswith("connect-src")]
    assert len(connect) == 1, csp
    assert "https://photos.example.com" in connect[0]
