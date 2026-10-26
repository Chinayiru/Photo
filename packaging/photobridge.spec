# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置（onedir + 无控制台）。

构建：

    .venv/Scripts/python.exe -m PyInstaller packaging/photobridge.spec --noconfirm

产物：`dist/PhotoBridge/PhotoBridge.exe`（整个目录要一起分发）。

## 为什么是 onedir 而不是 onefile

`--onefile` 每次启动都要把整个包解压到 `%TEMP%\\_MEIxxxxxx`：
- 启动多 1~3 秒（用户双击后迟迟没反应）；
- 杀毒软件会对"每次释放一堆 exe/dll 的进程"反复扫描，甚至直接拦；
- 退出后临时目录可能残留（进程被强杀时），几十 MB 垃圾。

代价是分发的是一个目录而不是单个文件 —— 用 zip 或
Inno Setup 打成安装包即可，那本来就是正常的分发方式。

## 为什么是 --noconsole

主界面是手机端的网页，电脑端只需要托盘图标。带控制台的话
用户会看到一个常驻黑框，关掉黑框就退出程序，很容易误操作。

## 关键 hiddenimports / collect

- `pillow_heif`：HEIC 支持靠它的二进制扩展，PyInstaller 的静态
  分析看不到动态加载的 `.pyd`，必须 `collect_all`。
- `uvicorn` 的协议实现（`uvicorn.protocols.*`、`uvicorn.lifespan.*`）
  是字符串动态导入的，不显式声明就会在启动时报
  `ImportError: uvicorn.lifespan.on not found`。
- `pystray._win32` 同理（pystray 按平台挑后端）。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH 由 PyInstaller 注入，值是**本 spec 文件所在目录**
# （即 .../packaging），所以仓库根是它的上一级。
REPO = Path(SPECPATH).resolve().parent      # noqa: F821

# --------------------------------------------------------------------------
# 数据文件
# --------------------------------------------------------------------------
datas = []

# 前端构建产物 → 解包后的 frontend/dist（server._dist_dir 认这个位置）
_frontend = REPO / "frontend" / "dist"
if _frontend.is_dir():
    datas.append((str(_frontend), "frontend/dist"))
else:
    print(f"⚠️  警告：{_frontend} 不存在，打包出的程序将没有网页界面。"
          f"请先在 frontend/ 下运行 npm run build。", file=sys.stderr)

# 默认配置模板（如果以后加了 templates/ 目录，这里会自动带上）
for name in ("templates", "assets"):
    p = REPO / name
    if p.is_dir():
        datas.append((str(p), name))

# --------------------------------------------------------------------------
# 隐藏导入
# --------------------------------------------------------------------------
hiddenimports = [
    "backend",
    "backend.app",
    "backend.app.main",
    "backend.app.tray",
    # uvicorn 的动态导入
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # pystray 的平台后端
    "pystray._win32",
    # 其他
    "PIL._tkinter_finder",
    "watchdog.observers.winapi",
]

# 整包收集（含二进制扩展与数据文件）
for pkg in ("pillow_heif", "pystray", "qrcode", "send2trash"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries = b        # noqa: F841  （在下面统一处理）
        hiddenimports += h
    except Exception as exc:        # noqa: BLE001
        print(f"⚠️  collect_all({pkg}) 失败：{exc}", file=sys.stderr)

binaries = []
try:
    _d, binaries, _h = collect_all("pillow_heif")
    datas += _d
    hiddenimports += _h
except Exception:       # noqa: BLE001
    pass

hiddenimports += collect_submodules("uvicorn.protocols")

# --------------------------------------------------------------------------
# 排除
# --------------------------------------------------------------------------
excludes = [
    # 开发期依赖，运行时完全用不到，能省 20+ MB
    "pytest", "_pytest", "pytest_timeout", "httpx", "httpcore",
    "tkinter.test", "test", "unittest", "pydoc_data",
    # 科学计算栈：Pillow 不需要，但某些 hook 会误拉进来
    "numpy", "scipy", "matplotlib", "pandas",
    # PyQt/PySide：pystray 有多个后端，Windows 上只用 _win32
    "PyQt5", "PyQt6", "PySide2", "PySide6",
]


a = Analysis(       # noqa: F821
    [str(REPO / "packaging" / "launch.py")],
    pathex=[str(REPO)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)       # noqa: F821

exe = EXE(              # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PhotoBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX 压缩过的 exe 被杀毒软件误报的概率明显更高
    console=False,      # 托盘程序，不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,          # 托盘图标是代码画的；exe 图标留待后续用 .ico 替换
)

coll = COLLECT(         # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PhotoBridge",
)

# --------------------------------------------------------------------------
# 产物自带的版本清单（S06：让"仓库钉了 Pillow 13"和"用户手上这个包是不是
# 13"能一条命令对上）
# --------------------------------------------------------------------------
# 为什么必须在**构建时**写：Pillow 的漏洞是"编译进产物里的解码器"的漏洞，
# 改了 requirements.txt 不会让任何已经打好的包变安全。2026-10-18 打的那个
# `dist/PhotoBridge` 就还冻着 12.x，而仓库里 1700+ 条测试全绿也发现不了 ——
# 因为测试跑的是 `.venv`，不是那个包。把版本写进产物，
# `backend/tests/test_packaging_s06_pillow.py` 才能把它和 requirements.lock
# 对齐（在 CI 之外、用户机器上也一样看得见）。
import hashlib as _hashlib
import json as _json
import platform as _platform
from datetime import datetime as _datetime, timezone as _timezone

import PIL as _PIL

_build_info = {
    "app": "PhotoBridge",
    "built_at": _datetime.now(_timezone.utc).isoformat(timespec="seconds"),
    "python": _platform.python_version(),
    "pillow": _PIL.__version__,
    "requirements_lock_sha256": _hashlib.sha256(
        (REPO / "requirements.lock").read_bytes()
    ).hexdigest(),
}
_out = Path(DISTPATH) / "PhotoBridge" / "build_info.json"   # noqa: F821
_out.write_text(_json.dumps(_build_info, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
print(f"📦 已写入 {_out}：Pillow {_build_info['pillow']}")
