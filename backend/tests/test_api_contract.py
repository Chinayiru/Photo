"""前后端字段契约测试 —— 防止后端改字段名而前端静默失效。

为什么单独一个文件
------------------
`frontend/src/api/types.ts` 里的 interface 是**手工照抄**后端输出的，
不是生成的。抄错或后端改了名，症状极其难查：

* 后端把 `file_name` 改成 `name`，前端仍读 `item.file_name`
  → 界面上文件名一片空白，**没有任何报错**（JS 读不存在的属性得 undefined）
* 后端把 `thumb_v` 去掉，前端仍拼 `?v=undefined`
  → 缩略图 URL 变得与不带版本参数一样，而 `Cache-Control: immutable`
    会让用户替换照片后永远看旧图
* 后端把时间戳从"秒"改成"毫秒"，前端按秒格式化
  → 所有照片显示成 1970 年或 5 万年以后

这些都不是类型检查能发现的（TS 的 interface 是我们自己写的，
它只会验证我们**自己写的代码**自洽）。所以这里做两件事：

1. 从 `types.ts` 里把每个 interface 声明的字段名抠出来；
2. 真起服务，对每个接口发真实请求，断言响应里**每个声明过的字段都在**。

反过来的方向（响应里有 types.ts 没声明的字段）只警告不失败 ——
后端多返回一个字段无害，前端漏声明才是 bug。
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from backend.app.config import Config                                # noqa: E402
from backend.app.db import make_db                                   # noqa: E402
from backend.app.server import build_app, build_state                # noqa: E402

TYPES_TS = REPO / "frontend" / "src" / "api" / "types.ts"

#: 从 types.ts 抠出 interface 字段。只取顶层字段，跳过注释。
_IFACE_RE = re.compile(r"export interface (\w+)(?:\s+extends\s+(\w+))?\s*\{(.*?)\n\}", re.S)
#: 第二组捕获 `?` —— TS 里 `foo?: T` 是**可选**字段，后端不给也合法。
#: 不区分的话，`final_rel?: string | null`（秒传未命中时后端确实不返回）
#: 会被当成缺失字段而误报。
_FIELD_RE = re.compile(r"^\s{2}(\w+)(\?)?\s*[?:]", re.M)
_COMMENT_LINE_RE = re.compile(r"^\s*(/\*|\*|//)")


def _parse_types() -> dict[str, tuple[str | None, list[str], list[str]]]:
    """`{interface 名: (父接口, 必需字段, 可选字段)}`。"""
    if not TYPES_TS.is_file():
        pytest.skip("frontend/src/api/types.ts 不存在")
    text = TYPES_TS.read_text(encoding="utf-8")

    out: dict[str, tuple[str | None, list[str], list[str]]] = {}
    for m in _IFACE_RE.finditer(text):
        name, parent, body = m.group(1), m.group(2), m.group(3)
        # 去掉注释行，否则注释里的 " * item.foo" 之类会被误当字段
        body = "\n".join(
            ln for ln in body.splitlines() if not _COMMENT_LINE_RE.match(ln)
        )
        req: list[str] = []
        opt: list[str] = []
        for fm in _FIELD_RE.finditer(body):
            (opt if fm.group(2) else req).append(fm.group(1))
        out[name] = (parent, req, opt)
    return out


def _declared_fields() -> dict[str, list[str]]:
    """`{interface 名: [必需字段名, ...]}`，含 `extends` 的父接口字段。

    只返回**必需**字段（TS 里不带 `?` 的）。可选字段见 `_declared_optional()`。
    """
    raw = _parse_types()
    out: dict[str, list[str]] = {}

    def resolve(name: str, seen: set[str]) -> list[str]:
        if name in out:
            return out[name]
        if name in seen or name not in raw:
            return []
        seen.add(name)
        parent, req, _opt = raw[name]
        merged = list(resolve(parent, seen)) if parent else []
        merged += [f for f in req if f not in merged]
        out[name] = merged
        return merged

    for name in raw:
        resolve(name, set())
    return out


def _declared_optional() -> dict[str, list[str]]:
    """`{interface 名: [可选字段名, ...]}`（TS 里带 `?` 的）。"""
    raw = _parse_types()
    out: dict[str, list[str]] = {}
    for name, (_parent, _req, opt) in raw.items():
        out[name] = list(opt)
    return out


# ---------------------------------------------------------------------------
# 起一个真服务（与 test_server.py 同样的做法：后台线程跑 uvicorn）
# ---------------------------------------------------------------------------

class _Headers(dict):
    """大小写不敏感的响应头视图（uvicorn 一律发小写）。"""

    def __init__(self, items=()):
        super().__init__((k.lower(), v) for k, v in items)

    def __getitem__(self, k):
        return super().__getitem__(k.lower())

    def __contains__(self, k):
        return super().__contains__(k.lower())

    def get(self, k, default=None):
        return super().get(k.lower(), default)


class _Response:
    def __init__(self, status: int, headers, body: bytes):
        self.status = status
        self.headers = _Headers(headers)
        self.body = body

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class _Server:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = None
        self.transport = None
        self.loop = None
        self.port = 0
        self._thread = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def start(self, timeout: float = 30.0) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="contract-server")
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"服务启动超时：{self._error!r}")
        if self._error:
            raise RuntimeError(f"服务启动失败：{self._error!r}")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:       # noqa: BLE001
            self._error = exc
            self._ready.set()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:              # noqa: BLE001
                pass
            loop.close()

    async def _serve(self) -> None:
        # ★ 必须传 db=。不传的话 `build_state` 会回退到全局单例
        # `get_db()`，而它按磁盘上的 data/config.json 解析路径 ——
        # 在开发机上那就是**用户真实的照片库索引**。
        # 这个测试曾经因此把 4915 行真实记录标成 missing，
        # 用户的时间线变成空白。`make_db(cfg)` 永远按传入的 cfg
        # 解析路径，从源头杜绝。
        db = make_db(self.cfg)
        db.init()
        self.db = db
        st = build_state(self.cfg, db=db)
        _, transport, st = build_app(self.cfg, state=st)
        self.state, self.transport = st, transport
        await transport.start()
        self.port = transport.port_actual or transport.port
        # 自己造一个停止事件：LocalHttpTransport 只负责 uvicorn，
        # 没有"保持进程活着"的语义（那是 server.serve() 的职责）。
        self._stop_ev = asyncio.Event()
        self._ready.set()
        await self._stop_ev.wait()
        await transport.stop()
        db.close()

    def stop(self) -> None:
        if self.loop is None:
            return
        ev = getattr(self, "_stop_ev", None)
        if ev is not None:
            try:
                self.loop.call_soon_threadsafe(ev.set)
            except Exception:                  # noqa: BLE001
                pass
        if self._thread:
            self._thread.join(timeout=10)

    def request(self, method: str, path: str, *, token: str | None = None,
                body: dict | None = None, raw: bool = False):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = resp.read()
                return _Response(resp.status, resp.headers.items(), payload)
        except urllib.error.HTTPError as e:
            return _Response(e.code, e.headers.items(), e.read())

    def call(self, coro, timeout: float = 60.0):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)


def _make_photo(path: Path, *, taken: str, color=(180, 90, 40)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (160, 120), color)
    ex = Image.Exif()
    # ★ DateTimeOriginal(0x9003) 必须写进 Exif SubIFD(0x8769)，
    #   直接挂顶层时 Image.open().getexif().get(0x9003) 读不到。
    ex.get_ifd(0x8769)[0x9003] = taken
    ex.get_ifd(0x8769)[0x9004] = taken
    im.save(path, "JPEG", exif=ex)


@pytest.fixture()
def srv(tmp_path: Path):
    lib = tmp_path / "lib"
    _make_photo(lib / "2024" / "2024-05" / "a.jpg", taken="2024:05:12 18:30:00")
    _make_photo(lib / "2024" / "2024-05" / "b.jpg", taken="2024:05:12 18:30:01")
    _make_photo(lib / "青甘环线" / "敦煌.jpg", taken="2023:07:08 10:11:12")

    data = tmp_path / "data"
    cfg = Config(
        {
            # `panel_http: True` —— 本文件要遍历全部接口做契约校验，
            # `_token()` 必须能通过 HTTP 取到配对码。生产默认 False
            # （审计 F1-b：本机浏览器与用户本人无从区分）。
            "server": {"host": "127.0.0.1", "port": 0, "allow_lan": False,
                       "panel_http": True},
            "roots": [{
                "id": "library", "path": str(lib), "label": "库",
                "mode": "readwrite", "enabled": True, "recursive": True,
            }],
            "auth": {"require_pairing": True, "allow_delete": True},
            "index": {"scan_on_start": False, "watch": False},
        },
        path=data / "config.json",
        data_dir=data,
    )
    s = _Server(cfg)
    s.start()
    try:
        s.call(s.state.indexer.scan_all())
        yield s
    finally:
        s.stop()


def _token(s: _Server) -> str:
    r = s.request("POST", "/panel/pair-code")
    assert r.status == 201, r.body
    code = r.json()["code"]
    r = s.request("POST", "/api/pair", body={"code": code})
    assert r.status == 201, r.body
    return r.json()["client_token"]


def _assert_fields(payload: dict, interface: str, *, where: str) -> list[str]:
    """断言 payload 含 interface 声明的全部**必需**字段，返回多出来的字段名。

    可选字段（TS 里的 `foo?:`）不强制要求 —— 后端在某些分支下确实不返回它们，
    例如 `UploadInitResponse.final_rel?` 只在秒传命中时才有值。
    """
    declared = _declared_fields().get(interface)
    assert declared, f"types.ts 里找不到 interface {interface}"
    missing = [f for f in declared if f not in payload]
    assert not missing, (
        f"{where} 的响应缺少 {interface} 声明的字段：{missing}\n"
        f"  实际字段：{sorted(payload)}\n"
        f"  （后端改了字段名而 frontend/src/api/types.ts 没跟着改）"
    )
    return [k for k in payload if k not in declared]


def _assert_no_unknown_fields(payload: dict, interface: str, *, where: str) -> None:
    """严格版：连可选字段也不许出现未声明的键。

    用在"响应结构固定"的接口上，比 `_assert_fields` 更能发现漂移。
    """
    declared = _declared_fields().get(interface, [])
    optional = _declared_optional().get(interface, [])
    allowed = set(declared) | set(optional)
    extra = sorted(k for k in payload if k not in allowed)
    assert not extra, (
        f"{where} 出现了 {interface} 未声明的字段：{extra}\n"
        f"  （后端加了字段但 types.ts 没同步；前端读不到没关系，"
        f"但类型就骗人了）"
    )


# ---------------------------------------------------------------------------
# 逐接口比对
# ---------------------------------------------------------------------------

def test_ping_matches_types(srv) -> None:
    extra = _assert_fields(srv.request("GET", "/api/system/ping").json(),
                           "PingResponse", where="GET /api/system/ping")
    assert extra == [], f"多出未声明字段：{extra}"


def test_system_info_matches_types(srv) -> None:
    tok = _token(srv)
    body = srv.request("GET", "/api/system/info", token=tok).json()
    _assert_fields(body, "SystemInfo", where="GET /api/system/info")
    _assert_fields(body["roots"][0], "RootInfo", where="SystemInfo.roots[0]")
    _assert_fields(body["index"], "IndexInfo", where="SystemInfo.index")
    _assert_fields(body["limits"], "LimitsInfo", where="SystemInfo.limits")


def test_system_stats_matches_types(srv) -> None:
    tok = _token(srv)
    _assert_fields(srv.request("GET", "/api/system/stats", token=tok).json(),
                   "SystemStats", where="GET /api/system/stats")


def test_timeline_matches_types(srv) -> None:
    tok = _token(srv)
    body = srv.request("GET", "/api/timeline", token=tok).json()
    _assert_fields(body, "TimelineResponse", where="GET /api/timeline")
    _assert_fields(body["items"][0], "MediaItem", where="timeline.items[0]")
    assert body["items"][0]["name"] == "b.jpg", "轻量项必须带文件名"
    assert body["items"][0]["dir"] == "2024/2024-05", "轻量项必须带所在目录"
    # 缩略图版本串必须能拼进 URL —— 后端发 immutable，没它就会永远看旧图
    assert body["items"][0]["thumb_v"], "thumb_v 不能为空，否则 ?v= 失效"


def test_media_detail_matches_types(srv) -> None:
    tok = _token(srv)
    items = srv.request("GET", "/api/timeline", token=tok).json()["items"]
    mid = items[0]["id"]
    body = srv.request("GET", f"/api/items/{mid}", token=tok).json()
    _assert_fields(body, "MediaDetail", where=f"GET /api/items/{mid}")
    _assert_fields(body["exif"], "ExifInfo", where="MediaDetail.exif")
    _assert_fields(body["urls"], "urls", where="MediaDetail.urls") \
        if "urls" in _declared_fields() else None
    for key in ("thumb", "preview", "raw"):
        assert key in body["urls"], f"MediaDetail.urls 缺 {key}"


def test_search_matches_types(srv) -> None:
    tok = _token(srv)
    q = urllib.parse.quote("敦煌")
    body = srv.request("GET", f"/api/search?q={q}", token=tok).json()
    _assert_fields(body, "TimelineResponse", where="GET /api/search")
    _assert_fields(body["items"][0], "MediaItem", where="search.items[0]")


def test_folders_matches_types(srv) -> None:
    tok = _token(srv)
    body = srv.request("GET", "/api/folders", token=tok).json()
    _assert_fields(body, "FoldersResponse", where="GET /api/folders")
    _assert_fields(body["folders"][0], "FolderEntry", where="folders[0]")


def test_upload_init_matches_types(srv) -> None:
    tok = _token(srv)
    body = srv.request("POST", "/api/uploads/init", token=tok,
                       body={"name": "x.jpg", "size": 128}).json()
    _assert_fields(body, "UploadInitResponse", where="POST /api/uploads/init")
    _assert_no_unknown_fields(body, "UploadInitResponse",
                              where="POST /api/uploads/init")
    assert body["upload_id"], "新建会话必须返回 upload_id"
    assert body["chunk_size"] > 0, "前端靠它决定切片大小"


def test_upload_status_and_complete_match_types(srv) -> None:
    tok = _token(srv)
    # 先造出要传的字节，**再**按它的真实长度 init。
    # 反过来的话（init 声明 64 而只发 60 字节）PATCH 会以
    # `E1001 分片声明的总大小与上传会话不符` 拒绝 —— 这是服务端的正确行为，
    # 但测试会看起来像"接口坏了"。
    payload = b"\xff\xd8\xff\xe0" + b"z" * 60

    init = srv.request("POST", "/api/uploads/init", token=tok,
                       body={"name": "y.jpg", "size": len(payload)}).json()
    uid = init["upload_id"]

    st = srv.request("GET", f"/api/uploads/{uid}", token=tok).json()
    _assert_fields(st, "UploadStatusResponse", where="GET /api/uploads/{id}")
    _assert_no_unknown_fields(st, "UploadStatusResponse",
                              where="GET /api/uploads/{id}")

    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/api/uploads/{uid}",
        data=payload, method="PATCH",
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Range": f"bytes 0-{len(payload) - 1}/{len(payload)}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 206

    done = srv.request("POST", f"/api/uploads/{uid}/complete", token=tok,
                       body={"sha256": __import__("hashlib").sha256(payload).hexdigest()}).json()
    _assert_fields(done, "UploadCompleteResponse",
                   where="POST /api/uploads/{id}/complete")
    _assert_no_unknown_fields(done, "UploadCompleteResponse",
                              where="POST /api/uploads/{id}/complete")


def test_oversized_chunk_reports_chunk_error_not_file_too_large(srv) -> None:
    """超过服务端分片大小的 PATCH 必须报 E1007「分片过大」。

    2026-10-23 云隧道实测：2 MB 分片多带 8 KB，客户端收到的是
    **E1006「文件超过 2 GB 上限」**（`ctx.body_bytes` 的上限是按分片算的，
    抛的却是文件级的错误码）。客户端看到 E1006 会以为整个文件超限、
    直接放弃上传，而实际只超了一片、切小一点就能过。
    """
    tok = _token(srv)
    total = 10 * 1024 * 1024
    init = srv.request("POST", "/api/uploads/init", token=tok,
                       body={"name": "big.jpg", "size": total}).json()
    uid = init["upload_id"]
    chunk = int(init["chunk_size"])

    over = b"x" * (chunk + 2048)          # 越过 `chunk_size + 1024` 的容差
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/api/uploads/{uid}",
        data=over, method="PATCH",
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Range": f"bytes 0-{len(over) - 1}/{total}"},
    )
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req, timeout=30)
    assert ei.value.code == 413
    body = json.loads(ei.value.read().decode())
    assert body["error"]["code"] == "E1007", body
    assert "2 GB" not in body["error"].get("user_hint", ""), body


def test_error_envelope_matches_types(srv) -> None:
    body = srv.request("GET", "/api/timeline").json()      # 无 token
    _assert_fields(body, "ApiErrorEnvelope", where="401 错误体")
    _assert_fields(body["error"], "ApiErrorBody", where="错误体 .error")
    assert body["error"]["code"] == "E2002"


def test_no_declared_field_is_never_present(srv) -> None:
    """反向检查：types.ts 里声明了、但所有真实响应里都没出现过的字段。

    这通常意味着 interface 是照着**旧版文档**抄的而不是照代码 ——
    比如声明了 `thumb_url` 而实际字段叫 `thumb_v`。
    只对"核心对象"做这个检查，因为可选字段（`sha256`）确实可能为 null
    但键仍在，而某些 interface 只在特定分支返回。
    """
    tok = _token(srv)
    seen: dict[str, set[str]] = {}

    def note(iface: str, payload: dict) -> None:
        seen.setdefault(iface, set()).update(payload)

    note("PingResponse", srv.request("GET", "/api/system/ping").json())
    info = srv.request("GET", "/api/system/info", token=tok).json()
    note("SystemInfo", info)
    note("RootInfo", info["roots"][0])
    note("IndexInfo", info["index"])
    note("LimitsInfo", info["limits"])
    note("SystemStats", srv.request("GET", "/api/system/stats", token=tok).json())

    tl = srv.request("GET", "/api/timeline", token=tok).json()
    note("TimelineResponse", tl)
    note("MediaItem", tl["items"][0])

    detail = srv.request("GET", f"/api/items/{tl['items'][0]['id']}", token=tok).json()
    note("MediaDetail", detail)
    note("ExifInfo", detail["exif"])

    note("FoldersResponse", srv.request("GET", "/api/folders", token=tok).json())

    declared = _declared_fields()
    problems = []
    for iface, keys in seen.items():
        # PageInfo 是 extends 的父接口，字段已并进 TimelineResponse
        if iface == "PageInfo":
            continue
        absent = [f for f in declared.get(iface, []) if f not in keys]
        if absent:
            problems.append(f"{iface}: 声明了但响应里从未出现 {absent}")

    assert not problems, "types.ts 与真实响应不一致：\n  " + "\n  ".join(problems)


def test_types_ts_covers_every_route(srv) -> None:
    """前端引用的接口都应有对应的 interface 声明（防止"能跑但没类型"）。"""
    declared = _declared_fields()
    # 这里只做一个下限检查：核心对象必须有类型声明
    for required in ("MediaItem", "MediaDetail", "TimelineResponse", "SystemInfo",
                     "UploadInitResponse", "PairResponse", "ApiErrorBody"):
        assert required in declared, f"types.ts 缺少 {required}"


# ---------------------------------------------------------------------------
# 覆盖门禁：types.ts 的每个 interface 都必须有人证（F3 收尾）
# ---------------------------------------------------------------------------
#
# 为什么要有这张表
# ----------------
# 2026-11-01 的架构审查（F3）统计：types.ts 声明了 32 个 interface，
# 而这个文件当时只对 19 个做过真实断言 —— 漏掉的 13 个恰好是
# "改起来最容易忘"的一批（删除/移动/改名/重扫的响应、设备列表、
# 上传分片的 PATCH/ABORT 响应、VideoInfo/WatchInfo）。
#
# 漏了不会红：后端把 `error` 改成 `code`，前端仍读 `f.error`，
# 界面只是"删除失败时提示里少一句原因"，没有人会为此提 bug。
# 所以覆盖率不能靠"上次好像加过"，必须由一张登记表 + 一条测试钉住：
#
# * 新增 interface 却没登记 → 失败（提醒你补断言，别只补类型）
# * 登记了但 types.ts 已删除 → 失败（提醒你清理死登记）
# * 登记的函数名不存在 → 失败（防止把没写的东西先登进来）
COVERED: dict[str, str] = {
    "ApiErrorBody": "test_error_envelope_matches_types",
    "ApiErrorEnvelope": "test_error_envelope_matches_types",
    # PageInfo 是 TimelineResponse 的 extends 父接口，字段已并进子接口断言
    "PageInfo": "test_timeline_matches_types",
    "PingResponse": "test_ping_matches_types",
    "RootInfo": "test_system_info_matches_types",
    "IndexInfo": "test_system_info_matches_types",
    "WatchInfo": "test_watch_info_matches_types",
    "LimitsInfo": "test_system_info_matches_types",
    "SystemInfo": "test_system_info_matches_types",
    "SystemStats": "test_system_stats_matches_types",
    "MediaItem": "test_timeline_matches_types",
    "TimelineResponse": "test_timeline_matches_types",
    "ExifInfo": "test_media_detail_matches_types",
    "VideoInfo": "test_video_info_matches_types",
    "MediaDetail": "test_media_detail_matches_types",
    "FolderEntry": "test_folders_matches_types",
    "FoldersResponse": "test_folders_matches_types",
    "DeleteResponse": "test_delete_matches_types",
    "MoveResponse": "test_move_matches_types",
    "RenameResponse": "test_rename_matches_types",
    "RescanResponse": "test_rescan_matches_types",
    "UploadInitRequest": "test_upload_init_request_fields_are_consumed",
    "UploadInitResponse": "test_upload_init_matches_types",
    "UploadPatchResponse": "test_upload_patch_and_abort_match_types",
    "UploadStatusResponse": "test_upload_status_and_complete_match_types",
    "UploadCompleteResponse": "test_upload_status_and_complete_match_types",
    "UploadAbortResponse": "test_upload_patch_and_abort_match_types",
    "PairRequest": "test_pair_request_matches_types",
    "PairResponse": "test_pair_request_matches_types",
    "MeResponse": "test_me_and_devices_match_types",
    "DeviceEntry": "test_me_and_devices_match_types",
    "DevicesResponse": "test_me_and_devices_match_types",
}


def test_every_declared_interface_has_a_contract_check() -> None:
    """types.ts 里出现的每个 interface 都必须登记到 `COVERED`。

    这条测试**不碰服务器** —— 它只是把"覆盖率"变成可判定的，
    从而让上面那 13 个漏网的接口不会再悄悄多出第 14 个。
    """
    declared = set(_parse_types())
    missing = sorted(declared - set(COVERED))
    stale = sorted(set(COVERED) - declared)

    assert not missing, (
        "types.ts 里这些 interface 还没有契约断言：\n  "
        + "\n  ".join(missing)
        + "\n  → 补一条对真实响应/真实产出函数的断言，再登记进 COVERED"
    )
    assert not stale, (
        "COVERED 里登记了 types.ts 已不存在的 interface：\n  "
        + "\n  ".join(stale)
        + "\n  → 删掉这些死登记（它们会让覆盖率看起来比实际高）"
    )

    dangling = sorted(
        f"{iface} -> {fn}" for iface, fn in COVERED.items()
        if not callable(globals().get(fn))
    )
    assert not dangling, (
        "COVERED 指向了本文件里不存在的测试函数：\n  " + "\n  ".join(dangling)
    )


# ---------------------------------------------------------------------------
# 写操作与设备：删除 / 移动 / 改名 / 重扫 / 我 / 设备列表
# ---------------------------------------------------------------------------

def test_delete_matches_types(srv) -> None:
    tok = _token(srv)
    items = srv.request("GET", "/api/timeline", token=tok).json()["items"]
    victim = items[0]["id"]

    body = srv.request("POST", "/api/items/delete", token=tok,
                       body={"ids": [victim, 10 ** 9]}).json()
    _assert_fields(body, "DeleteResponse", where="POST /api/items/delete")
    _assert_no_unknown_fields(body, "DeleteResponse",
                              where="POST /api/items/delete")
    assert body["deleted"] == 1, body
    # `failed` 的元素是 `{id, error}`，不是错误码字符串 —— 前端
    # `DeleteResponse` 里那句话（"⚠️ 不是数字"）写的正是这一点。
    assert body["failed"] == [{"id": 10 ** 9, "error": "not_found"}], body


def test_move_matches_types(srv) -> None:
    tok = _token(srv)
    items = srv.request("GET", "/api/timeline", token=tok).json()["items"]
    mid = items[0]["id"]

    body = srv.request("POST", "/api/items/move", token=tok,
                       body={"ids": [mid], "target_dir": "2024"}).json()
    _assert_fields(body, "MoveResponse", where="POST /api/items/move")
    _assert_no_unknown_fields(body, "MoveResponse",
                              where="POST /api/items/move")
    assert [m["id"] for m in body["moved"]] == [mid], body
    assert body["moved"][0]["to"].startswith("2024/"), body
    assert body["skipped"] == [] and body["failed"] == [], body
    assert body["target_dir"] == "2024", body

    # 失败项是 `{id, code, message}` 三件套（与 DeleteResponse 的
    # `{id, error}` 不同）—— 前端按 `code` 走错误映射表，按
    # `message` 兜底，两个都必须真的存在。
    bad = srv.request("POST", "/api/items/move", token=tok,
                      body={"ids": [10 ** 9], "target_dir": "2024"}).json()
    assert len(bad["failed"]) == 1, bad
    assert set(bad["failed"][0]) == {"id", "code", "message"}, bad


def test_rename_matches_types(srv) -> None:
    tok = _token(srv)
    items = srv.request("GET", "/api/timeline", token=tok).json()["items"]
    mid = items[0]["id"]

    body = srv.request("POST", "/api/items/rename", token=tok,
                       body={"id": mid, "new_name": "renamed.jpg"}).json()
    _assert_fields(body, "RenameResponse", where="POST /api/items/rename")
    _assert_no_unknown_fields(body, "RenameResponse",
                              where="POST /api/items/rename")
    assert body["to"].endswith("/renamed.jpg"), body
    # 改名可能改变 taken_at（文件名里原本带日期），所以这两个字段是
    # 必需项而不是可选项 —— 前端要靠 response 回写时间线。
    assert body["taken_at"] > 0 and body["taken_src"], body

    # 同名改名是幂等成功：不写审计、路径原样返回
    again = srv.request("POST", "/api/items/rename", token=tok,
                        body={"id": mid, "new_name": "renamed.jpg"}).json()
    assert again["from"] == again["to"] == body["to"], again


def test_rescan_matches_types(srv) -> None:
    tok = _token(srv)
    r = srv.request("POST", "/api/index/rescan", token=tok,
                    body={"root": "library"})
    assert r.status == 202, r.body
    body = r.json()
    _assert_fields(body, "RescanResponse", where="POST /api/index/rescan")
    _assert_no_unknown_fields(body, "RescanResponse",
                              where="POST /api/index/rescan")
    assert body["started"] is True and body["root"] == "library", body


def test_me_and_devices_match_types(srv) -> None:
    tok = _token(srv)
    me = srv.request("GET", "/api/clients/me", token=tok).json()
    _assert_fields(me, "MeResponse", where="GET /api/clients/me")
    _assert_no_unknown_fields(me, "MeResponse", where="GET /api/clients/me")

    devs = srv.request("GET", "/api/devices", token=tok).json()
    _assert_fields(devs, "DevicesResponse", where="GET /api/devices")
    _assert_no_unknown_fields(devs, "DevicesResponse", where="GET /api/devices")
    assert devs["items"], "刚配对过的客户端必须出现在设备列表里"
    _assert_fields(devs["items"][0], "DeviceEntry", where="GET /api/devices items[0]")
    _assert_no_unknown_fields(devs["items"][0], "DeviceEntry",
                              where="GET /api/devices items[0]")
    assert me["client_id"] in {d["client_id"] for d in devs["items"]}, devs


# ---------------------------------------------------------------------------
# 上传：请求体（反向）+ PATCH / ABORT 响应
# ---------------------------------------------------------------------------

def test_upload_init_request_fields_are_consumed(srv) -> None:
    """`UploadInitRequest` 是**请求体**，只能反向验：发全字段，看后端接不接。

    正向断言（响应里有声明字段）对请求体没有意义 —— 漂移方向是
    "前端多写了一个后端不认识的键"，后端会静默忽略，症状是
    "设置了目标目录却传到了默认目录"。所以这里发一整套声明字段，
    再从 `GET /api/uploads/{id}` 把被**消费**掉的字段读回来。
    """
    tok = _token(srv)
    req = {
        "name": "req-check.jpg",
        "size": 64,
        "mtime": 1_700_000_000.0,
        "sha256": "0" * 64,
        "root": "library",
        "target_dir": "2024",
        "declared_taken_at": 1_699_999_999.0,
    }
    init = srv.request("POST", "/api/uploads/init", token=tok, body=req)
    assert init.status == 201, init.body
    _assert_fields(init.json(), "UploadInitResponse",
                   where="POST /api/uploads/init")

    st = srv.request("GET", f"/api/uploads/{init.json()['upload_id']}",
                     token=tok).json()
    assert st["declared_name"] == "req-check.jpg", st
    assert (st["target_rel"] or "").strip("/") == "2024", st


def test_upload_patch_and_abort_match_types(srv) -> None:
    tok = _token(srv)
    total = 64
    init = srv.request("POST", "/api/uploads/init", token=tok,
                       body={"name": "chunk.jpg", "size": total}).json()
    uid = init["upload_id"]

    part = b"\xff\xd8\xff\xe0" + b"c" * 28      # 前 32 字节，留一半
    assert len(part) == total // 2
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/api/uploads/{uid}",
        data=part, method="PATCH",
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Range": f"bytes 0-{len(part) - 1}/{total}"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        assert resp.status == 206
        patched = json.loads(resp.read().decode("utf-8"))

    _assert_fields(patched, "UploadPatchResponse", where="PATCH /api/uploads/{id}")
    _assert_no_unknown_fields(patched, "UploadPatchResponse",
                              where="PATCH /api/uploads/{id}")
    # `offset` 是"以服务端为准"的那一个，客户端不许自己加
    assert patched["offset"] == len(part), patched

    aborted = srv.request("DELETE", f"/api/uploads/{uid}", token=tok).json()
    _assert_fields(aborted, "UploadAbortResponse",
                   where="DELETE /api/uploads/{id}")
    _assert_no_unknown_fields(aborted, "UploadAbortResponse",
                              where="DELETE /api/uploads/{id}")
    assert aborted["upload_id"] == uid and aborted["aborted"] is True, aborted


def test_pair_request_matches_types(srv) -> None:
    """`PairRequest` 只有 `code` 一个字段：它的契约就是"别的什么都不用发"。"""
    r = srv.request("POST", "/panel/pair-code")
    assert r.status == 201, r.body
    res = srv.request("POST", "/api/pair", body={"code": r.json()["code"]})
    assert res.status == 201, res.body
    body = res.json()
    _assert_fields(body, "PairResponse", where="POST /api/pair")
    _assert_no_unknown_fields(body, "PairResponse", where="POST /api/pair")


# ---------------------------------------------------------------------------
# 非响应体：VideoInfo / WatchInfo 由函数产出，直接对产出函数断言
# ---------------------------------------------------------------------------

def test_video_info_matches_types(srv) -> None:
    """`VideoInfo` 只在视频详情里出现，而 fixture 里全是 jpg。

    与其为了一个结构断言造一个真 MP4（还要 moov/faststart 探测），
    不如直接对产出它的 `_video_block()` 断言 —— 漂移发生在那里，
    修也在那里。
    """
    from backend.app.handlers import common as common_mod

    block = common_mod._video_block({
        "kind": "video", "container": "mp4", "vcodec": "h264",
        "acodec": "aac", "duration": 12.5, "faststart": 1,
    })
    assert block is not None
    _assert_fields(block, "VideoInfo", where="_video_block(mp4/h264/aac)")
    _assert_no_unknown_fields(block, "VideoInfo",
                              where="_video_block(mp4/h264/aac)")
    assert block["hint"] == "ok" and block["reason"] == "", block
    # 图片必须返回 None —— 前端靠这个 null 判断"要不要显示视频控件"
    assert common_mod._video_block({"kind": "image"}) is None


def test_watch_info_matches_types(srv) -> None:
    info = srv.state.watch_info()
    _assert_fields(info, "WatchInfo", where="state.watch_info()")
    _assert_no_unknown_fields(info, "WatchInfo", where="state.watch_info()")
