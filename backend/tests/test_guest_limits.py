"""游客访问的两处调整：A 已撤销邀请不再展示、B 在线游客上限 3 人。

## 这批用例守什么

**A**：撤销是**终态**（没有"恢复"），撤销项对主人没有操作价值。过滤必须落在
查询层 —— 前端 `v-if` 隐藏等于响应里照样带着"这条存在过"，页面短了、语义没变。
记录本身要留着：撤销是写操作，审计链不能断（`guest_revoke` 仍要落一行）。

**B**：上限是"同时能进几个人"，判定必须在**服务端的准入点**（`guest.redeem`
的兑换事务里），而且计数与插入要在同一个 `BEGIN IMMEDIATE` 事务里 —— 先查后插
的两步写法在"两个游客几乎同时点开"时会双双通过。超限的话术不能带人数：
还没进门的人没资格知道主人家里有几个客人。

## 为什么不用前端拦

前端拦得住浏览器，拦不住 curl。邀请链接一贴在聊天软件里，地址就脱离了我们的
页面，任何"页面上的规则"对那个人都不存在。
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import guest  # noqa: E402
from app.config import (  # noqa: E402
    GUEST_MAX_ONLINE_DEFAULT,
    GUEST_MAX_ONLINE_MAX,
    GUEST_MAX_ONLINE_MIN,
    Config,
)
from app.core import Ctx  # noqa: E402
from app.db import Database  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.handlers import guest as h_guest  # noqa: E402
from app.state import AppState  # noqa: E402

DEVICE = "a" * 32


# ------------------------------------------------------------------ 夹具

def _cfg(tmp_path: Path, **guest_cfg) -> Config:
    owner = tmp_path / "owner"
    owner.mkdir(parents=True, exist_ok=True)
    share = tmp_path / "share"
    share.mkdir(parents=True, exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    doc = {
        "app": {"mode": "readwrite"},
        "roots": [
            {"id": "library", "path": str(owner), "mode": "readwrite"},
            {"id": "share", "path": str(share), "mode": "readwrite",
             "visibility": "guest"},
        ],
        "cloud": {"enabled": True, "base_url": "https://example.com",
                  "device_id": DEVICE},
    }
    if guest_cfg:
        doc["guest"] = guest_cfg
    return Config(doc, path=data / "config.json", data_dir=data)


def _state(tmp_path: Path, **guest_cfg) -> AppState:
    cfg = _cfg(tmp_path, **guest_cfg)
    db = Database(cfg.db_path)
    db.init()
    return AppState(cfg, db)


def _ctx(st: AppState, *, role: str = "owner", params: dict | None = None,
         method: str = "GET", path: str = "/api/guest/invites") -> Ctx:
    class _Stream:
        def __aiter__(self):
            async def _empty():
                if False:      # pragma: no cover
                    yield b""
            return _empty()

    ctx = Ctx(method=method, path=path, query={}, headers={}, body=_Stream(),
              params=params or {}, app=st)
    ctx.state["client"] = {"id": "c" * 32, "name": "owner", "role": role}
    return ctx


def _call(fn, ctx: Ctx):
    return asyncio.run(fn(ctx))


def _json(reply) -> dict:
    return json.loads(reply.body)


def _invite(st: AppState, **kw) -> dict:
    return guest.create_invite(st.db, device_id=DEVICE, label="",
                               minutes=kw.pop("minutes", 60), via="cloud", **kw)


def _enter(st: AppState, **kw) -> dict:
    inv = _invite(st, **kw)
    return guest.redeem(st.db, token=inv["token"], device_id=DEVICE,
                        via="cloud",
                        max_online=st.cfg.guest_max_online)


def _session_count(st: AppState) -> int:
    return int(st.db.scalar(
        "SELECT COUNT(*) FROM guest_sessions"
        " WHERE revoked_at IS NULL AND expires_at > ?", (time.time(),)))


# ------------------------------------------------------- A：撤销后不再展示

def test_revoked_invite_disappears_from_the_list(tmp_path: Path) -> None:
    st = _state(tmp_path)
    keep = _invite(st)
    gone = _invite(st)
    assert guest.revoke_invite(st.db, gone["invite_id"]) is True

    rows = guest.list_invites(st.db)
    assert [r["invite_id"] for r in rows] == [keep["invite_id"]]
    # 记录还在，只是默认不查出来（排障要能取到）
    assert len(guest.list_invites(st.db, include_revoked=True)) == 2


def test_revoked_row_and_audit_trail_are_kept(tmp_path: Path) -> None:
    st = _state(tmp_path)
    inv = _invite(st)
    sess = guest.redeem(st.db, token=inv["token"], device_id=DEVICE,
                        via="cloud")
    guest.revoke_invite(st.db, inv["invite_id"])

    row = st.db.query_one("SELECT revoked_at, guest_client_id FROM guest_invites"
                          " WHERE id = ?", (inv["invite_id"],))
    assert row is not None and row["revoked_at"], "撤销必须留痕，不能物理删除"
    ok, reason = guest.session_active(st.db, sess["client_id"])
    assert not ok and reason == "revoked"


def test_revoke_through_the_handler_hides_the_row_and_audits(tmp_path: Path) -> None:
    st = _state(tmp_path)
    inv = _invite(st)
    reply = _call(h_guest.revoke_invite,
                  _ctx(st, params={"invite_id": inv["invite_id"]},
                       method="DELETE"))
    assert reply.status == 204

    assert st.db.scalar(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'guest_revoke'") == 1
    listed = _json(_call(h_guest.list_invites, _ctx(st)))["items"]
    assert listed == [], "响应里不该再有已撤销项"


def test_list_handler_returns_unrevoked_items_only(tmp_path: Path) -> None:
    st = _state(tmp_path)
    a, b = _invite(st), _invite(st)
    guest.revoke_invite(st.db, a["invite_id"])
    items = _json(_call(h_guest.list_invites, _ctx(st)))["items"]
    assert [i["invite_id"] for i in items] == [b["invite_id"]]


def test_used_invites_hide_by_default_but_stay_retrievable(tmp_path: Path) -> None:
    """已使用的邀请默认不展示（2026-10-28 用户要求），但记录与状态仍在。

    ★ 过期但未使用的邀请**仍然列出**：它还能说明"这条没被用掉，要不要重发"，
      属于主人要看的活信息；用完作废的那条则只剩一个点不动的"已使用"行。
    """
    st = _state(tmp_path)
    used = _invite(st)
    guest.redeem(st.db, token=used["token"], device_id=DEVICE, via="cloud")
    expired = _invite(st)
    st.db.execute("UPDATE guest_invites SET expires_at = ? WHERE id = ?",
                  (time.time() - 1, expired["invite_id"]))

    default = guest.list_invites(st.db)
    ids = {r["invite_id"] for r in default}
    assert used["invite_id"] not in ids          # 已使用 → 不展示
    assert expired["invite_id"] in ids           # 过期未使用 → 仍展示
    assert {r["invite_id"]: r["state"] for r in default}[expired["invite_id"]] == "expired"

    with_used = guest.list_invites(st.db, include_used=True)
    assert used["invite_id"] in {r["invite_id"] for r in with_used}


def test_revoked_link_still_says_it_was_revoked(tmp_path: Path) -> None:
    """不展示了，但点开仍要明确说失效（既有验收标准不能退化）。"""
    st = _state(tmp_path)
    inv = _invite(st)
    guest.revoke_invite(st.db, inv["invite_id"])
    with pytest.raises(AppError) as ei:
        guest.redeem(st.db, token=inv["token"], device_id=DEVICE, via="cloud")
    assert ei.value.spec.code == "E1023"


# ------------------------------------------------------- B：在线游客上限 3

def test_cap_defaults_to_three_and_is_clamped(tmp_path: Path) -> None:
    assert GUEST_MAX_ONLINE_DEFAULT == 3
    assert _state(tmp_path / "a").cfg.guest_max_online == 3
    assert _state(tmp_path / "b", max_online=0).cfg.guest_max_online == GUEST_MAX_ONLINE_MIN
    assert _state(tmp_path / "c", max_online=-5).cfg.guest_max_online == GUEST_MAX_ONLINE_MIN
    assert _state(tmp_path / "d", max_online=99).cfg.guest_max_online == GUEST_MAX_ONLINE_MAX
    assert _state(tmp_path / "e", max_online="很多").cfg.guest_max_online == 3


def test_fourth_guest_is_turned_away(tmp_path: Path) -> None:
    st = _state(tmp_path)
    for _ in range(3):
        _enter(st)
    assert _session_count(st) == 3

    late = _invite(st)
    with pytest.raises(AppError) as ei:
        guest.redeem(st.db, token=late["token"], device_id=DEVICE, via="cloud")
    assert ei.value.spec.code == "E1024"
    assert ei.value.spec.status == 429
    detail = str(ei.value.detail)
    assert "已满" in detail
    assert not any(ch.isdigit() for ch in detail), "超限提示不能带人数"
    # 被拒之后链接**没有**被消耗：主人踢人之后同一条还能用
    row = st.db.query_one("SELECT first_used_at FROM guest_invites WHERE id = ?",
                          (late["invite_id"],))
    assert row["first_used_at"] is None


def test_kicking_one_lets_the_next_guest_in(tmp_path: Path) -> None:
    st = _state(tmp_path)
    sessions = [_enter(st) for _ in range(3)]
    late = _invite(st)

    with pytest.raises(AppError):
        guest.redeem(st.db, token=late["token"], device_id=DEVICE, via="cloud")

    assert guest.kick_session(st.db, sessions[0]["client_id"]) is True
    sess = guest.redeem(st.db, token=late["token"], device_id=DEVICE,
                        via="cloud")
    assert sess["client_id"]
    assert _session_count(st) == 3


def test_two_simultaneous_guests_never_exceed_the_cap(tmp_path: Path) -> None:
    """已有 2 人时，两个游客几乎同时兑换 —— 最终在线数必须停在 3。"""
    st = _state(tmp_path)
    for _ in range(2):
        _enter(st)
    inv_a, inv_b = _invite(st), _invite(st)

    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def _try(token: str) -> None:
        barrier.wait(timeout=10)
        try:
            guest.redeem(st.db, token=token, device_id=DEVICE, via="cloud")
            tag = "ok"
        except AppError as exc:
            tag = exc.spec.code
        with lock:
            outcomes.append(tag)

    threads = [threading.Thread(target=_try, args=(t,))
               for t in (inv_a["token"], inv_b["token"])]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)
        assert not th.is_alive()

    assert sorted(outcomes) == ["E1024", "ok"], f"应恰好一个成功，实际 {outcomes}"
    assert _session_count(st) == 3, "并发下不能出现第 4 个人"


def test_cap_follows_the_configuration(tmp_path: Path) -> None:
    st = _state(tmp_path, max_online=1)
    _enter(st)
    with pytest.raises(AppError) as ei:
        _enter(st)
    assert ei.value.spec.code == "E1024"


def test_unused_invites_are_capped_by_the_same_number(tmp_path: Path) -> None:
    """B1「有效邀请数」：未使用的邀请也不该超过上限，否则一张截图能造无数入口。"""
    st = _state(tmp_path)
    for _ in range(3):
        _invite(st)
    with pytest.raises(AppError) as ei:
        _invite(st)
    assert ei.value.spec.code == "E1025"
    assert ei.value.spec.status == 429
    assert not any(ch.isdigit() for ch in str(ei.value.detail))
