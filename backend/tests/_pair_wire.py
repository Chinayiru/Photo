"""云端配对接口的测试客户端：走"申请 → 批准 → 轮询"这三步（`docs/04` §3.5）。

2026-11-24 的渗透报告 A1.3 之后，`POST /api/devices/{id}/pair` 不再当场签发
令牌，只回 202 + `request_id` + 6 位核对码。令牌要在用户于电脑端托盘里点过
「批准」之后，由手机 `POST /api/devices/{id}/pair/{request_id}` 一次性取走。

所以以前"一次 POST 拿 token"的测试都得拆成三步。这几步本身不难，但**顺序**
很容易写错（先轮询后批准 → 永远 pending；批准了不轮询 → 库里没有 clients 行），
拆错了断言会变成"看起来通过了但什么也没测"。把顺序收在这里，调用方只表达
"这台手机最终配上了没有"。
"""
from __future__ import annotations

from typing import Any

from starlette.responses import Response

#: 测试用的设备密钥：32 字节随机数的 hex。真实前端由 `crypto.getRandomValues`
#: 生成（`frontend/src/stores/auth.ts`），这里只要形状对（64 位小写 hex）。
DEVICE_KEY = "a1" * 32
#: 第二台手机的密钥（"换设备提交"的用例里要用另一把）。
OTHER_DEVICE_KEY = "b2" * 32


def pair_body(code: str | None = None, *, auto: bool = False, name: str = "手机",
              device_key: str = DEVICE_KEY) -> dict[str, Any]:
    """发起配对用的请求体（码路径 / 自动配对两条路都用它）。"""
    body: dict[str, Any] = {"client_name": name, "device_key": device_key}
    if auto:
        body["auto"] = True
    if code is not None:
        body["code"] = code
    return body


def start(client: Any, device_id: str, token: str,
          body: dict[str, Any]) -> Response:
    """第一步：`POST /api/devices/{id}/pair`（期望 202 + request_id）。"""
    return client.post(f"/api/devices/{device_id}/pair", json=body,
                       headers={"Authorization": f"Bearer {token}"})


def poll(client: Any, device_id: str, token: str, request_id: str,
         *, device_key: str = DEVICE_KEY) -> Response:
    """第三步：`POST /api/devices/{id}/pair/{request_id}`（取令牌 / 问进度）。"""
    return client.post(f"/api/devices/{device_id}/pair/{request_id}",
                       json={"device_key": device_key},
                       headers={"Authorization": f"Bearer {token}"})


def approve_and_poll(client: Any, device_id: str, token: str,
                     pending: Response, *, fake: Any = None,
                     device_key: str = DEVICE_KEY) -> Response:
    """把 202 的申请批准掉，然后轮询到令牌（模拟"用户在电脑上点了批准"）。

    `fake` 是 `_fake_control.FAKE`：它替身电脑端的状态机，`approve_all()`
    就是那次点击。传 None 表示"批准"由测试自己制造（例如直接改状态）。
    """
    assert pending.status_code == 202, pending.text
    if fake is not None:
        fake.approve_all()
    request_id = str(pending.json()["request_id"])
    return poll(client, device_id, token, request_id, device_key=device_key)


def pair_now(client: Any, device_id: str, token: str, body: dict[str, Any],
             *, fake: Any = None,
             device_key: str = DEVICE_KEY) -> Response:
    """发起 + 批准 + 轮询，一条路走完，返回最后一次响应（期望 200 + token）。"""
    return approve_and_poll(client, device_id, token,
                            start(client, device_id, token, body),
                            fake=fake, device_key=device_key)
