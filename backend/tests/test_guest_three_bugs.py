"""三个缺陷的回归（BUG-1 生成邀请 / BUG-2 列表折叠 / BUG-3 踢出）。

## 三条各自的判据

**BUG-1**（生成邀请恒 403 `token_resource_mismatch`）
根因是 `create_invite` 拿"通路"当判据：`ctx.via == CLOUD` 一刀切拒绝，而
主人扫的**就是云端二维码**，于是正常用法被自己的规则挡住。判据改成身份
两要素：角色必须是 owner（E2010）、凭证里声明的设备必须是这台电脑（E2011）、
云端账号绑定的设备必须是这台电脑（E2009，在中继侧判）。

**BUG-2**（在线列表不折叠）
展示层根本没有"上限"这个值。这里既钉后端（配置项 + `/api/system/info` 回显），
也钉前端源码（折叠逻辑、字段名、折叠条文案）。

**BUG-3**（踢出恒 not_found）
前端读 `s.revoked`，服务端发的是 `revoked_at` + `online` —— 字段名对不上，
于是每一行都长着"踢出"按钮，点已撤销的那行必然 0 行更新、后端只能回 404。
判据两头都钉：前端不许再读 `s.revoked`；后端对"已经不在线"回 409 E1026。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import auth, guest  # noqa: E402
from app.config import Config  # noqa: E402
from app.core import Ctx  # noqa: E402
from app.core.ctx import CLOUD, LAN  # noqa: E402
from app.db import Database  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.handlers import guest as h_guest, system as h_system  # noqa: E402
from app.state import AppState  # noqa: E402

OWNER_ID = "a" * 32
GUEST_A = "b" * 32
DEV_ID = "d" * 32
FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "src"


# ---------------------------------------------------------------- 夹具

class _Body:
    """最小请求体（`ctx.json()` 只要求 async-iterable 的字节块）。"""

    def __init__(self, payload: dict) -> None:
        self._data = json.dumps(payload).encode("utf-8")

    def __aiter__(self):
        async def gen():
            yield self._data
        return gen()


def _state(tmp_path: Path) -> AppState:
    (tmp_path / "owner").mkdir(exist_ok=True)
    (tmp_path / "guest").mkdir(exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    cfg = Config({
        "app": {"mode": "readwrite"},
        "roots": [
            {"id": "library", "path": str(tmp_path / "owner"), "mode": "readwrite"},
            {"id": "guest", "path": str(tmp_path / "guest"), "mode": "readwrite",
             "visibility": "guest"},
        ],
        "thumbs": {"sizes": [320, 1600]},
        "cloud": {"enabled": True, "base_url": "https://example.com",
                  "device_id": DEV_ID},
    }, path=data / "config.json", data_dir=data)
    db = Database(cfg.db_path)
    db.init()
    return AppState(cfg, db)


def _ctx(st: AppState, *, role: str = "owner", via: str = LAN,
         method: str = "GET", path: str = "/api/timeline",
         body: dict | None = None, params: dict | None = None) -> Ctx:
    ctx = Ctx(
        method=method, path=path, query={},
        headers={"content-type": "application/json"} if body is not None else {},
        body=_Body(body) if body is not None else None,
        params=params or {}, app=st, via=via,
    )
    ctx.state["client"] = {
        "id": OWNER_ID if role == "owner" else GUEST_A,
        "name": "t", "role": role,
        "guest": role == "guest",
    }
    return ctx


def _session(st: AppState, client_id: str, *, revoked: bool = False) -> None:
    now = time.time()
    invite = client_id[:31] + "9"
    conn = st.db.connect()
    conn.execute(
        "INSERT INTO guest_invites(id, device_id, token_hash, label, created_at,"
        " expires_at, first_used_at, created_via) VALUES (?, ?, ?, 't', ?, ?, ?, 'lan')",
        (invite, DEV_ID, "f" * 63 + client_id[0], now, now + 3600, now),
    )
    conn.execute(
        "INSERT INTO guest_sessions(client_id, token_hash, invite_id, device_id,"
        " label, created_at, expires_at, last_seen, revoked_at)"
        " VALUES (?, ?, ?, ?, 't', ?, ?, ?, ?)",
        (client_id, "1" * 63 + client_id[0], invite, DEV_ID, now, now + 3600, now,
         now + 10 if revoked else None),
    )
    conn.commit()


# ---------------------------------------------------------------- BUG-1

def test_cloud_transport_no_longer_blocks_invite_creation(tmp_path: Path) -> None:
    """主人从云端页面点"生成邀请"必须成功（BUG-1 的主症状）。"""
    st = _state(tmp_path)
    ctx = _ctx(st, role="owner", via=CLOUD, method="POST",
               path="/api/guest/invites", body={"label": "朋友", "minutes": 60})
    reply = asyncio.run(h_guest.create_invite(ctx))
    assert reply.status == 200
    body = json.loads(reply.body)
    assert body["token"].startswith("pbg1.")
    # 链接必须指向**云端**基址 + 本机设备 id（否则扫码的人进不来）
    assert body["link"] == f"https://example.com/d/{DEV_ID}/#guest={body['token']}"


def test_same_request_on_lan_still_works(tmp_path: Path) -> None:
    """通路不再是判据：局域网照旧能生成（不能把老用法改坏）。"""
    st = _state(tmp_path)
    ctx = _ctx(st, role="owner", via=LAN, method="POST",
               path="/api/guest/invites", body={"label": "朋友", "minutes": 60})
    assert asyncio.run(h_guest.create_invite(ctx)).status == 200


def test_viewer_role_can_still_manage_invites(tmp_path: Path) -> None:
    """`viewer` 必须能用。

    ★ 这条用例原本断言 `viewer → E2010`，是错的：配对时只有**第一台**
      设备拿到 `role="owner"`，之后每台都是 `viewer`，于是"主人的第二部
      手机"会被挡在邀请管理之外 —— 那正是 BUG-1 要修的场景（手机从云端
      页面点"生成邀请"）。真要防的是"别的设备/别的账号"，由 E2011/E2012
      与 `token_device_claim` 守。
    """
    st = _state(tmp_path)
    ctx = _ctx(st, role="viewer", method="POST",
               path="/api/guest/invites", body={"label": "x", "minutes": 60})
    assert asyncio.run(h_guest.create_invite(ctx)).status == 200


def test_guest_role_gets_owner_required(tmp_path: Path) -> None:
    st = _state(tmp_path)
    ctx = _ctx(st, role="guest", via=CLOUD, method="POST",
               path="/api/guest/invites", body={"label": "x", "minutes": 60})
    with pytest.raises(AppError) as exc:
        asyncio.run(h_guest.create_invite(ctx))
    assert (exc.value.status, exc.value.code) == (403, "E2010")


def test_token_for_another_device_gets_device_mismatch(tmp_path: Path,
                                                       monkeypatch) -> None:
    """凭证声明的设备不是这台电脑 → E2011，且不写库。"""
    st = _state(tmp_path)
    monkeypatch.setattr(h_guest, "token_device_claim", lambda *a, **k: "e" * 32)
    ctx = _ctx(st, role="owner", method="POST",
               path="/api/guest/invites", body={"label": "x", "minutes": 60})
    with pytest.raises(AppError) as exc:
        asyncio.run(h_guest.create_invite(ctx))
    assert (exc.value.status, exc.value.code) == (403, "E2011")
    assert st.db.query("SELECT id FROM guest_invites") == []


def test_matching_device_and_owner_passes_the_guard(tmp_path: Path,
                                                    monkeypatch) -> None:
    st = _state(tmp_path)
    monkeypatch.setattr(h_guest, "token_device_claim", lambda *a, **k: DEV_ID)
    ctx = _ctx(st, role="owner", method="POST",
               path="/api/guest/invites", body={"label": "x", "minutes": 60})
    assert asyncio.run(h_guest.create_invite(ctx)).status == 200


def test_the_three_mismatch_codes_are_distinct() -> None:
    """三个码必须各不相同、且都在错误码表里（细化不是换汤不换药）。"""
    from app.errors import SPECS

    codes = {c: SPECS[c] for c in ("E2010", "E2011", "E2012")}
    assert len({s.user_hint for s in codes.values()}) == 3
    assert {s.status for s in codes.values()} == {403}


# ---------------------------------------------------------------- BUG-3

def test_kick_a_live_session_succeeds_and_kills_it(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _session(st, GUEST_A)
    ctx = _ctx(st, method="DELETE", path=f"/api/guest/sessions/{GUEST_A}",
               params={"client_id": GUEST_A})
    assert asyncio.run(h_guest.kick_session(ctx)).status == 204
    assert guest.session_active(st.db, GUEST_A)[0] is False


def test_kick_an_already_revoked_session_is_not_not_found(tmp_path: Path) -> None:
    """已经退出的人再踢一次 → 409 E1026（不是 404，也不是静默成功）。"""
    st = _state(tmp_path)
    _session(st, GUEST_A, revoked=True)
    ctx = _ctx(st, method="DELETE", path=f"/api/guest/sessions/{GUEST_A}",
               params={"client_id": GUEST_A})
    with pytest.raises(AppError) as exc:
        asyncio.run(h_guest.kick_session(ctx))
    assert (exc.value.status, exc.value.code) == (409, "E1026")


def test_kick_an_unknown_client_id_is_indistinguishable_from_offline(
        tmp_path: Path) -> None:
    """猜一个不存在的 id 也只能得到同一句话（不泄露存在性）。"""
    st = _state(tmp_path)
    ctx = _ctx(st, method="DELETE", path="/api/guest/sessions/" + "9" * 32,
               params={"client_id": "9" * 32})
    with pytest.raises(AppError) as exc:
        asyncio.run(h_guest.kick_session(ctx))
    assert (exc.value.status, exc.value.code) == (409, "E1026")


def test_kick_removes_the_row_from_the_online_list(tmp_path: Path) -> None:
    st = _state(tmp_path)
    _session(st, GUEST_A)
    assert guest.list_sessions(st.db)[0]["online"] is True
    ctx = _ctx(st, method="DELETE", path=f"/api/guest/sessions/{GUEST_A}",
               params={"client_id": GUEST_A})
    asyncio.run(h_guest.kick_session(ctx))
    row = guest.list_sessions(st.db)[0]
    assert row["online"] is False and row["revoked_at"]


def test_session_rows_expose_online_and_revoked_at(tmp_path: Path) -> None:
    """前端就是靠这两个字段判断"能不能踢"（老字段 `revoked` 根本不存在）。"""
    st = _state(tmp_path)
    _session(st, GUEST_A)
    _session(st, "c" * 32, revoked=True)
    rows = {r["client_id"]: r for r in guest.list_sessions(st.db)}
    assert set(rows[GUEST_A]) >= {"online", "revoked_at", "created_at", "last_seen"}
    assert "revoked" not in rows[GUEST_A]
    assert rows[GUEST_A]["online"] is True
    assert rows["c" * 32]["online"] is False


# ---------------------------------------------------------------- BUG-2

def test_visible_limit_defaults_to_three(tmp_path: Path) -> None:
    st = _state(tmp_path)
    assert st.cfg.guest_max_visible == 3


@pytest.mark.parametrize("raw, expected", [
    (2, 2), (5, 5), (0, 1), (-3, 1), (99, 10), ("x", 3), (None, 3),
])
def test_visible_limit_is_clamped_not_fatal(raw, expected, tmp_path: Path) -> None:
    (tmp_path / "owner").mkdir()
    (tmp_path / "guest").mkdir()
    data = tmp_path / "data"
    data.mkdir()
    cfg = Config({
        "app": {"mode": "readwrite"},
        "roots": [
            {"id": "library", "path": str(tmp_path / "owner"), "mode": "readwrite"},
            {"id": "guest", "path": str(tmp_path / "guest"), "mode": "readwrite",
             "visibility": "guest"},
        ],
        "guest": {"max_visible": raw} if raw is not None else {},
    }, path=data / "config.json", data_dir=data)
    assert cfg.guest_max_visible == expected


def test_system_info_echoes_the_visible_limit_to_the_owner(tmp_path: Path) -> None:
    st = _state(tmp_path)
    ctx = _ctx(st, path="/api/system/info")
    body = json.loads(asyncio.run(h_system.info(ctx)).body)
    assert body["guest"] == {"max_online": 3, "max_visible": 3}


def test_system_info_hides_the_guest_section_from_guests(tmp_path: Path) -> None:
    st = _state(tmp_path)
    ctx = _ctx(st, role="guest", via=CLOUD, path="/api/system/info")
    body = json.loads(asyncio.run(h_system.info(ctx)).body)
    assert body["guest"] == {}


# ---------------------------------------------------------------- 前端源码判据

def _panel() -> str:
    """组件源码，**去掉注释**后再断言。

    注释里提到 `s.revoked`、`revoked: boolean` 是有意的（那是根因记录），
    不能算"还在用老字段"；判据只针对真会执行的代码。
    """
    src = (FRONTEND / "components" / "GuestPanel.vue").read_text(encoding="utf-8")
    kept = []
    for line in src.splitlines():
        s = line.strip()
        if s.startswith(("//", "/*", "*", "<!--")):
            continue
        kept.append(line)
    return "\n".join(kept)


def test_frontend_reads_the_fields_the_api_actually_sends() -> None:
    src = _panel()
    assert "revoked_at: number | null;" in src
    assert "online: boolean;" in src
    # 老字段是 BUG-3 的来源，必须彻底消失
    assert "\n  revoked: boolean;" not in src
    assert "s.revoked" not in src


def test_frontend_folds_the_list_by_the_configured_limit() -> None:
    src = _panel()
    assert "maxVisible" in src and "slice(0, maxVisible.value)" in src
    assert "hiddenSessions" in src
    # 上限来自服务端回显，不是写死的 3
    assert "/api/system/info" in src
    assert "guest?.max_visible" in src
    # 折叠条：计数 + 展开/收起
    assert "还有 {{ hiddenSessions }} 个未展开" in src
    assert '"展开全部"' in src and '"收起"' in src


def test_frontend_only_offers_kick_for_online_guests() -> None:
    src = _panel()
    assert 'v-for="s in visibleSessions"' in src
    assert '<div v-if="s.online" class="inv-acts">' in src
    # 已退出的人显示标签而不是按钮
    assert '<span v-if="!s.online" class="tag warn">已退出</span>' in src


def test_frontend_treats_E1026_as_a_harmless_refresh() -> None:
    src = _panel()
    assert '"E1026"' in src and "已经不在线" in src
