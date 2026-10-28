"""游客隔离回归（ISSUE-071 / 2026-10-28 五现象）。

## 这批用例在守什么

四条现象里有三条是"某个取数接口没按身份过滤"：缩略图能看、详情查无此图、
目录结构能翻。它们的共同点是**漏一处就是一个洞**，所以判据不能停在
"某个 handler 里写了 check"，得钉在**路由表**上：

- 主人专属的路径（目录树、删除、改名、移动）对游客必须是 `audience=owner`
  —— 中间件直接 404，走不到 handler；
- 游客能用的只读路径必须是 `audience=guest`，否则功能又没了（F-01 那次
  就是全 404）。

另外两条：
- 游客拿到的媒体响应必须 `no-store`，否则浏览器按 URL 缓存主人的缩略图，
  下次游客身份根本不发请求，直接回放（现象 2/4 的机械原因）。
- 目录名本身也是信息泄露（"全部备份""青甘环线"），所以 `/api/folders`
  对游客是关闭而不是"返回空目录树"。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.routers import build_router

REPO = Path(__file__).resolve().parents[2]

OWNER_ONLY = (
    ("GET", "/api/folders"),
    ("GET", "/api/folders/media"),
    ("POST", "/api/items/delete"),
    ("POST", "/api/items/move"),
    ("POST", "/api/items/abc/rename"),
    ("POST", "/api/browse/rescan"),
)

GUEST_ALLOWED = (
    ("GET", "/api/timeline"),
    ("GET", "/api/search"),
    ("GET", "/api/items/3"),
    ("GET", "/api/items/3/thumb"),
    ("GET", "/api/items/3/preview"),
    ("GET", "/api/items/3/raw"),
    ("GET", "/api/items/3/stream"),
)


@pytest.mark.parametrize("method, path", OWNER_ONLY)
def test_owner_only_paths_are_not_reachable_by_guests(method: str, path: str) -> None:
    assert build_router().audience_of(method, path) == "owner", (
        f"{method} {path} 对游客开放了 —— 目录/写操作必须只属于主人"
    )


@pytest.mark.parametrize("method, path", GUEST_ALLOWED)
def test_guest_read_paths_stay_open(method: str, path: str) -> None:
    assert build_router().audience_of(method, path) == "guest", (
        f"{method} {path} 被标成 owner 了 —— 游客连自己的照片都看不到（F-01）"
    )


def test_unknown_paths_fall_back_to_owner() -> None:
    """匹配不到的路由按 `owner` 处理：宁可多挡，不能默认放行。"""
    assert build_router().audience_of("GET", "/api/nope") == "owner"


def test_guest_media_responses_are_not_cacheable() -> None:
    """游客的媒体响应必须 `no-store`（现象 4：浏览器按 URL 缓存主人的缩略图）。"""
    src = (REPO / "backend/app/server.py").read_text(encoding="utf-8")
    assert 'reply.headers["cache-control"] = "no-store"' in src
    # 规则必须挂在"游客 + /api/items/"上，而不是无条件覆盖所有人的缓存头
    idx = src.index('reply.headers["cache-control"] = "no-store"')
    window = src[max(0, idx - 600): idx]
    assert 'client.get("guest")' in window
    assert 'ctx.path.startswith("/api/items/")' in window


def test_guest_entering_clears_the_previous_identity_state() -> None:
    """进入游客会话前必须清掉上一身份的列表数据（硬约束 6，不靠刷新）。"""
    src = (REPO / "frontend/src/App.vue").read_text(encoding="utf-8")
    idx = src.index("await redeemGuest(invite)")
    window = src[idx: idx + 900]
    assert "clearTimeline()" in window, "兑换成功后没有清时间线 store"
    assert "uploadStore.reset()" in window, "兑换成功后没有清上传队列"
