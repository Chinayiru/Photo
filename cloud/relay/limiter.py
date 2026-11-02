"""限流：令牌桶 + 计数信号量。

## 为什么自己写

`slowapi`/`limits` 会把"限流"变成一个装饰器，而这里需要的是**三件不同
的事**（`docs/04` §3.3）：

| 目标 | 额度 | 超限响应 |
|---|---|---|
| 单客户端请求频率 | 120 req/s | 429 `E4001` + `Retry-After: 1` |
| 单设备并发隧道请求 | 64 | 429 `E4003` + `Retry-After: 1` |
| 单设备并发上传 | 3 | 429 `E4002` + `Retry-After: 3` |

三者作用对象不同（客户端 / 设备 / 设备内的上传），用装饰器表达只会
让"到底限的是谁"变模糊。这里三个小类各司其职。

## 为什么是**固定窗口**而不是滑动日志

滑动日志要存每次请求的时间戳，120 req/s × 几千个客户端 = 每秒几十万条
记录，而它换来的只是"更平滑"。本产品的限流目的是**防误用与防跑飞**
（前端死循环重试、某台设备被爬），不是计费级的精确配额。固定窗口在
窗口边界允许瞬时 2 倍流量，但这在这里无害。

## 时钟

一律用 `time.monotonic()`，不用 `time.time()`：后者会被 NTP 校正，
一次向前跳 1 秒会让所有窗口瞬间清空（限流失效），向后跳会让客户端
被多锁几秒。单进程内 `monotonic` 是唯一正确的选择。
"""
from __future__ import annotations

import time
from typing import Any, Hashable

from shared.limits import MAX_RELAY_UPLOAD_INFLIGHT

__all__ = [
    "RATE_LIMIT_PER_SEC",
    "MAX_INFLIGHT_PER_DEVICE",
    "MAX_INFLIGHT_TOTAL",
    "MAX_UPLOAD_CONCURRENCY",
    "AUTH_RATE_PER_MIN",
    "AUTH_FAIL_MAX",
    "AUTH_FAIL_WINDOW",
    "RateLimiter",
    "ConcurrencyLimiter",
    "UploadLimiter",
]

#: 单客户端令牌每秒请求数（`docs/04` §3.3）。
RATE_LIMIT_PER_SEC: int = 120
#: 单设备并发隧道请求上限。
MAX_INFLIGHT_PER_DEVICE: int = 64
#: 单设备并发上传上限（`docs/04`：429 E4002 + Retry-After: 3）。
#: 与设备端 `MAX_OPEN_SESSIONS` 共用 `shared/limits.py` 里的同一个数字：
#: 这里只数在途请求（前置粗筛），真正的准入由设备端的未完成会话数决定。
MAX_UPLOAD_CONCURRENCY: int = MAX_RELAY_UPLOAD_INFLIGHT

#: 全云端**所有设备合计**的并发回源上限（审计 F03）。
#:
#: 没有它时只有单设备 64：三台设备各挂 60 个慢速 `/raw` 就是 180 条并发的
#: 隧道往返 + 同样多的文件句柄，而云主机只有 2 vCPU / 4 GB —— 一台机器被
#: 刷满就轮到整台服务器，别的用户的隧道一起掉线。
MAX_INFLIGHT_TOTAL: int = 512

#: 按来源 IP 的**鉴权尝试**上限（每分钟）。`/d/**` 上每一个要验令牌的请求
#: 都算一次。放得很宽：正常手机一次缩略图潮就有 20–60 个请求，快速滑动能到
#: 100+/分钟。这个桶挡的是"拿一个来源 IP 扫令牌"，不是正常浏览。
AUTH_RATE_PER_MIN: int = 600

#: 按来源 IP 的**认证失败**上限（窗口 `AUTH_FAIL_WINDOW` 秒）。
#:
#: 正常的失败只有"手机重新配对前的那一刻"（本地还留着旧令牌），一两次就到头；
#: 10 分钟 20 次足够容错，而一枚 32 字节随机令牌的爆破进度远达不到这个量级。
AUTH_FAIL_MAX: int = 20
#: 认证失败计数窗口（秒）。
AUTH_FAIL_WINDOW: float = 600.0


class RateLimiter:
    """按 key（客户端令牌哈希）的固定窗口计数器。"""

    __slots__ = ("_limit", "_window", "_buckets", "_last_gc")

    def __init__(self, limit: int = RATE_LIMIT_PER_SEC,
                 window: float = 1.0) -> None:
        self._limit = max(1, int(limit))
        self._window = max(0.1, float(window))
        #: key → (窗口起点, 已用次数)
        self._buckets: dict[Hashable, tuple[float, int]] = {}
        self._last_gc = time.monotonic()

    def hit(self, key: Hashable, cost: int = 1) -> tuple[bool, float]:
        """记一次请求。返回 `(允许?, 建议的 Retry-After 秒)`。"""
        now = time.monotonic()
        self._gc(now)
        start, used = self._buckets.get(key, (now, 0))
        if now - start >= self._window:
            start, used = now, 0
        used += max(1, int(cost))
        self._buckets[key] = (start, used)
        if used <= self._limit:
            return True, 0.0
        return False, max(0.0, self._window - (now - start))

    def _gc(self, now: float) -> None:
        """每 60 秒清一次过期桶。

        不清的话，一个被爬过的域名会在内存里留下几十万个 key —— 而每个
        key 只是一对浮点/整数，泄漏速度慢到不会有人发现，直到 OOM。
        """
        if now - self._last_gc < 60.0:
            return
        self._last_gc = now
        stale = [k for k, (start, _) in self._buckets.items()
                 if now - start >= self._window * 2]
        for k in stale:
            self._buckets.pop(k, None)

    def count(self) -> int:
        return len(self._buckets)

    def reset(self) -> None:
        """清空所有窗口。

        只给测试与运维用：`cloud/relay/auth.py` 的登录限流器是**模块级
        单例**（跨请求、跨连接共享），测试进程里几十个用例共用同一个
        `127.0.0.1` 桶，很快就把 10 次/600s 打满 —— 症状是后面的用例
        全在 `alogin()` 上拿 429 `E4001`，看起来像"配对坏了"。生产环境
        不该调它（清空等于把暴力破解的计数清零）。
        """
        self._buckets.clear()
        self._last_gc = time.monotonic()

    def stats(self) -> dict[str, Any]:
        return {"keys": len(self._buckets), "limit": self._limit,
                "window_sec": self._window}


class ConcurrencyLimiter:
    """按 key 的并发计数（不是频率）。

    用法：`if not lim.take(k): 429` … `lim.release(k)`。**必须**在
    `finally` 里 release：漏一次就永久少一个额度，64 次之后这台设备
    再也开不了新请求（症状是"用一阵子之后所有请求都 429"）。
    """

    __slots__ = ("_limit", "_counts")

    def __init__(self, limit: int) -> None:
        self._limit = max(1, int(limit))
        self._counts: dict[Hashable, int] = {}

    def take(self, key: Hashable) -> bool:
        n = self._counts.get(key, 0)
        if n >= self._limit:
            return False
        self._counts[key] = n + 1
        return True

    def release(self, key: Hashable) -> None:
        n = self._counts.get(key, 0)
        if n <= 1:
            self._counts.pop(key, None)
        else:
            self._counts[key] = n - 1

    def count(self, key: Hashable) -> int:
        return self._counts.get(key, 0)

    def stats(self) -> dict[str, Any]:
        return {"keys": len(self._counts), "limit": self._limit}


class UploadLimiter(ConcurrencyLimiter):
    """并发上传计数，上限 3。

    单独一个类只是为了让"为什么上传是 3 而不是 64"有一个能写注释的地方：
    上传占用的是**磁盘写入 + 校验**而不是带宽，3 个并发已经能把家庭宽带
    的上行打满，更多只会让每个都变慢，且失败时的重试风暴更难收敛。
    """

    def __init__(self, limit: int = MAX_UPLOAD_CONCURRENCY) -> None:
        super().__init__(limit)
