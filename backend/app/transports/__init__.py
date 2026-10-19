"""传输层：把 Router 暴露给手机的不同方式。

一期只实现 `local_http`（局域网直连，手机连同一 Wi-Fi 即可用，不需要 VPS）。
二期加 `cloud_tunnel`（电脑主动外连云端，手机在任何网络都能访问）。

新增一条通路**不需要改业务代码** —— 只需实现 `base.ServerTransport`，
再用 `register()` 登记一个名字。

## 为什么要有 `build()` 这层间接

`server.py` 原本直接 `LocalHttpTransport(cfg, router)`。那样虽然业务代码
确实与通路无关，但**"换一条通路"仍然要改 server.py**，而 `config.json`
里那个 `server.transport` 字段从头到尾没有任何人读 —— 它是个谎言：
用户改成 `"relay"` 什么也不会发生，程序照旧只监听局域网。

现在 `server.py` 只调 `build(cfg, router)`，由配置决定实例化哪一个。
二期的全部接入工作因此收敛为两件事：

1. 写 `cloud_tunnel.py`，实现 `ServerTransport` 的四个成员；
2. 在文件末尾 `register("cloud_tunnel", CloudTunnelTransport)`。

**不需要动 server.py、不需要动任何 handler、不需要动前端。**
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Final

from .base import (
    DEFAULT_MIME,
    EXTRA_MIME,
    RangeNotSatisfiable,
    ServerTransport,
    content_range_header,
    content_type_for,
    parse_range,
    range_length,
    unsatisfied_range_header,
)

__all__ = [
    "DEFAULT_MIME",
    "EXTRA_MIME",
    "RangeNotSatisfiable",
    "ServerTransport",
    "content_range_header",
    "content_type_for",
    "parse_range",
    "range_length",
    "unsatisfied_range_header",
    "build",
    "register",
    "available",
    "DEFAULT_TRANSPORT",
    "UnknownTransport",
]

log = logging.getLogger(__name__)

DEFAULT_TRANSPORT: Final[str] = "local_http"

#: 名字 → 构造函数 `(cfg, router) -> ServerTransport`。
# 用工厂注册表而不是 `if name == "local_http": ... else: ...`：
# 后者每加一条通路都要回来改这个文件，而这里只需要新模块自己登记。
_FACTORIES: dict[str, Callable[[Any, Any], ServerTransport]] = {}


class UnknownTransport(Exception):
    """配置里写了未注册的通路名。

    这是**配置错误**，不是运行时故障 —— 必须让用户看到原文与可选值，
    所以它跟 `ConfigError` 一样不进 API 脱敏路径，直接冒泡到启动日志。
    """

    def __init__(self, name: str, known: list[str]) -> None:
        self.name = name
        self.known = known
        super().__init__(
            f"未知的传输方式 {name!r}；可用：{', '.join(known) or '（无）'}")


def register(name: str, factory: Callable[[Any, Any], ServerTransport]) -> None:
    """登记一条通路。重复登记同一个名字会覆盖（便于测试注入替身）。"""
    _FACTORIES[name] = factory


def available() -> list[str]:
    """已登记的通路名，排序后返回（用于报错提示与自检）。"""
    return sorted(_FACTORIES)


def build(cfg: Any, router: Any) -> ServerTransport:
    """按 `server.transport` 造出对应的通路实例。

    配置缺失或为空时用 `DEFAULT_TRANSPORT`；名字不认识时抛
    `UnknownTransport` 并列出可选值 —— 静默退回局域网会让用户以为
    自己配的云端通路生效了，然后在外面怎么都连不上。
    """
    raw = cfg.get("server.transport", DEFAULT_TRANSPORT)
    name = str(raw or DEFAULT_TRANSPORT).strip() or DEFAULT_TRANSPORT
    factory = _FACTORIES.get(name)
    if factory is None:
        raise UnknownTransport(name, available())
    return factory(cfg, router)


def _register_builtin() -> None:
    """登记自带的通路。

    放在函数里而不是模块顶层，是为了让 `register()` 的调用点集中可见，
    同时避免 `import .local_http` 与 `local_http` 反过来 `import .base`
    形成循环。

    `cloud_tunnel` 的 import 也在这里：它内部 `from . import register`，
    而此刻本模块已经把 `register` 定义好了，所以不会形成循环。若改成
    在 `cloud_tunnel.py` 顶层 `from . import register` 而本模块顶层
    `import .cloud_tunnel`，两者就会互等到 `ImportError`。
    """
    from .cloud_tunnel import _register as _register_cloud
    from .local_http import LocalHttpTransport

    register("local_http", LocalHttpTransport)
    _register_cloud()


_register_builtin()
