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

    `?code=` 是前端 `PairView.vue` 认的参数（`pairParamsFromLocation()`），
    扫码后配对码会自动填进输入框，用户只需点"配对"。

    ★ `&did=` 带电脑的 `device_id`（AC-5.2）。它防的是一种很隐蔽的
    错配：二维码是**静态**的（用户可能截图存着、或者打印出来贴在
    机箱上），而局域网 IP 是 DHCP 分的。隔几天再扫，同一个 IP 可能
    已经分给了另一台也跑着 PhotoBridge 的电脑 —— 于是"扫码配对"
    会把用户的手机绑到别人的电脑上，还会把对方的照片显示出来。
    带上 device_id 后前端就能发现"这个码不属于这台电脑"。
    """
    url = f"{base_url.rstrip('/')}/?code={quote(code, safe='')}"
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


def _show_qr_window(title: str, url: str, code: str, ips: list[str]) -> None:
    """弹出一个二维码窗口（阻塞直到用户关闭）。

    在**独立线程**里跑自己的 `tkinter` 主循环。不用主线程是因为
    主线程要跑 asyncio；`tkinter` 要求在主线程创建，所以这里
    自己起一个线程并让它成为那个线程的"主线程"。
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
            root.attributes("-topmost", True)

            img = _qr_pil_image(url)
            photo = ImageTk.PhotoImage(img)

            tk.Label(root, image=photo).pack(padx=16, pady=(16, 8))
            # 保持引用：PhotoImage 被 GC 后窗口里就是一片空白
            root._qr_photo = photo            # type: ignore[attr-defined]

            big = tk.Label(root, text=code, font=("Segoe UI", 30, "bold"))
            big.pack(pady=(0, 4))
            tk.Label(root, text="在手机上扫码，或手动输入上面 6 位数字",
                     font=("Segoe UI", 10)).pack()

            tk.Label(root, text=url, font=("Consolas", 9),
                     fg="#2563eb", wraplength=380).pack(pady=(8, 2), padx=16)
            if len(ips) > 1:
                others = "  ".join(f"http://{ip}" for ip in ips[1:])
                tk.Label(root, text=f"其他网卡：{others}", font=("Segoe UI", 8),
                         fg="#6b7280", wraplength=380).pack(padx=16)

            tk.Label(root, text="配对码 5 分钟内有效，过期后重新生成",
                     font=("Segoe UI", 8), fg="#9ca3af").pack(pady=(6, 0))
            tk.Button(root, text="关闭", width=12, command=root.destroy).pack(pady=12)

            root.update_idletasks()
            # 居中
            w, h = root.winfo_width(), root.winfo_height()
            x = (root.winfo_screenwidth() - w) // 2
            y = (root.winfo_screenheight() - h) // 3
            root.geometry(f"+{max(0, x)}+{max(0, y)}")

            root.mainloop()
        except Exception:       # noqa: BLE001
            log.exception("二维码窗口异常")
        finally:
            try:
                root.destroy()
            except Exception:       # noqa: BLE001
                pass

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
        base = lan[0] if lan else (f"http://127.0.0.1:{self._port()}")
        log.info("托盘生成配对码，%d 秒内有效", int(PAIR_CODE_TTL))
        return code, pair_qr_url(base, code, st.device_id), ips

    def _port(self) -> int:
        return int(self.cfg.server.get("port", 8787))

    def _on_show_qr(self, *_: Any) -> None:
        got = self._new_pair_code()
        if got is None:
            notify_error("生成配对码失败，详见 data/logs/agent.log")
            return
        code, url, ips = got
        _show_qr_window("PhotoBridge 配对", url, code, ips)

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

    def _on_quit(self, *_: Any) -> None:
        log.info("托盘请求退出")
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None:
            try:
                loop.call_soon_threadsafe(stop.set)
            except Exception:       # noqa: BLE001
                log.warning("通知服务停止失败", exc_info=True)
        self._stop_icon()

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
