"""`cloud/relay/cli.py` 的账号运维子命令（`docs/11` ISSUE-040）。

云端**没有注册接口**（SEC-04），第一个账号与"忘了口令"只能在服务器本机
做。这些用例把"本机能做的那几件事"钉成契约：口令走 stdin、哈希可验证、
重复/弱口令被拒、改口令顺带解锁。

不测 `getpass` 分支：那需要真实 TTY，而 CI 与服务器脚本都走
`--password-stdin`。
"""

from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

import pytest

from cloud.relay import cli, security


def _run(argv: list[str], stdin: str = "") -> int:
    """跑一次 CLI，把 stdin 换成给定文本。"""
    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        return cli.main(argv)
    finally:
        sys.stdin = old


def _add(db: Path, email: str, password: str) -> int:
    return _run(
        ["account", "add", "--db", str(db), "--email", email, "--password-stdin"],
        stdin=password + "\n",
    )


def _pass_hash(db: Path, email: str) -> str:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT pass_hash FROM accounts WHERE email = ?", (email,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def test_add_creates_an_account_whose_password_verifies(tmp_path: Path) -> None:
    db = tmp_path / "cloud.db"
    assert _add(db, "me@example.com", "correct horse battery") == 0
    stored = _pass_hash(db, "me@example.com")
    assert security.verify_password("correct horse battery", stored) is True
    assert security.verify_password("wrong password", stored) is False
    # 落库的是 scrypt 串，不是明文
    assert "correct horse battery" not in stored


def test_add_lowercases_the_email(tmp_path: Path) -> None:
    db = tmp_path / "cloud.db"
    assert _add(db, "Me@Example.COM", "correct horse battery") == 0
    _pass_hash(db, "me@example.com")  # 小写能查到 ⇒ 确实被规范化了


def test_add_refuses_a_duplicate_email(tmp_path: Path) -> None:
    db = tmp_path / "cloud.db"
    assert _add(db, "me@example.com", "correct horse battery") == 0
    with pytest.raises(SystemExit) as ei:
        _add(db, "ME@example.com", "another password 1")
    assert "已存在" in str(ei.value)


def test_add_refuses_a_short_password(tmp_path: Path) -> None:
    db = tmp_path / "cloud.db"
    with pytest.raises(SystemExit) as ei:
        _add(db, "me@example.com", "short")
    assert str(cli.MIN_PASSWORD_LEN) in str(ei.value)


def test_add_refuses_obviously_weak_passwords(tmp_path: Path) -> None:
    db = tmp_path / "cloud.db"
    for weak in ("111111111111", "123456789012"):
        with pytest.raises(SystemExit):
            _add(db, "weak@example.com", weak)


def test_add_refuses_a_bad_email(tmp_path: Path) -> None:
    db = tmp_path / "cloud.db"
    with pytest.raises(SystemExit):
        _add(db, "not-an-email", "correct horse battery")


def test_passwd_changes_the_password_and_clears_the_lock(tmp_path: Path) -> None:
    db = tmp_path / "cloud.db"
    assert _add(db, "me@example.com", "correct horse battery") == 0

    # 造一个"已被锁定"的状态（登录失败 5 次的结果，见 ISSUE-031）
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "UPDATE accounts SET failed_count = 5, locked_until = 9e9 "
            "WHERE email = ?",
            ("me@example.com",),
        )
        conn.commit()
    finally:
        conn.close()

    assert _run(
        ["account", "passwd", "--db", str(db), "--email", "me@example.com",
         "--password-stdin"],
        stdin="another secret phrase\n",
    ) == 0

    stored = _pass_hash(db, "me@example.com")
    assert security.verify_password("another secret phrase", stored) is True
    assert security.verify_password("correct horse battery", stored) is False

    conn = sqlite3.connect(str(db))
    try:
        failed, locked = conn.execute(
            "SELECT failed_count, locked_until FROM accounts WHERE email = ?",
            ("me@example.com",),
        ).fetchone()
    finally:
        conn.close()
    assert (failed, locked) == (0, None)


def test_passwd_refuses_an_unknown_account(tmp_path: Path) -> None:
    db = tmp_path / "cloud.db"
    _add(db, "me@example.com", "correct horse battery")
    with pytest.raises(SystemExit) as ei:
        _run(
            ["account", "passwd", "--db", str(db), "--email", "nobody@example.com",
             "--password-stdin"],
            stdin="another secret phrase\n",
        )
    assert "没有这个账号" in str(ei.value)


def test_list_prints_one_line_per_account(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "cloud.db"
    _add(db, "a@example.com", "correct horse battery")
    _add(db, "b@example.com", "correct horse battery")
    capsys.readouterr()
    assert _run(["account", "list", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "a@example.com" in out
    assert "b@example.com" in out


def test_list_says_so_when_there_is_no_account(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db = tmp_path / "cloud.db"
    assert cli.main(["account", "list", "--db", str(db)]) == 0
    assert "还没有任何账号" in capsys.readouterr().out
