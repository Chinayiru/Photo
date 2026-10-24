#!/usr/bin/env python
"""云端运维命令行的启动入口：`python scripts/cloud_cli.py <子命令>`。

与 `scripts/cloud_run.py` 同一个理由：embeddable Python 只按
`python312._pth` 组装 `sys.path`，`python -m cloud.relay.cli` 会
`ModuleNotFoundError`；脚本文件不受影响，所以这里自己把包根塞进
`sys.path` 再调 `cloud.relay.cli.main()`。

例：

    python scripts/cloud_cli.py account add --db data/cloud.db \
        --email me@example.com            # 口令从 stdin 读，不进命令行

    python scripts/cloud_cli.py account passwd --db data/cloud.db \
        --email me@example.com

    python scripts/cloud_cli.py account list --db data/cloud.db
"""

from __future__ import annotations

import pathlib
import sys


def bootstrap() -> pathlib.Path:
    """把包根目录放进 `sys.path`，返回它。"""
    root = pathlib.Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def main(argv: list[str] | None = None) -> int:
    bootstrap()
    from cloud.relay.cli import main as run_cli

    return run_cli(argv)


if __name__ == "__main__":
    raise SystemExit(main())
