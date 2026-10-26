"""审计 F04：配对把「等电脑签发」移出事务。

## 这一条修的是什么

原来的 `redeem_pair_code()` 在一个 `BEGIN IMMEDIATE` 里做完三件事：抢配对
码、**`await` 电脑端签发**、写 `clients`。中间那个 `await` 要经隧道走一趟
控制通道（超时 5 秒），于是云端库的写锁在整个网络往返期间被占着 —— 登录、
心跳、上传登记、设备列表全排在后面。电脑卡一下或隧道抖一下，整个云端被
拖到超时；而配对本身还会因为外层事务被异常回滚而**静默丢掉**用户刚输的码。

## 拆法（三个函数，边界写在名字上）

* `_claim_code()` —— F04a 短事务：条件 `UPDATE ... WHERE used_at IS NULL`，
  影响行数就是那把锁，立即提交。
* `_mint_via_agent()` —— F04b 事务外：`await` 电脑签发，**不得持有事务或行锁**。
* `_write_client()` —— F04c 短事务：`INSERT clients`。

## 失败路径（这批用例的重点）

签不出来（电脑离线 / 隧道断 / 回来的内容不成立）或写库失败，都必须
`_release_code()` 把码**还回去**：用户拿着一个"已使用"的码是死路，他自己
没有任何办法恢复。释放只认自己那一次抢码（`WHERE used_at = ?`），不误伤。

所以这里断言的不是"能配对"，而是：**事务里没有 await、失败后码还能用、
并发抢码只有一个赢家**。
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from _fake_control import FAKE                      # noqa: E402
from cloud.relay import auth as cloud_auth           # noqa: E402
from cloud.relay import control as cloud_control     # noqa: E402
from cloud.relay import db as cloud_db               # noqa: E402
from cloud.relay import main as cloud_main           # noqa: E402
from cloud.relay import pairing as cloud_pairing     # noqa: E402
from cloud.relay import security                     # noqa: E402
from shared import client_token as shared_token      # noqa: E402

PASSWORD = "Correct-Horse-9"
CODE = "314159"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    FAKE.calls.clear()
    FAKE.online = True
    FAKE.secret = ""
    monkeypatch.setattr(cloud_control, "call_agent", FAKE)
    cloud_pairing._ATTEMPTS.clear()
    cloud_pairing._AUTO_ATTEMPTS.clear()
    cloud_auth._login_limiter.reset()
    cloud_auth._email_limiter.reset()
    yield
    cloud_pairing._ATTEMPTS.clear()
    cloud_pairing._AUTO_ATTEMPTS.clear()


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _app(tmp_path: Path):
    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    database.init()
    return cloud_main.create_app(database, hosts=[], dist=None)


def _account(client: TestClient, email: str = "owner@example.com") -> str:
    database = client.app.state.db
    cloud_auth.create_account(database, email, PASSWORD)
    response = client.post("/api/auth/login",
                           json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["account_token"])


def _device(client: TestClient) -> tuple[str, str]:
    """插一台设备，返回 `(device_id, agent_token 明文)`。"""
    database = client.app.state.db
    account = database.query_one("SELECT id FROM accounts WHERE email = ?",
                                 ("owner@example.com",))
    device_id = security.new_device_id()
    agent_token = security.new_token()
    database.execute(
        "INSERT INTO devices(id, account_id, name, agent_token, created_at, "
        "revoked) VALUES (?, ?, ?, ?, ?, 0)",
        (device_id, str(account["id"]), "家里的电脑",
         security.hash_token(agent_token), time.time()))
    FAKE.secret = shared_token.secret_for_agent_token(agent_token)
    return device_id, agent_token


def _pair_code(client: TestClient, device_id: str, code: str = CODE) -> str:
    database = client.app.state.db
    now = time.time()
    database.execute(
        "INSERT INTO pair_codes(code, device_id, expires_at, tries, "
        "created_at) VALUES (?, ?, ?, 0, ?)",
        (code, device_id, now + 300, now))
    return code


def _post_pair(client: TestClient, device_id: str, token: str, *,
               code: str = CODE):
    return client.post(f"/api/devices/{device_id}/pair",
                       headers={"Authorization": f"Bearer {token}"},
                       json={"code": code, "client_name": "我的手机"})


def _used_at(client: TestClient, code: str):
    row = client.app.state.db.query_one(
        "SELECT used_at FROM pair_codes WHERE code = ?", (code,))
    return None if row is None else row["used_at"]


def _client_rows(client: TestClient) -> int:
    row = client.app.state.db.query_one("SELECT COUNT(*) AS n FROM clients")
    return int(row["n"])


class _TxnProbe:
    """记录 `db.transaction()` 的嵌套深度与次数。

    `CloudDB.transaction` 是只读属性，装不到实例上，所以直接替类的实现：
    计数器 + 原实现。`depth` 是**当前**嵌套深度，`max_depth` 是历史最深。
    """

    def __init__(self, monkeypatch) -> None:
        self._orig = cloud_db.CloudDB.transaction
        self.depth = 0
        self.max_depth = 0
        self.count = 0
        monkeypatch.setattr(cloud_db.CloudDB, "transaction",
                            _make_transaction_probe(self, self._orig))


def _make_transaction_probe(probe: "_TxnProbe", origin):
    """包一层计数器的 `CloudDB.transaction`。

    ★ 必须是**普通函数**（不是 `probe._txn` 这样的绑定方法）：`setattr` 到类上
    的绑定方法不会再被当作描述符绑定实例，`db.transaction()` 会少传一个 self。
    """

    def _txn(database):
        class _Cm:
            def __enter__(inner):
                probe.depth += 1
                probe.count += 1
                probe.max_depth = max(probe.max_depth, probe.depth)
                inner.cm = origin.__get__(database, type(database))()
                inner.conn = inner.cm.__enter__()
                return inner.conn

            def __exit__(inner, *exc):
                try:
                    return inner.cm.__exit__(*exc)
                finally:
                    probe.depth -= 1

        return _Cm()

    return _txn


def _spy_agent(probe: _TxnProbe, seen: list[tuple[str, int]]):
    async def call(conn, action, params, *, timeout=None):
        seen.append((action, probe.depth))
        return await FAKE(conn, action, params, timeout=timeout)

    return call


# ---------------------------------------------------------------------------
# 三段式的形状
# ---------------------------------------------------------------------------


def test_signing_happens_with_no_transaction_open(monkeypatch, tmp_path) -> None:
    """三段式最关键的性质：`mint_client` 那次 await 的时刻，事务深度必须是 0。"""
    app = _app(tmp_path)
    client = TestClient(app)
    token = _account(client)
    did, _ = _device(client)
    _pair_code(client, did)

    probe = _TxnProbe(monkeypatch)
    seen: list[tuple[str, int]] = []
    monkeypatch.setattr(cloud_control, "call_agent", _spy_agent(probe, seen))

    response = _post_pair(client, did, token)
    assert response.status_code == 200, response.text

    mint_depths = [d for action, d in seen if action == "mint_client"]
    assert mint_depths == [0], seen        # 签发那一刻没有任何事务
    assert "mint_client" in [a for a, _ in seen]
    # 抢码一段 + 写 clients 一段；不嵌套、不长
    assert probe.count >= 2, probe.count
    assert probe.max_depth == 1, probe.max_depth
    assert _client_rows(client) == 1


def test_claim_and_write_are_synchronous_short_steps() -> None:
    """两段短事务必须是同步函数：同步函数里写不出 `await`，也就偷不回事务里。"""
    import inspect

    for fn in (cloud_pairing._claim_code, cloud_pairing._release_code,
               cloud_pairing._write_client):
        assert not inspect.iscoroutinefunction(fn), fn.__name__
    assert inspect.iscoroutinefunction(cloud_pairing._mint_via_agent)


def test_mint_step_touches_no_database_transaction() -> None:
    """`_mint_via_agent()` 的源码里不许出现 `db.transaction(`。"""
    import inspect

    src = inspect.getsource(cloud_pairing._mint_via_agent)
    assert "db.transaction(" not in src
    assert "call_agent" in src


# ---------------------------------------------------------------------------
# 失败路径：码必须还回去
# ---------------------------------------------------------------------------


def test_agent_offline_releases_the_code_so_the_user_can_retry(tmp_path) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    token = _account(client)
    did, _ = _device(client)
    code = _pair_code(client, did)

    FAKE.online = False
    response = _post_pair(client, did, token)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "E2007"
    assert _used_at(client, code) is None      # 码回来了
    assert _client_rows(client) == 0

    # 电脑恢复在线后，同一个码还能用 —— 用户不必回电脑前重新生成
    FAKE.online = True
    response = _post_pair(client, did, token)
    assert response.status_code == 200, response.text
    assert _client_rows(client) == 1
    assert _used_at(client, code) is not None


def test_malformed_mint_answer_is_treated_as_failure(tmp_path) -> None:
    """电脑端回了个不成立的载荷（老版本 / 自己算错）→ 释放码，不写半条 clients。"""
    app = _app(tmp_path)
    client = TestClient(app)
    token = _account(client)
    did, _ = _device(client)
    code = _pair_code(client, did)

    async def broken(conn, action, params, *, timeout=None):
        if action == "mint_client":
            return {"ok": True, "client_id": "", "token": "", "expires_at": 0}
        return await FAKE(conn, action, params, timeout=timeout)

    cloud_control.call_agent = broken  # type: ignore[assignment]
    try:
        response = _post_pair(client, did, token)
    finally:
        cloud_control.call_agent = FAKE  # type: ignore[assignment]

    assert response.status_code == 503, response.text
    assert _used_at(client, code) is None
    assert _client_rows(client) == 0


def test_write_failure_releases_the_code(monkeypatch, tmp_path) -> None:
    app = _app(tmp_path)
    client = TestClient(app)
    token = _account(client)
    did, _ = _device(client)
    code = _pair_code(client, did)

    real_write = cloud_pairing._write_client
    monkeypatch.setattr(cloud_pairing, "_write_client",
                        lambda _db, **_kw: False)
    response = _post_pair(client, did, token)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "E2007"
    assert _used_at(client, code) is None

    # 换回真实现（`monkeypatch.undo()` 会把上层的 FAKE 替身也一起撤掉，别用）
    monkeypatch.setattr(cloud_pairing, "_write_client", real_write)
    response = _post_pair(client, did, token)
    assert response.status_code == 200, response.text


def test_release_only_undoes_its_own_claim(tmp_path) -> None:
    """释放必须认「自己那一次」的时间戳，不能把别人的抢码/已用状态抹掉。"""
    app = _app(tmp_path)
    client = TestClient(app)
    _account(client)
    did, _ = _device(client)
    code = _pair_code(client, did)
    database = client.app.state.db

    first = time.time()
    assert cloud_pairing._claim_code(database, code, first) is True
    assert cloud_pairing._claim_code(database, code, first + 1) is False

    # 用一个不匹配的时间戳释放 → 不命中（也不改行）
    assert cloud_pairing._release_code(database, code, first + 99) is False
    assert _used_at(client, code) == pytest.approx(first)

    # 用真正抢码的那一次释放 → 命中
    assert cloud_pairing._release_code(database, code, first) is True
    assert _used_at(client, code) is None


def test_second_request_loses_the_race_with_the_same_error_as_used(tmp_path) -> None:
    """并发抢码只有一个赢家；输家看到的是「已被使用」（E1005），不是 500。"""
    app = _app(tmp_path)
    client = TestClient(app)
    token = _account(client)
    did, _ = _device(client)
    code = _pair_code(client, did)

    assert _post_pair(client, did, token).status_code == 200
    second = _post_pair(client, did, token)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "E1005"
    assert _client_rows(client) == 1


def test_write_client_reports_duplicate_key_without_raising(tmp_path) -> None:
    """`_write_client()` 撞主键要返回 False（调用方负责释放码），不能抛。"""
    app = _app(tmp_path)
    client = TestClient(app)
    _account(client)
    did, _ = _device(client)
    database = client.app.state.db
    now = time.time()
    minted = {"client_id": "f" * 32, "token": "pbc1." + "x" * 40,
              "expires_at": now + 86400}

    assert cloud_pairing._write_client(database, did=did, client_name="手机",
                                       minted=minted, now=now) is True
    assert cloud_pairing._write_client(database, did=did, client_name="手机",
                                       minted=minted, now=now) is False
    assert _client_rows(client) == 1
