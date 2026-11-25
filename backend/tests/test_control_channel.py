"""控制通道（`/.pb/control`）与"签发权回到电脑端"的回归判据。

## 这两条边界原来长什么样（`docs/06` §13）

1. 手机令牌的 HMAC 密钥是 `sha256(agent_token)`，而 `agent_token` 的哈希
   就存在**云端**数据库里 —— 库一泄露，攻击者能离线签出任意手机令牌。
2. 用户在手机上"移除设备"后，电脑端靠轮询 `GET /api/clients/revoked` 才发现，
   最长延迟一个轮询周期。

修法：把签名权搬回电脑端（`client_signing_secret()` 只存在电脑的 `settings`
表里），云端要签发/验签就通过 `/.pb/control` 问一句；撤销时云端主动推。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backend.app import auth as agent_auth            # noqa: E402
from backend.app import control as agent_control     # noqa: E402
from backend.app import pairing as pairing_mod      # noqa: E402
from backend.app.db import Database                  # noqa: E402
from shared import client_token as token_mod         # noqa: E402


CLOUD_DEVICE_ID = "cc6c80c4e9006779708735d1d676e030"
AGENT_TOKEN = "TestAgentToken-0123456789abcdef"


@pytest.fixture()
def state(tmp_path: Path):
    db = Database(tmp_path / "agent.db")
    db.init()
    values = {"cloud.device_id": CLOUD_DEVICE_ID,
              "cloud.agent_token": AGENT_TOKEN}

    def _get(key, default=None):
        return values.get(key, default)

    cfg = SimpleNamespace(get=_get)
    return SimpleNamespace(db=db, cfg=cfg, device_id="b504b57e0b6a41d0",
                           device_name="YiRu")


def _cloud_device_id() -> str:
    return CLOUD_DEVICE_ID


def _handle(state, action: str, payload: dict):
    return agent_control.handle(state, state.cfg, action, payload)


# --------------------------------------------------- ① 签发必须经过本机批准
#
# 2026-11-24 的渗透报告 A1.3 把这条边界又往前推了一步：原来"云端问一句、
# 电脑就签"，在"中继进程已被拿下"的威胁下依然是个洞 —— 中继手里有
# `agent_token`，它能扮演云端把 `mint_client` 发过来。现在控制通道只剩
# "登记申请"，签发要等用户在托盘里核对 6 位数字后点「批准」。

#: 测试用的手机设备密钥（64 位小写 hex）。
DEVICE_KEY = "c3" * 32
OTHER_KEY = "d4" * 32


def _request(state, *, name: str = "手机", ttl_days: int = 90,
             key: str = DEVICE_KEY, device_id: str = "") -> dict:
    return _handle(state, "request_pair", {
        "device_id": device_id or _cloud_device_id(),
        "client_name": name, "device_key": key, "ttl_days": ttl_days})


def _approve(state, request_id: str, *, approve: bool = True) -> dict:
    return pairing_mod.decide(state.db, request_id, approve=approve,
                              source="tray")


def _redeem(state, request_id: str, *, key: str = DEVICE_KEY) -> dict:
    return _handle(state, "pair_status",
                   {"request_id": request_id, "device_key": key})


def _pair(state, **kw) -> dict:
    """发起 + 批准 + 兑换，返回最后一步的结果（`approved` 时带令牌）。"""
    pending = _request(state, **kw)
    _approve(state, pending["request_id"])
    return _redeem(state, pending["request_id"], key=kw.get("key", DEVICE_KEY))


def test_request_pair_alone_signs_nothing(state) -> None:
    """申请只是登记：没有用户批准，库里一枚令牌都不该出现。"""
    pending = _request(state)
    assert pending["state"] == "pending"
    assert len(pending["verify_code"]) == 6
    assert len(pending["fingerprint"]) == 6

    waiting = _redeem(state, pending["request_id"])
    assert waiting["state"] == "pending"
    assert "token" not in waiting
    assert state.db.query_one("SELECT COUNT(*) AS n FROM devices")["n"] == 0


def test_request_pair_rejects_a_foreign_device_id(state) -> None:
    """云端不能替另一台电脑申请：`device_id` 必须与本机一致。"""
    with pytest.raises(agent_control.ControlError) as exc:
        _request(state, device_id="f" * 32)
    assert exc.value.code == "E5003"
    assert exc.value.status == 409


def test_approved_request_signs_with_the_computer_secret(state) -> None:
    """令牌由电脑端密钥签：用云端算得出的 `sha256(agent_token)` 验不过。"""
    minted = _pair(state, name="手机", ttl_days=90)
    assert minted["state"] == "approved"
    token = minted["token"]
    assert token.startswith("pbc1.")

    fresh = agent_control.client_signing_secret(state.db)
    claims = token_mod.verify_token(fresh, token,
                                    device_id=_cloud_device_id())
    assert claims is not None
    assert claims["sub"] == minted["client_id"]

    legacy = agent_auth.legacy_tunnel_secret(state.cfg)
    if legacy:                       # 没配 agent_token 时为空串，跳过
        assert token_mod.verify_token(
            legacy, token, device_id=_cloud_device_id()) is None, (
            "旧密钥还能验签 ⇒ 云端库泄露即可伪造令牌，边界没关上")


def test_pairing_does_not_write_a_local_devices_row(state) -> None:
    """签发不落库：落库是 `_verify_cloud_token()` 的职责，写两处必然分叉。"""
    _pair(state)
    assert state.db.query_one("SELECT COUNT(*) AS n FROM devices")["n"] == 0


def test_token_can_only_be_redeemed_once(state) -> None:
    """同一条申请只能取一次令牌（重放第二次必须被拒）。"""
    pending = _request(state)
    _approve(state, pending["request_id"])
    first = _redeem(state, pending["request_id"])
    assert first["state"] == "approved"
    second = _redeem(state, pending["request_id"])
    assert second["state"] == "rejected"
    assert "token" not in second


def test_a_different_device_key_cannot_redeem(state) -> None:
    """另一台手机拿到 `request_id` 也换不走令牌（密钥绑住这次申请）。

    对手机来说这是**终态结论**（`state=rejected`），不是"电脑不在线" ——
    云端把它转成 403 E2008，前端才会清掉本地密钥、重新发一份申请。
    """
    pending = _request(state)
    _approve(state, pending["request_id"])
    result = _redeem(state, pending["request_id"], key=OTHER_KEY)
    assert result["state"] == "rejected"
    assert "token" not in result
    # 申请不作废：合法设备随后仍然能兑换
    assert _redeem(state, pending["request_id"])["state"] == "approved"


def test_rejected_request_never_yields_a_token(state) -> None:
    """拒绝是终态：再兑换只能拿到"已拒绝"，不会补签。"""
    pending = _request(state)
    _approve(state, pending["request_id"], approve=False)
    result = _redeem(state, pending["request_id"])
    assert result["state"] == "rejected"
    assert "token" not in result


def test_mint_client_is_refused_outright(state) -> None:
    """旧动作保留只为显式拒绝 —— 渗透报告 A1.3 就是这条动作造的洞。"""
    with pytest.raises(agent_control.ControlError) as exc:
        _handle(state, "mint_client", {"name": "手机", "ttl_days": 90})
    assert exc.value.code == "E5015"
    assert exc.value.status == 403
    assert state.db.query_one("SELECT COUNT(*) AS n FROM pair_requests")["n"] == 0


def test_ping_reveals_nothing_about_this_computer(state) -> None:
    """探活只回 ok：设备名/版本/设备 ID 不该给未鉴权的调用方（A1.2）。"""
    assert _handle(state, "ping", {}) == {"ok": True}


# --------------------------------------------------- ② 云端请电脑判一枚令牌


def test_verify_client_accepts_what_this_computer_signed(state) -> None:
    minted = _pair(state, name="手机", ttl_days=30)
    verdict = _handle(state, "verify_client", {"token": minted["token"]})
    assert verdict["valid"] is True
    assert verdict["client_id"] == minted["client_id"]
    assert verdict["name"] == "手机"


def test_verify_client_rejects_a_forged_token(state) -> None:
    forged = token_mod.issue_token(
        "0" * 64, client_id="deadbeefdeadbeef",
        device_id=_cloud_device_id(), ttl_days=90)
    assert _handle(state, "verify_client",
                   {"token": forged})["valid"] is False


def test_refresh_client_preserves_client_id_and_rotates_token(state) -> None:
    old = _pair(state, name="手机", ttl_days=1)
    refreshed = _handle(state, "refresh_client", {
        "token": old["token"], "client_id": old["client_id"]})
    assert refreshed["client_id"] == old["client_id"]
    assert refreshed["token"] != old["token"]
    claims = token_mod.verify_token(
        agent_control.client_signing_secret(state.db), refreshed["token"],
        device_id=CLOUD_DEVICE_ID)
    assert claims is not None
    assert claims["sub"] == old["client_id"]


def test_refresh_refuses_a_revoked_client(state) -> None:
    """撤销过的设备不能靠续期把令牌续回来。"""
    old = _pair(state, ttl_days=1)
    agent_auth.sync_revoked_clients(state, [old["client_id"]])
    with pytest.raises(agent_control.ControlError) as exc:
        _handle(state, "refresh_client",
                {"token": old["token"], "client_id": old["client_id"]})
    assert exc.value.code == "E5004"
    assert exc.value.status == 403

# --------------------------------------------------- ③ 旧密钥默认关闭


def test_legacy_secret_is_rejected_by_default(state) -> None:
    """默认配置下，`sha256(agent_token)` 签的令牌一律不认。"""
    legacy = agent_auth.legacy_tunnel_secret(state.cfg)
    if not legacy:
        pytest.skip("没有配置 cloud.agent_token")
    old = token_mod.issue_token(legacy, client_id="old0000000000000",
                                device_id=_cloud_device_id(), ttl_days=90)
    assert agent_auth.legacy_window_open(state.db) is False
    assert _handle(state, "verify_client", {"token": old})["valid"] is False


def test_legacy_window_can_be_opened_explicitly(state) -> None:
    """运维开窗口后老令牌在窗口期内可用；窗口一过立刻失效。"""
    legacy = agent_auth.legacy_tunnel_secret(state.cfg)
    if not legacy:
        pytest.skip("没有配置 cloud.agent_token")
    state.db.execute(
        "INSERT INTO devices(id, name, created_at, revoked, allow_legacy_until)"
        " VALUES ('a' || 'b' * 15, '老手机', ?, 0, ?)",
        (time.time(), time.time() + 7 * 86400))
    assert agent_auth.legacy_window_open(state.db) is True

    old = token_mod.issue_token(legacy, client_id="old0000000000000",
                                device_id=_cloud_device_id(), ttl_days=90)
    assert _handle(state, "verify_client", {"token": old})["valid"] is True

    state.db.execute("UPDATE devices SET allow_legacy_until = 0")
    assert _handle(state, "verify_client", {"token": old})["valid"] is False


# --------------------------------------------------- ④ 撤销立刻生效


def test_revoke_clients_reports_how_many_rows_it_touched(state) -> None:
    """云端推撤销时拿到的行数就是它要的"立刻生效"的凭据。

    落库细节由 `auth.sync_revoked_clients()` 负责（它用 `expires_at = 0`
    表示撤销，而不是删行 —— 删了之后下一次验签会把行重新插回来，
    撤销就失效了）。这里只判它对控制通道的契约：一定回一个整数。
    """
    result = _handle(state, "revoke_clients", {"ids": ["x" * 16]})
    assert isinstance(result["revoked"], int)

    with pytest.raises(agent_control.ControlError):
        _handle(state, "revoke_clients", {"ids": ["y"] * 201})
