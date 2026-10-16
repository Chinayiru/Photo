"""TC-SWITCH-FUNC-006 / AC-6.7：通路抽象一致性。

## 这条用例要证的到底是什么

设计原则 DP-4 是「两条通路，一套 API」：业务逻辑写成协议无关的中间
表示（`Ctx` → `Reply`），传输适配层只做协议翻译。这带来一个**很强的
断言**：同一个请求，经 `local_http` 适配层走一圈，与直接调用
`Router.dispatch(ctx)`，返回的内容必须**逐字节相同**。

这不是形式主义的对称性。二期要接云端隧道（wss），届时 `Reply` 会被
翻译成隧道帧而不是 HTTP 响应。如果业务代码里混进了传输细节
（比如某个 handler 自己拼 HTTP 头、自己判断 `Connection: close`、
或者依赖 `Reply.headers` 里天然存在的某个键），局域网这边一切正常，
隧道那边就会静默出错 —— 而不做对比测试的话，问题要到二期上线
才会暴露，那时已经没法归因了。

## 为什么必须双向都跑真东西

* 只测「直接调用 handler」：适配层的翻译错误（漏头、拼错 Content-Type、
  流式 body 被吞）完全测不到。
* 只测「真实 HTTP」：没有对照组，等于在重复 HTTP 层自己的测试。

所以这里**同一个 `Router` + 同一个 `AppState`**，一路派发到 `Reply`，
另一路经 `LocalHttpTransport.build_app()` 的真实 ASGI 应用（后台线程里
跑 uvicorn），再把两边逐字段比对。

## 有意不比的字段

| 字段 | 为什么 |
|---|---|
| `date` / `server` | uvicorn 自己加的，与业务无关 |
| `connection` | 连接管理是 HTTP 层的职责，不属于「API 行为」 |
| `x-request-id` | 每次请求都是**新**的随机值，天生不同 |
| `set-cookie` 的 `Max-Age` | 它按「token 还剩多久」算，两次调用差 1 秒 |

`set-cookie` 的名字与值仍然比对（只放过 `Max-Age`）—— 这条恰恰是
最容易漂的地方：`Reply.cookies` 是列表，而 HTTP 头如果被塞进普通
字典就会互相覆盖，或者被拼成逗号分隔的单行（浏览器静默丢弃整串）。

反过来的方向（HTTP 有而 `Reply` 没有的头）**只允许上面这几条**，
其余一律失败：多出来的头同样意味着适配层在偷偷改变语义。
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
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.auth import consume_pair_code, store_pair_code                 # noqa: E402
from app.config import Config                                          # noqa: E402
from app.core import Ctx                                               # noqa: E402
from app.db import Database                                            # noqa: E402
from app.indexer import Indexer                                        # noqa: E402
from app.server import build_app, build_state                          # noqa: E402

DEVICE_ID = "test-device-0001"
PAIR_CODE = "424242"


# --------------------------------------------------------------------------
# 夹具：一个临时照片库 + 真实 HTTP 服务
# --------------------------------------------------------------------------

def _img(path: Path, size=(96, 72), color=(160, 120, 80)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG")
    return path


def _seed(library: Path) -> None:
    _img(library / "2024" / "2024-05" / "a.jpg")
    _img(library / "2024" / "2024-05" / "b.jpg")
    _img(library / "2025" / "2025-01" / "c.png", color=(10, 200, 40))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class _Live:
    """在一段独立的事件循环里跑真实 uvicorn，供 urllib 同步请求。"""

    def __init__(self, cfg: Config, st) -> None:
        self.cfg = cfg
        self.st = st
        self.port = _free_port()
        self.transport = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.error: BaseException | None = None

    def start(self) -> None:
        from app.transports.local_http import LocalHttpTransport

        router, _transport, _st = build_app(self.cfg, state=self.st)
        self.router = router

        def run() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.transport = LocalHttpTransport(self.cfg, router, port=self.port)

            async def boot() -> None:
                await self.transport.start()
                self._ready.set()
                while not getattr(self.transport._server, "should_exit", True):
                    await asyncio.sleep(0.05)

            try:
                self.loop.run_until_complete(boot())
            except BaseException as exc:        # noqa: BLE001
                self.error = exc
                self._ready.set()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=20):
            raise RuntimeError("HTTP 服务启动超时")
        if self.error:
            raise self.error

    def stop(self) -> None:
        if self.transport is not None and self.loop is not None:
            fut = asyncio.run_coroutine_threadsafe(self.transport.stop(), self.loop)
            try:
                fut.result(timeout=5)
            except Exception:                   # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)
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
            except Exception:                   # noqa: BLE001
                pass
            finally:
                loop.close()
        self.loop = None
        self._thread = None


class _Pair:
    """一个已配对的临时服务：真 HTTP 一份 + 直调 dispatch 一份。"""

    def __init__(self, tmp_path: Path) -> None:
        self.library = tmp_path / "lib"
        self.library.mkdir()
        _seed(self.library)

        data = tmp_path / "data"
        data.mkdir()
        self.cfg = Config({
            "app": {"mode": "readwrite"},
            "roots": [{"id": "library", "path": str(self.library),
                       "mode": "readwrite"}],
            "server": {"host": "127.0.0.1", "port": 0},
            "thumbs": {"sizes": [320, 1600]},
            "auth": {"allow_delete": True, "delete_to_recycle_bin": True},
            "index": {"batch_size": 50},
        }, path=data / "config.json", data_dir=data)

        self.db = Database(self.cfg.db_path)
        self.db.init()
        self.st = build_state(self.cfg, db=self.db)

        asyncio.run(Indexer(self.cfg, self.db).scan_root(
            self.cfg.root_by_id("library")))
        ids = [int(r["id"]) for r in
               self.db.query("SELECT id FROM media ORDER BY id")]
        assert ids, "测试库没有索引到任何照片"
        self.media_id = ids[0]

        store_pair_code(self.db, PAIR_CODE, DEVICE_ID, "测试电脑")
        _cid, self.token = consume_pair_code(
            self.db, PAIR_CODE, device_name="测试手机", cfg=self.cfg)

        self.live = _Live(self.cfg, self.st)
        self.live.start()

    def close(self) -> None:
        self.live.stop()
        try:
            self.db.close()
        except Exception:                       # noqa: BLE001
            pass

    # ---- 两条通路 ----

    def direct(self, method: str, path: str, *,
               query: dict[str, list[str]] | None = None,
               headers: dict[str, str] | None = None,
               body: bytes | None = None):
        """直调 `Router.dispatch`，返回 `(status, body_bytes, headers, cookies)`。"""
        hdrs = dict(headers or {})
        ctx = Ctx(method=method, path=path, query=query or {}, headers=hdrs,
                  params={}, app=self.st, client_ip="127.0.0.1")
        if body is not None:
            async def gen() -> object:
                yield body
            ctx.body = gen()                    # type: ignore[assignment]

        reply = asyncio.run(self.live.router.dispatch(ctx))
        raw = reply.body
        if raw is None:
            out = b""
        elif isinstance(raw, bytes):
            out = raw
        else:
            out = asyncio.run(_drain(raw))
        return (reply.status, out, reply.headers.as_dict(),
                list(reply.cookies))

    def live_http(self, method: str, path: str, *,
                  query: dict[str, list[str]] | None = None,
                  headers: dict[str, str] | None = None,
                  body: bytes | None = None):
        """真实 HTTP 请求，返回 `(status, body_bytes, headers_dict, set_cookies)`。"""
        from urllib.parse import urlencode

        url = f"http://127.0.0.1:{self.live.port}{path}"
        if query:
            url += "?" + urlencode(query, doseq=True)

        req = urllib.request.Request(url, method=method, data=body)
        for k, v in (headers or {}).items():
            req.add_header(k, v)

        try:
            resp = urllib.request.urlopen(req, timeout=20)
        except urllib.error.HTTPError as exc:
            resp = exc

        with resp:
            payload = resp.read()
            raw_headers = resp.headers
            pairs = list(raw_headers.items())

        out: dict[str, str] = {}
        cookies: list[str] = []
        for k, v in pairs:
            lk = k.lower()
            if lk == "set-cookie":
                cookies.append(v)
                continue
            out[lk] = v
        return resp.status, payload, out, cookies


async def _drain(agen) -> bytes:
    buf = bytearray()
    async for chunk in agen:
        buf.extend(chunk)
    return bytes(buf)


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("switch")
    p = _Pair(tmp)
    yield p
    p.close()


def _auth(pair: _Pair) -> dict[str, str]:
    return {"Authorization": f"Bearer {pair.token}"}


# --------------------------------------------------------------------------
# 探针表
# --------------------------------------------------------------------------

def _probes(p: _Pair) -> list[tuple[str, str, str, dict, dict, bytes | None, str]]:
    """`(说明, method, path, query, headers, body, 比对方式)`。

    比对方式：
    * `"bytes"`：状态码 + 响应体逐字节相等（二进制载荷、静态文本）
    * `"json"`：状态码 + JSON 语义相等（抹掉 `ts`/`request_id`
      这类天生每次不同的字段后逐值比），头与 cookie 照常逐个比
    * `"shape"`：状态码相等 + JSON 的**键集合**相等（值里含随机 id，
      比如新上传会话的 `upload_id`，不可能逐值相同）
    """
    a = _auth(p)
    mid = p.media_id
    return [
        ("免鉴权的 ping", "GET", "/api/system/ping", {}, {}, None, "json"),
        ("系统信息（大 JSON）", "GET", "/api/system/info", {}, a, None, "json"),
        ("库统计", "GET", "/api/system/stats", {}, a, None, "json"),
        ("时间线首页", "GET", "/api/timeline",
         {"limit": ["2"]}, a, None, "json"),
        ("单张详情", "GET", f"/api/items/{mid}", {}, a, None, "json"),
        ("缩略图（二进制 body）", "GET", f"/api/items/{mid}/thumb",
         {"size": ["320"]}, a, None, "bytes"),
        ("缩略图（缺失的 id）", "GET", "/api/items/999999/thumb",
         {}, a, None, "json"),
        ("原图完整下载", "GET", f"/api/items/{mid}/raw", {}, a, None, "bytes"),
        ("原图 Range 切片（206）", "GET", f"/api/items/{mid}/raw", {},
         {**a, "Range": "bytes=0-99"}, None, "bytes"),
        ("原图 Range 越界（416）", "GET", f"/api/items/{mid}/raw", {},
         {**a, "Range": "bytes=99999999-"}, None, "json"),
        ("HEAD 不返回 body", "HEAD", f"/api/items/{mid}/raw", {},
         a, None, "bytes"),
        ("目录列表", "GET", "/api/folders", {}, a, None, "json"),
        ("搜索（命中）", "GET", "/api/search",
         {"q": ["a"]}, a, None, "json"),
        ("搜索（空词）", "GET", "/api/search", {"q": [""]}, a, None, "json"),
        ("缺少凭证 → 401", "GET", "/api/timeline", {}, {}, None, "json"),
        # token 必须是 ASCII：HTTP 头值只能放 ISO-8859-1，urllib 会
        # 直接抛 UnicodeEncodeError，测到的是客户端发的意思而不是服务端。
        ("坏 token → 401", "GET", "/api/timeline", {},
         {"Authorization": "Bearer deadbeef-not-a-real-token"}, None, "json"),
        ("不存在的接口 → 404", "GET", "/api/nope", {}, a, None, "json"),
        ("方法不对 → 405", "DELETE", "/api/timeline", {}, a, None, "json"),
        ("参数非法 → 400", "GET", "/api/timeline",
         {"limit": ["99999"]}, a, None, "json"),
        ("上传初始化（含随机 id）", "POST", "/api/uploads/init", {}, a,
         json.dumps({"file_name": "x.jpg", "size": 10,
                     "sha256": None}).encode(), "shape"),
    ]


# --------------------------------------------------------------------------
# 比对
# --------------------------------------------------------------------------

#: 允许适配层/uvicorn 多出来的头（见模块 docstring 的表）。
_IGNORED = {"x-request-id", "date", "server", "connection", "keep-alive",
            "content-length"}

#: JSON 里**天生每次不同**的字段，比对前统一抹掉。
#:
#: * `ts` —— ping 的服务器时间戳。两次调用相隔几十毫秒，本来就该不同；
#:   断言它相等等于断言"时钟不走"。
#: * `request_id` —— 每个请求一个新的随机值，它就是用来在日志里区分
#:   两个请求的。
#: * `uptime_sec` —— 上面那条 `ts` 差在哪，它就差在哪，且它还会让
#:   `Content-Length` 跟着差 1 字节。
#: * `server_time` —— 时间线/搜索响应的服务端时间（前端用它校正本地钟）。
#:   它是**顶层**字段，与 `ts` 是两个不同的名字，只写一个会漏。
_VOLATILE_JSON = {"ts", "request_id", "uptime_sec", "server_time"}


def _normalize(obj):
    """把易变字段换成占位符，递归到嵌套结构里。"""
    if isinstance(obj, dict):
        return {k: ("<volatile>" if k in _VOLATILE_JSON else _normalize(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    return obj


def _compare_headers(step: str, d_headers, d_body, l_headers, l_body,
                     bad: list[str]) -> None:
    for k, v in d_headers.items():
        if k in _IGNORED:
            continue
        got = l_headers.get(k)
        if got is None:
            bad.append(f"{step}: HTTP 少了头 {k}={v!r}")
        elif got != v:
            bad.append(f"{step}: 头 {k} 不同 直调={v!r} HTTP={got!r}")

    for k in l_headers:
        if k in _IGNORED or k in d_headers:
            continue
        bad.append(f"{step}: HTTP 多了头 {k}={l_headers[k]!r}")

    # Content-Length 不能两边直接比（body 里的易变字段长度差几个字节，
    # 120 与 121 的差别不是 bug）。但有 body 时它必须等于 body 长度 ——
    # 这才是这个头唯一的语义。
    #
    # body 为空时跳过：HEAD 响应的 `Reply.body` 是 None 而
    # Content-Length 描述的是"GET 会返回多少"，749B 是**正确**的，
    # 断言它等于 0 会把正确的实现判成错。HEAD 的语义由
    # `test_head_has_no_body_but_keeps_length` 单独锁。
    for side, hdrs, body in (("直调", d_headers, d_body), ("HTTP", l_headers, l_body)):
        raw = hdrs.get("content-length")
        if raw is not None and body and int(raw) != len(body):
            bad.append(f"{step}: {side} Content-Length={raw} 与实际 body "
                       f"{len(body)}B 不符")


def _compare_cookies(step: str, d_cookies, l_cookies, bad: list[str]) -> None:
    # set-cookie 只比名字与值：Max-Age 按「token 还剩多久」算，
    # 两次调用相差 1 秒，逐字符比会得到一个与本次断言无关的失败。
    if len(d_cookies) != len(l_cookies):
        bad.append(f"{step}: set-cookie 条数 {len(d_cookies)} vs "
                   f"{len(l_cookies)}（多值头被覆盖或错误合并了）")
        return
    for i, (name, value, max_age) in enumerate(d_cookies):
        line = l_cookies[i]
        if not line.startswith(f"{name}={value}"):
            bad.append(f"{step}: set-cookie 第 {i} 条 直调={name}={value}… "
                       f"HTTP={line!r}")
        elif not value and max_age == 0:
            # `(名字, "", 0)` 是**刻意清除** cookie 的形态：token 被撤销/
            # 过期时要把浏览器里那枚废凭证立刻删掉，否则它会一直重试，
            # 表现为"JS 请求正常但缩略图全是灰块"。
            pass
        elif value and max_age <= 0:
            # 但**非空**值配一个非正的 Max-Age 是坏的自愈：浏览器会当场
            # 丢弃这枚 cookie，于是"顺手补发一个 cookie"永远补不上，
            # 而响应本身 200、body 正常，症状只在 <img> 上显现。
            bad.append(f"{step}: 自愈 cookie {name} 的 Max-Age={max_age}，"
                       f"非空值必须以正数下发")


def _compare_bytes(step: str, d, l) -> list[str]:
    """二进制载荷：必须逐字节相同。"""
    d_status, d_body, d_headers, d_cookies = d
    l_status, l_body, l_headers, l_cookies = l
    bad: list[str] = []

    if d_status != l_status:
        bad.append(f"{step}: 状态码不同 直调={d_status} HTTP={l_status}")

    if d_body != l_body:
        bad.append(
            f"{step}: 响应体不同 直调={len(d_body)}B "
            f"{d_body[:160]!r} HTTP={len(l_body)}B {l_body[:160]!r}")

    _compare_headers(step, d_headers, d_body, l_headers, l_body, bad)
    _compare_cookies(step, d_cookies, l_cookies, bad)
    return bad


def _compare_json(step: str, d, l) -> list[str]:
    """JSON 响应：**语义**相同（键、值、嵌套结构），抹掉易变字段后比。

    为什么不逐字节比：`/api/system/ping` 的 `ts` 是服务器当前时间、
    每个错误响应带一个新的 `request_id`。逐字节比会立刻得到 17 条
    "失败"，而它们全都不是 bug —— 一个满屏假阳性的测试比没有测试更糟，
    因为没人会再去看它的输出。
    """
    d_status, d_body, d_headers, d_cookies = d
    l_status, l_body, l_headers, l_cookies = l
    bad: list[str] = []

    if d_status != l_status:
        bad.append(f"{step}: 状态码不同 直调={d_status} HTTP={l_status}")
        return bad

    try:
        dj = _normalize(json.loads(d_body))
        lj = _normalize(json.loads(l_body))
    except Exception as exc:                    # noqa: BLE001
        return [f"{step}: 响应不是 JSON（{exc}）直调={d_body[:120]!r} "
                f"HTTP={l_body[:120]!r}"]

    if dj != lj:
        bad.append(f"{step}: JSON 不同\n      直调={json.dumps(dj, ensure_ascii=False)[:400]}"
                   f"\n      HTTP={json.dumps(lj, ensure_ascii=False)[:400]}")

    _compare_headers(step, d_headers, d_body, l_headers, l_body, bad)
    _compare_cookies(step, d_cookies, l_cookies, bad)
    return bad


def _compare_shape(step: str, d, l) -> list[str]:
    """含随机 id 的 JSON：只比**键集合**。

    `POST /api/uploads/init` 每次都生成一个新的 `upload_id`，
    逐值比必然不同 —— 但「哪些字段存在」仍然该完全一致。
    """
    d_status, d_body, d_headers, d_cookies = d
    l_status, l_body, l_headers, l_cookies = l
    bad: list[str] = []

    if d_status != l_status:
        bad.append(f"{step}: 状态码不同 直调={d_status} HTTP={l_status}")
        return bad

    try:
        dj = json.loads(d_body)
        lj = json.loads(l_body)
    except Exception as exc:                    # noqa: BLE001
        return [f"{step}: 响应不是 JSON（{exc}）直调={d_body[:120]!r} "
                f"HTTP={l_body[:120]!r}"]

    if _keys(dj) != _keys(lj):
        bad.append(f"{step}: JSON 键不同 直调={sorted(_keys(dj))} "
                   f"HTTP={sorted(_keys(lj))}")

    _compare_headers(step, d_headers, d_body, l_headers, l_body, bad)
    _compare_cookies(step, d_cookies, l_cookies, bad)
    return bad


def _keys(obj) -> set[str]:
    return set(obj) if isinstance(obj, dict) else set()

def test_two_paths_agree(pair: _Pair) -> None:
    """AC-6.7 本体：21 个探针在两条通路上的响应必须一致。"""
    problems: list[str] = []
    for step, method, path, query, headers, body, mode in _probes(pair):
        d = pair.direct(method, path, query=query, headers=headers, body=body)
        l = pair.live_http(method, path, query=query, headers=headers, body=body)
        if mode == "bytes":
            problems += _compare_bytes(step, d, l)
        elif mode == "json":
            problems += _compare_json(step, d, l)
        else:
            problems += _compare_shape(step, d, l)

    assert not problems, (
        f"有 {len(problems)} 处两条通路不一致（这正是二期接隧道时会静默出错的"
        f"地方）：\n  " + "\n  ".join(problems))


def test_probes_actually_exercise_both_paths(pair: _Pair) -> None:
    """防呆：探针不能全是同一个状态码。

    上一个用例如果因为"所有探针都返回 401"而通过，那它什么也没证明 ——
    两条路都短路在鉴权中间件里，业务代码一行没跑。
    """
    statuses = {}
    for step, method, path, query, headers, body, _mode in _probes(pair):
        d = pair.direct(method, path, query=query, headers=headers, body=body)
        statuses[step] = d[0]

    distinct = set(statuses.values())
    assert {200, 400, 401, 404, 405, 416} <= distinct, (
        f"探针覆盖面不足，只出现了 {sorted(distinct)}：{statuses}")

    # 至少有几个探针真的返回了非空 body，否则"响应体逐字节相同"是
    # 两个空字节串在互相比较。
    nonempty = 0
    for step, method, path, query, headers, body, _mode in _probes(pair):
        d = pair.direct(method, path, query=query, headers=headers, body=body)
        if len(d[1]) > 0:
            nonempty += 1
    assert nonempty >= 8, f"只有 {nonempty} 个探针有响应体"


def test_head_has_no_body_but_keeps_length(pair: _Pair) -> None:
    """HEAD 的 body 清空发生在**两个**地方，最易漂。"""
    mid = pair.media_id
    d = pair.direct("HEAD", f"/api/items/{mid}/raw", headers=_auth(pair))
    l = pair.live_http("HEAD", f"/api/items/{mid}/raw", headers=_auth(pair))

    assert d[1] == b"" and l[1] == b"", "HEAD 不该返回 body"
    # Content-Length 必须留着 —— 告知大小正是 HEAD 的意义。
    assert int(d[2]["content-length"]) > 0
    assert d[2]["content-length"] == l[2].get("content-length")


def test_panel_paths_are_404_over_the_lan(pair: _Pair) -> None:
    """/panel/** 只认本机。

    这条**不属于**两个通路的对比（局域网这一侧本来就是本机网段），
    它锁的是同一件事的另一面：手机不能通过隧道摸到面板。
    隧道通路上 `ctx.client_ip` 不会是本机回环地址，与这里的
    "非回环 IP"等价。
    """
    d = pair.direct("GET", "/panel/devices",
                    headers={**_auth(pair), "X-Forwarded-For": "10.0.0.5"})
    # client_ip 是 Ctx 的字段而不是头，直调时显式给一个非回环值。
    ctx = Ctx(method="GET", path="/panel/devices", headers=_auth(pair),
              params={}, app=pair.st, client_ip="192.168.0.55")
    reply = asyncio.run(pair.live.router.dispatch(ctx))
    assert reply.status == 404, (
        f"非回环来源访问面板得到 {reply.status}，应为 404（连存在性都不该暴露）")
