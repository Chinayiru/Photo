"""对真实库跑一次覆盖率实测（抽样）。

这是唯一能证明 exif.py 真的可用的办法：拿 4952 个真实文件跑，
看 taken_at 三级回退各占多少、多少文件最终落到最不可靠的 mtime。

只读，不写任何东西。抽样默认 400 个文件，可用 --all 跑全量。
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.config import ALLOWED_IMAGE_EXTS, DEFAULT_EXCLUDE_DIRS  # noqa: E402
from app.exif import extract_sync, parse_datetime_from_name, resolve_taken_at  # noqa: E402

LIBRARY = Path(r"D:\Photos")


def collect(limit: int | None, seed: int) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(LIBRARY, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS]
        for fn in filenames:
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            if ext not in ALLOWED_IMAGE_EXTS:
                continue
            files.append(Path(dirpath) / fn)
    files.sort()
    if limit and len(files) > limit:
        random.Random(seed).shuffle(files)
        files = sorted(files[:limit])
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if not LIBRARY.exists():
        print(f"库不存在: {LIBRARY}")
        return 2

    files = collect(None if args.all else args.limit, args.seed)
    print(f"库: {LIBRARY}")
    print(f"抽样: {len(files)} 个文件\n")

    src = Counter()
    ext_counter = Counter()
    no_exif = 0
    exif_err = Counter()
    filename_hits = 0
    filename_only = 0
    mtime_only = 0
    cam = Counter()
    gps_n = 0
    slow: list[tuple[float, str]] = []
    t0 = time.time()

    for i, p in enumerate(files, 1):
        t = time.perf_counter()
        info = extract_sync(p)
        dt_ms = (time.perf_counter() - t) * 1000
        slow.append((dt_ms, p.name))

        ext_counter[p.suffix.lower()] += 1
        if info.error:
            exif_err[info.error.split(":")[0]] += 1
        if not info.has_exif and not info.error:
            no_exif += 1

        taken, s = resolve_taken_at(info, p.name, p.stat().st_mtime)
        src[s] += 1
        if s == "filename":
            filename_hits += 1
            if not info.taken_at:
                filename_only += 1
        if s == "mtime":
            mtime_only += 1

        if info.camera:
            cam[info.camera] += 1
        if info.gps_lat is not None:
            gps_n += 1

        if i % 100 == 0:
            print(f"  …{i}/{len(files)}")

    el = time.time() - t0
    n = len(files)

    print(f"\n{'='*60}")
    print(f"耗时 {el:.1f}s，平均 {el/n*1000:.1f} ms/文件")
    print(f"\ntaken_at 来源分布:")
    for k in ("exif", "filename", "mtime"):
        c = src.get(k, 0)
        print(f"  {k:9} {c:5}  {c/n*100:6.1f}%")

    print(f"\nEXIF 情况:")
    print(f"  完全无 EXIF 块        {no_exif:5}  {no_exif/n*100:6.1f}%")
    print(f"  解析出错              {sum(exif_err.values()):5}  {sum(exif_err.values())/n*100:6.1f}%")
    for k, v in exif_err.most_common(8):
        print(f"      {k:32} {v}")

    print(f"\n文件名解析:")
    print(f"  命中（任何情况）      {filename_hits:5}  {filename_hits/n*100:6.1f}%")
    print(f"  ★ 无 EXIF 时救回来    {filename_only:5}  {filename_only/n*100:6.1f}%")

    print(f"\n最终只能靠 mtime（最不可靠）: {mtime_only}  {mtime_only/n*100:.1f}%")
    print(f"有 GPS: {gps_n}  ({gps_n/n*100:.1f}%)")

    print(f"\n扩展名分布: {dict(ext_counter.most_common(12))}")
    print(f"\n相机 Top10:")
    for k, v in cam.most_common(10):
        print(f"  {v:5}  {k}")

    slow.sort(reverse=True)
    print(f"\n最慢 5 个:")
    for ms, name in slow[:5]:
        print(f"  {ms:8.1f} ms  {name[:70]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
