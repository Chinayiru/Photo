"""按需转码的三个接口（V2 / ISSUE-058）。

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/items/{id}/transcode` | 开始转（幂等：已在跑就返回同一个 job） |
| GET  | `/api/items/{id}/transcode` | 查状态：`queued/running/ready/failed` + 进度 |
| GET  | `/api/items/{id}/transcode/stream?t=<票>` | 播放转码产物（支持 Range） |

## 为什么流要单独一条路径，而不是复用 `/stream`

`/stream` 回的是**源文件**。转码产物是另一个文件、另一种 Content-Type
（恒为 `video/mp4`），而且它可能还不存在（正在转）。混在一条路径里，前端
就得靠 `?transcoded=1` 之类的开关来切语义 —— 那种"一条路径两种东西"最容易
出现缓存串味（CDN/浏览器把转码前后当成同一个 URL 缓存）。

## 票

`<video>` 发不了 `Authorization`，所以播放地址里带 `t=`（见 `streamticket.py`）。
鉴权中间件只放行 `.../stream` 结尾的路径，这里的 `/transcode/stream` 正好
落在同一规则里；票的真伪在下面按 `media_id` 校验。
"""
from __future__ import annotations

from pathlib import Path

from .. import audit, sandbox, streamticket
from .. import transcode as tcmod
from .. import video as videomod
from ..config import Config
from ..core import Ctx, Reply, file_reply, json_reply
from ..errors import (
    E1001,
    E1008,
    E3004,
    E4007,
    AppError,
    Conflict,
    NotFound,
    Unauthorized,
)
from ..state import AppState
from ..transports.base import (
    RangeNotSatisfiable,
    content_range_header,
    parse_range,
    range_length,
    unsatisfied_range_header,
)
from . import common
from .common import iter_file

__all__ = ["start", "status", "stream"]


def _st(ctx: Ctx) -> AppState:
    return ctx.app  # type: ignore[return-value]


def _video_row(ctx: Ctx) -> dict:
    """取出这条 media 行，并确认它确实是视频。

    ★ 转码是**写操作**（要在 data/transcodes 里落文件），所以这里要求
    库根至少是 `upload` 模式？—— 不是。转码产物写在程序自己的 data 目录，
    不碰用户照片库，因此只读库**也可以**转码：用户的原始文件一个字节都不动。
    这也是"只读库 + 手机看不了 HEVC"这个最常见组合能被救回来的原因。
    """
    st = _st(ctx)
    mid = ctx.param_int("media_id")
    row = st.db.query_one(
        "SELECT id, root_id, rel_path, file_name, ext, size, mtime, kind,"
        "       missing, container, duration, uploader_client_id"
        "  FROM media WHERE id = ?",
        (mid,),
    )
    if row is None:
        raise NotFound(None)
    # ★ 审计 07f03d S-05：转码流是**独立入口**，raw 有的媒体授权这里
    #   也必须有一次 —— 否则游客拿主人视频的 id 就能直取转码产物，
    #   而同一 id 的 /raw 与 /stream-ticket 都是 404（自相矛盾即漏洞）。
    common.check_media_visible(ctx, st, row)
    if row["missing"]:
        raise NotFound(f"源文件已丢失 media_id={mid}")
    if videomod.row_kind(row) != "video":
        raise AppError(E1001, "只有视频可以转码")
    return row


def _source_path(st: AppState, row: dict):
    root = st.cfg.root_by_id(row["root_id"], allow_disabled=False)
    src = sandbox.resolve(root, row["rel_path"], "read", cfg=st.cfg)
    if not src.is_file():
        raise NotFound(f"源文件不存在 media_id={row['id']}")
    return src


def _ffmpeg(st: AppState) -> str:
    """找 ffmpeg；没装就明确告诉用户"这台电脑没有转码器"。"""
    try:
        return videomod.find_bin("ffmpeg", configured_dir=st.cfg.video_ffmpeg_dir)
    except videomod.FFmpegMissing as exc:
        raise AppError(E3004, str(exc)) from None




def _client_id(ctx: Ctx) -> str | None:
    """审计里的"是谁发起的"；取不到就 None（审计不许因此失败）。"""
    state = ctx.state
    client = state.get("client") if isinstance(state, dict) else None
    if isinstance(client, dict):
        return str(client.get("id") or "") or None
    return None
def _json(job: tcmod.TranscodeJob) -> Reply:
    r = json_reply(job.as_json())
    # 进度是"此刻的状态"，任何层都不许缓存它（否则前端会一直看到 0%）。
    r.set_cache(0, private=True)
    return r


async def start(ctx: Ctx) -> Reply:
    """`POST /api/items/{id}/transcode` —— 开始转码（幂等）。

    幂等很重要：用户在"转码中"的按钮上多点两下、或前端轮询时误发 POST，
    都不该起第二个 ffmpeg（那会把 2 vCPU 的机器直接打满）。
    """
    st = _st(ctx)
    cfg: Config = st.cfg
    if not cfg.video_enabled:
        raise AppError(E1001, "视频功能已关闭（配置 video.enabled=false）")
    row = _video_row(ctx)
    src = _source_path(st, row)
    ffmpeg = _ffmpeg(st)

    # ★ 排队上限（审查 R-M3-06）：`_slots` 只限并发跑的 ffmpeg，不限"等着的
    #   线程"。超限时明确 429，而不是再起第 33 个线程。
    running = st.transcodes.running_count()
    if running >= tcmod.MAX_PENDING_JOBS:
        raise AppError(
            E4007,
            f"转码任务太多（{running} 个在排队或正在转），"
            f"等前面的转完再试",
        )

    job = st.transcodes.start(
        media_id=int(row["id"]),
        src=src,
        duration=float(row["duration"] or 0) or None,
        ffmpeg=ffmpeg,
    )
    audit.record(
        st.db, st.cfg, "transcode.start",
        rel_path=str(row["rel_path"]), device=st.device_name,
        client_id=_client_id(ctx),
        detail={"media_id": int(row["id"]), "state": job.state},
    )
    return _json(job)


async def status(ctx: Ctx) -> Reply:
    """`GET /api/items/{id}/transcode` —— 查状态。

    没有 job 时回 `{"state": "none"}` 而不是 404：前端每次打开视频都会问
    一次"转过没有"，404 会让它把"没转过"当成"出错"。
    """
    st = _st(ctx)
    row = _video_row(ctx)
    job = st.transcodes.get(int(row["id"]))
    if job is None:
        r = json_reply({"media_id": int(row["id"]), "state": "none",
                        "progress": 0.0, "size": 0, "error": ""})
        r.set_cache(0, private=True)
        return r
    return _json(job)


async def stream(ctx: Ctx) -> Reply:
    """`GET /api/items/{id}/transcode/stream?t=<票>` —— 播放转码产物。"""
    st = _st(ctx)
    mid = ctx.param_int("media_id")

    ticket = ctx.q("t")
    if ticket and not streamticket.verify(st.db, ticket, mid):
        raise Unauthorized("播放票无效或已过期，请重新打开视频")

    row = _video_row(ctx)
    job = st.transcodes.get(int(row["id"]))
    if job is None or job.state != tcmod.STATE_READY or not job.path:
        # 还没转好：给 409 而不是 404 —— 前端据此显示"还在转/失败了"，
        # 而不是"这个视频不存在"。
        raise Conflict("转码产物还没准备好")

    path = Path(job.path)
    if not path.is_file():
        raise AppError(E1001, "转码产物已被清理，请重新转码")
    size = path.stat().st_size
    tcmod.Transcoder._touch(path)          # 播放 = 使用，LRU 里往后排

    etag = f"t-{int(row['mtime'])}-{size}"
    media_type = "video/mp4"               # 转码产物恒为 mp4/h264
    try:
        rng = parse_range(ctx.header("range"), size)
    except RangeNotSatisfiable:
        raise AppError(E1008, headers={
            "Content-Range": unsatisfied_range_header(size),
        }) from None

    if rng is None:
        r = file_reply(iter_file(path), media_type, size, 200)
    else:
        start_at, end = rng
        r = file_reply(iter_file(path, start=start_at, length=end - start_at + 1),
                       media_type, range_length(rng), 206,
                       headers={"Content-Range": content_range_header(rng, size)})
    r.headers["Accept-Ranges"] = "bytes"
    r.set_etag(etag)
    # 产物随源文件指纹变化 → 名字变了 URL 也变了，可以长缓存。
    r.set_cache(86400, private=True, immutable=True)
    return r
