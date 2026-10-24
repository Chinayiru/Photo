# -*- coding: utf-8 -*-
"""前端加固的**源码判据**（`docs/11` SEC-23、SEC-24）。

## 为什么用源码判据，而不是浏览器测试

这一层是"基址什么时候落盘"的时序问题：错的表现是"用户填错一次地址后，
下次冷启动全部请求打到旧地址"，本仓库没有 JS 测试运行器（`frontend/
package.json` 里只有 dev/build/typecheck，没有 vitest），起真浏览器跑这段
又要造一套 localStorage 夹具 —— 成本远高于它挡的风险。而这条 bug 的全部
内容是"落盘发生在请求成功之前"，用顺序断言钉住它是精确的。

（真正需要行为验证的部分在 `backend/tests/test_p1_pages.py`：那边起真
uvicorn，验证页能拿到、能配对、能拉时间线。）
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_pair_only_persists_the_base_after_the_server_accepts() -> None:
    """配对基址必须随 `savePairing()` 一起落盘，不能先写再试。

    SEC-23：原实现第一行就 `saveBase(cleanBase)`。后果有两个——
    ① 填错地址/电脑没开时，那个连不上的地址长期留在 localStorage 里，
       下次冷启动所有请求都打到那儿；
    ② `PairView` 每次挂载都会自动 `submit()`，失败一次攒一次限速额度，
       用户自己把自己锁在门外（配对码 5 次就锁 10 分钟）。
    """
    src = _read("frontend/src/stores/auth.ts")

    assert "saveBase(" not in src, (
        "auth.ts 里又出现了 saveBase()：基址必须在配对成功后才落盘，"
        "否则失败的候选地址会覆盖掉能用的地址（SEC-23）"
    )

    ping = src.index('api.get<PingResponse>("/api/system/ping"')
    post = src.index('api.post<PairResponse>("/api/pair"')
    saved = src.index("savePairing({")
    assert ping < post < saved, "落盘必须先于请求之外的顺序被打乱了"
    # 两个请求都得带上候选基址，否则它们会打到 localStorage 里的旧地址
    assert src.count("base: cleanBase") >= 2


def test_request_options_can_carry_a_one_shot_base() -> None:
    """`RequestOptions.base` 存在且真的覆盖 `currentBase()`，但不落盘。"""
    src = _read("frontend/src/api/client.ts")
    assert "base?: string;" in src
    assert "opt.base || currentBase()" in src, (
        "once() 没读 opt.base —— 配对时的候选基址会失效"
    )

# --- SEC-24：safeUrl() 守卫 -------------------------------------------------

def test_safe_url_returns_empty_string_for_dangerous_schemes() -> None:
    """TC-XSS-SEC-007：`javascript:` 之类必须返回空串 —— **真跑一遍 TS 实现**。

    这条判据原来只存在于文档里，仓库里同名函数（`PairView.vue`）返回的是
    `URL | null`，语义完全不同。现在 `frontend/src/utils/safeUrl.ts` 是唯一
    实现，`frontend/tests/safeurl_probe.ts` 是它的可执行探针。

    Node 22.6+ 支持 `--experimental-strip-types`（本机 v24 默认已能直接跑
    `.ts`），所以不必为了 13 行断言引入一整条 JS 测试栈（m02320：用户要求
    一条一条修，不顺手扩技术栈）。
    """
    util = REPO / "frontend/src/lib/safeUrl.ts"
    probe = REPO / "frontend/tests/safeurl_probe.ts"
    assert util.is_file(), "SEC-24 的守卫文件不见了"
    assert probe.is_file(), "探针不见了"

    node = shutil.which("node")
    if node is None:  # pragma: no cover - 只在没有 Node 的机器上走
        pytest.skip("没有 node，跳过 safeUrl 行为验证")

    proc = subprocess.run(
        [node, "--experimental-strip-types", str(probe)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        # node 的 stdout 是 UTF-8，而 Windows 的默认代码页是 GBK ——
        # 不显式指定就会在 `��� 13 条` 这一行抛 UnicodeDecodeError
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")
    assert "OK" in proc.stdout


def test_safe_url_rejects_control_characters_in_source() -> None:
    """守卫里必须有一张控制字符/BIDI 黑名单，否则 `java\nscript:` 能溜过去。"""
    src = _read("frontend/src/lib/safeUrl.ts")
    assert "\\u0000-\\u001f" in src, "控制字符范围被删了"
    assert "\\u202a-\\u202e" in src, "BIDI 覆盖符范围被删了"
    assert "const ALLOWED_PROTOCOLS = [\"http:\", \"https:\"]" in src, (
        "协议白名单被改动 —— 只允许 http/https（SEC-24）"
    )


def test_no_component_defines_its_own_safe_url() -> None:
    """不许再有第二个 `safeUrl`：同名不同语义正是 SEC-24 的成因。"""
    offenders = []
    for path in sorted((REPO / "frontend/src").rglob("*.vue")):
        src = path.read_text(encoding="utf-8")
        if re.search(r"function\s+safeUrl\s*\(", src):
            offenders.append(path.name)
    assert offenders == [], f"这些组件自己定义了 safeUrl：{offenders}"


def test_every_src_and_href_binding_goes_through_safe_url() -> None:
    """模板里所有 `:src` / `:href` 绑裸变量时，都必须过一遍 `safeUrl()`。

    这是 SEC-24 真正想挡的东西：Vue 只转义文本插值，属性里的 URL 原样下发，
    `javascript:` 绑到 `:href` 就是一次点击即执行。
    """
    bare = re.compile(r':(?:src|href)="([A-Za-z_$][\w$]*)"')
    offenders = []
    for path in sorted((REPO / "frontend/src").rglob("*.vue")):
        src = path.read_text(encoding="utf-8")
        for match in bare.finditer(src):
            offenders.append(f"{path.name}: :src/:href=\"{match.group(1)}\"")
    assert offenders == [], f"这些绑定没走 safeUrl()：{offenders}"

def test_the_linter_is_installed_and_forbids_v_html() -> None:
    """TC-XSS-SEC-006 的判据必须真的存在（docs/07 §11）。

    这条判据原先写成「vue/no-v-html 必须开启」，但仓库里既没有 eslint 依赖
    也没有 lint 脚本 —— 一条无法执行的判据等于零。现在它检查三件真东西：
    依赖在、脚本在、规则真的被配成 error（不是 warn，warn 会让流水线保持绿色）。
    """
    import json

    repo = REPO
    pkg = json.loads((repo / "frontend" / "package.json").read_text(encoding="utf-8"))

    dev = pkg["devDependencies"]
    for name in ("eslint", "eslint-plugin-vue", "vue-eslint-parser"):
        assert name in dev, f"缺 {name}：判据无从执行"
    assert "lint" in pkg["scripts"], "没有 lint 脚本，判据照样没人跑"
    assert "eslint" in pkg["scripts"]["lint"]
    # "--max-warnings 0" 是这条脚本的重点：warning 不该让流水线保持绿色
    assert "--max-warnings 0" in pkg["scripts"]["lint"]

    cfg = (repo / "frontend" / "eslint.config.mjs").read_text(encoding="utf-8")
    assert '"vue/no-v-html": "error"' in cfg, "v-html 没被禁成 error"
    assert '"no-eval": "error"' in cfg

