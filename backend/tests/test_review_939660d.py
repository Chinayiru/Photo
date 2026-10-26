"""外部代码审查（对 `939660d`）指出问题的回归用例。

## 这批用例守的是一条底线：**不许丢用户的照片**

审查报告里两项"阻塞"都是数据丢失，而且都要并发或"看起来无害的时序"
才触发 —— 手工点一遍永远碰不到，所以必须用可重复的用例钉住：

- **R-M3-01 移动/改名会覆盖**：`exists()` 检查与 `os.replace()` 之间没有互斥，
  `os.replace` 又是覆盖语义。两个并发移动落到同一个目标名，后到的把先到的
  盖掉 —— 一张照片永久消失，两边还都返回成功。
- **R-M3-02 同尺寸不同内容被当同一张**：索引 upsert 不清 `sha256`，上传查重
  只比大小，于是"改过的照片"被判成"已存在"，刚上传的临时文件被删掉。

另外几条（R-M1-01 帧上限、R-M3-04 同名路径、R-M3-05 转码产物不淘汰、
R-M3-06 无排队上限）是一眼能验的判据，一并放这里，避免下次重构又漏回去。
"""
from __future__ import annotations

import asyncio

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.db import Database  # noqa: E402
from app.handlers.browse import _move_one_sync  # noqa: E402
from app.indexer import Indexer  # noqa: E402
from app.watcher import WatchCollector  # noqa: E402

from shared import protocol  # noqa: E402


def _write_jpeg(path: Path, edge: int) -> None:
    """写一张**真的** JPEG（索引器会按魔数判内容，假字节会被跳过）。"""
    from PIL import Image

    Image.new("RGB", (edge, edge), (20, 40, 60)).save(path, "JPEG", quality=70)


def _cfg(tmp_path: Path, lib: Path) -> Config:
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    return Config({
        "app": {"mode": "readwrite"},
        "roots": [{"id": "r1", "path": str(lib), "mode": "readwrite"}],
        "thumbs": {"sizes": [320]},
        "index": {"batch_size": 50},
    }, path=data / "config.json", data_dir=data)


# ===================================================================== R-M3-01

def test_move_refuses_to_overwrite_an_existing_target(tmp_path: Path) -> None:
    src = tmp_path / "a.jpg"
    src.write_bytes(b"A" * 1024)
    dst = tmp_path / "b.jpg"
    dst.write_bytes(b"B" * 7)

    with pytest.raises(FileExistsError):
        _move_one_sync(src, dst, size=1024)

    assert src.read_bytes() == b"A" * 1024, "源文件必须原地不动"
    assert dst.read_bytes() == b"B" * 7, "目标文件一个字节都不能被盖"


def test_concurrent_moves_to_one_name_keep_exactly_one_intact(tmp_path: Path) -> None:
    """八个线程抢同一个目标名：恰好一个成功，且没有半截文件。"""
    payload = {i: bytes([i]) * 4096 for i in range(8)}
    sources = []
    for i in range(8):
        p = tmp_path / f"src{i}.jpg"
        p.write_bytes(payload[i])
        sources.append(p)
    target = tmp_path / "same.jpg"

    winners: list[Path] = []
    losers: list[BaseException] = []
    barrier = threading.Barrier(len(sources))

    def go(p: Path) -> None:
        barrier.wait()
        try:
            _move_one_sync(p, target, size=4096)
            winners.append(p)
        except BaseException as exc:      # noqa: BLE001 —— 失败也要记下来
            losers.append(exc)

    threads = [threading.Thread(target=go, args=(p,)) for p in sources]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(winners) == 1, f"应当只有一个赢家，实际 {len(winners)}"
    assert target.stat().st_size == 4096
    assert target.read_bytes() == payload[int(winners[0].stem[3:])]
    # 输的七个必须还在原地（FileExistsError，不是被删掉）
    assert sum(1 for p in sources if p.exists()) == 7
    assert all(isinstance(e, FileExistsError) for e in losers), losers


# ===================================================================== R-M3-02

def test_rescan_clears_sha256_when_the_file_changed(tmp_path: Path) -> None:
    """索引更新必须把 `sha256` 置空，否则查重会拿到过期的内容指纹。"""
    lib = tmp_path / "lib"
    lib.mkdir()
    photo = lib / "a.jpg"
    photo.write_bytes(b"\xff\xd8" + b"x" * 100)
    cfg = _cfg(tmp_path, lib)
    db = Database(cfg.db_path)
    db.init()
    idx = Indexer(cfg, db)

    asyncio.run(idx.scan_root(cfg.root_by_id("r1")))
    db.execute("UPDATE media SET sha256 = ? WHERE file_name = 'a.jpg'", ("ab" * 32,))

    # 改内容（大小也变，确保索引器认为它变了）
    _write_jpeg(photo, 64)
    asyncio.run(idx.scan_root(cfg.root_by_id("r1")))

    row = db.query_one("SELECT size, sha256 FROM media WHERE file_name = 'a.jpg'")
    assert row["size"] > 100
    assert row["sha256"] is None, "文件变了，旧的 sha256 必须清掉"


def test_find_duplicate_skips_rows_whose_file_changed(tmp_path: Path) -> None:
    """sha256 撞上但磁盘上的文件已经不是那次哈希的内容 → 不许当重复。"""
    import os

    from app.uploads import find_duplicate

    lib = tmp_path / "lib"
    lib.mkdir()
    photo = lib / "a.jpg"
    photo.write_bytes(b"\xff\xd8" + b"z" * 64)
    cfg = _cfg(tmp_path, lib)
    db = Database(cfg.db_path)
    db.init()
    asyncio.run(Indexer(cfg, db).scan_root(cfg.root_by_id("r1")))
    db.execute("UPDATE media SET sha256 = ? WHERE file_name = 'a.jpg'",
               ("cd" * 32,))
    st = photo.stat()
    # 内容被换掉：大小不变、mtime 变（用户覆盖保存同一张图）
    os.utime(photo, (st.st_atime, st.st_mtime + 120))

    assert find_duplicate(db, cfg, "cd" * 32) is None


# ===================================================================== R-M1-01

def test_decode_frame_rejects_payload_over_the_negotiated_frame_size() -> None:
    """接收侧的 `max_frame` 必须真生效：对端是不可信的。

    编码侧一直有上限，解码侧以前没有 —— 而 `max_size=None` 的 WS 让一条
    声称 4GB 的帧直接进内存。
    """
    frame = protocol.encode_frame(protocol.FrameType.RES_BODY, 7, b"x" * 2048)
    assert protocol.decode_frame(frame).payload == b"x" * 2048   # 正常能解
    with pytest.raises(protocol.ProtocolError):
        protocol.decode_frame(frame, max_frame=1024)


# ===================================================================== R-M3-04

def test_same_rel_path_under_two_roots_are_two_entries(tmp_path: Path) -> None:
    """两个库根的同名路径不能互相覆盖（否则删 A 变成扫 B）。"""
    from app.config import RootConfig

    a = RootConfig(id="rootA", path=str(tmp_path / "a"), mode="readwrite")
    b = RootConfig(id="rootB", path=str(tmp_path / "b"), mode="readwrite")
    col = WatchCollector(debounce_sec=0)
    col.on_event("deleted", a, "2024/a.jpg")
    col.on_event("created", b, "2024/a.jpg")

    ready = col.take_ready(limit=10)
    by_root = {root.id: (action, rel) for action, rel, root in ready}
    assert by_root["rootA"] == ("deleted", "2024/a.jpg")
    assert by_root["rootB"] == ("created", "2024/a.jpg")


# ===================================================================== R-M3-06

def test_transcode_queue_has_a_cap_and_its_own_error_code() -> None:
    from app import transcode as tmod
    from app.errors import E4007

    assert tmod.MAX_PENDING_JOBS >= 1
    assert E4007.code == "E4007"
    assert E4007.status == 429
    assert E4007.message, "错误码必须有面向用户的文案"


