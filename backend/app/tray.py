"""系统托盘（Windows）。

托盘是**外壳**，不是核心：它只做四件事 ——

1. 让用户知道服务在跑（图标常驻）与手机该连哪个地址；
2. 显示配对二维码（手机扫码即配对，不用手输 6 位码）；
3. 提供"退出"入口（没有托盘的话，用户只能去任务管理器杀进程，
   而强杀会让 WAL 日志留在磁盘上）；
4. 配对成功时弹一次通知（用户不用盯着手机猜有没有连上）。

设计取舍：

* **不用 `tkinter` 做主窗口**。主界面是手机端的网页；电脑端只需要
  一个托盘图标。`tkinter` 只在"显示二维码"这一件事上临时开一个窗口
  （`_show_qr_window`），关掉即销毁。这样可以打包成
  `--noconsole` 的 exe 而不出现常驻黑框。
* **托盘跑在自己的线程里**。`pystray.Icon.run()` 在 Windows 上
  不要求主线程，而主线程要跑 asyncio 事件循环。二者用
  `loop.call_soon_threadsafe` / `asyncio.run_coroutine_threadsafe` 通信。
* **所有托盘菜单动作都不阻塞**。菜单回调在 pystray 的线程里执行，
  任何一次卡住（例如等数据库锁）都会让托盘菜单变灰无响应。

`pystray` 在非 Windows 或没装依赖时会优雅降级：`TrayController.on_ready`
仍然可用，只是不创建图标 —— 这样 `--tray` 在 Linux 上也不会崩。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any
from urllib.parse import quote

from .config import Config, local_ipv4_addresses

log = logging.getLogger(__name__)

__all__ = ["TrayController", "notify_error", "build_icon_image", "pair_qr_url",
           "is_tray_running"]

#: 当前活着的托盘控制器（进程内最多一个）。
#: 用模块级引用而不是让 `system.py` 去 import `server`（那会形成环）。
_active: "TrayController | None" = None


#: 点"退出"后最多再等这么久就强退（秒）。见 `TrayController._on_quit`。
_QUIT_WATCHDOG_SECONDS = 15.0


def _autostart_on() -> bool:
    """自启当前是否已开（给菜单勾选状态用）。非 Windows 恒为 False。"""
    try:
        from .autostart import is_enabled       # noqa: PLC0415

        return bool(is_enabled())
    except Exception:       # noqa: BLE001 —— 菜单绘制不能因为读注册表失败而崩
        return False


def _force_exit() -> None:
    """看门狗：优雅收尾超时后强退。"""
    log.warning("优雅退出超过 %.0f 秒，强制结束进程", _QUIT_WATCHDOG_SECONDS)
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:       # noqa: BLE001
            pass
    os._exit(0)                 # noqa: SLF001 —— 这里要的就是绕过一切清理


def is_tray_running() -> bool:
    """托盘图标当前是否在跑（`/api/system/info` 展示用）。"""
    ctrl = _active
    if ctrl is None:
        return False
    icon = ctrl._icon
    return icon is not None and bool(getattr(icon, "visible", False))

#: 托盘轮询设备表的间隔。3 秒足够"扫完码手机上点确认、电脑马上弹通知"，
#: 又不会让空闲时的 CPU 占用可见（一次 COUNT 查询 <0.1ms）。
_POLL_SEC = 3.0

_ICON_SIZE = 64


# --------------------------------------------------------------------------
# 图标
# --------------------------------------------------------------------------

def build_icon_image(size: int = _ICON_SIZE) -> Any:
    """用代码画一个托盘图标，不依赖外部资源文件。

    为什么要画而不是放一个 .ico：打包成 exe 后，资源文件的位置取决于
    解包目录（`sys._MEIPASS`），一旦路径算错托盘图标就是空白的 ——
    而"图标是空白"这种故障用户根本没法排查。用代码画则不可能失败。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角方块底：蓝色，和前端主题一致
    r = max(2, size // 8)
    d.rounded_rectangle((0, 0, size - 1, size - 1), radius=r,
                        fill=(37, 99, 235, 255))

    # 一张"照片"：白框 + 山形 + 太阳
    pad = size // 6
    box = (pad, pad + size // 12, size - pad, size - pad - size // 12)
    d.rounded_rectangle(box, radius=max(1, size // 20),
                        outline=(255, 255, 255, 255), width=max(1, size // 24))

    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    # 太阳
    sr = max(1, size // 14)
    cx, cy = x0 + int(w * 0.28), y0 + int(h * 0.30)
    d.ellipse((cx - sr, cy - sr, cx + sr, cy + sr), fill=(255, 255, 255, 255))
    # 山（两个三角）
    d.polygon([(x0 + int(w * 0.08), y1 - int(h * 0.12)),
               (x0 + int(w * 0.42), y0 + int(h * 0.46)),
               (x0 + int(w * 0.72), y1 - int(h * 0.12))],
              fill=(255, 255, 255, 255))
    d.polygon([(x0 + int(w * 0.48), y1 - int(h * 0.12)),
               (x0 + int(w * 0.72), y0 + int(h * 0.60)),
               (x1 - int(w * 0.06), y1 - int(h * 0.12))],
              fill=(219, 234, 254, 255))
    return img


def pair_qr_url(base_url: str, code: str, device_id: str | None = None) -> str:
    """手机扫码后要打开的地址。

    `#code=`（**fragment**）是前端 `PairView.vue` 认的参数
    （`pairParamsFromLocation()`），扫码后配对码会自动填进输入框，
    用户只需点"配对"。

    ★ 为什么是 fragment 而不是 `?code=`：query string 会进服务端访问
      日志、反向代理日志、浏览器历史与 `Referer` 头 —— 每一层都得单独
      遮盖（`cloud/Caddyfile` 里那段 `filter` 就是干这个的，但它只是
      补丁）。fragment 不参与任何一次 HTTP 请求，从根上没有暴露面。
      代价是"把链接发给别人"时必须整条发（浏览器地址栏里的 fragment
      在复制时是保留的，实测无损）。

    ★ `&did=` 带电脑的 `device_id`（AC-5.2）。它防的是一种很隐蔽的
    错配：二维码是**静态**的（用户可能截图存着、或者打印出来贴在
    机箱上），而局域网 IP 是 DHCP 分的。隔几天再扫，同一个 IP 可能
    已经分给了另一台也跑着 PhotoBridge 的电脑 —— 于是"扫码配对"
    会把用户的手机绑到别人的电脑上，还会把对方的照片显示出来。
    带上 device_id 后前端就能发现"这个码不属于这台电脑"。
    """
    url = f"{base_url.rstrip('/')}/#code={quote(code, safe='')}"
    if device_id:
        url += f"&did={quote(device_id, safe='')}"
    return url


# --------------------------------------------------------------------------
# 二维码窗口
# --------------------------------------------------------------------------

def _qr_pil_image(data: str, *, box: int = 8, border: int = 3) -> Any:
    """生成二维码 PIL 图片。

    用 `qrcode` 库而不是自己实现 —— 二维码的纠错码与掩码选择
    很容易写错，而错了的表现是"某些手机扫不出来"，极难排查。
    """
    import qrcode

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box,
        border=border,
    )
    qr.add_data(data)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGB")


#: 同一时刻只允许一个二维码窗口。
#:
#: 用户连点两次"显示配对二维码"会起两个线程、两套 Tk 解释器；而
#: `tkinter._default_root` 是个进程级全局，第二个窗口的 `PhotoImage` 会挂到
#: 第一个解释器上，于是第二个窗口弹不出来、日志里是
#: `TclError: image "pyimage2" doesn't exist`（2026-10-24 实测）。
#: 与其在 Tk 层面绕，不如直接让第二次点击什么也不做 —— 第一个窗口本来
#: 就在屏幕上等着。
_QR_OPEN = threading.Event()


def _entry_rows(url: str, alt_urls: list[str] | None) -> list[tuple[str, str]]:
    """二维码窗口里要列出来的入口地址：`[(标签, 地址), ...]`。

    二维码只能编**一个**地址，而云端入口与局域网入口是两个不同的 origin，
    各自有独立的 localStorage 与 cookie —— 用户想在家直连，就得手动打开
    局域网那条地址单独配一次。所以窗口里必须把几条地址都**列出来**，
    并且能一键复制、一键打开（光显示成一行灰字，用户还得手抄一遍，
    实测真的会抄错）。

    去重按字符串精确比较：`http://a` 与 `http://a/` 视为两条是刻意的，
    后者会被浏览器当成同一个 origin，但混进来只会让窗口变长，
    所以调用方传进来的应当是规范化的地址。
    """
    rows: list[tuple[str, str]] = [("扫码地址", url)]
    seen = {url}
    for u in (alt_urls or []):
        u = (u or "").strip()
        if not u or u in seen:
            continue
        seen.add(u)
        rows.append(("在家直连" if len(rows) == 1 else "其他入口", u))
    return rows


def _make_url_entry(root: Any, url: str, *, primary: bool) -> Any:
    """造一个"内容已经在里面、但用户改不了"的地址框。

    ★ 顺序不能反：`tk.Entry(state="readonly")` 之后再 `insert()` 会被
    **静默丢弃** —— 窗口照样弹出来，只是每条地址都是空白的（2026-10-26
    实测踩到，用户看到的就是"扫码地址被屏蔽了"）。所以先 insert，再
    `configure(state="readonly")`，并且宽度按地址长度算，免得长地址被截。
    """
    import tkinter as tk

    ent = tk.Entry(
        root,
        font=("Consolas", 8),
        relief="flat",
        readonlybackground="#f8fafc",
        fg="#2563eb" if primary else "#059669",
        width=max(24, min(len(url) + 2, 78)),
    )
    ent.insert(0, url)
    ent.configure(state="readonly")
    return ent


def _show_qr_window(title: str, url: str, code: str, ips: list[str],
                    alt_urls: list[str] | None = None,
                    refetch: Any = None,
                    on_ready: Any = None) -> None:
    """弹出一个二维码窗口（阻塞直到用户关闭）。

    在**独立线程**里跑自己的 `tkinter` 主循环。不用主线程是因为
    主线程要跑 asyncio；`tkinter` 要求在主线程创建，所以这里
    自己起一个线程并让它成为那个线程的"主线程"。

    ## `alt_urls` 是干什么的

    二维码只编**一个** URL，而"云端入口"（`https://域名/d/<id>/`）与
    "局域网入口"（`http://192.168.x.x:8787/`）是两个不同的 origin，
    各自有**独立的 localStorage 与 cookie**：在哪个 origin 上配对，
    令牌就落在哪个 origin。所以二维码编哪个，决定了这台手机以后
    默认从哪个入口进来。

    P1 之后二维码编**云端**入口（否则用户出门后打开域名是一个空的
    配对页，而 6 位码要人站在电脑边上才看得到 —— 见 `docs/11` ISSUE-024）。
    代价是在家里也走中继，所以窗口里把局域网地址**明文列出来**：
    用户想在家直连，照着输一次即可（那个 origin 会独立配一次对）。

    ## `refetch` 是干什么的

    配对码只有 5 分钟寿命，二维码里编着码，所以"过期了怎么办"必须能在
    窗口里解决 —— 否则用户得关窗、再点一遍托盘菜单。`refetch()` 应当返回
    `(code, url, ips, alt_urls)`，由调用方（`TrayController._new_pair_code`）
    重新生成；它在**工作线程**里执行，算完再 `root.after` 回到 Tk 线程，
    免得生成过程中的数据库小抖动把窗口卡住。

    ## `on_ready` 是干什么的

    测试用的钩子：控件建好、`mainloop` 之前回调一次，参数是含
    `root / code_label / qr_label / refresh_btn / status / state` 的 dict。
    没有它就只能靠人眼看窗口，而"点刷新后码有没有换"恰恰是最容易悄悄坏掉
    的一环（线程回到 Tk、旧码失效、复制按钮跟不跟着变）。
    """
    def _run() -> None:
        try:
            import tkinter as tk
            from PIL import ImageTk
        except Exception:       # noqa: BLE001
            log.warning("没有可用的图形界面，无法显示二维码", exc_info=True)
            return

        try:
            root = tk.Tk()
        except Exception:       # noqa: BLE001
            log.warning("创建窗口失败（可能无桌面会话）", exc_info=True)
            return

        try:
            root.title(title)
            root.resizable(False, False)

            # ★ 不设 `-topmost`（P1-解释 C：普通窗口，靠用户自己切换）。
            #   早先这里 `root.attributes("-topmost", True)` 是**创建时设一次、
            #   全生命周期不取消**：窗口一旦弹出就压在所有程序之上，切到浏览器
            #   或文件管理器也照样挡着，只能手动关。配对码是"看一眼就够"的
            #   信息，不需要长期占用 z 序顶端；需要时再点托盘菜单即可。
            #   真正要显示到最前的动作是下面的 `deiconify() + lift()`（一次性），
            #   与"永久置顶"是两件事，别混在一起。

            def _close() -> None:
                """关窗的唯一出口：**先显式取消置顶**，再销毁。

                即使当前没设过 topmost（P1-C），也显式清一次：窗口属性是
                按 Tk 解释器存在的状态位，将来谁再把它设成 True，走这条路径
                关窗也不会留下"看不见但仍在置顶"的残留窗口。
                """
                try:
                    root.attributes("-topmost", False)
                except Exception:       # noqa: BLE001
                    pass                # 窗口已经没了
                try:
                    root.destroy()
                except Exception:       # noqa: BLE001
                    pass

            def _copy(text: str, what: str) -> None:
                """复制到剪贴板，并让按钮闪一下"已复制"。

                用 tkinter 自己的剪贴板而不是 `subprocess(\"clip\")`：
                这里本来就有 Tk 解释器，省掉一次进程创建，也不会闪黑框。
                """
                try:
                    root.clipboard_clear()
                    root.clipboard_append(text)
                    # 不 update 的话，有的系统在窗口销毁前读不到内容
                    root.update_idletasks()
                    # ★ 只记长度，不记内容（审查 R-M4-02）：`what` 可能是
                    #   "配对码"，把它写进 INFO 日志等于把一次性的登录凭据
                    #   抄进 `data/logs/*.log`，而日志会被用户随手发给别人。
                    log.info("已复制%s（%d 字符）", what, len(text))
                except Exception:       # noqa: BLE001
                    log.warning("复制%s失败", what, exc_info=True)

            def _open(u: str) -> None:
                # 地址里带 `#code=123456` 片段（ISSUE-051 之后配对码在
                # fragment 里），整条记下来同样会把码写进日志。
                log.info("从二维码窗口打开：%s", u.split("#", 1)[0])
                _open_path(u)

            def _url_row(parent: Any, label: str, u: str, *, primary: bool) -> None:
                row = tk.Frame(parent)
                row.pack(fill="x", padx=16, pady=(3, 0))
                tk.Label(row, text=label, font=("Segoe UI", 8), fg="#6b7280",
                         width=9, anchor="w").pack(side="left")
                # 用只读 Entry 而不是 Label：用户能点进去 Ctrl+A/Ctrl+C，
                # 也能整段选中看一眼自己复制的是什么。
                ent = _make_url_entry(row, u, primary=primary)
                ent.pack(side="left", fill="x", expand=True, ipady=2)
                ent.bind("<Button-1>", lambda _e, w=ent: w.select_range(0, "end"))
                ent.bind("<Double-Button-1>", lambda _e, x=u: _open(x))
                ent.bind("<Return>", lambda _e, x=u: _open(x))
                tk.Button(row, text="打开", width=5, font=("Segoe UI", 8),
                          command=lambda x=u: _open(x)).pack(side="left", padx=(4, 0))
                tk.Button(row, text="复制", width=5, font=("Segoe UI", 8),
                          command=lambda x=u: _copy(x, "地址")).pack(side="left", padx=(4, 0))

            img = _qr_pil_image(url)
            # ★ 必须显式给 `master`：不给的话 `ImageTk.PhotoImage` 会挂到
            # `tkinter._default_root` 上，而那个全局变量指向上一次创建的
            # 那个 Tk 解释器 —— 用户关掉二维码窗口再点一次时，它指向的是
            # 已经 `destroy()` 的解释器，于是 Label 引用一张"别的解释器里的
            # 图片"，抛 `TclError: image "pyimage2" doesn't exist`，窗口弹不
            # 出来（2026-10-24 实测：连点两次必现）。
            photo = ImageTk.PhotoImage(img, master=root)
            qr_label = tk.Label(root, image=photo)
            qr_label.pack(padx=16, pady=(16, 8))
            # 保持引用：PhotoImage 被 GC 后窗口里就是一片空白
            root._qr_photo = photo            # type: ignore[attr-defined]

            # 刷新会换掉码（连着 URL 一起换），所以"当前是哪个码"必须存在
            # 变量里而不是闭包里绑死 —— 否则点一下刷新，复制按钮还给你旧的。
            state = {"code": code, "url": url}

            code_row = tk.Frame(root)
            code_row.pack(pady=(0, 4))
            code_label = tk.Label(code_row, text=code, font=("Segoe UI", 30, "bold"),
                                  cursor="hand2")
            code_label.pack(side="left")
            code_label.bind("<Button-1>", lambda _e: _copy(state["code"], "配对码"))
            tk.Button(code_row, text="复制码", font=("Segoe UI", 8),
                      command=lambda: _copy(state["code"], "配对码")).pack(side="left", padx=(10, 0))
            tk.Label(root, text="在手机上扫码，或手动输入上面 6 位数字",
                     font=("Segoe UI", 10)).pack()

            # 地址不是给人"抄"的，是给人"点"的：一行一个入口，
            # 双击或按「打开」直接在浏览器里跑起来，按「复制」进剪贴板。
            tk.Frame(root, height=6).pack()
            rows_holder = tk.Frame(root)
            rows_holder.pack(fill="x")
            ips_label = tk.Label(root, text="", font=("Segoe UI", 8),
                                 fg="#9ca3af", wraplength=420)
            ips_label.pack(padx=16, pady=(4, 0))

            def _paint_rows(u: str, alts: list[str] | None) -> None:
                for child in rows_holder.winfo_children():
                    child.destroy()
                for i, (label, addr) in enumerate(_entry_rows(u, alts)):
                    _url_row(rows_holder, label, addr, primary=(i == 0))

            def _paint_ips(ips_now: list[str]) -> None:
                others = "  ".join(f"http://{ip}" for ip in ips_now[1:])
                ips_label.configure(text=f"其他网卡：{others}" if others else "")

            _paint_rows(url, alt_urls)
            _paint_ips(ips)

            status = tk.Label(root, text="", font=("Segoe UI", 8), fg="#b45309")
            status.pack(pady=(6, 0))

            def _apply(res: Any) -> None:
                refresh_btn.configure(state="normal", text="刷新二维码和配对码")
                if res is None:
                    status.configure(text="刷新失败，详见 data/logs/agent.log")
                    return
                new_code, new_url, new_ips, new_alts = res
                state["code"], state["url"] = new_code, new_url
                code_label.configure(text=new_code)
                try:
                    new_photo = ImageTk.PhotoImage(_qr_pil_image(new_url), master=root)
                    qr_label.configure(image=new_photo)
                    # 旧图必须被引用到新图之后才能放手，否则中间会有
                    # 一帧白框（Tk 会立刻回收已无引用的 PhotoImage）
                    root._qr_photo = new_photo    # type: ignore[attr-defined]
                except Exception:       # noqa: BLE001
                    log.warning("刷新二维码图片失败", exc_info=True)
                _paint_rows(new_url, new_alts)
                _paint_ips(new_ips)
                status.configure(text=f"已刷新 {time.strftime('%H:%M:%S')}，"
                                      f"旧码立即失效（新码 5 分钟内有效）")
                try:
                    root.update_idletasks()
                except Exception:       # noqa: BLE001
                    pass

            def _refresh() -> None:
                if refetch is None:
                    status.configure(text="这个窗口没有提供刷新入口")
                    return
                refresh_btn.configure(state="disabled", text="正在刷新…")
                status.configure(text="")
                log.info("用户手动刷新二维码与配对码")

                def _work() -> None:
                    try:
                        res = refetch()
                    except Exception:       # noqa: BLE001
                        log.exception("刷新配对码失败")
                        res = None
                    try:
                        # Tk 只能在自己的线程里碰，回主循环去改控件
                        root.after(0, lambda: _apply(res))
                    except Exception:       # noqa: BLE001
                        pass                # 窗口已经关了，改不了就算了

                threading.Thread(target=_work, name="photobridge-qr-refresh",
                                 daemon=True).start()

            refresh_btn = tk.Button(root, text="刷新二维码和配对码",
                                    font=("Segoe UI", 9), command=_refresh)
            refresh_btn.pack(pady=(8, 0))
            tk.Label(root, text="配对码 5 分钟内有效；想换一个就点上面的刷新",
                     font=("Segoe UI", 8), fg="#9ca3af").pack(pady=(2, 0))

            tk.Button(root, text="关闭", width=12, command=_close).pack(pady=12)

            # 右上角 × 与 Alt+F4 也走同一个出口（"关闭"按钮之外的两条关闭路径）。
            root.protocol("WM_DELETE_WINDOW", _close)

            if on_ready is not None:
                on_ready({
                    "root": root,
                    "qr_label": qr_label,
                    "code_label": code_label,
                    "refresh_btn": refresh_btn,
                    "status": status,
                    "state": state,
                    "_refresh": _refresh,
                    "_close": _close,
                })

            root.update_idletasks()
            # 居中
            w, h = root.winfo_width(), root.winfo_height()
            x = (root.winfo_screenwidth() - w) // 2
            y = (root.winfo_screenheight() - h) // 3
            root.geometry(f"+{max(0, x)}+{max(0, y)}")

            # ★ 显式"显示并提到最前" —— 这是**一次性**的抬升，不是置顶。
            # 实测过窗口被创建出来、尺寸内容都对，但 `IsWindowVisible()`
            # 返回 False 的情形 —— 用户看到的是"托盘就绪了，但没有窗口"。
            # `deiconify()` 解决 withdrawn 状态；`lift()` + `focus_force()`
            # 只在**打开的那一瞬**把窗口抬到前面并给一次焦点，之后用户切到
            # 别的程序就不会再被抢回来（P1-解释 C：不做置顶，靠用户自己切换）。
            try:
                root.deiconify()
                root.lift()
                root.focus_force()
            except Exception:       # noqa: BLE001
                pass

            root.mainloop()
        except Exception:       # noqa: BLE001
            log.exception("二维码窗口异常")
        finally:
            # 统一走 `_close()`：先取消置顶再销毁。若异常发生在 `_close`
            # 定义之前（例如 `root.title()` 抛），退回到裸 destroy。
            closer = locals().get("_close")
            if closer is not None:
                closer()
            else:
                try:
                    root.destroy()
                except Exception:       # noqa: BLE001
                    pass
            _QR_OPEN.clear()

    if _QR_OPEN.is_set():
        log.info("二维码窗口已经开着，忽略这次点击")
        return
    _QR_OPEN.set()
    threading.Thread(target=_run, name="photobridge-qr", daemon=True).start()


# --------------------------------------------------------------------------
# 托盘控制器
# --------------------------------------------------------------------------

class TrayController:
    """把 asyncio 服务与 pystray 图标连起来。

    生命周期：`serve()` 在端口监听成功后调 `on_ready(transport, state, loop, stop)`，
    这里创建图标并启动托盘线程；用户点"退出"时通过
    `loop.call_soon_threadsafe(stop.set)` 让服务优雅关闭。
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._icon: Any = None
        self._thread: threading.Thread | None = None
        self._loop: Any = None
        self._stop: Any = None
        self._transport: Any = None
        self._state: Any = None
        self._poll_thread: threading.Thread | None = None
        self._known_devices: int | None = None

    # ------------------------------------------------------------------
    # 从 serve() 调用
    # ------------------------------------------------------------------

    def on_ready(self, transport: Any, state: Any, loop: Any, stop: Any) -> None:
        self._transport = transport
        self._state = state
        self._loop = loop
        self._stop = stop

        try:
            import pystray
        except Exception:       # noqa: BLE001
            log.warning("未安装 pystray，托盘不可用（服务照常运行）")
            return

        urls = list(getattr(transport, "urls", []) or [])
        lan = [u for u in urls if "127.0.0.1" not in u and "localhost" not in u]
        tip = lan[0] if lan else (urls[0] if urls else "未监听")

        try:
            menu = pystray.Menu(
                pystray.MenuItem("显示配对二维码", self._on_show_qr,
                                 default=True),
                pystray.MenuItem("复制手机访问地址", self._on_copy_url,
                                 enabled=bool(lan)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("打开手机端网页", self._on_open_browser),
                pystray.MenuItem("打开照片库文件夹", self._on_open_library),
                pystray.MenuItem("打开日志", self._on_open_log),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("设备列表", self._on_devices),
                pystray.MenuItem("重新扫描照片库", self._on_rescan),
                pystray.MenuItem("开机自启", self._on_toggle_autostart,
                                 checked=lambda _item: _autostart_on()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", self._on_quit),
            )
            self._icon = pystray.Icon(
                "PhotoBridge",
                icon=build_icon_image(),
                title=f"PhotoBridge —— {tip}",
                menu=menu,
            )
        except Exception:       # noqa: BLE001
            log.exception("创建托盘图标失败（服务照常运行）")
            return

        self._thread = threading.Thread(target=self._run_icon, name="photobridge-tray",
                                        daemon=True)
        self._thread.start()

        self._poll_thread = threading.Thread(target=self._poll_devices,
                                             name="photobridge-tray-poll", daemon=True)
        self._poll_thread.start()

        global _active
        _active = self

        log.info("系统托盘已就绪（%s）", tip)

        # ★ 首次运行时主动把配对码摆到用户眼前。
        # 没有这一步，用户双击 exe 后看到的是"什么都没发生" —— 程序没有
        # 主窗口，托盘图标又被 Windows 收进折叠区，配对码完全无从发现。
        self._maybe_show_qr_on_start()

    def _run_icon(self) -> None:
        try:
            self._icon.run()
        except Exception:       # noqa: BLE001
            log.exception("托盘图标线程退出")

    # ------------------------------------------------------------------
    # 菜单动作
    # ------------------------------------------------------------------

    def _lan_urls(self) -> list[str]:
        urls = list(getattr(self._transport, "urls", []) or [])
        lan = [u for u in urls if "127.0.0.1" not in u and "localhost" not in u]
        return lan or urls

    def _cloud_entry(self) -> str:
        """云端入口基址 `https://域名/d/<云端 device_id>`，不可用时空串。

        ★ 为什么二维码要编**云端**地址而不是局域网地址

        两条通路的 origin 必然不同（`http://192.168.x.x:8787` vs
        `https://域名`），而 localStorage 与 cookie 都按 origin 隔离。
        一期只编局域网地址，于是用户出门打开域名时 LocalStorage 是空的
        —— 手机回到配对页，而配对码要人站在电脑边上看（`docs/11`
        ISSUE-024 有浏览器实测证据）。让二维码直接指云端入口，用户
        第一次配对就发生在云端 origin 上，令牌、cookie、图片全同源，
        出门回家都能用；代价是在家里也走中继（窗口里另外列出局域网
        地址，想要直连的人照着输一次即可）。

        取不到就返回空串，让调用方回退到局域网 —— 二维码编一个连不上
        的地址比"编局域网地址"更糟：用户会以为程序坏了。
        """
        st = self._state
        if st is None:
            return ""
        try:
            from .tunnel import get_tunnel

            client = get_tunnel()
            if client is None:
                return ""
            base = str(client.public_base() or "").strip()
            # 只有隧道**真的连着**才把二维码指过去。`public_base()` 在
            # 配置齐备但连不上时也会算出地址（它是纯函数），拿它编二维码
            # 会得到一个"手机打不开"的码。
            if not base or not bool(client.status().get("connected")):
                return ""
            return base
        except Exception:       # noqa: BLE001 —— 托盘任何功能都不该因此崩
            log.debug("读取云端入口失败", exc_info=True)
            return ""

    def _new_pair_code(self) -> tuple[str, str, list[str]] | None:
        """生成一枚新配对码，返回 (code, url, ips)。失败返回 None。"""
        st = self._state
        if st is None:
            return None
        from .auth import (PAIR_CODE_TTL, generate_pair_code, purge_expired_codes,
                           revoke_all_codes, store_pair_code)

        try:
            revoke_all_codes(st.db)
            purge_expired_codes(st.db)
            code = generate_pair_code()
            store_pair_code(st.db, code, st.device_id, st.device_name)
        except Exception:       # noqa: BLE001
            log.exception("生成配对码失败")
            return None

        ips = local_ipv4_addresses()
        lan = self._lan_urls()
        lan_base = lan[0] if lan else (f"http://127.0.0.1:{self._port()}")
        # 云端可用就编云端入口（见 `_cloud_entry` 的长注释）；否则退回局域网。
        base = self._cloud_entry() or lan_base
        if base != lan_base:
            log.info("二维码指向云端入口 %s（局域网 %s 作为备选）", base, lan_base)
        # ★ 配对码同时写进日志：窗口是"额外能力"（无桌面会话、Tk 异常、
        # 连点冲突都可能让窗口弹不出来），而配对码本身是当次交互的一次性
        # 凭据、5 分钟后失效 —— 写下来比"用户看不到码、以为程序坏了"划算。
# ★ 审计 F05：这里原来把 6 位码原样写进日志（`%s`）—— 能读
            #   `data/logs/agent.log` 的人就多了一次 5 分钟内的配对机会。
            log.info("托盘生成配对码（%d 秒内有效，码本身不落日志）",
                     int(PAIR_CODE_TTL))
        return code, pair_qr_url(base, code, st.device_id), ips

    def _port(self) -> int:
        return int(self.cfg.server.get("port", 8787))

    def _alt_urls_for(self, url: str) -> list[str]:
        """二维码编的是云端入口时，返回要列出来的局域网备选地址。

        判据用"URL 是否以某个局域网地址开头"而不是"再问一次隧道"：
        二维码到底编了哪个地址，只有这个 URL 说了算，重新推导一遍
        会在隧道恰好断开的一瞬间给出与二维码不一致的结论。

        `transport.urls` 里尾斜杠形态不固定（`http://ip:port` 与
        `http://ip:port/` 都可能出现），所以这里去重 —— 同一个地址在
        窗口里出现两次，用户会以为有两个不同的入口。
        """
        lan = self._lan_urls()
        if any(url.startswith(u.rstrip("/") + "/") for u in lan):
            return []       # 二维码本来就是局域网地址，不需要备选
        out: list[str] = []
        for u in lan:
            entry = u.rstrip("/") + "/"
            if entry not in out:
                out.append(entry)
        return out

    def _on_show_qr(self, *_: Any) -> None:
        # ★ 先看窗口闸门**再**生成配对码：反过来的话，用户连点两次会先
        # 用掉一个码（让屏幕上正显示的那个失效）、然后什么也不显示 ——
        # 用户拿到的码和屏幕上的不一致，只会更难排查。
        if _QR_OPEN.is_set():
            log.info("二维码窗口已经开着，忽略这次点击（沿用屏幕上那个码）")
            return
        got = self._new_pair_code()
        if got is None:
            notify_error("生成配对码失败，详见 data/logs/agent.log")
            return
        code, url, ips = got

        def _refetch() -> tuple[str, str, list[str], list[str]] | None:
            """窗口里点「刷新」时重新生成一枚码（顺带重算入口地址）。"""
            again = self._new_pair_code()
            if again is None:
                return None
            new_code, new_url, new_ips = again
            return new_code, new_url, new_ips, (self._alt_urls_for(new_url) or [])

        _show_qr_window("PhotoBridge 配对", url, code, ips,
                        self._alt_urls_for(url) or None, refetch=_refetch)

    def _has_paired_device(self) -> bool:
        """是否已经有手机配对过（未撤销的）。"""
        st = self._state
        if st is None:
            return True         # 状态还没就绪时不要弹窗，宁可不动
        try:
            return bool(
                st.db.scalar(
                    "SELECT 1 FROM devices WHERE revoked = 0 LIMIT 1",
                    default=None,
                )
            )
        except Exception:       # noqa: BLE001
            log.exception("查询已配对设备失败")
            return True         # 查不到就当"已配对"，避免多余弹窗打扰

    def _maybe_show_qr_on_start(self) -> None:
        """★ 首次运行主动弹出配对窗口 —— 否则用户根本找不到配对码。

        ## 问题（用户实际反馈："配对码是隐藏的"）

        配对窗口原先**只能**通过托盘右键菜单「显示配对二维码」打开，启动时
        完全不出现。而 Windows 默认把新出现的托盘图标收进折叠区（那个 `^`
        小箭头里），用户双击 exe 之后看到的是"什么都没有发生"：

          * 没有主窗口（本程序是托盘常驻的，本来就没有窗口）；
          * 托盘图标藏在折叠区里，看不见；
          * 于是"配对码在哪"完全无解 —— 只能去翻文档。

        这不是界面风格问题，是**首次使用被卡死在第一步**。

        ## 做法

        仅在「一台手机都没配对过」时自动弹一次。已有配对设备就不再打扰 ——
        那种场景用户是重启程序，弹窗只会碍事。

        在独立线程里延迟几秒再弹：`on_ready` 是在服务刚监听成功时调用的，
        此刻托盘图标本身还在初始化，抢着开第二个 Tk 窗口容易互相干扰。
        延迟也让"先看到托盘图标出现、再看到配对窗口"的顺序更自然。
        失败一律吞掉 —— 弹不出窗口不该影响服务本身。
        """
        if self._has_paired_device():
            return

        def _later() -> None:
            time.sleep(2.0)
            try:
                self._on_show_qr()
            except Exception:       # noqa: BLE001
                log.exception("首次运行自动弹出配对窗口失败（不影响服务）")

        threading.Thread(
            target=_later, name="photobridge-first-pair", daemon=True,
        ).start()

    def _on_copy_url(self, *_: Any) -> None:
        lan = self._lan_urls()
        if not lan:
            notify_error("没有可用的局域网地址，请检查网络连接")
            return
        text = lan[0]
        try:
            import subprocess
            # `clip` 是 Windows 自带的，不用额外依赖 pyperclip
            subprocess.run("clip", input=text.encode("utf-16le"),
                           check=True, shell=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._notify("已复制", text)
        except Exception:       # noqa: BLE001
            log.warning("复制到剪贴板失败", exc_info=True)
            notify_error(f"复制失败，地址是 {text}")

    def _on_open_browser(self, *_: Any) -> None:
        lan = self._lan_urls()
        if not lan:
            notify_error("没有可用的局域网地址")
            return
        _open_path(lan[0])

    def _on_open_library(self, *_: Any) -> None:
        roots = [r for r in self.cfg.roots if r.enabled]
        if not roots:
            notify_error("还没有配置照片库目录")
            return
        _open_path(str(roots[0].path))

    def _on_open_log(self, *_: Any) -> None:
        p = self.cfg.logs_dir / "agent.log"
        if not p.exists():
            notify_error("还没有日志文件")
            return
        _open_path(str(p))

    def _on_devices(self, *_: Any) -> None:
        st = self._state
        if st is None:
            return
        try:
            rows = st.db.query(
                "SELECT name, revoked, last_seen, expires_at FROM devices"
                " ORDER BY created_at DESC"
            )
        except Exception:       # noqa: BLE001
            log.exception("读取设备列表失败")
            return
        if not rows:
            self._notify("设备列表", "还没有手机配对过")
            return
        now = time.time()
        lines = []
        for r in rows:
            mark = "（已撤销）" if r["revoked"] else ""
            # AC-5.10：过期状态必须在托盘上看得见。手机掉了但没撤销时，
            # token 会自己过期 —— 用户需要知道它什么时候失效。
            exp = r["expires_at"]
            if not mark and exp is not None:
                if exp <= now:
                    mark = "（已过期）"
                elif exp - now < 7 * 86400:
                    mark = f"（{(exp - now) / 86400:.0f} 天后过期）"
            lines.append(f"{r['name']}{mark}")
        self._notify(f"已配对 {len(rows)} 台设备", "\n".join(lines[:8]))

    def _on_rescan(self, *_: Any) -> None:
        """触发一次全量重扫（在线程池里跑，不阻塞托盘）。"""
        st = self._state
        loop = self._loop
        if st is None or loop is None:
            return

        def _work() -> None:
            try:
                import asyncio as aio

                fut = aio.run_coroutine_threadsafe(st.indexer.scan_all(), loop)
                stats = fut.result(timeout=1800)
                total = sum(getattr(s, "seen", 0) for s in stats)
                self._notify("重新扫描完成", f"共检查 {total} 个文件")
            except Exception:       # noqa: BLE001
                log.exception("托盘触发的重扫失败")
                notify_error("重新扫描失败，详见日志")

        threading.Thread(target=_work, name="photobridge-rescan",
                         daemon=True).start()
        self._notify("重新扫描", "已开始，完成后会通知你")

    def _on_toggle_autostart(self, *_: Any) -> None:
        """菜单里的「开机自启」：勾上写入注册表 Run 键，取消则删除。"""
        from .autostart import is_enabled, set_autostart       # noqa: PLC0415

        want = not is_enabled()
        ok = set_autostart(want)
        icon = self._icon
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:   # noqa: BLE001
                pass
        if ok:
            self._notify("开机自启",
                         "已开启：下次登录 Windows 会自动在后台启动" if want
                         else "已关闭：开机不再自动启动")
        else:
            self._notify("开机自启", "设置失败，详见日志")

    def _on_quit(self, *_: Any) -> None:
        log.info("托盘请求退出")
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except Exception:       # noqa: BLE001
                log.warning("通知服务停止失败", exc_info=True)
        self._stop_icon()
        # ★ 兜底：优雅收尾卡住时也要让进程真的消失。
        #
        # 优雅路径 = `stop.set()` → `serve()` 退出 → `_shutdown()`（停监听、
        # 关监听线程、drain 缩略图队列最多 20 秒、checkpoint WAL）。绝大多数
        # 情况它几十毫秒就结束。但托盘程序是 pythonw 启动的**没有控制台**，
        # 一旦某一步卡住，用户看到的就是"点了退出、图标不见了、任务管理器里
        # 还躺着一个 python"——他没有任何办法判断该不该强杀。
        # 所以这里挂一个 15 秒的看门狗：超时直接 `os._exit(0)`。
        # WAL 是 SQLite 崩溃安全格式，强退最坏丢的是"这一次运行的最后几笔"
        # （缩略图缓存与索引都可由重扫重建），比"进程永远残留"好得多。
        timer = threading.Timer(_QUIT_WATCHDOG_SECONDS, _force_exit)
        timer.daemon = True
        timer.start()

    def _stop_icon(self) -> None:
        global _active
        icon = self._icon
        if icon is not None:
            try:
                icon.stop()
            except Exception:       # noqa: BLE001
                pass
        if _active is self:
            _active = None

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------

    def _notify(self, title: str, message: str) -> None:
        icon = self._icon
        if icon is None:
            return
        try:
            icon.notify(message, title)
        except Exception:       # noqa: BLE001
            # 某些 Windows 版本上通知需要额外的 COM 支持，
            # 失败不该影响任何功能。
            log.debug("托盘通知失败", exc_info=True)

    def _poll_devices(self) -> None:
        """轮询设备表，配对成功时弹一次通知。

        为什么用轮询而不是事件总线：配对是**手机**发起的请求，
        它走的是 HTTP 通路；事件总线是给手机端 SSE 用的。
        托盘监听自己的数据库更直接，也不依赖事件循环还活着。
        """
        st = self._state
        if st is None:
            return
        # 第一次只记录基线，不通知（否则启动时会对已有设备误报）
        while True:
            try:
                n = st.db.scalar("SELECT COUNT(*) FROM devices", default=0) or 0
            except Exception:       # noqa: BLE001
                time.sleep(_POLL_SEC)
                continue

            if self._known_devices is None:
                self._known_devices = n
            elif n > self._known_devices:
                try:
                    row = st.db.query_one(
                        "SELECT name FROM devices ORDER BY created_at DESC LIMIT 1"
                    )
                    name = row["name"] if row else "新设备"
                except Exception:       # noqa: BLE001
                    name = "新设备"
                self._known_devices = n
                self._notify("配对成功", f"{name} 已连接")
                log.info("新设备已配对：%s", name)
            elif n < self._known_devices:
                self._known_devices = n

            time.sleep(_POLL_SEC)


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------

def _open_path(target: str) -> None:
    """用系统默认程序打开一个路径（文件或文件夹）。"""
    import subprocess

    try:
        import os

        os.startfile(target)        # type: ignore[attr-defined]  # Windows only
    except AttributeError:
        subprocess.Popen(["xdg-open", target])       # type: ignore[attr-defined]
    except Exception:       # noqa: BLE001
        log.warning("打开 %s 失败", target, exc_info=True)


def notify_error(message: str) -> None:
    """在没有托盘对象时也能弹通知（启动失败路径用）。

    `run()` 里服务还没起来就报错时，`TrayController.on_ready` 从未被调用，
    因此这里新建一个**临时**图标只为了发一条通知 —— 用户双击 exe
    后如果什么反应都没有，他只会以为是程序坏了。
    """
    try:
        import pystray

        icon = pystray.Icon("PhotoBridge", icon=build_icon_image(),
                            title="PhotoBridge")
        icon.run_detached()
        try:
            icon.notify(message, "PhotoBridge 启动失败")
        except Exception:       # noqa: BLE001
            log.debug("错误通知发送失败", exc_info=True)
        time.sleep(6)
        icon.stop()
    except Exception:       # noqa: BLE001
        log.debug("无法弹出错误通知", exc_info=True)
