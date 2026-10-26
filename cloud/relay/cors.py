"""云端 `/d/**` 的跨源白名单。

## 为什么云端**确实**需要 CORS

`cloud/relay/main.py` 的模块文档原来写着"云端与 PWA 同源，跨源请求一个都
不需要"。那句话在"用户永远从云端打开页面"这个前提下成立，但真实使用
路径有两个源：

| 页面从哪来 | 调云端的请求 | 是否跨源 |
|---|---|---|
| `https://域名/d/<id>/` | `https://域名/d/<id>/api/**` | 同源 |
| `http://192.0.2.10:8787/` | `https://域名/d/<id>/api/**` | **跨源** |

第二行是**最常见**的那一种：用户在家扫码，页面由电脑端伺服（局域网地址），
写进 `pb.cloud` 的云端基址是另一个源。他带着这个已经打开的页面出门，
局域网探活失败，双通路状态机回退到云端 —— 此时每一个请求都是跨源
`fetch`。没有 CORS 头，浏览器会在**响应到达 JS 之前**就把它丢掉，前端
只看到 `TypeError: Failed to fetch`，于是状态机判云端也不可用、进
`OFFLINE`，而隧道其实完全正常。

## 为什么是"局域网源"而不是配置里的固定域名

因为局域网地址是 **DHCP 分的**：`192.0.2.10` 明天可能是
`192.168.0.117`。要求用户把地址填进云端配置既做不到（他不知道自己的
手机明天从哪个 IP 打开页面），也会在换路由器后静默失效。

所以判据是"**这个 Origin 的主机是不是私有/回环地址**"，而不是精确匹配。
这个放宽的代价必须说清楚：

* 攻击者要利用它，必须让自己的页面跑在受害者局域网内的某个私有地址上
  （`192.168/16`、`10/8`、`172.16/12`、回环）。而"局域网内能被投毒的
  页面"这个前提一旦成立，攻击者本来就离受害者很近，能做的事远多于
  读一次 API 响应。
* **仍然必须拿到令牌**。CORS 只决定"JS 能不能读响应"，不提供凭证。
  云端同时**不发** `Access-Control-Allow-Credentials`（见
  `cors_headers` 的注释），所以攻击者页面既读不到 `localStorage` 里的
  令牌，也借用不到用户的 `pb_token` cookie。
* 公网源（`https://evil.com`）**一个都进不来** —— 这条是本模块的主要
  目的，也是一期审计 F1 的教训：宁可漏掉一个合法源（表现为"回退到云端
  失败，需要重新打开页面"），也不能回显任意 `Origin`。

## 为什么不用 `starlette.middleware.cors.CORSMiddleware`

它接受 `allow_origins` 列表或 `"*"`，没有"按 IP 段判断"的口子；而
`allow_origin_regex=".*"` 正是被否掉的那种写法。自己写 40 行比把
`CORSMiddleware` 的语义掰弯更好解释，也更好测（这里全是纯函数）。
"""
from __future__ import annotations

import ipaddress
import re
from typing import Iterable
from urllib.parse import urlsplit

__all__ = [
    "ALLOWED_REQUEST_HEADERS",
    "EXPOSED_RESPONSE_HEADERS",
    "PREFLIGHT_MAX_AGE",
    "is_lan_origin",
    "cors_headers",
]

#: 允许跨源请求携带的头。
#:
#: 必须是显式清单：`Authorization` 是主令牌通道，`X-PB-Client` 进审计日志，
#: `If-None-Match`/`Range` 是缩略图与原图的缓存/续传协商。少写一个的症状
#: 是"预检通过、真实请求被浏览器拦掉"（头不在允许列表里），在 DevTools
#: 里只显示一条 `Failed to fetch`，极难定位。
ALLOWED_REQUEST_HEADERS: tuple[str, ...] = (
    "authorization",
    "x-pb-client",
    "x-request-id",
    "content-type",
    "if-none-match",
    "if-modified-since",
    "range",
    # ★ 分片上传会带 `Content-Range`（`frontend/src/api/uploader.ts` 发，
    #   见审查 R-M5-02）。少写一个的症状是预检通过、真实请求被浏览器拦掉，
    #   DevTools 里只有一句 `Failed to fetch`。
    "content-range",
)

#: 允许 JS **读取**的响应头。
#:
#: 默认情况下跨源响应除少数几个"简单响应头"外都不可读，`ETag` 与
#: `Content-Range` 都在不可读之列。不暴露它们，缩略图的条件请求每次都会
#: 拿全量（304 协商失效），`Retry-After` 也读不到，前端无法按服务端要求
#: 退避。
EXPOSED_RESPONSE_HEADERS: tuple[str, ...] = (
    "etag",
    "content-range",
    "accept-ranges",
    "retry-after",
    "x-pb-tunnel",
    "x-pb-device-online",
    # 断点续传纠偏靠它：手机要知道"服务端已经有 N 字节"（416 响应头）。
    "x-upload-offset",
)

#: 预检结果缓存秒数。1 小时：通路上偶尔才跨源，缓存久一点省掉往返。
PREFLIGHT_MAX_AGE = 3600

#: scheme//host:port 的形状。只用来**解析**，安全判断交给 `ipaddress`。
#:
#: ★ `host` 必须先匹配方括号包裹的 IPv6 再退回普通形式。只写
#: `[^/?#:]+` 的话 `http://[::1]:8787` 解析不出来（方括号里的冒号会被
#: 当成端口分隔符），于是**回环 IPv6 源全部被判成非局域网源** —— 用户在
#: 支持 IPv6 的局域网里打开页面，每一个请求都会被浏览器丢掉，而服务端
#: 日志干净得像什么都没发生。
_ORIGIN_RE = re.compile(
    r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://"
    r"(?P<host>\[[0-9A-Fa-f:.]+\]|[^/?#:]+)"
    r"(?::(?P<port>\d{1,5}))?$")


def is_lan_origin(origin: str) -> bool:
    """`origin` 是否是一个**局域网/回环**来源。

    判据只看主机是不是私有地址，端口与协议不参与判断 —— 同一台电脑可能
    用 `8787`、也可能是开发期的 `5173`（`vite dev`），把端口写死只会让
    开发时莫名其妙连不上。

    `localhost` 单独认：它不是 IP 字面量，但浏览器确实会发
    `Origin: http://localhost:5173`，而它解析成 `127.0.0.1`/`::1`。
    """
    raw = (origin or "").strip()
    if not raw or raw == "null":
        # `Origin: null` 来自沙箱 iframe / `file://` / 跨源重定向后的隐私
        # 上下文。它无法被归属到任何可信主体，一律拒。
        return False
    m = _ORIGIN_RE.match(raw)
    if m is None:
        return False
    host = m.group("host").strip().strip("[]").lower()
    if not host:
        return False
    if host == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # 普通域名（包括 `evil.com`、以及 `192.168.0.1.evil.com` 这种
        # 看着像 IP 的域名）一律不进白名单。**这一步是防绕过的关键**：
        # 如果这里换成 `host.startswith("192.168.")`，那么
        # `192.168.1.1.evil.com` 会被判成局域网源。
        return False
    if ip.is_loopback:
        return True
    # `is_private` 覆盖 10/8、172.16/12、192.168/16、fc00::/7、
    # 169.254/16（链路本地）与 ::1。刻意**不**包含公网地址。
    return ip.is_private or ip.is_link_local


def cors_headers(origin: str, *,
                 allowed: Iterable[str] = ()) -> dict[str, str]:
    """该给这个 `Origin` 回哪些 CORS 头；不该回时返回空字典。

    `allowed` 是配置里显式列出的额外来源（`PB_CLOUD_CORS_ORIGINS`），
    用于一些非私有地址的部署（比如 Cloudflare Tunnel 把电脑端挂在
    `*.example.com` 下、而页面也从那里加载）。空集是默认值 —— 与一期
    `Router.enable_cors()` 的"空白名单 = 完全不启用"保持一致。

    ★ **不发 `Access-Control-Allow-Credentials`**。发了之后浏览器会允许
    跨源请求携带 `pb_token` cookie，于是"局域网里任何一个能投毒的页面"
    就真的能带着用户的 cookie 读相册了 —— 而不发的时候，跨源请求默认
    `credentials: omit`，攻击者手里必须有令牌本身才有戏。前端的
    `client.ts` 也是显式 `credentials: "omit"`，两边一致。
    """
    raw = (origin or "").strip()
    if not raw:
        return {}
    if raw not in allowed and not is_lan_origin(raw):
        return {}
    return {
        "access-control-allow-origin": raw,
        # 每次响应都可以不同（取决于 Origin），所以必须声明。
        # 少了它，中间任何一层共享缓存都可能把给 A 源的响应交给 B 源。
        "vary": "Origin",
        "access-control-expose-headers": ", ".join(EXPOSED_RESPONSE_HEADERS),
    }


def preflight_headers(origin: str, *,
                      allowed: Iterable[str] = ()) -> dict[str, str]:
    """预检（`OPTIONS` + `Access-Control-Request-Method`）要回的头。

    与 `cors_headers` 分开是因为预检**绝不能**带业务响应头：它必须被
    云端就地回 204，一个字节都不该进隧道 —— 预检不带 `Authorization`
    （浏览器规范如此），转发进去只会被电脑端判成 401，于是真实请求根本
    不会发出，前端看到的是预检失败。
    """
    base = cors_headers(origin, allowed=allowed)
    if not base:
        return {}
    base.update({
        "access-control-allow-methods": "GET, HEAD, POST, PUT, PATCH, DELETE",
        "access-control-allow-headers": ", ".join(ALLOWED_REQUEST_HEADERS),
        "access-control-max-age": str(PREFLIGHT_MAX_AGE),
    })
    return base


def is_preflight(request: object) -> bool:
    """是不是一个 CORS 预检。

    三个条件缺一不可：`OPTIONS` 方法、有 `Origin`、有
    `Access-Control-Request-Method`。只看方法会把普通的 `OPTIONS`（客户端
    查 `Allow` 头）也截下来，那是另一个语义。
    """
    headers = getattr(request, "headers", None)
    method = str(getattr(request, "method", "") or "").upper()
    if method != "OPTIONS" or headers is None:
        return False
    return bool(headers.get("origin")) and bool(
        headers.get("access-control-request-method"))


def origin_of(request: object) -> str:
    """取请求的 `Origin`；没有返回空串。"""
    headers = getattr(request, "headers", None)
    if headers is None:
        return ""
    return str(headers.get("origin") or "").strip()


def host_of(origin: str) -> str:
    """`https://a.example.com:8443` → `a.example.com`。仅用于日志。"""
    try:
        return urlsplit(origin).hostname or ""
    except ValueError:
        return ""
