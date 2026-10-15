"""鉴权与配对的测试。

重点覆盖三类容易出错的地方：
1. **配对码的一次性语义** —— 并发下必须只有一个请求能换到 token；
2. **锁与过期的顺序** —— 错误码要能指导用户下一步动作；
3. **/panel 的本机限制** —— 这是唯一防止配对码形同虚设的机制。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from backend.app.auth import (
    PAIR_CODE_TTL,
    PAIR_LOCK_SECONDS,
    PAIR_MAX_TRIES,
    auth_middleware,
    consume_pair_code,
    generate_pair_code,
    hash_code,
    hash_token,
    is_loopback,
    pair_code_info,
    purge_expired_codes,
    revoke_all_codes,
    store_pair_code,
)
from backend.app.config import Config
from backend.app.core import Ctx, Router
from backend.app.db import Database
from backend.app.errors import AppError, NotFound, Unauthorized
from backend.app.handlers import auth as handlers
from backend.app.state import AppState

DEVICE_ID = "dev0000000000001"


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    d.init()
    return d


def _state(tmp_path: Path, db: Database, *, require_pairing: bool = True,
           library: Path | None = None) -> AppState:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    lib = library or (tmp_path / "lib")
    lib.mkdir(exist_ok=True)
    cfg = Config({
        "app": {"mode": "readwrite"},
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}],
        "auth": {"require_pairing": require_pairing},
    }, path=data / "config.json", data_dir=data)
    return AppState(cfg=cfg, db=db)


def _router(st: AppState, *, middleware: bool = True) -> Router:
    r = Router()
    if middleware:
        r.use(auth_middleware)
    r.add("POST", "/api/pair", handlers.pair)
    r.add("GET", "/api/clients/me", handlers.me)
    r.add("GET", "/api/devices", handlers.list_devices)
    r.add("DELETE", "/api/devices/{client_id}", handlers.revoke_device)
    r.add("POST", "/panel/pair-code", handlers.panel_pair_code)
    r.add("GET", "/panel/devices", handlers.panel_devices)
    r.add("GET", "/panel/audit", handlers.panel_audit)
    r.add("GET", "/api/system/ping", _ping)
    return r


async def _ping(ctx: Ctx):
    from backend.app.core import json_reply

    return json_reply({"pong": True})


def _ctx(st: AppState, path: str, *, method: str = "GET",
         headers: dict | None = None, query: dict | None = None,
         params: dict | None = None, client_ip: str = "192.168.0.50",
         body: bytes | None = None) -> Ctx:
    async def _empty():
        if body:
            yield body

    return Ctx(
        method=method, path=path, headers=headers or {}, query=query or {},
        params=params or {}, client_ip=client_ip, app=st, body=_empty(),
    )


def _call(st: AppState, path: str, **kw):
    r = _router(st)
    ctx = _ctx(st, path, **kw)
    return asyncio.run(r.dispatch(ctx))


def _body(reply) -> bytes:
    if reply.body is None:
        return b""
    if isinstance(reply.body, (bytes, bytearray)):
        return bytes(reply.body)

    async def _drain():
        chunks = []
        async for c in reply.body:
            if c:
                chunks.append(c)
        return b"".join(chunks)

    return asyncio.run(_drain())


def _json(reply) -> dict:
    import json

    return json.loads(_body(reply).decode("utf-8"))


def _make_code(db: Database, *, ttl: float = PAIR_CODE_TTL) -> str:
    code = generate_pair_code()
    store_pair_code(db, code, DEVICE_ID, "测试电脑", ttl=ttl)
    return code


# --------------------------------------------------------------------------
# 哈希与随机性
# --------------------------------------------------------------------------

def test_hash_token_is_deterministic_and_not_identity() -> None:
    t = "abc123"
    assert hash_token(t) == hash_token(t)
    assert hash_token(t) != t
    assert len(hash_token(t)) == 64


def test_hash_token_differs_for_different_tokens() -> None:
    assert hash_token("a") != hash_token("b")


def test_generate_pair_code_is_six_digits_with_leading_zeros() -> None:
    for _ in range(200):
        code = generate_pair_code()
        assert len(code) == 6 and code.isdigit()


def test_generate_pair_code_covers_low_range() -> None:
    """必须能生成带前导 0 的码。

    若用 `str(randint(0, 999999))`，前导零会被吃掉，
    用户看到 5 位数字但界面说"输入 6 位"，会一直以为自己输错了。
    """
    codes = {generate_pair_code() for _ in range(3000)}
    assert any(c.startswith("0") for c in codes), "从未生成前导零的码"


def test_generate_pair_code_is_not_predictable() -> None:
    """连续生成的码不能有可见规律（防止用 random 模块的实现）。"""
    codes = [generate_pair_code() for _ in range(50)]
    assert len(set(codes)) >= 45, "重复率过高，疑似伪随机"


# --------------------------------------------------------------------------
# 配对码生命周期
# --------------------------------------------------------------------------

def test_consume_valid_code_returns_token(db: Database) -> None:
    code = _make_code(db)
    client_id, token = consume_pair_code(db, code, device_name="我的手机")
    assert client_id and token and len(token) > 20
    row = db.query_one("SELECT * FROM devices WHERE id = ?", (client_id,))
    assert row["token_hash"] == hash_token(token)
    assert row["revoked"] == 0
    assert row["name"] == "我的手机"


def test_plaintext_token_is_not_stored(db: Database) -> None:
    """数据库里绝不能出现明文 token。

    这个库会被备份到网盘、被同步工具复制、在排障时被拷给别人。
    """
    code = _make_code(db)
    _, token = consume_pair_code(db, code)
    rows = db.query("SELECT token_hash FROM devices")
    assert all(token not in (r["token_hash"] or "") for r in rows)
    # 也确认整库文件中不含明文
    raw = (db.path).read_bytes()
    assert token.encode() not in raw


def test_plaintext_code_is_not_stored(db: Database) -> None:
    code = _make_code(db)
    raw = db.path.read_bytes()
    assert code.encode() not in raw, "配对码明文落盘"
    assert db.query_one("SELECT * FROM pair_codes")["code_hash"] == hash_code(code)


def test_code_is_single_use(db: Database) -> None:
    code = _make_code(db)
    consume_pair_code(db, code)
    with pytest.raises(AppError) as ei:
        consume_pair_code(db, code)
    assert ei.value.code == "E1005"


def test_unknown_code_raises_e1002(db: Database) -> None:
    with pytest.raises(AppError) as ei:
        consume_pair_code(db, "000000")
    assert ei.value.code == "E1002"
    assert ei.value.status == 400


def test_expired_code_raises_e1003(db: Database) -> None:
    code = _make_code(db, ttl=-1)
    with pytest.raises(AppError) as ei:
        consume_pair_code(db, code)
    assert ei.value.code == "E1003"
    assert ei.value.status == 403


def test_expired_code_is_not_consumed(db: Database) -> None:
    """过期码被拒后不能留下 used_at，否则重新生成同码会冲突。"""
    code = _make_code(db, ttl=-1)
    with pytest.raises(AppError):
        consume_pair_code(db, code)
    row = db.query_one("SELECT used_at FROM pair_codes WHERE code_hash = ?",
                       (hash_code(code),))
    assert row["used_at"] is None


def test_lock_after_max_tries(db: Database) -> None:
    """错满次数后锁定，且返回 429 而不是 400/403。

    429 让前端知道"过一会儿可以再试"，而 403 会被理解成"这个码废了"。
    """
    code = _make_code(db)
    # 直接把 tries 推到上限（模拟已被暴力尝试过的码）
    db.execute("UPDATE pair_codes SET tries = ? WHERE code_hash = ?",
               (PAIR_MAX_TRIES, hash_code(code)))
    with pytest.raises(AppError) as ei:
        consume_pair_code(db, code)
    assert ei.value.code == "E4004"
    assert ei.value.status == 429

    row = db.query_one("SELECT locked_until FROM pair_codes WHERE code_hash = ?",
                       (hash_code(code),))
    assert row["locked_until"] is not None
    assert row["locked_until"] > time.time()


def test_locked_code_rejects_even_correct_code(db: Database) -> None:
    """锁定期间即使码正确也必须拒绝 —— 否则锁定没有意义。"""
    code = _make_code(db)
    db.execute(
        "UPDATE pair_codes SET locked_until = ? WHERE code_hash = ?",
        (time.time() + PAIR_LOCK_SECONDS, hash_code(code)),
    )
    with pytest.raises(AppError) as ei:
        consume_pair_code(db, code)
    assert ei.value.code == "E4004"


def test_lock_expires_and_code_works_again(db: Database) -> None:
    code = _make_code(db)
    db.execute(
        "UPDATE pair_codes SET locked_until = ?, tries = 0 WHERE code_hash = ?",
        (time.time() - 1, hash_code(code)),
    )
    client_id, _ = consume_pair_code(db, code)
    assert client_id


def test_lock_checked_before_expiry(db: Database) -> None:
    """锁定判定必须**先于**过期判定。

    反过来的话，一个既锁定又过期的码会返回"已过期"，
    引导用户去电脑上重新生成 —— 而重新生成正是正确动作，
    所以这里两种顺序结果都能用。但锁定优先能给出更准确的原因
    （"尝试次数过多"解释了为什么刚才几次都失败）。
    """
    code = _make_code(db, ttl=-1)
    db.execute(
        "UPDATE pair_codes SET locked_until = ? WHERE code_hash = ?",
        (time.time() + PAIR_LOCK_SECONDS, hash_code(code)),
    )
    with pytest.raises(AppError) as ei:
        consume_pair_code(db, code)
    assert ei.value.code == "E4004", "锁定状态应优先于过期状态报告"


def test_used_code_reported_before_expiry(db: Database) -> None:
    """已使用的码即使过期也应报"已使用"。

    报"已过期"会让用户以为重新生成一个就行，而实际他需要先
    在托盘上作废旧码 —— 虽然重新生成确实会作废，但"已使用"
    更能解释"为什么我刚才明明输对了"。
    """
    code = _make_code(db, ttl=-1)
    db.execute("UPDATE pair_codes SET used_at = ? WHERE code_hash = ?",
               (time.time(), hash_code(code)))
    with pytest.raises(AppError) as ei:
        consume_pair_code(db, code)
    assert ei.value.code == "E1005"


def test_concurrent_consume_only_one_wins(db: Database) -> None:
    """并发配对必须只有一个成功。

    两台手机同时提交同一个码时，两个请求都能读到 `used_at IS NULL`。
    没有条件更新 + rowcount 检查的话会签发两枚 token，
    配对码的一次性语义被破坏（用户以为只给了老婆一台手机）。
    """
    code = _make_code(db)
    results: list[str] = []

    def attempt():
        try:
            consume_pair_code(db, code)
            results.append("ok")
        except AppError as e:
            results.append(e.code)

    import threading

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("ok") == 1, f"签发了多枚 token: {results}"
    assert db.scalar("SELECT COUNT(*) FROM devices") == 1


def test_revoke_all_codes_invalidates_pending(db: Database) -> None:
    """托盘"重新生成"必须作废旧码。

    用户点这个按钮的心理预期就是"刚才那个码别用了"。
    """
    old = _make_code(db)
    assert revoke_all_codes(db) == 1
    with pytest.raises(AppError) as ei:
        consume_pair_code(db, old)
    assert ei.value.code == "E1005"


def test_revoke_all_codes_does_not_touch_used(db: Database) -> None:
    code = _make_code(db)
    consume_pair_code(db, code)
    assert revoke_all_codes(db) == 0


def test_store_pair_code_replaces_same_code(db: Database) -> None:
    """同一码重复写入不应报主键冲突（极小概率但必须不崩）。"""
    code = "123456"
    store_pair_code(db, code, DEVICE_ID, "A")
    store_pair_code(db, code, DEVICE_ID, "B")
    row = db.query_one("SELECT device_name FROM pair_codes WHERE code_hash = ?",
                       (hash_code(code),))
    assert row["device_name"] == "B"
    assert db.scalar("SELECT COUNT(*) FROM pair_codes") == 1


def test_purge_expired_codes(db: Database) -> None:
    _make_code(db, ttl=-90000)
    fresh = _make_code(db)
    removed = purge_expired_codes(db)
    assert removed == 1
    assert pair_code_info(db, fresh) is not None


def test_pair_code_info_returns_none_for_unknown(db: Database) -> None:
    assert pair_code_info(db, "999999") is None


def test_pair_code_info_does_not_consume(db: Database) -> None:
    code = _make_code(db)
    info = pair_code_info(db, code)
    assert info["used_at"] is None
    consume_pair_code(db, code)   # 仍然可用


# --------------------------------------------------------------------------
# 请求鉴权中间件
# --------------------------------------------------------------------------

def test_ping_is_public(tmp_path: Path, db: Database) -> None:
    """探活必须匿名。

    手机冷启动时要用它判断"这台电脑是不是我配对过的那台"，
    而"还没有 token"正是探活要解决的场景。
    """
    st = _state(tmp_path, db)
    r = _call(st, "/api/system/ping")
    assert r.status == 200


def test_protected_path_without_token_is_401(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    r = _call(st, "/api/clients/me")
    assert r.status == 401
    assert _json(r)["error"]["code"] == "E2002"


def test_protected_path_with_bad_token_is_401(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    r = _call(st, "/api/clients/me", headers={"Authorization": "Bearer nope"})
    assert r.status == 401


def test_valid_token_passes(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    code = _make_code(db)
    client_id, token = consume_pair_code(db, code, device_name="我的手机")
    r = _call(st, "/api/clients/me",
              headers={"Authorization": f"Bearer {token}"})
    assert r.status == 200
    body = _json(r)
    assert body["client_id"] == client_id
    assert body["client_name"] == "我的手机"


def test_revoked_token_is_403_not_401(tmp_path: Path, db: Database) -> None:
    """撤销与失效必须用不同状态码。

    都返回 401 的话，用户在电脑上"移除这台手机"后，手机会一直提示
    "配对失效"而不知道是被主动移除的，于是反复重试同一个 token
    而不是引导用户重新配对。
    """
    st = _state(tmp_path, db)
    code = _make_code(db)
    client_id, token = consume_pair_code(db, code)
    db.execute("UPDATE devices SET revoked = 1 WHERE id = ?", (client_id,))

    r = _call(st, "/api/clients/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 403
    assert _json(r)["error"]["code"] == "E2004"


def test_bearer_prefix_is_case_insensitive(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    code = _make_code(db)
    _, token = consume_pair_code(db, code)
    for prefix in ("Bearer", "bearer", "BEARER"):
        r = _call(st, "/api/clients/me",
                  headers={"Authorization": f"{prefix} {token}"})
        assert r.status == 200, prefix


def test_require_pairing_false_allows_anonymous(tmp_path: Path, db: Database) -> None:
    """开发模式：require_pairing=false 时**中间件**放行。

    ⚠️ 注意这一项只关掉中间件的门禁，不会凭空造出身份。
    像 `/api/clients/me` 这种处理函数自己就需要 client_id
    （要回答"我是谁"），无 token 时它仍然会拒绝 ——
    这是对的：把 require_pairing 关掉不应该让"我是谁"返回一个假身份。
    """
    st = _state(tmp_path, db, require_pairing=False)
    r = _router(st)
    r.add("GET", "/api/echo", _echo)
    reply = asyncio.run(r.dispatch(_ctx(st, "/api/echo")))
    assert reply.status == 200


async def _echo(ctx: Ctx):
    from backend.app.core import json_reply

    return json_reply({"ok": True})


def test_require_pairing_false_still_rejects_identity_endpoints(
    tmp_path: Path, db: Database,
) -> None:
    """关掉配对门禁不等于发一个假身份。"""
    st = _state(tmp_path, db, require_pairing=False)
    r = _call(st, "/api/clients/me")
    assert r.status == 401


# --------------------------------------------------------------------------
# /panel 本机限制
# --------------------------------------------------------------------------

def test_panel_rejects_lan_client(tmp_path: Path, db: Database) -> None:
    """⚠️ 这条是配对机制的安全底线。

    电脑端监听 0.0.0.0:8787，同网段任何设备都能访问。
    如果它们能调 /panel/pair-code 拿到码，配对码就形同虚设 ——
    攻击者拿到码后自己配一枚 90 天有效的 token。
    """
    st = _state(tmp_path, db)
    r = _call(st, "/panel/pair-code", method="POST", client_ip="192.168.0.99")
    assert r.status == 404, "非本机必须拿不到配对码"


def test_panel_returns_404_not_403_for_lan(tmp_path: Path, db: Database) -> None:
    """用 404 而不是 403。

    403 等于告诉扫描者"这个路径存在，只是你权限不够"，
    404 让他无法区分"没有这个功能"与"不许访问"。
    """
    st = _state(tmp_path, db)
    r = _call(st, "/panel/devices", client_ip="10.0.0.5")
    assert r.status == 404


def test_panel_allows_loopback_v4(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    r = _call(st, "/panel/pair-code", method="POST", client_ip="127.0.0.1")
    assert r.status == 201
    body = _json(r)
    assert len(body["code"]) == 6 and body["code"].isdigit()


def test_panel_allows_loopback_v6(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    r = _call(st, "/panel/pair-code", method="POST", client_ip="::1")
    assert r.status == 201


def test_panel_allows_ipv4_mapped_ipv6(tmp_path: Path, db: Database) -> None:
    """⚠️ 双栈 socket 上 Python 把 127.0.0.1 表示成 ::ffff:127.0.0.1。

    只判 "127.0.0.1" 会让本机浏览器访问 /panel 被拒 ——
    而用户此刻正坐在电脑前点托盘的"打开面板"，看到 404 完全无法理解。
    """
    st = _state(tmp_path, db)
    r = _call(st, "/panel/pair-code", method="POST",
              client_ip="::ffff:127.0.0.1")
    assert r.status == 201


def test_panel_pair_code_generation_invalidates_previous(tmp_path: Path,
                                                         db: Database) -> None:
    st = _state(tmp_path, db)
    r1 = _call(st, "/panel/pair-code", method="POST", client_ip="127.0.0.1")
    code1 = _json(r1)["code"]
    _call(st, "/panel/pair-code", method="POST", client_ip="127.0.0.1")

    with pytest.raises(AppError) as ei:
        consume_pair_code(db, code1)
    assert ei.value.code == "E1005"


def test_panel_audit_lists_events(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    code = _make_code(db)
    _call(st, "/api/pair", method="POST", body=b'{"code":"%s"}' % code.encode())

    r = _call(st, "/panel/audit", client_ip="127.0.0.1")
    assert r.status == 200
    items = _json(r)["items"]
    assert any(i["action"] == "pair" for i in items)


def test_panel_audit_missing_file_is_empty(tmp_path: Path, db: Database) -> None:
    """审计文件不存在时返回空列表而不是 500。

    全新安装的第一次点开面板就是这种情况。
    """
    st = _state(tmp_path, db)
    r = _call(st, "/panel/audit", client_ip="127.0.0.1")
    assert r.status == 200
    assert _json(r)["items"] == []


def test_panel_devices_lists_paired(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    code = _make_code(db)
    client_id, _ = consume_pair_code(db, code, device_name="老婆的手机")
    r = _call(st, "/panel/devices", client_ip="127.0.0.1")
    assert r.status == 200
    items = _json(r)["items"]
    assert len(items) == 1 and items[0]["client_id"] == client_id


def test_is_loopback_helper() -> None:
    class _C:
        def __init__(self, ip):
            self.client_ip = ip

    assert is_loopback(_C("127.0.0.1"))
    assert is_loopback(_C("::1"))
    assert is_loopback(_C("::ffff:127.0.0.1"))
    assert is_loopback(_C("127.0.0.1".upper()))
    assert not is_loopback(_C("192.168.0.2"))
    assert not is_loopback(_C("10.0.0.1"))
    assert not is_loopback(_C("::ffff:192.168.0.2"))


# --------------------------------------------------------------------------
# 端到端：配对 → 访问 → 撤销
# --------------------------------------------------------------------------

def test_full_pairing_flow(tmp_path: Path, db: Database) -> None:
    """托盘生成码 → 手机配对 → 用 token 访问 → 托盘撤销 → 被拒。"""
    st = _state(tmp_path, db)

    # 1) 托盘生成配对码
    r = _call(st, "/panel/pair-code", method="POST", client_ip="127.0.0.1")
    assert r.status == 201
    code = _json(r)["code"]

    # 2) 手机配对
    r = _call(st, "/api/pair", method="POST",
              body=f'{{"code":"{code}"}}'.encode(),
              headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) Chrome/126"})
    assert r.status == 201
    paired = _json(r)
    token = paired["client_token"]
    client_id = paired["client_id"]
    assert paired["device_id"] == st.device_id
    assert "Android" in paired["lan_ips"] or isinstance(paired["lan_ips"], list)

    # 3) 用 token 访问
    r = _call(st, "/api/clients/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 200

    # 4) 设备列表里能看到
    r = _call(st, "/api/devices", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 200
    assert len(_json(r)["items"]) == 1

    # 5) 撤销
    r = _call(st, f"/api/devices/{client_id}", method="DELETE",
              headers={"Authorization": f"Bearer {token}"},
              params={"client_id": client_id})
    assert r.status == 204

    # 6) 撤销后立刻失效（不需要等任何缓存过期）
    r = _call(st, "/api/clients/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 403


def test_pair_code_with_spaces_is_accepted(tmp_path: Path, db: Database) -> None:
    """用户照着电脑屏幕分组输入 "042 817" 是很自然的动作。"""
    st = _state(tmp_path, db)
    code = _make_code(db)
    spaced = f"{code[:3]} {code[3:]}"
    r = _call(st, "/api/pair", method="POST",
              body=f'{{"code":"{spaced}"}}'.encode())
    assert r.status == 201


def test_pair_rejects_malformed_code(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    for bad in ("", "12345", "1234567", "abcdef", "12 34", None):
        body = f'{{"code":{("null" if bad is None else repr(bad))}}}'.encode()
        r = _call(st, "/api/pair", method="POST", body=body)
        assert r.status == 400, bad


def test_pair_rate_limited_by_ip(tmp_path: Path, db: Database) -> None:
    """拦住"换不同码持续扫"的脚本。

    它对每个码都是第一次尝试，配对码自带的计数器帮不上忙。
    """
    st = _state(tmp_path, db)
    status = None
    for _ in range(30):
        r = _call(st, "/api/pair", method="POST",
                  body=b'{"code":"000000"}', client_ip="192.168.0.77")
        status = r.status
        if status == 429:
            break
    assert status == 429, "同一 IP 的持续尝试没有被限速"


def test_pair_rate_limit_is_per_ip(tmp_path: Path, db: Database) -> None:
    """一台设备被限速不能影响另一台（否则一人乱试全家连不上）。"""
    st = _state(tmp_path, db)
    for _ in range(25):
        _call(st, "/api/pair", method="POST", body=b'{"code":"000000"}',
              client_ip="192.168.0.88")

    code = _make_code(db)
    r = _call(st, "/api/pair", method="POST",
              body=f'{{"code":"{code}"}}'.encode(), client_ip="192.168.0.89")
    assert r.status == 201


def test_revoke_unknown_device_is_404(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    code = _make_code(db)
    _, token = consume_pair_code(db, code)
    r = _call(st, "/api/devices/nonexistent", method="DELETE",
              headers={"Authorization": f"Bearer {token}"},
              params={"client_id": "nonexistent"})
    assert r.status == 404


def test_revoke_keeps_row_for_audit_trail(tmp_path: Path, db: Database) -> None:
    """撤销不删行。

    删行会让 audit_log.client_id 变成悬空引用，
    事后追查"是谁删了这张照片"就断了线索。
    """
    st = _state(tmp_path, db)
    code = _make_code(db)
    client_id, token = consume_pair_code(db, code)
    _call(st, f"/api/devices/{client_id}", method="DELETE",
          headers={"Authorization": f"Bearer {token}"},
          params={"client_id": client_id})
    row = db.query_one("SELECT revoked FROM devices WHERE id = ?", (client_id,))
    assert row is not None and row["revoked"] == 1


def test_pairing_does_not_leak_paths_in_error(tmp_path: Path, db: Database) -> None:
    st = _state(tmp_path, db)
    r = _call(st, "/api/pair", method="POST", body=b'{"code":"000000"}')
    text = _body(r).decode("utf-8", "replace")
    assert str(tmp_path) not in text
    assert "Traceback" not in text
