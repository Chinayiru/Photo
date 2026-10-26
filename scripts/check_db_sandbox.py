"""验证 sandbox 能挡住 junction 逃逸 + db 层可用。

为什么单独跑 junction：`os.path.islink()` 对 Windows junction 返回 False
（实测），所以任何用 islink 做重解析点检测的实现都会**静默放行**越界。
这个脚本用真实 `mklink /J` 造一个指向库外的联接，再尝试穿越。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.config import RootConfig  # noqa: E402
from app.db import Database  # noqa: E402
from app.sandbox import SandboxError, resolve  # noqa: E402

fails = 0


def check(ok: bool, label: str, extra: str = "") -> None:
    global fails
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}{'  ' + extra if extra else ''}")


# ==========================================================================
# 1. junction 逃逸
# ==========================================================================
print("=== junction / symlink 逃逸 ===")
tmp = Path(tempfile.mkdtemp(prefix="pbsbx_"))
lib = tmp / "library"
outside = tmp / "OUTSIDE"
(lib / "sub").mkdir(parents=True)
outside.mkdir()
(outside / "secret.jpg").write_bytes(b"SECRET")
(lib / "a.jpg").write_bytes(b"a")

junc = lib / "junc"
r = subprocess.run(["cmd", "/c", "mklink", "/J", str(junc), str(outside)],
                   capture_output=True, text=True)
made = r.returncode == 0 and junc.exists()
print(f"     mklink /J rc={r.returncode} exists={junc.exists()}")

if made:
    # 先确认这确实是"islink 漏检"的场景
    st = os.lstat(junc)
    attrs = getattr(st, "st_file_attributes", 0)
    print(f"     islink={os.path.islink(junc)}  st_file_attributes=0x{attrs:x}  reparse={bool(attrs & 0x400)}")
    check(os.path.islink(junc) is False, "前提成立：junction 的 islink() == False")
    check(bool(attrs & 0x400), "前提成立：st_file_attributes 含 0x400")

    ROOT = RootConfig(id="library", path=lib, mode="readwrite", recursive=True)
    for rel in ("junc/secret.jpg", "junc", "sub/../junc/secret.jpg"):
        try:
            p = resolve(ROOT, rel, "read")
            check(False, f"junction 穿越被拦: {rel}", f"但放行到 {p}")
        except SandboxError as e:
            check(e.rule in ("reparse_point", "parent_traversal"),
                  f"junction 穿越被拦: {rel}", f"rule={e.rule}")

    # 反证：不加检查时确实能读到库外文件
    naive = (lib / "junc" / "secret.jpg").resolve()
    check(naive.read_bytes() == b"SECRET",
          "反证：朴素拼接确实读到了库外文件（所以逐段检查不可省）")

    # 目录联接指向库内也拒绝（规则简单一致，不留"指向库内就放行"的歧义）
    inner = tmp / "inner"
    inner.mkdir()
    junc2 = lib / "junc_inner"
    r2 = subprocess.run(["cmd", "/c", "mklink", "/J", str(junc2), str(inner)],
                        capture_output=True, text=True)
    if r2.returncode == 0:
        try:
            resolve(ROOT, "junc_inner/x.jpg", "read")
            check(False, "指向库内的 junction 也被拒")
        except SandboxError as e:
            check(e.rule == "reparse_point", "指向库内的 junction 也被拒", f"rule={e.rule}")
else:
    print("     SKIP: 无法创建 junction（权限不足）")

# ==========================================================================
# 2. 数据库层
# ==========================================================================
print("\n=== 数据库层 ===")
dbdir = Path(__file__).resolve().parent.parent / "data" / "_db_test"
dbdir.mkdir(parents=True, exist_ok=True)
dbp = dbdir / "t.db"
for s in ("", "-wal", "-shm"):
    try:
        Path(str(dbp) + s).unlink()
    except OSError:
        pass

db = Database(dbp)
db.init()
# 迁移账本：跑完所有迁移后，当前版本必须等于账本最后一版（docs/05 §7.1）。
from app.db import MIGRATIONS, SCHEMA_VERSION
check(db.schema_version() == SCHEMA_VERSION,
      f"schema_version = {SCHEMA_VERSION}（账本 {len(MIGRATIONS)} 版）",
      f"got={db.schema_version()}")
check(db.initialized, "initialized 标志")

n = db.scalar("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
check(n >= 10, f"建表数量 >= 10", f"got={n}")

# PRAGMA 是连接级：每条新连接都必须重设
for name, want in (("journal_mode", "wal"), ("busy_timeout", "30000"), ("foreign_keys", "1")):
    v = db.scalar(f"PRAGMA {name}")
    check(str(v).lower() == want, f"PRAGMA {name} = {want}", f"got={v}")

# media 插入 + 触发器同步 FTS
now = time.time()
mid = db.insert_returning_id(
    "INSERT INTO media(root_id, rel_path, file_name, ext, kind, size, mtime, taken_at, taken_src,"
    " width, height, camera, thumb_state, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    ("library", "2024/2024-05/a.jpg", "a.jpg", "jpg", "image", 1024, now, now, "exif",
     4000, 3000, "Xiaomi 13", "pending", now),
)
check(mid == 1, "插入返回 id=1", f"got={mid}")
# 注意：tokenize 里把 '.' 声明为 tokenchar，所以 "a.jpg" 是**一个** token。
# 直接写 MATCH 'a.jpg' 会被 FTS5 当成查询语法解析而报 "syntax error near ."，
# 必须写成带引号的短语 MATCH '"a.jpg"'。前端构造搜索词时必须做同样的转义。
fts = db.scalar('SELECT COUNT(*) FROM media_fts WHERE media_fts MATCH \'"a.jpg"\'')
check(fts == 1, "FTS 短语匹配 a.jpg", f"got={fts}")
# 前缀查询（用户在搜索框里边打边搜的场景）：
fts_pfx = db.scalar('SELECT COUNT(*) FROM media_fts WHERE media_fts MATCH \'"a.j"*\'')
check(fts_pfx == 1, "FTS 前缀查询 a.j* 命中", f"got={fts_pfx}")
# 反例（记录设计取舍）：'.' 是 tokenchar，所以按扩展名单独搜 "jpg" 搜不到。
# 这是刻意的 —— 让 IMG_1234.jpg 成为一个整体 token，前缀搜索才好用。
fts_ext = db.scalar('SELECT COUNT(*) FROM media_fts WHERE media_fts MATCH \'jpg\'')
check(fts_ext == 0, "按扩展名单独搜 jpg 不命中（'.' 是 tokenchar，已知取舍）", f"got={fts_ext}")

# 约束真的生效
for sql, label, want_exc in (
    # ISSUE-055 之前这里钉的是"拒绝 video"；放开视频后视频是合法值（审查 6.2
    # 指出这条已过时），改成钉**仍然非法**的枚举值，否则这条用例会自己造一行
    # 脏数据，把下面三条 stats 断言一起打红。
    ("INSERT INTO media(root_id,rel_path,file_name,ext,size,mtime,taken_at,kind) VALUES('l','b.jpg','b.jpg','jpg',1,1,1,'audio')", "kind 枚举拒绝非法值 audio", "IntegrityError"),
    # 注意 root_id 必须与 id=1 那一行一致（'library'）才会真的撞唯一键。
    # 写成 ('l','a.jpg') 是不会冲突的 —— 这是我第一版脚本的错。
    ("INSERT INTO media(root_id,rel_path,file_name,ext,size,mtime,taken_at) VALUES('library','2024/2024-05/a.jpg','a.jpg','jpg',1,1,1)", "UNIQUE(root_id,rel_path) 生效", "IntegrityError"),
    ("INSERT INTO media(root_id,rel_path,file_name,ext,size,mtime,taken_at,width) VALUES('l','c.jpg','c.jpg','jpg',1,1,1,-5)", "width>0 约束生效", "IntegrityError"),
    ("INSERT INTO media(root_id,rel_path,file_name,ext,size,mtime,taken_at,taken_src) VALUES('l','d.jpg','d.jpg','jpg',1,1,1,'guessed')", "taken_src 枚举约束生效", "IntegrityError"),
    ("INSERT INTO media(root_id,rel_path,file_name,ext,size,mtime,taken_at,missing) VALUES('l','e.jpg','e.jpg','jpg',1,1,1,2)", "missing 只能 0/1", "IntegrityError"),
):
    try:
        db.execute(sql)
        check(False, label, "竟然插入成功")
    except Exception as e:  # noqa: BLE001
        check(type(e).__name__ == want_exc, label, type(e).__name__)

# 正向：video 是合法值（ISSUE-055），四列可写；写完立刻删掉，不污染 stats。
vid = db.insert_returning_id(
    "INSERT INTO media(root_id, rel_path, file_name, ext, kind, size, mtime, taken_at,"
    " duration, container, vcodec, acodec, faststart, indexed_at)"
    " VALUES('library','2024/v.mp4','v.mp4','mp4','video',2048,?,?,'12.5','mp4',"
    " 'hevc','aac',1,?)",
    (now, now, now),
)
check(isinstance(vid, int) and vid > 1, "video 行可插入（kind/四列已放开）", f"id={vid}")
db.execute("DELETE FROM media WHERE id = ?", (vid,))
check(db.scalar("SELECT COUNT(*) FROM media") == 1, "删除后只剩 id=1 那一行",
      str(db.scalar("SELECT COUNT(*) FROM media")))

# 事务回滚
try:
    with db.transaction():
        db.execute("INSERT INTO media(root_id,rel_path,file_name,ext,size,mtime,taken_at) "
                   "VALUES('l','tx.jpg','tx.jpg','jpg',1,1,1)")
        raise RuntimeError("boom")
except RuntimeError:
    pass
check(db.scalar("SELECT COUNT(*) FROM media WHERE rel_path='tx.jpg'") == 0, "事务异常后回滚")

# 时间线索引真的被采用（EXPLAIN QUERY PLAN）
plan = " | ".join(r["detail"] for r in db.query(
    "EXPLAIN QUERY PLAN SELECT id FROM media WHERE missing=0 AND (taken_at, id) < (?, ?) "
    "ORDER BY taken_at DESC, id DESC LIMIT 100", (now, 999)))
check("idx_media_timeline" in plan, "时间线游标查询走 idx_media_timeline", plan)

plan2 = " | ".join(r["detail"] for r in db.query(
    "EXPLAIN QUERY PLAN SELECT id FROM media WHERE thumb_state='pending' AND missing=0 LIMIT 10"))
check("idx_media_thumbs" in plan2, "缩略图待办查询走 idx_media_thumbs", plan2)

# settings 往返（含中文与嵌套）
db.set_setting("hello", {"中文": "值", "n": [1, 2]})
check(db.get_setting("hello")["中文"] == "值", "settings JSON 往返")
check(db.get_setting("nope", "dflt") == "dflt", "settings 默认值")

# db_size 必须用 str(path)+"-wal"，不是 with_suffix
sz = db.db_size_bytes()
check(sz > 0, "db_size_bytes > 0", f"{sz}B")
# WAL 判据（审查 R-M7-02）：原来写的是 `... or True`，恒真 —— 等于没测。
# 要验的是「`db_size_bytes()` 用的是 `str(path) + '-wal'`，不是 `with_suffix`」：
# `with_suffix('.db-wal')` 在 `photobridge.db` 上会得到 `photobridge.db-wal` 之外的
# 东西（把最后一段当后缀替换），路径拼错时大小会少算一整个 WAL。
wal_path = Path(str(dbp) + "-wal")
if wal_path.exists():
    expect = dbp.stat().st_size + wal_path.stat().st_size
    check(sz >= expect, "db_size_bytes 含 WAL（路径拼接正确）",
          f"{sz}B vs 至少 {expect}B")
else:
    check(sz >= dbp.stat().st_size, "db_size_bytes 覆盖主库",
          f"{sz}B")

# 统计（前面的约束用例每次都是抛异常，不会留下脏数据；这里只应有 id=1 那一行）
st = db.stats()
check(st["media_total"] == 1, "stats.media_total", str(st["media_total"]))
check(st["thumb_pending"] == 1, "stats.thumb_pending", str(st["thumb_pending"]))
check(st["bytes_total"] == 1024, "stats.bytes_total", str(st["bytes_total"]))

# 并发写：验证 busy_timeout 真的设上了（没有它这里会立刻 SQLITE_BUSY）
import threading
errs: list[str] = []


def writer(k: int) -> None:
    d = Database(dbp)
    try:
        for i in range(30):
            d.execute("INSERT INTO media(root_id,rel_path,file_name,ext,size,mtime,taken_at) "
                      "VALUES('l',?,?,'jpg',1,1,1)", (f"t{k}_{i}.jpg", f"t{k}_{i}.jpg"))
    except Exception as e:  # noqa: BLE001
        errs.append(f"{type(e).__name__}: {e}")
    finally:
        d.close()


ts = [threading.Thread(target=writer, args=(k,)) for k in range(6)]
[t.start() for t in ts]
[t.join() for t in ts]
check(not errs, "6 线程并发写无 SQLITE_BUSY", "; ".join(errs[:2]))
check(db.scalar("SELECT COUNT(*) FROM media WHERE rel_path LIKE 't%'") == 180,
      "并发写入 180 行全部落库", str(db.scalar("SELECT COUNT(*) FROM media WHERE rel_path LIKE 't%'")))

db.close()
print(f"\n{'ALL PASS' if fails == 0 else str(fails) + ' FAILURES'}")
sys.exit(1 if fails else 0)
