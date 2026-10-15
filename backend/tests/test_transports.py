"""传输层测试：Range/MIME 语义 + 真实 HTTP 端到端。

端到端部分是重点：它验证整条链路
`HTTP → adapt_request → Ctx → Router → Reply → adapt_reply → HTTP`，
而不是各部件单独可用。若只在单元层面测 Ctx 与 Reply，
适配器里把 `Content-Type` 设两次、或把 204 带上 Content-Length 这类
协议违规是测不出来的。
"""
from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "backend"))

from app.config import Config  # noqa: E402
from app.core import Ctx, Router, json_reply, stream_reply  # noqa: E402
from app.errors import BadRequest, SandboxError  # noqa: E402
from app.transports import (  # noqa: E402
    RangeNotSatisfiable,
    content_range_header,
    content_type_for,
    parse_range,
    range_length,
    unsatisfied_range_header,
)
from app.transports.base import DEFAULT_MIME  # noqa: E402


# --------------------------------------------------------------------------
# parse_range
# --------------------------------------------------------------------------


@pytest.mark.parametrize("header,size,want", [
    (None, 1000, None),
    ("", 1000, None),
    ("bytes=0-", 1000, (0, 999)),
    ("bytes=500-999", 1000, (500, 999)),
    ("bytes=0-0", 1000, (0, 0)),
    ("bytes=999-", 1000, (999, 999)),
    ("bytes=0-99999", 1000, (0, 999)),      # 上界超长钳到末尾
    ("bytes=  0-99  ", 1000, (0, 99)),      # 容忍空白
    ("BYTES=0-9", 1000, (0, 9)),            # 单位大小写不敏感
    ("items=0-5", 1000, None),              # 非 bytes 单位 → 忽略（回 200 全文）
])
def test_parse_range_valid(header, size, want) -> None:
    assert parse_range(header, size) == want


@pytest.mark.parametrize("header,size", [
    ("bytes=-500", 1000),      # 单独测，因为期望值特殊
])
def test_parse_range_suffix(header, size) -> None:
    """`bytes=-500` 是**最后 500 字节**，不是 0..500。

    把它当成 "0 到 500" 是经典 bug：下载器做断点续传时请求最后一段，
    服务器却从头给，客户端会拼出损坏的文件。
    """
    assert parse_range(header, 1000) == (500, 999)


def test_parse_range_suffix_larger_than_file() -> None:
    """`bytes=-2000` 而文件只有 1000 字节 → 整个文件。"""
    assert parse_range("bytes=-2000", 1000) == (0, 999)


@pytest.mark.parametrize("header,size", [
    ("bytes=1000-", 1000),        # start 越界
    ("bytes=2000-3000", 1000),
    ("bytes=5-2", 1000),          # 逆序
    ("bytes=-0", 1000),           # 后缀长度为 0
    ("bytes=0-1,5-9", 1000),      # 多区间不支持
    ("bytes=abc", 1000),
    ("bytes=-", 1000),
    ("bytes=", 1000),
    ("bytes=abc-def", 1000),
])
def test_parse_range_invalid_raises_416(header, size) -> None:
    """非法 Range 必须抛异常让调用方回 416，不能静默返回全文。

    静默返回 200 全文件会让客户端以为续传成功，却拿到从头开始的数据。
    """
    with pytest.raises(RangeNotSatisfiable):
        parse_range(header, size)


def test_parse_range_zero_size_file() -> None:
    """空文件任何 Range 都无法满足。"""
    with pytest.raises(RangeNotSatisfiable):
        parse_range("bytes=0-", 0)


def test_range_helpers() -> None:
    assert range_length((0, 999)) == 1000
    assert range_length((500, 500)) == 1
    assert content_range_header((0, 499), 1000) == "bytes 0-499/1000"
    assert unsatisfied_range_header(1000) == "bytes */1000"


# --------------------------------------------------------------------------
# content_type_for
# --------------------------------------------------------------------------


def test_content_type_known_formats() -> None:
    assert content_type_for("jpg") == "image/jpeg"
    assert content_type_for("JPG") == "image/jpeg"
    assert content_type_for(".jpeg") == "image/jpeg"
    # 系统 mimetypes 对这两种格式的识别依赖注册表，跨机器不一致，必须写死
    assert content_type_for("heic") == "image/heic"
    assert content_type_for("heif") == "image/heif"
    assert content_type_for("avif") == "image/avif"


def test_content_type_never_returns_text_html() -> None:
    """照片字节由用户提供；若被当成 HTML 渲染就是存储型 XSS。

    即使库里混进一个 .html 文件，也必须返回 octet-stream。
    """
    for ext in ("html", "htm", "svg", "xhtml", "xml", "js"):
        ct = content_type_for(ext)
        assert not ct.startswith("text/"), f"{ext} 不得返回 {ct}"
        assert "html" not in ct.lower() or ct == DEFAULT_MIME


def test_xhtml_is_not_served_as_xhtml() -> None:
    """`.xhtml` 是最隐蔽的一个：它既不是 text/*，又会被浏览器当 HTML 执行。

    系统 mimetypes 对 .xhtml 返回 `application/xhtml+xml`，
    只判断"是不是 text/*"会直接放行，于是库里的 .xhtml 变成
    本站源下的存储型 XSS。必须靠 DANGEROUS_EXTS 显式拦下。
    """
    import mimetypes

    # 先证明"系统确实会给一个看似安全的类型"，否则这条测试是空的
    guess, _ = mimetypes.guess_type("x.xhtml")
    assert guess is not None and not guess.startswith("text/"), \
        f"前提不成立：系统对 .xhtml 返回 {guess!r}"

    assert content_type_for("xhtml") == DEFAULT_MIME
    assert content_type_for("xht") == DEFAULT_MIME
    assert content_type_for("XHTML") == DEFAULT_MIME, "大小写不能绕过"
    assert content_type_for(".xhtml") == DEFAULT_MIME, "带点不能绕过"


def test_dangerous_exts_never_leak_executable_types() -> None:
    """把整张危险表过一遍，逐条确认降级为 octet-stream。"""
    from app.transports.base import DANGEROUS_EXTS

    for ext in DANGEROUS_EXTS:
        assert content_type_for(ext) == DEFAULT_MIME, f"{ext} 未被拦截"


def test_content_type_unknown_falls_back() -> None:
    assert content_type_for("exe") == "application/x-msdownload"
    assert content_type_for("") == DEFAULT_MIME
    assert content_type_for("jpg-lx2") == DEFAULT_MIME, "小米回收站残留不是图片"


def test_content_type_video_for_phase2() -> None:
    assert content_type_for("mp4") == "video/mp4"
    assert content_type_for("mov") == "video/quicktime"


# --------------------------------------------------------------------------
# 端到端：真实 HTTP 服务
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_cfg() -> Config:
    return Config({
        "server": {"host": "127.0.0.1", "port": 0},
        "roots": [],
    })


def _build_router() -> Router:
    r = Router("e2e")

    @r.get("/api/system/ping")
    async def ping(ctx: Ctx):
        return json_reply({"ok": True, "via": ctx.via,
                           "ua": ctx.header("user-agent", "")})

    @r.get("/api/items/{id}")
    async def item(ctx: Ctx):
        return json_reply({"id": ctx.param_int("id"),
                           "q": ctx.q("q"), "all": ctx.q_all("tag")})

    @r.post("/api/echo")
    async def echo(ctx: Ctx):
        data = await ctx.json()
        return json_reply(data, status=201)

    @r.get("/api/stream")
    async def stream(ctx: Ctx):
        async def gen():
            for i in range(5):
                yield f"chunk{i}\n".encode()
        return stream_reply(gen(), "text/plain; charset=utf-8", length=35)

    @r.get("/api/big")
    async def big(ctx: Ctx):
        """返回 1000 字节，供 Range 测试。

        Range 解析属于业务处理函数，不在传输适配层 —— 适配层只做协议翻译。
        云端隧道同样要支持 Range（手机断点续传下载 2GB 原图），所以这段
        逻辑走的是与真实 /raw 接口完全相同的 parse_range 路径。
        """
        from app.core import file_reply
        from app.transports.base import (
            RangeNotSatisfiable,
            content_range_header,
            parse_range,
            range_length,
            unsatisfied_range_header,
        )

        total = 1000
        payload = bytes(range(256)) * 3 + b"x" * 232

        try:
            rng = parse_range(ctx.header("range"), total)
        except RangeNotSatisfiable:
            return json_reply({"error": "range"}, status=416, headers={
                "Content-Range": unsatisfied_range_header(total),
            })

        if rng is None:
            async def full():
                yield payload
            return stream_reply(full(), "application/octet-stream", length=total)

        start, end = rng

        async def part():
            yield payload[start:end + 1]

        return file_reply(part(), "application/octet-stream",
                          length=range_length(rng),
                          status=206, headers={
                              "Content-Range": content_range_header(rng, total),
                          })

    @r.get("/api/denied")
    async def denied(ctx: Ctx):
        raise SandboxError("parent_traversal", r"D:\Photos\secret.jpg")

    @r.get("/api/empty")
    async def empty(ctx: Ctx):
        from app.core import empty_reply
        return empty_reply(204)

    return r


class _Server:
    """在后台线程里跑 asyncio 事件循环 + uvicorn，供 urllib 同步请求。"""

    def __init__(self) -> None:
        self.port = _free_port()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.transport = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.error: BaseException | None = None

    def start(self) -> None:
        from app.transports.local_http import LocalHttpTransport

        cfg = _make_cfg()
        router = _build_router()

        def run() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.transport = LocalHttpTransport(cfg, router, port=self.port)

            async def boot():
                await self.transport.start()
                self._ready.set()
                # 一直跑到被要求停止
                while not getattr(self.transport._server, "should_exit", True):
                    await asyncio.sleep(0.05)

            try:
                self.loop.run_until_complete(boot())
            except BaseException as exc:  # noqa: BLE001
                self.error = exc
                self._ready.set()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=15):
            raise RuntimeError("服务启动超时")
        if self.error:
            raise self.error

    def stop(self) -> None:
        if self.transport is not None and self.loop is not None:
            fut = asyncio.run_coroutine_threadsafe(self.transport.stop(), self.loop)
            try:
                fut.result(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

        # 关闭事件循环并回收残留任务。
        # 不关的话，IocpProactor 的 accept 协程会一直 pending，被 GC 时
        # 打印 "Task was destroyed but it is pending!" —— 那是测试没收拾干净，
        # 不是被测代码的问题，混在输出里会掩盖真正的失败。
        loop = self.loop
        if loop is not None and not loop.is_closed():
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            finally:
                loop.close()
        self.loop = None
        self._thread = None

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"


@pytest.fixture(scope="module")
def server():
    s = _Server()
    s.start()
    yield s
    s.stop()


class _Headers(dict):
    """大小写不敏感的响应头视图。

    HTTP 头名按 RFC 7230 是大小写不敏感的，Starlette 一律发小写
    （HTTP/2 的要求，HTTP/1.1 同样合法）。测试关心的是"这个头在不在、
    值对不对"，不是它的字母大小写，所以这里统一归一化后再比较 ——
    否则 `headers["Content-Type"]` 会因为服务端发的是小写而 KeyError，
    测出来的是实现细节而非协议语义。
    """

    def __init__(self, raw) -> None:
        super().__init__((k.lower(), v) for k, v in raw)

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def __contains__(self, key) -> bool:
        return super().__contains__(key.lower())

    def get(self, key, default=None):
        return super().get(key.lower(), default)


def _get(server: _Server, path: str, headers: dict | None = None):
    req = urllib.request.Request(server.url(path), headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), _Headers(resp.headers.items())
    except urllib.error.HTTPError as e:
        return e.code, e.read(), _Headers(e.headers.items())


def _post(server: _Server, path: str, body: bytes, ctype="application/json"):
    req = urllib.request.Request(
        server.url(path), data=body, method="POST",
        headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read(), _Headers(resp.headers.items())
    except urllib.error.HTTPError as e:
        return e.code, e.read(), _Headers(e.headers.items())


def test_e2e_ping(server) -> None:
    status, body, headers = _get(server, "/api/system/ping")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    assert data["via"] == "lan", "局域网适配器必须标记 via=lan"
    assert headers["Content-Type"].startswith("application/json")
    assert headers["X-Request-Id"]
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_e2e_request_headers_passed_through(server) -> None:
    """请求头必须原样送达业务层（大小写不敏感）。"""
    _, body, _ = _get(server, "/api/system/ping", headers={"User-Agent": "TestUA/1.0"})
    assert json.loads(body)["ua"] == "TestUA/1.0"


def test_e2e_query_params_multi_value(server) -> None:
    """`?tag=a&tag=b` 必须聚合成 list，而不是只剩最后一个。"""
    _, body, _ = _get(server, "/api/items/7?q=hello&tag=a&tag=b")
    d = json.loads(body)
    assert d["id"] == 7
    assert d["q"] == "hello"
    assert d["all"] == ["a", "b"]


def test_e2e_utf8_path_is_decoded_once(server) -> None:
    """URL 里的中文路径必须解码一次且只有一次。

    二次解码会让 `%252e%252e` 变成 `..`，是目录穿越的经典入口。
    """
    from urllib.parse import quote
    status, body, _ = _get(server, "/api/items/" + quote("中文") + "?q=" + quote("照片"))
    assert status == 400, "非整数 id 应报 400"
    err = json.loads(body)["error"]
    assert err["code"] == "E1001"


def test_e2e_post_json_roundtrip(server) -> None:
    payload = {"name": "照片.jpg", "size": 12345}
    status, body, _ = _post(server, "/api/echo", json.dumps(payload).encode())
    assert status == 201
    assert json.loads(body) == payload, "中文必须原样往返"


def test_e2e_streamed_response(server) -> None:
    """流式响应必须完整送达且带 Content-Length。"""
    status, body, headers = _get(server, "/api/stream")
    assert status == 200
    assert body == b"chunk0\nchunk1\nchunk2\nchunk3\nchunk4\n"
    assert headers.get("Content-Length") == "35"


def test_e2e_range_request_returns_206(server) -> None:
    status, body, headers = _get(server, "/api/big", headers={"Range": "bytes=0-9"})
    assert status == 206
    assert len(body) == 10
    assert headers["Content-Range"] == "bytes 0-9/1000"


def test_e2e_full_download_without_range(server) -> None:
    status, body, headers = _get(server, "/api/big")
    assert status == 200
    assert len(body) == 1000
    assert "Content-Range" not in headers


def test_e2e_error_body_is_sanitized(server) -> None:
    """端到端确认沙箱错误的绝对路径不会出现在响应体里。"""
    status, body, headers = _get(server, "/api/denied")
    assert status == 400
    assert b"software" not in body
    assert b"E:" not in body
    err = json.loads(body)["error"]
    assert err["code"] == "E1009"
    assert err["user_hint"]
    assert headers["X-Request-Id"] == err["request_id"]


def test_e2e_204_has_no_body_or_content_length(server) -> None:
    """204 带 Content-Length 是协议违规，某些客户端会因此挂住。"""
    status, body, headers = _get(server, "/api/empty")
    assert status == 204
    assert body == b""
    assert "Content-Length" not in headers


def test_e2e_unknown_path_404(server) -> None:
    status, body, _ = _get(server, "/api/nope")
    assert status == 404
    assert json.loads(body)["error"]["code"] == "E1004"


def test_e2e_wrong_method_405(server) -> None:
    """405 必须带 Allow 头（RFC 7231 要求）。"""
    status, body, headers = _post(server, "/api/system/ping", b"")
    assert status == 405
    assert "GET" in headers["Allow"]


def test_e2e_content_type_set_exactly_once(server) -> None:
    """Content-Type 只能出现一次。

    适配层若不从 headers 里剔除手动设的 Content-Type，
    Starlette 会再追加一个，某些客户端看到两个会判为协议错误。
    """
    _, _, headers = _get(server, "/api/stream")
    assert headers["Content-Type"] == "text/plain; charset=utf-8"


def test_transport_status_and_urls(server) -> None:
    st = server.transport.status()
    assert st["transport"] == "local_http"
    assert st["listening"] is True
    assert st["port"] == server.port
    assert server.transport.urls == [f"http://127.0.0.1:{server.port}"]


def test_bind_safety_rejects_public_ip() -> None:
    """公网 IP 一律拒绝绑定。"""
    from app.config import check_bind_safety
    cfg = _make_cfg()
    ok, why = check_bind_safety("8.8.8.8", cfg)
    assert ok is False and "公网" in why


def _wildcard_cfg(**server_overrides) -> Config:
    """能通过"已配根目录 + 需配对"两道关、只剩 allow_lan 可控的配置。

    通配地址的三道门是有先后顺序的（allow_lan → roots 非空 → require_pairing）。
    只给 `roots: []` 会让检查停在"尚未配置媒体根目录"，测不到 allow_lan 这一条，
    所以这里必须把另外两道门都打开，才能真正验证 allow_lan 的作用。
    """
    server = {"host": "0.0.0.0", "allow_lan": True}
    server.update(server_overrides)
    return Config({
        "server": server,
        "roots": [{"id": "library", "path": "D:\\Photos",
                   "mode": "readwrite", "enabled": True}],
        "auth": {"require_pairing": True},
    })


def test_bind_safety_requires_lan_flag_for_wildcard() -> None:
    """绑 0.0.0.0 需要显式 allow_lan，否则同网段任何人可探测。"""
    from app.config import check_bind_safety
    cfg = _wildcard_cfg(allow_lan=False)
    ok, why = check_bind_safety("0.0.0.0", cfg)
    assert ok is False and "allow_lan" in why


def test_bind_safety_wildcard_needs_roots() -> None:
    """没有媒体根目录时拒绝通配监听：此时服务什么都读不到，开放毫无意义。"""
    from app.config import check_bind_safety
    cfg = Config({
        "server": {"host": "0.0.0.0", "allow_lan": True},
        "roots": [],
        "auth": {"require_pairing": True},
    })
    ok, why = check_bind_safety("0.0.0.0", cfg)
    assert ok is False and "根目录" in why


def test_bind_safety_wildcard_needs_pairing() -> None:
    """关掉配对还监听所有网卡 = 无鉴权暴露整个照片库，必须拒绝。"""
    from app.config import check_bind_safety
    cfg = Config({
        "server": {"host": "0.0.0.0", "allow_lan": True},
        "roots": [{"id": "library", "path": "D:\\Photos",
                   "mode": "readwrite", "enabled": True}],
        "auth": {"require_pairing": False},
    })
    ok, why = check_bind_safety("0.0.0.0", cfg)
    assert ok is False and "require_pairing" in why


def test_bind_safety_wildcard_ok_when_all_gates_open() -> None:
    """三道门都开时允许 —— 这是局域网直连的正常配置。"""
    from app.config import check_bind_safety
    ok, why = check_bind_safety("0.0.0.0", _wildcard_cfg())
    assert ok is True, why


def test_bind_safety_allows_loopback_always() -> None:
    from app.config import check_bind_safety
    cfg = _make_cfg()
    for host in ("127.0.0.1", "localhost", "::1", "127.0.0.5"):
        ok, why = check_bind_safety(host, cfg)
        assert ok is True, f"{host} 应允许：{why}"


def test_transport_stop_is_idempotent() -> None:
    """stop() 必须可重复调用（托盘退出与信号处理都会调它）。"""
    from app.transports.local_http import LocalHttpTransport

    async def main():
        t = LocalHttpTransport(_make_cfg(), _build_router(), port=_free_port())
        await t.start()
        await t.stop()
        await t.stop()   # 第二次不应抛异常

    asyncio.run(main())
