"""云端统一的响应与错误信封。

## 为什么云端要**模仿**电脑端的错误形状

`frontend/src/api/client.ts` 的 `ApiError` 只认一种形状：

```json
{"error": {"code": "E2002", "message": "...", "detail": null,
           "retryable": false, "request_id": "...", "user_hint": "..."}}
```

云端是另一个进程、另一套代码，但只要它回的也是这个形状，前端的
`isNetwork` / `retryable` / `friendly` 三套判断就一行都不用改。反之如果
云端回 `{"detail": "..."}`（FastAPI 的默认形状），手机上会显示
"未知错误"——而这正是用户最需要知道"是电脑离线还是配对过期"的时刻。

## 为什么 `detail` 永远是 None

`docs/06` 与一期审计都确认：错误响应里出现绝对路径、SQL、堆栈，
等于把服务器的内部结构免费送人。云端的 `detail` 只进日志。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from starlette.responses import Response

__all__ = [
    "CODES",
    "json_reply",
    "json_error",
    "text_reply",
    "no_content",
    "error_body",
]

#: 云端错误码 → (HTTP 状态, 文案, 是否可重试, 用户提示)。
#:
#: 这张表同时被 `/d/**` 的帧错误映射与账号接口共用，目的是**同一个码
#: 永远同一个状态**。曾经踩过的坑：`E2002` 在一个接口是 401、在另一个
#: 是 403，前端因此把"配对过期"当成"没权限"，重试逻辑全错。
CODES: dict[str, tuple[int, str, bool, str]] = {
    "E1001": (400, "请求参数不正确", False, "检查一下填的内容"),
    "E1004": (404, "内容不存在", False, "可能已被删除"),
    "E1006": (413, "文件太大", False, "换一张小一点的照片"),
    "E1009": (403, "该路径不允许访问", False, ""),
    "E2001": (401, "登录已过期，请重新登录", False, "重新登录账号"),
    "E2002": (401, "配对已失效，请重新配对", False, "在电脑上重新配对"),
    "E2003": (403, "无权访问该设备", False, ""),
    "E2005": (401, "邮箱或密码错误", False, "检查邮箱和密码"),
    "E2006": (403, "该电脑已被移除", False, ""),
    "E3001": (500, "服务器处理失败", True, "稍后再试"),
    "E3003": (503, "电脑离线", True, "确认那台电脑开着并且联网"),
    "E3008": (503, "正在重新连接电脑", True, "等几秒再试"),
    "E3009": (504, "电脑响应超时", True, "稍后再试"),
    "E4001": (429, "请求太频繁", True, "慢一点"),
    "E4002": (429, "同时进行的任务太多", True, "等当前的上传完成"),
    "E4003": (429, "请求太密集", True, "慢一点"),
    "E4004": (429, "配对码尝试次数过多", False, "10 分钟后再试"),
}


def error_body(code: str, *, message: str | None = None,
               request_id: str = "") -> dict[str, Any]:
    """构造标准错误体。未登记的码按 500 处理，绝不放行未知码当成功。"""
    status, default_msg, retryable, hint = CODES.get(
        code, (500, "服务器处理失败", True, "稍后再试"))
    return {
        "error": {
            "code": code,
            "message": message or default_msg,
            "detail": None,             # ★ 永不外泄
            "retryable": retryable,
            "request_id": request_id,
            "user_hint": hint,
        }
    }


def json_error(code: str, *, message: str | None = None,
               request_id: str = "", headers: dict[str, str] | None = None,
               retry_after: int | None = None) -> Response:
    status, _msg, _retry, _hint = CODES.get(code, (500, "服务器处理失败",
                                                   True, "稍后再试"))
    hdrs = {"content-type": "application/json; charset=utf-8",
            "cache-control": "no-store"}
    if request_id:
        hdrs["x-request-id"] = request_id
    if retry_after is not None:
        hdrs["retry-after"] = str(max(0, int(retry_after)))
    if headers:
        hdrs.update(headers)
    return Response(
        json.dumps(error_body(code, message=message,
                              request_id=request_id),
                   ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status_code=status, headers=hdrs)


def json_reply(data: Any, *, status: int = 200,
               headers: dict[str, str] | None = None,
               request_id: str = "") -> Response:
    hdrs = {"content-type": "application/json; charset=utf-8",
            "cache-control": "no-store"}
    if request_id:
        hdrs["x-request-id"] = request_id
    if headers:
        hdrs.update(headers)
    return Response(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        status_code=status, headers=hdrs)


def text_reply(text: str, *, status: int = 200,
               headers: dict[str, str] | None = None) -> Response:
    hdrs = {"content-type": "text/plain; charset=utf-8"}
    if headers:
        hdrs.update(headers)
    return Response(text.encode("utf-8"), status_code=status, headers=hdrs)


def no_content(extra: Iterable[tuple[str, str]] = ()) -> Response:
    """204。**不带 content-type**：带了会让某些客户端去解析一个空体。"""
    resp = Response(status_code=204)
    for k, v in extra:
        resp.raw_headers.append((k.encode("latin-1"), v.encode("latin-1")))
    return resp


def add_headers(resp: Response, headers: dict[str, str]) -> Response:
    for k, v in headers.items():
        resp.headers[k] = v
    return resp
