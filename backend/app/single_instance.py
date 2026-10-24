"""单实例守卫：同一个数据目录只允许一个进程在跑。

## 为什么需要

托盘程序没有主窗口。用户第二次双击图标时**什么都看不到**：如果端口还被
上一个实例占着，新进程会在绑定阶段报错退出（用户只看到"闪一下"）；如果
上一个实例换了端口、或绑在另一个地址上，新进程会顺利起来，于是两个进程
各自扫索引、各自跑缩略图队列、各自持有 SQLite 写锁 —— 用户看到的现象就是
"后台残留了一堆 python，托盘图标却只有一个"。

## 判定方式：锁文件 + 端口探活

不用 Windows 命名互斥体：那需要 `pywin32`（重依赖）或 ctypes 手写句柄，
而"进程还活着吗"本身可以更直接地问出来 —— 锁文件里记下端口，然后

    GET http://127.0.0.1:<port>/api/system/ping

只要它答 200 并且 `ok=true`，就说明**那个端口上确实有一个 PhotoBridge**
（`device_id` 也是同一条链路上的证据）。这比 `pid` 可靠：pid 会被复用，
而一个答得上 `/api/system/ping` 的进程不可能是别的软件。

锁文件过期（探活失败）时直接覆盖 —— 断电、强杀、崩溃都会留下过期锁，
如果沿用"锁在就是有人"，用户会遇到"程序再也起不来"。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["LOCK_NAME", "lock_path", "read_lock", "find_running", "acquire",
           "release"]

#: 锁文件名（放在 `cfg.data_dir` 下，跟 `photobridge.db` 同级）。
LOCK_NAME = "app.lock"

#: 探活超时。短 —— 本机回环，1.5 秒还没答就是没在跑。
_PROBE_TIMEOUT = 1.5


def lock_path(cfg) -> Path:
    return Path(cfg.data_dir) / LOCK_NAME


def read_lock(cfg) -> dict[str, object] | None:
    """读锁文件；畸形/缺失一律当没有。"""
    try:
        raw = lock_path(cfg).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _probe(port: int, timeout: float = _PROBE_TIMEOUT) -> dict[str, object] | None:
    """问一下这个端口上是不是 PhotoBridge。答得上就返回响应体。"""
    url = f"http://127.0.0.1:{int(port)}/api/system/ping"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                return None
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    # 这里只做"HTTP 层是否答了 200 + 是不是 JSON 对象"的判断；
    # "是不是 PhotoBridge" 交给 `find_running` —— 那一层才是判定语义所在，
    # 也才好被单元测试替换掉探活实现。
    if not isinstance(body, dict):
        return None
    return body


def find_running(cfg) -> dict[str, object] | None:
    """返回正在运行的实例信息；没有就返回 None。

    ## 端口以**当前配置**为准，不读锁文件里的端口

    这条一开始写反了，代价很直接：锁文件是上一次运行留下的（用户强杀/断电
    时不会删），里面记着老端口 8799；而 8799 上恰好坐着**另一个**配置的
    PhotoBridge（比如测试用的实例）。于是新进程探活成功，判定"已在运行"，
    安静退出 —— 用户拖了半天的"程序起不来"，根因是一个过期的锁文件。

    锁文件只提供两件不影响判定的事：pid（写进提示信息）和排查线索。
    真正决定"能不能起"的永远是**我要绑的那个端口有没有被占**。
    """
    info = read_lock(cfg) or {}
    try:
        port = int(cfg.server.get("port", 8787))
    except (TypeError, ValueError):
        return None
    body = _probe(port)
    # `/api/system/ping` 的正常响应是 `{"pong": true, ...}`；同时接受 `ok`
    # —— 别的入口（云端 `/api/health`）用的是那个键。都不认就当没在跑：
    # 端口上可能坐着别的软件，那种情况该让"端口被占"的错误自己冒出来。
    if body is None or not (body.get("pong") or body.get("ok")):
        return None
    out: dict[str, object] = {"port": port, "url": f"http://127.0.0.1:{port}"}
    pid = info.get("pid")
    if isinstance(pid, int):
        out["pid"] = pid
    did = body.get("device_id")
    if isinstance(did, str):
        out["device_id"] = did
    return out


def acquire(cfg) -> tuple[bool, dict[str, object] | None]:
    """抢锁。返回 `(是否拿到, 已在运行的实例信息)`。

    ★ 抢不到锁**不是错误**：调用方应当安静退出（退出码 0）。用户双击第二次
    期待的是"把已经开着的那个窗口叫出来"，而不是又起一个后台进程或弹错误框。

    ★ 写锁失败**不算占用**：数据目录只读（比如程序被塞进 Program Files 且
    没有权限）时，宁可允许启动，也不要让用户面对"程序打不开"。
    """
    running = find_running(cfg)
    if running is not None:
        log.info("检测到已在运行的实例：%s（pid=%s）",
                 running.get("url"), running.get("pid"))
        return False, running

    path = lock_path(cfg)
    payload = {
        "pid": os.getpid(),
        "port": int(cfg.server.get("port", 8787)),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        log.debug("写单实例锁文件失败（不阻止启动）", exc_info=True)
    return True, None


def _unlink_quietly(path: Path) -> None:
    """删一个非用户数据文件（锁文件），不存在/占用都不算错。

    走独立函数是仓库规则 2 的要求：所有真删调用点必须一眼能看出"删的不是
    用户照片"，这个名字本身就把语义写在脸上了。
    """
    try:
        path.unlink()
    except OSError:
        pass


def release(cfg) -> None:
    """退出时删锁。删不掉也无所谓：过期锁会被 `acquire` 的探活覆盖。"""
    _unlink_quietly(lock_path(cfg))
