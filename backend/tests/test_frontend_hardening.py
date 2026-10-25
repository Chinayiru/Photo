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


# ---------------------------------------------------------------- ISSUE-044
def test_raw_prefetch_waits_for_the_preview_and_has_a_size_cap() -> None:
    """原图预取必须晚于预览，且有硬上限（ISSUE-044 / docs/08 附录 B.10）。

    这条判据的由来：预取由"查看器打开"直接触发时，一张 22 MB 的原图会和
    用户正等着的 411 KB 预览抢同一条上行，慢链路上表现为"点进去十几秒"。
    只断言"存在某个函数"没有意义，所以这里钉住三条**因果**：
      ① 预览没画完（`previewLoadedId !== id`）就直接返回；
      ② 上限是 8 MB，且收字节前先看 `content-length`；
      ③ 视图层在主图 `@load` 里调用 `markPreviewLoaded`。
    """
    viewer = _read("frontend/src/stores/viewer.ts")
    head, _, tail = viewer.partition("export function schedulePrefetchRaw")
    assert head.strip(), "schedulePrefetchRaw 不见了"
    body = tail.split("\n}\n", 1)[0]
    assert "if (previewLoadedId !== id) return;" in body, (
        "预取没有等预览：慢链路上原图会抢掉预览的带宽")
    assert "const RAW_PREFETCH_MAX_BYTES = 8 * 1024 * 1024;" in viewer
    assert "export function markPreviewLoaded" in viewer
    fetched = viewer.split("async function prefetchRaw", 1)[1]
    assert 'res.headers.get("content-length")' in fetched, (
        "下载前不看声明长度，22 MB 的原图会被收完之后才丢掉")
    assert "RAW_PREFETCH_MAX_BYTES) return;" in fetched

    view = _read("frontend/src/views/ViewerView.vue")
    assert '@load="onMainLoad"' in view
    assert "markPreviewLoaded(s.id)" in view


# ---------------------------------------------------------------- ISSUE-041
def test_relay_root_account_login_sources() -> None:
    """中继首页（`https://域名/`）必须能账号登录，且不出现局域网控件。

    真机那次「变回了局域网连接、页面没有账号登录」是这么来的：
    `cloudPage` 由 `cloudBaseFromLocation()` 推出，只有 `/d/<32hex>/` 匹配；
    首页匹配不上就把隧道域名当"电脑地址"预填成局域网页。
    """
    storage = (REPO / "frontend" / "src" / "api" / "storage.ts").read_text(
        encoding="utf-8")
    auth = (REPO / "frontend" / "src" / "stores" / "auth.ts").read_text(
        encoding="utf-8")
    view = (REPO / "frontend" / "src" / "views" / "PairView.vue").read_text(
        encoding="utf-8")

    # 1) 中继首页的判据：公网主机 + 不是设备页
    assert "export function relayRootBase" in storage
    assert "export function isLanHost" in storage
    root_fn = storage[storage.index("export function relayRootBase"):]
    assert "/^\\/d\\/[0-9a-fA-F]{32}" in root_fn, "设备页必须排除"
    assert "isLanHost(hostname)" in root_fn, "私有网段必须排除"

    # 2) 登录与配对分开：首页拿设备清单，配对才落盘
    assert "export async function loginAccount" in auth
    assert "export async function pairAccountDevice" in auth
    pair_fn = auth[auth.index("export async function pairAccountDevice"):]
    assert "auto: true" in pair_fn
    assert "applyPaired(r, `${origin}/d/${deviceId}`)" in pair_fn, (
        "首页配完要把基址锚到设备页，否则刷新后设备 id 丢了")

    # 3) 视图：入口（局域网/云端）× 登录方式（配对码/账号）两组切换
    #
    # 2026-10-26 起配对页是四个组合，不再有 `showAccount` 这个总开关：
    # 原来"云端/中继首页才给账号入口"的写法被"进入方式"开关取代。断言跟着
    # 实现走，但守的还是同一条语义——两组开关都在、且各自只控制自己那一块。
    assert 'v-if="codeAvailable && accountAvailable"' in view, "两组都能用时才出现切换"
    assert 'role="tablist" aria-label="登录方式"' in view
    assert '@click="mode = \'code\'"' in view
    assert '@click="mode = \'account\'"' in view
    assert "entry = 'lan'" in view, "进入方式：局域网"
    assert "entry = 'cloud'" in view, "进入方式：云端"
    assert '<template v-if="accountAvailable && mode === \'account\'">' in view
    # 配对码区只在"配对码"模式下出现（两种入口都要能填码）
    assert 'v-if="codeAvailable && mode === \'code\'" class="field"' in view
    # 云端入口用配对码时，必须多一个"云端链接"输入框，否则不知道连哪台电脑
    assert "codeAvailable && mode === 'code' && entry === 'cloud' && !cloudPage" in view
    assert 'v-if="relayRoot && accountDevices.length"' in view, "中继首页要能列电脑"
    # 账号配对的落地调用：中继首页列电脑时点哪台配哪台（第三个参数是基址）
    assert "pairAccountDevice(id, accountToken.value, resolvedAccountBase.value)" in view
    assert "chooseAccountDevice(d.id)" in view
