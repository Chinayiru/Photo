"""路由注册表：把 URL 路径映射到处理函数。

**整个程序只有这一份路由表。** 局域网 HTTP 适配器只注册一条 catch-all，
把请求交给 `Router`；二期云隧道适配器把帧还原成 `Ctx` 后交给同一个
`Router`。两条通路走完全相同的处理函数（设计原则 DP-4），因此不可能
出现"局域网能访问、云端 404"。

注册顺序无关紧要（正则按整段匹配），但按功能分组便于人工核对
docs/04-接口设计文档.md 的接口清单。
"""
from __future__ import annotations

from ..core import Router
from ..handlers import auth as h_auth
from ..handlers import browse as h_browse
from ..handlers import events as h_events
from ..handlers import system as h_system
from ..handlers import transcode as h_transcode
from ..handlers import uploads as h_uploads

__all__ = ["build_router"]


def build_router() -> Router:
    """构造完整路由表。"""
    r = Router()

    # --- 系统 ---------------------------------------------------------
    r.add("GET", "/api/system/ping", h_system.ping, name="system.ping")
    r.add("GET", "/api/system/info", h_system.info, name="system.info")
    r.add(
        "GET", "/api/system/entrances", h_system.entrances, name="system.entrances",
    )
    r.add("GET", "/api/system/stats", h_system.stats, name="system.stats")
    r.add("GET", "/api/events", h_events.events, name="system.events")

    # --- 配对与设备 ---------------------------------------------------
    r.add("POST", "/api/pair", h_auth.pair, name="auth.pair")
    r.add("GET", "/api/clients/me", h_auth.me, name="auth.me")
    r.add("GET", "/api/devices", h_auth.list_devices, name="auth.devices")
    r.add("DELETE", "/api/devices/{client_id}", h_auth.revoke_device, name="auth.revoke")

    # --- 浏览 ---------------------------------------------------------
    r.add("GET", "/api/timeline", h_browse.timeline, name="browse.timeline")
    r.add("GET", "/api/search", h_browse.search, name="browse.search")
    r.add("GET", "/api/folders", h_browse.folders, name="browse.folders")
    # 「按文件夹」视图的正文：某一层目录里**直接**存放的条目（游标分页）。
    # 独立端点而非给 /api/folders 加参数：前者是一页媒体、后者是目录列表，
    # 两者的分页语义不同（一个按 taken_at 游标、一个按目录名字典序）。
    r.add("GET", "/api/folders/media", h_browse.folder_media,
          name="browse.folder_media")
    r.add("GET", "/api/items/{media_id}", h_browse.get_item, name="browse.item")
    r.add("GET", "/api/items/{media_id}/thumb", h_browse.get_thumb, name="browse.thumb")
    r.add("GET", "/api/items/{media_id}/preview", h_browse.get_preview, name="browse.preview")
    r.add("GET", "/api/items/{media_id}/raw", h_browse.get_raw, name="browse.raw")
    # 视频播放流：与 /raw 同一实现，只是前端据此选 <video>（ISSUE-055）
    r.add("GET", "/api/items/{media_id}/stream", h_browse.stream_video,
          name="browse.stream")
    # 换票：`<video>` 发不了 Authorization，前端先换一张短命票拼进播放地址
    r.add("GET", "/api/items/{media_id}/stream-ticket", h_browse.stream_ticket,
          name="browse.stream_ticket")
    # 按需转码（ISSUE-058）：手机解不了 HEVC 时，让电脑先转成 h264 再播
    r.add("POST", "/api/items/{media_id}/transcode", h_transcode.start,
          name="transcode.start")
    r.add("GET", "/api/items/{media_id}/transcode", h_transcode.status,
          name="transcode.status")
    r.add("GET", "/api/items/{media_id}/transcode/stream", h_transcode.stream,
          name="transcode.stream")

    # --- 修改 ---------------------------------------------------------
    r.add("POST", "/api/items/delete", h_browse.delete_items, name="browse.delete")
    r.add("POST", "/api/items/move", h_browse.move_items, name="browse.move")
    r.add("POST", "/api/items/rename", h_browse.rename_item, name="browse.rename")
    r.add("POST", "/api/index/rescan", h_browse.rescan, name="browse.rescan")

    # --- 上传 ---------------------------------------------------------
    r.add("POST", "/api/uploads/init", h_uploads.init_upload, name="upload.init")
    r.add("GET", "/api/uploads/{upload_id}", h_uploads.get_upload, name="upload.get")
    r.add("PATCH", "/api/uploads/{upload_id}", h_uploads.patch_upload, name="upload.patch")
    r.add(
        "POST",
        "/api/uploads/{upload_id}/complete",
        h_uploads.complete_upload,
        name="upload.complete",
    )
    r.add("DELETE", "/api/uploads/{upload_id}", h_uploads.abort_upload, name="upload.abort")

    # --- 本机管理面板（仅回环，中间件已强制） --------------------------
    r.add("POST", "/panel/pair-code", h_auth.panel_pair_code, name="panel.pair_code")
    r.add("GET", "/panel/devices", h_auth.panel_devices, name="panel.devices")
    r.add("GET", "/panel/audit", h_auth.panel_audit, name="panel.audit")

    return r
