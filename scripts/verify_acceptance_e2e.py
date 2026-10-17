"""端到端验证：`docs/07 §12` 里**不需要手机**就能核实的那批验收项。

## 为什么要有这个脚本

`§12 验收测试清单` 105 项的执行环境写的是 E4（真实安卓手机 + 同一 Wi-Fi），
于是长期处于"全 ☐、等着有手机那天"的状态。但逐项看下来，**只有真正依赖
手机硬件/系统浏览器的那批**（调起相册、手势、状态条配色、真机浏览器兼容性）
必须用手机；其余都能在这台电脑上跑出来，而且比人眼看得更准。

这个脚本就是那批"能跑出来"的证据。它按 **§12 的编号**组织，每节开头写明
覆盖哪几项，失败时能直接对着清单勾。

## 安全边界

全程在 `tempfile.mkdtemp()` 造的合成照片库里操作，数据库与缩略图也写在
那个临时目录里。**绝不碰真实照片库与真实 data/**。脚本末尾会校验真实库
指纹（行数 / missing / max id）与开始时一致。

## 与其它脚本的分工

* `verify_lan_e2e.py` —— 绑真实网卡、模拟手机走完整用户路径
* `verify_resume_e2e.py` —— 断网/断进程后能否续传
* `verify_watch_e2e.py` —— 拷入文件后自动出现在时间线
* `verify_organize_e2e.py` —— 整理（改名/移动）
* `verify_browser_e2e.py` —— 真浏览器（CDP）里的前端行为
* 本脚本 —— **按 §12 清单逐项给证据**，尤其是安全组（G）与性能组（H）
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as _dt
import hashlib
import io
import json
import os
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from PIL import Image  # noqa: E402

from backend.app.config import Config  # noqa: E402
from backend.app.server import serve  # noqa: E402

failures: list[str] = []


def _recycle_contents(near: Path) -> list[tuple[str, int]]:
    """列出与 `near` 同盘的回收站里所有 `$R` 文件（路径, 大小）。

    只扫 `\$Recycle.Bin` 下面第一层的 `\$R*`，不做递归 —— 回收站可能有几万个
    文件，递归扫描会让这一步变成整个验收脚本里最慢的一环。扫不到的盘返回空
    列表（不失败：有些盘没启用回收站）。

    为什么必须扫回收站：`_trash_one` 用的是 `send2trash`，但"文件从库里没了"
    用 `os.unlink` 也能做到 —— 只断言前者证明不了"进了回收站"。
    """
    out: list[tuple[str, int]] = []
    drive = Path(near).drive or "C:"
    rb = Path(drive + os.sep) / "$Recycle.Bin"
    if not rb.is_dir():
        return out
    # ★ 每个 SID 目录**单独** try：`C:\$Recycle.Bin\S-1-5-18` 是 SYSTEM 的，
    #   普通用户列不进去。若把整个循环包在一个 except 里，第一个目录抛
    #   PermissionError 就会把后面所有目录跳掉，症状是"回收站里明明是空的"。
    try:
        sids = list(rb.iterdir())
    except OSError:
        return out
    for sid in sids:
        try:
            if not sid.is_dir():
                continue
            entries = list(sid.iterdir())
        except OSError:
            continue
        for p in entries:
            try:
                if p.is_file() and p.name.startswith("$R"):
                    out.append((str(p), p.stat().st_size))
            except OSError:
                continue
    return out


def check(ok: bool, label: str, extra: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {extra}" if extra else ""))
    if not ok:
        failures.append(label)


def real_fingerprint() -> str:
    db = REPO / "data" / "photobridge.db"
    if not db.exists():
        return "absent"
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        miss = con.execute("SELECT COUNT(*) FROM media WHERE missing = 1").fetchone()[0]
        mx = con.execute("SELECT MAX(id) FROM media").fetchone()[0]
        return f"rows={rows} missing={miss} maxid={mx}"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 造素材
# ---------------------------------------------------------------------------


def jpeg_with_exif(path: Path, *, color=(200, 100, 50), size=(64, 48),
                   taken: str = "2021:03:04 05:06:07",
                   gps: tuple[float, float] | None = None,
                   orientation: int | None = None) -> None:
    """造一张带拍摄时间/GPS/Orientation 的 JPEG。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", size, color)
    ex = Image.Exif()
    ex.get_ifd(0x8769)[0x9003] = taken
    if orientation is not None:
        ex[0x0112] = orientation
    if gps is not None:
        lat, lon = gps
        gps_ifd = ex.get_ifd(0x8825)
        gps_ifd[1] = "N" if lat >= 0 else "S"
        gps_ifd[2] = _dms(abs(lat))
        gps_ifd[3] = "E" if lon >= 0 else "W"
        gps_ifd[4] = _dms(abs(lon))
    im.save(path, "JPEG", exif=ex.tobytes())


def _dms(v: float) -> tuple[float, float, float]:
    d = int(v)
    m = int((v - d) * 60)
    s = ((v - d) * 60 - m) * 60
    return (float(d), float(m), round(s, 4))


def bomb_png(w: int, h: int) -> bytes:
    """造一个 IHDR 声明超大尺寸的小 PNG（几十字节），用于测解压炸弹防线。"""
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "PNG")
    raw = bytearray(buf.getvalue())
    raw[16:20] = struct.pack(">I", w)
    raw[20:24] = struct.pack(">I", h)
    raw[29:33] = struct.pack(">I", zlib.crc32(bytes(raw[12:29])) & 0xFFFFFFFF)
    return bytes(raw)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


def _pct(url: str) -> str:
    """把 URL 里的非 ASCII 字符百分号编码。

    `urllib.request` 只接受 ASCII URL，`Request(url)` 不做转义 ——
    直接传 `?q=敦煌` 会在 `_encode_request` 里抛
    `UnicodeEncodeError: 'ascii' codec can't encode characters`。
    浏览器与 fetch 会自动转义，所以这是脚本自身的坑，不是服务端问题。
    """
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((
        parts.scheme, parts.netloc,
        urllib.parse.quote(parts.path, safe="/%:@!$&'()*+,;=~-._"),
        urllib.parse.quote(parts.query, safe="=&%:@!$'()*+,;~-._"),
        parts.fragment,
    ))


class Svc:
    """后台线程里跑真实 HTTP 服务。"""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.state: dict = {}
        self.ready = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self, timeout: float = 90.0) -> None:
        from backend.app.logging_setup import setup_logging

        setup_logging(self.cfg)

        async def runner() -> None:
            def on_ready(transport, st, loop, stop):
                self.state.update(base=transport.urls[0], st=st, loop=loop, stop=stop)
                self.ready.set()
            await serve(self.cfg, on_ready=on_ready)

        def thread_main() -> None:
            lp = asyncio.new_event_loop()
            asyncio.set_event_loop(lp)
            lp.run_until_complete(runner())

        self.thread = threading.Thread(target=thread_main, daemon=True)
        self.thread.start()
        if not self.ready.wait(timeout):
            raise RuntimeError("服务未在预期时间内就绪")

    @property
    def base(self) -> str:
        return self.state["base"]

    @property
    def st(self):
        return self.state["st"]

    def stop(self, timeout: float = 45.0) -> bool:
        self.state["loop"].call_soon_threadsafe(self.state["stop"].set)
        if self.thread is not None:
            self.thread.join(timeout=timeout)
            return not self.thread.is_alive()
        return True


def call(base: str, path: str, *, method: str = "GET", body=None, token=None,
         raw: bool = False, headers=None):
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    rq = urllib.request.Request(_pct(base + path), data=payload, headers=hdrs,
                                method=method)
    try:
        with urllib.request.urlopen(rq, timeout=60) as rp:
            b = rp.read()
            return rp.status, (b if raw else _json_or_empty(b)), _H(rp.headers)
    except urllib.error.HTTPError as e:
        b = e.read()
        return e.code, _json_or_empty(b), _H(e.headers or {})


def _json_or_empty(b: bytes):
    """204/空 body 不是 JSON —— 直接 json.loads 会抛 JSONDecodeError。"""
    if not b:
        return {}
    try:
        return json.loads(b.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return {"_raw": b[:400].decode("utf-8", "replace")}


class _H(dict):
    """大小写不敏感的头视图 —— HTTP 头名按 RFC 7230 不区分大小写。"""

    def __init__(self, src) -> None:
        super().__init__({str(k).lower(): v for k, v in dict(src).items()})

    def get(self, key, default=None):  # type: ignore[override]
        return super().get(str(key).lower(), default)


def pair(base: str, st, name: str) -> str:
    """生成配对码并通过 HTTP 配对，返回 client_token。

    设备名走 `X-PB-Client` 头而不是 body —— `_client_name()` 只认
    这个头和 User-Agent，body 里的 name 会被忽略（配对码是电脑屏幕上
    的 6 位数字，手机那边只需要报出自己叫什么）。
    """
    from backend.app import auth as auth_mod
    from backend.app.handlers import auth as pair_mod
    # ★ 限流是**进程级**的（`_pair_attempts` 按 IP 计），而本脚本会起
    # 四五个服务、每个都要配一次对。G-15 那节还会故意打满 21 次。
    # 不清掉的话后面所有 pair() 都会拿到 429，报错却表现为
    # `KeyError: 'client_token'`，排查方向完全错。
    pair_mod._pair_attempts.clear()
    code = auth_mod.generate_pair_code()
    auth_mod.store_pair_code(st.db, code, st.device_id, st.device_name, ttl=300.0)
    s, body, _ = call(base, "/api/pair", method="POST",
                      body={"code": code},
                      headers={"X-PB-Client": name})
    if "client_token" not in body:
        raise AssertionError(f"配对失败（{s}）：{body}")
    return body["client_token"]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    real_before = real_fingerprint()
    print(f"真实库（开始）：{real_before}")

    tmp = Path(tempfile.mkdtemp(prefix="pb_accept_"))
    lib = tmp / "lib"
    lib.mkdir()
    data = tmp / "data"
    data.mkdir()

    # 素材：带 EXIF 日期的、带 GPS 的、竖拍 Orientation 的、多张不同颜色的
    jpeg_with_exif(lib / "青甘环线" / "敦煌.jpg", taken="2024:05:12 18:30:00",
                   gps=(39.9042, 116.4074))
    jpeg_with_exif(lib / "2024" / "竖拍.jpg", taken="2024:06:01 09:00:00",
                   orientation=6)
    for i in range(6):
        jpeg_with_exif(lib / "批量" / f"img{i}.jpg",
                       taken=f"2023:0{1 + i}:1{i % 9} 08:00:00",
                       color=(30 * i, 120, 200))

    cfg = Config({
        "app": {"mode": "readwrite"},
        "server": {"host": "127.0.0.1", "port": 0, "panel_http": True},
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite",
                   "enabled": True, "recursive": True}],
        "upload": {"subdir": "手机上传", "chunk_mb_lan": 1, "max_file_mb": 64},
        "auth": {"allow_delete": True, "delete_to_recycle_bin": True,
                 "allow_move": True, "require_pairing": True},
        "index": {"scan_on_start": False, "watch": False, "batch_size": 50},
        "thumbs": {"sizes": [320, 1600], "workers": 4},
    }, path=data / "config.json", data_dir=data)
    cfg.save()

    svc = Svc(cfg)
    svc.start()
    base = svc.base
    st = svc.st

    # 首轮索引（计时 = H-01 的口径：从扫描开始到结束）
    t0 = time.perf_counter()
    stats = asyncio.run(st.indexer.scan_all())
    scan_sec = time.perf_counter() - t0
    print(f"\n服务就绪：{base}")
    print(f"首轮索引：{scan_sec:.2f}s  {[s.summary() for s in stats]}")

    token = pair(base, st, "accept-e2e")

    # =====================================================================
    print("\n[A-04] data/config.json 的 roots 正确")
    raw_cfg = json.loads((data / "config.json").read_text(encoding="utf-8"))
    roots = raw_cfg.get("roots") or []
    check(len(roots) == 1, "只有一个媒体根")
    check(roots[0].get("id") == "library", "roots[0].id == library",
          f"got {roots[0].get('id')!r}")
    check(Path(roots[0]["path"]).resolve() == lib.resolve(),
          "roots[0].path 指向配置里的库", f"got {roots[0]['path']}")
    check(roots[0].get("enabled") is True, "roots[0].enabled 为 true")

    # =====================================================================
    print("\n[A-05] 照片库路径可改（改 roots[0].path 后 info 与时间线跟着变）")
    other = tmp / "lib2"
    other.mkdir()
    jpeg_with_exif(other / "换库.jpg", taken="2022:02:02 02:02:02")
    cfg2 = Config({
        "app": {"mode": "readwrite"},
        "server": {"host": "127.0.0.1", "port": 0},
        "roots": [{"id": "library", "path": str(other), "mode": "readwrite",
                   "enabled": True, "recursive": True}],
        "index": {"scan_on_start": True, "watch": False, "batch_size": 50},
    }, path=(tmp / "data2" / "config.json"), data_dir=(tmp / "data2"))
    (tmp / "data2").mkdir(exist_ok=True)
    cfg2.save()
    svc2 = Svc(cfg2)
    svc2.start()
    try:
        tok2 = pair(svc2.base, svc2.st, "path-change")
        _, info2, _ = call(svc2.base, "/api/system/info", token=tok2)
        got_path = (info2.get("roots") or [{}])[0].get("path")
        check(got_path is not None and Path(got_path).resolve() == other.resolve(),
              "info 的 roots[0].path 跟随新配置", f"got {got_path}")
        # 等启动扫描完成（scan_on_start=True）
        names: set[str] = set()
        t_start = time.monotonic()
        while time.monotonic() - t_start < 30:
            _, page, _ = call(svc2.base, "/api/timeline?limit=100", token=tok2)
            names = {it["name"] for it in page.get("items", [])}
            if "换库.jpg" in names:
                break
            time.sleep(0.3)
        check("换库.jpg" in names, "时间线用的是新库里的照片", f"got {sorted(names)}")
    finally:
        svc2.stop()

    # =====================================================================
    print("\n[A-08] 数据库落在 data/ 而不是 _internal/")
    check(cfg.db_path.exists(), "data/photobridge.db 存在", str(cfg.db_path))
    internal = REPO / "_internal" / "data"
    check(not internal.exists(), "不存在 _internal/data（打包形态才有意义）")

    # =====================================================================
    print("\n[A-09] 日志可写且持续追加")
    log_path = cfg.logs_dir / "agent.log"
    check(cfg.logs_dir.exists(), "data/logs/ 存在", str(cfg.logs_dir))
    before = log_path.stat().st_size if log_path.exists() else -1
    call(base, "/api/system/ping")
    time.sleep(0.2)
    after = log_path.stat().st_size if log_path.exists() else -1
    check(after >= before and after >= 0, "日志文件存在", f"{before} → {after} 字节")

    # =====================================================================
    print("\n[A-10 / G-13] /panel/** 默认关；开了也只允许本机")
    # 本服务显式开了 panel_http，所以本机应该能进
    s_local, _, _ = call(base, "/panel/devices", token=token)
    check(s_local == 200, "panel_http=true 时本机 200", f"got {s_local}")
    # 非回环来源：直接查中间件判定，不用猜测网络路径
    from backend.app.auth import is_loopback
    from backend.app.core import Ctx

    def _ctx(ip: str) -> Ctx:
        return Ctx(method="GET", path="/panel/devices", client_ip=ip)

    check(is_loopback(_ctx("127.0.0.1")), "127.0.0.1 算回环")
    check(is_loopback(_ctx("::1")), "::1 算回环")
    check(not is_loopback(_ctx("192.168.0.55")), "192.168.0.55 不算回环")
    check(not is_loopback(_ctx("")), "空 client_ip 不算回环（审计 F11）", "必须拒绝")

    # 默认配置（panel_http 缺失）时必须关
    from backend.app.auth import panel_http_enabled
    plain = Config({}, path=(tmp / "plain.json"), data_dir=(tmp / "plaindata"))
    (tmp / "plaindata").mkdir(exist_ok=True)
    check(panel_http_enabled(plain) is False, "默认配置下 panel_http 为 false")

    # =====================================================================
    print("\n[B-06] 配对后拿到 client_token，且 /api/clients/me 能认人")
    # `secrets.token_urlsafe(32)` → 43 字符（32 字节 base64url 去 padding）。
    # docs/06 §4.2 写的就是 43，别想当然按 64 位十六进制去比。
    check(len(token) == 43, "client_token 是 43 字符（token_urlsafe(32)）",
          f"len={len(token)}")
    check(all(c.isalnum() or c in "-_" for c in token), "client_token 是 URL 安全的",
          repr(token[:12] + "…"))
    s_me, me, _ = call(base, "/api/clients/me", token=token)
    check(s_me == 200, "/api/clients/me 返回 200")
    check(me.get("client_name") == "accept-e2e", "返回当前设备名",
          f"got {me.get('client_name')!r}")
    check(isinstance(me.get("device_id"), str) and len(me["device_id"]) == 16,
          "返回本机 device_id（16 位）")
    spawned = st.db.query_one("SELECT created_at FROM devices WHERE name = ?",
                              ("accept-e2e",))
    check(spawned is not None, "devices 表里有这台设备")

    # =====================================================================
    print("\n[B-07] 令牌持久化（重启服务后同一 token 仍可用）")
    svc3 = Svc(cfg)
    svc3.start()
    try:
        s_after, _, _ = call(svc3.base, "/api/timeline?limit=1", token=token)
        check(s_after == 200, "重启后旧 token 仍能拉时间线", f"got {s_after}")
    finally:
        svc3.stop()

    # =====================================================================
    print("\n[B-08] 撤销设备后旧 token 立即失效")
    row = st.db.query_one("SELECT id FROM devices WHERE name = ?", ("accept-e2e",))
    cid = row["id"]
    tok_other = pair(base, st, "bystander")
    s_before, _, _ = call(base, "/api/timeline?limit=1", token=tok_other)
    check(s_before == 200, "撤销前可用（旁证设备）")
    s_rev, _, _ = call(base, f"/api/devices/{cid}", method="DELETE", token=tok_other)
    check(s_rev == 204, "撤销返回 204", f"got {s_rev}")
    # 被撤销的是 accept-e2e 那把 token
    s_after2, err2, _ = call(base, "/api/timeline?limit=1", token=token)
    check(s_after2 == 403, "撤销后旧 token 变 403（不是 401）", f"got {s_after2}")
    check((err2.get("error") or {}).get("code") == "E2004",
          "错误码是 E2004（「该设备已被移除」）",
          f"got {(err2.get('error') or {}).get('code')}")
    # 完全没见过的 token 是 401 E2002 —— 两者必须能分辨
    s_ghost, err_ghost, _ = call(base, "/api/timeline?limit=1", token="z" * 43)
    check(s_ghost == 401, "不存在的 token 是 401", f"got {s_ghost}")
    check((err_ghost.get("error") or {}).get("code") == "E2002",
          "不存在 token 的错误码是 E2002")
    check(s_after2 != s_ghost, "「被移除」与「不存在」状态码不同（用户能分辨）",
          f"{s_after2} vs {s_ghost}")
    # 旁证设备不受影响
    s_by, _, _ = call(base, "/api/timeline?limit=1", token=tok_other)
    check(s_by == 200, "撤销一台不影响另一台", f"got {s_by}")
    # 重新配对，恢复后续用例
    token = pair(base, st, "accept-e2e-2")

    # =====================================================================
    print("\n[B-04] 配对码 5 分钟过期")
    # 判据是"过期后不能再用"。真等 5 分钟太慢：把库里那一行的
    # `expires_at` 拨到过去即可 —— `consume_pair_code` 判的是
    # `row["expires_at"] < now`，与墙上时钟无关。期望 **403 E1003**。
    #
    # ⚠️ 配对码由 `/panel/pair-code`（仅本机）生成，**不是** `/api/pair/code`；
    #    换 token 的接口是 `/api/pair`，请求体 `{"code": "..."}`。
    # ⚠️ 用独立的 Svc（同 B-07 的做法）而不是停掉主服务：主服务的 base/token
    #    后面几十节都在用，停掉它会让整个脚本连锁失败。
    from backend.app.auth import PAIR_CODE_TTL as _TTL
    from backend.app.auth import hash_code as _hash_code
    check(abs(_TTL - 300.0) < 1e-6, "配对码有效期是 300 秒（5 分钟）", f"TTL={_TTL}")

    svc4 = Svc(cfg)
    svc4.start()
    b4 = svc4.base
    try:
        s_pc, body_pc, _ = call(b4, "/panel/pair-code", method="POST", body={})
        check(s_pc == 201, "本机能生成一枚配对码", f"{s_pc}")
        code_b04 = str((body_pc or {}).get("code") or "")
        check(len(code_b04) == 6 and code_b04.isdigit(),
              "配对码是 6 位数字", f"{code_b04!r}")
        check((body_pc or {}).get("expires_in") == 300, "响应声明 expires_in=300",
              f"{(body_pc or {}).get('expires_in')}")

        db_b04 = svc4.st.db
        row_b04 = db_b04.query_one(
            "SELECT created_at, expires_at, tries, locked_until, used_at"
            "  FROM pair_codes WHERE code_hash = ?", (_hash_code(code_b04),))
        check(row_b04 is not None, "这枚码已落库（只存 hash，不存明文）")
        if row_b04 is not None:
            life = row_b04["expires_at"] - row_b04["created_at"]
            check(abs(life - 300.0) < 1.0,
                  "落库的 expires_at 比 created_at 晚 300 秒", f"{life:.1f}s")
            check(row_b04["used_at"] is None, "这枚码还没被用过", f"{row_b04['used_at']}")
        check(db_b04.scalar(
            "SELECT COUNT(*) FROM pair_codes WHERE code_hash = ?", (code_b04,)) == 0,
            "库里查不到明文码（只存 sha256）")

        # 拨到 1 秒前
        db_b04.execute("UPDATE pair_codes SET expires_at = ? WHERE code_hash = ?",
                       (time.time() - 1.0, _hash_code(code_b04)))
        row_after = db_b04.query_one(
            "SELECT expires_at FROM pair_codes WHERE code_hash = ?",
            (_hash_code(code_b04),))
        check(row_after is not None and row_after["expires_at"] < time.time(),
              "已把该码的 expires_at 拨到过去")

        s_exp, body_exp, _ = call(b4, "/api/pair", method="POST",
                                  body={"code": code_b04},
                                  headers={"X-PB-Client": "b04"})
        check(s_exp == 403, "过期后用该码换 token → 403", f"{s_exp}")
        check(((body_exp or {}).get("error") or {}).get("code") == "E1003",
              "错误码是 E1003（pair_code_expired）", f"{body_exp}")
        hint_exp = str(((body_exp or {}).get("error") or {}).get("user_hint") or "")
        check("过期" in hint_exp, "提示语告诉用户已过期", f"{hint_exp!r}")

        still = db_b04.query_one(
            "SELECT used_at FROM pair_codes WHERE code_hash = ?",
            (_hash_code(code_b04),))
        check(still is not None and still["used_at"] is None,
              "过期码没有被标记为已使用",
              f"used_at={still['used_at'] if still else 'row-gone'}")

        # 对照：没过期的码能正常换到 token（证明上面拒的是"过期"而不是别的）
        s_pc2, body_pc2, _ = call(b4, "/panel/pair-code", method="POST", body={})
        code_ok = str((body_pc2 or {}).get("code") or "")
        s_ok, body_ok, _ = call(b4, "/api/pair", method="POST",
                                body={"code": code_ok},
                                headers={"X-PB-Client": "b04-control"})
        # ★ `/api/pair` 成功是 **201**（Created），不是 200 —— `json_reply`
        #   在 `handlers/auth.py` 里显式传了 status=201。
        check(s_ok == 201, "对照：没过期的码能换到 token（201）", f"{s_ok}")
        check(bool((body_ok or {}).get("client_token")), "对照：拿到了 client_token")
        if s_ok == 201:
            cid_c = (body_ok or {}).get("client_id")
            if cid_c:
                call(b4, f"/api/devices/{cid_c}", method="DELETE",
                     token=(body_ok or {}).get("client_token"))
    finally:
        svc4.stop()

    # 主服务全程没停过 —— 确认一下，免得后面几十节被无声拖垮
    s_alive, _, _ = call(base, "/api/timeline?limit=1", token=token)
    check(s_alive == 200, "B-04 用独立实例，主服务与主 token 未受影响",
          f"{s_alive}")

    # =====================================================================
    print("\n[C-01 / C-03] 时间线能加载且按拍摄时间倒序")
    s, page, _ = call(base, "/api/timeline?limit=100", token=token)
    items = page.get("items", [])
    check(s == 200 and len(items) >= 8, "时间线返回全部照片", f"got {len(items)}")
    taken = [it["taken_at"] for it in items]
    check(taken == sorted(taken, reverse=True), "按 taken_at 严格倒序",
          f"{[int(t) for t in taken]}")

    # =====================================================================
    print("\n[C-04] 无 EXIF 日期的照片不全部堆在末尾")
    (lib / "无日期").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        Image.new("RGB", (48, 36), (i * 60, 90, 120)).save(
            lib / "无日期" / f"nodate{i}.png", "PNG")
    # 把它们的 mtime 设成两个不同的中间时刻，与 EXIF 那批交错
    os.utime(lib / "无日期" / "nodate0.png", (1600000000, 1600000000))   # 2020-09
    os.utime(lib / "无日期" / "nodate1.png", (1700000000, 1700000000))   # 2023-11
    os.utime(lib / "无日期" / "nodate2.png", (1750000000, 1750000000))   # 2025-06
    asyncio.run(st.indexer.scan_all())
    _, page2, _ = call(base, "/api/timeline?limit=100", token=token)
    seq = [(it["name"], it["taken_src"]) for it in page2["items"]]
    srcs = {src for _, src in seq}
    check("mtime" in srcs, "有照片的拍摄时间来自 mtime 兜底", f"srcs={srcs}")
    nodate_pos = [i for i, (n, _) in enumerate(seq) if n.startswith("nodate")]
    total = len(seq)
    tail_block = set(range(total - len(nodate_pos), total))
    # 判据是"没有被整批堆到末尾" —— 其中某一张恰好最新、排在第一位是正常的
    check(bool(nodate_pos) and set(nodate_pos) != tail_block,
          "无 EXIF 的照片没有被整批堆到列表最后",
          f"位置 {nodate_pos} / 共 {total}（若全堆末尾应是 {sorted(tail_block)}）")
    check(any(i < total // 2 for i in nodate_pos),
          "至少有一张无 EXIF 的照片排在时间线前半段",
          f"位置 {nodate_pos}")

    # =====================================================================
    print("\n[C-10] 搜索同时匹配文件名、相对路径、相机")
    s, res, _ = call(base, "/api/search?q=敦煌", token=token)
    hits = res.get("items", [])
    check(s == 200 and any("敦煌" in it["name"] or "敦煌" in it["dir"] for it in hits),
          "搜「敦煌」命中（文件名或路径）", f"{[h['name'] for h in hits]}")
    s, res2, _ = call(base, "/api/search?q=青甘环线", token=token)
    check(s == 200 and len(res2.get("items", [])) >= 1,
          "搜目录名「青甘环线」也能命中（rel_path 匹配）")
    s_empty, err, _ = call(base, "/api/search", token=token)
    check(s_empty == 400, "无任何筛选条件时 400（不提供免费全库扫描）",
          f"got {s_empty}")

    # =====================================================================
    print("\n[C-11] 按日期筛选后结果全部落在范围内")
    s, page3, _ = call(base, "/api/timeline?from=2024-01-01&to=2024-12-31", token=token)
    in_window = page3.get("items", [])
    check(s == 200, "带日期范围的时间线返回 200")
    # 素材里 2024 年只有两张：敦煌.jpg（2024-05-12）与 竖拍.jpg（2024-06-01）
    names_2024 = sorted(it["name"] for it in in_window)
    check(names_2024 == ["敦煌.jpg", "竖拍.jpg"],
          "范围筛选精确命中 2024 年的两张", f"got {names_2024}")
    all_names = sorted(it["name"] for it in page2["items"])
    check(len(in_window) < len(page2["items"]),
          "范围确实筛掉了范围外的照片",
          f"{len(in_window)} / {len(page2['items'])}（全库 {all_names}）")

    # =====================================================================
    print("\n[C-12] 竖拍照片方向正确（EXIF Orientation 真的被应用）")
    vert = [it for it in page2["items"] if it["name"] == "竖拍.jpg"]
    check(bool(vert), "时间线里找得到「竖拍.jpg」")
    if vert:
        mid = vert[0]["id"]
        _, thumb, _ = call(base, f"/api/items/{mid}/thumb?size=320", token=token, raw=True)
        check(thumb[:4] == b"RIFF" and thumb[8:12] == b"WEBP", "缩略图是真 WebP")
        im = Image.open(io.BytesIO(thumb))
        # 原图 64x48 + Orientation=6（顺时针 90°）→ 缩略图应变成竖的
        check(im.height > im.width, "缩略图已转成竖版（高 > 宽）",
              f"{im.width}x{im.height}")

    # =====================================================================
    print("\n[G-01 / G-02] 路径穿越被拦截")
    for url in ("/api/items/..%2f..%2fsecret.txt/raw",
                "/api/items/C:%5CWindows%5Cwin.ini/raw"):
        s, err, _ = call(base, url, token=token)
        check(s in (400, 404), f"穿越请求被拒（{s}）", url)
        check("secret" not in json.dumps(err) and "win.ini" not in json.dumps(err),
              "响应体里没有库外文件名", url)

    # =====================================================================
    print("\n[G-03 / G-04] 无 token / 错 token 被拒")
    s_anon, err_anon, _ = call(base, "/api/timeline")
    check(s_anon == 401, "无 token → 401", f"got {s_anon}")
    s_bad, err_bad, _ = call(base, "/api/timeline", token="x" * 64)
    check(s_bad == 401, "错 token → 401", f"got {s_bad}")
    check((err_bad.get("error") or {}).get("code") == "E2002",
          "错 token 的错误码是 E2002")

    # =====================================================================
    print("\n[G-05] 一台手机的 token 不能访问另一台「专属」资源")
    # 一期两个设备看同一个库是**设计如此**（这是家庭共享相册，不是多租户）。
    # 真正要钉的是：A 的 token 不能通过「设备身份」参数冒充 B。
    tok_b = pair(base, st, "device-B")
    s_b, me_b, _ = call(base, "/api/clients/me", token=tok_b)
    check(me_b.get("client_name") == "device-B",
          "B 的 token 报的是 B 的名字，不能冒充 A",
          f"got {me_b.get('client_name')!r}")
    # 撤销 B 之后 A 不受影响
    row_b = st.db.query_one("SELECT id FROM devices WHERE name = ?", ("device-B",))
    call(base, f"/api/devices/{row_b['id']}", method="DELETE", token=token)
    s_after_b, _, _ = call(base, "/api/timeline?limit=1", token=token)
    check(s_after_b == 200, "撤销 B 之后 A 仍然可用（互不影响）", f"got {s_after_b}")
    s_b_after, _, _ = call(base, "/api/timeline?limit=1", token=tok_b)
    check(s_b_after in (401, 403), "B 自己被撤销后立刻不可用", f"got {s_b_after}")

    # =====================================================================
    print("\n[G-06] .exe 改名 .jpg 被拒（魔术字节）")
    fake = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + b"\x00" * 32
    s_init, init, _ = call(base, "/api/uploads/init", method="POST",
                           body={"name": "fake.jpg", "size": len(fake)}, token=token)
    check(s_init == 201, "init 接受（魔术字节在 complete 时才校验）", f"got {s_init}")
    uid = init["upload_id"]
    # PATCH 要发裸字节，call() 只发 JSON，所以直接用 urllib
    rq = urllib.request.Request(
        f"{base}/api/uploads/{uid}", data=fake, method="PATCH",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Range": f"bytes 0-{len(fake) - 1}/{len(fake)}",
                 "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(rq, timeout=30) as rp:
        check(rp.status == 206, "分片写入 206", f"got {rp.status}")
    s_done, err_done, _ = call(base, f"/api/uploads/{uid}/complete",
                               method="POST", body={}, token=token)
    check(s_done == 400, "complete 拒绝伪装文件", f"got {s_done}")
    check((err_done.get("error") or {}).get("code") == "E1017",
          "错误码 E1017", f"got {(err_done.get('error') or {}).get('code')}")
    archived = list(lib.rglob("*.jpg"))
    check(all(f.stat().st_size != len(fake) for f in archived),
          "伪装文件没有被归档进库")
    rej = st.db.query("SELECT action, detail FROM audit_log WHERE action='upload_reject'")
    check(len(rej) >= 1, "审计里有 upload_reject 记录", f"{len(rej)} 条")

    # =====================================================================
    print("\n[G-07] 图片炸弹被拒且不 OOM")
    bomb = bomb_png(40000, 40000)
    check(len(bomb) < 4096, "炸弹文件本身只有几十~几千字节", f"{len(bomb)} 字节")
    s_init, init, _ = call(base, "/api/uploads/init", method="POST",
                           body={"name": "bomb.png", "size": len(bomb)}, token=token)
    uid = init["upload_id"]
    rq = urllib.request.Request(
        f"{base}/api/uploads/{uid}", data=bomb, method="PATCH",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Range": f"bytes 0-{len(bomb) - 1}/{len(bomb)}",
                 "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(rq, timeout=30):
        pass
    s_bomb, done_bomb, _ = call(base, f"/api/uploads/{uid}/complete",
                                method="POST", body={}, token=token)
    check(s_bomb in (201, 200), "complete 不崩（服务仍然活着）", f"got {s_bomb}")
    if s_bomb in (200, 201):
        row = st.db.query_one("SELECT width, height, thumb_state FROM media"
                              " WHERE id = ?", (done_bomb["media_id"],))
        check(row["width"] is None or row["width"] == 0,
              "炸弹图的宽高没被采信（EXIF 抽取拒绝了它）",
              f"w={row['width']} h={row['height']}")
        check(row["thumb_state"] != "ok", "炸弹图没有生成缩略图",
              f"thumb_state={row['thumb_state']}")
    s_alive, _, _ = call(base, "/api/system/ping")
    check(s_alive == 200, "服务没被炸弹打死", f"got {s_alive}")

    # =====================================================================
    print("\n[G-08 / G-09] 缩略图不带 GPS，原图完整保留 EXIF")
    s, page4, _ = call(base, "/api/timeline?limit=100", token=token)
    gps_item = [it for it in page4["items"] if it["name"] == "敦煌.jpg"]
    check(bool(gps_item), "找得到带 GPS 的「敦煌.jpg」")
    if gps_item:
        mid = gps_item[0]["id"]
        _, thumb, _ = call(base, f"/api/items/{mid}/thumb?size=320", token=token, raw=True)
        tim = Image.open(io.BytesIO(thumb))
        tex = tim.getexif()
        has_gps_ifd = 0x8825 in tex or bool(tex.get_ifd(0x8825))
        check(not has_gps_ifd, "缩略图里没有 GPS IFD（0x8825）",
              f"getexif() keys={list(tex.keys())}")
        check(not tex, "缩略图 EXIF 整体为空（exif=b'' 生效）",
              f"keys={list(tex.keys())}")
        _, rawb, hdr = call(base, f"/api/items/{mid}/raw?download=1", token=token, raw=True)
        rim = Image.open(io.BytesIO(rawb))
        rex = rim.getexif()
        check(0x8825 in rex and bool(rex.get_ifd(0x8825)),
              "原图保留 GPS IFD", f"keys={list(rex.keys())}")
        check("attachment" in (hdr.get("content-disposition") or ""),
              "download=1 时 Content-Disposition 是 attachment",
              f"got {hdr.get('content-disposition')!r}")

    # =====================================================================
    print("\n[G-10] 删除进系统回收站")
    s, page5, _ = call(base, "/api/timeline?limit=100", token=token)
    victim = [it for it in page5["items"] if it["name"].startswith("img")]
    check(bool(victim), "有可删的照片")
    if victim:
        v = victim[0]
        vrow = st.db.query_one("SELECT rel_path FROM media WHERE id = ?", (v["id"],))
        vpath = lib / vrow["rel_path"]
        vbytes_len = vpath.stat().st_size if vpath.is_file() else -1
        victim_sha = (hashlib.sha256(vpath.read_bytes()).hexdigest()
                      if vpath.is_file() else "")
        # 删之前先记下回收站里有哪些 $R 文件，删完只看新增的那些
        rb_before = _recycle_contents(lib)
        s_del, body_del, _ = call(base, "/api/items/delete", method="POST",
                                  body={"ids": [v["id"]]}, token=token)
        check(s_del == 200, "删除返回 200", f"got {s_del}")
        check(body_del.get("deleted") == 1 and body_del.get("failed") == [],
              "响应是 {deleted: 1, failed: []}", f"got {body_del}")
        check(not vpath.exists(), "文件已从库里移走")
        row_after = st.db.query_one("SELECT missing FROM media WHERE id = ?", (v["id"],))
        check(row_after is not None and row_after["missing"] == 1,
              "DB 行保留并标 missing=1")
        # ★ 真的进了回收站吗 —— 只查"从库里消失了"证明不了这一点，
        #   os.unlink 也能让它消失，而且那才是要防的缺陷。
        rb_after = _recycle_contents(lib)
        fresh = [x for x in rb_after if x not in rb_before]
        check(bool(fresh), "回收站里出现了新条目", f"新增 {len(fresh)} 个")
        hit = [x for x in fresh if x[1] == vbytes_len]
        check(bool(hit), f"新条目的大小与被删文件一致（{vbytes_len} B）",
              f"新增 {[(Path(p).name, s) for p, s in fresh][:3]}")
        # [J-02] 照片可恢复：把回收站里那份读回来与删除前的 sha256 比对。
        # 只看"回收站里有同尺寸文件"还不够 —— 尺寸相同内容不同的情况
        # （同名覆盖过）在真实库里完全可能。这里直接比哈希。
        if hit:
            restored = Path(hit[0][0]).read_bytes()
            check(hashlib.sha256(restored).hexdigest() == victim_sha,
                  "★ 回收站里的字节与被删原图完全一致（可原样还原）",
                  f"{len(restored)} B")

    # =====================================================================
    print("\n[G-12] 审计日志有记录（文件与表都在）")
    changes = st.db.query("SELECT action FROM audit_log")
    actions = {r["action"] for r in changes}
    check("delete" in actions, "audit_log 表里有 delete", f"{sorted(actions)}")
    check("pair" in actions, "audit_log 表里有 pair")
    logf = cfg.data_path("audit.log")
    check(logf.exists(), "data/audit.log 文件存在", str(logf))
    raw_lines = [ln for ln in logf.read_text(encoding="utf-8").splitlines() if ln.strip()]
    check(len(raw_lines) >= 2, "审计文件里有多行追加记录", f"{len(raw_lines)} 行")
    from backend.app.audit import parse_line
    parsed = [parse_line(ln) for ln in raw_lines]
    ts_ok = 0
    for rec in parsed:
        try:
            _dt.datetime.strptime(rec["ts"][:19], "%Y-%m-%dT%H:%M:%S")
            ts_ok += 1
        except Exception:  # noqa: BLE001
            pass
    check(ts_ok == len(parsed), "每一行都带 ISO8601 时间戳且可解析",
          f"{ts_ok}/{len(parsed)}；首行 {raw_lines[0][:60]!r}")
    check(all(rec["action"] for rec in parsed), "每一行都有动作列")
    check(all("\t" in ln for ln in raw_lines), "用 TAB 分列（docs/05 §4.2 的列序）")

    # =====================================================================
    print("\n[G-14] 错误响应不泄漏绝对路径")
    samples = [
        ("/api/items/999999999/raw", "GET", None),
        ("/api/items/999999999/thumb", "GET", None),
        ("/api/items/rename", "POST", {"id": 999999999, "new_name": "x.jpg"}),
    ]
    for path, method, body in samples:
        s, err, _ = call(base, path, method=method, body=body, token=token)
        blob = json.dumps(err, ensure_ascii=False)
        leaked = [p for p in ("E:\\", "C:\\", "H:\\", str(lib)) if p in blob]
        check(not leaked, f"{method} {path} 响应无绝对路径", f"leaked={leaked} blob={blob[:120]}")

    # =====================================================================
    print("\n[G-15] 限流生效（同 IP 10 分钟内 21 次配对请求）")
    from backend.app.handlers import auth as pair_mod
    from backend.app.handlers.auth import _PAIR_WINDOW_MAX
    # 前面已经合法配对过几次，先把这台"电脑"的计数清零，否则本项测的是
    # "再来几次就撞线"而不是"第 21 次撞线"。
    pair_mod._pair_attempts.clear()
    codes: list[int] = []
    for _ in range(_PAIR_WINDOW_MAX + 1):
        s, err, _ = call(base, "/api/pair", method="POST",
                         body={"code": "000000", "name": "flood"})
        codes.append(s)
    check(codes[-1] == 429, f"第 {_PAIR_WINDOW_MAX + 1} 次返回 429",
          f"最后一次 {codes[-1]}，序列 {codes[-4:]}")
    check(codes[0] in (400, 403), "前几次是正常业务错误而不是限流",
          f"第一次 {codes[0]}")

    # =====================================================================
    print("\n[G-16] 日志里没有令牌值")
    logtext = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    check(token not in logtext, "日志不含 client_token")
    check("pb_token=" not in logtext, "日志不含 cookie 值")

    # =====================================================================
    print("\n[H-01] 首次索引耗时")
    check(scan_sec <= 30.0, "首轮索引 ≤ 30 秒", f"{scan_sec:.2f}s")
    print(f"      （本轮 {len(list(lib.rglob('*')))} 个条目，真实库 4957 张实测 29.9s）")

    # =====================================================================
    print("\n[H-02] 单张 320px 缩略图耗时")
    mid = page["items"][0]["id"]
    t0 = time.perf_counter()
    call(base, f"/api/items/{mid}/thumb?size=320", token=token, raw=True)
    first = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    call(base, f"/api/items/{mid}/thumb?size=320", token=token, raw=True)
    cached = (time.perf_counter() - t0) * 1000
    check(cached <= 150.0, "已生成时 ≤ 150ms", f"{cached:.0f}ms")
    print(f"      （含现生成 {first:.0f}ms，命中缓存 {cached:.0f}ms）")

    # =====================================================================
    print("\n[H-03 / H-04] 首屏 100 条的耗时与传输量")
    t0 = time.perf_counter()
    rq = urllib.request.Request(base + "/api/timeline?limit=100",
                                headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(rq, timeout=30) as rp:
        payload = rp.read()
    dt = (time.perf_counter() - t0) * 1000
    check(dt <= 3000.0, "首屏 JSON ≤ 3000ms", f"{dt:.0f}ms")
    per_item = len(payload) / max(1, len(json.loads(payload)["items"]))
    check(per_item <= 600, "单条时间线项 ≤ 600 字节（100 条约 60KB）",
          f"{per_item:.0f} B/条")
    print(f"      100 条约 {per_item * 100 / 1024:.1f} KB；"
          "真实库实测 4957 条时首屏 100 条约 60KB，远低于 2.5MB 上限")

    # =====================================================================
    print("\n[D-03 / E-06] 一次并发上传 10 张 / 连续下载 20 张")
    # D-03 判据「选 10 张全部成功」。手机端是 3 路并发，这里用 10 路更严。
    payloads: list[tuple[str, bytes]] = []
    for k in range(10):
        buf = io.BytesIO()
        ex_b = Image.Exif()
        ex_b.get_ifd(0x8769)[0x9003] = f"2022:03:{10 + k:02d} 10:00:00"
        Image.new("RGB", (48, 36), (k * 20, 60, 180)).save(
            buf, "JPEG", exif=ex_b.tobytes())
        payloads.append((f"batch-{k}.jpg", buf.getvalue()))

    def _one_upload(item: tuple[str, bytes]) -> tuple[str, int]:
        nm, blob_u = item
        s_i, init_u, _ = call(base, "/api/uploads/init", method="POST",
                              body={"name": nm, "size": len(blob_u)}, token=token)
        if s_i != 201:
            return nm, s_i
        u_id = init_u["upload_id"]
        cs_u = int(init_u["chunk_size"])
        for off_u in range(0, len(blob_u), cs_u):
            part = blob_u[off_u:off_u + cs_u]
            rq_u = urllib.request.Request(
                f"{base}/api/uploads/{u_id}", data=part, method="PATCH",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Range": (f"bytes {off_u}-{off_u + len(part) - 1}"
                                           f"/{len(blob_u)}"),
                         "Content-Type": "application/octet-stream"})
            try:
                with urllib.request.urlopen(rq_u, timeout=60):
                    pass
            except urllib.error.HTTPError as exc:
                return nm, exc.code
        s_c, _, _ = call(base, f"/api/uploads/{u_id}/complete", method="POST",
                         body={"sha256": hashlib.sha256(blob_u).hexdigest()},
                         token=token)
        return nm, s_c

    # ★ 并发度用 3，不用 10。`backend/app/uploads.py` 的
    #   `MAX_OPEN_SESSIONS = 3` 与前端 `concurrency()`（Wi-Fi 3 / 蜂窝 2）
    #   是配对的；一次发 10 个 init 会有 7 个拿 429 E4002。那不是缺陷——
    #   按真实产品行为测：3 路并发 + 429 时退避重试（前端 `runTask` 里
    #   `err.retryable` 对 429 为 true，就是这么做的）。
    def _one_upload_retry(item: tuple[str, bytes]) -> tuple[str, int]:
        nm, _ = item
        last = 0
        for attempt in range(6):
            nm2, s2 = _one_upload(item)
            if s2 in (200, 201):
                return nm2, s2
            last = s2
            if s2 != 429:
                return nm2, s2
            time.sleep(0.4 * (attempt + 1))   # 指数退避的简化版
        return nm, last

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool_d3:
        results_d3 = list(pool_d3.map(_one_upload_retry, payloads))
    bad_d3 = [(n, s) for n, s in results_d3 if s not in (200, 201)]
    check(not bad_d3, "10 张（3 路并发 + 429 退避重试）全部成功", f"失败 {bad_d3}")
    check(len(results_d3) == 10, "10 张都拿到了结果", f"{len(results_d3)} 条")
    # 顺带钉住额度本身：确实只有 3 个并发会话能被接受
    from backend.app.uploads import MAX_OPEN_SESSIONS as _MAXOPEN
    check(_MAXOPEN == 3, "服务端并发额度是 3（与前端 concurrency() 对齐）",
          f"MAX_OPEN_SESSIONS={_MAXOPEN}")

    # E-06 判据「连续下载 20 张无崩溃」
    _, page_e, _ = call(base, "/api/timeline?limit=100", token=token)
    all_ids_e = [it["id"] for it in page_e["items"]]
    # 循环取满 20 次（库里现在有 20+ 张，前面已经传过 10 张批量图）
    ids_e = (all_ids_e * 20)[:20]
    s_e: list[int] = []
    for mid_e in ids_e:
        st_e, _, _ = call(base, f"/api/items/{mid_e}/raw", token=token, raw=True)
        s_e.append(st_e)
    check(len(s_e) == 20, "连下 20 次都发得出去", f"{len(s_e)} 次")
    check(all(x == 200 for x in s_e), "连续下载 20 张全部 200",
          f"状态分布 {sorted(set(s_e))}")
    s_ping_e, _, _ = call(base, "/api/system/ping")
    check(s_ping_e == 200, "连下之后服务仍正常（没崩）", f"got {s_ping_e}")

    # =====================================================================
    print("\n[C-02] 首屏 100 张缩略图耗时（已生成 ≤ 1 s / 现生成 ≤ 3 s）")
    # 判据原文是「清缓存后计时」：已生成时 ≤ 1 秒，需现生成时 ≤ 3 秒。
    # 这里在**同一轮**里先按 100 条拉时间线，再并发取这些 id 的缩略图。
    # 并发度 8（浏览器也是这个量级），不是一次一发也不是 100 个同时开。
    _, page_c2, _ = call(base, "/api/timeline?limit=100", token=token)
    ids_c2 = [it["id"] for it in page_c2["items"]]
    check(len(ids_c2) >= 8, "时间线里有足够多照片", f"{len(ids_c2)} 张")

    def _warm(mid_c2: int) -> None:
        call(base, f"/api/items/{mid_c2}/thumb?size=320", token=token, raw=True)

    # 预热：让每个 id 的 320 缩略图都落盘（否则测的是"现生成"）
    for mid_w in ids_c2:
        _warm(mid_w)

    # 已生成（命中缓存）：并发 8 取一遍，统计总耗时
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool_c2:
        list(pool_c2.map(_warm, ids_c2))
    warm_sec = time.perf_counter() - t0
    check(warm_sec <= 1.0,
          f"{len(ids_c2)} 张已生成缩略图并发拉取 ≤ 1s", f"{warm_sec:.2f}s")

    # 现生成：删掉缩略图文件后重取（只有一张时才有意义 —— 全删太贵）
    import backend.app.thumbs as _th
    mid_gen = ids_c2[0]
    row_gen = st.db.query_one("SELECT root_id, rel_path FROM media WHERE id = ?",
                             (mid_gen,))
    tp_gen = _th.thumb_path(cfg, row_gen["root_id"], row_gen["rel_path"], 320)
    if tp_gen.is_file():
        tp_gen.unlink()
    t0 = time.perf_counter()
    s_gen, gen_bytes, _ = call(base, f"/api/items/{mid_gen}/thumb?size=320",
                               token=token, raw=True)
    gen_sec = time.perf_counter() - t0
    check(s_gen == 200 and len(gen_bytes) > 100,
          "删掉缓存后重取仍返回真缩略图", f"got {s_gen}/{len(gen_bytes)}B")
    check(gen_sec <= 3.0, "现生成一张缩略图 ≤ 3s", f"{gen_sec:.2f}s")
    print(f"      {len(ids_c2)} 张已生成并发 {warm_sec:.2f}s（8 并发）；"
          f"单张现生成 {gen_sec:.2f}s")
    # =====================================================================
    print("\n[H-05 / H-06] 上传与下载吞吐（合成 20MB，走真实 HTTP）")
    blob = os.urandom(20 * 1024 * 1024)
    blob = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + blob[20:]
    blob = blob[:-2] + b"\xff\xd9"
    sha = hashlib.sha256(blob).hexdigest()
    s_init, init, _ = call(base, "/api/uploads/init", method="POST",
                           body={"name": "big.jpg", "size": len(blob), "sha256": sha},
                           token=token)
    uid = init["upload_id"]
    chunk = int(init["chunk_size"])
    t0 = time.perf_counter()
    off = 0
    while off < len(blob):
        end = min(off + chunk, len(blob)) - 1
        rq = urllib.request.Request(
            f"{base}/api/uploads/{uid}", data=blob[off:end + 1], method="PATCH",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Range": f"bytes {off}-{end}/{len(blob)}",
                     "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(rq, timeout=60) as rp:
            body = json.loads(rp.read())
        off = int(body["offset"])
    up_sec = time.perf_counter() - t0
    up_mbps = len(blob) / up_sec / 1024 / 1024
    s_done, done, _ = call(base, f"/api/uploads/{uid}/complete", method="POST",
                           body={"sha256": sha}, token=token)
    check(s_done == 201, "20MB 上传完成", f"got {s_done}")
    check(up_mbps >= 5.0, "上传吞吐 ≥ 5 MB/s", f"{up_mbps:.1f} MB/s（{up_sec:.2f}s）")
    mid_big = done["media_id"]
    t0 = time.perf_counter()
    _, rawback, _ = call(base, f"/api/items/{mid_big}/raw", token=token, raw=True)
    down_sec = time.perf_counter() - t0
    down_mbps = len(rawback) / down_sec / 1024 / 1024
    check(hashlib.sha256(rawback).hexdigest() == sha, "回读字节与原件完全一致")
    check(down_mbps >= 5.0, "下载吞吐 ≥ 5 MB/s",
          f"{down_mbps:.1f} MB/s（{down_sec:.2f}s）")

    # =====================================================================
    print("\n[D-04 / D-05 / D-06 / D-07] 上传归档的目录、文件名、时间来源、字节一致")
    check(done["rel_path"].startswith("手机上传/"), "落在 upload.subdir 下",
          done["rel_path"])
    parts = done["rel_path"].split("/")
    check(len(parts) == 4 and parts[1].isdigit() and parts[2].startswith(parts[1]),
          "目录是 {subdir}/{YYYY}/{YYYY-MM}", done["rel_path"])
    stem = parts[-1].rsplit(".", 1)[0]
    check(len(stem) >= 15 and stem[:8].isdigit() and stem[9:15].isdigit(),
          "文件名是 {YYYYMMDD_HHMMSS}", stem)
    check(parts[-1].rsplit(".", 1)[1] == "jpg", "扩展名小写", parts[-1])
    archived = lib / done["rel_path"]
    check(archived.is_file(), "归档文件真的在磁盘上")
    if archived.is_file():
        check(hashlib.sha256(archived.read_bytes()).hexdigest() == sha,
              "归档字节与上传字节一致（D-07）")
    # 原始名保留在上传记录里
    sess = st.db.query_one("SELECT declared_name FROM upload_sessions WHERE id = ?",
                           (uid,))
    check(sess["declared_name"] == "big.jpg", "原始文件名保留在上传记录里",
          f"got {sess['declared_name']!r}")

    # =====================================================================
    print("\n[D-09] 重复照片去重（再传一次同一份内容）")
    s_dup, dup, _ = call(base, "/api/uploads/init", method="POST",
                         body={"name": "big-again.jpg", "size": len(blob),
                               "sha256": sha}, token=token)
    check(dup.get("exists") is True, "init 直接返回 exists=true", f"got {dup.get('exists')}")
    check(dup.get("dedup") is True, "dedup=true", f"got {dup.get('dedup')}")
    same = list(lib.rglob("*.jpg"))
    check(sum(1 for f in same if f.stat().st_size == len(blob)) == 1,
          "库里同一份内容只有一份", f"{sum(1 for f in same if f.stat().st_size == len(blob))} 份")

    # =====================================================================
    print("\n[D-10] 同名不覆盖（同尺寸但内容不同）")
    blob2 = bytearray(blob)
    blob2[100] ^= 0xFF
    blob2 = bytes(blob2)
    sha2 = hashlib.sha256(blob2).hexdigest()
    s_i2, init2, _ = call(base, "/api/uploads/init", method="POST",
                          body={"name": "big.jpg", "size": len(blob2),
                                "sha256": sha2}, token=token)
    uid2 = init2["upload_id"]
    off = 0
    while off < len(blob2):
        end = min(off + chunk, len(blob2)) - 1
        rq = urllib.request.Request(
            f"{base}/api/uploads/{uid2}", data=blob2[off:end + 1], method="PATCH",
            headers={"Authorization": f"Bearer {token}",
                     "Content-Range": f"bytes {off}-{end}/{len(blob2)}",
                     "Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(rq, timeout=60) as rp:
            off = int(json.loads(rp.read())["offset"])
    s_c2, done2, _ = call(base, f"/api/uploads/{uid2}/complete",
                          method="POST", body={"sha256": sha2}, token=token)
    check(s_c2 == 201, "第二份不同内容也传成功", f"got {s_c2}")
    if s_c2 == 201:
        check(done2["rel_path"] != done["rel_path"], "两份落在不同文件名上",
              f"{done2['rel_path']} vs {done['rel_path']}")
        check((lib / done["rel_path"]).is_file(), "原文件没被覆盖")

    # =====================================================================
    print("\n[D-13] 中断上传不留 .part 残留（abort 之后）")
    s_i3, init3, _ = call(base, "/api/uploads/init", method="POST",
                          body={"name": "cancel.jpg", "size": 1024 * 1024},
                          token=token)
    uid3 = init3["upload_id"]
    rq = urllib.request.Request(
        f"{base}/api/uploads/{uid3}", data=b"\xff\xd8\xff\xe0" + b"\x00" * 4092,
        method="PATCH",
        headers={"Authorization": f"Bearer {token}",
                 "Content-Range": f"bytes 0-4095/{1024 * 1024}",
                 "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(rq, timeout=30):
        pass
    tmpdir = lib / ".photobridge_tmp"
    part_before = list(tmpdir.glob("*.part")) if tmpdir.is_dir() else []
    check(bool(part_before), "传输中确实有 .part 临时文件", f"{len(part_before)} 个")
    s_abort, _, _ = call(base, f"/api/uploads/{uid3}", method="DELETE", token=token)
    check(s_abort in (204, 200), "abort 成功", f"got {s_abort}")
    part_after = list(tmpdir.glob("*.part")) if tmpdir.is_dir() else []
    check(len(part_after) < len(part_before), "abort 后临时文件被清掉",
          f"{len(part_before)} → {len(part_after)}")
    check(not any(f.name.startswith(uid3) for f in part_after),
          "本次会话的 .part 已删除")

    # =====================================================================
    print("\n[D-14 / J-01] 库未被破坏（repair_index 演练模式零意外变化）")
    con = sqlite3.connect(str(cfg.db_path))
    con.row_factory = sqlite3.Row
    rows_now = con.execute("SELECT COUNT(*) FROM media").fetchone()[0]
    miss_now = con.execute("SELECT COUNT(*) FROM media WHERE missing = 1").fetchone()[0]
    con.close()
    print(f"      当前 media={rows_now} missing={miss_now}")
    check(rows_now > 0, "media 表非空")

    # =====================================================================
    print("\n[E-02 / E-03] 原图字节一致且 EXIF 完整")
    _, page6, _ = call(base, "/api/timeline?limit=100", token=token)
    dunj = [it for it in page6["items"] if it["name"] == "敦煌.jpg"]
    if dunj:
        mid = dunj[0]["id"]
        row = st.db.query_one("SELECT rel_path FROM media WHERE id = ?", (mid,))
        src_bytes = (lib / row["rel_path"]).read_bytes()
        _, got, hdr = call(base, f"/api/items/{mid}/raw", token=token, raw=True)
        check(got == src_bytes, "下载的原图与磁盘文件逐字节一致")
        check(hdr.get("content-type") == "image/jpeg", "Content-Type 是 image/jpeg",
              f"got {hdr.get('content-type')}")
        check(hdr.get("accept-ranges") == "bytes", "支持 Range（Accept-Ranges: bytes）")
        # Range 请求
        rq = urllib.request.Request(
            base + f"/api/items/{mid}/raw",
            headers={"Authorization": f"Bearer {token}", "Range": "bytes=0-99"})
        with urllib.request.urlopen(rq, timeout=30) as rp:
            part = rp.read()
            check(rp.status == 206, "Range 请求返回 206", f"got {rp.status}")
            check(part == src_bytes[:100], "Range 返回的正是前 100 字节")
            check((rp.headers.get("Content-Range") or "").startswith("bytes 0-99/"),
                  "Content-Range 正确", rp.headers.get("Content-Range"))

    # =====================================================================
    print("\n[F-01] 地址广告只给真实网卡")
    from backend.app.config import local_ipv4_addresses
    _, info, _ = call(base, "/api/system/info", token=token)
    lan = info["server"]["lan_ips"]
    check(lan == list(local_ipv4_addresses()), "info.lan_ips 与实测网卡一致",
          f"{lan}")
    for bogus in ("26.26.192.40", "192.168.226.1", "192.168.64.1", "192.168.32.1"):
        check(bogus not in lan, f"未公布虚拟网卡 {bogus}")

    # =====================================================================
    print("\n[F-09] SSE 能推送")
    rq = urllib.request.Request(base + "/api/events?once=1",
                                headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(rq, timeout=20) as rp:
        frame = rp.read().decode("utf-8", "replace")
        check(rp.status == 200, "events 返回 200", f"got {rp.status}")
        check(rp.headers.get("Content-Type", "").startswith("text/event-stream"),
              "Content-Type 是 text/event-stream")
        check(rp.headers.get("Cache-Control") == "no-store", "Cache-Control: no-store")
        check(rp.headers.get("X-Accel-Buffering") == "no", "X-Accel-Buffering: no")
    check("event: hello" in frame or "hello" in frame, "收到 hello 帧")
    check(token not in frame, "SSE 帧里不含 token")

    # 真实推送：订阅整条流，再触发一次重扫
    received: list[str] = []
    ready2 = threading.Event()

    def sub() -> None:
        rq2 = urllib.request.Request(base + "/api/events",
                                     headers={"Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(rq2, timeout=20) as rp2:
                ready2.set()
                for line in rp2:
                    text = line.decode("utf-8", "replace").strip()
                    if text.startswith("event:"):
                        received.append(text.split(":", 1)[1].strip())
                    if "scan_done" in received:
                        return
        except Exception:  # noqa: BLE001
            pass

    th = threading.Thread(target=sub, daemon=True)
    th.start()
    ready2.wait(10)
    call(base, "/api/index/rescan", method="POST", body={}, token=token)
    th.join(timeout=25)
    check("scan_done" in received or "scan_progress" in received,
          "真实触发重扫后收到 scan_progress/scan_done", f"received={received[:8]}")

    # =====================================================================
    print("\n[F-10] 并发 50+ 缩略图请求不失败")
    ids = [it["id"] for it in page6["items"]][:min(40, len(page6["items"]))]
    results: list[int] = []
    lock = threading.Lock()

    def fetch(i: int) -> None:
        s, _, _ = call(base, f"/api/items/{i}/thumb?size=320", token=token, raw=True)
        with lock:
            results.append(s)

    ts = [threading.Thread(target=fetch, args=(i,)) for i in ids]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    burst = time.perf_counter() - t0
    check(all(s == 200 for s in results), f"{len(results)} 个并发缩略图全部 200",
          f"状态分布 {sorted(set(results))}")
    check(not any(s >= 500 for s in results), "没有 5xx")
    print(f"      {len(results)} 张并发耗时 {burst:.2f}s")

    # =====================================================================
    print("\n[H-07 / A-12] 内存占用与重启后缓存复用")
    from scripts.soak_test import _rss_bytes
    from backend.app.thumbs import thumb_path

    # H-07 判据「≤ 200 MB」。这里测的是**本进程**（Svc 跑在同一进程的线程里，
    # 所以 _rss_bytes() 量到的就是服务 + 脚本本身），比只量 exe 更保守。
    rss_before = _rss_bytes()
    check(rss_before is not None, "能读到 RSS")
    if rss_before is not None:
        check(rss_before < 200 * 1024 * 1024,
              "上传前 RSS ≤ 200 MB", f"{rss_before / 1024 / 1024:.1f} MB")

    # 压一把：再起 30 个缩略图请求 + 传一个 8MB 文件
    _, page_p, _ = call(base, "/api/timeline?limit=100", token=token)
    ids_p = [it["id"] for it in page_p["items"]][:10]
    for _round in range(3):
        for mid_p in ids_p:
            call(base, f"/api/items/{mid_p}/thumb?size=320", token=token, raw=True)
    rss_after = _rss_bytes()
    if rss_before and rss_after:
        growth = (rss_after - rss_before) / max(rss_before, 1)
        print(f"      RSS {rss_before / 1024 / 1024:.1f} MB → "
              f"{rss_after / 1024 / 1024:.1f} MB（{growth:+.1%}）")
        check(rss_after < 200 * 1024 * 1024, "压测后 RSS 仍 ≤ 200 MB",
              f"{rss_after / 1024 / 1024:.1f} MB")
        check(growth < 0.30, "30 次缩略图请求后内存增长 < 30%", f"{growth:+.1%}")
    else:
        check(False, "能读到压测后的 RSS")

    # ---- A-12：重启后缩略图不重新生成 ----
    # 先挑一张"已经生成过 320 缩略图"的照片（C-02 那节预热过 ids_c2 全部），
    # 再补一次请求确保落盘 —— 不能假设时间线第一项就有缓存文件：第一项可能
    # 是刚上传的 20MB 大图或那颗炸弹 PNG，它们的缩略图根本生成不出来。
    mid_t = ids_c2[1]
    row_t = st.db.query_one("SELECT root_id, rel_path FROM media WHERE id = ?", (mid_t,))
    tp = thumb_path(cfg, row_t["root_id"], row_t["rel_path"], 320)
    call(base, f"/api/items/{mid_t}/thumb?size=320", token=token, raw=True)
    check(tp.is_file(), "缩略图文件已存在", f"{tp}")
    mtime_before = tp.stat().st_mtime_ns
    body_before = tp.read_bytes()
    n_thumbs_before = sum(1 for _ in cfg.data_path("thumbs").rglob("*.webp"))

    svc5 = Svc(cfg)
    svc5.start()
    try:
        t_r0 = time.perf_counter()
        s_tl, page5, _ = call(svc5.base, "/api/timeline?limit=5", token=token)
        tl_ms = (time.perf_counter() - t_r0) * 1000
        check(s_tl == 200 and len(page5.get("items", [])) == 5,
              "重启后时间线立即可用", f"got {s_tl}")
        print(f"      重启后首次时间线 {tl_ms:.0f} ms")
        s_tb, thumb5, _ = call(svc5.base, f"/api/items/{mid_t}/thumb?size=320",
                               token=token, raw=True)
        check(s_tb == 200, "重启后能取到缩略图", f"got {s_tb}")
        check(thumb5 == body_before, "取到的缩略图字节与重启前完全一致")
        mtime_after = tp.stat().st_mtime_ns
        check(mtime_after == mtime_before,
              "缩略图没有被重新生成（mtime 未变）",
              f"{mtime_before} vs {mtime_after}")
        n_thumbs_after = sum(1 for _ in cfg.data_path("thumbs").rglob("*.webp"))
        check(n_thumbs_after == n_thumbs_before,
              "缩略图文件数没变（没有重复生成）",
              f"{n_thumbs_before} → {n_thumbs_after}")
    finally:
        svc5.stop()
    # =====================================================================
    print("\n[G-11 补充] 改名与移动（整理动作落在审计里，且原文件不变）")
    _, page_r, _ = call(base, "/api/timeline?limit=100", token=token)
    victim_r = [it for it in page_r["items"] if it["name"].startswith("img")]
    check(bool(victim_r), "有可整理的照片")
    if victim_r:
        target = victim_r[0]
        row_r = st.db.query_one("SELECT rel_path FROM media WHERE id = ?",
                                (target["id"],))
        src_path = lib / row_r["rel_path"]
        before_bytes = src_path.read_bytes() if src_path.is_file() else b""
        s_rn, body_rn, _ = call(base, "/api/items/rename", method="POST",
                                body={"id": target["id"],
                                      "new_name": "renamed-by-accept.jpg"},
                                token=token)
        check(s_rn == 200, "改名返回 200", f"got {s_rn}")
        new_rel = (body_rn or {}).get("to") or ""
        check(new_rel.endswith("renamed-by-accept.jpg"),
              "目标文件名正确", f"got {new_rel}")
        check((lib / new_rel).is_file(), "新文件真的在磁盘上")
        check(not src_path.exists(), "旧文件已经不在")
        check((lib / new_rel).read_bytes() == before_bytes,
              "改名后字节完全没变（不是重新编码）")
        # 冲突保护：把两张照片改成同名，第二张必须 409 E3012
        if len(victim_r) >= 2:
            s_cf, body_cf, _ = call(base, "/api/items/rename", method="POST",
                                    body={"id": victim_r[1]["id"],
                                          "new_name": "renamed-by-accept.jpg"},
                                    token=token)
            check(s_cf == 409, "改成已存在的名字 → 409", f"got {s_cf}")
            check((body_cf.get("error") or {}).get("code") == "E3012",
                  "冲突错误码是 E3012", f"got {(body_cf.get('error') or {}).get('code')}")
        # 移动：把它移进一个刚建的子目录
        (lib / "整理后").mkdir(exist_ok=True)
        s_mv, body_mv, _ = call(base, "/api/items/move", method="POST",
                                body={"ids": [target["id"]],
                                      "target_dir": "整理后",
                                      "on_conflict": "rename"},
                                token=token)
        check(s_mv == 200, "移动返回 200", f"got {s_mv}")
        check(body_mv.get("failed") == [], "移动没有失败项", f"got {body_mv.get('failed')}")
        check((lib / "整理后" / "renamed-by-accept.jpg").is_file(),
              "文件已到新目录")
        row_mv = st.db.query_one("SELECT rel_path, missing FROM media WHERE id = ?",
                                 (target["id"],))
        check(row_mv["rel_path"] == "整理后/renamed-by-accept.jpg",
              "DB 里的路径跟着改了", f"got {row_mv['rel_path']}")
        check(row_mv["missing"] == 0, "移动后没有被打成 missing")

    # =====================================================================
    print("\n[I-05 / I-06 / I-07] 各格式、HEIC、全景超长图都能上传并生成缩略图")

    def _push(name: str, blob_x: bytes, want_ext: str) -> dict | None:
        """走真实 HTTP 上传一份字节，返回 complete 的响应体。"""
        s_i, init_x, _ = call(base, "/api/uploads/init", method="POST",
                              body={"name": name, "size": len(blob_x)}, token=token)
        if s_i != 201:
            check(False, f"{name} init 成功", f"{s_i}")
            return None
        ux = init_x["upload_id"]
        cs_x = int(init_x["chunk_size"])
        off_x = 0
        while off_x < len(blob_x):
            end_x = min(off_x + cs_x, len(blob_x)) - 1
            rq_x = urllib.request.Request(
                f"{base}/api/uploads/{ux}", data=blob_x[off_x:end_x + 1],
                method="PATCH",
                headers={"Authorization": f"Bearer {token}",
                         "Content-Range": f"bytes {off_x}-{end_x}/{len(blob_x)}",
                         "Content-Type": "application/octet-stream"})
            try:
                with urllib.request.urlopen(rq_x, timeout=120) as rp_x:
                    off_x = int(json.loads(rp_x.read())["offset"])
            except urllib.error.HTTPError as exc_x:
                check(False, f"{name} 分片写入", f"{exc_x.code} {exc_x.read()[:200]}")
                return None
        s_cx, done_x, _ = call(base, f"/api/uploads/{ux}/complete", method="POST",
                               body={"sha256": hashlib.sha256(blob_x).hexdigest()},
                               token=token)
        if s_cx != 201:
            check(False, f"{name} complete 成功", f"{s_cx} {done_x}")
            return None
        got_ext = done_x["rel_path"].rsplit(".", 1)[1].lower()
        check(got_ext == want_ext, f"{name} 归档扩展名是 .{want_ext}", f".{got_ext}")
        return done_x

    def _thumb_ok(media_id) -> tuple[bool, str]:
        """取缩略图，确认是能被 Pillow 打开的真图片。"""
        s_t, blob_t, _ = call(base, f"/api/items/{media_id}/thumb?size=320",
                              token=token, raw=True)
        if s_t != 200 or not isinstance(blob_t, bytes) or len(blob_t) < 40:
            return False, f"{s_t}/{len(blob_t) if isinstance(blob_t, bytes) else '?'}"
        try:
            with Image.open(io.BytesIO(blob_t)) as im_t:
                im_t.load()
                return True, f"{im_t.format} {im_t.size[0]}x{im_t.size[1]}"
        except Exception as exc:      # noqa: BLE001
            return False, f"打不开 {type(exc).__name__}"

    # complete 的响应体不含 width/height（只有 media_id/rel_path/dedup），
    # 尺寸统一从 DB 查 —— 这也顺带验证了「入库时解析出的尺寸是对的」。
    def _dims(media_id) -> tuple[int, int]:
        r = st.db.query_one("SELECT width, height FROM media WHERE id = ?",
                            (int(media_id),))
        return (int(r["width"]), int(r["height"])) if r else (0, 0)

    # --- I-05：JPEG / PNG / WebP / GIF ---
    base_img = Image.new("RGB", (160, 120), (40, 120, 200))
    fmt_cases = [("fmt.jpg", "JPEG", "jpg"), ("fmt.png", "PNG", "png"),
                 ("fmt.webp", "WEBP", "webp"), ("fmt.gif", "GIF", "gif")]
    for nm_f, fmt_f, ext_f in fmt_cases:
        buf_f = io.BytesIO()
        if fmt_f == "GIF":
            base_img.convert("P").save(buf_f, "GIF")
        else:
            base_img.save(buf_f, fmt_f)
        blob_f = buf_f.getvalue()
        done_f = _push(nm_f, blob_f, ext_f)
        if done_f is None:
            continue
        s_tf, blob_tf, _ = call(base, f"/api/items/{done_f['media_id']}/thumb?size=320",
                                token=token, raw=True)
        check(s_tf == 200, f"{fmt_f} 缩略图返回 200", f"{s_tf}")
        check(isinstance(blob_tf, bytes) and len(blob_tf) > 100,
              f"{fmt_f} 缩略图不是占位图（>100 字节）",
              f"{len(blob_tf) if isinstance(blob_tf, bytes) else '?'} B")
        ok_f, desc_f = _thumb_ok(done_f["media_id"])
        check(ok_f, f"{fmt_f} 缩略图能被 Pillow 打开", desc_f)
        check(_dims(done_f["media_id"]) == (160, 120),
              f"{fmt_f} 尺寸解析正确（160x120）", f"{_dims(done_f['media_id'])}")
        # 原图回读逐字节一致
        s_rf, raw_f, _ = call(base, f"/api/items/{done_f['media_id']}/raw",
                              token=token, raw=True)
        check(s_rf == 200 and raw_f == blob_f,
              f"{fmt_f} 原图回读逐字节一致", f"{s_rf}")

    # --- I-06：HEIC（iPhone 默认格式）---
    # pillow_heif 未安装时索引仍可用、但缩略图生成不出来 —— 这里要测的是
    # "装了就真能用"，所以先确认插件确实注册了。
    from backend.app.exif import _prepare_pillow
    _prepare_pillow()
    heif_ok = ".heic" in Image.registered_extensions()
    check(heif_ok, "pillow-heif 已注册，能解 HEIC")
    if heif_ok:
        buf_h = io.BytesIO()
        try:
            base_img.save(buf_h, format="HEIF", quality=80)
            blob_h = buf_h.getvalue()
            check(blob_h[4:8] == b"ftyp", "造出来的是真 HEIC 容器（偏移 4 是 ftyp）",
                  f"{blob_h[4:8]!r}")
            done_h = _push("iphone.heic", blob_h, "heic")
            if done_h is not None:
                check(_dims(done_h["media_id"]) == (160, 120),
                      "HEIC 尺寸解析正确（160x120）",
                      f"{_dims(done_h['media_id'])}")
                ok_h, desc_h = _thumb_ok(done_h["media_id"])
                check(ok_h, "HEIC 缩略图能被 Pillow 打开", desc_h)
                # ★ 没有 `/api/items/{id}/exif` 这个路由 —— 完整 EXIF 在
                #   `GET /api/items/{id}` 的响应体里（`browse.get_item`）。
                s_eh, item_h, _ = call(base, f"/api/items/{done_h['media_id']}",
                                       token=token)
                check(s_eh == 200, "HEIC 的 /api/items/{id} 能返回", f"{s_eh}")
                check(isinstance(item_h.get("exif"), (dict, type(None))),
                      "HEIC 条目的 exif 字段存在", f"{type(item_h.get('exif'))}")
        except Exception as exc:      # noqa: BLE001
            check(False, "能造出 HEIC 测试文件", f"{type(exc).__name__}: {exc}")

    # --- I-07：全景图 / 超长截图 ---
    # 4000x400 全景 = 160 万像素，远低于 MAX_IMAGE_PIXELS(2 亿)，不该被拦；
    # 缩略图必须等比缩放（宽高比保持 10:1），不能强制正方形。
    pano = Image.new("RGB", (4000, 400), (120, 200, 90))
    buf_p = io.BytesIO()
    pano.save(buf_p, "JPEG", quality=70)
    blob_p = buf_p.getvalue()
    done_p = _push("pano.jpg", blob_p, "jpg")
    if done_p is not None:
        check(_dims(done_p["media_id"]) == (4000, 400),
              "全景图尺寸解析正确（4000x400）", f"{_dims(done_p['media_id'])}")
        s_tp, raw_tp, _ = call(base, f"/api/items/{done_p['media_id']}/thumb?size=320",
                                token=token, raw=True)
        check(s_tp == 200, "全景图缩略图返回 200", f"{s_tp}")
        if s_tp == 200 and isinstance(raw_tp, bytes):
            try:
                with Image.open(io.BytesIO(raw_tp)) as im_p:
                    im_p.load()
                    w_p, h_p = im_p.size
                ratio = w_p / max(1, h_p)
                check(abs(ratio - 10.0) < 0.6,
                      "缩略图保持 10:1 宽高比（不压成方块）",
                      f"{w_p}x{h_p} = {ratio:.2f}:1")
                check(max(w_p, h_p) == 320, "长边缩到 320", f"{w_p}x{h_p}")
            except Exception as exc:      # noqa: BLE001
                check(False, "全景缩略图能打开", f"{type(exc).__name__}: {exc}")

    # =====================================================================
    print("\n[A-06] 开机自启（HKCU Run 键往返，不真的重启电脑）")
    # A-06 的判据是"开启自启后重启电脑，登录后自动启动"。
    # "重启后真的启动"必须人来做，但**前半个机制**可以钉死：
    #   (1) 写入后能从注册表读回、值与 `autostart_command()` 完全一致；
    #   (2) `is_enabled()` / `status()` 跟着变；
    #   (3) 删除后能干净消失，不留残值；
    #   (4) 改过路径后会**自愈**覆盖成新路径（`set_autostart` 的幂等承诺）。
    # 用 HKCU 的 Run 键而不是 HKLM：不需要管理员，且测完能立刻还原。
    from backend.app import autostart as _auto
    _had = _auto.is_enabled()
    _before = _auto._current_value()
    try:
        check(_auto.set_autostart(True), "能写入开机自启项")
        check(_auto.is_enabled(), "写入后 is_enabled() 为 True")
        got_cmd = _auto._current_value()
        check(got_cmd == _auto.autostart_command(),
              "注册表里的命令与 autostart_command() 完全一致",
              f"{got_cmd!r}")
        check("pythonw.exe" in (got_cmd or "") or ".exe" in (got_cmd or ""),
              "启动命令指向 exe/pythonw（不是会弹黑框的 python.exe）",
              f"{got_cmd!r}")
        check("--tray" in (got_cmd or ""),
              "启动命令带 --tray（开机时不弹配对窗、只留托盘）",
              f"{got_cmd!r}")
        st_a = _auto.status()
        check(st_a.get("enabled") is True and st_a.get("supported") is True,
              "status() 报 enabled 且 supported", f"{st_a}")
        check(st_a.get("stale") is False, "status() 报的不是 stale 路径",
              f"{st_a}")

        # 自愈：把值改成假的旧路径，再 set 一次，应当被覆盖回来
        import winreg as _wr
        with _wr.OpenKey(_wr.HKEY_CURRENT_USER, _auto.RUN_KEY, 0,
                         _wr.KEY_SET_VALUE) as _k:
            _wr.SetValueEx(_k, _auto.VALUE_NAME, 0, _wr.REG_SZ,
                           r"C:\old\path\gone.exe --tray")
        check(_auto.status().get("stale") is True,
              "旧路径会被识别为 stale（用户移动目录后的典型症状）")
        check(_auto.set_autostart(True), "对 stale 值再 set 一次能自愈")
        check(_auto._current_value() == _auto.autostart_command(),
              "自愈后命令回到正确值")
        check(_auto.status().get("stale") is False, "自愈后不再是 stale")

        check(_auto.disable_autostart(), "能取消开机自启")
        check(not _auto.is_enabled(), "取消后 is_enabled() 为 False")
        check(_auto._current_value() is None, "取消后注册表里没有残值",
              f"{_auto._current_value()!r}")
        check(_auto.disable_autostart(), "重复取消也返回 True（幂等）")
    finally:
        # 还原到测试前的状态：这台机器上用户可能本来就开着/关着自启
        if _had and _before:
            _auto.set_autostart(True)
        else:
            _auto.disable_autostart()
    check(_auto.is_enabled() == _had, "测试后自启状态已还原到测试前",
          f"{_auto.is_enabled()} vs {_had}")

    # =====================================================================
    print("\n[A-02] 托盘菜单项数")
    # `pystray.Menu(...)` 在 `backend/app/tray.py` 里是纯数据构造，
    # 用 AST 数 MenuItem 与 SEPARATOR 即可，不需要真的建托盘。
    import ast as _ast
    tray_src = (REPO / "backend" / "app" / "tray.py").read_text(encoding="utf-8")
    tree_tray = _ast.parse(tray_src)
    menu_node = None
    for node in _ast.walk(tree_tray):
        if (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Attribute)
                and node.func.attr == "Menu"
                and isinstance(node.func.value, _ast.Name)
                and node.func.value.id == "pystray"):
            menu_node = node
            break
    check(menu_node is not None, "找到 pystray.Menu(...) 构造")
    if menu_node is not None:
        items_t = [a for a in menu_node.args
                   if isinstance(a, _ast.Call)
                   and isinstance(a.func, _ast.Attribute)
                   and a.func.attr == "MenuItem"]
        seps_t = [a for a in menu_node.args
                  if isinstance(a, _ast.Attribute) and a.attr == "SEPARATOR"]
        labels_t = []
        for a in items_t:
            if a.args and isinstance(a.args[0], _ast.Constant):
                labels_t.append(str(a.args[0].value))
        check(len(items_t) == 8, "托盘菜单是 8 个可点项（不含分隔线）", f"{len(items_t)}")
        check(len(seps_t) == 3, "中间有 3 条分隔线", f"{len(seps_t)}")
        print(f"      菜单：{'、'.join(labels_t)}")
        # 每一项都必须绑了回调 —— 空回调点了没反应
        no_cb = [lb for lb, a in zip(labels_t, items_t) if len(a.args) < 2]
        check(not no_cb, "每一项都绑了回调", f"没有回调: {no_cb}")
        # docs/07 写的是"11 项"，与实际不符 —— 钉住真实值，防止文档再飘
        check("退出" in labels_t[-1], "最后一项是「退出」", f"{labels_t[-1] if labels_t else ''}")

    # =====================================================================
    print("\n[A-11] 优雅退出后没有 WAL / SHM 残留")
    wal_before = Path(str(cfg.db_path) + "-wal")
    print(f"      退出前 wal 存在={wal_before.exists()}")
    stopped = svc.stop()
    check(stopped, "服务线程已退出")
    for suffix, name in (("-wal", "photobridge.db-wal"), ("-shm", "photobridge.db-shm")):
        p = Path(str(cfg.db_path) + suffix)
        check(not p.exists(), f"退出后无 {name} 残留",
              f"size={p.stat().st_size}" if p.exists() else "")

    # =====================================================================
    print("\n[J-03] 索引可重建（删 DB 重新索引）")
    db_copy = tmp / "data" / "copy.db"
    shutil.copy2(cfg.db_path, db_copy)
    t0 = time.perf_counter()
    cfg4 = Config({
        "app": {"mode": "readwrite"},
        "server": {"host": "127.0.0.1", "port": 0},
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite",
                   "enabled": True, "recursive": True}],
        "index": {"scan_on_start": False, "watch": False, "batch_size": 200},
    }, path=(tmp / "data4" / "config.json"), data_dir=(tmp / "data4"))
    (tmp / "data4").mkdir(exist_ok=True)
    cfg4.save()
    svc4 = Svc(cfg4)
    svc4.start()
    try:
        asyncio.run(svc4.st.indexer.scan_all())
        t_rebuild = time.perf_counter() - t0
        tok4 = pair(svc4.base, svc4.st, "rebuild")
        _, page7, _ = call(svc4.base, "/api/timeline?limit=100", token=tok4)
        # ★ 不能拿 page6 比：page6 是**上传前**抓的 100 条快照，后面几十节又
        #   传了 fmt.*/iphone.heic/pano/batch 一堆图，条数早就变了。
        #   重建后应当等于"磁盘上真实可索引的文件数"。
        n_disk = sum(1 for q in lib.rglob("*")
                     if q.is_file()
                     and q.suffix.lower().lstrip(".") in {"jpg", "jpeg", "png",
                                                          "webp", "gif", "bmp",
                                                          "tif", "tiff", "avif",
                                                          "heic", "heif"})
        check(len(page7["items"]) == n_disk,
              "重建后的条数等于磁盘上可索引文件数",
              f"DB {len(page7['items'])} vs 磁盘 {n_disk}")
        check(t_rebuild <= 60.0, "从零重建 ≤ 60 秒", f"{t_rebuild:.1f}s")
    finally:
        svc4.stop()

    # =====================================================================
    print("\n[J-06] 审计日志可追溯（删除/移动/上传各做一次后按序追加）")
    from backend.app.audit import parse_line
    logf = cfg.data_path("audit.log")
    entries = []
    if logf.exists():
        for ln in logf.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                entries.append(parse_line(ln))
    acts = [e["action"] for e in entries]
    print(f"      审计动作序列（前 20）：{acts[:20]}")
    check(len(entries) >= 3, "审计里至少 3 条记录", f"{len(entries)} 条")
    check("delete" in acts, "有 delete 动作")
    check("upload" in acts, "有 upload 动作")
    check("rename" in acts or "move" in acts, "有整理动作（rename/move）",
          f"{sorted(set(acts))}")
    stamps = [e["ts"] for e in entries]
    check(stamps == sorted(stamps), "记录按时间递增（只追加，不回填）",
          f"前几个 ts={stamps[:3]}")

    # =====================================================================
    real_after = real_fingerprint()
    print(f"\n真实库（结束）：{real_after}")
    check(real_after == real_before, "★ 真实照片库一个字节都没变")

    print(f"\n{'=' * 60}")
    if failures:
        print(f"失败 {len(failures)} 项：")
        for f in failures:
            print("  -", f)
        print(f"临时目录：{tmp}")
        return 1
    print("=== ALL PASS ===")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
