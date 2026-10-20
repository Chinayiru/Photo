"""2026-10-20 黑盒白盒复测里 F13/F14/F16/F18/F19 与 F5 残留的回归用例。

这些用例的共同点：**修之前它们都是红的**，而每一条的失败信息都能指向
一个用户真的会遇到的场景（不是"某行代码长什么样"）。

按缺陷分组：

* F13  `GET /api/uploads/{id}` 对已结束的会话做磁盘对账 → 把 `received`
       写回 0 → 客户端按 0 重传 → `409 E1012` 不可重试。
* F14  写入口（move / 上传）不认扫描排除目录 → 照片放进去，下次扫描
       标 missing，而 missing 行的 move / rename 一律 `E1004`，
       界面上"照片消失且移不回来"。
* F16  `search?q=<NUL>` 的 LIKE 模式在 NUL 处截断成 `"%"` → 200 全表。
* F18  上传会话从不续期，24 小时是"从 init 起算的绝对期限"。
* F19  `name` 含路径分隔符照收（文档说 400）；GET 响应缺 `media_id` 等字段。
* F5 残留  `/preview` 对已删除的照片仍给缓存一年 + 304（`/thumb` 修了它没修）。
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import sandbox, uploads  # noqa: E402
from app.config import Config  # noqa: E402
from app.core import Router  # noqa: E402
from app.db import Database  # noqa: E402
from app.indexer import Indexer  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.handlers import browse, uploads as h_uploads  # noqa: E402
from app.state import AppState  # noqa: E402
from app.thumbs import thumb_path  # noqa: E402

JPEG_HEAD = b"\xff\xd8\xff\xe0"


# --------------------------------------------------------------------------
# 夹具（与 test_handlers.py / test_uploads.py 同形，但两者各自私有一份，
# 这里独立再造一遍 —— 跨文件 import 夹具会让"改一处红一片"）
# --------------------------------------------------------------------------

def _img(path: Path, size=(80, 60), color=(200, 100, 50)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, "JPEG")
    return path


def _cfg(tmp_path: Path, library: Path, *, mode: str = "readwrite",
         exclude_dirs: list[str] | None = None, **upload) -> Config:
    up = {
        "subdir": "手机上传",
        "chunk_mb_lan": 1,
        "max_file_mb": 4,
        "session_ttl_hours": 24,
        "dedup": True,
    }
    up.update(upload)
    root: dict = {"id": "library", "path": str(library), "mode": mode,
                  "enabled": True, "recursive": True}
    if exclude_dirs is not None:
        root["exclude_dirs"] = exclude_dirs
    return Config(
        {"roots": [root], "thumbs": {"sizes": [320, 1600]}, "upload": up},
        path=tmp_path / "config.json",
        data_dir=tmp_path / "data",
    )


def _db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    d.init()
    return d


def _state(cfg: Config, db: Database, *, indexer=None) -> AppState:
    # `indexer` 默认空：大多数用例只验 handler 层，不需要索引。
    # 但 F19 那条要验 `media_id`，而 media_id 只有索引器会写。
    return AppState(cfg, db, indexer=indexer)


def _router() -> Router:
    r = Router()
    for method, pattern, handler in [
        ("GET", "/api/items/{media_id}/thumb", browse.get_thumb),
        ("GET", "/api/items/{media_id}/preview", browse.get_preview),
        ("GET", "/api/search", browse.search),
        ("POST", "/api/items/move", browse.move_items),
        ("POST", "/api/items/rename", browse.rename_item),
        ("POST", "/api/uploads/init", h_uploads.init_upload),
        ("PATCH", "/api/uploads/{upload_id}", h_uploads.patch_upload),
        ("GET", "/api/uploads/{upload_id}", h_uploads.get_upload),
    ]:
        r.add(method, pattern, handler)
    return r


def _ctx(st: AppState, method: str, path: str, *, query: dict | None = None,
         body: bytes | None = None, params: dict | None = None,
         headers: dict | None = None):
    from app.core import Ctx

    q: dict[str, list[str]] = {}
    for k, v in (query or {}).items():
        q[k] = v if isinstance(v, list) else [str(v)]

    async def _stream():
        if body is not None:
            yield body

    # `headers` 不能不接：F5 那条要验的就是 `If-None-Match`
    # 命中之后"已删除的照片还能不能拿 304"。
    return Ctx(method=method, path=path, query=q, headers=headers or {},
               body=_stream() if body is not None else None,
               params=params or {}, app=st)


def _call(st: AppState, method: str, path: str, **kw):
    return asyncio.run(_router().dispatch(_ctx(st, method, path, **kw)))


def _json_body(obj) -> bytes:
    import json

    return json.dumps(obj).encode("utf-8")


def _body(reply) -> bytes:
    b = reply.body
    if b is None:
        return b""
    if isinstance(b, bytes):
        return b
    chunks = []

    async def _drain():
        async for c in b:
            chunks.append(c)

    asyncio.run(_drain())
    return b"".join(chunks)


def _json(reply):
    import json

    return json.loads(_body(reply))


def _scan(st: AppState) -> None:
    from app.indexer import Indexer

    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))


# ==========================================================================
# F13 —— 已结束的会话，查询接口不该有写副作用
# ==========================================================================

def test_completed_session_status_keeps_offset_and_reports_media_id(tmp_path):
    """F13：`complete` 之后 `GET` 不能把 `offset` 写回 0，且必须给出 `media_id`。

    `complete` 归档后 `.part` 被删掉（磁盘 0 字节）。早先 `_reconcile_offset`
    对**任何**会话都以磁盘为准，于是把 DB 里的 `received` 写成 0：
    客户端读到 `offset=0, complete=false`，按 0 重传拿 `409 E1012`
    （`retryable=false`），界面报"上传失败" —— 而照片其实早已入库。
    """
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)
    # ★ 必须带 indexer：生产里 `AppState.indexer` 非空，`media_id`
    # 就是索引器写回来的。装置不带它时 `media_id` 恒为 None，
    # 那条断言变成在验一个装置缺失，而不是在验 F19。
    st = _state(cfg, db, indexer=Indexer(cfg, db))

    data = JPEG_HEAD + b"x" * 200
    sess = uploads.create_session(db, cfg, name="a.jpg", size=len(data))
    uid = sess["upload_id"]
    uploads.append_chunk(db, cfg, uid, start=0, end=len(data) - 1,
                         total=len(data), data=data)
    # ★ indexer 必须显式传：`complete_session` 的签名里它默认是 None，
    # 而生产路径（`handlers/uploads.py:182`）一定会把 `st.indexer` 传进去。
    done = uploads.complete_session(
        db, cfg, uid, indexer=Indexer(cfg, db), thumbs=None)
    assert done["media_id"] is not None, "索引器在场时必须拿到 media_id"

    # 磁盘上临时文件确实没了 —— 这是 F13 的前提，必须先证明它成立。
    assert not Path(uploads.get_session(db, uid)["temp_path"]).exists()

    r = _call(st, "GET", f"/api/uploads/{uid}", params={"upload_id": uid})
    assert r.status == 200
    body = _json(r)
    assert body["offset"] == len(data), (
        f"已完成的会话 offset 必须保持 total_size，不能因为磁盘 0 字节就写回 0"
        f"（拿到 {body['offset']}）")
    assert body["complete"] is True
    assert body["status"] == "completed"
    assert body["media_id"] == done["media_id"], "必须给出 media_id（F19）"
    assert body["final_rel"] == done["rel_path"]
    assert body["declared_name"] == "a.jpg"
    assert body["dedup"] is False

    # 再查一次：查询接口反复调用不能把 DB 改坏。
    r2 = _call(st, "GET", f"/api/uploads/{uid}", params={"upload_id": uid})
    assert _json(r2)["offset"] == len(data)


def test_open_session_status_still_reconciles_with_disk(tmp_path):
    """反向对照：**未结束**的会话仍要以磁盘为准（F13 不能把对账一起废掉）。

    DB 偏大是崩溃的典型症状（写完文件没来得及更新 DB）。不按磁盘修正的话，
    客户端会跳过一段，文件中间留空洞 —— 照片上表现为灰条，sha256 永远对不上。
    """
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)
    st = _state(cfg, db)

    data = JPEG_HEAD + b"y" * 300
    sess = uploads.create_session(db, cfg, name="b.jpg", size=len(data))
    uid = sess["upload_id"]
    uploads.append_chunk(db, cfg, uid, start=0, end=99, total=len(data),
                         data=data[:100])
    # 人为把 DB 调大，模拟"DB 说 250、磁盘只有 100"。
    db.execute("UPDATE upload_sessions SET received = ? WHERE id = ?", (250, uid))

    r = _call(st, "GET", f"/api/uploads/{uid}", params={"upload_id": uid})
    assert r.status == 200
    assert _json(r)["offset"] == 100, "open 会话必须以磁盘为准（DB 偏大会跳过一段）"


# ==========================================================================
# F18 —— 每次成功分片都续期
# ==========================================================================

def test_each_chunk_refreshes_expires_at(tmp_path):
    """F18：分片成功必须把 `expires_at` 续成 `now + ttl`，且带上下界。

    早先只写 `received`，24 小时是**从 init 起算的绝对期限**：跨天续传
    拿到 `409 E1012` 不可重试，前端 `releaseSession()`，重试从 0 开始，
    已传的字节全部白费 —— 而 `docs/04:3405-3409` 明写"每次成功分片都刷新"。
    """
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library, session_ttl_hours=12)
    db = _db(tmp_path)
    st = _state(cfg, db)

    total = 2000
    sess = uploads.create_session(db, cfg, name="c.jpg", size=total)
    uid = sess["upload_id"]
    first_exp = float(sess["expires_at"])
    created = float(uploads.get_session(db, uid)["created_at"])

    # 把 created_at 推到"11 小时前创建"，模拟跨夜；但 expires_at 仍在未来
    # （ttl=12h → created_at+ttl = now+1h）。
    #
    # ⚠️ 不能把 expires_at 也推到过去：`get_session` 对过期会话会直接抛
    # `E1012`，于是这条变成在验"过期会报错"，而不是在验续期公式。
    past = time.time() - 11 * 3600
    db.execute(
        "UPDATE upload_sessions SET created_at = ?, expires_at = ? WHERE id = ?",
        (past, past + 12 * 3600, uid),
    )
    assert float(uploads.get_session(db, uid)["expires_at"]) > time.time()
    assert float(uploads.get_session(db, uid)["expires_at"]) < time.time() + 3600.5

    before = time.time()
    uploads.append_chunk(db, cfg, uid, start=0, end=99, total=total,
                         data=JPEG_HEAD + b"z" * 96)
    after = time.time()

    new_exp = float(uploads.get_session(db, uid)["expires_at"])
    ttl = 12 * 3600.0
    assert new_exp >= before + ttl - 1, (
        f"分片成功必须续期到 now+ttl（now≈{before:.0f}, ttl={ttl:.0f}, 得 {new_exp:.0f}）")
    assert new_exp <= after + ttl + 1
    # 下界：不能比"创建时算出的过期"更早（防时钟被往回拨）。
    assert new_exp >= past + ttl
    assert new_exp > first_exp - 1 or new_exp > past, "续期后必须晚于原来的过期时间"

    # PATCH 响应里的 expires_at/chunk_size 由下一条专门验（它会带上
    # Content-Range）。这里不重复——早先那个“顺手验一下”的 PATCH 没带
    # Content-Range，恒为 400，写成 `if r.status == 206:` 就是一条永远不会执行的断言。


def test_patch_response_carries_expires_at(tmp_path):
    """F18/F19：`PATCH` 的成功响应里必须有 `expires_at` 与 `chunk_size`。

    `docs/04` §4.3.2 的字段表列了 `expires_at`（"每次成功分片都会刷新"），
    实现此前不返回它，客户端无从知道会话被续到什么时候。
    """
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)
    st = _state(cfg, db)

    blob = JPEG_HEAD + b"q" * 60
    sess = uploads.create_session(db, cfg, name="d.jpg", size=len(blob))
    uid = sess["upload_id"]
    # ★ Content-Range 不能不带：handler 第一件事就是读它，
    # 缺了直接 400 E1010，根本走不到“响应里有没有 expires_at”。
    r = _call(st, "PATCH", f"/api/uploads/{uid}", params={"upload_id": uid},
              headers={"content-range": f"bytes 0-{len(blob) - 1}/{len(blob)}"},
              body=blob)
    assert r.status == 206, (r.status, _body(r)[:200])
    b = _json(r)
    assert b["expires_at"] > time.time()
    assert b["chunk_size"] == int(sess["chunk_size"])
    assert b["complete"] is True
    assert b["duplicate"] is False


# ==========================================================================
# F19 —— `name` 含路径分隔符必须 400（文档一直这么写）
# ==========================================================================

@pytest.mark.parametrize("bad", [
    "../escape.jpg", "a/b.jpg", "a\\b.jpg", "C:\\Windows\\x.jpg",
    "/etc/x.jpg", "sub/dir/ok.jpg",
])
def test_upload_name_with_separator_is_400(tmp_path, bad):
    """F19：`docs/04:3131` 写"含 `/` `\\` 一律 400 E1001"，实现此前照收。

    实现侧原来不是漏洞（`declared_name` 只是记录，归档会重新命名，
    沙箱在 `archive.place()` 里再校验），但"文档写的拒绝行为不存在"
    会让按文档写客户端的人自己也不再校验 —— 而归档路径哪天改成
    "保留原名"（`declared_name` 就在手边），这条就真成漏洞了。
    """
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)

    with pytest.raises(AppError) as ei:
        uploads.create_session(db, cfg, name=bad, size=100)
    assert ei.value.code == "E1001", f"{bad} 应被拒"
    assert "分隔符" in str(ei.value.detail)


def test_upload_plain_name_still_accepted(tmp_path):
    """反向对照：真实前端传的 `File.name` 永远不含路径，不能被一起挡掉。"""
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)

    for ok in ("IMG_1234.jpg", "敦煌 2024.05.12.jpg", "a.b.c.jpeg", "名字.PNG"):
        sess = uploads.create_session(db, cfg, name=ok, size=100)
        assert sess["upload_id"], ok
        # ★ 必须收拾：`MAX_OPEN_SESSIONS = 3`（并发会话数上限，
        # 不是文件名数上限）。不收拾的话第 4 个名字拿到的是
        # `E4002 too_many_uploads`，看起来像“这个名字被拒了”。
        uploads.abort_session(db, cfg, sess["upload_id"])


# ==========================================================================
# F14 —— 写入口不认扫描排除目录
# ==========================================================================

def _seed_one(st: AppState, library: Path) -> int:
    _img(library / "2024" / "a.jpg")
    _scan(st)
    return int(st.db.query_one("SELECT id FROM media")["id"])


def test_move_into_excluded_dir_is_400(tmp_path):
    """F14：把照片移进 `data` / `#recycle` / `.photobridge_tmp` 这类目录必须 400。

    索引器会跳过这些目录 ⇒ 下一次扫描把照片标成 missing ⇒ 而 missing 行的
    move / rename 一律 `E1004`。结果是照片从界面上消失、又无法用 API 移回，
    用户只看到"移过去就没了"。
    """
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)
    st = _state(cfg, db)
    mid = _seed_one(st, library)

    for bad in ("data", "node_modules", "#recycle", ".photobridge_tmp",
                "2024/data", "Data/sub"):
        r = _call(st, "POST", "/api/items/move",
                  body=_json_body({"ids": [mid], "target_dir": bad}))
        assert r.status == 400, f"移动到 {bad} 应 400，实际 {r.status}"
        assert _json(r)["error"]["code"] == "E1009", bad

    # 文件必须原地未动。
    assert (library / "2024" / "a.jpg").exists()
    assert st.db.query_one("SELECT rel_path FROM media WHERE id=?",
                           (mid,))["rel_path"] == "2024/a.jpg"


def test_move_into_normal_dir_still_works(tmp_path):
    """反向对照：正常目录（包括与排除名相似但有扩展名的）不受影响。"""
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)
    st = _state(cfg, db)
    mid = _seed_one(st, library)

    for ok in ("2025/2025-01", "data.jpg", "mydata", "整理"):
        r = _call(st, "POST", "/api/items/move",
                  body=_json_body({"ids": [mid], "target_dir": ok}))
        assert r.status == 200, f"移动到 {ok} 应成功，实际 {r.status}"
    assert not (library / "2024" / "a.jpg").exists()


def test_upload_subdir_excluded_is_400(tmp_path):
    """F14：`upload.subdir` 指向排除目录时必须 400（否则每张上传都立刻 missing）。"""
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library, subdir="data/手机上传")
    db = _db(tmp_path)

    with pytest.raises(AppError) as ei:
        uploads.create_session(db, cfg, name="a.jpg", size=100)
    assert ei.value.code == "E1009"
    assert "索引范围" in str(ei.value.detail)


def test_missing_row_in_excluded_dir_says_why(tmp_path):
    """F14 第 3 条：missing 行的错误要能说清"为什么救不回来"。

    把照片移进排除目录后，下一次扫描标 missing。早先 move / rename 一律回
    "照片不存在" —— 用户会以为文件被删了，而正确的动作是去磁盘上把它搬出来。
    """
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)
    st = _state(cfg, db)
    mid = _seed_one(st, library)

    # 手工复现"照片已经躺在 data/ 里、行被标 missing"的状态。
    dst = library / "data" / "a.jpg"
    dst.parent.mkdir(parents=True, exist_ok=True)
    (library / "2024" / "a.jpg").rename(dst)
    db.execute("UPDATE media SET rel_path = ?, missing = 1 WHERE id = ?",
               ("data/a.jpg", mid))

    r = _call(st, "POST", "/api/items/move",
              body=_json_body({"ids": [mid], "target_dir": "2025"}))
    assert r.status == 200
    failed = _json(r)["failed"]
    assert len(failed) == 1
    assert "data" in failed[0]["message"], (
        f"必须点名排除目录，实际：{failed[0]['message']!r}")
    assert "移到别的目录" in failed[0]["message"]

    # rename 走同一套说明。
    r2 = _call(st, "POST", "/api/items/rename",
               body=_json_body({"id": mid, "new_name": "b.jpg"}))
    assert r2.status == 404
    err = _json(r2)["error"]
    # ★ `message` 是 `ErrorSpec.message`（稳定的英文标识符
    # `file_in_excluded_dir`），人话在 `detail` 与 `user_hint` 里。
    # 早先写成在 `message` 里找 "data" 永远不会匹配。
    assert err["code"] == "E1019"
    assert "data" in (err["detail"] or "")
    assert "移到别的目录" in err["user_hint"]


def test_index_one_file_skips_excluded_dir(tmp_path):
    """F14 第 2 条：单文件增量入口也要认排除目录。

    ★ 这条是变异验证逼出来的：本文件最初只用
    `sandbox.excluded_dir_segment` 直接验判定，于是把
    `should_index()` 里那两行**删掉也不会变红** ——
    `_walk` 是遍历时跳目录，而 `index_one_file` 不走遍历，
    它才是真正需要那两行的入口。

    不接线的后果与 move 那条一样：上传到 `data/手机上传/`
    的照片会先出现在时间线上、几秒后被下一次扫描标成 missing。
    """
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)
    indexer = Indexer(cfg, db)
    root = cfg.root_by_id("library")

    _img(library / "data" / "a.jpg")
    _img(library / "normal.jpg")

    assert indexer.index_one_file(root, "data/a.jpg") is None, (
        "排除目录下的文件不该被单文件增量收进库")
    assert db.query_one("SELECT id FROM media WHERE rel_path = ?",
                        ("data/a.jpg",)) is None

    # 反向对照：正常位置必须照收，否则上传完照片不会出现在时间线上。
    mid = indexer.index_one_file(root, "normal.jpg")
    assert mid is not None, "正常目录下的文件必须被索引"


def test_excluded_segment_ignores_filename(tmp_path):
    """`excluded_dir_segment` 只看目录段：文件夹叫 `data` 被挡，文件叫 `data.jpg` 不挡。"""
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    root = cfg.root_by_id("library")
    assert root.exclude_dirs, "夹具必须真的带上默认排除表"

    assert sandbox.excluded_dir_segment("data/a.jpg", root) == "data"
    assert sandbox.excluded_dir_segment("2024/data/a.jpg", root) == "data"
    assert sandbox.excluded_dir_segment("2024/a.jpg", root) == ""
    assert sandbox.excluded_dir_segment("data.jpg", root) == ""
    # 目录前缀模式（上传的 subdir）：每段都判。
    assert sandbox.excluded_dir_segment("data/手机上传", root,
                                        last_is_file=False) == "data"
    assert sandbox.excluded_dir_segment("手机上传/2026", root,
                                        last_is_file=False) == ""


# ==========================================================================
# F16 —— search 的 q 含控制字符
# ==========================================================================

def test_search_with_nul_is_400_not_full_scan(tmp_path):
    """F16：`?q=<NUL>` 早先 200 返回全表（SQLite 的 LIKE 在 NUL 处截断成 `%`）。

    不是注入（参数化与 `_esc` 都正确），被削掉的是"至少需要一个筛选条件"
    那条守卫 —— 它想防的正是"手机端一个 bug 就把索引器挤掉"。
    """
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)
    st = _state(cfg, db)
    for i in range(3):
        _img(library / f"{i}.jpg")
    _scan(st)

    for bad in ("\x00", "zzz\x00zzz", "\x01", "a\x1fb"):
        r = _call(st, "GET", "/api/search", query={"q": bad})
        assert r.status == 400, f"q={bad!r} 应 400，实际 {r.status}"
        assert _json(r)["error"]["code"] == "E1001"


def test_search_normal_query_still_works(tmp_path):
    """反向对照：正常关键词与**夹制表符**的粘贴文本不受影响。"""
    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)
    st = _state(cfg, db)
    _img(library / "敦煌.jpg")
    _scan(st)

    for ok in ("敦煌", "2024", "\t敦煌"):
        r = _call(st, "GET", "/api/search", query={"q": ok})
        assert r.status == 200, f"q={ok!r} 应 200，实际 {r.status}"
    # ★ `/api/search` 的响应与 `/api/timeline` 同形：
    # `{items, next_cursor, has_more, server_time, tz_offset_minutes}`，
    # **没有 `hits` 字段**。早先那个断言是在要一个从未存在过的键。
    hit = _json(_call(st, "GET", "/api/search", query={"q": "敦煌"}))
    assert len(hit["items"]) == 1, hit.get("items")


# ==========================================================================
# F5 残留 —— `/preview` 与 `/thumb` 对已删除的照片要给同一个答案
# ==========================================================================

def _delete(st: AppState, mid: int):
    from app.handlers import browse as b

    r = asyncio.run(b.delete_items(_ctx(
        st, "POST", "/api/items/delete",
        body=_json_body({"ids": [mid]}))))
    assert r.status == 200
    return r


def test_preview_of_missing_row_is_no_store_placeholder(tmp_path):
    """F5 残留：已删除照片的 1600px 预览图不能带 `immutable` 缓存一年。

    `/thumb` 已经修好了，`/preview` 没修：删除后缓存的 webp 仍在磁盘上，
    `dst.exists()` 为真，一路落到末尾的 `set_cache(31536000, immutable)`。
    手机缓存一年、一年内不再发请求 —— 用户删了照片，那台手机仍能拿出来。
    """
    from app.thumbs import generate_thumb, placeholder_webp

    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)
    st = _state(cfg, db)
    mid = _seed_one(st, library)
    rel = st.db.query_one("SELECT rel_path FROM media WHERE id=?",
                          (mid,))["rel_path"]

    # 先把 1600 的预览图生成出来并写进磁盘缓存（复现"删除后缓存仍在"）。
    dst = thumb_path(cfg, "library", rel, 1600)
    dst.parent.mkdir(parents=True, exist_ok=True)
    generate_thumb(str(library / rel), dst, 1600, orientation=1)
    assert dst.exists()

    _delete(st, mid)
    assert dst.exists(), "预览图缓存不随删除清理（这正是 F5 残留的前提）"

    r = _call(st, "GET", f"/api/items/{mid}/preview",
              params={"media_id": str(mid)})
    assert r.status == 200
    assert _body(r) == placeholder_webp(1600), "必须给占位图"
    cache = r.headers.get("Cache-Control", "")
    assert "immutable" not in cache, f"占位图绝不能 immutable（{cache!r}）"
    assert "no-store" in cache, f"占位图必须 no-store（{cache!r}）"


def test_thumb_and_preview_of_missing_row_agree(tmp_path):
    """F5 残留的同源修正：304 必须排在 `missing` 判定之后。

    早先 304 排在最前面，于是"已删除的照片 + 手机带着旧 ETag 来问"拿到 304，
    而 304 的语义是"继续用你缓存那份" —— 那份正是照片还活着时生成的缩略图。
    `no-store` 占位图分支永远走不到。
    """
    from app.thumbs import generate_thumb, placeholder_webp

    library = tmp_path / "lib"
    library.mkdir()
    cfg = _cfg(tmp_path, library)
    db = _db(tmp_path)
    st = _state(cfg, db)
    mid = _seed_one(st, library)
    rel = st.db.query_one("SELECT rel_path FROM media WHERE id=?",
                          (mid,))["rel_path"]

    etags = {}
    for size in (320, 1600):
        dst = thumb_path(cfg, "library", rel, size)
        dst.parent.mkdir(parents=True, exist_ok=True)
        generate_thumb(str(library / rel), dst, size, orientation=1)
        r = _call(st, "GET", f"/api/items/{mid}/preview" if size == 1600
                  else f"/api/items/{mid}/thumb",
                  query={"size": str(size)}, params={"media_id": str(mid)})
        assert r.status == 200
        etags[size] = r.headers.get("ETag") or r.headers.get("etag")
        assert etags[size], f"照片活着时必须发 ETag（size={size}）"

    _delete(st, mid)

    for size in (320, 1600):
        path = (f"/api/items/{mid}/thumb?size={size}" if size == 320
                else f"/api/items/{mid}/preview")
        r = _call(st, "GET", f"/api/items/{mid}/thumb" if size == 320
                  else f"/api/items/{mid}/preview",
                  query={"size": str(size)} if size == 320 else None,
                  params={"media_id": str(mid)},
                  headers={"if-none-match": etags[size]})
        assert r.status == 200, (
            f"{path} 带着旧 ETag 必须回 200 占位图，不能回 304（实际 {r.status}）")
        assert _body(r) == placeholder_webp(size), (
            f"{path} 必须给占位图，不能回 304 让客户端用缓存")
