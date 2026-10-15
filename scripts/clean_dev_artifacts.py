"""清理开发期在**真实数据**里留下的测试痕迹。

## 背景

开发期的测试与排查脚本曾经直接连到真实的 `data/photobridge.db`
（详见 `backend/tests/test_isolation.py` 记录的事故），于是真实库里
混进了一批本不该存在的记录：

* `devices` 表 47 行 —— 只有 3 行是用户真的手机（`PhotoBridge iPhone`），
  其余 40 行是 `Python-urllib/3.12`（端到端测试）、3 行 `curl/8.22.0`
  （子代理排查）、1 行 `PhotoBridge Test`。这些"幽灵设备"会出现在
  电脑端的设备列表里，用户会以为自己的账号被入侵了。
* `pair_codes` 54 行 —— 测试生成的配对码。
* `upload_sessions` 3 行 —— 空的上传会话。
* `E:/software/相册/.photobridge_tmp/` 下的 0 字节 `.part` 文件。

## 判据

**只删明确可识别的测试痕迹**，靠 `devices.name` 里的 User-Agent 特征：

    保留：PhotoBridge iPhone（用户真机）、以及任何不像测试客户端的名字
    删除：Python-urllib*、curl/*、PhotoBridge Test、httpx/*、pytest*

宁可不删也不误删：用户的真机名字是他在配对时手机浏览器报上来的
User-Agent 截断，通常是 `Mozilla/5.0 (Linux; Android 13; ...)` 这类，
不会长得像 urllib/curl。

## 用法

    .venv/Scripts/python.exe scripts/clean_dev_artifacts.py          # 演练
    .venv/Scripts/python.exe scripts/clean_dev_artifacts.py --apply  # 执行
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from backend.app.config import get as get_config      # noqa: E402

#: 明显是测试客户端上报的名字（大小写不敏感的子串匹配）。
_TEST_NAME_PATTERNS = (
    "python-urllib", "python-requests", "urllib", "curl", "wget",
    "httpx", "httpcore", "pytest", "aiohttp", "node-fetch",
    "photobridge test", "photobridge-test", "test client", "testclient",
    # "PhotoBridge iPhone" 也是测试造的：用户的手机是**安卓**，
    # 而且真机上报的是浏览器 User-Agent（`Mozilla/5.0 (Linux; Android …)`），
    # 不会是一个自造的名字。这几行来自前端子代理模拟配对流程。
    "photobridge iphone",
)
_TEST_NAME_RE = re.compile("|".join(re.escape(p) for p in _TEST_NAME_PATTERNS),
                           re.I)


def _is_test_name(name: str | None) -> bool:
    return bool(name) and bool(_TEST_NAME_RE.search(name))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="清理真实数据里的开发期测试痕迹")
    ap.add_argument("--apply", action="store_true", help="真的删（默认只演练）")
    args = ap.parse_args(argv)

    cfg = get_config()
    db_path = Path(cfg.db_path)
    if not db_path.is_file():
        print(f"数据库不存在：{db_path}")
        return 2

    print(f"数据库：{db_path}")
    print(f"模式：{'执行' if args.apply else '演练（不删任何东西）'}")
    print()

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        # --- devices -------------------------------------------------------
        rows = list(con.execute("SELECT id, name, created_at FROM devices"))
        doomed = [r for r in rows if _is_test_name(r["name"])]
        keep = [r for r in rows if not _is_test_name(r["name"])]

        print(f"devices：共 {len(rows)} 行")
        print(f"  保留 {len(keep)} 行（用户真机）：")
        for r in keep[:10]:
            print(f"    · {r['name']}")
        if len(keep) > 10:
            print(f"    … 还有 {len(keep) - 10} 行")
        print(f"  删除 {len(doomed)} 行（测试客户端）：")
        by_name: dict[str, int] = {}
        for r in doomed:
            by_name[r["name"]] = by_name.get(r["name"], 0) + 1
        for name, cnt in sorted(by_name.items(), key=lambda kv: -kv[1]):
            print(f"    · {cnt:3d} x {name}")
        print()

        # --- pair_codes / upload_sessions ----------------------------------
        codes = con.execute("SELECT COUNT(*) FROM pair_codes").fetchone()[0]
        sess = con.execute(
            "SELECT COUNT(*) FROM upload_sessions WHERE status = 'open'"
        ).fetchone()[0]
        print(f"pair_codes：{codes} 行（全是开发期生成的，配对码 5 分钟就过期）")
        print(f"upload_sessions（open）：{sess} 行（空会话）")
        print()

        # --- 临时区 --------------------------------------------------------
        tmp_dirs = []
        for root in cfg.roots:
            d = Path(root.path) / ".photobridge_tmp"
            if d.is_dir():
                parts = list(d.glob("*.part"))
                tmp_dirs.append((d, parts))
                total = sum(p.stat().st_size for p in parts if p.is_file())
                print(f"临时区 {d}：{len(parts)} 个 .part，共 {total} 字节")
        print()

        if not args.apply:
            print("演练结束，未改动任何数据。加 --apply 真的执行。")
            return 0

        # --- 备份 ----------------------------------------------------------
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup = db_path.with_name(f"{db_path.name}.bak_{stamp}")
        shutil.copy2(db_path, backup)
        print(f"已备份到：{backup}")

        con.execute("BEGIN IMMEDIATE")
        if doomed:
            ids = [r["id"] for r in doomed]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join("?" * len(chunk))
                con.execute(f"DELETE FROM devices WHERE id IN ({marks})", chunk)
        # 配对码全是开发期的（一期还没交付给用户），清空不影响任何人
        con.execute("DELETE FROM pair_codes")
        con.execute("DELETE FROM upload_sessions WHERE status = 'open'")
        con.commit()

        after = con.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        print()
        print(f"devices 清理后：{after} 行")
        print(f"pair_codes 已清空；open 会话已删除")

        # --- 临时文件 ------------------------------------------------------
        removed = 0
        for d, parts in tmp_dirs:
            for p in parts:
                try:
                    p.unlink()
                    removed += 1
                except OSError as e:      # noqa: BLE001
                    print(f"  删不掉 {p.name}: {e}")
        if removed:
            print(f"临时区清理：删除 {removed} 个 .part")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    raise SystemExit(main())
