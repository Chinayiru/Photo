"""PhotoBridge 电脑端入口。

用法：

    python -m backend.app.main              # 用 data/config.json
    python -m backend.app.main --port 8787  # 覆盖端口
    python -m backend.app.main --host 127.0.0.1

`--host` 默认取配置值（出厂是 127.0.0.1，只监听本机）。
要让手机连上必须显式传 `--host 0.0.0.0` 或改配置 —— 这是刻意的：
把照片库开放到局域网应当是一个明确的动作，而不是默认后果。
"""
from __future__ import annotations

import argparse
import sys

from .config import get as get_config
from .config import load as load_config
from .server import run

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="photobridge",
        description="PhotoBridge —— 私有照片库服务（局域网优先）",
    )
    parser.add_argument("--host", default=None,
                        help="监听地址（默认取配置；0.0.0.0 表示允许手机访问）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 8787）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--rescan", action="store_true",
                        help="启动前先做一次全量扫描（默认异步在后台扫）")
    parser.add_argument("--tray", action="store_true",
                        help="启动后驻留系统托盘（显示配对二维码、退出入口）")
    parser.add_argument("--no-tray", action="store_true",
                        help="即使配置里开了托盘也不显示（排障用）")
    parser.add_argument("--version", action="store_true", help="打印版本后退出")
    args = parser.parse_args(argv)

    if args.version:
        from . import __version__

        print(f"PhotoBridge {__version__}")
        return 0

    if args.config:
        from pathlib import Path

        from .config import Config, _read_json      # noqa: PLC0415

        cfg = Config(_read_json(Path(args.config)), path=Path(args.config))
    else:
        # load_config() 会读磁盘并缓存，get_config() 拿缓存。
        # 必须用 get_config() 而不是 load_config()：后者返回的是
        # 一个**新对象**，下面改 host/port 就只改到临时副本上。
        load_config()
        cfg = get_config()

    if args.host:
        cfg._raw.setdefault("server", {})["host"] = args.host
    if args.port:
        cfg._raw.setdefault("server", {})["port"] = int(args.port)
    if args.rescan:
        cfg._raw.setdefault("index", {})["scan_on_start"] = True

    # 托盘默认开：双击 exe 的用户需要一个看得见的图标与退出入口，
    # 否则他只能去任务管理器杀进程（而强杀会留下 WAL）。
    # 但命令行前台运行时不该弹托盘 —— 那种场景用户要的是日志。
    tray = bool(cfg.app.get("tray", True)) and not args.no_tray
    if args.tray:
        tray = True

    return run(cfg, tray=tray)


if __name__ == "__main__":
    sys.exit(main())
