"""控制通道（`/.pb/control`）的测试替身：假装自己是电脑端。

## 为什么测试需要它

2026-10-26 起，手机令牌的**签发权回到了电脑端**：云端库泄露也签不出
令牌（`docs/06` §13）。代价是云端要"问一句"才能配对/认令牌，也就是
`cloud/relay/control.py` 里的 `call_agent()` —— 它要一条真隧道。

单元测试不想为每一次配对起一条 WebSocket 隧道，所以把 `call_agent`
换成本文件里的 `FakeAgent`：它做的事情跟 `backend/app/control.py` 里
的真实实现一致（用密钥签令牌 / 用密钥验令牌），但不走网络。

用法：

    from _fake_control import FAKE

    def test_xxx(monkeypatch, tmp_path):
        app = _app(tmp_path)
        did, agent_token = _device(app)
        FAKE.secret = shared_token.secret_for_agent_token(agent_token)
        monkeypatch.setattr(cloud_control, "call_agent", FAKE)
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from shared import client_token as shared_token


class FakeAgent:
    """`call_agent()` 的替身：只在内存里签名/验签。"""

    def __init__(self) -> None:
        self.secret = ""
        self.online = True
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, conn: Any, action: str,
                       params: dict[str, Any], *,
                       timeout: float | None = None) -> dict | None:
        self.calls.append((action, dict(params)))
        if not self.online:
            return None
        if action == "mint_client":
            return self._mint(params)
        if action == "verify_client":
            return self._verify(params)
        if action == "revoke_clients":
            return {"ok": True, "revoked": len(params.get("ids") or [])}
        return None

    # -- 动作实现 -------------------------------------------------
    def _mint(self, params: dict[str, Any]) -> dict:
        client_id = "c" + uuid.uuid4().hex[:15]
        device_id = str(params.get("device_id") or "")
        name = str(params.get("name") or "手机")[:64]
        ttl_days = int(params.get("ttl_days") or 90)
        now = time.time()
        token = shared_token.issue_token(
            self.secret, client_id=client_id, device_id=device_id,
            ttl_days=ttl_days, now=now, client_name=name)
        return {"ok": True, "client_id": client_id, "token": token,
                "expires_at": now + ttl_days * 86400, "name": name}

    def _verify(self, params: dict[str, Any]) -> dict | None:
        token = str(params.get("token") or "")
        claims = shared_token.verify_token(self.secret, token)
        if claims is None:
            return {"ok": True, "valid": False}
        return {"ok": True, "valid": True,
                "client_id": str(claims.get("sub") or ""),
                "device_id": str(claims.get("dev") or ""),
                "expires_at": float(claims.get("exp") or 0.0),
                "name": str(claims.get("nam") or "")}


FAKE = FakeAgent()
