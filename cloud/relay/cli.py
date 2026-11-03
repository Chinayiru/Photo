#!/usr/bin/env python
"""云端中继的运维命令行（`python scripts/cloud_cli.py account ...`）。

## 为什么需要它

云端**没有注册接口**（`docs/11` §8.9 SEC-04：开放注册等于让任何人给自己
开一个能登录的账号）。于是"第一个账号"、"忘了口令"、"删错设备"这几件事
只能在服务器本机做。以前的做法是临时写一段 sqlite3/repl 脚本直接改库——
那会把 scrypt 参数、`failed_count`/`locked_until` 的语义、以及"口令不许
进命令行"这些细节散落在一次性脚本里，迟早写错一次。

这个模块把这几件事收成子命令，全部走 `security.hash_password()`，并且
**口令只从 stdin 或交互式提示读**（命令行参数会进 `ps`/shell 历史/
`run_relay.ps1` 的日志）。

## 子命令

    account add     --db <path> --email <mail>      # 口令走 stdin（或提示）
    account passwd  --db <path> --email <mail>
    account list    --db <path>

    # 例（口令用管道喂，不进命令行）：
    echo 'mypassword' | python scripts/cloud_cli.py account add \
        --db data/cloud.db --email me@example.com --password-stdin

## 与 `scripts/cloud_run.py` 同一套启动方式

embeddable Python 只按 `python312._pth` 组装 `sys.path`，
`python -m cloud.relay.cli` 会 `ModuleNotFoundError`；所以真正的入口是
`scripts/cloud_cli.py`（它先 bootstrap 再调这里的 `main()`）。
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
import time

from . import db as cloud_db
from . import security

#: 口令最短长度。账号名下的设备数不多，但每台设备都能读全部照片 ——
#: 12 位是"人能接受"与"暴力猜解不现实"之间的合理折中（配合 5 次锁定）。
MIN_PASSWORD_LEN = 12

__all__ = ["main", "MIN_PASSWORD_LEN"]


def _open(path: str) -> cloud_db.CloudDB:
    db = cloud_db.CloudDB(path)
    db.init()
    return db


def _read_password(*, from_stdin: bool, confirm: bool) -> str:
    """读口令。`from_stdin` 时只读一行，不提示、不回显。"""
    if from_stdin:
        line = sys.stdin.readline()
        return line.rstrip("\r\n")
    if confirm:
        first = getpass.getpass("口令（不回显）: ")
        second = getpass.getpass("再输一次: ")
        if first != second:
            raise SystemExit("两次输入不一致")
        return first
    return getpass.getpass("口令（不回显）: ")


def _check_password(password: str, email: str) -> None:
    """口令策略。只做"显然弱"的拦截 —— 熵只能靠长度。"""
    if len(password) < MIN_PASSWORD_LEN:
        raise SystemExit(f"口令至少 {MIN_PASSWORD_LEN} 位（当前 {len(password)} 位）")
    if password.lower() == email.lower():
        raise SystemExit("口令不能等于邮箱")
    if len(set(password)) == 1:
        raise SystemExit("口令不能是同一个字符重复")
    if password.isdigit():
        raise SystemExit("口令不能全是数字")


def _account_id() -> str:
    """账号 id：32 hex，与库里已有行同形（`accounts.id` 只做标识）。"""
    return secrets.token_hex(16)


def cmd_account_add(args: argparse.Namespace) -> int:
    email = str(args.email or "").strip().lower()
    if not security.EMAIL_RE.match(email):
        raise SystemExit(f"邮箱格式不对：{email!r}")
    password = _read_password(from_stdin=args.password_stdin, confirm=True)
    _check_password(password, email)

    db = _open(args.db)
    try:
        row = db.query_one("SELECT id FROM accounts WHERE email = ?", (email,))
        if row is not None:
            raise SystemExit(f"账号已存在：{email}（id={row['id']}）")
        db.insert(
            "INSERT INTO accounts(id, email, pass_hash, created_at) "
            "VALUES (?, ?, ?, ?)",
            (_account_id(), email, security.hash_password(password), time.time()),
        )
    finally:
        db.close()
    print(f"OK 已创建账号 {email}")
    print("下一步：在电脑上打开 PhotoBridge → 设置 → 登录这个账号（或部署时用")
    print("`cloud_init.py init --email ... --password ...` 让设备挂到这个账号下）。")
    return 0


def cmd_account_passwd(args: argparse.Namespace) -> int:
    email = str(args.email or "").strip().lower()
    password = _read_password(from_stdin=args.password_stdin, confirm=True)
    _check_password(password, email)

    db = _open(args.db)
    try:
        row = db.query_one("SELECT id FROM accounts WHERE email = ?", (email,))
        if row is None:
            raise SystemExit(f"没有这个账号：{email}")
        # 改口令顺手解锁：否则用户改完口令仍被上一次的 `locked_until`
        # 挡在门外，会以为是"没改成功"。
        db.execute(
            "UPDATE accounts SET pass_hash = ?, failed_count = 0, "
            "locked_until = NULL WHERE id = ?",
            (security.hash_password(password), row["id"]),
        )
    finally:
        db.close()
    print(f"OK 已更新口令并解锁：{email}")
    print("注意：已有登录态（account_token）仍然有效，最长 7 天。")
    return 0


def cmd_account_list(args: argparse.Namespace) -> int:
    db = _open(args.db)
    try:
        rows = db.query(
            "SELECT a.id, a.email, a.created_at, a.last_login, "
            "(SELECT COUNT(*) FROM devices d WHERE d.account_id = a.id "
            " AND d.revoked = 0) AS devices "
            "FROM accounts a ORDER BY a.created_at"
        )
    finally:
        db.close()
    if not rows:
        print("（还没有任何账号）")
        return 0
    print(f"{'email':32} {'设备':>4}  {'创建时间':19}  id")
    for r in rows:
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["created_at"]))
        print(f"{r['email']:32} {r['devices']:>4}  {created:19}  {r['id']}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cloud_cli",
        description="PhotoBridge 云端中继运维命令（账号在本机维护，云端无注册接口）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    account = sub.add_parser("account", help="账号管理")
    asub = account.add_subparsers(dest="action", required=True)

    for name, func, text in (
        ("add", cmd_account_add, "创建账号"),
        ("passwd", cmd_account_passwd, "改口令（并解锁）"),
    ):
        sp = asub.add_parser(name, help=text)
        sp.add_argument("--db", required=True, help="cloud.db 路径")
        sp.add_argument("--email", required=True, help="账号邮箱")
        sp.add_argument(
            "--password-stdin", action="store_true",
            help="从 stdin 读一行口令（不给就从终端提示，不回显）",
        )
        sp.set_defaults(func=func)

    lp = asub.add_parser("list", help="列出账号")
    lp.add_argument("--db", required=True, help="cloud.db 路径")
    lp.set_defaults(func=cmd_account_list)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
