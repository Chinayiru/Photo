"""游客隔离：写入闸门（中间件）与 `/api/system/{info,stats}` 的信息收口。

## 这一批守的是什么

前面几组用例保证"游客只能看见自己那个根"（`visible_root_ids`）与"游客上传
落在游客区"。但**看得见什么**与**能做什么**是两件事：

* 游客拿着自己的令牌去 `POST /api/items/delete` —— 如果不拦，`delete_items`
  里只判 `root.can_delete`（游客根是 upload 模式，删不了）→ 这条其实挡住了；
  但 `POST /api/items/rename` / `move` 判的是 `can_modify`，而 upload 模式
  **不能** modify（F01 之后也是分开的）。也就是说删改其实都挡住了，
  但挡住的方式是"逐端点各判一次"，只要将来有人新增一个写端点、忘了一行
  判断，边界就破了。

  所以这里守的是一条**全局**规则：游客的非只读请求只允许 `/api/uploads/`。
  白名单写在中间件里，新增接口默认对游客关闭。

* `/api/system/info` 与 `stats` 原本会把主人的全库信息端出来：
  根的绝对路径、局域网 IP、托盘状态、全库照片总数。这些与"看一次旅行的
  照片"无关，属于不该给的信息。
"""
from __future__ import annotations

import json

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth  # noqa: E402
from app.config import Config  # noqa: E402
from app.core import Ctx  # noqa: E402
from app.db import Database  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.handlers import system  # noqa: E402
from app.state import AppState  # noqa: E402


# --------------------------------------------------------------------- 夹具

def _cfg(tmp_path: Path, *, with_guest: bool = True) -> Config:
    owner = tmp_path / "owner"
    owner.mkdir(exist_ok=True)
    roots = [{"id": "library", "path": str(owner), "mode": "readwrite"}]
    if with_guest:
        guest = tmp_path / "guest"
        guest.mkdir(exist_ok=True)
        roots.append({"id": "share", "path": str(guest), "mode": "upload",
                      "visibility": "guest"})
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Config({
        "app": {"mode": "readwrite"},
        "roots": roots,
        "thumbs": {"sizes": [320, 1600]},
        "cloud": {"enabled": True, "base_url": "https://example.com",
                  "device_id": "a" * 32},
    }, path=data / "config.json", data_dir=data)


def _ctx(st: AppState, *, role: str, method: str = "GET",
         path: str = "/api/timeline") -> Ctx:
    class _Stream:
        def __aiter__(self):
            async def _empty():
                if False:      # pragma: no cover - 空体
                    yield b""
            return _empty()

    ctx = Ctx(method=method, path=path, query={}, headers={},
              body=None, params={}, app=st)
    ctx.state["client"] = {"id": "c" * 32, "name": "t", "role": role}
    return ctx


def _state(tmp_path: Path, *, with_guest: bool = True) -> AppState:
    cfg = _cfg(tmp_path, with_guest=with_guest)
    db = Database(cfg.db_path)
    db.init()
    return AppState(cfg, db)


def _seed(st: AppState, n_owner: int, n_guest: int) -> None:
    """两个根各插若干行，用来验统计口径。"""
    now = 1_700_000_000.0
    rows = []
    for i in range(n_owner):
        rows.append(("library", f"o{i}.jpg", now + i))
    for i in range(n_guest):
        rows.append(("share", f"g{i}.jpg", now + 100 + i))
    for rid, name, ts in rows:
        st.db.execute(
            "INSERT INTO media(root_id, rel_path, file_name, ext, kind, size,"
            " mtime, taken_at, thumb_state)"
            " VALUES (?, ?, ?, 'jpg', 'image', 10, ?, ?, 'ok')",
            (rid, name, name, ts, ts),
        )


# --------------------------------------------------------------- 写入闸门

@pytest.mark.parametrize("method,path,ok", [
    ("GET", "/api/timeline", True),
    ("HEAD", "/api/items/1/thumb", True),
    ("OPTIONS", "/api/timeline", True),
    ("POST", "/api/uploads/init", True),
    ("PATCH", "/api/uploads/abc", True),
    ("POST", "/api/uploads/abc/complete", True),
    ("POST", "/api/items/delete", False),
    ("POST", "/api/items/move", False),
    ("POST", "/api/items/rename", False),
    ("DELETE", "/api/uploads/abc", False),
    ("POST", "/api/guest/invites", False),
    ("DELETE", "/api/guest/sessions/x", False),
    ("POST", "/api/system/rescan", False),
])
def test_guest_write_whitelist(method: str, path: str, ok: bool) -> None:
    assert auth.guest_may_write(method, path) is ok


def test_owner_is_not_restricted(tmp_path: Path) -> None:
    """主人不受这条闸门影响（哪怕路径不在白名单里）。"""
    st = _state(tmp_path)
    ctx = _ctx(st, role="owner", method="POST", path="/api/items/delete")
    auth._enforce_guest_scope(ctx, ctx.state["client"])   # 不抛就是通过


def test_guest_cannot_write_outside_uploads(tmp_path: Path) -> None:
    st = _state(tmp_path)
    ctx = _ctx(st, role="guest", method="POST", path="/api/items/delete")
    with pytest.raises(AppError) as exc:
        auth._enforce_guest_scope(ctx, ctx.state["client"])
    assert exc.value.status == 403
    assert exc.value.code == "E2003"


def test_guest_can_upload(tmp_path: Path) -> None:
    st = _state(tmp_path)
    ctx = _ctx(st, role="guest", method="POST", path="/api/uploads/init")
    auth._enforce_guest_scope(ctx, ctx.state["client"])


# --------------------------------------------------- info / stats 的信息收口

def test_guest_info_hides_owner_roots_and_machine_details(tmp_path: Path) -> None:
    st = _state(tmp_path)
    body = json.loads(asyncio.run(system.info(_ctx(st, role="guest"))).body)

    ids = [r["id"] for r in body["roots"]]
    assert ids == ["share"], "游客不该看到主人的根"
    assert body["server"]["lan_ips"] == []
    assert body["watch"] == {}
    assert body["host_app"] == {}


def test_owner_info_still_has_everything(tmp_path: Path) -> None:
    st = _state(tmp_path)
    body = json.loads(asyncio.run(system.info(_ctx(st, role="owner"))).body)
    ids = {r["id"] for r in body["roots"]}
    assert ids == {"library", "share"}


def test_guest_stats_count_only_the_guest_root(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _seed(st, n_owner=7, n_guest=3)
    body = json.loads(asyncio.run(system.stats(_ctx(st, role="guest"))).body)
    assert body["media"]["total"] == 3, "游客只该数到自己那个根"
    assert body["db"] == {}


def test_owner_stats_count_the_whole_library(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _seed(st, n_owner=7, n_guest=3)
    body = json.loads(asyncio.run(system.stats(_ctx(st, role="owner"))).body)
    assert body["media"]["total"] == 10
    assert body["db"], "主人仍应拿到库统计"
