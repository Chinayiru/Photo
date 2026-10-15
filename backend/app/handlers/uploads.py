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
from typing import Any

from .. import uploads
from ..core import Ctx, Reply, json_reply
from ..errors import AppError, BadRequest, E1001, E1010, E1012, NotFound
from ..transports.base import parse_range, RangeNotSatisfiable

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
        root_id=_opt_str(body.get("root") or body.get("root_id")),
        sha256=_opt_str(body.get("sha256")),
        mtime=_opt_float(body.get("mtime")),
        target_dir=_opt_str(body.get("target_dir")),
        declared_taken_at=_opt_float(body.get("declared_taken_at") or body.get("taken_at")),
        client_chunk_size=body.get("client_chunk_size"),
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
    sess = uploads.get_session(st.db, uid)
    limit = int(sess["chunk_size"]) + 1024
    data = await ctx.body_bytes(limit=limit)

    with uploads.session_lock(uid):
        result = uploads.append_chunk(
            st.db, st.cfg, uid, start=start, end=end, total=total, data=data
        )

    return json_reply(result, 206)


async def get_upload(ctx: Ctx) -> Reply:
    """`GET /api/uploads/{upload_id}` —— 查询已收字节数。

    手机端切网络后要靠这个接口恢复进度。返回的 `offset` 以磁盘为准，
    不是 DB 里的值（崩溃可能发生在写完文件与更新 DB 之间）。
    """
    st = _state(ctx)
    uid = _upload_id(ctx)
    sess = uploads.get_session(st.db, uid)
    received = uploads._reconcile_offset(st.db, sess)
    return json_reply({
        "upload_id": uid,
        "offset": received,
        "received": received,
        "total_size": int(sess["total_size"]),
        "chunk_size": int(sess["chunk_size"]),
        "status": sess["status"],
        "expires_at": float(sess["expires_at"]),
        "complete": received >= int(sess["total_size"]),
        "sha256": sess["sha256"],
    })


async def complete_upload(ctx: Ctx) -> Reply:
    """`POST /api/uploads/{upload_id}/complete` —— 校验、归档、索引。"""
    st = _state(ctx)
    uid = _upload_id(ctx)

    body: dict[str, Any] = {}
    if ctx.header("content-length") not in (None, "0"):
        body = await ctx.json(limit=16 * 1024)

    result = await uploads.complete_session_async(
        st.db,
        st.cfg,
        uid,
        indexer=getattr(st, "indexer", None),
        thumbs=getattr(st, "thumbs", None),
        sha256=_opt_str(body.get("sha256")),
        taken_at=_opt_float(body.get("taken_at")),
    )

    if result.get("media_id") is not None and getattr(st, "thumbs", None) is not None:
        try:
            st.thumbs.enqueue(int(result["media_id"]))
        except Exception:      # noqa: BLE001
            # 缩略图入队失败不影响上传结果：缩略图是惰性可补的，
            # 用户在时间线上点开时还会触发一次按需生成。
            log.debug("缩略图入队失败：media#%s", result.get("media_id"), exc_info=True)

    status = 200 if result.get("already_completed") else 201
    return json_reply(result, status)


async def abort_upload(ctx: Ctx) -> Reply:
    """`DELETE /api/uploads/{upload_id}` —— 取消上传。"""
    st = _state(ctx)
    uid = _upload_id(ctx)
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
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BadRequest("参数类型不正确")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            raise BadRequest("时间戳格式不正确") from None
    raise BadRequest("时间戳格式不正确")


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
