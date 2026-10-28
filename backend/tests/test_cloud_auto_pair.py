"""账号登录自动配对（`auto: true`）的回归判据。

## 这条功能做了什么、代价是什么

手机上登录**云端账号**后，只要那台电脑属于这个账号并且在线，云端直接
签发 `client_token`，用户不必再照抄电脑屏幕上的 6 位码。

代价是明确的：**账号口令从此等于照片访问权**。原来那 6 位码是"你就在
电脑旁边"的证据，自动配对把它换成了"你知道账号密码"。所以这个文件
测的不是"能不能配"，而是那几道必须一起成立的约束：

* 没有 `account_token` → 401（谁说"我已经登录了"都不算数）
* 设备不属于我 → 404（与码路径同一口径，不给设备存在性 oracle）
* 设备被撤销 → 403
* 设备不在线 → 503（签了也用不上，还会把"电脑没起来"伪装成"配对坏了"）
* 每账号 + 每 IP 5 次/分钟（口令泄露时限速）
* **码路径一个字都不能变**：`auto` 缺席时仍然走 6 位码校验
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from _fake_control import FAKE                     # noqa: E402
from cloud.relay import auth as cloud_auth          # noqa: E402
from cloud.relay import control as cloud_control    # noqa: E402
from cloud.relay import db as cloud_db             # noqa: E402
from cloud.relay import main as cloud_main         # noqa: E402
from cloud.relay import pairing as cloud_pairing   # noqa: E402
from cloud.relay import security                   # noqa: E402
from shared import client_token as shared_token    # noqa: E402

from starlette.testclient import TestClient        # noqa: E402

PASSWORD = "Correct-Horse-9"
DEVICE_NAME = "家里的电脑"


@pytest.fixture(autouse=True)
def _clean_auto_attempts(monkeypatch):
    """自动配对的计数是模块级字典，用例之间必须清空。

    另外把控制通道换成本地替身：2026-10-26 起令牌由电脑端签，
    `cloud/relay/pairing.py` 会真的去问隧道要令牌（见 `_fake_control`）。
    """
    FAKE.calls.clear()
    FAKE.online = True
    monkeypatch.setattr(cloud_control, "call_agent", FAKE)
    cloud_pairing._AUTO_ATTEMPTS.clear()
    cloud_auth._login_limiter.reset()
    cloud_auth._email_limiter.reset()
    yield
    cloud_pairing._AUTO_ATTEMPTS.clear()
    cloud_auth._login_limiter.reset()
    cloud_auth._email_limiter.reset()


def _app(tmp_path: Path):
    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    database.init()
    return cloud_main.create_app(database, hosts=[], dist=None)


def _account(client: TestClient, email: str = "owner@example.com") -> str:
    """建账号并登录，返回 `account_token`。"""
    database = client.app.state.db
    cloud_auth.create_account(database, email, PASSWORD)
    response = client.post("/api/auth/login",
                           json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["account_token"])


def _device(client: TestClient, *, email: str = "owner@example.com",
            name: str = DEVICE_NAME, revoked: int = 0) -> tuple[str, str]:
    """插一台设备，返回 `(device_id, agent_token 明文)`。

    ★ `devices.agent_token` 这一列在生产里存的是 **sha256**（`docs/05` §2.2，
    `find_device_by_agent_token` 就是拿 `hash_token(明文)` 去查的）。云端
    签 `client_token` 时直接把这一列当 HMAC 密钥用 —— 也就是
    `sha256(agent_token)`，与电脑端 `_tunnel_secret()` 算出来的完全一致。
    测试若把明文塞进这一列，签出来的令牌在电脑端一定验不过，而症状正是
    线上最怕的那句"配对成功、之后全 401"。
    """
    database = client.app.state.db
    account = database.query_one("SELECT id FROM accounts WHERE email = ?",
                                 (email,))
    device_id = security.new_device_id()
    agent_token = security.new_token()
    database.execute(
        "INSERT INTO devices(id, account_id, name, agent_token, created_at, "
        "revoked) VALUES (?, ?, ?, ?, ?, ?)",
        (device_id, str(account["id"]), name,
         security.hash_token(agent_token), time.time(), revoked))
    # 替身用与电脑端同一个密钥签名，这样"云端签的令牌电脑端能验"仍然可测。
    FAKE.secret = shared_token.secret_for_agent_token(agent_token)
    return device_id, agent_token


def _online(app, device_id: str) -> None:
    """伪造"这台电脑的隧道连着"。

    真场景里这行由 `/agent/ws` 握手成功时写入；自动配对只问
    `registry.get(device_id) is not None`，所以塞一个只带两个属性的替身
    就够了（没必要为了这一条起真隧道）。
    """
    asyncio.run(app.state.registry.register(
        SimpleNamespace(device_id=device_id, session_id="test-session")))


def _auto(client: TestClient, device_id: str, token: str,
          *, name: str = "手机"):
    return client.post(f"/api/devices/{device_id}/pair",
                       json={"auto": True, "client_name": name},
                       headers={"Authorization": f"Bearer {token}"})


# ------------------------------------------------------------------ 主路径


def test_auto_pair_issues_a_token_the_agent_can_verify(tmp_path: Path) -> None:
    """线上最关键的一条：云端签出来的令牌，电脑端必须能离线验签。

    云端的密钥是 `devices.agent_token`，电脑端手里是同一个字符串 ——
    两边算出同一个 HMAC。这条如果不成立，用户会看到"配对成功但一张图
    都打不开，每次都是 401 E2002"。
    """
    client = TestClient(_app(tmp_path))
    token = _account(client)
    device_id, agent_token = _device(client)
    _online(client.app, device_id)

    response = _auto(client, device_id, token)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["device_id"] == device_id
    assert body["device_name"] == DEVICE_NAME

    claims = shared_token.verify_token(
        shared_token.secret_for_agent_token(agent_token),
        str(body["client_token"]))
    assert claims is not None, "令牌验签失败：云端与电脑端的密钥不同源"
    assert claims["dev"] == device_id
    assert claims["sub"] == body["client_id"]

    # 库里只应有哈希，且绑定的是这台设备
    row = client.app.state.db.query_one(
        "SELECT device_id, token_hash FROM clients WHERE id = ?",
        (body["client_id"],))
    assert str(row["device_id"]) == device_id
    assert str(row["token_hash"]) != str(body["client_token"])


def test_client_me_and_token_refresh_endpoints(tmp_path: Path, monkeypatch) -> None:
    client = TestClient(_app(tmp_path))
    token = _account(client)
    device_id, _agent_token = _device(client)
    _online(client.app, device_id)
    paired = _auto(client, device_id, token).json()
    client_token = paired["client_token"]

    info = client.get("/api/clients/me",
                      headers={"Authorization": f"Bearer {client_token}"})
    assert info.status_code == 200, info.text
    assert info.json()["client_id"] == paired["client_id"]
    assert info.json()["device_id"] == device_id

    db = client.app.state.db
    db.execute("UPDATE clients SET expires_at = ? WHERE id = ?",
               (time.time() + 3600, paired["client_id"]))

    async def refresh_agent(_conn, action, payload):
        assert action == "refresh_client"
        assert payload["client_id"] == paired["client_id"]
        return {
            "client_id": paired["client_id"],
            "token": "rotated-token-for-test",
            "expires_at": time.time() + 90 * 86400,
        }

    monkeypatch.setattr(cloud_control, "call_agent", refresh_agent)
    refreshed = client.post(
        "/api/clients/token/refresh",
        headers={"Authorization": f"Bearer {client_token}"})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["client_token"] == "rotated-token-for-test"
    assert db.query_one("SELECT token_hash FROM clients WHERE id = ?",
                        (paired["client_id"],))["token_hash"] == security.hash_token(
                            "rotated-token-for-test")


def test_relay_rejects_unlisted_http_host(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))
    response = client.get("/api/health", headers={"Host": "evil.example"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E1004"
    assert response.json()["error"]["request_id"]
    assert response.headers["x-request-id"] == response.json()["error"]["request_id"]
    method_dir = tmp_path / "method"
    method_dir.mkdir()
    wrong_method = TestClient(_app(method_dir)).post("/api/clients/me")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"]["code"] == "E1018"


def test_guest_redeem_uses_public_not_pair_rate_bucket() -> None:
    from starlette.requests import Request

    from cloud.relay.forward import _is_pair

    def request(path: str) -> Request:
        return Request({
            "type": "http", "method": "POST", "path": path,
            "headers": [], "query_string": b"", "scheme": "https",
            "server": ("relay.example", 443), "client": ("192.0.2.1", 1234),
            "http_version": "1.1",
        })

    assert _is_pair(request("/d/device/api/pair")) is True
    assert _is_pair(request("/d/device/api/guest/redeem")) is False


def test_auto_pair_works_twice_for_the_same_phone(tmp_path: Path) -> None:
    """重装 App / 清缓存后再登一次，应该直接再配一枚新令牌。

    码路径是一次性的（同一枚码不能换两次），自动配对没有"码"这回事，
    若照搬码的一次性语义，用户会卡在"第二次登录必然失败"。
    """
    client = TestClient(_app(tmp_path))
    token = _account(client)
    device_id, _ = _device(client)
    _online(client.app, device_id)

    first = _auto(client, device_id, token)
    second = _auto(client, device_id, token)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["client_id"] != second.json()["client_id"]

    rows = client.app.state.db.query(
        "SELECT id FROM clients WHERE device_id = ?", (device_id,))
    assert len(rows) == 2, "两枚令牌都应该在库里（各自可被单独撤销）"


# ------------------------------------------------------------------ 四道闸门


def test_auto_pair_needs_a_logged_in_account(tmp_path: Path) -> None:
    """没有 `account_token` → 401。设备在线也救不了。"""
    client = TestClient(_app(tmp_path))
    _account(client)
    device_id, _ = _device(client)
    _online(client.app, device_id)

    response = client.post(f"/api/devices/{device_id}/pair",
                           json={"auto": True, "client_name": "手机"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "E2001"


def test_auto_pair_hides_devices_that_are_not_mine(tmp_path: Path) -> None:
    """别人的设备 → 404（与码路径一致，不泄露设备存在性）。"""
    client = TestClient(_app(tmp_path))
    mine = _account(client)
    _account(client, "other@example.com")
    other_device, _ = _device(client, email="other@example.com")
    _online(client.app, other_device)

    response = _auto(client, other_device, mine)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "E1004"


def test_auto_pair_refuses_a_revoked_device(tmp_path: Path) -> None:
    """被撤销的设备 → 403 E2006（先撤销再配对是常见错觉）。"""
    client = TestClient(_app(tmp_path))
    token = _account(client)
    device_id, _ = _device(client, revoked=1)
    _online(client.app, device_id)

    response = _auto(client, device_id, token)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "E2006"


def test_auto_pair_refuses_an_offline_device(tmp_path: Path) -> None:
    """电脑没连上来 → 503 E2007。

    不在这里卡一道的话，用户会拿到一枚"看起来成功"的令牌，然后在时间线
    上收到一连串 503/E5009，把"电脑根本没启动"误判成"配对坏了"。
    """
    client = TestClient(_app(tmp_path))
    token = _account(client)
    device_id, _ = _device(client)      # 故意不调 _online()

    response = _auto(client, device_id, token)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "E2007"


def test_auto_pair_is_rate_limited_per_account_and_ip(tmp_path: Path) -> None:
    """同一账号 + 同一出口 IP，第 6 次开始 429（额度 5/分钟）。"""
    client = TestClient(_app(tmp_path))
    token = _account(client)
    device_id, _ = _device(client)
    _online(client.app, device_id)

    for _ in range(cloud_pairing._AUTO_PER_WINDOW):
        assert _auto(client, device_id, token).status_code == 200
    blocked = _auto(client, device_id, token)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "E4004"
    assert int(blocked.headers.get("retry-after") or 0) > 0


def test_auto_pair_rejects_a_blank_client_name(tmp_path: Path) -> None:
    """手机名还是要校验：它会作为设备名显示在「设备管理」里。"""
    client = TestClient(_app(tmp_path))
    token = _account(client)
    device_id, _ = _device(client)
    _online(client.app, device_id)

    response = _auto(client, device_id, token, name="   ")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "E1001"


# ------------------------------------------------------------ 码路径不受影响


def test_code_pairing_semantics_are_unchanged(tmp_path: Path) -> None:
    """`auto` 缺席时仍然是码路径：没码 → E1001，有码 → 成功。

    自动配对是**加法**。这里钉住"旧路径没被顺手改坏"，否则已经装了旧版
    App 的用户会在升级后突然配不上。
    """
    client = TestClient(_app(tmp_path))
    token = _account(client)
    device_id, _ = _device(client)
    _online(client.app, device_id)

    no_code = client.post(f"/api/devices/{device_id}/pair",
                          json={"client_name": "手机"},
                          headers={"Authorization": f"Bearer {token}"})
    assert no_code.status_code == 400
    assert no_code.json()["error"]["code"] == "E1001"

    code = security.new_pair_code()
    client.app.state.db.execute(
        "INSERT INTO pair_codes(code, device_id, expires_at, tries, "
        "created_at) VALUES (?, ?, ?, 0, ?)",
        (code, device_id, time.time() + 300, time.time()))
    ok = client.post(f"/api/devices/{device_id}/pair",
                     json={"code": code, "client_name": "手机"},
                     headers={"Authorization": f"Bearer {token}"})
    assert ok.status_code == 200, ok.text
    # 码是一次性的：再用一次必须失败
    again = client.post(f"/api/devices/{device_id}/pair",
                        json={"code": code, "client_name": "手机"},
                        headers={"Authorization": f"Bearer {token}"})
    assert again.status_code == 409
