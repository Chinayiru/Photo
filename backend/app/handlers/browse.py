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
import time
from pathlib import Path
from typing import Any

from .. import sandbox
from ..core import Ctx, Reply, empty_reply, file_reply, json_reply, stream_reply
from ..core.cursor import decode_cursor, encode_cursor
from ..errors import (
    E1001,
    E1004,
    E1008,
    E1015,
    E3004,
    E3012,
    AppError,
    BadRequest,
    NotFound,
    ReadonlyError,
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

    root_id = ctx.q("root")
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
    mid = ctx.param_int("id")

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

    # 条件请求 → 304。省下的是**隧道带宽**，弱网下价值最大。
    if etag_matches(ctx.header("if-none-match"), sig):
        r = empty_reply(304)
        r.set_etag(sig)
        r.set("Vary", "Authorization")
        return r

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

    if not dst.exists():
        # ★ 不返回 404/500，而是返回灰色占位图 + 200。
        # 时间线上一个破图会让用户以为"照片丢了"，而实际只是
        # 缩略图还没生成出来（后台队列在忙）。前端看到 200 的占位图
        # 会正常渲染，用户稍后刷新即可。
        data = placeholder_webp(size or PLACEHOLDER_SIZE)
        r = Reply(status=200, body=data, media_type="image/webp")
        r.headers["Content-Type"] = "image/webp"
        r.headers["Content-Length"] = str(len(data))
        r.set_cache(60, private=True)      # 占位图只缓存 1 分钟，别把真的挡住
        r.set("Vary", "Authorization")
        return r

    return await _file_thumb_reply(st, dst, sig, ctx)


async def _generate_sync(st: AppState, row: Any, dst: Path, size: int) -> None:
    """没有 ThumbEngine 时的兜底生成（测试与轻量嵌入场景）。"""
    from ..thumbs import generate_thumb

    src = sandbox.resolve(st.cfg.root_by_id(row["root_id"]), row["rel_path"],
                          "read", cfg=st.cfg)
    if not src.exists():
        return
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _call_generate, str(src), dst, size,
                              row["orientation"])
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
    return await _serve_thumb(_st(ctx), ctx, ctx.param_int("id"))


async def get_preview(ctx: Ctx) -> Reply:
    """`GET /api/items/{id}/preview?size=1600` —— 查看器用的中等尺寸。

    预览图**按需生成但不写回 DB 的 thumb_state**：1600 是"用户点开了这张"
    的信号，不是批量后台任务；把它计入 thumb_state 会让重扫时的
    "待生成"数量与用户预期不符。
    """
    st = _st(ctx)
    db = st.db
    mid = ctx.param_int("id")
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

    if etag_matches(ctx.header("if-none-match"), sig):
        r = empty_reply(304)
        r.set_etag(sig)
        r.set("Vary", "Authorization")
        return r

    if not dst.exists() and not row["missing"]:
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
        if row["missing"]:
            raise NotFound(f"源文件已丢失 media_id={mid}")
        data = placeholder_webp(size)
        r = Reply(status=200, body=data, media_type="image/webp")
        r.headers["Content-Type"] = "image/webp"
        r.headers["Content-Length"] = str(len(data))
        r.set_cache(60, private=True)
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
    mid = ctx.param_int("id")

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

    root_id = ctx.q("root") or st.cfg.default_root_id
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
    root_id = ctx.q("root") or None
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
        where += " AND file_name LIKE ? ESCAPE '\\'"
        params.append("%" + _esc(q) + "%")

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
    _audit(st, "delete", row["rel_path"], {"media_id": mid, "root": row["root_id"]})
    return True


def _audit(st: AppState, action: str, rel_path: str, detail: dict[str, Any]) -> None:
    """写审计日志。

    文件是主记录（追加写，即使进程被杀也已落盘），数据库是索引。
    写表失败只记日志不报错 —— 为了写审计而让用户的删除操作失败是荒谬的。
    """
    import json

    line = "\t".join([
        time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        action,
        str(st.device_name),
        rel_path,
        json.dumps(detail, ensure_ascii=False),
    ]) + "\n"
    try:
        path = st.cfg.audit_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        log.warning("写审计日志失败 action=%s", action, exc_info=True)


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
    root_id = body.get("root") or ctx.q("root")

    loop = asyncio.get_running_loop()
    if root_id:
        if not st.cfg.has_root(root_id):
            raise AppError(E1001, f"未知的 root：{root_id}")
        loop.create_task(_run_scan_root(st, root_id))
    else:
        loop.create_task(_run_scan_all(st))
    _audit(st, "rescan", root_id or "*", {"root": root_id or "*"})

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
