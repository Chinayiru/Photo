"""安全响应头的**唯一**定义处（云端中继与电脑端共用）。

## 为什么只有这一份

2026-10-23 的安全审计发现：这些头原先分别写在 `cloud/Caddyfile`（Docker
部署）与"哪都没写"（Windows 部署）两处，于是换掉反代之后头就消失了，而
没有测试会发现。同一个页面存在**两条伺服路径**：

* 云端：`cloud/relay/pages.py`（手机走公网）
* 局域网：`backend/app/static.py`（手机与电脑同 Wi-Fi）

两条路径必须发同一套头。所以定义放 `shared/`（两个包都能 import，仓库
里 `shared/mux.py`、`shared/protocol.py` 已经是先例），伺服层只负责把
它贴到响应上。

## CSP 为什么长这样

* `default-src 'none'` 起步：没显式允许的一律不许。
* `script-src 'self'`：产物是外链 `<script type="module" src="./assets/…">`，
  页面里没有内联脚本，所以**不需要** `'unsafe-inline'`（那一项等于把
  XSS 防线拆掉一半）。
* `style-src 'self' 'unsafe-inline'`：Vite 生产产物把样式打成外链 CSS，
  但 Vue 的 `:style` 会写 `el.style`，部分浏览器把它算作内联样式。保留
  `'unsafe-inline'` 是**有意的取舍**：样式注入的危害远小于脚本注入，
  而白屏的代价是产品不可用。
* `img-src … blob: data: http://*:8787`：原图走 `URL.createObjectURL`
  （`blob:`）、占位图走 `data:`、双通路下手机可能直接从局域网
  `http://192.168.x.x:8787` 取缩略图。
* `connect-src 'self' https: http://*:8787`：页面可能由云端伺服、却要回
  局域网取图；中继域名又是运行时才知道的（quick tunnel 每次重启都变）。
  收紧到具体域名需要把域名喂进伺服层，见 `docs/11` ISSUE-034。
* `frame-ancestors 'none'` + `X-Frame-Options: DENY`：点击劫持。这个面的
  代价实测过 —— 透明 iframe 把"删除照片"盖在"领取奖品"下面，一次点击
  就是不可逆的批量删除。
* `form-action 'none'` / `base-uri 'none'`：页面不发原生表单、也不用
  `<base>`，禁掉可以拦"改 base 劫持相对路径"那一类。
"""
from __future__ import annotations

__all__ = [
    "COMMON_HEADERS",
    "PAGE_CSP",
    "PAGE_HEADERS",
    "csp_for",
    "headers_for",
]

#: 所有响应都带的头（JSON 也带：`nosniff` 对 JSON 同样有意义）。
COMMON_HEADERS: tuple[tuple[str, str], ...] = (
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("x-frame-options", "DENY"),
    ("permissions-policy",
     "camera=(), microphone=(), geolocation=(), usb=(), payment=()"),
)

#: 只给 HTML 文档的 CSP。
PAGE_CSP: str = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' blob: data: http://*:8787; "
    "media-src 'self' blob:; "
    "connect-src 'self' https: http://*:8787; "
    "font-src 'self' data:; "
    "manifest-src 'self'; "
    "frame-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "object-src 'none'"
)

#: 公共头 + CSP。伺服层可以直接 `for k, v in PAGE_HEADERS`。
PAGE_HEADERS: tuple[tuple[str, str], ...] = COMMON_HEADERS + (
    ("content-security-policy", PAGE_CSP),
)


def csp_for(*, extra_connect: list[str] | None = None,
            lan_port: int | None = None) -> str:
    """按需改写/追加来源：`lan_port` 换掉通配端口，`extra_connect` 追进 `connect-src`。

    ## 两个参数各解决什么

    * `lan_port`：`PAGE_CSP` 里写死的是 `http://*:8787`（默认端口）。电脑端
      改了 `server.port` 之后，页面从 `http://<ip>:8877` 发请求就会被
      **自己的 CSP 拦掉** —— 审计 F13 / M10-004：页面能渲染，`fetch` 全红。
      这里把通配里的端口换成真实端口，通配的范围不变（仍是"任意主机的这个
      端口"），因为局域网 IP 是运行时才知道的（`192.168.x.x`、Tailscale、
      Radmin 各一套）。
    * `extra_connect`：页面**由电脑端伺服**、却要访问云端的 `/d/<id>/` 时，
      那是另一个 origin，通配端口盖不住它。电脑端配置里本来就有
      `cloud.base_url`，所以这里可以点名（`server._page_connect_sources`）。
      只有知道确切来源的伺服层才传这个参数；中继域名是运行时的，它不传
      （见 `docs/11` ISSUE-034）。

    ★ 追加必须进 `connect-src` 里。早先的实现是 `csp + " " + add`，于是
    整串多出一个**不属于任何指令**的尾部 token，浏览器直接忽略它 ——
    看起来"改了 CSP"，实际一次都没放行。
    """
    port_ok = lan_port is not None and 1 <= int(lan_port) <= 65535
    add = [s.strip() for s in (extra_connect or []) if s and s.strip()]
    if not port_ok and not add:
        return PAGE_CSP
    port = f"http://*:{int(lan_port)}" if port_ok else "http://*:8787"

    out: list[str] = []
    for part in PAGE_CSP.split(";"):
        directive = part.strip()
        if not directive:
            continue
        name, _, rest = directive.partition(" ")
        tokens = [t for t in rest.split() if t]
        # 取图/取流也走同一个端口，通配端口一换就得跟着换。
        if name in ("img-src", "media-src", "connect-src"):
            tokens = [port if t == "http://*:8787" else t for t in tokens]
        if name == "connect-src":
            tokens += [a for a in add if a not in tokens]
        out.append(" ".join([name, *tokens]))
    return "; ".join(out)


def headers_for(content_type: str, *,
                lan_port: int | None = None,
                extra_connect: list[str] | None = None,
                ) -> tuple[tuple[str, str], ...]:
    """按响应的 `Content-Type` 选一套头。

    判断依据是**实际发出去的类型**，而不是"我们打算发什么"：`pages.py`
    在没找到 dist 时会回一段 `text/plain` 的提示，那时贴 CSP 无意义；
    反过来只要类型是 `text/html`，就必须有 CSP —— 页面里可能藏着
    `localStorage` 里的令牌，缺了 CSP 就少了一道 XSS 兜底。

    `extra_connect` 给"知道确切来源"的伺服层用：电脑端在配置里就有
    自己的端口与云端基址，因此能把 `connect-src` 收到具体 origin，
    而不必依赖 `http://*:8787` 那个通配（见 `csp_for` 的说明）。
    """
    if (content_type or "").lower().startswith("text/html"):
        if lan_port is None and not extra_connect:
            return PAGE_HEADERS
        return COMMON_HEADERS + (("content-security-policy",
                                  csp_for(extra_connect=extra_connect,
                                          lan_port=lan_port)),)
    return COMMON_HEADERS
