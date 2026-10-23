"""浏览类接口：时间线、单项、缩略图/预览/原图、目录、搜索、回收站删除。

## 这个模块的分工

所有 handler 都写成 `async def f(ctx: Ctx) -> Reply`，只依赖 `ctx`。
不 import starlette、不碰 Request/Response —— 二期云端隧道复用的正是这些
函数（设计原则 DP-4「两条通路，一套 API」）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .. import audit, sandbox
from ..config import ALLOWED_IMAGE_EXTS
from ..core import Ctx, Reply, empty_reply, file_reply, json_reply, stream_reply
from ..core.cursor import decode_cursor, encode_cursor
from ..errors import (
    E1001,
    E1004,
    E1008,
    E1012,
    E1015,
    E1019,
    E3004,
    E3012,
    AppError,
    ArchiveError,
    BadRequest,
    Conflict,
    NotFound,
    ReadonlyError,
    SandboxError,
)
from ..state import AppState
from ..thumbs import placeholder_webp, source_signature, thumb_path
from ..transports.base import (
    RangeNotSatisfiable,
    content_range_header,
    content_type_for,
    parse_range,
    range_length,
    unsatisfied_range_header,
)
from . import common
from .common import (
    TIMELINE_COLUMNS,
    etag_matches,
    iter_file,
    normalize_dir,
    parse_size,
    parse_ts,
    row_to_detail,
    row_to_item,
    stat_size,
    thumb_etag,
    timeline_order,
    timeline_where,
)

log = logging.getLogger(__name__)

__all__ = [
    "timeline",
    "get_item",
    "get_thumb",
    "get_preview",
    "get_raw",
    "folders",
    "search",
    "delete_items",
    "rescan",
]

MAX_LIMIT = 500
DEFAULT_LIMIT = 100
PLACEHOLDER_SIZE = 320

#: 兜底缩略图解码用的**单线程**池（没有 ThumbEngine 时才会用到）。
#:
#: ★ 原来这里传的是 `run_in_executor(None, ...)`，也就是 asyncio 的默认
#: 线程池 —— 默认池的大小是 `min(32, cpu+4)`，完全不受 `ThumbEngine._sem`
#: （4 并发）约束。结果是"正常路径有并发闸门、兜底路径没有"：4 个引擎
#: 解码 + N 个兜底解码同时跑，一张 48MP 图解码后是 144MB RGB，几路并发
#: 就能把家用小主机的内存吃光。
#:
#: 用模块级单线程而不是"每个请求一个池"：池的创建成本要摊掉，而且这里
#: 要的正是**全局串行**（同一时刻只解一张图）。它与事件循环无关，
#: 请求之间的排队发生在队列里，不占事件循环。
_THUMB_FALLBACK_POOL = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="thumb-fallback"
)


def _st(ctx: Ctx) -> AppState:
    st = getattr(ctx, "app", None)
    if st is None:  # pragma: no cover
        raise RuntimeError("AppState 未注入 Ctx（server 装配遗漏）")
    return st


# --------------------------------------------------------------------------
# 时间线
# --------------------------------------------------------------------------

async def timeline(ctx: Ctx) -> Reply:
    """`GET /api/timeline` —— 按拍摄时间倒序翻页。

    ## 为什么必须用游标而不是 OFFSET

    `LIMIT 100 OFFSET 200` 在**索引器正在跑**的时候会出错：新照片插入到
    列表前面，整个窗口后移，用户翻到第 3 页会看到第 2 页出现过的照片
    （重复），同时漏掉另一些（丢失）—— 这就是"分页漂移"。
    游标锚定在具体一行上，插入多少新行都不影响。

    ## 为什么游标里必须有 id

    `taken_at` 不唯一：连拍同秒、批量拷贝时 mtime 相同（实测真实库中
    有大量同秒记录）。只带 taken_at 的话，同秒的其余记录会被整段跳过。
    """
    st = _st(ctx)
    db = st.db

    limit = ctx.q_int("limit", DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)
    order_sql = timeline_order(ctx.q("order"))

    root_id = common.root_id_param(ctx)
    if root_id is not None and root_id != "":
        if not st.cfg.has_root(root_id):
            raise AppError(E1001, f"未知的 root：{root_id}")

    ts_from = parse_ts(ctx.q("from"), what="from")
    ts_to = parse_ts(ctx.q("to"), what="to")
    if ts_from is not None and ts_to is not None and ts_from > ts_to:
        raise AppError(E1001, "from 晚于 to")

    where, params = timeline_where(
        root_id=root_id or None,
        ts_from=ts_from,
        ts_to=ts_to,
        kind=ctx.q("kind"),
    )

    cursor = ctx.q("cursor")
    if cursor:
        cur_ts, cur_id = decode_cursor(cursor)
        # 括号不能省：`taken_at < ? OR (taken_at = ? AND id < ?)` 与
        # 不带括号的版本在 SQL 里含义完全不同（后者会无视 taken_at 越界）。
        where += " AND (taken_at < ? OR (taken_at = ? AND id < ?))"
        params.extend([cur_ts, cur_ts, cur_id])

    # ★ 多取一条判断 has_more。用 COUNT(*) 另查一次会在 5000 行时
    # 多扫一遍全表；多取一行的代价可以忽略。
    sql = (f"SELECT {TIMELINE_COLUMNS} FROM media{where}"
           f" {order_sql} LIMIT ?")
    rows = db.query(sql, [*params, limit + 1])

    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    items = [row_to_item(r) for r in rows]
    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(float(last["taken_at"]), int(last["id"]))

    return json_reply({
        "items": items,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "server_time": time.time(),
        "tz_offset_minutes": int(st.cfg.tz_offset_minutes),
    })


# --------------------------------------------------------------------------
# 单项
# --------------------------------------------------------------------------

async def get_item(ctx: Ctx) -> Reply:
    """`GET /api/items/{id}` —— 完整 EXIF + 前后邻居。"""
    st = _st(ctx)
    db = st.db
    mid = ctx.param_int("media_id")

    row = db.query_one("SELECT * FROM media WHERE id = ?", (mid,))
    if row is None:
        raise NotFound(f"media id={mid}")

    # 前后邻居用于查看器左右滑动。这里各查一次而不是等到滑动时再查，
    # 是因为滑动时的额外往返在弱网下会明显卡顿，而两次主键索引查询
    # 在本地只需几十微秒。
    prev_id = db.scalar(
        "SELECT id FROM media WHERE missing = 0 AND kind = 'image'"
        " AND (taken_at > ? OR (taken_at = ? AND id > ?))"
        " ORDER BY taken_at ASC, id ASC LIMIT 1",
        (row["taken_at"], row["taken_at"], mid),
        default=None,
    )
    next_id = db.scalar(
        "SELECT id FROM media WHERE missing = 0 AND kind = 'image'"
        " AND (taken_at < ? OR (taken_at = ? AND id < ?))"
        " ORDER BY taken_at DESC, id DESC LIMIT 1",
        (row["taken_at"], row["taken_at"], mid),
        default=None,
    )

    return json_reply(row_to_detail(row, prev_id=prev_id, next_id=next_id))


# --------------------------------------------------------------------------
# 缩略图 / 预览 / 原图
# --------------------------------------------------------------------------

def _size_allowed(st: AppState, size: int) -> bool:
    return size in st.thumb_sizes()


def _source_sig(st: AppState, row: Any) -> str:
    """缩略图缓存签名 —— 取**磁盘上**的真实 mtime/size。

    ⚠️ 不能用 DB 里的 mtime/size。索引器与 watchdog 会把它们同步，
    但存在两个真实的空窗期：
      1. `index.watch` 关掉、或程序没在运行时用户替换了照片；
      2. 替换发生在两次扫描之间（debounce 2 秒 + 扫描耗时）。
    这期间 DB 的 mtime 还是旧值 → 签名不变 → ETag 不变 →
    而缩略图 URL 带 `immutable` 缓存一年且不再校验 →
    用户**永远**看到旧图。用磁盘值可以自愈，无需等下一次扫描。

    代价是每次缩略图请求多一次 sandbox.resolve + os.stat。
    与"多一次 200 万像素的解码"相比可以忽略。
    文件读不到（已丢失/越界）时退回 DB 值，让上层走 missing 分支。
    """
    try:
        root = st.cfg.root_by_id(row["root_id"])
        if root is None:
            raise LookupError("root 不存在")
        src = sandbox.resolve(root, row["rel_path"], "read", cfg=st.cfg)
        stt = os.stat(src)
        return source_signature(stt.st_mtime, stt.st_size)
    except Exception:  # noqa: BLE001 —— 沙箱拒绝/文件没了都用 DB 值兜底
        return source_signature(row["mtime"], row["size"])


async def _serve_thumb(st: AppState, ctx: Ctx, mid: int) -> Reply:
    db = st.db
    sizes = st.thumb_sizes()
    size = parse_size(ctx.q("size"), sizes)

    row = db.query_one(
        "SELECT id, root_id, rel_path, mtime, size, orientation,"
        "       thumb_state, thumb_etag, missing FROM media WHERE id = ?",
        (mid,),
    )
    if row is None:
        raise NotFound(f"media id={mid}")

    if not row["missing"] and row["thumb_state"] == "failed":
        # 已判定失败的图不再反复重试：每次请求都重试会让一张坏图
        # 持续占用线程池（用户在列表里反复划到它），而它 100% 还会失败。
        raise AppError(E3004, f"thumb failed media_id={mid}")

    src_sig = _source_sig(st, row)
    sig = f"w{size}-{src_sig}"

    dst = thumb_path(st.cfg, row["root_id"], row["rel_path"], size)

    # ★ 是否陈旧必须在**生成之前**判定。
    # 早先写成 `need_generate = not dst.exists() or row["thumb_etag"] != thumb_version(row)`，
    # 而 `thumb_version(row)` 就是 `row["thumb_etag"]` 本身 —— 这个条件恒为 False，
    # 于是只有"文件不存在"才会触发生成：用户手工替换了同名照片（mtime 变了），
    # 旧缩略图仍在磁盘上，程序会**永远**继续端出旧图。
    #
    # 现在统一用源签名（mtime-size）对比。`thumb_etag IS NULL` 也算陈旧：
    # 那是早期版本或手工改库留下的行，重算一次即可自愈（生成后会写回）。
    stored = row["thumb_etag"]
    stale = stored != src_sig
    need_generate = (not dst.exists()) or stale

    if need_generate and not row["missing"] and row["thumb_state"] != "failed":
        # ★ 陈旧时必须**先删掉旧缓存文件**。
        # `generate_thumb` 内部用的是 `open(tmp,"wb")` 再 os.replace，
        # 所以不会"跳过已存在的目标"；但引擎的 `ensure()` 会因为
        # `thumb_etag == sig` 之外的路径直接 return False，而此刻
        # 磁盘上躺着的仍是旧图。不删就等于把过期内容继续端出去。
        if stale and dst.exists():
            try:
                dst.unlink()
            except OSError as e:  # noqa: BLE001
                log.warning("删除陈旧缩略图失败 %s: %s", dst.name, e)

        engine = st.thumbs
        if engine is not None:
            try:
                await engine.ensure(mid)
            except Exception as e:  # noqa: BLE001
                log.warning("thumb ensure failed media_id=%s: %s", mid, e)
        # 引擎不可用、队列已满、或它只生成了 1600 而没生成 320 时，
        # 退回同步生成：用户此刻正盯着这张图，给占位图不如多花 30ms 真生成一张。
        if not dst.exists():
            try:
                await _generate_sync(st, row, dst, size)
            except Exception as e:  # noqa: BLE001
                log.warning("thumb sync generate failed media_id=%s: %s", mid, e)

    if row["missing"] or not dst.exists():
        # ★ 不返回 404/500，而是返回灰色占位图 + 200。
        # 时间线上一个破图会让用户以为"照片丢了"，而实际只是
        # 缩略图还没生成出来（后台队列在忙）。前端看到 200 的占位图
        # 会正常渲染，用户稍后刷新即可。
        #
        # ★★ `row["missing"]` 也走这条路（审计 F5）。
        # 已删除/已丢失的照片，缩略图缓存文件通常**还在磁盘上** ——
        # 它从不被清理（`thumb_path` 没有任何 unlink 调用）。
        # 早先这里只判 `not dst.exists()`，于是 missing 的行照旧走
        # `_file_thumb_reply()`，带上 `Cache-Control: max-age=31536000,
        # immutable` 端出去 —— 手机缓存一年且**不再发请求**。
        # 用户删了照片、界面上看着消失了，可只要那台手机曾经加载过
        # 列表，一年之内它仍然能把这批"已删除"的缩略图从缓存里拿出来；
        # 反过来，如果用户删错了想恢复，客户端也不会重新请求，
        # 因为他手上那份看起来完全正常。
        # 占位图**整个不缓存**（审计 F5 的原文建议是 `Cache-Control: no-store`）。
        # 60 秒对一个"删除 → 撤销删除"的动作仍然太长：用户删错了、马上撤销，
        # 界面在接下来一分钟里还是灰图，而 `no-store` 一刷新就恢复。
        # 占位图只有几百字节，重算一次的成本远低于"让用户怀疑照片没救回来"。
        data = placeholder_webp(size or PLACEHOLDER_SIZE)
        r = Reply(status=200, body=data, media_type="image/webp")
        r.headers["Content-Type"] = "image/webp"
        r.headers["Content-Length"] = str(len(data))
        r.headers["Cache-Control"] = "no-store"
        r.set("Vary", "Authorization")
        return r

    # ⚠️ 304 必须排在 `missing` 判定**之后**（审计 F5 残留的同源修正）。
    # 早先它排在函数最前面，于是"已删除的照片 + 手机带着旧 ETag 来问"
    # 拿到 304，而 304 的语义是"继续用你缓存那份" —— 那份正是照片
    # 还活着时生成的缩略图。上面那个 `no-store` 占位图分支永远走不到，
    # `no-store` 也就永远不会被执行。已删除的照片没有可校验的实体，
    # 只能回一条不缓存的占位图。
    #
    # 条件请求 → 304。省下的是**隧道带宽**，弱网下价值最大。
    if etag_matches(ctx.header("if-none-match"), sig):
        r = empty_reply(304)
        r.set_etag(sig)
        r.set("Vary", "Authorization")
        return r

    return await _file_thumb_reply(st, dst, sig, ctx)


async def _generate_sync(st: AppState, row: Any, dst: Path, size: int) -> None:
    """没有 ThumbEngine 时的兜底生成（测试与轻量嵌入场景）。"""
    from ..thumbs import OversizedImageError

    src = sandbox.resolve(st.cfg.root_by_id(row["root_id"]), row["rel_path"],
                          "read", cfg=st.cfg)
    if not src.exists():
        return
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(_THUMB_FALLBACK_POOL, _call_generate, str(src),
                                   dst, size, row["orientation"])
    except OversizedImageError as exc:
        # 声明尺寸超单边上限 → 走与 ThumbEngine 完全相同的"永久失败"落库，
        # 让时间线拿到占位图而不是反复重试。不落库的话每次刷新都会再试一次，
        # 而这类图每次都要读文件头 + 抛异常，是个可以被反复触发的放大型操作。
        log.warning("thumb skipped (image too large) media_id=%s: %s",
                    row["id"], exc)
        st.db.execute("UPDATE media SET thumb_state='failed' WHERE id=?", (row["id"],))
        return
    # ★ 写回**磁盘上**的签名，与 `_source_sig()` 保持同一口径。
    # 若这里写 DB 的旧 mtime，下一次请求算出的 disk 签名与它不等，
    # 就会判定"陈旧"并重新生成 —— 用户每刷新一次页面就重生成一遍缩略图。
    stt = os.stat(src)
    st.db.execute(
        "UPDATE media SET thumb_state='ok', thumb_etag=? WHERE id=?",
        (source_signature(stt.st_mtime, stt.st_size), row["id"]),
    )


def _call_generate(src: str, dst: Path, size: int, orientation: Any) -> None:
    from ..thumbs import generate_thumb

    generate_thumb(Path(src), dst, size, orientation=orientation)


async def _file_thumb_reply(st: AppState, dst: Path, sig: str, ctx: Ctx) -> Reply:
    """把已存在的缩略图文件流出去。"""
    try:
        length = stat_size(dst)
    except OSError:
        raise NotFound("缩略图文件已消失") from None

    r = stream_reply(iter_file(dst), "image/webp", 200, length=length)
    r.headers["Content-Type"] = "image/webp"
    r.set_etag(sig)
    # ★ immutable + URL 带 v= 版本参数（接口文档方案 A）。
    # 若只加 immutable 而不带版本参数，用户替换同名照片后
    # 手机会**永远**显示旧图 —— 缓存一年且不再校验。
    r.set_cache(31536000, immutable=True, private=True)
    r.set("Vary", "Authorization")
    return r


async def get_thumb(ctx: Ctx) -> Reply:
    """`GET /api/items/{id}/thumb?size=320`"""
    return await _serve_thumb(_st(ctx), ctx, ctx.param_int("media_id"))


async def get_preview(ctx: Ctx) -> Reply:
    """`GET /api/items/{id}/preview?size=1600` —— 查看器用的中等尺寸。

    预览图**按需生成但不写回 DB 的 thumb_state**：1600 是"用户点开了这张"
    的信号，不是批量后台任务；把它计入 thumb_state 会让重扫时的
    "待生成"数量与用户预期不符。
    """
    st = _st(ctx)
    db = st.db
    mid = ctx.param_int("media_id")
    sizes = st.thumb_sizes()
    default = sizes[-1] if sizes else 1600
    size = parse_size(ctx.q("size"), sizes, name="size") if ctx.q("size") else default

    row = db.query_one(
        "SELECT id, root_id, rel_path, mtime, size, orientation, missing"
        "  FROM media WHERE id = ?",
        (mid,),
    )
    if row is None:
        raise NotFound(f"media id={mid}")

    dst = thumb_path(st.cfg, row["root_id"], row["rel_path"], size)
    sig = f"p{size}-{int(row['mtime'])}-{int(row['size'])}"

    # ⚠️ 下面四步的顺序是**判据的一部分**，不能调换（审计 F5 残留）：
    #   ① missing → 占位图（不管缓存文件在不在）
    #   ② 缓存文件也不在 → 占位图 / 404
    #   ③ 都齐了 → 304 与带一年缓存的流
    #
    # 早先 304 排在最前、`missing` 只在"缓存文件也不在"的分支里被用到，
    # 于是已删除的照片仍然命中 `dst.exists()`（缩略图缓存从不被清理），
    # 一路落到末尾的 `set_cache(31536000, immutable)`：手机缓存一年、
    # 一年内不再发请求 —— 用户删了照片，那台手机仍能从缓存里拿出来；
    # 删错了想恢复，也不会重新请求，因为手上那份看着完全正常。
    #
    # 占位图**不缓存在 DB 侧也不缓存在客户端**（`no-store`）：60 秒对一个
    # "删除 → 撤销删除"的动作仍然太长，而占位图只有几百字节，
    # 重算一次的成本远低于"让用户怀疑照片没救回来"。
    if row["missing"]:
        data = placeholder_webp(size)
        r = Reply(status=200, body=data, media_type="image/webp")
        r.headers["Content-Type"] = "image/webp"
        r.headers["Content-Length"] = str(len(data))
        r.headers["Cache-Control"] = "no-store"
        r.set("Vary", "Authorization")
        return r

    if etag_matches(ctx.header("if-none-match"), sig):
        r = empty_reply(304)
        r.set_etag(sig)
        r.set("Vary", "Authorization")
        return r

    if not dst.exists():
        try:
            if st.thumbs is not None:
                await _ensure_size(st, row, dst, size)
            else:
                await _generate_sync(st, row, dst, size)
        except Exception as e:  # noqa: BLE001
            log.warning("preview generate failed media_id=%s: %s", mid, e)
        # 生成可能成功写出文件但引擎返回 False（队列满/已存在），
        # 也可能失败什么都没留下。统一再看一眼磁盘，别把刚生成好的
        # 预览图当占位图端出去。
        if not dst.exists():
            try:
                await _generate_sync(st, row, dst, size)
            except Exception as e:  # noqa: BLE001
                log.warning("preview sync generate failed media_id=%s: %s", mid, e)

    if not dst.exists():
        data = placeholder_webp(size)
        r = Reply(status=200, body=data, media_type="image/webp")
        r.headers["Content-Type"] = "image/webp"
        r.headers["Content-Length"] = str(len(data))
        r.headers["Cache-Control"] = "no-store"
        r.set("Vary", "Authorization")
        return r

    try:
        length = stat_size(dst)
    except OSError:
        raise NotFound("预览图文件已消失") from None
    r = stream_reply(iter_file(dst), "image/webp", 200, length=length)
    r.headers["Content-Type"] = "image/webp"
    r.set_etag(sig)
    r.set_cache(31536000, immutable=True, private=True)
    r.set("Vary", "Authorization")
    return r


async def _ensure_size(st: AppState, row: Any, dst: Path, size: int) -> None:
    """只生成指定尺寸（预览用），不碰 thumb_state。"""
    from ..thumbs import generate_thumb

    src = sandbox.resolve(st.cfg.root_by_id(row["root_id"]), row["rel_path"],
                          "read", cfg=st.cfg)
    if not src.exists():
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, generate_thumb, src, dst, size,
                               row["orientation"])


async def get_raw(ctx: Ctx) -> Reply:
    """`GET /api/items/{id}/raw` —— 原图，支持 Range。

    ## Range 在一期就必须做

    2GB 的原图在 5 Mbps 下要 55 分钟。不支持 Range 的话，手机下到 95%
    断网就得从头再来。这不是优化项，是"能不能用"的问题。
    """
    st = _st(ctx)
    db = st.db
    mid = ctx.param_int("media_id")

    row = db.query_one(
        "SELECT id, root_id, rel_path, file_name, ext, size, missing"
        "  FROM media WHERE id = ?",
        (mid,),
    )
    if row is None:
        raise NotFound(f"media id={mid}")
    if row["missing"]:
        raise NotFound(f"源文件已丢失 media_id={mid}")

    root = st.cfg.root_by_id(row["root_id"])
    # ★ 即使路径来自我们自己的数据库，也要过沙箱：配置可能已经改过，
    # 旧行的 rel_path 现在可能指向库外（或变成 junction）。
    src = sandbox.resolve(root, row["rel_path"], "read", cfg=st.cfg)
    if not src.is_file():
        raise NotFound(f"源文件不存在 media_id={mid}")

    try:
        size = stat_size(src)
    except OSError:
        raise NotFound("源文件无法读取") from None

    media_type = content_type_for(row["ext"])
    download = ctx.q_bool("download", False)
    filename = common.download_filename(
        ctx.q("name"), row["file_name"]) if download else None

    try:
        rng = parse_range(ctx.header("range"), size)
    except RangeNotSatisfiable:
        # ★ 必须回 416 而不是静默给 200 全文件：那会让客户端以为
        # "续传成功了"，实际把重复数据拼进已下载的部分，得到损坏文件。
        #
        # ⚠️ Content-Range 必须挂在**异常**上。早先写成"先构造一个 416
        # Reply 再 raise"，那个 Reply 在抛出时就被丢弃了，dispatch 只认
        # app_exc.headers —— 客户端收到一个没有 Content-Range 的 416，
        # 按 RFC 7233 无法得知文件总长，只能整份重下。
        raise AppError(E1008, headers={
            "Content-Range": unsatisfied_range_header(size),
            "Accept-Ranges": "bytes",
        }) from None

    if rng is None:
        r = file_reply(iter_file(src), media_type, size, 200, filename=filename)
        r.headers["Accept-Ranges"] = "bytes"
        # 原图不做长缓存：用户可能重命名/替换，而原图的 ETag 变化
        # 不一定会触发前端刷新 URL。
        r.set_cache(0, private=True)
        _apply_disposition(r, filename, download=download)
        return r

    start, end = rng
    r = file_reply(iter_file(src, start=start, length=end - start + 1),
                   media_type, range_length(rng), 206, filename=filename,
                   headers={"Content-Range": content_range_header(rng, size)})
    r.headers["Accept-Ranges"] = "bytes"
    r.set_cache(0, private=True)
    _apply_disposition(r, filename, download=download)
    return r


def _apply_disposition(reply: Reply, filename: str | None,
                       *, download: bool) -> None:
    """覆盖 file_reply 默认的 `inline`。

    `file_reply` 一律写 inline（对缩略图/预览是对的：内联才能在 <img>
    里显示），但 `/raw?download=1` 必须用 attachment —— 用 inline 时
    手机浏览器会**在页面里打开图片**而不是存进相册，用户点"下载原图"
    却发现相册里什么都没有。
    """
    if filename:
        reply.headers["Content-Disposition"] = common.filename_disposition(
            filename, attachment=download)


# --------------------------------------------------------------------------
# 目录浏览
# --------------------------------------------------------------------------

async def folders(ctx: Ctx) -> Reply:
    """`GET /api/folders?root=&path=&depth=` —— 目录树 + 递归计数。

    计数用 `rel_path LIKE '前缀/%'` 一次算完，而不是每个目录查一次：
    真实库里 3 个一级目录、几十个二级目录，逐个查询会变成 N+1
    （N 可能上百），而一次 GROUP BY 只需扫一遍索引。
    """
    st = _st(ctx)
    db = st.db

    root_id = common.root_id_param(ctx) or st.cfg.default_root_id
    if not root_id or not st.cfg.has_root(root_id):
        raise AppError(E1001, f"未知的 root：{root_id}")
    root = st.cfg.root_by_id(root_id)
    assert root is not None

    raw_path = ctx.q("path") or ""
    # path 参与 SQL 前缀匹配，必须先用沙箱归一化。
    # 归一化只做形态整理（不做安全检查），所以这里还要**显式**拒绝 `..`：
    # 不拒绝的话 `path=../..` 会静默匹配不到任何目录并返回空列表，
    # 用户看到"这个目录是空的"而不是"路径非法"，会一直以为照片丢了。
    try:
        base = sandbox.normalize_rel(raw_path).strip("/")
    except Exception:
        raise AppError(E1001, "path 参数非法") from None
    if any(seg == ".." for seg in base.split("/")):
        raise AppError(E1001, "path 参数非法")

    depth = ctx.q_int("depth", 1, minimum=1, maximum=3)

    prefix = base + "/" if base else ""
    # 只查一层：先取 `prefix%`，再在 Python 里按 '/' 切出第 depth 层。
    # 用 SQL 的 substr/instr 也能做，但可读性差且容易写错（多一层少一层
    # 的 bug 很难靠测试发现），而候选行数在有索引时很小。
    rows = db.query(
        "SELECT rel_path FROM media"
        " WHERE missing = 0 AND kind = 'image' AND root_id = ?"
        "   AND rel_path LIKE ? ESCAPE '\\'",
        (root_id, _esc(base) + "/%" if base else "%"),
    )

    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        rel = r["rel_path"]
        rest = rel[len(prefix):] if prefix and rel.startswith(prefix) else rel
        segs = rest.split("/")
        if len(segs) < 2:
            continue                      # 直接在根下的文件，不属于任何子目录
        depth_segs = segs[:depth]
        if len(segs) - 1 < depth:
            continue                      # 文件所在目录比请求的深度浅
        sub = "/".join(depth_segs)
        b = buckets.get(sub)
        if b is None:
            b = {"name": depth_segs[-1], "path": (prefix + sub).strip("/"),
                 "count": 0, "has_children": False, "cover_id": None}
            buckets[sub] = b
        b["count"] += 1
        if b["cover_id"] is None:
            b["cover_id"] = None          # 封面单独查，避免这里多带一列
        if len(segs) - 1 > depth:
            b["has_children"] = True

    items = sorted(buckets.values(), key=lambda x: x["path"])
    _attach_covers(db, root_id, items)

    return json_reply({
        "root": root_id,
        "path": base,
        "folders": items,
        "total": len(items),
    })


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _attach_covers(db: Any, root_id: str, items: list[dict[str, Any]]) -> None:
    """给每个目录挑一张封面（最新的那张）。

    用 `MIN(id)` 不够：封面应当是**最新**的照片才符合"相册目录"的直觉。
    这里对每个目录各查一次，但目录数量是个位数到几十，且走的是
    idx_media_timeline（taken_at DESC, id DESC），代价可控。
    """
    for it in items:
        p = it["path"].strip("/")
        if not p:
            continue
        it["cover_id"] = db.scalar(
            "SELECT id FROM media WHERE missing = 0 AND kind = 'image'"
            " AND root_id = ? AND (rel_path LIKE ? ESCAPE '\\')"
            " ORDER BY taken_at DESC, id DESC LIMIT 1",
            (root_id, _esc(p) + "/%"),
            default=None,
        )


# --------------------------------------------------------------------------
# 搜索
# --------------------------------------------------------------------------

async def search(ctx: Ctx) -> Reply:
    """`GET /api/search` —— 组合筛选。

    ⚠️ 至少要有一个筛选条件，否则报 400。
    允许空查询等于给了一个"扫全库并返回 500 条"的免费接口，
    手机端一个 bug 就能把索引器挤掉。
    """
    st = _st(ctx)
    db = st.db

    q = (ctx.q("q") or "").strip()
    # ★ 审计 F16：`q` 里含 C0 控制字符就直接 400，别让它进 LIKE。
    #
    # SQLite 的 LIKE 在**模式串的 NUL 处截断**：`"%\x00%"` 退化为 `"%"`，
    # 于是 `?q=<NUL>` 会 200 返回全表（实测 hits=41 = 库里全部行）。
    # 这不是 SQL 注入（参数化与 `_esc` 都正确），被削掉的是下面那条
    # "至少需要一个筛选条件"的守卫意图 —— 它想防的正是"手机端一个
    # bug 就把索引器挤掉"，而一个退化成 `%` 的模式串等价于没有条件。
    #
    # 顺带挡掉其它 C0（`\x01`–`\x1f`）：它们在同一条 LIKE 上都不可见、
    # 也都不是用户在搜索框里能打出或想搜的东西，一并拒绝比逐个解释简单。
    # `\t` 放行 —— 粘贴来的文本里夹制表符是真实会发生的。
    if any(ch < " " and ch != "\t" for ch in q):
        raise AppError(E1001, "搜索词含不可见控制字符")
    root_id = common.root_id_param(ctx) or None
    ts_from = parse_ts(ctx.q("from"), what="from")
    ts_to = parse_ts(ctx.q("to"), what="to")
    camera = ctx.q("camera") or None
    dir_prefix = ctx.q("dir") or None
    gps_raw = ctx.q("has_gps")
    has_gps = None if gps_raw is None or gps_raw == "" else ctx.q_bool("has_gps")

    if root_id is not None and not st.cfg.has_root(root_id):
        raise AppError(E1001, f"未知的 root：{root_id}")
    if not any([q, root_id, ts_from is not None, ts_to is not None,
                camera, dir_prefix, has_gps is not None]):
        raise AppError(E1001, "至少需要一个筛选条件")

    limit = ctx.q_int("limit", DEFAULT_LIMIT, minimum=1, maximum=MAX_LIMIT)

    where, params = timeline_where(
        root_id=root_id, ts_from=ts_from, ts_to=ts_to,
        dir_prefix=dir_prefix, camera=camera, has_gps=has_gps,
    )
    if q:
        # 一期用 LIKE 而非 FTS5：文件名匹配是"包含"语义，
        # 而 FTS5 是分词匹配 —— 用户搜 "2024" 时 FTS5 会把
        # `2024-05-12` 切成分词后匹配到，看似正确，但搜中文文件名
        # （如 "敦煌"）在没有自定义分词器时反而匹配不到，
        # 因为 unicode61 不切中文。LIKE 虽慢但**行为可预期**。
        # 5000 行规模下 LIKE 全扫约 3ms，完全可接受。
        #
        # ★ 必须同时匹配 rel_path，只搜 file_name 是不够的：
        # 归档会把文件名改成 `20240512_183000.jpg`，用户早就记不住
        # 那个名字了 —— 他记得的是"敦煌那张""青甘环线里的"。
        # 只搜文件名时搜"敦煌"返回 0 条，用户会认为整个搜索坏了。
        # rel_path 里保留着原目录结构，是唯一还认得出来的线索。
        # （归档按设计**不保留原文件名**，原文件名只在
        #   upload_sessions.declared_name 与审计日志里。）
        # camera 也一并匹配：用户常搜 "iPhone" 或 "vivo"。
        pattern = "%" + _esc(q) + "%"
        where += (" AND (file_name LIKE ? ESCAPE '\\'"
                  " OR rel_path LIKE ? ESCAPE '\\'"
                  " OR camera LIKE ? ESCAPE '\\')")
        params.extend([pattern, pattern, pattern])

    cursor = ctx.q("cursor")
    if cursor:
        cur_ts, cur_id = decode_cursor(cursor)
        where += " AND (taken_at < ? OR (taken_at = ? AND id < ?))"
        params.extend([cur_ts, cur_ts, cur_id])

    rows = db.query(
        f"SELECT {TIMELINE_COLUMNS} FROM media{where}"
        f" ORDER BY taken_at DESC, id DESC LIMIT ?",
        [*params, limit + 1],
    )
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]

    next_cursor = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(float(last["taken_at"]), int(last["id"]))

    return json_reply({
        "items": [row_to_item(r) for r in rows],
        "next_cursor": next_cursor,
        "has_more": has_more,
        "server_time": time.time(),
        "tz_offset_minutes": int(st.cfg.tz_offset_minutes),
    })


# --------------------------------------------------------------------------
# 写操作：删除（回收站）
# --------------------------------------------------------------------------

async def delete_items(ctx: Ctx) -> Reply:
    """`POST /api/items/delete` —— 删除到**系统回收站**。

    ## 为什么只到回收站

    照片库是不可再生的数据。程序里不存在"永久删除"这条代码路径：
    所有删除都走 `send2trash`，用户在资源管理器里就能恢复。
    这也让"误点删除"从灾难降级为小麻烦。

    ## 为什么还要查配置

    `auth.allow_delete` 默认 False（安全默认）。即使用户把根目录设成
    readwrite，删除仍需显式开启 —— 两个开关管两件事，
    避免用户为了"能上传"而顺带得到"能删照片"。
    """
    st = _st(ctx)
    db = st.db
    cfg = st.cfg

    if not bool(cfg.get("auth.allow_delete", False)):
        raise AppError(E1015, "删除功能未在电脑端开启")

    if not bool(cfg.get("auth.delete_to_recycle_bin", True)):
        # 配置说不要走回收站 = 要求永久删除，而本程序不提供永久删除。
        raise AppError(E1015, "永久删除不受支持")

    body = await ctx.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise BadRequest("ids 必须是非空数组")
    if len(ids) > 200:
        # 一次删太多会让"部分成功"变得难以解释，也让审计日志失去可读性。
        raise BadRequest("一次最多删除 200 项")

    clean: list[int] = []
    for v in ids:
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise BadRequest("ids 必须是整数数组") from None
        clean.append(n)

    deleted = 0
    failed: list[dict[str, Any]] = []
    for mid in clean:
        try:
            if await _trash_one(st, mid):
                deleted += 1
            else:
                failed.append({"id": mid, "error": "not_found"})
        except Exception as e:  # noqa: BLE001 —— 一项失败不该中断整批
            log.warning("delete failed media_id=%s: %s", mid, e)
            failed.append({"id": mid, "error": "failed"})

    return json_reply({"deleted": deleted, "failed": failed})


async def _trash_one(st: AppState, mid: int) -> bool:
    """把一张照片移入系统回收站并标记 missing。

    先移文件再改库：反过来（先标 missing 再移文件）如果进程在中间被杀，
    文件还在但库里已标记丢失，用户会以为照片没了 —— 而实际只是
    一次未完成的操作。先移文件则最坏情况是"文件进了回收站、库里还在"，
    下次扫描会自动标 missing。
    """
    from send2trash import send2trash

    db = st.db
    row = db.query_one(
        "SELECT id, root_id, rel_path FROM media WHERE id = ?", (mid,))
    if row is None:
        return False

    root = st.cfg.root_by_id(row["root_id"])
    if root is None:
        return False
    path = sandbox.resolve(root, row["rel_path"], "delete", cfg=st.cfg)

    if path.exists():
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, send2trash, str(path))

    db.execute("UPDATE media SET missing = 1 WHERE id = ?", (mid,))
    audit.record(
        db, st.cfg, audit.ACTION_DELETE,
        rel_path=row["rel_path"], device=st.device_name,
        detail={"media_id": mid, "root": row["root_id"]},
    )
    return True


# --------------------------------------------------------------------------
# 写操作：移动与改名
# --------------------------------------------------------------------------
#
# 这两个接口在需求里是明确要做的（P1-2.8「允许移动 / 改名」，docs/01 的 Q-25
# 业主确认"允许改名"），但此前只有删除实现了。审计动作 `move` / `rename`
# 早已在 audit.py 与 db.py 的 CHECK 约束里，缺的只是调用点。
#
# ## 为什么移动必须区分"同卷"与"跨卷"
#
# `os.replace` 在**同卷**内是原子的（NTFS 上是 MFT 记录改写），但在
# **跨卷**时 Windows 直接抛 `OSError [WinError 17]`（EXDEV）—— 它根本不
# 会退化成复制。所以跨卷只能走 `shutil.move`（复制 + 删源），而这是
# **唯一允许的非原子操作**。它必须满足两条，否则宁可失败：
#
#   1. 复制完成后校验目标大小与源一致，**校验通过才删源**；
#   2. 中途任何失败 → 删掉目标半成品，**源文件保持不动**。
#
# 违反其中任何一条，用户就可能同时失去两边 —— 这是本模块最不能出的错。

#: 一次移动/改名最多处理多少项。与 delete 的 200 保持一致：
#: 再多会让"部分成功"难以解释，审计日志也会变成一堵墙。
_MAX_MOVE_BATCH = 200


def _split_dir_name(rel_path: str) -> tuple[str, str]:
    """`a/b/c.jpg` → `("a/b", "c.jpg")`。根目录返回 `("", name)`。"""
    p = rel_path.replace("\\", "/")
    if "/" in p:
        d, name = p.rsplit("/", 1)
        return d, name
    return "", p


def _unique_name_in(
    taken_names: set[str], name: str, *, limit: int = 999
) -> tuple[str, bool]:
    """在已占用的名字集合里给 `name` 找一个不冲突的名字。

    冲突时在**扩展名前**追加 `_1`/`_2`…（与归档的序号规则一致，
    序号从 1 开始，第一张不带序号）。

    返回 `(最终名字, 是否改过名)`。这是纯字符串层面的工具，供
    `_pick_free_name` 之外的调用方使用（例如未来的"批量重命名"）。
    """
    if name not in taken_names:
        return name, False
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, dot, ext = name, "", ""
    for seq in range(1, limit + 1):
        cand = f"{stem}_{seq}{dot}{ext}"
        if cand not in taken_names:
            return cand, True
    raise Conflict(f"目标目录里同名文件过多（{name}）", spec=E3012)


def _pick_free_name(
    root, rel_dir: str, name: str, taken: set[str], *, cfg, limit: int = 999
) -> tuple[str, str, Path] | None:
    """给 `name` 找一个**库和磁盘都没有占用**的名字。

    为什么必须同时查两处：
    - **只看库不够**：用户手动往库里拖了一张照片而扫描还没跑到时，
      库里查不到它，但磁盘上有 —— 只按库去重会把它覆盖掉。
    - **只看磁盘不够**：库里有行但文件刚好被回收站拿走的窗口期，
      磁盘探测看不到它，而那条记录马上就会变成"同名"。

    先试原名（库里没占且磁盘上没有就直接用），再依次试 `_1`/`_2`…
    序号规则与归档一致（在扩展名前追加，从 1 开始）。
    全部占用则返回 None，由调用方按 `on_conflict` 记 failed。
    """
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, dot, ext = name, "", ""

    def _try(cand: str) -> tuple[str, str, Path] | None:
        rel = f"{rel_dir}/{cand}" if rel_dir else cand
        p = sandbox.resolve(root, rel, "write", cfg=cfg)
        if p.exists():
            return None
        return cand, rel, p

    if name not in taken:
        got = _try(name)
        if got is not None:
            return got
    for seq in range(1, limit + 1):
        cand = f"{stem}_{seq}{dot}{ext}"
        if cand in taken:
            continue
        got = _try(cand)
        if got is not None:
            return got
    return None


def _normalize_target_dir(target_dir: str) -> str:
    """校验目标目录的**形态**并规范化成相对路径（不做沙箱检查）。

    形态检查在循环外做一次：`target_dir` 是整批共用的参数，它非法时
    整批都该 400，而不是逐项记 failed（那样用户会收到 200 加一屏
    "失败"，却不知道原因是自己传错了参数）。

    沙箱检查留在循环内 —— 它依赖具体的 `root`，而多根时同一个
    `target_dir` 字符串在不同根下解析出的绝对路径不同。
    """
    rel = (target_dir or "").replace("\\", "/").strip().strip("/")
    if rel in ("", "."):
        return ""
    for seg in rel.split("/"):
        if seg in ("", ".", ".."):
            raise BadRequest("目标目录不能包含 . 或 .. 段")
    return rel


def _move_one_sync(src: Path, dst: Path, *, size: int) -> None:
    """把 `src` 搬到 `dst`。同卷走原子 rename，跨卷走复制+校验+删源。

    这是**同步**函数，由调用方丢进线程池（`run_in_executor`）——
    跨卷复制一个 2GB 文件会阻塞事件循环几十秒，那期间手机端所有请求
    全部卡住。
    """
    if dst.exists():
        raise FileExistsError(str(dst))
    try:
        os.replace(src, dst)
        return
    except OSError as exc:
        # WinError 17 = EXDEV（跨卷）。**只在这一种情况下降级** ——
        # 其他 OSError（权限、目标被占用）降级成复制只会掩盖真因，
        # 而且复制到一半才发现不行，反而多了一次半成品。
        if getattr(exc, "winerror", None) != 17 and exc.errno != 18:
            raise
        log.info("跨卷移动，降级为复制+删除：%s → %s", src.name, dst.name)

    tmp = dst.with_name(dst.name + ".pbmove")
    try:
        shutil.copy2(src, tmp)
        # ★ 校验通过才删源。只比大小（不重算 sha256）：移动的目标是
        # "别丢数据"，而大小不符已经能抓住截断/磁盘写满这两类真实故障；
        # 2GB 文件重算哈希要多花十几秒，用户会以为程序卡死。
        got = tmp.stat().st_size
        if got != size:
            raise ArchiveError(f"复制后大小不符：期望 {size}，实际 {got}")
        os.replace(tmp, dst)
    except BaseException:
        # 中途失败 → 删掉半成品，**源文件保持不动**。
        try:
            if tmp.exists():
                os.unlink(tmp)
        except OSError:
            log.warning("清理移动半成品失败：%s", tmp, exc_info=True)
        raise
    os.unlink(src)


def _update_rel_path(
    db: Any, mid: int, dst_rel: str, name: str,
    *, taken: tuple[float, str] | None = None,
) -> None:
    """把 media 行的 `rel_path`/`file_name`（以及可能的 taken_*）改掉。

    ★ **必须在文件搬完之后调用，且必须能失败**（审计 F4）。

    原来这里是裸 `db.execute("UPDATE media SET rel_path = ...")`，写在
    搬文件的 `try` 之外、没有 `IntegrityError` 处理。而 `media` 表有
    `UNIQUE(root_id, rel_path)` 表级约束 —— 已删除的照片只被标
    `missing=1`、行仍占着 `(root_id, rel_path)`。于是"移动到一个曾
    删除过同名文件的目录"会抛 `IntegrityError` → 500，而此时**文件
    已经搬走了**，磁盘与数据库就此分叉：用户以为还在这儿的照片，
    库里指向一个不存在的路径，下次扫描把它标成"已丢失"。

    注意 `browse.py` 里挑候选名时本来就写了 `WHERE ... AND missing = 0`
    —— 也就是说选名逻辑认为"墓碑不占位"，而唯一索引认为"墓碑占位"，
    两处语义相反。**现在统一成"墓碑不占位"**：撞上墓碑行时把那条
    墓碑删掉（它指向的文件早已进回收站，行留着只会继续占坑、
    还会在下一次扫描时被"复活"成一条重复记录）。

    只删墓碑、不删活行：撞上 `missing=0` 的行仍然是真冲突，
    那是调用方挑选名字时应该避开的情况，不该在这里静默吞掉。
    """
    # * 两条 SQL 都是全字面量，不做字符串拼接（check_lint 规则 3）。
    # 早先写成 `"... file_name = ?" + extra_sql + " WHERE id = ?"` ——
    # 虽然调用方只传字面量、实际没有注入面，但"把变量拼进 SQL"正是
    # 规则 3 要禁止的形态，留着它等于给下一个人示范错误写法。
    if taken is None:
        sql = "UPDATE media SET rel_path = ?, file_name = ? WHERE id = ?"
        params: tuple[Any, ...] = (dst_rel, name, mid)
    else:
        sql = (
            "UPDATE media SET rel_path = ?, file_name = ?,"
            " taken_at = ?, taken_src = ? WHERE id = ?"
        )
        params = (dst_rel, name, taken[0], taken[1], mid)
    try:
        db.execute(sql, params)
    except sqlite3.IntegrityError:
        # 墓碑占位：清掉它再重试一次。墓碑的定义就是 missing=1 且
        # 指向的文件已不在磁盘上，删行不会丢任何用户数据。
        tomb = db.query_one(
            "SELECT id FROM media WHERE root_id ="
            " (SELECT root_id FROM media WHERE id = ?)"
            " AND rel_path = ? AND missing = 1",
            (mid, dst_rel),
        )
        if tomb is None:
            raise
        log.info("清理占位的墓碑行 media#%s（%s）", tomb["id"], dst_rel)
        db.execute("DELETE FROM media WHERE id = ?", (tomb["id"],))
        db.execute(sql, params)



async def move_items(ctx: Ctx) -> Reply:
    """`POST /api/items/move` —— 批量移动到指定目录。

    `on_conflict` 三种语义（默认 `rename`）：
    - `rename`：目标已存在同名 → 追加 `_1`/`_2`，`renamed=true`；
    - `skip`：跳过这一项，进 `skipped`；
    - `fail`：这一项记进 `failed`，其余照常处理。

    注意 `skip`/`fail` 都是**逐项**语义：一项冲突不影响同批其他项。
    只有"目标目录本身非法"才是整批 400。
    """
    st = _st(ctx)
    db = st.db

    # ★ `auth.allow_move` 此前是个**死配置**：`config.py` 声明了它、
    # 默认 True，但代码里零读取 —— 于是用户关掉之后一切照旧，
    # 界面上看不出任何区别，只会以为自己设错了。
    #
    # 为什么移动也要单独一个开关：移动会改变照片的目录结构，
    # 而用户的相册目录往往与他自己的整理习惯（或同步工具、
    # 备份脚本）绑定。删除进回收站是可撤销的，移动在某些同步盘上
    # 会触发"删除 + 重新上传"的连锁反应。
    if not bool(st.cfg.get("auth.allow_move", True)):
        raise AppError(E1015, "移动功能未在电脑端开启")

    body = await ctx.json()
    ids = body.get("ids")
    if not isinstance(ids, list) or not ids:
        raise BadRequest("ids 必须是非空数组")
    if len(ids) > _MAX_MOVE_BATCH:
        raise BadRequest(f"一次最多移动 {_MAX_MOVE_BATCH} 项")

    target_dir = body.get("target_dir")
    # 空串是**合法**的，表示库根 —— 不能因为"空"就当成参数缺失
    # （手机端"移到根目录"是正常操作）。
    if not isinstance(target_dir, str):
        raise BadRequest("target_dir 必须是字符串（空串表示库根）")

    on_conflict = str(body.get("on_conflict") or "rename").lower()
    if on_conflict not in ("rename", "skip", "fail"):
        raise BadRequest("on_conflict 只能是 rename / skip / fail")

    clean: list[int] = []
    for v in ids:
        try:
            clean.append(int(v))
        except (TypeError, ValueError):
            raise BadRequest("ids 必须是整数数组") from None

    # 目标目录按每个媒体行自己的 root_id 解析。一期只有一个根，但代码
    # 不该写死 root_id —— 多根是二期明确定下来的方向，而"根 A 的照片
    # 移动到根 A 的目录"和"根 B 的照片也移动到同一个 target_dir"是两件
    # 事（同一个 target_dir 字符串在不同根下解析出不同绝对路径）。
    # 形态检查在循环外做一次（参数非法 → 整批 400），沙箱检查在循环内。
    target_rel = _normalize_target_dir(target_dir)
    moved: list[dict[str, Any]] = []
    skipped: list[int] = []
    failed: list[dict[str, Any]] = []
    resolved_dirs: dict[str, str] = {}
    loop = asyncio.get_running_loop()

    for mid in clean:
        row = db.query_one(
            "SELECT id, root_id, rel_path, file_name, size, missing"
            "  FROM media WHERE id = ?",
            (mid,),
        )
        if row is None or row["missing"]:
            # ★ 审计 F14 第 3 条：missing 行原本一律回 "照片不存在"，
            # 而"照片被移进了扫描排除目录"这一种恰恰是**能用一句话说清、
            # 且用户能自己修**的 —— 文件还在磁盘上，只是索引器以后不再扫它。
            # 两者回同一句文案，用户会以为照片被删了，而正确的动作是
            # 去磁盘上把它搬到别的目录。这里点名那个目录段。
            seg = ""
            if row is not None:
                _root = st.cfg.root_by_id(row["root_id"])
                if _root is not None:
                    seg = sandbox.excluded_dir_segment(str(row["rel_path"]), _root)
            # ★ 命中排除目录时用专属码 E1019（与 rename 同源）。
            # `message` 不是给用户看的——`moveCurrent()` 把它放进
            # ApiError，前端仍然优先显示 `user_hint`；但代码也不能
            # 写 E1004，否则客户端无法把"被排除目录"与"真的没了"区开。
            failed.append({
                "id": mid,
                "code": "E1019" if seg else "E1004",
                "message": (
                    f"照片位于被排除的目录「{seg}」，请在电脑上把它移到别的目录"
                    if seg else "照片不存在"
                ),
            })
            continue

        root = st.cfg.root_by_id(row["root_id"])
        if root is None:
            failed.append({"id": mid, "code": "E1004", "message": "照片所在目录已移除"})
            continue
        if not root.can_write:
            raise ReadonlyError("当前为只读模式，无法移动")

        try:
            if root.id not in resolved_dirs:
                # 空串表示库根 —— 沙箱要求 rel_path 非空，所以根这一档
                # 不调 sandbox.resolve（它本来就是可信的根配置）。
                if target_rel:
                    sandbox.resolve(root, target_rel, "write", cfg=st.cfg)
                    # ★ 审计 F14：`resolve()` 只保证"没越出库根"，它不知道
                    # `data` / `node_modules` / `#recycle` / `.photobridge_tmp`
                    # 这些目录名在本库的语义里**不该放照片**（索引器会跳过，
                    # 于是下一次扫描把照片标成 missing，而 missing 行的
                    # move/rename 一律 E1004 —— 照片从界面上消失且无法用
                    # API 移回）。这一步必须在写之前，且整批 400：
                    # `target_dir` 是整批共用的参数，不是逐项冲突。
                    #
                    # ⚠️ `target_rel` 是**目录**，不是文件，必须传
                    # `last_is_file=False`：默认值 True 会把最后一段当成文件名跳过，
                    # 于是 `target_dir="data"`（只有一段）判出来是空列表，整条防线形同虚设
                    # —— 而"移到根目录下的 data 子目录"正是用户最容易误选的目标。
                    sandbox.reject_excluded(target_rel, root, last_is_file=False)
                resolved_dirs[root.id] = target_rel
            rel_dir = resolved_dirs[root.id]

            src_rel = row["rel_path"]
            src_dir, name = _split_dir_name(src_rel)
            if rel_dir == src_dir:
                # 目标就是当前目录 —— 不是错误，但没有任何事要做。
                # 静默成功比报错好：手机端"全选后移动到当前相册"是常见误操作。
                skipped.append(mid)
                continue

            dst_rel = f"{rel_dir}/{name}" if rel_dir else name
            dst = sandbox.resolve(root, dst_rel, "write", cfg=st.cfg)

            # 目标目录不存在则创建（mkdir -p 语义，见接口文档）
            if not dst.parent.exists():
                await loop.run_in_executor(
                    None, lambda p=dst.parent: p.mkdir(parents=True, exist_ok=True)
                )
                # 建完重新解析：新建目录也可能是别人刚放进去的 junction
                dst = sandbox.resolve(root, dst_rel, "write", cfg=st.cfg)

            if dst.exists():
                if on_conflict == "skip":
                    skipped.append(mid)
                    continue
                if on_conflict == "fail":
                    failed.append({"id": mid, "code": "E3012",
                                   "message": "目标目录已有同名文件"})
                    continue
                # rename：找一个**库和磁盘都没有**的名字。
                #
                # 只看库不够：用户手动往库里拖了一张照片而扫描还没跑到时，
                # 库里查不到它，但磁盘上有 —— 此时若只按库里的名字去重，
                # 会把它覆盖掉。
                # 只看磁盘也不够：库里有行但文件刚好被回收站拿走的窗口期，
                # 磁盘探测看不到，而那条记录马上会变成"同名"。
                #
                # 所以候选名必须同时通过两道检查，逐号往上试。
                taken = {
                    r["file_name"]
                    for r in db.query(
                        "SELECT file_name FROM media"
                        " WHERE root_id = ? AND rel_path LIKE ? AND missing = 0",
                        (root.id, f"{rel_dir}/%") if rel_dir else "%",
                    )
                }
                picked = _pick_free_name(root, rel_dir, name, taken, cfg=st.cfg)
                if picked is None:
                    failed.append({"id": mid, "code": "E3012",
                                   "message": "目标目录同名文件过多"})
                    continue
                name, dst_rel, dst = picked
                renamed = True
            else:
                renamed = False

            src = sandbox.resolve(root, src_rel, "write", cfg=st.cfg)
            if not src.exists():
                failed.append({"id": mid, "code": "E1004", "message": "文件已不在磁盘上"})
                continue

            await loop.run_in_executor(
                None, lambda s=src, d=dst, n=row["size"]: _move_one_sync(s, d, size=n)
            )
        except (SandboxError, ReadonlyError):
            raise
        except AppError as exc:
            failed.append({"id": mid, "code": exc.code, "message": exc.spec.message})
            continue
        except Exception as exc:  # noqa: BLE001 —— 一项失败不该中断整批
            log.warning("move failed media_id=%s: %s", mid, exc)
            failed.append({"id": mid, "code": "E3006", "message": "移动失败"})
            continue

        # 先搬文件再改库（与 _trash_one 同源的理由）：反过来如果进程在
        # 中间被杀，库里指向新路径而文件还在旧路径，下次扫描会把这条
        # 标 missing，用户以为照片丢了。
        #
        # ★ 但"先搬文件"也有它自己的坏结局：改库失败时文件已经搬走了。
        #   所以这里必须能被撤销 —— 撞上墓碑行时 `_update_rel_path`
        #   会先清墓碑再重试；真撞上活行才抛，此时把文件搬回原处。
        #   见审计 F4。
        try:
            _update_rel_path(db, mid, dst_rel, name)
        except Exception as exc:  # noqa: BLE001 —— 一项失败不该中断整批
            log.error("移动后写库失败，回滚文件 media_id=%s: %s", mid, exc)
            await loop.run_in_executor(
                None,
                lambda s=src, d=dst: _move_one_sync(d, s, size=row["size"]),
            )
            failed.append({"id": mid, "code": "E3012",
                           "message": "目标位置已有同名记录，移动已撤销"})
            continue
        moved.append({
            "id": mid,
            "from": src_rel,
            "to": dst_rel,
            "renamed": renamed,
        })
        audit.record(
            db, st.cfg, audit.ACTION_MOVE,
            rel_path=dst_rel, device=st.device_name,
            detail={"media_id": mid, "from": src_rel, "to": dst_rel,
                    "renamed": renamed, "root": row["root_id"]},
        )

    first_dir = next(iter(resolved_dirs.values()), "")
    return json_reply({
        "moved": moved,
        "skipped": skipped,
        "failed": failed,
        "target_dir": first_dir,
    })


async def rename_item(ctx: Ctx) -> Reply:
    """`POST /api/items/rename` —— 重命名单个文件。

    ## 为什么扩展名要单独校验

    新名字必须**仍在图片白名单里**。不校验的话，`new_name="x.exe"` 会把
    一张照片变成可执行文件（用户在资源管理器里双击就运行），
    而且它会立刻从时间线消失（索引器按扩展名过滤）。

    ## 为什么 taken_at 可能要重算

    拍摄时间有三个来源（EXIF → 文件名 → mtime）。若原本是
    **文件名**来源，改名后那个日期就不存在了 —— 此时必须按同一套
    顺序重算（走 `exif.resolve_taken_at`），否则一张本来有
    `DateTimeOriginal` 的照片会掉到 mtime 那个时刻，在时间线上跑到
    错误的位置。
    """
    st = _st(ctx)
    db = st.db

    # ★ 与 `move_items` 同一把开关。改名和移动都是"改变照片的名字/位置"，
    # 分成两个键的话用户的心理模型对不上 —— 他只有一个
    # "允许改动我的照片库 / 不允许"的开关。
    if not bool(st.cfg.get("auth.allow_move", True)):
        raise AppError(E1015, "改名功能未在电脑端开启")

    body = await ctx.json()
    mid = body.get("id")
    try:
        mid = int(mid)
    except (TypeError, ValueError):
        raise BadRequest("id 必须是整数") from None

    new_name = body.get("new_name")
    if not isinstance(new_name, str) or not new_name.strip():
        raise BadRequest("new_name 必填")
    new_name = new_name.strip()
    if len(new_name) > 255:
        raise BadRequest("文件名过长（上限 255）")

    row = db.query_one(
        "SELECT id, root_id, rel_path, file_name, size, taken_at, taken_src,"
        "       missing"
        "  FROM media WHERE id = ?",
        (mid,),
    )
    if row is None or row["missing"]:
        # 与 move_items 同源（审计 F14 第 3 条）：如果这条行的目录已经落进
        # 扫描排除范围，说清真正的原因 —— 文件还在，只是索引器不再认它。
        seg = ""
        if row is not None:
            _root = st.cfg.root_by_id(row["root_id"])
            if _root is not None:
                seg = sandbox.excluded_dir_segment(str(row["rel_path"]), _root)
        if seg:
            # ★ 用专属码 E1019 而不是 NotFound：前端弹的是
            # `user_hint`（`ViewerView.vue:243`、`client.ts:66`），而 E1004 的
            # 提示是"内容不存在或已被删除" —— 用户会以为照片被删了。
            # 不详细写这一条，下面那句话就只能在日志里被看到。
            raise AppError(
                E1019,
                f"照片位于被排除的目录「{seg}」，请在电脑上把它移到别的目录",
            )
        raise NotFound("照片不存在")

    root = st.cfg.root_by_id(row["root_id"])
    if root is None:
        raise NotFound("照片所在目录已移除")
    if not root.can_write:
        raise ReadonlyError("当前为只读模式，无法改名")

    # 名字里不能有路径分隔符 —— 否则"改名"会变成"移动"，而移动是另一个
    # 接口（它有自己的权限与冲突语义）。这一条必须在沙箱之前拦，
    # 因为沙箱对 `a/b.jpg` 是合法的（它就是为路径设计的）。
    if "/" in new_name or "\\" in new_name:
        raise BadRequest("new_name 不能包含路径分隔符")

    stem, dot, ext = new_name.rpartition(".")
    if not dot or not stem:
        raise BadRequest("new_name 必须包含扩展名")
    if ext.lower().lstrip(".") not in ALLOWED_IMAGE_EXTS:
        raise BadRequest("new_name 的扩展名不在支持的图片格式内")

    src_rel = row["rel_path"]
    src_dir, old_name = _split_dir_name(src_rel)
    if new_name == old_name:
        # 名字没变：幂等成功，不碰文件系统也不写审计（否则审计日志
        # 会被"点了没反应"的操作刷满）。
        return json_reply({
            "id": mid, "from": src_rel, "to": src_rel,
            "taken_at": float(row["taken_at"]), "taken_src": row["taken_src"],
        })

    dst_rel = f"{src_dir}/{new_name}" if src_dir else new_name
    # 沙箱会一并拒绝 Windows 保留名（CON/PRN/…）、非法字符、结尾空格或点
    dst = sandbox.resolve(root, dst_rel, "write", cfg=st.cfg)
    src = sandbox.resolve(root, src_rel, "write", cfg=st.cfg)

    if dst.exists():
        # spec 必须显式给 E3012。Conflict 的默认 spec 是 E1012，它的
        # user_hint 是「上传已结束，请重新开始」—— 对改名冲突完全不对，
        # 手机端会把这句话原样显示给用户。
        raise Conflict("同目录下已有同名文件", spec=E3012)
    if not src.exists():
        raise NotFound("文件已不在磁盘上")

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(
        None, lambda: _move_one_sync(src, dst, size=row["size"])
    )

    taken_at = float(row["taken_at"])
    taken_src = row["taken_src"]
    if taken_src == "filename":
        # 文件名日期没了 → 按索引器同一套顺序重算（EXIF → 文件名 → mtime）。
        # **必须走 resolve_taken_at 而不是自己拼**：EXIF 里可能本来就有
        # DateTimeOriginal，只是当初文件名也解析出了日期才没轮到它 ——
        # 直接退回 mtime 会让一张有 EXIF 的照片掉到时间线错误的位置。
        from ..exif import extract_sync, resolve_taken_at  # 局部导入避免循环

        try:
            st_mtime = dst.stat().st_mtime
        except OSError:
            st_mtime = float(row["taken_at"])
        info = await loop.run_in_executor(None, extract_sync, dst)
        taken_at, taken_src = resolve_taken_at(info, new_name, st_mtime)

    try:
        _update_rel_path(
            db, mid, dst_rel, new_name,
            taken=(taken_at, taken_src),
        )
    except Exception as exc:  # noqa: BLE001
        # 与 move_items 同理：文件已经改名了，写库失败必须把名字改回去，
        # 否则库里指向一个不存在的路径，照片在时间线上"消失"。
        log.error("改名后写库失败，回滚文件名 media_id=%s: %s", mid, exc)
        await loop.run_in_executor(
            None, lambda: _move_one_sync(dst, src, size=row["size"])
        )
        raise Conflict("同目录下已有同名记录，改名已撤销", spec=E3012) from exc
    audit.record(
        db, st.cfg, audit.ACTION_RENAME,
        rel_path=dst_rel, device=st.device_name,
        detail={"media_id": mid, "from": src_rel, "to": dst_rel,
                "root": row["root_id"]},
    )
    return json_reply({
        "id": mid,
        "from": src_rel,
        "to": dst_rel,
        "taken_at": taken_at,
        "taken_src": taken_src,
    })


# --------------------------------------------------------------------------
# 重扫
# --------------------------------------------------------------------------

async def rescan(ctx: Ctx) -> Reply:
    """`POST /api/index/rescan` —— 触发全量重扫。

    返回 202 而不是等扫描结束：真实库首轮约 20 秒，
    在云端隧道下等待这么久必然触发 60 秒超时，
    而且用户本来就应该在"设置"页看到进度条而不是转圈。
    """
    st = _st(ctx)
    indexer = st.indexer
    if indexer is None:  # pragma: no cover
        raise AppError(E3004, "索引器未启动")

    if getattr(indexer, "running", False):
        raise AppError(E3012, "已有扫描在进行中")

    body = await ctx.json()
    root_id = common.root_id_payload(body, ctx)

    loop = asyncio.get_running_loop()
    if root_id:
        if not st.cfg.has_root(root_id):
            raise AppError(E1001, f"未知的 root：{root_id}")
        loop.create_task(_run_scan_root(st, root_id))
    else:
        loop.create_task(_run_scan_all(st))
    audit.record(
        st.db, st.cfg, audit.ACTION_RESCAN,
        rel_path=root_id or "*", device=st.device_name,
        detail={"root": root_id or "*"},
    )

    return json_reply({"started": True, "root": root_id or None,
                       "ts": time.time()}, status=202)


async def _run_scan_all(st: AppState) -> None:
    try:
        await st.indexer.scan_all()
    except Exception:  # noqa: BLE001
        log.exception("rescan 失败")


async def _run_scan_root(st: AppState, root_id: str) -> None:
    try:
        root = st.cfg.root_by_id(root_id)
        if root is not None:
            await st.indexer.scan_root(root)
    except Exception:  # noqa: BLE001
        log.exception("rescan 失败 root=%s", root_id)
