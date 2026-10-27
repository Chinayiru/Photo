"""处理函数共享的 SQL 片段、行转换与文件流。

## 为什么单列一个模块

`browse`（时间线/查看）、`search`、`uploads`（上传后回id）都要把 media 行
转成 TimelineItem，也都要按 `taken_at DESC, id DESC` 排序。各处复制一份
SQL 列表的话，加一个字段就要改四个地方，而漏改的那个接口会静默少字段
（前端 `item.thumb_v` 变 undefined，缓存就永远命中不了）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

from .. import video as videomod
from ..thumbs import source_signature

log = logging.getLogger(__name__)

def is_guest(ctx: Any) -> bool:
    """这次请求是不是游客发起的（判据是令牌里的角色，不是有没有 cookie）。

    主人自己点开朋友的邀请链接时，浏览器里两种凭证可能同时存在；
    此时应以**本次请求实际使用的那枚令牌**为准 —— 中间件把验过的角色
    放在 `ctx.state["client"]["role"]`，这里只读它。
    """
    client = (getattr(ctx, "state", None) or {}).get("client") or {}
    return client.get("role") == "guest"


def visible_root_ids(ctx: Any, st: Any) -> list[str]:
    """本请求能看到的根 id 集合（主人/游客隔离的**唯一**事实来源）。

    主人：`enabled_roots`（停用的根看不见 —— 审计 F01）。
    游客：`guest_root_ids`（只有一个，见 `Config._check_guest_roots`）。
    """
    client = ctx.state.get("client") or {}
    if client.get("guest") or client.get("role") == "guest":
        return list(st.cfg.guest_root_ids)
    return [r.id for r in st.cfg.enabled_roots]


def check_visible_root(ctx: Any, st: Any, root_id: str) -> None:
    """点名一个本身份看不见的根 → 当"不存在"处理（404）。

    为什么是 404 而不是 403：403 等于告诉对方"这个根存在，只是你没权限"，
    而根 id（`library` / `guest`）是可以猜的短字符串。游客一次猜测就能
    确认主人配了哪些库，接着只需要猜 id 就能枚举 —— 这条边界必须与
    "配置里根本没有这个根"表现完全一致。
    """
    rid = (root_id or "").strip()
    if not rid:
        return
    if rid in visible_root_ids(ctx, st):
        return
    from ..errors import NotFound

    raise NotFound(None)


def guest_uploader_id(ctx: Any) -> str:
    """游客自己的 client_id；主人返回空串。

    ★ 审计 07f03d S-04：游客根是**共享**的（大家的照片都落在
    `游客上传/` 下），所以「看得见这个根」不等于「看得见这些照片」。
    归属的正确事实来源是 `media.uploader_client_id`（上传完成时写入），
    不是请求参数 —— 后者等于让调用方自己声明「我是谁」。
    """
    if not is_guest(ctx):
        return ""
    client = ctx.state.get("client") or {}
    return str(client.get("id") or "")


def check_media_visible(ctx: Any, st: Any, row: Any) -> None:
    """这条 media 能不能给本请求看：先看根，再（游客）看上传者。

    两处都用 404：`/api/items/{id}/raw` 返回 403 就等于确认「这个 id 存在」，
    游客拿 1…9999 扫一遍就能把主人的 id 空间探出来。
    """
    check_visible_root(ctx, st, row["root_id"])
    if not is_guest(ctx):
        return
    mine = guest_uploader_id(ctx)
    if (row["uploader_client_id"] or "") != mine:
        from ..errors import NotFound

        raise NotFound(None)


__all__ = [
    "TIMELINE_COLUMNS",
    "DETAIL_COLUMNS",
    "CHUNK_SIZE",
    "row_to_item",
    "row_to_detail",
    "timeline_where",
    "timeline_order",
    "iter_file",
    "stat_size",
    "thumb_version",
    "download_filename",
    "thumb_etag",
    "normalize_dir",
    "parse_ts",
    "is_guest",
    "visible_root_ids",
    "check_visible_root",
    "guest_uploader_id",
    "check_media_visible",
    "parse_size",
    "etag_matches",
    "root_id_param",
    "root_id_payload",
]

#: 时间线轻量项需要的列。
# 刻意**不查** exif 的十余个字段：一页 100 条时这能省掉约 60% 的字节，
# 且避免了"用户只是滑动列表却触发了 100 次 EXIF 反序列化"。
TIMELINE_COLUMNS = (
    "id, root_id, rel_path, file_name, ext, kind, size, mtime, taken_at, taken_src,"
    " width, height, duration, thumb_state, thumb_etag, missing,"
    " (gps_lat IS NOT NULL) AS has_gps"
)

#: 读取文件的分块大小。256KB 是权衡：8MB 分片会有 32 次系统调用，
# 而 4KB 会让 2GB 文件产生 50 万次 await，事件循环开销超过 IO 本身。
CHUNK_SIZE = 256 * 1024


def normalize_dir(rel_path: str) -> str:
    """取所在目录（不含文件名）。根目录返回空串。

    统一用 '/' 分隔：rel_path 入库时已归一化，
    但返回给前端前再保一道，避免 Windows 上冒出反斜杠让前端拼 URL 出错。
    """
    p = rel_path.replace("\\", "/")
    return p.rsplit("/", 1)[0] if "/" in p else ""


def row_to_item(row: Any) -> dict[str, Any]:
    """media 行 → TimelineItem（约 120 字节）。"""
    state = row["thumb_state"]
    return {
        "id": int(row["id"]),
        "name": row["file_name"],
        "kind": row["kind"],
        "duration": row["duration"],
        "taken_at": float(row["taken_at"]),
        "taken_src": row["taken_src"],
        "w": row["width"],
        "h": row["height"],
        # ★ thumb_v 恒有值，**不**按 thumb_state 分档。
        #
        # 它是"这张图的源文件版本"（mtime-size 签名），不是"缩略图生成好了没"。
        # 早期版本写成 `if state == "ok" else None`，理由是"pending 时给值会让
        # 浏览器把稍后才生成的图缓存一年"—— 这个理由站不住：能不能长期缓存
        # 由**服务端的响应头**决定（占位图发 `max-age=60`，真图才发 immutable），
        # 与 URL 里有没有 `v` 无关。
        #
        # 而那个写法会造成**死锁**：
        #   前端 `showImage = thumb_state === "ok" && !!thumb_v`，pending 的项
        #   连 `<img>` 都不渲染 → 永远不会请求缩略图 URL → 而服务端恰恰是靠
        #   "被请求"来按需生成的（`browse._serve_thumb` → `engine.ensure`）。
        #   结果：后台队列被丢弃的那些项（`enqueue` 队列满时**刻意**丢弃，
        #   不阻塞索引器）永远停在 pending，前端永远不请求 → 永久灰块。
        #   唯一的出路是重扫，而重扫又会再次丢队列。
        #
        # 恒给值之后，pending 项也会发请求，服务端当场同步生成并返回真图
        # （`_serve_thumb` 里引擎没生成出来时会退回 `_generate_sync`）。
        "thumb_v": thumb_etag(320, row),
        "size": int(row["size"]),
        "thumb_state": state,
        "has_gps": bool(row["has_gps"]),
        "root": row["root_id"],
        "dir": normalize_dir(row["rel_path"]),
    }


def thumb_version(row: Any) -> str | None:
    v = row["thumb_etag"]
    return str(v) if v else None


#: 详情页需要完整 EXIF，用 SELECT * 之外显式列出，避免以后加列时
#: 把大字段（如未来可能的 embedding）无意带进响应。
DETAIL_COLUMNS = "*"


def row_to_detail(row: Any, *, prev_id: int | None = None,
                  next_id: int | None = None) -> dict[str, Any]:
    """media 行 → 完整项（约 400 字节）。

    `missing=1` 时**仍然返回 200**：前端要显示"这张照片找不到了"的占位卡片，
    而 404 无法区分"记录不存在"与"记录在但文件被移动了"——
    后者用户手动找回文件即可恢复，前者才需要删记录。
    """
    lat, lon = row["gps_lat"], row["gps_lon"]
    taken = float(row["taken_at"])
    return {
        "id": int(row["id"]),
        "root": row["root_id"],
        "rel_path": row["rel_path"],
        "name": row["file_name"],
        "ext": row["ext"],
        "kind": row["kind"],
        "size": int(row["size"]),
        "mtime": float(row["mtime"]),
        "sha256": row["sha256"],
        "taken_at": taken,
        "taken_at_iso": _iso(taken),
        "taken_src": row["taken_src"],
        "width": row["width"],
        "height": row["height"],
        "orientation": row["orientation"],
        "video": _video_block(row),
        "exif": {
            "camera": row["camera"],
            "lens": row["lens"],
            "iso": row["iso"],
            "fnum": row["fnum"],
            "exposure": row["exposure"],
            "focal": row["focal"],
        },
        "gps": ({"lat": lat, "lon": lon} if lat is not None and lon is not None
                else None),
        "thumb_state": row["thumb_state"],
        # 与 row_to_item 同理：恒给值，否则 pending 的项在前端根本不发请求，
        # 服务端就永远没机会按需生成（死锁）。详见 row_to_item 的注释。
        "thumb_v": thumb_etag(320, row),
        "missing": bool(row["missing"]),
        "indexed_at": row["indexed_at"],
        "urls": {
            "thumb": f"/api/items/{int(row['id'])}/thumb?size=320",
            "preview": f"/api/items/{int(row['id'])}/preview",
            "raw": f"/api/items/{int(row['id'])}/raw",
            # 视频播放走 `/stream`：与 `/raw` 同一份实现（Range/206 全支持），
            # 但语义分开 —— 前端拿到这个 URL 才用 `<video>` 而不是 `<img>`。
            "video": (f"/api/items/{int(row['id'])}/stream"
                      if row["kind"] == "video" else None),
        },
        "prev_id": prev_id,
        "next_id": next_id,
    }


def _video_block(row: Any) -> dict[str, Any] | None:
    """视频的播放元信息；图片返回 `None`。

    `playable` 由**服务端**判定而不是让前端自己看编码：能否在浏览器里播
    取决于容器 + 视频编码 + 音频编码三者的组合（见 `video.is_playable`），
    前端多写一份判断必然漂移 —— 判断分歧的表现是"点了播放，一直转圈"。
    """
    if videomod.row_kind(row) != "video":
        return None
    try:
        container = row["container"] or ""
        vcodec = row["vcodec"] or ""
        acodec = row["acodec"] or ""
    except (IndexError, KeyError):
        # 老调用点只 SELECT 了部分列（如维护脚本）—— 不是错误，只是没信息
        return None
    try:
        faststart = row["faststart"]
    except (IndexError, KeyError):
        faststart = None

    hint = videomod.playback_hint(container=container, vcodec=vcodec,
                                  acodec=acodec)
    return {
        "duration": row["duration"],
        "container": container,
        "vcodec": vcodec,
        "acodec": acodec,
        # `playable` 是"Chrome 桌面确定能播"；`hint` 才是前端的行动依据：
        # HEVC 在 iOS Safari 能播、在 Windows Chrome 多半不能 —— 只给布尔值
        # 会让主客户端（手机）白白退回"下载原片"。
        "playable": videomod.is_playable(container=container, vcodec=vcodec,
                                         acodec=acodec),
        "hint": hint,
        # 为什么播不了/可能播不了 —— 文案由服务端出，前端照抄（判据和文案
        # 待在一起才不会互相矛盾）。
        "reason": videomod.playback_reason(container=container, vcodec=vcodec,
                                           acodec=acodec) if hint != "ok" else "",
        # moov 在 mdat 之前吗（None=判断不了）。False 表示未做 faststart：
        # 播放器必须先读到文件尾才起播，跨网络时就是长时间转圈。
        "faststart": None if faststart is None else bool(faststart),
    }


def _iso(ts: float) -> str:
    """Unix 秒 → 带本地时区偏移的 ISO8601。

    ⚠️ 必须带偏移量。只写 `2024-05-12T18:30:00` 的话，
    手机按自己的时区（或 UTC）解释，用户会看到照片"提前/推后 8 小时"，
    这是照片库最常被抱怨的 bug。JSON 体里的时间戳保持 Unix 秒不变，
    这个字段只是给人看与给 <time> 标签用的。
    """
    import datetime as _dt

    try:
        d = _dt.datetime.fromtimestamp(ts).astimezone()
        return d.isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return ""


def thumb_etag(size: int, row: Any) -> str:
    """缩略图的强 ETag。

    ⚠️ 用**源文件签名**（mtime-size）而不是 media id：
    用户手工替换同名文件后 id 不变，用 id 做 ETag 会让手机永远看旧图。

    ⚠️ 也不能用 DB 里的 `thumb_etag` 列：那一列只在**生成完成后**才写入，
    而用户往往是在"第一次请求"时才触发生成 —— 首次响应会带着
    `w320-0` 这样的 ETag，生成完变成 `w320-1715...`，
    手机把旧 ETag 发回来永远命不中 304，每次都要重下整张图。
    直接从 mtime/size 算，第一次和后续请求就都一致。

    ⚠️ 精度必须是**毫秒**，且必须与 `thumbs.source_signature()` 完全一致。
    两处精度不同（一个 int(mtime)、一个 int(mtime*1000)）会让
    browse 里的陈旧判定 `stored != source_signature(...)` **永远为真**，
    于是每次请求都重新生成缩略图 —— 索引器刚跑完 4915 张，
    用户一翻页又全量重生成，CPU 打满、手机端加载变慢。

    也没有用缩略图字节的 sha256（接口文档提过这一形式）——
    那要求每次请求都把整张缩略图读进来算哈希，20KB×100 张就是 2MB 的无谓读，
    而源签名同样是"内容变了才变"，效果等价、代价低两个数量级。
    """
    return f"w{size}-{source_signature(row['mtime'], row['size'])}"


def timeline_order(order: str | None) -> str:
    """校验排序参数并返回 SQL 片段。

    ⚠️ 一期只实现 desc。传 asc 必须**报 400**而不是静默按 desc 处理：
    前端若依赖升序做"跳到最早"却拿到倒数第一页，是最难排查的一类错。
    """
    from ..errors import E1001, AppError

    if order is None or order == "" or order == "desc":
        return "ORDER BY taken_at DESC, id DESC"
    raise AppError(E1001, f"不支持的 order：{order}（一期仅支持 desc）")


def timeline_where(*, root_id: str | None = None, ts_from: float | None = None,
                   ts_to: float | None = None, kind: str | None = None,
                   dir_prefix: str | None = None, camera: str | None = None,
                   has_gps: bool | None = None,
                   allow_video: bool = True,
                   enabled_root_ids: Sequence[str] | None = None,
                   uploader_client_id: str | None = None,
                   ) -> tuple[str, list[Any]]:
    """构造 WHERE 子句 + 参数。

    所有调用方共用它，是为了保证 `missing = 0` 与 `kind` 这两条判断
    永远不会被漏掉 —— 漏掉 missing 就会把"文件已被删除"的照片显示出来，
    点开才报错。

    `kind` 语义（docs/04 §5.2.1）：`None`/`"all"` = 图片 + 视频；`"image"` /
    `"video"` = 只看一种；其他值 = 空结果（不是报错，前端标签页自然空态）。
    `allow_video=False`（视频功能关闭时）会把视频整类排除 —— 让用户
    "看不见"比让他看见一排点了播不了的条目要好。

    ★ 审计 F01：`enabled_root_ids` 是"这块共享还存在吗"的唯一事实来源。
    传了它就只在这个集合里查；传空列表（所有根都停了）直接 `AND 0`。
    `None` 表示"调用方不按根过滤"（只有电脑端的维护/统计代码会这么用，
    对手机暴露的接口一律必须传）。**只靠 `?root=` 过滤是不够的**：不传
    `root` 时就是查全库，禁用根的照片仍然会出现在时间线和搜索结果里。
    """
    sql = " WHERE missing = 0"
    params: list[Any] = []

    if kind == "image":
        sql += " AND kind = 'image'"
    elif kind == "video":
        sql += " AND kind = 'video'" if allow_video else " AND 0"
    elif kind is None or kind == "all":
        # 混排：图片 + 视频（视频关闭时只剩图片）
        sql += "" if allow_video else " AND kind = 'image'"
    else:
        # 未知的 kind 值：空结果，让"只看某类"的标签页自然显示空态
        sql += " AND 0"

    if enabled_root_ids is not None:
        ids = [str(r) for r in enabled_root_ids]
        if not ids:
            # 一个根都没启用：返回空集，而不是拼出 `IN ()` 这种非法 SQL。
            sql += " AND 0"
        else:
            sql += f" AND root_id IN ({','.join('?' * len(ids))})"
            params.extend(ids)
    if root_id is not None:
        sql += " AND root_id = ?"
        params.append(root_id)
    if uploader_client_id is not None:
        # ★ 审计 07f03d S-04：游客只看自己传的。这个条件**不能**由调用方
        #   从请求参数里取，必须来自已验证身份（中间件写进 ctx.state）。
        sql += " AND uploader_client_id = ?"
        params.append(uploader_client_id)
    if ts_from is not None:
        sql += " AND taken_at >= ?"
        params.append(ts_from)
    if ts_to is not None:
        # `to` 是**不含**端点（接口文档 §5.2.1），用 < 而非 <=。
        sql += " AND taken_at < ?"
        params.append(ts_to)
    if camera is not None:
        sql += " AND camera = ?"
        params.append(camera)
    if has_gps is not None:
        sql += " AND gps_lat IS NOT NULL" if has_gps else " AND gps_lat IS NULL"
    if dir_prefix:
        # 前缀匹配必须带 '/' 或精确相等，否则 "2024" 会匹配到 "20240/..."。
        d = dir_prefix.replace("\\", "/").strip("/")
        if d:
            sql += " AND (rel_path = ? OR rel_path LIKE ? ESCAPE '\\')"
            params.append(d)
            params.append(_like_escape(d) + "/%")

    return sql, params


def _like_escape(s: str) -> str:
    """转义 LIKE 的通配符。

    不转义的话，目录名里带 `_`（如 `2024_备份`）会匹配任意字符，
    用户搜索一个目录却看到别的目录的照片。
    """
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_contains(s: str) -> str:
    return "%" + _like_escape(s) + "%"


# --------------------------------------------------------------------------
# 文件流
# --------------------------------------------------------------------------

def _open_rb(path: str) -> Any:
    return open(path, "rb", buffering=0)


def stat_size(path: Path) -> int:
    """取文件真实大小。

    用实际 stat 而不是 DB 里的 size：库里的是**索引时刻**的值，
    文件后来被替换过的话两者不一致，而 Range 用错误的 size 计算
    会切出错位的数据（用户拿到一张打不开的照片）。
    """
    return os.stat(path).st_size


async def iter_file(path: Path, *, start: int = 0, length: int | None = None,
                    chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
    """分块读文件，返回异步生成器。

    `length=None` 表示读到文件末尾。用于 206 时传精确字节数。

    ⚠️ 用 try/finally 保证文件被关闭：客户端中途断开时协程会被取消，
    `GeneratorExit` 在 yield 处抛出，finally 仍会执行。
    少了它，用户滑动浏览几十张照片就会泄漏几十个文件句柄 ——
    Windows 上还会因此无法删除被"占用"的照片。
    """
    loop = asyncio.get_running_loop()
    f = await loop.run_in_executor(None, _open_rb, str(path))
    try:
        if start:
            await loop.run_in_executor(None, f.seek, start)
        remaining = length
        while True:
            if remaining is not None and remaining <= 0:
                return
            n = chunk_size if remaining is None else min(chunk_size, remaining)
            chunk = await loop.run_in_executor(None, f.read, n)
            if not chunk:
                return
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk
    finally:
        try:
            f.close()
        except OSError:  # pragma: no cover
            log.debug("close failed for %s", path)


def download_filename(name: str | None, fallback: str) -> str:
    """清洗下载文件名。

    ⚠️ 必须同时去掉路径分隔符与 CR/LF：CRLF 注入可以伪造响应头，
    而 `..\\` 在 `Content-Disposition` 里可能被某些客户端当作路径。
    """
    raw = (name or fallback or "download").strip()
    cleaned = "".join(c for c in raw if c not in '\\/:*?"<>|\r\n\t')
    cleaned = cleaned.strip().strip(".")
    if not cleaned:
        return "download"
    return cleaned[:180]


def filename_disposition(filename: str, *, attachment: bool) -> str:
    """构造 Content-Disposition。

    `download=1` 必须用 **attachment**：用 inline 的话手机浏览器会直接
    打开图片而不是存到相册，"下载原图"这个功能就完全失效了。

    同时给 `filename=` 与 RFC 5987 的 `filename*=`：前者是 ASCII 兜底，
    后者承载中文名。只留 filename* 时部分安卓浏览器会把文件存成
    无扩展名的 "download"。
    """
    from urllib.parse import quote

    kind = "attachment" if attachment else "inline"
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii").strip() or "image"
    return (f'{kind}; filename="{ascii_fallback}"; '
            f"filename*=UTF-8''{quote(filename)}")


def now() -> float:
    return time.time()


def parse_ts(value: str | None, *, what: str) -> float | None:
    """把 `from`/`to` 参数解析成 Unix 秒。

    接口文档规定是 Unix 时间戳，这里额外容忍 `YYYY-MM-DD` 与
    `YYYY-MM-DDTHH:MM:SS`：网页端用 <input type="date"> 拿到的就是日期串，
    强迫前端换算只会让"按日期筛选"这个功能在时区上反复出错。
    两种都解析不了才报 400。
    """
    if value is None or value == "":
        return None
    s = str(value).strip()
    try:
        return float(s)
    except ValueError:
        pass
    from ..errors import E1001, AppError

    try:
        import datetime as _dt

        if len(s) == 10:
            d = _dt.datetime.strptime(s, "%Y-%m-%d")
        else:
            d = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            return d.timestamp()      # 无时区信息时按本机本地时间解释
        return d.timestamp()
    except ValueError:
        raise AppError(E1001, f"参数 {what} 不是合法的时间（{value!r}）") from None


def parse_size(value: str | None, allowed: tuple[int, ...], *, name: str = "size") -> int:
    """校验缩略图尺寸参数。

    ⚠️ 这里**必须报错**而不是像 limit 那样钳制：尺寸决定了缓存文件名，
    悄悄换成 320 会让前端拿到的图与它请求的尺寸不符，
    表现为"预览图怎么是糊的"。
    """
    from ..errors import E1001, AppError

    if value is None or value == "":
        return allowed[0]
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise AppError(E1001, f"参数 {name} 必须是整数") from None
    if n not in allowed:
        raise AppError(E1001, f"不支持的 {name}={n}，可选：{', '.join(map(str, allowed))}")
    return n


#: 根目录参数的两个合法拼写。**顺序有意**：`root` 是接口文档用的名字，
#: `root_id` 是数据库列名，两者都收，但报错信息里统一说 `root`。
ROOT_PARAM_NAMES: tuple[str, ...] = ("root", "root_id")


def root_id_param(ctx: Any) -> str | None:
    """从 query 里读根目录 id，`root` 与 `root_id` 都认。

    ★ 为什么"两个都认"而不是"只留一个并把另一个判 400"：
    接口文档（docs/04）写的是 `root`，但上传接口一直写的是 `root_id`，
    手机端第二期已经按 `root_id` 发请求了。突然把 `root_id` 判成错误
    会让已配对设备上的上传全部失败 —— 客户端没法在不重新发布的前提下
    改回来。所以这里做**宽松接受 + 冲突报错**：只给一个就用那个，
    给两个且不一致才 400（那种情况客户端一定自己写错了）。
    """
    values = []
    for name in ROOT_PARAM_NAMES:
        raw = ctx.q(name)
        if raw is not None and raw != "":
            values.append((name, raw))
    return _pick_root(values, where="查询参数")


def root_id_payload(body: Any, ctx: Any) -> str | None:
    """从 JSON body 里读根目录 id，body 优先于 query。"""
    values = []
    for name in ROOT_PARAM_NAMES:
        raw = body.get(name) if isinstance(body, dict) else None
        if raw is not None and raw != "":
            values.append((f"body.{name}", raw))
            continue
        raw = ctx.q(name)
        if raw is not None and raw != "":
            values.append((name, raw))
    return _pick_root(values, where="请求参数")


def _pick_root(values: list[tuple[str, Any]], *, where: str) -> str | None:
    if not values:
        return None
    first_name, first = values[0]
    for name, val in values[1:]:
        if str(val) != str(first):
            from ..errors import E1001, AppError

            raise AppError(
                E1001,
                f"{where} {first_name}={first!r} 与 {name}={val!r} 不一致，"
                f"请只传一个（推荐 root）",
            )
    if not isinstance(first, str):
        from ..errors import E1001, AppError

        raise AppError(E1001, f"{where} {first_name} 必须是字符串")
    return first


def etag_matches(if_none_match: str | None, etag: str) -> bool:
    """判断 `If-None-Match` 是否命中（支持 `*`、`W/` 前缀、逗号列表）。

    RFC 7232 的弱比较要求忽略 `W/` 前缀：漏掉这一点会让手机端
    发 `W/"abc"` 时永远命不中，等于没做条件请求 —— 弱网下每次都重传整张图。
    """
    if not if_none_match:
        return False
    raw = if_none_match.strip()
    if raw == "*":
        return True
    for part in raw.split(","):
        p = part.strip()
        if p.startswith(("W/", "w/")):
            p = p[2:]
        p = p.strip().strip('"')
        if p == etag:
            return True
    return False
