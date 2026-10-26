"""端到端测试：真的起一个 HTTP 服务，用真的 HTTP 请求走完整流程。

**为什么必须有一层端到端测试。** 单元测试里 `Router.dispatch(ctx)` 是
直接调用的，绕过了三样东西：Starlette 的 ASGI 适配、uvicorn 的
HTTP 解析、以及真实网络下的分块与并发。这三样里任何一个出问题
（比如 Starlette 把函数端点的 methods 静默改成 ["GET"]），
单元测试全绿而手机上什么都点不动。

这里测的是"用户拿手机连上来会发生什么"：
    配对 → 浏览时间线 → 看缩略图 → 传一张照片 → 在时间线上看到它。
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
from PIL import Image

from backend.app.config import Config
from backend.app.config import _in_non_lan_net, _usable_lan_ips
from backend.app.config import local_ipv4_addresses
from backend.app.db import Database
from backend.app.server import build_app
from backend.app.state import AppState
from backend.app.thumbs import ThumbEngine

# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_photo(path: Path, size=(120, 90), color=(200, 100, 50), taken=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", size, color)
    if taken is not None:
        ex = Image.Exif()
        # DateTimeOriginal 必须写进 Exif SubIFD(0x8769) —— 写顶层虽然
        # Pillow 读得到，但那不是照片实际的存储结构。
        ex.get_ifd(0x8769)[0x9003] = taken
        im.save(path, exif=ex)
    else:
        im.save(path)


class _Headers(dict):
    """大小写不敏感的头视图。

    HTTP 头名按 RFC 7230 大小写不敏感，uvicorn 一律发小写，
    直接 `headers["Content-Type"]` 会 KeyError —— 那样测出来的是
    实现细节而不是协议语义。
    """

    def __init__(self, src):
        super().__init__({str(k).lower(): v for k, v in dict(src).items()})

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def __contains__(self, key):
        return super().__contains__(str(key).lower())

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)


class _Response:
    def __init__(self, status: int, headers, body: bytes):
        self.status = status
        self.headers = _Headers(headers)
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


class _Server:
    """在后台线程里跑 uvicorn，主线程用 urllib 发同步请求。

    不能用同一个事件循环里的异步客户端：`urllib` 是阻塞的，
    在事件循环线程里调用会直接死锁（服务器永远没机会处理请求）。
    """

    def __init__(self, cfg: Config, db: Database, library: Path) -> None:
        self.cfg = cfg
        self.db = db
        self.library = library
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.loop: asyncio.AbstractEventLoop | None = None
        self.transport = None
        self.state: AppState | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="e2e-server", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30.0):
            raise AssertionError(f"服务器启动超时：{self._error!r}")
        if self._error is not None:
            raise AssertionError(f"服务器启动失败：{self._error!r}")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except BaseException as exc:        # noqa: BLE001
            self._error = exc
            self._ready.set()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:               # noqa: BLE001
                pass
            loop.close()

    async def _main(self) -> None:
        from backend.app.transports.local_http import LocalHttpTransport

        thumbs = ThumbEngine(self.cfg, self.db, workers=2)
        from backend.app.indexer import Indexer

        indexer = Indexer(self.cfg, self.db, thumbs=thumbs)
        st = AppState(self.cfg, self.db, thumbs=thumbs, indexer=indexer)
        self.state = st
        thumbs.start()

        _, transport, _ = build_app(self.cfg, state=st)
        # 端口固定成我们预先探测到的空闲端口
        transport.port = self.port
        self.transport = transport
        await transport.start()
        self._ready.set()
        # 一直活着，直到 stop()
        self._stop_event = asyncio.Event()
        await self._stop_event.wait()

    def stop(self) -> None:
        if self.loop is None:
            return
        ev = getattr(self, "_stop_event", None)
        if ev is not None:
            self.loop.call_soon_threadsafe(ev.set)
        if self._thread is not None:
            self._thread.join(timeout=20.0)
            self._thread = None

    # -- 请求 -------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        json_body=None,
        raw: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> _Response:
        url = self.base + path
        data = raw
        hdrs: dict[str, str] = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return _Response(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as exc:
            return _Response(exc.code, dict(exc.headers), exc.read())

    def get(self, path, **kw) -> _Response:
        return self.request("GET", path, **kw)

    def post(self, path, **kw) -> _Response:
        return self.request("POST", path, **kw)


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture()
def env(tmp_path: Path):
    library = tmp_path / "lib"
    library.mkdir()
    _make_photo(library / "2024/2024-05/a.jpg", taken="2024:05:12 18:30:00")
    _make_photo(library / "2024/2024-05/b.jpg", taken="2024:05:12 18:30:01")
    _make_photo(library / "2025/2025-01/c.jpg", taken="2025:01:02 09:00:00")
    _make_photo(library / "青甘环线/敦煌.png", taken="2023:07:08 10:11:12")
    # 不该被索引的：视频与小米回收站残留
    (library / "clip.mp4").write_bytes(b"\x00" * 16)
    (library / "dead.jpg-lx2").write_bytes(b"junk")

    cfg = Config(
        {
            # `panel_http: True` —— 这个测试文件**本身**就是"用 HTTP 调面板
            # 接口拿配对码"的调用方（`_pair()` 走 POST /panel/pair-code）。
            # 生产默认是 False（审计 F1-b：本机浏览器与用户无从区分），
            # 这里显式打开，等价于用户自己把面板接口开出来。
            "server": {"host": "127.0.0.1", "port": 0, "allow_lan": False,
                       "panel_http": True},
            "roots": [
                {
                    "id": "library",
                    "path": str(library),
                    "label": "测试库",
                    "mode": "readwrite",
                    "enabled": True,
                }
            ],
            "auth": {"require_pairing": True, "allow_delete": True},
            "index": {"scan_on_start": False, "watch": False},
        },
        path=tmp_path / "config.json",
        data_dir=tmp_path / "data",
    )
    db = Database(tmp_path / "t.db")
    db.init()

    srv = _Server(cfg, db, library)
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()
        db.close()


def _pair(srv: _Server) -> str:
    """走完整配对流程，返回 client token。"""
    r = srv.post("/panel/pair-code", headers={"User-Agent": "pytest"})
    assert r.status == 201, r.text
    code = r.json()["code"]
    r = srv.post("/api/pair", json_body={"code": code},
                 headers={"User-Agent": "Mozilla/5.0 (Android)"})
    assert r.status == 201, r.text
    return r.json()["client_token"]


def _index(srv: _Server) -> None:
    """在服务器的事件循环里跑一次全量扫描。"""
    fut = asyncio.run_coroutine_threadsafe(srv.state.indexer.scan_all(), srv.loop)
    fut.result(timeout=60)


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------


def test_ping_is_anonymous_and_reports_device(env):
    r = env.get("/api/system/ping")
    assert r.status == 200
    body = r.json()
    assert body["pong"] is True
    assert body["device_id"]
    assert body["via"] == "lan"
    # 探活必须匿名：设成需要鉴权的话，手机冷启动永远 401，
    # 无法区分"电脑没开"与"token 过期"。
    assert "X-Request-Id" in r.headers


def test_protected_route_requires_token(env):
    r = env.get("/api/timeline")
    assert r.status == 401
    body = r.json()
    assert body["error"]["code"] == "E2002"
    # message 是稳定的机器码，user_hint 才是给用户看的中文。
    # 断言机器码而不是文案：文案会随措辞调整，测试不该跟着一起改。
    assert body["error"]["message"] == "client_token_invalid"
    assert body["error"]["user_hint"]


def test_panel_rejects_lan_client(env):
    """配对码面板只允许本机访问 —— 这是配对机制的安全底线。"""
    # 测试里请求来自 127.0.0.1，所以是允许的；这里验证的是
    # 面板路由确实存在且返回配对码。
    r = env.post("/panel/pair-code")
    assert r.status == 201
    assert len(r.json()["code"]) == 6


def test_full_pairing_flow(env):
    token = _pair(env)
    assert token

    r = env.get("/api/clients/me", token=token)
    assert r.status == 200
    me = r.json()
    assert me["device_id"]
    assert me["client_id"]

    r = env.get("/api/devices", token=token)
    assert r.status == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["revoked"] == 0

    # 撤销后立即失效，且是 403（不是 401）—— 让手机知道是被主动移除的
    cid = me["client_id"]
    r = env.request("DELETE", f"/api/devices/{cid}", token=token)
    assert r.status == 204
    r = env.get("/api/timeline", token=token)
    assert r.status == 403
    assert r.json()["error"]["code"] == "E2004"


def test_router_has_app_bound(env):
    """处理函数靠 ctx.app 拿 AppState —— 装配漏了会让所有接口 500。"""
    assert env.state is not None
    assert env.get("/api/system/ping").json()["device_id"] == env.state.device_id


def test_timeline_over_http(env):
    token = _pair(env)
    _index(env)

    r = env.get("/api/timeline?limit=10", token=token)
    assert r.status == 200, r.text
    body = r.json()
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    items = body["items"]
    # 视频与 .jpg-lx2 不索引 → 恰好 4 张
    assert len(items) == 4
    # 时间线必须严格倒序
    times = [it["taken_at"] for it in items]
    assert times == sorted(times, reverse=True)
    names = {it["name"] for it in items}
    assert "敦煌.png" in names
    assert "clip.mp4" not in names
    assert "dead.jpg-lx2" not in names
    # 轻量项字段集合（`kind`/`duration` 是加视频时新增的两个小字段）
    assert set(items[0]) == {
        "id", "name", "kind", "duration", "taken_at", "taken_src", "w", "h",
        "thumb_v", "size", "thumb_state", "has_gps", "root", "dir",
    }


def test_thumb_and_preview_over_http(env):
    token = _pair(env)
    _index(env)
    mid = env.get("/api/timeline?limit=1", token=token).json()["items"][0]["id"]

    r = env.get(f"/api/items/{mid}/thumb?size=320", token=token)
    assert r.status == 200, r.text
    assert r.body[:4] == b"RIFF" and r.body[8:12] == b"WEBP"
    etag = r.headers["ETag"]
    assert etag.startswith('"w320-')
    assert "immutable" in r.headers["Cache-Control"]
    assert r.headers["Vary"] == "Authorization"

    # 条件请求命中 → 304 且无体（304 的价值就是省隧道带宽）
    r2 = env.get(f"/api/items/{mid}/thumb?size=320", token=token,
                 headers={"If-None-Match": etag})
    assert r2.status == 304
    assert r2.body == b""

    r3 = env.get(f"/api/items/{mid}/preview", token=token)
    assert r3.status == 200
    assert r3.body[:4] == b"RIFF"


def test_raw_range_over_http(env):
    token = _pair(env)
    _index(env)
    mid = env.get("/api/timeline?limit=1", token=token).json()["items"][0]["id"]

    full = env.get(f"/api/items/{mid}/raw", token=token)
    assert full.status == 200
    assert full.headers["Accept-Ranges"] == "bytes"

    part = env.get(f"/api/items/{mid}/raw", token=token,
                   headers={"Range": "bytes=10-19"})
    assert part.status == 206
    assert part.body == full.body[10:20]          # 只断言 206 是没用的
    assert part.headers["Content-Range"] == f"bytes 10-19/{len(full.body)}"

    bad = env.get(f"/api/items/{mid}/raw", token=token,
                  headers={"Range": "bytes=99999999-"})
    assert bad.status == 416
    assert bad.headers["Content-Range"] == f"bytes */{len(full.body)}"

    dl = env.get(f"/api/items/{mid}/raw?download=1", token=token)
    assert "attachment" in dl.headers["Content-Disposition"]


def test_search_and_folders_over_http(env):
    token = _pair(env)
    _index(env)

    r = env.get("/api/search?q=" + urllib.parse.quote("敦煌"), token=token)
    assert r.status == 200
    assert len(r.json()["items"]) == 1

    r = env.get("/api/search", token=token)
    assert r.status == 400        # 空查询等于送一个扫全库的免费接口

    r = env.get("/api/folders?depth=1", token=token)
    assert r.status == 200
    folders = {f["name"]: f["count"] for f in r.json()["folders"]}
    assert folders["2024"] == 2
    assert folders["2025"] == 1
    assert folders["青甘环线"] == 1


def test_upload_round_trip(env):
    """完整上传：init → PATCH 分片 → complete → 出现在时间线上。"""
    token = _pair(env)
    # 先跑一次索引，确认库里初始是 4 张
    _index(env)

    src = env.library / "src.jpg"
    _make_photo(src, size=(300, 200), color=(10, 200, 30), taken="2026:03:04 05:06:07")
    data = src.read_bytes()
    half = len(data) // 2

    r = env.post("/api/uploads/init", token=token,
                 json_body={"name": "IMG_9999.jpg", "size": len(data)})
    assert r.status == 201, r.text
    init = r.json()
    uid = init["upload_id"]
    assert init["offset"] == 0
    assert init["chunk_size"] >= 256 * 1024
    assert init["exists"] is False

    r = env.request(
        "PATCH", f"/api/uploads/{uid}", token=token, raw=data[:half],
        headers={"Content-Range": f"bytes 0-{half - 1}/{len(data)}"},
    )
    assert r.status == 206, r.text
    assert r.json()["offset"] == half

    # 断点查询（手机切网络后靠这个恢复）
    r = env.get(f"/api/uploads/{uid}", token=token)
    assert r.status == 200
    assert r.json()["offset"] == half
    assert r.json()["complete"] is False

    r = env.request(
        "PATCH", f"/api/uploads/{uid}", token=token, raw=data[half:],
        headers={"Content-Range": f"bytes {half}-{len(data) - 1}/{len(data)}"},
    )
    assert r.status == 206, r.text
    assert r.json()["complete"] is True

    r = env.post(f"/api/uploads/{uid}/complete", token=token, json_body={})
    assert r.status == 201, r.text
    done = r.json()
    assert done["dedup"] is False
    rel = done["rel_path"]
    # 归档路径按 EXIF 时间（2026-03-04）而不是"现在"
    assert rel.startswith("手机上传/2026/2026-03/")
    assert (env.library / rel).read_bytes() == data

    # 重新提交同一个会话 → 幂等，不报错
    r = env.post(f"/api/uploads/{uid}/complete", token=token, json_body={})
    assert r.status == 200
    assert r.json()["already_completed"] is True

    # 秒传：同样的内容再传一次，一个字节都不用发
    r = env.post("/api/uploads/init", token=token,
                 json_body={"name": "again.jpg", "size": len(data),
                            "sha256": __import__("hashlib").sha256(data).hexdigest()})
    assert r.status == 200
    assert r.json()["exists"] is True
    assert r.json()["upload_id"] is None


def test_upload_out_of_order_over_http(env):
    token = _pair(env)
    data = b"x" * 4096
    r = env.post("/api/uploads/init", token=token,
                 json_body={"name": "ooo.jpg", "size": len(data)})
    uid = r.json()["upload_id"]

    r = env.request("PATCH", f"/api/uploads/{uid}", token=token, raw=data[:1024],
                    headers={"Content-Range": f"bytes 0-1023/{len(data)}"})
    assert r.status == 206

    # 跳号 → 416 + 告诉客户端从哪继续
    r = env.request("PATCH", f"/api/uploads/{uid}", token=token, raw=data[2048:3072],
                    headers={"Content-Range": f"bytes 2048-3071/{len(data)}"})
    assert r.status == 416
    assert r.headers["X-Upload-Offset"] == "1024"

    # 取消
    r = env.request("DELETE", f"/api/uploads/{uid}", token=token)
    assert r.status == 200
    assert r.json()["aborted"] is True


def test_upload_rejects_bad_extension_over_http(env):
    token = _pair(env)
    # `.mkv`：能索引、不能上传（浏览器放不了，传上来只会多一个打不开的条目）
    r = env.post("/api/uploads/init", token=token,
                 json_body={"name": "movie.mkv", "size": 1024})
    assert r.status == 400
    assert r.json()["error"]["code"] == "E1017"
    # `.mp4` 在默认配置（`video.enabled = true`）下可以建会话 ——
    # 老用户的 `upload.allowed_exts` 里没有 mp4，靠开关取并集才不用手改配置。
    r2 = env.post("/api/uploads/init", token=token,
                  json_body={"name": "movie.mp4", "size": 1024})
    assert r2.status == 201, r2.text


def test_rescan_returns_202_immediately(env):
    """真实库首轮约 20 秒；在隧道下等待必然触发 60 秒超时。"""
    token = _pair(env)
    t0 = time.perf_counter()
    r = env.post("/api/index/rescan", token=token, json_body={})
    assert r.status == 202, r.text
    assert time.perf_counter() - t0 < 5.0
    assert r.json()["started"] is True


def test_events_stream_hello(env):
    token = _pair(env)
    r = env.get("/api/events?once=1", token=token)
    assert r.status == 200
    assert r.headers["Content-Type"].startswith("text/event-stream")
    assert r.headers["Cache-Control"] == "no-store"
    # 反向代理默认攒够 4KB 才转发，SSE 会变成"每 30 秒蹦一批"
    assert r.headers["X-Accel-Buffering"] == "no"
    text = r.text
    assert text.startswith("event: hello\n")
    assert '"via":"lan"' in text


def test_system_info_and_stats(env):
    token = _pair(env)
    _index(env)

    r = env.get("/api/system/info", token=token)
    assert r.status == 200, r.text
    info = r.json()
    assert info["app"]["name"]
    assert info["tunnel"]["mode"] == "offline"      # 一期无云端
    assert info["roots"][0]["id"] == "library"
    assert info["roots"][0]["count"] == 4
    assert info["roots"][0]["path"] != str(env.library)
    assert info["roots"][0]["path"].endswith("\\lib") or info["roots"][0]["path"].endswith("/lib")
    if "host_app" in info:
        assert info["host_app"]["autostart"]["command"] is None
        assert info["host_app"]["autostart"]["expected"] is None
    assert info["thumbs"]["sizes"] == [320, 1600]
    assert info["limits"]["max_file_bytes"] == 2048 * 1024 * 1024
    # SEC-17：这里由 `server.host` 推导。测试环境绑的是 127.0.0.1，
    # 所以是 loopback —— 之前那条 `== "private"` 断言的正是那个硬编码常量。
    assert info["server"]["bind_scope"] == "loopback"
    assert info["time"]["tz_offset_minutes"] is not None

    r = env.get("/api/system/stats", token=token)
    assert r.status == 200
    stats = r.json()
    assert stats["media"]["total"] == 4
    assert stats["media"]["videos"] == 0


def test_delete_moves_to_recycle_bin(env):
    token = _pair(env)
    _index(env)
    item = env.get("/api/timeline?limit=1", token=token).json()["items"][0]
    mid = item["id"]
    before = len(list(env.library.rglob("*.jpg"))) + len(list(env.library.rglob("*.png")))

    r = env.post("/api/items/delete", token=token, json_body={"ids": [mid]})
    assert r.status == 200, r.text
    assert r.json()["deleted"] == 1
    assert r.json()["failed"] == []

    after = len(list(env.library.rglob("*.jpg"))) + len(list(env.library.rglob("*.png")))
    assert after == before - 1
    # 行保留、标 missing —— 删行会让"最近删除"失去依据
    row = env.db.query_one("SELECT missing FROM media WHERE id = ?", (mid,))
    assert row["missing"] == 1
    # 时间线上不再出现
    ids = [i["id"] for i in env.get("/api/timeline?limit=50", token=token).json()["items"]]
    assert mid not in ids


def test_no_absolute_path_leaks_in_any_error(env):
    """响应体里绝不能出现库根路径 —— 这是接口文档 §6 的硬约束。"""
    token = _pair(env)
    probes = [
        ("GET", "/api/items/999999", None, None),
        ("GET", "/api/items/999999/thumb", None, None),
        ("GET", "/api/timeline?cursor=!!!!", None, None),
        ("GET", "/api/timeline?order=asc", None, None),
        ("GET", "/api/timeline?limit=abc", None, None),
        ("GET", "/api/folders?root=nope", None, None),
        ("GET", "/api/folders?path=../../..", None, None),
        ("GET", "/api/items/abc/thumb", None, None),
        ("POST", "/api/items/delete", {"ids": []}, None),
        ("POST", "/api/index/rescan", {}, None),
        ("GET", "/api/nope", None, None),
    ]
    root = str(env.library)
    for method, path, body, _ in probes:
        r = env.request(method, path, token=token, json_body=body)
        text = r.text
        assert root not in text, f"{method} {path} 泄漏了库根路径"
        assert "Traceback" not in text, f"{method} {path} 泄漏了堆栈"
        assert 'File "' not in text, f"{method} {path} 泄漏了堆栈文件名"
        # 错误体结构统一
        if r.status >= 400:
            assert "error" in r.json(), f"{method} {path} 错误体结构不对：{text}"
            err = r.json()["error"]
            assert set(err) >= {"code", "message", "detail", "retryable",
                                "request_id", "user_hint"}


def test_missing_file_degrades_gracefully(env):
    """文件被手工删掉但索引还在 —— 缩略图给占位图而不是 500。

    一个破图会让用户以为照片丢了；500 会让前端弹错误对话框，
    而实际上只是缩略图没生成出来。
    """
    token = _pair(env)
    _index(env)
    item = env.get("/api/timeline?limit=1", token=token).json()["items"][0]
    mid = item["id"]
    row = env.db.query_one("SELECT rel_path FROM media WHERE id = ?", (mid,))
    _unlink_settled(env.library / row["rel_path"])

    r = env.get(f"/api/items/{mid}/thumb?size=320", token=token)
    assert r.status == 200, r.text
    assert r.body[:4] == b"RIFF"

    # 原图必须明确报错（不能拿占位图冒充原图 —— 用户会以为自己
    # 下载到了照片，存进相册才发现是一张灰图）
    r = env.get(f"/api/items/{mid}/raw", token=token)
    assert r.status >= 400


def _unlink_settled(path: Path, *, tries: int = 40, delay: float = 0.05) -> None:
    """删除文件，容忍 Windows 上短暂的共享冲突。

    ★ 为什么需要重试：`env.library` 里的文件同时被**后台缩略图线程**读取。
    索引器刚把文件入队，worker 可能正拿着它生成缩略图（Pillow 打开文件
    期间 Windows 不允许删除），此时 `unlink()` 抛
    `PermissionError: [WinError 32] 另一个程序正在使用此文件`。

    这是**测试自身的竞态**，不是产品缺陷：真实用户删照片时程序也在读它，
    而 `items/delete` 走的是回收站（`send2trash`），它内部同样要处理
    占用 —— 但那属于 `handlers` 的职责，这里只是测试夹具在模拟"文件
    从磁盘上消失了"。

    实测：不加重试时全库跑约每 8~10 次出现 1 次失败（`test_missing_file_is_flagged_after_rescan`），
    单跑该文件 8 次全过 —— 典型的并发时序问题，不是逻辑错误。
    """
    last: OSError | None = None
    for _ in range(tries):
        try:
            path.unlink()
            return
        except FileNotFoundError:
            return                      # 已经没了，目标达成
        except PermissionError as exc:  # noqa: PERF203
            last = exc
            time.sleep(delay)
    raise AssertionError(f"文件一直被占用，无法删除：{path}（{last}）")


def test_missing_file_is_flagged_after_rescan(env):
    """重扫之后 missing 必须为 1，前端才知道该显示"找不到了"。"""
    token = _pair(env)
    _index(env)
    item = env.get("/api/timeline?limit=1", token=token).json()["items"][0]
    mid = item["id"]
    row = env.db.query_one("SELECT rel_path FROM media WHERE id = ?", (mid,))
    _unlink_settled(env.library / row["rel_path"])

    _index(env)
    r = env.get(f"/api/items/{mid}", token=token)
    assert r.status == 200, r.text
    assert r.json()["missing"] == 1


# --------------------------------------------------------------------------
# 局域网地址过滤（本机实测回归）
#
# 背景：开发机有 5 个 IPv4，其中 4 个来自虚拟网卡 —— Radmin VPN
# 26.26.192.40、VMware VMnet8 192.168.226.1、VMnet1 192.168.64.1、
# WSL vEthernet 192.168.32.1，只有 192.0.2.10 是真实局域网地址。
#
# 这些地址会被托盘二维码、`/api/system/info` 的 `lan_ips`、以及
# `LocalHttpTransport.urls` 展示给用户。展示错了，手机就去连一个永远连不上的
# 地址，用户看到的是"这个软件坏了" —— 这类问题极难自助排查。
#
# 早期实现只用网段前缀过滤（只认识 172.16–172.31），实测 5 个里漏掉 3 个。
# 下面把两种判据分别钉死：网段表负责 Radmin（它真的能路由，探测不出来），
# 源地址路由探测负责 host-only 虚拟网卡（它们的网段无法穷举）。
# --------------------------------------------------------------------------


def test_non_lan_net_table_covers_radmin_and_virtual_nets():
    """网段表必须认识本机实际遇到的每一类非局域网地址。"""
    for bogus in ("26.26.192.40", "25.1.2.3", "172.20.0.1", "127.0.0.1",
                  "169.254.1.1", "100.64.0.1"):
        assert _in_non_lan_net(bogus), f"{bogus} 应被判为非局域网地址"


def test_non_lan_net_table_keeps_real_lan_addresses():
    """真实局域网地址不能被误杀（误杀会让手机连不上，比多显示更严重）。"""
    for good in ("192.0.2.10", "192.168.1.7", "10.0.0.5", "172.15.0.1",
                 "172.32.0.1"):
        assert not _in_non_lan_net(good), f"{good} 是可用地址，不该被过滤"


def test_usable_lan_ips_drops_host_only_via_route_probe():
    """源地址路由探测必须滤掉 host-only 虚拟网卡（它们网段无法穷举）。

    本机实测：绑定 192.168.226.1 / 192.168.64.1 / 192.168.32.1 后
    connect() 直接抛 WSAENETUNREACH(10051)，而 192.0.2.10 能拿到路由。
    这里注入假的探测函数，把「不能出网」这件事确定性地表达出来。
    """
    def route(addr: str) -> bool:
        return addr == "192.0.2.10"

    got = _usable_lan_ips(
        "192.0.2.10",
        ["26.26.192.40", "192.168.226.1", "192.168.64.1", "192.168.32.1"],
        routes_off_link=route,
    )
    assert got == ["192.0.2.10"], got


def test_usable_lan_ips_keeps_second_real_nic():
    """有多张真实网卡（比如有线 + 无线）时，两张都要保留。"""
    got = _usable_lan_ips(
        "192.0.2.10",
        ["192.168.1.20", "192.168.226.1"],
        routes_off_link=lambda a: a != "192.168.226.1",
    )
    assert got == ["192.0.2.10", "192.168.1.20"], got


def test_usable_lan_ips_primary_is_always_first():
    """默认路由选出的地址必须排第一 —— 二维码用它生成，手机先扫到的应该是能连的。"""
    got = _usable_lan_ips(
        "192.0.2.10",
        ["192.168.1.20"],
        routes_off_link=lambda a: True,
    )
    assert got[0] == "192.0.2.10", got


def test_usable_lan_ips_drops_virtual_primary():
    """★ 默认路由本身落在虚拟网卡上时必须丢弃，而不是因为"是 primary"就放行。

    场景：装了 Radmin / Hamachi 且它的虚拟网卡抢到了默认路由。
    早期实现无条件 `found.append(primary)`，此时会把唯一保留的名额
    浪费在一个手机连不上的地址上，同时真实网卡被落在后面。
    """
    got = _usable_lan_ips("26.26.192.40", ["192.0.2.10"],
                          routes_off_link=lambda a: True)
    assert got == ["192.0.2.10"], got


def test_usable_lan_ips_is_deduped_and_safe_on_empty():
    """primary 同时出现在 others 里时不能重复；全空时返回空列表而不是抛异常。"""
    assert _usable_lan_ips("192.0.2.10", ["192.0.2.10"],
                           routes_off_link=lambda a: True) == ["192.0.2.10"]
    assert _usable_lan_ips(None, [], routes_off_link=lambda a: True) == []


def test_usable_lan_ips_ignores_route_probe_for_primary():
    """primary 不跑路由探测。

    理由：探测要建 socket 并 bind，对默认网卡做一次没有信息量
    （它就是路由表选出来的），却会在每个请求 `/api/system/info` 时重复执行。
    """
    calls: list[str] = []

    def route(addr: str) -> bool:
        calls.append(addr)
        return True

    _usable_lan_ips("192.0.2.10", [], routes_off_link=route)
    assert calls == [], calls


def test_local_ipv4_addresses_filters_virtual_nics_on_this_machine():
    """端到端：在本机（真的存在 4 张虚拟网卡）调用真实实现。

    这条是"真实环境回归" —— 换一台没有虚拟网卡的机器时，断言退化为
    "结果非空且不含量已知虚拟地址"，仍然有意义。
    """
    ips = local_ipv4_addresses()
    assert ips, "至少应找到一个可用地址"
    for bogus in ("26.26.192.40", "192.168.226.1", "192.168.64.1",
                  "192.168.32.1", "127.0.0.1"):
        assert bogus not in ips, f"{bogus} 不该出现在给用户看的地址列表里: {ips}"
    assert len(ips) == len(set(ips)), f"地址重复: {ips}"


def test_transport_urls_do_not_advertise_virtual_nics():
    """`transport.urls` 是托盘二维码与"复制地址"菜单的数据源，必须干净。"""
    from backend.app.transports.local_http import LocalHttpTransport

    cfg = Config({})
    t = LocalHttpTransport(cfg, router=None, host="0.0.0.0", port=8787)
    t._actual_port = 8787
    urls = t.urls
    for bogus in ("26.26.192.40", "192.168.226.1", "192.168.64.1",
                  "192.168.32.1"):
        assert not any(bogus in u for u in urls), f"{bogus} 出现在 {urls}"
    # 回环绑定时只给回环地址，不受本函数影响
    t2 = LocalHttpTransport(cfg, router=None, host="127.0.0.1", port=8787)
    t2._actual_port = 8787
    assert t2.urls == ["http://127.0.0.1:8787"]


# --------------------------------------------------------------------------
# 启动时清理过期上传（server._purge_stale_uploads）
#
# 背景：purge_expired() 原本只在 create_session() 里被调用 —— 也就是
# "用户下次发起上传时顺手做"。可这个程序是常驻的，而中断是常态
# （用户传一半关掉手机、程序被强杀、这台机器从此不再发起上传）。
# 那些 .part 躺在 <库根>/.photobridge_tmp/ —— 用户的照片库目录里，
# 既不会被索引器扫到（在 exclude_dirs 里），也没人清，只会一直堆积。
# --------------------------------------------------------------------------


def _make_stale_session(cfg, db, *, name: str = "half.jpg",
                        age_hours: float = 48.0) -> tuple[str, Path]:
    """造一个"已过期但还没被清理"的上传会话，并真的写一个 .part 文件。

    返回 (upload_id, temp_path)。
    """
    from backend.app import uploads as uploads_mod

    sess = uploads_mod.create_session(db, cfg, name=name, size=4096)
    uid = sess["upload_id"]
    temp = Path(db.scalar(
        "SELECT temp_path FROM upload_sessions WHERE id = ?", (uid,)))
    # 写点字节，证明清理确实删掉了文件而不只是改了数据库
    temp.write_bytes(bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"x" * 512)
    # 把过期时间推到过去
    past = time.time() - age_hours * 3600.0
    db.execute("UPDATE upload_sessions SET expires_at = ? WHERE id = ?", (past, uid))
    return uid, temp


def test_startup_purges_expired_uploads_and_removes_part_files(env) -> None:
    """★ 启动清理必须同时做到：删临时文件 + 把会话标成 expired。

    只删行不删文件 → 用户的照片库里留下一个永远没人管的 .part；
    只删文件不删行 → upload_sessions 无限增长，而且那个会话看起来
    还能继续传（再 PATCH 会报"临时文件已丢失"，用户莫名其妙）。
    """
    from backend.app.server import _purge_stale_uploads

    uid, temp = _make_stale_session(env.cfg, env.db)
    assert temp.is_file(), "夹具没造出临时文件"

    n = _purge_stale_uploads(env.cfg, env.state)

    assert n == 1, f"应清理 1 个过期会话，实际 {n}"
    assert not temp.exists(), "临时文件没被删掉 —— 它会永远留在照片库里"
    status = env.db.scalar("SELECT status FROM upload_sessions WHERE id = ?", (uid,))
    assert status == "expired", f"会话状态没更新：{status}"


def test_startup_purge_leaves_fresh_sessions_alone(env) -> None:
    """★ 正在上传的会话绝不能被启动清理误删。

    这是本功能最危险的失败模式：用户传了 1.9 GB / 2 GB，程序重启一次
    把临时文件删了，用户从 0 重新开始 —— 而且没有任何提示指向原因。
    """
    from backend.app import uploads as uploads_mod
    from backend.app.server import _purge_stale_uploads

    sess = uploads_mod.create_session(env.db, env.cfg, name="fresh.jpg", size=4096)
    temp = Path(env.db.scalar(
        "SELECT temp_path FROM upload_sessions WHERE id = ?",
        (sess["upload_id"],)))
    temp.write_bytes(b"y" * 512)

    n = _purge_stale_uploads(env.cfg, env.state)

    assert n == 0, "把未过期的会话也清掉了"
    assert temp.is_file(), "★ 正在上传的临时文件被删了"
    status = env.db.scalar("SELECT status FROM upload_sessions WHERE id = ?",
                           (sess["upload_id"],))
    assert status == "open", f"未过期会话被改成了 {status}"


def test_startup_purge_never_blocks_startup(env, monkeypatch) -> None:
    """★ 清理失败绝不能挡住启动 —— 返回 0 且不抛。

    最坏情况只是临时文件多留一会儿；而"程序起不来"是用户无法自助
    恢复的（他看不到日志，只会觉得双击没反应）。
    """
    from backend.app import uploads as uploads_mod
    from backend.app.server import _purge_stale_uploads

    def _boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(uploads_mod, "purge_expired", _boom)
    assert _purge_stale_uploads(env.cfg, env.state) == 0


def test_serve_calls_startup_purge() -> None:
    """★ 结构回归：serve() 里必须真的调了启动清理。

    用 AST 而不是文本匹配 —— 本仓库已经三次栽在"注释里出现了被搜索的
    词"（scripts/check_docs.py 把代码块里的正则当成断链、test_thumbs 的
    注释里有 wait_for）。
    """
    import ast
    import inspect
    import textwrap

    from backend.app import server as server_mod

    src = textwrap.dedent(inspect.getsource(server_mod.serve))
    tree = ast.parse(src)
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                called.add(f.id)
            elif isinstance(f, ast.Attribute):
                called.add(f.attr)
    assert "_purge_stale_uploads" in called, (
        "serve() 没有调用启动清理 —— 强杀留下的 .part 会永远堆在照片库里")


def test_startup_purge_runs_before_transport_listens() -> None:
    """★ 结构回归：清理必须在 transport.start() **之前**完成。

    顺序反了会有一个窗口：手机已经能发请求、PATCH 打到一个正在被删的
    会话上。启动阶段本来就不该并发，串行做完再开门。
    """
    import ast
    import inspect
    import textwrap

    from backend.app import server as server_mod

    src = textwrap.dedent(inspect.getsource(server_mod.serve))
    tree = ast.parse(src)

    def _first_line_of(pred) -> "int | None":
        hits = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call) and pred(n)]
        return min(hits) if hits else None

    def _is_purge(call: ast.Call) -> bool:
        f = call.func
        return isinstance(f, ast.Name) and f.id == "_purge_stale_uploads"

    def _is_transport_start(call: ast.Call) -> bool:
        f = call.func
        if not (isinstance(f, ast.Attribute) and f.attr == "start"):
            return False
        base = f.value
        return ((isinstance(base, ast.Name) and base.id == "transport")
                or (isinstance(base, ast.Attribute) and base.attr == "transport"))

    purge = _first_line_of(_is_purge)
    listen = _first_line_of(_is_transport_start)
    assert purge is not None, "serve() 里找不到启动清理"
    assert listen is not None, "serve() 里找不到 transport.start()"
    assert purge < listen, (
        f"启动清理（第 {purge} 行）应在 transport.start()（第 {listen} 行）之前")


# --------------------------------------------------- bind_scope 的推导（SEC-17）


def test_bind_scope_follows_the_real_listen_address() -> None:
    """`bind_scope` 必须由 `server.host` 推导（SEC-17）。

    之前它是个硬编码常量 `"private"`：把 `server.host` 改成 `0.0.0.0` 之后
    设置页仍然显示"仅私有网络"，等于告诉用户"没有对外开放"——恰好说反了。
    """
    from backend.app.handlers.system import _bind_scope

    assert _bind_scope("127.0.0.1") == "loopback"
    assert _bind_scope("::1") == "loopback"
    assert _bind_scope("[::1]") == "loopback"
    assert _bind_scope("localhost") == "loopback"
    assert _bind_scope("") == "loopback"
    assert _bind_scope("0.0.0.0") == "any"
    assert _bind_scope("::") == "any"
    assert _bind_scope("192.0.2.10") == "private"
    assert _bind_scope("10.1.2.3") == "private"
    assert _bind_scope("100.64.0.9") == "private"      # CGNAT
    assert _bind_scope("fd00::1") == "private"         # IPv6 ULA
    assert _bind_scope("8.8.8.8") == "public"
    # 认不出的写法不猜
    assert _bind_scope("my-pc.local") == "custom"
