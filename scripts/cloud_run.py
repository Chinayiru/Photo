#!/usr/bin/env python
"""云端中继的启动入口：`python scripts/cloud_run.py`。

为什么需要这个文件（而不是 `python -m cloud.relay.main`）：

`-m` 依赖「当前目录 / `PYTHONPATH` 在 `sys.path` 里」。这两条在
**embeddable（embeddable zip 版）Python 上都不成立**：发行包里的
`python312._pth` 一旦存在，CPython 就进入「只按 `_pth` 组装 `sys.path`」
的模式，`PYTHONPATH` 被**整条忽略**（实测：`sys.path[:3]` 只剩
`python312.zip / C:\\Python312 / Lib\\site-packages`，于是
`ModuleNotFoundError: No module named 'cloud'`）。

脚本文件（`python xxx.py`）不受这个限制：`__file__` 一定指向真实路径，
所以这里自己把仓库根塞进 `sys.path`，两条发行方式（MSI / embeddable）
都成立，且不必为了它去改全局的 `python312._pth`。

`main()` 自己负责 `uvicorn.run(...)` 的那几个安全开关
（`proxy_headers=False` / `server_header=False` / `access_log=False`），
见 `docs/11` ISSUE-030 / ISSUE-032。
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


def main() -> int:
    bootstrap()
    from cloud.relay.main import main as run_relay

    run_relay()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
