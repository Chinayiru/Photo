"""端到端验证：上传传到一半断网 / 断进程 / 断电，**照片还能接着传完且不损坏**。

为什么单测不够
--------------
`backend/tests/test_uploads.py` 直接调 `append_chunk` / `complete_session`，
中间没有真实 HTTP、没有真实 socket 中断、没有真实的"进程被杀掉再起来"。
而"断网恢复"最怕的失败模式恰恰全在层与层之间：

* 客户端重发了一片，服务端重复写入 → **文件变长**，sha256 永远对不上，
  用户反复重传都失败，却没有任何提示指向真正原因
* 网络中断后客户端从服务端报的 offset 续传，而那个 offset 是 DB 里的
  陈旧值（比磁盘大）→ 文件中间留下**空洞**，照片上表现为灰条
* 进程被强杀后重启，DB 里的 offset 与磁盘实际大小不一致 → 同上
* 续传拼出来的文件"能打开"但字节与原件不同 → 用户以为传成功了

所以这里走真实 HTTP + 真实 socket，最后**逐字节比对**归档文件的 sha256，
而不是相信接口的返回值。

## 这一节替代了哪个"必须真机"的验收项

`docs/07-测试计划与用例.md` 里的 L5「断网恢复实测」原本要求"拿手机在
上传中途关 Wi-Fi"。该场景的实质是**分片中断后能否正确续传**，与介质
无关 —— 手机切 Wi-Fi、4G 掉线、代理超时，对服务端都表现为"这一片没传完"
或"这一片客户端重发了"。因此这里用真实 socket 直接制造同样的两种中断，
可在没有手机的机器上重复执行。

## 安全边界

全程在 `tempfile.mkdtemp()` 造的合成照片库里操作，**绝不碰真实照片库**。
脚本结束时会校验真实库指纹与开始时一致（行数 / missing / max id）。
"""
from __future__ import annotations

import hashlib
import http.client
import json
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from _e2e_common import real_db_fingerprint  # noqa: E402

from backend.app.config import Config  # noqa: E402
from backend.app.server import serve  # noqa: E402

failures: list[str] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {extra}" if extra else ""))
    if not ok:
        failures.append(label)


def real_fingerprint() -> str:
    """真实库指纹：行数 / 失效数 / 最大 id（实现见 `_e2e_common`）。"""
    return real_db_fingerprint(REPO / "data" / "photobridge.db")


# ---------------------------------------------------------------------------
# HTTP 辅助
# ---------------------------------------------------------------------------


def req(url: str, token: str | None = None, *, method: str = "GET",
        body: dict | None = None, raw: bool = False, headers: dict | None = None):
    data = None
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    with urllib.request.urlopen(r, timeout=30) as resp:
        payload = resp.read()
        return resp.status, (payload if raw else json.loads(payload.decode("utf-8")))


def req_err(url: str, token: str | None = None, *, method: str = "GET",
            body: dict | None = None, headers: dict | None = None):
    """发一个预期会失败的请求，返回 (status, parsed_body, response_headers)。"""
    data = None
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            payload = resp.read()
            return resp.status, json.loads(payload.decode("utf-8")), dict(resp.headers)
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            parsed = {"_raw": payload[:200].decode("utf-8", "replace")}
        return e.code, parsed, dict(e.headers or {})


def split_host(base: str) -> tuple[str, int]:
    rest = base.split("://", 1)[1]
    host, _, port = rest.partition(":")
    return host, int(port or 80)


def patch_chunk(base: str, token: str, uid: str, start: int, end: int,
                total: int, data: bytes):
    """用真实 socket 发一个分片。"""
    host, port = split_host(base)
    conn = http.client.HTTPConnection(host, port, timeout=30)
    try:
        conn.putrequest("PATCH", f"/api/uploads/{uid}", skip_accept_encoding=True)
        conn.putheader("Authorization", f"Bearer {token}")
        conn.putheader("Content-Range", f"bytes {start}-{end}/{total}")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(len(data)))
        conn.endheaders()
        conn.send(data)
        resp = conn.getresponse()
        payload = resp.read()
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            parsed = {"_raw": payload[:200].decode("utf-8", "replace")}
        return resp.status, parsed, dict(resp.getheaders())
    finally:
        conn.close()


def patch_chunk_then_cut(base: str, token: str, uid: str, start: int, end: int,
                         total: int, data: bytes) -> None:
    """★ 发一半就掐断 socket —— 模拟手机走出 Wi-Fi 覆盖范围。

    声明了完整 Content-Length 但只发一半，然后直接关掉连接。
    服务端会读到不完整的 body，必须**什么都不写**（既不能写半片，
    也不能把 offset 往前推）。
    """
    host, port = split_host(base)
    conn = http.client.HTTPConnection(host, port, timeout=10)
    try:
        conn.putrequest("PATCH", f"/api/uploads/{uid}", skip_accept_encoding=True)
        conn.putheader("Authorization", f"Bearer {token}")
        conn.putheader("Content-Range", f"bytes {start}-{end}/{total}")
        conn.putheader("Content-Type", "application/octet-stream")
        conn.putheader("Content-Length", str(len(data)))
        conn.endheaders()
        conn.send(data[: max(1, len(data) // 3)])
        # 粗暴掐断，不做 TCP 正常关闭 —— 正常关闭会让服务端以为
        # "body 就这么长"，测不出真实的中断场景。
        try:
            conn.sock.close()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        # 连接被我们自己弄断了，任何异常都属预期
        pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 服务启停
# ---------------------------------------------------------------------------


class Service:
    """在后台线程里跑一个真实 HTTP 服务，可停可再起（同一 data 目录）。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state: dict = {}
        self.ready = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self, *, timeout: float = 90.0) -> None:
        self.ready.clear()
        self.state = {}

        async def runner() -> None:
            def on_ready(transport, st, _loop, _stop):
                self.state["transport"] = transport
                self.state["st"] = st
                self.state["base"] = transport.urls[0]
                self.state["stop_event"] = _stop
                self.state["loop"] = _loop
                self.ready.set()

            await serve(self.cfg, on_ready=on_ready)

        def thread_main() -> None:
            import asyncio

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(runner())

        self.thread = threading.Thread(target=thread_main, daemon=True,
                                       name="resume-e2e")
        self.thread.start()
        if not self.ready.wait(timeout):
            raise RuntimeError("服务未在预期时间内就绪")

    @property
    def base(self) -> str:
        return self.state["base"]

    @property
    def st(self):
        return self.state["st"]

    def stop(self, *, timeout: float = 30.0) -> None:
        ev = self.state.get("stop_event")
        lp = self.state.get("loop")
        if ev is not None and lp is not None:
            lp.call_soon_threadsafe(ev.set)
        if self.thread is not None:
            self.thread.join(timeout=timeout)
        self.thread = None

    def kill(self) -> None:
        """★ 不优雅关闭 —— 模拟断电/被任务管理器强杀。

        **必须先真的停掉监听**（审查 R-M7-01）。原来只写 `self.thread = None`
        就返回：旧服务仍在同一个进程里活着、继续 accept 请求，随后 `start()`
        起来的"重启后服务"其实抢不到端口或者请求全打到旧实例上 —— 我们要测的
        "断电后 WAL 里数据还在、新进程能接着传"就完全没被测到，用例是假的绿。

        做法：不 set stop_event（不跑优雅收尾），直接关监听 socket 并让
        `serve()` 退出，然后**等端口真的拒绝连接**再返回。
        """
        lp = self.state.get("loop")
        transport = self.state.get("transport")
        base = self.state.get("base", "")
        if lp is not None and transport is not None:
            lp.call_soon_threadsafe(_hard_kill, transport)
            deadline = time.time() + 15.0
            while time.time() < deadline and _port_open(base):
                time.sleep(0.05)
        if self.thread is not None:
            self.thread.join(timeout=5.0)
        self.thread = None
        self.state = {}


def _hard_kill(transport) -> None:
    """在服务自己的事件循环里执行：关监听、断在途连接、不跑优雅收尾。"""
    srv = getattr(transport, "_server", None)
    if srv is None:                                   # pragma: no cover
        return
    for s in list(getattr(srv, "servers", None) or []):
        try:
            s.close()                                # 关监听 socket
        except Exception:                            # noqa: BLE001
            pass
    srv.should_exit = True                           # 让 serve() 尽快返回


def _port_open(base: str) -> bool:
    """`base` 形如 `http://127.0.0.1:8123`；能连上就说明服务还活着。"""
    try:
        u = urlsplit(base)
    except ValueError:                               # pragma: no cover
        return False
    if not u.hostname or not u.port:
        return False
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect((u.hostname, u.port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def pair(service: Service, name: str) -> str:
    from backend.app import auth as auth_mod

    st = service.st
    code = auth_mod.generate_pair_code()
    auth_mod.store_pair_code(st.db, code, st.device_id, st.device_name, ttl=300.0)
    _, paired = req(service.base + "/api/pair", method="POST",
                    body={"code": code, "name": name})
    return paired["client_token"]


def init_upload(base: str, token: str, name: str, total: int,
                *, sha256: str | None = None) -> dict:
    body: dict = {"name": name, "size": total}
    if sha256:
        body["sha256"] = sha256
    status, resp = req(base + "/api/uploads/init", token, method="POST",
                       body=body)
    if status not in (200, 201):
        raise RuntimeError(f"init 失败：{status} {resp}")
    return resp


def get_offset(base: str, token: str, uid: str) -> dict:
    _, body = req(f"{base}/api/uploads/{uid}", token)
    return body


def send_all(base: str, token: str, uid: str, payload: bytes, total: int,
             chunk: int, *, start: int = 0) -> int:
    """从 start 开始顺序把剩下的分片发完，返回最终 offset。"""
    off = start
    while off < total:
        end = min(off + chunk, total) - 1
        status, body, _ = patch_chunk(base, token, uid, off, end, total,
                                      payload[off:end + 1])
        if status != 206:
            raise RuntimeError(f"分片 {off}-{end} 失败：{status} {body}")
        off = int(body["offset"])
    return off


def make_photo_bytes(size: int, tag: bytes = b"") -> bytes:
    """造一张体积可控、内容随机的"JPEG"。

    内容必须随机：全零字节的文件无法暴露"中间留了空洞"这类缺陷
    （空洞读出来也是零，sha256 一样对得上）。

    `tag` 让每个场景拿到**互不相同**的字节。这一点很关键：如果所有
    场景都用同一份 payload，那么"服务端把 A 会话的分片写进了 B 会话"
    这种串线缺陷会被去重逻辑掩盖（内容一样，去重后指向同一行，
    一切看起来都正常）。
    """
    import os

    head = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + tag.ljust(8, b"\x00")[:8])
    tail = b"\xff\xd9"
    body = os.urandom(max(0, size - len(head) - len(tail)))
    return head + body + tail


# ---------------------------------------------------------------------------


def main() -> int:
    real_before = real_fingerprint()
    print(f"真实库（开始）：{real_before}")

    tmp = Path(tempfile.mkdtemp(prefix="pb_resume_"))
    library = tmp / "lib"
    library.mkdir()
    data_dir = tmp / "data"
    data_dir.mkdir()

    CHUNK_MB = 1
    total = int(3.5 * 1024 * 1024)
    chunk = CHUNK_MB * 1024 * 1024

    def new_payload(tag: str) -> tuple[bytes, str]:
        """每个场景一份独立的内容 —— 否则去重会把串线缺陷掩盖掉。"""
        data = make_photo_bytes(total, tag.encode("ascii"))
        return data, hashlib.sha256(data).hexdigest()

    payload, want_sha = new_payload("ctrl")
    print(f"合成库：{library}")
    print(f"测试照片：{total} 字节，分片 {CHUNK_MB} MiB，每个场景内容互不相同\n")

    cfg = Config({
        "app": {"mode": "readwrite"},
        "server": {"host": "127.0.0.1", "port": 0},
        "roots": [{
            "id": "library", "path": str(library),
            "mode": "readwrite", "enabled": True, "recursive": True,
        }],
        "upload": {"subdir": "手机上传", "chunk_mb_lan": CHUNK_MB,
                   "session_ttl_hours": 24, "dedup": True},
        "auth": {"allow_delete": True, "allow_move": True},
        "index": {"scan_on_start": False, "watch": False, "batch_size": 50},
    }, path=data_dir / "config.json", data_dir=data_dir)
    cfg.save()

    svc = Service(cfg)
    svc.start()
    print(f"服务就绪：{svc.base}\n")
    token = pair(svc, "resume-e2e")
    base = svc.base

    # =====================================================================
    print("[1] 正常传完一遍（对照组）")
    up = init_upload(base, token, "完整.jpg", total)
    check(up["chunk_size"] == chunk, f"服务端分片大小是 {CHUNK_MB} MiB",
          f"got {up.get('chunk_size')}")
    off = send_all(base, token, up["upload_id"], payload, total, chunk)
    check(off == total, "全部字节已收", f"offset={off}")
    status, done = req(f"{base}/api/uploads/{up['upload_id']}/complete", token,
                       method="POST", body={"sha256": want_sha})
    check(status == 201, "complete 返回 201", f"got {status}")
    good_rel = done["rel_path"]
    good_path = library / good_rel
    check(good_path.is_file(), f"归档文件存在：{good_rel}")
    got_sha = hashlib.sha256(good_path.read_bytes()).hexdigest()
    check(got_sha == want_sha, "★ 归档文件 sha256 与原件一致（逐字节）")

    # =====================================================================
    print("\n[2] ★ 传到一半掐断 socket —— 半片不能落盘")
    payload_cut, sha_cut = new_payload("cut")
    up2 = init_upload(base, token, "断网.jpg", total)
    uid2 = up2["upload_id"]
    # 先正常传两片
    o = 0
    for _ in range(2):
        e = min(o + chunk, total) - 1
        s, b, _ = patch_chunk(base, token, uid2, o, e, total, payload_cut[o:e + 1])
        check(s == 206, f"第 {o // chunk + 1} 片正常", f"got {s}")
        o = int(b["offset"])
    check(o == 2 * chunk, "两片已落盘", f"offset={o}")

    # 第三片发一半掐断
    e3 = min(o + chunk, total) - 1
    patch_chunk_then_cut(base, token, uid2, o, e3, total, payload_cut[o:e3 + 1])
    time.sleep(0.8)     # 给服务端时间处理断开
    q = get_offset(base, token, uid2)
    check(q["offset"] == o, "★ 掐断后 offset 没动（半片未被计入）",
          f"期望 {o}，实际 {q['offset']}")
    temp2 = library / ".photobridge_tmp" / f"{uid2}.part"
    actual2 = temp2.stat().st_size if temp2.is_file() else -1
    check(actual2 == o, "★ 磁盘上临时文件大小 == offset（没有写入半片）",
          f"磁盘 {actual2}")

    # =====================================================================
    print("\n[3] ★ 客户端重发同一片 —— 幂等，文件不能变长")
    # 这是"网络恢复后客户端没收到上次响应、重发"的真实场景
    s, b, _ = patch_chunk(base, token, uid2, chunk, 2 * chunk - 1, total,
                          payload_cut[chunk:2 * chunk])
    check(s == 206, "重发第 2 片返回 206", f"got {s}")
    check(b.get("duplicate") is True, "★ 服务端识别为重复分片（duplicate=true）",
          f"got duplicate={b.get('duplicate')}")
    actual3 = temp2.stat().st_size
    check(actual3 == o, "★ 重发后文件长度没变（重复写会让文件变长）",
          f"磁盘 {actual3}，期望 {o}")

    # =====================================================================
    print("\n[4] ★ 跳号分片 —— 416 + X-Upload-Offset，客户端才能对齐")
    status, body, hdrs = patch_chunk(base, token, uid2, 3 * chunk, total - 1,
                                     total, payload_cut[3 * chunk:])
    check(status == 416, "跳号返回 416", f"got {status}")
    got_hdr = hdrs.get("X-Upload-Offset") or hdrs.get("x-upload-offset")
    check(got_hdr is not None and int(got_hdr) == o,
          "★ 响应头 X-Upload-Offset 给出真实 offset（前端靠它续传）",
          f"got {got_hdr!r}")
    actual4 = temp2.stat().st_size
    check(actual4 == o, "跳号分片没有落盘", f"磁盘 {actual4}")

    # =====================================================================
    print("\n[5] 从 offset 续传剩下的，并校验最终文件")
    off = send_all(base, token, uid2, payload_cut, total, chunk, start=o)
    check(off == total, "续传后收满", f"offset={off}")
    status, done2 = req(f"{base}/api/uploads/{uid2}/complete", token,
                        method="POST", body={"sha256": sha_cut})
    check(status == 201, "complete 返回 201", f"got {status}")
    p2 = library / done2["rel_path"]
    sha2 = hashlib.sha256(p2.read_bytes()).hexdigest()
    check(sha2 == sha_cut, "★★ 中断+重发+续传后的文件与原件逐字节一致")
    check(p2.stat().st_size == total, "文件大小正确", f"{p2.stat().st_size}")

    # =====================================================================
    print("\n[6] ★ DB 里的 offset 比磁盘大 —— 必须以磁盘为准（否则留空洞）")
    payload_db, sha_db = new_payload("dirty")
    up3 = init_upload(base, token, "脏DB.jpg", total)
    uid3 = up3["upload_id"]
    e = chunk - 1
    s, b, _ = patch_chunk(base, token, uid3, 0, e, total, payload_db[:chunk])
    check(s == 206, "第一片已落盘")
    disk3 = library / ".photobridge_tmp" / f"{uid3}.part"
    real3 = disk3.stat().st_size
    # 模拟"写完文件、更新 DB 之前进程被杀"的反面：DB 被改大了
    svc.st.db.execute("UPDATE upload_sessions SET received = ? WHERE id = ?",
                      (total, uid3))
    q3 = get_offset(base, token, uid3)
    check(q3["offset"] == real3,
          "★ GET 报告的 offset 是磁盘真实大小，不是 DB 里的值",
          f"期望 {real3}，实际 {q3['offset']}")
    # 客户端按这个 offset 继续传，最终文件必须完整
    send_all(base, token, uid3, payload_db, total, chunk, start=real3)
    status, done3 = req(f"{base}/api/uploads/{uid3}/complete", token,
                        method="POST", body={"sha256": sha_db})
    check(status == 201, "complete 返回 201", f"got {status}")
    p3 = library / done3["rel_path"]
    check(hashlib.sha256(p3.read_bytes()).hexdigest() == sha_db,
          "★★ 以磁盘为准续传后的文件逐字节一致（没有空洞）")

    # =====================================================================
    print("\n[7] ★ 服务进程被强杀后重启 —— 会话与 offset 必须还在")
    payload_kill, sha_kill = new_payload("kill")
    up4 = init_upload(base, token, "断电.jpg", total)
    uid4 = up4["upload_id"]
    o4 = 0
    for _ in range(2):
        e4 = min(o4 + chunk, total) - 1
        s, b, _ = patch_chunk(base, token, uid4, o4, e4, total,
                              payload_kill[o4:e4 + 1])
        check(s == 206, f"第 {o4 // chunk + 1} 片已落盘", f"got {s}")
        o4 = int(b["offset"])

    svc.kill()          # ← 不优雅关闭，模拟断电
    time.sleep(1.0)

    # ★ 用一条**独立的 sqlite3 连接**直接读数据库文件，绕过服务进程。
    # 只调 GET 接口证明不了持久化 —— 服务可能把 offset 放在内存里，
    # 而真实断电时内存就没了。这里要证明的是"已经落到 DB 文件里"。
    con = sqlite3.connect(str(cfg.db_path))
    try:
        row = con.execute(
            "SELECT received, status FROM upload_sessions WHERE id = ?", (uid4,)
        ).fetchone()
    finally:
        con.close()
    check(row is not None, "★ 独立连接能读到这条会话（已落盘）")
    if row is not None:
        check(int(row[0]) == o4,
              "★ 独立连接读到的 received 与磁盘一致（不是内存态）",
              f"期望 {o4}，实际 {row[0]}")

    svc2 = Service(cfg)
    svc2.start()
    base2 = svc2.base
    print(f"  重启后：{base2}")
    # 会话 token 存在库里，重启后同一个 token 必须仍然有效
    q4 = get_offset(base2, token, uid4)
    check(q4["status"] == "open", "★ 重启后上传会话仍是 open",
          f"got {q4['status']}")
    check(q4["offset"] == o4, "★ 重启后 offset 从磁盘恢复正确",
          f"期望 {o4}，实际 {q4['offset']}")
    off4 = send_all(base2, token, uid4, payload_kill, total, chunk, start=o4)
    check(off4 == total, "重启后继续传完", f"offset={off4}")
    status, done4 = req(f"{base2}/api/uploads/{uid4}/complete", token,
                        method="POST", body={"sha256": sha_kill})
    check(status == 201, "complete 返回 201", f"got {status}")
    p4 = library / done4["rel_path"]
    check(hashlib.sha256(p4.read_bytes()).hexdigest() == sha_kill,
          "★★ 跨进程重启续传后的文件逐字节一致")

    # =====================================================================
    print("\n[8] ★ 重启后同一个 client_token 仍有效（配对状态不丢）")
    status, me = req(base2 + "/api/clients/me", token)
    check(status == 200, "重启后 /api/clients/me 返回 200", f"got {status}")
    check(bool(me.get("device_id")), "能读到所属电脑", f"got {me.get('device_id')}")

    # =====================================================================
    print("\n[9] 临时区已清空（续传成功的会话不留 .part）")
    leftover = [p.name for p in (library / ".photobridge_tmp").glob("*.part")]
    check(not leftover, "没有残留的 .part 文件", f"got {leftover}")

    # =====================================================================
    print("\n[10] 时间线上能看到全部 4 张（4 个场景内容互不相同）")
    # ⚠️ 这里**不能**用同一份 payload 传 4 次然后期望 4 行 —— 服务端会
    # 正确地按 sha256 去重，4 行合成 1 行。上面每个场景都用了各自的
    # tag，所以这里期望 4 张不同的照片。
    deadline = time.time() + 30
    items: list = []
    while time.time() < deadline:
        _, page = req(base2 + "/api/timeline?limit=50", token)
        items = page.get("items", [])
        if len(items) >= 4:
            break
        time.sleep(0.4)
    check(len(items) == 4, f"时间线正好 {len(items)} 张（4 个场景各一张）")
    sizes = {it["size"] for it in items}
    check(sizes == {total}, f"每张都是 {total} 字节", f"got {sizes}")

    # ---- 秒传 / 去重：同一份字节再传一次，不能落第二份 ----
    print("\n[11] ★ 同一份字节再传一次 —— 秒传或去重，不能落第二份")
    up5 = init_upload(base2, token, "重复.jpg", total, sha256=want_sha)
    if up5.get("exists"):
        check(True, "★ init 阶段即命中秒传（exists=true，一个字节都不用传）")
        check(up5.get("media_id") is not None, "秒传给出已有 media_id")
    else:
        send_all(base2, token, up5["upload_id"], payload, total, chunk)
        status, done5 = req(f"{base2}/api/uploads/{up5['upload_id']}/complete",
                            token, method="POST", body={"sha256": want_sha})
        # complete 的 200/201 不是判据：新建会话走的也是 201，
        # 去重要看 **dedup 字段**（docs/04 §4.3.4、frontend types.ts:406）。
        check(done5.get("dedup") is True, "★ dedup=true，没有落第二份文件",
              f"got {done5.get('dedup')} status={status}")

    _, page2 = req(base2 + "/api/timeline?limit=50", token)
    check(len(page2.get("items", [])) == 4,
          "★ 重复上传后时间线仍是 4 张（没多出第 5 张）",
          f"got {len(page2.get('items', []))}")

    # =====================================================================
    print("\n[12] 库里没有多余的归档文件（去重后不留副本）")
    placed = sorted(p.name for p in library.rglob("*.jpg"))
    check(len(placed) == 4, f"磁盘上正好 4 个 .jpg", f"got {placed}")

    svc2.stop()
    print("\n服务已优雅退出")

    real_after = real_fingerprint()
    print(f"真实库（结束）：{real_after}")
    if real_before != real_after:
        check(False, "★ 真实库指纹未变", f"{real_before} -> {real_after}")
    else:
        check(True, "★ 真实库指纹未变")

    print()
    if failures:
        print(f"=== {len(failures)} FAILED ===")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("=== ALL PASS ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
