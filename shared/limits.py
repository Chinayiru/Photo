"""两端共用的限额常量。

为什么单独一个文件：上传并发在两侧各有一道闸门，
`backend/app/uploads.py` 数「未完成的会话」，`cloud/relay/limiter.py` 数
「正在转发的上传请求」。两者**语义不同、数字必须一致** —— 否则同一个用户
会看到「怎么都是 429，但删了任务还是 429」这种没法解释的状态。

放在 `shared/` 是因为两端都要 import：设备端与中继端各自演进，常量抄一份
就会漂（审计报告 R2「限流计数」项）。权威口径在设备端；中继那道只是前置
粗筛，减少无谓的隧道往返。
"""

from __future__ import annotations

from typing import Final

#: 设备端：单台电脑同时允许的**未完成上传会话**数。
MAX_UPLOAD_SESSIONS: Final[int] = 3

#: 中继端：单设备同时**在途的上传请求**数（含 init/patch/complete）。
#: 只做粗筛，真正的准入由设备端 `MAX_UPLOAD_SESSIONS` 决定。
MAX_RELAY_UPLOAD_INFLIGHT: Final[int] = 3

__all__ = ["MAX_RELAY_UPLOAD_INFLIGHT", "MAX_UPLOAD_SESSIONS"]
