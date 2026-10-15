"""日志初始化。

三条硬约束（docs/03 §6）：

1. **落盘用 UTF-8**。Windows 的 `RotatingFileHandler` 默认用系统
   编码（GBK），日志里一旦出现"敦煌"或"青甘环线"就会抛
   `UnicodeEncodeError`，而异常发生在日志调用里 —— 结果是
   一个本该记录错误的函数自己崩掉，掩盖了原始问题。
2. **只在交互式终端里加 StreamHandler**。打包成 exe 后 stderr
   可能指向一个不存在的控制台，写它会抛异常；而用户双击 exe
   时看到的黑框一闪而过，日志也留不下来。
3. **降噪**。uvicorn/watchdog/PIL 在 DEBUG 级别会输出海量行，
   把真正的问题埋掉。它们的 INFO 对我们毫无价值。
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from .config import Config

__all__ = ["LOG_FORMAT", "setup_logging"]

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 每个日志文件 10 MB，保留 5 个（共约 60 MB 上限）。
#: 不设上限的话，一个卡在重试循环里的手机端能在一天内写出几个 GB。
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

_NOISY = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "watchdog",
    "watchdog.observers",
    "asyncio",
    "PIL",
    "PIL.Image",
    "multipart",
    "httpx",
    "httpcore",
)


def setup_logging(cfg: Config, *, level: int | None = None, force: bool = False) -> Path:
    """配置根 logger，返回日志文件路径。

    `force=True` 会先清掉已有 handler —— 测试里反复调用 `setup_logging`
    而不清理，会让 handler 越积越多，同一条日志被写 N 遍。
    """
    logs_dir = cfg.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "agent.log"

    root = logging.getLogger()
    if force:
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:       # noqa: BLE001
                pass

    if level is None:
        level = logging.DEBUG if cfg.app.get("debug") else logging.INFO
    root.setLevel(level)

    fmt = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    already = any(
        isinstance(h, logging.handlers.RotatingFileHandler)
        and Path(getattr(h, "baseFilename", "")) == log_path
        for h in root.handlers
    )
    if not already:
        fh = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",       # ← 不能省，见模块文档
            delay=True,
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # 只在真正的交互式终端里加屏幕输出
    if sys.stderr is not None and hasattr(sys.stderr, "isatty") and sys.stderr.isatty():
        if not any(isinstance(h, logging.StreamHandler)
                   and not isinstance(h, logging.FileHandler) for h in root.handlers):
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(fmt)
            root.addHandler(sh)

    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)

    return log_path
