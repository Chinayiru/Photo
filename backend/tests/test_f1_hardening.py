"""F1（审计严重项）的行为级回归测试。

审计报告 `report/黑盒白盒测试报告.md` §2 的攻击链是一条完整的链：

    POST /panel/pair-code（Origin: evil）
      → 拿 6 位配对码
    POST /api/pair（Content-Type: text/plain，绕过 CORS 预检）
      → 拿 90 天 token + cookie
    GET /api/timeline、/api/items/{id}/raw
      → 读走整个照片库

链上每一环单独看都不算"漏洞"，合起来就是"任何网页都能读全库"。
所以本文件按**环**测：每个测试对应一环，任一环失守都能被单独指认出来，
而不是"修了四层、其中一层其实没接上"。
"""
import sys
from pathlib import Path

import pytest

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from backend.app import netguard  # noqa: E402
from backend.app.config import (  # noqa: E402
    Config,
    ConfigError,
    check_bind_safety,
)
from backend.app.core import Ctx  # noqa: E402
from backend.app.errors import BadRequest  # noqa: E402


def _cfg(tmp_path: Path, **server) -> Config:
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    return Config({
        "server": server,
        "roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}],
        "auth": {"require_pairing": True},
    }, path=data / "config.json", data_dir=data)


# ---------------------------------------------------------------------------
# 第 1 环（F1-a）：CORS 只认白名单，默认完全不启用
# ---------------------------------------------------------------------------

def test_no_cors_headers_when_nothing_configured(tmp_path: Path) -> None:
    """默认配置下 `server.cors_origins` 为空 → 一个 CORS 头都不发。

    这条守的是"新增配置项被手滑填上默认值"。`enable_cors` 本身的行为
    由 test_core.py 的 6 条用例覆盖；这里守的是**装配处**没把它打开。
    """
    from backend.app.server import _cors_origins

    cfg = _cfg(tmp_path, host="127.0.0.1", port=0, allow_lan=False)
    assert _cors_origins(cfg) == ()


def test_cors_origins_rejects_entry_without_scheme(tmp_path: Path) -> None:
    """少写 scheme 的项必须被丢掉，否则它是个永远匹配不上的死项。

    `Origin` 头一定是 `scheme://host[:port]`；白名单里写
    `"localhost:5173"` 与真实 Origin 永远不可能相等，而配置的人会以为
    自己配对了 —— 静默失效比报错更难查。
    """
    from backend.app.server import _cors_origins

    cfg = _cfg(tmp_path, host="127.0.0.1", port=0, allow_lan=False,
               cors_origins=["localhost:5173", "  http://localhost:5173  ",
                             "https://photo.example.com"])
    assert _cors_origins(cfg) == ("http://localhost:5173",
                                  "https://photo.example.com")


def test_cors_origins_accepts_a_single_string(tmp_path: Path) -> None:
    """写成字符串（不是数组）也要能用 —— 配置文件的常见手误。"""
    from backend.app.server import _cors_origins

    cfg = _cfg(tmp_path, host="127.0.0.1", port=0, allow_lan=False,
               cors_origins="https://photo.example.com")
    assert _cors_origins(cfg) == ("https://photo.example.com",)


# ---------------------------------------------------------------------------
# 第 2 环（F1-c）：Host 头校验 —— DNS rebinding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "evil.example.com",
    "evil.example.com:8787",
    "attacker.test:80",
    "0.0.0.0:8787",          # 绑定地址不是可访问地址
    "user@evil.com",         # 别把 userinfo 当成主机名的一部分
])
def test_host_guard_rejects_foreign_hosts(tmp_path: Path, raw: str) -> None:
    cfg = _cfg(tmp_path, host="0.0.0.0", port=8787, allow_lan=True)
    assert netguard.check_host(raw, cfg) is not None, f"{raw!r} 竟然被放行"


@pytest.mark.parametrize("raw", [
    "127.0.0.1",
    "127.0.0.1:8787",
    "localhost",
    "LOCALHOST:8787",
    "[::1]:8787",
    "::1",
])
def test_host_guard_accepts_local_hosts(tmp_path: Path, raw: str) -> None:
    cfg = _cfg(tmp_path, host="127.0.0.1", port=8787, allow_lan=False)
    assert netguard.check_host(raw, cfg) is None, f"{raw!r} 被误拒"


def test_host_guard_accepts_this_machines_lan_ip(tmp_path: Path) -> None:
    """本机局域网 IP 必须被认（否则手机连上来反而打不开）。"""
    from backend.app.config import local_ipv4_addresses

    ips = local_ipv4_addresses()
    if not ips:
        pytest.skip("本机没有可用 IPv4")
    cfg = _cfg(tmp_path, host="0.0.0.0", port=8787, allow_lan=True)
    assert netguard.check_host(f"{ips[0]}:8787", cfg) is None


def test_host_guard_allows_empty_host(tmp_path: Path) -> None:
    """★ 空 Host 放行，不是遗漏。

    这层防护针对的是**浏览器驱动**的攻击，而浏览器不会省略 Host；
    能省略 Host 的客户端同样能伪造一个合法 Host，所以拦它没有收益。
    反过来，`Ctx` 并不只由 HTTP 适配器构造 —— 隧道适配器、内部
    dispatch、测试夹具都没有 Host 的概念，判死会让它们全部失效
    （实测一次判死炸出 80 个用例失败）。
    """
    cfg = _cfg(tmp_path, host="0.0.0.0", port=8787, allow_lan=True)
    assert netguard.check_host(None, cfg) is None
    assert netguard.check_host("", cfg) is None
    assert netguard.check_host("   ", cfg) is None


def test_host_guard_is_wired_into_the_middleware(tmp_path: Path) -> None:
    """校验必须真的接在鉴权中间件里 —— 否则模块写了等于没写。

    这里故意**不**测 handler，测的是"被调用"这件事：
    `netguard` 是新增模块，最可能的失败方式是"写了、导了、忘了调"。
    """
    import inspect

    from backend.app import auth as auth_mod

    src = inspect.getsource(auth_mod.auth_middleware)
    assert "check_host(" in src, "auth_middleware 里没有调用 check_host"
    # 必须排在托管面板之前 —— 攻击链第一步读的就是 /panel/pair-code，
    # 那时还没有 token，鉴权分支管不到它。
    assert src.index("check_host(") < src.index("PANEL_PATHS") or \
        "for prefix in PANEL_PATHS" in src


# ---------------------------------------------------------------------------
# 第 3 环（F1-b）：/panel 默认整条关闭
# ---------------------------------------------------------------------------

def test_panel_is_off_by_default(tmp_path: Path) -> None:
    """`server.panel_http` 默认 False —— 生产配置里不带这个键就是关的。"""
    from backend.app.auth import panel_http_enabled

    cfg = _cfg(tmp_path, host="127.0.0.1", port=0, allow_lan=False)
    assert panel_http_enabled(cfg) is False


def test_panel_can_be_enabled_explicitly(tmp_path: Path) -> None:
    from backend.app.auth import panel_http_enabled

    cfg = _cfg(tmp_path, host="127.0.0.1", port=0, allow_lan=False,
               panel_http=True)
    assert panel_http_enabled(cfg) is True


def test_panel_http_forbids_listening_on_all_interfaces(tmp_path: Path) -> None:
    """面板开着时不许绑 0.0.0.0 —— 否则面板直接暴露到局域网。

    这是第四道门。前三道是 allow_lan / roots / require_pairing；
    面板本身没有 token 机制，所以它必须和"仅回环"绑在一起。
    """
    cfg = _cfg(tmp_path, host="0.0.0.0", port=8787, allow_lan=True,
               panel_http=True)
    ok, why = check_bind_safety("0.0.0.0", cfg)
    assert ok is False
    assert "panel_http" in why


def test_panel_http_off_still_allows_lan_pairing(tmp_path: Path) -> None:
    """反过来：面板关着时，局域网直连（手机配对）必须照常可用。"""
    cfg = _cfg(tmp_path, host="0.0.0.0", port=8787, allow_lan=True,
               panel_http=False)
    ok, why = check_bind_safety("0.0.0.0", cfg)
    assert ok is True, why


# ---------------------------------------------------------------------------
# 第 4 环（F1 根因）：ctx.json() 必须校验 Content-Type
# ---------------------------------------------------------------------------

def _run(coro):
    import asyncio

    return asyncio.run(coro)


@pytest.mark.parametrize("ctype", [
    "text/plain",
    "application/x-www-form-urlencoded",
    "multipart/form-data",
    "application/octet-stream",
    "text/html",
])
def test_json_rejects_non_json_content_type(ctype: str) -> None:
    """这四环里最隐蔽的一环：不校验 Content-Type，等于拆掉 CORS 预检。

    `text/plain` 是浏览器认定的"简单请求"，不发 OPTIONS 预检。
    服务端只要照收，攻击页面就能把 JSON 直接发给 `/api/pair` ——
    上面对 CORS 的所有收敛全被这一步绕过去。
    """
    ctx = Ctx("POST", "/api/pair",
              headers={"content-type": ctype, "content-length": "9"},
              body=b'{"a":123}')
    with pytest.raises(BadRequest) as ei:
        _run(ctx.json())
    assert ei.value.status == 400
    assert "Content-Type" in str(ei.value.detail)


@pytest.mark.parametrize("ctype", [
    "application/json",
    "application/json; charset=utf-8",
    "APPLICATION/JSON",
    "application/vnd.photobridge+json",
    "text/json",
])
def test_json_accepts_json_content_types(ctype: str) -> None:
    ctx = Ctx("POST", "/api/pair",
              headers={"content-type": ctype, "content-length": "9"},
              body=b'{"a":123}')
    assert _run(ctx.json()) == {"a": 123}


def test_json_requires_content_type_when_there_is_a_body() -> None:
    """有 body 却不带 Content-Type 也必须拒。

    否则攻击者只要省掉这个头就能绕过上一层的检查 —— 而"省掉一个头"
    对攻击页面来说零成本。
    """
    ctx = Ctx("POST", "/api/pair", headers={"content-length": "9"},
              body=b'{"a":123}')
    with pytest.raises(BadRequest):
        _run(ctx.json())


def test_json_tolerates_missing_content_type_for_empty_body() -> None:
    """空 body 不校验 —— 否则 `POST /api/...` 不带参数会莫名 400。"""
    assert _run(Ctx("POST", "/x").json()) == {}
    assert _run(Ctx("POST", "/x", headers={"content-length": "0"}).json()) == {}


def test_json_content_type_check_can_be_opted_out() -> None:
    """内部调用可以显式放宽 —— 但必须是**逐个调用点**显式写出来。

    留这个口子是为了二期隧道适配器（它会在解密后自己校验报文完整性），
    而不是让谁都能悄悄绕过：`require_json=False` 会出现在 diff 里。
    """
    ctx = Ctx("POST", "/x", headers={"content-type": "text/plain"},
              body=b'{"a":1}')
    assert _run(ctx.json(require_json=False)) == {"a": 1}


# ---------------------------------------------------------------------------
# 配置层：三个开关都要有默认值，否则升级用户的旧 config.json 会 KeyError
# ---------------------------------------------------------------------------

def test_new_server_keys_have_defaults(tmp_path: Path) -> None:
    """老配置文件没有这三个键时不能炸 —— 升级路径必须平滑。"""
    cfg = _cfg(tmp_path, host="127.0.0.1", port=0, allow_lan=False)
    assert cfg.get("server.trusted_hosts") == []
    assert cfg.get("server.panel_http") is False
    assert cfg.get("server.cors_origins") == []


def test_trusted_hosts_wildcard_is_honoured(tmp_path: Path) -> None:
    """`trusted_hosts: ["*"]` 是显式退出 —— 给反代/NAS 用户留的逃生口。

    与 CORS 的 `"*"` 一致：必须显式写出来，默认永远不是它。
    """
    cfg = _cfg(tmp_path, host="0.0.0.0", port=8787, allow_lan=True,
               trusted_hosts=["*"])
    assert netguard.check_host("anything.example.com", cfg) is None


def test_trusted_hosts_extra_entries_are_honoured(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, host="0.0.0.0", port=8787, allow_lan=True,
               trusted_hosts=["nas.local", "photo.example.com:443"])
    assert netguard.check_host("nas.local:8787", cfg) is None
    assert netguard.check_host("photo.example.com", cfg) is None
    assert netguard.check_host("other.example.com", cfg) is not None


def test_config_error_is_still_a_config_error(tmp_path: Path) -> None:
    """`check_root_safety` 那道门不能因为 F1 的改动而松掉。"""
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    cfg = Config({
        "server": {"host": "127.0.0.1"},
        "roots": [{"id": "bad", "path": str(data), "mode": "readwrite"}],
    }, path=data / "config.json", data_dir=data)
    with pytest.raises(ConfigError):
        _ = cfg.roots
