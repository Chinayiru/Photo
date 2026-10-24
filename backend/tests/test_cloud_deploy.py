"""P1-10 云端部署文件的结构断言（`cloud/docker-compose.yml` 等）。

## 为什么这些是测试而不是文档

部署文件里每一条都是"写错了会在生产上出事、而且在开发机上完全看不出来"
的决定。举三个真实后果：

* `relay` 写成 `ports: 8000:8000` 而不是 `expose` —— Docker 直接写 iptables
  **绕过 ufw**，中继端口裸露在公网。而开发机上 `docker compose up` 一切正常。
* `PB_CLOUD_DB` 没指到挂载卷 —— `cloud.db` 落在镜像内部 `/app`，升级时
  `docker compose up -d --build` 一重建，**账号、设备、配对码全清空**。
* Caddyfile 里 `/agent/ws` 写在 `/d/*` 之后 —— Caddy 的 handle 是先命中
  先生效，隧道入口被业务规则截走，表现是"浏览器打开域名挺好，隧道永远连不上"。

这三条都不会被 `pytest backend/tests` 以外的任何检查覆盖，所以钉在这里。

## 为什么不校验 Caddyfile 语法

那需要 `caddy validate`（一个 Docker 镜像）。CI 里未必有 Docker，而
"用 Docker 校验 Docker 配置"在离线环境是个死结。这里退一步做**结构**断言：
顺序、关键指令、兜底响应码。真正的语法校验是 `bash cloud/deploy.sh --check`。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

CLOUD = REPO / "cloud"

yaml = pytest.importorskip("yaml", reason="PyYAML 只在开发依赖里，缺了跳过")


def _compose() -> dict:
    return yaml.safe_load((CLOUD / "docker-compose.yml").read_text(encoding="utf-8"))


def _caddyfile() -> str:
    return (CLOUD / "Caddyfile").read_text(encoding="utf-8")


def _dockerfile() -> str:
    return (CLOUD / "Dockerfile").read_text(encoding="utf-8")


# ---------------------------------------------------------------- compose


def test_compose_has_relay_and_caddy() -> None:
    services = _compose()["services"]
    assert set(services) == {"relay", "caddy"}


def test_relay_is_not_published_to_the_host() -> None:
    """`relay` 只能用 `expose`。

    `ports` 会让 Docker 绕过 ufw 把 8000 捅到公网。这个断言是
    "上生产前最后一道自动检查"。
    """
    relay = _compose()["services"]["relay"]
    assert "ports" not in relay, "relay 不能用 ports（会绕过 ufw 暴露到公网）"
    assert relay["expose"] == ["8000"]


def test_only_caddy_publishes_ports() -> None:
    services = _compose()["services"]
    assert services["caddy"]["ports"] == ["80:80", "443:443", "443:443/udp"]


def test_cloud_db_lives_on_a_persistent_volume() -> None:
    """库必须在挂载卷上，且启动命令必须指过去。

    只挂卷不改 `PB_CLOUD_DB` 是无效的：默认路径是工作目录下的
    `cloud.db`，也就是镜像内部的 `/app/cloud.db`，重建即丢。
    """
    relay = _compose()["services"]["relay"]
    assert "./data:/data" in relay["volumes"]
    assert relay["environment"]["PB_CLOUD_DB"] == "/data/cloud.db"


def test_relay_sets_host_allowlist() -> None:
    """`PB_CLOUD_HOSTS` 是 DNS rebinding 的唯一防线，不能漏配。"""
    relay = _compose()["services"]["relay"]
    assert relay["environment"]["PB_CLOUD_HOSTS"] == "${DOMAIN}"


def test_build_context_is_repo_root() -> None:
    """构建上下文必须是仓库根 —— `cloud/relay` 依赖 `shared/`。

    在 `cloud/` 里构建会缺 `shared/protocol.py`，而复制一份进
    `cloud/` 会造出两份帧协议副本（正是 docs/09 §9.6 要避免的漂移）。
    """
    build = _compose()["services"]["relay"]["build"]
    assert build["context"] == ".."
    assert build["dockerfile"] == "cloud/Dockerfile"


def test_caddy_depends_on_relay_and_mounts_the_caddyfile() -> None:
    caddy = _compose()["services"]["caddy"]
    assert caddy["depends_on"] == ["relay"]
    assert "./Caddyfile:/etc/caddy/Caddyfile:ro" in caddy["volumes"]
    # 证书卷不挂的话，每次重建都重新申请 Let's Encrypt，很快触发限流
    assert any("caddy_data:/data" in v for v in caddy["volumes"])


# ---------------------------------------------------------------- Caddyfile


def _handle_positions(text: str) -> dict[str, int]:
    """每个 `handle <pattern> {` 的行号。"""
    out: dict[str, int] = {}
    for n, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith("handle"):
            continue
        # `handle /agent/ws {` / `handle {`
        rest = stripped[len("handle"):].strip()
        if rest.endswith("{"):
            rest = rest[:-1].strip()
        out[rest or "(catch-all)"] = n
    return out


def test_agent_ws_handle_comes_first() -> None:
    """`/agent/ws` 必须排在 `/d/*`、`/api/*` 与兜底之前。

    Caddy 的 handle 是**互斥匹配、先命中先生效**。写晚了隧道入口
    会被业务规则或静态规则截走，症状是"网页能开、隧道永远连不上"。
    """
    pos = _handle_positions(_caddyfile())
    assert "/agent/ws" in pos, pos
    agent = pos["/agent/ws"]
    for other in ("/d/*", "/api/*", "/healthz", "(catch-all)"):
        assert other in pos, f"Caddyfile 缺 handle {other}：{pos}"
        assert agent < pos[other], (
            f"handle /agent/ws（第 {agent} 行）必须在 {other}"
            f"（第 {pos[other]} 行）之前")


def test_websocket_and_streaming_handles_disable_buffering() -> None:
    """`flush_interval -1` + `read_timeout 0`。

    不关缓冲：帧被攒批（缩略图一张张蹦、心跳延迟）；不关超时：
    长连接被 Caddy 剪断。两者也直接决定 AC-4.2（2 GB 传输云端内存
    < 200 MB）—— 缓冲意味着整张图先在 Caddy 里攒一遍。
    """
    text = _caddyfile()
    assert text.count("flush_interval -1") >= 2, "隧道与 /d/* 都要关缓冲"
    assert text.count("read_timeout 0") >= 2
    assert text.count("write_timeout 0") >= 2


def test_unmatched_paths_are_not_served_as_200_pages() -> None:
    """兜底不能把未匹配路径当静态文件服务。

    P1 之前这里断言"兜底必须 404 且不含 `file_server`"，因为 relay 不
    托管页面、`frontend/dist` 也不进镜像。P1 之后页面**由 relay 自己
    伺服**（`cloud/relay/pages.py`），所以兜底改成 `reverse_proxy`；
    404 的保证挪到了 relay 里（带扩展名的未知路径仍 404）。

    这条用例钉的仍是"不能无脑 `file_server`"这个性质 —— Caddy 直接
    服务目录会绕过 relay 的路径判定，`/app.js.map` 之类就被放出来了。
    """
    text = _caddyfile()
    tail = text[text.index("handle {"):]
    assert "file_server" not in tail, "兜底不该是静态文件服务（会绕过 relay 的 404 判定）"
    assert "respond" not in tail, "兜底不再直接 404 —— 页面由 relay 伺服"
    assert "reverse_proxy relay:8000" in tail, tail


def test_caddyfile_has_no_media_caching() -> None:
    """AC-7.5：云端配置里不能有任何媒体缓存指令。

    中继只搬字节。一句 `cache` / `expires` 就可能让 Caddy 把照片
    留在云盘的缓存里 —— 那是产品对用户承诺过不会发生的事。
    """
    # 只查**媒体缓存指令**。HSTS 的 `max-age` 与 `Cache-Control: no-store`
    # 是安全响应头，不是缓存 —— 用 `max-age` 这个词做全文匹配会把它们
    # 一起判死。
    for n, line in enumerate(_caddyfile().splitlines(), start=1):
        bare = line.strip().lower()
        head = bare.split(" ", 1)[0] if bare else ""
        assert head not in ("cache", "expires", "cache-control"), (
            f"Caddyfile:{n} 出现媒体缓存指令：{line.strip()}")
        assert "cache-control" not in bare or "no-store" in bare, (
            f"Caddyfile:{n} 的 Cache-Control 只允许 no-store：{line.strip()}")


# ---------------------------------------------------------------- Dockerfile


def test_dockerfile_runs_single_worker() -> None:
    """必须单进程：设备注册表是内存 map。

    多 worker 下"电脑连在 A、手机请求打到 B"变成常态，表现是随机 503。
    """
    assert '"--workers", "1"' in _dockerfile()
    assert "cloud.relay.main:create_app" in _dockerfile()
    assert "--factory" in _dockerfile()


def test_dockerfile_runs_as_non_root() -> None:
    text = _dockerfile()
    assert "\nUSER relay" in text
    assert "useradd" in text


def test_dockerfile_trusts_only_docker_ranges_for_forwarded_headers() -> None:
    """`--forwarded-allow-ips` 不能是 `*`。

    前面是 Caddy 时确实需要读 `X-Forwarded-For`（否则所有手机共用
    Caddy 一个 IP，限流桶变成全局的）。但信任 `*` 等于信任任何人，
    而 8000 端口一旦被误暴露，伪造头就能绕过限流。
    """
    text = _dockerfile()
    assert "--forwarded-allow-ips" in text
    assert '"*"' not in text
    assert "172.16.0.0/12" in text


# ---------------------------------------------------------------- 上下文与密钥


def test_dockerignore_excludes_heavy_and_secret_paths() -> None:
    """构建上下文是仓库根，所以排除清单必须靠谱。

    `frontend/node_modules` + `data/thumbs`(800 MB) + `dist`(434 MB)
    一起塞进构建缓存的话，一次 build 要好几分钟；而 `.env` 进了镜像
    等于把配置连着镜像一起分发。

    ★ `frontend/dist` 恰恰**必须**放回（云端要伺服页面），所以这里
    断言的是"放回的是 dist 而不是整个 frontend"—— 一条 `!frontend`
    看着更省事，实际会把 `node_modules` 也带进去。
    """
    text = (REPO / ".dockerignore").read_text(encoding="utf-8")
    # 注释行不算规则
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    for needed in ("!cloud", "!shared", "!requirements.txt"):
        assert needed in lines, f".dockerignore 缺 {needed}"
    # 全排之后必须有选择地放回，否则上下文是空的
    assert lines[0] == "*", f"第一条规则必须是全排，实际是 {lines[0]!r}"
    # 页面产物要进镜像（`cloud/Dockerfile` 的 `COPY frontend/dist/`）
    assert "!frontend/dist" in lines, "缺 !frontend/dist ⇒ 镜像里没有页面"
    assert "!frontend/dist/**" in lines, lines
    # ★ `!frontend` 是必需的（docker 不允许在已排除的目录里放回子项），
    #   但**必须紧跟一条 `frontend/*` 把子项再排掉**，否则 node_modules
    #   会跟着进构建上下文。这个"放回目录、立刻排掉子项、再放回子目录"
    #   的三步是 dockerignore 的硬要求，不是这里的啰嗦。
    if "!frontend" in lines:
        after = lines[lines.index("!frontend") + 1:]
        assert after and after[0] == "frontend/*", (
            "`!frontend` 之后紧跟着的必须是 `frontend/*`，"
            f"否则 node_modules 会进构建上下文；实际是 {after[:2]}")
        assert lines.index("!frontend") < lines.index("!frontend/dist")
    # sourcemap 不进镜像（`pages.py` 的 `_BLOCKED_SUFFIXES` 也不服务它）
    assert "frontend/dist/**/*.map" in lines, lines


def test_env_example_has_no_secrets() -> None:
    """`.env.example` 只放域名与邮箱。

    `agent_token` 由 `scripts/cloud_init.py` 生成、只打印一次，
    绝不进配置文件模板 —— 模板是会被提交到 git 的。
    """
    text = (CLOUD / ".env.example").read_text(encoding="utf-8")
    assert "DOMAIN=" in text
    assert "ACME_EMAIL=" in text
    # 只看真正的赋值行（注释里提到 agent_token 是说明，不是配置）
    assignments = [ln.strip() for ln in text.splitlines()
                   if "=" in ln and not ln.strip().startswith("#")]
    keys = {a.split("=", 1)[0].strip().lower() for a in assignments}
    assert keys == {"domain", "acme_email"}, keys
    for a in assignments:
        value = a.split("=", 1)[1].strip()
        assert value in ("photos.example.com", "you@example.com"), a


# ---------------------------------------------------------------- Windows 部署


def _deploy_ps1() -> bytes:
    return (CLOUD / "deploy.ps1").read_bytes()


def test_windows_deploy_script_exists_and_is_utf8_with_bom() -> None:
    """`cloud/deploy.ps1` 必须是 **UTF-8 with BOM**。

    ★ 这条是实测出来的，不是洁癖：Windows Server 默认只有
    Windows PowerShell 5.1，它读**无 BOM** 的 `.ps1` 会按系统 ANSI 码页
    （中文机器是 936）解码。于是文件里每个中文字符串都变乱码，
    而乱码字节里包含 `0x22`（引号）时会把字符串**提前闭合**，
    整份脚本报出几十条 `Missing ')' in function parameter list` ——
    错误信息还指向完全无辜的行。

    开发机上是 PowerShell 7，它能自动识别 UTF-8，所以同样的文件
    在开发机语法检查一次就过。这正是"部署文件写错在开发机看不出来"
    的那一类缺陷，只能在这里钉住。
    """
    raw = _deploy_ps1()
    assert raw.startswith(b"\xef\xbb\xbf"), (
        "deploy.ps1 缺 UTF-8 BOM —— Windows PowerShell 5.1 会把中文按 "
        "GBK 解码并吞掉引号，整个脚本报一堆假语法错误")


def test_windows_deploy_script_uses_lf_only() -> None:
    """换行必须是 LF（`test_repo_hygiene` 的基线之外不允许 CRLF）。"""
    raw = _deploy_ps1().lstrip(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw, "deploy.ps1 里出现了 CRLF"


def test_windows_deploy_script_runs_the_entry_module_not_uvicorn_cli() -> None:
    """单进程 + 走 `main()`，不走 uvicorn 的命令行入口。

    两条理由都不允许退回 CLI：

    1. **单进程**：设备注册表是内存 map（同 Dockerfile），`--workers>1`
       会让"电脑连在 worker A、手机打 worker B"变成常态。
    2. **CLI 的默认开关是错的**：`python -m uvicorn` 默认
       `--proxy-headers` + `forwarded_allow_ips=127.0.0.1`（把
       `scope["client"]` 改写成可伪造的 XFF）、access log 全开（把
       `?code=NNNNNN` 写进日志）、并回 `Server: uvicorn`。这些只能在
       `main()` 里一次设对（`docs/11` ISSUE-030 / 032）。
    """
    text = _deploy_ps1().decode("utf-8-sig")
    # 只看非注释行：注释里要解释"为什么不能用 uvicorn CLI"，必然出现
    # `-m uvicorn` 字面量。
    code = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "scripts\\cloud_run.py" in code
    assert "-m cloud.relay.main" not in code
    assert "-m uvicorn" not in code
    assert "--workers" not in code


def test_windows_deploy_script_points_the_web_dir_at_the_bundled_dist() -> None:
    """`PB_CLOUD_WEB_DIR` 必须指向**随包带的** `frontend/dist`。

    缺这个环境变量时 `StaticPages` 会退回 `Path(__file__).parents[2]`
    推出的默认位置。那个位置在仓库布局里是对的，但服务器上是
    `C:\\PhotoBridgeCloud\\frontend\\dist` —— 恰好也对。所以不能靠
    "默认值没错"来省这一行：一旦安装目录换个形状（例如把包解压到
    子目录再运行），默认值就悄悄指到不存在的地方，而症状是
    「隧道全通、手机打开域名 404」这种最难排查的形态。
    """
    text = _deploy_ps1().decode("utf-8-sig")
    assert "PB_CLOUD_WEB_DIR" in text
    assert "frontend\\dist" in text


def test_windows_deploy_script_kills_by_port_not_by_process_name() -> None:
    """重启旧 relay 时只能按端口找占用者。

    `Stop-Process -Name python -Force` 会把服务器上**所有** Python
    一起杀掉。这台机器上还跑着别的东西（端口 8000 只是其中之一），
    而 deploy.ps1 是给不熟这台机器的人跑的。
    """
    text = _deploy_ps1().decode("utf-8-sig")
    assert "Get-NetTCPConnection" in text
    assert "Stop-Process -Name" not in text


def test_windows_deploy_script_survives_schtasks_stderr_on_first_run() -> None:
    """首次部署时 `schtasks /Delete` 报"任务不存在"不能中断脚本。

    实测踩到（2026-10-23，203.0.113.10 首次部署）：deploy.ps1 全程设了
    `$ErrorActionPreference = "Stop"`，而外部命令写 stderr 在 PowerShell
    5.1 里会被升级成**终止性**的 NativeCommandError，`2>$null` 拦不住。
    于是第 8 步第一次 `schtasks /Delete`（任务还不存在）就把脚本打断：
    前 7 步全绿、开机自启没注册、第 9 步的交付信息（含健康检查地址）
    一行都没打印——而退出码看起来只是失败，很容易被当成网络问题。
    """
    text = _deploy_ps1().decode("utf-8-sig")
    # 旧写法：裸调 schtasks，依赖 2>$null 压制 stderr。
    assert "schtasks /Delete /TN $taskName /F 2>$null" not in text
    # 新写法：临时降级 EAP，并把各流合并进成功流。
    assert "schtasks /Delete /TN $taskName /F *>&1" in text
    assert "schtasks @stArgs *>&1" in text
    assert "$ErrorActionPreference = \"Continue\"" in text
    # 失败时要靠退出码判定，而不是被异常提前打断。
    assert "$taskRc = $LASTEXITCODE" in text
    assert "Stop-Process -Id" in text


def test_windows_deploy_script_has_no_literal_password_assignment() -> None:
    """`deploy.ps1` 里不允许出现任何"字面量口令"赋值。

    ★ 这条用例的前身断言的是 `'Password   = "PbCloud2026!"' in text`
    —— 名字叫「不许硬编码口令」，断言却是「必须等于这个硬编码值」，把
    线上真实口令钉成了契约（与 ISSUE-018 同型，`docs/11` ISSUE-032）。
    改成**负向**判据：任何形如 `$X = "<非空字面量>"` 的赋值都算违规，
    口令只能来自 `Read-Host` 或 stdin。
    """
    import re

    text = _deploy_ps1().decode("utf-8-sig")
    bad = [ln.strip() for ln in text.splitlines()
           if re.search(r'\$(?:Password|Pass|Pwd)\w*\s*=\s*"[^"]+"', ln)]
    assert not bad, f"deploy.ps1 里出现了字面量口令：{bad}"
    # 口令必须真的经 stdin 送进 cloud_init（不然交互式输入就白问了）
    assert "--password-stdin" in text
    assert "Read-Host -AsSecureString" in text


# ---------------------------------------------------------------- 入口行为


def test_open_db_honours_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`open_db()` 必须认 `PB_CLOUD_DB`。

    容器里靠这个把库指向 `/data`。不认的话默认落在工作目录 ——
    而工作目录是镜像内部的 `/app`，重建即丢。
    """
    from cloud.relay import db as cloud_db

    target = tmp_path / "sub" / "cloud.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PB_CLOUD_DB", str(target))
    monkeypatch.setattr(cloud_db, "_db", None)
    try:
        handle = cloud_db.open_db()
        assert handle.path == str(target)
        assert target.exists()
    finally:
        try:
            cloud_db._db.close_all()  # noqa: SLF001 —— 测试清理，别无入口
        except Exception:  # noqa: BLE001
            pass
        cloud_db.set_db(None)
        # 清掉环境变量，避免污染同进程里其它用例
        os.environ.pop("PB_CLOUD_DB", None)


def test_cloud_entry_never_lets_uvicorn_rewrite_the_client_ip() -> None:
    """`main()` 里 uvicorn 的 proxy headers 必须**关着**。

    历史：2026-10-23 之前默认 `forwarded_allow_ips="172.16.0.0/12"`（假设
    前面是 Docker 里的 Caddy）。实际部署是 Windows + cloudflared，从
    `127.0.0.1` 连入 —— 于是真实的 XFF 被**丢弃**，所有手机共用
    `127.0.0.1` 一个限流桶，一个人刷 10 个假登录就能把主人锁在门外
    （`docs/11` ISSUE-030）。

    ★ 判据不能是"某个具体网段字面量"：上一个版本的这条用例断言的就是
    `"172.16.0.0/12" in src`，把一个**错误的拓扑假设**钉成了契约，改对了
    反而变红（与 ISSUE-018 同型）。现在的契约是"谁都不许改写
    `scope['client']`"，来源判定统一走 `netutil`。
    """
    from cloud.relay import main as cloud_main

    src = Path(cloud_main.__file__).read_text(encoding="utf-8")
    # 只看代码行：注释里要**解释**那段历史，必然会出现旧网段与旧参数名，
    # 拿整份源码当判据会把「解释为什么改」判成「没改」。
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "proxy_headers=False" in code
    assert "forwarded_allow_ips" not in code
    assert "172.16.0.0/12" not in code


# ---------------------------------------------------- TC-CLOUD-SEC-004 / 009


def test_caddyfile_sets_no_media_cache_and_no_cdn() -> None:
    """TC-CLOUD-SEC-004：无反代层媒体缓存、无 CDN 标记。

    云端**只做透传**，`Cache-Control` 由电脑端按文件类型决定
    （缩略图 `private, max-age=…`、原图 `private, max-age=0`）。
    Caddy 这边任何 `cache`/`expires`/`header Cache-Control` 都会把
    "云端不缓存照片"变成谎话 —— 而且它的缓存落在反代进程里，
    用户与审计都看不见。
    """
    import re

    text = _caddyfile()
    for directive in ("cache", "expires", "cache-control", "stale",
                      "header_down", "precompressed"):
        # ★ 必须按行首匹配（允许缩进）。第一版写的是 `"\n" + directive`，
        #   而 Caddyfile 里的指令都是缩进的（`\n\texpires 365d`），
        #   于是**每一种**变异都逃逸 —— 断言看着严格，其实永远为真。
        hit = re.search(rf"(?im)^\s*{re.escape(directive)}\b", text)
        assert hit is None, f"Caddyfile 里出现了 {directive}：{hit.group(0)!r}"
    # 不许注入 CDN 家族的头（Cloudflare / Fastly / Varnish）
    low = text.lower()
    for cdn in ("cf-cache-status", "x-cache", "x-served-by", "x-varnish",
                "cdn-cache-control", "surrogate-control"):
        assert cdn not in low, cdn


def test_forward_never_invents_a_public_cache_control() -> None:
    """TC-CLOUD-SEC-004 的另一半：云端**不自己造** `public`。

    这条是"透传"的边界。`_res_headers()` 只放行白名单里的头，电脑端
    没发 `cache-control` 时云端也不能补一个 —— 补 `public, max-age=…`
    等于替电脑端宣布"这张照片可以进共享缓存"，而手机用的是带令牌的
    私有请求，中间任何一层共享缓存都会把照片发给下一个请求者。
    """
    from cloud.relay.forward import _res_headers

    headers, cookies = _res_headers({"headers": {"content-type": "image/jpeg"}})
    assert "cache-control" not in headers
    assert cookies == []

    # 错误响应必须显式 no-store（这条本来就该有）
    from cloud.relay.forward import _error

    resp = _error(503, "E3003", "电脑离线")
    assert resp.headers["cache-control"] == "no-store"


def test_cloud_access_log_has_no_media_payload() -> None:
    """TC-CLOUD-SEC-009：云端日志不记媒体路径与查询串。

    电脑端的 `_request_log_middleware` 只记 `ctx.path`（不含查询串），
    而云端 `forward.py` 的日志分支里只有 device_id 与耗时。这里用
    AST 找出所有 `log.*` 调用，断言没有任何一处传了
    `request.url` / `request.query_params` / 完整路径变量。
    """
    import ast

    src = (REPO / "cloud" / "relay" / "forward.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bad: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                and fn.value.id == "log"):
            continue
        for arg in node.args:
            text = ast.unparse(arg)
            for needle in ("request.url", "query_params", "full_path",
                           "raw_path"):
                if needle in text:
                    bad.append(f"log.{fn.attr}() 传了 {text}")
    assert bad == [], bad


def test_relay_container_has_a_read_only_root_filesystem() -> None:
    """TC-CLOUD-SEC-010：根文件系统只读，可写的地方只有数据卷与 /tmp。

    前提是**云端代码不写盘**（除 sqlite）。这个前提由下面那条 AST
    用例守着 —— 两条必须一起看：只读根却不给 /tmp，Python 在某些
    操作（sqlite 的临时排序文件、locale）上会莫名失败；给了 /tmp
    却不是 tmpfs，临时文件就落进了容器可写层，`docker commit` 能把
    照片字节带出来。

    ★ `cap_drop: ALL` 与 `no-new-privileges` 一并钉住：relay 只需要
    监听 8000、读数据卷，任何 capability 都是多余的授权面。
    """
    relay = _compose()["services"]["relay"]
    assert relay.get("read_only") is True
    assert "/tmp" in (relay.get("tmpfs") or [])
    assert relay.get("cap_drop") == ["ALL"]
    assert "no-new-privileges:true" in (relay.get("security_opt") or [])
    # 唯一允许持久化的地方是数据卷，且它就是 sqlite 的目录
    assert "./data:/data" in (relay.get("volumes") or [])
    assert relay["environment"]["PB_CLOUD_DB"] == "/data/cloud.db"


def test_cloud_code_never_writes_files_except_sqlite() -> None:
    """只读根的**前提条件**：`cloud/**` 不落盘（sqlite 除外）。

    这条如果破了，上一条 `read_only: true` 会让容器在生产上直接
    `OSError: Read-only file system` —— 而开发机（没开只读）一切正常。
    这正是"部署文件写错了在开发机看不出来"那一类缺陷，所以用 AST
    在这里钉死，而不是靠人记。
    """
    import ast

    banned = {"write_text", "write_bytes", "mkdir", "makedirs", "unlink",
              "rmdir", "touch", "symlink_to", "rename", "replace"}
    bad: list[str] = []
    for path in sorted((REPO / "cloud").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(REPO)
        for node in ast.walk(tree):
            # ① Path(...).write_text(...) 这类
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in banned:
                    bad.append(f"{rel}:{node.lineno} .{node.func.attr}()")
                # ② 内建 open(...) 写模式
                if node.func.attr == "open":
                    mode = node.args[0] if node.args else None
                    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
                        if any(ch in mode.value for ch in "wax+"):
                            bad.append(f"{rel}:{node.lineno} open({mode.value!r})")
            # ③ tempfile 家族
            if isinstance(node, ast.Attribute) and node.attr in (
                    "NamedTemporaryFile", "mkstemp", "mkdtemp",
                    "TemporaryDirectory", "TemporaryFile"):
                bad.append(f"{rel}:{node.lineno} tempfile.{node.attr}")
    assert bad == [], bad


def test_windows_deploy_script_starts_the_task_it_registers() -> None:
    """注册完计划任务必须 `/Run` 它，并在启动后再验一次健康。

    实测踩到（2026-10-23）：脚本只 `/Create` 从不 `/Run`，而第 7 步的试跑
    进程是 deploy.ps1 的子进程、SSH 会话一断就被带走 ⇒ 表面上「9 步全绿」，
    过一会儿外网全 502，任务 `Last Result` 停在 267011
    （`SCHED_S_TASK_HAS_NOT_RUN`）。
    """
    text = _deploy_ps1().decode("utf-8-sig")
    code = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "schtasks /Run /TN $taskName" in code
    after = code.split("schtasks /Run /TN $taskName", 1)[1]
    assert "/api/health" in after


def test_cloud_run_entry_is_a_file_so_embeddable_python_can_find_the_package() -> None:
    """启动入口必须是**脚本文件**，不能靠 `-m cloud.relay.main`。

    embeddable 版 Python 的 `python312._pth` 一旦存在，CPython 只按它组装
    `sys.path`，`PYTHONPATH` 被**整条忽略**（实测 `sys.path[:3]` 只剩
    `python312.zip` / 安装目录 / `Lib\\site-packages`），于是
    `-m cloud.relay.main` 报 `ModuleNotFoundError: No module named 'cloud'`
    （2026-10-23 在服务器上踩到，部署第 7 步自检直接失败）。

    脚本文件（`python scripts/cloud_run.py`）自带 `__file__`，自己把包根
    塞进 `sys.path`，MSI / embeddable 两种发行都成立。
    """
    import ast

    src = (CLOUD.parent / "scripts" / "cloud_run.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {"bootstrap", "main"} <= names
    assert "__file__" in src
    assert "sys.path" in src
    assert "cloud.relay.main" in src


def test_windows_deploy_script_only_asks_for_the_password_when_it_registers() -> None:
    """口令询问必须落在「真要登记新设备」的那一刻（`docs/11` ISSUE-037）。

    早先这段在脚本开头无条件执行：无人值守升级（SSH / 计划任务 / 没有
    控制台）会卡在 `Read-Host -AsSecureString` 上，脚本连第 1 步都进不去。
    这条按**行号顺序**钉住：提问必须排在重装检测与库检测之后、`cloud_init`
    之前 —— 也就是说，升级路径上根本走不到它。
    """
    import re

    src = (CLOUD / "deploy.ps1").read_text(encoding="utf-8-sig")
    # 帮助块（`<# ... #>`）与行注释里也写着这两个字面量，先清掉再数
    code = re.sub(r"<#.*?#>", "", src, flags=re.S)
    code = "\n".join(line for line in code.splitlines()
                      if not line.lstrip().startswith("#"))

    assert code.count("Read-Host -AsSecureString") == 1, "口令提问出现了不止一次"
    ask = code.index("Read-Host -AsSecureString")
    func = code.index("function Resolve-Password")
    prev = code.index("$prevRun  = Join-Path")
    init = code.index('& $py "scripts\\cloud_init.py" "init"')

    assert func < ask, "Read-Host 跑到了 Resolve-Password 外面"
    # 调用点只有一个，并且排在重装检测之后、cloud_init 之前：升级路径上
    # （`$prevRun` 命中或库已存在）根本走不到它，这才是这条修复的全部内容。
    assert code.count("$Password = Resolve-Password") == 1, "调用点不止一个"
    call = code.index("$Password = Resolve-Password")
    assert prev < call, "调用点排在重装检测之前：无人值守升级会先被卡住"
    assert call < init, "口令必须在 cloud_init 之前拿到"
