"""云端到本机信任边界的攻击回归；所有状态都在 pytest 临时目录。"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from backend.app import auth
from backend.app.config import Config
from backend.app.core import Router
from backend.app.db import Database
from backend.app.handlers import auth as handlers
from backend.app.state import AppState
from backend.app.transports.cloud_tunnel import TunnelDispatcher
from backend.app.transports.local_http import LocalHttpTransport
from cloud.relay.body import JSON_BODY_LIMIT, RequestBodyTooLarge, read_json_object
from cloud.relay.db import CloudDB
from cloud.relay.main import create_app
from shared import client_token

DEVICE = "a" * 32
AGENT = "security-test-agent-token-only"
SECRET = client_token.secret_for_agent_token(AGENT)


@pytest.fixture
def local(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    cfg = Config(
        {
            "cloud": {"device_id": DEVICE, "agent_token": AGENT},
            "roots": [{"id": "library", "path": str(library), "mode": "readwrite"}],
        },
        path=tmp_path / "config.json",
        data_dir=tmp_path / "data",
    )
    db = Database(tmp_path / "test.db")
    db.init()
    st = AppState(cfg=cfg, db=db)
    router = Router(app=st)
    router.use(auth.auth_middleware)
    router.add("GET", "/api/clients/me", handlers.me)
    router.add("DELETE", "/api/devices/{client_id}", handlers.revoke_device)
    try:
        yield st, router
    finally:
        db.close_all()


def _token(cid="cloud-client"):
    return client_token.issue_token(SECRET, client_id=cid, device_id=DEVICE)


def _pair(st, code):
    auth.store_pair_code(st.db, code, st.device_id, "test")
    return auth.consume_pair_code(st.db, code, device_name="test", cfg=st.cfg)


def _get(st, router, token):
    with TestClient(
        LocalHttpTransport(st.cfg, router).build_app(), base_url="http://127.0.0.1"
    ) as client:
        return client.get("/api/clients/me", headers={"Authorization": f"Bearer {token}"})


@pytest.mark.parametrize("suffix", ["=", "==", "!", "@"])
def test_revoked_local_token_cannot_return_through_cloud_fallback(local, suffix):
    st, router = local
    cid, token = _pair(st, "123456")
    assert _get(st, router, token).status_code == 200
    st.db.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (cid,))
    assert _get(st, router, token).status_code == 403
    assert _get(st, router, token + suffix).status_code == 401


def test_signature_with_changed_unused_base64_bits_is_rejected():
    token = _token()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    alternative = token[:-1] + alphabet[alphabet.index(token[-1]) + 1]
    assert client_token.verify_token(SECRET, token, device_id=DEVICE)
    assert client_token.verify_token(SECRET, alternative, device_id=DEVICE) is None


def test_unseen_revoked_cloud_token_stays_revoked_after_restart(local):
    st, router = local
    token = _token()
    # 云端已撤销，但电脑从未见过这枚令牌。
    assert auth.sync_revoked_clients(st, ["cloud-client"]) >= 1
    assert auth.sync_revoked_clients(st, ["cloud-client"]) == 0
    st.db.close_all()
    assert _get(st, router, token).status_code == 401
    assert _get(st, router, token).status_code == 401


def test_new_signature_cannot_reactivate_revoked_local_client(local):
    st, router = local
    cid, _ = _pair(st, "123456")
    st.db.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (cid,))
    assert _get(st, router, _token(cid)).status_code == 401


def test_revocation_lookup_failure_denies_access(local, monkeypatch):
    st, router = local
    token = _token()
    assert _get(st, router, token).status_code == 200
    original = st.db.query_one

    def broken(sql, *args, **kwargs):
        if "FROM cloud_clients" in sql:
            raise sqlite3.OperationalError("test revocation cache unavailable")
        return original(sql, *args, **kwargs)

    monkeypatch.setattr(Database, "query_one", lambda self, sql, *a, **kw: broken(sql, *a, **kw))
    assert _get(st, router, token).status_code == 401


@pytest.mark.parametrize("ip", ["127.0.0.1", "::1", "::ffff:127.0.0.1", "localhost"])
def test_cloud_frame_cannot_gain_local_privileges(local, ip):
    st, router = local
    owner, _ = _pair(st, "123456")
    _, viewer_token = _pair(st, "654321")

    class Mux:
        status = None

        async def res_head(self, rid, status, *args, **kwargs):
            self.status = status

        async def res_body(self, *args):
            pass

        async def res_end(self, *args):
            pass

    async def body():
        if False:
            yield b""

    req = SimpleNamespace(
        req_id=1,
        iter_body=body,
        head={
            "method": "DELETE",
            "path": f"/api/devices/{owner}",
            "client_ip": ip,
            "headers": {"authorization": f"Bearer {viewer_token}"},
        },
    )
    mux = Mux()
    asyncio.run(TunnelDispatcher(router, st.cfg).handle(mux, req))
    assert mux.status == 403
    assert st.db.query_one("SELECT revoked FROM devices WHERE id = ?", (owner,))["revoked"] == 0


def test_local_http_does_not_trust_forwarded_identity(local):
    st, router = local

    async def run():
        transport = LocalHttpTransport(st.cfg, router, host="127.0.0.1", port=0)
        try:
            await transport.start()
            assert transport._server.config.proxy_headers is False
        finally:
            await transport.stop()

    asyncio.run(run())


@pytest.mark.parametrize("chunked", [False, True])
def test_anonymous_cloud_login_body_is_bounded(tmp_path, chunked):
    db = CloudDB(str(tmp_path / "cloud.db"))
    app = create_app(db, hosts=["testserver"])
    payload = b'{"padding":"' + b"x" * 65536 + b'"}'
    content = iter([payload[:32768], payload[32768:]]) if chunked else payload
    try:
        with TestClient(app) as client:
            response = client.post("/api/auth/login", content=content)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "E1006"
    finally:
        db.close_all()


def test_chunked_json_stops_reading_as_soon_as_limit_is_exceeded():
    reads = 0

    async def receive():
        nonlocal reads
        reads += 1
        assert reads <= 2, "不应在超限后继续读取请求体"
        return {
            "type": "http.request",
            "body": b"x" * (JSON_BODY_LIMIT // 2 + 1),
            "more_body": True,
        }

    request = Request({"type": "http", "headers": []}, receive)
    with pytest.raises(RequestBodyTooLarge):
        asyncio.run(read_json_object(request))
    assert reads == 2


def test_deeply_nested_cloud_json_returns_bad_request(tmp_path):
    db = CloudDB(str(tmp_path / "cloud.db"))
    try:
        with TestClient(create_app(db, hosts=["testserver"])) as client:
            response = client.post("/api/auth/login", content=b"[" * 2000 + b"]" * 2000)
        assert response.status_code == 400
    finally:
        db.close_all()


@pytest.mark.parametrize("scenario", ["local-revocation", "unseen-cloud-revocation"])
def test_revocation_across_real_http_and_websocket_tunnel(tmp_path, scenario):
    from backend.tests.test_p1_tunnel import _Rig

    async def run():
        rig = _Rig(tmp_path)
        try:
            await rig.start()
            await rig.wait_tunnel()
            if scenario == "local-revocation":
                code = await rig.lan_pair_code()
                status, body, _ = await rig.lan.arequest("POST", "/api/pair", body={"code": code})
                assert status == 201
                token = body["client_token"]
                rig.state.db.execute(
                    "UPDATE devices SET revoked = 1 WHERE id = ?", (body["client_id"],)
                )
                replay = token + "="
            else:
                await rig.apair()
                token = rig.client_token
                status, _, _ = await rig.cloud.arequest(
                    "DELETE",
                    f"/api/devices/{rig.device_id}/clients/{rig.client_id}",
                    token=rig.account_token,
                )
                assert status == 204
                assert auth.sync_revoked_clients(rig.state, [rig.client_id]) >= 1
                replay = token
            for url, path in [
                (rig.lan, "/api/timeline"),
                (rig.cloud, rig.tunnel_path("/api/timeline")),
            ]:
                status, _, _ = await url.arequest("GET", path, token=replay)
                assert status in (401, 403)
        finally:
            await rig.stop()

    asyncio.run(run())
