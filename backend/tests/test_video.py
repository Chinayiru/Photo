"""视频（ISSUE-055）：容器识别、可播判定、探测解析、抽帧、库约束、接口口径。

## 这批用例守的是什么

视频这条链路有四个环节，每个都有一类"静默错"：

1. **容器识别**：`ftyp` 这个盒子 HEIC/AVIF 与 mp4/mov 共用。判错的后果不是
   报错，而是一箱 iPhone 的 HEIC 被当成视频去排 ffprobe —— 扫描时间翻几倍，
   而且每条都生成一张"抽帧失败"的占位图。
2. **扩展名与容器一致**：把 `.mkv` 改名成 `.mp4` 传上来，如果只看扩展名放行，
   手机拿到 `Content-Type: video/mp4` 去解 Matroska，症状是"一直转圈"。
3. **可播判定**：iPhone 默认录 HEVC，Chrome 桌面不支持。不判编码的话，
   用户会看到"我手机上明明能播"。
4. **库约束**：`kind` 的 CHECK 与视频四列的可空性。写错不会立刻报错，
   而是在某个 batch 上抛 IntegrityError，整轮扫描中断。

外部依赖（ffmpeg/ffprobe）在这些用例里**一律不打真二进制**：真跑那条由
`build/_video_smoke.py` 负责（它现场生成一段 H.264 视频走完整链路）。
这里是纯逻辑层，可以在任何机器上跑。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import struct
import subprocess
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import video as V                                       # noqa: E402
from app.config import Config                                   # noqa: E402
from app.core import Ctx                                        # noqa: E402
from app.db import Database                                     # noqa: E402
from app.errors import AppError                                 # noqa: E402
from app.handlers import browse, common                          # noqa: E402


# --------------------------------------------------------------------------
# 造字节：容器头
# --------------------------------------------------------------------------

def _ftyp(brand: bytes, *, size: int = 64) -> bytes:
    """一个 ISO-BMFF 头：4 字节长度 + 'ftyp' + brand，其余填零。"""
    head = struct.pack(">I", 32) + b"ftyp" + brand + b"\x00\x00\x00\x00"
    return (head + b"\x00" * size)[:size]


MP4_HEAD = _ftyp(b"isom")
MOV_HEAD = _ftyp(b"qt  ")
HEIC_HEAD = _ftyp(b"heic")
AVIF_HEAD = _ftyp(b"avif")
WEBM_HEAD = b"\x1a\x45\xdf\xa3" + b"\x00" * 4 + b"\x42\x82\x84webm" + b"\x00" * 40
MKV_HEAD = b"\x1a\x45\xdf\xa3" + b"\x00" * 4 + b"\x42\x82\x88matroska" + b"\x00" * 40
AVI_HEAD = b"RIFF" + struct.pack("<I", 100) + b"AVI " + b"\x00" * 52
WAV_HEAD = b"RIFF" + struct.pack("<I", 100) + b"WAVE" + b"\x00" * 52


# --------------------------------------------------------------------------
# 1. 容器识别
# --------------------------------------------------------------------------

@pytest.mark.parametrize("head,expected", [
    (MP4_HEAD, "mp4"),
    (_ftyp(b"mp42"), "mp4"),
    (MOV_HEAD, "mov"),
    (WEBM_HEAD, "webm"),
    (MKV_HEAD, "mkv"),
    (AVI_HEAD, "avi"),
])
def test_sniff_container_recognises_each_family(head: bytes, expected: str) -> None:
    assert V.sniff_container(head) == expected


@pytest.mark.parametrize("head", [HEIC_HEAD, AVIF_HEAD])
def test_sniff_container_rejects_image_brands_inside_bmff(head: bytes) -> None:
    """HEIC/AVIF 与 mp4 共用 `ftyp` 盒子 —— 必须靠 brand 排除。

    这是最容易写错的一处：只判断 `head[4:8] == b"ftyp"` 就把整箱 iPhone
    照片当成视频。
    """
    assert V.sniff_container(head) is None


def test_sniff_container_rejects_wav_hidden_in_riff() -> None:
    """RIFF 也被 wav（以及 webp）用，必须看第三个四字节。"""
    assert V.sniff_container(WAV_HEAD) is None


def test_sniff_container_needs_enough_bytes() -> None:
    assert V.sniff_container(b"") is None
    assert V.sniff_container(b"\x00" * 11) is None


def test_sniff_container_falls_back_to_mp4_for_unknown_bmff_brand() -> None:
    """厂商私有 brand（如 Sony XAVC）仍按 mp4 处理，交给 ffprobe 定论。"""
    assert V.sniff_container(_ftyp(b"XAVC")) == "mp4"


# --------------------------------------------------------------------------
# 2. 扩展名 ↔ 容器一致
# --------------------------------------------------------------------------

def test_check_container_accepts_matching_extension() -> None:
    assert V.check_container("mp4", MP4_HEAD) == (True, "mp4")


def test_check_container_allows_mov_extension_with_mp4_brand() -> None:
    """`.mov` 与 `.mp4` 解码路径相同，brand 串用不该拒。"""
    ok, actual = V.check_container("mov", MP4_HEAD)
    assert ok is True and actual == "mp4"


def test_check_container_rejects_matroska_renamed_to_mp4() -> None:
    ok, actual = V.check_container("mp4", MKV_HEAD)
    assert ok is False
    assert actual == "mkv", "要能让用户看到'实际是 mkv'"


def test_check_container_rejects_image_file_declared_as_video() -> None:
    ok, actual = V.check_container("mp4", HEIC_HEAD)
    assert ok is False and actual == ""


def test_container_of_ext_is_case_and_dot_insensitive() -> None:
    assert V.container_of_ext(".MP4") == frozenset({"mp4"})
    assert V.container_of_ext("watch.mkv") == frozenset(), "不认识的扩展名回空集"


# --------------------------------------------------------------------------
# 3. ffprobe JSON → ProbeResult
# --------------------------------------------------------------------------

def _probe_json(**over: object) -> dict:
    data = {
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "12.5"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920,
             "height": 1080},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }
    data.update(over)
    return data


def test_parse_probe_reads_the_fields_we_store() -> None:
    pr = V._parse_probe(_probe_json())        # noqa: SLF001
    assert pr.ok and pr.container == "mp4"
    assert pr.duration == pytest.approx(12.5)
    assert (pr.width, pr.height) == (1920, 1080)
    assert pr.vcodec == "h264" and pr.acodec == "aac"


def test_parse_probe_without_video_stream_is_not_ok() -> None:
    """只有音轨的文件不是视频：不能索引成一条点了播不了的条目。"""
    data = _probe_json(streams=[{"codec_type": "audio", "codec_name": "mp3"}])
    pr = V._parse_probe(data)                # noqa: SLF001
    assert pr.ok is False and pr.error


def test_parse_probe_survives_bad_duration_and_missing_fields() -> None:
    data = {"format": {"format_name": "matroska,webm", "duration": "N/A"},
            "streams": [{"codec_type": "video", "codec_name": "vp9",
                         "duration": "0.000"}]}
    pr = V._parse_probe(data)                # noqa: SLF001
    assert pr.ok
    assert pr.duration == 0.0
    assert pr.container == "webm"
    assert (pr.width, pr.height) == (0, 0), "缺失按未知处理，不抛"


@pytest.mark.parametrize("fmt,expected", [
    ("mov,mp4,m4a,3gp,3g2,mj2", "mp4"),
    ("matroska,webm", "webm"),
    ("matroska", "mkv"),
    ("avi", "avi"),
    ("", ""),
])
def test_container_from_format(fmt: str, expected: str) -> None:
    assert V._container_from_format(fmt) == expected   # noqa: SLF001


def test_probe_result_as_db_writes_null_for_unknown_numbers() -> None:
    """宽高为 0 必须写 NULL：SCHEMA 上有 `width > 0` 的 CHECK。"""
    row = V.ProbeResult(ok=True, container="mp4").as_db()
    assert row["width"] is None and row["height"] is None
    assert row["duration"] is None
    assert row["container"] == "mp4"


# --------------------------------------------------------------------------
# 4. 可播判定（D2）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("container,vcodec,playable", [
    ("mp4", "h264", True),
    ("mov", "h264", True),
    ("webm", "vp9", True),
    ("mp4", "hevc", False),      # iPhone 默认编码，Chrome 桌面不支持
    ("mkv", "h264", False),      # 容器不在白名单
    ("avi", "mpeg4", False),
    ("mp4", "", False),
])
def test_is_playable_matrix(container: str, vcodec: str, playable: bool) -> None:
    assert V.is_playable(container=container, vcodec=vcodec) is playable


def test_hevc_is_rejected_even_inside_mp4() -> None:
    """这条单独写出来，因为它是"手机上能播、电脑上不能播"的唯一来源。"""
    assert V.is_playable(container="mp4", vcodec="hevc") is False


# --------------------------------------------------------------------------
# 5. 找可执行文件 / 抽帧（不打真二进制）
# --------------------------------------------------------------------------

def test_find_bin_raises_a_deploy_hint_when_missing(monkeypatch) -> None:
    V.reset_bin_cache()
    monkeypatch.setattr(V, "_app_dirs", lambda: [])
    monkeypatch.delenv("PHOTOBRIDGE_FFMPEG_DIR", raising=False)
    monkeypatch.setattr(V.shutil, "which", lambda _n: None)
    with pytest.raises(V.FFmpegMissing) as ei:
        V.find_bin("ffmpeg")
    assert "tools/ffmpeg" in str(ei.value)
    V.reset_bin_cache()


def test_find_bin_honours_configured_dir(tmp_path, monkeypatch) -> None:
    V.reset_bin_cache()
    exe = tmp_path / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    exe.write_bytes(b"")
    got = V.find_bin("ffmpeg", configured_dir=str(tmp_path))
    assert Path(got) == exe
    V.reset_bin_cache()


class _CP:
    """`subprocess.CompletedProcess` 的替身。"""

    def __init__(self, returncode: int, stderr: bytes = b"") -> None:
        self.returncode = returncode
        self.stdout = b""
        self.stderr = stderr


def test_extract_frame_falls_back_to_the_first_frame(tmp_path, monkeypatch) -> None:
    """第 1 秒取不到（短视频/截断）时必须回退到 0 秒，而不是报错。"""
    V.reset_bin_cache()
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 16)
    dst = tmp_path / "out" / "frame.jpg"
    monkeypatch.setattr(V, "find_bin", lambda name, configured_dir="": "ffmpeg")
    seen: list[str] = []

    def fake_run(argv, *, timeout):        # noqa: ANN001
        seek = argv[argv.index("-ss") + 1]
        seen.append(seek)
        if seek != "0.000":
            return _CP(1, b"Invalid data found")
        Path(argv[-1]).parent.mkdir(parents=True, exist_ok=True)
        Path(argv[-1]).write_bytes(b"\xff\xd8\xff\xd9")
        return _CP(0)

    monkeypatch.setattr(V, "_run", fake_run)
    got = V.extract_frame(src, dst, at=1.0)
    assert got == dst and dst.stat().st_size == 4
    assert seen == ["1.000", "0.000"], "先试 1 秒，再回退 0 秒"


def test_extract_frame_raises_when_both_attempts_fail(tmp_path, monkeypatch) -> None:
    V.reset_bin_cache()
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 16)
    monkeypatch.setattr(V, "find_bin", lambda name, configured_dir="": "ffmpeg")
    monkeypatch.setattr(V, "_run",
                        lambda argv, timeout: _CP(1, b"moov atom not found"))
    with pytest.raises(RuntimeError) as ei:
        V.extract_frame(src, tmp_path / "frame.jpg")
    assert "moov atom not found" in str(ei.value)


def test_extract_frame_treats_empty_output_as_failure(tmp_path, monkeypatch) -> None:
    """ffmpeg 退出码 0 但文件是 0 字节（磁盘满/编码器抽风）也要算失败。"""
    V.reset_bin_cache()
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"\x00" * 16)
    monkeypatch.setattr(V, "find_bin", lambda name, configured_dir="": "ffmpeg")

    def fake_run(argv, *, timeout):        # noqa: ANN001
        Path(argv[-1]).write_bytes(b"")
        return _CP(0)

    monkeypatch.setattr(V, "_run", fake_run)
    with pytest.raises(RuntimeError):
        V.extract_frame(src, tmp_path / "frame.jpg")


# --------------------------------------------------------------------------
# 6. 库约束
# --------------------------------------------------------------------------

def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "pb.db")
    db.init()
    return db


def _insert(db: Database, *, kind: str, **over: object) -> None:
    vals = {
        "root_id": "library", "rel_path": f"v/{kind}-{os.urandom(3).hex()}.mp4",
        "file_name": "x.mp4", "ext": "mp4", "kind": kind, "size": 10,
        "mtime": 1.0, "taken_at": 1.0,
    }
    vals.update(over)
    cols = ", ".join(vals)
    marks = ", ".join("?" * len(vals))
    db.execute(f"INSERT INTO media ({cols}) VALUES ({marks})",
               tuple(vals.values()))


def test_media_accepts_video_kind(tmp_path) -> None:
    db = _db(tmp_path)
    _insert(db, kind="video", duration=3.5, container="mp4",
            vcodec="h264", acodec="aac", width=640, height=480)
    row = db.query_one("SELECT kind, duration, vcodec FROM media")
    assert (row["kind"], row["duration"], row["vcodec"]) == ("video", 3.5, "h264")


def test_media_rejects_unknown_kind(tmp_path) -> None:
    """CHECK 不认 `audio`：将来真接音频要走一次显式迁移，不能靠碰巧。"""
    db = _db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _insert(db, kind="audio")


def test_video_columns_are_nullable_for_images(tmp_path) -> None:
    db = _db(tmp_path)
    _insert(db, kind="image")
    row = db.query_one("SELECT duration, container FROM media")
    assert row["duration"] is None and row["container"] is None


def test_row_kind_tolerates_rows_without_the_column() -> None:
    """缩略图引擎/维护脚本会拿到只有部分列的假行，不能因此崩。"""
    assert V.row_kind({}) == "image"
    assert V.row_kind({"kind": "video"}) == "video"
    assert V.row_kind({"kind": None}) == "image"
    assert V.row_kind(object()) == "image"


# --------------------------------------------------------------------------
# 7. 时间线口径（kind 过滤 + 开关）
# --------------------------------------------------------------------------

def test_timeline_where_filters_video_only_when_switched_on() -> None:
    on, _ = common.timeline_where(kind="video", allow_video=True)
    assert "kind = 'video'" in on and "AND 0" not in on
    off, _ = common.timeline_where(kind="video", allow_video=False)
    assert "AND 0" in off, "关掉视频时必须回空集而不是报错"


def test_timeline_where_image_excludes_video_rows() -> None:
    sql, _ = common.timeline_where(kind="image", allow_video=True)
    assert "kind = 'image'" in sql
    # 不能只写 kind='image' 又追加 AND 0 的旧逻辑
    assert "AND 0" not in sql


def test_timeline_where_all_hides_video_when_disabled() -> None:
    """关掉视频时「全部」必须退化成「只看图片」。

    这里刻意**不用** `AND 0`：`kind = 'image'` 能走时间线部分索引，
    而 `AND 0` 让规划器只能扫全表再逐行丢弃。
    """
    on, _ = common.timeline_where(kind=None, allow_video=True)
    assert "AND 0" not in on
    assert "kind" not in on, "开着视频时不该加任何 kind 约束"
    off, _ = common.timeline_where(kind=None, allow_video=False)
    assert "kind = 'image'" in off
    assert "AND 0" not in off


# --------------------------------------------------------------------------
# 8. /stream 的用户口径
# --------------------------------------------------------------------------

def _cfg(tmp_path: Path, *, enabled: bool = True) -> Config:
    return Config(
        {
            "roots": [{"id": "library", "path": str(tmp_path / "lib"),
                       "mode": "readonly"}],
            "video": {"enabled": enabled},
        },
        path=tmp_path / "config.json", data_dir=tmp_path / "data",
    )


def test_upload_extension_gate_follows_the_video_switch(tmp_path) -> None:
    from app import uploads

    off = _cfg(tmp_path, enabled=False)
    on = _cfg(tmp_path, enabled=True)
    with pytest.raises(AppError) as ei:
        uploads.check_extension(off, "a.mp4")
    assert ei.value.code == "E1017"
    assert uploads.check_extension(on, "a.mp4") == ".mp4"


def test_upload_rejects_matroska_disguised_as_mp4(tmp_path) -> None:
    from app import uploads

    cfg = _cfg(tmp_path)
    with pytest.raises(AppError) as ei:
        uploads.check_magic(cfg, "x.mp4", MKV_HEAD)
    assert ei.value.code == "E1017"
    assert "mkv" in ei.value.detail


def test_upload_accepts_real_mp4(tmp_path) -> None:
    from app import uploads

    assert uploads.check_magic(_cfg(tmp_path), "x.mp4", MP4_HEAD) == ".mp4"


def test_upload_does_not_offer_mkv_even_when_video_is_on(tmp_path) -> None:
    """mkv/avi 可索引但不可上传：传上来用户自己也播不了（D1）。"""
    cfg = _cfg(tmp_path)
    with pytest.raises(AppError):
        from app import uploads

        uploads.check_extension(cfg, "a.mkv")


def test_stream_rejects_an_image_item(tmp_path) -> None:
    """/stream 只服务视频：图片走 /raw，混用会让前端拿错元素渲染。"""
    from app.state import AppState

    cfg = _cfg(tmp_path)
    (tmp_path / "lib").mkdir()
    db = Database(cfg.db_path)
    db.init()
    _insert(db, kind="image")
    media_id = int(db.query_one("SELECT id FROM media")["id"])
    st = AppState(cfg, db)

    ctx = Ctx(method="GET", path=f"/api/items/{media_id}/stream", query={},
              headers={}, body=None, params={"media_id": str(media_id)}, app=st)
    with pytest.raises(AppError) as ei:
        asyncio.run(browse.stream_video(ctx))
    assert ei.value.code == "E1001"


# --------------------------------------------------------------------------
# 9. 系统信息里的能力申报
# --------------------------------------------------------------------------

def test_system_info_declares_video_capability(tmp_path, monkeypatch) -> None:
    """前端据此决定要不要显示"视频"筛选：不能靠试探接口。"""
    from app.handlers import system

    monkeypatch.setattr(V, "find_bin", lambda name, configured_dir="": f"/{name}")
    cfg = _cfg(tmp_path)
    info = system._video_info(cfg)          # noqa: SLF001
    assert info["enabled"] is True
    assert info["available"] is True
    assert "mp4" in info["exts"]


def test_system_info_reports_unavailable_when_ffmpeg_missing(tmp_path, monkeypatch) -> None:
    from app.handlers import system

    def boom(name: str, configured_dir: str = "") -> str:
        raise V.FFmpegMissing("nope")

    monkeypatch.setattr(V, "find_bin", boom)
    info = system._video_info(_cfg(tmp_path))    # noqa: SLF001
    assert info["enabled"] is True and info["available"] is False




# --------------------------------------------------------------------------
# 10. 播放建议（hint）：比布尔值多一档"看客户端"
# --------------------------------------------------------------------------

@pytest.mark.parametrize("container,vcodec,hint", [
    ("mp4", "h264", V.HINT_OK),
    ("mov", "h264", V.HINT_OK),
    ("webm", "vp9", V.HINT_OK),
    ("mp4", "hevc", V.HINT_MAYBE),
    ("mov", "hevc", V.HINT_MAYBE),
    ("mp4", "", V.HINT_MAYBE),
    ("mkv", "h264", V.HINT_NO),
    ("avi", "mpeg4", V.HINT_NO),
    ("mp4", "mpeg4", V.HINT_NO),
])
def test_playback_hint_matrix(container: str, vcodec: str, hint: str) -> None:
    assert V.playback_hint(container=container, vcodec=vcodec) == hint


def test_hevc_is_maybe_not_no() -> None:
    """HEVC 在 iOS Safari 能播 —— 判 `no` 会让主客户端白白退回下载。"""
    assert V.playback_hint(container="mov", vcodec="hevc") == V.HINT_MAYBE
    # 但 `playable` 仍是 False：那是"Chrome 桌面确定能播"的结论
    assert V.is_playable(container="mov", vcodec="hevc") is False


def test_detail_carries_hint_for_video_rows() -> None:
    from app.handlers.common import _video_block

    row = {"kind": "video", "duration": 3.0, "container": "mov",
           "vcodec": "hevc", "acodec": "aac"}
    block = _video_block(row)
    assert block["hint"] == V.HINT_MAYBE
    assert block["playable"] is False


def test_detail_video_block_is_none_for_images() -> None:
    from app.handlers.common import _video_block

    assert _video_block({"kind": "image"}) is None
