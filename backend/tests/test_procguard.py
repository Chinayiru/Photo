"""媒体子进程护栏（`backend/app/procguard.py`）的测试。

## 为什么这些用例值得存在

护栏本身不产出任何用户可见的东西，所以它坏掉时**没有任何症状**：
超时没生效 = 某天一张坏视频把工作线程占住一小时；内存上限没生效 =
某天一台 8 GB 的笔记本开始交换，用户以为电脑坏了。这两种"沉默的退化"
只能靠测试钉住 —— 每条用例都真的起一个子进程，真的把它逼到越界。

用 `sys.executable` 当被测程序，而不是 `ffmpeg`：测试环境不保证装了
ffmpeg，而护栏要是只能在装了 ffmpeg 的机器上验证，等于没验证。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from backend.app import procguard, video

PY = sys.executable


def test_run_returns_stdout_and_zero_code() -> None:
    result = procguard.run([PY, "-c", "print(42)"], timeout=30)

    assert result.returncode == 0
    assert result.stdout.strip() == b"42"
    assert result.elapsed >= 0


def test_run_raises_and_kills_on_timeout() -> None:
    """超时必须真的掐死，而不是"抛了异常、进程还在后台跑"。"""
    started = time.monotonic()
    with pytest.raises(procguard.ProcTimeout):
        procguard.run([PY, "-c", "import time; time.sleep(30)"], timeout=1.0)
    elapsed = time.monotonic() - started

    # 1 秒超时 + kill 的收尾：给到 15 秒已经非常宽松
    assert elapsed < 15, f"超时后收尾太慢：{elapsed:.1f}s"


def test_memory_limit_stops_a_runaway_allocation() -> None:
    """上限 256 MB 时，申请 2 GB 必须失败 —— 死的是子进程，不是测试进程。"""
    if procguard.memory_mb() < 256:      # 环境变量把它调得极小时跳过
        pytest.skip("PB_MEDIA_MEMORY_MB 太小，这条用例没有意义")

    result = procguard.run(
        [PY, "-c", "b = bytearray(2_000_000_000); print(len(b))"],
        timeout=120, limit_mb=256)

    assert result.returncode != 0, "内存上限没生效：2 GB 的分配居然成功了"


def test_child_runs_in_the_media_scratch_dir() -> None:
    """默认落点是临时目录下的专用目录，不是程序目录。"""
    result = procguard.run([PY, "-c", "import os; print(os.getcwd())"], timeout=30)
    assert result.returncode == 0

    cwd = Path(result.stdout.decode("utf-8", "replace").strip())
    scratch = procguard.scratch_dir()
    assert cwd.resolve() == scratch.resolve()
    # 关键：不在仓库里。否则 ffmpeg 按"当前目录"写中间文件就会写到代码旁边
    assert "backend" not in cwd.parts
    assert cwd.resolve() != Path.cwd().resolve()


def test_video_run_still_raises_subprocess_timeout() -> None:
    """`video._run()` 要把 `ProcTimeout` 翻译回 `TimeoutExpired`。

    为什么在意：`probe()` / `extract_frame()` 的 `except` 分支写的是
    `subprocess.TimeoutExpired`，那里决定日志措辞与"这张封面算失败还是
    算没装 ffmpeg"。翻译错一次，坏视频就会被报成"没装 ffmpeg"。
    """
    with pytest.raises(subprocess.TimeoutExpired):
        video._run([PY, "-c", "import time; time.sleep(30)"], timeout=1.0)
