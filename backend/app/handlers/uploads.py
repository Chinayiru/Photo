"""上传相关路由处理函数。

接口约定见 docs/04-接口设计文档.md §5。三条必须遵守的规则：

1. **init 与 complete 两处都判秒传。** init 判是为了省掉全部字节；
   complete 判是为了覆盖"客户端没带 sha256"和两台手机同时传同一张
   照片的情况。只在 init 判的话，并发上传会双双落盘，同一张照片
   在库里出现两次，用户翻时间线时看到重复。

2. **`client_chunk_size` 被服务端忽略。** 分片大小由服务端定：
   客户端报的分片大小若与 DB 里记的不一致，续传时 offset 对不上，
   而客户端会以为是自己算错了，反复重试到会话过期。

3. **complete 返回的 `media_id` 可能是 null。** 索引失败时文件
   已经安全落盘，但还没进库。此时报成功而不是报错 ——
   报错会让用户重传几百 MB，而照片其实就在库里。
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

from .. import archive, uploads
from . import common
from ..core import Ctx, Reply, json_reply
from ..errors import AppError, BadRequest, E1007, E1010

log = logging.getLogger(__name__)

__all__ = ["init_upload", "patch_upload", "get_upload", "complete_upload", "abort_upload"]


def _state(ctx: Ctx):
    st = getattr(ctx, "app", None)
    if st is None:
        raise RuntimeError("Ctx.app 未装配：无法访问 AppState")
    return st


def _upload_id(ctx: Ctx) -> str:
    uid = ctx.params.get("upload_id") or ""
    if not isinstance(uid, str) or not uid:
        raise BadRequest("缺少 upload_id")
    return uid


def _client_id(ctx: Ctx) -> str:
    """当前请求的设备身份（`devices.id`），用于绑定上传会话（ISSUE-034 ②）。

    来源是中间件里 `auth.require_client()` 写进 `ctx.state["client"]` 的
    结果 —— 那是本项目里"一个客户端身份"唯一的权威来源（`devices.id`，
    也就是客户端看到的 `client_id`）。这里**不**新造标识（比如从 User-Agent
    或 IP 推）：UA 可随便改、IP 会随 DHCP/切网变，用它们做归属判断
    会在"手机换了 Wi-Fi"时把用户自己的会话判成别人的。

    取不到（直接调 handler 的单元测试、`require_pairing=false` 的本机
    调试模式）时返回空串表示"未知归属"：这种调用方只在同样未知归属的
    会话之间复用，绝不会碰到真实设备的会话。
    """
    info = ctx.state.get("client")
    if isinstance(info, dict):
        return str(info.get("id") or "")
    return ""


def _owned_session(st: Any, uid: str, ctx: Ctx) -> dict[str, Any]:
    """按"属于当前设备"取会话；不属于时是 404（不是 403，见 `get_session`）。"""
    return uploads.get_session(st.db, uid, client_id=_client_id(ctx))


async def init_upload(ctx: Ctx) -> Reply:
    """`POST /api/uploads/init` —— 创建会话或命中秒传。

    状态码区分三种结果：
      * 201 新建了会话（客户端应开始传第一片）
      * 200 秒传命中（客户端一个字节都不用传）
      * 200 复用了已有会话（客户端从返回的 offset 继续）
    """
    st = _state(ctx)
    body = await ctx.json(limit=64 * 1024)

    name = body.get("name")
    size = body.get("size")
    if isinstance(size, str) and size.isdigit():
        size = int(size)
    if not isinstance(size, int) or isinstance(size, bool):
        raise BadRequest("size 必须是整数")

    result = uploads.create_session(
        st.db,
        st.cfg,
        name=name,
        size=size,
        root_id=common.root_id_payload(body, ctx),
        sha256=_opt_str(body.get("sha256")),
        mtime=_opt_float(body.get("mtime")),
        target_dir=_opt_str(body.get("target_dir")),
        declared_taken_at=_opt_float(body.get("declared_taken_at") or body.get("taken_at")),
        client_chunk_size=body.get("client_chunk_size"),
        client_id=_client_id(ctx),
        # ★ 审计 07f03d S-03：游客身份必须传进业务层。不传的话
        #   `create_session` 走默认 guest=False，就会**按请求里的 root**
        #   建普通会话 —— 游客于是能往主库写文件，且 uploader_client_id
        #   为空，后面「只看自己传的」也跟着失效。
        guest=common.is_guest(ctx),
    )

    payload: dict[str, Any] = {
        "upload_id": result["upload_id"],
        "chunk_size": result["chunk_size"],
        "offset": result["offset"],
        "exists": result["exists"],
        "media_id": result["media_id"],
        "expires_at": result["expires_at"],
        "max_size": int(st.cfg.upload["max_file_mb"]) * 1024 * 1024,
        "sha256_required": False,
        "dedup": result["dedup"],
    }
    if result["exists"]:
        payload["final_rel"] = result.get("final_rel")
    status = 201 if result["created"] else 200
    return json_reply(payload, status)


async def patch_upload(ctx: Ctx) -> Reply:
    """`PATCH /api/uploads/{upload_id}` —— 追加一个分片。

    用 `Content-Range: bytes start-end/total` 表达位置。之所以不用
    `X-Offset` 之类的自定义头：Content-Range 是标准头，手机端
    fetch 直接设得上，而且将来接云隧道时中间层也能识别。
    """
    st = _state(ctx)
    uid = _upload_id(ctx)

    raw = ctx.header("content-range") or ctx.header("x-content-range")
    if not raw:
        raise AppError(E1010, "缺少 Content-Range 头")
    start, end, total = _parse_upload_range(raw)

    # 边读边判长度上限：读完再判的话，一个声明 2 GB 的分片会先
    # 把内存吃满。分片上限取"配置分片大小 + 1 字节"，多出来的
    # 那一点留给客户端在最后一片上略微超出。
    sess = _owned_session(st, uid, ctx)
    limit = int(sess["chunk_size"]) + 1024
    try:
        data = await ctx.body_bytes(limit=limit)
    except AppError as exc:
        # `body_bytes` 只有「超过 limit」一种失败，但它抛的是 **E1006
        # 「文件超过 2 GB 上限」** —— 对分片上传是彻底的误导：客户端会以为
        # 整个文件超限而直接放弃，而真正超的只是这一片。
        # `uploads.append_chunk` 里同类越界报的是 E1007「分片过大」，
        # 这里把 handler 层提前撞上的那种也转成同一条（2026-10-23 云隧道实测：
        # 2 MB 分片多带 8 KB，回的是 E1006/「2 GB 上限」）。
        if exc.code == "E1006":
            raise AppError(E1007, f"分片超过 {sess['chunk_size']} 字节上限") from exc
        raise

    # ⚠️ 不要在 handler 里再取一次 session_lock：append_chunk 内部已经
    # 加了同一把锁，而 threading.Lock **不可重入** —— 重复获取会自我死锁，
    # 表现为 PATCH 永远不返回（客户端超时），且服务端没有任何异常日志。
    result = uploads.append_chunk(
        st.db, st.cfg, uid, start=start, end=end, total=total, data=data
    )

    return json_reply(result, 206)


async def get_upload(ctx: Ctx) -> Reply:
    """`GET /api/uploads/{upload_id}` —— 查询已收字节数。

    手机端切网络后要靠这个接口恢复进度。返回的 `offset` 以磁盘为准，
    不是 DB 里的值（崩溃可能发生在写完文件与更新 DB 之间）。

    ★ 对**已结束**的会话（`status != 'open'`）不做磁盘对账（审计 F13）：
    `complete` 归档后 `.part` 已被删掉，磁盘是 0 字节，若照旧对账会把
    DB 里的 `received` 写成 0，返回 `offset=0, complete=false` ——
    客户端按 0 重传拿到 `409 E1012`（`retryable=false`），界面报"上传失败"，
    而照片其实早已入库。查询接口不该有写副作用。

    ★ 同时补齐 `media_id` / `dedup` / `final_rel` / `declared_name`（审计 F19）：
    `docs/04` §4.3.3 列了它们，而客户端正是靠 `media_id` 判断
    "其实已经成功了、别再传"，缺了它 F13 的后果会更严重。
    """
    st = _state(ctx)
    uid = _upload_id(ctx)
    sess = _owned_session(st, uid, ctx)
    received = uploads._reconcile_offset(st.db, sess)
    total = int(sess["total_size"])
    return json_reply({
        "upload_id": uid,
        "offset": received,
        "received": received,
        "total_size": total,
        "chunk_size": int(sess["chunk_size"]),
        "status": sess["status"],
        "expires_at": float(sess["expires_at"]),
        "complete": received >= total,
        "sha256": sess["sha256"],
        "declared_name": sess["declared_name"],
        "target_rel": sess["target_rel"],
        "final_rel": sess["final_rel"],
        "media_id": sess["media_id"],
        "dedup": None if sess["dedup"] is None else bool(sess["dedup"]),
    })


async def complete_upload(ctx: Ctx) -> Reply:
    """`POST /api/uploads/{upload_id}/complete` —— 校验、归档、索引。"""
    st = _state(ctx)
    uid = _upload_id(ctx)

    # 归属校验放在最前面：`complete_session_async` 内部还会按 upload_id
    # 取一次会话，但那时已经开始校验/归档了 —— 别人的 uid 必须在
    # 任何副作用之前就变成 404，否则"能 complete 别人的会话"就等于
    # 能把别人传了一半的文件提前归档进库。
    _owned_session(st, uid, ctx)

    # ★ 不能拿 `Content-Length` 当"有没有 body"的判据（审查 R-M4-01）：
    #   云端隧道适配器转发请求时只带白名单里的头，`content-length` 不在
    #   其中 —— 于是手机经云通路补的 `sha256`/`taken_at` 被**静默丢弃**，
    #   同一次调用在局域网是 400/413、经云通路却是 201，客户端完全看不出。
    #   `ctx.json()` 本身对空体返回 `{}`，所以无条件解析是安全的。
    body: dict[str, Any] = await ctx.json(limit=16 * 1024)

    result = await uploads.complete_session_async(
        st.db,
        st.cfg,
        uid,
        indexer=getattr(st, "indexer", None),
        thumbs=getattr(st, "thumbs", None),
        sha256=_opt_str(body.get("sha256")),
        taken_at=_opt_float(body.get("taken_at")),
        guest=common.is_guest(ctx),
    )

    if result.get("media_id") is not None and getattr(st, "thumbs", None) is not None:
        try:
            st.thumbs.enqueue(int(result["media_id"]))
        except Exception:      # noqa: BLE001
            # 缩略图入队失败不影响上传结果：缩略图是惰性可补的，
            # 用户在时间线上点开时还会触发一次按需生成。
            log.debug("缩略图入队失败：media#%s", result.get("media_id"), exc_info=True)

    status = 200 if result.get("already_completed") else 201

    # ★ AC-3.13：上传完成必须推一条 `upload_done`。
    #
    # 为什么在这一层发而不是在 `uploads.py` 里：`uploads.py` 是协议无关的
    # 业务层，连 EventBus 是什么都不知道（DP-4「两条通路，一套 API」）——
    # 二期经隧道上传时走的是另一条通路，在业务层发事件会让二期多一份。
    #
    # 为什么必须发：手机端传完最后一片后**自己并不知道**服务端把文件
    # 归档到了哪里（`_place_sync` 可能改名去重，`rel_path` 未必是
    # 客户端猜的那个）。不推的话用户看到的是"进度条满了但照片没出现在
    # 时间线里"，只能手动下拉刷新。
    bus = getattr(st, "bus", None)
    if bus is not None:
        try:
            bus.publish({
                "type": "upload_done",
                "upload_id": uid,
                "media_id": result.get("media_id"),
                "rel_path": result.get("rel_path"),
                # 秒传/重复完成时 media 表其实没新增行，前端要能分辨这一点。
                "dedup": bool(result.get("dedup")),
                "already_completed": bool(result.get("already_completed")),
                "ts": time.time(),
            })
        except Exception:      # noqa: BLE001
            # 推送失败绝不能让上传报错：文件已经安全落盘并入库，
            # 让用户重传几百 MB 才是真正的损失。前端下次刷新就会看到。
            log.debug("upload_done 推送失败：%s", uid, exc_info=True)

    return json_reply(result, status)


async def abort_upload(ctx: Ctx) -> Reply:
    """`DELETE /api/uploads/{upload_id}` —— 取消上传。"""
    st = _state(ctx)
    uid = _upload_id(ctx)
    # 同 complete：先判归属。否则一台设备可以把别人的在传会话删掉，
    # 对端表现为"传了一半突然 409/419"，而服务端没有任何越权痕迹。
    _owned_session(st, uid, ctx)
    uploads.abort_session(st.db, st.cfg, uid)
    return json_reply({"upload_id": uid, "aborted": True})


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRequest("参数类型不正确")
    v = value.strip()
    return v or None


def _opt_float(value: Any) -> float | None:
    """取可选的时间戳参数。

    ★ 必须挡住 `inf`/`nan`/超界值（审计 F2）。早先这里只判"能不能转成
    float"，于是 `declared_taken_at=1e300` 一路穿过校验，在
    `archive.archive_dir()` 的 `datetime.fromtimestamp()` 处抛
    `OverflowError`；`OverflowError` 不在 `errors.EXC_MAP` 里 → 500，
    而且它抛在 `complete_session` 归档之后，会话永远停在 `status='open'`
    —— 攒满 `MAX_OPEN_SESSIONS`(3) 之后**全站上传被拒一天**。

    合法区间与 `archive.MIN_ARCHIVE_TS` / `MAX_ARCHIVE_TS` 一致。
    超界值直接 400 而不是静默夹取：客户端显式声明了一个 3000 年后的
    拍摄时间，多半是它自己的 bug，静默改成"现在"会把照片归到错误
    的年份目录里，用户几个月后才会发现。
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BadRequest("参数类型不正确")
    if isinstance(value, (int, float)):
        ts = float(value)
    elif isinstance(value, str):
        try:
            ts = float(value)
        except ValueError:
            raise BadRequest("时间戳格式不正确") from None
    else:
        raise BadRequest("时间戳格式不正确")
    if not math.isfinite(ts):
        raise BadRequest("时间戳格式不正确")
    if ts < archive.MIN_ARCHIVE_TS or ts > archive.MAX_ARCHIVE_TS:
        raise BadRequest("时间戳超出合理范围（1980 年至 3000 年之间）")
    return ts


def _parse_upload_range(raw: str) -> tuple[int, int, int]:
    """解析 `bytes start-end/total`。

    与下载用的 `parse_range` 不同：这里 **end 是含端点的实际结束位置**
    且必须显式给出（`bytes 0-8388607/10485760`），不接受 `bytes=0-`
    这种开放式写法 —— 上传必须说清这一段有多长，否则无法校验
    "收到的字节数 == 声明的范围长度"，而长度不符正是文件损坏的
    最主要表现。
    """
    s = raw.strip()
    if not s.lower().startswith("bytes "):
        raise AppError(E1010, f"Content-Range 格式不正确：{raw[:40]}")
    s = s[6:].strip()
    if "/" not in s:
        raise AppError(E1010, "Content-Range 缺少总长度")
    rng, _, total_s = s.partition("/")
    rng = rng.strip()
    if "-" not in rng:
        raise AppError(E1010, "Content-Range 缺少范围")
    start_s, _, end_s = rng.partition("-")
    try:
        start = int(start_s.strip())
        end = int(end_s.strip())
        total = int(total_s.strip())
    except ValueError:
        raise AppError(E1010, f"Content-Range 不是整数：{raw[:40]}") from None
    if total <= 0:
        raise AppError(E1010, "总长度必须为正")
    if start < 0 or end < start:
        raise AppError(E1010, "范围不合法")
    if end >= total:
        raise AppError(E1010, "范围超出文件大小")
    return start, end, total
