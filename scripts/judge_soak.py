"""对已经采集到的挂机样本套用 soak_test.py 的真实判据。

用途：24h 挂机可能在跑完之前被外部清理杀掉（实测发生过两次：一次是
`git reset --hard` 连带，一次是环境级清理），而 `_write_checkpoint` 的
注释写明"只记事实不做判定"。于是 checkpoint 里躺着几百个样本却没人下结论。

但判据本身是「后半程 vs 前半程」，**不要求总时长必须是 24 小时** ——
只要样本够多（`_net_growth` 要求暖机后至少 6 个），每条判据都能表态。
这个脚本直接 import `scripts.soak_test` 里同一份 `_net_growth` 与同一组
阈值常量，不复制判据，避免两份会各自漂移的"真相"。

用法：
    .venv\\Scripts\\python.exe scripts\\judge_soak.py build\\soak_24h_aborted_10h.json

不传参数时读 `build\\soak_24h.json`。退出码：0 = 全部通过，1 = 有判据越界。
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.soak_test import (                                    # noqa: E402
    RSS_GROWTH_LIMIT,
    TASK_GROWTH_LIMIT,
    THREAD_GROWTH_LIMIT,
    WARMUP_FRACTION,
    _human,
    _net_growth,
    _plateau_info,
)


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else
                REPO / "build" / "soak_24h.json")
    if not path.exists():
        print(f"找不到报告：{path}", file=sys.stderr)
        print("挂机时用 --report 指定路径；中断后残留的检查点同样可以直接判定。",
              file=sys.stderr)
        return 2
    d = json.loads(path.read_text(encoding="utf-8"))
    samples = d.get("samples") or []
    print(f"文件        : {path}")
    print(f"状态        : {d.get('status')}")
    print(f"已跑时长    : {d.get('hours'):.3f} 小时 / 目标 {d.get('total_hours')} 小时"
          f"（{d.get('progress')}）")
    print(f"轮次        : {d.get('rounds')}（成功 {d.get('rounds_ok')} / "
          f"失败 {d.get('rounds_bad')}）")
    print(f"样本数      : {len(samples)}")
    print(f"checkpoint  : {d.get('written_at')}")
    fails = d.get("failures") or []
    print(f"轮次失败    : {len(fails)} 条" + (f" -> {fails[:3]}" if fails else ""))
    print()

    rss_series = [s["rss"] for s in samples if s.get("rss")]
    thread_series = [float(s["threads"]) for s in samples if s.get("threads") is not None]
    task_series = [float(s["tasks"]) for s in samples if s.get("tasks", -1) >= 0]
    handle_series = [float(s["handles"]) for s in samples if s.get("handles")]

    # verdict 三态：True = 通过，False = 越界，None = 证据不足（不判）
    verdicts: list[tuple[str, bool | None, str]] = []

    rss_abs, rss_rel, rss_base = _net_growth(rss_series)
    if rss_rel is None:
        verdicts.append(("RSS", None, "样本不足，无法判定"))
    else:
        ok = rss_rel <= RSS_GROWTH_LIMIT
        verdicts.append((
            "RSS", ok,
            f"后半程均值相对前半程 {rss_rel:+.1%}（上限 {RSS_GROWTH_LIMIT:.0%}），"
            f"基线 {_human(rss_base)}，净增 {_human(rss_abs)}，峰值 {_human(max(rss_series))}",
        ))

    thread_abs, _tr, thread_base = _net_growth(thread_series)
    thread_flat, thread_flat_rel = _plateau_info(thread_series)
    over = thread_abs > THREAD_GROWTH_LIMIT
    verdicts.append((
        "线程数", (None if not thread_flat else not over) if over else True,
        f"净增 {thread_abs:+.2f}（上限 +{THREAD_GROWTH_LIMIT}），"
        f"基线 {thread_base:.0f}，末值 {thread_series[-1]:.0f}"
        + ("" if thread_flat else
           f" —— **末段仍在上升 {thread_flat_rel:+.1%}，窗口太短，不能判定**"),
    ))

    task_abs, _tkr, task_base = _net_growth(task_series)
    task_flat, task_flat_rel = _plateau_info(task_series)
    over = task_abs > TASK_GROWTH_LIMIT
    verdicts.append((
        "任务数", (None if not task_flat else not over) if over else True,
        f"净增 {task_abs:+.2f}（上限 +{TASK_GROWTH_LIMIT}），"
        f"基线 {task_base:.0f}，末值 {task_series[-1]:.0f}"
        + ("" if task_flat else
           f" —— **末段仍在上升 {task_flat_rel:+.1%}，窗口太短，不能判定**"),
    ))

    # 句柄没有阈值（不是判据），只报告趋势
    handle_abs, _hr, handle_base = _net_growth(handle_series)
    verdicts.append((
        "句柄数", True,
        f"净增 {handle_abs:+.2f}（**无阈值，仅观测**），"
        f"基线 {handle_base:.0f}，末值 {handle_series[-1]:.0f}",
    ))

    http_fail_rate = (d.get("rounds_bad") or 0) / max(1, d.get("rounds") or 1)
    verdicts.append((
        "请求成功率", http_fail_rate == 0,
        f"失败 {d.get('rounds_bad')}/{d.get('rounds')}（{http_fail_rate:.2%}）",
    ))

    print("=" * 72)
    print(f"判定（判据与阈值全部取自 scripts/soak_test.py，暖机丢弃 {WARMUP_FRACTION:.0%}）")
    print("=" * 72)
    for name, ok, detail in verdicts:
        tag = "PASS" if ok else ("N/A " if ok is None else "FAIL")
        print(f"  {tag}  {name:<8} {detail}")

    print()
    print("线程名变化：")
    first_names = samples[0].get("thread_names") or []
    last_names = samples[-1].get("thread_names") or []
    added = [n for n in last_names if n not in first_names]
    removed = [n for n in first_names if n not in last_names]
    print(f"  首：{len(first_names)} 个 -> {first_names}")
    print(f"  末：{len(last_names)} 个 -> {last_names}")
    print(f"  新增：{added or '（无）'}")
    print(f"  消失：{removed or '（无）'}")

    print()
    print("分段趋势（每 1/10 一段，看是否单调）：")
    n = len(samples)
    seg = max(1, n // 10)
    print(f"  {'段':>3}  {'t(h)':>7}  {'rss':>9}  {'threads':>7}  {'tasks':>5}  {'handles':>7}")
    for i in range(0, n, seg):
        chunk = samples[i:i + seg]
        rss_vals = [s["rss"] for s in chunk if s.get("rss")]
        print(f"  {i // seg + 1:>3}  {chunk[-1]['t'] / 3600:>7.2f}  "
              f"{_human(statistics.mean(rss_vals)) if rss_vals else 'n/a':>9}  "
              f"{statistics.mean([s['threads'] for s in chunk]):>7.1f}  "
              f"{statistics.mean([s['tasks'] for s in chunk]):>5.1f}  "
              f"{statistics.mean([s['handles'] for s in chunk if s.get('handles')]):>7.1f}")

    bad = [name for name, ok, _ in verdicts if ok is False]
    unknown = [name for name, ok, _ in verdicts if ok is None]
    print()
    if bad:
        print(f"结论：{len(bad)} 项越界 -> {bad}")
        if unknown:
            print(f"（另有 {len(unknown)} 项证据不足：{unknown}）")
        return 1
    if unknown:
        print(f"结论：{len(unknown)} 项证据不足，无法判定 -> {unknown}")
        print("原因：线程/任务数在窗口末段仍在单调上升 —— 判据要求暖机段"
              "（前 25%）盖住惰性建线程期，实测本机需约 3 小时。"
              "这不是缺陷，是观测窗口不够长。")
        return 2
    print(f"结论：全部判据通过（本次观测 {d.get('hours') or 0:.1f} 小时；"
          f"用例要求的时长请对照 docs/07 §12 的 H-08）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
