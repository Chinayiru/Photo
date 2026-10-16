"""端到端验证：**用真实浏览器把手机端网页跑一遍**。

## 为什么需要它

`frontend/` 到目前为止只被 `vue-tsc --noEmit`（类型检查）和 `vite build`
（打包）验证过 —— 也就是说，**这个网页的 JavaScript 从来没有真正执行过**。
类型检查能证明"类型对得上"，打包能证明"模块能解析"，但都不能证明：

  * 运行时是否抛异常（`undefined.map`、响应字段名拼错、store 初始化顺序）
  * 首屏是否真的渲染出内容（不是白屏）
  * 配对流程是否真的能走通（表单 → 请求 → localStorage → 切到时间线）
  * 时间线的缩略图 `<img src>` 是否真的能加载出像素

这类缺陷在类型层面完全看不出来，而且症状是"手机上打开是白屏"，
用户无法自助排查。所以这里用 **headless Chrome + CDP** 真的跑一遍。

## 判定标准

全部用**浏览器里可观测的事实**判定，不读内部对象：
  * `document.querySelector` 能查到元素（渲染出来了）
  * `Runtime.evaluate` 拿到的 `localStorage` 值（配对真的落盘了）
  * `<img>` 的 `naturalWidth > 0`（缩略图真的解码出了像素，
    而不是 404 占位 —— 后者同样有 `src`，但 `naturalWidth` 是 0）
  * `window.__errors` 里没有未捕获异常（前端没有任何 JS 报错）

## 安全边界

全程在 `tempfile.mkdtemp()` 造的合成照片库里操作，**绝不碰真实照片库**。
脚本结束校验真实库指纹与开始时一致。
"""
from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.app.config import Config  # noqa: E402
from backend.app.server import serve  # noqa: E402

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

failures: list[str] = []


def check(ok: bool, label: str, extra: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {extra}" if extra else ""))
    if not ok:
        failures.append(label)


def find_browser() -> str | None:
    for c in CHROME_CANDIDATES:
        if Path(c).exists():
            return c
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


# --------------------------------------------------------------------------
# 极简 CDP 客户端（不装 websocket 依赖之外的东西：本项目已有 websockets）
# --------------------------------------------------------------------------

class Browser:
    """headless Chrome 的一个标签页，通过 CDP 驱动。"""

    def __init__(self, exe: str, profile: Path, port: int) -> None:
        self.port = port
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--disable-extensions",
             f"--remote-debugging-port={port}",
             f"--user-data-dir={profile}",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.ws = None
        self._id = 0
        self._wait_devtools()

    def _wait_devtools(self, budget: float = 30.0) -> None:
        end = time.time() + budget
        while time.time() < end:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/version", timeout=2) as r:
                    json.loads(r.read())
                    return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("DevTools 端口没起来")

    def targets(self) -> list[dict]:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/list", timeout=5) as r:
            return json.loads(r.read())

    def connect(self) -> None:
        import asyncio

        import websockets

        pages = [t for t in self.targets() if t.get("type") == "page"]
        if not pages:
            raise RuntimeError("没有可用的 page target")
        url = pages[0]["webSocketDebuggerUrl"]

        self._loop = asyncio.new_event_loop()
        self.ws = self._loop.run_until_complete(websockets.connect(
            url, max_size=64 * 1024 * 1024))

    def send(self, method: str, **params):
        self._id += 1
        mid = self._id
        self._loop.run_until_complete(self.ws.send(json.dumps(
            {"id": mid, "method": method, "params": params})))
        while True:
            raw = self._loop.run_until_complete(self.ws.recv())
            msg = json.loads(raw)
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def evaluate(self, expr: str):
        r = self.send("Runtime.evaluate", expression=expr,
                      returnByValue=True, awaitPromise=True)
        res = r.get("result", {})
        if r.get("exceptionDetails"):
            raise RuntimeError(f"JS 抛异常: {r['exceptionDetails']}")
        return res.get("value")

    def goto(self, url: str) -> None:
        self.send("Page.enable")
        self.send("Page.navigate", url=url)

    def enable_touch(self) -> None:
        """开启触摸模拟。

        ⚠️ 不开启的话 `Input.dispatchTouchEvent` 会被静默忽略 ——
        `document` 上不会产生任何 touchstart/touchmove/touchend，
        手势代码一行都不执行，而脚本却"成功"跑完并全部通过。
        这是最容易产出假绿的地方，所以下面每个手势断言都先确认
        事件真的到达了（用 `window.__touched` 计数器）。
        """
        self.send("Emulation.setTouchEmulationEnabled",
                  enabled=True, maxTouchPoints=5)

    def touch(self, kind: str, points: list[tuple[float, float]]) -> None:
        """派发一次触摸事件。kind ∈ touchStart/touchMove/touchEnd/touchCancel。"""
        self.send("Input.dispatchTouchEvent",
                  type=kind,
                  touchPoints=[{"x": x, "y": y} for x, y in points])

    def wait_for(self, expr: str, *, budget: float = 20.0, label: str = "") -> bool:
        end = time.time() + budget
        while time.time() < end:
            try:
                if self.evaluate(expr):
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def close(self) -> None:
        try:
            if self.ws is not None:
                self._loop.run_until_complete(self.ws.close())
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def make_image(path: Path, color: tuple[int, int, int], size=(240, 180),
               *, gps: tuple[float, float] | None = None) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    exif = None
    if gps is not None:
        # 真的写 GPS EXIF（而不是往库里塞假数据）：`viewer.ts: gpsMapUrl`
        # 依赖后端从 EXIF 解析出的 `gps_lat/lon`。塞库只能测到"前端会渲染
        # 一个数字"，测不到"EXIF 的度分秒被正确换算成十进制度" ——
        # 而换算正是 AC-2.12 真正会出错的地方。
        from PIL.ExifTags import GPS

        lat, lon = gps

        def dms(v: float) -> tuple[float, float, float]:
            d = int(abs(v))
            m = int((abs(v) - d) * 60)
            return (float(d), float(m), round((abs(v) - d - m / 60) * 3600, 4))

        exif = Image.Exif()
        exif[0x8825] = {
            GPS.GPSLatitudeRef: "N" if lat >= 0 else "S",
            GPS.GPSLatitude: dms(lat),
            GPS.GPSLongitudeRef: "E" if lon >= 0 else "W",
            GPS.GPSLongitude: dms(lon),
        }
    im = Image.new("RGB", size, color)
    # ⚠️ 不能传 `exif=None`：Pillow 的 JPEG 编码器会直接 `len(exif)`
    # 抛 TypeError（它在判空之前就取长度了）。
    if exif is None:
        im.save(path, "JPEG", quality=88)
    else:
        im.save(path, "JPEG", quality=88, exif=exif)


def main() -> int:
    print("=" * 60)
    print("=== 真实浏览器端到端验证（手机端网页的 JS 真的会跑吗）===")
    print("=" * 60)

    exe = find_browser()
    if not exe:
        print("SKIP 本机没有 Chrome/Edge，跳过")
        return 0
    print(f"浏览器: {exe}")
    fp0 = real_fingerprint()
    print(f"真实库（开始）: {fp0}")

    tmp = Path(tempfile.mkdtemp(prefix="pb_browser_"))
    lib = tmp / "lib"
    port = free_port()
    data = tmp / "data"
    data.mkdir(parents=True)

    # 造 3 张图，让时间线有内容可渲染。
    # a.jpg 带 GPS：AC-2.12 要求详情面板显示坐标并可跳地图
    make_image(lib / "a.jpg", (200, 40, 40), gps=(38.800691, 93.577734))
    make_image(lib / "b.jpg", (40, 160, 60))
    make_image(lib / "sub" / "c.jpg", (40, 60, 200))

    cfg_raw = {
        "server": {"port": port, "transport": "local_http", "bind": "0.0.0.0"},
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite",
                   "enabled": True}],
    }
    (data / "config.json").write_text(json.dumps(cfg_raw), encoding="utf-8")
    cfg = Config(cfg_raw, path=data / "config.json", data_dir=data)
    cfg.save()

    state: dict = {}
    ready = threading.Event()

    async def runner() -> None:
        def on_ready(transport, st, _loop, _stop):
            state["st"] = st
            state["base"] = transport.urls[0]
            state["stop"] = _stop
            ready.set()

        await serve(cfg, on_ready=on_ready)

    def thread_main() -> None:
        import asyncio

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        state["loop"] = loop
        loop.run_until_complete(runner())

    t = threading.Thread(target=thread_main, daemon=True, name="browser-e2e")
    t.start()
    if not ready.wait(90):
        print("FAIL 服务 90 秒内未就绪")
        return 2
    base = state["base"]
    st = state["st"]
    print(f"服务: {base}")

    # 等首扫完成，否则时间线是空的（那会让"渲染出来了吗"无法判定）
    for _ in range(120):
        if st.indexer.running:
            time.sleep(0.5)
        else:
            break
    time.sleep(1.0)

    # 拿配对码：直接调本机面板接口（模拟用户在电脑上点"生成配对码"）
    with urllib.request.urlopen(urllib.request.Request(
            f"{base}/panel/pair-code", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"}), timeout=10) as r:
        pc = json.loads(r.read())
    code = pc["code"]
    print(f"配对码: {code}")

    br = Browser(exe, tmp / "profile", free_port())
    try:
        br.connect()
        br.send("Runtime.enable")
        br.send("Log.enable")

        # 注入错误收集器：任何未捕获异常都记下来
        br.send("Page.enable")
        # 触摸事件计数器：用来证明 `Input.dispatchTouchEvent` 真的送到了页面。
        # 没有它，一个"事件根本没派发"的环境会让所有手势断言凭空通过。
        br.send("Page.addScriptToEvaluateOnNewDocument", source="""
            window.__touch = {start: 0, move: 0, end: 0, cancel: 0};
            window.addEventListener('touchstart', () => window.__touch.start++, true);
            window.addEventListener('touchmove', () => window.__touch.move++, true);
            window.addEventListener('touchend', () => window.__touch.end++, true);
            window.addEventListener('touchcancel', () => window.__touch.cancel++, true);
        """)
        br.send("Page.addScriptToEvaluateOnNewDocument", source="""
            window.__errors = [];
            window.addEventListener('error', e => {
              window.__errors.push(String(e.message || e.error));
            });
            window.addEventListener('unhandledrejection', e => {
              window.__errors.push('unhandled: ' + String(e.reason));
            });
        """)
        # ★ SSE 观测器：包一层 `EventSource` 记录**浏览器真的收到了什么**。
        #
        # 为什么不查前端 store：那等于用自己的实现验证自己的实现。
        # 这里记录的是原生 SSE 事件对象，与应用代码无关 —— 服务端到底
        # 有没有把 `scan_progress` / `media_changed` 推过网络，一目了然。
        # 同时记下 URL，用来证明 token 没有拼进查询串（AC 的安全要求）。
        br.send("Page.addScriptToEvaluateOnNewDocument", source="""
            window.__sse = {urls: [], events: []};
            (() => {
              const Native = window.EventSource;
              if (!Native) return;
              function Wrapped(url, opts) {
                window.__sse.urls.push(String(url));
                const es = new Native(url, opts);
                // 命名帧必须逐类型监听：服务端用的是 `event: <type>`。
                for (const t of ['hello','ping','scan_start','scan_progress',
                                 'scan_done','thumb_progress','upload_done',
                                 'media_changed','index_changed']) {
                  es.addEventListener(t, ev => {
                    let data = null;
                    try { data = JSON.parse(ev.data); } catch (e) { data = null; }
                    window.__sse.events.push({type: t, data: data,
                                              at: Date.now()});
                  });
                }
                return es;
              }
              Wrapped.prototype = Native.prototype;
              Object.assign(Wrapped, {CONNECTING: 0, OPEN: 1, CLOSED: 2});
              window.EventSource = Wrapped;
            })();
        """)

        # ---------------------------------------------------------------
        print("\n[1] 首屏渲染（未配对 → 应当看到配对页）")
        # ---------------------------------------------------------------
        br.goto(base + "/")
        ok = br.wait_for(
            "!!document.querySelector('input[placeholder*=\"192.168\"]')",
            budget=25, label="配对页输入框")
        check(ok, "配对页渲染出来了（JS 真的执行了）")
        title = br.evaluate("document.title")
        check(bool(title), f"页面标题非空  got {title!r}")
        # ★ 关键：Vue 挂载前 #app 是空的。如果 JS 抛异常，这里会是空串。
        body_len = br.evaluate("document.body.innerText.length")
        check(isinstance(body_len, int) and body_len > 20,
              f"首屏有可见文字（不是白屏）  got {body_len} 字")

        # ---------------------------------------------------------------
        print("\n[2] 配对流程（填表单 → 点按钮 → 真的落盘）")
        # ---------------------------------------------------------------
        br.evaluate("""
          (() => {
            const set = (el, v) => {
              el.value = v;
              el.dispatchEvent(new Event('input', {bubbles: true}));
              el.dispatchEvent(new Event('blur', {bubbles: true}));
            };
            const host = document.querySelector('input[placeholder*="192.168"]');
            const codeEl = document.querySelector('input.code');
            set(host, '127.0.0.1');
            const portEl = document.querySelector('input.port');
            if (portEl) set(portEl, '%d');
            set(codeEl, '%s');
          })()
        """ % (port, code))
        time.sleep(0.4)
        # 点"连接电脑"
        br.evaluate("""
          (() => {
            const btns = [...document.querySelectorAll('button')];
            const b = btns.find(x => /连接电脑/.test(x.textContent||''));
            if (b) b.click();
            return !!b;
          })()
        """)
        ok = br.wait_for("!!window.localStorage.getItem('pb.token')",
                         budget=25, label="token 落盘")
        check(ok, "配对成功后 token 写进了 localStorage")
        tok = br.evaluate("window.localStorage.getItem('pb.token')")
        check(bool(tok), f"token 非空  got {(tok or '')[:12]}…")
        got_base = br.evaluate("window.localStorage.getItem('pb.base')")
        check("127.0.0.1" in (got_base or ""), f"base 落盘正确  got {got_base!r}")
        dev = br.evaluate("window.localStorage.getItem('pb.device_id')")
        check(bool(dev), f"device_id 落盘  got {(dev or '')[:12]}…")

        # ---------------------------------------------------------------
        print("\n[3] 配对后切到时间线（照片真的渲染出来）")
        # ---------------------------------------------------------------
        ok = br.wait_for("document.querySelectorAll('[data-id]').length >= 3",
                         budget=30, label="时间线格子")
        n = br.evaluate("document.querySelectorAll('[data-id]').length")
        check(ok, f"时间线渲染出照片格子  got {n} 个")
        check(n >= 3, f"三张图都在  got {n}")

        # ★★ 缩略图真的解码出像素了吗（404 占位也有 src，但 naturalWidth 是 0）
        ok = br.wait_for("""
          (() => {
            const imgs = [...document.querySelectorAll('[data-id] img')];
            return imgs.length > 0 && imgs.every(i => i.complete && i.naturalWidth > 0);
          })()
        """, budget=30, label="缩略图解码")
        dims = br.evaluate("""
          [...document.querySelectorAll('[data-id] img')].map(
            i => [i.naturalWidth, i.naturalHeight])
        """)
        check(ok, f"★ 缩略图真的解码出像素（不是 404 占位）  got {dims}")

        # ---------------------------------------------------------------
        print("\n[4] 点开查看器（大图 + 底部操作按钮）")
        # ---------------------------------------------------------------
        br.evaluate("""
          (() => {
            const el = document.querySelector('[data-id]');
            if (el) el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
            return !!el;
          })()
        """)
        ok = br.wait_for("""
          (() => {
            const t = document.body.innerText;
            return /下载/.test(t) && /改名/.test(t) && /移动/.test(t) && /回收站/.test(t);
          })()
        """, budget=20, label="查看器四个按钮")
        check(ok, "查看器打开且四个操作按钮都在（下载/改名/移动/回收站）")

        # ---------------------------------------------------------------
        print("\n[4b] AC-2.12 GPS 坐标与地图链接（详情面板）")
        # ---------------------------------------------------------------
        # 当前打开的不一定是带 GPS 那张：三张图的 taken_at 都回落到 mtime，
        # 排序可能变。所以**逐张翻找**，而不是假设第一张就是 a.jpg。
        def open_info() -> bool:
            if br.evaluate("!!document.querySelector('.sheet')"):
                return True
            br.evaluate("""
              (() => {
                const b = [...document.querySelectorAll('.v-btn')]
                  .find(x => /信息/.test(x.getAttribute('aria-label')||''));
                if (b) b.click();
                return !!b;
              })()
            """)
            time.sleep(0.45)
            return bool(br.evaluate("!!document.querySelector('.sheet')"))

        def close_info() -> None:
            if br.evaluate("!!document.querySelector('.sheet')"):
                br.evaluate("""
                  (() => {
                    const b = [...document.querySelectorAll('.v-btn')]
                      .find(x => /信息/.test(x.getAttribute('aria-label')||''));
                    if (b) b.click();
                    return !!b;
                  })()
                """)
                time.sleep(0.35)

        geo = None
        for attempt in range(3):
            if open_info():
                geo = br.evaluate("""
                  (() => {
                    const a = document.querySelector('.sheet .geo');
                    return a ? {text: a.textContent.trim(), href: a.getAttribute('href'),
                                target: a.getAttribute('target'), rel: a.getAttribute('rel')}
                             : null;
                  })()
                """)
                if geo:
                    break
                close_info()
            br.evaluate("""
              (() => {
                const b = [...document.querySelectorAll('.nav')]
                  .find(x => x.getAttribute('aria-label') === '下一张');
                if (b) b.click(); return !!b;
              })()
            """)
            time.sleep(0.6)

        check(bool(geo), "★ 带 GPS 的照片在详情面板显示了坐标行")
        if geo:
            # 精确比对能同时验证"EXIF 度分秒 → 十进制度"的换算没错
            check("38.800691" in geo["text"] and "93.577734" in geo["text"],
                  f"★ 坐标数值正确（EXIF 换算无误）  got {geo['text']!r}")
            check("openstreetmap.org" in (geo["href"] or ""),
                  f"★ 链接指向 OpenStreetMap  got {(geo['href'] or '')[:60]}")
            check(geo["target"] == "_blank", "地图链接新窗口打开")
            # noopener 不是可选的：不加的话目标页能通过 window.opener
            # 把当前页导航走（tabnabbing）
            check("noopener" in (geo["rel"] or ""),
                  f"地图链接带 rel=noopener  got {geo['rel']!r}")
        # 没有 GPS 的照片**不能**显示一行空的坐标行。
        # ⚠️ 这里不能无脑点「下一张」：如果带 GPS 那张恰好是相册最后一张，
        # 点「下一张」是个 no-op，接着"验证下一张没有坐标"就会重新打开
        # **同一张带 GPS 的照片**，然后断言失败 —— 那是测试自己的 bug。
        # 所以必须确认标题真的换了，必要时改按「上一张」。
        close_info()
        cur_title = br.evaluate(
            "(document.querySelector('.v-title')||{}).textContent || ''")
        moved = False
        for want in ("下一张", "上一张"):
            br.evaluate("""
              (() => {
                const b = [...document.querySelectorAll('.nav')]
                  .find(x => x.getAttribute('aria-label') === %s);
                if (b) b.click(); return !!b;
              })()
            """ % json.dumps(want))
            time.sleep(0.6)
            new_title = br.evaluate(
                "(document.querySelector('.v-title')||{}).textContent || ''")
            if new_title and new_title != cur_title:
                moved = True
                break
        check(moved, f"翻到另一张照片（标题真的换了）  got {cur_title!r}")
        if moved and open_info():
            empty_geo = br.evaluate("!!document.querySelector('.sheet .geo')")
            check(not empty_geo, "★ 没有 GPS 的照片不显示空的坐标行")
            close_info()

        # ---------------------------------------------------------------
        print("\n[4c] ★ 翻页时 <img> 节点不泄漏（DOM 无界增长回归）")
        # ---------------------------------------------------------------
        # 这个缺陷曾经真实存在：`.stage` 里的条件兄弟节点（缩略图占位、
        # 加载动画、错误提示、左右翻页按钮）**没有 key**，于是 Vue 在
        # 「nav 按钮因 hasNext 变假而消失」时按位置做 diff，把 `<img>` 的
        # vnode 错配到别的 DOM 节点上 —— 旧 `<img>` 就永远留在 DOM 里。
        # 每翻一页多一个已解码的 1600px 位图，手机上滑几百张就是几百个。
        #
        # ⚠️ 必须**跨越库尾那条边界**才复现：只在中间来回翻是看不出来的
        # （我第一版就因此误判成"无泄漏"）。这里一路翻到尽头再翻回来。
        n_before = br.evaluate("document.querySelectorAll('.v-img.main').length")
        # 先到库尾（只有 3 张图，2 次足够）
        for _ in range(2):
            br.evaluate("""
              (() => {
                const b = [...document.querySelectorAll('.nav')]
                  .find(x => x.getAttribute('aria-label') === '下一张');
                if (b) b.click(); return !!b;
              })()
            """)
            time.sleep(0.6)
        # 再反复跨越边界
        for i in range(6):
            want = "上一张" if i % 2 == 0 else "下一张"
            br.evaluate("""
              (() => {
                const b = [...document.querySelectorAll('.nav')]
                  .find(x => x.getAttribute('aria-label') === %s);
                if (b) b.click(); return !!b;
              })()
            """ % json.dumps(want))
            time.sleep(0.55)
        n_after = br.evaluate("document.querySelectorAll('.v-img.main').length")
        check(n_after == n_before,
              f"★ 8 次翻页（含反复跨越库尾）后 <img> 数量不变"
              f"  got {n_before} → {n_after}")
        check(n_after == 1,
              f"★ 查看器里始终只有 1 张主图（不是越翻越多）  got {n_after}")
        # 顺带确认缩略图占位也没有堆积
        n_thumb = br.evaluate("document.querySelectorAll('.v-img.thumb').length")
        check(n_thumb <= 1, f"缩略图占位不堆积  got {n_thumb}")

        # ---------------------------------------------------------------
        print("\n[4d] AC-2.11 手势（双指缩放 / 双击放大 / 下滑关闭）")
        # ---------------------------------------------------------------
        br.enable_touch()

        def title() -> str:
            return br.evaluate(
                "(document.querySelector('.v-title')||{}).textContent || ''")

        def zoom_scale() -> float:
            """从实际生效的 CSS transform 里读出缩放倍数。

            不读组件内部变量（`zoom` 在 `<script setup>` 里，外部取不到），
            读 DOM 上的 transform 更接近"用户真的看到了什么"。
            """
            return float(br.evaluate("""
              (() => {
                const el = document.querySelector('.v-img.main');
                if (!el) return 1;
                const t = getComputedStyle(el).transform;
                if (!t || t === 'none') return 1;
                const m = t.match(/matrix\\(([^)]+)\\)/);
                if (!m) return 1;
                return parseFloat(m[1].split(',')[0]);
              })()
            """) or 1)

        # --- 双指张开 → 放大 ---
        t0 = title()
        c = (200.0, 300.0)
        br.touch("touchStart", [c])
        br.touch("touchStart", [(c[0] - 30, c[1] - 30), (c[0] + 30, c[1] + 30)])
        for step in range(1, 6):
            d = 30 + step * 25
            br.touch("touchMove", [(c[0] - d, c[1] - d), (c[0] + d, c[1] + d)])
            time.sleep(0.05)
        br.touch("touchEnd", [])
        time.sleep(0.35)
        z_in = zoom_scale()
        touched = br.evaluate("window.__touch.start")
        check(touched and touched > 0,
              f"★ 触摸事件真的派发到了页面（不是假绿）  touchstart={touched}")
        check(z_in > 1.2, f"★ 双指张开真的放大了图片  scale={z_in:.2f}")
        check(title() == t0,
              f"★ 缩放过程中没有误翻页  标题仍是 {t0!r}")

        # --- 放大后单指拖动 → 平移（不应翻页）---
        br.touch("touchStart", [c])
        for i in range(1, 5):
            br.touch("touchMove", [(c[0] - i * 25, c[1] - i * 20)])
            time.sleep(0.05)
        br.touch("touchEnd", [])
        time.sleep(0.3)
        check(title() == t0, f"★ 放大后拖动是平移、没有翻页  got {title()!r}")
        check(zoom_scale() > 1.2, "放大后拖动不会把缩放重置掉")

        # --- 双击 → 归位到 1x（此时已放大，双击的语义是还原）---
        for _ in range(2):
            br.touch("touchStart", [c])
            br.touch("touchEnd", [])
            time.sleep(0.08)
        time.sleep(0.4)
        z_dbl = zoom_scale()
        check(z_dbl < 1.2, f"★ 双击把已放大的图还原  scale={z_dbl:.2f}")

        # --- 双击（1x 时）→ 放大到 2x ---
        for _ in range(2):
            br.touch("touchStart", [c])
            br.touch("touchEnd", [])
            time.sleep(0.08)
        time.sleep(0.4)
        z_dbl2 = zoom_scale()
        check(z_dbl2 > 1.5, f"★ 1x 时双击放大到约 2 倍  scale={z_dbl2:.2f}")

        # 还原，免得影响后面的下滑关闭
        for _ in range(2):
            br.touch("touchStart", [c])
            br.touch("touchEnd", [])
            time.sleep(0.08)
        time.sleep(0.4)

        # --- 左右滑动翻页（单指，水平为主）---
        # ⚠️ 先定位到**第一张**，否则左滑(=下一张)可能因为已经在库尾而
        # 变成 no-op —— 那是测试自己站错了位置，不是手势失灵。
        # 上一张按钮消失即表示已到第一张。
        for _ in range(6):
            moved = br.evaluate("""
              (() => {
                const b = [...document.querySelectorAll('.nav')]
                  .find(x => x.getAttribute('aria-label') === '上一张');
                if (b) b.click(); return !!b;
              })()
            """)
            if not moved:
                break
            time.sleep(0.5)
        t_before = title()
        br.touch("touchStart", [(300.0, 400.0)])
        for i in range(1, 7):
            br.touch("touchMove", [(300.0 - i * 40, 400.0 + i * 3)])
            time.sleep(0.04)
        br.touch("touchEnd", [])
        time.sleep(0.7)
        check(title() != t_before,
              f"★ 左滑翻到下一张  {t_before!r} → {title()!r}")

        # 再右滑回上一张，确认方向没接反
        t_mid = title()
        br.touch("touchStart", [(120.0, 400.0)])
        for i in range(1, 7):
            br.touch("touchMove", [(120.0 + i * 40, 400.0 + i * 3)])
            time.sleep(0.04)
        br.touch("touchEnd", [])
        time.sleep(0.7)
        check(title() == t_before,
              f"★ 右滑回到上一张（方向没接反）  {t_mid!r} → {title()!r}")

        # --- 下滑关闭（单指，垂直为主）---
        open_before = br.evaluate("!!document.querySelector('.viewer')")
        br.touch("touchStart", [(200.0, 200.0)])
        for i in range(1, 9):
            br.touch("touchMove", [(200.0 + i * 2, 200.0 + i * 45)])
            time.sleep(0.04)
        br.touch("touchEnd", [])
        time.sleep(0.7)
        open_after = br.evaluate("!!document.querySelector('.viewer')")
        check(open_before and not open_after,
              f"★ 下滑关闭了查看器  open {open_before} → {open_after}")

        # --- touchcancel 不触发翻页/关闭（来电打断）---
        # 重新打开一张
        br.evaluate("""
          (() => {
            const el = document.querySelector('[data-id]');
            if (el) el.dispatchEvent(new MouseEvent('click', {bubbles: true}));
            return !!el;
          })()
        """)
        time.sleep(0.8)
        if br.evaluate("!!document.querySelector('.viewer')"):
            t_cancel = title()
            br.touch("touchStart", [(200.0, 200.0)])
            for i in range(1, 9):
                br.touch("touchMove", [(200.0 + i * 2, 200.0 + i * 45)])
                time.sleep(0.03)
            # 系统打断（来电/手势返回）—— 绝不能因此关闭或翻页
            br.touch("touchCancel", [])
            time.sleep(0.5)
            still_open = br.evaluate("!!document.querySelector('.viewer')")
            check(still_open,
                  "★ touchcancel（来电打断）不会误关闭查看器")
            check(title() == t_cancel,
                  f"★ touchcancel 不会误翻页  got {title()!r}")
            # 收尾：正常关闭，回到时间线
            br.evaluate("""
              (() => {
                const b = [...document.querySelectorAll('.v-btn')]
                  .find(x => x.getAttribute('aria-label') === '返回');
                if (b) b.click(); return !!b;
              })()
            """)
            time.sleep(0.5)

        # 关掉信息面板，后面的步骤不依赖它
        close_info()

        # ---------------------------------------------------------------
        print("\n[4e] AC-2.15/2.16 SSE 事件流（真的收到了服务端的推送）")
        # ---------------------------------------------------------------
        # [1] 连接建立且 URL 里不含 token
        ok = br.wait_for("window.__sse && window.__sse.urls.length > 0",
                         budget=15, label="EventSource 被创建")
        urls = br.evaluate("JSON.stringify(window.__sse ? window.__sse.urls : [])")
        url_list = json.loads(urls) if urls else []
        check(ok and url_list, f"前端真的建了 EventSource  got {url_list}")
        check(any("/api/events" in u for u in url_list),
              "连的是 /api/events", f"got {url_list}")
        # ★ token 绝不能出现在 URL 里：它会落进浏览器缓存键、服务器
        #   访问日志、以及任何 Referer。
        check(not any("token" in u.lower() for u in url_list),
              "★ URL 里没有 token（鉴权走 cookie）", f"got {url_list}")

        # [2] 收到 hello（证明这条流真的通了，而不是挂在那儿没数据）
        ok = br.wait_for("""
          (window.__sse?.events || []).some(e => e.type === 'hello')
        """, budget=20, label="hello 帧")
        check(ok, "收到 hello 帧（服务端确认连接）")

        # [3] ★ AC-2.16：触发一次真实重扫，浏览器必须收到 scan_progress。
        #     这是本节的**核心断言** —— 后端一直在推，但前端从来没接过
        #     （`grep EventSource frontend/src/` 曾经零命中），所以
        #     "设置页只有一个静止的扫描中标签"正是这个缺口的表象。
        before = br.evaluate(
            "(window.__sse.events.filter(e => e.type === 'scan_progress')).length")
        # 直接对本机服务发起重扫（模拟用户在设置页点按钮；这里用 HTTP
        # 是为了不依赖按钮的可见性/滚动位置）
        try:
            rq = urllib.request.Request(
                f"{base}/api/index/rescan", data=b"{}", method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(rq, timeout=10) as r:
                r.read()
        except urllib.error.HTTPError as e:
            # 3012 = 已有扫描在进行中，也算"触发了"
            if e.code not in (202, 400):
                raise

        ok = br.wait_for("""
          (window.__sse?.events || []).filter(
            e => e.type === 'scan_progress').length > %d
        """ % before, budget=40, label="scan_progress 帧")
        check(ok, "★ AC-2.16 浏览器收到 scan_progress（实时进度）")

        # [4] 进度帧里要有 total —— 没有它前端画不出百分比，
        #     用户只能看着一个不断增加的绝对数字干等。
        prog = br.evaluate("""
          JSON.stringify((window.__sse.events.filter(
            e => e.type === 'scan_progress').slice(-1)[0] || {}).data)
        """)
        pdata = json.loads(prog) if prog and prog != "null" else None
        check(bool(pdata) and "total" in pdata,
              f"进度帧带 total（能算百分比）  got {pdata}")

        # [5] 要有 scan_done，否则前端永远停在"扫描中"
        ok = br.wait_for("""
          (window.__sse?.events || []).some(e => e.type === 'scan_done')
        """, budget=40, label="scan_done 帧")
        check(ok, "收到 scan_done（进度条会消失）")

        # [6] ★ AC-2.15：在电脑上往库目录拷一张图，**不做任何手动操作**，
        #     时间线必须自己长出新照片。
        #     这里用 shutil.copy2 模拟"用户在资源管理器里粘贴"。
        new_name = "自动出现的新图.jpg"
        src = tmp / "inbox" / new_name
        make_image(src, (250, 120, 20))
        shutil.copy2(src, lib / new_name)
        # 前端在"时间线不在顶部"时会**故意**不自动重载（免得把用户
        # 正翻到的地方冲掉），所以这里先滚回顶部再等。
        br.evaluate("""
          (() => {
            const g = document.querySelector('.grid-root.scroll');
            if (g) g.scrollTop = 0;
            return !!g;
          })()
        """)
        # ★ 照片文件名**不在 `innerText` 里** —— `PhotoCell.vue` 只把它放在
        #   `aria-label` 与 `<img alt>` 上（网格格子太小，不显示文件名）。
        #   一开始用 `document.body.innerText` 判定，新图其实已经渲染出来
        #   了断言仍然失败 —— 那是探针的错，不是功能的错。
        ok = br.wait_for(
            "document.body.innerHTML.includes(%r)" % new_name,
            budget=45, label="时间线自动出现新图")
        check(ok, f"★ AC-2.15 新照片自动出现在时间线（无需手动刷新）  {new_name}")

        # [7] 上面这条能成立，前提是前端确实收到了 media_changed
        mc = br.evaluate("""
          JSON.stringify((window.__sse.events.filter(
            e => e.type === 'media_changed').slice(-1)[0] || {}).data)
        """)
        check(mc and mc != "null", f"收到 media_changed 事件  got {mc}")

        # 把新图删掉，后面的步骤（Tab 切换、未配对）不受影响
        (lib / new_name).unlink()

        # ---------------------------------------------------------------
        print("\n[5] 切 Tab（KeepAlive 缓存 + 不报错）")
        # ---------------------------------------------------------------
        for label in ("搜索", "上传", "我的", "照片"):
            br.evaluate("""
              (() => {
                const b = [...document.querySelectorAll('.tabbar button')]
                  .find(x => (x.textContent||'').includes('%s'));
                if (b) b.click();
                return !!b;
              })()
            """ % label)
            time.sleep(0.35)
        txt = br.evaluate("document.body.innerText")
        check(bool(txt) and len(txt) > 10, "切完一圈 Tab 后页面仍有内容")

        # ---------------------------------------------------------------
        print("\n[6] 全程没有未捕获的 JS 异常")
        # ---------------------------------------------------------------
        errs = br.evaluate("JSON.stringify(window.__errors || [])")
        parsed = json.loads(errs) if errs else []
        check(not parsed, f"无未捕获异常  got {parsed[:3]}")

        # ---------------------------------------------------------------
        print("\n[7] 未配对时不应该泄漏已配对界面")
        # ---------------------------------------------------------------
        br.evaluate("window.localStorage.clear()")
        br.goto(base + "/")
        ok = br.wait_for("!!document.querySelector('input[placeholder*=\"192.168\"]')",
                         budget=25, label="清空后回配对页")
        check(ok, "清掉 localStorage 后回到配对页")

    finally:
        br.close()

    # -------------------------------------------------------------------
    print("\n[8] 真实照片库未被触碰")
    # -------------------------------------------------------------------
    fp1 = real_fingerprint()
    check(fp1 == fp0, f"真实照片库一个字节都没变  {fp0}")

    print("\n" + "=" * 60)
    if failures:
        print(f"=== {len(failures)} 项失败 ===")
        for f in failures:
            # 这里**必须**是 ASCII：GBK 控制台下打印 U+2717 会抛
            # UnicodeEncodeError，把真正的失败清单整个吞掉，
            # 只留下一个与本任务无关的编码报错。
            print("  [X]", f)
        return 1
    print("=== ALL PASS ===")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
