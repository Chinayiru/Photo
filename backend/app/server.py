"""组装与启动。

这是**唯一**把各部分接起来的地方：配置 → 数据库 → 缩略图引擎 →
索引器 → 路由表 → 传输层。任何一个模块都不 import 这个文件，
依赖方向始终是单向的（DP-8 单一事实来源）。

启动顺序有讲究：

1. 先 `ensure_dirs()` + `db.init()`（后面所有步骤都依赖库存在）；
2. 再建缩略图引擎并 `start()`（索引器入队时它必须在跑，否则
   首批缩略图会静默丢弃 —— 队列满时我们选择丢弃而不是阻塞）；
3. 再建索引器；
4. 再建路由表并装上中间件（鉴权中间件依赖 AppState）；
5. 最后才 `transport.start()` —— 端口一旦监听，手机就可能发请求，
   此刻所有依赖必须已经就绪。反过来做会有一个几百毫秒的窗口，
   手机上的请求会拿到 500。

启动后的后台任务（扫描、缩略图）都是"尽力而为"：失败只记日志，
绝不让整个服务起不来 —— 用户宁可看到一个没有缩略图的照片库，
也不要看到一个完全打不开的程序。
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

from . import events as events_mod
from .auth import auth_middleware
from .config import Config, check_bind_safety, ensure_dirs, get as get_config, is_first_run
from .db import Database, get_db
from .errors import AppError
from .indexer import Indexer
from .routers import build_router
from .state import AppState, set_state
from .thumbs import ThumbEngine
from .transports.local_http import LocalHttpTransport

log = logging.getLogger(__name__)

__all__ = ["build_state", "build_app", "run"]


def build_state(cfg: Config | None = None, *, db: Database | None = None) -> AppState:
    """构造 AppState 并启动缩略图引擎。

    与 `run()` 分开是为了让测试与脚本能在不起 HTTP 服务的情况下
    拿到一个完全装配好的状态对象。
    """
    cfg = cfg or get_config()
    ensure_dirs(cfg)

    if db is None:
        db = get_db()
    db.init()

    bus = events_mod.get_bus()
    bus.bind_loop(asyncio.get_event_loop())

    workers = int(cfg.thumbs.get("workers", 4) or 4)
    thumbs = ThumbEngine(cfg, db, workers=workers, bus=bus)
    indexer = Indexer(cfg, db, thumbs=thumbs, bus=bus)

    st = AppState(cfg, db, thumbs=thumbs, indexer=indexer, bus=bus)
    set_state(st)
    return st


def build_app(cfg: Config | None = None, *, state: AppState | None = None) -> Any:
    """构造路由表 + 中间件 + 传输层。返回 (router, transport, state)。"""
    cfg = cfg or get_config()
    st = state or build_state(cfg)

    router = build_router()
    # 把共享依赖容器交给 Router：处理函数从 ctx.app 取 AppState，
    # 而不是 import 模块级单例（那样测试无法注入临时库）。
    router.bind_app(st)

    # 中间件注册顺序 = 洋葱从外到内。鉴权必须在**内层**，
    # 这样请求日志能包住 401（否则鉴权失败不进访问日志，排障看不到）。
    router.use(_request_log_middleware)
    router.use(auth_middleware)
    router.enable_cors()

    transport = LocalHttpTransport(cfg, router)
    return router, transport, st


async def _request_log_middleware(ctx, nxt):
    """请求日志：只记慢请求与错误，不记每个缩略图请求。

    4915 张缩略图会在日志里刷出 4915 行 "200 3ms"，把真正的问题
    埋掉；而慢请求与错误才是需要看的。阈值 300ms 是"用户在手机上
    能感觉到的卡顿"的下限。
    """
    t0 = time.perf_counter()
    try:
        reply = await nxt(ctx)
    except Exception:
        log.warning("%s %s 异常 %.0fms", ctx.method, ctx.path,
                    (time.perf_counter() - t0) * 1000)
        raise
    ms = (time.perf_counter() - t0) * 1000
    if ms >= 300 or reply.status >= 400:
        log.info("%s %s -> %s %.0fms", ctx.method, ctx.path, reply.status, ms)
    return reply


async def _startup_scan(st: AppState) -> None:
    """启动时的后台全量扫描。"""
    try:
        await st.indexer.scan_all()
    except Exception:       # noqa: BLE001
        log.exception("启动扫描失败（不影响服务）")


async def serve(cfg: Config | None = None) -> None:
    """启动服务并阻塞，直到收到中断信号。"""
    cfg = cfg or get_config()

    if is_first_run():
        from .config import bootstrap_default_root

        bootstrap_default_root()
        cfg = get_config()
        log.info("首次运行：已写入默认媒体根，请到设置里确认路径")

    st = build_state(cfg)
    _, transport, st = build_app(cfg, state=st)

    st.thumbs.start()

    if cfg.index.get("scan_on_start", True):
        # 不 await：4915 张要 20 秒，先让手机能连上来。
        asyncio.create_task(_startup_scan(st), name="startup-scan")

    stop = asyncio.Event()

    def _on_signal(*_: Any) -> None:
        log.info("收到退出信号，正在停止…")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except (NotImplementedError, ValueError):
            # Windows 的事件循环不支持 add_signal_handler。
            # 退到 signal.signal（在非主线程会抛 ValueError，同样忽略）。
            try:
                signal.signal(sig, _on_signal)
            except ValueError:
                pass

    await transport.start()
    log.info("PhotoBridge 已启动：%s", ", ".join(transport.urls))

    try:
        await stop.wait()
    finally:
        await _shutdown(st, transport)


async def _shutdown(st: AppState, transport: Any) -> None:
    """按依赖的反序关闭，每一步都不能让后面的步骤被跳过。"""
    try:
        await transport.stop()
    except Exception:       # noqa: BLE001
        log.warning("关闭传输层失败", exc_info=True)

    try:
        # drain=True：把队列里剩下的缩略图跑完，否则用户下次打开
        # 会看到一片灰图。但要有上限 —— 关机不该等几分钟。
        await asyncio.wait_for(st.thumbs.stop(drain=True), timeout=20.0)
    except (asyncio.TimeoutError, Exception):   # noqa: BLE001
        log.warning("关闭缩略图引擎超时", exc_info=True)

    try:
        events_mod.get_bus().close()
    except Exception:       # noqa: BLE001
        pass

    try:
        st.db.close()
    except Exception:       # noqa: BLE001
        log.warning("关闭数据库失败", exc_info=True)

    log.info("PhotoBridge 已停止")


def run(cfg: Config | None = None) -> int:
    """命令行入口：配置日志、跑事件循环、返回退出码。"""
    from .logging_setup import setup_logging

    cfg = cfg or get_config()
    setup_logging(cfg)

    try:
        asyncio.run(serve(cfg))
    except KeyboardInterrupt:
        return 0
    except AppError as exc:
        # 配置类错误（端口被占、绑定地址不安全）必须让用户看到原文，
        # 否则他只会看到"启动失败"而不知道是端口冲突。
        log.error("启动失败：%s", exc.detail or exc.code)
        print(f"启动失败：{exc.detail or exc.code}", file=sys.stderr)
        return 2
    except Exception:       # noqa: BLE001
        log.exception("未预期错误，程序退出")
        return 1
    return 0
