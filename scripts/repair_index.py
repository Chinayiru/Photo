"""修复真实照片库索引被测试污染的问题。

## 事故经过

开发过程中，`backend/tests/test_api_contract.py` 与一个临时排查脚本
调用了 `server.build_state(cfg)` **而不传 `db=`**，于是 `build_state`
内部的 `get_db()` 回退到了**真实的** `data/photobridge.db`
（`backend/app/db.py:579` 的模块级单例，按 `data/config.json` 解析路径）。

后果有两层：

1. **合成测试行被写进真实库**：测试夹具造的 `2024/2024-05/a.jpg`、
   `青甘环线/敦煌.jpg`、`2024/000.jpg..059.jpg` 等行（id 4916..4978）
   落进了真实库。这些文件在照片库根目录下根本不存在。
2. **真实行被误标 missing=1**：测试库的目录里只有 3 个文件，
   扫描结束后 `_mark_missing(root_id='library', seen={那 3 个})`
   把真实库的 4915 行全部标成"文件已丢失"——而它们在磁盘上好好的。
   时间线只显示 missing=0 的行，于是用户看到**空的相册**。

## 修复原则

**只依据磁盘事实恢复，不依据"哪个 id 看起来像测试行"猜。**

判据：把 `media` 表每一行按 `root_id` 解析出根路径，再经**路径沙箱**
解析 `rel_path`，用 `os.stat` 确认文件真实存在且大小与库里记录一致。

* 文件存在 → `missing = 0`（恢复可见）
* 文件不存在 → `missing = 1`（合成测试行会自然落进这一类，因为
  它们的文件本来就不在磁盘上）
* 大小不一致 → 也标 `missing = 1` 并单独报告（可能是同名不同文件，
  不该直接相信）

这样做的好处是**即使事故的范围判断错了也不会造成二次伤害**：
判据是磁盘，不是我的推断。

## 用法

    # 先看要改什么，不写库
    .venv/Scripts/python.exe scripts/repair_index.py

    # 确认无误后真的改
    .venv/Scripts/python.exe scripts/repair_index.py --apply

    # 顺便删掉确认不存在的合成行（默认只标 missing，不删）
    .venv/Scripts/python.exe scripts/repair_index.py --apply --purge-synthetic
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.app import sandbox                       # noqa: E402
from backend.app.config import get as get_config      # noqa: E402


def _backup(db_path: Path) -> Path:
    """先备份。这个脚本要改用户的真实照片索引，不能没有退路。"""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dst = db_path.with_name(f"{db_path.name}.bak_{stamp}")
    shutil.copy2(db_path, dst)
    return dst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="按磁盘事实修复 media.missing 标记")
    ap.add_argument("--apply", action="store_true",
                    help="真的写库（不加这个参数只做演练）")
    ap.add_argument("--purge-synthetic", action="store_true",
                    help="删掉确认文件不存在的行（默认只标 missing=1）")
    ap.add_argument("--db", default=None, help="覆盖数据库路径")
    args = ap.parse_args(argv)

    cfg = get_config()
    db_path = Path(args.db) if args.db else cfg.db_path

    if not db_path.is_file():
        print(f"数据库不存在：{db_path}")
        return 2

    print(f"数据库：{db_path}")
    print(f"模式：{'写库' if args.apply else '演练（不写库）'}")
    print()

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        before = {r["missing"]: r["c"] for r in con.execute(
            "SELECT missing, COUNT(*) c FROM media GROUP BY missing")}
        total = con.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        print(f"修复前：共 {total} 行，missing=0 有 {before.get(0, 0)} 行，"
              f"missing=1 有 {before.get(1, 0)} 行")
        print()

        roots = {r.id: r for r in cfg.roots}

        # --- 逐行核对磁盘事实 ---------------------------------------------
        exists_ok: list[int] = []       # 文件在且大小对 → missing=0
        size_bad: list[tuple[int, str, int, int]] = []   # 大小不符
        gone: list[tuple[int, str]] = []                 # 文件不存在
        denied: list[tuple[int, str]] = []               # 沙箱拒绝

        for row in con.execute(
            "SELECT id, root_id, rel_path, size FROM media ORDER BY id"
        ):
            root = roots.get(row["root_id"])
            if root is None:
                denied.append((row["id"], "root 未在配置里"))
                continue
            try:
                p = sandbox.resolve(root, row["rel_path"], "read", cfg=cfg)
            except Exception as exc:            # noqa: BLE001
                denied.append((row["id"], f"沙箱拒绝：{type(exc).__name__}"))
                continue
            try:
                st = os.stat(p)
            except OSError:
                gone.append((row["id"], row["rel_path"]))
                continue
            if st.st_size != row["size"]:
                size_bad.append((row["id"], row["rel_path"], row["size"], st.st_size))
                continue
            exists_ok.append(row["id"])

        print(f"磁盘核对结果：")
        print(f"  ✓ 文件在且大小一致      ：{len(exists_ok)} 行 → 应恢复 missing=0")
        print(f"  ✗ 文件不存在            ：{len(gone)} 行 → 应标 missing=1")
        print(f"  ! 文件在但大小不一致    ：{len(size_bad)} 行 → 保守起见标 missing=1")
        print(f"  ? 无法判定（沙箱/配置） ：{len(denied)} 行 → 不动")
        print()

        if size_bad:
            print("大小不一致的明细（最多 10 条）：")
            for mid, rel, want, got in size_bad[:10]:
                print(f"  id={mid} {rel} 库里 {want} 字节，磁盘 {got} 字节")
            print()
        if denied:
            print("无法判定的明细（最多 10 条）：")
            for mid, why in denied[:10]:
                print(f"  id={mid} {why}")
            print()

        # 合成行：文件不存在且 rel_path 落在测试夹具用过的目录里。
        # 只用于**报告**，不用于决定改不改 —— 判据始终是磁盘。
        synthetic_hint = [
            (mid, rel) for mid, rel in gone
            if rel.startswith("2024/2024-05/") or rel == "青甘环线/敦煌.jpg"
            or (rel.startswith("2024/") and rel.endswith(".jpg"))
        ]

        # --- 写库 ---------------------------------------------------------
        if not args.apply:
            print("演练结束，未改动任何数据。加 --apply 真的执行。")
            return 0

        backup = _backup(db_path)
        print(f"已备份到：{backup}")

        con.execute("BEGIN IMMEDIATE")
        if exists_ok:
            # 分块更新，避免 SQL 变量数上限（SQLite 默认 999）
            for i in range(0, len(exists_ok), 500):
                chunk = exists_ok[i:i + 500]
                marks = ",".join("?" * len(chunk))
                con.execute(
                    f"UPDATE media SET missing = 0 WHERE id IN ({marks})", chunk)
        to_mark = [mid for mid, _ in gone] + [mid for mid, _, _, _ in size_bad]
        for i in range(0, len(to_mark), 500):
            chunk = to_mark[i:i + 500]
            marks = ",".join("?" * len(chunk))
            con.execute(
                f"UPDATE media SET missing = 1 WHERE id IN ({marks})", chunk)

        purged = 0
        if args.purge_synthetic and gone:
            ids = [mid for mid, _ in gone]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                cur = con.execute(
                    f"DELETE FROM media WHERE id IN ({marks})", chunk)
                purged += cur.rowcount
        con.commit()

        after = {r["missing"]: r["c"] for r in con.execute(
            "SELECT missing, COUNT(*) c FROM media GROUP BY missing")}
        total_after = con.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        print()
        print(f"修复后：共 {total_after} 行，missing=0 有 {after.get(0, 0)} 行，"
              f"missing=1 有 {after.get(1, 0)} 行")
        if purged:
            print(f"已删除确认不存在的合成行：{purged} 行")

        if synthetic_hint:
            print()
            print(f"其中 {len(synthetic_hint)} 行的 rel_path 形如测试夹具"
                  f"（{synthetic_hint[0][1]} 等）——")
            print("它们是开发期测试误写进来的，磁盘上从来没有这些文件。")
            if not args.purge_synthetic:
                print("如需删除请加 --purge-synthetic（不删也不影响使用，"
                      "它们 missing=1 不会出现在时间线里）。")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    raise SystemExit(main())
