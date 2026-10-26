"""审计 F01：`enabled: false` 的根必须**从所有出口一起消失**。

## 这条闸门为什么必须写在"按 id 取根"这一层

管理员把某块共享关掉，意图是"这块照片不再能被远程看到"。真正容易漏掉的
不是列表，而是那几个"按 id 直取"的出口：

- `GET /api/items/{id}`         —— EXIF / 路径 / 拍摄地点
- `GET /api/items/{id}/thumb`   —— 缩略图**缓存文件还在磁盘上**
- `GET /api/items/{id}/preview` —— 同上，而且走 `immutable` 一年缓存
- `GET /api/items/{id}/raw`     —— 原图
- 以及不带 `root` 参数的时间线/搜索（= 查全库）

所以判据只有一条：**停用根的媒体，从这五条路上都拿不到**，而且状态码是
"不存在"（404 E1004），不是"没权限"（403）—— 403 会把"这台电脑上有哪些
根"这件事告诉对方。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config, RootDisabledError, UnknownRootError  # noqa: E402
from app.core import Ctx, Router  # noqa: E402
from app.db import Database  # noqa: E402
from app.errors import NotFound, to_app_error  # noqa: E402
from app.handlers import browse, system  # noqa: E402
from app.state import AppState  # noqa: E402


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

def _img(path: Path, *, taken: str = "2024:05:12 18:30:00") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (80, 60), (200, 100, 50))
    ex = Image.Exif()
    ex.get_ifd(0x8769)[0x9003] = taken
    im.save(path, "JPEG", exif=ex.tobytes())
    return path


def _mk_state(data: Path, library: Path, *, enabled: bool) -> AppState:
    """同一个 data 目录、同一个库，只是根的 `enabled` 不同。

    复用 data 目录是**故意的**：缩略图缓存写在 `data/thumbs/<size>/` 下，
    只有让它跨两个 AppState 存活，才能测出"停用后缓存文件还在、但不能再端出去"。
    """
    data.mkdir(exist_ok=True)
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


def _router() -> Router:
    r = Router()
    for method, pattern, handler in [
        ("GET", "/api/system/ping", system.ping),
        ("GET", "/api/timeline", browse.timeline),
        ("GET", "/api/items/{media_id}", browse.get_item),
        ("GET", "/api/items/{media_id}/thumb", browse.get_thumb),
        ("GET", "/api/items/{media_id}/preview", browse.get_preview),
        ("GET", "/api/items/{media_id}/raw", browse.get_raw),
        ("GET", "/api/search", browse.search),
    ]:
        r.add(method, pattern, handler)
    return r


def _ctx(st: AppState, path: str, *, query: dict | None = None,
         params: dict | None = None) -> Ctx:
    q: dict[str, list[str]] = {k: [str(v)] for k, v in (query or {}).items()}
    return Ctx(method="GET", path=path, query=q, headers={}, body=None,
               params=params or {}, app=st)


def _call(st: AppState, path: str, **kw):
    return asyncio.run(_router().dispatch(_ctx(st, path, **kw)))


def _body(reply) -> bytes:
    b = reply.body
    if b is None:
        return b""
    if isinstance(b, bytes):
        return b
    return asyncio.run(_drain(b))


async def _drain(agen) -> bytes:
    out = bytearray()
    async for chunk in agen:
        out.extend(chunk)
    return bytes(out)


@pytest.fixture()
def seeded(tmp_path: Path):
    """建库 → 索引两张照片 → 生成缩略图缓存 → 返回 (data, library, ids)。"""
    library = tmp_path / "lib"
    library.mkdir()
    _img(library / "2024" / "a.jpg", taken="2024:05:12 18:30:00")
    _img(library / "2024" / "b.jpg", taken="2024:05:13 09:00:00")

    data = tmp_path / "data"
    st = _mk_state(data, library, enabled=True)

    from app.indexer import Indexer

    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))
    ids = [int(r["id"]) for r in st.db.query("SELECT id FROM media ORDER BY id")]

    # 先把缩略图"烧进缓存"：这是停用后最容易漏掉的那条路。
    for mid in ids:
        r = _call(st, f"/api/items/{mid}/thumb", params={"media_id": mid},
                  query={"size": 320})
        assert r.status == 200, r.body
        assert len(_body(r)) > 0
    return data, library, ids


# --------------------------------------------------------------------------
# 1. 取根这一层：默认拒绝，例外要显式
# --------------------------------------------------------------------------

def test_root_by_id_rejects_disabled_root_by_default(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = Config({"roots": [{"id": "shared", "path": str(lib), "enabled": False}]},
                 path=tmp_path / "c.json", data_dir=tmp_path / "data")
    with pytest.raises(RootDisabledError):
        cfg.root_by_id("shared")
    # 未知 id 仍然走原来的 400 语义，两者不能被合成一个异常
    with pytest.raises(UnknownRootError):
        cfg.root_by_id("nope")


def test_allow_disabled_is_the_only_way_in(tmp_path: Path) -> None:
    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = Config({"roots": [{"id": "shared", "path": str(lib), "enabled": False}]},
                 path=tmp_path / "c.json", data_dir=tmp_path / "data")
    got = cfg.root_by_id("shared", allow_disabled=True)
    assert got.id == "shared"


def test_disabled_root_maps_to_404_not_403(tmp_path: Path) -> None:
    err = to_app_error(RootDisabledError("shared"))
    assert isinstance(err, NotFound)
    assert err.status == 404
    # 文案不能带根 id / 路径 —— 404 的目的就是"看不出存在过"
    assert "shared" not in (err.detail or "")


# --------------------------------------------------------------------------
# 2. 沙箱这一层：读、写、移动、删除全都拒
# --------------------------------------------------------------------------

@pytest.mark.parametrize("need", ["read", "write", "modify", "delete"])
def test_sandbox_refuses_every_need_on_disabled_root(tmp_path: Path, need: str) -> None:
    from app import sandbox

    lib = tmp_path / "lib"
    (lib / "2024").mkdir(parents=True)
    (lib / "2024" / "a.jpg").write_bytes(b"x")
    cfg = Config({"roots": [{"id": "shared", "path": str(lib),
                             "mode": "readwrite", "enabled": False}]},
                 path=tmp_path / "c.json", data_dir=tmp_path / "data")
    root = cfg.root_by_id("shared", allow_disabled=True)

    with pytest.raises(sandbox.SandboxError) as ei:
        sandbox.resolve(root, "2024/a.jpg", need, cfg=cfg)
    assert ei.value.rule == "root_disabled"
    assert ei.value.spec.code == "E1004"


def test_assert_writable_also_sees_the_disabled_flag(tmp_path: Path) -> None:
    from app import sandbox

    lib = tmp_path / "lib"
    lib.mkdir()
    cfg = Config({"roots": [{"id": "shared", "path": str(lib), "enabled": False}]},
                 path=tmp_path / "c.json", data_dir=tmp_path / "data")
    root = cfg.root_by_id("shared", allow_disabled=True)
    with pytest.raises(sandbox.SandboxError) as ei:
        sandbox.assert_writable(root, "write")
    assert ei.value.rule == "root_disabled"


# --------------------------------------------------------------------------
# 3. 时间线 / 搜索：不带 root 时不能漏
# --------------------------------------------------------------------------

def test_timeline_hides_photos_of_disabled_root(seeded) -> None:
    data, library, ids = seeded
    st = _mk_state(data, library, enabled=False)
    r = _call(st, "/api/timeline")
    assert r.status == 200
    assert _body_json(r)["items"] == [], "停用根的照片不能出现在时间线上"


def test_search_hides_photos_of_disabled_root(seeded) -> None:
    data, library, _ids = seeded
    st = _mk_state(data, library, enabled=False)
    r = _call(st, "/api/search", query={"q": "a.jpg"})
    assert r.status == 200
    assert _body_json(r)["items"] == []


def test_naming_a_disabled_root_is_404(seeded) -> None:
    data, library, _ids = seeded
    st = _mk_state(data, library, enabled=False)
    for path, query in (("/api/timeline", {"root": "library"}),
                        ("/api/search", {"q": "a", "root": "library"})):
        r = _call(st, path, query=query)
        assert r.status == 404, (path, r.status)
        assert _body_json(r)["error"]["code"] == "E1004"


def test_timeline_where_empty_enabled_set_returns_no_rows(seeded) -> None:
    """所有根都停了 → `AND 0`，而不是拼出非法 SQL `IN ()`。"""
    data, _library, _ids = seeded
    st = _mk_state(data, _library, enabled=False)
    from app.handlers.common import timeline_where

    where, params = timeline_where(enabled_root_ids=[])
    assert " AND 0" in where and params == []


# --------------------------------------------------------------------------
# 4. 按 id 直取的四条路：详情 / 缩略图 / 预览 / 原图
# --------------------------------------------------------------------------

@pytest.mark.parametrize("suffix", ["", "/thumb", "/preview", "/raw"])
def test_by_id_endpoints_404_after_disable(seeded, suffix: str) -> None:
    data, library, ids = seeded
    mid = ids[0]
    st = _mk_state(data, library, enabled=False)
    r = _call(st, f"/api/items/{mid}{suffix}", params={"media_id": mid},
              query={"size": 320} if suffix == "/thumb" else None)
    assert r.status == 404, (suffix, r.status)
    assert _body_json(r)["error"]["code"] == "E1004"


def test_thumb_cache_on_disk_is_not_enough(seeded, tmp_path: Path) -> None:
    """缓存文件明明在磁盘上，也必须 404 —— 否则"停用前看过的图"永久可读。"""
    data, library, ids = seeded
    st = _mk_state(data, library, enabled=False)
    from app.thumbs import thumb_path

    row = st.db.query_one("SELECT root_id, rel_path FROM media WHERE id = ?", (ids[0],))
    cached = thumb_path(st.cfg, row["root_id"], row["rel_path"], 320)
    assert cached.exists(), "前提：缓存文件确实还在"      # 夹具已经生成过
    r = _call(st, f"/api/items/{ids[0]}/thumb", params={"media_id": ids[0]},
              query={"size": 320})
    assert r.status == 404


def _body_json(reply) -> dict:
    import json

    raw = _body(reply)
    assert raw, "响应体不能为空"
    return json.loads(raw)
