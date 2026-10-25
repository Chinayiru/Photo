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


# --------------------------------------------------- ① 签发的令牌只认新密钥


def test_mint_client_signs_with_a_secret_the_cloud_never_sees(state) -> None:
    """令牌必须由电脑端密钥签：用云端算得出的 `sha256(agent_token)` 验不过。"""
    result = _handle(state, "mint_client", {"name": "手机", "ttl_days": 90})
    token = result["token"]
    assert token.startswith("pbc1.")

    fresh = agent_control.client_signing_secret(state.db)
    assert token_mod.verify_token(
        fresh, token, device_id=_cloud_device_id()) is not None

    legacy = agent_auth.legacy_tunnel_secret(state.cfg)
    if legacy:                       # 没配 agent_token 时为空串，跳过
        assert token_mod.verify_token(
            legacy, token, device_id=_cloud_device_id()) is None, (
            "旧密钥还能验签 ⇒ 云端库泄露即可伪造令牌，边界没关上")


def test_mint_client_does_not_write_a_local_devices_row(state) -> None:
    """签发不落库：落库是 `_verify_cloud_token()` 的职责，写两处必然分叉。"""
    _handle(state, "mint_client", {"name": "手机", "ttl_days": 90})
    assert state.db.query_one("SELECT COUNT(*) AS n FROM devices")["n"] == 0


# --------------------------------------------------- ② 云端请电脑判一枚令牌


def test_verify_client_accepts_what_this_computer_signed(state) -> None:
    minted = _handle(state, "mint_client", {"name": "手机", "ttl_days": 30})
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
