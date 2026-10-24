"""密钥卫生与"安全头单一来源"的回归判据（2026-10-23 安全审计）。

## 为什么需要这一层

审计里最贵的三个发现都不是"代码写错了"，而是**财产放错了地方**：

* 服务器 `Administrator` 的口令明文写在 `build/_askpass.cmd` 里（那是为了
  非交互 SSH 临时造的），同一台机器的 3389 当时对全网开放 —— 拿到仓库
  目录就等于拿到服务器。
* 一个 90 天有效的客户端令牌落在 `build/_sec_token.txt` 里，又出现在一次
  端到端测试的日志里（`build/_p1_e2e.log:113`）。`build/` 刚好被
  `.gitignore` 挡住才没进 git，**这是运气，不是设计**。
* `cloud/deploy.ps1` 曾经把 `PbCloud2026!` 当 `-Password` 的默认值，而
  `backend/tests/test_cloud_deploy.py` 里有一条测试断言"默认口令必须
  存在" —— 缺陷被测试钉成了契约。

所以这里判的不是"密码强度"，而是"**这些东西不该出现在仓库里**"。

## 扫描范围

只扫 `git ls-files` 的产物（也就是真正会被推送出去的东西）。`build/`、
`.venv/`、`node_modules/`、`frontend/dist/` 里的二进制不扫。这样"本机
临时文件泄漏"由 `.gitignore` 那几条用例兜住，"已经进了 git 的泄漏"由
内容模式兜住。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

#: 这些后缀是文本，扫内容；其余（图片、字体、zip）只看文件名。
_TEXT_SUFFIXES = {
    ".py", ".ps1", ".psm1", ".cmd", ".bat", ".sh", ".md", ".txt", ".json",
    ".toml", ".ini", ".cfg", ".yml", ".yaml", ".ts", ".js", ".vue", ".css",
    ".html", ".webmanifest", ".caddyfile", "", ".example",
}

#: 单文件扫描上限：超过这个大小的基本是数据文件，不是源码。
_MAX_BYTES = 512 * 1024


def _tracked_files() -> list[str]:
    """`git ls-files`。仓库不是 git 仓库时跳过整个文件。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(REPO), capture_output=True, check=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        pytest.skip("不是 git 仓库，跳过密钥卫生扫描")
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def _text_of(rel: str) -> str | None:
    path = REPO / rel
    if not path.is_file() or path.suffix.lower() not in _TEXT_SUFFIXES:
        return None
    try:
        if path.stat().st_size > _MAX_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _matches(pattern: str, *, flags: int = 0) -> list[str]:
    """返回 `文件:行号` 形式的命中列表（只报位置，不回显内容）。"""
    rx = re.compile(pattern, flags)
    hits: list[str] = []
    for rel in _tracked_files():
        text = _text_of(rel)
        if text is None:
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{no}")
    return hits


# --------------------------------------------------------------- 不该进仓库


def test_no_real_client_token_is_committed() -> None:
    """真令牌长这样：`pbc1.<base64 载荷>.<签名>`。

    文档里允许出现**截断的**示例（`pbc1.eyJzdWI…`），所以判据是"载荷段
    长度 ≥ 20"——截断的示例过不了这条线，真令牌一定过。
    """
    hits = _matches(r"pbc1\.[A-Za-z0-9_\-]{20,}")
    assert hits == [], f"仓库里出现了完整客户端令牌：{hits}"


def test_no_private_keys_are_committed() -> None:
    hits = _matches(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
    assert hits == [], f"仓库里出现了私钥：{hits}"


def test_no_server_password_is_committed() -> None:
    """服务器口令（以及任何"给 Administrator 用"的口令）不许出现在仓库里。

    `_askpass.cmd` 那种 `echo <口令>` 是最常见的形态：为了非交互 SSH
    临时写一句，然后忘了删。
    """
    hits = _matches(r"(?i)^\s*echo\s+\S{8,}\s*\|?\s*$")
    # 只关心出现在"看起来像 askpass/ssh 脚本"里的
    suspicious = [h for h in hits
                  if any(k in h for k in ("askpass", "ssh", "vps", "deploy"))]
    assert suspicious == [], f"疑似明文口令：{suspicious}"


def test_deploy_script_does_not_carry_a_default_password() -> None:
    """`-Password` 不能有默认值：默认值等于"部署脚本本身就是凭据文件"。"""
    text = (REPO / "cloud" / "deploy.ps1").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            continue
        assert not re.match(r'^\$Password\s*=\s*["\'][^"\']+["\']', stripped), \
            f"deploy.ps1 里仍有默认口令：{stripped}"
    # 必须支持从标准输入取口令（否则运维只能把它写回命令行）
    assert "--password-stdin" in text


# --------------------------------------------------------- .gitignore 兜底


def test_gitignore_covers_secret_shaped_paths() -> None:
    """这几类路径必须被忽略 —— 它们是"临时文件放错地方"的默认落点。"""
    lines = {ln.strip() for ln in
             (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for pattern in ("*.log", "_askpass*", "*_token*.txt", ".env", ".env.*"):
        assert pattern in lines, f".gitignore 缺少 {pattern}"


# ------------------------------------------- 安全头：两条伺服路径同一份


def test_both_serving_paths_share_one_header_definition() -> None:
    """云端页与局域网页必须发同一套头。

    修之前的真实状态：云端那套写在 `cloud/Caddyfile`（换成 quick tunnel 后
    根本不在路径上），局域网那套**不存在**。所以判据不是"两边都有头"，而是
    "两边都指向 `shared/security_headers.py` 这一个定义"。
    """
    relay_headers = (REPO / "cloud" / "relay" / "headers.py").read_text("utf-8")
    static = (REPO / "backend" / "app" / "static.py").read_text("utf-8")
    for text, name in ((relay_headers, "cloud/relay/headers.py"),
                       (static, "backend/app/static.py")):
        assert "shared.security_headers" in text, f"{name} 没有复用共享定义"
    # 定义处以外的地方不许再写一遍 CSP 字面量（否则迟早再次分裂）。
    # 测试自己当然要断言这个字符串，所以排除测试目录。
    hits = [h for h in _matches(r"default-src 'none'")
            if not h.startswith("shared/security_headers.py")
            and "/tests/" not in h]
    assert hits == [], f"CSP 字面量出现在共享定义之外：{hits}"
