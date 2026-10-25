"""外部审计 F01 / F04 / F07 的回归（2026-10-26）。

三条都是"代码看着像做了权限检查、其实检查错了对象"：

- **F01**（CWE-863）`upload` 模式本该"只能新增、不能改已有照片"，但
  `move_items` / `rename_item` 用的是 `root.can_write`，而 `upload`
  也算 `can_write` ⇒ 改名、移动照样放行。修法：新增 `can_modify`
  （只有 `readwrite` 为真）+ 沙箱独立的 `"modify"` 权限档。
- **F04**（CWE-306）`check_bind_safety()` 里"关掉配对鉴权不许监听网络"
  这项只写在**通配地址**分支，于是绑一个具体内网 IP 就能起无鉴权服务。
- **F07**（CWE-778）删除/移动/改名的审计没带 `client_id`，事后没法
  知道是哪台手机干的。`audit.record()` 早就收这个参数，只是没人传。

这三条都不能靠"看到 403"就算过 —— F01 还要断言**磁盘上的文件没动**，
F07 要断言**审计表里那一列真的写进去了**。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config, check_bind_safety  # noqa: E402
from app.core import Ctx, Router  # noqa: E402
from app.db import Database  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.handlers import browse  # noqa: E402
from app.state import AppState  # noqa: E402

CLIENT_ID = "0123456789abcdef0123456789abcdef"


# ---------------------------------------------------------------- 夹具

@pytest.fixture()
def library(tmp_path: Path) -> Path:
    lib = tmp_path / "lib"
    lib.mkdir()
    return lib


def _img(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 48), (10, 20, 30)).save(path, "JPEG")
    return path


def _state(tmp_path: Path, library: Path, *, mode: str) -> AppState:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    cfg = Config({
        "app": {"mode": mode},
        "roots": [{"id": "library", "path": str(library), "mode": mode}],
        "thumbs": {"sizes": [320, 1600]},
        "auth": {"allow_delete": True, "delete_to_recycle_bin": True,
                 "allow_move": True},
        "index": {"batch_size": 50},
    }, path=data / "config.json", data_dir=data)
    db = Database(cfg.db_path)
    db.init()
    return AppState(cfg, db)


def _router() -> Router:
    r = Router()
    for method, pattern, handler in [
        ("POST", "/api/items/move", browse.move_items),
        ("POST", "/api/items/rename", browse.rename_item),
    ]:
        r.add(method, pattern, handler)
    return r


def _ctx(st: AppState, method: str, path: str, *, body: bytes | None = None,
         client_id: str | None = None) -> Ctx:
    async def _stream():
        if body:
            yield body

    ctx = Ctx(method=method, path=path, query={}, headers={},
              body=_stream() if body is not None else None, params={}, app=st)
    if client_id:
        ctx.state["client"] = {"id": client_id, "name": "UnitTest Phone"}
    return ctx


def _call(st: AppState, method: str, path: str, **kw):
    return asyncio.run(_router().dispatch(_ctx(st, method, path, **kw)))


def _seed(st: AppState, library: Path) -> dict[str, int]:
    from app.indexer import Indexer

    _img(library / "2024" / "2024-05" / "a.jpg")
    _img(library / "2024" / "2024-05" / "b.jpg")
    asyncio.run(Indexer(st.cfg, st.db).scan_root(st.cfg.root_by_id("library")))
    return {r["file_name"]: int(r["id"]) for r in
            st.db.query("SELECT id, file_name FROM media")}


def _body(**kw) -> bytes:
    return json.dumps(kw).encode("utf-8")


# ---------------------------------------------------------------- F01

@pytest.mark.parametrize("apiname", ["rename", "move"])
def test_upload_mode_cannot_modify_existing_files(tmp_path, library, apiname):
    """`upload` 模式改名/移动已有照片必须 403，且磁盘上什么都没动。"""
    st = _state(tmp_path, library, mode="upload")
    ids = _seed(st, library)
    src = library / "2024" / "2024-05" / "a.jpg"
    assert src.exists()

    if apiname == "rename":
        reply = _call(st, "POST", "/api/items/rename",
                      body=_body(id=ids["a.jpg"], new_name="hacked.jpg"))
    else:
        reply = _call(st, "POST", "/api/items/move",
                      body=_body(ids=[ids["a.jpg"]], target_dir="2024/2024-05"))

    # dispatch 把 AppError 转成错误 Reply（不抛异常），所以判 status/code
    assert reply.status == 403, reply.body
    assert b"E1015" in reply.body, reply.body
    # 关键：不是"报错了就算"，文件必须原封不动
    assert src.exists(), "upload 模式下文件被移走了"
    assert not (src.parent / "hacked.jpg").exists()
    row = st.db.query_one("SELECT rel_path FROM media WHERE id=?", (ids["a.jpg"],))
    assert row["rel_path"].endswith("a.jpg")


def test_readwrite_mode_can_still_rename(tmp_path, library):
    """收紧之后 `readwrite` 模式不能被顺手锁死。"""
    st = _state(tmp_path, library, mode="readwrite")
    ids = _seed(st, library)

    reply = _call(st, "POST", "/api/items/rename",
                  body=_body(id=ids["a.jpg"], new_name="renamed.jpg"))
    assert reply.status == 200
    assert (library / "2024" / "2024-05" / "renamed.jpg").exists()


def test_readwrite_mode_can_still_move(tmp_path, library):
    st = _state(tmp_path, library, mode="readwrite")
    ids = _seed(st, library)
    (library / "dest").mkdir()

    reply = _call(st, "POST", "/api/items/move",
                  body=_body(ids=[ids["a.jpg"]], target_dir="dest"))
    assert reply.status == 200
    assert (library / "dest" / "a.jpg").exists()


# ---------------------------------------------------------------- F04

def _cfg(**auth) -> Config:
    """注意必须给 roots —— 没配根目录时 `check_bind_safety()` 会先以
    "尚未配置媒体根目录"拒掉，测不到 require_pairing 那道闸。"""
    return Config({
        "server": {"host": "192.168.1.10", "port": 8787},
        "roots": [{"id": "library", "path": "C:/photos", "mode": "readwrite"}],
        "auth": auth,
    })


@pytest.mark.parametrize("host", ["192.168.1.10", "0.0.0.0", "::"])
def test_bind_safety_refuses_open_service_on_any_non_loopback(host):
    """关掉配对鉴权时，绑任何非回环地址都必须被拒（不只通配地址）。"""
    ok, why = check_bind_safety(host, _cfg(require_pairing=False))
    assert ok is False, f"{host} 竟然允许无鉴权监听"
    assert "require_pairing" in why


@pytest.mark.parametrize("host", ["192.168.1.10", "0.0.0.0"])
def test_bind_safety_refuses_panel_http_on_any_non_loopback(host):
    cfg = Config({
        "server": {"host": host, "port": 8787, "panel_http": True},
        "roots": [{"id": "library", "path": "C:/photos", "mode": "readwrite"}],
        "auth": {"require_pairing": True},
    })
    ok, why = check_bind_safety(host, cfg)
    assert ok is False, f"{host} 竟然允许把面板开到网络上"
    assert "panel_http" in why


def test_bind_safety_still_allows_loopback_without_pairing():
    ok, _ = check_bind_safety("127.0.0.1", _cfg(require_pairing=False))
    assert ok is True


def test_bind_safety_allows_lan_with_pairing():
    ok, why = check_bind_safety("192.168.1.10", _cfg(require_pairing=True))
    assert ok is True, why


# ---------------------------------------------------------------- F07

def test_audit_records_the_client_that_did_it(tmp_path, library):
    """改名成功的审计行必须带上是哪台手机发起的。"""
    st = _state(tmp_path, library, mode="readwrite")
    ids = _seed(st, library)

    _call(st, "POST", "/api/items/rename",
          body=_body(id=ids["a.jpg"], new_name="from-phone.jpg"),
          client_id=CLIENT_ID)

    rows = st.db.query(
        "SELECT action, client_id FROM audit_log WHERE action='rename'")
    assert rows, "没有写审计"
    assert rows[-1]["client_id"] == CLIENT_ID


def test_audit_move_records_client_id(tmp_path, library):
    st = _state(tmp_path, library, mode="readwrite")
    ids = _seed(st, library)
    (library / "dest").mkdir()

    _call(st, "POST", "/api/items/move",
          body=_body(ids=[ids["a.jpg"]], target_dir="dest"),
          client_id=CLIENT_ID)

    rows = st.db.query(
        "SELECT client_id FROM audit_log WHERE action='move'")
    assert rows and rows[-1]["client_id"] == CLIENT_ID


def test_audit_delete_records_client_id(tmp_path, library):
    """删除走回收站，但审计同样要能归因。"""
    st = _state(tmp_path, library, mode="readwrite")
    ids = _seed(st, library)
    st.db.execute("INSERT INTO audit_log(ts, device_name, client_id, action,"
                  " rel_path, detail) VALUES (0, NULL, NULL, 'delete', NULL, '{}')")

    asyncio.run(browse._trash_one(st, ids["a.jpg"], client_id=CLIENT_ID))
    rows = st.db.query(
        "SELECT client_id FROM audit_log WHERE action='delete' ORDER BY rowid")
    assert rows[-1]["client_id"] == CLIENT_ID


# ---------------------------------------------------------------- 沙箱档位

def test_sandbox_modify_need_is_separate_from_write(tmp_path, library):
    """`upload` 模式：`write` 放行，`modify` 必须拒 —— 这是 F01 的地基。"""
    from app import sandbox

    _img(library / "a.jpg")
    cfg = _state(tmp_path, library, mode="upload").cfg
    root = cfg.root_by_id("library")

    sandbox.resolve(root, "new.jpg", "write", cfg=cfg)      # 新增：允许
    with pytest.raises(AppError):
        sandbox.resolve(root, "a.jpg", "modify", cfg=cfg)    # 改已有：拒绝
