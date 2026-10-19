"""P1-11：云端隧道 24 小时稳定性 soak（AC-1.9 / DoD 第 9 条）。

## 为什么不能复用 `scripts/soak_test.py`

那个脚本压的是**局域网**直连通路，它采样的指标（RSS / 线程数 /
asyncio 任务数 / 扫描目录句柄）都围绕索引器与缩略图。隧道独有的故障它
一个都看不到：

* `req_id` 泄漏（`Mux.cancel` 忘了 `_abort`，4096 次取消之后整条隧道全
  429 —— 这是 P1 开发期真实出现过的缺陷）；
* `PendingRequest` 泄漏（`_sweep_once` 没清干净）；
* 心跳 / 重连循环堆积（每次重连多留一个 task）；
* 云端 `traffic` 表无限膨胀；
* 隧道反复重建后的会话号漂移。

## 它查什么

每轮采样（默认 60 s）：

1. `GET /api/health` 的 `online_devices` 必须恒为 1（隧道没断）；
2. `GET /api/devices/{id}/status` 的 `tunnel.active_reqs` 必须回到 0
   （**泄漏检测的主指标**）；
3. `session_id` 变化次数（每次变化 = 一次重连，记下来但不判失败 ——
   网络抖动导致重连是正常行为，只有"从不重连"或"疯狂重连"才异常）；
4. 云端进程 RSS（增量，用**后半程均值 vs 前半程均值**判断，不用首尾
   两点：冷启动与 V8/JIT 类的暖机会让开头必然上涨）；
5. 云端 `traffic` 表的行数与 `req_count` 总量（必须单调但不失控）；
6. 本轮所有业务请求的状态码（任何 5xx 都算失败）；
7. 电脑端 `transport.status()["tunnel"]` 的 `attempt`（退避次数）与
   `last_error`（有错就记下来）。

## 它不查什么

不做"人怎么用"的真机体验。它是**持续压测 + 采样**。

## 用法

    # 默认 24 小时，每 60 秒一轮
    .venv\\Scripts\\python.exe scripts\\soak_tunnel.py --hours 24 --report build\\soak_tunnel.json

    # 快速自检（3 分钟，确认脚本本身能跑通）
    .venv\\Scripts\\python.exe scripts\\soak_tunnel.py --hours 0.05 --interval 10

    # 加速轮转（每轮多打几次请求，用来在短时间内逼出 req_id 泄漏）
    .venv\\Scripts\\python.exe scripts\\soak_tunnel.py --hours 0.1 --interval 5 --rounds-requests 8

**中断也能看进度**：带 `--report` 时每 10 轮原子写一次检查点
（`"status": "running"`），跑一半被杀也能看到跑到第几轮。

退出码：0 = 通过；1 = 指标越界；2 = 环境问题（端口被占 / 隧道起不来）。
"""
from __future__ import annotations

import argparse
import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# ── Windows RSS 采样（不依赖 psutil）─────────────────────────────
# ★ GetProcessMemoryInfo 在 psapi.dll，不在 kernel32.dll。放错报
# `AttributeError: function 'GetProcessMemoryInfo' not found`。
class _PMC(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.restype = wt.HANDLE
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.CloseHandle.argtypes = [wt.HANDLE]
_psapi = ctypes.WinDLL("psapi", use_last_error=True)
_psapi.GetProcessMemoryInfo.argtypes = [
    wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]


def rss_mb(pid: int) -> float:
    """进程当前工作集（MB）。取不到返回 -1。"""
    h = _k32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_INFORMATION|VM_READ
    if not h:
        return -1.0
    try:
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(pmc)
        if not _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return -1.0
        return pmc.WorkingSetSize / (1024 * 1024)
    finally:
        _k32.CloseHandle(h)


# ── HTTP（保持简单，不引入依赖）──────────────────────────────────
def http_json(url: str, *, token: str = "", method: str = "GET",
              body: Any = None, timeout: float = 20.0,
              binary: bool = False) -> tuple[int | None, Any]:
    """发一个请求。`binary=True` 时只读状态与字节数，不解析 JSON。

    ★ 必须分开：`/thumb` 回 webp、`/raw` 回 JPEG，硬走 `json.loads` 会抛
    `JSONDecodeError`，被兜底成 `(None, "...")` —— 于是**成功的图片请求
    会被记成失败**。第一版就这么误报了 24 次"5xx/连接失败"。
    """
    data = None
    req = urllib.request.Request(url, method=method)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.data = data
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if binary:
                return resp.status, len(raw)
            return resp.status, json.loads(raw or b"null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except Exception:                                # noqa: BLE001
            return exc.code, None
    except Exception as exc:                             # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


async def ahttp_json(*a, **kw):
    return await asyncio.to_thread(http_json, *a, **kw)


# ── 装配 ────────────────────────────────────────────────────────
class Soak:
    """云端 relay 独立进程 + 电脑端 transport 本进程。"""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.tmp = Path(args.workdir)
        self.cloud_proc = None
        self.transport = None
        self.cdb_path = ""
        self.device_id = ""
        self.agent_token = ""
        self.client_token = ""
        self.account_token = ""
        self.cloud_port = 0
        self.lan_port = 0
        self.samples: list[dict[str, Any]] = []
        self.failures: list[str] = []
        self._stop = False

    # -- 起云端（独立进程，才能单独量它的 RSS）--------------------
    async def start_cloud(self) -> None:
        import subprocess

        self.tmp.mkdir(parents=True, exist_ok=True)
        (self.tmp / "cloud").mkdir(exist_ok=True)
        dbp = self.tmp / "cloud" / "cloud.db"
        if dbp.exists():
            dbp.unlink()
        self.cdb_path = str(dbp)

        # 造账号 + 设备（直接写库，不走 HTTP —— 生产由 cloud_init.py 做）
        from cloud.relay import db as cloud_db
        from cloud.relay import security
        from cloud.relay.auth import create_account

        cdb = cloud_db.CloudDB(self.cdb_path)
        cdb.init()
        acc = create_account(cdb, "soak@example.com", "password123")
        self.device_id = security.new_device_id()
        self.agent_token = security.new_token()
        cdb.execute(
            "INSERT INTO devices(id, account_id, name, agent_token, version,"
            " last_lan, created_at, revoked)"
            " VALUES (?,?,?,?,?,'[]',?,0)",
            (self.device_id, acc["id"], "soak-pc",
             security.hash_token(self.agent_token), "0.1.0", time.time()))
        cdb.close_all()

        # 云端用固定端口（重连要靠同一个地址）
        self.cloud_port = _free_port()
        env = dict(os.environ)
        env["PB_CLOUD_DB"] = self.cdb_path
        env["PB_CLOUD_HOST"] = "127.0.0.1"
        env["PB_CLOUD_PORT"] = str(self.cloud_port)
        env["PB_CLOUD_LOG_LEVEL"] = "WARNING"
        env["PYTHONPATH"] = str(REPO)
        log = open(self.tmp / "cloud.log", "a", encoding="utf-8")  # noqa: SIM115
        self.cloud_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "cloud.relay.main:create_app",
             "--factory", "--host", "127.0.0.1", "--port", str(self.cloud_port),
             "--log-level", "warning", "--ws", "websockets"],
            cwd=str(REPO), env=env, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            st, body = await ahttp_json(
                f"http://127.0.0.1:{self.cloud_port}/api/health")
            if st == 200:
                return
            if self.cloud_proc.poll() is not None:
                raise RuntimeError("云端进程启动即退出，见 cloud.log")
            await asyncio.sleep(0.2)
        raise RuntimeError("云端 30 秒内未就绪")

    # -- 起电脑端 ---------------------------------------------------
    async def start_agent(self) -> None:
        from backend.app.config import Config
        from backend.app.db import make_db
        from backend.app.server import build_app, build_state
        from backend.app.transports.cloud_tunnel import CloudTunnelTransport

        lib = self.tmp / "lib"
        _make_photos(lib, self.args.photos)
        data = self.tmp / "data"
        cfg = Config(
            {
                "server": {"host": "127.0.0.1", "port": 0, "allow_lan": False,
                           "panel_http": True},
                "roots": [{"id": "library", "path": str(lib), "label": "库",
                           "mode": "readwrite", "enabled": True,
                           "recursive": True}],
                "auth": {"require_pairing": True, "allow_delete": True},
                "index": {"scan_on_start": False, "watch": False},
                "cloud": {"base_url": f"http://127.0.0.1:{self.cloud_port}",
                          "device_id": self.device_id,
                          "agent_token": self.agent_token, "enabled": True},
            },
            path=data / "config.json", data_dir=data)
        db = make_db(cfg)
        db.init()
        st = build_state(cfg, db=db)
        router, _t, st = build_app(cfg, state=st)
        await st.indexer.scan_all()
        self.transport = CloudTunnelTransport(cfg, router)
        await self.transport.start()
        self.lan_port = int(self.transport.status().get("port") or 0)

    async def wait_tunnel(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            t = self.transport.status()["tunnel"]
            if t.get("connected"):
                return
            await asyncio.sleep(0.1)
        raise RuntimeError(f"隧道 {timeout}s 内未建立：{self.transport.status()}")

    # -- 配对（走云端，拿一个能过隧道的令牌）------------------------
    async def pair(self) -> None:
        base = f"http://127.0.0.1:{self.cloud_port}"
        st, body = await ahttp_json(
            f"{base}/api/devices/{self.device_id}/paircode",
            token=self.agent_token, method="POST", body={})
        if st != 201:
            raise RuntimeError(f"领配对码失败 {st}: {body}")
        code = body["code"]
        st, body = await ahttp_json(
            f"{base}/api/auth/login", method="POST",
            body={"email": "soak@example.com", "password": "password123"})
        if st != 200:
            raise RuntimeError(f"登录失败 {st}: {body}")
        self.account_token = body["account_token"]
        st, body = await ahttp_json(
            f"{base}/api/devices/{self.device_id}/pair",
            token=self.account_token, method="POST",
            body={"code": code, "client_name": "soak-phone"})
        if st != 200:
            raise RuntimeError(f"兑换令牌失败 {st}: {body}")
        self.client_token = body["client_token"]

    # -- 采样助手 ---------------------------------------------------
    async def _tunnel_status(self) -> dict[str, Any]:
        st, body = await ahttp_json(
            f"http://127.0.0.1:{self.cloud_port}"
            f"/api/devices/{self.device_id}/status",
            token=self.account_token)
        if st != 200 or not isinstance(body, dict):
            return {}
        tun = body.get("tunnel")
        return tun if isinstance(tun, dict) else {}

    async def _active_reqs(self) -> int:
        tun = await self._tunnel_status()
        raw = tun.get("active_reqs")
        return int(raw) if isinstance(raw, int) else 0

    # -- 一轮压测 ---------------------------------------------------
    async def one_round(self, idx: int) -> dict[str, Any]:
        """一轮 = 并发打 N 组请求，**在途时**采一次 `active_reqs`。

        ★ 必须并发并在途采样。第一版是串行的，采到的 `active_reqs`
        恒为 0 —— 而 0 正好是"req_id 没泄漏"的期望值，于是这个指标
        **永远通过**，等于没测。隧道泄漏的表现是"取消/超时之后
        `_pending` 里留下孤儿 req_id"，只有在请求仍在飞的时候才看得到
        峰值，结束后才归零。
        """
        base = f"http://127.0.0.1:{self.cloud_port}"
        dev = self.device_id
        codes: list[int | None] = []
        errors: list[str] = []
        peak = 0

        async def one_group() -> None:
            st, body = await ahttp_json(f"{base}/d/{dev}/api/system/ping")
            codes.append(st)
            if st != 200:
                errors.append(f"ping -> {st}: {str(body)[:120]}")
            st, body = await ahttp_json(f"{base}/d/{dev}/api/timeline?limit=2",
                                        token=self.client_token)
            codes.append(st)
            if st != 200:
                errors.append(f"timeline -> {st}: {str(body)[:120]}")
            items: list[Any] = []
            if isinstance(body, dict):
                raw_items = body.get("items")
                if isinstance(raw_items, list):
                    items = raw_items
            if not items:
                return
            mid = items[0]["id"]
            st, nbytes = await ahttp_json(
                f"{base}/d/{dev}/api/items/{mid}/thumb?size=320",
                token=self.client_token, binary=True)
            codes.append(st)
            if st != 200:
                errors.append(f"thumb -> {st}: {str(nbytes)[:120]}")
            st, nbytes = await ahttp_json(
                f"{base}/d/{dev}/api/items/{mid}/raw",
                token=self.client_token, binary=True)
            codes.append(st)
            if st != 200:
                errors.append(f"raw -> {st}: {str(nbytes)[:120]}")

        groups = max(1, self.args.rounds_requests)
        tasks = [asyncio.create_task(one_group()) for _ in range(groups)]
        # 边等边采在途数（不空转：0.05 s 一次，足够抓几十毫秒级的请求）
        while not all(t.done() for t in tasks):
            cur = await self._active_reqs()
            peak = max(peak, cur)
            await asyncio.sleep(0.05)
        await asyncio.gather(*tasks, return_exceptions=True)
        peak = max(peak, await self._active_reqs())

        bad = [c for c in codes if c is None or c >= 500]
        if bad:
            detail = errors[0] if errors else ""
            self.failures.append(
                f"round {idx}: 5xx/连接失败 {bad[:5]}；首个错误={detail[:200]}")

        health = await ahttp_json(f"{base}/api/health")
        tun = await self._tunnel_status()
        local = self.transport.status()["tunnel"]

        sample = {
            "t": time.time(),
            "round": idx,
            "reqs": len(codes),
            "bad": len(bad),
            "online": (health[1] or {}).get("online_devices")
            if health[0] == 200 else None,
            "active_reqs": tun.get("active_reqs"),
            "active_reqs_peak": peak,
            "session_id": tun.get("session_id"),
            "cloud_rss_mb": round(rss_mb(self.cloud_proc.pid), 2),
            "attempt": local.get("attempt"),
            "local_connected": local.get("connected"),
            "last_error": local.get("last_error"),
        }
        return sample

    # -- 报告 -------------------------------------------------------
    def report(self) -> dict[str, Any]:
        s = self.samples
        if not s:
            return {"status": "empty"}
        half = len(s) // 2
        rss_all = [x["cloud_rss_mb"] for x in s if x["cloud_rss_mb"] >= 0]
        rss_head = [x["cloud_rss_mb"] for x in s[:half] if x["cloud_rss_mb"] >= 0]
        rss_tail = [x["cloud_rss_mb"] for x in s[half:] if x["cloud_rss_mb"] >= 0]
        sessions = {x["session_id"] for x in s if x["session_id"]}
        active = [x["active_reqs"] for x in s if x["active_reqs"] is not None]
        peaks = [x.get("active_reqs_peak") or 0 for x in s]
        return {
            "rounds": len(s),
            "hours": round((s[-1]["t"] - s[0]["t"]) / 3600.0, 3),
            "reqs_total": sum(x["reqs"] for x in s),
            "bad_total": sum(x["bad"] for x in s),
            "online_all": all(x["online"] == 1 for x in s),
            "active_reqs_max": max(active) if active else None,
            "active_reqs_peak_max": max(peaks) if peaks else None,
            "active_reqs_final": active[-1] if active else None,
            "session_count": len(sessions),
            "cloud_rss_start_mb": rss_all[0] if rss_all else None,
            "cloud_rss_end_mb": rss_all[-1] if rss_all else None,
            "cloud_rss_peak_mb": max(rss_all) if rss_all else None,
            "cloud_rss_head_avg_mb": (round(statistics.fmean(rss_head), 2)
                                      if rss_head else None),
            "cloud_rss_tail_avg_mb": (round(statistics.fmean(rss_tail), 2)
                                      if rss_tail else None),
            "attempts_seen": sorted({x["attempt"] for x in s
                                     if x["attempt"] is not None}),
            "errors": [e for e in {x["last_error"] for x in s
                                   if x["last_error"]}],
            "failures": self.failures[:20],
        }

    def judge(self, rep: dict[str, Any]) -> list[str]:
        """返回问题清单（空 = 通过）。"""
        bad: list[str] = []
        if not rep.get("online_all"):
            bad.append("隧道中途掉过（online_devices 出现过非 1）")
        if rep.get("bad_total"):
            bad.append(f"有 {rep['bad_total']} 次请求 5xx/连接失败")
        if rep.get("active_reqs_final") not in (0, None):
            bad.append(f"active_reqs 收尾为 {rep['active_reqs_final']}，不是 0"
                       "（req_id 泄漏）")
        amax = rep.get("active_reqs_max")
        if amax is not None and amax > 8:
            bad.append(f"active_reqs 峰值 {amax} 异常偏高")
        ppeak = rep.get("active_reqs_peak_max")
        if ppeak is not None and ppeak > 32:
            bad.append(f"在途请求峰值 {ppeak}，超过 32（MAX_ACTIVE=4096 的 1%）")
        # ★ 如果连并发都没被观测到，说明这轮压测没真正并发 —— 报告要
        # 标出来，否则"active_reqs 全 0"会被误读成"没泄漏"。
        if not ppeak:
            bad.append("全程未观测到在途请求（active_reqs 峰值 0）—— "
                       "压力没并发起来，泄漏检测无效")
        head, tail = rep.get("cloud_rss_head_avg_mb"), rep.get("cloud_rss_tail_avg_mb")
        if head and tail and tail - head > 30.0:
            bad.append(f"云端 RSS 后半程均值 {tail} 比前半程 {head} 高 30 MB 以上")
        if rep.get("session_count", 0) > max(3, rep.get("rounds", 1) // 5):
            bad.append(f"重连 {rep['session_count']} 次，过于频繁")
        return bad

    def save(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)

    async def stop(self) -> None:
        self._stop = True
        if self.transport is not None:
            try:
                await self.transport.stop()
            except BaseException:                        # noqa: BLE001
                pass
            self.transport = None
        if self.cloud_proc is not None and self.cloud_proc.poll() is None:
            self.cloud_proc.terminate()
            try:
                await asyncio.to_thread(self.cloud_proc.wait, 15)
            except BaseException:                        # noqa: BLE001
                self.cloud_proc.kill()
            self.cloud_proc = None


# ── 造数 ────────────────────────────────────────────────────────
def _make_photos(lib: Path, count: int) -> None:
    """造 `count` 张真 JPEG（带 EXIF 拍摄时间，索引器靠它填 taken_at）。"""
    if count <= 0:
        return
    from PIL import Image

    for i in range(count):
        d = lib / "2024" / "2024-05"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"p{i:04d}.jpg"
        if p.exists():
            continue
        im = Image.new("RGB", (64, 48), (i % 256, (i * 7) % 256, 90))
        ex = Image.Exif()
        ex[0x8769] = {0x9003: f"2024:05:{1 + i % 28:02d} 10:00:00",
                      0x9004: f"2024:05:{1 + i % 28:02d} 10:00:00"}
        im.save(p, "JPEG", quality=70, exif=ex)


def _free_port() -> int:
    import socket

    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


# ── 主流程 ──────────────────────────────────────────────────────
async def run(args: argparse.Namespace) -> int:
    soak = Soak(args)
    report_path = Path(args.report) if args.report else None
    try:
        await soak.start_cloud()
        await soak.start_agent()
        await soak.wait_tunnel()
        await soak.pair()
        print(f"隧道已建立：cloud={soak.cloud_port} lan={soak.lan_port} "
              f"device={soak.device_id[:8]}…", flush=True)

        deadline = time.monotonic() + args.hours * 3600.0
        idx = 0
        while time.monotonic() < deadline and not soak._stop:
            s = await soak.one_round(idx)
            soak.samples.append(s)
            print(f"[{idx:5d}] online={s['online']} active={s['active_reqs']} "
                  f"rss={s['cloud_rss_mb']:.1f}MB bad={s['bad']} "
                  f"attempt={s['attempt']}", flush=True)
            if report_path is not None and idx % 10 == 0:
                soak.save(report_path, {"status": "running",
                                        "rounds": idx + 1,
                                        "last": s})
            idx += 1
            left = deadline - time.monotonic()
            if left <= 0:
                break
            await asyncio.sleep(min(args.interval, max(0.0, left)))

        rep = soak.report()
        problems = soak.judge(rep)
        rep["problems"] = problems
        rep["pass"] = not problems
        if report_path is not None:
            soak.save(report_path, rep)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if not problems else 1
    except Exception as exc:                             # noqa: BLE001
        print(f"环境问题：{type(exc).__name__}: {exc}", file=sys.stderr)
        rep = soak.report()
        rep["error"] = f"{type(exc).__name__}: {exc}"
        if report_path is not None:
            soak.save(report_path, rep)
        return 2
    finally:
        await soak.stop()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="P1 云端隧道 24 小时稳定性 soak")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--interval", type=float, default=60.0,
                    help="每轮间隔秒数")
    ap.add_argument("--rounds-requests", type=int, default=4,
                    help="每轮打几组请求（每组 4 个）")
    ap.add_argument("--photos", type=int, default=40,
                    help="合成库照片数")
    ap.add_argument("--report", default="", help="报告输出路径（JSON）")
    ap.add_argument("--workdir", default=str(REPO / "build" / "_soak_tunnel"))
    args = ap.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
