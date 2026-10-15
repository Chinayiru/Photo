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
            "server": {"host": "127.0.0.1", "port": 0, "allow_lan": False},
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
    # 轻量项字段集合
    assert set(items[0]) == {
        "id", "name", "taken_at", "taken_src", "w", "h", "thumb_v",
        "size", "thumb_state", "has_gps", "root", "dir",
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
    r = env.post("/api/uploads/init", token=token,
                 json_body={"name": "movie.mp4", "size": 1024})
    assert r.status == 400
    assert r.json()["error"]["code"] == "E1017"


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
    assert info["thumbs"]["sizes"] == [320, 1600]
    assert info["limits"]["max_file_bytes"] == 2048 * 1024 * 1024
    assert info["server"]["bind_scope"] == "private"
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
    (env.library / row["rel_path"]).unlink()

    r = env.get(f"/api/items/{mid}/thumb?size=320", token=token)
    assert r.status == 200, r.text
    assert r.body[:4] == b"RIFF"

    # 原图必须明确报错（不能拿占位图冒充原图 —— 用户会以为自己
    # 下载到了照片，存进相册才发现是一张灰图）
    r = env.get(f"/api/items/{mid}/raw", token=token)
    assert r.status >= 400


def test_missing_file_is_flagged_after_rescan(env):
    """重扫之后 missing 必须为 1，前端才知道该显示"找不到了"。"""
    token = _pair(env)
    _index(env)
    item = env.get("/api/timeline?limit=1", token=token).json()["items"][0]
    mid = item["id"]
    row = env.db.query_one("SELECT rel_path FROM media WHERE id = ?", (mid,))
    (env.library / row["rel_path"]).unlink()

    _index(env)
    r = env.get(f"/api/items/{mid}", token=token)
    assert r.status == 200, r.text
    assert r.json()["missing"] == 1
