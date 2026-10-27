"""审计日志 —— 谁在什么时候对哪个文件做了什么。

## 为什么要有这个模块

设计（`docs/02-概要设计说明书.md` §3 模块清单、`docs/03-详细设计说明书.md`
§6.2.2）要求审计日志有**一个**实现：`backend/app/audit.py`。实际落地时
写成了三份 —— `handlers/auth.py`、`handlers/browse.py`、`uploads.py`
各有一份 `_audit()`，而且三份**互不一致**。实测出的后果：

| 缺陷 | 证据 |
|---|---|
| 只有 `uploads._audit` 做 `flush()` + `os.fsync()` | 另两份的 docstring 写着"追加写，即使进程被杀也已落盘"，代码只有 `with open(...).write()` —— 进程被强杀确实能幸存（`with` 退出时关闭文件，把 Python 缓冲区交给了内核），但**断电/系统崩溃会丢**，因为字节还在页缓存里 |
| 只有 `auth._audit` 写 `audit_log` 表 | 真实库 `audit_log` 表 56 行**全是 `pair`**；而 `audit.log` 文件里有 `pair` 16 + `rescan` 2 + `delete` 1。**删除与重扫从未进过索引表** |
| `auth._audit` 把 `client_id` 塞进 `rel_path` 列 | `_audit(st, "pair", client_id, {...})` —— 于是 `pair` 行的"路径"列里是一个 32 位十六进制串 |
| `uploads._audit` 的设备名列写空串 | 同一列在三份实现里分别写设备名 / 空串 |
| `audit_log.client_id` 永远是 NULL | 三份实现都写死 `NULL`，而 `handlers/auth.py` 的注释却说"删行会让 `audit_log.client_id` 变成悬空引用" |

这些缺陷**不会报错**，只会让审计记录悄悄变得不完整 —— 而审计日志的
全部价值就在于"出事时它是可信的"。所以这里把三份合并成一份。

## 文件是主记录，表是索引

`data/audit.log` 是**主记录**：追加写、`fsync`、不轮转、不自动删除。
`audit_log` 表只是索引（`docs/05-数据库设计文档.md` §4.2）。

顺序不能反：**先写文件并 fsync，再写表**。写表失败只记日志不报错 ——
为了写审计而让用户的删除操作失败是荒谬的。反过来若先写表、文件写失败，
用户会以为"有记录"，而唯一能在资源管理器之外证明他做过什么的文件却不存在。

## 为什么 fsync 不能省

`f.write()` 只是把字节交给 Python 缓冲区。`with` 退出时关闭文件，
缓冲区交给**操作系统页缓存** —— 此时进程被强杀这一行**还在**
（实测：`with open(...).write()` 之后 `os._exit(7)`，45 字节的
记录确实幸存）。但页缓存里的字节**遇到断电或系统崩溃就没了**。

`os.fsync()` 才真正落到盘上。代价是每次审计一次磁盘同步
（机械硬盘约 10 ms，SSD 亚毫秒）。审计只在删除/上传/重扫/配对时触发，
这些都不是高频路径，这个代价买的是"记录不会说谎" ——
而设计文档把审计日志定义为"用户能证明自己没误删"的**唯一凭据**
（`docs/03-详细设计说明书.md` §6.2.2），凭据经不起"可能没落盘"。

⚠️ 不要把这里的理由说成"不 fsync 进程被杀就丢记录" —— 实测不成立
（见上）。真正的理由是断电/崩溃，以及"确定性"：`os.write` + `os.fsync`
的落盘时机是确定的，文本模式缓冲的时机不是。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Mapping

if TYPE_CHECKING:  # pragma: no cover
    from .config import Config
    from .db import Database

log = logging.getLogger(__name__)

__all__ = [
    "ACTIONS",
    "ACTION_CONFIG",
    "ACTION_DELETE",
    "ACTION_GUEST_DENIED",
    "ACTION_GUEST_ENTER",
    "ACTION_GUEST_INVITE",
    "ACTION_GUEST_REVOKE",
    "ACTION_GUEST_UPLOAD",
    "ACTION_MOVE",
    "ACTION_PAIR",
    "ACTION_RENAME",
    "ACTION_RESCAN",
    "ACTION_REVOKE",
    "ACTION_UPLOAD",
    "ACTION_UPLOAD_REJECT",
    "record",
    "read_entries",
    "format_line",
    "parse_line",
    "safe_field",
]

# --------------------------------------------------------------------------
# 动作枚举
# --------------------------------------------------------------------------

ACTION_UPLOAD: Final = "upload"
ACTION_DELETE: Final = "delete"
ACTION_MOVE: Final = "move"
ACTION_RENAME: Final = "rename"
ACTION_RESCAN: Final = "rescan"
ACTION_CONFIG: Final = "config"
ACTION_PAIR: Final = "pair"
ACTION_REVOKE: Final = "revoke"
ACTION_UPLOAD_REJECT: Final = "upload_reject"

#: 游客访问（ISSUE-063）。六类事件在库里收敛成五个动作：
#: 生成链接 / 兑换链接 / 游客上传 / 撤销（含踢出）/ 越权被拒。
#: 只在 `detail` 里区分"撤销邀请"与"踢出会话"，因为两者对审计读者的意义
#: 完全一样（某个游客的访问被终止了），多一个动作只会多一处要同步的 CHECK。
ACTION_GUEST_INVITE: Final = "guest_invite"
ACTION_GUEST_ENTER: Final = "guest_enter"
ACTION_GUEST_UPLOAD: Final = "guest_upload"
ACTION_GUEST_REVOKE: Final = "guest_revoke"
ACTION_GUEST_DENIED: Final = "guest_denied"

#: 必须与 `db.py` 的 `audit_log.action` CHECK 约束**逐字一致**。
#: 往表里写一个 CHECK 不接受的动作会抛 IntegrityError，
#: 而写表失败是被容忍的（只记日志）—— 于是记录静默消失。
#: `test_audit.py` 里有一条用例直接读 `sqlite_master` 比对这两者。
ACTIONS: Final[frozenset[str]] = frozenset({
    ACTION_UPLOAD,
    ACTION_DELETE,
    ACTION_MOVE,
    ACTION_RENAME,
    ACTION_RESCAN,
    ACTION_CONFIG,
    ACTION_PAIR,
    ACTION_REVOKE,
    ACTION_UPLOAD_REJECT,
    ACTION_GUEST_INVITE,
    ACTION_GUEST_ENTER,
    ACTION_GUEST_UPLOAD,
    ACTION_GUEST_REVOKE,
    ACTION_GUEST_DENIED,
})

#: 文件与表都是多线程可达的：handlers 在事件循环上跑，
#: `uploads.complete_session` 在线程池里跑。Windows 上 `O_APPEND`
#: 的原子性弱于 POSIX（缓冲写入可能被拆成多次 `WriteFile`），
#: 所以这里显式串行化，让一行的字节不会被另一行插进去。
_file_lock = threading.Lock()


def safe_field(value: Any) -> str:
    """把任意值压成**单行、不含制表符**的字段。

    日志格式是 TAB 分隔的五列。制表符与换行会把一行拆成两行、
    或把一列挤成两列，于是**整份日志从此错位**，而错位之后
    每一行的列含义都不再可信。

    这不是理论风险：设备名来自手机上报的 User-Agent
    （`handlers/auth.py` 的 `_client_name()`），客户端可以把它设成任何字符串。
    """
    text = "" if value is None else str(value)
    if not text:
        return ""
    for ch in ("\t", "\n", "\r"):
        if ch in text:
            text = text.replace(ch, " ")
    return text


def format_line(
    action: str,
    *,
    rel_path: str = "",
    device: str = "",
    detail: Mapping[str, Any] | None = None,
    ts: float | None = None,
) -> str:
    """拼一行审计记录（不含结尾换行）。

    列序由 `docs/05-数据库设计文档.md` §4.2 固定：
    `ISO8601时间 \\t 动作 \\t 来源 \\t 路径 \\t JSON详情`。

    "来源"是**设备名**（人可读，用来回答"哪台手机干的"），
    不是 `client_id`（32 位十六进制，人眼没法核对）。
    `client_id` 进 `detail`，同时进索引表的 `client_id` 列。
    """
    when = time.localtime(ts) if ts is not None else time.localtime()
    return "\t".join([
        time.strftime("%Y-%m-%dT%H:%M:%S%z", when),
        safe_field(action),
        safe_field(device),
        safe_field(rel_path),
        json.dumps(dict(detail or {}), ensure_ascii=False),
    ])


def parse_line(line: str) -> dict[str, str]:
    """解析一行审计记录。

    列数不足时**按位补齐空串**而不是丢弃整行：老版本的记录、
    或用户手工编辑过的文件，仍然值得显示出来。丢行会让
    "日志里少了一条"与"这条从来没发生"无法区分。
    """
    parts = line.split("\t")
    while len(parts) < 5:
        parts.append("")
    return {
        "ts": parts[0],
        "action": parts[1],
        "device": parts[2],
        "path": parts[3],
        "detail": parts[4],
    }


# --------------------------------------------------------------------------
# 写入
# --------------------------------------------------------------------------

def record(
    db: "Database",
    cfg: "Config",
    action: str,
    *,
    rel_path: str = "",
    device: str = "",
    client_id: str | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    """写一条审计记录：**先文件（并 fsync），再索引表**。

    `db` 可以为 `None`（极少数没有库的路径，例如启动早期的失败记录）——
    此时只写文件。文件是主记录，它必须尽量写成。

    本函数**不抛异常**。审计失败不该让用户的删除/上传失败；
    但也**绝不静默**：文件写失败记 WARNING，表写失败记 DEBUG
    （表是索引，丢了能靠文件重建；文件丢了才是真的没了）。
    """
    payload = dict(detail or {})
    if client_id:
        # 索引表有 client_id 列，但文件只有五列。放进 detail
        # 才不会在"主记录"里丢掉这个关联键。
        payload.setdefault("client_id", client_id)

    line = format_line(action, rel_path=rel_path, device=device, detail=payload)

    _append_file(cfg, line, action=action)

    if db is None:
        return
    if action not in ACTIONS:
        # 写进表必然被 CHECK 拒绝（然后被 except 吞掉），
        # 与其静默失败不如在日志里点名 —— 这是代码 bug，不是运行时状况。
        log.warning("审计动作 %r 不在允许集合内，已只写入文件", action)
        return
    try:
        db.execute(
            "INSERT INTO audit_log"
            "(ts, device_name, client_id, action, rel_path, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), safe_field(device) or None, client_id,
             action, safe_field(rel_path),
             json.dumps(payload, ensure_ascii=False)),
        )
    except Exception:  # noqa: BLE001 —— 索引写失败不能影响业务
        log.debug("写审计索引表失败 action=%s", action, exc_info=True)


def _append_file(cfg: "Config", line: str, *, action: str) -> None:
    """追加一行到 `data/audit.log` 并 fsync。

    用 `os.open` + `os.write` 而不是 `open(..., "a")` 的文本模式：
    文本模式的缓冲让我们无法确定字节何时真正到达内核，
    而 `os.write` + `os.fsync` 的落盘时机是确定的。
    """
    try:
        path = cfg.audit_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (line + "\n").encode("utf-8")
        with _file_lock:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError:
        # 磁盘满、权限、文件被独占……都只记日志。
        # 用户的删除操作已经完成，不能因为写不了审计而回滚。
        log.warning("写审计日志文件失败 action=%s", action, exc_info=True)


# --------------------------------------------------------------------------
# 读取（托盘面板）
# --------------------------------------------------------------------------

def read_entries(cfg: "Config", *, limit: int = 100) -> tuple[list[dict[str, str]], bool]:
    """读审计日志尾部，最新的在最前面。

    返回 `(entries, truncated)`。**读文件而不是读表**：表只是索引，
    而且历史上有过"删除/重扫从未进表"的时期 —— 排查"照片怎么没了"
    时必须看最原始的那份。

    从文件尾部按字节读，避免把几十 MB 的日志整个读进内存。
    每行约 120 字节，`limit` 行取 400 倍字节数留足余量。
    """
    path = cfg.audit_log_path
    if not path.exists():
        return [], False

    raw = _tail(path, max_bytes=max(8192, limit * 400))
    lines = [ln for ln in raw.splitlines() if ln.strip()]
    entries = [parse_line(ln) for ln in lines[-limit:]]
    entries.reverse()   # 最新的在最上面
    return entries, len(lines) > limit


def _tail(path: Path, *, max_bytes: int) -> str:
    """读文件尾部若干字节，并按 UTF-8 容错解码。

    ⚠️ 从中间截断可能切断一个多字节汉字，直接 decode 会抛
    UnicodeDecodeError 让整个面板 500。用 `errors="replace"` 兜住。
    """
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
    except OSError as e:
        log.warning("读取审计日志失败：%s", e)
        return ""
    text = data.decode("utf-8", "replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]   # 丢掉可能残缺的首行
    return text
