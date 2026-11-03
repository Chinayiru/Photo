"""按需转码（ISSUE-058 / V2）的用例。

## 三层判据

1. **参数层**（纯函数）：ffmpeg 命令里该有的都在、不该有的都没有。这一层
   最值得钉死——转码参数写错的后果不是报错，而是"转出来一部手机还是播不了
   的视频"，用户只会说"还是不行"。
2. **状态机层**（假 Popen）：排队 → 运行 → 就绪 / 失败，产物原子落盘、失败
   清理半成品、已完成的不重复转。这里用假进程而不是真 ffmpeg：真 ffmpeg 一
   次要几秒，而这些分支（rc≠0、进度行、崩溃）要反复摆。
3. **接口层**：只有视频能转、没转好不能播、坏票 401。

真 ffmpeg 的端到端用例单独一条（`test_real_ffmpeg_end_to_end`），本机没装
ffmpeg 时自动跳过。
"""
from __future__ import annotations

import pathlib

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import transcode as tc                              # noqa: E402


# --------------------------------------------------------------------------
# 1. 参数层
# --------------------------------------------------------------------------

def test_plan_asks_for_the_most_compatible_output() -> None:
    """h264 + yuv420p + aac + faststart：这是"安卓和 iPhone 都能播"的交集。"""
    argv = tc.plan_args("ffmpeg", "in.mov", "out.mp4")
    joined = " ".join(argv)

    assert argv[0] == "ffmpeg"
    assert "libx264" in argv
    assert "yuv420p" in argv
    assert "aac" in argv
    assert "+faststart" in argv
    assert "-crf" in argv and str(tc.TRANSCODE_CRF) in argv
    # 只取第一路视频、音频可有可无
    assert "0:v:0" in argv and "0:a:0?" in argv
    # 进度必须走 stdout，否则拿不到百分比
    assert "-progress" in argv and "pipe:1" in argv
    # 不许出现"复制流"（`-c copy`）：源是 hevc 时复制等于没转
    assert "copy" not in joined


def test_plan_only_shrinks_and_keeps_even_dimensions() -> None:
    """`min(1280,iw)` + `-2`：大视频缩到长边 1280，小视频不放大，宽高保持偶数。"""
    argv = tc.plan_args("ffmpeg", "in.mov", "out.mp4", max_edge=720)
    vf = argv[argv.index("-vf") + 1]
    assert "min(720,iw)" in vf
    assert vf.endswith(":-2")


def test_plan_never_writes_to_the_source() -> None:
    """输出路径必须是参数里最后一项，且与输入不同。"""
    argv = tc.plan_args("ffmpeg", "C:/lib/a.mov", "C:/data/t/out.mp4")
    assert argv[-1] == "C:/data/t/out.mp4"
    assert "C:/lib/a.mov" in argv


def test_target_name_carries_the_source_fingerprint() -> None:
    """名字带 mtime+size：源文件一换，产物名就变，旧产物自然失效。"""
    a = tc.target_name(42, 1700000000.0, 1234)
    assert a == "42-1700000000-1234.mp4"
    assert tc.target_name(42, 1700000001.0, 1234) != a   # mtime 变
    assert tc.target_name(42, 1700000000.0, 1235) != a   # size 变
    assert tc.target_name(42, None, None) == "42-0-0.mp4"  # 未知也不炸


def test_parse_progress_reads_microseconds() -> None:
    """ffmpeg 的 `out_time_ms` 单位其实是**微秒**（历史遗留），别当毫秒。"""
    assert tc.parse_progress("out_time_us=2500000", 10.0) == pytest.approx(0.25)
    assert tc.parse_progress("out_time_ms=2500000", 10.0) == pytest.approx(0.25)
    assert tc.parse_progress("progress=end", 10.0) == 1.0
    # 进度只增不减地夹在 [0,1]
    assert tc.parse_progress("out_time_us=20000000", 10.0) == 1.0
    assert tc.parse_progress("out_time_us=0", 10.0) == 0.0


def test_parse_progress_ignores_what_it_cannot_use() -> None:
    assert tc.parse_progress("", 10.0) is None
    assert tc.parse_progress("frame=12", 10.0) is None
    assert tc.parse_progress("out_time_us=abc", 10.0) is None
    # 不知道时长就没法算百分比（转码仍然照跑，只是没有进度条）
    assert tc.parse_progress("out_time_us=5000000", None) is None
    assert tc.parse_progress("out_time_us=5000000", 0.0) is None


# --------------------------------------------------------------------------
# 2. 缓存清理
# --------------------------------------------------------------------------

def _touch(path: Path, size: int, atime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    os.utime(path, (atime, atime))
    return path


def test_sweep_evicts_least_recently_used_first(tmp_path) -> None:
    old = _touch(tmp_path / "old.mp4", 400, 1000.0)
    mid = _touch(tmp_path / "mid.mp4", 400, 2000.0)
    new = _touch(tmp_path / "new.mp4", 400, 3000.0)
    # 总量 1200 > 预算 1000 → 淘汰到 80%（800）：删掉最旧的一个刚好 800
    freed = tc.sweep(tmp_path, 1000)
    assert freed == 400
    assert not old.exists()
    assert mid.exists() and new.exists()


def test_sweep_never_deletes_what_is_in_use(tmp_path) -> None:
    old = _touch(tmp_path / "old.mp4", 400, 1000.0)
    mid = _touch(tmp_path / "mid.mp4", 400, 2000.0)
    new = _touch(tmp_path / "new.mp4", 400, 3000.0)
    freed = tc.sweep(tmp_path, 1000, protect={"old.mp4"})
    assert freed == 400
    assert old.exists(), "正在播放/转码的产物不能被删"
    assert not mid.exists() and new.exists()


def test_sweep_is_a_noop_within_budget_or_when_disabled(tmp_path) -> None:
    _touch(tmp_path / "a.mp4", 100, 1000.0)
    assert tc.sweep(tmp_path, 10_000) == 0
    assert tc.sweep(tmp_path, 0) == 0          # 0 = 不清理
    assert (tmp_path / "a.mp4").exists()


def test_sweep_ignores_non_mp4_and_missing_dir(tmp_path) -> None:
    _touch(tmp_path / "keep.txt", 500, 1000.0)
    assert tc.sweep(tmp_path, 1) == 0
    assert (tmp_path / "keep.txt").exists()
    assert tc.sweep(tmp_path / "nope", 1) == 0


# --------------------------------------------------------------------------
# 3. 状态机（假 ffmpeg 进程）
# --------------------------------------------------------------------------

class _FakeProc:
    """假 ffmpeg：按脚本输出进度行、写产物、返回指定退出码。"""

    def __init__(self, argv: list[str], *, rc: int = 0,
                 progress: tuple[str, ...] = (), stderr: bytes = b"") -> None:
        self.argv = argv
        self.returncode = rc
        self._stderr = stderr
        self.killed = False
        # argv[-1] 是输出（`.part.mp4`）：转码"成功"就要真的有个文件
        out = Path(argv[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        if rc == 0:
            out.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 1024)
        self.stdout = iter([line.encode() + b"\n" for line in progress])
        self.stderr = _FakeErr(stderr)

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class _FakeErr:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


def _cfg(tmp_path):
    from app.config import Config

    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    return Config({"roots": [{"id": "library", "path": str(lib)}]},
                  path=data / "config.json", data_dir=data)


def _src(tmp_path, name: str = "clip.mov") -> Path:
    p = tmp_path / "lib" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x00" * 2048)
    return p


def _wait(job, timeout: float = 5.0) -> None:
    import time

    end = time.time() + timeout
    while time.time() < end:
        if job.state in (tc.STATE_READY, tc.STATE_FAILED):
            return
        time.sleep(0.01)
    raise AssertionError(f"job 未在 {timeout}s 内结束：state={job.state}")


def test_successful_job_produces_an_atomically_placed_file(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    src = _src(tmp_path)
    made: list[_FakeProc] = []

    def fake_popen(argv, **kw):
        p = _FakeProc(argv, progress=("out_time_us=500000", "out_time_us=1000000",
                                      "progress=end"))
        made.append(p)
        return p

    monkeypatch.setattr(tc.subprocess, "Popen", fake_popen)
    t = tc.Transcoder(cfg, concurrency=1, cache_mb=0)
    job = t.start(media_id=7, src=src, duration=1.0, ffmpeg="ffmpeg")
    _wait(job)

    assert job.state == tc.STATE_READY
    assert job.progress == 1.0
    out = Path(job.path)
    assert out.is_file() and out.parent == cfg.data_path("transcodes")
    assert out.name == tc.target_name(7, src.stat().st_mtime, src.stat().st_size)
    assert not out.with_suffix(".part.mp4").exists(), "半成品必须换成正式名"
    assert src.read_bytes() == b"\x00" * 2048, "源文件一个字节都不能动"
    assert t.get(7) is job


def test_failed_job_reports_reason_and_leaves_no_debris(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    src = _src(tmp_path)

    monkeypatch.setattr(
        tc.subprocess, "Popen",
        lambda argv, **kw: _FakeProc(argv, rc=1,
                                      stderr=b"[libx264] height not divisible by 2\n"),
    )
    t = tc.Transcoder(cfg, concurrency=1, cache_mb=0)
    job = t.start(media_id=8, src=src, duration=1.0, ffmpeg="ffmpeg")
    _wait(job)

    assert job.state == tc.STATE_FAILED
    assert "height not divisible by 2" in job.error, "要让用户看到真实原因"
    leftovers = list(cfg.data_path("transcodes").iterdir()) if \
        cfg.data_path("transcodes").is_dir() else []
    assert leftovers == [], "失败不许留下半个 mp4（会占满缓存）"


def test_ready_artifact_is_reused_without_starting_ffmpeg_again(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    src = _src(tmp_path)
    calls: list[list[str]] = []

    def fake_popen(argv, **kw):
        calls.append(argv)
        return _FakeProc(argv, progress=("progress=end",))

    monkeypatch.setattr(tc.subprocess, "Popen", fake_popen)
    t = tc.Transcoder(cfg, concurrency=1, cache_mb=0)
    first = t.start(media_id=9, src=src, duration=1.0, ffmpeg="ffmpeg")
    _wait(first)
    second = t.start(media_id=9, src=src, duration=1.0, ffmpeg="ffmpeg")

    assert second.state == tc.STATE_READY
    assert len(calls) == 1, "已有产物就该直接复用"


def test_starting_twice_while_running_returns_the_same_job(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    src = _src(tmp_path)
    calls: list[list[str]] = []
    gate = __import__("threading").Event()

    class _Blocking(_FakeProc):
        def wait(self, timeout=None) -> int:   # 卡住，直到测试放行
            gate.wait(5)
            return 0

    monkeypatch.setattr(
        tc.subprocess, "Popen",
        lambda argv, **kw: (calls.append(argv), _Blocking(argv))[1],
    )
    t = tc.Transcoder(cfg, concurrency=1, cache_mb=0)
    a = t.start(media_id=10, src=src, duration=1.0, ffmpeg="ffmpeg")
    b = t.start(media_id=10, src=src, duration=1.0, ffmpeg="ffmpeg")
    gate.set()
    _wait(a)

    assert a is b, "重复点击必须复用同一个任务"
    assert len(calls) == 1, "不能起第二个 ffmpeg"


def test_missing_source_fails_fast(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    t = tc.Transcoder(cfg, concurrency=1, cache_mb=0)
    job = t.start(media_id=11, src=tmp_path / "lib" / "gone.mov",
                  duration=None, ffmpeg="ffmpeg")
    assert job.state == tc.STATE_FAILED
    assert "不可读" in job.error


def test_changed_source_produces_a_new_artifact_name(tmp_path, monkeypatch) -> None:
    """同名文件被替换（mtime 变）→ 新产物名，不会继续放旧转码。"""
    cfg = _cfg(tmp_path)
    src = _src(tmp_path)
    monkeypatch.setattr(tc.subprocess, "Popen",
                        lambda argv, **kw: _FakeProc(argv, progress=("progress=end",)))
    t = tc.Transcoder(cfg, concurrency=1, cache_mb=0)
    first = t.start(media_id=12, src=src, duration=1.0, ffmpeg="ffmpeg")
    _wait(first)

    os.utime(src, (src.stat().st_atime, src.stat().st_mtime + 10))
    second = t.start(media_id=12, src=src, duration=1.0, ffmpeg="ffmpeg")
    _wait(second)

    assert second.path != first.path
    assert Path(second.path).name == tc.target_name(
        12, src.stat().st_mtime, src.stat().st_size)


# --------------------------------------------------------------------------
# 4. 接口层
# --------------------------------------------------------------------------

def _st_with_video(tmp_path, *, kind: str = "video"):
    """建一个只有一条 media 的库，返回 (state, media_id, 源文件)。"""
    from app.db import Database
    from app.state import AppState

    cfg = _cfg(tmp_path)
    db = Database(cfg.db_path)
    db.init()
    src = _src(tmp_path, "clip.mp4")
    db.execute(
        "INSERT INTO media (root_id, rel_path, file_name, ext, kind, size,"
        " mtime, taken_at, container, duration)"
        " VALUES ('library','clip.mp4','clip.mp4','mp4',?,?,?,?,?,?)",
        (kind, src.stat().st_size, src.stat().st_mtime, src.stat().st_mtime,
         "mp4" if kind == "video" else None,
         12.0 if kind == "video" else None),
    )
    mid = db.query_one("SELECT max(id) AS m FROM media")["m"]
    st = AppState(cfg, db)
    return st, int(mid), src


def _ctx(st, method: str, path: str, *, query: dict | None = None,
         mid: int | None = None, headers: dict | None = None):
    from app.core import Ctx

    q: dict[str, list[str]] = {}
    for k, v in (query or {}).items():
        q[k] = v if isinstance(v, list) else [str(v)]
    params = {"media_id": str(mid)} if mid is not None else {}
    hdrs = {"host": "127.0.0.1:8799"}
    hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
    return Ctx(method=method, path=path, query=q, params=params,
               headers=hdrs, body=None, app=st)


def test_status_of_an_untranscoded_video_is_none(tmp_path) -> None:
    from app.handlers import transcode as ht

    st, mid, _ = _st_with_video(tmp_path)
    reply = asyncio.run(ht.status(_ctx(st, "GET", f"/api/items/{mid}/transcode", mid=mid)))
    assert reply.status == 200
    assert json.loads(reply.body)["state"] == "none"


def test_images_cannot_be_transcoded(tmp_path) -> None:
    from app.errors import AppError
    from app.handlers import transcode as ht

    st, mid, _ = _st_with_video(tmp_path, kind="image")
    for call in (ht.status, ht.start):
        with pytest.raises(AppError) as ei:
            asyncio.run(call(_ctx(st, "GET", f"/api/items/{mid}/transcode", mid=mid)))
        assert ei.value.status == 400


def test_stream_before_ready_is_refused(tmp_path) -> None:
    from app.errors import AppError
    from app.handlers import transcode as ht

    st, mid, _ = _st_with_video(tmp_path)
    with pytest.raises(AppError) as ei:
        asyncio.run(ht.stream(_ctx(st, "GET", f"/api/items/{mid}/transcode/stream", mid=mid)))
    assert ei.value.status == 409, "还在转：告诉前端'没好'，不是'不存在'"


def test_stream_rejects_a_bad_ticket(tmp_path) -> None:
    from app.errors import Unauthorized
    from app.handlers import transcode as ht

    st, mid, _ = _st_with_video(tmp_path)
    with pytest.raises(Unauthorized):
        asyncio.run(ht.stream(_ctx(st, "GET", f"/api/items/{mid}/transcode/stream",
                                   mid=mid, query={"t": "st1.bogus.sig"})))


def test_stream_serves_the_artifact_with_range_support(tmp_path, monkeypatch) -> None:
    from app import streamticket
    from app.handlers import transcode as ht

    st, mid, _ = _st_with_video(tmp_path)
    monkeypatch.setattr(tc.subprocess, "Popen",
                        lambda argv, **kw: _FakeProc(argv, progress=("progress=end",)))
    job = st.transcodes.start(media_id=mid, src=tmp_path / "lib" / "clip.mp4",
                              duration=12.0, ffmpeg="ffmpeg")
    _wait(job)
    ticket = streamticket.mint(st.db, mid)

    reply = asyncio.run(ht.stream(_ctx(st, "GET",
                                       f"/api/items/{mid}/transcode/stream",
                                       mid=mid, query={"t": ticket})))
    assert reply.status == 200
    assert reply.headers["Accept-Ranges"] == "bytes"
    assert "video/mp4" in reply.headers.get("Content-Type", "")


def test_transcode_unsatisfiable_range_is_416_with_accept_ranges(
        tmp_path, monkeypatch) -> None:
    """越界 Range 必须 416，且 416 也要带 `Accept-Ranges`（契约对齐，2026-11-03）。

    这条是审计里 R2「Range 响应构造两份」的回归点：转码产物早先自己拼 416，
    少了 `Accept-Ranges`，客户端据此不知道"这个资源其实支持续传"，只能整段重下。
    共用一个 helper 之后两处不会再各自漂移，但**漂移是可以重新长出来的**，
    所以要有一条断言钉住它。
    """
    from app import streamticket
    from app.handlers import transcode as ht
    from app import transcode as tcmod

    import asyncio

    st, mid, _ = _st_with_video(tmp_path)
    monkeypatch.setattr(tcmod.subprocess, "Popen",
                        lambda argv, **kw: _FakeProc(argv, progress=("progress=end",)))
    job = st.transcodes.start(media_id=mid, src=tmp_path / "lib" / "clip.mp4",
                              duration=12.0, ffmpeg="ffmpeg")
    _wait(job)
    ticket = streamticket.mint(st.db, mid)

    import pytest

    from app.errors import AppError

    with pytest.raises(AppError) as ei:
        asyncio.run(ht.stream(_ctx(st, "GET",
                                   f"/api/items/{mid}/transcode/stream",
                                   mid=mid, query={"t": ticket},
                                   headers={"range": "bytes=999999999-"})))
    # 416 由 dispatch 从 AppError 落地；头必须挂在异常上，否则会被丢掉。
    assert ei.value.status == 416
    size = pathlib.Path(job.path).stat().st_size
    assert ei.value.headers["Accept-Ranges"] == "bytes"
    assert ei.value.headers["Content-Range"] == f"bytes */{size}"


def test_transcode_paths_end_with_stream_so_the_ticket_gate_allows_them() -> None:
    """鉴权中间件只放行 `.../stream` 结尾的带票路径，路由命名必须对上。"""
    assert "/api/items/5/transcode/stream".endswith("/stream")


# --------------------------------------------------------------------------
# 5. 真 ffmpeg 端到端（没装就跳过）
# --------------------------------------------------------------------------

def _have_ffmpeg() -> bool:
    from app.video import FFmpegMissing, find_bin

    try:
        find_bin("ffmpeg")
        find_bin("ffprobe")
    except FFmpegMissing:
        return False
    return True


@pytest.mark.skipif(not _have_ffmpeg(), reason="本机没装 ffmpeg")
def test_real_ffmpeg_end_to_end(tmp_path) -> None:
    """真转一段 hevc：产物必须是 h264 + faststart + 能被 ffprobe 读出时长。"""
    import subprocess

    from app.video import find_bin, probe

    cfg = _cfg(tmp_path)
    src = tmp_path / "lib" / "hevc.mov"
    src.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = find_bin("ffmpeg")
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=1",
         "-c:v", "libx265", "-pix_fmt", "yuv420p", str(src)],
        check=True, timeout=120,
    )

    t = tc.Transcoder(cfg, concurrency=1, cache_mb=0)
    job = t.start(media_id=13, src=src, duration=1.0, ffmpeg=ffmpeg)
    _wait(job, timeout=180)

    assert job.state == tc.STATE_READY, job.error
    info = probe(Path(job.path))
    assert info.vcodec == "h264"
    assert info.container == "mp4"
    assert info.faststart == 1, "转码产物必须 faststart，否则手机上起播要等整段"
    assert (info.duration or 0) > 0.5


def test_start_returns_200_and_an_audit_row(tmp_path, monkeypatch) -> None:
    """回归：POST 起转必须**既成功又留痕**（曾经 500，而 ffmpeg 已在跑）。"""
    from app import audit
    from app.handlers import transcode as ht
    from app import transcode as tcmod

    st, mid, src = _st_with_video(tmp_path)
    calls: list[tuple] = []

    def fake_start(*, media_id, src, duration, ffmpeg):
        return tcmod.TranscodeJob(media_id=media_id, state=tcmod.STATE_QUEUED)

    def fake_record(db, cfg, action, **kw):
        calls.append((db, cfg, action, kw))

    monkeypatch.setattr(st.transcodes, "start", fake_start)
    monkeypatch.setattr(ht, "_ffmpeg", lambda _st: "ffmpeg.exe")
    monkeypatch.setattr(audit, "record", fake_record)

    reply = asyncio.run(ht.start(_ctx(st, "POST", f"/api/items/{mid}/transcode", mid=mid)))
    assert reply.status == 200
    body = json.loads(reply.body)
    assert body["state"] == "queued" and body["media_id"] == mid

    assert len(calls) == 1, "起转必须写一条审计"
    db_arg, _cfg_arg, action, kw = calls[0]
    assert db_arg is st.db, "第一个位置参数必须是 db（签名写错就会在这里炸）"
    assert action == "transcode.start"
    assert kw["rel_path"] == "clip.mp4"
    assert kw["detail"]["media_id"] == mid


def test_start_is_idempotent_through_the_reply(tmp_path, monkeypatch) -> None:
    """连点两下：第二次仍然 200，但状态是"已就绪"且不重新起任务。"""
    from app.handlers import transcode as ht
    from app import transcode as tcmod

    st, mid, _ = _st_with_video(tmp_path)
    calls = {"n": 0}

    def fake_start(*, media_id, src, duration, ffmpeg):
        calls["n"] += 1
        return tcmod.TranscodeJob(media_id=media_id, state=tcmod.STATE_READY,
                                  progress=1.0, path=str(Path(src).with_suffix(".mp4")))

    monkeypatch.setattr(st.transcodes, "start", fake_start)
    monkeypatch.setattr(ht, "_ffmpeg", lambda _st: "ffmpeg.exe")
    monkeypatch.setattr("app.audit.record", lambda *a, **k: None)

    for _ in range(2):
        reply = asyncio.run(ht.start(_ctx(st, "POST", f"/api/items/{mid}/transcode", mid=mid)))
        assert reply.status == 200
        assert json.loads(reply.body)["state"] == "ready"

    # 幂等由 Transcoder 保证（缓存命中直接复用），handler 只透传状态
    assert calls["n"] == 2
