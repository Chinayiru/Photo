"""打包入口（PyInstaller 用）。

为什么不直接把 `backend/app/main.py` 当入口：那里全是相对导入
（`from .config import ...`），一旦被当作顶层脚本执行就会
`ImportError: attempted relative import with no known parent package`。
这个文件把仓库根塞进 `sys.path`，再以**包**的方式导入，问题就消失了。

另外处理冻结后的两个 Windows 特有陷阱：

1. **`--noconsole` 时 `sys.stdout`/`sys.stderr` 是 `None`**。
   任何一处 `print()` 或 logging 的 StreamHandler 碰到它们都会抛
   `AttributeError: 'NoneType' object has no attribute 'write'`。
   而 uvicorn 默认就往 stderr 装 StreamHandler —— 表现是"双击 exe
   一闪而过，什么都没有"。这里提前换成写往 `os.devnull` 的哑对象。
2. **工作目录不是 exe 所在目录**。用户从开始菜单或双击启动时，
   cwd 可能是 `C:\\Windows\\System32`。所有路径都由 `APP_ROOT`
   （基于 `__file__`）推导，所以不受影响，但这里显式切到 exe
   目录，让"用户自己放了个 config.json 在旁边"这种用法也成立。
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path


def _fix_streams() -> None:
    """`--noconsole` 下把 None 的 stdout/stderr 换成哑流。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, io.TextIOWrapper(
                    open(os.devnull, "wb"), encoding="utf-8", errors="replace"))
            except Exception:       # noqa: BLE001
                setattr(sys, name, io.StringIO())
    if getattr(sys, "stdin", None) is None:
        try:
            sys.stdin = io.TextIOWrapper(open(os.devnull, "rb"))
        except Exception:       # noqa: BLE001
            sys.stdin = io.StringIO()


def _base_dir() -> Path:
    """仓库根（开发时）或解包目录（冻结时）。"""
    if getattr(sys, "frozen", False):
        # PyInstaller onedir：exe 在 dist/PhotoBridge/ 下，
        # 数据文件（frontend/dist）也在同一个目录里。
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def main() -> int:
    _fix_streams()

    base = _base_dir()
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    # 冻结后 cwd 可能是 System32；切到程序目录让相对路径可预期。
    try:
        os.chdir(base)
    except OSError:
        pass

    from backend.app.main import main as app_main

    return app_main()


if __name__ == "__main__":
    sys.exit(main())
