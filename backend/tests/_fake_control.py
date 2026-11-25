"""控制通道（`/.pb/control`）的测试替身：假装自己是电脑端。

## 为什么测试需要它

2026-10-26 起，手机令牌的**签发权回到了电脑端**：云端库泄露也签不出
令牌（`docs/06` §13）。代价是云端要"问一句"才能配对/认令牌，也就是
`cloud/relay/control.py` 里的 `call_agent()` —— 它要一条真隧道。

2026-11-24 的渗透报告（A1.3）又往前推了一步：**光"问一句电脑"不够**，
因为"电脑"这个身份本身由云端持有（`agent_token` 就在云端库里）。所以
`mint_client` 被拆成了 `request_pair` + `pair_status`：前者只登记一条
待批准记录，后者在用户于本机托盘核对 6 位数字、点过「批准」之后，才把
令牌算出来交给手机。这个替身必须照着这层语义实现，否则测的是旧世界。

用法：

    from _fake_control import FAKE

    def test_xxx(monkeypatch, tmp_path):
        app = _app(tmp_path)
        did, agent_token = _device(app)
        FAKE.reset(secret=shared_token.secret_for_agent_token(agent_token))
        monkeypatch.setattr(cloud_control, "call_agent", FAKE)
        ...
        FAKE.approve_all()      # 模拟用户点了「批准」
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from shared import client_token as shared_token


class FakeAgent:
    """`call_agent()` 的替身：内存里管申请、签发、验签。

    `pending[request_id]` 里的 `state` 与电脑端 `backend/app/pairing.py`
    的状态机一致：`pending` → `approved` → `consumed`，或 `pending` →
    `rejected`。`approve_all()` / `reject_all()` 是测试里的"用户点击"。
    """

    def __init__(self) -> None:
        self.secret = ""
        self.online = True
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.pending: dict[str, dict[str, Any]] = {}

    def reset(self, *, secret: str = "") -> None:
        self.secret = secret
        self.online = True
        self.calls.clear()
        self.pending.clear()

    async def __call__(self, conn: Any, action: str,
                       params: dict[str, Any], *,
                       timeout: float | None = None) -> dict | None:
        self.calls.append((action, dict(params)))
        if not self.online:
            return None
        if action == "request_pair":
            return self._request(params)
        if action == "pair_status":
            return self._status(params)
        if action == "mint_client":
            # 旧动作现在必须被拒：渗透报告 A1.3 就是这条动作造的洞。
            return {"ok": False, "code": "E5015", "message": "需要电脑端批准"}
        if action == "verify_client":
            return self._verify(params)
        if action == "revoke_clients":
            return {"ok": True, "revoked": len(params.get("ids") or [])}
        return None

    # -- 测试用的"用户点击" ---------------------------------------
    def approve_all(self) -> None:
        for row in self.pending.values():
            if row["state"] == "pending":
                row["state"] = "approved"

    def reject_all(self) -> None:
        for row in self.pending.values():
            if row["state"] == "pending":
                row["state"] = "rejected"
                row["reason"] = "用户拒绝了这次配对"

    # -- 动作实现 -------------------------------------------------
    def _request(self, params: dict[str, Any]) -> dict:
        request_id = uuid.uuid4().hex
        now = time.time()
        code = f"{len(self.pending) % 1000000:06d}"
        self.pending[request_id] = {
            "state": "pending",
            "device_id": str(params.get("device_id") or ""),
            "device_key": str(params.get("device_key") or ""),
            "client_name": str(params.get("client_name") or "手机")[:64],
            "ttl_days": int(params.get("ttl_days") or 90),
            "verify_code": code,
            "expires_at": now + 300.0,
        }
        return {"ok": True, "state": "pending", "request_id": request_id,
                "verify_code": code,
                "fingerprint": "0f0f0f",
                "expires_at": now + 300.0}

    def _status(self, params: dict[str, Any]) -> dict | None:
        request_id = str(params.get("request_id") or "")
        row = self.pending.get(request_id)
        if row is None:
            return {"ok": True, "state": "rejected", "reason": "申请不存在"}
        if str(params.get("device_key") or "") != row["device_key"]:
            # 与线上对齐（`control._pair_status`）：密钥不符是**终态**，
            # 归一成 rejected 让手机去重新配对，而不是含糊的 "电脑不在线"。
            return {"ok": True, "state": "rejected",
                    "reason": "这台手机的配对密钥与申请不一致，请重新配对"}
        if row["state"] == "rejected":
            return {"ok": True, "state": "rejected",
                    "reason": row.get("reason") or "已拒绝"}
        if row["state"] == "consumed":
            return {"ok": True, "state": "rejected", "reason": "已经被取走了"}
        if row["state"] == "pending":
            return {"ok": True, "state": "pending",
                    "verify_code": row["verify_code"],
                    "expires_at": row["expires_at"]}
        now = time.time()
        client_id = "c" + uuid.uuid4().hex[:15]
        token = shared_token.issue_token(
            self.secret, client_id=client_id, device_id=row["device_id"],
            ttl_days=row["ttl_days"], now=now,
            client_name=row["client_name"])
        row["state"] = "consumed"
        return {"ok": True, "state": "approved", "client_id": client_id,
                "token": token, "expires_at": now + row["ttl_days"] * 86400,
                "client_name": row["client_name"]}

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
