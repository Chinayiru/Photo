"""`scripts/soak_tunnel.py` 的判定逻辑（P1-11 / AC-1.9）。

不跑真实 soak（那是 24 小时的事），只测它**怎么判**。这层逻辑有独立的
出错风险：一个永远返回"通过"的判定器会让 24 小时的机器时间白烧，而
报告上看不出任何异常。

## 为什么这些用例值得写

写脚本时真实踩到的两个坑，都是"判定器悄悄失灵"：

* **串行请求 ⇒ `active_reqs` 恒为 0。** 第一版每轮顺序发 4 个请求，
  在轮次结束时采样。请求都结束了，在途数当然是 0 —— 而 0 恰好是
  "req_id 没泄漏"的期望值。于是这个指标**永远通过**，泄漏检测等于
  不存在。修法是并发发送 + 在途轮询，并且**判定器要反过来检查"到底
  有没有观测到并发"**，观测不到就报问题而不是放行。
* **二进制端点被当 JSON 解析 ⇒ 成功的图片请求被记成失败。** `/thumb`
  回 webp、`/raw` 回 JPEG，硬走 `json.loads` 抛 `JSONDecodeError`，
  被兜底成 `(None, ...)`，于是 24 小时报告里全是假 5xx。

第二条在本文件用"混入一个 bad 计数"的等价方式覆盖（HTTP 层本身不属于
本脚本的职责）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

import soak_tunnel as S  # noqa: E402


def _soak(**kw: Any) -> S.Soak:
    """只造对象，不启动任何进程（判定逻辑不需要 I/O）。"""
    args = argparse.Namespace(
        hours=0.0, interval=1.0, rounds_requests=1, photos=1,
        report="", workdir=str(REPO / "build" / "_soak_unit"), **kw)
    return S.Soak(args)


def _sample(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "t": 1_700_000_000.0, "round": 0, "reqs": 4, "bad": 0,
        "online": 1, "active_reqs": 0, "active_reqs_peak": 2,
        "session_id": "s1", "cloud_rss_mb": 40.0, "attempt": 0,
        "local_connected": True, "last_error": None,
    }
    base.update(kw)
    return base


# ── 判定器：该通过的要通过 ────────────────────────────────────
def test_healthy_run_passes() -> None:
    soak = _soak()
    soak.samples = [_sample(t=1_700_000_000.0 + i * 60, round=i)
                    for i in range(20)]
    rep = soak.report()
    assert rep["online_all"] is True
    assert rep["bad_total"] == 0
    assert rep["active_reqs_final"] == 0
    assert rep["active_reqs_peak_max"] == 2
    assert soak.judge(rep) == []


def test_slow_rss_growth_within_budget_passes() -> None:
    """后半程比前半程涨 10 MB 属于正常抖动（索引缓存），不该报。"""
    soak = _soak()
    soak.samples = ([_sample(cloud_rss_mb=40.0)] * 10
                    + [_sample(cloud_rss_mb=50.0)] * 10)
    rep = soak.report()
    assert rep["cloud_rss_tail_avg_mb"] - rep["cloud_rss_head_avg_mb"] == 10.0
    assert soak.judge(rep) == []


# ── 判定器：该失败的要失败（每条都对应一种真故障）────────────
def test_tunnel_drop_is_caught() -> None:
    soak = _soak()
    soak.samples = [_sample(), _sample(online=0), _sample()]
    rep = soak.report()
    assert rep["online_all"] is False
    problems = soak.judge(rep)
    assert any("掉过" in p for p in problems), problems


def test_5xx_is_caught() -> None:
    soak = _soak()
    soak.samples = [_sample(), _sample(bad=2)]
    problems = soak.judge(soak.report())
    assert any("5xx" in p for p in problems), problems


def test_req_id_leak_is_caught() -> None:
    """收尾时 `active_reqs` 非 0 = `Mux` 里有孤儿 req_id。

    这正是 P1 开发期真实出现过的缺陷（`cancel()` 忘了 `_abort`，
    每取消一次泄漏一个槽位，4096 次后整条隧道全 429）。
    """
    soak = _soak()
    soak.samples = [_sample(), _sample(active_reqs=3)]
    rep = soak.report()
    assert rep["active_reqs_final"] == 3
    problems = soak.judge(rep)
    assert any("req_id 泄漏" in p for p in problems), problems


def test_high_inflight_peak_is_caught() -> None:
    soak = _soak()
    soak.samples = [_sample(active_reqs_peak=40)]
    problems = soak.judge(soak.report())
    assert any("在途请求峰值" in p for p in problems), problems


def test_rss_growth_is_caught() -> None:
    soak = _soak()
    soak.samples = ([_sample(cloud_rss_mb=40.0)] * 10
                    + [_sample(cloud_rss_mb=95.0)] * 10)
    problems = soak.judge(soak.report())
    assert any("RSS" in p for p in problems), problems


def test_reconnect_storm_is_caught() -> None:
    soak = _soak()
    soak.samples = [_sample(session_id=f"s{i}", round=i) for i in range(10)]
    rep = soak.report()
    assert rep["session_count"] == 10
    problems = soak.judge(rep)
    assert any("重连" in p for p in problems), problems


def test_occasional_reconnect_is_fine() -> None:
    """24 小时里断一两次是正常的（运营商重播、路由器重启）。"""
    soak = _soak()
    soak.samples = ([_sample(session_id="s1", round=i) for i in range(40)]
                    + [_sample(session_id="s2", round=40 + i)
                       for i in range(40)])
    rep = soak.report()
    assert rep["session_count"] == 2
    assert soak.judge(rep) == []


# ── 最要紧的一条：压力没并发起来时必须报，不能放行 ──────────
def test_no_observed_concurrency_is_a_problem() -> None:
    """`active_reqs_peak_max == 0` ⇒ 判定必须失败。

    这是第一版脚本的真实缺陷：串行发送导致在途数恒为 0，而 0 是
    "没泄漏"的期望值，于是判定器永远通过、24 小时白跑。判定器必须
    能识别"我没测到"，而不是把"没测到"当成"没问题"。
    """
    soak = _soak()
    soak.samples = [_sample(active_reqs_peak=0) for _ in range(20)]
    rep = soak.report()
    assert rep["active_reqs_peak_max"] == 0
    problems = soak.judge(rep)
    assert any("未观测到在途请求" in p for p in problems), problems


# ── 报告聚合：窗口与方向 ────────────────────────────────────
def test_report_window_and_totals() -> None:
    soak = _soak()
    soak.samples = [_sample(t=1_700_000_000.0, reqs=4),
                    _sample(t=1_700_003_600.0, reqs=6)]
    rep = soak.report()
    assert rep["rounds"] == 2
    assert rep["hours"] == 1.0
    assert rep["reqs_total"] == 10
    assert rep["hours"] == round((1_700_003_600.0 - 1_700_000_000.0) / 3600, 3)


def test_empty_report_is_empty() -> None:
    assert _soak().report() == {"status": "empty"}


def test_errors_are_deduplicated() -> None:
    soak = _soak()
    soak.samples = [_sample(last_error="ConnectError: down"),
                    _sample(last_error="ConnectError: down"),
                    _sample(last_error="InvalidStatus: 401")]
    rep = soak.report()
    assert sorted(rep["errors"]) == ["ConnectError: down", "InvalidStatus: 401"]


# ── 早期自检：报告路径必须能原子落盘（被杀掉也能看进度）─────
def test_checkpoint_is_written_atomically(tmp_path: Path) -> None:
    soak = _soak()
    p = tmp_path / "r" / "soak.json"
    soak.save(p, {"status": "running", "rounds": 3})
    assert p.exists()
    assert '"running"' in p.read_text(encoding="utf-8")
    assert not p.with_suffix(".json.tmp").exists(), "临时文件没被改名"
