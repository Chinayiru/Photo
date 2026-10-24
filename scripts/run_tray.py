"""托盘启动入口：让"开机自启"那条命令行不依赖工作目录。

## 为什么需要这个文件

`backend/app/autostart.py` 往注册表 Run 键写一行命令，Windows 在用户登录时
**从 `C:\\Windows\\System32` 拉起它**（Run 键没有"起始位置"这一栏）。
所以写进去的命令必须是：

* **绝对路径**（不能靠 `cd` 到仓库目录），并且
* **不靠 `-m backend.app.main`**（`-m` 会把 `sys.path[0]` 设成当前目录，
  System32 里没有 `backend` 包，结果是启动即退出、用户以为自启坏了）。

把引导逻辑放进这个脚本，注册表里就只是一条"pythonw.exe 绝对路径 + 本文件
绝对路径"，谁在什么目录拉起都一样。`scripts/cloud_run.py` 在云端用的是同一
套办法（那边是 `python -m` 在 embeddable 发行版里找不到包）。

## 为什么默认带 `--tray`

开机自启时用户不会盯着控制台，托盘图标是他唯一能看到"程序在跑"的证据。
想跑前台看日志的人直接敲 `python -m backend.app.main`，不必经过这里。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def ensure_std_streams() -> None:
    """`pythonw.exe` 下 `sys.stdout/stderr` 是 `None` —— 必须补上真的文件对象。

    ## 为什么这是致命的

    uvicorn 在 `Config.__init__` 里会 `logging.config.dictConfig()` 配自己的
    日志格式器，而那个格式器要判断"终端是不是 tty"（决定要不要上色）：

        File ".../uvicorn/logging.py", line 76, in __init__
            self.use_colors = sys.stdout.isatty()
        AttributeError: 'NoneType' object has no attribute 'isatty'

    被 `dictConfig` 包了一层之后，用户看到的是

        ValueError: Unable to configure formatter 'default'

    ——**这个报错里没有一个字提到控制台**，而 `pythonw.exe` 没有控制台也不
    留下任何窗口，于是用户看到的现象是"开机后程序根本没启动"。这就是
    `pythonw` + uvicorn 组合下"自启永不生效"的真正原因（比注册表里那条
    命令写错更隐蔽：命令本身是对的，进程也确实起来了，只是启动即崩）。

    指到 `os.devnull` 而不是内存缓冲：这些输出本来就没人看（日志走文件
    处理器），留着缓冲只会在崩溃时丢掉真正的报错。
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")   # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")   # noqa: SIM115


def bootstrap() -> None:
    """把仓库根塞进 `sys.path` —— 与 CWD 无关。"""
    root = Path(__file__).resolve().parents[1]
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def main(argv: list[str] | None = None) -> int:
    """跑托盘模式；命令行参数原样透传给 `backend.app.main`。"""
    ensure_std_streams()
    bootstrap()
    from backend.app.main import main as app_main       # noqa: PLC0415

    args = ["--tray"]
    if argv:
        args.extend(argv)
    return app_main(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
