"""AC-2.10 修订后的门禁：云端通路不许自动预取原图（`docs/11` §8.55）。

判据放在后端测试里，是因为仓库没有 CI workflow，`pytest backend/tests`
就是实际的 CI（见 `docs/11` §8.51 的同一决定）。断言的是**源码结构**：
这类"顺手优化回去"的改动几乎没有运行时痕迹 —— 它只是让弱网变慢一点，
端到端用例不抓包就看不出来。
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
VIEWER = REPO / "frontend" / "src" / "stores" / "viewer.ts"
VIEW = REPO / "frontend" / "src" / "views" / "ViewerView.vue"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_prefetch_is_disabled_on_cloud_transport() -> None:
    """`prefetchWorthIt()` 必须在任何 effectiveType 判断之前就挡掉云端。"""
    src = _text(VIEWER)
    body = src.split("function prefetchWorthIt", 1)[1].split("\n}", 1)[0]
    assert 'currentVia() === "cloud"' in body, (
        "云端通路必须提前返回 false —— effectiveType 描述的是手机最后一跳，"
        "不是中继出口的 1 Mbps（docs/11 §8.55）"
    )
    # 顺序即语义：云端判断必须在 connection 读取之前。
    assert body.index('currentVia() === "cloud"') < body.index("connection"), (
        "云端判据要放在最前面：否则 saveData/effectiveType 分支会先执行"
    )
    assert 'import { currentVia' in src or "currentVia," in src


def test_viewer_requires_explicit_permission_on_cloud() -> None:
    """云端放大到 1.5× 时只给入口，不自动拉原图。"""
    src = _text(VIEW)
    assert "const askedRaw = ref(false)" in src
    assert "needRawPermit" in src
    assert "askedRaw = true" in src or "@click=\"askedRaw = true\"" in src, (
        "必须有一个用户可点的入口把许可置为 true"
    )
    # 直连仍然自动，不能被"统一改成点击"顺手砍掉。
    assert 'currentVia() === "lan"' in src, "局域网直连必须保持放大即换原图"
    # 切图重置许可：否则下一张会继承上一张的许可。
    assert "askedRaw.value = false" in src
