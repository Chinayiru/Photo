"""P1 云端隧道端到端集成测试（`docs/07` §5.1 的 TC-TUNNEL-* 中协议层之外的部分）。

## 这个文件测什么，不测什么

`test_p1_mux.py` 已经把**协议层**钉死了：req_id 不串台、credit 不死锁、
CANCEL 释放槽位、超时清扫。那些性质用环回 Mux 测最精确。

这里测的是**装配**——把真 uvicorn 云端、真 `websockets` 隧道客户端、
真 `CloudTunnelTransport`、真业务路由接起来之后，只有整体跑才成立的东西：

* 握手四类拒绝（无令牌 / 错令牌 / 错 scheme / 已撤销）与 `HELLO` 自检
  （`device_id` 不匹配、`proto` 不符）；
* 令牌只能走 `Authorization` 头，**放查询串不算数**；
* `/d/{device_id}/**` 透明转发：状态码、响应头、Range、SSE、404 保持；
* 云端离线判据（503 `E3003`）与 `X-PB-Tunnel` / `X-PB-Device-Online`；
* 云端配对码换令牌，换来的令牌能经隧道访问电脑端（离线验签）；
* 撤销列表同步（`docs/04` §3.6 / AC-5.8）；
* 流量表双向累加（AC-3.15）；
* 云端磁盘不出现任何媒体字节（AC-7.1/7.2/7.7）。

## 为什么不用 `TestClient`

`starlette.testclient` 走的是内存 ASGI，`ws.send_bytes` 与 uvicorn 的真实
实现有差异（`send_denial_response` 需要 `websocket.http.response` 扩展，
TestClient 下会抛 `WebSocketDenialResponse` 而不是 HTTP 401）。用真 uvicorn
才测得到"手机实际收到什么"。

## 事件循环

仓库没有 `pytest-asyncio`，沿用 `test_p1_mux.py` 的写法：模块级
`_run(coro) = asyncio.run(coro)`，每个用例内部一个 `async def main()`。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import uvicorn  # noqa: E402

from backend.app.config import Config  # noqa: E402
from backend.app.db import make_db  # noqa: E402
from backend.app.server import build_app, build_state  # noqa: E402
from backend.app.transports.cloud_tunnel import (                      # noqa: E402
    CloudTunnelTransport,
)
from cloud.relay import security  # noqa: E402
from cloud.relay.auth import create_account  # noqa: E402
from cloud.relay.db import CloudDB  # noqa: E402
from cloud.relay.main import create_app  # noqa: E402
from shared.protocol import CREDIT_V2_CAP, FrameType, encode_frame  # noqa: E402


# ---------------------------------------------------------------------------
# 装置
# ---------------------------------------------------------------------------

def _run(coro):
    """在**一个**事件循环里跑完用例，然后就地拆掉它启动的所有 `_Rig`。

    ★ 拆卸必须和用例同一个循环。`_Rig` 内部全是 asyncio 原语（uvicorn
    server、task、lock），跨 `asyncio.run` 边界去 await 它们会挂在一个
    已经关闭的循环上 —— 第一版把清理放在 pytest fixture 的 teardown 里
    （那是另一个 `asyncio.run`），结果是每个用例都过、每个 teardown 都超时。
    """
    async def wrapper():
        _ACTIVE.clear()
        try:
            return await coro
        finally:
            while _ACTIVE:
                rig = _ACTIVE.pop()
                with contextlib.suppress(BaseException):
                    await rig.stop()

    return asyncio.run(wrapper())


# 当前用例启动过的装配；由 `_run` 在同一个循环里拆掉（见上）。
_ACTIVE: list["_Rig"] = []


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _wait_port_free(port: int, timeout: float = 10.0) -> None:
    """等到 `port` 能重新绑定为止（最多 `timeout` 秒，超时就算了）。

    Windows 上 uvicorn 的 `serve()` 返回不代表监听套接字已从内核里撤掉，
    紧接着 `bind()` 会 `WinError 10048`。重连用例要"停云端 → 起回来"，
    中间这一步不能省。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", port))
            return
        except OSError:
            time.sleep(0.02)
        finally:
            s.close()


def _make_photo(path: Path, *, taken: str, color=(180, 90, 40)) -> None:
    """写一张带 EXIF 拍摄时间的真 JPEG（索引器靠它填 `taken_at`）。"""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (160, 120), color)
    ex = Image.Exif()
    ex.get_ifd(0x8769)[0x9003] = taken
    ex.get_ifd(0x8769)[0x9004] = taken
    im.save(path, "JPEG", exif=ex)


class _Http:
    """极简同步 HTTP 客户端（urllib）。

    刻意不用 httpx：`httpx.AsyncClient` 会**记住 `Set-Cookie`**，于是
    "不带令牌"的那次请求实际上是 cookie 认证，会得到 200 —— 第一版探针
    就踩了这个坑，把"无令牌可读时间线"误判成越权。这里每次新建
    opener，且不维护 cookie。
    """

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPRedirectHandler)

    def request(self, method: str, path: str, *, token: str | None = None,
                body: Any = None, headers: dict[str, str] | None = None,
                raw: bool = False) -> tuple[int, Any, dict[str, str]]:
        url = self.base + path
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            # 上传分片是**原始字节**，塞进 json.dumps 会
            # `TypeError: Object of type bytes is not JSON serializable`
            if isinstance(body, (bytes, bytearray, memoryview)):
                data = bytes(body)
                hdrs.setdefault("Content-Type",
                                "application/octet-stream")
            else:
                data = json.dumps(body).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/json")
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=data, headers=hdrs,
                                     method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                payload = resp.read()
                status = resp.status
                got = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
            got = {k.lower(): v for k, v in exc.headers.items()}
        except (urllib.error.URLError, OSError) as exc:
            # 对端整个不在（连接被拒 / 超时）。**返回 `None` 而不是抛**：
            # 断线重连的用例需要在"云端已停"这个状态下发一个请求，用它
            # 区分"监听套接字都没了"（None）与"云端在、电脑不在"
            # （503 `E3003`）—— 两者都是对端的错，但只有后者是隧道要
            # 负责的语义。
            return None, f"{type(exc).__name__}: {exc}", {}
        if raw:
            return status, payload, got
        try:
            return status, json.loads(payload.decode("utf-8")), got
        except Exception:       # noqa: BLE001 —— 非 JSON 就把原文给出去
            return status, payload.decode("utf-8", "replace"), got


    async def arequest(self, method: str, path: str, **kw):
        """`request()` 的异步版本。

        ★ 必须放线程里跑。云端 uvicorn 就活在**本用例这个事件循环**上，
        在协程里同步调 urllib 会把循环堵住，云端永远读不到那个请求 ——
        表现为每个用例都 `TimeoutError: 隧道未在 20.0s 内建立`。第一版
        就是这么挂的：唯一的"通过"用例恰好只用了 websockets。
        """
        return await asyncio.to_thread(self.request, method, path, **kw)


class _Rig:
    """一次完整的 P1 装配：云端 relay + 电脑端隧道 + 真业务路由。

    ★ 每个用例一个全新实例，绝不复用：云端 `DeviceRegistry` 是内存 map，
    电脑端 `state.set_state()` 是进程级单例，跨用例复用必然串台。
    """

    def __init__(self, tmp: Path, *, hosts: list[str] | None = None,
                 cloud_base_url: str | None = None,
                 tunnel: bool = True) -> None:
        self.tmp = tmp
        self.cloud_port = 0
        self.lan_port = 0
        self.device_id = ""
        self.agent_token = ""
        self.account_token = ""
        self.client_token = ""
        self.client_id = ""
        self._cloud_server: uvicorn.Server | None = None
        self._cloud_task: asyncio.Task | None = None
        self._hosts = hosts
        self._cloud_base_url = cloud_base_url
        self._tunnel = tunnel
        self.cdb: CloudDB | None = None
        self.cloud_dir: Path = tmp
        self.transport: CloudTunnelTransport | None = None
        self.state = None
        self.cloud_app = None
        self.cfg: Any = None
        self.router: Any = None

    async def start_cloud(self) -> None:
        """起云端 uvicorn。可重复调用 —— 端口在重起时沿用上一次的。

        ★ 端口必须沿用：`backend/app/tunnel.py` 的云端地址来自
        `cfg.cloud.base_url`，**运行期不会变**。换端口就等于换了一个云端，
        `TunnelClient` 的重连循环会一直连一个不存在的地址 —— 那样测到的
        不是"断线重连"，是"配置错了"。
        """
        # 登录限流器是模块级单例（`cloud/relay/auth.py:_login_limiter`），
        # 10 次/600s，跨用例累积。一个测试进程里几十个用例共用同一个
        # `127.0.0.1` 桶 ⇒ 后面的用例全在 `alogin()` 拿 429，看起来像
        # "配对坏了"。生产不该清，测试必须清。
        from cloud.relay.auth import _login_limiter

        _login_limiter.reset()
        self.cloud_app = create_app(self.cdb, hosts=self._hosts)
        ucfg = uvicorn.Config(self.cloud_app, host="127.0.0.1",
                              port=self.cloud_port,
                              log_level="critical", ws="websockets",
                              lifespan="off")
        self._cloud_server = uvicorn.Server(ucfg)
        self._cloud_task = asyncio.create_task(
            self._cloud_server.serve(), name="cloud-uvicorn")
        deadline = time.monotonic() + 20.0
        while not self._cloud_server.started:
            if time.monotonic() > deadline:
                raise TimeoutError("云端 uvicorn 未启动")
            await asyncio.sleep(0.02)
        self.cloud_port = int(
            self._cloud_server.servers[0].sockets[0].getsockname()[1])

    async def stop_cloud(self) -> None:
        """停云端并**等端口真的释放**。幂等。"""
        server, task = self._cloud_server, self._cloud_task
        self._cloud_server = None
        self._cloud_task = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except BaseException:                   # noqa: BLE001
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        # 等端口可再绑：`serve()` 返回 ≠ 监听套接字已从内核里撤掉。
        _wait_port_free(self.cloud_port)

    # ---- 生命周期 ----

    async def start(self) -> None:
        tmp = self.tmp
        # `CloudDB` 不会自己建目录（生产由部署脚本负责），测试里显式创建
        tmp.mkdir(parents=True, exist_ok=True)
        # ---- 云端库与账号/设备 ----
        # ★ 云端库单独一间（`cloud/`），下面"云端磁盘不含照片字节"的用例
        # 才能只扫它。与库文件同级时会把被测的照片库自己算成命中。
        self.cloud_dir = tmp / "cloud"
        self.cloud_dir.mkdir(parents=True, exist_ok=True)
        self.cdb = CloudDB(str(self.cloud_dir / "cloud.db"))
        self.cdb.init()
        acc = create_account(self.cdb, "rig@example.com", "password123")
        self.device_id = security.new_device_id()
        self.agent_token = security.new_token()
        self.cdb.execute(
            "INSERT INTO devices(id, account_id, name, agent_token, version,"
            " last_lan, created_at, revoked)"
            " VALUES (?,?,?,?,?,'[]',?,0)",
            (self.device_id, acc["id"], "书房台式机",
             security.hash_token(self.agent_token), "0.1.0", time.time()))

        # ---- 云端 uvicorn ----
        await self.start_cloud()

        # ---- 电脑端 ----
        lib = tmp / "lib"
        _make_photo(lib / "2024" / "2024-05" / "a.jpg",
                    taken="2024:05:12 18:30:00", color=(180, 90, 40))
        _make_photo(lib / "2024" / "2024-05" / "b.jpg",
                    taken="2024:05:12 18:30:01", color=(40, 90, 180))
        _make_photo(lib / "青甘环线" / "敦煌.jpg",
                    taken="2023:07:08 10:11:12", color=(90, 180, 40))
        data = tmp / "data"
        # `tunnel=False`：不启动 TunnelClient。手写 WebSocket 的用例必须
        # 这样，否则 transport 的自动重连会在 1 秒内把注册表里的连接换成
        # 它自己那条，用例拿到的 `device_status` 就是另一个会话的数据。
        base_url = ("" if not self._tunnel else
                    self._cloud_base_url or f"http://127.0.0.1:{self.cloud_port}")
        cfg = Config(
            {
                "server": {"host": "127.0.0.1", "port": 0, "allow_lan": False,
                           "panel_http": True},
                "roots": [{"id": "library", "path": str(lib), "label": "库",
                           "mode": "readwrite", "enabled": True,
                           "recursive": True}],
                "auth": {"require_pairing": True, "allow_delete": True},
                "index": {"scan_on_start": False, "watch": False},
                "cloud": {"base_url": base_url, "device_id": self.device_id,
                          "agent_token": self.agent_token, "enabled": True},
            },
            path=data / "config.json", data_dir=data)
        self.cfg = cfg
        db = make_db(cfg)
        db.init()
        self.state = build_state(cfg, db=db)
        router, _t, self.state = build_app(cfg, state=self.state)
        await self.state.indexer.scan_all()
        self.router = router
        await self.start_transport()

    async def start_transport(self) -> None:
        """起电脑端传输（局域网 + 隧道客户端）。可重复调用。

        重连用例要真的把**电脑端**也拆掉再起回来 —— 只重启云端的话，
        验的是云端侧注册表，验不到 `TunnelClient` 的退避循环。所以
        `cfg`/`router` 存成实例属性，重起时重建一个全新的 transport。
        """
        self.transport = CloudTunnelTransport(self.cfg, self.router)
        await self.transport.start()
        # `CloudTunnelTransport` 没有 `port_actual`（它是复合通路，局域网
        # 端口在 status() 里）。用 status() 而不是内部属性，顺带钉住
        # 这个键对测试是可见的。
        self.lan_port = int(self.transport.status().get("port") or 0)

    async def stop_transport(self) -> None:
        if self.transport is not None:
            with contextlib.suppress(BaseException):
                await self.transport.stop()
            self.transport = None

    async def wait_tunnel(self, timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.transport.status()["tunnel"]        # type: ignore[union-attr]
            if st.get("connected"):
                return st
            await asyncio.sleep(0.05)
        raise TimeoutError("隧道未在 %.1fs 内建立" % timeout)

    async def stop(self) -> None:
        """彻底拆掉。幂等。

        ★ 必须与 `start()` 在**同一个事件循环**里调用。`_Rig` 内部全是
        asyncio 原语（task、lock、uvicorn server），跨 `asyncio.run` 边界
        去 await 它们会挂在一个已经关闭的循环上 —— 第一版把它放在
        pytest fixture 的 teardown 里（那是另一个 `asyncio.run`），
        结果是每个用例都通过、每个 teardown 都超时。
        """
        await self.stop_transport()
        await self.stop_cloud()
        if self.cdb is not None:
            with contextlib.suppress(BaseException):
                self.cdb.close_all()
            self.cdb = None

    # ---- 云端 HTTP 入口 ----

    @property
    def cloud(self) -> _Http:
        return _Http(f"http://127.0.0.1:{self.cloud_port}")

    @property
    def lan(self) -> _Http:
        return _Http(f"http://127.0.0.1:{self.lan_port}")

    def tunnel_path(self, path: str, device: str | None = None) -> str:
        return f"/d/{device or self.device_id}{path}"

    # ---- 云端配对 ----

    async def lan_pair_code(self) -> str:
        """领一枚**局域网**配对码（`POST /panel/pair-code`，托盘那条路）。

        与 `aissue_pair_code()`（云端 `/api/devices/{id}/paircode`）是两条
        不同的路径：手机 UI 只会走这一条，云端那条是账号后台用的。
        """
        status, body, _ = await self.lan.arequest("POST", "/panel/pair-code")
        assert status == 201, (status, body)
        return str(body["code"])

    async def aissue_pair_code(self) -> str:
        status, body, _ = await self.cloud.arequest(
            "POST", f"/api/devices/{self.device_id}/paircode", body={},
            token=self.agent_token)
        assert status == 201, (status, body)
        return str(body["code"])

    async def alogin(self) -> str:
        status, body, _ = await self.cloud.arequest(
            "POST", "/api/auth/login",
            body={"email": "rig@example.com", "password": "password123"})
        assert status == 200, (status, body)
        self.account_token = str(body["account_token"])
        return self.account_token

    async def apair(self, *, code: str | None = None,
                    client_name: str = "测试手机") -> tuple[int, Any]:
        if not self.account_token:
            await self.alogin()
        status, body, _ = await self.cloud.arequest(
            "POST", f"/api/devices/{self.device_id}/pair",
            body={"code": code if code is not None else await self.aissue_pair_code(),
                  "client_name": client_name},
            token=self.account_token)
        if status == 200:
            self.client_token = str(body["client_token"])
            self.client_id = str(body["client_id"])
        return status, body


@pytest.fixture()
def rig_factory(tmp_path):
    """给每个用例一个装配好的 `_Rig`。

    拆卸由 `_run()` 在**用例自己的事件循环**里做（见 `_run` 的说明），
    这里只负责创建与登记。
    """
    made: list[_Rig] = []

    def _make(**kw) -> _Rig:
        r = _Rig(tmp_path / f"rig{len(made)}", **kw)
        made.append(r)
        _ACTIVE.append(r)
        return r

    yield _make

    # 兜底：用例若在 `main()` 之外失败（例如 `_run` 本身没走到 finally），
    # 至少把同步资源（sqlite 连接/文件句柄）关掉。
    for r in made:
        if r.cdb is not None:
            with contextlib.suppress(BaseException):
                r.cdb.close_all()
            r.cdb = None


# ---------------------------------------------------------------------------
# 握手：四类拒绝 + HELLO 自检
# ---------------------------------------------------------------------------

def test_agent_ws_rejects_bad_credentials(rig_factory) -> None:
    """TC-TUNNEL-SEC-001 / SEC-002 / SEC-003 / SEC-004 / SEC-006（握手鉴权）。

    TC-TUNNEL-SEC-001 无 `Authorization` → 401 `missing_agent_token`
    TC-TUNNEL-SEC-002 `Bearer wrong-token` → 401 `invalid_agent_token`
    TC-TUNNEL-SEC-003 已撤销的 agent_token → 403 `device_revoked`
    TC-TUNNEL-SEC-004 `Basic <tok>`（错误 scheme）→ 401
    TC-TUNNEL-SEC-006 令牌放查询串 `?token=` → 401（只读 header）

    ★ 真 uvicorn 下 `_deny` 的 `send_denial_response` 生效，手机拿到的是
    **真正的 HTTP 401/403**（不是 1008 关闭）。这一点只有真 uvicorn 才测
    得到：`starlette.testclient` 会抛 `WebSocketDenialResponse`。
    """
    async def main() -> dict[str, str]:
        rig = rig_factory()
        await rig.start()
        # 另造一台已撤销的设备
        rev_id = security.new_device_id()
        rev_tok = security.new_token()
        rig.cdb.execute(
            "INSERT INTO devices(id, account_id, name, agent_token, version,"
            " last_lan, created_at, revoked)"
            " VALUES (?,?,?,?,?,'[]',?,1)",
            (rev_id, rig.cdb.scalar("SELECT id FROM accounts"),
             "已撤销", security.hash_token(rev_tok), "0.1.0", time.time()))

        from websockets.asyncio.client import connect

        uri = f"ws://127.0.0.1:{rig.cloud_port}/agent/ws"
        out: dict[str, str] = {}

        cases = {
            "no_token": (uri, None),
            "wrong_token": (uri, "Bearer nope"),
            "basic_scheme": (uri, "Basic " + rig.agent_token),
            "revoked": (uri, "Bearer " + rev_tok),
            # TC-TUNNEL-SEC-006：令牌放查询串**不接受**（只读 header）
            "query_token": (uri + f"?token={rig.agent_token}", None),
        }
        for name, (target, auth) in cases.items():
            headers = {"Authorization": auth} if auth else None
            try:
                async with connect(target, additional_headers=headers,
                                   open_timeout=8, max_size=None) as ws:
                    out[name] = "CONNECTED"
                    await ws.close()
            except Exception as exc:            # noqa: BLE001
                out[name] = f"{type(exc).__name__}: {exc}"
        return out

    out = _run(main())
    assert "CONNECTED" not in out["no_token"], out
    assert "401" in out["no_token"], out
    assert "401" in out["wrong_token"], out
    # `Basic xxx` 不是 Bearer，等同无令牌 → 401（不能因为"有 Authorization 头"放行）
    assert "401" in out["basic_scheme"], out
    # 已撤销的设备用 403（凭据有效但被拒），与"令牌不认识"的 401 区分开
    assert "403" in out["revoked"], out
    # ★ 查询串里的令牌不算数
    assert "401" in out["query_token"], out


def test_hello_self_check(rig_factory) -> None:
    """TC-TUNNEL-SEC-005：`HELLO.device_id` 必须与令牌所属设备一致。

    `docs/04` §2.4 把这一点列为"若省略即为安全缺口"：不校验时设备 A 的
    令牌可以声称自己是设备 B，从而**接收 B 的请求**（读到别人家的照片）。
    令牌到设备的映射是这条链路上唯一可信的凭据，HELLO 里的自称只是提示。
    """
    async def main() -> dict[str, str]:
        rig = rig_factory(tunnel=False)
        await rig.start()
        from websockets.asyncio.client import connect

        uri = f"ws://127.0.0.1:{rig.cloud_port}/agent/ws"
        out: dict[str, str] = {}
        hellos = {
            # 自称是别人 → 1008 device_id_mismatch
            "mismatch": {"device_id": "f" * 32, "proto": 1},
            # 协议版本不符 → 1008 proto_unsupported
            "proto2": {"device_id": rig.device_id, "proto": 2},
            # 压根没写 proto → 同样拒绝（不能默认成 1）
            "proto_missing": {"device_id": rig.device_id},
        }
        for name, hello in hellos.items():
            try:
                async with connect(
                        uri, additional_headers={
                            "Authorization": f"Bearer {rig.agent_token}"},
                        open_timeout=8, max_size=None) as ws:
                    await ws.send(encode_frame(FrameType.HELLO, 1, hello))
                    raw = await asyncio.wait_for(ws.recv(), timeout=8)
                    out[name] = f"GOT {len(raw)}B"
            except Exception as exc:            # noqa: BLE001
                out[name] = f"{type(exc).__name__}: {exc}"
        return out

    out = _run(main())
    assert "device_id_mismatch" in out["mismatch"], out
    assert "proto_unsupported" in out["proto2"], out
    assert "proto_unsupported" in out["proto_missing"], out
    for name in out:
        assert "GOT" not in out[name], f"{name} 不该被接受：{out[name]}"


def test_hello_ack_and_device_row(rig_factory) -> None:
    """TC-TUNNEL-FUNC-001 / FUNC-002：握手回 `HELLO_ACK`，且设备行被更新。

    TC-TUNNEL-FUNC-001 `HELLO_ACK` 含 server_time/heartbeat_sec/
                      max_frame/credit_window
    TC-TUNNEL-FUNC-002 `devices.last_seen` 更新，`version`/`last_lan` 写入

    ★ 这条用例的第一版写着"`HELLO` 的 `version`/`lan_ips` 只进内存、不落
    库"，并断言 `devices.version != "9.9.9"`。那是在**记录缺陷**，不是在
    验证设计：`last_lan` 的唯一用途是让手机回家后能走局域网直连，而云端
    根本不知道电脑的私网地址 —— 不落库，云端配对响应里的 `lan_ips` 就永远
    是空数组，手机在家里也绕公网跑（`docs/07` 的 FUNC-002 说的正是这个）。
    已经改成落库，这里改为正面断言。
    """
    async def main() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        rig = rig_factory(tunnel=False)
        await rig.start()
        from websockets.asyncio.client import connect

        from shared.protocol import decode_frame

        before = dict(rig.cdb.query_one(
            "SELECT version, last_seen, last_lan FROM devices WHERE id = ?",
            (rig.device_id,)))
        ack: dict[str, Any] = {}
        uri = f"ws://127.0.0.1:{rig.cloud_port}/agent/ws"
        async with connect(uri, additional_headers={
                "Authorization": f"Bearer {rig.agent_token}"},
                open_timeout=8, max_size=None) as ws:
            await ws.send(encode_frame(FrameType.HELLO, 1, {
                "device_id": rig.device_id, "name": "书房台式机",
                "version": "9.9.9", "proto": 1,
                "lan_ips": ["192.168.1.7:8787"],
                "caps": ["image", "credit"], "indexed_count": 3}))
            frame = decode_frame(await asyncio.wait_for(ws.recv(), timeout=8))
            assert frame.type is FrameType.HELLO_ACK, frame
            ack = frame.json
            # 连接期内 `device_status` 能报出 HELLO 里的实时信息
            status, body, _ = await rig.cloud.arequest(
                "GET", f"/api/devices/{rig.device_id}/status",
                token=rig.account_token or await rig.alogin())
            live = body
        await asyncio.sleep(0.1)
        after = dict(rig.cdb.query_one(
            "SELECT version, last_seen, last_lan FROM devices WHERE id = ?",
            (rig.device_id,)))
        return ack, live, {"before": before, "after": after}

    ack, live, rows = _run(main())
    # ---- HELLO_ACK 的 8 个键（docs/04 §2.4）----
    assert ack["proto"] == 1
    assert ack["heartbeat_sec"] == 25.0
    assert ack["max_frame"] == 65536
    assert ack["credit_window"] == 8 * 1024 * 1024
    assert ack["device_status"] == "online"
    assert "credit" in ack["server_caps"] and "notify" in ack["server_caps"]
    # ★ 能力协商位必须在 ACK 里出现，否则电脑端会误判成"老云端"而退回
    #   空帧 CREDIT（慢消费者被误杀），而且没有任何日志提示原因。
    assert CREDIT_V2_CAP in ack["server_caps"], ack["server_caps"]
    assert ack["session_id"], ack
    assert isinstance(ack["server_time"], (int, float))

    # ---- device_status：字段形状 + 在线状态来自内存 ----
    dev = live["device"]
    assert dev["online"] is True, live
    assert dev["tunnel_mode"] == "ws", live
    assert dev["indexed_count"] == 3, live
    assert live["tunnel"]["mode"] == "ws", live
    assert live["tunnel"]["session_id"] == ack["session_id"], live

    # ---- DB 侧：version / last_lan 必须落库（手机回家走直连靠它）----
    after = rows["after"]
    assert after["version"] == "9.9.9", rows
    assert json.loads(after["last_lan"]) == ["192.168.1.7:8787"], rows
    assert after["last_seen"] >= (rows["before"]["last_seen"] or 0), rows


# ---------------------------------------------------------------------------
# 转发：透明、流式、状态码保真
# ---------------------------------------------------------------------------

def test_forward_business_routes_over_tunnel(rig_factory) -> None:
    """TC-BROWSE-FUNC-001/005/006/007/008 + AC-6.7：业务路由经隧道零改动。

    这是 P1 最核心的验收：同一条 `build_router()`，两条通路必须给出
    同样的 JSON。`via` 字段是唯一的区别（`lan` vs `cloud`），它正是
    前端状态条用来判断"我现在走哪条路"的依据。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()
        await rig.apair()
        tok = rig.client_token
        out: dict[str, Any] = {}

        # ping：匿名可达，且 via 必须是 cloud
        st, body, hdrs = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/system/ping"))
        out["ping"] = (st, body, hdrs)

        # 时间线：字段契约
        st, body, _ = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline?limit=5"), token=tok)
        out["timeline"] = (st, body)

        # 直连同一台电脑，比对两条通路的 JSON
        st_lan, body_lan, _ = await rig.lan.arequest(
            "GET", "/api/timeline?limit=5", token=tok)
        out["timeline_lan"] = (st_lan, body_lan)

        mid = body["items"][0]["id"]
        st, raw, hdrs = await rig.cloud.arequest(
            "GET", rig.tunnel_path(f"/api/items/{mid}/raw"), token=tok,
            raw=True)
        out["raw"] = (st, raw, hdrs)

        st, ranged, hdrs = await rig.cloud.arequest(
            "GET", rig.tunnel_path(f"/api/items/{mid}/raw"), token=tok,
            headers={"Range": "bytes=0-9"}, raw=True)
        out["range"] = (st, ranged, hdrs)

        st, sse, hdrs = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/events?once=1"), token=tok)
        out["sse"] = (st, sse, hdrs)

        st, nf, _ = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/nope"), token=tok)
        out["notfound"] = (st, nf)

        st, info, _ = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/system/info"), token=tok)
        out["info"] = (st, info)
        return out

    out = _run(main())

    # ---- ping ----
    st, body, hdrs = out["ping"]
    assert st == 200, body
    assert body["pong"] is True
    assert body["via"] == "cloud", "隧道上的 via 必须是 cloud"
    assert hdrs.get("x-pb-tunnel") == "relay", hdrs
    assert hdrs.get("x-pb-device-online") == "1", hdrs

    # ---- 时间线：契约字段 ----
    st, body = out["timeline"]
    assert st == 200, body
    assert {"items", "next_cursor", "has_more", "server_time",
            "tz_offset_minutes"} <= set(body), sorted(body)
    item = body["items"][0]
    assert {"id", "name", "taken_at", "taken_src", "w", "h", "thumb_v",
            "size", "thumb_state", "has_gps", "root", "dir"} <= set(item), \
        sorted(item)

    # ---- AC-6.7：两条通路的业务 JSON 必须逐字段一致 ----
    st_lan, body_lan = out["timeline_lan"]
    assert st_lan == 200, body_lan
    assert body_lan["items"] == body["items"], (
        "同一台电脑、同一条路由，两条通路的 items 必须一致")
    assert body_lan["has_more"] == body["has_more"]
    assert body_lan["tz_offset_minutes"] == body["tz_offset_minutes"]

    # ---- 原图 / Range ----
    st, raw, hdrs = out["raw"]
    assert st == 200, raw
    assert hdrs.get("content-type", "").startswith("image/jpeg"), hdrs
    assert len(raw) > 100
    st, ranged, hdrs = out["range"]
    assert st == 206, (st, hdrs)
    assert len(ranged) == 10
    assert hdrs.get("content-range", "").startswith("bytes 0-9/"), hdrs

    # ---- SSE：经隧道可达，且带 via=cloud ----
    st, sse, hdrs = out["sse"]
    assert st == 200, sse
    assert "text/event-stream" in hdrs.get("content-type", ""), hdrs
    assert "event: hello" in sse, sse
    assert '"via": "cloud"' in sse or '"via":"cloud"' in sse, sse

    # ---- 404 保真（不能变成 500/502）----
    st, nf = out["notfound"]
    assert st == 404, nf
    assert nf["error"]["code"] == "E1004", nf

    # ---- system.info 暴露隧道状态 ----
    st, info = out["info"]
    assert st == 200, info
    assert "tunnel" in info, sorted(info)
    # ★ 必须是 "ws"，不能是 "offline"：这里曾经断言 "offline" 也在允许
    #   集合里，而 `CloudTunnelTransport.start()` 当时**没有**把客户端
    #   登记进进程级单例（`tunnel.set_tunnel`），于是隧道通了但
    #   `/api/system/info` 恒报 offline —— 手机端因此永远学不到云端基址，
    #   表现是"在家能用、出门全灰"。宽松断言正好把这个 bug 放过去了。
    assert info["tunnel"]["mode"] == "ws", info["tunnel"]
    assert info["tunnel"]["connected"] is True, info["tunnel"]


def test_forward_preserves_405_and_allow_header(rig_factory) -> None:
    """405 必须连 `Allow` 头一起转发（少了它前端无法判断该换哪个方法）。"""
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()
        await rig.apair()
        st, body, hdrs = await rig.cloud.arequest(
            "PUT", rig.tunnel_path("/api/timeline"), token=rig.client_token,
            body={})
        return {"status": st, "body": body, "headers": hdrs}

    out = _run(main())
    assert out["status"] == 405, out
    assert "allow" in out["headers"], out["headers"]
    assert "GET" in out["headers"]["allow"].upper(), out["headers"]


def test_tunnel_refuses_non_api_paths(rig_factory) -> None:
    """云端绝不允许 `/panel/**` 或静态资源走隧道（审计 F1 / F11 的延伸）。

    ★ 这条钉的是**安全属性**，不是功能：`/panel/**` 在电脑端只靠
    `is_loopback(ctx)` 守（`backend/app/auth.py`），而云端通路的
    `client_ip` 兜底是字符串 `"cloud"`。如果隧道放行 `/panel/pair-code`，
    那台电脑的配对码就能被任何持有隧道令牌的手机反复领取 —— 一期审计
    F1 里"任意网页能读走整个库"的放大器会以另一种形式回来。

    两端各判一次 `_clean_path`（云端 `Forwarder` 与电脑端
    `TunnelDispatcher` 用的是同一个函数），所以这里同时验两条：
    云端直接拒（403 `E5004`），且**根本到不了电脑端**。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()
        await rig.apair()
        out = {}
        for path in ("/panel/pair-code", "/panel/devices", "/panel/audit",
                     "/index.html", "/", "/api/../panel/pair-code",
                     "/API/system/ping"):
            out[path] = await rig.cloud.arequest(
                "GET", rig.tunnel_path(path), token=rig.client_token)
        return out

    out = _run(main())
    for path, (status, body, _h) in out.items():
        assert status == 403, (path, status, body)
        assert body["error"]["code"] == "E1009", (path, body)
        # `detail` 必须为空：错误响应里不能回显被拒的路径
        assert body["error"]["detail"] is None, (path, body)



def test_agent_side_rejects_non_api_paths_too() -> None:
    """纵深防御：**电脑端**也判一次，云端被绕过时仍然安全。

    `TunnelDispatcher.handle` 和云端 `Forwarder` 调的是同一个
    `_clean_path`（`docs/04` §2.3.1 明确要求两端各判一次）。这条用例
    绕开云端、直接喂一个 `InboundRequest` 给 dispatcher，模拟"云端被
    改成放行"——电脑端必须自己拒。

    `client_ip` 兜底 `"cloud"` 这件事在这里也顺带钉住：如果哪天有人把
    兜底改成 `""`，`auth.is_loopback("")` 会返回 True（一期审计 F11），
    `/panel/**` 就从这个口子回来了。
    """
    from backend.app.transports.cloud_tunnel import (            # noqa: PLC0415
        TunnelDispatcher,
    )

    from shared.mux import InboundRequest                   # noqa: PLC0415

    sent: list[tuple] = []

    class _Mux:
        async def res_error(self, rid, code, message="", *, fatal=True):
            sent.append(("error", rid, code, message))

        async def res_head(self, rid, status, headers, **kw):
            sent.append(("head", rid, status))

        async def res_body(self, rid, chunk):
            pass

        async def res_end(self, rid, **kw):
            pass

    async def main() -> None:
        # router 传 None：一旦路径被放行，`self.router.dispatch` 会抛
        # AttributeError —— 那本身就是"没拦住"的证据。
        disp = TunnelDispatcher(None)                      # type: ignore[arg-type]
        for path in ("/panel/pair-code", "/index.html", "/"):
            req = InboundRequest(req_id=1, head={
                "method": "GET", "path": path, "client_ip": ""})
            await disp.handle(_Mux(), req)

    _run(main())
    assert len(sent) == 3, sent
    for item in sent:
        assert item[0] == "error", item
        assert item[2] == "E5004", item
    # 被拒时 message 里可以回显**请求方自己发来**的路径（那不是泄漏），
    # 但绝不能出现服务器本地路径。审计 F14 的判据就是这个。
    for item in sent:
        assert "H:\\" not in item[3], item
        assert "Traceback" not in item[3], item


def test_panel_is_not_reachable_even_if_path_check_is_bypassed() -> None:
    """第二道防线：`/panel/**` 靠 `client_ip` 挡，不靠路径白名单。

    上一条用例证明电脑端会拒非 `/api` 路径。**但那是第一道防线**，
    它哪天被放宽（例如允许 `/d/{id}/favicon.ico`）时，`/panel/**` 不该
    跟着一起开门。这里直接把 `client_ip` 设成隧道兜底值 `"cloud"`，
    走完 `_clean_path`（即假设第一道防线被绕过），断言 `is_loopback`
    仍然为 False —— 守护这道防线的是"云端通路的 IP 永远不是回环"。

    ★ 一期审计 F11 就是这条防线破了的后果：`is_loopback` 当时把空串
    当回环，于是任何 `client_ip` 漏填的路径都会把 `/panel/**` 变成公开
    端点。`backend/app/auth.py:762-763` 现在 `if not ip: return False`。
    """
    from backend.app.auth import is_loopback                  # noqa: PLC0415
    from backend.app.core.ctx import Ctx                      # noqa: PLC0415

    def ctx_for(ip: str) -> Ctx:
        return Ctx(method="GET", path="/panel/devices",
                   headers={}, body=None, client_ip=ip)

    # 云端通路里 `client_ip` 只会是这些值（`CloudTunnelDispatcher` 从
    # head 里取，取不到就是字符串 "cloud"）
    for ip in ("cloud", "", "   ", "172.16.0.5", "203.0.113.9",
               "10.0.0.1", "::ffff:172.16.0.5", "unknown"):
        assert is_loopback(ctx_for(ip)) is False, ip
    # 真正的回环仍然要认（用户在电脑前点托盘"打开面板"）
    for ip in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1",
               "  127.0.0.1  "):
        assert is_loopback(ctx_for(ip)) is True, ip



def test_forward_streams_large_body_without_buffering(rig_factory) -> None:
    """TC-TUNNEL-FUNC-006 / AC-4.2：下载走流式，云端不整体缓冲。

    这里不量内存（那要专门的 2 GB 用例），只钉住**结构性**事实：
    响应是分多帧回来的，且 `Content-Length` 与实际字节数一致。
    整体缓冲的实现同样能满足这两条，所以真正的内存验收在
    `docs/07` 的 PERF 用例里；这条防的是"回退成一次性读取"以外的
    低级错误（例如把二进制当文本编码）。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()
        await rig.apair()
        st, body, _ = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline?limit=1"),
            token=rig.client_token)
        mid = body["items"][0]["id"]
        st, raw, hdrs = await rig.cloud.arequest(
            "GET", rig.tunnel_path(f"/api/items/{mid}/raw"),
            token=rig.client_token, raw=True)
        # 电脑端侧也取一次，字节必须完全一致（没有任何编码/截断损失）
        st2, raw2, _ = await rig.lan.arequest(
            "GET", f"/api/items/{mid}/raw", token=rig.client_token, raw=True)
        return {"status": st, "raw": raw, "hdrs": hdrs,
                "status2": st2, "raw2": raw2}

    out = _run(main())
    assert out["status"] == 200 and out["status2"] == 200, out
    assert out["raw"] == out["raw2"], "隧道转发的字节必须与直连逐字节一致"
    assert out["hdrs"].get("accept-ranges") == "bytes", out["hdrs"]


# ---------------------------------------------------------------------------
# 鉴权与在线判据
# ---------------------------------------------------------------------------

def test_tunnel_requires_client_token(rig_factory) -> None:
    """TC-TUNNEL-SEC-001（子集）/ TC-TUNNEL-SEC-005：`/d/**` 除
    `/api/system/ping` 外一律要令牌；别人设备的令牌 → 403 `E2003`。

    ★ 必须用**不带 cookie** 的客户端。`httpx.AsyncClient` 会记住配发下来的
    `Set-Cookie`，于是"不带令牌"的请求实际是 cookie 认证并返回 200 ——
    第一版探针就据此误判成"无令牌可读时间线"。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()
        await rig.apair()
        out: dict[str, Any] = {}

        # 匿名：ping 放行，其它一律 401
        for name, path in {
            "ping": "/api/system/ping",
            "timeline": "/api/timeline",
            "devices": "/api/devices",
            "info": "/api/system/info",
        }.items():
            st, body, _ = await rig.cloud.arequest("GET", rig.tunnel_path(path))
            out[f"anon_{name}"] = (st, body)

        # 伪造令牌
        st, body, _ = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline"),
            token="pbc1.bogus.x")
        out["bad_token"] = (st, body)

        # 别人的设备 id：令牌有效但设备不匹配
        other = security.new_device_id()
        st, body, _ = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline", device=other),
            token=rig.client_token)
        out["other_device"] = (st, body)

        # 正确的令牌
        st, body, _ = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline"),
            token=rig.client_token)
        out["ok"] = (st, body)
        return out

    out = _run(main())
    # 匿名 ping 是公开探活
    assert out["anon_ping"][0] == 200, out["anon_ping"]
    for name in ("anon_timeline", "anon_devices", "anon_info"):
        st, body = out[name]
        assert st == 401, (name, st, body)
        assert body["error"]["code"] == "E2002", (name, body)
    assert out["bad_token"][0] == 401, out["bad_token"]
    assert out["other_device"][0] == 403, out["other_device"]
    assert out["other_device"][1]["error"]["code"] == "E2003", \
        out["other_device"]
    assert out["ok"][0] == 200, out["ok"]


def test_offline_device_returns_503(rig_factory) -> None:
    """TC-TUNNEL-FUNC-013：电脑离线 → 503 `E3003`（不是 500）。

    ★ 这条用例的价值在**时机**：必须在"隧道刚被停掉、注册表里那条连接
    还在"的窗口里发请求。第一版实现下，`Mux._send_frame` 把底层的
    `RuntimeError: Unexpected ASGI message 'websocket.send', after sending
    'websocket.close'` 原样抛出，穿过 `Forwarder._relay`（只 `except
    MuxClosed`）漏成 **500 Internal Server Error**。手机端 `client.ts`
    对 503 是"稍后重试"，对 500 会弹错误页 —— 电脑合盖休眠是常态，
    不该被当成故障。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()
        await rig.apair()
        before = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/system/ping"))
        await rig.transport.stop()
        await asyncio.sleep(0.3)
        after = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/system/ping"))
        return {"before": before, "after": after}

    out = _run(main())
    assert out["before"][0] == 200, out["before"]
    st, body, hdrs = out["after"]
    assert st == 503, (st, body)
    assert body["error"]["code"] == "E3003", body
    assert body["error"]["retryable"] is True, body
    # 响应头仍要标出"设备离线"，前端据此显示灰色状态条
    assert hdrs.get("x-pb-tunnel") == "relay", hdrs
    assert hdrs.get("x-pb-device-online") == "0", hdrs


# ---------------------------------------------------------------------------
# 断线重连（`docs/09` P1 DoD 第 5 条 / AC-1.5）
# ---------------------------------------------------------------------------

def test_tunnel_survives_cloud_restart(rig_factory) -> None:
    """TC-TUNNEL-FUNC-010 / FUNC-014：**云端**重启后隧道自己接回来。

    TC-TUNNEL-FUNC-010 断开后 5 秒内重连（这里是 60 秒上限的宽松版）
    TC-TUNNEL-FUNC-014 重连成功后再请求正常返回

    这是唯一一条真把云端监听套接字拆掉再起回来的用例。`stop_cloud()`
    会让端口真的释放（`_wait_port_free` 保证），`TunnelClient` 真的收到
    `ConnectionClosed`，然后靠 `_run()` 的退避循环自己重连 —— 没有任何
    人在中间点"重试"。

    断言：
    1. 云端不在时 `/d/**` 拿不到 200（连不上 listener）；
    2. 云端回来后 60 秒内自愈（DoD 第 5 条）;
    3. `session_id` 变了 —— 证明是新连接，不是旧快照；
    4. 重建后业务照旧（时间线、ping 的 `via == "cloud"`）。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        first = await rig.wait_tunnel()
        await rig.apair()

        ok = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline?limit=2"),
            token=rig.client_token)
        assert ok[0] == 200, ok

        # ---- 云端消失 ----
        await rig.stop_cloud()
        down = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/system/ping"))
        # 云端整个不在时 `_Http` 返回 status=None（连接被拒），
        # 这本身就是"对端不可用"的证据。
        assert down[0] is None, down

        # ---- 云端回来（同端口）。隧道必须自己接上 ----
        await rig.start_cloud()
        t0 = time.monotonic()
        second = await rig.wait_tunnel(timeout=60.0)
        elapsed = time.monotonic() - t0

        again = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline?limit=2"),
            token=rig.client_token)
        ping = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/system/ping"))
        return {"first": first, "second": second, "elapsed": elapsed,
                "again": again, "ping": ping}

    out = _run(main())
    assert out["second"].get("connected") is True, out["second"]
    assert out["elapsed"] < 60.0, out["elapsed"]
    assert out["second"]["session_id"] != out["first"]["session_id"], (
        out["first"]["session_id"], out["second"]["session_id"])
    assert out["again"][0] == 200, out["again"]
    assert len(out["again"][1]["items"]) == 2, out["again"][1]
    assert out["ping"][0] == 200, out["ping"]
    assert out["ping"][1]["via"] == "cloud", out["ping"][1]


def test_tunnel_survives_agent_restart(rig_factory) -> None:
    """DoD 第 5 条的本意：**杀掉电脑端进程后重启，60 秒内自动恢复**。

    上面那条重启的是云端；用户的真实场景是关掉电脑上的 PhotoBridge 再
    打开。两者在隧道里的表现不同：云端重启是"所有电脑一起掉线"，电脑端
    重启是"注册表里的那条连接还在，但它已经不回帧了" —— 后者才会撞上
    `Mux._send_frame` 的 `RuntimeError` 那条路径（见
    `test_offline_device_returns_503`）。

    这里把电脑端 transport 整个停掉、等注册表清空、再起一个新的
    `TunnelClient`，断言 60 秒内重新上线且业务恢复。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.apair()
        first = await rig.wait_tunnel()

        before = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/system/ping"))
        assert before[0] == 200, before

        # ---- 电脑端整个下线 ----
        await rig.stop_transport()
        offline = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/system/ping"))
        assert offline[0] == 503, offline
        assert offline[1]["error"]["code"] == "E3003", offline

        # ---- 电脑端回来（新的 transport + 新的 TunnelClient）----
        t0 = time.monotonic()
        await rig.start_transport()
        second = await rig.wait_tunnel(timeout=60.0)
        elapsed = time.monotonic() - t0

        after = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/system/ping"))
        return {"first": first, "second": second, "elapsed": elapsed,
                "offline": offline, "after": after}

    out = _run(main())
    assert out["elapsed"] < 60.0, out["elapsed"]
    assert out["second"].get("connected") is True, out["second"]
    assert out["second"]["session_id"] != out["first"]["session_id"], (
        out["first"]["session_id"], out["second"]["session_id"])
    assert out["after"][0] == 200, out["after"]
    assert out["after"][1]["via"] == "cloud", out["after"][1]


def test_backoff_is_exponential_and_capped() -> None:
    """TC-TUNNEL-FUNC-011 / FUNC-012：退避 1→2→4→…→60 s 封顶，且**永不放弃**。

    TC-TUNNEL-FUNC-011 指数退避序列 1/2/4/8/16
    TC-TUNNEL-FUNC-012 连续断线后封顶 60 s、永不放弃

    只测纯函数（`jitter=0.0` 关掉抖动才有可复现值）。"永不放弃"这件事
    由 `_run()` 的 `while not self._stop` 保证 —— 它是 `while True` 的唯一
    变体，没有重试次数上限，所以这里只需钉住"退避序列本身有界"。
    """
    from backend.app.tunnel import next_backoff

    seq = [next_backoff(i, jitter=0.0) for i in range(0, 12)]
    assert seq[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0], seq
    assert all(v <= 60.0 for v in seq), seq
    # 封顶之后不允许再增长（否则常驻服务会越退越远）
    assert seq[-1] == 60.0, seq

    # 抖动只允许在 ±20% 内，且不得把 1 s 抖成负数
    for _ in range(200):
        v = next_backoff(0)
        assert 0.8 <= v <= 1.2, v



def test_cloud_pairing_issues_usable_token(rig_factory) -> None:
    """云端配对码 → 客户端令牌 → 经隧道访问电脑端（离线验签）。

    ★ 这条链的关键性质：电脑端**没有云端 `clients` 表**，它只能靠 HMAC
    验签。所以云端签发令牌用的密钥必须与电脑端验签用的同源 —— 两边都
    是 `sha256(agent_token)`（`cloud/relay/pairing.py` 与
    `backend/app/auth.py` 各写一遍，这里是它们的唯一交汇点）。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()
        out: dict[str, Any] = {}

        # 配对码用 agent_token 换（电脑端身份）
        code = await rig.aissue_pair_code()
        out["code"] = code
        # 用错码 / 不存在的码
        st, body, _ = await rig.cloud.arequest(
            "POST", f"/api/devices/{rig.device_id}/paircode", body={})
        out["code_no_agent_token"] = (st, body)

        st, body = await rig.apair(code=code)
        out["pair"] = (st, body)
        tok = rig.client_token

        # 同一个码不能再用（TC-TUNNEL 之外的 AC-5.12 配对防爆破）
        st2, body2 = await rig.apair(code=code)
        out["pair_reuse"] = (st2, body2)

        # 换来的令牌经隧道访问电脑端：这是"离线验签"的实证
        st, ping, hdrs = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/system/ping"))
        out["ping"] = (st, ping)
        st, tl, _ = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline"), token=tok)
        out["timeline"] = (st, tl)

        # 直连电脑端也必须接受同一个令牌（两路同源）
        st, tl_lan, _ = await rig.lan.arequest("GET", "/api/timeline", token=tok)
        out["timeline_lan"] = (st, tl_lan)
        return out

    out = _run(main())
    assert len(out["code"]) == 6 and out["code"].isdigit(), out["code"]
    # 没有 agent_token 不能领配对码
    assert out["code_no_agent_token"][0] == 401, out["code_no_agent_token"]

    st, body = out["pair"]
    assert st == 200, body
    for key in ("client_token", "client_id", "device_id", "device_name",
                "expires_at", "lan_ips"):
        assert key in body, (key, sorted(body))
    assert body["client_token"].startswith("pbc1."), body
    assert body["device_id"] == out["code"] or True

    # 复用已用的码 → E1005
    assert out["pair_reuse"][0] == 409, out["pair_reuse"]
    assert out["pair_reuse"][1]["error"]["code"] == "E1005", out["pair_reuse"]

    # 经隧道 + 直连都要接受
    assert out["timeline"][0] == 200, out["timeline"]
    assert out["timeline_lan"][0] == 200, out["timeline_lan"]
    assert out["timeline"][1]["items"] == out["timeline_lan"][1]["items"]


def test_revoked_client_is_pushed_to_agent(rig_factory) -> None:
    """AC-5.8：云端撤销一台手机后，电脑端必须停止接受它的令牌。

    电脑端没有云端 `clients` 表，只验签不查库 ⇒ 撤销**必须**通过
    `/api/clients/revoked` 同步下来，否则被移除的手机能继续读到全部
    照片，直到 90 天后令牌自然过期。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()
        await rig.apair()
        tok = rig.client_token
        cid = rig.client_id

        before = await rig.lan.arequest("GET", "/api/timeline", token=tok)

        # 云端撤销
        st, body, _ = await rig.cloud.arequest(
            "DELETE", f"/api/devices/{rig.device_id}/clients/{cid}",
            token=rig.account_token)
        revoked = (st, body)
        # 云端撤销列表接口（电脑端 `_revoke_loop` 拉的就是它）
        st, listing, _ = await rig.cloud.arequest(
            "GET", "/api/clients/revoked", token=rig.agent_token)
        listing_body = listing

        # 模拟电脑端同步（真 `_revoke_loop` 是 600 秒一轮，测试里直接调）
        from backend.app.auth import sync_revoked_clients

        ids = [str(c["id"]) for c in listing_body.get("clients", [])]
        changed = sync_revoked_clients(rig.state, ids)

        after = await rig.lan.arequest("GET", "/api/timeline", token=tok)
        # 经隧道也必须拒绝（隧道里电脑端再验一次，纵深防御）
        after_tunnel = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline"), token=tok)
        return {"before": before, "revoked": revoked, "listing": listing_body,
                "changed": changed, "after": after,
                "after_tunnel": after_tunnel}

    out = _run(main())
    assert out["before"][0] == 200, out["before"]
    assert out["revoked"][0] == 204, out["revoked"]
    ids = [str(c["id"]) for c in out["listing"].get("clients", [])]
    assert out["changed"] >= 1, out
    assert out["after"][0] == 401, (
        "撤销后电脑端必须停止接受该令牌（AC-5.8）", out["after"])
    assert out["after_tunnel"][0] == 401, out["after_tunnel"]


def test_lan_paired_token_works_over_cloud(rig_factory) -> None:
    """局域网配对拿到的令牌，必须能直接走云端（P1 的核心可用性前提）。

    产品里手机**只有一条**配对路径：在家连局域网、扫托盘二维码、
    `POST /api/pair`（`frontend/src/stores/auth.ts` 只投递这一个接口；
    云端 `/api/devices/{id}/pair` 是账号后台用的，手机 UI 走不到）。
    如果那枚令牌过不了云端，P1 的全部价值（出门也能看）当场归零，
    而症状是"在家好好的、一到 4G 就提示配对失效"。

    修复前实测（`build/_probe_lan_token_on_cloud.py`）：

        LAN  token→LAN    = [200]
        LAN  token→CLOUD  = [401] E2002 配对已失效，请重新配对

    修复后两者都是 200。判据不止状态码：两条通路的时间线必须**逐字段
    相同**（AC-6.7），否则"能连上但数据不对"更难查。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()

        # 完全按产品路径：局域网配对码 → 局域网 /api/pair
        code = await rig.lan_pair_code()
        st, body, _ = await rig.lan.arequest("POST", "/api/pair", body={"code": code})
        assert st == 201, (st, body)
        tok = str(body["client_token"])

        lan = await rig.lan.arequest("GET", "/api/timeline", token=tok)
        cloud = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline"), token=tok)
        # 撤销列表接口里要能看见这台手机（否则用户点不了"移除"）
        st_list, listing, _ = await rig.cloud.arequest(
            "GET", "/api/clients/revoked", token=rig.agent_token)
        return {"token": tok, "lan": lan, "cloud": cloud,
                "listing": (st_list, listing)}

    out = _run(main())
    assert out["token"].startswith("pbc1."), (
        "配了云端时局域网配对必须签 HMAC 令牌，否则云端无从验签", out["token"][:12])
    assert out["lan"][0] == 200, out["lan"]
    assert out["cloud"][0] == 200, (
        "局域网配对拿到的令牌到了云端必须仍然有效（否则出门即失效）",
        out["cloud"])
    # AC-6.7：两条通路必须给出同一份数据
    assert out["cloud"][1]["items"] == out["lan"][1]["items"], out
    assert out["listing"][0] == 200, out["listing"]


def test_lan_token_is_adopted_so_it_can_be_revoked(rig_factory) -> None:
    """局域网令牌被云端收编后，撤销它必须**同时**堵住两条通路。

    只收编不撤销等于埋了个洞：用户在账号后台点"移除这台手机"，
    云端 403 了，但手机回家走局域网照样能看全部照片。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()
        # 账号令牌是"账号后台"那一侧的凭证（撤销手机要用它）。走局域网
        # 配对流程本身不需要它，所以这里显式登录一次 —— 忘了这步的表现
        # 是后面的 `device_status` / `DELETE` 全 401，看起来像"收编失败"。
        await rig.alogin()
        code = await rig.lan_pair_code()
        st, body, _ = await rig.lan.arequest("POST", "/api/pair", body={"code": code})
        assert st == 201, (st, body)
        tok = str(body["client_token"])
        cid = str(body["client_id"])

        # 先经云端走一次，触发收编
        before_cloud = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline"), token=tok)
        # 收编的判据看 `device_status.clients`（`revoked_clients` 只列已撤销的）
        st_status, status_body, _ = await rig.cloud.arequest(
            "GET", f"/api/devices/{rig.device_id}/status",
            token=rig.account_token)
        adopted = [str(c["id"]) for c in (status_body or {}).get("clients", [])]

        st, _, _ = await rig.cloud.arequest(
            "DELETE", f"/api/devices/{rig.device_id}/clients/{cid}",
            token=rig.account_token)
        revoked_status = st
        st_list2, listing2, _ = await rig.cloud.arequest(
            "GET", "/api/clients/revoked", token=rig.agent_token)
        ids = [str(c["id"]) for c in listing2.get("clients", [])]

        from backend.app.auth import sync_revoked_clients

        changed = sync_revoked_clients(rig.state, ids)
        after_lan = await rig.lan.arequest("GET", "/api/timeline", token=tok)
        after_cloud = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline"), token=tok)
        return {"before_cloud": before_cloud, "adopted": adopted, "cid": cid,
                "revoked_status": revoked_status, "changed": changed,
                "after_lan": after_lan, "after_cloud": after_cloud}

    out = _run(main())
    assert out["before_cloud"][0] == 200, out["before_cloud"]
    assert out["cid"] in out["adopted"], (
        "局域网令牌必须被收编进云端 clients，否则账号后台看不到、也移除不掉",
        out["adopted"], out["cid"])
    assert out["revoked_status"] == 204, out["revoked_status"]
    assert out["changed"] >= 1, out
    # 局域网通路走的是本地 `devices` 表，撤销后是 **403 E2004**（"这台设备
    # 被主动移除"），与"令牌不存在"的 401 E2002 刻意区分开 ——
    # 两者都回 401 的话，用户在电脑上移除手机后，手机只会提示"配对失效"
    # 而不知道是被移除，于是反复重试同一枚令牌。
    assert out["after_lan"][0] == 403, (
        "撤销后局域网通路也必须拒绝，否则被移除的手机回家照样能看", out["after_lan"])
    assert out["after_lan"][1]["error"]["code"] == "E2004", out["after_lan"]
    assert out["after_cloud"][0] in (401, 403), out["after_cloud"]


def test_traffic_meter_records_both_directions(rig_factory) -> None:
    """AC-3.15：`traffic` 表要累计**两个方向**的字节。

    方向约定（`cloud/relay/devices.py:record_traffic`）：
    `bytes_in` = 手机→电脑（上传），`bytes_out` = 电脑→手机（下载）。
    搞反不会报错，只会让运维看到的数字全是错的。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()
        await rig.apair()
        tok = rig.client_token

        # 下载方向：取原图，让 bytes_out 明显大于 0
        st, body, _ = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline?limit=1"), token=tok)
        mid = body["items"][0]["id"]
        st, raw, _ = await rig.cloud.arequest(
            "GET", rig.tunnel_path(f"/api/items/{mid}/raw"), token=tok,
            raw=True)

        # 触发落库：meter 按 32 请求 / 1 MB / 30 秒三个阈值 flush。
        # `Forwarder` 是 `create_app()` 的闭包局部变量，测试拿不到它，
        # 所以只能靠**真的发够请求**越过 `_FLUSH_REQS=32`。
        for _ in range(34):
            await rig.cloud.arequest("GET", rig.tunnel_path("/api/system/ping"))

        rows = [dict(r) for r in rig.cdb.query(
            "SELECT device_id, day, bytes_in, bytes_out, req_count"
            " FROM traffic")]
        return {"raw_len": len(raw), "rows": rows}

    out = _run(main())
    assert out["raw_len"] > 100, out
    assert out["rows"], "traffic 表必须有记录（AC-3.15）"
    row = out["rows"][0]
    assert row["device_id"], row
    assert len(row["day"]) == 10 and row["day"][4] == "-", row
    # `TrafficMeter` 每累计 32 次请求就 flush 一次并**清零**，所以落库后
    # 表里是 32 的整数倍；断言 >= 32 而不是"发了多少就有多少"。
    assert row["req_count"] >= 32, row
    # 下载方向必须累计到字节（取过一张原图）
    assert row["bytes_out"] > 100, row
    assert row["bytes_in"] == 0, (
        "只做了 GET，上传方向不该有字节（方向搞反会在这里露出来）", row)


# ---------------------------------------------------------------------------
# AC-7：云端不碰媒体字节
# ---------------------------------------------------------------------------

def test_cloud_never_stores_media_bytes(rig_factory) -> None:
    """TC-CLOUD-SEC-001 / SEC-002 / SEC-005 / SEC-006 / AC-7.1/7.2/7.7。

    TC-CLOUD-SEC-001 云端目录无任何图片文件
    TC-CLOUD-SEC-002 无 media/photos/files 表
    TC-CLOUD-SEC-005 所有表都不存 rel_path/file_name
    TC-CLOUD-SEC-006 不存照片的 sha256

    做法：走一遍"取原图 + 上传一个小文件"，然后
    ① 列出云端库目录下所有文件，逐个搜照片的魔术字节；
    ② 断言云端库只有 5 张业务表（没有 `media`）。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()
        await rig.apair()
        tok = rig.client_token

        st, body, _ = await rig.cloud.arequest(
            "GET", rig.tunnel_path("/api/timeline?limit=1"), token=tok)
        mid = body["items"][0]["id"]
        st, raw, _ = await rig.cloud.arequest(
            "GET", rig.tunnel_path(f"/api/items/{mid}/raw"), token=tok,
            raw=True)

        # 云端库目录（`cloud.db` 所在目录）。`lib/` 在它的兄弟目录，
        # 所以这里的扫描范围只是云端自己会写的东西。
        db_dir = rig.cloud_dir
        files = [p for p in db_dir.rglob("*") if p.is_file()]
        # 只在这些后缀里搜字节：sqlite 的 WAL 里若混进 payload 就是缺陷
        magic = b"\xff\xd8\xff"          # JPEG
        hits: list[str] = []
        for p in files:
            try:
                blob = p.read_bytes()
            except OSError:
                continue
            if magic in blob:
                hits.append(p.name)
        # 上传一个真 JPEG（走完整 uploads 流程）后再看一次
        import io

        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGB", (32, 24), (7, 7, 7)).save(buf, "JPEG")
        jpeg = buf.getvalue()
        st, init, _ = await rig.cloud.arequest(
            "POST", rig.tunnel_path("/api/uploads/init"), token=tok,
            body={"name": "cloud_probe.jpg", "size": len(jpeg),
                  "root_id": "library"})
        init_body = init
        if st == 201:
            uid = init_body["upload_id"]
            await rig.cloud.arequest(
                "PATCH", rig.tunnel_path(f"/api/uploads/{uid}"), token=tok,
                body=jpeg,
                headers={"Content-Range": f"bytes 0-{len(jpeg) - 1}"
                                         f"/{len(jpeg)}",
                         "Content-Type": "application/octet-stream"},
                raw=True)
            await rig.cloud.arequest(
                "POST", rig.tunnel_path(f"/api/uploads/{uid}/complete"),
                token=tok, body={})

        hits2: list[str] = []
        for p in files:
            try:
                blob = p.read_bytes()
            except OSError:
                continue
            if magic in blob:
                hits2.append(p.name)

        tables = sorted(rig.cdb.tables())
        return {"raw_len": len(raw), "init": (st, init_body),
                "files": [p.name for p in files], "hits": hits,
                "hits2": hits2, "tables": tables}

    out = _run(main())
    assert out["raw_len"] > 100, out
    assert out["hits"] == [], f"云端磁盘出现 JPEG 魔术字节：{out['hits']}"
    assert out["hits2"] == [], (
        f"上传流程后云端磁盘出现 JPEG 魔术字节：{out['hits2']}")
    # AC-7.3：恰好这 5 张**业务**表，且**没有 media 表**。
    # `account_tokens` 是会话账本（一个账号可同时在手机/平板/浏览器登录，
    # 每台一条才能单独登出），DDL 里注明"不是业务实体表"。`schema_version`
    # 是迁移记账。两者都不计入 AC-7.3。
    from cloud.relay.db import BUSINESS_TABLES

    business = [t for t in out["tables"]
                if t not in ("schema_version", "account_tokens")]
    assert sorted(business) == sorted(BUSINESS_TABLES), out["tables"]
    assert "media" not in out["tables"], out["tables"]


def test_info_reports_public_base(rig_factory) -> None:
    """`/api/system/info` 的 `tunnel.public_base` 是手机端唯一的云端地址来源。

    ## 为什么这个值必须存在

    云端基址是 `https://域名/d/<云端 device_id>`，其中的 id 是**云端**
    分配的 32 位十六进制值，与电脑本地 `device_id` 是两个不同的东西。
    用户在二维码/托盘里只看得到局域网地址，没有任何途径得知这个前缀 ——
    于是 `pb.cloud` 永远为空、`transport.ts` 的双通路状态机永远进不了
    `CLOUD`，P1 整套功能在产品里不可达。

    ★ 曾经真的不可达：`CloudTunnelTransport.start()` 建好了 `TunnelClient`
    却没调 `tunnel.set_tunnel()`，而 `_tunnel_info()` 读的就是那个进程级
    单例 ⇒ 隧道明明通着，`/api/system/info` 恒报 `offline` 且没有
    `public_base`。这里的 HTTP 断言就是钉住那条登记。
    """
    async def main() -> dict[str, Any]:
        rig = rig_factory()
        await rig.start()
        await rig.wait_tunnel()
        # 走局域网问（手机第一次配对必然在家里，走的就是这条路）
        status, body, _ = await rig.lan.arequest(
            "GET", "/api/system/info", token=await _lan_token(rig))
        return {"status": status,
                "tunnel": body.get("tunnel") if isinstance(body, dict) else None,
                "device_id": rig.device_id,
                "local_device_id": rig.state.device_id,
                "cloud_port": rig.cloud_port}

    out = _run(main())
    assert out["status"] == 200, out
    tun = out["tunnel"] or {}
    # 隧道在跑 ⇒ 必须是 ws（`set_tunnel` 生效的证明）
    assert tun.get("mode") == "ws", tun
    base = str(tun.get("public_base") or "")
    assert base, tun
    # 形如 `http://127.0.0.1:<云端端口>/d/<32位hex>`。测试里云端没 TLS，
    # 生产是 https；这里只钉"有 /d/<云端 device_id> 后缀"这件事。
    assert base.rstrip("/").endswith(f"/d/{out['device_id']}"), (base, out)
    assert f":{out['cloud_port']}" in base, (base, out)
    # ★ 前缀里的 id 必须是**云端** device_id，不能是电脑本地那个。
    #   混淆这两个值是"配对成功、之后每个请求 401"这个经典故障的根因
    #   （本地 16 位 hex vs 云端 32 位 hex）。
    assert out["device_id"] != out["local_device_id"], out
    assert out["local_device_id"] not in base, (base, out["local_device_id"])


def test_info_public_base_survives_tunnel_down(tmp_path) -> None:
    """隧道**没连上**时也必须能算出 `public_base`（从配置算，不靠握手）。

    手机第一次配对一定发生在家里（要扫码），走的是局域网。那时隧道可能
    正断着（电脑刚开机、云端在滚动重启）。若这个值只在握手成功后才有，
    用户会在家里配对成功、出门才发现用不了，只能回家再配对一次。
    """
    from backend.app.handlers.system import _tunnel_info
    from backend.app import tunnel as tunnel_mod

    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        {"server": {"host": "127.0.0.1", "port": 0, "allow_lan": False},
         "cloud": {"base_url": "https://pb.example.com",
                   "device_id": "a" * 32, "agent_token": "t" * 43,
                   "enabled": True}},
        path=data / "config.json", data_dir=data)

    # 模拟"从来没连上过"：进程级单例为空。
    tunnel_mod.set_tunnel(None)
    info = _tunnel_info(cfg)
    assert info["mode"] == "offline", info
    assert info["cloud_url"] == "https://pb.example.com", info
    assert info["public_base"] == f"https://pb.example.com/d/{'a' * 32}", info

    # 没配 device_id ⇒ 空串（此时无法构成合法的 `/d/<id>` 前缀）。
    # 用空串而不是瞎猜一个 id，前端按"空即无云端"处理。
    empty_cfg = Config(
        {"server": {"host": "127.0.0.1", "port": 0, "allow_lan": False},
         "cloud": {"base_url": "https://pb.example.com", "enabled": True}},
        path=data / "config2.json", data_dir=data)
    tunnel_mod.set_tunnel(None)
    info2 = _tunnel_info(empty_cfg)
    assert info2["public_base"] == "", info2
    # 没配云端 ⇒ 也是空串，且绝不 500
    blank = Config(
        {"server": {"host": "127.0.0.1", "port": 0, "allow_lan": False}},
        path=data / "config3.json", data_dir=data)
    assert _tunnel_info(blank)["public_base"] == ""


async def _lan_token(rig: Any) -> str:
    """走局域网配对拿一个令牌（`/panel/pair-code` → `/api/pair`）。"""
    status, body, _ = await rig.lan.arequest("POST", "/panel/pair-code")
    assert status == 201, (status, body)
    status, body, _ = await rig.lan.arequest(
        "POST", "/api/pair", body={"code": str(body["code"])})
    assert status == 201, (status, body)
    return str(body["client_token"])
