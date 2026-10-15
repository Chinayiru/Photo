"""对真实库跑一次完整索引，验证 AC-2.1（4915 张 ≤10 分钟）。

只读库，写临时 DB（不碰 D:\\Photos 里的任何文件）。
跑：.venv/Scripts/python.exe scripts/probe_real_index.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "backend"))

from app.config import Config                      # noqa: E402
from app.db import Database                        # noqa: E402
from app.indexer import Indexer                    # noqa: E402

LIBRARY = Path(r"D:\Photos")


def main() -> int:
    if not LIBRARY.is_dir():
        print(f"库不存在：{LIBRARY}")
        return 1

    logging.basicConfig(level=logging.WARNING,
                        format="%(levelname)s %(name)s %(message)s")

    tmp = Path(tempfile.mkdtemp(prefix="photobridge_probe_"))
    db = Database(tmp / "probe.db")
    db.init()

    cfg = Config({
        "roots": [{
            "id": "library",
            "path": str(LIBRARY),
            "mode": "readonly",        # ★ 探针用只读，杜绝任何写库的可能
            "recursive": True,
        }],
        "index": {"batch_size": 500},
    })

    idx = Indexer(cfg, db, thumbs=None)

    print(f"库：{LIBRARY}")
    print(f"临时 DB：{tmp / 'probe.db'}")
    print("开始全量扫描（无缩略图）...\n")

    t0 = time.time()
    stats = asyncio.run(idx.scan_root(cfg.root_by_id("library")))
    wall = time.time() - t0

    print("=" * 66)
    print(stats.summary())
    print("=" * 66)

    n = db.scalar("SELECT COUNT(*) FROM media")
    print(f"\nmedia 行数        : {n}")
    print(f"挂钟耗时          : {wall:.1f}s  （AC-2.1 目标 ≤600s）")
    if n:
        print(f"每张平均          : {wall / n * 1000:.1f} ms")

    print("\n--- taken_src 分布 ---")
    for r in db.query("SELECT taken_src, COUNT(*) c FROM media GROUP BY taken_src ORDER BY c DESC"):
        print(f"  {r['taken_src']:10} {r['c']:6}  {r['c'] / n * 100:5.1f}%")

    print("\n--- 扩展名分布 ---")
    for r in db.query("SELECT ext, COUNT(*) c FROM media GROUP BY ext ORDER BY c DESC"):
        print(f"  {r['ext']:8} {r['c']:6}")

    print("\n--- 年份分布（本地时区）---")
    rows = db.query(
        "SELECT strftime('%Y', taken_at, 'unixepoch', '+8 hours') y, COUNT(*) c"
        "  FROM media GROUP BY y ORDER BY y")
    for r in rows:
        print(f"  {r['y']}  {r['c']:6}")

    print("\n--- 相机 Top 8 ---")
    for r in db.query(
        "SELECT camera, COUNT(*) c FROM media WHERE camera IS NOT NULL"
        " GROUP BY camera ORDER BY c DESC LIMIT 8"):
        print(f"  {r['c']:6}  {r['camera']}")

    print("\n--- 抽样校验（前 3 行）---")
    for r in db.query(
        "SELECT id, rel_path, ext, size, width, height, taken_src, camera"
        "  FROM media ORDER BY id LIMIT 3"):
        print(f"  #{r['id']} {r['rel_path'][:60]}")
        print(f"      {r['size']}B {r['width']}x{r['height']} {r['taken_src']} {r['camera']}")

    print("\n--- 时间线首页查询（keyset 游标，验证索引命中）---")
    plan = db.query("EXPLAIN QUERY PLAN SELECT id FROM media WHERE missing = 0"
                    " ORDER BY taken_at DESC, id DESC LIMIT 100")
    for p in plan:
        print(f"  {p['detail']}")
    t1 = time.time()
    for _ in range(50):
        db.query("SELECT id FROM media WHERE missing = 0"
                 " ORDER BY taken_at DESC, id DESC LIMIT 100")
    print(f"  50 次首页查询：{(time.time() - t1) * 1000:.1f} ms 合计")

    print("\n--- 完整性检查 ---")
    print(f"  重复 (root_id, rel_path): "
          f"{db.scalar('SELECT COUNT(*) FROM (SELECT root_id, rel_path FROM media GROUP BY 1,2 HAVING COUNT(*)>1)')}")
    print(f"  taken_at 为 NULL       : {db.scalar('SELECT COUNT(*) FROM media WHERE taken_at IS NULL')}")
    print(f"  未解析出尺寸           : {db.scalar('SELECT COUNT(*) FROM media WHERE width IS NULL')}")
    print(f"  靠 mtime 兜底           : "
          f"{db.scalar('SELECT COUNT(*) FROM media WHERE taken_src = ?', ('mtime',))}")
    print(f"  missing=1              : {db.scalar('SELECT COUNT(*) FROM media WHERE missing = 1')}")

    print(f"\n临时 DB 保留在：{tmp}")

    # ---- 第二轮扫描：验证幂等性 ----
    # 必须全部 unchanged 且缩略图状态不被重置，否则每次启动都会重生成 4915 张。
    print("\n" + "=" * 66)
    print("第二轮扫描（验证幂等 + 缩略图状态保留）")
    print("=" * 66)
    t2 = time.time()
    s2 = asyncio.run(idx.scan_root(cfg.root_by_id("library")))
    wall2 = time.time() - t2
    print(s2.summary())
    print(f"\n  耗时            : {wall2:.1f}s（首轮 {wall:.1f}s）")
    print(f"  media 行数      : {db.scalar('SELECT COUNT(*) FROM media')}"
          f"（应仍为 {n}）")
    print(f"  unchanged       : {s2.unchanged}（应为 {n}）")
    print(f"  added           : {s2.added}（应为 0）")
    print(f"  missing         : {s2.missing}（应为 0）")

    db.close()
    print(f"\n临时 DB 保留在：{tmp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
