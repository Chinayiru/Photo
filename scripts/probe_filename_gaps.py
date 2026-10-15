"""列出最终只能靠 mtime 的文件名 —— 这就是文件名解析的漏网清单。

每一条漏网都对应一个应该新增的正则模式。先看清单再改代码，
不靠猜"可能还有什么格式"。
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.exif import extract_sync, parse_datetime_from_name, resolve_taken_at  # noqa: E402
from scripts.probe_exif_coverage import LIBRARY, collect  # noqa: E402


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
    files = collect(n, 42)
    missed: list[str] = []
    no_exif_at_all: list[str] = []
    shapes: Counter[str] = Counter()

    for p in files:
        info = extract_sync(p)
        _, s = resolve_taken_at(info, p.name, p.stat().st_mtime)
        if s != "mtime":
            continue
        missed.append(p.name)
        if not info.has_exif:
            no_exif_at_all.append(p.name)
        # 归纳形态：把所有数字替换成 #，看骨架
        import re

        shape = re.sub(r"\d+", "#", p.name)
        shape = re.sub(r"#+", "#", shape)
        shapes[shape] += 1

    print(f"抽样 {len(files)} 个，最终靠 mtime: {len(missed)} ({len(missed)/len(files)*100:.1f}%)")
    print(f"其中完全无 EXIF 块: {len(no_exif_at_all)}\n")

    print("=== 漏网文件形态归纳（Top 30）===")
    for shape, c in shapes.most_common(30):
        print(f"{c:5}  {shape[:95]}")

    print(f"\n=== 全部漏网文件名（最多 120 个）===")
    for name in missed[:120]:
        print(f"  {name[:110]}")

    print(f"\n=== 有 EXIF 块但没拍到拍摄时间的（这些不该靠文件名救）===")
    for p in files:
        info = extract_sync(p)
        _, s = resolve_taken_at(info, p.name, p.stat().st_mtime)
        if s == "mtime" and info.has_exif:
            print(f"  {p.name[:90]}  cam={info.camera}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
