"""播放票（`streamticket.py`）：`<video>` 的凭证为什么不能是 cookie。

## 这个文件守的是什么

2026-10-26 的真实故障：安卓手机上点开视频，播放器每 1.5 秒重试一次、
次次 `401 E2002`（"缺少凭证"），用户看到的是"永远在转圈"。

根因不是编码、不是容器：`<video src>` 由浏览器自己发请求，
**发不了 `Authorization` 头**，而 cookie 这条路两次被现实打掉：

- ISSUE-033：`Secure` 写死在 http 页面上 → 浏览器直接丢弃 cookie；
- ISSUE-057：前端 `credentials: "omit"` → 服务端的 `Set-Cookie` 被丢弃。

所以改成"换票"：已认证的 JS 先换一张 5 分钟、**只对单条目**有效的票
（`?t=`），代价是票会进 URL —— 用三条性质把它压到无害：绑定条目、
短命、由**只存在电脑上**的密钥签名。

判据：别的条目的票不能开这一条；过期票不能用；改一个字节就不能用。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db as db_mod  # noqa: E402
from app import streamticket  # noqa: E402


@pytest.fixture()
def database(tmp_path):
    d = db_mod.Database(tmp_path / "photobridge.db")
    d.init()
    yield d
    d.close()


def test_mint_then_verify_roundtrip(database) -> None:
    ticket = streamticket.mint(database, 42)
    assert streamticket.verify(database, ticket, 42) is True


def test_ticket_is_bound_to_one_item(database) -> None:
    """A 的票不能拿去开 B —— 泄漏一张票不该等于泄漏整个库。"""
    ticket = streamticket.mint(database, 42)
    assert streamticket.verify(database, ticket, 43) is False


def test_expired_ticket_is_rejected(database) -> None:
    past = time.time() - 10
    ticket = streamticket.mint(database, 7, ttl=1, now=past)
    assert streamticket.verify(database, ticket, 7) is False


def test_tampered_signature_is_rejected(database) -> None:
    ticket = streamticket.mint(database, 7)
    head, exp, sig = ticket.split(".")
    flipped = ("A" if sig[0] != "A" else "B") + sig[1:]
    assert streamticket.verify(database, f"{head}.{exp}.{flipped}", 7) is False


def test_extending_the_expiry_by_hand_breaks_the_signature(database) -> None:
    """票里唯一可变的部分是过期时间 —— 手改它必须导致签名对不上。"""
    ticket = streamticket.mint(database, 7)
    head, exp, sig = ticket.split(".")
    assert streamticket.verify(database, f"{head}.{int(exp) + 86400}.{sig}", 7) is False


@pytest.mark.parametrize("junk", ["", "st1", "st1.abc.def", "v2.1.2", "st1.1", "..."])
def test_garbage_tickets_never_raise(database, junk: str) -> None:
    """畸形票只回 False、不抛异常：调用方只关心"能不能放行"。"""
    assert streamticket.verify(database, junk, 7) is False


def test_secret_is_not_in_the_ticket(database) -> None:
    """票里不能出现密钥本身（哪怕片段），否则等于把签名权泄漏出去。"""
    from backend.app import control

    secret = control.client_signing_secret(database)
    ticket = streamticket.mint(database, 7)
    assert secret not in ticket
    assert secret[:8] not in ticket


def test_ttl_is_short(database) -> None:
    """票是"打开这一条视频"的凭证，不是长期通行证。"""
    assert 0 < streamticket.STREAM_TICKET_TTL <= 600

# --------------------------------------------------------------------------
# 鉴权中间件的放行条件（这是真正的安全边界，必须逐条钉住）
# --------------------------------------------------------------------------

def _st(tmp_path):
    from app.config import Config
    from app.db import Database
    from app.state import AppState

    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    cfg = Config({
        "roots": [{"id": "library", "path": str(lib)}],
        "thumbs": {"sizes": [320]},
    }, path=data / "config.json", data_dir=data)
    db = Database(cfg.db_path)
    db.init()
    return AppState(cfg, db)


def _ctx(st, path: str, *, query: dict | None = None):
    from app.core import Ctx

    q: dict[str, list[str]] = {}
    for k, v in (query or {}).items():
        q[k] = v if isinstance(v, list) else [str(v)]
    return Ctx(method="GET", path=path, query=q,
               headers={"host": "127.0.0.1:8799"}, body=None, app=st)


def _run_mw(st, path: str, *, query: dict | None = None):
    """跑一次鉴权中间件；放行返回 ("pass",)，拦截抛出异常。"""
    import asyncio

    from app.auth import auth_middleware

    async def nxt(_ctx):
        return "pass"

    return asyncio.run(auth_middleware(_ctx(st, path, query=query), nxt))


def test_empty_ticket_does_not_bypass_auth(tmp_path) -> None:
    """`?t=`（空值）绝不能绕过鉴权。

    `ctx.query` 的值是 `list[str]`：`?t=` 得到的是 `[""]`，非空列表恒为真。
    用 `ctx.query.get("t")` 判真会让**任何人**加个空参数就读到视频 ——
    这是本功能最危险的一行。
    """
    from app.errors import Unauthorized

    st = _st(tmp_path)
    for query in ({"t": ""}, {"t": "   "}, {}):
        with pytest.raises(Unauthorized):
            _run_mw(st, "/api/items/7/stream", query=query)


def test_non_empty_ticket_passes_the_gate(tmp_path) -> None:
    """有票就放行到处理器 —— 票的真伪由处理器按 `media_id` 验。"""
    st = _st(tmp_path)
    assert _run_mw(st, "/api/items/7/stream", query={"t": "st1.1.x"}) == "pass"


def test_ticket_param_on_other_paths_still_requires_auth(tmp_path) -> None:
    """`?t=` 只在 `/stream` 上生效：不能变成"带个参数就免鉴权"的万能钥匙。"""
    from app.errors import Unauthorized

    st = _st(tmp_path)
    for path in ("/api/items/7/raw", "/api/items/7", "/api/timeline",
                 "/api/items/7/stream-ticket", "/api/items/7/streamx"):
        with pytest.raises(Unauthorized):
            _run_mw(st, path, query={"t": "st1.1.x"})

