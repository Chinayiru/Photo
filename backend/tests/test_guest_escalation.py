"""游客越权回归（BUG-2）：**按 media_id 取资源**的每个入口都必须归属校验。

## 这一批守的是什么

`/api/timeline`、`/api/search` 这类"列表"接口早就按 `visible_root_ids` 过滤了，
所以游客在界面里看不到主人家照片。但"列表里没有"不等于"取不到"：每一个
`/api/items/{id}/...` 都是一个可以**逐号试探**的入口 —— 只要其中一个忘了
校验归属，游客就能拿着主库的 media_id 把整库照片一张张拉下来。

要守的是两条：

1. **每个**按 id 取资源的入口都校验（`get_item` / `thumb` / `preview` / `raw` /
   `stream` / `stream-ticket` / 转码三个）。少一处就是越权。
2. 拒绝的**形态**必须与"这个 id 不存在"不可区分（都是 404）。返回 403 等于
   确认"id 存在"，游客扫一遍就能把主人的 id 空间探出来。

另外守一条容易漏的：游客换播放票 `/stream-ticket` 必须在**签发前**判归属 ——
票是"没有 Bearer 也能过闸"的凭证，签发后再判就晚了。
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
from app.handlers import browse, transcode  # noqa: E402
from app.state import AppState  # noqa: E402


# ------------------------------------------------------------------ 夹具

def _cfg(tmp_path: Path) -> Config:
    owner = tmp_path / "owner"
    owner.mkdir(exist_ok=True)
    share = tmp_path / "share"
    share.mkdir(exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Config({
        "app": {"mode": "readwrite"},
        "roots": [
            {"id": "library", "path": str(owner), "mode": "readwrite"},
            {"id": "share", "path": str(share), "mode": "readwrite",
             "visibility": "guest"},
        ],
        "thumbs": {"sizes": [320, 1600]},
        "cloud": {"enabled": True, "base_url": "https://example.com",
                  "device_id": "a" * 32},
    }, path=data / "config.json", data_dir=data)


def _state(tmp_path: Path, *, with_guest: bool = True) -> AppState:
    cfg = _cfg(tmp_path)
    if not with_guest:
        # 模拟"主人还没开游客访问"：可见根为空，任何按 id 的请求都该 404。
        object.__setattr__(cfg, "_roots", [r for r in cfg.roots if r.id != "share"])
    db = Database(cfg.db_path)
    db.init()
    return AppState(cfg, db)


def _seed(st: AppState) -> tuple[int, int]:
    """主库一张、游客区一张。返回 (owner_id, guest_id)。"""
    now = 1_700_000_000.0
    ids = []
    for root_id, name, uploader in (
        ("library", "主人的照片.jpg", ""),
        ("share", "游客的照片.jpg", "c" * 32),
    ):
        cur = st.db.execute(
            "INSERT INTO media(root_id, rel_path, file_name, ext, kind, size,"
            " mtime, taken_at, thumb_state, uploader_client_id)"
            " VALUES (?, ?, ?, 'jpg', 'image', 10, ?, ?, 'ok', ?)",
            (root_id, name, name, now, now, uploader),
        )
        ids.append(int(cur.lastrowid))
    return ids[0], ids[1]


def _ctx(st: AppState, *, role: str, mid: int | None = None,
         query: dict | None = None, method: str = "GET",
         path: str = "/api/items/x") -> Ctx:
    class _Stream:
        def __aiter__(self):
            async def _empty():
                if False:      # pragma: no cover
                    yield b""
            return _empty()

    params = {} if mid is None else {"media_id": str(mid)}
    ctx = Ctx(method=method, path=path, query=query or {}, headers={},
              body=_Stream(), params=params, app=st)
    ctx.state["client"] = {"id": "c" * 32, "name": "guest", "role": role}
    return ctx


def _call(fn, ctx: Ctx):
    return asyncio.run(fn(ctx))


# ------------------------------------------------- 按 id 取资源：全部 404

BY_ID = [
    ("详情", browse.get_item),
    ("缩略图", browse.get_thumb),
    ("预览图", browse.get_preview),
    ("原图", browse.get_raw),
    ("视频流", browse.stream_video),
    ("播放票", browse.stream_ticket),
    ("转码起转", transcode.start),
    ("转码状态", transcode.status),
    ("转码流", transcode.stream),
]


@pytest.mark.parametrize("label,handler", BY_ID, ids=[b[0] for b in BY_ID])
def test_guest_cannot_reach_owner_media_by_id(tmp_path, label, handler) -> None:
    st = _state(tmp_path)
    owner_id, _ = _seed(st)
    ctx = _ctx(st, role="guest", mid=owner_id,
               query={"size": ["320"]}, method="POST")

    with pytest.raises(AppError) as ei:
        _call(handler, ctx)
    assert ei.value.status == 404, f"{label} 应该给 404（与不存在不可区分）"


@pytest.mark.parametrize("label,handler", BY_ID, ids=[b[0] for b in BY_ID])
def test_missing_id_and_forbidden_id_look_the_same(tmp_path, label, handler) -> None:
    """游客拿一个**根本不存在**的 id 与拿主人家的 id，响应必须一模一样。"""
    st = _state(tmp_path)
    owner_id, _ = _seed(st)

    with pytest.raises(AppError) as forbidden:
        _call(handler, _ctx(st, role="guest", mid=owner_id,
                            query={"size": ["320"]}, method="POST"))
    with pytest.raises(AppError) as missing:
        _call(handler, _ctx(st, role="guest", mid=999_999,
                            query={"size": ["320"]}, method="POST"))

    assert forbidden.value.status == missing.value.status == 404
    assert forbidden.value.code == missing.value.code


def test_guest_can_reach_its_own_upload(tmp_path) -> None:
    """反向验证：游客自己传的那张必须能打开，否则"隔离"就变成了"全禁"。"""
    st = _state(tmp_path)
    _, guest_id = _seed(st)
    reply = _call(browse.get_item, _ctx(st, role="guest", mid=guest_id))
    assert reply.status == 200


def test_owner_is_unaffected(tmp_path) -> None:
    """主人的行为不许变：同一批入口照旧能取主库那张。"""
    st = _state(tmp_path)
    owner_id, _ = _seed(st)
    reply = _call(browse.get_item, _ctx(st, role="owner", mid=owner_id))
    assert reply.status == 200


def test_guest_without_a_guest_root_sees_nothing(tmp_path) -> None:
    """主人干脆没开游客根时，游客的任何按 id 请求也必须 404。"""
    st = _state(tmp_path, with_guest=False)
    owner_id, _ = _seed(st)
    with pytest.raises(AppError) as ei:
        _call(browse.get_item, _ctx(st, role="guest", mid=owner_id))
    assert ei.value.status == 404


# ------------------------------------------------------- 列表/搜索/目录

def test_guest_timeline_only_contains_the_guest_root(tmp_path) -> None:
    st = _state(tmp_path)
    _seed(st)
    reply = _call(browse.timeline,
                  _ctx(st, role="guest", path="/api/timeline"))
    items = json.loads(reply.body)["items"]
    assert items, "游客区那张应该在列表里"
    assert {it["root"] for it in items} == {"share"}


def test_guest_cannot_ask_for_the_owner_root(tmp_path) -> None:
    st = _state(tmp_path)
    _seed(st)
    with pytest.raises(AppError) as ei:
        _call(browse.timeline,
              _ctx(st, role="guest", path="/api/timeline",
                   query={"root": ["library"]}))
    assert ei.value.status == 404


def test_guest_folder_browse_cannot_enter_the_owner_root(tmp_path) -> None:
    st = _state(tmp_path)
    _seed(st)
    with pytest.raises(AppError) as ei:
        _call(browse.folders,
              _ctx(st, role="guest", path="/api/folders",
                   query={"root": ["library"]}))
    assert ei.value.status == 404


def test_guest_search_never_returns_owner_files(tmp_path) -> None:
    st = _state(tmp_path)
    _seed(st)
    reply = _call(browse.search,
                  _ctx(st, role="guest", path="/api/search",
                       query={"q": ["主人的照片"]}))
    assert json.loads(reply.body)["items"] == []
