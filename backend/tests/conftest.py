"""pytest 全局配置：隔离测试之间的模块级状态。

## 为什么需要

生产代码里有几处**模块级单例/缓存**，它们在长驻进程里是正确设计
（省重复构造），但测试在同一进程里跑很多个用例时会互相污染：

* `backend/app/handlers/auth.py` 的 `_pair_attempts` —— 按 IP 的配对限速
  （10 分钟 20 次）。测试全从 `127.0.0.1` 发请求，跑十几个用例就把窗口
  填满，后面的用例拿到 `429 E4001`。**症状极具误导性**：
  报错说"尝试太频繁"，而实际是上一个测试用掉的额度。
* `backend/app/state.py` 的 `_state` —— 进程级 AppState 单例。
  上一个用例的临时库会被下一个用例读到。
* `backend/app/events.py` 的 `_bus` —— 同上，事件会串台。
* `backend/app/db.py` 的 `_db` —— 全局数据库单例，**最危险的一个**：
  它按磁盘上的 `data/config.json` 解析路径，在开发机上就是用户真实的
  照片库索引（见 `backend/tests/test_isolation.py` 记录的事故）。
* `backend/app/uploads.py` 的 `_locks` —— 按 upload_id 的会话锁。

## 做法

一个 `autouse=True` 的夹具，每个用例前后把上面这些**显式重置**。
用"显式重置"而不是"清理所有全局变量"，是因为后者会悄悄掩盖
真正该修的耦合。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _reset_global_state() -> None:
    """把模块级单例/缓存恢复到"刚启动"的状态。

    每处都用 try/except 包住：某个模块被删掉或改名时，
    这个夹具不该让整套测试崩掉 —— 它只是隔离手段，不是被测对象。
    """
    # 配对限速窗口
    try:
        from backend.app.handlers import auth as auth_handlers

        auth_handlers._pair_attempts.clear()
    except Exception:       # noqa: BLE001
        pass

    # 进程级 AppState / EventBus
    try:
        from backend.app import state as state_mod

        state_mod.set_state(None)
    except Exception:       # noqa: BLE001
        pass

    try:
        from backend.app import events as events_mod

        events_mod.set_bus(None)
    except Exception:       # noqa: BLE001
        pass

    # 全局数据库单例
    try:
        from backend.app import db as db_mod

        db_mod.close_db()
    except Exception:       # noqa: BLE001
        pass

    # 上传会话锁
    try:
        from backend.app import uploads as uploads_mod

        with uploads_mod._locks_guard:
            uploads_mod._locks.clear()
    except Exception:       # noqa: BLE001
        pass


@pytest.fixture(autouse=True)
def _isolate_global_state():
    """每个用例前后都重置模块级单例 —— 防止跨用例串台。"""
    _reset_global_state()
    yield
    _reset_global_state()
