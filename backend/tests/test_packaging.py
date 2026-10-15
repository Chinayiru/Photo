"""打包（PyInstaller）与首次运行的行为测试。

## 这些测试防的是什么

打包后才发现的问题，在开发机上**一个都复现不了** ——
因为开发机的目录结构与真实分发环境不同。这个文件用
`monkeypatch` 把 `sys.frozen` / `sys.executable` 伪装成打包形态，
在开发机上就把那三个 bug 钉死：

1. **`APP_ROOT` 必须跟着 exe 走，而不是跟着 `__file__` 走。**
   冻结后 `config.py` 位于 `.../_internal/backend/app/`，
   `parents[2]` 会算出 `.../_internal`，于是数据库与缩略图
   缓存全写进程序目录内部。实测后果：用户升级时删掉旧目录
   就等于删掉整个索引与所有缩略图。
2. **`bootstrap_default_root()` 不许硬编码开发机的路径。**
   原实现写死 `D:\\Photos`，别的用户装好程序后
   会去索引一个不存在的目录 —— 而开发机上有这个目录，
   所以这个 bug 只在分发后才暴露。
3. **自动探测必须真的能找到库**，且不能把 `Adobe_Photoshop_2020`
   当成照片库（"photos" 子串误命中）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from backend.app import config as cfg_mod                              # noqa: E402
from backend.app.config import (                                       # noqa: E402
    _guess_library_paths,
    _has_any_image,
    _is_plausible_parent,
    _resolve_app_root,
)


# ---------------------------------------------------------------------------
# 1. APP_ROOT 在冻结形态下的解析
# ---------------------------------------------------------------------------

def test_app_root_follows_exe_when_frozen(tmp_path, monkeypatch) -> None:
    """★ 打包后 DATA_DIR 必须与 exe 平级，不能落在 _internal 里。"""
    exe_dir = tmp_path / "PhotoBridge"
    exe_dir.mkdir()
    exe = exe_dir / "PhotoBridge.exe"
    exe.write_bytes(b"stub")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    root = _resolve_app_root()
    assert root == exe_dir.resolve()
    assert root.name != "_internal", "绝不能把数据写进 _internal"
    assert (root / "data") == exe_dir.resolve() / "data"


def test_app_root_is_repo_when_not_frozen(monkeypatch) -> None:
    """源码运行时仍然是仓库根（本文件的上两级）。"""
    monkeypatch.delattr(sys, "frozen", raising=False)
    root = _resolve_app_root()
    assert root == REPO, f"源码运行时 APP_ROOT 应是 {REPO}，实际 {root}"
    assert (root / "backend" / "app" / "config.py").is_file()


# ---------------------------------------------------------------------------
# 2. 首次运行不硬编码开发机路径
# ---------------------------------------------------------------------------

def test_bootstrap_does_not_hardcode_dev_path(monkeypatch) -> None:
    """★ 源码里不许再出现写死的 `D:\\Photos`。"""
    src = (REPO / "backend" / "app" / "config.py").read_text(encoding="utf-8")
    # 允许出现在文档/注释里说明历史，但**不许出现在可执行代码**中。
    # 判据：赋值语句右侧是那个字面量。
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""'):
            continue
        assert r"D:\Photos" not in stripped, (
            f"配置代码里仍有硬编码的开发机路径：{stripped}"
        )


def test_bootstrap_writes_empty_roots_when_nothing_found(tmp_path, monkeypatch) -> None:
    """探测不到库时写出**空** roots，而不是猜一个路径。"""
    monkeypatch.setattr(cfg_mod, "_guess_library_paths", lambda: [])
    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", tmp_path / "data" / "config.json")

    cfg = cfg_mod.Config({}, path=tmp_path / "data" / "config.json")
    monkeypatch.setattr(cfg_mod, "_config", cfg, raising=False)
    monkeypatch.setattr(cfg_mod, "load", lambda reload=False: cfg)       # noqa: ARG005
    monkeypatch.setattr(cfg_mod, "get", lambda: cfg)

    saved: list[dict] = []
    monkeypatch.setattr(type(cfg), "save", lambda self, path=None: saved.append(dict(self.raw)))

    cfg_mod.bootstrap_default_root()
    assert cfg.raw["roots"] == [], "探测不到时必须留空，让用户自己选"
    assert saved, "配置必须落盘"


def test_bootstrap_uses_detected_path(tmp_path, monkeypatch) -> None:
    """探测到库时把它写进 roots。"""
    lib = tmp_path / "相册"
    lib.mkdir()
    monkeypatch.setattr(cfg_mod, "_guess_library_paths",
                        lambda: [(lib, "相册")])

    cfg = cfg_mod.Config({}, path=tmp_path / "data" / "config.json")
    monkeypatch.setattr(cfg_mod, "load", lambda reload=False: cfg)       # noqa: ARG005
    monkeypatch.setattr(cfg_mod, "get", lambda: cfg)
    monkeypatch.setattr(type(cfg), "save", lambda self, path=None: None)

    cfg_mod.bootstrap_default_root()
    assert len(cfg.raw["roots"]) == 1
    assert cfg.raw["roots"][0]["path"] == str(lib)
    assert cfg.raw["roots"][0]["mode"] == "readwrite"


# ---------------------------------------------------------------------------
# 3. 自动探测
# ---------------------------------------------------------------------------

def _make_lib(folder: Path, *, n: int = 1) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (folder / f"IMG_{i}.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 32)
    return folder


def test_detects_one_level_layout(tmp_path, monkeypatch) -> None:
    """`D:\\Photos` 这种一层布局。"""
    drive = tmp_path / "drive"
    drive.mkdir()
    _make_lib(drive / "Photos")

    monkeypatch.setattr(cfg_mod, "_PROBE_ROOTS", (str(drive),))
    hits = _guess_library_paths()
    assert [h[0].name for h in hits] == ["Photos"]


def test_detects_two_level_layout(tmp_path, monkeypatch) -> None:
    """★ 本机真实布局：`D:\\Photos` 是两层。"""
    drive = tmp_path / "drive"
    (drive / "software").mkdir(parents=True)
    _make_lib(drive / "software" / "相册")

    monkeypatch.setattr(cfg_mod, "_PROBE_ROOTS", (str(drive),))
    hits = _guess_library_paths()
    assert [h[0].name for h in hits] == ["相册"], (
        "两层布局必须能找到 —— 这正是本机照片库的位置"
    )


def test_two_level_works_even_with_many_siblings(tmp_path, monkeypatch) -> None:
    """★ 回归：同层有大量无关目录时也要找得到。

    第一版实现是"有界 BFS 遍历前 N 个目录再筛名字"，实测在本机
    完全失败：`E:\\` 根下先被枚举的是 120 个无关目录，预算耗尽时
    还没走到 `D:\\Photos`。现在的实现是"按名字直接拼路径"，
    代价与兄弟目录数量无关。
    """
    drive = tmp_path / "drive"
    drive.mkdir()
    for i in range(200):
        (drive / f"junk_{i:03d}").mkdir()
    (drive / "software").mkdir()
    _make_lib(drive / "software" / "相册")

    monkeypatch.setattr(cfg_mod, "_PROBE_ROOTS", (str(drive),))
    hits = _guess_library_paths()
    assert [h[0].name for h in hits] == ["相册"]


def test_does_not_match_substring(tmp_path, monkeypatch) -> None:
    """★ 回归：`Adobe_Photoshop_2020` 不许被当成照片库。

    "photos" 作为子串会命中 `Photoshop`。第一版就是这么错的，
    实测在本机把 `C:\\tools\\Adobe_Photoshop_2020` 选成了照片库。
    """
    drive = tmp_path / "drive"
    (drive / "software").mkdir(parents=True)
    _make_lib(drive / "software" / "Adobe_Photoshop_2020")

    monkeypatch.setattr(cfg_mod, "_PROBE_ROOTS", (str(drive),))
    assert _guess_library_paths() == []


def test_skips_dir_without_images(tmp_path, monkeypatch) -> None:
    """名字对但没有图片 → 不采纳（可能是个空壳目录）。"""
    drive = tmp_path / "drive"
    (drive / "Photos").mkdir(parents=True)
    (drive / "Photos" / "readme.txt").write_text("no images here")

    monkeypatch.setattr(cfg_mod, "_PROBE_ROOTS", (str(drive),))
    assert _guess_library_paths() == []


def test_has_any_image_respects_whitelist(tmp_path) -> None:
    """`.jpg-lx2`（小米回收站残留）不算图片。"""
    folder = tmp_path / "lib"
    folder.mkdir()
    (folder / "a.jpg-lx2").write_bytes(b"x" * 64)
    assert _has_any_image(folder) is False, "jpg-lx2 不是真图片，不该触发采纳"

    (folder / "b.jpg").write_bytes(b"\xff\xd8\xff\xe0")
    assert _has_any_image(folder) is True


def test_has_any_image_is_bounded(tmp_path) -> None:
    """大目录不会让探测遍历到底（否则启动会卡住）。"""
    folder = tmp_path / "big"
    folder.mkdir()
    for i in range(600):
        (folder / f"f{i}.txt").write_text("x")
    assert _has_any_image(folder, limit=100) is False


def test_probe_skips_system_dirs(tmp_path) -> None:
    """一级目录筛选会跳过系统目录。"""
    assert _is_plausible_parent(tmp_path / "Windows") is False
    assert _is_plausible_parent(tmp_path / "Program Files") is False
    assert _is_plausible_parent(tmp_path / "$RECYCLE.BIN") is False
    real = tmp_path / "software"
    real.mkdir()
    assert _is_plausible_parent(real) is True


def test_probe_never_raises_on_bad_drive(monkeypatch) -> None:
    """探测一个不存在的盘符不许抛异常（启动路径不能崩）。"""
    monkeypatch.setattr(cfg_mod, "_PROBE_ROOTS", ("Q:\\nope\\", "Z:\\nope\\"))
    assert _guess_library_paths() == []
