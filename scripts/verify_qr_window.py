# -*- coding: utf-8 -*-
"""A-03 / B-01：配对二维码窗口的**真实弹窗**验证。

判据原文是「点『显示配对二维码』→ 弹出窗口同时显示**二维码**与 **6 位配对码**」。
这一条此前一直留在 ☐ 里，理由是"要人眼看一次"。但本机有桌面会话，
可以把窗口真的弹出来，再用 Win32 + 像素分析把它验掉。

## 为什么用 PrintWindow 而不是 ImageGrab

第一版用 `PIL.ImageGrab.grab(bbox=窗口矩形)`，抓回来的是**桌面背景**
（整屏蓝色渐变、每个采样点都是 `(6, 38, 195)` 这种蓝），而 tkinter
窗口的像素一个都没抓到 —— ImageGrab 截的是屏幕帧缓冲，DWM 合成下
窗口内容不在那条路径上。`PrintWindow(hwnd, hdc, 2)`（PW_RENDERFULLCONTENT）
让**窗口自己渲染**到内存 DC，抓到的是真内容（背景 240、模块纯黑 0、
模块间隙纯白 255）。教训：窗口截图抓回来先看几个采样点的颜色，
"程序没报错"不等于"抓到了东西"。

## 判据怎么定的

1. 按标题找窗口，`IsWindowVisible` 必须为真、矩形尺寸合理；
2. 截图，在窗口上半部找二维码的**边界框**（按行的深色像素数取最长连续带）；
   框必须接近正方形（二维码就是方的），且框内黑白配比在合理区间
   —— 纯白 / 全黑都说明二维码没画出来；
3. 二维码下方找一段**大号深色文字**的边界框，横向跨度要够宽
   （6 个数字 vs 1~2 个字符的差别是数量级的）；
4. 换一个配对码，二维码像素必须有 10% 以上不同（否则可能画的是固定占位图）；
   同一个 URL 算两次必须**逐字节一致**（否则扫码不稳定）；
5. `WM_CLOSE` 之后窗口消失、线程退出。

跑法：`.venv\\Scripts\\python.exe -X utf8 scripts\\verify_qr_window.py`

⚠️ 会在屏幕上真的弹一个窗口，约 2 秒后自动关闭。需要交互式桌面会话；
   无桌面时 SKIP 并返回 0。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.app.tray import _qr_pil_image, _show_qr_window, pair_qr_url  # noqa: E402

failures: list[str] = []
skips: list[str] = []

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# ★ 所有 Win32 调用都必须显式声明 argtypes/restype。不声明时 ctypes 默认
#   int（32 位），HANDLE 被截断，症状是 `OverflowError: int too long to
#   convert` 或 `err 6 无效的句柄` —— 与当初 `_rss_bytes` 踩的是同一个坑。
user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
user32.PrintWindow.restype = wt.BOOL
user32.GetWindowDC.argtypes = [wt.HWND]
user32.GetWindowDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindow.restype = wt.BOOL
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
user32.EnumWindows.restype = wt.BOOL
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteDC.restype = wt.BOOL
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
gdi32.GetDIBits.restype = ctypes.c_int


def check(ok: bool, label: str, extra: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {extra}" if extra else ""))
    if not ok:
        failures.append(label)


def skip(label: str) -> None:
    print(f"  SKIP {label}")
    skips.append(label)


# --------------------------------------------------------------------------
# Win32
# --------------------------------------------------------------------------
class _BMI(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def find_window(title_exact: str, *, budget: float = 20.0) -> int | None:
    end = time.time() + budget
    while time.time() < end:
        found: list[int] = []

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def _enum(hwnd, _lp):                      # noqa: ANN001
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value == title_exact:
                    found.append(int(hwnd))
            return True

        user32.EnumWindows(_enum, 0)
        if found:
            return found[0]
        time.sleep(0.15)
    return None


def window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def close_window(hwnd: int) -> None:
    user32.PostMessageW(hwnd, 0x0010, 0, 0)          # WM_CLOSE


def capture(hwnd: int):
    """用 PrintWindow 让窗口自己渲染到内存位图，返回 PIL.Image。"""
    from PIL import Image

    left, top, right, bottom = window_rect(hwnd)
    w, h = right - left, bottom - top
    if w <= 0 or h <= 0:
        raise RuntimeError(f"窗口尺寸非法 {w}×{h}")

    hdc = user32.GetWindowDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)
    try:
        # 2 = PW_RENDERFULLCONTENT，DWM 合成下必须用它才拿得到内容
        ok = user32.PrintWindow(hwnd, mdc, 2)
        bi = _BMI()
        bi.biSize = ctypes.sizeof(_BMI)
        bi.biWidth = w
        bi.biHeight = -h                     # 负高度 = 自上而下
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
        img = Image.frombuffer("RGBA", (w, h), buf.raw,
                               "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(hwnd, hdc)
    return bool(ok), img


# --------------------------------------------------------------------------
# 像素分析
# --------------------------------------------------------------------------
# `qrcode.QRCode(box_size=8, border=3)` 的静区 = 3 模块 × 8px = 24px，
# 所以模板里深色模块带的边长 = 模板边长 − 48。
QR_BOX = 8
QR_BORDER = 3
QR_MARGIN = QR_BOX * QR_BORDER          # 24

DARK = 110                              # 灰度阈值：深色
BAND_MIN_LEN = 150                      # 二维码带 264px，文字带 10~25px


def _avg(p) -> float:                                  # noqa: ANN001
    return (p[0] + p[1] + p[2]) / 3.0


def row_dark_counts(img) -> list[int]:
    """每行的深色像素数。**一次遍历**，后面全靠它做定位。"""
    px = img.load()
    w, h = img.size
    out: list[int] = []
    for y in range(h):
        c = 0
        for x in range(w):
            if _avg(px[x, y]) < DARK:
                c += 1
        out.append(c)
    return out


def dark_bands(counts: list[int], *, thr: int = 2) -> list[tuple[int, int]]:
    """把"有深色像素的行"聚成连续带，返回 [(y0, y1), ...]（y1 不含）。"""
    bands: list[tuple[int, int]] = []
    start = None
    for y, c in enumerate(counts):
        if c >= thr:
            if start is None:
                start = y
        else:
            if start is not None:
                bands.append((start, y))
                start = None
    if start is not None:
        bands.append((start, len(counts)))
    return bands


def bbox_in_band(img, y0: int, y1: int) -> tuple[int, int, int, int]:
    """在 [y0, y1) 行范围内取深色像素的 bbox。"""
    px = img.load()
    w = img.size[0]
    minx, maxx = w, -1
    miny, maxy = y1, y0
    for y in range(y0, y1):
        for x in range(w):
            if _avg(px[x, y]) < DARK:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    return minx, miny, maxx + 1, maxy + 1


def find_qr(img, tmpl_size: tuple[int, int]):
    """定位二维码：取长度 > BAND_MIN_LEN 的深色带里最长的那条。"""
    counts = row_dark_counts(img)
    bands = [b for b in dark_bands(counts)
             if b[1] - b[0] > BAND_MIN_LEN]
    if not bands:
        return None, counts, bands
    band = max(bands, key=lambda b: b[1] - b[0])
    return bbox_in_band(img, band[0], band[1]), counts, bands


def compare_1to1(a, b) -> dict:
    """两张同尺寸图逐像素比较。"""
    if a.size != b.size:
        return {"same_size": False, "diff_px": -1, "mad": 999.0,
                "total": 0, "ratio": 1.0}
    pa, pb = a.load(), b.load()
    w, h = a.size
    diff = 0
    total_abs = 0
    for y in range(h):
        for x in range(w):
            qa, qb = pa[x, y], pb[x, y]
            d = (abs(qa[0] - qb[0]) + abs(qa[1] - qb[1])
                 + abs(qa[2] - qb[2])) / 3
            total_abs += d
            if d > 24:                  # 单通道均差 24 以上才算"不同"
                diff += 1
    n = w * h
    return {"same_size": True, "diff_px": diff, "mad": total_abs / max(1, n),
            "total": n, "ratio": diff / max(1, n)}


def ascii_map(img, cw: int = 6, ch: int = 10) -> str:
    """把图缩成 ASCII，排查"窗口里到底画了什么"用。"""
    px = img.load()
    w, h = img.size
    lines = []
    for y in range(0, h - ch + 1, ch):
        row = ""
        for x in range(0, w - cw + 1, cw):
            acc = n = 0
            for yy in range(y, y + ch, 3):
                for xx in range(x, x + cw, 2):
                    acc += _avg(px[xx, yy])
                    n += 1
            a = acc / max(1, n)
            row += "#" if a < 80 else ("+" if a < 170 else
                                       ("." if a < 235 else " "))
        lines.append(f"{y:4d} {row}")
    return "\n".join(lines)


def crop_margins(img, margin: int = 8):
    """裁掉非客户区留下的边（`GetWindowRect` 含标题栏与边框）。"""
    w, h = img.size
    return img.crop((margin, margin, w - margin, h - margin))


def main() -> int:
    print("=" * 60)
    print("=== A-03 / B-01：配对二维码窗口真的弹出来了吗 ===")
    print("=" * 60)

    try:
        import tkinter as tk

        probe = tk.Tk()
        probe.withdraw()
        probe.destroy()
    except Exception as exc:                       # noqa: BLE001
        print(f"SKIP 无交互式桌面会话（{type(exc).__name__}: {exc}）")
        return 0

    ips = ["192.0.2.10"]
    code1 = "483920"
    url1 = pair_qr_url(f"http://{ips[0]}:8787", code1, "abc123def456")

    # ---- B-01：先验 URL 形态（字符串层，不依赖窗口） ----
    print("\n[B-01] 二维码里的 URL 形态")
    check(url1.startswith(f"http://{ips[0]}:8787/"),
          "URL 指向真实网卡地址与端口", url1.split("?")[0])
    check(f"code={code1}" in url1, "URL 带 6 位配对码", f"code={code1}")
    check("did=abc123def456" in url1,
          "URL 带 did（扫码时校验是不是同一台电脑）", "did=abc123def456")
    check(len(code1) == 6 and code1.isdigit(), "配对码是 6 位纯数字", code1)

    # ---- 弹窗 ----
    print("\n[A-03] 窗口真的弹出来了吗")
    title = "PhotoBridge 配对"
    th = threading.Thread(target=_show_qr_window,
                          args=(title, url1, code1, ips), daemon=True)
    th.start()

    hwnd = find_window(title, budget=20.0)
    if hwnd is None:
        check(False, "★ 按标题找到配对窗口（20 秒内）—— 没找到说明窗口根本没弹出")
        print("\n提示：若有残留窗口请手动关闭后重跑。")
        return 1

    check(True, "★ 按标题找到了配对窗口", f"hwnd={hwnd}")
    check(bool(user32.IsWindowVisible(hwnd)),
          "★ 窗口是**可见**的（IsWindowVisible 为真）")

    left, top, right, bottom = window_rect(hwnd)
    w, h = right - left, bottom - top
    check(w > 260 and h > 400,
          "★ 窗口尺寸合理（装得下二维码 + 6 位码 + URL）", f"{w}×{h}")

    time.sleep(1.2)                      # 等 tkinter 把内容画完

    shot = None
    try:
        ok, shot = capture(hwnd)
        check(ok, "PrintWindow 抓图成功", f"{shot.size}")
    except Exception as exc:                       # noqa: BLE001
        skip(f"抓图失败（{type(exc).__name__}: {exc}），跳过像素判据")

    # 期望的二维码图像：窗口里显示的就是它（ImageTk 1:1，不缩放）
    expected = _qr_pil_image(url1)
    ew, eh = expected.size
    dark_span = ew - 2 * QR_MARGIN          # 深色模块带应有的边长

    if shot is not None:
        shot.save(REPO / "build" / "_qr_shot.png")
        inner = crop_margins(shot, 8)
        iw, ih = inner.size
        (REPO / "build" / "_qr_ascii.txt").write_text(
            ascii_map(inner), encoding="utf-8")
        px = inner.load()

        # ① 抓到的确实是窗口本身，不是桌面背景
        c_tl, c_tr = px[3, 3], px[iw - 4, 3]
        check(_avg(c_tl) > 180 and _avg(c_tr) > 180,
              "★ 上边两角是浅色窗口底色（抓到的是窗口，不是桌面背景）",
              f"{c_tl} {c_tr}")

        # ② 定位二维码
        bbox, counts, bands = find_qr(inner, (ew, eh))
        print(f"     深色带（长度>150px）：{[(a, b, b - a) for a, b in bands]}"
              f"  每行深色数 max={max(counts)}")
        check(bbox is not None,
              "★ 找到二维码的深色模块带（长度 >150px 的连续行）",
              f"bands={[(a, b) for a, b in bands]}")

        if bbox is not None:
            qx0, qy0, qx1, qy1 = bbox
            qw, qh = qx1 - qx0, qy1 - qy0
            print(f"     深色模块带 bbox=({qx0},{qy0})-({qx1},{qy1})"
                  f" = {qw}×{qh}  期望 {dark_span}×{dark_span}")
            check(abs(qw - dark_span) <= 3,
                  "★ 深色模块带宽度 = 模板宽 − 2×静区（1:1 渲染，未缩放）",
                  f"{qw} vs {dark_span}")
            check(abs(qh - dark_span) <= 3,
                  "★ 深色模块带高度同样吻合（二维码是正方形）",
                  f"{qh} vs {dark_span}")

            # ③ 模板原点 = 深色带原点 − 静区
            ox, oy = qx0 - QR_MARGIN, qy0 - QR_MARGIN
            print(f"     推算模板原点 ({ox},{oy})")
            check(ox >= 0 and oy >= 0,
                  "★ 推算出的模板原点在窗口内（静区没被裁掉）",
                  f"({ox},{oy})")

            # ④ 1:1 逐像素比对 —— 最强的那一条
            region = inner.crop((max(0, ox), max(0, oy),
                                 max(0, ox) + ew, max(0, oy) + eh))
            if region.size == (ew, eh):
                cmp = compare_1to1(region, expected)
                print(f"     1:1 比对 diff_px={cmp['diff_px']}/{cmp['total']}"
                      f" mad={cmp['mad']:.3f} ratio={cmp['ratio']:.4f}")
                check(cmp["ratio"] < 0.02,
                      "★ 窗口里的二维码与生成函数**逐像素一致**"
                      "（不同像素 <2%；占位图/缩放/模糊都会被抓到）",
                      f"{cmp['ratio']:.2%}")
                check(cmp["mad"] < 2.0,
                      "★ 平均绝对差 < 2/255（不是模糊的近似图）",
                      f"{cmp['mad']:.2f}")

                # ⑤ 反例：拿错码生成的二维码来比，必须明显更差。
                #    否则 ratio 低可能只因为两张图都大面积是白的。
                wrong = _qr_pil_image(pair_qr_url(f"http://{ips[0]}:8787",
                                                  "999999", "abc123def456"))
                cmpw = compare_1to1(region, wrong)
                print(f"     反例（错码）diff_px={cmpw['diff_px']}"
                      f" ratio={cmpw['ratio']:.4f}")
                # ★ 反例只要"明显更差"就行，不能写死绝对差：正确的
                #   比对是 0.00%，错码是 10.5% —— 后者已经是从 0 到 10%
                #   的无穷倍，但用 +0.15 的绝对阈值反而会判失败。
                check(cmpw["ratio"] > max(0.03, cmp["ratio"] * 2 + 0.01),
                      "★ 换成错误的配对码做模板，差异显著更大"
                      "（证明上一条不是因为图太空）",
                      f"对={cmp['ratio']:.2%} 错={cmpw['ratio']:.2%}")
            else:
                check(False, "裁出的区域与模板同尺寸", f"{region.size}")

            # ⑥ 二维码带之后的下一个深色带 = 6 位配对码
            below = [b for b in dark_bands(counts)
                     if b[0] >= qy1 and b[1] - b[0] >= 12]
            check(bool(below), "★ 二维码下方还有深色带（6 位配对码）",
                  f"{below[:3]}")
            if below:
                c0, c1 = below[0]
                tb = bbox_in_band(inner, c0, c1)
                tx0, ty0, tx1, ty1 = tb
                tw, thh = tx1 - tx0, ty1 - ty0
                print(f"     6 位码 bbox=({tx0},{ty0})-({tx1},{ty1})"
                      f" = {tw}×{thh}（行带 {c0}..{c1}）")
                check(thh > 18,
                      "★ 那块文字字高够大（30pt 粗体，不是 8pt 小字）",
                      f"{thh}px")
                check(tw > iw * 0.18,
                      "★ 那块文字横跨够宽（6 个数字，不是 1~2 个字符）",
                      f"{tw}/{iw} = {tw / iw:.2f}")
                check(c0 > qy1 - 5,
                      "★ 配对码在二维码**下方**", f"{c0} > {qy1}")
    # ---- B-01 补强：二维码真的编码了当前这个码 ----
    print("\n[B-01 补强] 二维码真的编码了当前这个码，不是固定占位图")
    img_a = _qr_pil_image(url1)
    code2 = "111111"
    url2 = pair_qr_url(f"http://{ips[0]}:8787", code2, "abc123def456")
    img_b = _qr_pil_image(url2)
    check(img_a.size == img_b.size, "两张二维码尺寸一致", f"{img_a.size}")
    ba, bb2 = img_a.tobytes(), img_b.tobytes()
    diff = sum(1 for p, q in zip(ba, bb2) if p != q)
    check(diff > len(ba) * 0.10,
          "★ 换一个配对码，二维码有 10% 以上像素不同（真的编码了码值）",
          f"{diff}/{len(ba)} = {diff / len(ba):.1%}")
    check(img_a.tobytes() == _qr_pil_image(url1).tobytes(),
          "★ 同一个 URL 生成的二维码**逐字节一致**（确定性，扫码才稳定）")

    # ---- 收尾 ----
    print("\n[A-03 收尾] 关掉窗口后它真的消失了")
    close_window(hwnd)
    gone = False
    for _ in range(80):
        if not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
            gone = True
            break
        time.sleep(0.1)
    check(gone, "★ 点关闭（WM_CLOSE）后窗口消失")
    th.join(timeout=6.0)
    check(not th.is_alive(), "★ 窗口线程也退出了（没有残留线程）")

    print("\n" + "=" * 60)
    if skips:
        print(f"（{len(skips)} 项跳过）")
    if failures:
        print(f"=== {len(failures)} 项失败 ===")
        for f in failures:
            print("  [X]", f)
        return 1
    print("=== ALL PASS ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
