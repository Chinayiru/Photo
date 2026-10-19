"""一期验收用例在**云端通路**上零改动重跑（P1 DoD 第 7 条）。

## 这个文件为什么长这样

DoD 要求「一期已通过的浏览/上传验收用例，在云端通路上全部重跑通过」。
"零改动"的强证据不是我们照着抄了一遍断言，而是**同一批函数对象被再跑
一次** —— 所以这里用 `importlib` 把 `test_api_contract.py` 整个模块加载
进来，把它的 12 个用例函数**原样绑进本模块的命名空间**，只换掉 `srv`
这个 fixture。

`test_api_contract.py` 自己一行都没动。它跑在局域网通路上，本文件把
同样的函数跑在 `手机 → 云端 relay → 隧道 → 电脑端 Router` 上。

## 那个 shim 是什么、为什么必须存在

`test_api_contract.py` 的用例都通过 `srv.request(...)` 发请求，只有一处
是手搓 `urllib`（分片上传的 PATCH，因为要发原始字节）。它们共同的前提
是"本机上有一个 HTTP 端点"。云端通路的真实形态是 `/d/{device_id}/...`，
与用例里的路径差一个前缀。

于是这里起一个**极薄的本地转发器**（`_ProxySrv` 底下的 `/` 路由）：把
`/{path}` 原样转成云端 `http://<relay>/d/{device_id}/{path}`。

它不是被测代码的一部分，也不是在"模拟"云端 —— 请求真的走了完整链路：
`127.0.0.1:<shim> → 云端 relay → WebSocket 隧道 → 电脑端 Router`。
shim 只做两件事：补 `/d/{id}` 前缀、把响应原样搬回来。

### 两条路径被刻意留在局域网

- `POST /panel/pair-code`、`POST /api/pair`：**配对本来就只在局域网做**
  （`docs/03` §5.1：`IDLE → PROBING → DIRECT` 配对，之后才谈云端）。
  云端设计上不允许 `/panel/**`（`cloud_tunnel._clean_path` 只放行
  `/api/...`），硬把配对塞进云端会改变产品行为，那不叫零改动重跑。
  这一步同时也是在验证我们修好的那个缺陷：**局域网配对签出的令牌
  必须能直接走云端**（见 `test_p1_tunnel.py::test_lan_paired_token_works_over_cloud`）。

其余全部走云端。所以 12 条用例里有 11 条的数据面完全在隧道上，
只有"怎么拿到令牌"这一段在局域网 —— 这正是产品的真实顺序。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

_HERE = Path(__file__).resolve().parent


def _load(name: str):
    """按文件路径加载同目录的测试模块（`backend/tests` 不是 package）。"""
    path = _HERE / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_p1_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_contract = _load("test_api_contract")
_p1 = _load("test_p1_tunnel")

# 完整链路必须带的东西
_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
# 逐跳头：转发时必须丢掉（Starlette 自己会重算 Content-Length）
_HOP_REQ = frozenset({
    "host", "connection", "keep-alive", "transfer-encoding", "upgrade",
    "proxy-authorization", "proxy-authenticate", "te", "trailer",
    "content-length",
})
_HOP_RES = frozenset({
    "content-length", "content-encoding", "transfer-encoding", "connection",
    "date", "server", "keep-alive",
})

# 配对只在局域网做（见模块 docstring）。逐字匹配，不用 startswith ——
# `/api/pair` 与 `/api/pairX` 不该被同一条件命中。
_LAN_ONLY = frozenset({"/api/pair"})


def _needs_lan(path: str) -> bool:
    return path in _LAN_ONLY or path == "/panel" or path.startswith("/panel/")


class _ProxySrv:
    """把 `_contract` 的用例接到底层链路上的 shim（见模块 docstring）。"""

    def __init__(self, rig: Any) -> None:
        self._rig = rig
        self.port = 0
        self._app = self._build_app()
        self._server: Any = None
        self._task: asyncio.Task | None = None

    # ---- 转发 ----

    def _target(self, path: str, query: str) -> str:
        if _needs_lan(path):
            base = f"http://127.0.0.1:{self._rig.lan_port}"
            rest = path
        else:
            base = f"http://127.0.0.1:{self._rig.cloud_port}/d/{self._rig.device_id}"
            rest = path
        return base + rest + (f"?{query}" if query else "")

    def _build_app(self) -> Starlette:
        async def endpoint(request: Request) -> Response:
            target = self._target(request.url.path, request.url.query)
            headers = {
                k: v for k, v in request.headers.items()
                if k.lower() not in _HOP_REQ
            }
            data = await request.body()
            # ★ 必须 to_thread：云端 uvicorn 与本 shim 活在**同一个事件循环**
            # 上（都挂在 `_Rig` 里），循环里直接调同步 urllib 会互相等死
            # （踩过一次：`panel/pair-code` 直接 timed out）。
            status, hdrs, payload = await asyncio.to_thread(
                self._send, target, request.method, headers, data)
            out = {k: v for k, v in hdrs if k.lower() not in _HOP_RES}
            resp = Response(payload, status_code=status, headers=out)
            for k, v in hdrs:
                if k.lower() == "set-cookie":
                    resp.raw_headers.append((b"set-cookie", v.encode("latin-1")))
            return resp

        return Starlette(routes=[
            Route("/{path:path}", endpoint, methods=_ALL_METHODS),
        ])

    @staticmethod
    def _send(target: str, method: str, headers: dict[str, str],
              data: bytes | None):
        req = urllib.request.Request(
            target, data=data or None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status, list(resp.headers.items()), resp.read()
        except urllib.error.HTTPError as exc:
            # 4xx/5xx 也是**正常响应**，用例要看它的 body
            return exc.code, list(exc.headers.items()), exc.read()

    # ---- 生命周期 ----

    async def start(self) -> None:
        import uvicorn

        cfg = uvicorn.Config(self._app, host="127.0.0.1", port=0,
                             log_level="critical", lifespan="off")
        self._server = uvicorn.Server(cfg)
        self._task = asyncio.create_task(self._server.serve(), name="shim")
        deadline = asyncio.get_running_loop().time() + 20.0
        while not self._server.started:
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError("shim 未启动")
            await asyncio.sleep(0.02)
        self.port = int(self._server.servers[0].sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=10)
            except BaseException:               # noqa: BLE001
                self._task.cancel()
                try:
                    await self._task
                except BaseException:           # noqa: BLE001
                    pass
        self._server = None
        self._task = None

    # ---- 与 `_contract._Server` 对齐的接口 ----

    def request(self, method: str, path: str, *, token: str | None = None,
                body: dict | None = None, raw: bool = False):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None
        headers: dict[str, str] = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return _contract._Response(resp.status, resp.headers.items(),
                                           resp.read())
        except urllib.error.HTTPError as exc:
            return _contract._Response(exc.code, exc.headers.items(), exc.read())


class _RigThread:
    """把整个 P1 装置放进一个后台线程的独立事件循环。

    ★ 不能和用例跑在同一个循环里。`_Rig` 的云端 uvicorn 就活在那个循环
    上，而一期这些用例是**同步**的（`srv.request` 里是阻塞的 urllib）——
    两者挤在一个循环里会互相等死。`_contract._Server` 用的也是"后台线程
    + 独立 loop"这个形状，这里保持一致。

    另一个坑：不能借 `test_p1_tunnel._run()` 来做启动 —— 那个函数的
    `finally` 会把 `_ACTIVE` 里所有 rig 全拆掉，于是刚起好就被关了，
    表现是 `WinError 10061 目标计算机积极拒绝`。
    """

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: Any = None
        self.rig: Any = None
        self.shim: _ProxySrv | None = None
        self.error: BaseException | None = None
        self._ready = __import__("threading").Event()
        self._stop_ev: asyncio.Event | None = None

    # ---- 线程入口 ----

    def start(self, timeout: float = 90.0) -> None:
        import threading

        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="p1-acceptance")
        self.thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError(f"P1 装置启动超时：{self.error!r}")
        if self.error is not None:
            raise RuntimeError(f"P1 装置启动失败：{self.error!r}")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:           # noqa: BLE001
            self.error = exc
            self._ready.set()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except BaseException:              # noqa: BLE001
                pass
            loop.close()

    async def _serve(self) -> None:
        self.rig = _p1._Rig(self.tmp / "rig")
        try:
            await self.rig.start()
            await self.rig.wait_tunnel()
            self.shim = _ProxySrv(self.rig)
            await self.shim.start()
            self._stop_ev = asyncio.Event()
            self._ready.set()
            await self._stop_ev.wait()
        finally:
            if self.shim is not None:
                try:
                    await self.shim.stop()
                except BaseException:          # noqa: BLE001
                    pass
            if self.rig is not None:
                try:
                    await self.rig.stop()
                except BaseException:          # noqa: BLE001
                    pass

    def call(self, coro, timeout: float = 120.0):
        assert self.loop is not None, "装置还没启动"
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        if self.loop is not None and self._stop_ev is not None:
            try:
                self.loop.call_soon_threadsafe(self._stop_ev.set)
            except BaseException:              # noqa: BLE001
                pass
        if self.thread is not None:
            self.thread.join(timeout=30)


@pytest.fixture()
def srv(tmp_path):
    """与 `test_api_contract.srv` 同名同契约，但底层换成云端隧道。

    造数直接复用 `test_p1_tunnel._Rig`（同样是 a/b/敦煌 三张真 JPEG），
    这样 `test_timeline_matches_types` 的 `b.jpg`、`test_search_matches_types`
    的 `敦煌` 这些**内容级断言**仍然有意义 —— 换成别的库数据它们会假绿。
    """
    holder = _RigThread(tmp_path)
    holder.start()
    try:
        yield holder.shim
    finally:
        holder.stop()


# ---------------------------------------------------------------------------
# 一期 12 条验收用例 —— 函数对象原样搬过来，一个字没改
# ---------------------------------------------------------------------------

test_ping_matches_types = _contract.test_ping_matches_types
test_system_info_matches_types = _contract.test_system_info_matches_types
test_system_stats_matches_types = _contract.test_system_stats_matches_types
test_timeline_matches_types = _contract.test_timeline_matches_types
test_media_detail_matches_types = _contract.test_media_detail_matches_types
test_search_matches_types = _contract.test_search_matches_types
test_folders_matches_types = _contract.test_folders_matches_types
test_upload_init_matches_types = _contract.test_upload_init_matches_types
test_upload_status_and_complete_match_types = (
    _contract.test_upload_status_and_complete_match_types)
test_error_envelope_matches_types = _contract.test_error_envelope_matches_types
test_no_declared_field_is_never_present = (
    _contract.test_no_declared_field_is_never_present)
test_types_ts_covers_every_route = _contract.test_types_ts_covers_every_route


# ---------------------------------------------------------------------------
# 云通路的额外硬要求：这些不是一期的断言，是"在云上还成立"的前提
# ---------------------------------------------------------------------------

def test_shim_actually_goes_through_the_tunnel(srv) -> None:
    """shim 真的在走隧道 —— 否则上面 12 条只是把局域网又跑了一遍。

    证据取自云端的标记头：`X-PB-Tunnel: relay` 由 `cloud/relay/main.py`
    的 `forward()` 无条件加上，局域网通路**没有**这个头。
    """
    tok = _contract._token(srv)
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}/api/timeline",
        headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        assert resp.headers.get("x-pb-tunnel") == "relay", (
            "这条请求没经过云端 relay —— 本文件的所有比对都会失去意义",
            dict(resp.headers.items()))
        assert resp.headers.get("x-pb-device-online") == "1"
        body = json.loads(resp.read().decode("utf-8"))
    assert body["items"], body

    ping = srv.request("GET", "/api/system/ping").json()
    assert ping["via"] == "cloud", (
        "ping 的 via 必须是 cloud，否则说明实际打到了局域网端口", ping)


def test_pairing_stayed_on_the_lan(srv) -> None:
    """配对确实走的是局域网（不是被 shim 偷偷转进了云端）。

    `/panel/pair-code` 在云端是被路径白名单拒掉的（403 E1009）。
    如果哪天有人"顺手"让 shim 也把它转进云端，这条会立刻红 ——
    那是产品行为的改变，不该悄悄发生。
    """
    assert _needs_lan("/panel/pair-code") is True
    assert _needs_lan("/api/pair") is True
    # 这些必须走云端（否则隧道等于没测）
    for path in ("/api/timeline", "/api/uploads/init", "/api/items/9/raw"):
        assert _needs_lan(path) is False, path
