r"""cookie 鉴权的回归测试。

## 这个文件为什么存在

浏览器原生 `<img>` 与 `EventSource` **无法发送自定义请求头**，
所以 `Authorization: Bearer` 传不进去。缩略图走的就是
`<img src="/api/items/{id}/thumb">`，结果是每一张都 401 E2002，
`PhotoCell.vue: onError` 把格子标成 failed 渲染灰块 ——
用户看到的是"照片一张都加载不出来"。

真实浏览器验证（`scripts/verify_browser_e2e.py` 第 [3] 节）实测到过
这个症状：`cells=2 imgs=0 ph=2`，cellHTML 里只有
`<span class="ph">🖼</span>` 没有 `<img>`。

修法是让服务端同时接受 cookie。**这个文件的每一条用例都对应
修法里一个具体的、静默失败的坑**：

* cookie 解析不能整条丢弃（`http.cookies` 遇到坏段会丢整条）
* 两个 `Set-Cookie` 必须独立成行（拼成逗号分隔会被浏览器静默丢弃）
* 明文 HTTP 下不能带 `Secure`（浏览器直接丢弃该 cookie）
* 失效 cookie 必须清掉（否则重新配对后缩略图永远 401）
* 撤销设备时必须清 cookie（否则 `<img>` 继续用旧 cookie）
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.app.auth import (
    CLIENT_COOKIE,
    TOKEN_TTL_DAYS,
    auth_middleware,
    consume_pair_code,
    generate_pair_code,
    require_client,
)
from backend.app.config import Config
from backend.app.core import Ctx, Reply, Router, cookie_header_value
from backend.app.db import Database
from backend.app.handlers import auth as handlers
from backend.app.state import AppState

DEVICE_ID = "dev0000000000001"


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    d.init()
    return d


def _state(tmp_path: Path, db: Database) -> AppState:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    cfg = Config({
        "app": {"mode": "readwrite"},
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}],
        "auth": {"require_pairing": True},
    }, path=data / "config.json", data_dir=data)
    return AppState(cfg=cfg, db=db)


def _router(st: AppState) -> Router:
    r = Router()
    r.use(auth_middleware)
    r.add("POST", "/api/pair", handlers.pair)
    r.add("GET", "/api/clients/me", handlers.me)
    r.add("DELETE", "/api/devices/{client_id}", handlers.revoke_device)
    r.add("GET", "/api/thumb", _thumb)
    r.add("GET", "/api/system/ping", _ping)
    return r


async def _ping(ctx: Ctx) -> Reply:
    from backend.app.core import json_reply

    return json_reply({"pong": True})


async def _thumb(ctx: Ctx) -> Reply:
    """假装是一个缩略图响应，并带上真实的缓存头。"""
    r = Reply(status=200, body=b"\x00" * 8, media_type="image/webp")
    r.headers["Content-Type"] = "image/webp"
    r.set_cache(31536000, immutable=True, private=True)
    r.set("Vary", "Authorization")
    return r


def _ctx(st: AppState, path: str, *, method: str = "GET",
         headers: dict | None = None, params: dict | None = None,
         client_ip: str = "192.168.0.50", body: bytes | None = None) -> Ctx:
    async def _body():
        if body:
            yield body

    return Ctx(method=method, path=path, headers=headers or {},
               params=params or {}, client_ip=client_ip, app=st, body=_body())


def _call(st: AppState, path: str, **kw):
    return asyncio.run(_router(st).dispatch(_ctx(st, path, **kw)))


def _pair(st: AppState, db: Database, *, ip: str = "192.168.0.50"):
    """走完整的配对流程，返回 (client_id, token, reply)。

    配对码必须带上 `device_id`/`device_name` —— 它们是**电脑端**的身份，
    手机配对时校验 `device_id` 是否与自己记住的一致（防止 DHCP 换 IP
    后连到另一台电脑）。少传这两个参数会直接 TypeError。
    """
    import json

    from backend.app.auth import store_pair_code

    code = generate_pair_code()
    store_pair_code(db, code, DEVICE_ID, "测试电脑")
    r = _call(st, "/api/pair", method="POST",
              body=f'{{"code":"{code}"}}'.encode(), client_ip=ip)
    body = json.loads(bytes(r.body).decode())
    return body["client_id"], body["client_token"], r


# --------------------------------------------------------------------------
# 一、cookie 解析（core 层）
# --------------------------------------------------------------------------

def test_cookies_parses_basic() -> None:
    c = Ctx("GET", "/x", headers={"Cookie": "a=1; pb_token=tok; b=2"})
    assert c.cookies() == {"a": "1", "pb_token": "tok", "b": "2"}
    assert c.cookie_token("pb_token") == "tok"


def test_cookies_tolerates_garbage_segments() -> None:
    """★ 坏段跳过、好段保留。

    `http.cookies.SimpleCookie` 遇到非法字符会**静默丢弃整条** Cookie 头，
    于是一个无关的坏 cookie（比如某个统计脚本写的畸形值）就能让鉴权
    失败，而且没有任何日志线索 —— 排障时完全看不出原因。
    """
    c = Ctx("GET", "/x",
            headers={"Cookie": "bad; =; x=y; ; pb_token=tok"})
    assert c.cookies()["pb_token"] == "tok"
    assert c.cookies()["x"] == "y"


def test_cookies_missing_header() -> None:
    assert Ctx("GET", "/x").cookies() == {}
    assert Ctx("GET", "/x").cookie_token("pb_token") is None
    assert Ctx("GET", "/x", headers={"Cookie": ""}).cookies() == {}


def test_cookies_strips_quotes_and_space() -> None:
    c = Ctx("GET", "/x", headers={"Cookie": '  pb_token = "tok" ; a=1'})
    assert c.cookie_token("pb_token") == "tok"


def test_cookie_token_empty_value_is_none() -> None:
    """`pb_token=` 要当成"没有"，不能返回空串。

    空串会一路走到 `_lookup_device`，查不到再抛 401 —— 结果一样，
    但错误文案会变成"token 不存在"，误导排障方向。
    """
    c = Ctx("GET", "/x", headers={"Cookie": "pb_token=; a=1"})
    assert c.cookie_token("pb_token") is None


def test_bearer_token_does_not_fall_back_to_cookie() -> None:
    """★ `bearer_token()` 只认头，绝不回落到 cookie。

    调用方（`require_client`）自己决定接不接受 cookie。把回落藏进
    `bearer_token()` 会让"有没有凭证"出现两种口径 ——
    `test_core.py` 里就有一处用 `if not ctx.bearer_token()` 做判断。
    """
    c = Ctx("GET", "/x", headers={"Cookie": "pb_token=tok"})
    assert c.bearer_token() is None
    assert c.cookie_token("pb_token") == "tok"


# --------------------------------------------------------------------------
# 二、Set-Cookie 序列化（core 层）
# --------------------------------------------------------------------------

def test_cookie_header_value_shape() -> None:
    v = cookie_header_value("pb_token", "abc", 100)
    assert v.startswith("pb_token=abc")
    assert "Path=/" in v
    assert "Max-Age=100" in v
    assert "HttpOnly" in v
    assert "SameSite=Lax" in v


def test_cookie_header_value_never_sets_secure() -> None:
    """★★ 明文 HTTP 下**绝不能**带 `Secure`。

    浏览器会直接丢弃带 Secure 的 cookie，症状是"改了代码但缩略图
    仍然 401"，而 curl / pytest 都看不出来（它们不实现 cookie 策略）。
    """
    assert "Secure" not in cookie_header_value("pb_token", "abc", 100)


def test_cookie_header_value_rejects_injection() -> None:
    """值来自数据库，不能让它注入额外的 cookie 属性。"""
    for bad in ("a;b", "a\r\nb", "a\nb"):
        with pytest.raises(ValueError):
            cookie_header_value("pb_token", bad, 100)


def test_reply_keeps_every_cookie_separate() -> None:
    """★★ 两个 cookie 不能互相覆盖，也不能拼成一行。

    `Reply.headers` 是"头名 → 单值"的映射，写两次 `Set-Cookie`
    只有最后一个能活下来；而用逗号拼成**一个** `Set-Cookie` 行
    违反 RFC 6265，浏览器会把整串当成畸形 cookie 丢掉 ——
    响应 200、body 正常，只有缩略图 401，完全静默。
    """
    r = Reply()
    r.set_cookie("a", "1", max_age=5)
    r.set_cookie("b", "2", max_age=6)
    assert r.cookies == [("a", "1", 5), ("b", "2", 6)]
    # 不能污染 headers
    assert "set-cookie" not in r.headers


def test_clear_cookie_is_max_age_zero() -> None:
    r = Reply()
    r.clear_cookie("pb_token")
    assert r.cookies == [("pb_token", "", 0)]


def test_clear_does_not_remove_earlier_set() -> None:
    """先 set 再 clear 时两条都要在：顺序决定浏览器最终结果。

    浏览器按出现顺序应用，后面的 `Max-Age=0` 生效 ——
    这正是"续期后再撤销"想要的语义。
    """
    r = Reply()
    r.set_cookie("pb_token", "new", max_age=100)
    r.clear_cookie("pb_token")
    assert r.cookies == [("pb_token", "new", 100), ("pb_token", "", 0)]


# --------------------------------------------------------------------------
# 三、适配层：每个 cookie 独立成一行
# --------------------------------------------------------------------------

def test_adapt_reply_emits_separate_set_cookie_lines() -> None:
    """★★ 走真实的 Starlette 适配层，断言 ASGI 里真的是两行。

    这一条是"两个 cookie 独立成行"的**唯一**端到端证据：
    `Reply.cookies` 是个列表不代表传输层没把它们合并。
    """
    from backend.app.transports.local_http import adapt_reply

    r = Reply(status=200, body=b"x", media_type="text/plain")
    r.set_cookie("a", "1", max_age=5)
    r.set_cookie("b", "2", max_age=6)
    resp = adapt_reply(r)
    vals = resp.headers.getlist("set-cookie")
    assert len(vals) == 2, vals
    assert any(v.startswith("a=1") for v in vals)
    assert any(v.startswith("b=2") for v in vals)


def test_adapt_reply_no_cookies_no_header() -> None:
    from backend.app.transports.local_http import adapt_reply

    resp = adapt_reply(Reply(status=200, body=b"x", media_type="text/plain"))
    assert resp.headers.getlist("set-cookie") == []


def test_adapt_reply_cookie_on_204() -> None:
    """204 也要能带 Set-Cookie（撤销设备就是 204 + 清 cookie）。"""
    from backend.app.transports.local_http import adapt_reply

    r = Reply(status=204)
    r.clear_cookie("pb_token")
    resp = adapt_reply(r)
    assert resp.status_code == 204
    assert len(resp.headers.getlist("set-cookie")) == 1


# --------------------------------------------------------------------------
# 四、鉴权：cookie 能过
# --------------------------------------------------------------------------

def test_thumb_works_with_cookie_only(tmp_path: Path, db: Database) -> None:
    """★★ 核心用例：只有 cookie、没有 Authorization 头时缩略图必须 200。

    这就是 `<img src>` 发出的请求。修之前它是 401 E2002。
    """
    st = _state(tmp_path, db)
    _, token, _ = _pair(st, db)
    r = _call(st, "/api/thumb",
              headers={"Cookie": f"{CLIENT_COOKIE}={token}"})
    assert r.status == 200
    assert r.headers["content-type"] == "image/webp"


def test_thumb_without_any_credential_is_401(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    _pair(st, db)
    assert _call(st, "/api/thumb").status == 401


def test_bearer_still_works(tmp_path: Path, db: Database) -> None:
    """不能为了修 cookie 而弄坏原有的 Bearer 通路。"""
    st = _state(tmp_path, db)
    _, token, _ = _pair(st, db)
    r = _call(st, "/api/thumb", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 200


def test_header_wins_over_cookie(tmp_path: Path, db: Database) -> None:
    """★ 头优先于 cookie。

    同一浏览器里可能有多个身份的 cookie（换过电脑），而 JS 请求的
    Bearer 头一定是当前配对的。若 cookie 优先，用户在新电脑上会
    看到旧电脑的照片 —— 更糟的是 `_lookup_device` 查到旧设备后
    `require_client` 成功返回，没有任何报错。
    """
    st = _state(tmp_path, db)
    _, good, _ = _pair(st, db)
    r = _call(st, "/api/thumb", headers={
        "Authorization": f"Bearer {good}",
        "Cookie": f"{CLIENT_COOKIE}=garbage-token",
    })
    assert r.status == 200


def test_cookie_garbage_is_401(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    _pair(st, db)
    r = _call(st, "/api/thumb",
              headers={"Cookie": f"{CLIENT_COOKIE}=nope"})
    assert r.status == 401


def test_revoked_device_cookie_is_403(tmp_path: Path, db: Database) -> None:
    """被撤销的设备用 cookie 也要 403，不能因为换了凭证来源就放过。"""
    st = _state(tmp_path, db)
    client_id, token, _ = _pair(st, db)
    db.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (client_id,))
    r = _call(st, "/api/thumb",
              headers={"Cookie": f"{CLIENT_COOKIE}={token}"})
    assert r.status == 403


# --------------------------------------------------------------------------
# 五、自愈：用 Bearer 头时顺手补 cookie
# --------------------------------------------------------------------------

def test_pair_sets_cookie(tmp_path: Path, db: Database) -> None:
    """配对响应必须下发 cookie。

    ★ 用户手动清过站点数据、或从旧版本升级上来时，localStorage 里的
    token 还在但 cookie 没有。只靠前端 `setTokenCookie` 补是不够的：
    前端可能还没跑（用户在别的浏览器打开），服务端补一次才闭环。
    """
    st = _state(tmp_path, db)
    _, token, r = _pair(st, db)
    assert r.cookies, "配对响应没有下发 cookie"
    name, value, max_age = r.cookies[0]
    assert name == CLIENT_COOKIE
    assert value == token
    assert max_age == TOKEN_TTL_DAYS * 86400  # 默认 90 天


def test_bearer_request_refreshes_cookie(tmp_path: Path, db: Database) -> None:
    """★★ 老前端（只有 Bearer 头）首次请求时服务端补 cookie。

    这是升级路径的关键：用户手机上还是旧版 JS，不会主动写 cookie。
    若服务端不补，缩略图会一直是灰块，直到用户重新配对 ——
    而用户完全不知道要这么做。
    """
    st = _state(tmp_path, db)
    _, token, _ = _pair(st, db)
    r = _call(st, "/api/thumb", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 200
    # ★ 不断言精确等于 `90 * 86400`：cookie 的 Max-Age 现在取自
    #   "该 token 还剩多久"（`cookie_max_age(row)`），而请求总在签发的
    #   几毫秒之后发生，所以是 90 天**减去一点点**。
    #   cookie 绝不能活得比 token 长 —— 那样浏览器会一直带一枚服务端
    #   必然拒绝的凭证，缩略图间歇性变灰块。
    assert _cookie(st, r, token) > 0
    assert _cookie(st, r, token) <= TOKEN_TTL_DAYS * 86400


def test_cookie_request_does_not_re_set(tmp_path: Path, db: Database) -> None:
    """已经是 cookie 通路时不要每个响应都重发 Set-Cookie。

    滚动时间线时每秒几十个缩略图请求，每个都带 `Set-Cookie`
    是纯浪费，且会让浏览器反复重写 cookie 存储。
    """
    st = _state(tmp_path, db)
    _, token, _ = _pair(st, db)
    r = _call(st, "/api/thumb",
              headers={"Cookie": f"{CLIENT_COOKIE}={token}"})
    assert r.status == 200
    assert r.cookies == []


def test_bad_cookie_is_cleared(tmp_path: Path, db: Database) -> None:
    """★★ 失效的 cookie 必须清掉。

    不清的话浏览器会**永远**带着它：用户重新配对拿到新 token 后，
    `<img>` 仍优先发旧 cookie，缩略图继续 401，而 JS 请求用新
    Bearer 头一切正常 —— 症状是"时间线有照片但全是灰块"，
    用户无法理解也无法自助恢复（只有手动清站点数据才行）。
    """
    st = _state(tmp_path, db)
    _pair(st, db)
    r = _call(st, "/api/thumb",
              headers={"Cookie": f"{CLIENT_COOKIE}=stale-token"})
    assert r.status == 401
    assert (CLIENT_COOKIE, "", 0) in r.cookies


def test_revoked_cookie_is_cleared(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    client_id, token, _ = _pair(st, db)
    db.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (client_id,))
    r = _call(st, "/api/thumb",
              headers={"Cookie": f"{CLIENT_COOKIE}={token}"})
    assert r.status == 403
    assert (CLIENT_COOKIE, "", 0) in r.cookies


# --------------------------------------------------------------------------
# 六、撤销自己时清 cookie
# --------------------------------------------------------------------------

def test_self_revoke_clears_cookie(tmp_path: Path, db: Database) -> None:
    """手机撤销自己（或托盘借道手机页面撤销）时必须清 cookie。

    不清的话 `<img>` 仍带着旧 cookie 请求；虽然服务端因 `revoked=1`
    回 403，但重新配对后旧 cookie 依然在，缩略图继续 403 而 JS
    请求正常 —— 还是"照片全是灰块"。
    """
    st = _state(tmp_path, db)
    client_id, token, _ = _pair(st, db)
    r = _call(st, f"/api/devices/{client_id}", method="DELETE",
              headers={"Cookie": f"{CLIENT_COOKIE}={token}"},
              params={"client_id": client_id})
    assert r.status == 204
    assert (CLIENT_COOKIE, "", 0) in r.cookies


def test_revoking_other_device_keeps_cookie(tmp_path: Path, db: Database) -> None:
    """撤销**别的**设备时不能清自己的 cookie。

    清掉的话操作者自己立刻掉线，而他只是想把另一台旧手机移除 ——
    这是"撤销别人结果自己被踢"的经典缺陷。
    """
    st = _state(tmp_path, db)
    _, mine, _ = _pair(st, db, ip="192.168.0.51")
    other_id, _, _ = _pair(st, db, ip="192.168.0.52")
    assert other_id != _device_of(st, mine)
    r = _call(st, f"/api/devices/{other_id}", method="DELETE",
              headers={"Cookie": f"{CLIENT_COOKIE}={mine}"},
              params={"client_id": other_id})
    assert r.status == 204
    assert r.cookies == []


def _device_of(st: AppState, token: str) -> str:
    from backend.app.auth import hash_token

    row = st.db.query_one("SELECT id FROM devices WHERE token_hash = ?",
                          (hash_token(token),))
    assert row is not None
    return row["id"]


def _cookie(st, reply, token: str) -> int:
    """取出补发 cookie 的 Max-Age；没有这条 cookie 则断言失败。"""
    for name, value, max_age in reply.cookies:
        if name == CLIENT_COOKIE and value == token:
            return int(max_age)
    raise AssertionError(
        f"响应里没有补发 {CLIENT_COOKIE}={token[:8]}…：{reply.cookies!r}")


# --------------------------------------------------------------------------
# 七、dispatch 把 ctx 上的 cookie 搬到 reply
# --------------------------------------------------------------------------

def test_dispatch_moves_ctx_cookies_to_reply(tmp_path: Path, db: Database) -> None:
    """★★ 中间件在拿到 Reply **之前** 跑，只能把 cookie 记在 ctx 上。

    少了这个搬运，`require_client` 里所有自愈逻辑都**静默无效** ——
    测试如果只断言 `ctx.set_cookies` 有值就会全绿，而真实响应里
    一个 `Set-Cookie` 都没有。
    """
    st = _state(tmp_path, db)
    _, token, _ = _pair(st, db)
    r = _call(st, "/api/thumb", headers={"Authorization": f"Bearer {token}"})
    assert 0 < _cookie(st, r, token) <= TOKEN_TTL_DAYS * 86400


def test_ctx_cookie_survives_error_reply(tmp_path: Path, db: Database) -> None:
    """★ 错误响应也要带上 Set-Cookie。

    "清掉失效 cookie" 恰恰发生在**抛异常**的路径上（401/403）。
    若搬运写在 `try` 里面、异常直接跳出，清 cookie 就永远发不出去。
    """
    st = _state(tmp_path, db)
    _pair(st, db)
    r = _call(st, "/api/thumb",
              headers={"Cookie": f"{CLIENT_COOKIE}=stale"})
    assert r.status == 401
    assert (CLIENT_COOKIE, "", 0) in r.cookies


def test_public_path_gets_no_cookie(tmp_path: Path, db: Database) -> None:
    """公开路径（ping）不鉴权，不该被塞 cookie。"""
    st = _state(tmp_path, db)
    _pair(st, db)
    r = _call(st, "/api/system/ping")
    assert r.status == 200
    assert r.cookies == []


# --------------------------------------------------------------------------
# 八、require_client 直接调用（不经路由）
# --------------------------------------------------------------------------

def test_require_client_accepts_cookie(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    client_id, token, _ = _pair(st, db)
    ctx = _ctx(st, "/api/thumb",
               headers={"Cookie": f"{CLIENT_COOKIE}={token}"})
    info = require_client(ctx)
    assert info["id"] == client_id
    assert ctx.state["via_cookie"] is True


def test_require_client_via_header_flags_false(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    client_id, token, _ = _pair(st, db)
    ctx = _ctx(st, "/api/thumb",
               headers={"Authorization": f"Bearer {token}"})
    info = require_client(ctx)
    assert info["id"] == client_id
    assert ctx.state["via_cookie"] is False


def test_require_client_error_message_mentions_both(tmp_path: Path, db: Database) -> None:
    """错误 detail 要同时提到两种凭证来源。

    只写"缺少 Authorization 头"会把排障方向指偏 —— 用户/开发者会去
    检查头，而真实原因可能是 cookie 被清了。

    ⚠️ 断言的是 `.detail` 而不是 `str(exc)`：`AppError.__str__` 返回
    `f"{spec.code} {spec.message}"`，即 `"E2002 client_token_invalid"`，
    是给日志用的稳定标识，**不含**具体原因。具体原因在 `.detail` 里，
    且只在开发模式下回传给客户端（`errors.py:159`）。
    """
    from backend.app.errors import Unauthorized

    st = _state(tmp_path, db)
    ctx = _ctx(st, "/api/thumb")
    with pytest.raises(Unauthorized) as ei:
        require_client(ctx)
    detail = ei.value.detail or ""
    assert "cookie" in detail.lower()
    assert "Authorization" in detail
