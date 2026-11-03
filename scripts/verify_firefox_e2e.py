"""端到端验证：**在 Firefox 里把手机端网页跑一遍**（验收项 I-02）。

## 为什么需要它

`scripts/verify_browser_e2e.py` 的 25 节 219 条断言全部跑在 **Chrome/155**
上（I-01）。I-02「Firefox 正常」长期是 ☐，理由是「真机测试」—— 而这句话
后来被证明是「没查过」的代名词（见 docs/11 的 Q-026）：逐条核完 21 个跨
浏览器差异点后，能说「没有用到 Firefox 不支持的 API」，不能说「这段代码
在 Firefox 里跑过」。

本脚本就是把它跑一遍。

## 与 Chrome 版的关键差别：协议不是 CDP

**Firefox 141 起移除了 CDP** —— `--remote-debugging-port` 不再提供
`/json/version`，改用 W3C **WebDriver BiDi**。所以：

  * Chrome 版：`http://127.0.0.1:PORT/json/version` 拿 `webSocketDebuggerUrl`
  * Firefox 版：从 stderr 抓 `WebDriver BiDi listening on ws://...`，
    连的时候**必须加 `/session` 后缀**（直接连根地址会得到
    `server rejected WebSocket connection: HTTP 200`）

命令名也全不一样，对应关系（都已实测）：

    CDP                                    BiDi
    ------------------------------------   ----------------------------------
    Page.navigate                        → browsingContext.navigate
    Emulation.setDeviceMetricsOverride   → browsingContext.setViewport
    Page.addScriptToEvaluateOnNewDocument→ script.addPreloadScript
    Runtime.evaluate                     → script.evaluate
    Input.dispatchTouchEvent             → input.performActions(pointerType=touch)
    Page.bringToFront                    → browsingContext.activate
    DOM.setFileInputFiles                → input.setFiles
    Log.enable / Runtime.enable          → session.subscribe(["log.entryAdded"])
    Network.emulateNetworkConditions     → 无对应命令，只能 network.addIntercept
                                           + network.failRequest 造"请求失败"

## 判定标准

与 Chrome 版同源：全部用**浏览器里可观测的事实**判定，不读内部对象。
`session.new` 返回的 `capabilities.browserVersion` 就是 I-02 需要的版本证据
（比解析 exe 的 VersionInfo 可信）。

## 安全边界

全程在 `tempfile.mkdtemp()` 造的合成照片库里操作，**绝不碰真实照片库**。
脚本结束校验真实库指纹与开始时一致。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.app.config import Config  # noqa: E402
from backend.app.server import serve  # noqa: E402

# 便携版 Firefox 的查找顺序。本机没有管理员权限，装不上正式版
# （`msiexec /i /qn` → 1603；NSIS `/S` → exit -1），只能把官方安装包
# 解出来的 core/ 目录放到用户目录下用。
FIREFOX_CANDIDATES = (
    Path.home() / "AppData/Local/Programs/FirefoxPortable/firefox.exe",
    Path(r"C:\Program Files\Mozilla Firefox\firefox.exe"),
    Path(r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"),
    Path.home() / "AppData/Local/Mozilla Firefox/firefox.exe",
    Path.home() / "AppData/Local/Programs/Mozilla Firefox/firefox.exe",
)

failures: list[str] = []
firefox_version = ""


def check(ok: bool, label: str, extra: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f"   {extra}" if extra else ""))
    if not ok:
        failures.append(label)


def find_firefox() -> str | None:
    for p in FIREFOX_CANDIDATES:
        if p.exists():
            return str(p)
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def real_fingerprint() -> str:
    """真实照片库的指纹。跑完要和跑前一致 —— 这是安全边界，不是装饰。"""
    lib = Path(r"D:\Photos")
    if not lib.exists():
        return "NO-REAL-LIBRARY"
    import hashlib

    h = hashlib.sha256()
    n = 0
    for f in sorted(lib.rglob("*")):
        if f.is_file():
            st = f.stat()
            h.update(f"{f.name}:{st.st_size}:{int(st.st_mtime)}".encode())
            n += 1
    return f"{n} files / {h.hexdigest()[:16]}"


# ---------------------------------------------------------------------------
# WebDriver BiDi 客户端
# ---------------------------------------------------------------------------
class BidiBrowser:
    """headless Firefox 的一个标签页，通过 WebDriver BiDi 驱动。

    刻意保持与 Chrome 版 `Browser` **同名同形状**的方法（`connect` /
    `evaluate` / `goto` / `set_viewport` / `touch` / `close`），这样断言
    读起来是同一套，差异只集中在协议层。
    """

    def __init__(self, exe: str, profile: Path) -> None:
        self.exe = exe
        self.profile = profile
        profile.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            [exe, "-headless", "-no-remote", "-profile", str(profile),
             "--remote-debugging-port=0", "about:blank"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        self.ws = None
        self._id = 0
        self._events: list[dict] = []
        self.ctx = ""
        self.version = ""
        self._ws_url = self._read_bidi_url()

    def _read_bidi_url(self, budget: float = 60.0) -> str:
        """从 stderr 里抓 BiDi 地址。

        Firefox 把 `WebDriver BiDi listening on ws://127.0.0.1:PORT` 打在
        **stderr** 上，不是 stdout。
        """
        end = time.time() + budget
        while time.time() < end:
            line = self.proc.stderr.readline()
            if not line:
                if self.proc.poll() is not None:
                    raise RuntimeError("Firefox 进程提前退出")
                continue
            m = re.search(r"WebDriver BiDi listening on (ws://\S+)", line)
            if m:
                # ★ 真正的端点在 `/session`。直接连根地址会得到
                #   `websockets.exceptions.InvalidStatus: server rejected
                #    WebSocket connection: HTTP 200` —— 状态码 200 看着像
                #   成功，其实是被拒了，很容易误判成"网络问题"。
                return m.group(1).rstrip("/") + "/session"
        raise RuntimeError("60 秒内没拿到 BiDi 地址")

    def connect(self) -> None:
        import websockets

        self._loop = asyncio.new_event_loop()
        self.ws = self._loop.run_until_complete(
            websockets.connect(self._ws_url, max_size=64 * 1024 * 1024))
        r = self.send("session.new", {"capabilities": {}})
        caps = r.get("capabilities", {})
        self.version = caps.get("browserVersion", "")
        self.ua = caps.get("userAgent", "")
        r = self.send("browsingContext.create", {"type": "tab"})
        self.ctx = r["context"]

    def send(self, method: str, params: dict | None = None):
        self._id += 1
        mid = self._id
        msg: dict = {"id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        self._loop.run_until_complete(self.ws.send(json.dumps(msg)))
        while True:
            # 与 Chrome 版同样的理由：没有超时的话，一条命令不回就把整个
            # 脚本挂死，日志停在某一行的下一行，看不出是谁卡的。
            try:
                raw = self._loop.run_until_complete(
                    asyncio.wait_for(self.ws.recv(), timeout=120))
            except asyncio.TimeoutError:
                raise RuntimeError(
                    f"BiDi {method} 等待响应超过 120 秒（事件已收 "
                    f"{len(self._events)} 条）") from None
            m = json.loads(raw)
            if m.get("id") != mid:
                self._events.append(m)
                continue
            if m.get("type") != "success":
                raise RuntimeError(f"{method}: {m.get('error')}")
            return m.get("result") or {}

    def evaluate(self, expr: str, *, user_gesture: bool = False):
        """执行 JS 并取回值。

        ★ BiDi 的返回是**两层嵌套**，比 CDP 多一层：
            {"result": {"realm": ..., "type": "success",
                        "result": {"type": "number", "value": 2}}}
        只剥一层会拿到 `{'type': 'number', 'value': 2}` 这个 dict 而不是 2，
        断言 `isinstance(n, int)` 就会静默变成 False。
        """
        params = {
            "expression": expr,
            "target": {"context": self.ctx},
            "awaitPromise": True,
            "resultOwnership": "none",
        }
        if user_gesture:
            params["userActivation"] = True
        r = self.send("script.evaluate", params)
        if r.get("type") == "exception":
            raise RuntimeError(
                f"页面 JS 抛异常: {json.dumps(r.get('result'), ensure_ascii=False)[:300]}")
        return (r.get("result") or {}).get("value")

    def add_preload(self, function_declaration: str) -> None:
        self.send("script.addPreloadScript", {
            "functionDeclaration": function_declaration,
            "contexts": [self.ctx],
        })

    def set_viewport(self, width: int, height: int, dpr: float = 3.0) -> None:
        self.send("browsingContext.setViewport", {
            "context": self.ctx,
            "viewport": {"width": width, "height": height},
            "devicePixelRatio": dpr,
        })

    def goto(self, url: str, *, budget: float = 30.0) -> None:
        self.send("browsingContext.navigate",
                  {"context": self.ctx, "url": url, "wait": "complete"})

    def wait_for(self, expr: str, *, budget: float = 25.0, label: str = "") -> bool:
        end = time.time() + budget
        while time.time() < end:
            try:
                if self.evaluate(expr):
                    return True
            except Exception:                          # noqa: BLE001
                pass
            time.sleep(0.25)
        if label:
            print(f"      等待超时: {label}")
        return False

    def tap(self, x: float, y: float) -> None:
        """派发**真实触摸**（不是 `el.click()`）。

        `el.click()` 在 JS 层面直接调 handler，绕开命中测试 —— 元素被遮住、
        坐标算错、指针事件没接上，它都会"成功"。这里走的是浏览器自己的
        输入管线，页面收到的是 `touchstart/touchend`（已实测确认）。
        """
        self.send("input.performActions", {
            "context": self.ctx,
            "actions": [{
                "type": "pointer", "id": "finger1",
                "parameters": {"pointerType": "touch"},
                "actions": [
                    {"type": "pointerMove", "x": x, "y": y},
                    {"type": "pointerDown", "button": 0},
                    {"type": "pointerUp", "button": 0},
                ],
            }],
        })

    def console_errors(self) -> list[str]:
        out = []
        for e in self._events:
            if e.get("method") == "log.entryAdded":
                p = e.get("params", {})
                if p.get("level") == "error":
                    out.append(str(p.get("text", "")))
        return out

    def close(self) -> None:
        try:
            if self.ws is not None:
                self._loop.run_until_complete(self.ws.close())
        except Exception:                              # noqa: BLE001
            pass
        # Firefox 会派生一堆子进程（内容进程 / GPU 进程），父进程退出后
        # 它们可能还活着，导致下一个 profile 被锁。
        #
        # ★ `taskkill /T` 杀**整棵进程树**，这是正确工具。原来写的是
        #   `wmic process where CommandLine like ...`，而 **wmic 在
        #   Windows 11 / 新版 Server 上已被移除** —— `FileNotFoundError:
        #   [WinError 2]`，而且它抛在 `finally` 里，会把已经算好的
        #   「几项失败」整个吞掉，只留下一个与本任务无关的崩溃。
        #   也绝不能用 `taskkill /IM firefox.exe`：那会连带杀掉用户自己
        #   开着的 Firefox。
        try:
            subprocess.run(
                ["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                capture_output=True, timeout=20)
        except Exception:                              # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:                              # noqa: BLE001
            self.proc.kill()


def make_image(path: Path, color: tuple[int, int, int],
               size=(240, 180)) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG", quality=88)


def main() -> int:
    print("=" * 60)
    print("=== Firefox 端到端验证（验收项 I-02）===")
    print("=" * 60)

    exe = find_firefox()
    if not exe:
        print("SKIP 本机没有 Firefox，跳过（I-02 保持 ☐）")
        for p in FIREFOX_CANDIDATES:
            print(f"       找过: {p}")
        return 0
    print(f"Firefox: {exe}")

    fp0 = real_fingerprint()
    print(f"真实库（开始）: {fp0}")

    tmp = Path(tempfile.mkdtemp(prefix="pb_firefox_"))
    lib = tmp / "lib"
    port = free_port()
    data = tmp / "data"
    data.mkdir(parents=True)

    make_image(lib / "a.jpg", (200, 40, 40))
    make_image(lib / "b.jpg", (40, 160, 60))
    make_image(lib / "sub" / "c.jpg", (40, 60, 200))

    cfg_raw = {
        # 与 Chrome 版一样绑 0.0.0.0：Firefox 要从**局域网明文 HTTP** 打开
        # 页面，那才是真机的安全上下文（`isSecureContext === false`）。
        # 键名是 `host` 不是 `bind`（见 verify_browser_e2e.py 里的长注释）。
        "host": "0.0.0.0",
        "server": {"port": port, "transport": "local_http", "host": "0.0.0.0",
                   # panel_http 为真时 check_bind_safety 禁止绑通配地址，
                   # 所以这里关掉，改用进程内直调 handler 取配对码。
                   "panel_http": False},
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
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        state["loop"] = loop
        loop.run_until_complete(runner())

    t = threading.Thread(target=thread_main, daemon=True, name="firefox-e2e")
    t.start()
    if not ready.wait(90):
        print("FAIL 服务 90 秒内未就绪")
        return 2
    base = state["base"]
    st = state["st"]
    page_host = base.split("://", 1)[-1].split("/")[0].split(":")[0]
    print(f"服务: {base}")

    for _ in range(120):
        if st.indexer.running:
            time.sleep(0.5)
        else:
            break
    time.sleep(1.0)

    # 配对码走托盘真正走的那条路：直接调 handler 函数。
    from backend.app.core import Ctx as _Ctx
    from backend.app.handlers.auth import panel_pair_code as _panel_pair_code

    def _new_code() -> dict:
        _ctx = _Ctx(method="POST", path="/panel/pair-code",
                    client_ip="127.0.0.1", app=st)
        _rep = asyncio.run(_panel_pair_code(_ctx))
        return json.loads(_rep.body or b"{}")

    pc = _new_code()
    code = pc["code"]
    print(f"配对码: {code}")

    br = BidiBrowser(exe, tmp / "ffprofile")
    try:
        br.connect()
        global firefox_version
        firefox_version = br.version
        print(f"Firefox 版本: {br.version}")
        print(f"UA: {br.ua}")

        # 注入错误收集器 + 触摸计数器。触摸计数器不是装饰：一个"事件根本
        # 没派发"的环境会让所有手势断言凭空通过。
        br.add_preload("""() => {
            window.__errors = [];
            window.addEventListener('error', e => {
              window.__errors.push(String(e.message || e.error));
            });
            window.addEventListener('unhandledrejection', e => {
              window.__errors.push('unhandled: ' + String(e.reason));
            });
            window.__touch = {start: 0, move: 0, end: 0};
            window.addEventListener('touchstart', () => window.__touch.start++, true);
            window.addEventListener('touchmove', () => window.__touch.move++, true);
            window.addEventListener('touchend', () => window.__touch.end++, true);
        }""")
        br.send("session.subscribe", {"events": ["log.entryAdded"]})

        # 手机视口。Firefox 的 setViewport 实测生效（innerWidth/Height/DPR
        # 与 100dvh 的计算高度都跟着变）。
        br.set_viewport(390, 844, 3.0)

        # -------------------------------------------------------------------
        print("\n[1] 首屏渲染（未配对 → 应当看到配对页）")
        # -------------------------------------------------------------------
        br.goto(base + "/")
        ok = br.wait_for(
            "!!document.querySelector('input[placeholder*=\"192.168\"]')",
            budget=30, label="配对页输入框")
        check(ok, "配对页渲染出来了（Firefox 里 JS 真的执行了）")
        title = br.evaluate("document.title")
        check(bool(title), f"页面标题非空  got {title!r}")
        body_len = br.evaluate("document.body.innerText.length")
        check(isinstance(body_len, int) and body_len > 20,
              f"首屏有可见文字（不是白屏）  got {body_len} 字")

        # ★ 这一节的核心：Firefox 的**安全上下文判定**与 Chrome 一致吗。
        #   局域网明文 HTTP 下 `isSecureContext` 必须是 false —— 这正是
        #   真机的条件，也是「复制服务地址」会踩坑的原因。
        sec = br.evaluate("JSON.stringify({secure: isSecureContext, "
                          "clipboard: typeof navigator.clipboard, "
                          "dpr: devicePixelRatio, w: innerWidth, h: innerHeight, "
                          "origin: location.origin})")
        info = json.loads(sec or "{}")
        print(f"      环境: {sec}")
        check(info.get("origin", "").startswith("http://" + page_host),
              f"页面来源是局域网明文 HTTP  got {info.get('origin')}")
        check(info.get("secure") is False,
              "Firefox 在局域网明文 HTTP 下 isSecureContext 也是 false")
        check(info.get("w") == 390 and info.get("h") == 844 and info.get("dpr") == 3,
              f"手机视口生效  got {info.get('w')}x{info.get('h')}@{info.get('dpr')}x")

        # -------------------------------------------------------------------
        print("\n[2] 配对流程（填表单 → 点按钮 → 真的落盘）")
        # -------------------------------------------------------------------
        br.evaluate("""
          (() => {
            const set = (el, v) => {
              el.value = v;
              el.dispatchEvent(new Event('input', {bubbles: true}));
              el.dispatchEvent(new Event('blur', {bubbles: true}));
            };
            const host = document.querySelector('input[placeholder*="192.168"]');
            const codeEl = document.querySelector('input.code');
            set(host, '%s');
            const portEl = document.querySelector('input.port');
            if (portEl) set(portEl, '%d');
            set(codeEl, '%s');
          })()
        """ % (page_host, port, code))
        time.sleep(0.4)
        br.evaluate("""
          (() => {
            const btns = [...document.querySelectorAll('button')];
            const b = btns.find(x => /连接电脑/.test(x.textContent||''));
            if (b) b.click();
            return !!b;
          })()
        """)
        ok = br.wait_for("!!window.localStorage.getItem('pb.token')",
                         budget=30, label="token 落盘")
        check(ok, "配对成功后 token 写进了 localStorage（Firefox 的 fetch 真的通了）")
        tok = br.evaluate("window.localStorage.getItem('pb.token')")
        check(bool(tok), f"token 非空  got {(tok or '')[:12]}…")
        got_base = br.evaluate("window.localStorage.getItem('pb.base')")
        check(page_host in (got_base or ""), f"base 落盘正确  got {got_base!r}")

        # -------------------------------------------------------------------
        print("\n[3] 时间线：格子渲染 + 缩略图真的解码出像素")
        # -------------------------------------------------------------------
        ok = br.wait_for("document.querySelectorAll('[data-id]').length >= 3",
                         budget=35, label="时间线格子")
        n = br.evaluate("document.querySelectorAll('[data-id]').length")
        check(ok, f"时间线渲染出照片格子  got {n} 个")

        ok = br.wait_for("""
          (() => {
            const imgs = [...document.querySelectorAll('[data-id] img')];
            return imgs.length > 0 && imgs.every(i => i.complete && i.naturalWidth > 0);
          })()
        """, budget=35, label="缩略图解码")
        dims = br.evaluate("""
          JSON.stringify([...document.querySelectorAll('[data-id] img')].map(
            i => [i.naturalWidth, i.naturalHeight]))
        """)
        check(ok, f"★ 缩略图真的解码出像素（Firefox 的 WebP 解码）  got {dims}")

        # -------------------------------------------------------------------
        print("\n[4] 点开查看器（真实触摸 → 大图 + 操作按钮）")
        # -------------------------------------------------------------------
        box = br.evaluate("""
          (() => {
            const el = document.querySelector('[data-id]');
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return JSON.stringify({x: r.left + r.width/2, y: r.top + r.height/2});
          })()
        """)
        check(bool(box), f"取到第一个格子的中心坐标  got {box}")
        if box:
            c = json.loads(box)
            br.tap(c["x"], c["y"])
            ok = br.wait_for("!!document.querySelector('.viewer, [class*=viewer]')",
                             budget=20, label="查看器打开")
            check(ok, "真实触摸点开后查看器打开了")
            # ★ 触摸真的送到了吗（不然上面的"打开"可能是别的原因）
            #
            # 判据是**计数器**，不是字符串包含。第一版写的
            # `"touchstart" in tc` 永远为假 —— 计数器是
            # `{"start":1,"move":0,"end":1}`，键名里根本没有 `touchstart`
            # 这个子串，于是"事件真的到了"这件事被判成失败。
            tc = br.evaluate("JSON.stringify(window.__touch)")
            tcnt = json.loads(tc or "{}")
            check(tcnt.get("start", 0) > 0 and tcnt.get("end", 0) > 0,
                  f"★ 页面真的收到了 touchstart/touchend（不是合成 click）  got {tc}")
            br.evaluate("""
              (() => {
                const b = [...document.querySelectorAll('button')]
                  .find(x => /关闭|返回|×/.test(x.textContent||''));
                if (b) b.click();
              })()
            """)
            time.sleep(0.8)

        # -------------------------------------------------------------------
        print("\n[4b] 设置页「复制服务地址」—— 非安全上下文下的兜底")
        # -------------------------------------------------------------------
        # 这一条是 ISSUE-019 的正面回归：Chrome 那边曾经静默失效（可选链把
        # 整条链短路成 undefined，成功与失败回调一次都不跑）。Firefox 在
        # 局域网明文 HTTP 下 `navigator.clipboard` 同样是 undefined，所以
        # 兜底路径必须真的能复制。
        clip = br.evaluate("typeof navigator.clipboard")
        print(f"      navigator.clipboard = {clip}")
        check(clip == "undefined",
              f"Firefox 非安全上下文下 clipboard 也是 undefined  got {clip}")

        # ★ 切页必须点底部 Tab 的「我的」，不能往 localStorage 塞
        #   `pb.tab` —— 那是一个不存在的键，页面照样停在时间线，
        #   然后"找不到「复制服务地址」这一行"会被误判成功能坏了。
        switched = br.evaluate("""
          (() => {
            const b = [...document.querySelectorAll('button, .tabbar button, nav button')]
              .find(x => /我的/.test(x.textContent || ''));
            if (b) b.click();
            return !!b;
          })()
        """)
        check(bool(switched), "底部 Tab 里有「我的」，点得动")
        time.sleep(1.0)

        row = br.evaluate("""
          (() => {
            const btn = [...document.querySelectorAll('button')]
              .find(x => /复制服务地址/.test(x.textContent || ''));
            if (!btn) return '';
            const r = btn.getBoundingClientRect();
            return JSON.stringify({x: r.x + r.width/2, y: r.y + r.height/2,
                                   w: r.width, h: r.height});
          })()
        """)
        rd = json.loads(row or "{}") if row else {}
        check(bool(rd.get("w")), "设置页有「复制服务地址」这一行且可见",
              f"{rd.get('w')}×{rd.get('h')}" if rd else "没找到")

        if rd.get("w"):
            # 装一个探针，记录 execCommand 的真实返回值
            br.evaluate("""
              (() => {
                window.__copyProbe = {calls: 0, results: []};
                const orig = document.execCommand.bind(document);
                document.execCommand = function (cmd, ...rest) {
                  const r = orig(cmd, ...rest);
                  if (cmd === 'copy') {
                    window.__copyProbe.calls += 1;
                    window.__copyProbe.results.push(r);
                  }
                  return r;
                };
              })()
            """)
            # ★ 用 `userActivation: true` 的 script.evaluate 里 `btn.click()`。
            #
            # `execCommand('copy')` 只在**有用户手势**时返回 true
            # （Chrome 侧探针实测：有手势 → true，无手势 → false）。
            # BiDi 的 `userActivation` 就是把这个求值标记成用户激活，
            # 与 CDP 的 `userGesture: true` 同一件事。
            #
            # 不用 `input.performActions` 派真指针：Chrome 版在同类场景里
            # 出现过"命令被接受、不报错、页面收不到"（[4g] 记录的输入域
            # 失灵）。这里要验的是「有手势时 execCommand 的行为」，
            # 不是「输入通道通不通」—— 后者已经由 [4] 的真实触摸验过了。
            clicked = br.evaluate("""
              (() => {
                const btn = [...document.querySelectorAll('button')]
                  .find(x => /复制服务地址/.test(x.textContent || ''));
                if (!btn) return 'no-button';
                btn.click();
                return 'clicked';
              })()
            """, user_gesture=True)
            print(f"      点击结果: {clicked}")
            time.sleep(1.2)
            cp = br.evaluate("JSON.stringify(window.__copyProbe)")
            print(f"      __copyProbe = {cp}")
            pr = json.loads(cp or "{}")
            check(pr.get("calls", 0) >= 1,
                  f"★ 复制兜底真的执行了 execCommand('copy')  got calls={pr.get('calls')}")
            check(any(pr.get("results", [])),
                  f"★★ execCommand('copy') 返回 true（真的复制了）  got {pr.get('results')}")
            txt = br.evaluate("document.body.innerText")
            check("已复制" in (txt or "") or "复制失败" in (txt or ""),
                  "用户看到了明确反馈（不是静默无反应）")

        # -------------------------------------------------------------------
        print("\n[5] 全程没有未捕获的 JS 异常")
        # -------------------------------------------------------------------
        errs = br.evaluate("JSON.stringify(window.__errors)")
        check(errs in ("[]", None) or json.loads(errs or "[]") == [],
              f"window.__errors 为空  got {errs}")
        cerr = br.console_errors()
        # 404 之类的资源告警不算 JS 异常，但真的 Error 必须暴露出来
        real = [c for c in cerr if "Error" in c and "favicon" not in c.lower()]
        check(not real, f"控制台没有 JS Error  got {real[:3]}")

        # -------------------------------------------------------------------
        print("\n[6] 真实照片库未被触碰")
        # -------------------------------------------------------------------
        fp1 = real_fingerprint()
        check(fp1 == fp0, f"真实照片库一个字节都没变  {fp0}")
    finally:
        br.close()

    print("\n" + "=" * 60)
    if failures:
        print(f"=== {len(failures)} 项失败 ===")
        for f in failures:
            print("  [X]", f)
        return 1
    print(f"=== ALL PASS ===  Firefox {firefox_version}".rstrip())
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
