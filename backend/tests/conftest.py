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

    # 云端登录的两个限流桶是模块级单例（`cloud/relay/auth.py`）。
    # `_login_limiter` 10 次/600 s 按来源 IP，`_email_limiter` 同样 10 次/600 s
    # 按邮箱 —— 整套测试里几十个用例都从 `127.0.0.1` 用同一个邮箱登录，
    # 不清就会在后半段集体 429，表现成"隧道/配对坏了"，且只在跑全量时出现。
    # 生产当然不该清，测试必须清。
    try:
        from cloud.relay import auth as cloud_auth

        cloud_auth._login_limiter.reset()
        cloud_auth._email_limiter.reset()
    except Exception:       # noqa: BLE001
        pass

    # 缩略图缓存统计的 60 秒 memo（ISSUE-034 ①）。
    # 不清的话，前一个用例算出的 (文件数, 总字节) 会漏到后一个用例里 ——
    # 后者往自己的 tmp 缩略图目录里写文件后断言"字节数变了"就会失败，
    # 而且是"有时失败有时不失败"（取决于 60 秒窗口和用例顺序）。
    #
    # ⚠️ 必须清**两种模块名**：测试文件 `sys.path.insert(0, "backend")` 之后
    # import 的是 `app.handlers.system`，而本文件 import 的是
    # `backend.app.handlers.system` —— 在 `sys.modules` 里这是**两个不同的
    # 模块对象**，而 `_thumb_cache_memo` 属于前者。只清后者的效果是"假装清过了"：
    # 本文件看到的 memo 归零，被测代码用的那份原封不动。
    for _mod_name in ("app.handlers.system", "backend.app.handlers.system"):
        _mod = sys.modules.get(_mod_name)
        if _mod is not None:
            _mod._thumb_cache_memo = None


@pytest.fixture(autouse=True)
def _isolate_global_state():
    """每个用例前后都重置模块级单例 —— 防止跨用例串台。"""
    _reset_global_state()
    yield
    _reset_global_state()


# ---------------------------------------------------------------------------
# 真实 data/config.json 守卫
# ---------------------------------------------------------------------------

def _real_config_path() -> Path:
    """生产路径上的 `data/config.json`（不是任何临时库的那份）。"""
    from backend.app.config import CONFIG_PATH

    return Path(CONFIG_PATH)


@pytest.fixture(autouse=True)
def _protect_real_config():
    """★ 禁止任何用例写坏开发机上真实的 `data/config.json`。

    ## 为什么需要（真实事故）

    `Config.__init__` 的 `path` 参数**有默认值** `CONFIG_PATH`，也就是生产
    那份配置。写用例时很容易顺手写 `Config({})` 而忘了传
    `path=tmp_path/...`；此时 `bootstrap_default_root(cfg)` 里的
    `cfg.save()` 会**真的写到开发机的配置文件上**。

    实测后果：跑一轮新写的用例，把开发机上那份 `data/config.json` 的
    `roots` 换成了 pytest 临时目录、`host` 改成了别的值，且**没有任何
    报错** —— 测试全绿，程序下次启动却索引不到照片。这类损坏极难归因：
    配置文件看着是"正常"的 JSON，只是内容被换掉了。

    ## 做法

    用例前后比对真实配置的字节内容；发现被改动就**还原并让该用例失败**，
    把"哪个用例干的"直接摆在面前。还原比只报错重要：否则第一个用例污染
    之后，所有用例都在脏数据上跑，失败会互相掩盖。

    只在文件**原本存在**时守卫。测试环境里若本来没有这份配置，
    `bootstrap_default_root` 写出一份是正当行为，不该拦截。
    """
    real = _real_config_path()
    existed = real.exists()
    before = real.read_bytes() if existed else None

    yield

    if not existed:
        return
    try:
        after = real.read_bytes()
    except FileNotFoundError:
        after = None
    if after != before:
        real.write_bytes(before or b"")
        pytest.fail(
            f"用例把真实的 {real} 改了（已还原）。"
            "多半是构造 Config 时忘了传 path=tmp_path/'config.json'。"
        )
