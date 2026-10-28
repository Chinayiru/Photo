"""静态前端服务测试。

这一层的风险不是"文件发不出去"，而是**路径越界**与**把 API 吃掉**：
`/` 是兜底路由，写松一点就变成一个"能读服务器任意文件"的接口。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.app.core import Ctx
from backend.app.core.dispatch import Router
from backend.app.errors import AppError
from backend.app.static import StaticFiles


def _dist(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><div id=app></div>", encoding="utf-8")
    (d / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (d / "assets" / "index-abc123.css").write_text("body{}", encoding="utf-8")
    (d / "manifest.webmanifest").write_text('{"name":"PB"}', encoding="utf-8")
    (d / "sw.js").write_text("// sw", encoding="utf-8")
    (d / "secret.txt.map").write_text("sourcemap", encoding="utf-8")
    return d


def _ctx(path: str, *, method: str = "GET", headers: dict | None = None) -> Ctx:
    return Ctx(method=method, path=path, headers=headers or {})


def _run(static: StaticFiles, ctx: Ctx):
    return asyncio.run(static(ctx))


def _body(reply) -> bytes:
    async def drain():
        out = bytearray()
        async for c in reply.body:
            out.extend(c)
        return bytes(out)
    return asyncio.run(drain())


def test_disabled_when_no_build(tmp_path) -> None:
    """没有构建产物时，`/` 应该老实回 404，而不是抛异常或返回空 200。"""
    s = StaticFiles(tmp_path / "nope")
    assert s.enabled is False
    with pytest.raises(AppError) as ei:
        _run(s, _ctx("/"))
    assert ei.value.status == 404


def test_index_served_at_root(tmp_path) -> None:
    s = StaticFiles(_dist(tmp_path))
    assert s.enabled
    r = _run(s, _ctx("/"))
    assert r.status == 200
    assert b"<div id=app>" in _body(r)
    assert r.headers["Content-Type"].startswith("text/html")


def test_index_is_not_cached_but_hashed_assets_are(tmp_path) -> None:
    """外壳必须 no-cache，带哈希的资源必须 immutable。

    外壳被缓存住 → 升级后浏览器拿旧 index 去请求已删除的旧 JS → 白屏。
    哈希资源不缓存 → 每次打开都重下几百 KB。
    """
    s = StaticFiles(_dist(tmp_path))
    assert _run(s, _ctx("/")).headers["Cache-Control"] == "no-cache"

    r = _run(s, _ctx("/assets/index-abc123.js"))
    assert r.status == 200
    assert "immutable" in r.headers["Cache-Control"]
    assert "max-age=31536000" in r.headers["Cache-Control"]
    assert r.headers["Content-Type"].startswith("text/javascript")

    r = _run(s, _ctx("/manifest.webmanifest"))
    assert r.headers["Cache-Control"] == "public, max-age=3600"
    assert r.headers["Content-Type"].startswith("application/manifest+json")


def test_unknown_path_falls_back_to_index(tmp_path) -> None:
    """单页应用回退：查看器里刷新页面不能 404。"""
    s = StaticFiles(_dist(tmp_path))
    r = _run(s, _ctx("/viewer/12345"))
    assert r.status == 200
    assert b"<div id=app>" in _body(r)


def test_directory_request_serves_index(tmp_path) -> None:
    s = StaticFiles(_dist(tmp_path))
    r = _run(s, _ctx("/assets/"))
    assert r.status == 200
    assert b"<div id=app>" in _body(r)


@pytest.mark.parametrize("bad", [
    "/../secret.txt",
    "/assets/../../secret.txt",
    "/./../secret.txt",
])
def test_path_traversal_is_rejected(tmp_path, bad) -> None:
    """越界必须回 404，绝不能把 dist 之外的文件发出去。"""
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    s = StaticFiles(_dist(tmp_path))
    # 越界一律 404，**不是**回退到 index —— 回退会把"有人在试探路径穿越"
    # 这件事伪装成一次正常的页面加载，日志里什么都看不出来。
    with pytest.raises(AppError) as ei:
        _run(s, _ctx(bad))
    assert ei.value.status == 404


def test_percent_encoded_traversal_is_harmless(tmp_path) -> None:
    """URL 编码的 `..%2f` 不应被当作路径分隔符处理。

    Starlette 交给我们的已经是**解码后**的路径，所以 `..%2f` 会以
    `../` 的形式出现并被上面的用例拦住。但万一上游行为变化（或二期
    隧道自己解析路径），这里也必须保证不泄漏 —— 它会被当成一个
    普通文件名，落到 dist 里不存在的路径上，走 index 回退。
    关键是**绝不打开 dist 之外的文件**。
    """
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    s = StaticFiles(_dist(tmp_path))
    r = _run(s, _ctx("/..%2fsecret.txt"))
    assert b"TOP SECRET" not in _body(r)


def test_backslash_traversal_is_rejected(tmp_path) -> None:
    """Windows 上反斜杠也是分隔符，只查正斜杠会漏掉。"""
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    s = StaticFiles(_dist(tmp_path))
    with pytest.raises(AppError):
        _run(s, _ctx("/.." + chr(92) + "secret.txt"))


def test_sourcemaps_are_not_served(tmp_path) -> None:
    """`.map` 会暴露完整源码与注释。"""
    s = StaticFiles(_dist(tmp_path))
    with pytest.raises(AppError) as ei:
        _run(s, _ctx("/secret.txt.map"))
    assert ei.value.status == 404


def test_only_get_and_head(tmp_path) -> None:
    """静态资源没有"提交"语义，POST 应该 404 而不是 405。"""
    s = StaticFiles(_dist(tmp_path))
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        with pytest.raises(AppError) as ei:
            _run(s, _ctx("/index.html", method=method))
        assert ei.value.status == 404


def test_etag_304(tmp_path) -> None:
    """条件请求命中回 304 且不带体 —— 省的是手机流量。"""
    s = StaticFiles(_dist(tmp_path))
    r1 = _run(s, _ctx("/assets/index-abc123.js"))
    etag = r1.headers["ETag"]
    assert etag

    r2 = _run(s, _ctx("/assets/index-abc123.js",
                      headers={"If-None-Match": etag}))
    assert r2.status == 304
    assert r2.body is None
    # 304 也要带缓存头，否则浏览器不知道还能继续用多久
    assert "immutable" in r2.headers["Cache-Control"]


def test_router_fallback_does_not_shadow_api(tmp_path) -> None:
    """兜底绝不能吃掉已注册的 API 路由。

    这是本层最危险的失效模式：`/` 兜底一旦匹配优先级高于 API，
    表现是"所有接口都返回 index.html"，而 HTTP 状态码全是 200，
    前端只会报 JSON 解析失败，根本看不出是后端路由问题。
    """
    from backend.app.core import json_reply

    r = Router()
    r.set_fallback(StaticFiles(_dist(tmp_path)))

    @r.get("/api/system/ping")
    async def ping(ctx):
        return json_reply({"pong": True})

    ok = asyncio.run(r.dispatch(_ctx("/api/system/ping")))
    assert ok.status == 200
    assert b"pong" in ok.body

    # 未注册的 API 路径也必须回 404 错误体，而不是前端外壳
    miss = asyncio.run(r.dispatch(_ctx("/api/nope")))
    assert miss.status == 404
    assert b'"error"' in miss.body

    # 前端路由才走兜底
    shell = asyncio.run(r.dispatch(_ctx("/viewer/7")))
    assert shell.status == 200
    assert b"<div id=app>" in _body(shell)


def test_router_405_wins_over_fallback(tmp_path) -> None:
    """路径存在但方法不对时回 405，不让前端外壳用 200 盖住问题。"""
    from backend.app.core import json_reply

    r = Router()
    r.set_fallback(StaticFiles(_dist(tmp_path)))

    @r.post("/api/uploads/init")
    async def init(ctx):
        return json_reply({})

    resp = asyncio.run(r.dispatch(_ctx("/api/uploads/init")))
    assert resp.status == 405
    assert "POST" in resp.headers["Allow"]


# --------------------------------------------------- 安全响应头（2026-10-23）

def test_html_gets_csp_and_framing_headers(tmp_path) -> None:
    """局域网伺服的页面必须带同一套头。

    修之前 `backend/app/static.py` 一个头都没有：手机在家里 Wi-Fi 下走的
    正是这条路径，而云端那条路径的头当时只写在 `cloud/Caddyfile` 里 ——
    两条路径同时裸奔。头现在定义在 `shared/security_headers.py`，两条
    路径共用。
    """
    s = StaticFiles(_dist(tmp_path))
    r = _run(s, _ctx("/"))
    assert r.headers["content-type"].startswith("text/html")
    assert "default-src 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer"


def test_html_csp_uses_configured_lan_port(tmp_path) -> None:
    response = _run(StaticFiles(_dist(tmp_path), lan_port=8877), _ctx("/"))
    csp = response.headers["content-security-policy"]
    assert "http://*:8877" in csp
    assert "http://*:8787" not in csp


def test_assets_get_nosniff_but_no_csp(tmp_path) -> None:
    """静态资源不需要 CSP（CSP 只管文档），但 `nosniff` 必须有。"""
    s = StaticFiles(_dist(tmp_path))
    r = _run(s, _ctx("/assets/index-abc123.js"))
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in r.headers


def test_304_keeps_the_security_headers(tmp_path) -> None:
    """304 也要带头：浏览器缓存的可能是**没有头**的那一版。"""
    s = StaticFiles(_dist(tmp_path))
    first = _run(s, _ctx("/assets/index-abc123.css"))
    again = _run(s, _ctx("/assets/index-abc123.css",
                        headers={"If-None-Match": first.headers["ETag"]}))
    assert again.status == 304
    assert again.headers["x-content-type-options"] == "nosniff"
