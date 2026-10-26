"""审计 M01：私有签名密钥读不出来时，不许退化成旧密钥。

## 这个测试防的是什么

ISSUE-049 把手机令牌的签名密钥从云端搬回了电脑端（`settings.client_signing_secret`），
旧密钥 `sha256(agent_token)` 只在迁移窗口内兜底。但当时留了一条后路：

    try:
        return client_signing_secret(db)
    except Exception:
        return legacy_tunnel_secret(cfg)      # ← 就是这一行

于是一个**只在错误路径上出现**的分支把修好的东西又拆了：库损坏、迁移半途、
`settings` 表只读 —— 任何一次"读私钥失败"都会让验签悄悄改用那枚云端库里
**也有明文**的旧密钥。攻击者拿到库（含 `agent_token`）就能签令牌，而受害
电脑此时正好在降级状态，验签通过；日志里一个字都没有。

这才是 CWE-636（失败时采用不安全状态）的要害：不安全的不是某个判断，而是
**故障时的默认动作**。

## 改法（三条，互相独立）

1. 读私钥失败 → 抛 `SigningSecretUnavailable`，不再返回任何密钥；
2. 验签侧捕获它 → 拒绝（返回 `None`）+ 记 warning，而不是拿候选列表继续试；
3. 旧密钥只在 `devices.allow_legacy_until` 窗口内、作为**独立候选**参与，
   与私钥失败这件事彻底解耦（窗口检查在 `legacy_window_open()` 里）。

判据是"**故障时行为**"，所以这里全部用 monkeypatch 制造故障，不碰真库。
"""
from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from backend.app import auth, control

ROOT = Path(__file__).resolve().parents[2]


class _Boom(RuntimeError):
    """模拟"读私钥时炸了"（库损坏 / 只读 / 迁移半途都会长这样）。"""


def _broken(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_db: Any) -> str:
        raise _Boom("database is locked")

    monkeypatch.setattr(control, "client_signing_secret", boom)


# --------------------------------------------------------------- 私钥读取

def test_signing_secret_raises_instead_of_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 核心断言：坏掉时**抛**，而不是拿旧密钥顶上。"""
    _broken(monkeypatch)
    cfg = {"cloud.agent_token": "tok"}
    with pytest.raises(auth.SigningSecretUnavailable):
        auth._signing_secret(object(), cfg)


def test_candidate_secrets_does_not_become_legacy_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选列表也不许退化成 [旧密钥] —— 那会让 for 循环验签成功。"""
    _broken(monkeypatch)
    monkeypatch.setattr(auth, "legacy_window_open", lambda db=None: True)
    cfg = {"cloud.agent_token": "tok"}
    with pytest.raises(auth.SigningSecretUnavailable):
        auth._candidate_secrets(object(), cfg)


def test_signing_secret_hard_failure_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """告警必须落日志：静默降级最坏的地方就是没人知道发生过。"""
    _broken(monkeypatch)
    with caplog.at_level("WARNING"):
        with pytest.raises(auth.SigningSecretUnavailable):
            auth._signing_secret(object(), {"cloud.agent_token": "tok"})
    assert any("签名密钥" in r.message or "签名密钥" in r.getMessage()
               for r in caplog.records), caplog.text


def test_db_none_still_uses_legacy_for_old_call_sites() -> None:
    """`db is None` 的老调用点（`_tunnel_secret()` 的历史用法）保持原样。

    这条不是"允许降级"，而是"没有 db 就无从谈私有密钥"——它的输出只用于
    签发/比对，且不接受未经窗口检查的验签（`_candidate_secrets` 里那段）。
    """
    assert auth._signing_secret(None, {"cloud.agent_token": "tok"}) == \
        auth.legacy_tunnel_secret({"cloud.agent_token": "tok"})


# --------------------------------------------------------------- 验签侧

def test_cloud_token_verification_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 私钥不可用时，即便旧密钥窗口开着、令牌是旧密钥签的，也必须拒绝。"""
    _broken(monkeypatch)
    monkeypatch.setattr(auth, "legacy_window_open", lambda db=None: True)

    ctx = types.SimpleNamespace(app=types.SimpleNamespace(cfg={"cloud.agent_token": "tok"}))
    assert auth._verify_cloud_token(ctx, object(), "pbc1.aaa.bbb") is None


def test_cloud_token_verification_fails_closed_and_says_so(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _broken(monkeypatch)
    ctx = types.SimpleNamespace(app=types.SimpleNamespace(cfg={}))
    with caplog.at_level("WARNING"):
        assert auth._verify_cloud_token(ctx, object(), "pbc1.aaa.bbb") is None
    assert "拒绝" in caplog.text or "不可用" in caplog.text, caplog.text


# --------------------------------------------------------------- 控制通道

def test_control_channel_rejects_when_the_secret_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """云端请电脑验签（`/.pb/control` 的 `verify_client`）同样要失败关闭。"""
    _broken(monkeypatch)
    state = types.SimpleNamespace(db=object(), cfg={"cloud.agent_token": "tok"})
    with pytest.raises(control.ControlError):
        control._candidate_secrets(state)


def test_control_channel_error_is_not_a_401() -> None:
    """这是**服务端故障**，不是客户端令牌有问题：用 500，别让手机去重新配对。"""
    err = control.ControlError("E5001", "签名密钥不可用，已拒绝校验", 500)
    assert err.status == 500


# --------------------------------------------------------------- 源码闸门

def test_no_generic_exception_falls_back_to_the_legacy_secret() -> None:
    """源码级闸门：`except` 处理器里不许再 `return legacy_tunnel_secret(...)`。

    用 AST 而不是找字符串：`_signing_secret()` 末尾本来就有一句正常的
    `return legacy_tunnel_secret(cfg)`（`db is None` 的老调用点），
    字符串匹配会把它和"异常降级"混为一谈。
    """
    import ast

    src = (ROOT / "backend" / "app" / "auth.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    offenders: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Return) or sub.value is None:
                continue
            call = sub.value
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "legacy_tunnel_secret"):
                offenders.append(sub.lineno)
    assert not offenders, (
        "except 里又出现了「返回旧密钥」的降级路径，行号：" + str(offenders))
