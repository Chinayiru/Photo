"""exif.py 单元测试。

用例来源：
  * docs/03 §4.1.5 的"必须通过的解析用例"表（12 条）
  * docs/07 §4.3 EXIF 用例
  * 对真实库 D:\\Photos 实测归纳出的漏网形态（见
    scripts/probe_filename_gaps.py 的输出），每一条都对应一个真实文件名

跑：.venv/Scripts/python.exe -m pytest backend/tests/test_exif.py -v
"""
from __future__ import annotations

import datetime as dt
import sys
import warnings
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.exif import (  # noqa: E402
    MAX_IMAGE_PIXELS,
    MIN_VALID_TS,
    ExifInfo,
    _clean,
    _clean_pair,
    _dms_to_deg,
    _float_or_none,
    _int_or_none,
    _orientation_or_none,
    _ratio_to_float,
    _ts_in_range,
    extract_sync,
    parse_datetime_from_name,
    resolve_taken_at,
)


def _fmt(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else None


# ==========================================================================
# docs/03 §4.1.5 的 12 条必须通过用例
# ==========================================================================

@pytest.mark.parametrize(
    ("name", "want"),
    [
        ("IMG_20240512_183000.jpg", "2024-05-12 18:30:00"),
        ("20240512_183000.jpg", "2024-05-12 18:30:00"),
        ("2024-05-12.jpg", "2024-05-12 12:00:00"),
        ("2024-05-12 18.30.00.jpg", "2024-05-12 18:30:00"),
        ("2024年05月12日_183000.jpg", "2024-05-12 18:30:00"),
        ("mmexport1715509800000.jpg", "2024-05-12 18:30:00"),
        ("1715509800.jpg", "2024-05-12 18:30:00"),
        ("DSC_0001.JPG", None),
        ("IMG_0001.jpg", None),
        ("Screenshot_2024-13-45.png", None),
        ("照片20240512.jpg", "2024-05-12 12:00:00"),
    ],
)
def test_doc_required_cases(name: str, want: str | None) -> None:
    assert _fmt(parse_datetime_from_name(name)) == want


def test_pxl_milliseconds() -> None:
    ts = parse_datetime_from_name("PXL_20240512_183000123.jpg")
    assert ts is not None
    assert dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] == \
        "2024-05-12 18:30:00.123"


# ==========================================================================
# 真实库漏网形态（扩充的模式，每条都是实测文件名）
# ==========================================================================

@pytest.mark.parametrize(
    ("name", "want"),
    [
        # 下划线作日期分隔符 —— 最初漏掉就是因为分隔符集合里没有 '_'
        ("cafestella_screenshot 2020_12_07 23_15_47.jpg", "2020-12-07 23:15:47"),
        ("wx_camera_1715509800000.jpg", "2024-05-12 18:30:00"),
        ("Screenshot_20240512-183000.png", "2024-05-12 18:30:00"),
        ("DOKA_20240512_183000.jpg", "2024-05-12 18:30:00"),
        ("2024.05.12 18.30.00.jpg", "2024-05-12 18:30:00"),
        ("2024年05月12日 18:30:00.jpg", "2024-05-12 18:30:00"),
        ("IMG_20240512_183000_1.jpg", "2024-05-12 18:30:00"),
        ("2024-05-12_18-30-00.jpg", "2024-05-12 18:30:00"),
        ("VID_20240512_183000.mp4", "2024-05-12 18:30:00"),
    ],
)
def test_real_library_shapes(name: str, want: str) -> None:
    assert _fmt(parse_datetime_from_name(name)) == want


@pytest.mark.parametrize(
    "name",
    [
        # 回归：这两条曾经被切成 "d=1, H=2, M=18, S=30" 得到 2024-05-01 02:18:30。
        # 原因一是日期/时间分界符集合里混进了 '-' 和 '.'，二是那个分界符
        # 被写成可选（`[ _\-T]?`）。两条都必须保持"日取两位、分界符强制"。
        "2024-05-12_18-30-00.jpg",
        "2024-05-12_183000.jpg",
        "2024-05-12 183000.jpg",
        "2024.05.12_18.30.00.jpg",
        "2024年05月12日183000.jpg",
    ],
)
def test_date_time_split_is_not_greedy_wrong(name: str) -> None:
    got = _fmt(parse_datetime_from_name(name))
    assert got == "2024-05-12 18:30:00", f"{name} → {got}（日期/时间切分错误）"


@pytest.mark.parametrize(
    "name",
    [
        # 单数字月/日 + 连写时间。必须优先按"两位日"解析，而不是 d=1。
        "2024-5-12_183000.jpg",
        "2024-5-2_183000.jpg",
        "2024.5.12 18:30:00.jpg",
    ],
)
def test_single_digit_month_day_with_time(name: str) -> None:
    ts = parse_datetime_from_name(name)
    assert ts is not None
    d = dt.datetime.fromtimestamp(ts)
    assert (d.year, d.month, d.day, d.hour, d.minute) == (2024, 5, 12, 18, 30) or \
        (d.year, d.month, d.day) == (2024, 5, 2)


@pytest.mark.parametrize(
    "name",
    [
        # 这四类在真实库里存在且**确实不含日期**，必须返回 None 而不是猜一个。
        # 猜错的代价比不猜大得多：时间线上会出现顺序错误的照片且无法解释。
        "12d4632987162351.jpg",                            # 16 位 hex
        "308c026d7f6d96c88826f035bc5b52858ce91bc1.webp",   # 40 位 hex
        "5B2392FC03B1EF0EC11E14039E6EC520.png",            # 32 位大写 hex
        "IMG_0001.jpg",                                    # 纯序号
        "IMG_E0001.JPG",                                   # 纯序号（编辑版）
        "part_4.jpg",
        "preview.jpg",
        "COVER.jpg",
        "01_戈壁_指远方.jpg",
        "2560亊1440(侾俇丗俋).png",                          # 尺寸描述，不是日期
        "587539.0.jpg",
        "i0_raw.webp",
        "英桀表盘.png",
        "ev202a_Ba.jpg",
        "-396343904.jpg",
    ],
)
def test_no_false_positives(name: str) -> None:
    """这些必须返回 None。误匹配会让时间线顺序错乱且用户无法理解原因。"""
    assert parse_datetime_from_name(name) is None, f"{name} 被误匹配为 {_fmt(parse_datetime_from_name(name))}"


@pytest.mark.parametrize("name", ["", ".jpg", ".", "x", "a" * 300 + ".jpg"])
def test_degenerate_names(name: str) -> None:
    assert parse_datetime_from_name(name) is None


# ==========================================================================
# 时间范围夹取
# ==========================================================================

def test_ts_in_range() -> None:
    assert not _ts_in_range(MIN_VALID_TS - 1)
    assert _ts_in_range(MIN_VALID_TS)
    assert _ts_in_range(dt.datetime(2024, 5, 12, 18, 30).timestamp())
    assert _ts_in_range(dt.datetime.now().timestamp() + 3600)      # 时区没同步的手机
    assert not _ts_in_range(dt.datetime.now().timestamp() + 3 * 86400)
    assert not _ts_in_range(dt.datetime(2035, 1, 1).timestamp())   # 相机没设时间


@pytest.mark.parametrize(
    "name",
    [
        "20240230.jpg",            # 2 月 30 日不存在
        "20241301.jpg",            # 13 月
        "20240532.jpg",            # 32 日
        "18991231.jpg",            # 1899 不在 19|20 的 4 位范围内 → 不匹配
        "17991231.jpg",
    ],
)
def test_invalid_dates_fall_through(name: str) -> None:
    assert parse_datetime_from_name(name) is None


def test_year_range_floor_is_1980() -> None:
    """1980 是硬下限（MIN_VALID_TS），早于此一律判脏数据。

    正则本身能识别 19xx，但 1980 前的日期在照片库里只可能是错误数据
    （相机没设时间、扫描件乱填），放进时间线只会把排序搞乱。
    """
    assert _fmt(parse_datetime_from_name("19800101.jpg")) == "1980-01-01 12:00:00"
    assert _fmt(parse_datetime_from_name("19800102.jpg")) == "1980-01-02 12:00:00"
    assert parse_datetime_from_name("19000101.jpg") is None   # 正则匹配，但被范围拒绝
    assert parse_datetime_from_name("19791231.jpg") is None
    assert parse_datetime_from_name("20000101.jpg") == pytest.approx(
        dt.datetime(2000, 1, 1, 12, 0, 0).timestamp()
    )
    # 2099 在正则范围内，但远超 now+1d，同样被拒
    assert parse_datetime_from_name("20990101.jpg") is None


# ==========================================================================
# 三级回退
# ==========================================================================

def test_resolve_prefers_exif() -> None:
    ts, src = resolve_taken_at(ExifInfo(taken_at=1715509800.0), "IMG_20240512_183000.jpg", 0.0)
    assert src == "exif" and ts == 1715509800.0


def test_resolve_falls_back_to_filename() -> None:
    ts, src = resolve_taken_at(ExifInfo(), "IMG_20240512_183000.jpg", 0.0)
    assert src == "filename"
    assert _fmt(ts) == "2024-05-12 18:30:00"


def test_resolve_falls_back_to_mtime() -> None:
    mtime = dt.datetime(2023, 1, 2, 3, 4, 5).timestamp()
    ts, src = resolve_taken_at(ExifInfo(), "DSC_0001.JPG", mtime)
    assert src == "mtime" and ts == mtime


def test_resolve_rejects_dirty_exif() -> None:
    """EXIF 时间在 1980 前或未来 1 天以上 → 判脏数据，走下一级。"""
    ts, src = resolve_taken_at(ExifInfo(taken_at=100.0), "IMG_20240512_183000.jpg", 0.0)
    assert src == "filename"

    future = dt.datetime.now().timestamp() + 10 * 86400
    ts, src = resolve_taken_at(ExifInfo(taken_at=future), "DSC_0001.JPG", 1715509800.0)
    assert src == "mtime"


def test_resolve_clamps_dirty_mtime() -> None:
    """mtime 本身也可能是脏的（0 = 1970），必须夹到合法区间。"""
    ts, src = resolve_taken_at(ExifInfo(), "DSC_0001.JPG", 0.0)
    assert src == "mtime" and ts == MIN_VALID_TS


def test_resolve_accepts_none_exif() -> None:
    ts, src = resolve_taken_at(None, "IMG_20240512_183000.jpg", 0.0)
    assert src == "filename"


# ==========================================================================
# 类型转换工具
# ==========================================================================

def test_int_or_none() -> None:
    assert _int_or_none(5) == 5
    assert _int_or_none("7") == 7
    assert _int_or_none((9,)) == 9
    assert _int_or_none(None) is None
    assert _int_or_none("abc") is None
    assert _int_or_none(()) is None


def test_float_or_none_rejects_nonpositive_and_nan() -> None:
    assert _float_or_none(2.8) == 2.8
    assert _float_or_none(0) is None            # ISO 0 / 光圈 0 是无效值
    assert _float_or_none(-1) is None
    assert _float_or_none(float("nan")) is None
    assert _float_or_none(float("inf")) is None
    assert _float_or_none(None) is None
    assert _float_or_none("x") is None


def test_ratio_to_float() -> None:
    assert _ratio_to_float((1, 100)) == pytest.approx(0.01)     # 快门 1/100
    assert _ratio_to_float((28, 10)) == pytest.approx(2.8)      # 光圈 f/2.8
    assert _ratio_to_float((0, 1)) is None
    assert _ratio_to_float((1, 0)) is None                      # 分母 0 不炸
    assert _ratio_to_float(None) is None
    assert _ratio_to_float("x") is None


def test_clean_strips_nul_and_truncates() -> None:
    assert _clean("Xiaomi\x00") == "Xiaomi"
    assert _clean("  Xiaomi  ") == "Xiaomi"
    assert _clean("") is None
    assert _clean(None) is None
    assert _clean(b"vivo") == "vivo"
    assert len(_clean("x" * 500)) == 128


def test_clean_pair_avoids_duplication() -> None:
    # 真实库里 vivo 的 Make 与 Model 都是 "vivo V2338A" 形态，不能拼成
    # "vivo V2338A vivo V2338A"
    assert _clean_pair("vivo", "vivo V2338A") == "vivo V2338A"
    assert _clean_pair("vivo V2338A", "vivo") == "vivo V2338A"
    assert _clean_pair("Apple", "iPhone 15 Pro") == "Apple iPhone 15 Pro"
    assert _clean_pair(None, "iPhone") == "iPhone"
    assert _clean_pair(None, None) is None


def test_dms_to_deg() -> None:
    # 39°54'27" N → 39.9075
    assert _dms_to_deg((39, 54, 27), "N", neg="S") == pytest.approx(39.9075, abs=1e-4)
    assert _dms_to_deg((39, 54, 27), "S", neg="S") == pytest.approx(-39.9075, abs=1e-4)
    assert _dms_to_deg((116, 23, 29), "E", neg="W") == pytest.approx(116.3914, abs=1e-4)
    assert _dms_to_deg((116, 23, 29), "W", neg="W") == pytest.approx(-116.3914, abs=1e-4)
    # 小写参考位与 bytes 参考位都要能处理
    assert _dms_to_deg((39, 54, 27), b"s", neg="S") == pytest.approx(-39.9075, abs=1e-4)
    assert _dms_to_deg(None, "N", neg="S") is None
    assert _dms_to_deg((), "N", neg="S") is None
    assert _dms_to_deg("x", "N", neg="S") is None


# ==========================================================================
# 真实文件读取
# ==========================================================================

def test_extract_sync_missing_file_does_not_raise() -> None:
    """坏文件/不存在的文件绝不抛异常 —— 一个坏文件不能中断整轮 4952 个文件的扫描。"""
    info = extract_sync(Path("Z:/definitely/not/here.jpg"))
    assert isinstance(info, ExifInfo)
    assert info.error is not None
    assert info.width is None


def test_extract_sync_on_non_image(tmp_path: Path) -> None:
    p = tmp_path / "fake.jpg"
    p.write_bytes(b"this is not an image at all")
    info = extract_sync(p)
    assert info.error is not None
    assert info.has_exif is False


def test_extract_sync_on_real_png(tmp_path: Path) -> None:
    """造一张真 PNG，验证尺寸读取与无 EXIF 时的行为。"""
    from PIL import Image

    p = tmp_path / "t.png"
    Image.new("RGB", (640, 480), (10, 20, 30)).save(p)
    info = extract_sync(p)
    assert info.error is None
    assert (info.width, info.height) == (640, 480)
    assert info.has_exif is False
    assert info.taken_at is None


def test_extract_sync_decompression_bomb_rejected(tmp_path: Path) -> None:
    """声明超大尺寸的极小 PNG 必须被拒绝，而不是分配巨量内存。

    docs/06 F6 / docs/07 IMG 用例：661 字节的 PNG 声明 200000×200000，
    Image.open() 成功（不检查像素），只有 load()/verify() 才抛。
    这里直接构造一个声明尺寸超限的 PNG 头。
    """
    import struct
    import zlib

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    # 声明 200000×200000 = 4×10^10 像素，远超 MAX × 2 = 4×10^8
    ihdr = struct.pack(">IIBBBBB", 200_000, 200_000, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
    png += chunk(b"IDAT", zlib.compress(b"\x00" * 100))
    png += chunk(b"IEND", b"")

    p = tmp_path / "bomb.png"
    p.write_bytes(png)
    assert p.stat().st_size < 1000, "攻击样本应当极小"

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        info = extract_sync(p)
    assert info.error is not None, "解压炸弹必须被拒绝"
    assert "bomb" in info.error


def test_orientation_or_none_accepts_only_1_to_8() -> None:
    """Orientation 只接受 1..8。

    实测真实库 D:\\Photos 有 73 个文件写着 Orientation=0。
    media 表有 CHECK(orientation BETWEEN 1 AND 8)，若让 0 落库，
    executemany 会让同批 500 张照片一起失败。
    """
    for good in (1, 2, 3, 4, 5, 6, 7, 8):
        assert _orientation_or_none(good) == good
    for bad in (0, -1, 9, 99, 255, 65535):
        assert _orientation_or_none(bad) is None
    assert _orientation_or_none(None) is None
    assert _orientation_or_none("x") is None
    # 元组形式（EXIF 里出现过 (0,)）
    assert _orientation_or_none((6,)) == 6
    assert _orientation_or_none((0,)) is None


def test_extract_sync_orientation_zero_becomes_none(tmp_path: Path) -> None:
    """端到端确认：文件里写 Orientation=0，抽出来必须是 None 而不是 0。"""
    from PIL import Image
    p = tmp_path / "zero_orient.jpg"
    im = Image.new("RGB", (32, 32), (10, 20, 30))
    ex = Image.Exif()
    ex[0x0112] = 0
    im.save(p, "JPEG", exif=ex.tobytes())

    info = extract_sync(p)
    assert info.orientation is None, "0 是非法值，必须归零为 None"
    assert info.width == 32 and info.height == 32


def test_extract_sync_orientation_six_kept(tmp_path: Path) -> None:
    """合法值必须原样保留（竖拍照片靠它转正）。"""
    from PIL import Image
    p = tmp_path / "six_orient.jpg"
    im = Image.new("RGB", (32, 32), (10, 20, 30))
    ex = Image.Exif()
    ex[0x0112] = 6
    im.save(p, "JPEG", exif=ex.tobytes())

    assert extract_sync(p).orientation == 6


def test_max_image_pixels_allows_48mp() -> None:
    """48MP（8192×6144）是真实库里存在的正常照片，必须放行。

    第一版把上限设成 5000 万，实测直接炸了 —— 真实库里就有
    50331648 像素的照片。这个测试锁住这个教训。
    """
    assert 8192 * 6144 <= MAX_IMAGE_PIXELS
    # 2 亿像素（当前手机最高规格）也要能过
    assert 200_000_000 <= MAX_IMAGE_PIXELS
    # 但仍要远小于"声明 4×10^10 像素"这类攻击值
    assert MAX_IMAGE_PIXELS < 4_000_000_000
