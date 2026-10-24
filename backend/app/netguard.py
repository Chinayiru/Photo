"""Host 头校验：挡住 DNS rebinding，并把通配监听下"哪些 Host 算自己"集中在一处。

## 为什么必须有这一层（审计 F1-c）

`/panel/**` 原来的唯一边界是"来源 IP 是回环"，而恶意页面只要让受害者的
浏览器去访问 `http://evil.example.com:8787`（该域名解析到 `127.0.0.1`），
就能从受害者**自己的电脑**上发出请求 —— 来源 IP 是 `127.0.0.1`，
浏览器也认为这是同源（页面与 API 都是 `evil.example.com:8787`）。

Host 校验是这条路的唯一有效拦截点：浏览器在 DNS rebinding 下发出的
请求里，`Host` 头就是攻击者的域名，而服务只认自己的名字。

## 判据

允许的 Host 是"本机可能被用来访问自己的名字"：

  * `127.0.0.1` / `localhost` / `::1`（含 `:端口`）；
  * `local_ipv4_addresses()` 枚举出的真实网卡地址（已滤掉 VMware/WSL
    那些手机路由不到的虚拟网卡，见 `config.local_ipv4_addresses`）；
  * 主机名（`socket.gethostname()`），用户可能用 `DESKTOP-ABC:8787` 访问。

**端口不参与比对**：端口是我们自己监听的，攻击者无法用它绕过；
而把端口写进白名单会让 `--port 0`（测试与挂机用）与"配置改了端口
但白名单没更新"变成两类假故障。

## 什么情况下不校验

`trusted_hosts` 里显式写了 `"*"` 就不校验。这**只**给"用户明确要求把
服务放到反向代理后面，且代理改写了 Host"这种场景留出口，默认不开。

## 为什么空 Host 放行

这层防护针对的是**浏览器驱动的攻击**（DNS rebinding、跨源读响应）。
浏览器发出的每一个请求都必带 Host（RFC 7230 §5.4，HTTP/1.1 强制）；
一个能省略 Host 的客户端也能随手把 Host 写成 `127.0.0.1`，校验对它
毫无价值。而 `Ctx` 并不只由 HTTP 适配器构造 —— 二期隧道适配器、
内部 dispatch、测试夹具都会自己拼 Ctx，那里根本没有 Host 的概念。

所以语义是「带了就必须是本机；没带就不是这层防护的场景」。
这与 F11（`is_loopback("")` 返回 True 导致 `/panel` 静默放行）**不矛盾**：
那边空值是"忘了填 IP"，放进去的是浏览器请求；这边空值是"这个入口
压根没有 Host"，浏览器进不来。

★ 第一版写成"空 Host = 拒绝"，全量测试炸出 80 个失败 —— 那不是测试
写得不好，是这个语义本身错了。防护要卡在攻击真正经过的位置。
"""
from __future__ import annotations

import logging
import socket
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:      # pragma: no cover
    from .config import Config

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_TRUSTED_HOSTS",
    "TRUSTED_HOSTS_KEY",
    "host_of",
    "is_host_allowed",
    "check_host",
    "allowed_hosts",
]

#: 配置键。留空 = 用默认集合；写 `["*"]` = 不校验（仅供反向代理场景）。
TRUSTED_HOSTS_KEY: Final[str] = "server.trusted_hosts"

#: 恒定信任的名字。注意不含 `0.0.0.0` —— 它不是可访问地址，
#: 只是绑定地址，写进白名单只会让"服务到底叫什么"更难说清。
DEFAULT_TRUSTED_HOSTS: Final[frozenset[str]] = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "[::1]",
})

#: 「像本机地址」的网段。命中这些的 Host 直接放行。
#:
#: ## 为什么按网段判，而不是只认 `local_ipv4_addresses()` 枚举的结果
#:
#: `config.local_ipv4_addresses()` 的职责是回答**另一个问题**："哪个地址
#: 值得印到二维码里让手机去连"。为了不把 VMware/WSL 那类手机永远路由不到
#: 的地址显示给用户，它主动滤掉了虚拟网卡 —— 连 Radmin/Tailscale 这类
#: **用户确实要用的**组网网卡也一起滤掉了（它们的地址段不是 RFC1918）。
#:
#: 但 Host 校验要回答的是"这个 Host 是不是本机自己"：手机经 Radmin 访问
#: `26.26.192.40:8799` 时，Host 就是那个组网地址，它确实是本机地址。
#: 两个问题混用同一个枚举，结果就是首页能打开（静态资源不校验 Host）、
#: 而所有 `/api/*` 一律 404 —— 实测日志原文：
#:
#:     GET /api/system/ping 被 Host 校验拒绝：Host 头 '26.26.192.40'
#:     不属于本机（防 DNS rebinding）
#:
#: ## 为什么放宽到整个网段也不会削弱防护
#:
#: DNS rebinding 的攻击形态是"**用攻击者的域名**做 Host，让请求落到
#: 127.0.0.1"。域名永远不可能是这些网段的字面量，所以放行字面量对那条
#: 攻击路径零影响；而攻击者若直接拿 `26.1.2.3` 当 Host，他连的就是那台
#: 机器，跟我们无关。
_LOCAL_LITERAL_NETWORKS: Final[tuple] = (
    ("10.0.0.0", 8),          # RFC1918
    ("127.0.0.0", 8),         # 回环
    ("169.254.0.0", 16),      # 链路本地
    ("172.16.0.0", 12),       # RFC1918
    ("192.168.0.0", 16),      # RFC1918
    ("25.0.0.0", 8),          # Hamachi / LogMeIn
    ("26.0.0.0", 8),          # Radmin VPN
    ("100.64.0.0", 10),       # CGNAT：Tailscale / ZeroTier 常用
)


def _is_local_literal(host: str) -> bool:
    """Host 是不是「本机地址」的字面量（IPv4 私网/组网段，或 IPv6 回环/ULA）。"""
    h = (host or "").strip().strip("[]").lower()
    if not h:
        return False
    if ":" in h:                       # IPv6 字面量
        return (h == "::1"
                or h.startswith("fe80:")      # 链路本地
                or h.startswith("fc")         # ULA fc00::/7
                or h.startswith("fd"))
    parts = h.split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p, 10) for p in parts]
    except ValueError:
        return False
    if any(n < 0 or n > 255 for n in nums):
        return False
    value = (nums[0] << 24) | (nums[1] << 16) | (nums[2] << 8) | nums[3]
    for base, prefix in _LOCAL_LITERAL_NETWORKS:
        b = [int(x) for x in base.split(".")]
        base_value = (b[0] << 24) | (b[1] << 16) | (b[2] << 8) | b[3]
        mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF
        if (value & mask) == (base_value & mask):
            return True
    return False


def host_of(raw: str | None) -> str:
    """从 Host 头里取出主机名，丢掉端口与 IPv6 方括号。

    `"192.0.2.10:8787"` → `"192.0.2.10"`
    `"[::1]:8787"` → `"::1"`
    `"LOCALHOST"` → `"localhost"`

    ★ 不用 `urllib.parse.urlsplit("//" + raw)`：它在 Host 形如
    `"[::1"`（缺右括号）或含用户信息时会抛异常或把 `user@host`
    当成主机名的一部分，而这正是攻击者会送到校验器手里的字符串。
    这里逐字符处理，任何异常形态都退化成"原样返回、比对必然失败"。
    """
    h = (raw or "").strip().lower()
    if not h:
        return ""
    if h.startswith("["):
        end = h.find("]")
        if end > 0:
            return h[1:end]
        # 缺右括号：不是合法 Host，原样返回让它匹配不上任何白名单项。
        return h
    if ":" in h:
        # 只在**唯一的**冒号处切 —— 裸 IPv6（`::1`）没有端口，
        # 直接切会把 `::1` 切成空串。
        if h.count(":") == 1:
            return h.split(":", 1)[0]
        return h
    return h


def _machine_names(cfg: "Config | None") -> set[str]:
    """本机可能被用来访问自己的名字（不含回环与默认集合）。

    每次都现算而不是缓存：DHCP 换 IP、插拔网卡都不该要求重启服务
    —— 缓存会让"手机突然连不上"变成必须重启才能修的问题。
    代价是每个请求一次 getaddrinfo，实测 <0.1ms，且只在 Host 校验
    这一处发生。
    """
    names: set[str] = set()
    try:
        from .config import local_ipv4_addresses

        names.update(a.lower() for a in local_ipv4_addresses())
    except Exception:  # noqa: BLE001 —— 拿不到网卡不该让请求 500
        log.debug("枚举本机 IPv4 失败，Host 白名单退化为回环", exc_info=True)
    try:
        h = socket.gethostname()
        if h:
            names.add(h.lower())
            # 某些系统上 gethostname() 返回短名，Host 头带的是 FQDN。
            # 用 fqdn 兜一下，失败就算了。
            fq = socket.getfqdn()
            if fq:
                names.add(fq.lower())
    except OSError:
        pass
    if cfg is not None:
        try:
            port = int(cfg.get("server.port", 0) or 0)
        except (TypeError, ValueError):
            port = 0
        # 用户可能把服务挂在反向代理后面，代理会用上游名做 Host。
        for extra in cfg.get(TRUSTED_HOSTS_KEY, ()) or ():
            s = str(extra).strip().lower()
            if s and s != "*":
                names.add(host_of(s))
        if port:
            pass        # 端口不参与比对，见模块 docstring
    names.discard("")
    return names


def allowed_hosts(cfg: "Config | None" = None) -> frozenset[str]:
    """当前允许的全部 Host（已小写、无端口）。"""
    return frozenset(DEFAULT_TRUSTED_HOSTS | _machine_names(cfg))


def is_host_allowed(raw: str | None, cfg: "Config | None" = None) -> bool:
    """Host 头是否属于本机。空/缺失一律 False。"""
    configured = ()
    if cfg is not None:
        try:
            configured = tuple(cfg.get(TRUSTED_HOSTS_KEY, ()) or ())
        except Exception:  # noqa: BLE001
            configured = ()
    if any(str(x).strip() == "*" for x in configured):
        return True
    h = host_of(raw)
    if not h:
        return False
    if h in allowed_hosts(cfg):
        return True
    # 私网/组网网段的字面量：手机经 Radmin/Tailscale 直连时 Host 就是它。
    return _is_local_literal(h)


def check_host(raw: str | None, cfg: "Config | None" = None) -> str | None:
    """校验 Host。通过（或没有 Host 可校验）返回 None，不通过返回日志文案。"""
    if not (raw or "").strip():
        # 没有 Host 头：不是浏览器能发出来的请求，本层防护不适用。
        return None
    if is_host_allowed(raw, cfg):
        return None
    h = host_of(raw) or "<无法解析>"
    return f"Host 头 {h!r} 不属于本机（防 DNS rebinding）"
