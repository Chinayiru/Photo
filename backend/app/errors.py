"""PhotoBridge 统一错误模型。

设计要点（docs/03-详细设计说明书.md §6.1、docs/04-接口设计文档.md §5）：

1. **一个错误码 = 一个 ErrorSpec 常量**。码表是数据不是逻辑，集中在此，
   新增错误只需加一行常量，不必改任何分支代码。
2. **错误码前缀即语义**：E1xxx→4xx 可修复、E2xxx→401/403 鉴权、E3xxx→5xx 服务端、
   E4xxx→429 限流、E5xxx→隧道内部（不直接暴露给手机）。前端按前缀决定重试策略。
3. **绝不把 str(e) 直接返回客户端**。to_app_error 把所有未匹配异常归一成 E3001，
   并剥掉异常文本。响应体禁止出现：绝对路径、Traceback、SQL 片段、库内文件名、版本号。
   这不是洁癖——「D:\\Photos\\2024\\IMG_001.jpg 不存在」会同时泄漏
   目录结构、文件名和个人照片存在性三件事。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Final

# --------------------------------------------------------------------------
# ErrorSpec：错误码的数据定义
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ErrorSpec:
    """一个错误码的全部静态信息。"""

    code: str
    status: int
    message: str        # 面向开发者：稳定、简短、英文，可用于日志与断言
    user_hint: str      # 面向用户：简体中文，前端可直接显示
    retryable: bool = False


# --- E1xxx 请求/参数类 ------------------------------------------------------
E1001: Final = ErrorSpec("E1001", 400, "invalid_request", "请求参数有误")
E1002: Final = ErrorSpec("E1002", 400, "pair_code_not_found", "配对码无效，请核对电脑上显示的 6 位数字")
E1003: Final = ErrorSpec("E1003", 403, "pair_code_expired", "配对码已过期，请在电脑上重新生成")
E1004: Final = ErrorSpec("E1004", 404, "not_found", "内容不存在或已被删除")
E1005: Final = ErrorSpec("E1005", 409, "pair_code_used", "配对码已使用，请重新生成")
E1006: Final = ErrorSpec("E1006", 413, "file_too_large", "文件超过 2 GB 上限")
E1007: Final = ErrorSpec("E1007", 413, "chunk_too_large", "分片过大")
E1008: Final = ErrorSpec("E1008", 416, "range_not_satisfiable", "文件已变化，请重新下载")
E1009: Final = ErrorSpec("E1009", 400, "path_denied", "路径不合法")
E1010: Final = ErrorSpec("E1010", 400, "bad_content_range", "上传参数异常")
E1011: Final = ErrorSpec("E1011", 416, "chunk_out_of_order", "上传顺序异常，正在自动修复", retryable=True)
E1012: Final = ErrorSpec("E1012", 409, "upload_state_conflict", "上传已结束，请重新开始")
E1013: Final = ErrorSpec("E1013", 400, "hash_mismatch", "文件校验失败，请重新上传")
E1014: Final = ErrorSpec("E1014", 409, "upload_incomplete", "文件未传完，正在继续", retryable=True)
E1015: Final = ErrorSpec("E1015", 403, "write_disabled", "此操作已被电脑端设置禁用")
E1016: Final = ErrorSpec("E1016", 403, "pair_code_wrong_device", "配对码不属于这台电脑")
E1017: Final = ErrorSpec("E1017", 400, "ext_not_allowed", "不支持的文件格式")
E1018: Final = ErrorSpec("E1018", 405, "method_not_allowed", "该操作不被支持")

# --- E2xxx 认证/授权类 ------------------------------------------------------
E2001: Final = ErrorSpec("E2001", 401, "account_token_invalid", "登录已过期，请重新登录")
E2002: Final = ErrorSpec("E2002", 401, "client_token_invalid", "配对已失效，请重新配对")
E2003: Final = ErrorSpec("E2003", 403, "token_resource_mismatch", "无权访问该设备")
E2004: Final = ErrorSpec("E2004", 403, "token_revoked", "该设备已被移除，请重新配对")
E2005: Final = ErrorSpec("E2005", 401, "bad_credentials", "邮箱或密码错误")
E2006: Final = ErrorSpec("E2006", 403, "device_revoked", "该电脑已从账号中移除")

# --- E3xxx 服务端/资源类 ----------------------------------------------------
E3001: Final = ErrorSpec("E3001", 500, "internal_error", "服务器开小差了，请重试", retryable=True)
E3002: Final = ErrorSpec("E3002", 503, "db_busy", "电脑正忙，请稍后重试", retryable=True)
E3003: Final = ErrorSpec("E3003", 503, "device_offline", "电脑离线，请确认电脑已开机且 PhotoBridge 正在运行", retryable=True)
E3004: Final = ErrorSpec("E3004", 500, "thumb_failed", "这张图片无法生成缩略图")
E3005: Final = ErrorSpec("E3005", 500, "thumb_failed_cached", "这张图片无法生成缩略图")
E3006: Final = ErrorSpec("E3006", 500, "fs_operation_failed", "文件操作失败，请查看电脑上的日志")
E3007: Final = ErrorSpec("E3007", 500, "exif_parse_failed", "无法读取照片信息")
E3008: Final = ErrorSpec("E3008", 503, "tunnel_reconnecting", "正在重新连接电脑…", retryable=True)
E3009: Final = ErrorSpec("E3009", 504, "agent_timeout", "电脑响应超时，请重试", retryable=True)
E3010: Final = ErrorSpec("E3010", 507, "disk_full", "电脑磁盘空间不足")
E3011: Final = ErrorSpec("E3011", 500, "indexer_error", "索引出错，请查看电脑上的日志")
E3012: Final = ErrorSpec("E3012", 409, "target_exists", "目标位置已有同名文件")

# --- E4xxx 限流/配额类 ------------------------------------------------------
E4001: Final = ErrorSpec("E4001", 429, "rate_limited", "操作太频繁，请稍后再试", retryable=True)
E4002: Final = ErrorSpec("E4002", 429, "too_many_uploads", "同时上传的任务太多", retryable=True)
E4003: Final = ErrorSpec("E4003", 429, "too_many_tunnel_requests", "请求太密集，请稍后再试", retryable=True)
E4004: Final = ErrorSpec("E4004", 429, "pair_code_tries_exceeded", "尝试次数过多，请 10 分钟后再试", retryable=True)
E4005: Final = ErrorSpec("E4005", 429, "quota_exceeded", "本月流量已用完")

# --- E5xxx 隧道协议类（不直接暴露给手机） -----------------------------------
E5001: Final = ErrorSpec("E5001", 400, "bad_frame", "内部协议错误")
E5002: Final = ErrorSpec("E5002", 500, "agent_exception", "服务器内部错误", retryable=True)
E5003: Final = ErrorSpec("E5003", 413, "frame_too_large", "请求体过大")
E5004: Final = ErrorSpec("E5004", 403, "path_not_allowed", "该路径不允许访问")
E5005: Final = ErrorSpec("E5005", 403, "agent_revoked", "该电脑已被移除")
E5006: Final = ErrorSpec("E5006", 500, "protocol_version_mismatch", "版本不匹配，请升级")
E5007: Final = ErrorSpec("E5007", 404, "no_route", "内容不存在")
E5008: Final = ErrorSpec("E5008", 500, "credit_timeout", "传输卡住，请重试", retryable=True)
E5009: Final = ErrorSpec("E5009", 504, "request_timeout", "电脑响应超时", retryable=True)
E5010: Final = ErrorSpec("E5010", 429, "req_id_exhausted", "请求太密集", retryable=True)
E5011: Final = ErrorSpec("E5011", 499, "cancelled", "请求已取消")
E5012: Final = ErrorSpec("E5012", 500, "bad_json", "内部协议错误")

SPECS: Final[dict[str, ErrorSpec]] = {
    s.code: s
    for s in (
        E1001, E1002, E1003, E1004, E1005, E1006, E1007, E1008, E1009,
        E1010, E1011, E1012, E1013, E1014, E1015, E1016, E1017, E1018,
        E2001, E2002, E2003, E2004, E2005, E2006,
        E3001, E3002, E3003, E3004, E3005, E3006, E3007, E3008, E3009,
        E3010, E3011, E3012,
        E4001, E4002, E4003, E4004, E4005,
        E5001, E5002, E5003, E5004, E5005, E5006, E5007, E5008, E5009,
        E5010, E5011, E5012,
    )
}


# --------------------------------------------------------------------------
# 响应体的敏感信息防线
# --------------------------------------------------------------------------

# 每一条都对应一次真实的信息泄漏。写测试断言这些正则不会命中任何错误响应。
_FORBIDDEN: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("absolute_path", re.compile(r"[A-Za-z]:[\\/]|\\\\[^\\]|/(?:home|root|Users|var|etc)/")),
    ("traceback", re.compile(r"Traceback|\bFile \"|line \d+, in ")),
    ("sql", re.compile(r"\bSELECT\b|\bINSERT\b|\bUPDATE\b|\bDELETE FROM\b|sqlite3\.|no such table")),
    ("version", re.compile(r"Python/\d|Pillow\b|uvicorn/|fastapi/", re.I)),
    ("stack_class", re.compile(r"\b\w+Error\b|\b\w+Exception\b")),
)


def find_leak(text: str) -> str | None:
    """返回命中的禁用规则名，未命中返回 None。测试与开发断言用。"""
    for name, pat in _FORBIDDEN:
        if pat.search(text):
            return name
    return None


def sanitize(text: str | None, fallback: str = "") -> str:
    """把不可信文本洗成可安全返回的字符串。

    为什么是"整条丢弃"而不是"替换敏感片段"：部分脱敏（如把 `E:\\a\\b.jpg` 换成
    `E:\\***\\b.jpg`）仍会泄漏文件名，且正则总有绕过方式。安全默认值是丢弃。
    """
    if not text:
        return fallback
    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > 300:
        text = text[:300] + "…"
    return fallback if find_leak(text) else text


# --------------------------------------------------------------------------
# AppError
# --------------------------------------------------------------------------


class AppError(Exception):
    """所有可预期错误的基类。

    detail 只在开发模式下回传；生产环境下 to_response 会把它洗一遍。
    """

    def __init__(
        self,
        spec: ErrorSpec,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"{spec.code} {spec.message}")
        self.spec = spec
        self.detail = detail
        self.headers = headers or {}

    # 便捷只读属性，让调用方不必到处写 .spec.xxx
    @property
    def code(self) -> str:
        return self.spec.code

    @property
    def status(self) -> int:
        return self.spec.status

    @property
    def retryable(self) -> bool:
        return self.spec.retryable

    def to_response(self, request_id: str) -> dict[str, Any]:
        """转成 docs/04 §1.6 的统一错误结构。"""
        err: dict[str, Any] = {
            "code": self.spec.code,
            "message": self.spec.message,
            "detail": sanitize(self.detail) or None,
            "retryable": self.spec.retryable,
            "request_id": request_id,
            "user_hint": self.spec.user_hint,
        }
        return {"error": err}

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.spec.code} {self.detail or ''}>"


class BadRequest(AppError):
    def __init__(self, detail: str | None = None, spec: ErrorSpec = E1001) -> None:
        super().__init__(spec, detail)


class NotFound(AppError):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__(E1004, detail)


class Unauthorized(AppError):
    def __init__(self, detail: str | None = None, spec: ErrorSpec = E2002) -> None:
        super().__init__(spec, detail)


class Forbidden(AppError):
    def __init__(self, detail: str | None = None, spec: ErrorSpec = E2003) -> None:
        super().__init__(spec, detail)


class Conflict(AppError):
    def __init__(self, detail: str | None = None, spec: ErrorSpec = E1012) -> None:
        super().__init__(spec, detail)


class Internal(AppError):
    """未预期异常的统一出口。detail 绝不来自 str(exc)。"""

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(E3001, detail)


class SandboxError(AppError):
    """路径沙箱拒绝。

    这是安全事件而非普通参数错误，因此额外携带 `rule`（七步校验中哪一步拒绝），
    供审计日志与告警使用；但 rule 本身不进响应体（会提示攻击者如何绕过）。
    """

    def __init__(self, rule: str, detail: str | None = None, spec: ErrorSpec = E1009) -> None:
        super().__init__(spec, detail)
        self.rule = rule


class ReadonlyError(AppError):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__(E1015, detail)


class ArchiveError(AppError):
    def __init__(self, detail: str | None = None) -> None:
        super().__init__(E3006, detail)


class UploadError(AppError):
    pass


# --------------------------------------------------------------------------
# 异常归一
# --------------------------------------------------------------------------

# 顺序敏感：先具体后宽泛。sqlite3.OperationalError 必须在 OSError 之前，
# 因为 OperationalError 不是 OSError 的子类，但两者都想映射到不同的码。
EXC_MAP: Final[tuple[tuple[type[BaseException], ErrorSpec], ...]] = (
    (SandboxError, E1009),
    (ReadonlyError, E1015),
    (ArchiveError, E3006),
    (sqlite3.OperationalError, E3002),
    (OSError, E3006),
)


def to_app_error(exc: BaseException) -> AppError:
    """把任意异常归一成 AppError。未匹配的一律 E3001，且**不携带异常文本**。"""
    if isinstance(exc, AppError):
        return exc
    # ★ UnknownRootError 是请求参数错误，必须 400（审计 F7）。
    # 在 EXC_MAP 之前判，因为它在概念上不属于"运行时故障"那一张表；
    # 也因为它继承 ConfigError（普通 Exception），落不到任何 OSError 分支。
    # 惰性 import 避免 config ↔ errors 的循环导入（config 不 import errors）。
    try:
        from .config import UnknownRootError
    except Exception:  # noqa: BLE001 —— 配置模块缺失时退化为原行为
        UnknownRootError = ()  # type: ignore[assignment,misc]
    if isinstance(exc, UnknownRootError):
        return BadRequest("媒体根目录不存在")
    for typ, spec in EXC_MAP:
        if isinstance(exc, typ):
            # 注意：这里传 detail=None 而不是 str(exc)。str(OSError) 在 Windows 上
            # 会是 "[WinError 3] 系统找不到指定的路径: 'E:\\...'"，含绝对路径。
            return AppError(spec, None)
    return Internal(None)


__all__ = [
    "AppError", "ArchiveError", "BadRequest", "Conflict", "ErrorSpec",
    "Forbidden", "Internal", "NotFound", "ReadonlyError", "SPECS",
    "SandboxError", "Unauthorized", "UploadError",
    "find_leak", "sanitize", "to_app_error",
    *(s.code for s in SPECS.values()),
]
