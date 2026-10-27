"""BUG-1 回归：点开邀请链接必须直接进入游客视图，不出现配对码。

真机故障是"邀请链接被当成配对页参数丢掉了"，人停在配对码输入框前面 ——
实际是三个小坑叠在一起：

1. 邀请码被截断：`#guest=pbg1.xxx` 里点号是分隔符，旧清洗只留
   `[A-Za-z0-9_-]`，`pbg1.xxx` 变 `pbg1xxx`，服务端必判无效。
2. 字段名对不上：服务端回 `{"token": …}`，前端读 `r.client_token` ->
   `undefined` -> 什么都没存，等于没进游客态。
3. 失败不吭声：兑换失败时既不进游客视图也不说原因，视觉上就是"停在配对页"。

这三条只有真机点一次才暴露，所以用源码判据钉住：谁把判据改回去，测试立刻红。
它不替代真机验证（`build/_probe_bug1.py` 跑的是真 Chrome），只防回退。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "frontend" / "src" / "App.vue"
AUTH = ROOT / "frontend" / "src" / "stores" / "auth.ts"
CLIENT = ROOT / "frontend" / "src" / "api" / "client.ts"
STORAGE = ROOT / "frontend" / "src" / "api" / "storage.ts"
GUEST_HANDLER = ROOT / "backend" / "app" / "handlers" / "guest.py"


def _code(path: Path) -> str:
    """去掉注释块，避免"注释里提过"就算通过。"""
    text = path.read_text(encoding="utf-8-sig")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text


def test_invite_token_is_read_from_the_hash_and_keeps_the_prefix() -> None:
    code = _code(AUTH)
    assert "guestInviteFromLocation" in code
    assert "location.hash" in code
    assert "pbg1\\." in code, "正则必须显式匹配 pbg1. 前缀（点号是分隔符，别丢）"


def test_invite_regex_accepts_the_real_token_shape() -> None:
    code = _code(AUTH)
    line = next((ln for ln in code.splitlines() if "pbg1\\." in ln), "")
    assert line, "找不到邀请码正则"
    body = line.split("/", 1)[1].rsplit("/", 1)[0]
    pattern = re.compile(body)
    real = "pbg1." + "A" * 43
    assert pattern.match(real)
    assert not pattern.match("pbg1.x"), "过短的垃圾邀请码必须拒绝"


def test_redeem_reads_the_field_the_server_actually_returns() -> None:
    code = _code(AUTH)
    assert "r.token" in code, "必须读 r.token（服务端字段名）"


def test_guest_session_marks_itself_as_guest() -> None:
    assert "setGuest(true)" in _code(AUTH)
    assert 'K_GUEST = "pb.guest"' in _code(STORAGE)


def test_guest_is_a_first_class_identity_in_the_ui() -> None:
    code = _code(APP)
    assert "isGuest()" in code
    assert "handleGuestLink" in code


def test_failure_is_shown_instead_of_silently_falling_back() -> None:
    code = _code(APP)
    assert "guestFail" in code
    assert re.search(r'v-if="[^"]*guestFail', code), "失败态必须有自己的渲染分支"


def test_401_does_not_bounce_a_guest_to_the_pairing_page() -> None:
    code = _code(APP)
    m = re.search(r"if \(isGuest\(\)\) \{(.{0,200})", code, flags=re.S)
    assert m, "会话失效处理里要有游客分支"
    assert "guestFail" in m.group(1), "游客分支应提示重发邀请"


def test_guest_requests_are_tagged_with_the_client_header() -> None:
    assert "X-PB-Client" in _code(CLIENT)


def test_redeem_reply_uses_the_token_field_name() -> None:
    assert '"token":' in _code(GUEST_HANDLER)


def test_redeem_reply_carries_the_local_device_id() -> None:
    code = _code(GUEST_HANDLER)
    assert '"device_id":' in code
    assert '"device_id": _device_id(st)' not in code, (
        "不能用云端那串 32 位 device_id —— 前端比对会永远不等"
    )
    assert 'getattr(st, "device_id"' in code