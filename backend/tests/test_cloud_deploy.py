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


def test_unmatched_paths_are_404_not_200() -> None:
    """兜底必须是 404。

    relay 不托管页面，所以任何未匹配路径都不该返回 200 —— 否则
    扫描器请求 `/.env` 也会拿到"页面存在"的信号。
    """
    text = _caddyfile()
    tail = text[text.index("handle {"):]
    assert "404" in tail, tail
    assert "file_server" not in tail, "兜底不该是静态文件服务（没有前端产物）"


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

    `frontend/dist`(123 MB) + `data/thumbs`(800 MB) + `dist`(434 MB)
    一起塞进构建缓存的话，一次 build 要好几分钟；而 `.env` 进了镜像
    等于把配置连着镜像一起分发。
    """
    text = (REPO / ".dockerignore").read_text(encoding="utf-8")
    # 注释行不算规则
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    for needed in ("!cloud", "!shared", "!requirements.txt"):
        assert needed in lines, f".dockerignore 缺 {needed}"
    # 全排之后必须有选择地放回，否则上下文是空的
    assert lines[0] == "*", f"第一条规则必须是全排，实际是 {lines[0]!r}"


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


def test_cloud_entry_trusts_forwarded_only_from_docker_ranges() -> None:
    """`main()` 里 forwarded_allow_ips 的默认值。

    用 uvicorn 的默认值（127.0.0.1）会让 Caddy 那一跳被当成"没有代理"，
    所有手机共用 Caddy 容器 IP。
    """
    from cloud.relay import main as cloud_main

    src = Path(cloud_main.__file__).read_text(encoding="utf-8")
    assert "forwarded_allow_ips=forwarded" in src
    assert "PB_CLOUD_FORWARDED_ALLOW_IPS" in src
    # 不许信任一切：默认值必须是具体网段。直接读那行赋值，避免被
    # 文档字符串里讨论 `["*"]` 的文字干扰。
    default_line = next(ln for ln in src.splitlines()
                        if "PB_CLOUD_FORWARDED_ALLOW_IPS" in ln)
    assert '"*"' not in default_line, default_line
    assert "172.16.0.0/12" in src
