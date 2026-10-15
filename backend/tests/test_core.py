"""core/ctx.py 与 core/dispatch.py 的测试。

这一层是"两条通路一套 API"的落点，也是**唯一**把异常翻译成响应的地方，
所以测试重点在：路由匹配语义、参数校验语义、以及错误响应不泄漏敏感信息。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from app.core import Ctx, Headers, Reply, Router, iter_body, json_reply  # noqa: E402
from app.errors import (  # noqa: E402
    E1001,
    E1004,
    E1009,
    E1018,
    E2002,
    E3001,
    AppError,
    BadRequest,
    SandboxError,
    Unauthorized,
)


def run(coro):
    return asyncio.run(coro)


def _body(reply: Reply) -> bytes:
    async def collect() -> bytes:
        return b"".join([c async for c in iter_body(reply)])
    return run(collect())


def _json(reply: Reply) -> dict:
    return json.loads(_body(reply).decode("utf-8"))


@pytest.fixture()
def router() -> Router:
    r = Router("test")

    @r.get("/api/system/ping")
    async def ping(ctx: Ctx) -> Reply:
        return json_reply({"ok": True, "via": ctx.via})

    @r.get("/api/items/{id}/thumb")
    async def thumb(ctx: Ctx) -> Reply:
        return json_reply({
            "id": ctx.param_int("id"),
            "size": ctx.q_int("size", 320, minimum=1, maximum=4096),
        })

    @r.post("/api/uploads/init")
    async def init(ctx: Ctx) -> Reply:
        return json_reply(await ctx.json(), status=201)

    @r.delete("/api/items/{id}")
    async def delete(ctx: Ctx) -> Reply:
        return json_reply({"deleted": ctx.param_int("id")})

    @r.get("/api/sandbox")
    async def sandbox_err(ctx: Ctx) -> Reply:
        raise SandboxError("parent_traversal", r"D:\Photos\..\secret.jpg")

    @r.get("/api/oserror")
    async def oserror(ctx: Ctx) -> Reply:
        raise FileNotFoundError(2, "No such file", r"D:\Photos\x.jpg")

    @r.get("/api/sql")
    async def sqlerr(ctx: Ctx) -> Reply:
        import sqlite3
        raise sqlite3.OperationalError("no such column: SELECT * FROM media")

    @r.get("/api/unauth")
    async def unauth(ctx: Ctx) -> Reply:
        raise Unauthorized("token=abc123 expired")

    @r.get("/api/boom")
    async def boom(ctx: Ctx) -> Reply:
        raise RuntimeError("unexpected")

    return r


# --------------------------------------------------------------------------
# Headers
# --------------------------------------------------------------------------


def test_headers_case_insensitive() -> None:
    h = Headers({"Content-Type": "image/webp", "ETag": '"abc"'})
    assert h["content-type"] == "image/webp"
    assert h["CONTENT-TYPE"] == "image/webp"
    assert h.get("etag") == '"abc"'
    assert "Etag" in h


def test_headers_set_and_delete_case_insensitive() -> None:
    h = Headers()
    h["Content-Length"] = "10"
    assert h["content-length"] == "10"
    h["CONTENT-LENGTH"] = "20"
    assert len(h) == 1, "大小写不同的同名头必须覆盖而不是新增"
    del h["Content-Length"]
    assert len(h) == 0


def test_headers_from_pairs_and_copy() -> None:
    h = Headers([("A", "1"), ("B", "2")])
    assert h.as_dict() == {"a": "1", "b": "2"}
    c = h.copy()
    c["a"] = "9"
    assert h["a"] == "1", "copy 必须深拷贝内部字典"


# --------------------------------------------------------------------------
# Ctx 参数校验
# --------------------------------------------------------------------------


def test_q_int_clamps_out_of_range() -> None:
    """limit 超出范围要**钳制**，不能报错。

    手机滚动加载可能传 limit=1000，返回 500 条比 400 让用户卡住好。
    """
    ctx = Ctx("GET", "/x", query={"limit": ["1000"]})
    assert ctx.q_int("limit", 100, minimum=1, maximum=500) == 500
    ctx2 = Ctx("GET", "/x", query={"limit": ["0"]})
    assert ctx2.q_int("limit", 100, minimum=1, maximum=500) == 1


def test_q_int_rejects_non_numeric() -> None:
    """非数字必须报 400：那是客户端 bug，静默纠正会掩盖它。"""
    ctx = Ctx("GET", "/x", query={"limit": ["abc"]})
    with pytest.raises(BadRequest) as ei:
        ctx.q_int("limit", 100, minimum=1, maximum=500)
    assert ei.value.code == "E1001"


def test_q_int_default_when_absent_or_empty() -> None:
    assert Ctx("GET", "/x").q_int("n", 7, minimum=0, maximum=9) == 7
    assert Ctx("GET", "/x", query={"n": [""]}).q_int("n", 7, minimum=0, maximum=9) == 7


def test_q_takes_first_value_only() -> None:
    """?tag=a&tag=b 取第一个，不报错（多值查询是合法的）。"""
    ctx = Ctx("GET", "/x", query={"tag": ["a", "b"]})
    assert ctx.q("tag") == "a"
    assert ctx.q_all("tag") == ["a", "b"]
    assert ctx.q("missing") is None


def test_q_bool_accepts_common_forms() -> None:
    for v in ("1", "true", "TRUE", "yes", "on"):
        assert Ctx("GET", "/x", query={"f": [v]}).q_bool("f") is True
    for v in ("0", "false", "no", "off", ""):
        assert Ctx("GET", "/x", query={"f": [v]}).q_bool("f") is False
    assert Ctx("GET", "/x").q_bool("f", True) is True


def test_param_int_reports_400_not_500() -> None:
    """路径参数非整数是客户端错误，必须是 400。

    若让它冒泡成 ValueError，用户会看到 500"服务器开小差"，
    而真实原因是自己拼错了 URL。
    """
    ctx = Ctx("GET", "/x", params={"id": "abc"})
    with pytest.raises(BadRequest) as ei:
        ctx.param_int("id")
    assert ei.value.status == 400


def test_bearer_token_parsing() -> None:
    assert Ctx("GET", "/x", headers={"Authorization": "Bearer tok"}).bearer_token() == "tok"
    assert Ctx("GET", "/x", headers={"authorization": "bearer tok"}).bearer_token() == "tok"
    assert Ctx("GET", "/x", headers={"Authorization": "Basic xyz"}).bearer_token() is None
    assert Ctx("GET", "/x").bearer_token() is None
    assert Ctx("GET", "/x", headers={"Authorization": "Bearer "}).bearer_token() is None


def test_json_body_parsing() -> None:
    ctx = Ctx("POST", "/x", body=b'{"name":"a.jpg","size":123}')
    assert run(ctx.json()) == {"name": "a.jpg", "size": 123}
    assert run(Ctx("POST", "/x").json()) == {}


def test_json_body_rejects_bad_json() -> None:
    with pytest.raises(BadRequest):
        run(Ctx("POST", "/x", body=b"{not json").json())
    with pytest.raises(BadRequest):
        run(Ctx("POST", "/x", body=b"[1,2,3]").json())   # 顶层必须是对象


def test_body_bytes_limit_enforced_while_streaming() -> None:
    """必须在读取过程中判上限，不能读完再判。

    等读完再判的话，一个 2GB 的恶意请求会先把内存吃满再被拒。
    """
    chunks_read = {"n": 0}

    async def gen():
        for _ in range(100):
            chunks_read["n"] += 1
            yield b"x" * 1024

    ctx = Ctx("POST", "/x", body=gen())
    with pytest.raises(AppError) as ei:
        run(ctx.body_bytes(limit=4096))
    assert ei.value.status == 413
    assert chunks_read["n"] < 100, "应在超限后立即停止读取"


def test_body_bytes_accepts_bytes_body() -> None:
    assert run(Ctx("POST", "/x", body=b"abc").body_bytes()) == b"abc"
    assert run(Ctx("POST", "/x").body_bytes()) == b""


# --------------------------------------------------------------------------
# Reply 构造器
# --------------------------------------------------------------------------


def test_json_reply_is_compact_and_utf8() -> None:
    r = json_reply({"名称": "照片", "n": 1})
    body = _body(r)
    assert "照片".encode("utf-8") in body, "中文必须原样输出，不能转义成 \\uXXXX"
    assert b", " not in body and b": " not in body, "必须用紧凑分隔符省流量"
    assert r.headers["content-type"].startswith("application/json")
    assert r.headers["content-length"] == str(len(body))


def test_etag_quoting_is_normalized() -> None:
    """ETag 必须带引号。调用方手写 f'"{v}"' 很容易漏，格式错会让条件请求失效。"""
    r = Reply()
    r.set_etag("abc")
    assert r.headers["ETag"] == '"abc"'
    r2 = Reply()
    r2.set_etag('"abc"')
    assert r2.headers["ETag"] == '"abc"', "已带引号不应重复加"
    r3 = Reply()
    r3.set_etag("abc", weak=True)
    assert r3.headers["ETag"] == 'W/"abc"'


def test_cache_control_builder() -> None:
    r = Reply().set_cache(31536000, immutable=True)
    assert r.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    r2 = Reply().set_cache(60, private=True)
    assert r2.headers["Cache-Control"] == "private, max-age=60"


def test_empty_reply_has_no_content_length() -> None:
    """204 不能带 Content-Length（RFC 7230 明确禁止）。"""
    r = Reply(status=204)
    assert r.headers.get("content-length") is None


def test_iter_body_yields_bytes_then_stream() -> None:
    assert _body(Reply(body=b"hi")) == b"hi"
    assert _body(Reply(body=None)) == b""

    async def gen():
        yield b"a"
        yield b""
        yield b"b"

    assert _body(Reply(body=gen())) == b"ab", "空 chunk 应被跳过"


# --------------------------------------------------------------------------
# 路由匹配
# --------------------------------------------------------------------------


def test_basic_routing(router: Router) -> None:
    r = run(router.dispatch(Ctx("GET", "/api/system/ping")))
    assert r.status == 200
    assert _json(r) == {"ok": True, "via": "lan"}


def test_path_params_and_query(router: Router) -> None:
    r = run(router.dispatch(Ctx("GET", "/api/items/42/thumb",
                                query={"size": ["1600"]})))
    assert _json(r) == {"id": 42, "size": 1600}


def test_path_param_does_not_cross_segments(router: Router) -> None:
    """{id} 用 [^/]+ 而非 .+，否则 /api/items/1/thumb 会被 /api/items/{id} 吞掉。"""
    r = run(router.dispatch(Ctx("GET", "/api/items/1/thumb")))
    assert r.status == 200
    assert _json(r) == {"id": 1, "size": 320}, "必须命中 thumb 路由"


def test_trailing_slash_tolerated(router: Router) -> None:
    """手机端/浏览器有时带尾斜杠，不该因此 404。"""
    assert run(router.dispatch(Ctx("GET", "/api/system/ping/"))).status == 200


def test_unknown_path_is_404(router: Router) -> None:
    r = run(router.dispatch(Ctx("GET", "/api/nope")))
    assert r.status == 404
    assert _json(r)["error"]["code"] == "E1004"


def test_wrong_method_is_405_with_allow_header(router: Router) -> None:
    """路径存在、方法不对必须 405 而不是 404。

    报 404 会让调试的人去查路径拼写，方向完全错了。
    """
    r = run(router.dispatch(Ctx("POST", "/api/system/ping")))
    assert r.status == 405
    assert "GET" in r.headers["Allow"]
    assert _json(r)["error"]["code"] == "E1018"


def test_head_reuses_get_handler_without_body(router: Router) -> None:
    r = run(router.dispatch(Ctx("HEAD", "/api/system/ping")))
    assert r.status == 200
    assert r.body is None, "HEAD 不能返回正文"
    assert r.headers["content-length"] == "23", "但要保留长度（这是 HEAD 的意义）"


def test_delete_route_registered(router: Router) -> None:
    r = run(router.dispatch(Ctx("DELETE", "/api/items/7")))
    assert _json(r) == {"deleted": 7}


def test_post_json_body_roundtrip(router: Router) -> None:
    body = json.dumps({"name": "a.jpg", "size": 9}).encode()
    r = run(router.dispatch(Ctx("POST", "/api/uploads/init", body=body)))
    assert r.status == 201
    assert _json(r) == {"name": "a.jpg", "size": 9}


def test_router_describe_lists_routes(router: Router) -> None:
    lines = router.describe()
    assert any("/api/items/{id}/thumb" in ln for ln in lines)
    assert len(lines) == len(router.routes)


# --------------------------------------------------------------------------
# 异常收敛 —— 本层最重要的职责
# --------------------------------------------------------------------------


def test_sandbox_error_does_not_leak_path(router: Router) -> None:
    """沙箱错误的 detail 里带着绝对路径，响应体里绝不能出现。

    str(OSError) 在 Windows 上恰好包含绝对路径，这是最现实的泄漏途径。
    """
    r = run(router.dispatch(Ctx("GET", "/api/sandbox")))
    body = _body(r)
    assert r.status == 400
    assert b"software" not in body
    assert b"E:" not in body
    assert b"secret" not in body
    assert _json(r)["error"]["code"] == "E1009"
    assert _json(r)["error"]["user_hint"], "必须有面向用户的中文提示"


def test_oserror_does_not_leak_path(router: Router) -> None:
    r = run(router.dispatch(Ctx("GET", "/api/oserror")))
    body = _body(r)
    assert b"software" not in body and b"E:" not in body
    assert r.status == 500
    assert _json(r)["error"]["code"] == "E3006", "OSError 映射到文件操作失败"


def test_sql_error_does_not_leak_sql(router: Router) -> None:
    r = run(router.dispatch(Ctx("GET", "/api/sql")))
    body = _body(r)
    assert b"SELECT" not in body and b"media" not in body
    assert _json(r)["error"]["code"] == "E3002", "数据库忙/错映射到 E3002"


def test_unexpected_exception_is_internal_without_str(router: Router) -> None:
    """未预期异常绝不能携带 str(exc)。"""
    r = run(router.dispatch(Ctx("GET", "/api/boom")))
    assert r.status == 500
    assert b"unexpected" not in _body(r)
    assert _json(r)["error"]["code"] == "E3001"


def test_error_body_shape_is_complete(router: Router) -> None:
    """错误体结构必须与 docs/04 §1.6 一致。"""
    r = run(router.dispatch(Ctx("GET", "/api/nope")))
    err = _json(r)["error"]
    assert set(err) == {"code", "message", "detail", "retryable", "request_id",
                        "user_hint"}
    assert err["request_id"]


def test_5xx_is_not_cached(router: Router) -> None:
    """缓存住一个 500 会让用户长时间打不开照片。"""
    r = run(router.dispatch(Ctx("GET", "/api/boom")))
    assert r.headers["Cache-Control"] == "no-store"


def test_4xx_is_cacheable_by_default(router: Router) -> None:
    r = run(router.dispatch(Ctx("GET", "/api/nope")))
    assert r.headers.get("cache-control") != "no-store"


def test_request_id_echoed_and_stable(router: Router) -> None:
    ctx = Ctx("GET", "/api/nope")
    rid = ctx.request_id
    r = run(router.dispatch(ctx))
    assert r.headers["X-Request-Id"] == rid
    assert _json(r)["error"]["request_id"] == rid


def test_request_id_present_even_when_middleware_crashes() -> None:
    """中间件崩了也必须带上 request_id —— 报障时唯一能在日志里定位的线索。"""
    r = Router("t")

    @r.get("/x")
    async def x(ctx: Ctx) -> Reply:
        return json_reply({})

    async def bad_mw(ctx: Ctx, nxt):
        raise RuntimeError("mw exploded")

    r.use(bad_mw)
    rep = run(r.dispatch(Ctx("GET", "/x")))
    assert rep.status == 500
    assert rep.headers["X-Request-Id"]


def test_on_error_callback_receives_exception(router: Router) -> None:
    seen: list[str] = []
    router.on_error(lambda ctx, exc: seen.append(type(exc).__name__))
    run(router.dispatch(Ctx("GET", "/api/boom")))
    assert seen == ["RuntimeError"]


def test_on_error_callback_crash_is_swallowed(router: Router) -> None:
    """回调自身抛错不能把响应变成 500 之外的另一种失败。"""
    def bad(ctx, exc):
        raise ValueError("callback boom")
    router.on_error(bad)
    r = run(router.dispatch(Ctx("GET", "/api/boom")))
    assert r.status == 500
    assert _json(r)["error"]["code"] == "E3001"


# --------------------------------------------------------------------------
# 中间件与 CORS
# --------------------------------------------------------------------------


def test_middleware_onion_order(router: Router) -> None:
    """先注册的在外层（同 Express/Django）。

    顺序很重要：请求日志中间件要先注册才能包住鉴权中间件的拒绝响应，
    否则 401 不会被记录。
    """
    order: list[str] = []

    async def mw1(ctx, nxt):
        order.append("1-in")
        rep = await nxt(ctx)
        order.append("1-out")
        return rep

    async def mw2(ctx, nxt):
        order.append("2-in")
        rep = await nxt(ctx)
        order.append("2-out")
        return rep

    router.use(mw1)
    router.use(mw2)   # 先注册的 mw1 在外层
    run(router.dispatch(Ctx("GET", "/api/system/ping")))
    assert order == ["1-in", "2-in", "2-out", "1-out"]


def test_middleware_can_short_circuit(router: Router) -> None:
    """鉴权中间件靠短路实现：不调 nxt 就直接返回。"""
    async def auth(ctx, nxt):
        if not ctx.bearer_token():
            return json_reply({"error": {"code": "E2002"}}, status=401)
        return await nxt(ctx)

    router.use(auth)
    assert run(router.dispatch(Ctx("GET", "/api/system/ping"))).status == 401
    assert run(router.dispatch(Ctx("GET", "/api/system/ping",
                                   headers={"Authorization": "Bearer t"}))).status == 200


def test_cors_disabled_by_default(router: Router) -> None:
    r = run(router.dispatch(Ctx("GET", "/api/system/ping")))
    assert r.headers.get("access-control-allow-origin") is None


def test_cors_echoes_origin_when_present(router: Router) -> None:
    """带凭据的请求不能用 "*"，必须回显来源。"""
    router.enable_cors()
    r = run(router.dispatch(Ctx("GET", "/api/system/ping",
                                headers={"Origin": "http://localhost:5173"})))
    assert r.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
    assert r.headers["Vary"] == "Origin"


def test_cors_exposes_etag_for_conditional_requests(router: Router) -> None:
    """不 expose ETag 的话，浏览器拿不到它，条件请求永远不生效。"""
    router.enable_cors()
    r = run(router.dispatch(Ctx("GET", "/api/system/ping")))
    assert "ETag" in r.headers["Access-Control-Expose-Headers"]


def test_nosniff_header_always_set(router: Router) -> None:
    """照片字节由用户提供；若被嗅探成 HTML 就是存储型 XSS。"""
    r = run(router.dispatch(Ctx("GET", "/api/system/ping")))
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_route_pattern_escaping() -> None:
    """路径里的正则元字符必须被转义，否则会变成通配。"""
    r = Router()
    r.add("GET", "/api/a.b", lambda ctx: json_reply({}))
    assert r.match("GET", "/api/a.b")[0].pattern == "/api/a.b"
    # 未转义时 "a.b" 会匹配 "axb"
    with pytest.raises(Exception):
        r.match("GET", "/api/axb")
