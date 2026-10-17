"""挂机脚本的检查点 —— 证明"中途被打断也留得下数据"。

## 为什么需要这个文件

`scripts/soak_test.py` 原本**只在收尾处**写一次报告。于是：

- 挂机跑 8.00 小时 / 481 轮之后，进程被外部进程杀掉（实测踩到：
  同期另一个 agent 在同一台机器上执行 `git reset --hard`）；
- `build\\soak_24h.json` **根本不存在**；
- 只剩一份日志要人肉反推"到底跑到第几轮、失败过几次"。

更糟的是这件事**在单测里永远发现不了** —— 没有人会去测"脚本被 kill 之后
留下什么"，因为正常跑完一切正常。教训：**验收类脚本的"异常退出路径"
本身也是被测对象**，尤其是要跑 24 小时、跨越人睡觉时间的那种。

这个文件不跑真实挂机（那要 24 小时），只验检查点写盘这一小段契约：
状态字段、原子替换、数量截断、异常不外溢。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOAK = REPO / "scripts" / "soak_test.py"


def _load_soak():
    """按文件路径加载 `scripts/soak_test.py`（`scripts/` 不是 Python 包）。

    `soak_test.py` 在导入时会把自己所属仓库插进 `sys.path` 并 import
    `backend.app.*`，那些都是纯模块级导入、不启动服务，所以可以安全加载。
    """
    assert SOAK.is_file(), f"找不到挂机脚本 {SOAK}"
    spec = importlib.util.spec_from_file_location("_pb_soak_test", SOAK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def soak():
    return _load_soak()


def _call(soak, path, **kw):
    """调用 `_write_checkpoint`，其余参数取合理默认值。"""
    args = {
        "hours": 1.5,
        "rounds": 90,
        "rounds_ok": 90,
        "rounds_bad": 0,
        "samples": [{"t": 0.0, "tag": "baseline", "rss": 66000000, "threads": 8,
                     "tasks": 3, "handles": 320, "thread_names": ["MainThread"]}],
        "failures": [],
        "total_hours": 24.0,
    }
    args.update(kw)
    soak._write_checkpoint(path, **args)
    return json.loads(Path(path).read_text(encoding="utf-8"))


class TestCheckpoint:
    def test_writes_running_status_not_verdict(self, soak, tmp_path) -> None:
        """检查点只记事实，不下判定 —— 样本不足时判定必然是误导。

        判据是"丢掉前 1/4 暖机后比较前后半程均值"，样本少于 6 个时
        `_net_growth` 直接返回 0；如果检查点也写个 `rss_growth: 0`，
        人看到会误以为"跑过了、没泄漏"。
        """
        d = _call(soak, tmp_path / "ck.json")

        assert d["status"] == "running", "检查点不该声称自己已经跑完"
        for verdict_key in ("rss_growth", "thread_growth", "handle_growth"):
            assert verdict_key not in d, (
                f"检查点里出现了判定字段 {verdict_key}：样本不足时这个数字"
                f"没有意义，会让人误读成结论"
            )
        assert d["rounds"] == 90
        assert d["total_hours"] == 24.0

    def test_progress_is_computable_and_honest(self, soak, tmp_path) -> None:
        """进度必须能从文件里直接读出来，而不是靠算。"""
        d = _call(soak, tmp_path / "ck.json", hours=6.0, total_hours=24.0)
        assert d["progress"] == "25.0%"

    def test_atomic_replace_leaves_no_tmp_file(self, soak, tmp_path) -> None:
        """必须用 `os.replace` 原子替换。

        若直接 `write_text` 到目标文件，进程正好在写到一半时被杀，
        就留下半截 JSON —— 之后"看一眼进度"反而 `JSONDecodeError`，
        比没有文件更糟（会让人以为文件坏了/脚本写错了）。
        """
        target = tmp_path / "ck.json"
        _call(soak, target)

        assert target.is_file()
        assert not target.with_suffix(target.suffix + ".tmp").exists(), (
            "临时文件没被清理：说明不是原子替换，或者是直接写目标文件"
        )
        # 再写一次也不能留下垃圾
        _call(soak, target, rounds=120)
        assert not target.with_suffix(target.suffix + ".tmp").exists()

    def test_failures_are_capped(self, soak, tmp_path) -> None:
        """失败列表只留最近 50 条。

        24 小时 × 每 60 秒 = 1440 轮；真出了系统性故障，每轮一条失败，
        不截断的话检查点会膨胀到几百 KB，且每 30 轮重写一遍。
        """
        many = [f"第 {i} 轮请求失败：timeout" for i in range(200)]
        d = _call(soak, tmp_path / "ck.json", failures=many)

        assert len(d["failures"]) == 50
        assert d["failures"][-1] == many[-1], "截断时把最新的丢掉了"
        assert d["failures"][0] == many[-50]

    def test_never_raises_on_unwritable_path(self, soak, tmp_path) -> None:
        """写失败一律吞掉 —— 挂机不该因为磁盘满/文件被占用而中断。

        这条尤其重要：检查点是在主循环里调的，抛异常会直接杀掉整个
        24 小时挂机，比"没有检查点"还糟。
        """
        # 目标是个目录 → 写入必然失败
        bad = tmp_path / "iam_a_dir"
        bad.mkdir()
        soak._write_checkpoint(
            bad, hours=1.0, rounds=30, rounds_ok=30, rounds_bad=0,
            samples=[], failures=[], total_hours=24.0,
        )  # 不抛异常即通过

    def test_no_report_path_is_a_noop(self, soak) -> None:
        """没传 `--report` 时不许写任何文件（也不许崩）。"""
        soak._write_checkpoint(
            None, hours=1.0, rounds=30, rounds_ok=30, rounds_bad=0,
            samples=[], failures=[], total_hours=24.0,
        )


class TestReportPathResolvedBeforeLoop:
    """报告路径必须在循环**之前**解析好。

    这是那个 bug 的另一半：原实现把 `Path(args.report)` 写在收尾处，
    于是循环体根本拿不到 `rp`，想加检查点也无从下手。
    """

    def test_report_path_arg_is_resolved_up_front(self, soak) -> None:
        import inspect

        src = inspect.getsource(soak.main)
        head, _, tail = src.partition("svc.start(")
        assert "report_path" in head, (
            "报告路径是在服务启动之后才解析的 —— 检查点在循环里就用不到它"
        )
        assert "report_path" in tail

    def test_loop_actually_writes_checkpoints(self, soak) -> None:
        import inspect

        src = inspect.getsource(soak.main)
        assert "_write_checkpoint(" in src, "主循环里没有落检查点"
        assert "% 30" in src, (
            "检查点不是按轮次周期落的：每轮都写会盖过被测逻辑"
        )


class TestPlateauGuard:
    """「还在上升」时必须拒绝判定，而不是报 FAIL。

    ## 这条是怎么来的

    2026-10-18 为了"快点看到结果"，用 `--hours 0.25 --interval 5` 跑了
    174 轮。脚本报 `=== FAIL（1 项） === 线程数净增 +6.1 超过上限`。
    但同一台机器跑满 10 小时的数据是 `+0.26 PASS`。

    查下去：本机 `os.cpu_count() = 20`，asyncio 默认执行器上限
    `min(32, 20+4) = 24`，线程数从 5 爬到 29 **用了约 3 小时**。
    15 分钟窗口里 `WARMUP_FRACTION=0.25` 只丢掉最初 3.75 分钟，
    后面测到的全是"正在建线程"，于是"净增"当然超限。

    **假失败比没有结论更糟**：它会让"缩短挂机"看起来像"发现了泄漏"，
    而泄漏本应只在长窗口里显形。所以判据必须带一个前提检查：
    指标在窗口末段还在单调上升 → 不判 PASS/FAIL，判 INCONCLUSIVE。
    """

    def test_rising_series_is_not_flat(self, soak) -> None:
        """持续上升的序列必须被判为"未趋平"。"""
        series = [5.0 + i * 0.5 for i in range(100)]
        flat, rel = soak._plateau_info(series)
        assert flat is False, "仍在上升却没被识别出来 —— 会报假 FAIL"
        assert rel > 0

    def test_flat_series_is_flat(self, soak) -> None:
        """爬到平台后不动的序列必须被判为"已趋平"。"""
        # 前 20 个爬升，之后恒定 —— 模拟真实的惰性建线程后稳定
        series = [5.0 + min(i, 20) * 0.5 for i in range(100)]
        flat, _rel = soak._plateau_info(series)
        assert flat is True, "已经趋平却判成仍在上升 —— 会把 PASS 降级成 N/A"

    def test_ten_hour_shape_passes_the_guard(self, soak) -> None:
        """复现 10 小时那份数据的形状（3h 爬平，之后恒定 6.5h）。

        这段必须"已趋平"，否则真实的长挂机会被误降级。
        """
        series = ([5.0 + i * 24 / 60 for i in range(60)]      # 0→3h 爬到 29
                  + [29.0] * 140)                              # 3→10h 恒定
        flat, _rel = soak._plateau_info(series)
        assert flat is True

    def test_fifteen_minute_shape_fails_the_guard(self, soak) -> None:
        """复现 15 分钟那份数据的形状（全程仍在爬升）。

        这段必须"未趋平"，否则假 FAIL 会重新出现。
        """
        series = [5.0 + i * 15 / 176 for i in range(176)]
        flat, _rel = soak._plateau_info(series)
        assert flat is False

    def test_short_series_is_not_flagged(self, soak) -> None:
        """样本太少时不去质疑 —— 交给 `_net_growth` 返回 None。"""
        flat, rel = soak._plateau_info([1.0, 2.0, 3.0])
        assert flat is True
        assert rel == 0.0

    def test_noise_below_absolute_threshold_is_flat(self, soak) -> None:
        """抖动小于 `PLATEAU_MIN_ABS` 的一律视为已趋平。

        线程数是整数，末端抖动 1 个线程不该把判定翻成 N/A。
        """
        series = [29.0] * 90 + [30.0] * 10     # 末段 +1，绝对差 1.0 不超阈值
        flat, _rel = soak._plateau_info(series)
        assert flat is True

    def test_inconclusive_lists_are_separate_from_failures(self, soak) -> None:
        """`inconclusive` 与 `failures` 必须是两个独立概念。

        合并它们就等于把"证据不足"当成"产品坏了"（或反过来）。
        这里钉住源码里两者是分开收集、分开输出的。
        """
        import inspect

        src = inspect.getsource(soak.main)
        assert "inconclusive: list[str] = []" in src
        assert '=== INCONCLUSIVE' in src
        # 证据不足时退出码是 2（与"环境问题"同义），不是 1（越界）
        assert "return 2" in src

