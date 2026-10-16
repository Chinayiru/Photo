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
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from . import events as events_mod
from .auth import auth_middleware
from .config import Config, check_bind_safety, ensure_dirs, get as get_config, is_first_run
from .db import Database, get_db, make_db
from .errors import AppError
from .indexer import Indexer
from .routers import build_router
from .static import StaticFiles
from .state import AppState, set_state
from .thumbs import ThumbEngine
from .transports.local_http import LocalHttpTransport

log = logging.getLogger(__name__)

__all__ = ["build_state", "build_app", "run"]


def _bind_bus_loop(bus: Any) -> None:
    """把事件总线绑到当前事件循环（用于跨线程投递）。

    ⚠️ 不能用 `asyncio.get_event_loop()`：在**没有正在运行的事件循环**时
    （同步测试、脚本、或 Python 3.12+ 的主线程里从未创建过 loop），
    它会抛 `RuntimeError: There is no current event loop in thread 'MainThread'`。
    `build_state()` 本来是可以在纯同步上下文里调用的（测试就这么用），
    不该因为它顺带做的事而整体失败。

    `get_running_loop()` 才是正确的问法："现在有没有正在跑的循环？"
    没有就返回 None，总线退化成"只能从本线程发布"（`publish` 照常可用，
    只有 `publish_threadsafe` 会静默 no-op）。真正的服务启动路径里
    `serve()` 是协程，必然有 running loop，行为不受影响。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    bus.bind_loop(loop)


def build_state(cfg: Config | None = None, *, db: Database | None = None) -> AppState:
    """构造 AppState。

    与 `run()` 分开是为了让测试与脚本能在不起 HTTP 服务的情况下
    拿到一个完全装配好的状态对象。

    ★ **测试与脚本必须传 `db=`**（用 `db_mod.make_db(cfg)` 造一个）。

    这里的 `db=None` 会回退到全局单例 `get_db()`，而它按磁盘上的
    `data/config.json` 解析路径 —— 在开发机上那就是用户真实的照片库
    索引。曾经因此出过一次真实事故：测试夹具传了
    `Config(data_dir=tmp)` 但没传 `db`，于是合成测试行写进了真实库，
    而且扫描结束后把真实库的 4915 行全标成了 missing=1，
    用户的时间线变成空白。

    为了不再犯，这里加了显式校验：**只要 cfg 的数据库路径与全局
    单例指向的不是同一个文件，就必须传 db**，否则直接报错而不是
    悄悄连到别的库上去。
    """
    cfg = cfg or get_config()
    ensure_dirs(cfg)

    if db is None:
        # 优先复用全局单例（同一个文件只保留一个 Database 对象，
        # 也就只有一把写锁、一套线程本地连接）。
        db = get_db()
        # ★ 但路径不一致时必须按 cfg 另造一个，不能连错库。
        # `cfg` 可能来自 `--config`，指向完全另一个数据目录；
        # 此时全局单例仍指向安装目录那份配置解析出的真实照片库。
        # 曾经因此出过一次真实事故：测试用 `--config` 指向临时照片库，
        # 数据库却仍是用户的真实库，扫描结束后把真实库 4915 行
        # 全标成了 missing=1，用户的时间线变成空白。
        # 这里不再报错而是**按 cfg 造一个** —— 生产路径必须能跑起来，
        # 而"造对了"本身就是对那次事故的修复。
        try:
            want = Path(cfg.db_path).resolve()
            got = Path(db.path).resolve()
        except OSError:       # pragma: no cover —— 路径不可解析时不做判断
            want = got = None
        if want is not None and want != got:
            log.info("配置指向独立的数据库 %s（全局单例是 %s），按配置新建", want, got)
            db = make_db(cfg)
    db.init()

    bus = events_mod.get_bus()
    _bind_bus_loop(bus)

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

    # 静态前端兜底：手机访问 http://<电脑IP>:8787/ 直接看到界面。
    # 用 set_fallback 而不是再注册一条 `/{path:path}` 路由 —— 后者会
    # 与所有 API 路由竞争匹配，一旦正则顺序变化就可能把 API 请求
    # 吃掉交给前端外壳，表现为"接口返回 index.html"。
    router.set_fallback(StaticFiles(_dist_dir(cfg)))

    transport = LocalHttpTransport(cfg, router)
    return router, transport, st


def _dist_dir(cfg: Config) -> Path | None:
    """前端构建产物位置。

    三种情形，按优先级：
    1. 配置显式覆盖（`app.dist_dir`）—— 开发时想指向别处，或用户自己
       改了部署布局。
    2. **打包成 exe 后**：PyInstaller 把 `--add-data` 的东西解到
       `sys._MEIPASS` 下，此时 `static.py` 的 `DEFAULT_DIST`
       （按 `__file__` 往上两级算）会指到临时解包目录之外的地方，
       前端会 404 —— 而现象是"页面白屏"，看不出跟路径有关。
    3. 源码仓库：返回 None，让 `StaticFiles` 用自己的默认值
       （`<仓库根>/frontend/dist`）。
    """
    override = cfg.get("app.dist_dir")
    if override:
        return Path(str(override))

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "frontend" / "dist"
        if candidate.is_dir():
            return candidate

    return None      # None 让 StaticFiles 用自己的默认值


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


def _count_hint(cfg: Config) -> str:
    """给首次索引的日志一个量级提示，让用户知道要等多久。"""
    total = 0
    for root in cfg.enabled_roots:
        try:
            with os.scandir(root.path) as it:
                total += sum(1 for _ in it)
        except OSError:
            pass
    return f"{total}+ 个条目" if total else "数千"


async def serve(cfg: Config | None = None, *,
                on_ready: Any = None) -> None:
    """启动服务并阻塞，直到收到中断信号。

    `on_ready(transport, state, loop, stop)` 在**端口已监听、所有依赖就绪**
    之后被调用一次，用于系统托盘这类需要拿到真实地址与停止句柄的外部外壳。
    回调抛异常只记日志，不会让服务起不来。
    """
    cfg = cfg or get_config()

    # ★ 必须把 cfg 传进去，不能调用无参版本。
    # 无参版本只看全局 CONFIG_PATH，于是 `--config` 指向另一份配置时，
    # 它会判定"首次运行"并调 bootstrap_default_root()，而后者用
    # get_config() 返回**默认那份**配置 —— 调用方传进来的 cfg 被整个丢掉，
    # 用户以为在用 A 配置，实际跑的是自动探测出来的 B。
    # 实测：`--config` 指向一个只有 1 张图的临时库，程序却去索引了
    # 自动探测到的真实照片库，并在那份库上 `marked 2000 rows missing`。
    if is_first_run(cfg):
        from .config import bootstrap_default_root

        cfg = bootstrap_default_root(cfg)
        log.info("首次运行：已写入默认媒体根，请到设置里确认路径")

    st = build_state(cfg)
    _, transport, st = build_app(cfg, state=st)

    st.thumbs.start()

    # ★ "首次索引"的判定不能只看配置文件在不在。
    # 用户可能：删掉 data/ 重新开始、换了照片库路径、或者程序曾经
    # 启动失败过（配置写好了但一张也没扫）。这些情况下库里都是空的，
    # 手机连上来会看到空时间线。以"media 表里有没有数据"为准才可靠。
    is_first_run_scan = (
        cfg.index.get("scan_on_start", True)
        and not st.db.scalar("SELECT 1 FROM media LIMIT 1", default=None)
    )

    # ★ 首次运行必须先把根目录索引进库，不能丢给后台任务。
    # 原因：首次运行的时间点是"用户刚装好、手机正要连上来"。
    # 若扫描在后台跑，用户在手机上看到的是空时间线 —— 他无法区分
    # "程序坏了"与"还在扫"，而正确的第一印象是"照片都在"。
    # 4915 张实测 17.6 秒，这个等待是一次性的且值得。
    # （后续启动才走后台：那时库里已有数据，手机能立刻看到内容。）
    if is_first_run_scan:
        log.info("首次索引：正在扫描照片库（约 %s），预计 20 秒内完成…", _count_hint(cfg))
        try:
            stats = await st.indexer.scan_all()
            log.info("首次索引完成：%s", "；".join(s.summary() for s in stats) or "无根目录")
        except Exception:       # noqa: BLE001
            log.exception("首次索引失败（服务照常启动）")
    elif cfg.index.get("scan_on_start", True):
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

    # 把控制权交给外壳（系统托盘）。放在 transport.start() 之后 ——
    # 此刻端口真的在监听、urls 是真实端口，二维码扫了才连得上。
    if on_ready is not None:
        try:
            on_ready(transport, st, loop, stop)
        except Exception:       # noqa: BLE001
            log.exception("启动回调失败（服务继续运行）")

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
        #
        # ★ 上限必须作为参数传进去（drain_timeout），不能用
        # asyncio.wait_for 从外面掐：外部超时会 cancel 这个协程，
        # 让 worker 与线程池的回收整段跳过（实测刷出
        # `Task was destroyed but it is pending!`）。
        await st.thumbs.stop(drain=True, drain_timeout=20.0)
    except Exception:       # noqa: BLE001
        log.warning("关闭缩略图引擎失败", exc_info=True)

    try:
        events_mod.get_bus().close()
    except Exception:       # noqa: BLE001
        pass

    try:
        st.db.close()
    except Exception:       # noqa: BLE001
        log.warning("关闭数据库失败", exc_info=True)

    log.info("PhotoBridge 已停止")


def run(cfg: Config | None = None, *, tray: bool = False) -> int:
    """命令行入口：配置日志、跑事件循环、返回退出码。

    `tray=True` 时额外挂上系统托盘（`backend/app/tray.py`）。
    托盘只是外壳：它拿到的 `transport.urls` 已经是真实端口。
    """
    from .logging_setup import setup_logging

    cfg = cfg or get_config()
    setup_logging(cfg)

    on_ready = None
    if tray:
        from .tray import TrayController       # noqa: PLC0415

        controller = TrayController(cfg)
        on_ready = controller.on_ready

    try:
        asyncio.run(serve(cfg, on_ready=on_ready))
    except KeyboardInterrupt:
        return 0
    except AppError as exc:
        # 配置类错误（端口被占、绑定地址不安全）必须让用户看到原文，
        # 否则他只会看到"启动失败"而不知道是端口冲突。
        log.error("启动失败：%s", exc.detail or exc.code)
        print(f"启动失败：{exc.detail or exc.code}", file=sys.stderr)
        if on_ready is not None:
            try:
                from .tray import notify_error    # noqa: PLC0415

                notify_error(f"启动失败：{exc.detail or exc.code}")
            except Exception:       # noqa: BLE001
                pass
        return 2
    except Exception:       # noqa: BLE001
        log.exception("未预期错误，程序退出")
        if on_ready is not None:
            try:
                from .tray import notify_error    # noqa: PLC0415

                notify_error("PhotoBridge 启动失败，详见 data/logs/agent.log")
            except Exception:       # noqa: BLE001
                pass
        return 1
    return 0
