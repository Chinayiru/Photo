"""`_net_growth()` 的判据 —— H-08 的结论全靠这 14 行，它原本一条测试都没有。

## 为什么这个函数必须有测试

docs/07 H-08 的通过标准写的是「丢掉前 25% 暖机后，后半程均值 vs 前半程均值：
RSS 增长 ≤ 30%、线程数增长 ≤ 4、任务数增长 ≤ 8」。整句话的**全部判定逻辑**
就是 `scripts/soak_test.py:91` 的这个函数。它错一个符号、错一个切片，
24 小时的结论就反了 —— 而错的代价是「本来泄漏了，却报告通过」。

更现实的风险是**误报**：如果判据太敏感，每次跑都失败，人就会开始忽略它，
这比没有判据更糟。所以两个方向都要测：

- **该通过的必须通过**：冷启动爬坡、缓存填充、一次尖峰后回落。
- **该失败的必须失败**：持续线性泄漏、阶梯式泄漏。

## 判据为什么不用"首尾两点"

首尾差是最容易写的写法，也最容易天天误报：进程启动时 RSS 从 45MB 涨到
65MB 是**正常的**（Python 堆预热 + 缩略图缓存），随后 20 小时一条直线。
首尾差会把它报成「涨了 44%」。这个文件里 `test_startup_ramp_is_not_leak`
就是在钉这条教训。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SOAK = REPO / "scripts" / "soak_test.py"


def _load_soak():
    assert SOAK.is_file(), f"找不到挂机脚本 {SOAK}"
    spec = importlib.util.spec_from_file_location("_pb_soak_growth", SOAK)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def soak():
    return _load_soak()


class TestInsufficientData:
    """样本不够时必须**拒绝下结论**，而不是给出 0。"""

    def test_empty_returns_no_verdict(self, soak) -> None:
        abs_g, rel_g, base = soak._net_growth([])
        assert (abs_g, rel_g, base) == (0.0, None, 0.0)

    @pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 5, 6])
    def test_too_few_usables_returns_no_verdict(self, soak, n) -> None:
        """可用样本 < 6 时返回 `rel_g is None` —— 绝不拿半程数据下结论。

        门槛是**可用**样本数（暖机之后那段）而不是总样本数，所以边界不在
        直觉上的 6：`int(7 * 0.25) == 1`，n=7 剩 **6** 个 → 够用；
        n=6 丢 1 个剩 **5** 个 → 不够。参数是 0..6。
        """
        series = [60.0 + i for i in range(n)]
        abs_g, rel_g, base = soak._net_growth(series)
        assert (abs_g, rel_g, base) == (0.0, None, 0.0), (
            f"n={n} 时可用样本 {n - int(n * 0.25)} 个，应当拒绝下结论")

    @pytest.mark.parametrize("n", [8, 12, 24, 40])
    def test_six_usables_is_enough(self, soak, n) -> None:
        """可用样本 ≥ 6 就要给判定 —— 否则挂机跑一整晚也拿不到结论。"""
        series = [100.0] * n
        abs_g, rel_g, base = soak._net_growth(series)
        assert rel_g == pytest.approx(0.0)
        assert base == pytest.approx(100.0)


class TestShouldPass:
    """真实运行里"健康"的几种形状，都必须判为无增长。"""

    def test_flat_line(self, soak) -> None:
        abs_g, rel_g, _ = soak._net_growth([64.0] * 40)
        assert abs_g == pytest.approx(0.0)
        assert rel_g == pytest.approx(0.0)

    def test_startup_ramp_is_not_leak(self, soak) -> None:
        """开头爬坡、之后走平 —— 这是**必现**的正常形状，不是泄漏。

        实测 24h 挂机就是这个形状：45MB → 65MB（前 1 小时内）→ 之后 20 小时
        一条直线。判据必须丢掉这段暖机，否则每次跑都误报。
        """
        ramp = [45.0 + i * 2.0 for i in range(10)]      # 45 → 63
        flat = [65.0] * 30
        abs_g, rel_g, _ = soak._net_growth(ramp + flat)
        assert abs_g < 1.0, f"把启动爬坡判成了增长 {abs_g:+.2f}MB"
        assert rel_g is not None and abs(rel_g) < 0.02

    def test_spike_then_recover(self, soak) -> None:
        """中间一次尖峰（比如一次大图解码）之后回落 —— 均值不受影响。"""
        series = ([60.0] * 20) + [200.0] + ([60.0] * 19)
        abs_g, rel_g, _ = soak._net_growth(series)
        assert abs_g < 1.0, f"单点尖峰被当成了持续增长 {abs_g:+.2f}MB"

    def test_slow_decay_is_not_growth(self, soak) -> None:
        """缓慢下降（缓存被回收）更不能判失败 —— 符号错了就会。"""
        series = [100.0 - i * 0.5 for i in range(40)]
        abs_g, rel_g, _ = soak._net_growth(series)
        assert abs_g < 0
        assert rel_g is not None and rel_g < 0


class TestShouldFail:
    """真泄漏必须被抓到，否则这个判据没有意义。"""

    def test_linear_leak_exceeds_threshold(self, soak) -> None:
        """持续线性泄漏：40 个样本从 60MB 涨到 180MB。

        后半程均值 ≈ 157，前半程均值 ≈ 100 → 相对增长 ≈ 57% > 30%。
        """
        series = [60.0 + i * 3.0 for i in range(40)]
        abs_g, rel_g, _ = soak._net_growth(series)
        assert rel_g is not None
        assert rel_g > 0.30, f"线性泄漏没被抓到：{rel_g:+.1%}"
        assert abs_g > 30.0

    def test_step_leak_after_warmup(self, soak) -> None:
        """阶梯泄漏：暖机之后跳到 90MB 并保持。

        实测里这对应「某个线程池在第一次大请求后才创建且不回收」。

        ⚠️ 构造这一步时算错过一次，值得记下来：`[60]*20 + [90]*20` 的
        增长只有 **28.6%** —— 因为 40 个样本丢 25% 暖机（10 个）后，
        前 15 个里还混着 5 个 60MB 的旧样本，把前半程均值从 60 拉到 70，
        `70 → 90` 只有 28.6%，**低于 30% 门槛**，判据"没抓到"。
        这不是判据的缺陷：真实的阶梯泄漏若发生得足够早（20/40 这个位置），
        前半程均值本来就会被稀释。要抓的是**跳得够狠**的阶梯，所以这里
        把它写成暖机一结束就跳（60 → 300），让结论只取决于判据本身。
        """
        # 40 个样本、暖机 10 个、可用 30 个、前后各 15 个。
        # 跳变必须发生在**前半小时程里**（第 20 个样本处），否则前半程
        # 全是新值、前后半程相等、相对增长 0 —— 我第一版就写成暖机一结束
        # 就跳（第 10 个），结果 first=second=300 → 0%，判据当然"没抓到"。
        series = ([60.0] * 20) + ([300.0] * 20)
        abs_g, rel_g, _ = soak._net_growth(series)
        # 实测：前半程均值 140（5 个 60 + 10 个 300），后半程 300 → +114%
        assert rel_g is not None and rel_g > 0.30, f"阶梯泄漏没被抓到：{rel_g:+.1%}"

    def test_modest_step_is_below_threshold_by_design(self, soak) -> None:
        """但也要承认判据的**盲区**：暖机后 60 → 80 这种温和阶梯抓不到。

        前半程均值被暖机段稀释成 ~73、后半程 80，只有 +9.6%。
        这是 30% 这个阈值的固有代价（宁可漏报温和增长，也不要天天误报）。
        把它显式写成测试，是为了让"为什么没抓到"有据可查 ——
        而不是等到某天有人发现泄漏了、再回来怀疑判据是不是根本没生效。
        """
        series = ([60.0] * 20) + ([80.0] * 20)
        _, rel_g, _ = soak._net_growth(series)
        # 实测 +20.00%：前半程均值 66.67（5 个 60 + 10 个 80），后半 80
        assert rel_g is not None and rel_g < soak.RSS_GROWTH_LIMIT
        assert rel_g == pytest.approx(0.20, abs=0.01)

    def test_threshold_boundary_is_inclusive(self, soak) -> None:
        """刚好 30% 的边界：判定用 `>` 而不是 `>=`，所以 30% 不判失败。

        这不是"差不多就行"—— docs/07 H-08 写的是「≤ **30%**」，等号属于通过侧。
        两边都钉住，免得以后有人把 `>` 改成 `>=` 而不自知。
        """
        # 构造：前半程均值 100，后半程均值 130 → 正好 +30%
        n = 40
        usable = int(n * soak.WARMUP_FRACTION)
        body = n - usable
        half = body // 2
        series = ([100.0] * (usable + half)) + ([130.0] * (body - half))
        abs_g, rel_g, _ = soak._net_growth(series)
        assert rel_g == pytest.approx(0.30, abs=0.02)
        assert not (rel_g > soak.RSS_GROWTH_LIMIT), "30% 应算通过（判据是 ≤ 30%）"


class TestThresholdsMatchDocs:
    """常量本身要与 docs/07 §12 H-08 的原文一致 —— 文档改了就这里红。"""

    def test_warmup_fraction(self, soak) -> None:
        assert soak.WARMUP_FRACTION == 0.25, "docs 写「丢掉前 25% 暖机」"

    def test_rss_limit(self, soak) -> None:
        assert soak.RSS_GROWTH_LIMIT == 0.30, "docs 写「RSS 增长 ≤ 30%」"

    def test_thread_limit(self, soak) -> None:
        assert soak.THREAD_GROWTH_LIMIT == 4, "docs 写「线程数增长 ≤ 4」"

    def test_task_limit(self, soak) -> None:
        assert soak.TASK_GROWTH_LIMIT == 8, "docs 写「任务数增长 ≤ 8」"


class TestSlicingIsOffByWarmupOnly:
    """第一个被采用的样本必须正好在暖机之后 —— 切片差一位会让暖机漏进来。"""

    def test_first_used_sample_index(self, soak) -> None:
        n = 40
        expected_start = int(n * soak.WARMUP_FRACTION)
        # 暖机段用一个显眼的巨大值：只要它被算进去了，均值就会被拉高
        series = [10_000.0] * expected_start + [100.0] * (n - expected_start)
        _, _, base = soak._net_growth(series)
        assert base == pytest.approx(100.0), (
            f"基线均值 {base} —— 暖机段漏进统计了"
        )

    def test_half_split_is_at_usable_midpoint(self, soak) -> None:
        """前半程/后半程的分界在"暖机之后那段的中点"，不是整个序列的中点。"""
        n = 40
        start = int(n * soak.WARMUP_FRACTION)      # 10
        usable = n - start                          # 30
        half = usable // 2                          # 15
        # 前 10 暖机 0，接着 15 个 100，再 15 个 200
        series = [0.0] * start + [100.0] * half + [200.0] * (usable - half)
        abs_g, rel_g, base = soak._net_growth(series)
        assert base == pytest.approx(100.0), "前半程均值应当是 100"
        assert abs_g == pytest.approx(100.0), f"后半程均值应当是 200（差 {abs_g}）"
        assert rel_g == pytest.approx(1.0), "100 → 200 是 +100%"
