"""审计日志测试（`backend/app/audit.py`）。

这些用例的存在理由是三份互不一致的实现合并成一份之前，
真实库里已经发生的**静默**缺陷：

1. `audit_log` 表 56 行**全是 `pair`** —— 因为只有 `handlers/auth.py`
   那份实现写表。`delete` 与 `rescan` 只落了文件、从未进过索引。
   而"跳过写表的代码路径"当时没有任何测试会失败。
2. 只有 `uploads.py` 那份实现做 `fsync`。另两份的 docstring 写着
   "即使进程被杀也已落盘"，代码里却只有 `f.write()`。
3. `pair` 记录把 `client_id` 写进了 `rel_path` 列。
4. `audit_log.client_id` 永远是 NULL，而 `handlers/auth.py` 的注释
   却在解释"为什么不能删行以免 client_id 悬空"。

所以这里的重点不是"能写进去"，而是**三条记录路径行为一致**、
**文件里有的表里也有**、以及**该 fsync 的地方真的 fsync 了**。

⚠️ 关于 fsync 的实测更正：我一开始以为"不 fsync 则进程被强杀会丢记录"，
实测**不成立** —— `with open(..., "a")` 退出时会关闭文件并把字节交给内核，
`os._exit(7)` 之后记录仍然在。fsync 真正防的是**断电/系统崩溃**。
所以"确实调了 fsync"只能用 monkeypatch 断言，不能靠杀进程证明；
`test_record_durable_across_abrupt_exit` 的定位是行为回归，不是 fsync 的证据。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from backend.app import audit
from backend.app.config import Config
from backend.app.db import Database

# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    # exist_ok=True：`db` 夹具可能已经先建过这个目录
    # （`Database.init()` 会创建父目录），两个夹具共用 tmp_path。
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    lib = tmp_path / "lib"
    lib.mkdir(exist_ok=True)
    return Config(
        {"roots": [{"id": "library", "path": str(lib), "mode": "readwrite"}]},
        path=data / "config.json", data_dir=data,
    )


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    d = Database(tmp_path / "data" / "audit.db")
    d.init()
    return d


def _lines(cfg: Config) -> list[str]:
    return [ln for ln in cfg.audit_log_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def _rows(db: Database) -> list[dict]:
    return db.query("SELECT * FROM audit_log ORDER BY id")


@pytest.fixture(autouse=True)
def _reset_index_warn_state() -> None:
    """索引告警节流是模块级状态，用例之间必须隔离。"""
    audit._index_fail_streak = 0
    audit._index_fail_since = 0.0
    audit._index_last_warn = 0.0


# --------------------------------------------------------------------------
# 与 schema 的一致性
# --------------------------------------------------------------------------


def test_actions_match_schema_check_constraint(db: Database) -> None:
    """`ACTIONS` 必须与 `audit_log.action` 的 CHECK 约束逐字一致。

    ⚠️ 这是本模块最容易出错也最难发现的地方：往表里写一个 CHECK
    不接受的动作会抛 IntegrityError，而**写表失败是被刻意容忍的**
    （只记 DEBUG），于是那条记录静默消失 —— 文件里有、表里没有，
    与合并前的缺陷一模一样。直接读 `sqlite_master` 比对。
    """
    sql = db.scalar(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'")
    assert sql, "audit_log 表不存在"

    start = sql.index("action", sql.index("CHECK"))
    frag = sql[start:sql.index(")", sql.index("IN (", start))]
    in_clause = frag[frag.index("IN (") + 4:]
    declared = {p.strip().strip("'") for p in in_clause.split(",")}

    assert declared == set(audit.ACTIONS), (
        f"schema 声明 {sorted(declared)}，"
        f"audit.ACTIONS 是 {sorted(audit.ACTIONS)}"
    )


def test_every_action_is_recordable(db: Database, cfg: Config) -> None:
    """每一个声明的动作都能同时写进文件与表（防 CHECK 漏配）。"""
    for action in sorted(audit.ACTIONS):
        audit.record(db, cfg, action, rel_path=f"x/{action}.jpg", device="PC")
    assert {r["action"] for r in _rows(db)} == set(audit.ACTIONS)
    assert len(_lines(cfg)) == len(audit.ACTIONS)


# --------------------------------------------------------------------------
# 文件与表的一致性（合并前最严重的缺陷）
# --------------------------------------------------------------------------


def test_delete_writes_both_file_and_table(db: Database, cfg: Config) -> None:
    """★ 回归：`delete` 必须同时进文件与表。

    合并前 `handlers/browse.py` 那份实现**只写文件**，
    于是真实库里 4915 张照片的删除操作在索引表里一条都没有。
    """
    audit.record(db, cfg, audit.ACTION_DELETE, rel_path="a/b.jpg", device="PC",
                 detail={"media_id": 7})

    assert len(_lines(cfg)) == 1
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0]["action"] == "delete"
    assert rows[0]["rel_path"] == "a/b.jpg"


def test_rescan_writes_both_file_and_table(db: Database, cfg: Config) -> None:
    """★ 回归：`rescan` 同样曾经只落文件不落表。"""
    audit.record(db, cfg, audit.ACTION_RESCAN, rel_path="*", device="PC",
                 detail={"root": "*"})
    assert len(_lines(cfg)) == 1
    assert [r["action"] for r in _rows(db)] == ["rescan"]


def test_upload_writes_both_file_and_table(db: Database, cfg: Config) -> None:
    """`upload` 在合并前只写文件（第三份实现也只调了一次表——但设备名写空）。"""
    audit.record(db, cfg, audit.ACTION_UPLOAD, rel_path="2026/2026-10/x.jpg",
                 device="PC", detail={"size": 1234})
    assert len(_lines(cfg)) == 1
    assert [r["action"] for r in _rows(db)] == ["upload"]


def test_file_line_count_matches_table_row_count(db: Database, cfg: Config) -> None:
    """多动作混合写入后，文件行数与表行数必须相等。

    单条用例只覆盖一种动作；这条覆盖"某些动作写表、另一些不写"
    这类分支不一致 —— 正是合并前的形态。
    """
    for i, action in enumerate(
            [audit.ACTION_PAIR, audit.ACTION_UPLOAD, audit.ACTION_DELETE,
             audit.ACTION_RESCAN, audit.ACTION_REVOKE]):
        audit.record(db, cfg, action, rel_path=f"p{i}", device="PC")

    assert len(_lines(cfg)) == 5
    assert db.scalar("SELECT COUNT(*) FROM audit_log") == 5


# --------------------------------------------------------------------------
# fsync（合并前只有一份实现做）
# --------------------------------------------------------------------------


def test_record_actually_fsyncs_to_disk(db: Database, cfg: Config,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 回归：`record()` 必须真的调 `os.fsync`。

    合并前只有 `uploads.py` 那份实现做 fsync，而 `handlers/auth.py`
    与 `handlers/browse.py` 的 docstring 都写着"追加写，即使进程被杀
    也已落盘"，代码里却只有 `f.write()`。

    ⚠️ 这条**只能**用 `monkeypatch` 验证，不能用"杀进程后记录还在不在"
    来验证 —— 我实测过：`with open(...).write()` 在进程被强杀时
    记录**确实幸存**（with 退出时关闭文件，字节已交给内核）。
    fsync 真正防的是**断电/系统崩溃**，而那没法在单测里模拟。
    所以这里直接断言调用发生。
    """
    calls: list[int] = []
    real_fsync = os.fsync

    def _spy(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(audit.os, "fsync", _spy)
    audit.record(db, cfg, audit.ACTION_DELETE, rel_path="a.jpg", device="PC")
    assert calls, "record() 没有调用 os.fsync —— 断电时记录可能还在页缓存里"


def test_record_durable_across_abrupt_exit(db: Database, cfg: Config,
                                           tmp_path: Path) -> None:
    """★ `record()` 之后进程不走正常收尾流程，记录仍在。

    子进程用 `os._exit(7)` 而不是 `sys.exit()`：`os._exit` **跳过所有
    stdio 缓冲刷新**且跨平台语义一致（`os.kill(pid, 9)` 在 Windows 上
    其实是 `TerminateProcess`，退出码是 9 而不是 -9，写跨平台断言
    很容易写错）。

    ⚠️ **这条用例证明不了"必须 fsync"** —— 我实测过：合并前那两份
    用 `with open(..., "a").write()` 的实现在这条用例下**也是通过的**，
    因为 `with` 退出时关闭文件，把 Python 缓冲区交给了内核。
    fsync 防的是**断电/系统崩溃**，而那是无法在单测里模拟的。

    所以这条用例的定位是"行为回归"（不依赖 `with` 的关闭时机、
    也不会因为将来有人把文件对象一直开着而静默丢记录），
    而"确实调了 fsync"由上面那条 `monkeypatch` 用例保证。
    两者缺一不可，理由不同。
    """
    script = textwrap.dedent(f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
        from backend.app import audit
        from backend.app.config import Config
        from backend.app.db import Database

        cfg = Config(
            {{"roots": []}},
            path=Path({str(cfg.audit_log_path.parent / "config.json")!r}),
            data_dir=Path({str(cfg.audit_log_path.parent)!r}),
        )
        db = Database(Path({str(db.path)!r}))
        db.init()
        audit.record(db, cfg, audit.ACTION_DELETE, rel_path="killme.jpg",
                     device="PC", detail={{"n": 1}})
        # 不跑任何收尾流程 —— 立刻退出
        import os
        os._exit(7)
    """)
    script_path = tmp_path / "writer.py"
    script_path.write_text(script, encoding="utf-8")

    r = subprocess.run([sys.executable, str(script_path)],
                       capture_output=True, timeout=60)
    assert r.returncode == 7, (
        f"子进程应以 os._exit(7) 退出，实际 returncode={r.returncode}；"
        f"stderr={r.stderr.decode('utf-8', 'replace')[:400]}"
    )

    lines = _lines(cfg)
    assert any("killme.jpg" in ln for ln in lines), (
        "进程未走正常收尾流程时审计记录丢了"
    )


# --------------------------------------------------------------------------
# 列语义（合并前 pair 把 client_id 塞进了路径列）
# --------------------------------------------------------------------------


def test_pair_puts_client_id_in_column_not_path(db: Database, cfg: Config) -> None:
    """★ 回归：合并前 `_audit(st, "pair", client_id, ...)` 把 32 位
    client_id 塞进了 `rel_path` 列，于是"路径"列里是一个哈希串。"""
    cid = "abcdef0123456789abcdef0123456789"
    audit.record(db, cfg, audit.ACTION_PAIR, device="PC", client_id=cid,
                 detail={"device_name": "Pixel 8"})

    row = _rows(db)[0]
    assert row["client_id"] == cid
    assert row["rel_path"] == "", f"rel_path 不该被 client_id 占用：{row['rel_path']!r}"

    # 文件是五列，没有 client_id 列 —— 它必须出现在 detail 里，
    # 否则"主记录"反而丢了这个关联键。
    detail = json.loads(_lines(cfg)[0].split("\t")[4])
    assert detail["client_id"] == cid


def test_client_id_defaults_to_null_not_empty_string(db: Database, cfg: Config) -> None:
    """没有 client_id 时应当是 NULL 而不是空串。

    `revoke` 之后客户端可能已被删，但 `audit_log.client_id` 的语义是
    "谁干的"。空串会被误读成"一个 id 为空的客户端"。
    """
    audit.record(db, cfg, audit.ACTION_DELETE, rel_path="a.jpg", device="PC")
    assert _rows(db)[0]["client_id"] is None


def test_device_name_is_recorded(db: Database, cfg: Config) -> None:
    """★ 回归：合并前 `uploads._audit` 把设备名列写成空串。"""
    audit.record(db, cfg, audit.ACTION_UPLOAD, rel_path="a.jpg", device="我的电脑")
    assert _rows(db)[0]["device_name"] == "我的电脑"
    assert _lines(cfg)[0].split("\t")[2] == "我的电脑"


def test_file_and_table_carry_same_device(db: Database, cfg: Config) -> None:
    """文件与表对同一次操作的"设备"描述必须一致。

    合并前三份实现在这一列上分别写设备名 / 空串 —— 于是
    "哪个进程干的"取决于这条记录走的是哪条代码路径。
    """
    audit.record(db, cfg, audit.ACTION_DELETE, rel_path="a.jpg", device="PC-1")
    assert _rows(db)[0]["device_name"] == _lines(cfg)[0].split("\t")[2] == "PC-1"


# --------------------------------------------------------------------------
# 行格式：TAB 五列，不能被字段内容破坏
# --------------------------------------------------------------------------


def test_format_line_has_five_tab_separated_columns(cfg: Config) -> None:
    line = audit.format_line(audit.ACTION_DELETE, rel_path="a/b.jpg",
                             device="PC", detail={"media_id": 1})
    assert line.count("\t") == 4
    assert len(line.split("\t")) == 5


def test_tab_in_device_name_does_not_break_columns(db: Database,
                                                   cfg: Config) -> None:
    """★ 设备名来自手机上报的 User-Agent，客户端可设成任何字符串。

    含制表符时若不处理，一行会被拆成六列，**整份日志从此错位** ——
    之后每一行的列含义都不再可信。
    """
    audit.record(db, cfg, audit.ACTION_PAIR, device="evil\tname\nwith\tbreaks",
                 client_id="x" * 32)

    lines = _lines(cfg)
    assert len(lines) == 1, "换行把一条记录拆成了多行"
    assert lines[0].count("\t") == 4, "制表符把一行撑成了更多列"
    assert audit.parse_line(lines[0])["action"] == "pair"


def test_safe_field_replaces_all_line_breakers() -> None:
    assert audit.safe_field("a\tb") == "a b"
    assert audit.safe_field("a\nb") == "a b"
    assert audit.safe_field("a\r\nb") == "a  b"   # \r 与 \n 各换一次
    assert audit.safe_field(None) == ""
    assert audit.safe_field(123) == "123"


def test_timestamp_is_iso8601_with_offset(cfg: Config) -> None:
    """时间格式是 ISO8601 带时区偏移（HTTP JSON 用 Unix 时间戳，
    日志用 ISO8601 —— 两套并存是设计约定）。"""
    ts = audit.format_line(audit.ACTION_PAIR).split("\t")[0]
    assert len(ts) == 24, ts
    assert ts[4] == "-" and ts[7] == "-" and ts[10] == "T"
    assert ts[19] in "+-", f"缺时区偏移：{ts}"


# --------------------------------------------------------------------------
# 失败不能影响业务
# --------------------------------------------------------------------------


def test_missing_db_only_writes_file(cfg: Config) -> None:
    """`db=None` 时只写文件（启动早期没有库的路径）。

    文件是主记录，它必须尽量写成。
    """
    audit.record(None, cfg, audit.ACTION_DELETE, rel_path="a.jpg", device="PC")
    assert len(_lines(cfg)) == 1


def test_table_failure_is_tolerated(cfg: Config) -> None:
    """写表失败只记**限频 WARNING**，不抛 —— 为了写审计而让用户的删除失败是荒谬的。"""
    class _BrokenDb:
        path = Path("broken.db")

        def execute(self, *a, **kw):
            raise sqlite3.OperationalError("database is locked")

    audit.record(_BrokenDb(), cfg, audit.ACTION_DELETE,   # type: ignore[arg-type]
                 rel_path="a.jpg", device="PC")
    assert len(_lines(cfg)) == 1, "表写失败不该影响文件（主记录）"


class _BrokenDb:
    """永远写表失败的最小替身（用于告警/节流用例）。"""

    path = Path("broken.db")

    def execute(self, *a, **kw):
        raise sqlite3.OperationalError("database is locked")


def test_table_failure_warns_at_warning_level(cfg: Config,
                                              caplog: pytest.LogCaptureFixture) -> None:
    """索引失败必须能被**生产日志级别**看见（原来只记 DEBUG ≈ 静默）。"""
    with caplog.at_level(logging.WARNING, logger="backend.app.audit"):
        audit.record(_BrokenDb(), cfg, audit.ACTION_DELETE,   # type: ignore[arg-type]
                     rel_path="a.jpg", device="PC")
    hits = [r for r in caplog.records if "写审计索引表失败" in r.getMessage()]
    assert len(hits) == 1
    msg = hits[0].getMessage()
    assert "连续 1 次" in msg
    assert "audit.log" in msg, "要告诉运维文件主记录仍在、索引可重建"
    assert "OperationalError" in msg, "失败类别要有，用来区分锁冲突与磁盘故障"
    # 不许把内部细节（SQL、路径、令牌）写进日志
    assert "INSERT INTO" not in msg
    assert "pbc1." not in msg


def test_table_failure_warning_is_throttled(cfg: Config,
                                            caplog: pytest.LogCaptureFixture) -> None:
    """连续失败只告警一次，否则每条业务操作都会刷一行 WARNING。"""
    with caplog.at_level(logging.WARNING, logger="backend.app.audit"):
        for i in range(5):
            audit.record(_BrokenDb(), cfg, audit.ACTION_DELETE,   # type: ignore[arg-type]
                         rel_path=f"{i}.jpg", device="PC")
    hits = [r for r in caplog.records if "写审计索引表失败" in r.getMessage()]
    assert len(hits) == 1
    assert audit._index_fail_streak == 5


def test_table_failure_rewarns_after_window(cfg: Config,
                                            caplog: pytest.LogCaptureFixture,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """节流窗口过后再告警一次（长时间故障不能被首条日志掩埋）。"""
    monkeypatch.setattr(audit, "_INDEX_WARN_EVERY", 0.0)
    with caplog.at_level(logging.WARNING, logger="backend.app.audit"):
        for i in range(3):
            audit.record(_BrokenDb(), cfg, audit.ACTION_DELETE,   # type: ignore[arg-type]
                         rel_path=f"{i}.jpg", device="PC")
    hits = [r for r in caplog.records if "写审计索引表失败" in r.getMessage()]
    assert len(hits) == 3


def test_index_recovery_is_reported_once(db: Database, cfg: Config,
                                         caplog: pytest.LogCaptureFixture) -> None:
    """恢复后补一条提示并清零；没有失败时不刷恢复提示。"""
    audit.record(_BrokenDb(), cfg, audit.ACTION_DELETE,   # type: ignore[arg-type]
                 rel_path="a.jpg", device="PC")
    with caplog.at_level(logging.WARNING, logger="backend.app.audit"):
        audit.record(db, cfg, audit.ACTION_DELETE, rel_path="b.jpg", device="PC")
        audit.record(db, cfg, audit.ACTION_DELETE, rel_path="c.jpg", device="PC")
    recovered = [r for r in caplog.records if "已恢复" in r.getMessage()]
    assert len(recovered) == 1, "只在恢复那一刻提示一次"
    assert audit._index_fail_streak == 0
    assert db.scalar("SELECT COUNT(*) FROM audit_log") == 2


def test_file_failure_is_tolerated(db: Database, cfg: Config,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """文件写不了（磁盘满/权限）也不能抛。"""
    def _boom(*a, **kw):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(audit.os, "open", _boom)
    audit.record(db, cfg, audit.ACTION_DELETE, rel_path="a.jpg", device="PC")
    assert db.scalar("SELECT COUNT(*) FROM audit_log") == 1


def test_unknown_action_still_writes_file(db: Database, cfg: Config) -> None:
    """未声明的动作仍落文件（主记录优先），但并不静默 —— 记 WARNING。

    写进表必然被 CHECK 拒绝然后被吞掉，那条记录就"一半有一半没有"。
    点名出来是因为这属于代码 bug，不是运行时状况。
    """
    audit.record(db, cfg, "not_a_real_action", rel_path="x", device="PC")
    assert len(_lines(cfg)) == 1
    assert db.scalar("SELECT COUNT(*) FROM audit_log") == 0


# --------------------------------------------------------------------------
# 读取
# --------------------------------------------------------------------------


def test_read_entries_returns_newest_first(cfg: Config) -> None:
    for i in range(3):
        audit.record(None, cfg, audit.ACTION_DELETE, rel_path=f"p{i}.jpg",
                     device="PC", detail={"i": i})
    entries, truncated = audit.read_entries(cfg)
    assert not truncated
    assert [e["path"] for e in entries] == ["p2.jpg", "p1.jpg", "p0.jpg"]


def test_read_entries_missing_file_is_empty(cfg: Config) -> None:
    """全新安装第一次点开面板就是这种情况（返回空而不是 500）。"""
    assert audit.read_entries(cfg) == ([], False)


def test_read_entries_respects_limit_and_sets_truncated(cfg: Config) -> None:
    for i in range(10):
        audit.record(None, cfg, audit.ACTION_DELETE, rel_path=f"p{i}.jpg")
    entries, truncated = audit.read_entries(cfg, limit=3)
    assert len(entries) == 3
    assert truncated
    assert entries[0]["path"] == "p9.jpg"


def test_read_entries_survives_truncated_utf8(cfg: Config) -> None:
    """★ 从中间截断会切断多字节汉字，直接 decode 会抛
    UnicodeDecodeError 让整个面板 500。"""
    audit.record(None, cfg, audit.ACTION_DELETE, rel_path="青甘环线/青海湖.jpg",
                 device="电脑")
    # 手工塞一条很长的记录，把文件撑到超过尾读窗口
    for i in range(200):
        audit.record(None, cfg, audit.ACTION_DELETE, rel_path=f"中文目录/照片{i}.jpg",
                     device="我的电脑")
    entries, _ = audit.read_entries(cfg, limit=5)
    assert len(entries) == 5


def test_read_entries_pads_short_lines(cfg: Config) -> None:
    """列数不足时按位补空串而不是丢行 ——
    "日志里少了一条"与"这条从来没发生"必须能区分。"""
    path = cfg.audit_log_path
    path.write_text("2026-10-16T09:00:00+0800\tdelete\n", encoding="utf-8")
    entries, _ = audit.read_entries(cfg)
    assert len(entries) == 1
    assert entries[0]["action"] == "delete"
    assert entries[0]["path"] == "" and entries[0]["device"] == ""


def test_parse_line_roundtrips_format_line() -> None:
    line = audit.format_line(audit.ACTION_MOVE, rel_path="a/b.jpg",
                             device="PC", detail={"from": "a", "to": "b"})
    parsed = audit.parse_line(line)
    assert parsed["action"] == "move"
    assert parsed["path"] == "a/b.jpg"
    assert json.loads(parsed["detail"]) == {"from": "a", "to": "b"}


# --------------------------------------------------------------------------
# 并发
# --------------------------------------------------------------------------


def test_concurrent_records_do_not_interleave(cfg: Config) -> None:
    """多线程写入时每一行必须是完整的五列。

    handlers 在事件循环上跑，`uploads.complete_session` 在线程池里跑，
    两条路径都会调 `record()`。Windows 上 `O_APPEND` 的原子性弱于 POSIX，
    所以模块内显式串行化。
    """
    import concurrent.futures

    def _one(i: int) -> None:
        audit.record(None, cfg, audit.ACTION_UPLOAD, rel_path=f"dir{i}/photo{i}.jpg",
                     device=f"dev{i}", detail={"i": i})

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(_one, range(60)))

    lines = _lines(cfg)
    assert len(lines) == 60
    for ln in lines:
        assert ln.count("\t") == 4, f"行被写坏了：{ln!r}"
    assert sorted(audit.parse_line(ln)["action"] for ln in lines) == ["upload"] * 60
