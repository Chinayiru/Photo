"""可信 Blob 边界必须在**一处**定义（F-01 / F-02 的共同前提）。

`frontend/tests/safeurl_probe.ts` 验证的是**行为**（真跑 Node）；
这个文件验证的是**结构**，补的是行为测不到的回归：

1. 守卫不能退回「协议白名单 + `blob:`」——那等于全面放开；
2. 两个出口（原图、二维码）都必须走同一个登记表，不能各写一套判断；
3. 登记表必须有上限，否则长期不 revoke 的泄漏没人兜底。

结构判据用源码级断言是刻意的：它们挡的是「以后有人图省事又加一条通道」，
而那类改动通常不会带测试。
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FRONT = REPO / "frontend" / "src"


def _read(rel: str) -> str:
    return (FRONT / rel).read_text(encoding="utf-8")


def test_guard_checks_origin_instead_of_whitelisting_blob() -> None:
    src = _read("lib/safeUrl.ts")
    assert "isTrustedBlobUrl" in src, "blob: 必须按来源校验"
    # 协议白名单里不能再出现 blob:（那正是被回退掉的写法）
    head = src.split("export function safeUrl", 1)[0]
    assert '"blob:"' not in head and "'blob:'" not in head, \
        "ALLOWED_PROTOCOLS 不得包含 blob: —— 放行必须来自登记表查询"


def test_registry_is_the_only_creation_entry() -> None:
    """全前端只有 `trustedBlob.ts` 可以调用 `URL.createObjectURL`。"""
    offenders: list[str] = []
    for path in sorted(FRONT.rglob("*.ts")) + sorted(FRONT.rglob("*.vue")):
        if path.name == "trustedBlob.ts":
            continue
        text = path.read_text(encoding="utf-8")
        if "URL.createObjectURL(" in text:
            offenders.append(str(path.relative_to(FRONT)))
    assert not offenders, (
        "这些文件绕过了可信登记表，直接用 createObjectURL：\n"
        + "\n".join(offenders)
        + "\n请改用 createTrustedBlobUrl(blob, origin)。"
    )


def test_viewer_raw_layer_uses_the_registry() -> None:
    src = _read("stores/viewer.ts")
    assert "createTrustedBlobUrl" in src
    assert "revokeTrustedBlobUrl" in src
    # 淘汰循环里不能再出现裸 revoke（否则台账会留下死条目）
    assert "URL.revokeObjectURL" not in src


def test_settings_qr_goes_through_authenticated_fetch() -> None:
    src = _read("views/SettingsView.vue")
    assert "api.getBytes" in src, "二维码必须经已认证 fetch 取二进制"
    assert "createTrustedBlobUrl" in src, "取回的 PNG 必须登记后再渲染"
    assert "entrances/qr" in src
    # 旧的裸 <img> 直连写法必须消失（它带不了 Authorization）
    assert "function qrSrc" not in src
    assert 'absUrl("/api/system/entrances/qr"' not in src


def test_registry_has_a_hard_cap() -> None:
    src = _read("lib/trustedBlob.ts")
    assert "MAX_TRUSTED_BLOBS" in src
    assert "revokeTrustedBlobUrl" in src
    # 上限必须是常量而不是随手写的数字，且要能解释
    assert "= 16" in src, "上限值应显式写出并注明年限（见文件头注释）"


def test_settings_qr_is_released_when_leaving() -> None:
    """设置页被 `<KeepAlive>` 缓存：离开只走 deactivated，不走 unmount。

    只挂 `onBeforeUnmount` 的话，用户每切进设置页一次就多留一张码的 blob，
    台账只增不减 —— `[6z]` 的「离开后台账归零」会一直红。
    """
    src = _read("views/SettingsView.vue")
    assert "function revokeAllQr" in src
    assert "onDeactivated(revokeAllQr)" in src
    assert "onBeforeUnmount(revokeAllQr)" in src
    # 回来时二维码已 revoke，entryCards 没变则 watch 不触发，必须显式重取
    assert "for (const c of entryCards.value) void loadQr(c.kind);" in src

