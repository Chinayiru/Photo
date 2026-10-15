"""归档模块的测试。

这里最要紧的两件事都关于**数据丢失**：
并发同名上传、以及占位文件失败后留下 0 字节的"照片"。
风险登记册 R-004 把"上传覆盖已有照片"列为唯一可能造成永久
数据丢失的软件缺陷，所以测试里有一条专门做破坏性并发。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from backend.app import archive, sandbox
from backend.app.config import Config, RootConfig
from backend.app.errors import AppError, ArchiveError

# 固定时间戳。**不要硬编码"本地时间应该是几点"** —— 同一秒在
# UTC+8 是 21:30、在 UTC 是 13:30，把 18:30 写进断言会在别的时区
# 直接失败，而代码其实是对的。所有期望值都从 datetime.fromtimestamp
# 现算。
TS = 1715520600.0


def _stamp(ts: float = TS) -> str:
    return f"{datetime.fromtimestamp(ts):%Y%m%d_%H%M%S}"


def _root(tmp_path: Path, *, subdir: str = "手机上传", mode: str = "readwrite") -> RootConfig:
    lib = tmp_path / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    return RootConfig(
        id="library",
        path=lib,
        label="库",
        mode=mode,           # type: ignore[arg-type]
        enabled=True,
        recursive=True,
        include_exts=frozenset({"jpg", "png"}),
        exclude_dirs=(),
        exclude_files=(),
    )


def _cfg(tmp_path: Path, root: RootConfig, *, subdir: str = "手机上传") -> Config:
    return Config(
        {
            "roots": [root.__dict__ if hasattr(root, "__dict__") else {
                "id": root.id, "path": str(root.path), "label": root.label,
                "mode": root.mode, "enabled": True, "recursive": True,
            }],
            "upload": {"subdir": subdir},
            "auth": {"allow_delete": True},
        },
        path=tmp_path / "config.json",
        data_dir=tmp_path / "data",
    )


def _src(tmp_path: Path, data: bytes = b"x" * 32, name: str = "a.part") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


# --- 路径生成 --------------------------------------------------------------

def test_archive_dir_uses_local_time_not_utc() -> None:
    """归档目录按**本地**时间。

    23:30 拍的照片在 UTC+8 下是次日 15:30。用 UTC 归档会把一整晚
    的照片挪到下个月甚至下一年，用户按日期找照片时找不到。
    """
    dt = datetime.fromtimestamp(TS)
    assert archive.archive_dir(TS) == f"{dt.year:04d}/{dt.year:04d}-{dt.month:02d}"
    # 钉住"本地时区"这个语义：23:30 这类时刻在 UTC 下可能落到另一天，
    # 归档目录必须跟着**本地**日期走。
    late = datetime.now().replace(hour=23, minute=30, second=0, microsecond=0).timestamp()
    ldt = datetime.fromtimestamp(late)
    assert archive.archive_dir(late) == f"{ldt.year:04d}/{ldt.year:04d}-{ldt.month:02d}"
    assert ldt.hour == 23


def test_archive_name_is_clean_for_first_photo() -> None:
    """第一张不带序号 —— 绝大多数文件名应该是干净的。"""
    assert archive.archive_name(TS, "jpg") == f"{_stamp()}.jpg"
    assert archive.archive_name(TS, "jpg", 1) == f"{_stamp()}_1.jpg"


def test_ext_is_normalized_to_lowercase_with_dot() -> None:
    """`.JPG` / `jpg` / `.jpg` 必须收敛成同一个。

    不收敛的话同一目录里会同时有 `.JPG` 和 `.jpg`，按扩展名筛选
    时漏掉一半（Windows 大小写不敏感所以看不出来）。
    """
    for raw in ("JPG", ".JPG", "jpg", ".jpg", " JPG "):
        assert archive.normalize_ext(raw) == ".jpg"
    assert archive.normalize_ext("") == ""


def test_candidate_names_are_unique_and_bounded() -> None:
    """候选名必须互不相同且有上限。

    没有上限时，若目标目录出现"永远 FileExistsError 但文件又不存在"
    的异常状态（网络盘、杀软隔离区），上传接口永远不返回。
    """
    names = list(archive.candidate_names(TS, "jpg", limit=50))
    assert len(names) == len(set(names))
    assert len(names) <= 52
    assert names[0] == f"{_stamp()}.jpg"
    assert names[1] == f"{_stamp()}_1.jpg"


def test_build_rel_path_layout() -> None:
    rel = archive.build_rel_path(TS, "jpg", subdir="手机上传")
    assert rel.startswith("手机上传/")
    assert rel.endswith(".jpg")
    parts = rel.split("/")
    assert len(parts) == 4
    assert parts[0] == "手机上传"
    assert parts[2] == f"{parts[1]}-{parts[2].split('-')[1]}"


def test_build_rel_path_without_subdir() -> None:
    rel = archive.build_rel_path(TS, "jpg", subdir="")
    assert len(rel.split("/")) == 3


def test_subdir_rejects_traversal_and_illegal_chars() -> None:
    for bad in ("../evil", "a/../../b", "a//b", ".", "..", "a/b/./c"):
        with pytest.raises(AppError):
            archive.subdir_prefix(bad)
    for bad in ("a:b", "a*b", "a?b", 'a"b', "a|b", "a<b", "con", "CON.txt",
                "a./b", "a /b"):
        # "a " 整体结尾的空格是被 strip 掉的（配置里手滑多打一个空格
        # 应该被容忍），但**中间段**结尾的空格/点必须拒绝 ——
        # Windows 会静默去掉它们，校验的名字与实际创建的名字不同。
        with pytest.raises(AppError):
            archive.subdir_prefix(bad)
    assert archive.subdir_prefix(" a/b ") == "a/b"
    assert archive.subdir_prefix("a ") == "a"
    assert archive.subdir_prefix("手机上传") == "手机上传"
    # 段**尾部**的空白/点要拒绝：Windows 会静默去掉它们，
    # 于是"配置里写的目录名"与"实际创建的目录名"不同，
    # 用户按配置去找那个目录会找不到。
    # （前导空格不拒绝 —— Windows 不会去掉它，目录名确实是 " b"。）
    for bad in (" a / b ", "a /b", "a./b"):
        with pytest.raises(AppError):
            archive.subdir_prefix(bad)


def test_subdir_accepts_chinese_and_spaces() -> None:
    """中文目录名与内部空格是合法的（用户会写"我的 照片"）。"""
    assert archive.subdir_prefix("我的 照片") == "我的 照片"


def test_validate_rel_path_rejects_non_archive_names() -> None:
    """归档文件名只允许 [0-9_.]。"""
    archive.validate_rel_path("手机上传/2024/2024-05/20240512_183000.jpg")
    for bad in ("2024/2024-05/照片.jpg", "a/../b.jpg", "", "x y.jpg", "noext", "2024/a.jpg-lx2"):
        with pytest.raises(AppError):
            archive.validate_rel_path(bad)


# --- 落盘 ------------------------------------------------------------------

def test_place_moves_file_to_dated_dir(tmp_path: Path) -> None:
    root = _root(tmp_path)
    cfg = _cfg(tmp_path, root)
    src = _src(tmp_path, b"hello-world")

    rel, dst = asyncio.run(archive.place(src, root, TS, "jpg", subdir="手机上传", cfg=cfg))

    assert dst.is_file()
    assert dst.read_bytes() == b"hello-world"
    assert not src.exists()
    assert rel.startswith("手机上传/")
    assert dst.parent.name.startswith(str(datetime.fromtimestamp(TS).year))


def test_place_never_overwrites_existing_photo(tmp_path: Path) -> None:
    """同名上传必须并存，绝不覆盖。这是 R-004。"""
    root = _root(tmp_path)
    cfg = _cfg(tmp_path, root)

    rel1, dst1 = asyncio.run(archive.place(_src(tmp_path, b"AAA", "a.part"), root, TS, "jpg", subdir="手机上传", cfg=cfg))
    rel2, dst2 = asyncio.run(archive.place(_src(tmp_path, b"BBB", "b.part"), root, TS, "jpg", subdir="手机上传", cfg=cfg))

    assert rel1 != rel2
    assert dst1 != dst2
    assert dst1.read_bytes() == b"AAA"      # 第一张内容完好
    assert dst2.read_bytes() == b"BBB"
    assert dst2.name.endswith("_1.jpg")


def test_place_third_same_second_gets_seq_2(tmp_path: Path) -> None:
    root = _root(tmp_path)
    cfg = _cfg(tmp_path, root)
    rels = [
        asyncio.run(archive.place(_src(tmp_path, bytes([i]) * 8, f"{i}.part"), root, TS, "jpg", subdir="手机上传", cfg=cfg))[0]
        for i in range(3)
    ]
    assert len(set(rels)) == 3
    assert rels[0].endswith(f"{_stamp()}.jpg")
    assert rels[1].endswith(f"{_stamp()}_1.jpg")
    assert rels[2].endswith(f"{_stamp()}_2.jpg")


def test_concurrent_place_same_timestamp_loses_nothing(tmp_path: Path) -> None:
    """并发同名上传：每个文件都必须完整落地，一张都不能少。

    这是 C4 的破坏性验收测试。用 `exists()` 检查再写会 TOCTOU ——
    两个线程同时通过检查、同时写，后写的覆盖先写的，
    用户的照片静默消失一张。
    """
    root = _root(tmp_path)
    cfg = _cfg(tmp_path, root)
    n = 12
    payloads = [bytes([i]) * 64 for i in range(n)]
    results: list[tuple[str, Path] | None] = [None] * n
    errors: list[BaseException] = []
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        try:
            src = _src(tmp_path, payloads[i], f"c{i}.part")
            barrier.wait(timeout=10)
            results[i] = asyncio.run(
                archive.place(src, root, TS, "jpg", subdir="手机上传", cfg=cfg)
            )
        except BaseException as exc:      # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    got = [r for r in results if r is not None]
    assert len(got) == n
    rels = [r[0] for r in got]
    assert len(set(rels)) == n, "并发落盘产生了重复路径"
    # 磁盘上必须恰好有 n 个文件，内容一一对应（无覆盖、无丢失）
    files = sorted(p for p in (root.path / "手机上传").rglob("*.jpg"))
    assert len(files) == n
    contents = sorted(p.read_bytes() for p in files)
    assert contents == sorted(payloads)


def test_place_cleans_placeholder_when_replace_fails(tmp_path: Path, monkeypatch) -> None:
    """os.replace 失败时必须清掉占位文件。

    否则库里留下一个 0 字节的"照片"，它会被索引器收录，
    用户在时间线上看到一张打不开的图。
    """
    root = _root(tmp_path)
    cfg = _cfg(tmp_path, root)

    def boom(src, dst):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(ArchiveError):
        asyncio.run(archive.place(_src(tmp_path, b"x"), root, TS, "jpg", subdir="手机上传", cfg=cfg))

    leftovers = list((root.path / "手机上传").rglob("*.jpg"))
    assert leftovers == []


def test_place_keeps_source_when_failed(tmp_path: Path, monkeypatch) -> None:
    """失败时临时文件必须原样保留，调用方才能重试。"""
    root = _root(tmp_path)
    cfg = _cfg(tmp_path, root)
    src = _src(tmp_path, b"keepme")

    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(OSError(13, "nope")))
    with pytest.raises(ArchiveError):
        asyncio.run(archive.place(src, root, TS, "jpg", subdir="手机上传", cfg=cfg))
    assert src.is_file()
    assert src.read_bytes() == b"keepme"


def test_place_rejects_missing_source(tmp_path: Path) -> None:
    root = _root(tmp_path)
    cfg = _cfg(tmp_path, root)
    with pytest.raises(ArchiveError):
        asyncio.run(archive.place(tmp_path / "nope.part", root, TS, "jpg", cfg=cfg))


def test_place_refuses_readonly_root(tmp_path: Path) -> None:
    """只读根不允许写入 —— 沙箱必须挡住。"""
    root = _root(tmp_path, mode="readonly")
    cfg = _cfg(tmp_path, root)
    with pytest.raises(AppError):
        asyncio.run(archive.place(_src(tmp_path, b"x"), root, TS, "jpg", subdir="手机上传", cfg=cfg))


def test_place_handles_deep_existing_dir(tmp_path: Path) -> None:
    """目标目录已存在（第二张同月照片）时不应报错。"""
    root = _root(tmp_path)
    cfg = _cfg(tmp_path, root)
    asyncio.run(archive.place(_src(tmp_path, b"1", "a.part"), root, TS, "jpg", subdir="手机上传", cfg=cfg))
    rel2, _ = asyncio.run(archive.place(_src(tmp_path, b"2", "b.part"), root, TS, "jpg", subdir="手机上传", cfg=cfg))
    assert rel2.endswith("_1.jpg")


def test_reserve_is_exclusive(tmp_path: Path) -> None:
    """reserve 两次同一个路径，第二次必须 FileExistsError。"""
    root = _root(tmp_path)
    cfg = _cfg(tmp_path, root)
    rel = "手机上传/2024/2024-05/20240512_183000.jpg"
    first = archive.reserve(root, rel, cfg=cfg)
    assert first.is_file() and first.stat().st_size == 0
    with pytest.raises(FileExistsError):
        archive.reserve(root, rel, cfg=cfg)


def test_reserve_creates_parent_dirs(tmp_path: Path) -> None:
    root = _root(tmp_path)
    cfg = _cfg(tmp_path, root)
    p = archive.reserve(root, "手机上传/2030/2030-01/20300101_000000.jpg", cfg=cfg)
    assert p.parent.is_dir()


def test_place_across_different_local_years(tmp_path: Path) -> None:
    """跨年照片进不同年目录。"""
    root = _root(tmp_path)
    cfg = _cfg(tmp_path, root)
    t2024 = datetime(2024, 12, 31, 23, 59, 59).timestamp()
    t2025 = datetime(2025, 1, 1, 0, 0, 1).timestamp()
    r1, d1 = asyncio.run(archive.place(_src(tmp_path, b"1", "a.part"), root, t2024, "jpg", subdir="手机上传", cfg=cfg))
    r2, d2 = asyncio.run(archive.place(_src(tmp_path, b"2", "b.part"), root, t2025, "jpg", subdir="手机上传", cfg=cfg))
    assert "2024/2024-12" in r1.replace("\\", "/")
    assert "2025/2025-01" in r2.replace("\\", "/")


def test_tmp_dir_is_inside_library_root(tmp_path: Path) -> None:
    """临时区必须在库根下（同卷才能原子 replace）。

    放在 data/ 下而库在另一个盘时，os.replace 抛 WinError 17，
    "原子落盘"不可能实现。
    """
    root = _root(tmp_path)
    d = sandbox.tmp_dir(root, create=True)
    assert d.is_dir()
    assert d.parent == root.path.resolve()
    assert d.name == ".photobridge_tmp"


def test_cleanup_tmp_removes_stale_files(tmp_path: Path) -> None:
    root = _root(tmp_path)
    d = sandbox.tmp_dir(root, create=True)
    old = d / ("a" * 20 + ".part")
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    fresh = d / ("b" * 20 + ".part")
    fresh.write_bytes(b"y")

    removed = archive.cleanup_tmp(root)
    assert removed == 1
    assert not old.exists()
    # 刚写进来的不动：用户可能刚好在程序重启前一秒开始传照片
    assert fresh.exists()


def test_cleanup_tmp_on_missing_dir_is_noop(tmp_path: Path) -> None:
    root = _root(tmp_path)
    assert archive.cleanup_tmp(root) == 0
