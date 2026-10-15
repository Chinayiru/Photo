"""EXIF 抽取与拍摄时间推断。

权威设计：docs/03-详细设计说明书.md §4.1.3–§4.1.5。

三个必须做对的点：

1. **同步调用必须丢线程池**。Pillow 的 `Image.open` + `getexif()` 是阻塞的，
   一个 50 MB 的 TIFF 可能 200 ms。在事件循环里跑 4900 次会把隧道心跳饿死，
   云端直接判定电脑离线。这是本项目最容易犯的并发错误。

2. **`Image.open()` 不检查像素数**。实测：661 字节的 PNG 声明 200000×200000，
   `open()` 成功，只有 `load()` / `verify()` 才抛 DecompressionBombError，
   且阈值是 `MAX_IMAGE_PIXELS × 2`。所以既要设 MAX_IMAGE_PIXELS，
   又要把 DecompressionBombWarning 升级成 error。

3. **taken_at 三级回退必须记录来源**。EXIF 最准；文件名次之（截图、微信保存
   的图常无 EXIF 但名字带时间）；mtime 最不准（复制、同步都会改它）。
   `taken_src` 入库是因为用户在时间线上看到排序"不对"时需要能解释原因。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

# --------------------------------------------------------------------------
# Pillow 配置（必须在任何 Image.open 之前）
# --------------------------------------------------------------------------

#: 单张图允许的最大像素数。
#:
#: ⚠️ 这个值必须按**真实相机上限**来定，不能凭"够用就行"拍脑袋。
#: 第一版设 5000 万，实测直接炸了：真实库里有 50331648 像素（48MP，
#: 8192×6144）的**正常照片**，Pillow 判定为解压炸弹。设成 2 亿（200MP）
#: 后，市面上任何手机/相机（当前最高 2 亿像素）都能通过，
#: 而 661 字节声明 4×10^10 像素的攻击文件仍被挡住 ——
#: Pillow 的报错阈值是 MAX × 2 = 4 亿，攻击值高它两个数量级。
MAX_IMAGE_PIXELS: Final[int] = 200_000_000

_PILLOW_READY = False


def _prepare_pillow() -> None:
    """注册 HEIC 解码器 + 收紧解压炸弹阈值。幂等。"""
    global _PILLOW_READY
    if _PILLOW_READY:
        return

    from PIL import Image

    # HEIC/HEIF 必须支持：iPhone 默认格式，一期白名单里有 heic/heif。
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except ImportError:                                     # pragma: no cover
        logging.getLogger(__name__).warning(
            "pillow-heif 未安装，HEIC/HEIF 将无法生成缩略图（索引仍可用）"
        )

    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    # ★ 把警告升级为异常。只设 MAX_IMAGE_PIXELS 时，Pillow 在 open() 阶段
    # 对 (MAX, 2MAX] 区间的图只发 DecompressionBombWarning 不抛错，
    # 真正的报错要等到 load()。不升级的话这个区间会静默通过并分配巨量内存。
    # 注意升级后抛的是 **DecompressionBombWarning**（RuntimeWarning 子类），
    # 不是 DecompressionBombError —— 两个都要在 except 里接住。
    warnings.simplefilter("error", Image.DecompressionBombWarning)

    # 真实库里大量文件带轻微损坏的 EXIF（实测每个文件都刷一条
    # "Corrupt EXIF data. Expecting to read 4 bytes but only got 0."）。
    # 这类文件我们**能**读出尺寸和大部分字段，不该让噪声刷满日志。
    warnings.filterwarnings("ignore", message=r".*Corrupt EXIF data.*", category=UserWarning)

    _PILLOW_READY = True


#: EXIF 抽取线程池。4 个 worker 对应 thumbs.workers 默认值，
#: 避免与缩略图生成抢满 CPU（那会让索引期间整机卡顿）。
_EXIF_POOL: Final[ThreadPoolExecutor] = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="exif"
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass(slots=True)
class ExifInfo:
    """从一张图里能拿到的全部元数据。字段名与 media 表列名一一对应。"""

    width: int | None = None
    height: int | None = None
    orientation: int | None = None
    camera: str | None = None
    lens: str | None = None
    iso: int | None = None
    fnum: float | None = None
    exposure: float | None = None
    focal: float | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    taken_at: float | None = None
    #: 是否成功读出过 EXIF 块（用于区分"没有 EXIF"与"解析失败"）
    has_exif: bool = False
    #: 解析失败的原因，仅用于日志
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "width": self.width, "height": self.height, "orientation": self.orientation,
            "camera": self.camera, "lens": self.lens, "iso": self.iso,
            "fnum": self.fnum, "exposure": self.exposure, "focal": self.focal,
            "gps_lat": self.gps_lat, "gps_lon": self.gps_lon,
            "taken_at": self.taken_at, "has_exif": self.has_exif,
        }


#: taken_at 的来源。写进 media.taken_src，CHECK 约束只允许这三个值。
TakenSrc = Literal["exif", "filename", "mtime"]

#: 时间合法区间：1980-01-01 起。早于此的 EXIF 一定是相机没设时间。
MIN_VALID_TS: Final[float] = 315532800.0


# --------------------------------------------------------------------------
# 同步抽取（只能在线程池里调用）
# --------------------------------------------------------------------------

# EXIF tag 号（docs/03 §4.1.3 的表）
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_ORIENTATION = 0x0112
_TAG_DATETIME = 0x0132
_TAG_EXIF_IFD = 0x8769
_TAG_GPS_IFD = 0x8825
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME_DIGITIZED = 0x9004
_TAG_ISO = 0x8827
_TAG_FNUMBER = 0x829D
_TAG_EXPOSURE_TIME = 0x829A
_TAG_FOCAL_LENGTH = 0x920A
_TAG_LENS_MODEL = 0xA434
_TAG_PIXEL_X = 0xA002
_TAG_PIXEL_Y = 0xA003
_TAG_GPS_LAT_REF = 0x0001
_TAG_GPS_LAT = 0x0002
_TAG_GPS_LON_REF = 0x0003
_TAG_GPS_LON = 0x0004

_EXIF_DT_FORMATS: Final[tuple[str, ...]] = (
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y:%m:%d %H:%M:%S.%f",
)


def extract_sync(path: str | Path) -> ExifInfo:
    """同步抽取 EXIF。**只能通过 extract() 在线程池里调用。**

    坏文件绝不抛异常：返回空 ExifInfo 并记 debug 日志，让调用方走三级回退。
    索引器面对的是 4952 个用户文件，其中必然有损坏的、被截断的、
    扩展名与内容不符的（实测库里 22 个 .jpg-lx2 真实内容是 PNG）。
    任何"一个坏文件中断整轮扫描"的实现都是不可接受的。
    """
    _prepare_pillow()
    from PIL import Image

    info = ExifInfo()
    try:
        with Image.open(path) as im:
            info.width, info.height = im.size
            exif = im.getexif()
            if exif:
                info.has_exif = True
                _read_exif(info, exif, im)
    except Image.DecompressionBombError as e:
        info.error = f"decompression_bomb: {e}"
        log.warning("拒绝疑似解压炸弹的图片 %s: %s", path, e)
    except Image.DecompressionBombWarning as e:
        # 因为上面把 warning 升级成了 error，落在 (MAX, 2MAX] 区间的图
        # 会走到这里。真实库里有 48MP 的正常照片，所以 MAX 定得很宽；
        # 走到这里说明像素数确实异常，但文件本身可能仍是合法大图，
        # 因此按"可读但超大"处理：记 info 不记 warning。
        info.error = f"decompression_bomb_warn: {e}"
        log.debug("图片像素数异常偏大 %s: %s", path, e)
    except Image.UnidentifiedImageError as e:
        info.error = f"unidentified_image: {e}"
    except (OSError, ValueError, SyntaxError, TypeError, KeyError, IndexError) as e:
        # 覆盖：截断文件（OSError）、畸形 EXIF（ValueError/KeyError/IndexError）、
        # HEIC 解析异常（SyntaxError）、Pillow 内部类型错误（TypeError）。
        info.error = f"{type(e).__name__}: {e}"
        log.debug("EXIF 抽取失败 %s: %s", path, e)
    return info


def _read_exif(info: ExifInfo, exif: Any, im: Any) -> None:
    """从已打开的 Image 里填 ExifInfo。所有字段读取都是尽力而为。"""
    # --- 0th IFD ---
    info.camera = _clean_pair(exif.get(_TAG_MAKE), exif.get(_TAG_MODEL))
    info.orientation = _int_or_none(exif.get(_TAG_ORIENTATION))

    # --- Exif 子 IFD ---
    try:
        sub = exif.get_ifd(_TAG_EXIF_IFD) or {}
    except (KeyError, ValueError, TypeError):
        sub = {}
    info.iso = _int_or_none(sub.get(_TAG_ISO))
    info.fnum = _float_or_none(sub.get(_TAG_FNUMBER))
    info.exposure = _ratio_to_float(sub.get(_TAG_EXPOSURE_TIME))
    info.focal = _ratio_to_float(sub.get(_TAG_FOCAL_LENGTH))
    info.lens = _clean(sub.get(_TAG_LENS_MODEL))

    info.taken_at = (
        _parse_exif_dt(sub.get(_TAG_DATETIME_ORIGINAL))
        or _parse_exif_dt(sub.get(_TAG_DATETIME_DIGITIZED))
        or _parse_exif_dt(exif.get(_TAG_DATETIME))
    )

    # 部分机型（尤其 iPhone 的 HEIC）尺寸只写在 Exif 子 IFD
    if not info.width:
        info.width = _int_or_none(sub.get(_TAG_PIXEL_X))
        info.height = _int_or_none(sub.get(_TAG_PIXEL_Y))

    # --- GPS IFD ---
    try:
        gps = exif.get_ifd(_TAG_GPS_IFD) or {}
    except (KeyError, ValueError, TypeError):
        gps = {}
    info.gps_lat, info.gps_lon = _parse_gps(gps)

    # --- 兜底：部分 iPhone HEIC 的 GPS 只在 im.info["exif"] 里 ---
    # 走第二条路是必要的：pillow-heif 对某些机型把 GPS 放在原始 exif 字节流中，
    # getexif() 拿不到。两条路都试，取第一个成功的结果。
    if info.gps_lat is None:
        raw = None
        try:
            raw = im.info.get("exif")
        except (AttributeError, TypeError):
            raw = None
        if raw:
            try:
                alt = im.getexif().get_ifd(_TAG_GPS_IFD)
                if not alt and isinstance(raw, bytes):
                    # 手工解析：跳过 "Exif\0\0" 头后交给 TiffImagePlugin
                    from PIL import TiffImagePlugin

                    body = raw[6:] if raw[:6] == b"Exif\x00\x00" else raw
                    parsed = TiffImagePlugin.ImageFileDirectory_v2()
                    parsed.load(body)
                    alt = parsed
                if alt:
                    info.gps_lat, info.gps_lon = _parse_gps(alt)
            except Exception as e:  # noqa: BLE001 — 兜底路径失败不影响主流程
                log.debug("GPS 兜底解析失败: %s", e)


# --------------------------------------------------------------------------
# 异步封装
# --------------------------------------------------------------------------


async def extract(path: str | Path) -> ExifInfo:
    """异步抽取 EXIF。内部丢线程池，不阻塞事件循环。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXIF_POOL, extract_sync, path)


def shutdown_pool(wait: bool = True) -> None:
    """进程退出时收线程池。"""
    _EXIF_POOL.shutdown(wait=wait, cancel_futures=True)


# --------------------------------------------------------------------------
# 类型转换工具（全部"失败返回 None"，不抛异常）
# --------------------------------------------------------------------------


def _int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    try:
        if isinstance(v, (tuple, list)):
            v = v[0] if v else None
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    try:
        if isinstance(v, (tuple, list)):
            v = v[0] if v else None
        f = float(v)
    except (TypeError, ValueError):
        return None
    # NaN / inf 会让 SQLite 存进奇怪的值，且 JSON 序列化会失败
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f if f > 0 else None


def _ratio_to_float(v: Any) -> float | None:
    """EXIF RATIONAL → float。Pillow 有时给 IFDRational，有时给 (num, den) 元组。"""
    if v is None:
        return None
    try:
        if isinstance(v, (tuple, list)) and len(v) == 2:
            num, den = v
            if den in (0, None):
                return None
            f = float(num) / float(den)
        else:
            f = float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if f != f or f <= 0:
        return None
    return f


def _clean(v: Any) -> str | None:
    """清理字符串字段：去空白与 NUL，截断到合理长度，空串转 None。

    EXIF 字符串里常见尾部 NUL 与多余空格（`"Xiaomi\\x00"`、`"Xiaomi  "`）。
    不清理会让前端显示 `Xiaomi` 后面带一个不可见字符。
    """
    if v is None:
        return None
    if isinstance(v, bytes):
        try:
            v = v.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return None
    if not isinstance(v, str):
        v = str(v)
    v = v.replace("\x00", "").strip()
    if not v:
        return None
    return v[:128]


def _clean_pair(*vals: Any) -> str | None:
    """Make + Model 合并。厂商名常是型号的前缀（"Xiaomi" + "Xiaomi 13"），
    直接拼接会得到 "Xiaomi Xiaomi 13"，所以要判断包含关系。"""
    parts = [_clean(v) for v in vals]
    parts = [p for p in parts if p]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    a, b = parts[0], parts[1]
    if b.lower().startswith(a.lower()):
        return b[:128]
    if a.lower().startswith(b.lower()):
        return a[:128]
    return f"{a} {b}"[:128]


def _parse_exif_dt(v: Any) -> float | None:
    """解析 EXIF 时间字符串为 Unix 秒。超出合理区间判为脏数据返回 None。"""
    s = _clean(v)
    if not s:
        return None
    for fmt in _EXIF_DT_FORMATS:
        try:
            ts = datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
        return ts if _ts_in_range(ts) else None
    return None


def _parse_gps(gps: Any) -> tuple[float | None, float | None]:
    """解析 GPS IFD → (纬度, 经度)。

    EXIF 存的是 (度, 分, 秒) 三个 RATIONAL，配合 N/S/E/W 参考位。
    常见坑：某些设备把参考位写成小写、或写成 b'N'；也有设备省略参考位。
    参考位缺失时按正值处理（中国境内 E/N 都是正）。
    """
    if not gps:
        return None, None
    lat = _dms_to_deg(_gps_get(gps, _TAG_GPS_LAT), _gps_get(gps, _TAG_GPS_LAT_REF), neg="S")
    lon = _dms_to_deg(_gps_get(gps, _TAG_GPS_LON), _gps_get(gps, _TAG_GPS_LON_REF), neg="W")
    # 越界值一律丢弃：GPS 解析错位会得到 (0,0) 或 91.x 这类明显无效的值
    if lat is not None and not (-90.0 <= lat <= 90.0):
        lat = None
    if lon is not None and not (-180.0 <= lon <= 180.0):
        lon = None
    # (0,0) 是几内亚湾，几乎总是"没有 GPS"被误解析的结果
    if lat == 0.0 and lon == 0.0:
        return None, None
    return lat, lon


def _gps_get(gps: Any, tag: int) -> Any:
    try:
        return gps.get(tag)
    except (AttributeError, KeyError, TypeError):
        return None


def _dms_to_deg(dms: Any, ref: Any, *, neg: str) -> float | None:
    if dms is None:
        return None
    try:
        if isinstance(dms, (int, float)):
            val = float(dms)
        else:
            parts = list(dms)
            if not parts:
                return None
            d = float(parts[0])
            m = float(parts[1]) if len(parts) > 1 else 0.0
            s = float(parts[2]) if len(parts) > 2 else 0.0
            val = d + m / 60.0 + s / 3600.0
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        return None
    if val != val:
        return None
    r = _clean(ref)
    if r and r.upper().startswith(neg):
        val = -val
    return round(val, 7)


# --------------------------------------------------------------------------
# 文件名日期解析（docs/03 §4.1.5 + 对真实库的覆盖率修正）
# --------------------------------------------------------------------------

#: 模式按优先级排列：越具体的越靠前，避免 "20240512" 被更宽泛的规则误匹配。
#:
#: ⚠️ 设计文档 §4.1.5 的原始列表对**真实库的覆盖率只有 6.2%**
#: （549 个无 EXIF 拍摄时间的文件中只命中 34 个）。这里按实测的库内命名形态
#: 做了扩充，新增的模式都标注了来源样例。每一条都是先测出漏网文件、
#: 再补规则，不是凭想象加的。
NAME_PATTERNS: Final[list[tuple[re.Pattern[str], str]]] = [
    # ---- 1. 连写日期+时间（可带毫秒），分隔符任意 ----
    # IMG_20240512_183000 / PXL_20240512_183000123 / Screenshot_20240512-183000
    # / VID_20240512_183000 / mmexport 等前缀均不影响（用 search 不是 match）
    (re.compile(
        r"(?<!\d)(?P<Y>19\d{2}|20\d{2})(?P<m>0[1-9]|1[0-2])(?P<d>0[1-9]|[12]\d|3[01])"
        r"[_\-T .]?(?P<H>[01]\d|2[0-3])(?P<M>[0-5]\d)(?P<S>[0-5]\d)(?P<ms>\d{0,3})(?!\d)"
    ), "YmdHMS"),
    # ---- 2. 带分隔符的日期 + 时间 ----
    # 2024-05-12 18:30:00 / 2024-05-12_18-30-00 / 2024.05.12 18.30.00
    # / 2024年05月12日 18:30:00 / 2024年05月12日_183000
    # / cafestella_screenshot 2020_12_07 23_15_47  ← 下划线作日期分隔符，
    #   实测库内存在，最初漏掉就是因为日期分隔符集合里没有 '_'
    # ⚠️ 两条容易踩的坑：
    #  1) 日期与时间之间的分隔符**不能**包含 '-' 和 '.'。`2024-05-12_18-30-00`
    #     里 `12_18` 也可读成 "d=1, 分隔符=_ , H=2, M=18"，正则引擎左侧最短
    #     匹配优先，会先接受这个错误切分，解析出 2024-05-01 02:18:30。
    #  2) 该分隔符必须是**强制**的，不能写成 `[ _\-T]?`。否则
    #     `2024-05-12_183000` 会切成 d=1、H=2、M=18、S=30，
    #     同样得到 2024-05-01 02:18:30。日期后紧跟 6 位时间却没有分隔符的
    #     形态由模式 1 的连写规则负责，这里不必也不该容忍。
    #  '-' 与 '.' 留给日期内部（年-月-日）和时间内部（时-分-秒）各自使用。
    #  "日" 字本身是硬边界，可以充当分界符（`2024年05月12日183000`），
    #  它后面不可能是月份的延续，因此不存在上面第 1 条的歧义。
    (re.compile(
        r"(?<!\d)(?P<Y>19\d{2}|20\d{2})[-._\u5e74](?P<m>0?[1-9]|1[0-2])[-._\u6708]"
        r"(?P<d>0?[1-9]|[12]\d|3[01])(?:\u65e5[ _\-T]?|[ _\-T])"
        r"(?P<H>[01]?\d|2[0-3])[:\-._]?(?P<M>[0-5]\d)(?:[:\-._]?(?P<S>[0-5]\d))?(?P<ms>\d{0,3})(?!\d)"
    ), "YmdHMS"),
    # ---- 3. 只有日期（补 12:00:00）----
    # 2024-05-12 / 2024.05.12 / 2024年05月12日 / 2024_05_12
    (re.compile(
        r"(?<!\d)(?P<Y>19\d{2}|20\d{2})[-._\u5e74](?P<m>0?[1-9]|1[0-2])[-._\u6708]"
        r"(?P<d>0?[1-9]|[12]\d|3[01])\u65e5?(?![\d:])"
    ), "Ymd"),
    # ---- 4. 连续 8 位日期（补 12:00:00）----
    (re.compile(
        r"(?<!\d)(?P<Y>19\d{2}|20\d{2})(?P<m>0[1-9]|1[0-2])(?P<d>0[1-9]|[12]\d|3[01])(?!\d)"
    ), "Ymd"),
    # ---- 5. 13 位毫秒时间戳（微信 mmexport / 手机厂商导出）----
    (re.compile(r"(?<!\d)(?P<ts>1[0-9]{12})(?!\d)"), "MS_EPOCH"),
    # ---- 6. 10 位秒级时间戳 ----
    (re.compile(r"(?<!\d)(?P<ts>1[0-9]{9})(?!\d)"), "S_EPOCH"),
]


def parse_datetime_from_name(name: str) -> float | None:
    """从文件名解析拍摄时间。失败返回 None。

    用 `search` 而不是 `match`：真实库里文件名普遍带前缀
    （`mmexport1715526600000`、`wx_camera_1715526600000`、`DOKA_20240512_183000`），
    用 match 会全部漏掉。
    """
    if not name:
        return None
    stem = name.rsplit(".", 1)[0] if "." in name else name
    for pat, fmt in NAME_PATTERNS:
        for m in pat.finditer(stem):
            ts = _ts_from_match(m, fmt)
            if ts is not None:
                return ts
    return None


def _ts_from_match(m: re.Match[str], fmt: str) -> float | None:
    g = m.groupdict()
    try:
        if fmt == "MS_EPOCH":
            ts = int(g["ts"]) / 1000.0
        elif fmt == "S_EPOCH":
            ts = float(g["ts"])
        else:
            h = int(g.get("H") or 12)
            mi = int(g.get("M") or 0)
            s = int(g.get("S") or 0)
            ms_raw = g.get("ms") or "0"
            us = int(ms_raw.ljust(3, "0")[:3]) * 1000
            dt = datetime(int(g["Y"]), int(g["m"]), int(g["d"]), h, mi, s, us)
            ts = dt.timestamp()
    except (ValueError, KeyError, OSError, OverflowError):
        return None
    return ts if _ts_in_range(ts) else None


def _ts_in_range(ts: float) -> bool:
    """1980-01-01 ~ 明天。

    上界给一天余量是因为手机与电脑时钟可能差几小时（时区没同步的设备）。
    超出上界的一律判为脏数据 —— 相机没设时间时常出现 2035 年这类值。
    """
    return MIN_VALID_TS <= ts <= time.time() + 86400.0


# --------------------------------------------------------------------------
# 三级回退
# --------------------------------------------------------------------------


def resolve_taken_at(
    exif: ExifInfo | None, file_name: str, mtime: float
) -> tuple[float, TakenSrc]:
    """决定 taken_at 与来源。返回 (Unix 秒, 来源)。

    顺序：EXIF → 文件名 → mtime。理由见模块文档字符串第 3 点。
    """
    if exif is not None and exif.taken_at and _ts_in_range(exif.taken_at):
        return exif.taken_at, "exif"
    ts = parse_datetime_from_name(file_name)
    if ts is not None:
        return ts, "filename"
    # mtime 也可能是脏的（比如 1970 或未来时间），但此时没有更好的选择。
    # 仍做一次夹取，避免时间线上出现 1970 年的照片把滚动条拉到没意义的位置。
    if not _ts_in_range(mtime):
        mtime = min(max(mtime, MIN_VALID_TS), time.time())
    return mtime, "mtime"


__all__ = [
    "MAX_IMAGE_PIXELS", "MIN_VALID_TS", "NAME_PATTERNS", "ExifInfo", "TakenSrc",
    "extract", "extract_sync", "parse_datetime_from_name", "resolve_taken_at",
    "shutdown_pool",
]
