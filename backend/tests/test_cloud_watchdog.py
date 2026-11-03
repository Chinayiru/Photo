"""`cloud/watchdog.ps1` 的结构断言。

## 这个脚本防的是什么

中继与 cloudflared 都靠**计划任务**常驻。计划任务只保证"开机跑一次"，
不保证"一直活着"：进程被 OOM 杀掉、断网重连失败退出之后，任务状态会
停在 Ready，没有任何人会把它拉起来，也没有任何提示。2026-10-30 12:54
就是这样断的 —— 两个任务都在，两个进程都没了，直到第二天人工发现
（`docs/11` R-002 / R-010）。看门狗把发现时间从"一天"压到"5 分钟"。

## 为什么这些必须是测试

脚本写错了在开发机上是**完全看不出来**的：

* 判定"任务在不在"如果去解析 `schtasks /Query` 的输出，中文机器上是
  "状态: 正在运行"，英文机器上是 "Status: Running" —— 在开发机
  （可能是中文 Win11）通过，部署到英文 Server 上静默失效。这类缺陷
  只在"换一台机器"时才暴露，正好是最难复现的那一类。
* 任务名写错（`PhotoBridge Cloud Relay` vs `PhotoBridge CloudRelay`）
  时，`schtasks /Run` 返回非 0，看门狗以为自己重启成功了，实际什么
  都没发生 —— 事后看日志才发现"每 5 分钟重启一个不存在的任务"。
* `.ps1` 缺 BOM 在 Windows PowerShell 5.1 上会按 GBK 解码（同
  `test_cloud_deploy.py::test_windows_deploy_script_exists_and_is_utf8_with_bom`）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

CLOUD = REPO / "cloud"
DOCS = REPO / "docs"
WATCHDOG = CLOUD / "watchdog.ps1"
DEPLOY = CLOUD / "deploy.ps1"


def _raw() -> bytes:
    return WATCHDOG.read_bytes()


def _text() -> str:
    return _raw().decode("utf-8-sig")


def _code() -> str:
    """去掉注释后的代码。

    注释里必然出现"为什么不用 `schtasks /Query`"这类字面量，
    直接全文搜会被自己的说明文字绊住。脚本头部的 `<# ... #>`
    块注释也要一起剥掉 —— 那里正是解释"为什么不用"的地方。
    """
    text = re.sub(r"<#.*?#>", "", _text(), flags=re.S)
    return "\n".join(
        ln for ln in text.splitlines() if not ln.lstrip().startswith("#")
    )


# ---------------------------------------------------------------- 文件本身


def test_watchdog_exists_and_is_utf8_with_bom() -> None:
    """必须是 UTF-8 **with BOM**：Windows Server 只有 PowerShell 5.1。"""
    assert WATCHDOG.is_file(), "cloud/watchdog.ps1 不见了"
    assert _raw().startswith(b"\xef\xbb\xbf"), (
        "watchdog.ps1 缺 UTF-8 BOM —— Windows PowerShell 5.1 会按 GBK 解码，"
        "中文字符串里的 0x22 会提前闭合引号，整份脚本报一堆假语法错误"
    )


def test_watchdog_uses_lf_only() -> None:
    """与 deploy.ps1 同一条基线：仓库里 .ps1 一律 LF。"""
    raw = _raw().lstrip(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw, "watchdog.ps1 里出现了 CRLF"


# ---------------------------------------------------------------- 判定口径


def test_watchdog_never_parses_localized_schtasks_output() -> None:
    """不许靠 `schtasks /Query` 的文本判断任务状态。

    那段输出是本地化的：中文机"状态: 正在运行"、英文机"Status: Running"。
    按列名解析会在另一种语言的机器上**静默失效** —— 看门狗以为自己
    一切正常，实际上中继早就死了。
    """
    code = _code()
    assert "schtasks /Query" not in code
    assert "Status:" not in code
    assert "正在运行" not in code


def test_watchdog_probes_relay_over_http() -> None:
    """"活着"的唯一判据是中继自己的 promise：`GET /api/health` 回 200。"""
    code = _code()
    assert "Invoke-WebRequest" in code
    assert "-UseBasicParsing" in code, "PS 5.1 上不加它会被 IE 引擎拖慢/报错"
    assert "-TimeoutSec" in code, "没有超时的探测会挂死看门狗自己"
    assert "/api/health" in code, "默认探测路径必须是中继的健康端点"
    assert "200" in code


def test_watchdog_treats_tunnel_liveness_as_a_process_check() -> None:
    """cloudflared 没有可匿名探测的本机端点，只能看进程。"""
    code = _code()
    assert "Get-Process" in code
    assert "cloudflared" in code


# ---------------------------------------------------------------- 名字一致性


def _relay_task_name_in_deploy() -> str:
    m = re.search(r'\$taskName\s*=\s*"([^"]+)"', DEPLOY.read_text(encoding="utf-8-sig"))
    assert m, "deploy.ps1 里找不到 $taskName 的定义"
    return m.group(1)


def test_watchdog_relay_task_name_matches_deploy_script() -> None:
    """两处任务名必须一字不差。

    改了 `deploy.ps1` 的 `$taskName` 却忘了改看门狗，结果就是看门狗每
    5 分钟对着一个不存在的任务 `schtasks /Run`，失败还被吞掉。
    """
    expected = _relay_task_name_in_deploy()
    m = re.search(r'\$RelayTask\s*=\s*"([^"]+)"', _text())
    assert m, "watchdog.ps1 里找不到 $RelayTask 的默认值"
    assert m.group(1) == expected, f"看门狗里的中继任务名应是 {expected!r}"


def test_watchdog_tunnel_task_name_matches_the_documented_one() -> None:
    """隧道任务是手工注册的（`docs/08` §5），名字以文档为准。"""
    m = re.search(r'\$TunnelTask\s*=\s*"([^"]+)"', _text())
    assert m, "watchdog.ps1 里找不到 $TunnelTask 的默认值"
    name = m.group(1)
    doc = (DOCS / "08-部署与运维手册.md").read_text(encoding="utf-8")
    assert name in doc, f"文档里没有出现过隧道任务名 {name!r}，两边已经漂了"


# ---------------------------------------------------------------- 自愈动作


def test_watchdog_registers_itself_every_few_minutes_as_system() -> None:
    """装成"每 N 分钟、SYSTEM、最高权限"的计划任务。"""
    code = _code()
    assert "schtasks /Create" in code
    assert "/SC MINUTE" in code
    assert "/RU SYSTEM" in code
    assert "/RL HIGHEST" in code, "没有最高权限时 /End 别人的任务会失败"
    assert "PhotoBridge Watchdog" in code


def test_watchdog_restarts_are_exit_code_only() -> None:
    """重启只看退出码，不看返回文本（同样是本地化问题）。"""
    code = _code()
    assert "schtasks /End" in code
    assert "schtasks /Run" in code
    assert "$LASTEXITCODE" in code


def test_watchdog_exits_nonzero_when_still_broken() -> None:
    """坏着退出 1：任务历史里能直接看出"这次没修好"。"""
    code = _code()
    assert "exit 0" in code and "exit 1" in code


# ---------------------------------------------------------------- 域名漂移


def test_watchdog_records_the_public_url_and_warns_when_it_changes() -> None:
    """快隧道每次重启都换域名 —— 至少让"地址变了"有据可查。"""
    text = _text()
    assert "public_url.txt" in text
    assert "trycloudflare" in text
    assert "公网地址已变化" in text, "地址变化时要写一条 WARN，否则无从追溯"


# ---------------------------------------------------------------- 接线


def test_deploy_script_installs_the_watchdog() -> None:
    """`deploy.ps1` 必须顺手把看门狗装上，且失败不致命。"""
    text = DEPLOY.read_text(encoding="utf-8-sig")
    assert 'Step "8c"' in text, "deploy.ps1 缺少注册看门狗的 8c 步"
    assert "watchdog.ps1" in text
    assert "-Install" in text
    assert "-CloudflaredLog" in text, "不把隧道日志传过去，看门狗读不到公网地址"
    # 装不上只 Warn：看门狗是"额外一层保险"，不该让整个部署失败
    after = text.split('Step "8c"', 1)[1].split("# ── 9.", 1)[0]
    assert "Warn" in after


def test_watchdog_contains_no_credentials() -> None:
    """脚本会以 SYSTEM 跑在服务器上 —— 里面不许有任何口令。"""
    text = _text()
    for bad in ("PB_PW", "password=", "Passw0rd", "Sup3rS3cretPw1"):
        assert bad not in text, f"watchdog.ps1 里出现了疑似凭据字面量：{bad}"
