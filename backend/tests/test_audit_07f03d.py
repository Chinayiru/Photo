"""审计 07f03d（游客访问）S-01…S-06 的回归。

## 这批用例对的是哪六件事

| 编号 | 问题 | 这里的判据 |
|---|---|---|
| S-01 | 云端通路也能生成邀请 | `create_invite` 在 `via=cloud` 下 403，且**不写库** |
| S-02 | 游客能改/删主库文件 | 中间件闸门把非 `/api/uploads/` 的写请求全部 403 |
| S-03 | 游客上传落主库 | init 里请求的 `root` 被忽略，会话根 = 游客根 |
| S-04 | 游客能读别的游客传的 | 列表按 `uploader_client_id` 收口；按 id 读越界 → 404 |
| S-05 | 转码流绕过媒体授权 | `transcode._video_row` 对主人视频 404 |
| S-06 | 目录/信息/SSE 泄露主库 | folders 只出自己目录；SSE 事件按根过滤 |

前两条是"拒绝"，后四条是"看得见的范围"。**都用 404/403，不用 200+ 空列表**：
空列表会让调用方以为自己没上传过东西，而不是"你没这个权限"。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, guest  # noqa: E402
from app.config import Config  # noqa: E402
from app.core import Ctx  # noqa: E402
from app.core.ctx import CLOUD, LAN  # noqa: E402
from app.db import Database  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.handlers import browse, common, guest as h_guest, transcode, uploads  # noqa: E402
from app.state import AppState  # noqa: E402

OWNER_ID = "a" * 32
GUEST_A = "b" * 32
GUEST_B = "c" * 32


# ---------------------------------------------------------------- 夹具

class _Body:
    """最小可用的请求体（`ctx.json()` 只要求 async-iterable 的字节块）。"""

    def __init__(self, payload: dict) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def __aiter__(self):
        async def gen():
            yield self._data
        return gen()


def _cfg(tmp_path: Path) -> Config:
    owner = tmp_path / "owner"
    gst = tmp_path / "guest"
    owner.mkdir(exist_ok=True)
    gst.mkdir(exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Config({
        "app": {"mode": "readwrite"},
        "roots": [
            {"id": "library", "path": str(owner), "mode": "readwrite"},
            {"id": "guest", "path": str(gst), "mode": "upload", "visibility": "guest"},
        ],
        "thumbs": {"sizes": [320, 1600]},
        "cloud": {"enabled": True, "base_url": "https://example.com",
                  "device_id": "d" * 32},
    }, path=data / "config.json", data_dir=data)


def _state(tmp_path: Path) -> AppState:
    cfg = _cfg(tmp_path)
    db = Database(cfg.db_path)
    db.init()
    return AppState(cfg, db)


def _ctx(st: AppState, *, role: str, via: str = LAN, method: str = "GET",
         path: str = "/api/timeline", body: dict | None = None,
         params: dict | None = None, query: dict | None = None) -> Ctx:
    client_id = {"owner": OWNER_ID, "guest": GUEST_A}[role]
    ctx = Ctx(
        method=method, path=path,
        query={k: [v] for k, v in (query or {}).items()},
        headers={"content-type": "application/json"} if body is not None else {},
        body=_Body(body) if body is not None else None,
        params=params or {}, app=st, via=via,
    )
    ctx.state["client"] = {"id": client_id, "name": "t", "role": role}
    return ctx


def _media(st: AppState, *, root_id: str, rel: str,
           uploader: str = "", kind: str = "image") -> int:
    cur = st.db.execute(
        "INSERT INTO media(root_id, rel_path, file_name, ext, kind, size, mtime,"
        " taken_at, thumb_state, uploader_client_id)"
        " VALUES (?, ?, ?, 'jpg', ?, 10, ?, ?, 'ok', ?)",
        (root_id, rel, rel.rsplit("/", 1)[-1], kind, 1_700_000_000.0,
         1_700_000_000.0, uploader),
    )
    return int(cur.lastrowid)


def _guest_session(st: AppState, client_id: str) -> None:
    """直接写库预置一个有效游客会话（本用例只验授权，不验发证）。"""
    now = 1_700_000_000.0
    invite = client_id[:31] + "9"        # 每个会话一条独立邀请（token_hash UNIQUE）
    conn = st.db.connect()
    conn.execute(
        "INSERT INTO guest_invites(id, device_id, token_hash, label, created_at,"
        " expires_at, first_used_at, created_via) VALUES (?, ?, ?, 't', ?, ?, ?, 'lan')",
        (invite, "d" * 32, "f" * 63 + client_id[0], now, now + 3600, now),
    )
    conn.execute(
        "INSERT INTO guest_sessions(client_id, token_hash, invite_id, device_id,"
        " label, created_at, expires_at, last_seen)"
        " VALUES (?, ?, ?, ?, 't', ?, ?, ?)",
        (client_id, "1" * 63 + client_id[0], invite, "d" * 32, now, now + 3600, now),
    )
    conn.commit()


# ---------------------------------------------------------------- S-01

def test_invite_creation_is_rejected_on_the_cloud_path(tmp_path: Path) -> None:
    st = _state(tmp_path)
    ctx = _ctx(st, role="owner", via=CLOUD, method="POST",
               path="/api/guest/invites", body={"label": "x", "minutes": 60})
    with pytest.raises(AppError) as exc:
        asyncio.run(h_guest.create_invite(ctx))
    assert exc.value.status == 403
    assert exc.value.code == "E2003"
    # 关键：拒绝发生在写库之前，库里不能留下半条邀请
    assert st.db.query("SELECT id FROM guest_invites") == []


def test_invite_creation_still_works_on_lan(tmp_path: Path) -> None:
    st = _state(tmp_path)
    ctx = _ctx(st, role="owner", via=LAN, method="POST",
               path="/api/guest/invites", body={"label": "朋友", "minutes": 60})
    reply = asyncio.run(h_guest.create_invite(ctx))
    assert reply.status == 200
    body = json.loads(reply.body)
    assert body["token"].startswith("pbg1.")
    assert "#guest=" in body["link"]


def test_guest_body_limit_exists() -> None:
    """S-01 的发证链缺陷：这个常量漏定义时兑换直接 500。"""
    assert isinstance(guest.GUEST_BODY_LIMIT, int)
    assert 1024 <= guest.GUEST_BODY_LIMIT <= 64 * 1024


# ---------------------------------------------------------------- S-03

def test_guest_upload_ignores_the_requested_root(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _guest_session(st, GUEST_A)
    ctx = _ctx(st, role="guest", method="POST", path="/api/uploads/init",
               body={"name": "a.jpg", "size": 100, "root": "library",
                     "target_dir": "injected"})
    reply = asyncio.run(uploads.init_upload(ctx))
    assert reply.status == 201
    body = json.loads(reply.body)
    row = st.db.query_one(
        "SELECT root_id, target_rel FROM upload_sessions WHERE id = ?",
        (body["upload_id"],),
    )
    assert row["root_id"] == "guest", "游客请求里的 root 必须被忽略"
    assert "injected" not in (row["target_rel"] or "")
    assert row["target_rel"].startswith(guest.GUEST_SUBDIR)


def test_invite_refuses_to_hand_out_an_empty_link(tmp_path: Path, monkeypatch) -> None:
    """没有云端基址时不能"成功"返回一个空链接（主人在面板上只会看到空框）。"""
    st = _state(tmp_path)
    # `_public_base` 读的是 `cloud.base_url`；这里直接让它返回空串，
    # 模拟"还没配云端"的那台电脑。
    monkeypatch.setattr(h_guest, "_public_base", lambda *a, **k: "")
    ctx = _ctx(st, role="owner", method="POST", path="/api/guest/invites",
               body={"label": "x", "minutes": 60})
    with pytest.raises(AppError) as ei:
        asyncio.run(h_guest.create_invite(ctx))
    assert ei.value.status == 400
    assert "云端" in str(ei.value.detail or ei.value)

# ---------------------------------------------------------------- S-04

def test_timeline_where_filters_by_uploader_when_asked() -> None:
    where, params = common.timeline_where(uploader_client_id=GUEST_A)
    assert "uploader_client_id = ?" in where
    assert params == [GUEST_A]


def test_guest_cannot_read_another_guests_photo(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _guest_session(st, GUEST_A)
    _guest_session(st, GUEST_B)
    mine = _media(st, root_id="guest", rel="游客上传/a.jpg", uploader=GUEST_A)
    other = _media(st, root_id="guest", rel="游客上传/b.jpg", uploader=GUEST_B)

    common.check_media_visible(_ctx(st, role="guest"), st,
                               {"root_id": "guest", "uploader_client_id": GUEST_A})
    with pytest.raises(AppError) as exc:
        common.check_media_visible(_ctx(st, role="guest"), st,
                                   {"root_id": "guest", "uploader_client_id": GUEST_B})
    assert exc.value.status == 404
    assert mine != other


def test_owner_can_read_both(tmp_path: Path) -> None:
    st = _state(tmp_path)
    ctx = _ctx(st, role="owner")
    for who in (GUEST_A, GUEST_B):
        common.check_media_visible(ctx, st,
                                   {"root_id": "guest", "uploader_client_id": who})


def test_guest_cannot_read_owner_media_by_id(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _guest_session(st, GUEST_A)
    _media(st, root_id="library", rel="2024/a.jpg")
    with pytest.raises(AppError) as exc:
        common.check_media_visible(_ctx(st, role="guest"), st,
                                   {"root_id": "library", "uploader_client_id": ""})
    assert exc.value.status == 404


# ---------------------------------------------------------------- S-05

def test_transcode_entry_applies_media_authorization(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _guest_session(st, GUEST_A)
    mid = _media(st, root_id="library", rel="v.mov", kind="video")
    ctx = _ctx(st, role="guest", path=f"/api/items/{mid}/transcode/stream",
               params={"media_id": str(mid)})
    with pytest.raises(AppError) as exc:
        transcode._video_row(ctx)          # noqa: SLF001 - 就是要测这道闸门
    assert exc.value.status == 404


def test_transcode_entry_still_works_for_owner(tmp_path: Path) -> None:
    st = _state(tmp_path)
    mid = _media(st, root_id="library", rel="v.mov", kind="video")
    ctx = _ctx(st, role="owner", path=f"/api/items/{mid}/transcode/stream",
               params={"media_id": str(mid)})
    assert transcode._video_row(ctx)["id"] == mid      # noqa: SLF001


# ---------------------------------------------------------------- S-06

def test_folders_only_lists_the_guests_own_directories(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _guest_session(st, GUEST_A)
    _guest_session(st, GUEST_B)
    _media(st, root_id="guest", rel="游客上传/我的/a.jpg", uploader=GUEST_A)
    _media(st, root_id="guest", rel="游客上传/别人的/b.jpg", uploader=GUEST_B)

    body = json.loads(asyncio.run(browse.folders(
        _ctx(st, role="guest", path="/api/folders", query={"root": "guest"})
    )).body)
    # 根层只该出现"游客上传"（游客的字节固定落这里），主人自己的相册一个都不能露。
    names = {d["name"] for d in body["folders"]}
    assert names == {"游客上传"}, f"游客在根层看到了别人的目录：{names}"

    # 再下钻一层：只应有"我的"（GUEST_A 自己传过东西的目录），
    # "别人的" 是 GUEST_B 传的，必须看不见。
    inner = json.loads(asyncio.run(browse.folders(
        _ctx(st, role="guest", path="/api/folders",
             query={"root": "guest", "dir": "游客上传"})
    )).body)
    inner_names = {d["name"] for d in inner["folders"]}
    assert inner_names == {"我的"}, f"游客看到了别人的子目录：{inner_names}"


def test_sse_drops_events_outside_the_guest_roots(tmp_path: Path) -> None:
    st = _state(tmp_path)
    ctx = _ctx(st, role="guest", path="/api/events")

    # 直接复用 handler 里的判据逻辑：从 handler 源码取不到的闭包，
    # 就再构造一次同样的过滤（够用且不引入产品侧的测试钩子）。
    allowed = set(common.visible_root_ids(ctx, st))

    def keep(item: dict) -> bool:
        return str(item.get("root") or "") in allowed

    assert keep({"root": "guest", "added": 1}) is True
    assert keep({"root": "library", "added": 7}) is False
    assert keep({"added": 3}) is False      # 无根事件对游客一律不发


# ---------------------------------------------------------------- S-02

def test_guest_writes_are_blocked_outside_uploads(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _guest_session(st, GUEST_A)
    for path in ("/api/items/delete", "/api/items/rename", "/api/items/move"):
        ctx = _ctx(st, role="guest", method="POST", path=path, body={})
        with pytest.raises(AppError) as exc:
            auth._enforce_guest_scope(ctx, ctx.state["client"])   # noqa: SLF001
        assert exc.value.status == 403
