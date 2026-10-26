"""审计 S06：不可信图片进入解析路径（Pillow 解码器）。

## 这条审计项特殊在哪

别的项修完就完了；这一项修的是**第三方库的漏洞**，所以"仓库改对了"不等于
"用户手上那个程序安全"。Pillow 的解码器是编译进产物里的二进制，
`requirements.txt` 从 10.3 提到 13.0.0 之后：

- `.venv` 里的 Python 是新的（跑测试、跑 `scripts/run_tray.py` 都是它）；
- 但 `dist/PhotoBridge`（PyInstaller onedir 包）是 2026-10-18 打的，
  `_internal/PIL/*.pyd` 还是 Pillow 12.x —— 用户拿到的正是它。

这类"仓库绿、产物旧"的偏差，任何跑在 `.venv` 里的测试都发现不了。所以判据
分两层：

1. **来源层**（永远能查）：`requirements.txt` 的下限、`requirements.lock` 的
   钉版本、打包脚本是否把版本写进产物。
2. **产物层**（有包才查）：`dist/PhotoBridge/build_info.json` 里的 Pillow 必须
   `>= 13`，且 lock 指纹与仓库当前一致 —— 指纹不一致说明包是用另一份依赖
   清单打的，同样要重打。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "packaging" / "photobridge.spec"
REQS = ROOT / "requirements.txt"
LOCK = ROOT / "requirements.lock"
BUILD_INFO = ROOT / "dist" / "PhotoBridge" / "build_info.json"

PILLOW_FLOOR = (13, 0, 0)


def _parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


# ------------------------------------------------------------------ 来源层

def test_requirements_floor_is_pillow_13() -> None:
    text = REQS.read_text(encoding="utf-8")
    m = re.search(r"(?m)^Pillow>=\s*([0-9][^\s#]*)", text)
    assert m, "requirements.txt 里找不到 Pillow 的下限约束"
    assert _parse(m.group(1)) >= PILLOW_FLOOR, (
        f"Pillow 下限是 {m.group(1)}，13 之前有 TIFF/BMP 解码的已知漏洞"
    )


def test_lock_pins_pillow_13_or_newer() -> None:
    text = LOCK.read_text(encoding="utf-8")
    m = re.search(r"(?m)^pillow==([^\s\\]+)", text)
    assert m, "requirements.lock 里没有 pillow 的钉版本"
    assert _parse(m.group(1)) >= PILLOW_FLOOR, f"锁里的 Pillow 是 {m.group(1)}"


def test_packaging_writes_a_version_manifest() -> None:
    """打包脚本必须把实际用到的 Pillow 版本写进产物。

    没有这张表，就只能靠"包是什么时候打的"去猜；有了它，用户机器上
    `type build_info.json` 就能核对。
    """
    text = SPEC.read_text(encoding="utf-8")
    assert "build_info.json" in text, "打包脚本没有写版本清单"
    assert "PIL.__version__" in text or "_PIL.__version__" in text
    assert "requirements_lock_sha256" in text


def test_upload_whitelist_really_reaches_the_tiff_and_bmp_decoders() -> None:
    """S06 不是纸上风险：这两种格式确实能通过上传的魔数校验。

    一旦哪天白名单收紧到只剩 png/jpeg/webp，这条断言会红 —— 那时可以
    把这里改成"确实不可达"，但必须是**知情**地改，不能默默失效。
    """
    from backend.app import uploads

    kinds = {kind for kind, _magic, _off in uploads._MAGIC}   # noqa: SLF001
    assert "tiff" in kinds and "bmp" in kinds


# ------------------------------------------------------------------ 产物层

def test_shipped_bundle_declares_a_patched_pillow() -> None:
    if not BUILD_INFO.is_file():
        # 没有产物（CI、干净 clone）时这一层无从检查，来源层已覆盖
        return
    info = json.loads(BUILD_INFO.read_text(encoding="utf-8"))
    assert _parse(info["pillow"]) >= PILLOW_FLOOR, (
        f"打出来的包里是 Pillow {info['pillow']}，需要 >= 13（重新打包）"
    )
    assert _parse(info["python"]) >= (3, 11)


def test_bundle_actually_carries_the_patched_pillow() -> None:
    """直接从产物里把 `PIL._version` 抠出来读一遍。

    这一步是必要的：`build_info.json` 是**构建时**写的，写的时候读的是
    `.venv` 里的 Pillow —— 如果哪天 spec 被改坏（比如漏了 collect，产物里
    是别的 Pillow），清单和实装就会不一致。这里解开 PYZ 真读一次。
    """
    exe = ROOT / "dist" / "PhotoBridge" / "PhotoBridge.exe"
    if not exe.is_file():
        return
    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError:      # 打包工具没装时跳过（不是每次测都装）
        return

    arch = CArchiveReader(str(exe))
    inner = arch.open_embedded_archive("PYZ.pyz")
    scope: dict[str, object] = {}
    exec(inner.extract("PIL._version"), scope)   # noqa: S102 - 只取常量
    assert _parse(str(scope.get("__version__"))) >= PILLOW_FLOOR, (
        f"产物里冻的是 Pillow {scope.get('__version__')}，S06 之后必须 >= 13"
    )


def test_shipped_bundle_was_built_from_the_current_lock() -> None:
    """包的依赖指纹必须对得上仓库当前这一份 lock。

    对不上只有两种可能：包是旧 lock 打的（要重打），或者 lock 改了没重打
    （同样要重打）。
    """
    if not BUILD_INFO.is_file():
        return
    info = json.loads(BUILD_INFO.read_text(encoding="utf-8"))
    want = hashlib.sha256(LOCK.read_bytes()).hexdigest()
    assert info["requirements_lock_sha256"] == want, (
        "产物记录的依赖指纹与 requirements.lock 不一致，需要重新打包"
    )
