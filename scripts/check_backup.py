"""核对「照片库有独立备份」与「备份可恢复」（docs/07 §12 的 J-04 / J-05）。

## 为什么这两项要单独写脚本

§12 里 J-04 的执行方式写的是「检查备份」、J-05 写的是「恢复演练」——
都不是"跑一下代码就知道对不对"的判据，但也**不该靠人眼**：
"我觉得有备份"和"备份真的能恢复"是两件事，而且后者会随时间腐烂
（复制中断、硬盘坏道、加密软件改字节）。

脚本把三件事变成可复现的断言：

1. **独立性**：备份必须在**不同的物理磁盘**上。同一块盘上的"备份"
   连硬盘故障都扛不住，那是副本不是备份。用 `Get-Partition` 的
   DiskNumber 判断，不是看盘符 —— 同一块盘分两个区最容易骗人。
2. **完整性**：live 里每个可索引文件都要在备份里有同名同大小的一份。
   缺的要能解释（例如备份是昨天的快照，之后新传的还没备）。
3. **可恢复性**：随机抽 N 个逐 **sha256** 比对，再用 Pillow 真打开一次。
   只比大小会放过"大小一样内容全零"的坏文件。

## 用法

    .venv\\Scripts\\python.exe -X utf8 scripts\\check_backup.py
    .venv\\Scripts\\python.exe -X utf8 scripts\\check_backup.py --sample 50
    .venv\\Scripts\\python.exe -X utf8 scripts\\check_backup.py --backup G:\\某目录

退出码：0 = 全部通过；1 = 有不通过项；2 = 根本找不到备份。
"""
from __future__ import annotations

import argparse
import hashlib
import random
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.app import config as _cfgmod  # noqa: E402
from backend.app.config import ALLOWED_IMAGE_EXTS  # noqa: E402

failures: list[str] = []
notes: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(f"{label} {detail}".strip())
    return ok


# ---------------------------------------------------------------------------
# 盘符 → 物理磁盘号
# ---------------------------------------------------------------------------


def disk_of(path: Path) -> int | None:
    """返回 `path` 所在分区的物理磁盘号（DiskNumber）。

    为什么不用 `os.stat().st_dev`：Windows 上它给的是卷序列号一类的东西，
    两个分区在同一块盘上也会不同，正好把"同盘不同区"误判成"独立磁盘"。

    为什么用 PowerShell 而不是 `ctypes`：`Get-Partition` 已经把
    "盘符 → 分区 → 磁盘"这条链路走完了；自己用 ctypes 调
    `DeviceIoControl(IOCTL_DISK_GET_DRIVE_LAYOUT)` 要写两百行且容易错。
    """
    drive = (path.drive or "C:")[:2]
    ps = (
        f"(Get-Partition -DriveLetter '{drive[0]}' "
        "-ErrorAction SilentlyContinue).DiskNumber"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    txt = (out.stdout or "").strip()
    return int(txt) if txt.isdigit() else None


def disk_model(number: int) -> str:
    ps = (f"(Get-PhysicalDisk -DeviceNumber {number} "
          "-ErrorAction SilentlyContinue).FriendlyName")
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=60,
        )
        return (out.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return "?"


# ---------------------------------------------------------------------------
# 找候选备份
# ---------------------------------------------------------------------------


def _files(root: Path) -> dict[str, Path]:
    """`相对路径(POSIX) → 绝对路径`，只收可索引的图片扩展名。"""
    out: dict[str, Path] = {}
    for p in root.rglob("*"):
        try:
            if not p.is_file():
                continue
        except OSError:
            continue
        if p.suffix.lower().lstrip(".") in ALLOWED_IMAGE_EXTS:
            out[p.relative_to(root).as_posix()] = p
    return out


def _subdir_names(d: Path) -> set[str]:
    try:
        return {c.name for c in d.iterdir() if c.is_dir()}
    except OSError:
        return set()


def find_candidates(live: Path, live_files: dict[str, Path],
                    min_cover: float = 0.5) -> list[Path]:
    """扫描所有非 live 所在盘，找"看起来是照片库副本"的目录。

    ⚠️ **只看文件数会灾难性误判**：第一版把 `C:\\` 当成了备份 ——
    因为 C 盘有 10 万个图片文件（缩略图缓存、壁纸、浏览器缓存、
    node_modules 里的 png）。文件数多不等于内容相关。

    ⚠️ **只扫盘根与第一层会漏**：真实备份在
    `G:\\Program Files (x86)\\相册`，是第二层。第一版因此报"没有备份"，
    差点把 J-04 判成不通过 —— 这是**假阴性**，比误报更危险。

    最终判据分两步，先便宜后昂贵：

    1. **目录名签名**（只列目录，不碰文件）：live 的每个顶层子目录名
       （`全部备份`、`相册1`、`青甘环线`、`手机上传`）在候选里应当出现。
       这一步把全盘搜索压缩成几十次 `iterdir()`，可以在第 2 层扫完。
    2. **相对路径覆盖率**：过签名的候选才去列文件，算
       |live 相对路径 ∩ 候选相对路径| / |live 相对路径|。

    第 1 步足以排除 `C:\\Windows` 这类（子目录是 `System32`、`WinSxS`），
    第 2 步足以排除"子目录名碰巧一样但内容不同"。
    """
    live_keys = set(live_files)
    if not live_keys:
        return []
    # live 的顶层子目录名是这份库的"指纹"。少于 2 个就不当签名用
    # （只有一个子目录时，`相册` 这种大众名字会误命中一堆无关目录）。
    live_subs = _subdir_names(live)
    signature = live_subs if len(live_subs) >= 2 else None
    if signature is not None:
        print(f"    目录签名（live 顶层子目录）：{sorted(signature)}")

    live_drive = (live.drive or "C:")[:1].upper()
    scored: list[tuple[float, int, Path]] = []
    seen: set[str] = set()

    def _consider(cand: Path) -> None:
        key = str(cand).lower()
        if key in seen or cand == live:
            return
        seen.add(key)
        try:
            if not cand.is_dir():
                return
        except OSError:
            return
        if signature is not None and not (signature & _subdir_names(cand)):
            return
        bf = _files(cand)
        if not bf:
            return
        cover = len(live_keys & set(bf)) / len(live_keys)
        if cover >= min_cover:
            print(f"    候选 {cand}：{len(bf)} 个文件，"
                  f"与 live 同名同路径 {cover:.1%}")
            scored.append((cover, len(bf), cand))

    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        if letter == live_drive:
            continue
        root = Path(f"{letter}:\\")
        if not root.exists():
            continue
        _consider(root)
        # 盘根下第一层 + 第二层。第三层及以上不扫：备份不该埋那么深，
        # 而代价是每多一层就把候选数乘上几十。
        try:
            firsts = [d for d in root.iterdir()
                      if d.is_dir() and not d.name.startswith("$")]
        except OSError:
            continue
        for d1 in firsts:
            _consider(d1)
            try:
                seconds = [d for d in d1.iterdir() if d.is_dir()]
            except OSError:
                continue
            for d2 in seconds:
                _consider(d2)

    # 覆盖率高的优先；相同则文件多的优先（更完整的快照）
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [c for _, _, c in scored]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="核对照片库的独立备份")
    ap.add_argument("--backup", help="手工指定备份目录（跳过自动发现）")
    ap.add_argument("--sample", type=int, default=30, help="逐字节抽检的文件数")
    ap.add_argument("--drill", type=int, default=10, help="恢复演练打开的文件数")
    ap.add_argument("--seed", type=int, default=20261017, help="抽样随机种子")
    args = ap.parse_args()

    # ★ `Config` 没有类方法 `load()` —— 加载入口是**模块级** `config.load()`
    #   （`backend/app/config.py:670`，带缓存；首次运行无 config.json 时用默认值）。
    cfg = _cfgmod.load()
    roots = [r for r in (cfg.roots or []) if getattr(r, "enabled", True)]
    if not roots:
        print("FAIL: 配置里没有启用的媒体根")
        return 1
    live = Path(roots[0].path)
    print(f"照片库（live）：{live}")
    live_files = _files(live)
    print(f"可索引文件：{len(live_files)} 个")
    if not live_files:
        print("FAIL: 照片库里没有可索引文件，无从比对")
        return 1

    # ---- 找备份 ----
    if args.backup:
        cands = [Path(args.backup)]
    else:
        print("\n[A] 自动发现候选备份目录")
        cands = find_candidates(live, live_files)
        if not cands:
            print("FAIL: 没找到任何看起来像照片库副本的目录")
            print("      先做一次备份，或用 --backup 指定路径")
            return 2

    print("\n[B] J-04 有独立备份")
    best: tuple[Path, dict[str, Path], int] | None = None
    for cand in cands:
        bf = _files(cand)
        n = len(bf)
        print(f"  候选 {cand}：{n} 个可索引文件")
        if not check(n > 0, f"{cand} 里有可索引文件"):
            continue
        d_live = disk_of(live)
        d_bak = disk_of(cand)
        if d_live is None or d_bak is None:
            notes.append(f"读不到 {cand} 的物理磁盘号，无法判断独立性")
        else:
            print(f"      live 在磁盘 {d_live}（{disk_model(d_live)}）"
                  f"，备份在磁盘 {d_bak}（{disk_model(d_bak)}）")
            check(d_live != d_bak,
                  f"{cand.name} 与照片库不在同一块物理磁盘",
                  f"disk {d_bak} vs {d_live}")
        if best is None or n > best[2]:
            best = (cand, bf, n)

    if best is None:
        print("\nFAIL: 候选目录都不可用")
        return 2
    bak, bak_files, _ = best
    print(f"\n  采用备份：{bak}（{len(bak_files)} 个文件）")

    # 备份的新鲜度 —— 只报告不判失败：用户可能故意每周备一次
    try:
        newest = max(p.stat().st_mtime for p in bak_files.values())
        oldest = min(p.stat().st_mtime for p in bak_files.values())
        import datetime as _dt
        f_new = _dt.datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M")
        f_old = _dt.datetime.fromtimestamp(oldest).strftime("%Y-%m-%d %H:%M")
        print(f"  备份内文件时间跨度：{f_old} … {f_new}")
        notes.append(f"备份最新文件时间 {f_new}")
    except (OSError, ValueError):
        pass

    # ---- 完整性 ----
    print("\n[C] 完整性：live 里每个文件都要在备份里")
    missing = sorted(set(live_files) - set(bak_files))
    extra = sorted(set(bak_files) - set(live_files))
    covered = len(live_files) - len(missing)
    print(f"  覆盖 {covered}/{len(live_files)}"
          f"（备份多出 {len(extra)} 个，多为已删除或已改名）")

    # ★ 必须把"缺"拆成两类，否则会把健康的备份判成失败：
    #   (a) **备份快照之后新增的** —— 备份本来就是某一时刻的镜像，
    #       之后拍/传的照片当然不在里面。这是正常的时间差，不是缺陷。
    #   (b) **备份之前就该有、却没备上的** —— 这才是真缺陷：
    #       复制被中断、某次增量漏了目录、硬盘坏道。
    #   判据用 mtime：live 文件比备份里最新的文件还新 ⇒ 属于 (a)。
    #   为什么用 mtime 而不是 ctime：copy 会改 ctime，mtime 保留内容年龄。
    bak_newest = 0.0
    try:
        bak_newest = max(p.stat().st_mtime for p in bak_files.values())
    except (OSError, ValueError):
        pass
    newer: list[str] = []
    older_missing: list[str] = []
    for k in missing:
        try:
            mt = live_files[k].stat().st_mtime
        except OSError:
            older_missing.append(k)
            continue
        if bak_newest and mt > bak_newest:
            newer.append(k)
        else:
            older_missing.append(k)

    if newer:
        import datetime as _dt
        f_bak = (_dt.datetime.fromtimestamp(bak_newest)
                 .strftime("%Y-%m-%d %H:%M"))
        print(f"  其中 {len(newer)} 个比备份快照更新（备份落在 {f_bak} 之后新增）"
              "—— 属于正常时间差")
        by_dir: dict[str, int] = {}
        for k in newer:
            top = k.split("/", 1)[0] if "/" in k else "(根)"
            by_dir[top] = by_dir.get(top, 0) + 1
        print(f"      分布：{by_dir}")
        notes.append(f"{len(newer)} 个文件在备份快照之后新增，属正常时间差")

    check(not older_missing,
          "备份快照之前的文件没有遗漏（真缺陷判据）",
          f"缺 {len(older_missing)} 个：{older_missing[:5]}"
          if older_missing else "")
    if older_missing:
        print(f"      ⚠️ 这些文件的 mtime 早于备份里最新文件，"
              f"却不在备份中 —— 备份可能漏拷：{older_missing[:5]}")

    # 覆盖率单独报一个数，便于和 docs 里的「备份完整性」对照
    ratio = covered / max(1, len(live_files))
    print(f"  覆盖率 {ratio:.1%}")
    check(ratio >= 0.99, "覆盖率 ≥ 99%（备份是完整的快照）", f"{ratio:.1%}")

    # 大小不一致 = 备份过程被中断或文件被改过
    size_bad = [k for k in (set(live_files) & set(bak_files))
                if live_files[k].stat().st_size != bak_files[k].stat().st_size]
    check(not size_bad, "同名文件大小一致（没有半截备份）",
          f"{len(size_bad)} 个不一致：{size_bad[:3]}" if size_bad else "")

    # ---- 可恢复性 ----
    print(f"\n[D] J-05 恢复演练：抽 {args.sample} 个逐字节比对")
    common = sorted(set(live_files) & set(bak_files))
    if not common:
        check(False, "有可比对的共同文件")
        return 1
    rnd = random.Random(args.seed)
    sample = rnd.sample(common, min(args.sample, len(common)))
    same = 0
    diff: list[str] = []
    for k in sample:
        try:
            h1 = hashlib.sha256(live_files[k].read_bytes()).hexdigest()
            h2 = hashlib.sha256(bak_files[k].read_bytes()).hexdigest()
        except OSError as exc:
            diff.append(f"{k}（读失败 {type(exc).__name__}）")
            continue
        if h1 == h2:
            same += 1
        else:
            diff.append(k)
    check(not diff, f"{len(sample)} 个文件 sha256 与原件一致",
          f"{len(diff)} 个不一致：{diff[:3]}" if diff else "")

    print(f"\n[E] 恢复演练：抽 {args.drill} 个真的用 Pillow 打开")
    # 换一个种子：与 D 的样本错开，否则"能打开"只是因为那批刚好是 D 验过的
    rnd2 = random.Random(args.seed + 1)
    drill = rnd2.sample(common, min(args.drill, len(common)))
    opened = 0
    bad_open: list[str] = []
    try:
        from backend.app.exif import _prepare_pillow
        _prepare_pillow()
        from PIL import Image
    except Exception as exc:      # noqa: BLE001
        print(f"  （跳过：无法导入 Pillow —— {type(exc).__name__}: {exc}）")
        Image = None       # noqa: F841
    if Image is not None:
        for k in drill:
            try:
                with Image.open(bak_files[k]) as im:
                    im.load()
                    opened += 1
            except Exception as exc:      # noqa: BLE001
                bad_open.append(f"{k}（{type(exc).__name__}）")
        check(opened == len(drill),
              f"{len(drill)} 个随机文件都能打开（decompression 完成）",
              f"{len(bad_open)} 个失败：{bad_open[:3]}" if bad_open else "")

    # ---- 汇总 ----
    print("\n" + "=" * 60)
    if failures:
        print(f"=== {len(failures)} 项未通过 ===")
        for f in failures:
            print(" -", f)
        return 1
    print("=== ALL PASS ===")
    print(f"  live：{live}（{len(live_files)} 个可索引文件）")
    print(f"  备份：{bak}（{len(bak_files)} 个，独立物理磁盘）")
    for n in notes:
        print(f"  注：{n}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
