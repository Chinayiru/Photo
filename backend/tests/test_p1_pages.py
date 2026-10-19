"""P1 收尾：云端托管手机端页面 + 匿名配对 + CORS 白名单。

## 为什么这一组用例必须存在

P1 主体（隧道、relay、双通路状态机）全绿的时候，产品**仍然不可用**：
手机出门后手里只有域名，而 `https://域名/` 是 404、`POST /d/<id>/api/pair`
是 401。两件事叠加的效果是"用户在外面永远进不去自己的相册"，而这一点
没有任何一条既有用例能测出来 —— 它们全都假设"已经拿到令牌、直接打 API"。

这里把三个缺口各钉一遍：

1. 云端要会发页面（`pages.StaticPages`）；
2. 云端要允许匿名 `POST /d/<id>/api/pair`（否则页面能打开也配不上对）；
3. 跨源（页面在局域网源、请求打云端）要回 CORS 头，否则浏览器把
   响应丢掉、状态机判云端不可用。

第 2、3 条都是**安全边界**上的改动，所以同时钉住"没有因此放宽别的"：
匿名只放行配对一个路径一个方法、预检不进隧道、`/panel/**` 仍然到不了
电脑端、CORS 不回显任意来源也不发 `Allow-Credentials`。
"""
from __future__ import annotations

import asyncio
import http.client
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from cloud.relay import cors as cors_mod                            # noqa: E402
from cloud.relay import pages as pages_mod                          # noqa: E402
from cloud.relay.pages import StaticPages                           # noqa: E402

# ---------------------------------------------------------------- 造数


def _make_dist(tmp: Path) -> Path:
    """造一份最小可用的前端产物。"""
    dist = tmp / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body><div id=app></div>"
        "<script src='./assets/index-abc.js'></script></body></html>",
        encoding="utf-8")
    (dist / "assets" / "index-abc.js").write_text(
        "console.log('hi')", encoding="utf-8")
    (dist / "assets" / "style-abc.css").write_text(
        ".a{color:red}", encoding="utf-8")
    (dist / "assets" / "index-abc.js.map").write_text(
        "{}", encoding="utf-8")
    return dist


class _Req:
    """够 `StaticPages` 用的极简请求对象（不引 TestClient）。"""

    __slots__ = ("method", "headers", "scope")

    def __init__(self, method: str = "GET", *, path: str = "/",
                 headers: dict[str, str] | None = None) -> None:
        self.method = method
        self.headers = headers or {}
        self.scope = {"path": path}


# ---------------------------------------------------------------- Cors 单元


def test_lan_origin_accepts_private_and_loopback_only() -> None:
    """只有局域网/回环地址算"局域网源"。

    ★ 这一步是防绕过的关键：`192.168.0.1.evil.com` 是一个**公网域名**，
    只是长得像私有 IP。写成 `host.startswith("192.168.")` 就会把它放进来
    —— 而攻击者完全可以注册这种域名。
    """
    for ok in ("http://192.0.2.10:8787", "http://10.0.0.5",
               "https://172.16.3.9:8443", "http://127.0.0.1:8787",
               "http://localhost:5173", "http://[::1]:8787",
               "https://[fe80::1]"):
        assert cors_mod.is_lan_origin(ok) is True, ok
    for bad in ("https://evil.example.com", "http://192.168.0.1.evil.com",
                "https://pb.example.com", "http://8.8.8.8",
                "null", "", "   ", "file:///etc/passwd",
                "http://192.168.0.1:8787/path"):
        assert cors_mod.is_lan_origin(bad) is False, bad


def test_cors_headers_never_echo_arbitrary_origin_or_allow_credentials() -> None:
    """白名单不命中就一个 CORS 头都不回，且永远不发 `Allow-Credentials`。

    发 `Access-Control-Allow-Credentials: true` 之后，浏览器会允许跨源
    请求带上 `pb_token` cookie —— 那等于让局域网里任何一个能投毒的页面
    拿用户的 cookie 读相册（一期审计 F1 的形态）。前端 `client.ts` 也是
    `credentials: "omit"`，两边必须一致。
    """
    hit = cors_mod.cors_headers("http://192.0.2.10:8787")
    assert hit["access-control-allow-origin"] == "http://192.0.2.10:8787"
    assert hit["vary"] == "Origin"
    assert "access-control-allow-credentials" not in hit

    assert cors_mod.cors_headers("https://evil.example.com") == {}
    assert cors_mod.cors_headers("") == {}

    # 显式白名单里的公网来源可用，但同样不发 credentials
    extra = frozenset({"https://pb.example.com"})
    got = cors_mod.cors_headers("https://pb.example.com", allowed=extra)
    assert got["access-control-allow-origin"] == "https://pb.example.com"
    assert "access-control-allow-credentials" not in got
    # 白名单是**精确**的，不做后缀/前缀匹配
    assert cors_mod.cors_headers("https://pb.example.com.evil.com",
                                 allowed=extra) == {}


def test_preflight_headers_are_only_for_allowed_origins() -> None:
    pre = cors_mod.preflight_headers("http://192.0.2.10:8787")
    assert pre["access-control-allow-methods"].startswith("GET")
    assert "authorization" in pre["access-control-allow-headers"].lower()
    assert pre["access-control-max-age"] == str(cors_mod.PREFLIGHT_MAX_AGE)
    assert "access-control-allow-credentials" not in pre

    assert cors_mod.preflight_headers("https://evil.example.com") == {}


def test_is_preflight_needs_all_three_conditions() -> None:
    """方法、`Origin`、`Access-Control-Request-Method` 缺一不可。

    只看 `OPTIONS` 会把"客户端查 `Allow` 头"这种普通预检也截下来，
    那是另一个语义（我们想让它进路由、回 405 + `Allow`）。
    """
    full = _Req("OPTIONS", headers={
        "origin": "http://192.0.2.10:8787",
        "access-control-request-method": "GET"})
    assert cors_mod.is_preflight(full) is True

    assert cors_mod.is_preflight(_Req("OPTIONS", headers={"origin": "x"})) is False
    assert cors_mod.is_preflight(_Req("OPTIONS", headers={
        "access-control-request-method": "GET"})) is False
    assert cors_mod.is_preflight(_Req("GET", headers={
        "origin": "http://127.0.0.1", "access-control-request-method": "GET"})) is False


# ---------------------------------------------------------------- pages 单元


def test_pages_serves_index_and_hashed_assets_with_the_right_cache(
        tmp_path: Path) -> None:
    """外壳 `no-cache`、带哈希的资源一年 `immutable`。

    外壳被缓存住会在升级后白屏（浏览器拿旧 index 请求已删除的旧
    `assets/index-*.js`）—— 一期踩过同一个坑。
    """
    sp = StaticPages(_make_dist(tmp_path))
    assert sp.enabled is True

    import asyncio

    idx = asyncio.run(sp(_Req(path="/")))
    assert idx.status_code == 200
    assert idx.headers["cache-control"] == pages_mod.PAGE_CACHE
    assert idx.headers["x-content-type-options"] == "nosniff"
    assert "text/html" in idx.headers["content-type"]

    js = asyncio.run(sp(_Req(path="/assets/index-abc.js")))
    assert js.status_code == 200
    assert js.headers["cache-control"] == pages_mod.ASSET_CACHE
    assert "javascript" in js.headers["content-type"]
    # etag 必须**正好一层**引号：多一层会让 304 永不生效
    etag = js.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')
    assert not etag.startswith('""')


def test_pages_supports_conditional_requests(tmp_path: Path) -> None:
    import asyncio

    sp = StaticPages(_make_dist(tmp_path))
    first = asyncio.run(sp(_Req(path="/assets/index-abc.js")))
    etag = first.headers["etag"]

    for inm in (etag, f"W/{etag}", f'"other", {etag}', "*"):
        got = asyncio.run(sp(_Req(path="/assets/index-abc.js",
                                  headers={"if-none-match": inm})))
        assert got.status_code == 304, inm
    miss = asyncio.run(sp(_Req(path="/assets/index-abc.js",
                              headers={"if-none-match": '"nope"'})))
    assert miss.status_code == 200


def test_pages_refuses_to_escape_the_dist_directory(tmp_path: Path) -> None:
    """`..` 段与路径穿越必须被挡（不依赖框架的解码行为）。

    这里的 `secret.txt` 就在 `dist` 的**同级**目录里 —— 只要有一处按
    字符串前缀判断就会漏出去。
    """
    dist = _make_dist(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET", encoding="utf-8")
    sp = StaticPages(dist)

    for rel in ("../secret.txt", "assets/../../secret.txt", "/../secret.txt",
                "..", "../"):
        assert sp._resolve(rel) is None, rel          # noqa: SLF001
    # 正常路径仍然可解析
    assert sp._resolve("assets/index-abc.js") is not None   # noqa: SLF001


def test_pages_never_serves_source_maps_or_falls_back_for_extensions(
        tmp_path: Path) -> None:
    """`.map` 不服务；带扩展名的未知路径**不**回退到 index.html。

    不回退的理由有两层：给一份 HTML 去响应 `<script>` 请求会让浏览器报
    "资源被解释为 script 但 MIME 是 text/html"；而把任意拼错的路径都答成
    200 会让扫描器认为站点"什么都存在"。
    """
    import asyncio

    sp = StaticPages(_make_dist(tmp_path))
    # 文件确实存在，但 .map 被策略挡掉
    assert (tmp_path / "dist" / "assets" / "index-abc.js.map").is_file()
    blocked = asyncio.run(sp(_Req(path="/assets/index-abc.js.map")))
    assert blocked.status_code == 404

    for path in ("/missing.png", "/assets/gone.js", "/favicon.ico"):
        got = asyncio.run(sp(_Req(path=path)))
        assert got.status_code == 404, path
        assert "text/plain" in got.headers["content-type"]

    # 无扩展名的未知路径回退到 index.html（单页应用刷新不 404）
    spa = asyncio.run(sp(_Req(path="/some/deep/route")))
    assert spa.status_code == 200
    assert "text/html" in spa.headers["content-type"]


def test_pages_disabled_when_dist_missing(tmp_path: Path) -> None:
    """没有构建产物时**不抛异常**，只回 404。

    云端路由表把它挂成最后的兜底，抛异常会变成 500 —— 而"这台云端没带
    前端产物"与"这个路径不存在"对用户是同一件事。更重要的是：`cloud/`
    应该能在没有前端的机器上单独跑（压测 relay 时就是这样）。
    """
    import asyncio

    sp = StaticPages(tmp_path / "nope")
    assert sp.enabled is False
    assert sp.root is None
    got = asyncio.run(sp(_Req(path="/")))
    assert got.status_code == 404


def test_pages_rejects_non_read_methods(tmp_path: Path) -> None:
    import asyncio

    sp = StaticPages(_make_dist(tmp_path))
    for method in ("POST", "PUT", "DELETE"):
        got = asyncio.run(sp(_Req(method, path="/assets/index-abc.js")))
        assert got.status_code == 404, method


# ------------------------------------------------------- 端到端：真 uvicorn

def _http(port: int, method: str, path: str,
          headers: dict[str, str] | None = None,
          body: bytes | None = None) -> tuple[int, bytes, dict[str, str]]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        payload = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, payload, hdrs
    finally:
        conn.close()


class _LiveRig:
    """把整个 P1 装置放进**后台线程的独立事件循环**，供同步用例使用。

    ★ 不能和用例跑在同一个循环里。云端 uvicorn 就活在那个循环上，而这里
    的 HTTP 请求是**同步**的（`http.client`，阻塞）—— 两者挤在一个循环里
    会互相等死（`WinError 10061 目标计算机积极拒绝`）。`test_p1_tunnel`
    用 `asyncio.to_thread` 绕开这一点，但那要求用例本身是协程；这里保持
    同步写法，把装置挪到线程里。`test_p1_acceptance._RigThread` 是同一个
    形状，理由相同。

    另一个坑：不能借 `test_p1_tunnel._run()` 来起 —— 那个函数的 `finally`
    会把 `_ACTIVE` 里所有 rig 全拆掉，于是刚起好就被关了。
    """

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.rig: Any = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: Any = None
        self._ready = threading.Event()
        self.error: BaseException | None = None

    # ---- 线程入口 ----

    def start(self, timeout: float = 120.0) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="p1-pages")
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError(f"P1 装置启动超时：{self.error!r}")
        if self.error is not None:
            raise RuntimeError(f"P1 装置启动失败：{self.error!r}")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            from backend.tests.test_p1_tunnel import _Rig  # noqa: PLC0415

            self.rig = _Rig(self.tmp, tunnel=True)
            loop.run_until_complete(self.rig.start())
            loop.run_until_complete(self.rig.wait_tunnel())
            loop.run_until_complete(self.rig.apair())
        except BaseException as exc:        # noqa: BLE001
            self.error = exc
            self._ready.set()
            return
        self._ready.set()
        loop.run_forever()

    # ---- 供用例调用 ----

    def call(self, coro_fn, *, timeout: float = 60.0):
        """在装置循环里跑一个协程并等结果。"""
        assert self.loop is not None
        fut = asyncio.run_coroutine_threadsafe(coro_fn(self.rig), self.loop)
        return fut.result(timeout)

    @property
    def cloud_port(self) -> int:
        return int(self.rig.cloud_port)

    @property
    def device_id(self) -> str:
        return str(self.rig.device_id)

    @property
    def client_token(self) -> str:
        return str(self.rig.client_token)

    def close(self) -> None:
        if self.loop is None or self.rig is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.rig.stop(), self.loop).result(60)
        except Exception:                   # noqa: BLE001
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread is not None:
            self._thread.join(10)


@pytest.fixture()
def live_rig(tmp_path: Path):
    """起一套真装配（真 uvicorn 云端 + 真隧道 + 真业务路由）。"""
    rig = _LiveRig(tmp_path)
    rig.start()
    try:
        yield rig
    finally:
        rig.close()


def test_cloud_serves_the_app_shell_at_root_and_device_prefix(
        live_rig) -> None:
    """`GET /` 与 `GET /d/<id>/` 都要给出同一个外壳。

    根路径是"手机出门打开域名"的入口；`/d/<id>/` 是 `public_base` 拼出来
    的形状（`backend/app/tunnel.py:public_base_for`）。两个都必须能用：
    `frontend/vite.config.ts` 的 `base: "./"` 让 index.html 里的
    `./assets/...` 在前者解析成 `/assets/...`、在后者解析成
    `/d/<id>/assets/...`，同一份产物两种前缀都对。
    """
    import asyncio
    import re

    async def main():
        root = await asyncio.to_thread(_http, live_rig.cloud_port, "GET", "/")
        dev = await asyncio.to_thread(
            _http, live_rig.cloud_port, "GET", f"/d/{live_rig.device_id}/")
        return root, dev

    root, dev = asyncio.run(main())
    for status, body, hdrs in (root, dev):
        assert status == 200, (status, body[:80])
        assert "text/html" in hdrs["content-type"], hdrs
        assert b'id="app"' in body or b"id=app" in body, body[:120]

    # ★ 资源名从 index.html 里现取，不写死。
    #   构建产物是 `assets/index-<hash>.js`，hash 每次改前端都会变 ——
    #   写死的话这条用例会在"某次纯前端改动之后"变红，且红得毫无信息量
    #   （上一次就是这么红的：`index-C8PdhKZN.js` → `index-Dn27c4np.js`）。
    #   顺带这条比原来更强：它验的正是"index.html 引用的那个文件真的取得到"。
    names = sorted(set(re.findall(rb"\./(assets/[^\"']+\.js)", root[1])))
    assert names, root[1][:400]

    async def fetch_assets():
        out = []
        for raw in names:
            rel = raw.decode("utf-8")
            out.append((
                await asyncio.to_thread(
                    _http, live_rig.cloud_port, "GET", "/" + rel),
                await asyncio.to_thread(
                    _http, live_rig.cloud_port, "GET",
                    f"/d/{live_rig.device_id}/{rel}"),
            ))
        return out

    pairs = asyncio.run(fetch_assets())
    for (status, _body, hdrs), (dstatus, _dbody, dhdrs) in pairs:
        assert status == 200, status
        assert dstatus == 200, dstatus
        assert "javascript" in hdrs["content-type"], hdrs
        assert "javascript" in dhdrs["content-type"], dhdrs
    # 同源页面不带隧道标记
    assert "x-pb-tunnel" not in root[2]


def test_cloud_pages_do_not_require_a_token(live_rig) -> None:
    """页面与静态资源必须匿名可取 —— 否则手机上连配对界面都打不开。"""
    import asyncio

    async def main():
        return {
            "/": await asyncio.to_thread(_http, live_rig.cloud_port, "GET", "/"),
            "/favicon.ico": await asyncio.to_thread(
                _http, live_rig.cloud_port, "GET", "/favicon.ico"),
            "/.env": await asyncio.to_thread(
                _http, live_rig.cloud_port, "GET", "/.env"),
            "/wp-login.php": await asyncio.to_thread(
                _http, live_rig.cloud_port, "GET", "/wp-login.php"),
        }

    got = asyncio.run(main())
    assert got["/"][0] == 200
    # 带扩展名的未知路径一律 404，扫描器拿不到"这个站什么都有"
    for path in ("/favicon.ico", "/.env", "/wp-login.php"):
        assert got[path][0] == 404, path
        assert b"not found" in got[path][1].lower()


def test_unknown_api_path_is_not_swallowed_by_the_spa(live_rig) -> None:
    """`/api/**` 不存在时必须回 404 纯文本，不能被单页回退吃成 200 HTML。

    被吃掉的症状是前端 `JSON.parse("<html>")` 抛异常，却看不出是"接口
    没实现"还是"接口返回了怪东西"。`docs/04` 规划的 `/api/clients/me`
    在云端就处于这种状态。
    """
    import asyncio

    async def main():
        return await asyncio.to_thread(
            _http, live_rig.cloud_port, "GET", "/api/clients/me")

    status, body, hdrs = asyncio.run(main())
    assert status == 404, (status, body[:120])
    assert "text/html" not in hdrs.get("content-type", "")
    assert b"<!doctype" not in body.lower()


def test_pairing_works_on_the_cloud_origin(live_rig) -> None:
    """P1 的核心可用性：**在云端源上**用 6 位码换到令牌。

    ★ 这条是"手机出门后能不能用"的唯一判据。改动前这里必 401
    `E2002 缺少配对令牌`，而局域网源上的同一个请求 201 —— 于是用户
    在家扫一次码就再也离不开家。

    配对码本身仍由电脑端生成（`/panel/pair-code`，只走局域网），云端
    只负责把 `POST /d/<id>/api/pair` 匿名放行并转发。
    """
    import asyncio

    async def main():
        code = live_rig.call(lambda r: r.lan_pair_code())
        body = json.dumps({"code": code}).encode()
        return await asyncio.to_thread(
            _http, live_rig.cloud_port, "POST",
            f"/d/{live_rig.device_id}/api/pair",
            {"content-type": "application/json",
             "x-pb-client": "cloud-e2e"},
            body)

    status, payload, hdrs = asyncio.run(main())
    assert status == 201, (status, payload[:300])
    data = json.loads(payload)
    assert data["client_token"]
    # ★ 配对响应里的 `device_id` 是**电脑本地**的 16 位 id，不是云端分配给
    #   路径的那个 32 位 id（`backend/app/tunnel.py:cloud_device_id` 与
    #   `state.device_id` 是两个东西）。混用会让握手 `device_id_mismatch`。
    assert data["device_id"] == live_rig.rig.state.device_id
    assert data["device_id"] != live_rig.device_id
    # 响应确实来自电脑端（走隧道）
    assert hdrs.get("x-pb-tunnel") == "relay"

    # 拿到的令牌立刻能用
    async def usetoken():
        return await asyncio.to_thread(
            _http, live_rig.cloud_port, "GET",
            f"/d/{live_rig.device_id}/api/timeline",
            {"authorization": f"Bearer {data['client_token']}"})

    st2, body2, _ = asyncio.run(usetoken())
    assert st2 == 200, (st2, body2[:200])
    assert len(json.loads(body2)["items"]) == 3


def test_anonymous_pairing_is_the_only_widened_path(live_rig) -> None:
    """匿名只放行 `POST /api/pair`，其他一律仍然要令牌。

    白名单的代价必须被精确控制住：一旦有人把 `_is_public` 改成"路径以
    `/api/` 开头就放行"，整条隧道就变成公网可匿名驱动的代理。
    """
    import asyncio

    async def main():
        out = {}
        paths = {
            ("GET", "/api/timeline"): None,
            ("GET", "/api/clients/me"): None,
            ("POST", "/api/pair"): b'{"code":"000000"}',
            ("PUT", "/api/pair"): b'{"code":"000000"}',
            ("DELETE", "/api/pair"): None,
            ("GET", "/api/pair"): None,
            ("POST", "/api/system/ping"): b"{}",
        }
        for (method, path), body in paths.items():
            out[(method, path)] = await asyncio.to_thread(
                _http, live_rig.cloud_port, method,
                f"/d/{live_rig.device_id}{path}",
                {"content-type": "application/json"}, body)
        # 匿名 ping 仍要能用（状态机探针）
        out[("GET", "/api/system/ping")] = await asyncio.to_thread(
            _http, live_rig.cloud_port, "GET",
            f"/d/{live_rig.device_id}/api/system/ping")
        return out

    got = asyncio.run(main())
    # 未鉴权的业务读 → 401
    for key in (("GET", "/api/timeline"), ("GET", "/api/clients/me")):
        assert got[key][0] == 401, (key, got[key][0])
    # POST 配对是唯一被放开的非只读方法
    assert got[("POST", "/api/pair")][0] in (400, 401), got[("POST", "/api/pair")][0]
    # 同一路径的其它方法**不能**跟着免鉴权
    for key in (("PUT", "/api/pair"), ("DELETE", "/api/pair"),
                ("GET", "/api/pair")):
        assert got[key][0] in (401, 405), (key, got[key][0])
    # 探针仍匿名可用
    assert got[("GET", "/api/system/ping")][0] == 200


def test_cors_headers_are_returned_for_lan_origins(live_rig) -> None:
    """局域网源拿得到 CORS 头，公网源拿不到（也不回显）。"""
    import asyncio

    async def main():
        out = {}
        for origin in ("http://192.0.2.10:8787", "http://localhost:5173",
                       "https://evil.example.com",
                       "http://192.168.0.1.evil.com"):
            out[origin] = await asyncio.to_thread(
                _http, live_rig.cloud_port, "GET",
                f"/d/{live_rig.device_id}/api/system/ping",
                {"origin": origin})
        return out

    got = asyncio.run(main())
    for origin in ("http://192.0.2.10:8787", "http://localhost:5173"):
        status, _body, hdrs = got[origin]
        assert status == 200, origin
        assert hdrs.get("access-control-allow-origin") == origin, origin
        assert hdrs.get("vary") == "Origin"
        assert "access-control-allow-credentials" not in hdrs

    for origin in ("https://evil.example.com", "http://192.168.0.1.evil.com"):
        _status, _body, hdrs = got[origin]
        assert "access-control-allow-origin" not in hdrs, (origin, hdrs)


def test_preflight_never_enters_the_tunnel(live_rig) -> None:
    """`OPTIONS` 预检由云端就地回 204，一个字节都不进隧道。

    预检**不带** `Authorization`（浏览器规范如此）。转发进去只会被电脑端
    判 401，于是真实请求根本不会发出、前端看到"预检失败"。
    `x-pb-tunnel` 缺失就是"没进隧道"的决定性证据。
    """
    import asyncio

    async def main():
        return await asyncio.to_thread(
            _http, live_rig.cloud_port, "OPTIONS",
            f"/d/{live_rig.device_id}/api/uploads/init",
            {"origin": "http://192.0.2.10:8787",
             "access-control-request-method": "POST",
             "access-control-request-headers": "authorization, content-type"})

    status, body, hdrs = asyncio.run(main())
    assert status == 204, status
    assert body == b""
    assert hdrs.get("access-control-allow-origin") == "http://192.0.2.10:8787"
    assert "POST" in hdrs.get("access-control-allow-methods", "")
    assert "authorization" in hdrs.get("access-control-allow-headers", "").lower()
    assert "x-pb-tunnel" not in hdrs, hdrs.get("x-pb-tunnel")


def test_preflight_from_a_bad_origin_gets_no_cors_headers(live_rig) -> None:
    """恶意源的预检也回 204，但**不带**任何 `Access-Control-*` 头。

    回 403 会把"跨源被拒"与"云端拒绝服务"混在一起，前端无法区分；
    而不回头浏览器自己就会拒掉真实请求，这才是正确的位置。
    """
    import asyncio

    async def main():
        return await asyncio.to_thread(
            _http, live_rig.cloud_port, "OPTIONS",
            f"/d/{live_rig.device_id}/api/timeline",
            {"origin": "https://evil.example.com",
             "access-control-request-method": "GET"})

    status, body, hdrs = asyncio.run(main())
    assert status == 204
    assert body == b""
    assert not any(k.startswith("access-control-") for k in hdrs), hdrs


def test_panel_still_unreachable_over_the_tunnel(live_rig) -> None:
    """收尾复查：三个缺口都补上之后，`/panel/**` 仍然到不了电脑端。

    页面路由接管了 `/d/<id>/**` 的非 `/api` 前缀，所以这些请求现在回
    index.html。判据不是状态码，而是**没有 `x-pb-tunnel`**（那才是
    "由电脑端产生"的标记）—— 电脑端 router 里 `/panel/*` 是**存在**的，
    真被转发进去同样会回 200，只有这个头与响应体形状能区分。

    越界写法（`/api/../panel/...`）看着以 `/api` 开头、会进转发器，
    必须在那里被 `_clean_path` 拒掉。
    """
    import asyncio

    token = live_rig.client_token

    async def main():
        out = {}
        for path in ("/panel/pair-code", "/panel/devices", "/panel/audit",
                     "/api/../panel/pair-code", "/api/./../panel/audit"):
            out[path] = await asyncio.to_thread(
                _http, live_rig.cloud_port, "GET",
                f"/d/{live_rig.device_id}{path}",
                {"authorization": f"Bearer {token}"})
        return out

    got = asyncio.run(main())
    for path in ("/panel/pair-code", "/panel/devices", "/panel/audit"):
        status, body, hdrs = got[path]
        assert status == 200, (path, status)
        assert "x-pb-tunnel" not in hdrs, path
        assert b"<!doctype" in body.lower(), path
        # 页面里不该出现任何令牌痕迹
        assert b"pb.token" not in body, path

    for path in ("/api/../panel/pair-code", "/api/./../panel/audit"):
        status, body, _hdrs = got[path]
        assert status == 403, (path, status, body[:200])
        assert json.loads(body)["error"]["code"] == "E1009"
        assert json.loads(body)["error"]["detail"] is None, path
