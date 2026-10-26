"""「按文件夹」浏览（ISSUE-060 A 项）的接口契约。

## 这批用例在守什么

`GET /api/folders/media` 是本轮新增的唯一"按目录取内容"入口。它有三个
容易写坏的地方，正好各一条用例钉住：

1. **只列本层**：混进子目录的条目，"按文件夹分类"就退化成第二个「全部图片」，
   用户点进"青甘环线"却看到整个根下的所有照片。
2. **空目录不是错误**：空目录 / 只有子目录没有直接文件，都必须 200 + 空列表。
   报 400 或 500 的话前端只能白屏，而"这个文件夹是空的"和"出错了"
   是完全不同的两件事。
3. **目录参数必须过沙箱**：`dir` 直接进 SQL 前缀。不校验就留下一条绕过
   sandbox 的路径参数（硬约束 1），而口子一旦存在就没人再看它。

另外验证两件"看不见但会疼"的事：游标翻页不重复不丢项、以及
按目录分页真的走 `idx_media_root_dir`（硬约束 9 要求 EXPLAIN 确认）。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.core import Ctx, Router  # noqa: E402
from app.db import Database  # noqa: E402
from app.handlers import browse, system  # noqa: E402
from app.state import AppState  # noqa: E402


# ------------------------------------------------------------------ 夹具

def _state(tmp_path: Path, *, enabled: bool = True) -> AppState:
    library = tmp_path / "lib"
    library.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    cfg = Config({
        "app": {"mode": "readwrite"},
        "roots": [{"id": "library", "path": str(library), "mode": "readwrite",
                   "enabled": enabled}],
        "thumbs": {"sizes": [320, 1600]},
        "auth": {"allow_delete": True, "delete_to_recycle_bin": True},
        "index": {"batch_size": 50},
    }, path=data / "config.json", data_dir=data)
    db = Database(cfg.db_path)
    db.init()
    return AppState(cfg, db)


def _touch(st: AppState, rel: str) -> None:
    """在磁盘上真的建出这个文件（空文件就够：handler 只查库，但不存在的
    目录会被 `_dir_param` 判成 400，所以夹具必须让目录真的存在）。"""
    base = Path(st.cfg.roots[0].path)
    target = base / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(b"")


def _add(st: AppState, rel: str, *, taken_at: float, kind: str = "image") -> int:
    _touch(st, rel)
    name = rel.rsplit("/", 1)[-1]
    cur = st.db.execute(
        "INSERT INTO media(root_id, rel_path, file_name, ext, kind, size, mtime,"
        " taken_at, thumb_state, missing) VALUES(?,?,?,?,?,?,?,?,?,0)",
        ("library", rel, name, name.rsplit(".", 1)[-1].lower(), kind,
         1234, taken_at, taken_at, "ok"),
    )
    return int(cur.lastrowid or 0)


def _ctx(st: AppState, method: str, path: str, *, query: dict | None = None) -> Ctx:
    q: dict[str, list[str]] = {}
    for k, v in (query or {}).items():
        q[k] = v if isinstance(v, list) else [str(v)]
    return Ctx(method=method, path=path, query=q, headers={}, body=None,
               params={}, app=st)


def _call(st: AppState, method: str, path: str, **kw):
    """走完整路由表（含参数抽取），和线上同一条路径。"""
    r = Router()
    r.add("GET", "/api/folders", browse.folders)
    r.add("GET", "/api/folders/media", browse.folder_media)
    r.add("GET", "/api/search", browse.search)
    r.add("GET", "/api/timeline", browse.timeline)
    ctx = _ctx(st, method, path, **kw)
    return asyncio.run(r.dispatch(ctx))


def _body(reply) -> dict:
    import json
    return json.loads(reply.body)


def _code(reply) -> str:
    """错误码在 `{"error": {...}}` 里（docs/04 §1.6）。"""
    return _body(reply)["error"]["code"]


def _seed(st: AppState, library: Path | None = None) -> dict[str, int]:
    """根层 1 张、`dir/` 里 3 张、`dir/sub/` 里 1 张、`other/` 空。"""
    base = library or Path(st.cfg.roots[0].path)
    (base / "other").mkdir(exist_ok=True)
    return {
        "root": _add(st, "a.jpg", taken_at=100),
        "dir1": _add(st, "dir/b.jpg", taken_at=300),
        "dir2": _add(st, "dir/c.jpg", taken_at=200),
        "dir3": _add(st, "dir/d.jpg", taken_at=100),
        "sub": _add(st, "dir/sub/e.jpg", taken_at=400),
    }


# ------------------------------------------------------------------ 只列本层

def test_folder_media_lists_only_the_requested_level(tmp_path) -> None:
    st = _state(tmp_path)
    ids = _seed(st)
    reply = _call(st, "GET", "/api/folders/media", query={"dir": "dir"})
    assert reply.status == 200
    body = _body(reply)
    got = [i["id"] for i in body["items"]]
    assert got == [ids["dir1"], ids["dir2"], ids["dir3"]], \
        "子目录里的 e.jpg 不该出现在 dir 这一层"
    assert body["total"] == 3
    assert body["dir"] == "dir"


def test_root_level_excludes_every_subdirectory(tmp_path) -> None:
    st = _state(tmp_path)
    ids = _seed(st)
    body = _body(_call(st, "GET", "/api/folders/media"))
    assert [i["id"] for i in body["items"]] == [ids["root"]]
    assert body["total"] == 1


def test_folder_media_orders_exactly_like_the_timeline(tmp_path) -> None:
    """两个视图顺序必须一致（D12），否则切来切去会失去方向感。"""
    st = _state(tmp_path)
    _seed(st)
    folder = [i["id"] for i in _body(
        _call(st, "GET", "/api/folders/media", query={"dir": "dir"}))["items"]]
    timeline = [i["id"] for i in _body(_call(st, "GET", "/api/timeline"))["items"]
                if i["dir"] == "dir"]
    assert folder == timeline


# ------------------------------------------------------------------ 空与非法

def test_empty_directory_is_200_with_an_empty_list(tmp_path) -> None:
    st = _state(tmp_path)
    _seed(st)
    reply = _call(st, "GET", "/api/folders/media", query={"dir": "other"})
    assert reply.status == 200, "空目录不是错误"
    body = _body(reply)
    assert body["items"] == [] and body["total"] == 0 and body["has_more"] is False


def test_directory_with_only_subdirectories_is_empty_not_broken(tmp_path) -> None:
    st = _state(tmp_path)
    _add(st, "only/deep/f.jpg", taken_at=10)
    body = _body(_call(st, "GET", "/api/folders/media", query={"dir": "only"}))
    assert body["items"] == [] and body["total"] == 0


@pytest.mark.parametrize("bad", ["../..", "dir/../../etc", "/abs", ".."])
def test_dotdot_in_dir_is_rejected(tmp_path, bad: str) -> None:
    st = _state(tmp_path)
    _seed(st)
    reply = _call(st, "GET", "/api/folders/media", query={"dir": bad})
    assert reply.status == 400
    assert _code(reply) == "E1001"


def test_unknown_directory_is_rejected_without_leaking_paths(tmp_path) -> None:
    st = _state(tmp_path)
    _seed(st)
    reply = _call(st, "GET", "/api/folders/media", query={"dir": "nope/deeper"})
    assert reply.status == 400
    body = _body(reply)
    assert _code(reply) == "E1001"
    assert str(tmp_path) not in str(body), "错误响应不许含绝对路径"


# ------------------------------------------------------------------ 游标分页

def test_cursor_paging_covers_every_row_once(tmp_path) -> None:
    st = _state(tmp_path)
    for i in range(7):
        _add(st, f"p/{i}.jpg", taken_at=100 + i)
    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        q = {"dir": "p", "limit": 2}
        if cursor:
            q["cursor"] = cursor
        body = _body(_call(st, "GET", "/api/folders/media", query=q))
        seen.extend(i["id"] for i in body["items"])
        pages += 1
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
        assert cursor, "has_more=true 时必须给游标"
        assert pages < 10
    assert len(seen) == 7 and len(set(seen)) == 7, "翻完不能重复也不能丢"
    assert pages == 4                      # 2+2+2+1


def test_new_photo_mid_paging_does_not_shift_the_window(tmp_path) -> None:
    """翻页中途插入新照片（索引器在跑）不能让已翻过的页错位。"""
    st = _state(tmp_path)
    for i in range(4):
        _add(st, f"q/{i}.jpg", taken_at=100 + i)
    first = _body(_call(st, "GET", "/api/folders/media",
                        query={"dir": "q", "limit": 2}))
    _add(st, "q/late.jpg", taken_at=999)   # 比所有都新
    second = _body(_call(st, "GET", "/api/folders/media",
                         query={"dir": "q", "limit": 2,
                                "cursor": first["next_cursor"]}))
    assert not ({i["id"] for i in first["items"]} & {i["id"] for i in second["items"]})


# ------------------------------------------------------------------ 目录树计数

def test_folders_report_direct_and_recursive_counts_separately(tmp_path) -> None:
    """D8：本层张数与递归张数必须分开给，不能只给一个。"""
    st = _state(tmp_path)
    _seed(st)
    body = _body(_call(st, "GET", "/api/folders"))
    d = {f["path"]: f for f in body["folders"]}
    assert d["dir"]["direct"] == 3, "dir 里直接放了 3 张"
    assert d["dir"]["count"] == 4, "含 dir/sub 里的那张共 4 张"
    assert d["dir"]["has_children"] is True


def test_folders_report_current_level_file_count(tmp_path) -> None:
    st = _state(tmp_path)
    _seed(st)
    body = _body(_call(st, "GET", "/api/folders"))
    assert body["files"] == 1, "根层直接放了 a.jpg"
    assert body["total"] == 2, "根层有两个子目录 dir/ 与 other/"


# ------------------------------------------------------------------ 索引

def test_directory_paging_uses_the_directory_index(tmp_path) -> None:
    """硬约束 9：新增查询必须 EXPLAIN QUERY PLAN 确认走索引。"""
    st = _state(tmp_path)
    _seed(st)
    where, params = _folder_where(st, "dir")
    plan = st.db.query(
        "EXPLAIN QUERY PLAN SELECT id FROM media" + where
        + " ORDER BY taken_at DESC, id DESC LIMIT 51", params)
    text = " | ".join(r["detail"] for r in plan)
    assert "idx_media_root_dir" in text, f"按目录分页没走索引：{text}"
    assert "SCAN media" not in text, f"退化成全表扫描：{text}"


def _folder_where(st: AppState, base: str):
    from app.handlers import common
    where, params = common.timeline_where(
        root_id="library",
        allow_video=bool(st.cfg.video_enabled),
        enabled_root_ids=[r.id for r in st.cfg.enabled_roots],
    )
    where += browse._DIRECT_ONLY if not base else browse._DIRECT_ONLY_NESTED  # noqa: SLF001
    if base:
        params.extend([browse._esc(base) + "/%", browse._esc(base) + "/%/%"])  # noqa: SLF001
    return where, params


# ------------------------------------------------------------------ 搜索按目录

def test_search_dir_filter_is_recursive(tmp_path) -> None:
    st = _state(tmp_path)
    _seed(st)
    body = _body(_call(st, "GET", "/api/search",
                       query={"dir": "dir", "q": "e"}))
    assert [i["name"] for i in body["items"]] == ["e.jpg"]
    assert body["items"][0]["dir"] == "dir/sub"


def test_search_dir_is_sandbox_checked(tmp_path) -> None:
    st = _state(tmp_path)
    _seed(st)
    reply = _call(st, "GET", "/api/search", query={"dir": "../../..", "q": "a"})
    assert reply.status == 400 and _code(reply) == "E1001"


def test_folder_media_requires_a_known_root(tmp_path) -> None:
    st = _state(tmp_path)
    reply = _call(st, "GET", "/api/folders/media", query={"root": "ghost"})
    assert reply.status == 400
    assert _code(reply) == "E1001"


def test_folder_media_is_reachable_through_the_public_system_path() -> None:
    """新端点必须注册在唯一路由表里（硬约束 5）。"""
    from app.routers import build_router
    paths = {(r.method, r.pattern) for r in build_router().routes}
    assert ("GET", "/api/folders/media") in paths


def test_folder_media_hides_disabled_roots(tmp_path) -> None:
    """停用的根一块都不能露（审计 F01 的不变量）。"""
    st = _state(tmp_path, enabled=False)
    _seed(st, tmp_path / "lib")
    reply = _call(st, "GET", "/api/folders/media")
    assert reply.status == 404
    assert _code(reply) == "E1004"


def test_folder_media_skips_missing_rows(tmp_path) -> None:
    """被删掉的文件不能出现在目录视图里（点了就 404 更糟）。"""
    st = _state(tmp_path)
    ids = _seed(st)
    st.db.execute("UPDATE media SET missing = 1 WHERE id = ?", (ids["dir1"],))
    body = _body(_call(st, "GET", "/api/folders/media", query={"dir": "dir"}))
    assert ids["dir1"] not in [i["id"] for i in body["items"]]
    assert body["total"] == 2


def test_server_time_is_present_for_clock_skew(tmp_path) -> None:
    st = _state(tmp_path)
    _seed(st)
    body = _body(_call(st, "GET", "/api/folders/media", query={"dir": "dir"}))
    assert abs(body["server_time"] - time.time()) < 60
    assert isinstance(body["tz_offset_minutes"], int)

# --------------------------------------------------- 参数名与计数口径

def test_folders_accepts_dir_alias_like_path(tmp_path) -> None:
    """`?dir=` 与 `?path=` 必须等价。

    两个端点（目录列表 / 层内媒体）在描述"同一层"时参数名不一致，前端一定
    会写错一个 —— 实测踩过：前端传 `dir`，目录列表只读 `path`，于是每一层
    都返回根层目录，点进去纹丝不动。
    """
    st = _state(tmp_path)
    _seed(st)
    a = _body(_call(st, "GET", "/api/folders", query={"path": "dir"}))
    b = _body(_call(st, "GET", "/api/folders", query={"dir": "dir"}))
    assert a["path"] == b["path"] == "dir"
    assert [f["path"] for f in a["folders"]] == [f["path"] for f in b["folders"]]


def test_folders_reports_direct_and_recursive_counts(tmp_path) -> None:
    """`count` 递归、`direct` 直属、`files` 本层直属总数。

    三个数字回答三个不同问题："这文件夹一共多少张"、"点进去这一层有多少张"、
    "现在这一层有多少张"。少一个就必须在 UI 上猜口径。
    """
    st = _state(tmp_path)
    _seed(st)
    body = _body(_call(st, "GET", "/api/folders"))
    assert body["files"] == 1, "根层直属 1 张（a.jpg）"
    dirs = {f["path"]: f for f in body["folders"]}
    assert dirs["dir"]["direct"] == 3, "dir/ 直属 3 张"
    assert dirs["dir"]["count"] == 4, "dir/ 递归 4 张（含 sub/）"
    assert dirs["dir"]["count"] > dirs["dir"]["direct"]
