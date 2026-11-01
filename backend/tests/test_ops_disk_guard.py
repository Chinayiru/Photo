"""运维风险 R-023（磁盘写满）三项落地的回归测试。

对应 `docs/11-风险登记册与决策记录.md` R-023 的三条措施：

  ① 缩略图缓存设上限，超了按最久未访问淘汰   → `thumbs.sweep_cache()`
  ③ 临时区定期清理（含没有会话认领的孤儿文件）→ `server._cleanup_orphan_tmp()`
  ⑤ 上传前检查剩余空间                      → `uploads._ensure_disk_space()`

三条的共同点：**都是"事后无法补救"的失效模式**。磁盘写满时 SQLite 会
先坏（写入失败留下半条 WAL），照片也会归档成 0 字节 —— 用户的照片库
不是"慢了一点"，而是"坏了"。所以三道闸都必须在**动作之前**生效。
"""
from __future__ import annotations

import os
import time
import types
from pathlib import Path

import pytest

from backend.app import archive, server, thumbs, uploads
from backend.app.config import Config
from backend.app.db import Database
from backend.app.errors import AppError


@pytest.fixture()
def library(tmp_path: Path) -> Path:
    d = tmp_path / "lib"
    d.mkdir()
    return d


def _cfg(tmp_path: Path, library: Path, **extra) -> Config:
    raw: dict = {
        "roots": [
            {
                "id": "library",
                "path": str(library),
                "label": "测试库",
                "mode": "readwrite",
                "enabled": True,
                "recursive": True,
            }
        ],
        "upload": {
            "subdir": "手机上传",
            "chunk_mb_lan": 1,
            "max_file_mb": 64,
            "session_ttl_hours": 24,
            "dedup": False,
        },
    }
    raw.update(extra)
    return Config(raw, path=tmp_path / "config.json", data_dir=tmp_path / "data")


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "test.db")
    d.init()
    yield d
    d.close()


def _usage(free: int) -> types.SimpleNamespace:
    """假装 `shutil.disk_usage()` 的返回值（只用到 `.free`）。"""
    return types.SimpleNamespace(total=10 ** 12, used=0, free=free)


# --------------------------------------------------------------------------
# ⑤ 上传前预检
# --------------------------------------------------------------------------


def test_init_refuses_upload_when_disk_is_full(tmp_path, library, db, monkeypatch):
    """剩余空间连"文件大小 + 余量"都不够时：507 E3010，且**不建会话**。

    为什么必须在 init 阶段拦：会话建出来之后，客户端会把几百 MB 传完，
    才发现写不进去。用户白等一场，而且临时区里留下一个半截文件。
    """
    cfg = _cfg(tmp_path, library)
    monkeypatch.setattr(uploads.shutil, "disk_usage", lambda _p: _usage(1024))

    with pytest.raises(AppError) as ei:
        uploads.create_session(db, cfg, name="a.jpg", size=4 * 1024 * 1024)

    assert ei.value.code == "E3010"
    assert ei.value.status == 507
    assert "空间不足" in (ei.value.detail or "")
    assert db.scalar("SELECT COUNT(*) FROM upload_sessions") == 0


def test_init_allows_upload_when_space_is_enough(tmp_path, library, db, monkeypatch):
    """够用就正常建会话 —— 预检不能顺手把所有上传都挡了。"""
    cfg = _cfg(tmp_path, library)
    monkeypatch.setattr(
        uploads.shutil, "disk_usage",
        lambda _p: _usage(10 ** 9),
    )
    r = uploads.create_session(db, cfg, name="a.jpg", size=1024)
    assert r["created"] is True


def test_init_reserves_headroom_beyond_the_file_itself(tmp_path, library, db, monkeypatch):
    """刚好等于文件大小的剩余空间也算不够：同分区还要长 WAL/缩略图/日志。"""
    cfg = _cfg(tmp_path, library)
    size = 1024 * 1024
    monkeypatch.setattr(uploads.shutil, "disk_usage", lambda _p: _usage(size))

    with pytest.raises(AppError) as ei:
        uploads.create_session(db, cfg, name="a.jpg", size=size)
    assert ei.value.code == "E3010"


def test_precheck_measures_the_tmp_dir_volume(tmp_path, library, db, monkeypatch):
    """预检必须量临时区所在卷，不是"库根"这个字符串。"""
    cfg = _cfg(tmp_path, library)
    seen: list[str] = []

    def _fake(path):                      # noqa: ANN001 - 测试替身
        seen.append(str(path))
        return _usage(10 ** 9)

    monkeypatch.setattr(uploads.shutil, "disk_usage", _fake)
    uploads.create_session(db, cfg, name="a.jpg", size=1024)

    assert seen == [str(library / archive.TMP_DIRNAME)]


def test_precheck_fails_open_when_usage_is_unavailable(tmp_path, library, db, monkeypatch):
    """读不到用量（网络盘、配额盘）时放行 —— 不能把能传的挡在门外。"""

    def _boom(_p):                        # noqa: ANN001 - 测试替身
        raise OSError("no statvfs on this device")

    monkeypatch.setattr(uploads.shutil, "disk_usage", _boom)
    cfg = _cfg(tmp_path, library)
    r = uploads.create_session(db, cfg, name="a.jpg", size=1024)
    assert r["created"] is True


# --------------------------------------------------------------------------
# ③ 临时区清理（孤儿文件）
# --------------------------------------------------------------------------


def test_cleanup_orphan_tmp_removes_files_without_a_session(tmp_path, library):
    """`_purge_stale_uploads()` 看不到的孤儿 .part 也要清掉。

    进程在 `os.open()` 之后、`INSERT` 之前被杀，留下的文件**没有 DB 行**，
    所以只有 `archive.cleanup_tmp()` 会收 —— 它此前在生产代码里零调用。
    """
    cfg = _cfg(tmp_path, library)
    tmp = library / archive.TMP_DIRNAME
    tmp.mkdir()
    orphan = tmp / "deadbeef.part"
    orphan.write_bytes(b"x" * 4096)
    old = time.time() - 600
    os.utime(orphan, (old, old))

    assert server._cleanup_orphan_tmp(cfg) == 1
    assert not orphan.exists()


def test_cleanup_orphan_tmp_keeps_a_file_that_is_still_being_written(tmp_path, library):
    """60 秒内新写进来的文件不动：用户可能刚好在上一次启动前一秒开始传。"""
    cfg = _cfg(tmp_path, library)
    tmp = library / archive.TMP_DIRNAME
    tmp.mkdir()
    fresh = tmp / "fresh.part"
    fresh.write_bytes(b"x" * 16)

    assert server._cleanup_orphan_tmp(cfg) == 0
    assert fresh.exists()


def test_cleanup_orphan_tmp_survives_an_unreadable_root(tmp_path, library, monkeypatch):
    """某个根清不掉不能让启动失败，也不能挡住别的根。"""
    cfg = _cfg(tmp_path, library)

    def _boom(_root, **_kw):              # noqa: ANN001 - 测试替身
        raise OSError("拒绝访问")

    monkeypatch.setattr(archive, "cleanup_tmp", _boom)
    assert server._cleanup_orphan_tmp(cfg) == 0


# --------------------------------------------------------------------------
# ① 缩略图缓存上限
# --------------------------------------------------------------------------


def _make_thumb(cfg: Config, name: str, size: int, *, age_sec: float) -> Path:
    d = cfg.data_path("thumbs", "lib", name[:2])
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}_320.webp"
    p.write_bytes(b"\0" * size)
    t = time.time() - age_sec
    os.utime(p, (t, t))
    return p


def test_thumb_cache_evicts_oldest_until_under_budget(tmp_path, library):
    """超上限时按 atime 从旧到新删，删到 80% 为止。"""
    cfg = _cfg(tmp_path, library)
    old = _make_thumb(cfg, "aaaa", 400, age_sec=9000)
    mid = _make_thumb(cfg, "bbbb", 400, age_sec=6000)
    new = _make_thumb(cfg, "cccc", 400, age_sec=60)
    assert thumbs.cache_bytes(cfg) == 1200

    freed = thumbs.sweep_cache(cfg, budget_bytes=1000)

    assert freed == 400
    assert not old.exists()          # 最久没看过的先走
    assert mid.exists() and new.exists()


def test_thumb_cache_is_left_alone_when_under_budget(tmp_path, library):
    """没超上限一个字节都不删 —— 否则每次启动都在白白重算缩略图。"""
    cfg = _cfg(tmp_path, library)
    keep = _make_thumb(cfg, "aaaa", 100, age_sec=9000)

    assert thumbs.sweep_cache(cfg, budget_bytes=1000) == 0
    assert keep.exists()


def test_thumb_cache_ignores_non_webp_files(tmp_path, library):
    """只统计/删除 `.webp`，别的文件（用户手放的、调试留下的）不碰。"""
    cfg = _cfg(tmp_path, library)
    other = cfg.data_path("thumbs", "keepme.txt")
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_bytes(b"x" * 5000)

    assert thumbs.sweep_cache(cfg, budget_bytes=10) == 0
    assert other.exists()


def test_thumb_cache_budget_comes_from_config(tmp_path, library):
    """默认 1 GB，可在 `thumbs.cache_mb` 里改；<=0 表示不清理。"""
    assert _cfg(tmp_path, library).thumbs_cache_mb == 1024
    cfg = _cfg(tmp_path, library, thumbs={"cache_mb": 64})
    assert cfg.thumbs_cache_mb == 64
    cfg = _cfg(tmp_path, library, thumbs={"cache_mb": 0})
    assert thumbs.sweep_cache(cfg) == 0
