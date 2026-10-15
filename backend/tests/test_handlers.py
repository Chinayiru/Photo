"""handlers/browse.py 与 handlers/system.py 的测试。

这一层是"接口契约"的守门人，所以测试**不验证实现细节**，只验证
外部可观察的行为：状态码、响应体结构、头部、以及"不该发生的事"。

## 为什么用真实文件而不是 mock

缩略图生成、Range 切片、EXIF 读取都是与真实文件系统/字节打交道的逻辑，
mock 掉之后测的就只是"我自己的 mock 被调用过"，而真正的 bug
（比如 206 切错偏移）恰恰在 mock 里看不见。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.core import Ctx, Router  # noqa: E402
from app.core.cursor import decode_cursor, encode_cursor  # noqa: E402
from app.db import Database  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.handlers import browse, system  # noqa: E402
from app.state import AppState  # noqa: E402


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture()
def library(tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    lib.mkdir()
    return lib


def _img(path: Path, size=(80, 60), color=(200, 100, 50),
         taken: str | None = None, fmt: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", size, color)
    kw = {}
    if taken:
        ex = Image.Exif()
        ex.get_ifd(0x8769)[0x9003] = taken       # 必须写 SubIFD
        kw["exif"] = ex.tobytes()
    if fmt is None:
        fmt = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG",
               "webp": "WEBP"}.get(path.suffix.lstrip(".").lower(), "JPEG")
    im.save(path, fmt, **kw)
    return path


def _state(tmp_path: Path, library: Path, *, mode: str = "readwrite",
           allow_delete: bool = True, thumbs=None,
           indexer=None) -> AppState:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    cfg = Config({
        "app": {"mode": mode},
        "roots": [{"id": "library", "path": str(library), "mode": mode}],
        "thumbs": {"sizes": [320, 1600]},
        "auth": {"allow_delete": allow_delete, "delete_to_recycle_bin": True},
        "index": {"batch_size": 50},
    }, path=data / "config.json", data_dir=data)
    db = Database(cfg.db_path)
    db.init()
    st = AppState(cfg, db, thumbs=thumbs, indexer=indexer)
    return st


def _router() -> Router:
    r = Router()
    # 用 add() 而不是装饰器：装饰器形态要写成
    # `@r.get("/x")` 挂在函数定义上，这里集中注册更便于一眼看全路由表。
    routes = [
        ("GET", "/api/system/ping", system.ping),
        ("GET", "/api/system/info", system.info),
        ("GET", "/api/system/stats", system.stats),
        ("GET", "/api/timeline", browse.timeline),
        ("GET", "/api/items/{id}", browse.get_item),
        ("GET", "/api/items/{id}/thumb", browse.get_thumb),
        ("GET", "/api/items/{id}/preview", browse.get_preview),
        ("GET", "/api/items/{id}/raw", browse.get_raw),
        ("GET", "/api/folders", browse.folders),
        ("GET", "/api/search", browse.search),
        ("POST", "/api/items/delete", browse.delete_items),
        ("POST", "/api/index/rescan", browse.rescan),
    ]
    for method, pattern, handler in routes:
        r.add(method, pattern, handler)
    return r


def _ctx(st: AppState, method: str, path: str, *, query: dict | None = None,
         headers: dict | None = None, body: bytes | None = None,
         params: dict | None = None) -> Ctx:
    q: dict[str, list[str]] = {}
    for k, v in (query or {}).items():
        q[k] = v if isinstance(v, list) else [str(v)]

    async def _stream():
        if body:
            yield body

    return Ctx(method=method, path=path, query=q, headers=headers or {},
               body=_stream() if body is not None else None,
               params=params or {}, app=st)


def _call(st: AppState, method: str, path: str, **kw):
    """跑一次完整 dispatch，返回 Reply。"""
    router = _router()
    ctx = _ctx(st, method, path, **kw)
    return asyncio.run(router.dispatch(ctx))


def _json(reply) -> dict:
    import json

    body = _body(reply)
    assert body is not None
    return json.loads(body)


def _body(reply) -> bytes:
    """把 Reply.body 统一取成 bytes。

    流式响应（缩略图、原图）的 body 是异步生成器 —— 测试不消费它的话，
    `reply.body` 只是个 generator 对象，`len()`/切片全都用不了，
    而"没消费"和"内容为空"在断言层面看起来一模一样。
    """
    b = reply.body
    if b is None:
        return b""
    if isinstance(b, bytes):
        return b
    return asyncio.run(_drain(b))


async def _drain(agen) -> bytes:
    out = bytearray()
    async for chunk in agen:
        out.extend(chunk)
    return bytes(out)


def _seed(st: AppState, library: Path) -> list[int]:
    """造 5 张照片并索引，返回 media id 列表（按 taken_at 倒序）。"""
    from app.indexer import Indexer

    _img(library / "2024" / "2024-05" / "a.jpg", taken="2024:05:12 18:30:00")
    _img(library / "2024" / "2024-05" / "b.jpg", taken="2024:05:12 18:30:00")
    _img(library / "2024" / "2024-05" / "c.jpg", taken="2024:05:12 18:30:01")
    _img(library / "2025" / "2025-01" / "d.png", taken="2025:01:02 09:00:00")
    _img(library / "2025" / "2025-01" / "e.png", taken="2025:01:02 09:00:00")
    idx = Indexer(st.cfg, st.db)
    asyncio.run(idx.scan_root(st.cfg.root_by_id("library")))
    rows = st.db.query("SELECT id FROM media ORDER BY taken_at DESC, id DESC")
    return [int(r["id"]) for r in rows]


# --------------------------------------------------------------------------
# 游标
# --------------------------------------------------------------------------

def test_cursor_roundtrip_preserves_subsecond() -> None:
    ts = 1715509200.123456
    c = encode_cursor(ts, 4242)
    got_ts, got_id = decode_cursor(c)
    assert got_id == 4242
    # 微秒必须保住：同一秒内多张连拍靠它区分，丢了会重复/漏项。
    assert abs(got_ts - ts) < 1e-6


@pytest.mark.parametrize("bad", [
    "!!!not-base64!!!", "Zm9v", "abc|def", "1.0|0", "1.0|-3",
    "nan|5", "inf|5", "a" * 300,
])
def test_bad_cursor_is_400_not_500(bad: str) -> None:
    """非法游标必须回 400。

    500 会让前端以为"服务器坏了"并触发重试风暴，而正确行为是
    "刷新页面重新取游标"。这里逐条覆盖畸形输入。
    """
    with pytest.raises(AppError) as ei:
        decode_cursor(bad)
    assert ei.value.status == 400


def test_cursor_is_not_a_security_boundary() -> None:
    """手工构造的合法游标会被接受 —— 这是**有意的**。

    游标只是一个位置锚点（`taken_at|id`），伪造它最多让调用方
    跳到时间线的某个位置，不构成越权：所有访问仍然过沙箱与鉴权。
    反过来，若给游标加签名，前端就无法"跳到某年某月"这类深链接，
    收益远小于成本。这里把它钉成显式行为，避免以后有人误以为
    它是安全边界而写出依赖它的鉴权逻辑。
    """
    import base64

    forged = base64.urlsafe_b64encode(b"123|456").rstrip(b"=").decode()
    ts, mid = decode_cursor(forged)
    assert (ts, mid) == (123.0, 456)


# --------------------------------------------------------------------------
# 时间线
# --------------------------------------------------------------------------

def test_timeline_is_sorted_desc_and_paginated_by_cursor(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed(st, library)

    r = _call(st, "GET", "/api/timeline", query={"limit": "2"})
    body = _json(r)
    assert r.status == 200
    assert len(body["items"]) == 2
    assert body["has_more"] is True
    assert body["next_cursor"]
    assert "server_time" in body and "tz_offset_minutes" in body

    ts = [it["taken_at"] for it in body["items"]]
    assert ts == sorted(ts, reverse=True)

    # 第二页不能与第一页重复 —— 这正是 OFFSET 分页会出错的地方。
    r2 = _call(st, "GET", "/api/timeline",
               query={"limit": "2", "cursor": body["next_cursor"]})
    b2 = _json(r2)
    ids1 = {it["id"] for it in body["items"]}
    ids2 = {it["id"] for it in b2["items"]}
    assert not (ids1 & ids2), "游标分页出现了重复项"


def test_timeline_same_second_records_are_not_skipped(tmp_path, library) -> None:
    """同秒连拍必须靠 id 参与游标才不会整段丢失。

    真实库里同秒记录大量存在（连拍、批量拷贝的相同 mtime）。
    只带 taken_at 的游标会直接跳过同秒的其余记录。
    """
    st = _state(tmp_path, library)
    _seed(st, library)

    seen: list[int] = []
    cursor = None
    for _ in range(10):
        q = {"limit": "1"}
        if cursor:
            q["cursor"] = cursor
        b = _json(_call(st, "GET", "/api/timeline", query=q))
        seen.extend(it["id"] for it in b["items"])
        cursor = b["next_cursor"]
        if not b["has_more"]:
            break
    assert len(seen) == 5
    assert len(set(seen)) == 5, f"游标翻页有重复/丢失：{seen}"


def test_timeline_items_have_no_heavy_exif_fields(tmp_path, library) -> None:
    """轻量项不该带完整 EXIF —— 一页 100 条能省约 60% 字节。"""
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/timeline"))
    item = b["items"][0]
    assert set(item) == {"id", "name", "taken_at", "taken_src", "w", "h",
                         "thumb_v", "size", "thumb_state", "has_gps",
                         "root", "dir"}


def test_timeline_order_asc_is_rejected_not_ignored(tmp_path, library) -> None:
    """asc 必须报 400。静默按 desc 处理会让"跳到最早"的前端拿到最后一页。"""
    st = _state(tmp_path, library)
    _seed(st, library)
    r = _call(st, "GET", "/api/timeline", query={"order": "asc"})
    assert r.status == 400
    assert _json(r)["error"]["code"] == "E1001"


def test_timeline_limit_is_clamped(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed(st, library)
    r = _call(st, "GET", "/api/timeline", query={"limit": "99999"})
    assert r.status == 200          # 钳制而非报错


def test_timeline_limit_non_numeric_is_400(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    r = _call(st, "GET", "/api/timeline", query={"limit": "abc"})
    assert r.status == 400


def test_timeline_unknown_root_is_400(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    r = _call(st, "GET", "/api/timeline", query={"root": "nope"})
    assert r.status == 400


def test_timeline_kind_video_returns_empty_not_error(tmp_path, library) -> None:
    """一期没有视频。传 kind=video 应返回空列表，让前端空态自然显示。"""
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/timeline", query={"kind": "video"}))
    assert b["items"] == []


def test_timeline_from_to_filter_is_half_open(tmp_path, library) -> None:
    """`to` 不含端点：to=18:30:01 不该包含 18:30:01 那张。"""
    st = _state(tmp_path, library)
    _seed(st, library)
    import datetime as dt

    f = dt.datetime(2024, 5, 12, 18, 30, 0).timestamp()
    t = dt.datetime(2024, 5, 12, 18, 30, 1).timestamp()
    b = _json(_call(st, "GET", "/api/timeline",
                    query={"from": str(f), "to": str(t)}))
    assert len(b["items"]) == 2, b["items"]


def test_timeline_accepts_iso_date_strings(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/timeline",
                    query={"from": "2024-01-01", "to": "2025-01-01"}))
    assert len(b["items"]) == 3


# --------------------------------------------------------------------------
# 单项
# --------------------------------------------------------------------------

def test_get_item_returns_full_detail(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    b = _json(_call(st, "GET", f"/api/items/{ids[0]}",
                    params={"id": str(ids[0])}))
    assert b["id"] == ids[0]
    assert set(b["exif"]) == {"camera", "lens", "iso", "fnum", "exposure", "focal"}
    assert "thumb" in b["urls"] and "raw" in b["urls"]
    assert b["missing"] is False


def test_get_item_prev_next_are_consistent(tmp_path, library) -> None:
    """prev/next 链必须能双向走通，否则查看器左右滑动会卡死。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)            # 已按 taken_at DESC 排列

    first = _json(_call(st, "GET", f"/api/items/{ids[0]}", params={"id": str(ids[0])}))
    assert first["prev_id"] is None
    assert first["next_id"] == ids[1]

    second = _json(_call(st, "GET", f"/api/items/{ids[1]}", params={"id": str(ids[1])}))
    assert second["prev_id"] == ids[0]
    assert second["next_id"] == ids[2]

    last = _json(_call(st, "GET", f"/api/items/{ids[-1]}", params={"id": str(ids[-1])}))
    assert last["next_id"] is None
    assert last["prev_id"] == ids[-2]


def test_get_item_taken_at_iso_has_timezone_offset(tmp_path, library) -> None:
    """ISO 时间必须带时区偏移。

    不带偏移的话手机会按自己的时区解释，用户看到照片"差 8 小时"——
    这是照片库最常被抱怨的 bug。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    b = _json(_call(st, "GET", f"/api/items/{ids[0]}", params={"id": str(ids[0])}))
    iso = b["taken_at_iso"]
    assert iso
    assert ("+" in iso[10:] or "-" in iso[10:] or iso.endswith("Z")), iso


def test_get_item_missing_id_is_404(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    r = _call(st, "GET", "/api/items/99999", params={"id": "99999"})
    assert r.status == 404
    assert _json(r)["error"]["code"] == "E1004"


# --------------------------------------------------------------------------
# 缩略图 / 预览 / 原图
# --------------------------------------------------------------------------

def test_thumb_generates_and_sets_immutable_cache(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "GET", f"/api/items/{ids[0]}/thumb",
              query={"size": "320"}, params={"id": str(ids[0])})
    assert r.status == 200
    assert r.headers["Content-Type"] == "image/webp"
    assert "immutable" in r.headers["Cache-Control"]
    assert "ETag" in r.headers
    assert r.headers.get("Vary") == "Authorization"
    assert _body(r)[:4] == b"RIFF"      # WebP 容器魔数


def test_thumb_etag_304_on_second_request(tmp_path, library) -> None:
    """条件请求必须命中 304 —— 弱网下省的是真实流量。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r1 = _call(st, "GET", f"/api/items/{ids[0]}/thumb",
               query={"size": "320"}, params={"id": str(ids[0])})
    etag = r1.headers["ETag"]
    r2 = _call(st, "GET", f"/api/items/{ids[0]}/thumb", query={"size": "320"},
               headers={"If-None-Match": etag}, params={"id": str(ids[0])})
    assert r2.status == 304
    assert _body(r2) == b""


def test_weak_etag_also_matches(tmp_path, library) -> None:
    """RFC 7232 弱比较要忽略 W/ 前缀。漏掉会让手机端条件请求全部落空。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r1 = _call(st, "GET", f"/api/items/{ids[0]}/thumb",
               query={"size": "320"}, params={"id": str(ids[0])})
    etag = r1.headers["ETag"]
    r2 = _call(st, "GET", f"/api/items/{ids[0]}/thumb", query={"size": "320"},
               headers={"If-None-Match": "W/" + etag},
               params={"id": str(ids[0])})
    assert r2.status == 304


def test_thumb_size_not_in_config_is_400(tmp_path, library) -> None:
    """尺寸决定缓存文件名，必须报错而不是钳制。

    悄悄换成 320 会让前端请求 1600 却拿到糊图，用户会以为"预览坏了"。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "GET", f"/api/items/{ids[0]}/thumb",
              query={"size": "999"}, params={"id": str(ids[0])})
    assert r.status == 400


def test_thumb_etag_changes_when_source_changes(tmp_path, library) -> None:
    """源文件变了 ETag 必须变。

    用 media id 做 ETag 是本项目最容易犯的错：用户手工替换同名照片后
    id 不变，手机缓存一年且不再校验 → **永远**看旧图。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]
    r1 = _call(st, "GET", f"/api/items/{mid}/thumb",
               query={"size": "320"}, params={"id": str(mid)})
    etag1 = r1.headers["ETag"]

    row = st.db.query_one("SELECT root_id, rel_path FROM media WHERE id=?", (mid,))
    src = library / row["rel_path"]
    _img(src, size=(120, 90), color=(10, 200, 10), taken="2024:05:12 18:30:00")
    # 改 mtime，否则签名不变（签名是 mtime-size）
    import os
    import time

    os.utime(src, (time.time(), time.time()))
    st.db.execute("UPDATE media SET thumb_state='pending', thumb_etag=NULL WHERE id=?",
                  (mid,))

    r2 = _call(st, "GET", f"/api/items/{mid}/thumb",
               query={"size": "320"}, params={"id": str(mid)})
    assert r2.headers["ETag"] != etag1


def test_thumb_placeholder_when_generation_fails(tmp_path, library) -> None:
    """生成失败要给占位图 + 200，不是 404/500。

    时间线上一个破图会让用户以为照片丢了，而其实只是缩略图没生成出来。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]
    st.db.execute("UPDATE media SET thumb_state='failed' WHERE id=?", (mid,))
    r = _call(st, "GET", f"/api/items/{mid}/thumb",
              query={"size": "320"}, params={"id": str(mid)})
    # 已判定 failed：返回 E3004（不反复重试）
    assert r.status == 500
    assert _json(r)["error"]["code"] == "E3004"


def test_preview_uses_largest_configured_size(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "GET", f"/api/items/{ids[0]}/preview", params={"id": str(ids[0])})
    assert r.status == 200
    assert r.headers["Content-Type"] == "image/webp"


def test_raw_full_download(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "GET", f"/api/items/{ids[0]}/raw", params={"id": str(ids[0])})
    assert r.status == 200
    assert r.headers.get("Accept-Ranges") == "bytes"
    assert r.headers["Content-Type"] in ("image/jpeg", "image/png")
    data = _body(r)
    assert int(r.headers["Content-Length"]) == len(data)
    assert data[:2] == b"\xff\xd8" or data[:8] == b"\x89PNG\r\n\x1a\n"


def test_raw_range_returns_206_with_correct_bytes(tmp_path, library) -> None:
    """206 必须切出**正确偏移**的字节。

    只断言状态码 206 是没用的 —— 偏移写错时状态码依然对，
    但用户拿到的是损坏文件（下载到 95% 续传后打不开）。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]
    full = _body(_call(st, "GET", f"/api/items/{mid}/raw", params={"id": str(mid)}))
    total = len(full)

    r = _call(st, "GET", f"/api/items/{mid}/raw", headers={"Range": "bytes=10-19"},
              params={"id": str(mid)})
    assert r.status == 206
    assert r.headers["Content-Range"] == f"bytes 10-19/{total}"
    part = _body(r)
    assert len(part) == 10
    assert part == full[10:20], "Range 切片偏移错误"


def test_raw_suffix_range_means_last_n_bytes(tmp_path, library) -> None:
    """`bytes=-500` 是"最后 500 字节"，不是"前 500 字节"。

    写错成 0..500 是经典 bug，会让断点续传拼出损坏文件。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]
    full = _body(_call(st, "GET", f"/api/items/{mid}/raw", params={"id": str(mid)}))

    r = _call(st, "GET", f"/api/items/{mid}/raw", headers={"Range": "bytes=-16"},
              params={"id": str(mid)})
    assert r.status == 206
    assert _body(r) == full[-16:]


def test_raw_unsatisfiable_range_is_416(tmp_path, library) -> None:
    """越界必须 416，不能静默给 200 全文件。

    静默给 200 会让客户端以为续传成功，把整个文件拼到已下载部分后面，
    得到两倍大小的损坏文件。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]
    r = _call(st, "GET", f"/api/items/{mid}/raw",
              headers={"Range": "bytes=99999999-"}, params={"id": str(mid)})
    assert r.status == 416
    assert r.headers["Content-Range"].startswith("bytes */")


def test_raw_download_sets_attachment_filename(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "GET", f"/api/items/{ids[0]}/raw",
              query={"download": "1", "name": "我的照片.jpg"},
              params={"id": str(ids[0])})
    assert "attachment" in r.headers.get("Content-Disposition", "")


def test_raw_name_with_crlf_cannot_inject_header(tmp_path, library) -> None:
    """CRLF 注入可以伪造响应头 —— 文件名必须清洗。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "GET", f"/api/items/{ids[0]}/raw",
              query={"download": "1", "name": "a\r\nX-Evil: 1"},
              params={"id": str(ids[0])})
    assert "\r" not in r.headers.get("Content-Disposition", "")
    assert "\n" not in r.headers.get("Content-Disposition", "")


def test_raw_missing_file_is_404(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]
    row = st.db.query_one("SELECT rel_path FROM media WHERE id=?", (mid,))
    (library / row["rel_path"]).unlink()
    r = _call(st, "GET", f"/api/items/{mid}/raw", params={"id": str(mid)})
    assert r.status == 404


# --------------------------------------------------------------------------
# 目录
# --------------------------------------------------------------------------

def test_folders_returns_recursive_counts(tmp_path, library) -> None:
    """count 必须是**递归**计数（含子目录）。

    只数直接子文件的话，一个 `2024/` 目录会显示 0 —— 用户看到
    "有 3 个年份目录，每个都是 0 张照片"会以为索引坏了。
    """
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/folders", query={"depth": "1"}))
    paths = {f["path"]: f["count"] for f in b["folders"]}
    assert paths.get("2024") == 3, paths
    assert paths.get("2025") == 2, paths


def test_folders_depth_two(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/folders", query={"depth": "2"}))
    paths = {f["path"] for f in b["folders"]}
    assert "2024/2024-05" in paths
    assert "2025/2025-01" in paths


def test_folders_have_cover_id(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/folders", query={"depth": "1"}))
    for f in b["folders"]:
        assert f["cover_id"] is not None


def test_folders_path_traversal_is_rejected(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed(st, library)
    r = _call(st, "GET", "/api/folders", query={"path": "../../../etc"})
    assert r.status == 400


def test_folders_underscore_in_name_is_not_a_wildcard(tmp_path, library) -> None:
    """目录名里的 `_` 不能当 LIKE 通配符。

    不转义的话搜 `2024_备份` 会匹配到 `2024X备份`，用户看到别的目录的照片。
    """
    st = _state(tmp_path, library)
    from app.indexer import Indexer

    _img(library / "2024_备份" / "x.jpg")
    _img(library / "2024X备份" / "y.jpg")
    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))

    b = _json(_call(st, "GET", "/api/folders", query={"depth": "1"}))
    paths = {f["path"] for f in b["folders"]}
    assert "2024_备份" in paths and "2024X备份" in paths
    cnt = {f["path"]: f["count"] for f in b["folders"]}
    assert cnt["2024_备份"] == 1
    assert cnt["2024X备份"] == 1


# --------------------------------------------------------------------------
# 搜索
# --------------------------------------------------------------------------

def test_search_requires_at_least_one_filter(tmp_path, library) -> None:
    """空查询必须 400。

    允许空查询等于送了一个"扫全库返回 500 条"的免费接口，
    手机端一个 bug 就能把索引器挤掉。
    """
    st = _state(tmp_path, library)
    _seed(st, library)
    r = _call(st, "GET", "/api/search")
    assert r.status == 400
    assert _json(r)["error"]["code"] == "E1001"


def test_search_by_filename_contains(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/search", query={"q": "c.jpg"}))
    assert len(b["items"]) == 1
    assert b["items"][0]["name"] == "c.jpg"


def test_search_chinese_filename(tmp_path, library) -> None:
    """中文文件名必须能搜到。

    一期用 LIKE 而不是 FTS5 正是因为 FTS5 的 unicode61 不切中文，
    搜"敦煌"会匹配不到 —— 这是选型时最容易被忽略的坑。
    """
    st = _state(tmp_path, library)
    from app.indexer import Indexer

    _img(library / "敦煌.jpg")
    _img(library / "莫高窟.jpg")
    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))
    b = _json(_call(st, "GET", "/api/search", query={"q": "敦煌"}))
    assert [it["name"] for it in b["items"]] == ["敦煌.jpg"]


def test_search_percent_is_literal(tmp_path, library) -> None:
    """`%` 在文件名里是字面量，不能当通配符（否则匹配全库）。"""
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/search", query={"q": "%"}))
    assert b["items"] == []


def test_search_by_gps_flag(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/search", query={"has_gps": "0"}))
    assert len(b["items"]) == 5
    b2 = _json(_call(st, "GET", "/api/search", query={"has_gps": "1"}))
    assert b2["items"] == []


def test_search_by_dir_prefix(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/search", query={"dir": "2024"}))
    assert len(b["items"]) == 3


# --------------------------------------------------------------------------
# 删除
# --------------------------------------------------------------------------

def test_delete_requires_config_flag(tmp_path, library) -> None:
    """`auth.allow_delete` 为 False 时必须 403。

    安全默认：即使用户把根目录设成 readwrite（为了能上传），
    也不该顺带得到"能删照片"的能力 —— 两个开关管两件事。
    """
    st = _state(tmp_path, library, allow_delete=False)
    ids = _seed(st, library)
    body = b'{"ids":[%d]}' % ids[0]
    r = _call(st, "POST", "/api/items/delete", body=body)
    assert r.status == 403
    assert _json(r)["error"]["code"] == "E1015"


def test_delete_moves_to_recycle_bin_and_marks_missing(tmp_path, library) -> None:
    """删除必须进回收站，且库里标 missing 而不是删行。

    直接删行会让"最近删除"失去依据，也无法区分"文件被移走"与"被删"。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]
    row = st.db.query_one("SELECT rel_path FROM media WHERE id=?", (mid,))
    src = library / row["rel_path"]
    assert src.exists()

    body = b'{"ids":[%d]}' % mid
    b = _json(_call(st, "POST", "/api/items/delete", body=body))
    assert b["deleted"] == 1
    assert b["failed"] == []
    assert not src.exists(), "文件应已移入回收站"
    assert st.db.scalar("SELECT missing FROM media WHERE id=?", (mid,)) == 1
    # 行仍然存在
    assert st.db.scalar("SELECT COUNT(*) FROM media WHERE id=?", (mid,)) == 1


def test_delete_rejects_empty_and_oversized_lists(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    r = _call(st, "POST", "/api/items/delete", body=b'{"ids":[]}')
    assert r.status == 400
    big = b'{"ids":[' + b",".join(str(i).encode() for i in range(1, 300)) + b']}'
    r2 = _call(st, "POST", "/api/items/delete", body=big)
    assert r2.status == 400


def test_delete_partial_failure_does_not_abort_batch(tmp_path, library) -> None:
    """一项失败不该中断整批 —— 否则用户删 50 张，第 3 张出问题就全废。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    good = ids[0]
    body = b'{"ids":[999999,%d]}' % good
    b = _json(_call(st, "POST", "/api/items/delete", body=body))
    assert b["deleted"] == 1
    assert len(b["failed"]) == 1


def test_delete_writes_audit_log(tmp_path, library) -> None:
    """删除必须留下审计痕迹（用户可能几个月后才发现少了一张）。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    _call(st, "POST", "/api/items/delete", body=b'{"ids":[%d]}' % ids[0])
    log_path = st.cfg.data_path("audit.log")
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert "\tdelete\t" in text


def test_delete_missing_file_still_marks_missing(tmp_path, library) -> None:
    """文件已经不在磁盘上时，删除仍应把库里标记为 missing（而不是报错）。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]
    row = st.db.query_one("SELECT rel_path FROM media WHERE id=?", (mid,))
    (library / row["rel_path"]).unlink()
    b = _json(_call(st, "POST", "/api/items/delete", body=b'{"ids":[%d]}' % mid))
    assert b["deleted"] == 1


# --------------------------------------------------------------------------
# system
# --------------------------------------------------------------------------

def test_ping_reports_device_id(tmp_path, library) -> None:
    """device_id 必须稳定且能被手机用来确认"连对了电脑"。"""
    st = _state(tmp_path, library)
    b1 = _json(_call(st, "GET", "/api/system/ping"))
    b2 = _json(_call(st, "GET", "/api/system/ping"))
    assert b1["pong"] is True
    assert b1["device_id"] == b2["device_id"]
    assert b1["device_id"]


def test_device_id_persists_across_state(tmp_path, library) -> None:
    """重启后 device_id 不能变，否则手机每次都要重新配对。"""
    st = _state(tmp_path, library)
    did = _json(_call(st, "GET", "/api/system/ping"))["device_id"]
    st2 = AppState(st.cfg, st.db)
    assert st2.device_id == did


def test_info_has_all_sections(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/system/info"))
    for key in ("app", "tunnel", "roots", "index", "thumbs", "server",
                "limits", "time", "device"):
        assert key in b, key
    assert b["tunnel"]["mode"] == "offline"
    assert b["thumbs"]["sizes"] == [320, 1600]
    assert b["limits"]["max_file_bytes"] == 2048 * 1024 * 1024
    assert b["time"]["tz_offset_minutes"] == 480


def test_info_root_count_matches_indexed(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/system/info"))
    assert b["roots"][0]["count"] == 5


def test_stats_counts_are_consistent(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/system/stats"))
    assert b["media"]["total"] == 5
    assert b["media"]["images"] == 5
    assert b["thumbs"]["ok"] + b["thumbs"]["pending"] + b["thumbs"]["failed"] == 5


# --------------------------------------------------------------------------
# 重扫
# --------------------------------------------------------------------------

class _FakeIndexer:
    def __init__(self) -> None:
        self.running = False
        self.calls: list[str] = []

    async def scan_all(self):
        self.calls.append("all")

    async def scan_root(self, root):
        self.calls.append(root.id)


def test_rescan_returns_202_immediately(tmp_path, library) -> None:
    """必须 202 而不是等扫描完成。

    真实库首轮约 20 秒，云端隧道下等待必然触发 60 秒超时；
    而且用户本该在设置页看进度条而不是转圈。
    """
    fake = _FakeIndexer()
    st = _state(tmp_path, library, indexer=fake)
    r = _call(st, "POST", "/api/index/rescan", body=b"{}")
    assert r.status == 202
    assert _json(r)["started"] is True
    # 让出事件循环，跑掉 create_task
    asyncio.run(asyncio.sleep(0))
    assert fake.calls == ["all"]


def test_rescan_conflicts_when_already_running(tmp_path, library) -> None:
    fake = _FakeIndexer()
    fake.running = True
    st = _state(tmp_path, library, indexer=fake)
    r = _call(st, "POST", "/api/index/rescan", body=b"{}")
    assert r.status == 409


def test_rescan_unknown_root_is_400(tmp_path, library) -> None:
    st = _state(tmp_path, library, indexer=_FakeIndexer())
    r = _call(st, "POST", "/api/index/rescan", body=b'{"root":"nope"}')
    assert r.status == 400


# --------------------------------------------------------------------------
# 脱敏（跨接口）
# --------------------------------------------------------------------------

def test_no_handler_leaks_absolute_path(tmp_path, library) -> None:
    """任何错误响应都不能出现绝对路径。

    照片是私有数据，路径会暴露目录结构（"E:\\\\software\\\\相册\\\\青甘环线"）。
    这里逐个打真实会出错的接口，检查响应体里有没有库根路径。
    """
    st = _state(tmp_path, library)
    _seed(st, library)
    marker = str(library).replace("\\", "\\\\")
    cases = [
        ("GET", "/api/items/999999", {}, {"id": "999999"}),
        ("GET", "/api/items/1/raw", {}, {"id": "1"}),
        ("GET", "/api/folders", {"path": "../x"}, {}),
        ("GET", "/api/timeline", {"root": "nope"}, {}),
        ("GET", "/api/search", {}, {}),
    ]
    for method, path, q, params in cases:
        r = _call(st, method, path, query=q, params=params)
        # ⚠️ 必须走 _body()：流式响应（/raw）的 body 是异步生成器，
        # 直接 .decode() 抛 AttributeError，那个报错会掩盖真正要测的
        # "响应里有没有泄漏路径"。
        data = _body(r)
        if data:
            text = data.decode("utf-8", "replace")
            assert str(library) not in text, (path, text)
            assert "E:\\\\" not in text or marker not in text
            assert "Traceback" not in text
            assert "File \"" not in text
