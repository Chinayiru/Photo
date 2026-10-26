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
import os
import sys
import time
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
from app.thumbs import thumb_path  # noqa: E402


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
        ("GET", "/api/items/{media_id}", browse.get_item),
        ("GET", "/api/items/{media_id}/thumb", browse.get_thumb),
        ("GET", "/api/items/{media_id}/preview", browse.get_preview),
        ("GET", "/api/items/{media_id}/raw", browse.get_raw),
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
    """轻量项不该带完整 EXIF —— 一页 100 条能省约 60% 字节。

    `kind` / `duration` 是 2026-10 加视频时新增的两个小字段（字符串 + 浮点，
    约 20 字节），前端靠它们决定这一格用 `<img>` 还是角标标"视频"；
    它们不属于"重"字段，真重的 EXIF 那十几个键依然一个都不许出现。
    """
    st = _state(tmp_path, library)
    _seed(st, library)
    b = _json(_call(st, "GET", "/api/timeline"))
    item = b["items"][0]
    assert set(item) == {"id", "name", "kind", "duration", "taken_at",
                         "taken_src", "w", "h", "thumb_v", "size",
                         "thumb_state", "has_gps", "root", "dir"}


def test_timeline_order_asc_is_rejected_not_ignored(tmp_path, library) -> None:
    """asc 必须报 400。静默按 desc 处理会让"跳到最早"的前端拿到最后一页。"""
    st = _state(tmp_path, library)
    _seed(st, library)
    r = _call(st, "GET", "/api/timeline", query={"order": "asc"})
    assert r.status == 400
    assert _json(r)["error"]["code"] == "E1001"


def test_thumb_v_is_never_null_even_when_pending(tmp_path, library) -> None:
    """★ thumb_v 恒有值，pending 时也不能是 null。

    它是"源文件版本"（mtime-size 签名），不是"缩略图生成好了没"。

    早期版本写成 `if state == "ok" else None`，造成**死锁**：
      前端 `showImage = thumb_state === "ok" && !!thumb_v`
      → pending 的项连 `<img>` 都不渲染
      → 永远不会请求缩略图 URL
      → 而服务端恰恰靠"被请求"来按需生成（`browse._serve_thumb`）
      → 后台队列满时 `enqueue` 会**刻意丢弃**任务（不阻塞索引器）
      → 被丢的项永远停在 pending，前端永远不请求 → 永久灰块

    所以这条断言锁住的是"照片一定会显示出来"，不是字段格式。
    """
    st = _state(tmp_path, library)
    _seed(st, library)

    # 造一张 pending 的：直接把所有行的 thumb_state 改成 pending。
    # 真实场景里它来自"后台队列满被丢弃"，库里就是这个状态。
    st.db.execute("UPDATE media SET thumb_state = 'pending', thumb_etag = NULL")

    b = _json(_call(st, "GET", "/api/timeline"))
    assert b["items"], "时间线不该为空"
    for item in b["items"]:
        assert item["thumb_state"] == "pending", "前置条件：确实是 pending"
        assert item["thumb_v"], (
            f"pending 项的 thumb_v 是空的 → 前端不会请求缩略图 → 死锁。"
            f"item={item}"
        )
        assert item["thumb_v"].startswith("w320-"), item["thumb_v"]

    # 详情接口同样恒给值
    mid = b["items"][0]["id"]
    d = _json(_call(st, "GET", f"/api/items/{mid}",
                    params={"media_id": str(mid)}))
    assert d["thumb_v"], "详情接口的 thumb_v 也不能为空"


def test_pending_thumb_is_generated_on_demand(tmp_path, library) -> None:
    """★ 死锁的另一半：pending 项被请求时，服务端要真的生成出图。

    只让 thumb_v 恒有值还不够 —— 如果服务端对 pending 只回占位图，
    前端拿到 200 就再也不会重试，用户看到的仍然是灰块。
    """
    st = _state(tmp_path, library)
    _seed(st, library)

    b = _json(_call(st, "GET", "/api/timeline"))
    mid = b["items"][0]["id"]
    # 造出"缓存被删 + DB 说 pending"的真实场景
    st.db.execute(
        "UPDATE media SET thumb_state = 'pending', thumb_etag = NULL WHERE id = ?",
        (mid,),
    )
    for size in (320, 1600):
        p = thumb_path(st.cfg, "library", _rel_of(st, mid), size)
        if p.exists():
            p.unlink()

    r = _call(st, "GET", f"/api/items/{mid}/thumb",
              query={"size": "320"}, params={"media_id": str(mid)})
    assert r.status == 200
    body = _body(r)
    assert body[:4] == b"RIFF", "必须返回真 WebP，不能是空体"

    # 真图与灰色占位图必须能区分开：占位图是纯灰渐变，真图不会那么巧
    # 与占位图逐字节相同。用这个区分"真生成了"与"回了占位图"。
    from backend.app.thumbs import placeholder_webp

    assert body != placeholder_webp(320), (
        "返回的是灰色占位图 —— 服务端没有真的生成缩略图，"
        "前端拿到 200 后不会再重试，用户永远看到灰块"
    )

    # 生成后 DB 应被标成 ok（下次请求走缓存）
    row = st.db.query_one("SELECT thumb_state, thumb_etag FROM media WHERE id = ?",
                          (mid,))
    assert row["thumb_state"] == "ok", row["thumb_state"]
    assert row["thumb_etag"], "生成后必须写回签名，否则每次都判定陈旧"


def _rel_of(st, media_id: int) -> str:
    row = st.db.query_one("SELECT rel_path FROM media WHERE id = ?", (media_id,))
    return str(row["rel_path"])


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
                    params={"media_id": str(ids[0])}))
    assert b["id"] == ids[0]
    assert set(b["exif"]) == {"camera", "lens", "iso", "fnum", "exposure", "focal"}
    assert "thumb" in b["urls"] and "raw" in b["urls"]
    assert b["missing"] is False


def test_get_item_prev_next_are_consistent(tmp_path, library) -> None:
    """prev/next 链必须能双向走通，否则查看器左右滑动会卡死。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)            # 已按 taken_at DESC 排列

    first = _json(_call(st, "GET", f"/api/items/{ids[0]}", params={"media_id": str(ids[0])}))
    assert first["prev_id"] is None
    assert first["next_id"] == ids[1]

    second = _json(_call(st, "GET", f"/api/items/{ids[1]}", params={"media_id": str(ids[1])}))
    assert second["prev_id"] == ids[0]
    assert second["next_id"] == ids[2]

    last = _json(_call(st, "GET", f"/api/items/{ids[-1]}", params={"media_id": str(ids[-1])}))
    assert last["next_id"] is None
    assert last["prev_id"] == ids[-2]


def test_get_item_taken_at_iso_has_timezone_offset(tmp_path, library) -> None:
    """ISO 时间必须带时区偏移。

    不带偏移的话手机会按自己的时区解释，用户看到照片"差 8 小时"——
    这是照片库最常被抱怨的 bug。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    b = _json(_call(st, "GET", f"/api/items/{ids[0]}", params={"media_id": str(ids[0])}))
    iso = b["taken_at_iso"]
    assert iso
    assert ("+" in iso[10:] or "-" in iso[10:] or iso.endswith("Z")), iso


def test_get_item_missing_id_is_404(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    r = _call(st, "GET", "/api/items/99999", params={"media_id": "99999"})
    assert r.status == 404
    assert _json(r)["error"]["code"] == "E1004"


# --------------------------------------------------------------------------
# 缩略图 / 预览 / 原图
# --------------------------------------------------------------------------

def test_thumb_generates_and_sets_immutable_cache(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "GET", f"/api/items/{ids[0]}/thumb",
              query={"size": "320"}, params={"media_id": str(ids[0])})
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
               query={"size": "320"}, params={"media_id": str(ids[0])})
    etag = r1.headers["ETag"]
    r2 = _call(st, "GET", f"/api/items/{ids[0]}/thumb", query={"size": "320"},
               headers={"If-None-Match": etag}, params={"media_id": str(ids[0])})
    assert r2.status == 304
    assert _body(r2) == b""


def test_weak_etag_also_matches(tmp_path, library) -> None:
    """RFC 7232 弱比较要忽略 W/ 前缀。漏掉会让手机端条件请求全部落空。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r1 = _call(st, "GET", f"/api/items/{ids[0]}/thumb",
               query={"size": "320"}, params={"media_id": str(ids[0])})
    etag = r1.headers["ETag"]
    r2 = _call(st, "GET", f"/api/items/{ids[0]}/thumb", query={"size": "320"},
               headers={"If-None-Match": "W/" + etag},
               params={"media_id": str(ids[0])})
    assert r2.status == 304


def test_thumb_size_not_in_config_is_400(tmp_path, library) -> None:
    """尺寸决定缓存文件名，必须报错而不是钳制。

    悄悄换成 320 会让前端请求 1600 却拿到糊图，用户会以为"预览坏了"。
    """
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "GET", f"/api/items/{ids[0]}/thumb",
              query={"size": "999"}, params={"media_id": str(ids[0])})
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
               query={"size": "320"}, params={"media_id": str(mid)})
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
               query={"size": "320"}, params={"media_id": str(mid)})
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
              query={"size": "320"}, params={"media_id": str(mid)})
    # 已判定 failed：返回 E3004（不反复重试）
    assert r.status == 500
    assert _json(r)["error"]["code"] == "E3004"


def test_thumb_of_missing_row_is_placeholder_no_store(tmp_path, library) -> None:
    """已删除的照片：缩略图必须给占位图 + `no-store`，不能给真图 + 一年 immutable。

    审计 F5。删除只把行标成 `missing=1`，**缩略图缓存文件留在磁盘上**
    （`thumb_path` 全仓没有 unlink）。早先这里只判 `not dst.exists()`，
    于是判定"不需要生成"之后直接落到 `_file_thumb_reply()`，把那张
    已经删掉的照片的缩略图连 `max-age=31536000, immutable` 一起端出去 ——
    手机缓存一年且**不再发请求**：用户删了照片以为没了，那台手机一年内
    仍然拿得出来；删错了想恢复，客户端也不会重新问。

    对照 `get_raw`（`browse.py:484`）对同一行数据是 404，两个接口
    不能对"这张照片还在不在"给出相反答案。
    """
    from app.thumbs import generate_thumb, placeholder_webp

    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]
    row = st.db.query_one("SELECT rel_path FROM media WHERE id=?", (mid,))
    rel = row["rel_path"]

    # 先把真缩略图生成出来并写进磁盘缓存 —— 复现"删除后缓存文件仍在"。
    dst = thumb_path(st.cfg, "library", rel, 320)
    dst.parent.mkdir(parents=True, exist_ok=True)
    src = library / rel
    generate_thumb(str(src), dst, 320, orientation=1)
    assert dst.exists()

    # 走真实的删除路径：文件移入回收站 + 行标 missing=1。
    r = _call(st, "POST", "/api/items/delete", body=_json_body({"ids": [mid]}))
    assert r.status == 200
    assert not src.exists(), "删除后源文件应已移入回收站"
    assert st.db.query_one("SELECT missing FROM media WHERE id=?", (mid,))["missing"] == 1
    assert dst.exists(), "缩略图缓存文件**不**随删除清理（这正是 F5 的前提）"

    r = _call(st, "GET", f"/api/items/{mid}/thumb",
              query={"size": "320"}, params={"media_id": str(mid)})
    assert r.status == 200
    assert _body(r) == placeholder_webp(320), (
        "已删除的照片必须给占位图，不能把缓存里的真图端出去")
    cache = r.headers.get("Cache-Control", "")
    assert "immutable" not in cache, f"占位图绝不能带 immutable（拿到 {cache!r}）"
    assert "no-store" in cache, f"占位图必须 no-store（拿到 {cache!r}）"

    # 反向对照：删掉**源文件仍在库**的那张，缓存行为不能被一起改坏。
    live = ids[1]
    r2 = _call(st, "GET", f"/api/items/{live}/thumb",
               query={"size": "320"}, params={"media_id": str(live)})
    assert r2.status == 200
    assert "immutable" in r2.headers.get("Cache-Control", ""), (
        "正常照片的缩略图仍应走一年 immutable 缓存（URL 带 v= 版本参数）")


def test_preview_uses_largest_configured_size(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "GET", f"/api/items/{ids[0]}/preview", params={"media_id": str(ids[0])})
    assert r.status == 200
    assert r.headers["Content-Type"] == "image/webp"


def test_raw_full_download(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "GET", f"/api/items/{ids[0]}/raw", params={"media_id": str(ids[0])})
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
    full = _body(_call(st, "GET", f"/api/items/{mid}/raw", params={"media_id": str(mid)}))
    total = len(full)

    r = _call(st, "GET", f"/api/items/{mid}/raw", headers={"Range": "bytes=10-19"},
              params={"media_id": str(mid)})
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
    full = _body(_call(st, "GET", f"/api/items/{mid}/raw", params={"media_id": str(mid)}))

    r = _call(st, "GET", f"/api/items/{mid}/raw", headers={"Range": "bytes=-16"},
              params={"media_id": str(mid)})
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
              headers={"Range": "bytes=99999999-"}, params={"media_id": str(mid)})
    assert r.status == 416
    assert r.headers["Content-Range"].startswith("bytes */")


def test_raw_download_sets_attachment_filename(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "GET", f"/api/items/{ids[0]}/raw",
              query={"download": "1", "name": "我的照片.jpg"},
              params={"media_id": str(ids[0])})
    assert "attachment" in r.headers.get("Content-Disposition", "")


def test_raw_name_with_crlf_cannot_inject_header(tmp_path, library) -> None:
    """CRLF 注入可以伪造响应头 —— 文件名必须清洗。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    r = _call(st, "GET", f"/api/items/{ids[0]}/raw",
              query={"download": "1", "name": "a\r\nX-Evil: 1"},
              params={"media_id": str(ids[0])})
    assert "\r" not in r.headers.get("Content-Disposition", "")
    assert "\n" not in r.headers.get("Content-Disposition", "")


def test_raw_missing_file_is_404(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]
    row = st.db.query_one("SELECT rel_path FROM media WHERE id=?", (mid,))
    (library / row["rel_path"]).unlink()
    r = _call(st, "GET", f"/api/items/{mid}/raw", params={"media_id": str(mid)})
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


def test_search_matches_directory_path(tmp_path, library) -> None:
    """搜索必须能命中**目录名**，不能只看文件名。

    归档会把文件名改成 `20240512_183000.jpg`，用户记不住那个名字 ——
    他记得的是"敦煌那张""青甘环线里的"。只搜 file_name 时搜"敦煌"
    返回 0 条，用户会认为整个搜索功能坏了（真实库实测踩到：搜"敦煌"命中 0 条，而库里有 `青甘环线/敦煌/` 整个目录）。
    """
    st = _state(tmp_path, library)
    from app.indexer import Indexer

    _img(library / "青甘环线" / "敦煌" / "20240512_183000.jpg")
    _img(library / "其他地方" / "20240512_183001.jpg")
    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))
    b = _json(_call(st, "GET", "/api/search", query={"q": "敦煌"}))
    assert len(b["items"]) == 1
    assert b["items"][0]["dir"] == "青甘环线/敦煌"


def test_search_matches_camera(tmp_path, library) -> None:
    """用户常搜相机型号（"iPhone"/"vivo"），这也要能命中。"""
    st = _state(tmp_path, library)
    _seed(st, library)
    st.db.execute("UPDATE media SET camera = 'vivo iQOO Neo9' WHERE id = 1")
    b = _json(_call(st, "GET", "/api/search", query={"q": "iQOO"}))
    assert len(b["items"]) == 1


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
        ("GET", "/api/items/999999", {}, {"media_id": "999999"}),
        ("GET", "/api/items/1/raw", {}, {"media_id": "1"}),
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


def _json_body(obj) -> bytes:
    import json as _j

    return _j.dumps(obj).encode("utf-8")


def _seed_three(st, library) -> None:
    """造 3 张照片并索引（`_seed`/`_scan` 是本文件既有夹具的等价物）。"""
    from app.indexer import Indexer

    _img(library / "a.jpg")
    _img(library / "b.jpg")
    _img(library / "c.jpg")
    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))


# --------------------------------------------------------------------------
# root 参数名：`root` 与 `root_id` 都认，冲突才报错（审计 F12）
#
# 早先同一件事在不同接口用不同名字且互不认：timeline/folders/search/rescan
# 只认 `root`，uploads 认 `root`+`root_id`。传错名字不会报错，而是被
# 当成"没指定根" —— rescan 于是退化成**全库重扫**（真实库约 20 秒），
# 用户以为自己只扫了一个目录。静默降级比报错危险得多。
# --------------------------------------------------------------------------


def test_rescan_unknown_root_is_400_not_202(tmp_path, library) -> None:
    """未知的根必须 400，**绝不能** 202 + 静默降级成全库重扫。

    审计 F12 的核心危害就在这里：`{"root_id":"nope"}` 早先因为名字不被
    识别而走 "else: 扫全部" 分支，返回 202 `{"started":true,"root":null}`。
    调用方以为"只扫了 nope 那个根"，实际是全库重扫（真实库约 20 秒、
    4915 个文件的目录遍历与 stat）。这种"请求被接受了、但做的是另一件事"
    比直接报错危险得多，因为没有任何迹象提示用户去检查拼写。
    """
    fake = _FakeIndexer()
    st = _state(tmp_path, library, indexer=fake)

    for raw in (b'{"root_id":"nope"}', b'{"root":"nope"}'):
        r = _call(st, "POST", "/api/index/rescan", body=raw)
        assert r.status == 400, f"{raw!r} → {r.status} {_json(r)}"
        assert _json(r)["error"]["code"] == "E1001"
        assert "nope" in _json(r)["error"]["detail"]

    asyncio.run(asyncio.sleep(0))
    assert fake.calls == [], f"未知的根却真的触发了扫描：{fake.calls}"


def test_rescan_root_ids_are_consistent(tmp_path, library) -> None:
    """`root_id` 这个拼写在所有接口里都被真正识别（不只是不报错）。

    单看"返回 202"不足以证明别名生效 —— 静默降级的分支同样返回 202。
    所以这里断言的是**扫描真的发生在那个根上**（`fake.calls`），
    以及响应体里的 `root` 字段回填了它。
    """
    fake = _FakeIndexer()
    st = _state(tmp_path, library, indexer=fake)
    r = _call(st, "POST", "/api/index/rescan", body=b'{"root_id":"library"}')
    assert r.status == 202, _json(r)
    assert _json(r)["root"] == "library"
    asyncio.run(asyncio.sleep(0))
    assert fake.calls == ["library"]

    # 不给任何根 = 全库重扫，此时 fake 记的是 scan_all（"all"，不是某个根的 id）。
    fake.calls.clear()
    r = _call(st, "POST", "/api/index/rescan", body=b"{}")
    assert r.status == 202, _json(r)
    assert _json(r)["root"] is None
    asyncio.run(asyncio.sleep(0))
    assert fake.calls == ["all"], f"不给根时应对所有根扫一遍：{fake.calls}"


def test_rescan_accepts_root_id_alias(tmp_path, library) -> None:
    fake = _FakeIndexer()
    st = _state(tmp_path, library, indexer=fake)
    r = _call(st, "POST", "/api/index/rescan", body=b'{"root_id":"library"}')
    assert r.status == 202, _json(r)
    assert _json(r)["root"] == "library"
    asyncio.run(asyncio.sleep(0))
    assert fake.calls == ["library"], f"root_id 被忽略了，退化成了：{fake.calls}"


def test_rescan_root_query_string_is_honoured(tmp_path, library) -> None:
    fake = _FakeIndexer()
    st = _state(tmp_path, library, indexer=fake)
    r = _call(st, "POST", "/api/index/rescan", query={"root_id": "library"},
              body=b"{}")
    assert r.status == 202, _json(r)
    asyncio.run(asyncio.sleep(0))
    assert fake.calls == ["library"]


def test_rescan_conflicting_root_names_is_400(tmp_path, library) -> None:
    """两个名字给了不同的值 —— 一定是客户端写错了，必须报错而不是挑一个。

    挑一个的问题：挑哪个都不对，而且用户永远不知道另一个被忽略了。
    """
    st = _state(tmp_path, library, indexer=_FakeIndexer())
    r = _call(st, "POST", "/api/index/rescan",
              body=b'{"root":"library","root_id":"other"}')
    assert r.status == 400, _json(r)
    assert _json(r)["error"]["code"] == "E1001"


def test_timeline_accepts_root_id_alias(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    r = _call(st, "GET", "/api/timeline", query={"root_id": "library"})
    assert r.status == 200, _json(r)


def test_folders_accepts_root_id_alias(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    r = _call(st, "GET", "/api/folders", query={"root_id": "library"})
    assert r.status == 200, _json(r)
    assert _json(r)["root"] == "library"


def test_search_accepts_root_id_alias(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    r = _call(st, "GET", "/api/search", query={"root_id": "library", "q": "a"})
    assert r.status == 200, _json(r)


# --------------------------------------------------------------------------
# 删除响应形状（审计 F9）
#
# 实现在 2026-10-17 的审计里被判"与 docs/04 不一致"，结论是改文档：
# `deleted` 是**计数**不是 ID 数组，且行保留（missing=1）而不是删行。
# 这里把形状钉住，免得下一个人又照着旧文档改回去。
# --------------------------------------------------------------------------


def test_delete_response_is_count_not_id_list(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    _seed_three(st, library)
    mid = st.db.scalar("SELECT id FROM media ORDER BY id LIMIT 1")

    r = _call(st, "POST", "/api/items/delete", body=_json_body({"ids": [mid]}))
    assert r.status == 200, _json(r)
    body = _json(r)
    assert body == {"deleted": 1, "failed": []}, body
    # 明确断言"不是数组" —— 旧文档承诺的是 array<integer>
    assert isinstance(body["deleted"], int)


def test_delete_keeps_row_and_marks_missing(tmp_path, library) -> None:
    """行必须留着。"最近删除"全靠它，删行等于把这个功能的依据抹掉。"""
    st = _state(tmp_path, library)
    _seed_three(st, library)
    mid = st.db.scalar("SELECT id FROM media ORDER BY id LIMIT 1")
    r = _call(st, "POST", "/api/items/delete", body=_json_body({"ids": [mid]}))
    assert r.status == 200

    row = st.db.query_one("SELECT missing, rel_path FROM media WHERE id = ?", (mid,))
    assert row is not None, "删除后行不能消失"
    assert row["missing"] == 1
    assert st.db.scalar("SELECT COUNT(*) FROM media") == 3


def test_delete_unknown_id_goes_to_failed_not_4xx(tmp_path, library) -> None:
    """批量删除里一项无效不该让整批失败。"""
    st = _state(tmp_path, library)
    _seed_three(st, library)
    mid = st.db.scalar("SELECT id FROM media ORDER BY id LIMIT 1")

    r = _call(st, "POST", "/api/items/delete",
              body=_json_body({"ids": [mid, 999999]}))
    assert r.status == 200, _json(r)
    body = _json(r)
    assert body["deleted"] == 1
    assert body["failed"] == [{"id": 999999, "error": "not_found"}]


def test_delete_is_idempotent_and_still_counts(tmp_path, library) -> None:
    """重复删除：文件已不在，仍算成功（行已是 missing=1）。"""
    st = _state(tmp_path, library)
    _seed_three(st, library)
    mid = st.db.scalar("SELECT id FROM media ORDER BY id LIMIT 1")

    first = _json(_call(st, "POST", "/api/items/delete",
                        body=_json_body({"ids": [mid]})))
    second = _json(_call(st, "POST", "/api/items/delete",
                         body=_json_body({"ids": [mid]})))
    assert first == {"deleted": 1, "failed": []}
    assert second == {"deleted": 1, "failed": []}


# ---------------------------------------------------------------------------
# HTTP 层路径穿越（docs/07 §7.1 的 TC-SANDBOX-SEC-081~090）
# ---------------------------------------------------------------------------


def _traversal_urls() -> list[tuple[str, str, str]]:
    """(编号, URL, 期望状态)。"""
    return [
        ("SEC-081", "/api/items/..%2f..%2fsecret.txt/thumb", "400"),
        ("SEC-082", "/api/items/%2e%2e%2fsecret.txt/thumb", "400"),
        ("SEC-083", "/api/items/a.jpg%00.png/thumb", "400"),
        ("SEC-084", "/api/items/....//....//secret.txt/thumb", "404"),
        ("SEC-085", "/api/items/%252e%252e%252fsecret.txt/thumb", "400"),
        ("SEC-086", "/api/items/C:\\Windows\\win.ini/thumb", "400"),
        ("SEC-087", "/api/items/\\\\?\\C:\\Windows/thumb", "400"),
        ("SEC-088", "/api/items/CON/thumb", "400"),
        ("SEC-089", "/api/items/a.jpg:ads/thumb", "400"),
        ("SEC-090", "/api/items/./a.jpg/thumb", "404"),
    ]


@pytest.mark.parametrize(
    "case_id,url,expected",
    _traversal_urls(),
    ids=[c[0] for c in _traversal_urls()],
)
def test_http_layer_traversal_cannot_reach_outside_files(
    tmp_path, library, case_id: str, url: str, expected: str
) -> None:
    """docs/07 §7.1 SEC-081~090：这些 URL 一律**不能**返回库外文件。

    ## 它们实际挡在哪一层（与文档的差异）

    文档写的是"沙箱拦住这些 URL"。实测不是：这些 URL 里的 `media_id`
    段**根本不是整数**，所以它们在 `ctx.param_int()` 就被
    `400 E1001 路径参数 media_id 必须是整数` 拒掉，**走不到沙箱**。

    这是**分层正确**的结果，不是缺陷：
    * `/api/items/{media_id}` 的 media_id 是数据库主键，不是路径 ——
      用整数解析拒绝一切非数字形态，比让它们进沙箱再判要早一层；
    * SEC-084 / SEC-090 拿到的是 **404** 而不是 400，因为它们的
      `media_id` 段是 `....` 与 `.`：路径段数与路由模式对不上，
      直接落到 fallback 404。两种情况都**没有触到文件系统**，
      这才是本组真正要钉的性质。

    文档判据写"`400` 或 `404`"，正是承认了这个不确定性 —— 本用例把
    各自的实际值钉住（实现变了就会红），同时断言响应体里不出现库外
    路径，那一条才是安全性的实质。
    """
    st = _state(tmp_path, library)
    _seed_three(st, library)

    r = _call(st, "GET", url)
    assert str(r.status) == expected, f"{case_id} 期望 {expected}，实际 {r.status}"

    body = _body(r)
    assert b"secret" not in body
    assert b"win.ini" not in body
    # 响应体绝不能回显解析出的绝对路径
    assert str(library).encode() not in body


def test_http_layer_traversal_never_opens_a_file_outside(tmp_path, library) -> None:
    """同一组用例的**行为**断言：库外放一个陷阱文件，确认它从没被读到。

    URL 层面的状态码可能随路由实现变化，但"库外的字节有没有被读出来"
    是稳定的安全性质。陷阱文件内容足够独特，只要响应体里出现它就说明
    真的越界了 —— 这比断言 400/404 更接近我们真正关心的事。
    """
    st = _state(tmp_path, library)
    _seed_three(st, library)

    marker = b"OUTSIDE-MARKER-7f3a9c"
    (tmp_path / "secret.txt").write_bytes(marker)
    (library.parent / "secret.txt").write_bytes(marker)

    for case_id, url, _expected in _traversal_urls():
        r = _call(st, "GET", url)
        assert marker not in _body(r), f"{case_id} 把库外内容读出来了：{url}"


@pytest.mark.parametrize("suffix", ["", "/thumb", "/preview", "/raw"])
def test_every_item_endpoint_rejects_non_integer_media_id(
    tmp_path, library, suffix: str
) -> None:
    """`item` / `thumb` / `preview` / `raw` 四个端点都必须拒绝非整数 id。

    只测 thumb 会漏掉"某天有人给 raw 单独写了个解析函数"。四个端点共用
    `ctx.param_int()`，这条把它们钉在一起。
    """
    st = _state(tmp_path, library)
    _seed_three(st, library)
    r = _call(st, "GET", f"/api/items/..%2f..%2fsecret.txt{suffix}")
    assert r.status == 400
    assert _json(r)["error"]["code"] == "E1001"


# --------------------------------------------------------------------------
# 缩略图缓存统计：一次遍历 + 60 秒 memo（ISSUE-034 ①）
# --------------------------------------------------------------------------


def _seed_thumb_cache(st: AppState, count: int, *, size: int = 512) -> int:
    """在缩略图缓存目录里放 `count` 个 `.webp`，返回它们的总字节数。

    多层子目录是刻意的：真实布局是 `<thumbs>/<root_id>/<两级散列>/<尺寸>.webp`，
    只平铺一层的话"没递归进去"这种 bug 测不出来。
    """
    root = st.cfg.data_path("thumbs")
    for i in range(count):
        p = root / f"aa{i % 3:02x}" / f"bb{i % 5:02x}" / f"cc{i:04d}.webp"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\0" * size)
    return count * size


def test_system_info_reports_thumb_cache_bytes_and_files(
    tmp_path: Path, library: Path
) -> None:
    """`/api/system/info` 的两个缓存数字必须与磁盘一致（合并遍历后没算错）。

    合并成一次遍历时最容易犯的错是"把缓存目录里的**文件数**写成**目录数**"
    或"只数了第一层" —— 数值本身不对，但请求照样 200，不写这条就没人发现。
    """
    st = _state(tmp_path, library)
    total = _seed_thumb_cache(st, 7, size=100)

    info = _json(_call(st, "GET", "/api/system/info"))
    assert info["thumbs"]["cache_files"] == 7
    assert info["thumbs"]["cache_bytes"] == total

    # stats 用的是同一个统计，数字必须一致（它们是同一个 memo 的两列）
    stats_payload = _json(_call(st, "GET", "/api/system/stats"))
    assert stats_payload["thumbs"]["cache_files"] == 7
    assert stats_payload["thumbs"]["cache_bytes"] == total


def test_thumb_cache_scan_happens_once_per_ttl(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TTL 内第二次请求不允许再遍历目录。

    这就是这条审计项的核心：原本 `info` 与 `stats` 每次各跑两遍
    `rglob + stat`，10 万张照片 = 一次请求 40 万次系统调用，而且全在
    事件循环里。判据不看实现：数"磁盘遍历发生了几次"。
    """
    st = _state(tmp_path, library)
    _seed_thumb_cache(st, 4, size=50)

    walks: list[str] = []
    real_scan = system._scan_thumb_cache

    def _spy(root: Path) -> tuple[int, int]:
        walks.append(str(root))
        return real_scan(root)

    monkeypatch.setattr(system, "_scan_thumb_cache", _spy)

    for _ in range(3):
        _json(_call(st, "GET", "/api/system/info"))
    _json(_call(st, "GET", "/api/system/stats"))
    assert len(walks) == 1, f"60 秒内应该只遍历一次，实际 {len(walks)} 次"

    # 过期后必须重算（memo 不能变成"永远不再更新"的假数据）
    monkeypatch.setattr(system, "_THUMB_CACHE_TTL", 0.0)
    _json(_call(st, "GET", "/api/system/info"))
    assert len(walks) == 2


def test_thumb_cache_scan_is_shared_by_concurrent_requests(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发请求不能各自跑一遍遍历。

    memo 只挡"算完之后"的重复调用；冷启动时同时涌入的 N 个请求会**同时**
    发现 memo 为空 —— 没有锁的话它们各跑一遍（正是要消除的放大效应）。
    这里放 8 个并发，要求磁盘遍历仍然只发生 1 次。
    """
    st = _state(tmp_path, library)
    _seed_thumb_cache(st, 3, size=64)

    walks: list[str] = []
    real_scan = system._scan_thumb_cache

    def _spy(root: Path) -> tuple[int, int]:
        walks.append(str(root))
        time.sleep(0.01)          # 放大竞态窗口：不睡的话第一个可能已经算完了
        return real_scan(root)

    monkeypatch.setattr(system, "_scan_thumb_cache", _spy)

    async def _fan_out() -> list[dict]:
        ctxs = [_ctx(st, "GET", "/api/system/info") for _ in range(8)]
        replies = await asyncio.gather(*(system.info(c) for c in ctxs))
        return [_json(r) for r in replies]

    payloads = asyncio.run(_fan_out())
    assert len(walks) == 1, f"8 个并发请求应该只遍历一次，实际 {len(walks)} 次"
    assert all(p["thumbs"]["cache_files"] == 3 for p in payloads)


# --------------------------------------------------------------------------
# 缩略图兜底路径：单边上限 + 全局串行（ISSUE-034）
# --------------------------------------------------------------------------


def test_oversized_thumb_is_marked_failed_without_decoding(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单边超 20000 像素的图：标 `failed` + 回占位图，而不是硬解码。

    判据是**行为**而不是异常类型：请求本身必须成功拿到一张图（占位图），
    库里这一行必须变成 `failed`，而且下一次请求不能再尝试解码 —— 否则
    用户每刷新一次列表就会重新吃一遍峰值内存。
    """
    from app import thumbs as thumbs_mod

    st = _state(tmp_path, library)          # thumbs=None ⇒ 走兜底生成路径
    _img(library / "wide.png", size=(thumbs_mod.MAX_IMAGE_EDGE + 1, 8), fmt="PNG")
    asyncio.run(_index_all(st))
    mid = int(st.db.scalar("SELECT id FROM media WHERE file_name = ?", ("wide.png",)))

    calls: list[Path] = []
    real_call = browse._call_generate

    def _spy(src, dst, size, orientation):
        calls.append(Path(dst))
        return real_call(src, dst, size, orientation)

    monkeypatch.setattr(browse, "_call_generate", _spy)

    first = _call(st, "GET", f"/api/items/{mid}/thumb")
    assert first.status == 200
    assert first.headers["Content-Type"] == "image/webp"
    # 占位图不缓存：用户修好文件后刷新就能看到真图
    assert first.headers["Cache-Control"] == "no-store"
    assert len(_body(first)) > 0

    assert st.db.scalar("SELECT thumb_state FROM media WHERE id = ?", (mid,)) == "failed"
    assert len(calls) == 1, f"超限图应该只尝试一次，实际 {len(calls)} 次"

    # 第二次请求：不再解码，直接报"这张图无法生成缩略图"
    second = _call(st, "GET", f"/api/items/{mid}/thumb")
    assert second.status == 500
    assert _json(second)["error"]["code"] == "E3004"
    assert len(calls) == 1, f"已判失败的图不能再解码，实际 {len(calls)} 次"


def test_fallback_thumb_generation_is_serialized(
    tmp_path: Path, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """兜底路径的解码必须**全局串行**，不能绕开 4 并发闸。

    原来这里走的是 `run_in_executor(None, ...)`（asyncio 默认线程池，
    `min(32, cpu+4)` 个线程），与 `ThumbEngine._sem` 的 4 并发完全无关 ——
    4 个引擎任务 + N 个兜底任务可以同时解码，一张 48MP 图解码后是 150MB，
    几路并发就能把家用小主机拖进 swap。判据：同时在解码的峰值必须是 1。
    """
    st = _state(tmp_path, library)
    _seed_three(st, library)
    ids = [int(r["id"]) for r in st.db.query("SELECT id FROM media ORDER BY id")]

    active: list[int] = []
    peak = [0]
    real_call = browse._call_generate

    def _slow(src, dst, size, orientation):
        active.append(1)
        peak[0] = max(peak[0], len(active))
        try:
            time.sleep(0.05)      # 放大窗口：不睡的话第一个可能已经跑完了
            return real_call(src, dst, size, orientation)
        finally:
            active.pop()

    monkeypatch.setattr(browse, "_call_generate", _slow)

    async def _fan_out() -> list:
        ctxs = [_ctx(st, "GET", f"/api/items/{mid}/thumb",
                     params={"media_id": str(mid)}) for mid in ids]
        return await asyncio.gather(*(browse.get_thumb(c) for c in ctxs))

    replies = asyncio.run(_fan_out())
    assert [r.status for r in replies] == [200] * len(ids)
    assert peak[0] == 1, f"兜底解码必须串行，实际峰值 {peak[0]} 路并发"


async def _index_all(st: AppState) -> None:
    from app.indexer import Indexer

    await Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library"))


# --------------------------------------------------------------------------
# /raw 的条件请求（ETag + 304）
#
# 场景：在外面用 5G 看一张 10 MB 的原图，第一次要 40 秒。第二次看同一张时
# 浏览器手里已经有字节了，只要服务端给出 ETag，它只需发一个 `If-None-Match`
# 复验 —— 命中就 304（几百字节、一个往返），不必把 10 MB 再走一遍上行。
#
# 这条链路上"上行只有 2 Mbps"是硬约束，所以"不发第二次"比"发得更快"重要。
# --------------------------------------------------------------------------

def test_raw_returns_etag_and_answers_if_none_match_with_304(tmp_path, library) -> None:
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]

    r1 = _call(st, "GET", f"/api/items/{mid}/raw", params={"media_id": str(mid)})
    assert r1.status == 200
    etag = r1.headers.get("ETag")
    assert etag, "原图必须带 ETag，否则每次重看都要重下整张"
    _body(r1)  # 消费掉流，模拟真实读完

    # 复验：带回来同一个 ETag → 304，且**没有正文**
    r2 = _call(st, "GET", f"/api/items/{mid}/raw",
               headers={"If-None-Match": etag}, params={"media_id": str(mid)})
    assert r2.status == 304, f"同一张原图复验应当 304，实际 {r2.status}"
    assert _body(r2) == b""
    assert r2.headers.get("ETag") == etag


def test_raw_range_request_is_never_answered_with_304(tmp_path, library) -> None:
    """带 Range 的请求要的是"某一段字节"，回 304 会让客户端以为那段拿到了。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]

    etag = _call(st, "GET", f"/api/items/{mid}/raw",
                 params={"media_id": str(mid)}).headers["ETag"]
    r = _call(st, "GET", f"/api/items/{mid}/raw",
              headers={"Range": "bytes=0-15", "If-None-Match": etag},
              params={"media_id": str(mid)})
    assert r.status == 206, f"Range + If-None-Match 必须给 206，实际 {r.status}"
    assert len(_body(r)) == 16


def test_raw_etag_changes_when_the_source_file_changes(tmp_path, library) -> None:
    """照片被替换（同名覆盖）后，旧 ETag 必须失效，否则手机会一直看旧图。"""
    st = _state(tmp_path, library)
    ids = _seed(st, library)
    mid = ids[0]
    row = st.db.query_one("SELECT rel_path FROM media WHERE id=?", (mid,))
    src = library / row["rel_path"]

    etag_old = _call(st, "GET", f"/api/items/{mid}/raw",
                     params={"media_id": str(mid)}).headers["ETag"]

    # 换个内容 + 换个 mtime（真实替换至少会改其中一样）
    _img(src, size=(120, 90), color=(10, 20, 30))
    st.db.execute("UPDATE media SET mtime = mtime + 5, size = ? WHERE id = ?",
                  (src.stat().st_size, mid))

    r = _call(st, "GET", f"/api/items/{mid}/raw",
              headers={"If-None-Match": etag_old}, params={"media_id": str(mid)})
    assert r.status == 200, "源文件变了就不能再回 304（否则手机永远看旧图）"
    assert r.headers["ETag"] != etag_old