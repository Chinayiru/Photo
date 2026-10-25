"""Pillow 13 升级后暴露的加载/导出边界。

Pillow 13 收紧了几类输入校验，其中影响本仓库的只有「PNG 辅助块声明长度
与实际字节不符」：Pillow 12 静默忽略、照常出图，13 直接在 `Image.open()`
抛 `UnidentifiedImageError`。像素数据截断（IDAT 砍半）两版都抛
`OSError: image file is truncated`。

这两种都属于「重试一万次也不会好」的坏文件，所以必须在加载边界归一成
`CorruptImageError`，让缩略图引擎直接判永久失败，而不是按
RETRY_BACKOFF（5s/30s/300s）重排三次、每次占满一个解码闸门。

剩下几项（mode="I" 存 PNG、ImageQt.align8to32()、Image.fromarray(mode=...)、
ExifTags.IFD.Makernote）本仓库没有用到，用守卫用例钉住，防止以后有人写进来。
"""

from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image

from backend.app import exif, thumbs

REPO = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- 夹具

def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _small_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (9, 9, 9)).save(buf, "PNG")
    return buf.getvalue()


def _png_with_broken_ancillary(tag: bytes = b"cHRM", declared: int = 32,
                               present: int = 30) -> bytes:
    """在 IHDR 之后插一个「声明 N 字节、实际只有 N-2 字节」的辅助块。

    Pillow 12 当没看见继续解码；Pillow 13 在 open 阶段就拒绝。
    """
    raw = _small_png()
    ihdr_end = 8 + 12 + 13            # 签名 + IHDR(长度4 + 类型4 + 数据13 + CRC4)
    bad = struct.pack(">I", declared) + tag + b"\x00" * present
    return raw[:ihdr_end] + bad + raw[ihdr_end:]


def _truncated_idat_png() -> bytes:
    raw = _small_png()
    return raw[: len(raw) // 2]


@pytest.fixture()
def pngs(tmp_path: Path) -> dict[str, Path]:
    out = {}
    for name, data in (
        ("chrm", _png_with_broken_ancillary(b"cHRM")),
        ("gama", _png_with_broken_ancillary(b"gAMA", declared=4, present=2)),
        ("trns", _png_with_broken_ancillary(b"tRNS", declared=6, present=4)),
        ("idat", _truncated_idat_png()),
    ):
        p = tmp_path / f"{name}.png"
        p.write_bytes(data)
        out[name] = p
    return out


# ------------------------------------------------- 加载边界：归一成坏图

@pytest.mark.parametrize("key", ["chrm", "gama", "trns"])
def test_broken_ancillary_chunk_is_corrupt_not_unknown(pngs, tmp_path, key) -> None:
    """Pillow 13 起会拒绝，必须归一成 CorruptImageError（而不是让它裸奔）。"""
    with pytest.raises(thumbs.CorruptImageError) as exc:
        thumbs.generate_thumb(pngs[key], tmp_path / f"{key}.webp", 320)
    assert "UnidentifiedImageError" in str(exc.value)


def test_truncated_idat_is_corrupt_image(pngs, tmp_path) -> None:
    with pytest.raises(thumbs.CorruptImageError) as exc:
        thumbs.generate_thumb(pngs["idat"], tmp_path / "idat.webp", 320)
    assert "truncated" in str(exc.value).lower()


def test_corrupt_image_error_is_not_a_decode_oserror(pngs, tmp_path) -> None:
    """坏图不能以 OSError 身份漏出去 —— 引擎会把它当可重试错误。"""
    with pytest.raises(thumbs.CorruptImageError):
        thumbs.generate_thumb(pngs["chrm"], tmp_path / "x.webp", 320)


def test_missing_file_is_not_reported_as_corrupt(tmp_path) -> None:
    """文件不存在 ≠ 坏图：这条要留给调用方按「缺失」处理。"""
    with pytest.raises(FileNotFoundError):
        thumbs.generate_thumb(tmp_path / "nope.png", tmp_path / "o.webp", 320)


def test_exif_never_raises_on_corrupt_pngs(pngs) -> None:
    """EXIF 抽取对坏文件只能降级，不能中断整轮索引。"""
    for p in pngs.values():
        info = exif.extract_sync(p)
        assert info.error, f"{p.name} 应该带 error 标记"
        # 辅助块坏掉的头都读不了 → 尺寸拿不到；截断 IDAT 的头是好的，
        # 尺寸仍可读出（32×32），只要求"别抛异常"。
        assert info.error.startswith(("unidentified_image", "OSError"))


# --------------------------------------------- 导出边界：I;16 / mode I

def test_sixteen_bit_grayscale_png_thumbnails(tmp_path) -> None:
    """扫描件常见 I;16 PNG：要能出 WebP，且结果是 RGB。"""
    src = tmp_path / "gray16.png"
    Image.new("I;16", (40, 60), 30000).save(src, "PNG")
    dst = tmp_path / "gray16.webp"
    w, h = thumbs.generate_thumb(src, dst, 320)
    assert (w, h) == (40, 60)
    with Image.open(dst) as im:
        assert im.mode in ("RGB", "RGBA")


def test_pillow13_refuses_mode_i_png_export() -> None:
    """记录边界：13.x 明确拒绝把 mode="I" 存成 PNG（须先转 I;16）。"""
    with pytest.raises(OSError):
        Image.new("I", (8, 8), 1000).save(io.BytesIO(), "PNG")
    buf = io.BytesIO()
    Image.new("I;16", (8, 8), 1000).save(buf, "PNG")      # 正解
    with Image.open(io.BytesIO(buf.getvalue())) as im:
        assert im.mode == "I;16"


def test_thumb_pipeline_only_writes_webp() -> None:
    """缩略图链路只落 WebP —— 所以 mode="I" 的 PNG 导出限制碰不到我们。"""
    src = (REPO / "backend" / "app" / "thumbs.py").read_text(encoding="utf-8")
    assert 'im.save(tmp, "WEBP"' in src
    assert '"PNG"' not in src.split("def generate_thumb")[1].split("def _generate_thumb_kw")[0]


# ------------------------------------------- 引擎：坏图直接永久失败

def test_engine_treats_corrupt_image_as_permanent_failure() -> None:
    """坏文件不能走退避重试：分支必须在 `_on_failure` 之外提前返回。"""
    src = (REPO / "backend" / "app" / "thumbs.py").read_text(encoding="utf-8")
    assert "except CorruptImageError as exc:" in src
    block = src.split("except CorruptImageError as exc:")[1].split("except ")[0]
    assert 'self._set_state(media_id, "failed", None)' in block
    assert "return False" in block, "必须在闸门内就地返回，否则会被重排三次"
    # 与超大图的处理并列，两者都在通用 except 之前
    assert src.index("except OversizedImageError as exc:") < src.index("except CorruptImageError as exc:")


# ------------------------------------------------ 未使用 API 的守卫

@pytest.mark.parametrize("pattern", [
    "ImageQt",
    "align8to32",
    "Image.fromarray(",
    "ExifTags.IFD.Makernote",
])
def test_removed_pillow_apis_are_not_used(pattern: str) -> None:
    """Pillow 13 删/改了这些 API；本仓库若用到就必须显式替换。"""
    hits = []
    for sub in ("backend", "scripts", "shared", "cloud"):
        for p in (REPO / sub).rglob("*.py"):
            if "test_pillow13_boundaries" in p.name:
                continue
            if pattern in p.read_text(encoding="utf-8", errors="ignore"):
                hits.append(str(p.relative_to(REPO)))
    assert not hits, f"{pattern} 出现在 {hits}"


def test_requirements_pin_pillow_13() -> None:
    text = (REPO / "requirements.txt").read_text(encoding="utf-8")
    assert "Pillow>=13.0.0" in text
    assert "Pillow>=10.3\n" not in text


def test_python_floor_is_311() -> None:
    """Pillow 13 不再支持 3.10，本仓库的下限要跟上。"""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert 'requires-python = ">=3.11"' in text
    assert "3.10" not in text
