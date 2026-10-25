"""`x-pb-origin` 标记头（前端靠它区分"电脑伺服"与"中继伺服"）。

背景：配对页要决定给用户「配对码」还是「账号登录」。只看域名不够 ——
家里电脑用 Tailscale MagicDNS 或自备域名访问时，主机名和中继域名长得
一样；只看 `/api/system/ping` 的返回体也不够 —— 中继会把公开的
`/api/*` 转到设备上，中继首页同样能拿到 `{"pong":true}`。真机上表现为
"局域网入口里配对码整块消失"，用户报过两次。

所以判据落在响应头上，并且必须**每个响应都带**（包括 401/500），
否则"出错时页面跳成中继登录"这种更难查的 bug 就会冒出来。
"""

from __future__ import annotations

import asyncio

from backend.app.core.ctx import Ctx, Reply
from backend.app.server import _origin_marker_middleware


def _run(coro):
    return asyncio.run(coro)


def test_marks_normal_reply() -> None:
    async def nxt(_ctx: Ctx) -> Reply:
        return Reply(200, body=b"{}")

    reply = _run(_origin_marker_middleware(Ctx(method="GET", path="/api/system/ping"), nxt))
    assert reply.headers.get("x-pb-origin") == "agent"


def test_marks_error_reply_too() -> None:
    """401/500 也要带：否则失败时前端会误判成"这是中继首页"。"""

    async def nxt(_ctx: Ctx) -> Reply:
        return Reply(401, body=b"{}")

    reply = _run(_origin_marker_middleware(Ctx(method="GET", path="/api/system/info"), nxt))
    assert reply.status == 401
    assert reply.headers.get("x-pb-origin") == "agent"


def test_header_survives_handler_exception() -> None:
    """处理函数抛异常时中间件不该吞掉异常，也不该影响异常向上传播。"""

    async def nxt(_ctx: Ctx) -> Reply:
        raise RuntimeError("boom")

    try:
        _run(_origin_marker_middleware(Ctx(method="GET", path="/api/items/1/raw"), nxt))
    except RuntimeError as exc:
        assert "boom" in str(exc)
    else:  # pragma: no cover - 真失败时才走到
        raise AssertionError("异常应原样向上抛")
