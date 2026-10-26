"""公网 TLS 配置的静态契约（审查 Q-03 的"公网 TLS/ACL"一项）。

## 这一项为什么只能测"配置"而不能测"证书"

Q-03 要的三件事里，TLS 是唯一**当前无法真上线**的：域名 `photobridge.xyz`
仍处于 `clientHold`（未实名/未备案，见 docs/11 §8.40.3），阿里云大陆机上的
80/443 还要过 ICP 备案检查。没有可解析域名，Caddy 就签不出证书 —— 任何
"HTTPS 通过"的结论都会是假的。

能做且该做的：把**上线前必须成立的那几条**钉成用例，避免有人为了"先跑起来"
把自动 HTTPS 关掉、把站点改成裸 IP、或者把 XFF 从可伪造的来源放行 —— 这三种
改法都会让"以后一开 TLS 就安全了"变成错觉。

## 钉住的四条

1. **站点地址必须是主机名**（裸 IP 不会被 ACME 签发，`http://` 同理）；
2. **不许 `auto_https off`**、不许 `tls internal`（自签会让手机报证书错误）；
3. **X-Forwarded-For 只能由 Caddy 用 `{remote_host}` 写上**，不能透传客户端自带
   的头（`header_up X-Forwarded-For {http.request.header.X-Forwarded-For}` 是
   典型的"看起来配了其实能伪造"）；
4. **隧道入口必须在前**（`handle /agent/ws` 先于兜底 `handle`），否则 WebSocket
   升级会被兜底 handler 吃掉（表现为"手机配对成功但一直离线"）。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCKER = REPO / "cloud" / "Caddyfile"
WINDOWS = REPO / "cloud" / "Caddyfile.windows-domain"

# 站点地址可能是 `example.com`、`:443`，也可能是 `{$DOMAIN}` 这样的环境变量
# 占位（compose 里注入）。缩进的 `handle ... {` 不算站点块（行首有空格），
# 全局选项块是**单独一行**的 `{`，不匹配下面这个需要非空地址的正则。
SITE_RE = re.compile(r"^(?P<addr>\S+)\s*\{", re.MULTILINE)


def _site_addresses(text: str) -> list[str]:
    """取出站点块地址行（排除全局 `{ ... }` 选项块）。"""
    out = []
    for m in SITE_RE.finditer(text):
        addr = m.group("addr").strip()
        if addr == "{":            # 全局选项块
            continue
        out.append(addr)
    return out


def test_both_configs_bind_a_hostname_not_a_bare_ip() -> None:
    for path in (DOCKER, WINDOWS):
        text = path.read_text(encoding="utf-8")
        addrs = _site_addresses(text)
        assert addrs, f"{path.name} 里找不到站点块"
        for addr in addrs:
            assert not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", addr), (
                f"{path.name} 用裸 IP 做站点地址（{addr}）：ACME 不会给它签证书")
            assert not addr.startswith("http://"), (
                f"{path.name} 把站点写成了 http://（{addr}）：HTTPS 被显式关掉")
            assert addr.startswith(":") is False, (
                f"{path.name} 的站点地址缺主机名（{addr}）")


def test_automatic_https_is_never_disabled() -> None:
    for path in (DOCKER, WINDOWS):
        code = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.strip().startswith("#"))
        assert "auto_https off" not in code, f"{path.name} 关掉了自动 HTTPS"
        assert "tls internal" not in code, (
            f"{path.name} 用了自签证书：手机端会报证书错误，不能当“上线”用")


def test_forwarded_for_is_set_by_the_proxy_not_passed_through() -> None:
    text = WINDOWS.read_text(encoding="utf-8")
    assert "header_up X-Forwarded-For {remote_host}" in text
    assert "X-Forwarded-For {http.request.header" not in text, (
        "透传客户端自带的 XFF = 任何人可伪造来源 IP（限流与审计全部失效）")


def test_tunnel_upgrade_route_comes_before_the_catch_all() -> None:
    text = WINDOWS.read_text(encoding="utf-8")
    ws = text.find("handle /agent/ws")
    catch = text.find("\n\thandle {")
    assert ws > 0 and catch > 0
    assert ws < catch, "Caddy 的 handle 是互斥匹配、先命中先生效：WS 入口必须在前"


def test_domain_docs_spell_out_what_blocks_a_real_certificate() -> None:
    """文档必须写清"为什么现在签不出证书"，否则下次又被当成配置问题。"""
    doc = (REPO / "docs" / "08-部署与运维手册.md")
    text = doc.read_text(encoding="utf-8") if doc.exists() else ""
    assert text, "缺少 docs/08 部署手册"
    assert "备案" in text or "ICP" in text, "部署手册没提大陆机上的备案前置条件"
