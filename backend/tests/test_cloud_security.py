"""云端安全加固的回归判据（2026-10-23 安全审计，`docs/11` ISSUE-030~034）。

## 每条用例都对应一次**实测过的**攻击

不是"安全最佳实践清单"，而是"这一条如果不成立，我们已经知道会怎么被打"：

* `netutil` 的四条 —— 部署拓扑是 `公网 → cloudflared → 127.0.0.1:8000`，
  修之前所有手机共用 `127.0.0.1` 一个限流桶：一处刷 10 次假登录，主人
  自己也登不上（实测 `429 Retry-After: 368`）。
* 登录锁定三条 —— 之前只有按 IP 的频次限制、没有按账号的锁定，口令策略
  又允许 8 位弱口令。
* 安全响应头四条 —— 之前这些头只写在 `cloud/Caddyfile` 里，换成
  Cloudflare quick tunnel 后 Caddy 不在路径上，头就没了（`docs/11`
  ISSUE-028）。浏览器侧实测：`Content-Security-Policy` 缺失。
* `/.git/config` 那条 —— 实测返回 `200 text/html`（单页兜底把扫描器最
  爱打的路径答成了"页面存在"）。
* 访问日志那条 —— uvicorn 的 access log 会把 `?code=NNNNNN`（一次性
  配对凭据，TTL 300 秒）与用户搜索词原样写进 `relay.log`。

## 为什么用 `TestClient` 而不是起真 uvicorn

这一层判据全是**进程内**行为（中间件、限流器、SQL 迁移），不需要真端口。
真端口的端到端验证在 `backend/tests/test_p1_pages.py` 与 `test_p1_tunnel.py`
里（那两条链路的教训是"必须起真 uvicorn"，与本文件不冲突）。
"""
from __future__ import annotations

import logging
import pathlib
import sys
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from _fake_control import FAKE                     # noqa: E402
from cloud.relay import auth as cloud_auth          # noqa: E402
from cloud.relay import control as cloud_control    # noqa: E402
from cloud.relay import db as cloud_db             # noqa: E402
from cloud.relay import main as cloud_main         # noqa: E402
from cloud.relay import netutil                    # noqa: E402
from cloud.relay import security                   # noqa: E402
from shared import client_token as shared_token    # noqa: E402

from starlette.testclient import TestClient        # noqa: E402


# --------------------------------------------------------------- 构造工具


def _fake_request(peer: str | None, *, xff: str | None = None) -> SimpleNamespace:
    """够 `netutil` 用的最小请求：只需要 `.client.host` 与 `.headers`。"""
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    client = None if peer is None else SimpleNamespace(host=peer)
    return SimpleNamespace(client=client, headers=headers)


def _dist(tmp_path: Path) -> Path:
    """造一个最小的 `frontend/dist`：一个 index.html + 一个静态资源。"""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True, exist_ok=True)
    (root / "index.html").write_text(
        "<!doctype html><html><body><div id=\"app\"></div>"
        "<script type=\"module\" src=\"./assets/index-abc.js\"></script>"
        "</body></html>", encoding="utf-8")
    (root / "assets" / "index-abc.js").write_text("export {};\n",
                                                  encoding="utf-8")
    return root


def _app(tmp_path: Path, *, dist: Path | None = None):
    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    database.init()
    return cloud_main.create_app(
        database, hosts=[], dist=str(dist) if dist is not None else None)


@pytest.fixture(autouse=True)
def _clean_limiters(monkeypatch):
    """两个登录桶都是模块级单例，用例之间必须清空。

    控制通道换成本地替身：2026-10-26 起令牌由电脑端签/验（`docs/06` §13），
    配对成功要真的"问一句"，不起真隧道就靠 `_fake_control.FAKE`。
    """
    FAKE.calls.clear()
    FAKE.online = True
    monkeypatch.setattr(cloud_control, "call_agent", FAKE)
    cloud_auth._login_limiter.reset()
    cloud_auth._email_limiter.reset()
    yield
    cloud_auth._login_limiter.reset()
    cloud_auth._email_limiter.reset()


# ------------------------------------------------- ① netutil：谁是真客户端


def test_forged_forwarded_for_is_ignored_when_no_proxy_is_trusted(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """没配受信代理时，`X-Forwarded-For` 一个字都不看。

    这是最关键的一条：如果这里错了，攻击者每个请求换一个 XFF 就得到
    无限个限流桶 —— 登录爆破、配对码爆破、匿名探活限流**全部失效**。
    """
    monkeypatch.delenv(netutil.TRUSTED_ENV, raising=False)
    request = _fake_request("203.0.113.7", xff="1.2.3.4")
    assert netutil.client_ip(request) == "203.0.113.7"
    assert netutil.is_trusted_proxy(request) is False


def test_forwarded_for_is_used_only_from_a_trusted_peer(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """对端确实是我们的代理时，才从 XFF 里取客户端。"""
    monkeypatch.setenv(netutil.TRUSTED_ENV, "127.0.0.1,::1")
    from_proxy = _fake_request("127.0.0.1", xff="198.51.100.23")
    assert netutil.client_ip(from_proxy) == "198.51.100.23"
    # 同一个 XFF、但直连对端不是代理 → 仍然不看
    from_stranger = _fake_request("203.0.113.7", xff="198.51.100.23")
    assert netutil.client_ip(from_stranger) == "203.0.113.7"


def test_forwarded_for_is_scanned_from_the_right(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """XFF 要从**右往左**取第一个不受信的地址。

    最左边是客户端自己写的（可以随便填），最右边才是最后一跳代理追加
    的。从左边取等于把"我是谁"交给攻击者填。
    """
    monkeypatch.setenv(netutil.TRUSTED_ENV, "127.0.0.1,10.0.0.0/8")
    request = _fake_request(
        "127.0.0.1", xff="6.6.6.6, 198.51.100.23, 10.0.0.5")
    # 6.6.6.6 是客户端伪造的，10.0.0.5 是我们自己的代理链
    assert netutil.client_ip(request) == "198.51.100.23"


def test_missing_client_info_does_not_share_a_bucket_with_a_real_address() -> None:
    """取不到地址时用 `unknown`，绝不与某个真实地址同桶。"""
    assert netutil.peer_ip(_fake_request(None)) == netutil.UNKNOWN
    assert netutil.client_ip(_fake_request(None)) == netutil.UNKNOWN


def test_broken_trusted_proxy_setting_does_not_crash(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """配置写错（多空格、乱码）时进程还要能跑：坏值跳过。"""
    monkeypatch.setenv(netutil.TRUSTED_ENV, "127.0.0.1, 这不是地址 ,")
    assert netutil.client_ip(_fake_request("198.51.100.9")) == "198.51.100.9"


# ------------------------------------------------------ ② 按账号的登录锁定


def _login(client: TestClient, email: str, password: str):
    return client.post("/api/auth/login",
                       json={"email": email, "password": password})


def _make_account(tmp_path: Path, password: str = "Correct-Horse-9") -> str:
    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    database.init()
    cloud_auth.create_account(database, "owner@example.com", password)
    return "owner@example.com"


def test_five_failures_lock_the_account_even_with_the_right_password(
        tmp_path: Path) -> None:
    """连续 5 次口令错误后锁定 15 分钟，期间**正确口令也不放行**。

    修之前只有按 IP 的 `10 次/10 分钟`：换 IP 就无限试，而"按 IP"在
    Cloudflare 后面还是所有人的同一个桶（见 ① 的四条）。按账号的锁定
    是这条防线的第二道闸。
    """
    email = _make_account(tmp_path)
    client = TestClient(_app(tmp_path), client=("198.51.100.1", 5000))
    for _ in range(cloud_auth._ACCOUNT_MAX_FAILS):
        assert _login(client, email, "wrong-password").status_code == 401
    # 口令正确也必须被挡：锁定不是"错太多次就变慢"，而是"不许进"
    locked = _login(client, email, "Correct-Horse-9")
    assert locked.status_code == 401
    assert locked.json()["error"]["code"] == "E2005"

    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    row = database.query_one(
        "SELECT failed_count, locked_until FROM accounts WHERE email = ?",
        (email,))
    assert float(row["locked_until"] or 0) > 0
    # 失败计数归零：解锁后再错一次不该立刻又锁死
    assert int(row["failed_count"] or 0) == 0


def test_lock_expires_and_a_successful_login_resets_the_counters(
        tmp_path: Path) -> None:
    email = _make_account(tmp_path)
    client = TestClient(_app(tmp_path), client=("198.51.100.2", 5000))
    for _ in range(cloud_auth._ACCOUNT_MAX_FAILS):
        _login(client, email, "wrong-password")
    # 把锁定时间拨到过去，模拟 15 分钟之后
    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    database.execute(
        "UPDATE accounts SET locked_until = ? WHERE email = ?",
        (1.0, email))
    ok = _login(client, email, "Correct-Horse-9")
    assert ok.status_code == 200
    assert ok.json()["account_token"]
    row = database.query_one(
        "SELECT failed_count, locked_until FROM accounts WHERE email = ?",
        (email,))
    assert int(row["failed_count"] or 0) == 0
    assert row["locked_until"] is None


def test_lockout_does_not_reveal_whether_the_account_exists(
        tmp_path: Path) -> None:
    """锁定的账号与不存在的账号，必须回同一个错误码与状态码。"""
    email = _make_account(tmp_path)
    client = TestClient(_app(tmp_path), client=("198.51.100.3", 5000))
    for _ in range(cloud_auth._ACCOUNT_MAX_FAILS):
        _login(client, email, "wrong-password")
    locked = _login(client, email, "Correct-Horse-9")
    ghost = _login(client, "nobody@example.com", "Correct-Horse-9")
    assert locked.status_code == ghost.status_code == 401
    assert locked.json()["error"]["code"] == ghost.json()["error"]["code"]


def test_migration_adds_the_lockout_columns_to_an_old_database(
        tmp_path: Path) -> None:
    """老库（没有 `failed_count` / `locked_until`）升级后必须能直接用。

    线上那台服务器的 `cloud.db` 是 2026-10-23 之前建的。如果 `init()`
    只会 `CREATE TABLE IF NOT EXISTS`，新列永远不会出现，而症状是
    登录时 `no such column: locked_until` —— 一条 500，且只在生产出现。
    """
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE accounts ("
        " id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,"
        " pass_hash TEXT NOT NULL, created_at REAL NOT NULL,"
        " plan TEXT NOT NULL DEFAULT 'free', last_login REAL);")
    conn.execute(
        "INSERT INTO accounts(id, email, pass_hash, created_at, plan) "
        "VALUES ('a1', 'old@example.com', 'x', 1.0, 'free')")
    conn.commit()
    conn.close()

    database = cloud_db.CloudDB(str(path))
    database.init()          # ← 迁移在这里发生
    row = database.query_one(
        "SELECT failed_count, locked_until FROM accounts WHERE id = 'a1'")
    assert int(row["failed_count"] or 0) == 0
    assert row["locked_until"] is None


# -------------------------------------------------------- ③ 安全响应头 / CSP


def test_html_pages_carry_csp_and_framing_headers(tmp_path: Path) -> None:
    """页面必须带 CSP + 禁止被 iframe（点击劫持）。

    实测（2026-10-23）：换成 quick tunnel 之后，`Content-Security-Policy`
    与 `X-Frame-Options` 双双消失 —— 因为这两个头只写在 `Caddyfile` 里。
    """
    dist = _dist(tmp_path)
    client = TestClient(_app(tmp_path, dist=dist),
                        client=("198.51.100.4", 5000))
    response = client.get("/")
    assert response.status_code == 200
    csp = response.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    # 页面里没有内联脚本，所以绝不能为了省事开 'unsafe-inline'
    assert "script-src 'self' 'unsafe-inline'" not in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_api_responses_carry_nosniff_but_no_csp(tmp_path: Path) -> None:
    """JSON 也带 `nosniff`，但不该带 CSP（CSP 对 JSON 没有意义）。"""
    client = TestClient(_app(tmp_path), client=("198.51.100.5", 5000))
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in response.headers


def test_hsts_only_over_https(tmp_path: Path) -> None:
    """HSTS 只在 https 上加：明文 http 上声明 HSTS 会被浏览器忽略，
    而一旦哪天域名被劫持到 http，这条头反而会把人锁在外面。"""
    dist = _dist(tmp_path)
    app = _app(tmp_path, dist=dist)
    secure = TestClient(app, base_url="https://relay.example.com",
                        client=("198.51.100.6", 5000))
    assert "strict-transport-security" in secure.get("/").headers
    plain = TestClient(app, base_url="http://relay.example.com",
                       client=("198.51.100.7", 5000))
    assert "strict-transport-security" not in plain.get("/").headers


def test_dotted_paths_are_not_swallowed_by_the_spa_fallback(tmp_path: Path) -> None:
    """`/.git/config` 这类扫描器路径必须 404，不能回 `200 index.html`。

    实测（2026-10-23）：原先只判"最后一段是否含点"，于是 `.git/config`
    的最后一段是 `config` → 判成页面路由 → 回 200 + HTML。扫描器把
    "200 且是 HTML"记成"这个路径存在"，我们的本意是 404。
    """
    dist = _dist(tmp_path)
    client = TestClient(_app(tmp_path, dist=dist),
                        client=("198.51.100.8", 5000))
    for path in ("/.git/config", "/.env", "/.well-known/security.txt",
                 "/.aws/credentials"):
        response = client.get(path)
        assert response.status_code == 404, path
    # 正常的深链接仍然要被单页兜住（否则刷新就白屏）
    assert client.get("/albums/2026/10").status_code == 200


# ---------------------------------------------------------- ④ 访问日志脱敏


def test_access_log_never_records_the_query_string(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """日志里不许出现 `?code=` / `?q=`。

    配对入口本身就是 `GET /d/<id>/?code=NNNNNN`，那个码在 300 秒内可被
    重放；搜索词是用户隐私。uvicorn 的 access log 两者都照抄。
    """
    dist = _dist(tmp_path)
    client = TestClient(_app(tmp_path, dist=dist),
                        client=("198.51.100.9", 5000))
    caplog.set_level(logging.INFO, logger="pb.cloud.access")
    device = "0123456789abcdef0123456789abcdef"
    # 配对入口：`?code=` 在 300 秒内可被重放
    client.get(f"/d/{device}/?code=424242")
    # 搜索入口：`?q=` 是用户隐私
    client.get(f"/d/{device}/api/search?q=结婚证")
    text = caplog.text
    assert "424242" not in text
    assert "结婚证" not in text
    # 但路径本身要留下（不然日志就没用了）
    assert f"/d/{device}" in text
    assert "api/search" in text


# ---------------------------------------------------------------------------
# 第二轮（SEC-06 / SEC-10 / HSTS 真实生效）
#
# 前一轮留下的三条"登记未修"，修完各配一条判据。共同点是：**它们的失败
# 方式都是静默的** —— HSTS 发不出去没人报错，索引没建起来没人报错，
# 限速顺序写反了也没人报错（功能完全正常）。所以只能靠测试钉住。
# ---------------------------------------------------------------------------


def _drive_headers_middleware(scope_extra: dict, *, html: bool = True) -> dict:
    """直接驱动 `SecurityHeadersMiddleware`，返回它发出的响应头。"""
    import asyncio

    from cloud.relay.headers import SecurityHeadersMiddleware

    sent: list[dict] = []

    async def app(scope, receive, send):                 # noqa: ANN001
        ctype = b"text/html; charset=utf-8" if html else b"application/json"
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", ctype)]})
        await send({"type": "http.response.body", "body": b"x"})

    scope = {
        "type": "http", "method": "GET", "path": "/", "scheme": "http",
        "headers": [], "client": ("127.0.0.1", 50000),
        "server": ("relay", 8000), "query_string": b"",
    }
    scope.update(scope_extra)

    async def send(message):                              # noqa: ANN001
        sent.append(message)

    async def receive():                                  # noqa: ANN202
        return {"type": "http.request"}

    asyncio.run(SecurityHeadersMiddleware(app)(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return {k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in start["headers"]}


def test_hsts_follows_x_forwarded_proto_only_from_a_trusted_proxy(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """https 首页必须带 HSTS —— 哪怕 uvicorn 认为这次请求是 http。

    事故现场：`uvicorn.run(proxy_headers=False)` + cloudflared 之后，
    `scope["scheme"]` 永远是 `http`，于是 `Strict-Transport-Security`
    一次都没发出去（2026-10-23 线上实测：https 首页无 HSTS）。但补上
    "读 X-Forwarded-Proto"之后必须**只对受信代理**生效，否则任何人一个
    头就能让明文连接被 HSTS 记住，把用户锁在一个连不上的 https 上。
    """
    monkeypatch.setenv(netutil.TRUSTED_ENV, "127.0.0.1")

    h = _drive_headers_middleware({"headers": [(b"x-forwarded-proto", b"https")]})
    assert h["strict-transport-security"] == "max-age=15552000"

    # 没有 XFP ⇒ 明文，绝不发 HSTS
    h = _drive_headers_middleware({})
    assert "strict-transport-security" not in h

    # 对端不是我们的代理 ⇒ XFP 是攻击者写的，忽略
    monkeypatch.delenv(netutil.TRUSTED_ENV, raising=False)
    h = _drive_headers_middleware({"headers": [(b"x-forwarded-proto", b"https")]})
    assert "strict-transport-security" not in h


def test_migration_adds_a_unique_index_on_clients_token_hash(
        tmp_path: Path) -> None:
    """老库启动时要补上 `clients(token_hash)` 的唯一索引。

    `SCHEMA` 全是 `CREATE TABLE IF NOT EXISTS`，对已存在的表一个字节都不改，
    所以"加约束"只能靠 `_migrate` 建索引。这条如果失效，两行同 `token_hash`
    就会共存：撤销其中一个客户端会把另一个也废掉（两者共用一份令牌）。
    """
    import sqlite3

    path = tmp_path / "cloud.db"
    conn = sqlite3.connect(path)
    conn.executescript(cloud_db.SCHEMA)
    conn.execute("DROP INDEX IF EXISTS idx_clients_token_hash")
    conn.commit()
    conn.close()

    db = cloud_db.CloudDB(path)
    db.init()

    idx = db._conn().execute("PRAGMA index_list(clients)").fetchall()
    assert any(str(r["name"]) == "idx_clients_token_hash"
               and int(r["unique"]) == 1 for r in idx), "唯一索引没建起来"


def test_agent_ws_handshake_is_rate_limited_before_the_token_lookup() -> None:
    """`/agent/ws` 的限速必须排在令牌校验之前（SEC-06）。

    顺序反过来等于没加：攻击者仍然是"每次尝试只花一个 TCP 握手"，只是
    每分钟能试的次数不变。这条用源码顺序钉住 —— 行为上很难测（要造 60 条
    真连接），而"限速在令牌校验前面"正是这一条的**全部**内容。
    """
    from cloud.relay import tunnel

    src = pathlib.Path(tunnel.__file__).read_text(encoding="utf-8")
    body = src.split("async def handle_agent", 1)[1]
    assert body.index("_WS_HANDSHAKE.hit(") < body.index(
        "find_device_by_agent_token("), "限速被放到了令牌校验之后"

    # 桶的语义顺带钉住：第 limit 次放行，第 limit+1 次拒绝并给 Retry-After。
    from cloud.relay.limiter import RateLimiter

    lim = RateLimiter(tunnel.WS_HANDSHAKE_LIMIT, tunnel.WS_HANDSHAKE_WINDOW)
    for _ in range(tunnel.WS_HANDSHAKE_LIMIT):
        assert lim.hit("1.2.3.4")[0] is True
    allowed, retry_after = lim.hit("1.2.3.4")
    assert allowed is False and retry_after > 0
    # 换一个 IP 不受影响 —— 否则一个被打满的地址会牵连所有人
    assert lim.hit("5.6.7.8")[0] is True


# ------------------------------------ ⑥ 配对尝试计数落在库里（SEC-09）


def _account_token(client: TestClient, email: str,
                   password: str = "Correct-Horse-9") -> str:
    response = _login(client, email, password)
    assert response.status_code == 200, response.text
    return str(response.json()["account_token"])


def _insert_device(tmp_path: Path, *, name: str = "家里的电脑") -> str:
    """直接写一行设备：这条路不需要真 agent 连上来。"""
    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    account = database.query_one("SELECT id FROM accounts LIMIT 1")
    device_id = security.new_device_id()
    agent_token = security.new_token()
    database.execute(
        "INSERT INTO devices(id, account_id, name, agent_token, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (device_id, account["id"], name, security.hash_token(agent_token),
         time.time()))
    # 替身与"电脑端"用同一个密钥，配对/认令牌的链路才走得通。
    FAKE.secret = shared_token.secret_for_agent_token(agent_token)
    return device_id


def _pair(client: TestClient, device_id: str, token: str,
          code: str = "000000"):
    return client.post(f"/api/devices/{device_id}/pair",
                       json={"code": code, "client_name": "手机"},
                       headers={"Authorization": f"Bearer {token}"})


def test_migration_adds_the_pair_counter_columns_to_an_old_database(
        tmp_path: Path) -> None:
    """老 `devices` 表升级后必须长出 `pair_fails` / `pair_window` /
    `pair_locked_until`，并且已有设备行也算 0 次失败。

    线上那台服务器的库建于 2026-10-23，`devices` 里没有这三列。迁移漏了
    的表现是 `/api/devices/{id}/pair` 一条 500（`no such column`），而且
    只在生产出现 —— 测试建的都是新库，一条都不会红。
    """
    import sqlite3

    path = tmp_path / "old-devices.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE accounts ("
        " id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,"
        " pass_hash TEXT NOT NULL, created_at REAL NOT NULL);"
        "CREATE TABLE devices ("
        " id TEXT PRIMARY KEY, account_id TEXT NOT NULL,"
        " name TEXT NOT NULL, agent_token TEXT NOT NULL UNIQUE,"
        " version TEXT NOT NULL DEFAULT '', last_seen REAL,"
        " last_lan TEXT NOT NULL DEFAULT '[]',"
        " created_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);")
    conn.execute("INSERT INTO accounts(id, email, pass_hash, created_at) "
                 "VALUES ('a1', 'old@example.com', 'x', 1.0)")
    conn.execute("INSERT INTO devices(id, account_id, name, agent_token, "
                 "created_at) VALUES ('d1', 'a1', '老电脑', 'tok', 1.0)")
    conn.commit()
    conn.close()

    database = cloud_db.CloudDB(str(path))
    database.init()          # ← 迁移在这里发生
    row = database.query_one(
        "SELECT pair_fails, pair_window, pair_locked_until FROM devices "
        "WHERE id = 'd1'")
    assert int(row["pair_fails"] or 0) == 0
    assert row["pair_window"] is None
    assert row["pair_locked_until"] is None


def test_failed_pair_attempts_are_counted_in_the_database(
        tmp_path: Path) -> None:
    """一次失败必须落成设备行上的计数，而不是只活在进程内存里。

    修之前 `devices` 表根本没有这几列，`pair_codes.tries` 是"写得进、
    没人读"的死列 —— 重启一次，5 次额度就白送。
    """
    _make_account(tmp_path)
    client = TestClient(_app(tmp_path, dist=_dist(tmp_path)),
                        client=("203.0.113.11", 5000))
    token = _account_token(client, "owner@example.com")
    device_id = _insert_device(tmp_path)

    first = _pair(client, device_id, token)
    assert first.status_code == 400
    assert first.json()["error"]["code"] == "E1002"

    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    row = database.query_one(
        "SELECT pair_fails, pair_window, pair_locked_until FROM devices "
        "WHERE id = ?", (device_id,))
    assert int(row["pair_fails"]) == 1
    assert row["pair_window"] is not None, "窗口起点没写，计数永远不会过期"
    assert row["pair_locked_until"] is None

    assert _pair(client, device_id, token).status_code == 400
    row = database.query_one(
        "SELECT pair_fails FROM devices WHERE id = ?", (device_id,))
    assert int(row["pair_fails"]) == 2


def test_five_failures_lock_the_device_across_a_restart(tmp_path: Path) -> None:
    """5 次失败把**设备**锁住；换个出口 IP、并且模拟一次重启，照样锁着。

    这是 SEC-09 的核心：锁必须属于设备（库），不属于进程（内存）。
    换 IP 用到的是另一个客户端地址，它的 IP 桶是干净的 —— 如果计数还
    在内存里，这一次就会被放行。
    """
    from cloud.relay import pairing

    _make_account(tmp_path)
    app = _app(tmp_path, dist=_dist(tmp_path))
    client = TestClient(app, client=("203.0.113.21", 5000))
    token = _account_token(client, "owner@example.com")
    device_id = _insert_device(tmp_path)

    for _ in range(pairing._DEVICE_MAX_TRIES):
        assert _pair(client, device_id, token).status_code == 400

    # 「重启」：进程内计数清空 + 换一个新的 CloudDB 连接
    pairing._ATTEMPTS.clear()
    restarted = TestClient(app, client=("203.0.113.99", 5000))
    locked = _pair(restarted, device_id, token)
    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "E4004"
    assert int(locked.headers.get("retry-after", "0")) > 0

    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    row = database.query_one(
        "SELECT pair_fails, pair_locked_until FROM devices WHERE id = ?",
        (device_id,))
    assert float(row["pair_locked_until"] or 0) > time.time()
    assert int(row["pair_fails"]) == pairing._DEVICE_MAX_TRIES


def test_an_old_window_restarts_the_count_instead_of_locking(
        tmp_path: Path) -> None:
    """窗口过期后重新从 1 数起 —— 否则"半年里错 5 次"也会被锁。"""
    from cloud.relay import pairing

    _make_account(tmp_path)
    client = TestClient(_app(tmp_path, dist=_dist(tmp_path)),
                        client=("203.0.113.31", 5000))
    token = _account_token(client, "owner@example.com")
    device_id = _insert_device(tmp_path)

    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    database.execute(
        "UPDATE devices SET pair_fails = ?, pair_window = ?, "
        "pair_locked_until = NULL WHERE id = ?",
        (pairing._DEVICE_MAX_TRIES,
         time.time() - pairing.PAIR_LOCK_SECONDS - 1, device_id))

    assert _pair(client, device_id, token).status_code == 400
    row = database.query_one(
        "SELECT pair_fails, pair_locked_until FROM devices WHERE id = ?",
        (device_id,))
    assert int(row["pair_fails"]) == 1, "旧窗口没被重置，第 6 次就误锁了"
    assert row["pair_locked_until"] is None


def test_a_successful_pair_clears_the_device_counters(tmp_path: Path) -> None:
    """配对成功必须清零 —— 不清等于白送 5 次免费额度。"""
    _make_account(tmp_path)
    client = TestClient(_app(tmp_path, dist=_dist(tmp_path)),
                        client=("203.0.113.41", 5000))
    token = _account_token(client, "owner@example.com")
    device_id = _insert_device(tmp_path)

    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    code = "424242"
    database.execute(
        "INSERT INTO pair_codes(code, device_id, expires_at, created_at) "
        "VALUES (?, ?, ?, ?)",
        (code, device_id, time.time() + 300, time.time()))
    database.execute(
        "UPDATE devices SET pair_fails = 3, pair_window = ? WHERE id = ?",
        (time.time(), device_id))

    paired = _pair(client, device_id, token, code=code)
    assert paired.status_code == 200, paired.text
    assert paired.json()["client_token"].startswith("pbc1.")

    row = database.query_one(
        "SELECT pair_fails, pair_window, pair_locked_until FROM devices "
        "WHERE id = ?", (device_id,))
    assert int(row["pair_fails"]) == 0
    assert row["pair_window"] is None
    assert row["pair_locked_until"] is None


def test_the_pair_code_row_is_the_first_gate(tmp_path: Path) -> None:
    """`pair_codes.tries` / `locked_until` 不再是被写入却没人读的死列。

    直接给一个真实存在的码记 5 次失败（模拟"码拿对了但不是这台电脑"），
    第 6 次必须被这道码锁挡住 —— 设备锁此时是干净的，挡住它的只能是码锁。
    """
    from cloud.relay import pairing

    _make_account(tmp_path)
    client = TestClient(_app(tmp_path, dist=_dist(tmp_path)),
                        client=("203.0.113.51", 5000))
    token = _account_token(client, "owner@example.com")
    device_id = _insert_device(tmp_path)

    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    code = "515151"
    database.execute(
        "INSERT INTO pair_codes(code, device_id, expires_at, created_at) "
        "VALUES (?, ?, ?, ?)",
        (code, device_id, time.time() + 300, time.time()))

    for _ in range(pairing._DEVICE_MAX_TRIES):
        pairing._note_code_failure(database, code)

    row = database.query_one(
        "SELECT tries, locked_until FROM pair_codes WHERE code = ?", (code,))
    assert int(row["tries"]) == pairing._DEVICE_MAX_TRIES
    assert float(row["locked_until"] or 0) > time.time()

    blocked = _pair(client, device_id, token, code=code)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "E4004"


# ------------------------- ⑦ 配对码存在性 oracle（SEC-10a）


def _insert_device_for(path: Path, email: str, *, name: str) -> str:
    database = cloud_db.CloudDB(str(path / "cloud.db"))
    account = database.query_one(
        "SELECT id FROM accounts WHERE email = ?", (email,))
    device_id = security.new_device_id()
    agent_token = security.new_token()
    database.execute(
        "INSERT INTO devices(id, account_id, name, agent_token, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (device_id, account["id"], name, security.hash_token(agent_token),
         time.time()))
    # 替身与"电脑端"用同一个密钥，配对/认令牌的链路才走得通。
    FAKE.secret = shared_token.secret_for_agent_token(agent_token)
    return device_id


def _insert_code(path: Path, device_id: str, code: str) -> None:
    database = cloud_db.CloudDB(str(path / "cloud.db"))
    database.execute(
        "INSERT INTO pair_codes(code, device_id, expires_at, created_at) "
        "VALUES (?, ?, ?, ?)", (code, device_id, time.time() + 300,
                                time.time()))


def test_another_accounts_code_is_indistinguishable_from_a_missing_one(
        tmp_path: Path) -> None:
    """`E1016` 只对"我自己的另一台电脑"发；别人的码一律按不存在处理。

    修之前：任何有账号的人，把任意 6 位数字打到自己设备的配对接口，
    403 `E1016` 意味着"这个码此刻有效"，400 `E1002` 意味着无效 —— 一个
    现成的码存在性预言机。码空间 100 万、TTL 300 秒，配上它就不再是
    "猜"了。
    """
    _make_account(tmp_path)
    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    cloud_auth.create_account(database, "other@example.com", "Other-Horse-9")

    client = TestClient(_app(tmp_path, dist=_dist(tmp_path)),
                        client=("203.0.113.61", 5000))
    token = _account_token(client, "owner@example.com")
    mine = _insert_device_for(tmp_path, "owner@example.com", name="我的电脑")
    theirs = _insert_device_for(tmp_path, "other@example.com", name="别人的电脑")
    _insert_code(tmp_path, theirs, "777777")

    live = _pair(client, mine, token, code="777777")
    dead = _pair(client, mine, token, code="000001")

    assert live.status_code == 400, live.text
    assert dead.status_code == 400, dead.text
    assert live.json()["error"]["code"] == "E1002"
    # 两边的 error 对象必须一模一样 —— 差一个字段就是 oracle
    live_error = dict(live.json()["error"])
    dead_error = dict(dead.json()["error"])
    live_error.pop("request_id", None)
    dead_error.pop("request_id", None)
    assert live_error == dead_error


def test_my_other_computers_code_still_says_so(tmp_path: Path) -> None:
    """同一个账号下的两台电脑之间搞混，仍然给明确提示（这是可用性，不是泄露）。"""
    _make_account(tmp_path)
    client = TestClient(_app(tmp_path, dist=_dist(tmp_path)),
                        client=("203.0.113.62", 5000))
    token = _account_token(client, "owner@example.com")
    first = _insert_device_for(tmp_path, "owner@example.com", name="客厅电脑")
    second = _insert_device_for(tmp_path, "owner@example.com", name="书房电脑")
    _insert_code(tmp_path, second, "888888")

    mixed_up = _pair(client, first, token, code="888888")
    assert mixed_up.status_code == 403
    assert mixed_up.json()["error"]["code"] == "E1016"
    assert "另一台电脑" in mixed_up.json()["error"]["message"]


def test_a_revoked_agent_token_looks_exactly_like_an_unknown_one() -> None:
    """被撤销与不存在必须回同一个响应（SEC-06）。

    差别就是情报：攻击者拿到一串「曾经有效」的令牌，403 与 401 的不同会
    告他这串东西真签发过，于是爆破从「找 token」退化成「确认 token」。
    这里用假 ws 跑真正的方法，避免起 uvicorn。
    """
    import asyncio
    import types

    from cloud.relay import tunnel

    class _WS:
        def __init__(self) -> None:
            self.headers = {
                "host": "relay.example",
                "authorization": "Bearer whatever",
            }
            self.client = types.SimpleNamespace(host="203.0.113.9")
            self.denied: tuple[int, str] | None = None

        async def send_denial_response(self, response) -> None:
            self.denied = (response.status_code,
                           response.body.decode("utf-8"))

        async def accept(self) -> None:      # 不该走到这里
            raise AssertionError("被拒绝的连接不该被 accept")

    relay = types.SimpleNamespace(db=None, check_host=lambda host: True)

    def _run(row: dict | None) -> tuple[int, str]:
        original = tunnel.find_device_by_agent_token
        tunnel.find_device_by_agent_token = lambda db, token: row
        try:
            ws = _WS()
            asyncio.run(tunnel.TunnelServer.handle_agent(relay, ws))
            assert ws.denied is not None, "连接既没被拒绝也没被接受"
            return ws.denied
        finally:
            tunnel.find_device_by_agent_token = original

    revoked = _run({"id": "d1", "account_id": "a1", "revoked": 1})
    unknown = _run(None)
    assert revoked == unknown, f"撤销与不存在可区分：{revoked} vs {unknown}"
    assert revoked[0] == 401, f"撤销应为 401（与不存在一致），实际 {revoked[0]}"

# ------------------------------------------- ⑤ 控制通道：签发权已回到电脑端


def test_pair_fails_cleanly_when_the_computer_does_not_answer(
        tmp_path: Path) -> None:
    """电脑没回话时必须 503，而且**配对码不能被消费掉**。

    这条是 ISSUS-049 修订后最容易写错的地方：抢码（条件 UPDATE）和签发
    原来在同一个事务里，改成"先问电脑"之后，如果异常没让事务回滚，用户
    会遇到"提示电脑不在线，再试一次却提示配对码已被使用"，只能去电脑
    前重新生成码。
    """
    _make_account(tmp_path)
    client = TestClient(_app(tmp_path, dist=_dist(tmp_path)),
                        client=("203.0.113.51", 5000))
    token = _account_token(client, "owner@example.com")
    device_id = _insert_device(tmp_path)
    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    code = "555555"
    database.execute(
        "INSERT INTO pair_codes(code, device_id, expires_at, created_at) "
        "VALUES (?, ?, ?, ?)",
        (code, device_id, time.time() + 300, time.time()))

    FAKE.online = False
    failed = _pair(client, device_id, token, code=code)
    assert failed.status_code == 503, failed.text
    assert failed.json()["error"]["code"] == "E2007"
    row = database.query_one(
        "SELECT used_at FROM pair_codes WHERE code = ?", (code,))
    assert row["used_at"] is None, "电脑没回话却把配对码消费掉了"

    # 电脑回来后同一个码还能用 —— 这才是"重试一次就好"。
    FAKE.online = True
    assert _pair(client, device_id, token, code=code).status_code == 200


def test_removing_a_phone_pushes_the_revocation_immediately(
        tmp_path: Path) -> None:
    """用户点"移除"时云端要主动推给电脑端，不能只等电脑轮询。"""
    _make_account(tmp_path)
    client = TestClient(_app(tmp_path, dist=_dist(tmp_path)),
                        client=("203.0.113.52", 5000))
    token = _account_token(client, "owner@example.com")
    device_id = _insert_device(tmp_path)
    database = cloud_db.CloudDB(str(tmp_path / "cloud.db"))
    client_id = security.new_device_id()
    database.execute(
        "INSERT INTO clients(id, device_id, name, token_hash, last_seen, "
        "created_at, revoked, expires_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        (client_id, device_id, "手机", "0" * 64, time.time(), time.time(),
         time.time() + 86400))

    response = client.delete(f"/api/devices/{device_id}/clients/{client_id}",
                             headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 204, response.text

    pushes = [params["ids"] for action, params in FAKE.calls
              if action == "revoke_clients"]
    assert pushes and client_id in pushes[0], "撤销没有推给电脑端"
    row = database.query_one(
        "SELECT revoked FROM clients WHERE id = ?", (client_id,))
    assert int(row["revoked"]) == 1
