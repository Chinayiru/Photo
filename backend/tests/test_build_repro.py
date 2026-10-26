"""可复现构建：锁文件、SBOM、Dockerfile 的固定性。

## 这个测试防的是什么（外部审计 2026-10-26 的供应链条目）

审计指出两件事：

1. `requirements.txt` 全是下限约束（`starlette>=0.37`），于是"今天装"和
   "三个月后装"不是同一组版本；本仓库没有 lock、没有哈希，任何一次
   `pip install` 都可能在无人察觉的情况下引入一个新版本。
2. `cloud/Dockerfile` 用 `FROM python:3.12-slim` 这种浮动标签 —— 同一份
   源码两次构建可以产出不同的镜像，出了问题无法复现，也无法回答
   "线上那个镜像到底是什么"。

补法是三件套：`requirements.lock`（精确版本 + 每个发布物的 sha256，
安装时 `--require-hashes` 强制）、`sbom/cyclonedx.json`（组件清单）、
基础镜像按 digest 固定。

这个测试**不联网**：它只检查仓库里这三样东西是否自洽（锁文件覆盖
`requirements.txt`、每个条目都有哈希、SBOM 与锁文件版本一致、
Dockerfile 真的用锁文件装并在装的时候开哈希校验）。重新生成锁文件
需要联网，那是 `build/_make_lock.py` 的事。
"""
from __future__ import annotations

import json
import re
from importlib import metadata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "requirements.lock"
SBOM = ROOT / "sbom" / "cyclonedx.json"
REQ = ROOT / "requirements.txt"
DOCKERFILE = ROOT / "cloud" / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"

_PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)", re.MULTILINE)
_HASH_RE = re.compile(r"--hash=sha256:([0-9a-f]{64})")


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _lock_pins() -> dict[str, str]:
    text = LOCK.read_bytes().decode("utf-8")
    return {_canonical(m.group(1)): m.group(2) for m in _PIN_RE.finditer(text)}


def _declared_names() -> list[str]:
    out: list[str] = []
    for line in REQ.read_bytes().decode("utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if m:
            out.append(m.group(1))
    return out


class TestLockFile:
    def test_lock_exists_and_is_lf(self) -> None:
        assert LOCK.is_file(), "缺少 requirements.lock：跑 build/_make_lock.py"
        raw = LOCK.read_bytes()
        assert b"\r\n" not in raw, "锁文件必须是 LF（test_repo_hygiene 会拦 CRLF）"

    def test_every_declared_dependency_is_locked(self) -> None:
        pins = _lock_pins()
        missing = [n for n in _declared_names() if _canonical(n) not in pins]
        assert not missing, (
            f"这些直接依赖没进锁文件：{missing} —— 锁文件与 requirements.txt 脱节了"
        )

    def test_every_pin_carries_at_least_one_hash(self) -> None:
        text = LOCK.read_bytes().decode("utf-8")
        # 按包分段统计哈希：每个 `name==version \` 之后必须跟 ≥1 条 --hash
        blocks = re.split(r"^([A-Za-z0-9][A-Za-z0-9._-]*==[^\s\\]+)", text, flags=re.MULTILINE)
        body = blocks[1:]
        bare: list[str] = []
        for i in range(0, len(body), 2):
            head, rest = body[i], body[i + 1]
            chunk = rest.split("\n\n", 1)[0]
            if not _HASH_RE.search(chunk):
                bare.append(head)
        assert not bare, f"这些条目没有 sha256，等于没锁：{bare}"

    def test_locked_versions_match_the_tested_environment(self) -> None:
        """锁文件必须就是"跑过 1600+ 用例的那组版本"。

        环境里没装的包跳过（比如只在本机装了的开发工具）—— 这个断言的
        目的是"别让锁文件和实际验证过的环境分叉"，不是"环境必须一模一样"。
        """
        drift: list[str] = []
        for name, version in _lock_pins().items():
            try:
                installed = metadata.version(name)
            except metadata.PackageNotFoundError:
                continue
            if installed != version:
                drift.append(f"{name}: 锁 {version} / 实际 {installed}")
        assert not drift, (
            "锁文件与当前环境不一致（改了依赖没重新生成锁文件？）：\n  "
            + "\n  ".join(drift)
        )


class TestSbom:
    def test_sbom_is_cyclonedx_and_covers_lock(self) -> None:
        assert SBOM.is_file(), "缺少 sbom/cyclonedx.json：跑 build/_make_lock.py"
        data = json.loads(SBOM.read_bytes().decode("utf-8"))
        assert data.get("bomFormat") == "CycloneDX"
        assert str(data.get("specVersion", "")).startswith("1.")
        comps = {_canonical(c["name"]): c["version"] for c in data.get("components", [])}
        pins = _lock_pins()
        assert comps, "SBOM 里一个组件都没有"
        only_in_lock = sorted(set(pins) - set(comps))
        only_in_sbom = sorted(set(comps) - set(pins))
        assert not only_in_lock, f"锁文件里有、SBOM 里缺：{only_in_lock}"
        assert not only_in_sbom, f"SBOM 里有、锁文件里缺：{only_in_sbom}"
        mismatch = [
            f"{n}: lock {pins[n]} / sbom {comps[n]}"
            for n in pins
            if pins[n] != comps.get(n)
        ]
        assert not mismatch, "SBOM 与锁文件版本不一致：\n  " + "\n  ".join(mismatch)

    def test_sbom_entries_have_purl(self) -> None:
        data = json.loads(SBOM.read_bytes().decode("utf-8"))
        bad = [
            c.get("name")
            for c in data.get("components", [])
            if not str(c.get("purl", "")).startswith("pkg:pypi/")
        ]
        assert not bad, f"这些组件缺 purl，供应链工具认不出：{bad}"


class TestDockerfile:
    def _text(self) -> str:
        return DOCKERFILE.read_bytes().decode("utf-8")

    def test_base_image_is_pinned_by_digest(self) -> None:
        text = self._text()
        m = re.search(r"^FROM\s+(\S+)", text, re.MULTILINE)
        assert m, "找不到 FROM 行"
        ref = m.group(1)
        assert re.search(r"@sha256:[0-9a-f]{64}$", ref), (
            f"基础镜像必须按 digest 固定，实际是 {ref!r} —— "
            "浮动标签会让同一份源码构建出不同的镜像"
        )

    def test_installs_from_lock_with_hash_checking(self) -> None:
        text = self._text()
        assert "requirements.lock" in text, "Dockerfile 必须装锁文件"
        assert "--require-hashes" in text, (
            "装锁文件时必须带 --require-hashes：否则 pip 只把锁文件当普通"
            "约束，被换过的包照样装上"
        )
        assert re.search(r"pip install[^\n]*--require-hashes[^\n]*-r requirements\.lock",
                         text) or re.search(
            r"pip install[^\n]*-r requirements\.lock[^\n]*--require-hashes", text
        ), "pip install 那一行没有同时出现 --require-hashes 与 -r requirements.lock"

    def test_does_not_trust_forwarded_headers_blindly(self) -> None:
        # 只看**生效的行**：注释里会提到这两种参数（解释为什么不用它们），
        # 拿整份文件去 grep 会把说明文字当成代码，属于自欺欺人的断言。
        text = "\n".join(
            ln for ln in self._text().splitlines()
            if not ln.strip().startswith("#")
        )
        assert "--proxy-headers" not in text or "--no-proxy-headers" in text, (
            "不能无条件 --proxy-headers：X-Forwarded-For 会被当成真实来源 IP，"
            "限速桶形同不存在（ISSUE-049）"
        )
        assert "forwarded-allow-ips" not in text, (
            "已经改成 PB_CLOUD_TRUSTED_PROXIES（cloud/relay/netutil.py），"
            "不要在命令行再写死受信网段"
        )

    def test_sbom_is_shipped_in_the_image(self) -> None:
        assert re.search(r"^COPY\s+sbom/", self._text(), re.MULTILINE), (
            "镜像里要带 sbom/：不然拿到镜像也无法回答它装了哪些组件"
        )


class TestDockerignore:
    def test_lock_and_sbom_survive_the_context_filter(self) -> None:
        """`.dockerignore` 是白名单式排除：没放回来 = 构建时根本没有这个文件。"""
        lines = [
            ln.strip()
            for ln in DOCKERIGNORE.read_bytes().decode("utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        for needed in ("!requirements.lock", "!sbom", "!sbom/**"):
            assert needed in lines, (
                f".dockerignore 里缺 {needed!r}：Dockerfile 的 COPY 会因为"
                "文件不在构建上下文里而失败"
            )


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
