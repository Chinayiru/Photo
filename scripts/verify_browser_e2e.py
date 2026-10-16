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


def make_image(path: Path, color: tuple[int, int, int], size=(240, 180)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG", quality=88)


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

    # 造 3 张图，让时间线有内容可渲染
    make_image(lib / "a.jpg", (200, 40, 40))
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
        br.send("Page.addScriptToEvaluateOnNewDocument", source="""
            window.__errors = [];
            window.addEventListener('error', e => {
              window.__errors.push(String(e.message || e.error));
            });
            window.addEventListener('unhandledrejection', e => {
              window.__errors.push('unhandled: ' + String(e.reason));
            });
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
            print("  ✗", f)
        return 1
    print("=== ALL PASS ===")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
