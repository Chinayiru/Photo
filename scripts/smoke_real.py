"""真实库冒烟测试：拿 D:\\Photos 跑一遍完整服务。

与单测的区别：这里用的是**真实的 4915 张照片**和**真实的 HTTP 栈**
（真起 uvicorn、真发请求），能发现单测的假数据掩盖不了的问题
（编码、真实 EXIF 形态、真实目录深度、真实数据量下的性能）。

只读地使用照片库；数据库与缩略图写到临时目录，不污染真实 data/。

用法：
    .venv\\Scripts\\python.exe scripts\\smoke_real.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

LIBRARY = Path(r"D:\Photos")

from backend.app.config import Config                      # noqa: E402
from backend.app.db import Database                        # noqa: E402
from backend.app.events import get_bus                     # noqa: E402
from backend.app.indexer import Indexer                    # noqa: E402
from backend.app.server import build_app                   # noqa: E402
from backend.app.state import AppState                     # noqa: E402
from backend.app.thumbs import ThumbEngine                 # noqa: E402

PASS = 0
FAIL = 0


class _H(dict):
    """大小写不敏感的头视图。

    HTTP 头名按 RFC 7230 大小写不敏感，而 Starlette/uvicorn 一律发
    小写 —— 直接 `dict(headers).get("Content-Range")` 永远返回 None，
    测出来的是"我们怎么拼头名"而不是"头有没有发出去"。
    """

    def __init__(self, src) -> None:
        super().__init__({str(k).lower(): v for k, v in dict(src).items()})

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def __contains__(self, key) -> bool:
        return super().__contains__(str(key).lower())


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {extra}")


class Service:
    """在后台线程里跑完整服务，并允许主线程往那个循环里投协程。"""

    def __init__(self, cfg: Config, db: Database) -> None:
        self.cfg = cfg
        self.db = db
        self.loop: asyncio.AbstractEventLoop | None = None
        self.port = 0
        self.state: AppState | None = None
        self._ready = threading.Event()
        self._stop: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self, timeout: float = 30.0) -> None:
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("服务启动超时")

    def call(self, coro, timeout: float = 600.0):
        """把协程投到服务线程的事件循环里执行。"""
        assert self.loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        if self.loop is not None and self._stop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(timeout=15)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop

        async def main_async() -> None:
            bus = get_bus()
            bus.bind_loop(loop)
            thumbs = ThumbEngine(self.cfg, self.db, workers=4, bus=bus)
            indexer = Indexer(self.cfg, self.db, thumbs=thumbs, bus=bus)
            st = AppState(self.cfg, self.db, thumbs=thumbs, indexer=indexer, bus=bus)
            _, transport, _ = build_app(self.cfg, state=st)
            transport.port = 0
            await transport.start()
            self.port = transport.port_actual
            self.state = st
            self._stop = asyncio.Event()
            self._ready.set()
            await self._stop.wait()
            await transport.stop()

        try:
            loop.run_until_complete(main_async())
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:       # noqa: BLE001
                pass
            loop.close()


def main() -> int:
    if not LIBRARY.is_dir():
        print(f"照片库不存在：{LIBRARY}")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="pb_smoke_"))
    print(f"临时数据目录：{tmp}")
    print(f"照片库：{LIBRARY}")

    cfg = Config({
        "server": {"host": "127.0.0.1", "port": 0, "allow_lan": False},
        "roots": [{
            "id": "library", "label": "我的照片库", "path": str(LIBRARY),
            "mode": "readwrite", "enabled": True, "recursive": True,
        }],
        "auth": {"require_pairing": False, "allow_delete": False},
        "index": {"scan_on_start": False, "watch": False},
    }, path=tmp / "config.json", data_dir=tmp / "data")

    db = Database(cfg.db_path)
    db.init()
    svc = Service(cfg, db)
    svc.start()
    base = f"http://127.0.0.1:{svc.port}"
    assert svc.state is not None

    def get(path: str, *, raw: bool = False, headers: dict | None = None):
        req = urllib.request.Request(base + path, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                body = r.read()
                return r.status, _H(r.headers), (body if raw else _json(body))
        except urllib.error.HTTPError as e:
            body = e.read()
            return e.code, _H(e.headers), (body if raw else _json(body))

    def _json(b: bytes):
        try:
            return json.loads(b)
        except Exception:           # noqa: BLE001
            return {"_raw": b[:200].decode("utf-8", "replace")}

    try:
        print("\n[1] 索引真实库")
        t0 = time.time()
        stats = svc.call(svc.state.indexer.scan_all())
        dur = time.time() - t0
        s = stats[0]
        print(f"     {s.summary()}")
        total = db.scalar("SELECT COUNT(*) FROM media")
        check("索引出照片", total > 4000, f"total={total}")
        check("首轮耗时 < 10 分钟（AC-2.1）", dur < 600, f"{dur:.1f}s")
        check("视频未被索引",
              db.scalar("SELECT COUNT(*) FROM media WHERE ext IN ('mov','mp4')") == 0)
        check("小米回收站残留未被索引",
              db.scalar("SELECT COUNT(*) FROM media WHERE ext LIKE '%lx2%'") == 0)
        print(f"     taken_src: " + ", ".join(
            f"{r['taken_src']}={r['n']}" for r in
            db.query("SELECT taken_src, COUNT(*) n FROM media GROUP BY taken_src")))
        print(f"     扩展名: " + ", ".join(
            f"{r['ext']}={r['n']}" for r in
            db.query("SELECT ext, COUNT(*) n FROM media GROUP BY ext ORDER BY n DESC")))

        print("\n[2] 幂等重扫")
        t0 = time.time()
        s2 = svc.call(svc.state.indexer.scan_all())
        r2 = time.time() - t0
        check("重扫无新增", s2[0].added == 0, f"added={s2[0].added}")
        check("重扫未变等于总数", s2[0].unchanged == total,
              f"{s2[0].unchanged} vs {total}")
        check("重扫耗时 < 5s", r2 < 5, f"{r2:.2f}s")

        print("\n[3] 缩略图（真实 HEIC / 大图 / 中文路径）")
        rows = db.query(
            "SELECT id, rel_path FROM media ORDER BY id LIMIT 40")
        ok = 0
        sizes: list[int] = []
        for row in rows:
            code, _, body = get(f"/api/items/{row['id']}/thumb?size=320", raw=True)
            if code == 200 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
                ok += 1
                sizes.append(len(body))
            else:
                print(f"     FAIL id={row['id']} {row['rel_path'][:60]} -> {code}")
        check("前 40 张缩略图全部生成", ok == len(rows), f"{ok}/{len(rows)}")
        if sizes:
            check("缩略图平均 < 60KB", statistics.mean(sizes) < 60_000,
                  f"{statistics.mean(sizes):.0f}B")
            print(f"     平均 {statistics.mean(sizes)/1024:.1f}KB，"
                  f"最大 {max(sizes)/1024:.1f}KB")

        print("\n[4] 时间线（真实数据量）")
        times = []
        for _ in range(20):
            t0 = time.perf_counter()
            code, _, page = get("/api/timeline?limit=100")
            times.append((time.perf_counter() - t0) * 1000)
        check("时间线 200", code == 200)
        check("首屏 100 条", len(page.get("items", [])) == 100)
        times.sort()
        print(f"     首页平均 {statistics.mean(times):.1f}ms  "
              f"P95 {times[int(len(times) * 0.95)]:.1f}ms  最大 {times[-1]:.1f}ms")

        seen: list[int] = []
        cur = None
        pages = 0
        t0 = time.time()
        while True:
            q = "/api/timeline?limit=500" + (f"&cursor={cur}" if cur else "")
            _, _, pg = get(q)
            seen.extend(i["id"] for i in pg["items"])
            pages += 1
            cur = pg.get("next_cursor")
            if not pg.get("has_more") or pages > 60:
                break
        check("翻页无重复", len(seen) == len(set(seen)),
              f"{len(seen)} vs {len(set(seen))}")
        check("翻页覆盖全库", len(seen) == total, f"{len(seen)} vs {total}")
        print(f"     {pages} 页 / {len(seen)} 条 / {time.time()-t0:.2f}s")

        print("\n[5] 排序正确性")
        _, _, pg = get("/api/timeline?limit=500")
        ts = [i["taken_at"] for i in pg["items"]]
        check("严格倒序", all(ts[i] >= ts[i + 1] for i in range(len(ts) - 1)))

        print("\n[6] 详情 / 原图 Range")
        # 取中间一张：最新那张没有 prev（prev 是更新的邻居），
        # 拿它断言 prev_id 存在必然失败，而代码其实是对的。
        mid = seen[len(seen) // 2]
        code, _, detail = get(f"/api/items/{mid}")
        check("详情 200", code == 200)
        check("详情有 exif 段", "exif" in detail)
        check("详情有 urls 段", set(detail.get("urls", {})) == {"thumb", "preview", "raw"})
        check("prev_id 存在", detail.get("prev_id") is not None)
        check("next_id 存在", detail.get("next_id") is not None)
        check("taken_at_iso 带时区",
              (detail.get("taken_at_iso") or "").count(":") >= 2
              and ("+" in (detail.get("taken_at_iso") or "")
                   or (detail.get("taken_at_iso") or "").endswith("Z")),
              detail.get("taken_at_iso"))

        _, _, full = get(f"/api/items/{mid}/raw", raw=True)
        size = len(full)
        code, hdrs, part = get(f"/api/items/{mid}/raw", raw=True,
                               headers={"Range": "bytes=10-19"})
        check("Range 206", code == 206, f"{code}")
        check("Range 内容正确", part == full[10:20],
              f"{part!r} vs {full[10:20]!r}")
        check("Content-Range 正确",
              hdrs.get("Content-Range") == f"bytes 10-19/{size}",
              hdrs.get("Content-Range"))

        print("\n[7] 搜索与目录（中文）")
        code, _, res = get("/api/search?q=" + urllib.parse.quote("敦煌"))
        check("中文搜索 200", code == 200, str(res)[:120])
        print(f"     '敦煌' 命中 {len(res.get('items', []))} 条")
        code, _, res2 = get("/api/search?q=" + urllib.parse.quote("青甘"))
        check("搜索青甘 200", code == 200, str(res2)[:120])
        code, _, folders = get("/api/folders?depth=1")
        check("目录 200", code == 200)
        names = [f["name"] for f in folders.get("folders", [])]
        print(f"     根目录 {names}")
        check("看到真实子目录", len(names) >= 2, str(names))

        print("\n[8] 静态前端")
        code, hdrs, body = get("/", raw=True)
        if code == 200:
            check("根路径返回 HTML",
                  b"<html" in body.lower() or b"<!doctype" in body.lower())
        else:
            print(f"     （前端尚未构建，跳过；GET / -> {code}）")

        print("\n[9] 错误不泄漏路径")
        code, _, err = get("/api/items/99999999")
        blob = json.dumps(err, ensure_ascii=False)
        check("不存在回 404", code == 404, f"{code}")
        check("不含库根路径", "software" not in blob and "相册" not in blob)
        check("不含堆栈", "Traceback" not in blob)

        print("\n[10] SSE")
        code, hdrs, body = get("/api/events?once=1", raw=True)
        check("SSE once 200", code == 200, f"{code}")
        check("SSE 内容类型正确",
              hdrs.get("Content-Type", "").startswith("text/event-stream"),
              hdrs.get("Content-Type"))
        check("SSE 首帧是 hello", body.startswith(b"event: hello"), repr(body[:40]))
        check("SSE 不缓存", hdrs.get("Cache-Control") == "no-store")

    finally:
        svc.stop()
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 60}\n通过 {PASS}，失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
