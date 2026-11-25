"""配对批准模块（`backend/app/pairing.py`）的判据。

## 这个模块存在的理由

2026-11-24 的渗透报告 A1.3：假云端拿着 `agent_token` 走完 `/agent/ws` 握手，
发一条 `{"action": "mint_client"}` 就换到了真令牌，随后读到了时间线与原图。
问题不在"令牌怎么签"，而在**谁有权决定一台新手机可以成为客户端** —— 这个
决定必须是人在电脑前做的。

所以配对被拆成"申请 → 人在托盘里核对 6 位数字 → 批准 → 手机一次性兑换"。
本文件钉住这条路不往回退的性质：

* 申请本身**不产生任何令牌**；
* 兑换要带上**发起它的那台手机**的设备密钥；
* 一条申请只能兑换一次（并发也一样）；
* 拒绝/过期是终态，不会补签；
* 审计表记得下"谁在什么时候被批准/拒绝/撤销"，但**不记明文令牌**。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backend.app import pairing                            # noqa: E402
from backend.app.db import Database                        # noqa: E402
from backend.app.identity import client_signing_secret      # noqa: E402
from shared import client_token as token_mod                # noqa: E402

DEVICE_ID = "aa" * 16
DEVICE_KEY = "1f" * 32
OTHER_KEY = "2e" * 32


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "agent.db")
    database.init()
    return database


def _request(database: Database, **kw) -> dict:
    kw.setdefault("device_id", DEVICE_ID)
    kw.setdefault("device_key", DEVICE_KEY)
    return pairing.create_request(database, **kw)


def _events(database: Database, event: str) -> list:
    return database.query("SELECT * FROM pair_events WHERE event = ?", (event,))


# --------------------------------------------------------------- 申请


def test_a_request_records_no_token_and_no_device_row(db: Database) -> None:
    """申请只是登记：库里既没有令牌串，也没有 devices 行。"""
    request = _request(db, client_name="iPhone")
    assert request["state"] == "pending"
    assert len(request["request_id"]) == 32
    assert len(request["verify_code"]) == 6
    assert request["verify_code"].isdigit()
    assert request["fingerprint"] == pairing.fingerprint(DEVICE_KEY)
    assert db.query_one("SELECT COUNT(*) AS n FROM devices")["n"] == 0
    # 明文令牌串根本不该出现在任何列里
    dump = str(db.query("SELECT * FROM pair_requests"))
    assert "pbc1." not in dump


def test_fingerprint_is_six_hex_chars(db: Database) -> None:
    """托盘与手机比的是指纹，不是密钥本身。"""
    fp = pairing.fingerprint(DEVICE_KEY)
    assert len(fp) == 6 and all(c in "0123456789abcdef" for c in fp)
    assert pairing.fingerprint(DEVICE_KEY) == fp        # 稳定
    assert pairing.fingerprint(OTHER_KEY) != fp


def test_request_rejects_a_bad_device_key(db: Database) -> None:
    with pytest.raises(pairing.PairError) as exc:
        _request(db, device_key="zz" * 32)
    assert exc.value.code == "E5001"


def test_pending_requests_are_capped(db: Database) -> None:
    """挂着的申请太多 → E5004（用户可能不在电脑前，托盘那行没人点）。"""
    for _ in range(pairing.MAX_PENDING):
        _request(db)
    with pytest.raises(pairing.PairError) as exc:
        _request(db)
    assert exc.value.code == "E5004"
    assert exc.value.status == 429
    assert pairing.count_pending(db) == pairing.MAX_PENDING


def test_list_pending_never_leaks_the_device_key(db: Database) -> None:
    """托盘只该看到指纹：设备密钥是手机那边的凭据。"""
    _request(db, client_name="iPhone")
    rows = pairing.list_pending(db)
    assert len(rows) == 1
    assert rows[0]["client_name"] == "iPhone"
    assert "device_key" not in rows[0]
    assert rows[0]["fingerprint"] == pairing.fingerprint(DEVICE_KEY)


def test_an_expired_request_disappears_from_pending(db: Database) -> None:
    _request(db, now=time.time() - pairing.REQUEST_TTL - 1)
    assert pairing.list_pending(db) == []
    assert pairing.count_pending(db) == 0
    row = db.query_one("SELECT state FROM pair_requests")
    assert str(row["state"]) == "expired"


# --------------------------------------------------------------- 批准/拒绝


def test_only_pending_requests_can_be_decided(db: Database) -> None:
    """重复点「批准」→ E5011（409），不会签出第二个 client_id。"""
    request = _request(db)
    first = pairing.decide(db, request["request_id"], approve=True, source="tray")
    assert first["state"] == "approved"
    with pytest.raises(pairing.PairError) as exc:
        pairing.decide(db, request["request_id"], approve=True, source="tray")
    assert exc.value.code == "E5011"
    assert exc.value.status == 409


def test_unknown_request_is_404(db: Database) -> None:
    with pytest.raises(pairing.PairError) as exc:
        pairing.decide(db, "0" * 32, approve=True)
    assert exc.value.code == "E5010"
    assert exc.value.status == 404


def test_decide_checks_the_device_key_when_given(db: Database) -> None:
    """批准入口若挪到手机上，这一条保证"批准必须由同一台设备提交"。"""
    request = _request(db)
    with pytest.raises(pairing.PairError) as exc:
        pairing.decide(db, request["request_id"], approve=True,
                       device_key=OTHER_KEY)
    assert exc.value.code == "E5005"


def test_decide_on_an_expired_request_marks_it_expired(db: Database) -> None:
    request = _request(db)
    with pytest.raises(pairing.PairError) as exc:
        pairing.decide(db, request["request_id"], approve=True,
                       now=time.time() + pairing.REQUEST_TTL + 1)
    assert exc.value.code == "E5011"
    row = db.query_one("SELECT state FROM pair_requests WHERE request_id = ?",
                       (request["request_id"],))
    assert str(row["state"]) == "expired"


def test_rejection_is_terminal(db: Database) -> None:
    request = _request(db)
    pairing.decide(db, request["request_id"], approve=False, source="tray")
    with pytest.raises(pairing.PairError) as exc:
        pairing.redeem(db, request["request_id"], DEVICE_KEY)
    assert exc.value.code == "E5012"
    assert exc.value.status == 403


# --------------------------------------------------------------- 兑换


def test_pending_redeem_only_returns_the_verify_code(db: Database) -> None:
    """还没批准时轮询：只回核对码，没有任何令牌字段。"""
    request = _request(db)
    waiting = pairing.redeem(db, request["request_id"], DEVICE_KEY)
    assert waiting["state"] == "pending"
    assert waiting["verify_code"] == request["verify_code"]
    assert "token" not in waiting


def test_approved_request_yields_a_verifiable_token_once(db: Database) -> None:
    request = _request(db, client_name="iPhone", ttl_days=30)
    pairing.decide(db, request["request_id"], approve=True, source="tray")

    minted = pairing.redeem(db, request["request_id"], DEVICE_KEY)
    assert minted["state"] == "approved"
    assert minted["token"].startswith("pbc1.")
    assert minted["client_name"] == "iPhone"

    claims = token_mod.verify_token(client_signing_secret(db), minted["token"],
                                    device_id=DEVICE_ID)
    assert claims is not None
    assert claims["sub"] == minted["client_id"]

    # 一次性：第二次只拿到"已兑换"
    with pytest.raises(pairing.PairError) as exc:
        pairing.redeem(db, request["request_id"], DEVICE_KEY)
    assert exc.value.code == "E5013"
    assert exc.value.status == 409


def test_a_foreign_device_key_cannot_redeem(db: Database) -> None:
    """中继拿着 request_id 也换不走令牌：密钥不对就拒绝，并留痕。"""
    request = _request(db)
    pairing.decide(db, request["request_id"], approve=True, source="tray")
    with pytest.raises(pairing.PairError) as exc:
        pairing.redeem(db, request["request_id"], OTHER_KEY)
    assert exc.value.code == "E5005"
    assert exc.value.status == 403
    assert len(_events(db, "denied")) == 1
    # 申请没有被作废：合法手机还能继续轮询
    assert pairing.redeem(db, request["request_id"], DEVICE_KEY)["state"] == "approved"


def test_redeem_of_a_missing_request_is_404(db: Database) -> None:
    with pytest.raises(pairing.PairError) as exc:
        pairing.redeem(db, "0" * 32, DEVICE_KEY)
    assert exc.value.code == "E5010"
    assert exc.value.status == 404


def test_token_ttl_follows_the_request(db: Database) -> None:
    """有效期按申请时定的 ttl 走，且以**批准那一刻**为起点。"""
    request = _request(db, ttl_days=7)
    pairing.decide(db, request["request_id"], approve=True, source="tray")
    minted = pairing.redeem(db, request["request_id"], DEVICE_KEY)
    claims = token_mod.verify_token(client_signing_secret(db), minted["token"])
    assert claims is not None
    span = float(claims["exp"]) - float(claims["iat"])
    assert span == pytest.approx(7 * 86400, abs=2)


# --------------------------------------------------------------- 审计与清理


def test_events_tell_the_whole_story_without_secrets(db: Database) -> None:
    """审计要能回答"那天谁配了我的电脑"，但不能记令牌。"""
    request = _request(db, client_name="iPhone")
    pairing.decide(db, request["request_id"], approve=True, source="tray")
    minted = pairing.redeem(db, request["request_id"], DEVICE_KEY)

    kinds = [str(r["event"]) for r in db.query(
        "SELECT event FROM pair_events ORDER BY id")]
    assert kinds == ["requested", "approved", "consumed"]
    # sqlite3.Row 的 repr 不带值，要逐列取出来拼
    dump = "\n".join(
        f"{r['event']} {r['request_id']} {r['client_id']} {r['device_key']}"
        f" {r['result']}"
        for r in db.query("SELECT * FROM pair_events ORDER BY id"))
    assert minted["token"] not in dump
    assert minted["client_id"] in dump        # client_id 是允许留痕的
    # 审计里存的是**指纹**，不是密钥本身：将来把库整份贴给别人排查，
    # 也不该顺手把手机的配对凭据送出去（见 `_event()` 的实现）
    assert pairing.fingerprint(DEVICE_KEY) in dump
    assert DEVICE_KEY not in dump


def test_purge_keeps_events_longer_than_requests(db: Database) -> None:
    """请求是可兑换的凭据，越早消失越好；事件是审计，要多留。"""
    request = _request(db)
    pairing.decide(db, request["request_id"], approve=False, source="tray")
    now = time.time()
    assert pairing.purge(db, now=now + 10 * 86400) >= 1
    assert db.query_one("SELECT COUNT(*) AS n FROM pair_requests")["n"] == 0
    assert len(_events(db, "rejected")) == 1

    assert pairing.purge(db, now=now + 100 * 86400) >= 0
    assert len(_events(db, "rejected")) == 0
