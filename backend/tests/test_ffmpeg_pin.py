"""发布用 ffmpeg 的冻结清单（审查 Q-03）与查找路径的契约。

## 这份测试为什么存在

`build/tools/ffmpeg/` 里那两个 50 MB 的 exe 不受版本控制（`build/` 整体被
`.gitignore` 忽略），但它们是**真正解码用户视频**的组件。所以"我们发的是哪一份
ffmpeg"必须是仓库里能查、能对账的东西 —— 否则换掉 exe 就等于换掉解码行为，
而没人会发现。

`packaging/ffmpeg-pin.json` 是那张账；`scripts/check_ffmpeg_pin.py` 是对账工具；
这里钉住三件事：
1. 账本身完整（版本、来源、两个 exe 的 sha256 与大小）；
2. 账里写的**安装位置**与 `video.find_bin()` 的查找顺序一致
   （不然发布包把 ffmpeg 放对了地方，程序依然找不到）；
3. 本机真有那两份二进制时，它们必须与账一致（没有则跳过 —— CI 上不下载）。
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PIN = REPO / "packaging" / "ffmpeg-pin.json"
VIDEO_PY = REPO / "backend" / "app" / "video.py"
CHECKER = REPO / "scripts" / "check_ffmpeg_pin.py"

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _pin() -> dict:
    return json.loads(PIN.read_text(encoding="utf-8"))


def test_pin_lists_both_binaries_with_full_digests() -> None:
    pin = _pin()
    assert pin["version"].startswith("ffmpeg version ") is False
    assert "essentials" in pin["version"]
    assert pin["source_url"].startswith("https://")
    assert "gyan.dev" in pin["source_url"], "来源必须是可复现的官方构建页"
    files = pin["files"]
    assert set(files) == {"ffmpeg.exe", "ffprobe.exe"}
    for name, meta in files.items():
        assert _HEX64.match(meta["sha256"]), f"{name} 的 sha256 不是 64 位小写十六进制"
        assert meta["size"] > 10 * 1024 * 1024, f"{name} 的大小不合理"
    # 许可证：GPL 构建分发时必须随附
    assert "GPL" in pin["license"]


def test_pin_layout_matches_how_the_program_searches_for_ffmpeg() -> None:
    """账里写的安装位置必须能被 `video.find_bin()` 找到。"""
    pin = _pin()
    src = VIDEO_PY.read_text(encoding="utf-8")
    # 程序会在 <exe 目录>/tools/ffmpeg 下找（以及 tools/ffmpeg/bin）
    assert '"tools" / "ffmpeg"' in src
    assert '"tools" / "ffmpeg" / "bin"' in src
    for rel in pin["install_layout"]:
        assert "tools/ffmpeg/" in rel, f"{rel} 不在 find_bin 的查找范围里"


def test_checker_script_exists_and_is_wired_to_the_pin() -> None:
    src = CHECKER.read_text(encoding="utf-8")
    assert "ffmpeg-pin.json" in src
    assert "sha256" in src
    # 不允许"找不到就放行"：缺失必须算失败
    assert "缺少" in src


@pytest.mark.parametrize("name", ["ffmpeg.exe", "ffprobe.exe"])
def test_local_binaries_match_the_pin_when_present(name: str) -> None:
    local = REPO / "build" / "tools" / "ffmpeg" / name
    if not local.exists():
        pytest.skip("开发机上没有这份二进制（CI 不下载 200 MB）")
    want = _pin()["files"][name]
    assert local.stat().st_size == want["size"]
    h = hashlib.sha256()
    with local.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    assert h.hexdigest() == want["sha256"], f"{name} 与冻结清单不一致"
