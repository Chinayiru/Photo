"""游客访问媒体：`/thumb`、`/preview`、`/raw`、`/stream`、`/stream-ticket`。

## 这一批守的是什么

游客只看得见自己传的照片（`media.uploader_client_id`），这条判据由
`handlers/common.check_media_visible()` 统一执行。而 `/thumb`、`/preview`、
`/raw`、`/stream`、`/stream-ticket` 为了少读几列，各自的 SELECT 只挑了
路径/尺寸/kind 之类的字段，**没有 `uploader_client_id`**。

结果：授权判据读一个不存在的列 → `IndexError: No item with that key`
→ 游客点开自己的照片一律 **500 E3001**（列表看得见、每张都打不开）。
主人不受影响（`is_guest()` 为假时直接 return），所以这个洞只在游客视角
出现，而游客是"别人把链接发给你、你打开看一眼"的那条路径 ——
最该好用的那条路。

守两条：
1. 授权判据不依赖调用方选了哪几列（缺列就按主键回查，见 `common.uploader_of`）；
2. 五个端点在游客视角下对"自己的照片"都返回正常，对"别人的照片"一律 404。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.core import Ctx  # noqa: E402
from app.db import Database  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.handlers import browse  # noqa: E402
from app.handlers import common  # noqa: E402
from app.state import AppState  # noqa: E402

GUEST_A = "c" * 32
GUEST_B = "d" * 32


# --------------------------------------------------------------------- 夹具

def _state(tmp_path: Path) -> AppState:
    owner = tmp_path / "owner"
    owner.mkdir(exist_ok=True)
    guest = tmp_path / "guest"
    guest.mkdir(exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    cfg = Config({
        "app": {"mode": "readwrite"},
        "roots": [
            {"id": "library", "path": str(owner), "mode": "readwrite"},
            {"id": "share", "path": str(guest), "mode": "readwrite",
             "visibility": "guest"},
        ],
        "thumbs": {"sizes": [320, 1600]},
        "cloud": {"enabled": False},
    }, path=data / "config.json", data_dir=data)
    db = Database(cfg.db_path)
    db.init()
    return AppState(cfg, db)


def _jpeg(path: Path, size: tuple[int, int] = (640, 480)) -> None:
    """写一张真 JPEG（缩略图/预览要走 Pillow 解码，假字节过不去）。"""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (120, 60, 200)).save(path, "JPEG", quality=80)


def _insert(st: AppState, root_id: str, rel_path: str, uploader: str) -> int:
    now = 1_700_000_000.0
    cur = st.db.execute(
        "INSERT INTO media(root_id, rel_path, file_name, ext, kind, size,"
        " mtime, taken_at, thumb_state, uploader_client_id)"
        " VALUES (?, ?, ?, 'jpg', 'image', ?, ?, ?, 'pending', ?)",
        (root_id, rel_path, Path(rel_path).name,
         (st.cfg.root_by_id(root_id).path / rel_path).stat().st_size,
         now, now, uploader),
    )
    return int(cur.lastrowid)


def _ctx(st: AppState, *, role: str, media_id: int,
         query: dict[str, list[str]] | None = None,
         path: str = "/api/items/0/raw") -> Ctx:
    ctx = Ctx(method="GET", path=path, query=query or {}, headers={},
              body=None, params={"media_id": str(media_id)}, app=st)
    ctx.state["client"] = {"id": GUEST_A, "name": "t", "role": role}
    return ctx


async def _drain(reply) -> bytes:
    body = reply.body
    if body is None:
        return b""
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    out = bytearray()
    async for chunk in body:      # type: ignore[union-attr]
        out += chunk
    return bytes(out)


def _call(handler, ctx) -> tuple[int, bytes]:
    reply = asyncio.run(handler(ctx))
    return reply.status, asyncio.run(_drain(reply))


# ------------------------------------------------------- 授权判据本身（核心回归）

def test_narrow_row_without_uploader_column_still_authorises(tmp_path: Path) -> None:
    """行里没有 `uploader_client_id` 时，游客对自己照片的判定必须仍然成立。

    这就是线上 500 的根因：`/stream`、`/stream-ticket` 的 SELECT 只取
    `id, root_id, kind, missing`，`row["uploader_client_id"]` 直接抛
    `IndexError`。授权判据不能依赖"调用方碰巧选了哪几列"。
    """
    st = _state(tmp_path)
    _jpeg(st.cfg.root_by_id("share").path / "mine.jpg")
    mid = _insert(st, "share", "mine.jpg", GUEST_A)

    narrow = st.db.query_one(
        "SELECT id, root_id, kind, missing FROM media WHERE id = ?", (mid,))
    assert "uploader_client_id" not in narrow.keys(), "夹具应当复现缺列的形态"

    # 自己的 → 放行（不抛）
    common.check_media_visible(_ctx(st, role="guest", media_id=mid), st, narrow)


def test_narrow_row_still_hides_other_guests_media(tmp_path: Path) -> None:
    """缺列时的回查不能把"别人的照片"放过：仍然 404，且不带 id。"""
    st = _state(tmp_path)
    _jpeg(st.cfg.root_by_id("share").path / "other.jpg")
    mid = _insert(st, "share", "other.jpg", GUEST_B)

    narrow = st.db.query_one(
        "SELECT id, root_id, kind, missing FROM media WHERE id = ?", (mid,))
    with pytest.raises(AppError) as ei:
        common.check_media_visible(_ctx(st, role="guest", media_id=mid), st, narrow)
    assert ei.value.status == 404
    assert "other.jpg" not in json.dumps(getattr(ei.value, "detail", ""),
                                         ensure_ascii=False)


def test_owner_is_unaffected_by_the_narrow_row(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _jpeg(st.cfg.root_by_id("library").path / "own.jpg")
    mid = _insert(st, "library", "own.jpg", "")
    narrow = st.db.query_one(
        "SELECT id, root_id, kind, missing FROM media WHERE id = ?", (mid,))
    common.check_media_visible(_ctx(st, role="owner", media_id=mid), st, narrow)


def test_uploader_of_survives_a_row_that_has_vanished(tmp_path: Path) -> None:
    """回查也查不到（行已被删）时返回空串，而不是抛异常。"""
    st = _state(tmp_path)
    assert common.uploader_of(st, {"id": 999999}) == ""


# ------------------------------------------------------- 五个端点在游客视角

def test_guest_can_fetch_raw_of_their_own_photo(tmp_path: Path) -> None:
    st = _state(tmp_path)
    src = st.cfg.root_by_id("share").path / "mine.jpg"
    _jpeg(src)
    mid = _insert(st, "share", "mine.jpg", GUEST_A)

    status, body = _call(browse.get_raw, _ctx(st, role="guest", media_id=mid))
    assert status == 200, "游客打不开自己传的照片就是这次 500 的直接症状"
    assert body == src.read_bytes()


def test_guest_can_fetch_thumb_of_their_own_photo(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _jpeg(st.cfg.root_by_id("share").path / "mine.jpg")
    mid = _insert(st, "share", "mine.jpg", GUEST_A)

    status, body = _call(
        browse.get_thumb,
        _ctx(st, role="guest", media_id=mid, query={"size": ["320"]},
             path=f"/api/items/{mid}/thumb"))
    assert status == 200
    assert body[:4] == b"RIFF", "缩略图应当是 WebP（RIFF 容器）"


def test_guest_can_fetch_preview_of_their_own_photo(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _jpeg(st.cfg.root_by_id("share").path / "mine.jpg")
    mid = _insert(st, "share", "mine.jpg", GUEST_A)

    status, body = _call(
        browse.get_preview,
        _ctx(st, role="guest", media_id=mid, query={"size": ["1600"]},
             path=f"/api/items/{mid}/preview"))
    assert status == 200
    assert len(body) > 0


def test_guest_stream_on_an_image_says_not_a_video_not_500(tmp_path: Path) -> None:
    """先判归属再判类型：图片走 `/stream` 是 400「不是视频」，不是 500。"""
    st = _state(tmp_path)
    _jpeg(st.cfg.root_by_id("share").path / "mine.jpg")
    mid = _insert(st, "share", "mine.jpg", GUEST_A)

    with pytest.raises(AppError) as ei:
        asyncio.run(browse.stream_video(_ctx(st, role="guest", media_id=mid)))
    assert ei.value.status == 400


def test_guest_can_mint_a_stream_ticket_for_their_own_photo(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _jpeg(st.cfg.root_by_id("share").path / "mine.jpg")
    mid = _insert(st, "share", "mine.jpg", GUEST_A)

    status, body = _call(
        browse.stream_ticket, _ctx(st, role="guest", media_id=mid,
                                   path=f"/api/items/{mid}/stream-ticket"))
    assert status == 200
    assert json.loads(body)["ticket"]


def test_guest_cannot_mint_a_ticket_for_the_owners_photo(tmp_path: Path) -> None:
    """播放票是"无 Bearer 也能过闸"的凭证，签发前必须判归属。"""
    st = _state(tmp_path)
    _jpeg(st.cfg.root_by_id("library").path / "own.jpg")
    mid = _insert(st, "library", "own.jpg", "")

    with pytest.raises(AppError) as ei:
        asyncio.run(browse.stream_ticket(_ctx(st, role="guest", media_id=mid)))
    assert ei.value.status == 404
