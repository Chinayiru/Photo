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
from typing import Any

import pytest

from backend.app.auth import (
    CLIENT_COOKIE,
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
           library: Path | None = None, ttl: object = None) -> AppState:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    lib = library or (tmp_path / "lib")
    lib.mkdir(exist_ok=True)
    auth: dict = {"require_pairing": require_pairing}
    if ttl is not None:
        auth["token_ttl_days"] = ttl
    cfg = Config({
        "app": {"mode": "readwrite"},
        # ★ `panel_http: True`：本文件的用例全部围绕 `/panel/**` 的访问控制，
        # 而生产默认是"整条关掉"（审计 F1-b：本机上任何网页的来源 IP 都是
        # 127.0.0.1，与用户本人无从区分）。显式打开才能测那层"回环 + 非回环"
        # 的判定；"默认关掉时连回环也 404"由 test_handlers / 专门用例覆盖。
        "server": {"panel_http": True},
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}],
        "auth": auth,
    }, path=data / "config.json", data_dir=data)
    return AppState(cfg=cfg, db=db)


def _set_cookie_max_age(reply) -> int:
    """从响应里取出 `Set-Cookie` 的 `Max-Age`（秒）。

    ★ 必须解析真实的 Set-Cookie 字符串，而不是断言调用了哪个函数 ——
    `pair()` 里那行 `max_age=cookie_max_age_for(...)` 曾经写成
    `max(1, token_ttl_days(cfg)) * 86400`，在 `token_ttl_days = 0`
    时算出 1 天。任何只测 `cookie_max_age_for` 自己的用例都发现不了它。
    """
    import re

    raw = repr(reply.headers)
    for entry in getattr(reply, "cookies", []):
        name, _value, max_age = entry
        if name == "pb_token":
            return int(max_age)
    m = re.search(r"pb_token=[^;]*; Path=/; Max-Age=(\d+)", raw)
    assert m, f"响应里没有 pb_token 的 Set-Cookie：{raw}"
    return int(m.group(1))


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


# --------------------------------------------------------------------------
# "错 5 次锁 10 分钟" 的真实计数路径（AC-5.5）
#
# ⚠️ 下面这几条**不允许**自己造前置状态。
#
# 上面 `test_lock_after_max_tries` 是 `UPDATE pair_codes SET tries = ?` 直接
# 把计数器推到上限的 —— 它测的是"读到满值之后怎么判"，而生产代码
# **从来没有把 tries 推到满值过**：猜错的码按 code_hash 查不到行，
# 于是直接走 E1002 返回，计数器一次都没动过。那 4 条用例全绿，
# 而"错 5 次锁 10 分钟"在真实攻击下一次都没生效。
#
# 所以这里的每一条都必须**只调用 consume_pair_code 去喂错误码**，
# 让计数由被测代码自己产生。这是本次修复唯一可靠的验收方式。
# --------------------------------------------------------------------------

def _active_code_row(db: Database) -> Any:
    """取当前活跃（未用、未过期）的那枚码的行。"""
    return db.query_one(
        "SELECT * FROM pair_codes WHERE used_at IS NULL AND expires_at >= ?",
        (time.time(),),
    )


def test_wrong_guess_increments_tries(db: Database) -> None:
    """猜错一次，活跃码的 tries 必须 +1 —— 这条是本次 bug 的回归钉。"""
    code = _make_code(db)
    assert _active_code_row(db)["tries"] == 0

    with pytest.raises(AppError) as ei:
        consume_pair_code(db, "000000")
    assert ei.value.code == "E1002"

    row = db.query_one("SELECT tries FROM pair_codes WHERE code_hash = ?",
                       (hash_code(code),))
    assert row["tries"] == 1, "猜错的尝试没有被记到活跃码上"


def test_five_wrong_guesses_lock_the_active_code(db: Database) -> None:
    """连错 5 次后锁定 10 分钟，且**期间正确码也不通过**（AC-5.5 原文）。"""
    code = _make_code(db)

    # 前 4 次：只是"码不存在"。
    for i in range(PAIR_MAX_TRIES - 1):
        with pytest.raises(AppError) as ei:
            consume_pair_code(db, f"{i:06d}")
        assert ei.value.code == "E1002", f"第 {i + 1} 次猜错不该锁"

    # 第 5 次：锁定当场生效，回 429 而不是 400。
    # 回 400 会让前端以为"码输错了"而继续重输，每次重输都在刷新锁定。
    with pytest.raises(AppError) as ei:
        consume_pair_code(db, "999999")
    assert ei.value.code == "E4004"
    assert ei.value.status == 429

    row = db.query_one("SELECT tries, locked_until FROM pair_codes"
                       " WHERE code_hash = ?", (hash_code(code),))
    assert row["tries"] == PAIR_MAX_TRIES
    assert row["locked_until"] > time.time()

    # ★ 核心断言：正确码在锁定期内也必须被拒。
    with pytest.raises(AppError) as ei:
        consume_pair_code(db, code, device_name="我的手机")
    assert ei.value.code == "E4004", "锁定期内正确码放行了，锁定形同虚设"


def test_wrong_guesses_share_budget_across_active_codes(db: Database) -> None:
    """多枚活跃码共享同一个 5 次预算。

    `docs/06-安全设计文档.md` §8.2 场景 B 指出：按 code 计数时，
    N 枚活跃码会把攻击者的预算放大到 5N 次。递增**全部**活跃码
    关掉了这个绕过 —— 否则这条用例会通过（第一枚被锁，第二枚还能试）。
    """
    first = _make_code(db)
    second = generate_pair_code()
    store_pair_code(db, second, DEVICE_ID, "测试电脑")

    for i in range(PAIR_MAX_TRIES):
        with pytest.raises(AppError):
            consume_pair_code(db, f"{i:06d}")

    for c in (first, second):
        row = db.query_one("SELECT tries FROM pair_codes WHERE code_hash = ?",
                           (hash_code(c),))
        assert row["tries"] == PAIR_MAX_TRIES, "第二枚码逃过了共享预算"
        with pytest.raises(AppError) as ei:
            consume_pair_code(db, c)
        assert ei.value.code == "E4004"


def test_wrong_guess_without_active_code_is_just_e1002(db: Database) -> None:
    """没有任何活跃码时猜错只报 E1002，不能崩。

    用户在托盘点"重新生成"之前，表里可能一枚活跃码都没有；
    此时猜错无处计数，由 handlers/auth.py 的按 IP 限速兜底。
    """
    with pytest.raises(AppError) as ei:
        consume_pair_code(db, "000000")
    assert ei.value.code == "E1002"


def test_expired_code_does_not_absorb_wrong_guesses(db: Database) -> None:
    """过期码不该被计入 —— 否则攻击者能靠猜错把老行一直续命。

    `expires_at >= now` 这个条件同时保证了"计数只记在还在生效的码上"。
    """
    code = _make_code(db, ttl=-1)
    with pytest.raises(AppError) as ei:
        consume_pair_code(db, "000000")
    assert ei.value.code == "E1002"

    row = db.query_one("SELECT tries FROM pair_codes WHERE code_hash = ?",
                       (hash_code(code),))
    assert row["tries"] == 0, "过期码被计入了错误尝试"


def test_regenerating_code_clears_the_lock(db: Database) -> None:
    """用户重新生成配对码后必须能立刻配对。

    这是 `docs/04` 给 E4004 开的恢复路径（「电脑托盘重新生成配对码
    可立即解除」）。若新码也继承了 tries，用户被锁 10 分钟且**无从自救**，
    只能重启软件 —— 那才是真正的可用性事故。
    """
    _make_code(db)
    for i in range(PAIR_MAX_TRIES):
        with pytest.raises(AppError):
            consume_pair_code(db, f"{i:06d}")

    # 托盘"重新生成"的动作：先作废全部旧码，再存新码。
    revoke_all_codes(db)
    fresh = generate_pair_code()
    store_pair_code(db, fresh, DEVICE_ID, "测试电脑")

    client_id, token = consume_pair_code(db, fresh, device_name="我的手机")
    assert client_id and token


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


def test_pair_lockout_end_to_end_over_http(tmp_path: Path, db: Database) -> None:
    """AC-5.5 的端到端验收：走完整 dispatch，只喂错误码。

    这条是"用户可感知"的那一层 —— 前面 `test_five_wrong_guesses_*` 验的是
    `consume_pair_code` 的计数，这里验的是 HTTP 状态码序列，
    因为前端的恢复动作完全取决于它：
      400 → 用户以为码输错了，继续重输（每次重输都在刷新锁定）
      429 → 前端提示"请 10 分钟后再试"，用户去电脑上重新生成
    """
    st = _state(tmp_path, db)
    code = _make_code(db)

    for i in range(PAIR_MAX_TRIES - 1):
        r = _call(st, "/api/pair", method="POST", body=b'{"code":"000001"}',
                  client_ip="192.168.0.90")
        assert r.status == 400, f"第 {i + 1} 次猜错不该锁"

    r = _call(st, "/api/pair", method="POST", body=b'{"code":"000002"}',
              client_ip="192.168.0.90")
    assert r.status == 429, "第 5 次猜错没有当场锁定"

    # 正确码 + 换一个 IP（绕开按 IP 的限速）仍然必须被拒。
    r = _call(st, "/api/pair", method="POST",
              body=f'{{"code":"{code}"}}'.encode(), client_ip="192.168.0.91")
    assert r.status == 429, "锁定期内正确码放行了，锁定形同虚设"


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


# --------------------------------------------------------------------------
# AC-5.10：client_token 过期
#
# ★ 这一组的存在理由：`expires_at` 原来只拼进**响应体**给客户端看，
#   库里一个字段都没有 —— 于是 token 永远有效，那个数字纯属装饰。
#   用户手机丢了、又没想起来点"移除设备"，那枚 token 就永久开门。
#   所以断言必须落到"库里存了什么"与"过期后请求是否真的被拒"，
#   只看响应体的 `expires_at` 是测不出这个缺陷的。
# --------------------------------------------------------------------------

def _pair_ctx(st: AppState, code: str):
    import json as _j
    return _ctx(st, "/api/pair", method="POST",
                body=_j.dumps({"code": code}).encode())


def test_pair_stores_expiry_in_db(tmp_path: Path, db: Database) -> None:
    """签发 token 时必须把过期时刻**落库**，不能只写进响应体。"""
    st = _state(tmp_path, db)
    code = _make_code(db)
    r = asyncio.run(_router(st).dispatch(_pair_ctx(st, code)))
    assert r.status == 201
    body = _json(r)

    row = db.query_one(
        "SELECT expires_at FROM devices WHERE id = ?", (body["client_id"],))
    assert row["expires_at"] is not None, "过期时刻没落库，token 就永不过期"
    assert abs(row["expires_at"] - body["expires_at"]) < 1.0


def test_expired_token_is_rejected(tmp_path: Path, db: Database) -> None:
    """★ 把库里那行的过期时刻改到过去，请求必须被拒。

    这是 AC-5.10 的核心断言。直接改库而不是等 90 天，是为了让这条
    回归在 CI 里 0.1 秒跑完 —— 而"等 90 天"实际上等于永远不测。
    """
    st = _state(tmp_path, db)
    code = _make_code(db)
    body = _json(asyncio.run(_router(st).dispatch(_pair_ctx(st, code))))
    token = body["client_token"]

    # 未过期时可用
    ok = _call(st, "/api/clients/me", headers={"Authorization": f"Bearer {token}"})
    assert ok.status == 200

    db.execute("UPDATE devices SET expires_at = ? WHERE id = ?",
               (time.time() - 1, body["client_id"]))

    r = _call(st, "/api/clients/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 401
    assert (CLIENT_COOKIE, "", 0) in r.cookies, (
        "过期时没清 cookie：浏览器会一直带它重试，缩略图持续 401 变灰块")


def test_expired_cookie_token_also_rejected(tmp_path: Path, db: Database) -> None:
    """过期判定必须对 cookie 通路同样生效。

    `<img>` 走的是 cookie，只有 Bearer 头被判过期的话，缩略图仍然
    能拿过期令牌换到照片 —— 那正是最需要挡住的那条路。
    """
    st = _state(tmp_path, db)
    code = _make_code(db)
    body = _json(asyncio.run(_router(st).dispatch(_pair_ctx(st, code))))
    db.execute("UPDATE devices SET expires_at = ? WHERE id = ?",
               (time.time() - 1, body["client_id"]))

    r = _call(st, "/api/clients/me",
              headers={"Cookie": f"{CLIENT_COOKIE}={body['client_token']}"})
    assert r.status == 401


def test_null_expiry_never_expires(tmp_path: Path, db: Database) -> None:
    """★ `expires_at IS NULL` 表示永不过期 —— 老库升级上来的行就是这个值。

    如果给它们强行安一个过期时刻，所有**已配对**的手机会在某一天
    集体掉线。那比"永不触发过期"更糟：用户什么都没做，手机突然要重新配对。
    """
    st = _state(tmp_path, db)
    code = _make_code(db)
    body = _json(asyncio.run(_router(st).dispatch(_pair_ctx(st, code))))

    db.execute("UPDATE devices SET expires_at = NULL WHERE id = ?",
               (body["client_id"],))
    r = _call(st, "/api/clients/me",
              headers={"Authorization": f"Bearer {body['client_token']}"})
    assert r.status == 200


def test_token_ttl_days_is_configurable(tmp_path: Path, db: Database) -> None:
    """AC-5.10 原文要求"过期策略可配置"。"""
    from backend.app.auth import token_expiry, token_ttl_days

    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    cfg = Config({
        "app": {"mode": "readwrite"},
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}],
        "auth": {"require_pairing": True, "token_ttl_days": 7},
    }, path=data / "config.json", data_dir=data)

    assert token_ttl_days(cfg) == 7
    assert token_expiry(1000.0, cfg) == 1000.0 + 7 * 86400


def test_zero_ttl_means_never_expires(tmp_path: Path, db: Database) -> None:
    """`token_ttl_days: 0` 是个有用的档位：用户自己承担风险时不该拦他。"""
    from backend.app.auth import token_expiry, token_ttl_days

    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    cfg = Config({
        "app": {"mode": "readwrite"},
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}],
        "auth": {"require_pairing": True, "token_ttl_days": 0},
    }, path=data / "config.json", data_dir=data)

    assert token_ttl_days(cfg) == 0
    assert token_expiry(1000.0, cfg) is None


def test_bad_ttl_falls_back_to_default(tmp_path: Path, db: Database) -> None:
    """非法值退回默认 90，不能把服务启不起来。"""
    from backend.app.auth import TOKEN_TTL_DAYS, token_ttl_days

    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    cfg = Config({
        "app": {"mode": "readwrite"},
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}],
        "auth": {"require_pairing": True, "token_ttl_days": "abc"},
    }, path=data / "config.json", data_dir=data)

    assert token_ttl_days(cfg) == TOKEN_TTL_DAYS


def test_cookie_max_age_never_outlives_token(tmp_path: Path, db: Database) -> None:
    """★ cookie 不能活得比 token 长。

    token 只剩 1 天而 cookie 写 90 天的话，浏览器会一直带着一枚
    服务端必然拒绝的凭证 —— 症状是缩略图间歇性变灰块，用户完全
    无法理解。反过来（cookie 短于 token）只是少一次自愈，无害。
    """
    from backend.app.auth import COOKIE_MAX_AGE, cookie_max_age

    now = time.time()
    assert cookie_max_age({"expires_at": now + 3600}, now) == 3600
    # 永不过期的行 → 用浏览器上限
    assert cookie_max_age({"expires_at": None}, now) == COOKIE_MAX_AGE
    # 已经过期的行不能算出 0 或负数（Max-Age=0 等于删除 cookie，
    # 而这里是在"补发"路径上，值必须为正）
    assert cookie_max_age({"expires_at": now - 100}, now) >= 1


def test_pair_sets_cookie_lasting_as_long_as_the_token(
        tmp_path: Path, db: Database) -> None:
    """★ 配对时下发的 cookie 必须和 token 活得一样久。

    `token_ttl_days = 0`（永不过期）这条路径曾经被写成
    `max(1, 0) * 86400` = **1 天** —— 于是"永不过期"的 token 配了一个
    一天就消失的 cookie，第二天缩略图全灰而 JS 请求一切正常，
    用户完全无从理解。这条用例直接读响应里的 `Max-Age`，
    任何只测 `cookie_max_age_for` 自身的用例都发现不了它。
    """
    from backend.app.auth import COOKIE_MAX_AGE

    # 默认 90 天 → cookie 就是 90 天
    st = _state(tmp_path, db)
    code = _make_code(db)
    r = _call(st, "/api/pair", method="POST", body=f'{{"code":"{code}"}}'.encode())
    assert r.status == 201
    assert _set_cookie_max_age(r) == 90 * 86400

    # 永不过期 → cookie 用浏览器上限，绝不是 1 天
    st2 = _state(tmp_path, db, ttl=0)
    code2 = _make_code(db)
    r2 = _call(st2, "/api/pair", method="POST", body=f'{{"code":"{code2}"}}'.encode())
    assert r2.status == 201
    age = _set_cookie_max_age(r2)
    assert age == COOKIE_MAX_AGE, f"永不过期的 token 配了 {age // 86400} 天的 cookie"

    # 显式短 TTL → cookie 跟着短，不能超过 token
    st3 = _state(tmp_path, db, ttl=1)
    code3 = _make_code(db)
    r3 = _call(st3, "/api/pair", method="POST", body=f'{{"code":"{code3}"}}'.encode())
    assert r3.status == 201
    assert _set_cookie_max_age(r3) <= 86400


def test_panel_devices_reports_expiry(tmp_path: Path, db: Database) -> None:
    """托盘设备列表要能看出"还有几天过期"，不能只回一个布尔。

    "这台手机 3 天后掉线"和"已经掉线了"需要完全不同的用户动作。
    """
    st = _state(tmp_path, db)
    code = _make_code(db)
    r = _call(st, "/api/pair", method="POST", body=f'{{"code":"{code}"}}'.encode())
    assert r.status == 201

    r = _call(st, "/panel/devices", client_ip="127.0.0.1")
    assert r.status == 200
    item = _json(r)["items"][0]
    assert item["expired"] is False
    assert item["expires_at"] is not None
    # 约 90 天后（允许几秒执行误差）
    assert abs(item["expires_at"] - (time.time() + 90 * 86400)) < 60

    # 手工把它改成过去 → 必须报 expired
    db.execute("UPDATE devices SET expires_at = ?", (time.time() - 10,))
    r = _call(st, "/panel/devices", client_ip="127.0.0.1")
    assert _json(r)["items"][0]["expired"] is True


def test_is_loopback_rejects_empty_and_missing_ip() -> None:
    """空 IP 绝不能当成回环（审计 F11）。

    `Ctx.client_ip` 的默认值就是空串，任何忘了填 IP 的构造路径
    （新传输层、测试夹具、某个 future 的进程内调用）会让
    `/panel/**` 静默变成公开接口 —— 而 `/panel/pair-code` 能直接
    签发配对码，等于把配对机制整个绕过。

    ★ 反向用例是这条测试的重点。只断言"127.0.0.1 为真"的话，
    把整个函数改成 `return True` 也能过。
    """
    class _C:
        def __init__(self, ip):
            self.client_ip = ip

    assert not is_loopback(_C(""))
    assert not is_loopback(_C("   "))
    assert not is_loopback(_C(None))
    # 回环本身仍然要认
    assert is_loopback(_C("127.0.0.1"))
    assert is_loopback(_C("::1"))


def test_panel_is_unreachable_when_client_ip_is_unknown(tmp_path: Path, db: Database) -> None:
    """端到端确认：client_ip 为空时 /panel 必须 404，而不是放行。

    这条测的是行为而不是辅助函数 —— `is_loopback` 被三处调用
    （中间件 + 三个 handler 里的第二次判定），任何一处漏用都会
    让上一层的修复白做。
    """
    st = _state(tmp_path, db)
    r = _call(st, "/panel/pair-code", method="POST", client_ip="")
    assert r.status == 404, f"空 client_ip 竟然能签发配对码：{r.status}"
