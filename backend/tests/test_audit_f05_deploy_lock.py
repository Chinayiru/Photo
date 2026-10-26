"""审计 F05：部署脚本装依赖时的**信任**问题。

## 这个测试防的是什么

`cloud/deploy.ps1` 原来是这么装的：

    pip install -i https://mirrors.aliyun.com/pypi/simple/ `
        --trusted-host mirrors.aliyun.com `
        "starlette>=0.37" "uvicorn>=0.30" "websockets>=12.0"

两个问题叠在一起：

- **下限约束**：装到哪个版本取决于"今天 PyPI 上是什么"，上游一次破坏性
  更新（或一次投毒）就会直接进生产。仓库早就有 `requirements.lock`，但这份
  脚本没用它。
- **`--trusted-host`**：关掉 TLS 证书校验。镜像站到服务器这一段变成明文可改，
  攻击者能替换 wheel —— 而后面没有任何哈希校验能发现它。

改法：`cloud/requirements.lock`（每包带 sha256，从已验证的 `requirements.lock`
原样切出）+ `--require-hashes`。镜像仍走阿里云（只为速度），但内容必须对上
哈希，对不上就装不上。

## 判据

这份测试**不联网**，只查仓库自洽：脚本里不许出现能绕开校验的参数、锁文件
里每个包都必须钉版本并带哈希、锁文件与全量锁里的版本/哈希完全一致、生成器
可复现同一份产物。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "cloud" / "deploy.ps1"
RELAY_LOCK = ROOT / "cloud" / "requirements.lock"
FULL_LOCK = ROOT / "requirements.lock"
#: 生成器必须**在版本库里**（审查 Q-02）：`build/` 被 `.gitignore` 整体忽略，
#: 放在那里会让干净 clone 上的两条用例直接找不到文件而红。
MAKER = ROOT / "packaging" / "make_relay_lock.py"

_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)", re.MULTILINE)
_HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})")

DIRECT = ("starlette", "uvicorn", "websockets")


def _code_lines(path: Path) -> list[str]:
    """去掉注释行，只留会被 PowerShell 执行的文本。

    注释里提到 `--trusted-host` 是允许的（我们正是在注释里解释为什么不能
    用它）；被判违规的必须是真参数。
    """
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("#"):
            out.append(line)
    return out


def _blocks(text: str) -> dict[str, str]:
    """{包名: 该包的整段（含续行）}。"""
    out: dict[str, str] = {}
    name = None
    buf: list[str] = []
    for line in text.splitlines():
        if line and not line[0].isspace() and not line.startswith("#"):
            if name:
                out[name] = "\n".join(buf)
            name = line.split("==", 1)[0].split("[", 1)[0].strip().lower()
            buf = [line]
        elif name is not None:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf)
    return out


# ------------------------------------------------------------------ 脚本

def test_deploy_never_disables_certificate_checking() -> None:
    code = "\n".join(_code_lines(DEPLOY))
    assert "--trusted-host" not in code, "trusted-host 等于关掉 TLS 校验"
    assert "--no-verify" not in code
    # 内网/明文的额外源也不许加
    assert "trusted-host" not in code


def test_deploy_installs_from_the_lock_with_hash_checking() -> None:
    code = "\n".join(_code_lines(DEPLOY))
    assert "--require-hashes" in code, "必须开哈希校验"
    assert "cloud\\requirements.lock" in code, "必须装锁文件，而不是下限约束"
    # 装锁文件时不能再把包名/版本写死一遍（两份真相会漂移）
    for name in DIRECT:
        assert f'"{name}>=' not in code, f"仍有下限约束的 {name}"


def test_deploy_fails_loudly_when_the_lock_is_missing() -> None:
    code = "\n".join(_code_lines(DEPLOY))
    idx = code.find("cloud\\requirements.lock")
    assert idx > 0
    window = code[max(0, idx - 400): idx + 400]
    assert "Fail" in window, "锁文件缺失必须当场失败，不能带着旧依赖继续跑"


# ------------------------------------------------------------------ 锁文件

def test_relay_lock_pins_every_package_with_hashes() -> None:
    text = RELAY_LOCK.read_text(encoding="utf-8")
    blocks = _blocks(text)
    assert blocks, "中继锁文件是空的"
    for name, block in blocks.items():
        assert _PIN_RE.search(block), f"{name} 没有钉死版本"
        assert len(set(_HASH_RE.findall(block))) >= 1, f"{name} 没有 sha256"
    for name in DIRECT:
        assert name in blocks, f"锁文件缺直接依赖 {name}"


def test_relay_lock_covers_exactly_the_declared_closure() -> None:
    """锁文件里多出来的包要能说出理由，少了的会装不全。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_make_relay_lock", MAKER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    blocks = _blocks(RELAY_LOCK.read_text(encoding="utf-8"))
    assert set(blocks) == set(mod.RELAY_PACKAGES)


def test_relay_lock_matches_the_verified_full_lock() -> None:
    """版本与哈希必须是全量锁里的那一份，不许手改。"""
    full = _blocks(FULL_LOCK.read_text(encoding="utf-8"))
    relay = _blocks(RELAY_LOCK.read_text(encoding="utf-8"))
    for name, block in relay.items():
        assert name in full, f"{name} 不在全量锁里"
        assert _PIN_RE.search(block).group(2) == _PIN_RE.search(full[name]).group(2)
        assert set(_HASH_RE.findall(block)) == set(_HASH_RE.findall(full[name])), \
            f"{name} 的哈希与全量锁不一致"


def test_generator_reproduces_the_checked_in_lock(tmp_path, monkeypatch) -> None:
    """生成器重跑一次要得到逐字节相同的文件（否则"自动生成"就是谎话）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_make_relay_lock", MAKER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    target = tmp_path / "requirements.lock"
    monkeypatch.setattr(mod, "DST", target)
    assert mod.main() == 0
    assert target.read_text(encoding="utf-8") == RELAY_LOCK.read_text(encoding="utf-8")
