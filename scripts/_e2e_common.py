"""e2e 脚本共用的小工具。

只放「多个 verify_*.py 都要用、且必须保持同一份语义」的东西 —— 目前是真实库指纹。
脚本按文件运行（`python scripts/xxx.py`），`scripts/` 天然在 `sys.path[0]`，
所以 `from _e2e_common import ...` 不需要额外改路径。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def real_db_fingerprint(db: Path) -> str:
    """真实照片库的数据库指纹：`rows=… missing=… maxid=…`。

    跑 E2E 前后各取一次、要求字符串相等 —— 这是「不碰真实主库」的安全边界，不是装饰。
    三条都是被测动作不应改变的量：行数、失效标记数、最大 id。
    库不存在时回 `absent`（临时环境/新机器），调用方直接按字符串比即可。
    """
    if not db.exists():
        return "absent"
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT COUNT(*) FROM media").fetchone()[0]
        miss = con.execute("SELECT COUNT(*) FROM media WHERE missing = 1").fetchone()[0]
        mx = con.execute("SELECT MAX(id) FROM media").fetchone()[0]
        return f"rows={rows} missing={miss} maxid={mx}"
    finally:
        con.close()
