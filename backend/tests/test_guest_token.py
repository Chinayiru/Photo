"""游客访问的令牌与鉴权（ISSUE-063 S2/S3）。

## 这批用例守的四条线

1. **主人令牌一个字节都不变**：`role`/`inv` 只在游客令牌上出现。升级
   前后同一枚主人令牌必须仍能验过 —— 否则线上会有一个"所有人同时 401"
   的窗口。
2. **游客令牌必须有 `inv`**：没有它，"主人立刻结束访问"（G13）就退化成
   "等 2 小时自然过期"。少一个 claim 的令牌不是"宽松一点"，而是**撤销
   失效**。
3. **通路门（G2）**：游客只能从云端进来。局域网通路上一律 401 —— 同一
   台电脑的 8799 也在监听，不卡这条就等于给"同一个 Wi-Fi 下的陌生人"
   开了一条绕过配对码的路。
4. **邀请是一次性的**（G11 A）：第二次兑换 410 E1022，但**已有会话照旧
   有效**（这正是选 A 的理由：链接失效与会话有效可以同时成立）。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from backend.app import auth, guest
from backend.app.core import Ctx
from backend.app.core.ctx import CLOUD, LAN
from backend.app.db import Database
from backend.app.errors import AppError
from shared import client_token as token_mod

DEVICE = "a" * 32


def _db(tmp_path) -> Database:
    db = Database(tmp_path / "guest.db")
    db.init()
    return db


def _ctx(via: str = CLOUD) -> Ctx:
    return Ctx(method="POST", path="/api/guest/redeem", via=via)


def _secret(db: Database) -> str:
    return auth._signing_secret(db, None)  # noqa: SLF001


# ------------------------------------------------------------------ 令牌

def test_owner_token_is_byte_identical_to_before(tmp_path) -> None:
    """主人令牌不写 `rl`/`inv`：升级不能造成一次全量 401。"""
    db = _db(tmp_path)
    secret = _secret(db)
    tok = token_mod.issue_token(secret, client_id="c" * 32, device_id=DEVICE,
                               now=1_700_000_000, client_name="手机")
    claims = token_mod.verify_token(secret, tok, device_id=DEVICE,
                                   now=1_700_000_001)
    assert claims is not None
    assert "rl" not in claims and "inv" not in claims
    assert claims.get("nam") == "手机"


def test_guest_token_carries_role_and_invite(tmp_path) -> None:
    db = _db(tmp_path)
    secret = _secret(db)
    tok = token_mod.issue_token(secret, client_id="g" * 32, device_id=DEVICE,
                               role="guest", invite_id="i" * 32,
                               now=1_700_000_000)
    claims = token_mod.verify_token(secret, tok, device_id=DEVICE,
                                   now=1_700_000_001)
    assert claims is not None
    assert claims["rl"] == "guest"
    assert claims["inv"] == "i" * 32


def test_guest_token_without_invite_is_rejected(tmp_path) -> None:
    """没有 `inv` 的游客令牌 → 验签不过（撤销会失效，宁可拒绝）。"""
    db = _db(tmp_path)
    secret = _secret(db)
    with pytest.raises(ValueError):
        token_mod.issue_token(secret, client_id="g" * 32, device_id=DEVICE,
                              role="guest")

    # 手工造一枚"缺 inv"的令牌：绕过 issue_token 的守卫，模拟别的实现。
    import base64
    import hashlib
    import hmac
    import json

    payload = {"sub": "g" * 32, "dev": DEVICE, "iat": 1_700_000_000,
               "exp": 1_700_000_900, "rl": "guest"}
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signing_input = f"{token_mod.TOKEN_PREFIX}.{body}"
    mac = hmac.new(secret.encode(), signing_input.encode("ascii"),
                   hashlib.sha256).digest()
    forged = f"{signing_input}." + \
        base64.urlsafe_b64encode(mac).decode().rstrip("=")
    assert token_mod.verify_token(secret, forged, device_id=DEVICE,
                                  now=1_700_000_001) is None


def test_unknown_role_is_rejected(tmp_path) -> None:
    """白名单：以后加角色时，旧版本代码必须**拒绝**而不是放行。"""
    db = _db(tmp_path)
    secret = _secret(db)
    import base64
    import hashlib
    import hmac
    import json

    payload = {"sub": "x" * 32, "dev": DEVICE, "iat": 1_700_000_000,
               "exp": 1_700_000_900, "rl": "admin", "inv": "i" * 32}
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signing_input = f"{token_mod.TOKEN_PREFIX}.{body}"
    mac = hmac.new(secret.encode(), signing_input.encode("ascii"),
                   hashlib.sha256).digest()
    forged = f"{signing_input}." + \
        base64.urlsafe_b64encode(mac).decode().rstrip("=")
    assert token_mod.verify_token(secret, forged, device_id=DEVICE,
                                  now=1_700_000_001) is None


# ------------------------------------------------------------------ 邀请与会话

def test_redeem_returns_a_working_session(tmp_path) -> None:
    db = _db(tmp_path)
    inv = guest.create_invite(db, device_id=DEVICE, label="老王", minutes=60,
                              via="cloud")
    sess = guest.redeem(db, token=inv["token"], device_id=DEVICE, via="cloud")
    assert sess["expires_at"] > time.time()
    ok, reason = guest.session_active(db, sess["client_id"])
    assert ok and reason == "ok"
    # 令牌能验过，且绑的是这台电脑
    claims = token_mod.verify_token(_secret(db), sess["token"], device_id=DEVICE)
    assert claims is not None and claims["rl"] == "guest"
    assert claims["inv"] == inv["invite_id"]


def test_invite_is_single_use_but_session_survives(tmp_path) -> None:
    """G11 解释 A：链接一次性，会话不受影响。"""
    db = _db(tmp_path)
    inv = guest.create_invite(db, device_id=DEVICE, label="", minutes=60,
                              via="cloud")
    sess = guest.redeem(db, token=inv["token"], device_id=DEVICE, via="cloud")
    with pytest.raises(AppError) as ei:
        guest.redeem(db, token=inv["token"], device_id=DEVICE, via="cloud")
    assert ei.value.spec.status == 410
    assert ei.value.spec.code == "E1022"
    assert guest.session_active(db, sess["client_id"])[0] is True


def test_invalid_and_expired_tokens_are_distinguishable(tmp_path) -> None:
    db = _db(tmp_path)
    with pytest.raises(AppError) as ei:
        guest.redeem(db, token="pbg1.nope", device_id=DEVICE, via="cloud")
    assert ei.value.spec.code == "E1020"

    inv = guest.create_invite(db, device_id=DEVICE, label="", minutes=5,
                              via="cloud")
    db.execute("UPDATE guest_invites SET expires_at = ? WHERE id = ?",
               (time.time() - 1, inv["invite_id"]))
    with pytest.raises(AppError) as ei2:
        guest.redeem(db, token=inv["token"], device_id=DEVICE, via="cloud")
    assert ei2.value.spec.code == "E1021"


def test_active_invites_are_capped(tmp_path) -> None:
    db = _db(tmp_path)
    for _ in range(guest.MAX_ACTIVE_INVITES):
        guest.create_invite(db, device_id=DEVICE, label="", minutes=60,
                            via="cloud")
    with pytest.raises(AppError) as ei:
        guest.create_invite(db, device_id=DEVICE, label="", minutes=60,
                            via="cloud")
    assert "最多同时保留" in str(ei.value.detail)


def test_online_guests_are_capped(tmp_path) -> None:
    db = _db(tmp_path)
    for _ in range(guest.MAX_ONLINE_GUESTS):
        inv = guest.create_invite(db, device_id=DEVICE, label="", minutes=60,
                                  via="cloud")
        guest.redeem(db, token=inv["token"], device_id=DEVICE, via="cloud")
    inv = guest.create_invite(db, device_id=DEVICE, label="", minutes=60,
                              via="cloud")
    with pytest.raises(AppError) as ei:
        guest.redeem(db, token=inv["token"], device_id=DEVICE, via="cloud")
    assert "已在线" in str(ei.value.detail)


def test_revoking_the_invite_kills_its_sessions(tmp_path) -> None:
    """G13「撤销即刻失效」：会话状态在库里，下一个请求就 403。"""
    db = _db(tmp_path)
    inv = guest.create_invite(db, device_id=DEVICE, label="", minutes=60,
                              via="cloud")
    sess = guest.redeem(db, token=inv["token"], device_id=DEVICE, via="cloud")
    assert guest.revoke_invite(db, inv["invite_id"]) is True
    ok, reason = guest.session_active(db, sess["client_id"])
    assert not ok and reason == "revoked"


def test_kick_session_is_immediate(tmp_path) -> None:
    db = _db(tmp_path)
    inv = guest.create_invite(db, device_id=DEVICE, label="", minutes=60,
                              via="cloud")
    sess = guest.redeem(db, token=inv["token"], device_id=DEVICE, via="cloud")
    assert guest.kick_session(db, sess["client_id"]) is True
    assert guest.session_active(db, sess["client_id"])[0] is False


def test_minutes_are_clamped_not_silently_rewritten(tmp_path) -> None:
    """超上限要报错：静默改成 7 天会让主人以为发了 30 天。"""
    assert guest.clamp_minutes("5") == 5
    assert guest.clamp_minutes(10080) == 10080
    for bad in ("4", 0, 20000, "abc", None):
        with pytest.raises(AppError):
            guest.clamp_minutes(bad)


# ------------------------------------------------------------------ 通路门（G2）

def test_guest_token_from_lan_is_rejected(tmp_path) -> None:
    db = _db(tmp_path)
    inv = guest.create_invite(db, device_id=DEVICE, label="", minutes=60,
                              via="cloud")
    sess = guest.redeem(db, token=inv["token"], device_id=DEVICE, via="cloud")
    claims = token_mod.verify_token(_secret(db), sess["token"], device_id=DEVICE)
    assert claims is not None

    ctx = _ctx(via=LAN)
    with pytest.raises(AppError) as ei:
        auth._admit_guest(ctx, db, claims)  # noqa: SLF001
    assert ei.value.spec.status == 401


def test_guest_token_on_cloud_path_is_admitted(tmp_path) -> None:
    db = _db(tmp_path)
    inv = guest.create_invite(db, device_id=DEVICE, label="", minutes=60,
                              via="cloud")
    sess = guest.redeem(db, token=inv["token"], device_id=DEVICE, via="cloud")
    claims = token_mod.verify_token(_secret(db), sess["token"], device_id=DEVICE)
    ctx = _ctx(via=CLOUD)
    info = auth._admit_guest(ctx, db, claims)  # noqa: SLF001
    assert info["guest"] is True
    assert info["role"] == "guest"
    assert info["invite_id"] == inv["invite_id"]


def test_revoked_guest_session_maps_to_e1023(tmp_path) -> None:
    db = _db(tmp_path)
    inv = guest.create_invite(db, device_id=DEVICE, label="", minutes=60,
                              via="cloud")
    sess = guest.redeem(db, token=inv["token"], device_id=DEVICE, via="cloud")
    claims = token_mod.verify_token(_secret(db), sess["token"], device_id=DEVICE)
    guest.kick_session(db, sess["client_id"])
    ctx = _ctx(via=CLOUD)
    with pytest.raises(AppError) as ei:
        auth._admit_guest(ctx, db, claims)  # noqa: SLF001
    assert ei.value.spec.code == "E1023"


def test_guest_cookie_is_separate_from_owner_cookie() -> None:
    """共用一个 cookie 名会让游客身份覆盖主人身份（最坏方向）。"""
    assert guest.GUEST_COOKIE != auth.CLIENT_COOKIE


# ------------------------------------------------------------------ 根可见性

def _st_for(*, guest_ids: list[str], owner_ids: list[str]) -> SimpleNamespace:
    cfg = SimpleNamespace(
        guest_root_ids=guest_ids,
        enabled_roots=[SimpleNamespace(id=i) for i in owner_ids],
    )
    return SimpleNamespace(cfg=cfg)


def _client_ctx(role: str) -> Ctx:
    ctx = Ctx(method="GET", path="/api/timeline", via=CLOUD)
    ctx.state["client"] = {"id": "c" * 32, "role": role,
                           "guest": role == "guest"}
    return ctx


def test_guest_sees_only_the_guest_root() -> None:
    from backend.app.handlers import common

    st = _st_for(guest_ids=["guest"], owner_ids=["library", "phone"])
    assert common.visible_root_ids(_client_ctx("guest"), st) == ["guest"]
    assert common.visible_root_ids(_client_ctx("owner"), st) == ["library", "phone"]


def test_naming_an_invisible_root_is_404() -> None:
    from backend.app.errors import AppError as AE
    from backend.app.handlers import common

    st = _st_for(guest_ids=["guest"], owner_ids=["library"])
    common.check_visible_root(_client_ctx("guest"), st, "guest")  # 放行
    common.check_visible_root(_client_ctx("guest"), st, "")      # 不点名也放行
    with pytest.raises(AE) as ei:
        common.check_visible_root(_client_ctx("guest"), st, "library")
    # 404 而不是 403：403 等于确认"这个根存在，只是你没权限"。
    assert ei.value.spec.status == 404
