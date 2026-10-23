# -*- coding: utf-8 -*-
"""前端加固的**源码判据**（`docs/11` SEC-23）。

## 为什么用源码判据，而不是浏览器测试

这一层是"基址什么时候落盘"的时序问题：错的表现是"用户填错一次地址后，
下次冷启动全部请求打到旧地址"，本仓库没有 JS 测试运行器（`frontend/
package.json` 里只有 dev/build/typecheck，没有 vitest），起真浏览器跑这段
又要造一套 localStorage 夹具 —— 成本远高于它挡的风险。而这条 bug 的全部
内容是"落盘发生在请求成功之前"，用顺序断言钉住它是精确的。

（真正需要行为验证的部分在 `backend/tests/test_p1_pages.py`：那边起真
uvicorn，验证页能拿到、能配对、能拉时间线。）
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_pair_only_persists_the_base_after_the_server_accepts() -> None:
    """配对基址必须随 `savePairing()` 一起落盘，不能先写再试。

    SEC-23：原实现第一行就 `saveBase(cleanBase)`。后果有两个——
    ① 填错地址/电脑没开时，那个连不上的地址长期留在 localStorage 里，
       下次冷启动所有请求都打到那儿；
    ② `PairView` 每次挂载都会自动 `submit()`，失败一次攒一次限速额度，
       用户自己把自己锁在门外（配对码 5 次就锁 10 分钟）。
    """
    src = _read("frontend/src/stores/auth.ts")

    assert "saveBase(" not in src, (
        "auth.ts 里又出现了 saveBase()：基址必须在配对成功后才落盘，"
        "否则失败的候选地址会覆盖掉能用的地址（SEC-23）"
    )

    ping = src.index('api.get<PingResponse>("/api/system/ping"')
    post = src.index('api.post<PairResponse>("/api/pair"')
    saved = src.index("savePairing({")
    assert ping < post < saved, "落盘必须先于请求之外的顺序被打乱了"
    # 两个请求都得带上候选基址，否则它们会打到 localStorage 里的旧地址
    assert src.count("base: cleanBase") >= 2


def test_request_options_can_carry_a_one_shot_base() -> None:
    """`RequestOptions.base` 存在且真的覆盖 `currentBase()`，但不落盘。"""
    src = _read("frontend/src/api/client.ts")
    assert "base?: string;" in src
    assert "opt.base || currentBase()" in src, (
        "once() 没读 opt.base —— 配对时的候选基址会失效"
    )