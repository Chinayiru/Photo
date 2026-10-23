"""thumbs.py 单元测试。

重点覆盖三类最容易出错的地方：
  1. EXIF Orientation 的**方向**（搞反了照片就是倒的，且不容易发现）
  2. 大图内存控制（48MP 图 4 并发 = 600MB，越大越会 OOM）
  3. 缓存失效与原子落盘（半截 WebP 被浏览器永久缓存是最恶心的 bug）
"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import thumbs  # noqa: E402
from app.thumbs import (  # noqa: E402
    ORIENTATION_OPS,
    THUMB_SIZES,
    _apply_orientation,
    _open_downscaled,
    generate_thumb,
    placeholder_webp,
    source_signature,
    thumb_key,
    thumb_path,
)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

#: 扩展名 → Pillow 格式名。Pillow 认 "JPEG" 不认 "JPG"，
#: 直接拿后缀大写去 save 会 KeyError: 'JPG'。
_PIL_FORMAT = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "webp": "WEBP"}


def _fmt_for(path: Path, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    ext = path.suffix.lstrip(".").lower()
    return _PIL_FORMAT.get(ext, ext.upper())


def _make_image(path: Path, size: tuple[int, int], color=(200, 100, 50),
                *, orientation: int | None = None, fmt: str | None = None) -> Path:
    """造一张测试图。orientation 非 None 时写入 EXIF。"""
    im = Image.new("RGB", size, color)
    kwargs = {}
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif.tobytes()
    im.save(path, _fmt_for(path, fmt), **kwargs)
    return path


def _marker_image(path: Path, size: tuple[int, int], block: int = 10) -> Path:
    """白底 + 左上角红色方块，用于验证旋转**方向**。

    刻意存成 PNG（无损）：JPEG 的色度二次采样会把红点糊成粉色，
    让 `> 200` 这类阈值断言变得不可靠。用方块而不是单像素，
    是因为 WebP 有损压缩会在孤立像素上产生明显的颜色偏移。
    """
    im = Image.new("RGB", size, (255, 255, 255))
    for y in range(block):
        for x in range(block):
            im.putpixel((x, y), (255, 0, 0))
    im.save(path, "PNG")
    return path


def _corner_is_red(im: Image.Image, corner: str, block: int = 10) -> bool:
    """检查指定角落的方块是否仍是红色（容忍 WebP 有损压缩）。"""
    rgb = im.convert("RGB")
    w, h = rgb.size
    box = {
        "tl": (0, 0, block, block),
        "tr": (w - block, 0, w, block),
        "bl": (0, h - block, block, h),
        "br": (w - block, h - block, w, h),
    }[corner]
    region = rgb.crop(box)
    # 用 ImageStat 而不是 getdata()：后者在 Pillow 14 被移除（DeprecationWarning），
    # 且一次遍历 C 层算完均值，比拉回 Python 列表再求平均快得多。
    from PIL import ImageStat

    avg_r, avg_g, _ = ImageStat.Stat(region).mean
    return avg_r > 150 and avg_g < 100


def _open(path: Path) -> Image.Image:
    return Image.open(path)


# --------------------------------------------------------------------------
# 缓存键与路径
# --------------------------------------------------------------------------

def test_thumb_key_is_stable_and_distinct() -> None:
    a = thumb_key("library", "2024/05/a.jpg")
    b = thumb_key("library", "2024/05/a.jpg")
    assert a == b
    assert len(a) == 40
    assert thumb_key("library", "2024/05/b.jpg") != a
    # root_id 参与哈希：不同根下的同名文件不能撞缓存
    assert thumb_key("other", "2024/05/a.jpg") != a


def test_thumb_key_no_ambiguity_between_fields() -> None:
    """root_id 与 rel_path 之间用 \\x00 分隔，避免拼接歧义。

    若用 '-' 之类可出现在路径里的字符，("a-b", "c") 与 ("a", "b-c")
    会得到同一个键 —— 两个不同文件共用缓存，显示错误照片。
    """
    assert thumb_key("a-b", "c") != thumb_key("a", "b-c")


def test_thumb_key_handles_chinese_and_long_paths() -> None:
    """中文与超长路径都要能生成定长键（Windows 260 限制）。"""
    k = thumb_key("library", "青甘环线/2024年05月12日 18时30分/" + "很长的目录名/" * 30 + "照片.jpg")
    assert len(k) == 40
    assert k.isalnum()


def test_thumb_path_layout(tmp_path: Path, monkeypatch) -> None:
    """data/thumbs/{root_id}/{hash 前 2 位}/{hash}_{size}.webp"""
    import app.config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path)
    cfg = config_mod.Config({})
    p = thumb_path(cfg, "library", "a/b.jpg", 320)
    assert p.parent.name == p.name.split("_")[0][:2]
    assert p.name.endswith("_320.webp")
    assert p.parts[-3] == "library"


def test_source_signature_changes_with_mtime_and_size() -> None:
    assert source_signature(100.0, 5) != source_signature(101.0, 5)
    assert source_signature(100.0, 5) != source_signature(100.0, 6)


def test_source_signature_has_sub_second_resolution() -> None:
    """微秒精度是必需的，不是锦上添花。

    用户在文件管理器里复制一张同名照片覆盖原图时，新旧 mtime 很容易
    落在同一秒内。若签名只到整秒，两者相等 → ETag 不变 → 缩略图 URL
    带 immutable 缓存一年且不再校验 → 用户**永远**看到旧图。
    """
    assert source_signature(100.001, 5) != source_signature(100.900, 5)
    assert source_signature(100.0001, 5) != source_signature(100.0009, 5)
    # 真实 mtime 量级下不丢精度（float64 精确整数上限 2^53≈9e15）
    assert source_signature(1_700_000_000.123456, 5) != \
        source_signature(1_700_000_000.123457, 5)
    # 亚微秒差异视为相同（NTFS 分辨率 100ns，不会误判）
    assert source_signature(100.0000001, 5) == source_signature(100.0000002, 5)


# --------------------------------------------------------------------------
# Orientation ★ 方向正确性是本模块最容易出错的地方
# --------------------------------------------------------------------------

def test_orientation_6_rotates_clockwise(tmp_path: Path) -> None:
    """Orientation=6（手机竖拍最常见）必须顺时针转 90°。

    用明显非对称的图验证方向，而不是只看尺寸 —— 尺寸对但方向反了
    （转了 270°）同样会通过尺寸断言。左上角的红块顺时针转 90° 后
    应该出现在右上角。
    """
    src = _marker_image(tmp_path / "o6.png", (100, 50))
    out = tmp_path / "o6_out.webp"
    generate_thumb(src, out, 320, orientation=6)

    with _open(out) as r:
        assert r.size == (50, 100), f"尺寸应互换，实际 {r.size}"
        assert _corner_is_red(r, "tr"), "红块应在右上角（顺时针 90°）"
        assert not _corner_is_red(r, "tl")
        assert not _corner_is_red(r, "bl")


def test_orientation_8_rotates_counterclockwise(tmp_path: Path) -> None:
    """Orientation=8 与 6 相差 180°，必须区分开。"""
    src = _marker_image(tmp_path / "o8.png", (100, 50))
    out = tmp_path / "o8_out.webp"
    generate_thumb(src, out, 320, orientation=8)

    with _open(out) as r:
        assert r.size == (50, 100)
        assert _corner_is_red(r, "bl"), "红块应在左下角（逆时针 90°）"
        assert not _corner_is_red(r, "tr")


def test_orientation_3_is_180(tmp_path: Path) -> None:
    src = _marker_image(tmp_path / "o3.png", (100, 50))
    out = tmp_path / "o3_out.webp"
    generate_thumb(src, out, 320, orientation=3)
    with _open(out) as r:
        assert r.size == (100, 50)
        assert _corner_is_red(r, "br")
        assert not _corner_is_red(r, "tl")


def test_orientation_1_and_none_are_noop(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "plain.png", (100, 50))
    for o in (1, None):
        out = tmp_path / f"n{o}.webp"
        generate_thumb(src, out, 320, orientation=o)
        with _open(out) as r:
            assert r.size == (100, 50)


def test_all_orientation_values_covered() -> None:
    """1..8 全覆盖，1 与无效值不产生变换。"""
    assert set(ORIENTATION_OPS) == {2, 3, 4, 5, 6, 7, 8}
    assert 1 not in ORIENTATION_OPS
    im = Image.new("RGB", (10, 10))
    assert _apply_orientation(im, 1) is im
    assert _apply_orientation(im, 0) is im
    assert _apply_orientation(im, None) is im
    assert _apply_orientation(im, 99) is im


def test_orientation_transform_does_not_interpolate(tmp_path: Path) -> None:
    """必须用 transpose 而非 rotate —— rotate 会插值，纯色块边缘会变糊。

    用棋盘格验证：旋转后每个像素仍必须是纯黑或纯白，不能出现灰。
    """
    src = tmp_path / "checker.png"
    im = Image.new("RGB", (8, 8))
    for y in range(8):
        for x in range(8):
            im.putpixel((x, y), (255, 255, 255) if (x + y) % 2 else (0, 0, 0))
    im.save(src)

    out = tmp_path / "checker_out.webp"
    # quality=100 + 无损会掩盖问题，这里用默认质量但尺寸不缩放（8x8 < 320）
    generate_thumb(src, out, 320, orientation=6, quality=100)
    with _open(out) as r:
        colors = {p for _, p in r.convert("RGB").getcolors(maxcolors=1000)}
    # 允许 WebP 有损压缩带来的轻微偏差，但不能出现大量中间灰
    assert len(colors) <= 4, f"出现插值灰阶：{sorted(colors)[:6]}"


def test_generated_thumb_has_no_orientation_tag(tmp_path: Path) -> None:
    """输出 WebP **不能**带 Orientation 标签。

    像素已经转正了；若再保留原图的 Orientation=6，浏览器会再转一次，
    照片就反了。这是必须显式保证的（Pillow 传 exif 会原样保留）。
    """
    src = tmp_path / "tag.jpg"
    im = Image.new("RGB", (100, 50), (10, 20, 30))
    exif = Image.Exif()
    exif[0x0112] = 6
    im.save(src, "JPEG", exif=exif.tobytes())

    out = tmp_path / "tag_out.webp"
    generate_thumb(src, out, 320, orientation=6)
    with _open(out) as r:
        assert r.getexif().get(0x0112) in (None, 1), "缩略图不应携带 Orientation"


# --------------------------------------------------------------------------
# 尺寸与不放大
# --------------------------------------------------------------------------

def test_thumbnail_does_not_upscale(tmp_path: Path) -> None:
    """200x200 的图请求 1600 缩略图，必须保持 200x200。

    resize 会插值放大成 1600x1600 —— 浪费 64 倍带宽且更糊。
    """
    src = _make_image(tmp_path / "small.png", (200, 200))
    out = tmp_path / "small_out.webp"
    w, h = generate_thumb(src, out, 1600)
    assert (w, h) == (200, 200)
    with _open(out) as r:
        assert r.size == (200, 200)


def test_thumbnail_preserves_aspect_ratio(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "wide.jpg", (4000, 3000))
    out = tmp_path / "wide_out.webp"
    w, h = generate_thumb(src, out, 320)
    assert max(w, h) == 320
    assert (w, h) == (320, 240)


def test_thumbnail_target_sizes(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "big.jpg", (6000, 4000))
    for size in THUMB_SIZES:
        out = tmp_path / f"big_{size}.webp"
        w, h = generate_thumb(src, out, size)
        assert max(w, h) == size
        assert out.stat().st_size > 0


def test_output_is_webp(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "x.png", (500, 500))
    out = tmp_path / "x.webp"
    generate_thumb(src, out, 320)
    with _open(out) as r:
        assert r.format == "WEBP"


def test_rgba_and_palette_inputs(tmp_path: Path) -> None:
    """PNG 调色板/带透明通道的图必须能处理。"""
    p1 = tmp_path / "p.png"
    Image.new("P", (100, 100)).save(p1)
    out1 = tmp_path / "p.webp"
    generate_thumb(p1, out1, 320)

    p2 = tmp_path / "rgba.png"
    Image.new("RGBA", (100, 100), (255, 0, 0, 128)).save(p2)
    out2 = tmp_path / "rgba.webp"
    generate_thumb(p2, out2, 320)
    assert out1.exists() and out2.exists()


# --------------------------------------------------------------------------
# 大图内存控制
# --------------------------------------------------------------------------

def test_open_downscaled_uses_draft_for_jpeg(tmp_path: Path) -> None:
    """JPEG 必须走 draft 让解码器直接出小图（内存 1/64）。"""
    src = _make_image(tmp_path / "huge.jpg", (8000, 6000))
    with _open_downscaled(src, 320) as im:
        # draft 后长边应远小于原始 8000（具体值由 libjpeg 的 1/2^n 决定）
        assert max(im.size) < 8000, f"draft 未生效，实际 {im.size}"


def test_open_downscaled_reduce_for_large_target(tmp_path: Path) -> None:
    """非 JPEG（PNG 不支持 draft）走 reduce() 整数倍降采样。"""
    src = _make_image(tmp_path / "huge.png", (4000, 3000))
    with _open_downscaled(src, 320) as im:
        # target*2 = 640，min(4000,3000)//640 = 4 → reduce(4) 后 1000x750
        assert im.size == (1000, 750), f"reduce 未按整数倍生效，实际 {im.size}"


def test_open_downscaled_small_image_untouched(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "tiny.png", (100, 80))
    with _open_downscaled(src, 320) as im:
        assert im.size == (100, 80)


def test_48mp_image_generates_without_oom(tmp_path: Path) -> None:
    """48MP（8192x6144）的图必须能生成缩略图而不是 OOM。

    裸解码是 8192*6144*3 = 151MB RGB；4 个 worker 并发就是 600MB。
    draft/reduce 把它压到可接受范围。这里只验证单张能过 ——
    并发的内存峰值由 worker 信号量（默认 4）约束。

    ★ 这个尺寸是**刻意**选的：它是真实相机的挂机上限（48MP = 8192×6144
    = 5033 万像素），也是 `MAX_IMAGE_PIXELS` 的下界来源。原来这里用的是
    12000×8000（9600 万像素），按 ISSUE-034 收紧后的上限，那种图会被
    Pillow 判成解压炸弹（见 test_max_image_pixels_caps_bomb_memory）——
    也就是说它根本进不到解码这一步，测不到 draft/reduce 的效果。
    """
    src = _make_image(tmp_path / "48mp.png", (8192, 6144))
    out = tmp_path / "48mp.webp"
    w, h = generate_thumb(src, out, 320)
    assert (w, h) == (320, 240)


def test_very_large_image_targets_both_sizes(tmp_path: Path) -> None:
    """大图同时生成 320 与 1600 两个尺寸都要正确（1600 是重解码路径）。"""
    src = _make_image(tmp_path / "xl.jpg", (6000, 4000))
    for size in THUMB_SIZES:
        out = tmp_path / f"xl_{size}.webp"
        w, h = generate_thumb(src, out, size)
        assert max(w, h) == size, f"{size}: {w}x{h}"


def test_corrupt_file_raises_but_does_not_leave_partial(tmp_path: Path) -> None:
    """坏文件必须抛异常（由引擎决定重试），且**不能留下半截输出文件**。

    半截 WebP 被 Service Worker 永久缓存是最恶心的 bug —— 用户之后
    每次打开都是破图，除非清缓存。
    """
    src = tmp_path / "corrupt.jpg"
    src.write_bytes(b"\xff\xd8\xff\xe0 garbage not an image")
    out = tmp_path / "corrupt.webp"

    with pytest.raises(Exception):
        generate_thumb(src, out, 320)

    assert not out.exists(), "失败后不应留下目标文件"
    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert not leftovers, f"残留临时文件：{leftovers}"


def test_tmp_file_is_cleaned_on_success(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "ok.jpg", (500, 400))
    out = tmp_path / "ok.webp"
    generate_thumb(src, out, 320)
    assert out.exists()
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_generate_creates_parent_dirs(tmp_path: Path) -> None:
    src = _make_image(tmp_path / "d.jpg", (500, 400))
    out = tmp_path / "a" / "b" / "c" / "d.webp"
    generate_thumb(src, out, 320)
    assert out.exists()


# --------------------------------------------------------------------------
# 占位图
# --------------------------------------------------------------------------

def test_placeholder_is_valid_webp_and_cached() -> None:
    data = placeholder_webp(320)
    assert data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    with Image.open(io.BytesIO(data)) as im:
        assert im.size == (320, 320)
    assert placeholder_webp(320) is data, "应命中缓存，不重复编码"


def test_placeholder_different_sizes() -> None:
    a, b = placeholder_webp(320), placeholder_webp(1600)
    assert a != b
    with Image.open(io.BytesIO(b)) as im:
        assert im.size == (1600, 1600)


# --------------------------------------------------------------------------
# 引擎（asyncio）
# --------------------------------------------------------------------------

class _FakeRow(dict):
    """模拟 sqlite3.Row 的下标访问。"""

    def __getitem__(self, k):  # type: ignore[override]
        return dict.__getitem__(self, k)


class _FakeDB:
    def __init__(self, rows: list[dict]) -> None:
        self.rows = {r["id"]: dict(r) for r in rows}
        self.updates: list[tuple] = []
        self._lock = __import__("threading").Lock()

    def query_one(self, sql, params=()):  # noqa: ANN001
        return _FakeRow(self.rows[params[0]]) if params[0] in self.rows else None

    def execute(self, sql, params=()):  # noqa: ANN001
        with self._lock:
            self.updates.append(params)
            if params[2] in self.rows:
                self.rows[params[2]]["thumb_state"] = params[0]
                self.rows[params[2]]["thumb_etag"] = params[1]
        return None

    def execute_rowcount(self, sql, params=()):  # noqa: ANN001
        return 0

    def scalar(self, sql, params=(), default=None):  # noqa: ANN001
        return default


def _engine_fixture(tmp_path: Path, monkeypatch, rows: list[dict], workers: int = 2):
    import app.config as config_mod

    monkeypatch.setattr(config_mod, "DATA_DIR", tmp_path / "data")
    cfg = config_mod.Config({"roots": [{"id": "library", "path": str(tmp_path),
                                        "mode": "readwrite", "recursive": True}]})
    db = _FakeDB(rows)
    return thumbs.ThumbEngine(cfg, db, workers=workers), cfg, db  # type: ignore[arg-type]


def test_engine_generates_and_marks_ok(tmp_path: Path, monkeypatch) -> None:
    src = _make_image(tmp_path / "e.jpg", (800, 600))
    st = src.stat()
    rows = [{"id": 1, "root_id": "library", "rel_path": "e.jpg", "mtime": st.st_mtime,
             "size": st.st_size, "orientation": 1, "thumb_state": "pending",
             "thumb_etag": None, "missing": 0}]
    eng, cfg, db = _engine_fixture(tmp_path, monkeypatch, rows)

    async def main() -> None:
        eng.start()
        eng.enqueue(1)
        await eng.queue.join()
        await eng.stop()

    asyncio.run(main())

    assert eng.done == 1 and eng.failed == 0
    for size in THUMB_SIZES:
        assert thumb_path(cfg, "library", "e.jpg", size).exists()
    assert db.rows[1]["thumb_state"] == "ok"


def test_engine_skips_missing_rows(tmp_path: Path, monkeypatch) -> None:
    rows = [{"id": 7, "root_id": "library", "rel_path": "gone.jpg", "mtime": 1.0,
             "size": 1, "orientation": 1, "thumb_state": "pending",
             "thumb_etag": None, "missing": 1}]
    eng, _, _ = _engine_fixture(tmp_path, monkeypatch, rows)

    async def main() -> None:
        eng.start()
        eng.enqueue(7)
        await eng.queue.join()
        await eng.stop()

    asyncio.run(main())
    assert eng.skipped == 1 and eng.done == 0


def test_engine_cache_hit_skips_regeneration(tmp_path: Path, monkeypatch) -> None:
    """源文件未变时第二次入队必须直接跳过，不重新编码。"""
    src = _make_image(tmp_path / "c.jpg", (800, 600))
    st = src.stat()
    sig = source_signature(st.st_mtime, st.st_size)
    rows = [{"id": 2, "root_id": "library", "rel_path": "c.jpg", "mtime": st.st_mtime,
             "size": st.st_size, "orientation": 1, "thumb_state": "ok",
             "thumb_etag": sig, "missing": 0}]
    eng, cfg, _ = _engine_fixture(tmp_path, monkeypatch, rows)

    async def main() -> None:
        eng.start()
        # 先把缓存文件造出来
        for size in THUMB_SIZES:
            p = thumb_path(cfg, "library", "c.jpg", size)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"cached")
        eng.enqueue(2)
        await eng.queue.join()
        await eng.stop()

    asyncio.run(main())
    assert eng.skipped == 1
    # 内容未被覆盖，证明没有重新生成
    assert thumb_path(cfg, "library", "c.jpg", 320).read_bytes() == b"cached"


def test_engine_bad_file_goes_to_retry_not_failed_immediately(
    tmp_path: Path, monkeypatch
) -> None:
    """首次失败应进入重试，而不是直接标 failed。

    瞬时失败（文件被占用、磁盘忙）通常 1 次重试就过；直接标 failed
    会让用户在杀毒软件扫描时就永久失去缩略图。
    """
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not an image")
    st = bad.stat()
    rows = [{"id": 3, "root_id": "library", "rel_path": "bad.jpg", "mtime": st.st_mtime,
             "size": st.st_size, "orientation": 1, "thumb_state": "pending",
             "thumb_etag": None, "missing": 0}]
    eng, _, db = _engine_fixture(tmp_path, monkeypatch, rows)

    async def main() -> None:
        eng.start()
        eng.enqueue(3)
        await eng.queue.join()
        await asyncio.sleep(0.05)
        await eng.stop()

    asyncio.run(main())
    assert eng.failed == 0, "首次失败不该直接标 failed"
    assert db.rows[3]["thumb_state"] == "pending", "应保持 pending 等待重试"


def test_engine_queue_full_drops_without_blocking(tmp_path: Path, monkeypatch) -> None:
    """队列满时 enqueue 必须立即返回 False，不能阻塞索引器。"""
    rows = [{"id": i, "root_id": "library", "rel_path": f"{i}.jpg", "mtime": 1.0,
             "size": 1, "orientation": 1, "thumb_state": "pending",
             "thumb_etag": None, "missing": 0} for i in range(1, 20)]
    eng, _, _ = _engine_fixture(tmp_path, monkeypatch, rows)
    eng.queue = asyncio.Queue(maxsize=3)

    async def main() -> None:
        accepted = eng.enqueue_many(range(1, 20))
        assert accepted == 3
        assert eng.enqueue(99) is False

    asyncio.run(main())


def test_engine_stats_shape(tmp_path: Path, monkeypatch) -> None:
    eng, _, _ = _engine_fixture(tmp_path, monkeypatch, [])
    s = eng.stats()
    assert {"done", "failed", "skipped", "queued", "pending",
            "failed_total", "workers"} <= set(s)


def test_engine_stop_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    eng, _, _ = _engine_fixture(tmp_path, monkeypatch, [])

    async def main() -> None:
        eng.start()
        await eng.stop()
        await eng.stop()          # 第二次不应抛异常

    asyncio.run(main())


# ---------------------------------------------------------------------------
# 关机回收回归
#
# 事故经过：`server._shutdown` 曾用
# `asyncio.wait_for(st.thumbs.stop(drain=True), timeout=20.0)` 从外面掐超时。
# 超时会 **cancel 这个协程**，而当时的回收代码写在 `await queue.join()`
# 之后（没有 try/finally），于是回收整段被跳过；又因为 `_running` 已置
# False，第二次 stop() 直接 early-return —— worker 与线程池
# **永远不会被回收**。
#
# 实测症状（`scripts/verify_lan_e2e.py` 跑真实库时暴露）：
#   - 事件循环关闭刷出 4 条 `Task was destroyed but it is pending!`
#   - 托盘退出看起来卡死，只能去任务管理器杀进程（强杀留下 WAL）
#
# 修法：drain 的超时长在函数内部（`drain_timeout`），超时只停止等待、
# 不取消协程，回收在同一协程里走完；`finally` 作为第二道保险。
#
# 下面两条分别钉住「行为」与「结构」：行为测外部取消下不泄漏，
# 结构测 server 不再使用那个脆弱的外部 wait_for 写法。
# ---------------------------------------------------------------------------


def test_engine_survives_external_cancel_without_leaking_workers(
        tmp_path: Path, monkeypatch) -> None:
    """★ 事故直接回归：外部 cancel 之后 worker 与线程池必须已被回收。

    实测对照（本机）：

    - 修复前：`workers after external cancel: 2`、`_stopped: False`、
      `LEAKED worker tasks: 2` —— 事件循环关闭时这 2 个任务被销毁并刷警告。
    - 修复后：`workers after external cancel: 0`、`_stopped: True`、
      `LEAKED worker tasks: 0`。

    构造方式忠实于真实场景：让 `_generate_one` 永久阻塞（等价于队列里
    堆了 2000 张待生成缩略图、20 秒跑不完），这样 `queue.join()` 真的挂住，
    外部 `wait_for` 才会超时并取消。
    """
    import pytest as _pytest

    eng, _, _ = _engine_fixture(tmp_path, monkeypatch, [], workers=2)

    async def never_finishes(media_id: int) -> bool:
        await asyncio.Event().wait()          # 永久阻塞
        return True                            # pragma: no cover

    monkeypatch.setattr(eng, "_generate_one", never_finishes)

    async def main() -> None:
        eng.start()
        assert len(eng._tasks) == 2
        for _ in range(5):
            eng.enqueue(999)
        # ★ 这正是 server._shutdown 早期的写法
        with _pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(eng.stop(drain=True), timeout=0.3)

    asyncio.run(main())

    leaked = [t for t in eng._tasks if not t.done()]
    assert not leaked, f"worker 泄漏 {len(leaked)} 个: {leaked}"
    assert eng._stopped is True, "外部取消后必须已完成回收"
    assert eng.queue.empty(), f"队列未清空，剩余 {eng.queue.qsize()}"
    # 线程池已 shutdown：再提交任务必须抛 RuntimeError
    with _pytest.raises(RuntimeError):
        eng._pool.submit(lambda: None)


def test_server_shutdown_passes_internal_drain_timeout() -> None:
    """结构回归：server._shutdown 必须用 drain_timeout，不能用外部 wait_for。

    行为测试只能证明"外部取消时不会泄漏"，但无法阻止有人把外部
    `wait_for` 写回去（那样每次正常关机都要依赖取消路径的鲁棒性）。

    这里走 AST 而不是字符串匹配：注释里提到 `wait_for`（解释为什么
    不能用它）是正常的，字符串匹配会误报 —— 这正是本仓库
    `scripts/check_docs.py` 踩过的坑（代码块里的正则被当成断链）。
    """
    import ast
    import inspect
    import textwrap

    from backend.app import server as server_mod

    src = textwrap.dedent(inspect.getsource(server_mod._shutdown))
    tree = ast.parse(src)

    # 收集所有被调用的名字，形如 asyncio.wait_for(...)
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                called.add(f.attr)
            elif isinstance(f, ast.Name):
                called.add(f.id)

    assert "wait_for" not in called, (
        "server._shutdown 不应调用 wait_for —— 它会取消协程，"
        "使 ThumbEngine 的 worker 与线程池回收被跳过（真实事故）"
    )

    # 必须把超时作为参数传进 stop()
    stop_calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "stop"
    ]
    assert stop_calls, "server._shutdown 应调用 thumbs.stop()"
    assert any(
        kw.arg == "drain_timeout" for c in stop_calls for kw in c.keywords
    ), "thumbs.stop() 必须收到 drain_timeout"


def test_engine_stop_after_drain_timeout_does_not_hang(tmp_path: Path,
                                                       monkeypatch) -> None:
    """超时之后再调一次 stop() 不能挂住（幂等，且回收已完成）。"""
    eng, _, _ = _engine_fixture(tmp_path, monkeypatch, [], workers=2)

    async def main() -> None:
        eng.start()
        await eng.stop(drain=True, drain_timeout=0.0)
        # 第二次必须快速返回；给它 5 秒，挂住就算失败
        await asyncio.wait_for(eng.stop(drain=True, drain_timeout=0.0),
                               timeout=5.0)

    asyncio.run(main())


def test_engine_drain_success_still_reports_done(tmp_path: Path,
                                                 monkeypatch) -> None:
    """正常路径不能被修坏：drain 足够长时必须真的把队列跑完。"""
    src = _make_image(tmp_path / "d.jpg", (640, 480))
    st = src.stat()
    rows = [{"id": 1, "root_id": "library", "rel_path": "d.jpg",
             "mtime": st.st_mtime, "size": st.st_size, "orientation": 1,
             "thumb_state": "pending", "thumb_etag": None, "missing": 0}]
    eng, cfg, db = _engine_fixture(tmp_path, monkeypatch, rows)

    async def main() -> None:
        eng.start()
        eng.enqueue(1)
        await eng.stop(drain=True, drain_timeout=30.0)

    asyncio.run(main())
    assert eng.done == 1, f"drain 应把队列跑完，done={eng.done}"
    assert db.rows[1]["thumb_state"] == "ok"


def test_engine_start_after_stop_is_rejected(tmp_path: Path,
                                             monkeypatch) -> None:
    """已回收的引擎不能重启 —— 线程池已 shutdown，重启会静默不干活。"""
    import pytest as _pytest
    eng, _, _ = _engine_fixture(tmp_path, monkeypatch, [])

    async def main() -> None:
        eng.start()
        await eng.stop()

    asyncio.run(main())
    with _pytest.raises(RuntimeError, match="不能重新启动"):
        eng.start()


def test_engine_teardown_balances_queue_counters(tmp_path: Path,
                                                 monkeypatch) -> None:
    """★ 回收时必须清空队列并平衡 task_done()。

    否则残留的未平衡计数会让后续任何 `queue.join()` **永久挂起** ——
    表现是"第二次关机卡死"，而第一次看起来正常。
    """
    eng, _, _ = _engine_fixture(tmp_path, monkeypatch, [], workers=1)

    async def main() -> None:
        eng.start()
        # 塞 5 个任务，但用 drain_timeout=0 让它们来不及被处理
        for _ in range(5):
            eng.enqueue(999)
        await eng.stop(drain=True, drain_timeout=0.0)
        # 队列已清空，且 join() 立刻返回（计数已平衡）
        assert eng.queue.empty(), "队列未清空"
        await asyncio.wait_for(eng.queue.join(), timeout=2.0)

    asyncio.run(main())


# --------------------------------------------------------------------------
# HEIC / HEIF —— 一期硬需求，但此前没有任何测试真的处理过 HEIC 文件
#
# docs/01 把 HEIC 列为必须支持（iPhone 默认格式，白名单里有 heic/heif），
# 而在此之前整个测试套件只断言过 `content_type_for("heic") == "image/heic"`
# —— 那只是字符串映射，**没有任何一张 HEIC 被解码过**。
# 一旦 pillow-heif 没装、或注册时机不对（必须在任何 Image.open 之前），
# 表现为"iPhone 用户的照片全是灰块"，而单元测试全绿。
# --------------------------------------------------------------------------


def _heic_available() -> bool:
    try:
        import pillow_heif  # noqa: F401

        return True
    except ImportError:
        return False


needs_heif = pytest.mark.skipif(not _heic_available(),
                                reason="pillow-heif 未安装")


def _make_heic(path: Path, size: tuple[int, int], color=(10, 120, 200)) -> Path:
    """造一张真的 HEIC。必须先注册 opener 才能 save。"""
    from app.exif import _prepare_pillow

    _prepare_pillow()
    Image.new("RGB", size, color).save(path, "HEIF", quality=80)
    return path


@needs_heif
def test_heic_thumbnail_is_generated(tmp_path: Path, monkeypatch) -> None:
    """★ iPhone 的 HEIC 必须能生成缩略图，否则一期等于不支持 iPhone。"""
    src = _make_heic(tmp_path / "IMG_0001.heic", (1200, 900))
    st = src.stat()
    rows = [{"id": 1, "root_id": "library", "rel_path": "IMG_0001.heic",
             "mtime": st.st_mtime, "size": st.st_size, "orientation": 1,
             "thumb_state": "pending", "thumb_etag": None, "missing": 0}]
    eng, cfg, db = _engine_fixture(tmp_path, monkeypatch, rows)

    async def main() -> None:
        eng.start()
        eng.enqueue(1)
        await eng.queue.join()
        await eng.stop()

    asyncio.run(main())

    assert eng.failed == 0, "HEIC 缩略图生成失败"
    assert eng.done == 1
    for size in THUMB_SIZES:
        out = thumb_path(cfg, "library", "IMG_0001.heic", size)
        assert out.is_file(), f"缺少 {size} 缩略图"
        assert out.read_bytes()[:4] == b"RIFF", "输出不是 WebP"
    assert db.rows[1]["thumb_state"] == "ok"


@needs_heif
def test_heic_generate_thumb_directly(tmp_path: Path) -> None:
    """直接调 generate_thumb，不经过引擎 —— 隔离掉队列与 DB 的干扰。"""
    src = _make_heic(tmp_path / "a.heic", (800, 600))
    out = tmp_path / "a_320.webp"

    w, h = generate_thumb(src, out, 320)
    assert out.read_bytes()[:4] == b"RIFF", "输出不是 WebP"
    # 缩略图必须真的缩小了，而不是原样拷一份
    assert max(w, h) <= 320, f"没有缩小：{(w, h)}"
    with Image.open(out) as im:
        assert im.size == (w, h)


@needs_heif
def test_heic_exif_is_read(tmp_path: Path) -> None:
    """HEIC 的 EXIF 拍摄时间要能读出来。

    iPhone 的 HEIC 里 DateTimeOriginal 在 EXIF 子 IFD 里，与 JPEG 的
    解析路径不完全相同。读不出来就会退回 mtime —— 用户看到的时间线
    是按"拷贝到电脑的时间"排的，与照片内容完全脱节。
    """
    from app import exif as exif_mod

    _make_heic(tmp_path / "b.heic", (640, 480))
    info = exif_mod.extract_sync(tmp_path / "b.heic")
    # 我们造的文件没有 EXIF，但**必须**能读出尺寸而不是报错
    assert info.width == 640 and info.height == 480, f"尺寸读错：{info}"
    assert info.error is None, f"读 HEIC 报错了：{info.error}"


@needs_heif
def test_portrait_heic_stays_portrait(tmp_path: Path) -> None:
    """★ 竖拍的 HEIC 必须得到竖的缩略图（不能横着显示）。

    这里必须走**真实的那条链路**：`extract_sync` 读出 orientation →
    传给 `generate_thumb`。实测发现 pillow-heif 在**读取时就已应用**
    Orientation 并把它归一成 1（PIL 看到的尺寸已经是转过之后的），
    所以若手工硬传一个 6 进去，反而会**转两次**变回横的。

    生产代码正是传 `extract_sync` 读出来的值，所以这里就照抄那条路径 ——
    这条用例保护的是"exif → thumbs"这个交接点，而不是某个固定数字。
    """
    from app.exif import _prepare_pillow

    _prepare_pillow()
    src = tmp_path / "rot.heic"
    im = Image.new("RGB", (1200, 600), (30, 30, 200))
    exif = Image.Exif()
    exif[0x0112] = 6                      # 顺时针 90°，真实手机竖拍常见值
    im.save(src, "HEIF", exif=exif.tobytes(), quality=80)

    from app import exif as exif_mod

    info = exif_mod.extract_sync(src)

    out_path = tmp_path / "rot_320.webp"
    w, h = generate_thumb(src, out_path, 320, orientation=info.orientation)

    assert h > w, (
        f"竖拍 HEIC 的缩略图是横的：{(w, h)}（orientation={info.orientation}）")
    assert max(w, h) <= 320
    with Image.open(out_path) as out:
        assert out.size == (w, h)


@needs_heif
def test_heic_orientation_is_not_applied_twice(tmp_path: Path) -> None:
    """★ 回归：HEIC 的 orientation 不能被应用两次。

    实测事实：pillow-heif 读取时就把 Orientation 应用掉并把值归一成 1，
    PIL 看到的尺寸已经是转过之后的（1200x600 的文件读出来是 600x1200）。
    如果哪天有人"顺手"在 thumbs 里改用 `ImageOps.exif_transpose` 或
    把原始 EXIF 里的 6 存进数据库再传进来，竖拍照片就会被转两次 ——
    表现是"竖着拍的照片显示成横的"，而缩略图本身完全正常，极难排查。
    """
    from app import exif as exif_mod
    from app.exif import _prepare_pillow

    _prepare_pillow()
    src = tmp_path / "rot2.heic"
    im = Image.new("RGB", (1200, 600), (30, 30, 200))
    exif = Image.Exif()
    exif[0x0112] = 6
    im.save(src, "HEIF", exif=exif.tobytes(), quality=80)

    # 库读出来的方向已经是"无需再转"
    assert exif_mod.extract_sync(src).orientation == 1, (
        "pillow-heif 的行为变了：它以前会在读取时应用 Orientation 并归一成 1。"
        "若确实变了，thumbs 里对 HEIC 的处理需要重新评估。")

    out_path = tmp_path / "rot2_320.webp"
    w, h = generate_thumb(src, out_path, 320,
                          orientation=exif_mod.extract_sync(src).orientation)
    assert h > w, f"竖拍 HEIC 被转成了横的：{(w, h)}"


# --------------------------------------------------------------------------
# 单边尺寸上限（ISSUE-034）
# --------------------------------------------------------------------------
#
# `MAX_IMAGE_PIXELS` 只挡住"宽×高"很大的一类；`200000×10` 这种**单边**超长
# 的图能过像素数检查，但 Pillow 解码时按行累积，照样把内存吃满。
# 判据放在 `Image.open()` 之后、任何解码动作之前：`im.size` 来自文件头，
# 读它不分配像素缓冲。


def _oversized_png(path: Path, *, width: int = None, height: int = None) -> Path:
    """造一张单边超限的 PNG（只 2 像素高，文件很小）。"""
    w = width if width is not None else thumbs.MAX_IMAGE_EDGE + 1
    h = height if height is not None else 2
    Image.new("RGB", (w, h), (10, 120, 200)).save(path)
    return path


def test_open_downscaled_rejects_too_wide_image(tmp_path: Path) -> None:
    """单边超上限 → 抛异常，不进解码。"""
    path = _oversized_png(tmp_path / "wide.png")
    with pytest.raises(thumbs.OversizedImageError):
        _open_downscaled(path, 320)


def test_open_downscaled_rejects_too_tall_image(tmp_path: Path) -> None:
    """高也同样要挡（竖长图同样能撑爆内存）。"""
    path = _oversized_png(tmp_path / "tall.png", width=2,
                          height=thumbs.MAX_IMAGE_EDGE + 1)
    with pytest.raises(thumbs.OversizedImageError):
        _open_downscaled(path, 320)


def test_open_downscaled_accepts_normal_image(tmp_path: Path) -> None:
    """正常照片不受影响（防止把上限写反成"一律拒绝"）。"""
    path = _make_image(tmp_path / "ok.jpg", (800, 600))
    im = _open_downscaled(path, 320)
    try:
        assert im.width == 800 and im.height == 600
    finally:
        im.close()


def test_generate_thumb_propagates_oversized_error(tmp_path: Path) -> None:
    """`generate_thumb` 不能把 OversizedImageError 吞掉或转成别的异常类型。

    调用方（ThumbEngine / browse 兜底）靠这个类型决定"直接判失败、不重试"，
    一旦被包成 `Exception` 就退回"重试三次、每次占满一个解码闸门"。
    """
    path = _oversized_png(tmp_path / "wide2.png")
    with pytest.raises(thumbs.OversizedImageError):
        generate_thumb(path, tmp_path / "out.webp", 320)


def test_engine_marks_oversized_image_failed_without_retry(tmp_path: Path,
                                                           monkeypatch) -> None:
    """超大图进 `failed` 终态，且**不进**退避重试队列。

    重试对它是纯浪费：结果永远一样，而每次重试都要占一个解码闸门（只有 4 个）。
    几张超大图就能把整个缩略图队列堵住 5 分钟以上，表现成"新照片的缩略图
    一直不出"。
    """
    src = _oversized_png(tmp_path / "big.png")
    st = src.stat()
    rows = [{"id": 5, "root_id": "library", "rel_path": "big.png",
             "mtime": st.st_mtime, "size": st.st_size, "orientation": 1,
             "thumb_state": "pending", "thumb_etag": None, "missing": 0}]
    eng, cfg, db = _engine_fixture(tmp_path, monkeypatch, rows)

    async def main() -> None:
        eng.start()
        eng.enqueue(5)
        await eng.queue.join()
        await eng.stop()

    asyncio.run(main())

    assert db.rows[5]["thumb_state"] == "failed"
    assert eng.done == 0
    # 没有重试排入：队列空
    assert eng.queue_depth() == 0
    # 一个缩略图文件都不该被写出来
    assert not any(thumb_path(cfg, "library", "big.png", s).exists()
                   for s in THUMB_SIZES)
