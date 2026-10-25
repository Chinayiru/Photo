"""照片桥的运维小命令（`python -m backend.pbctl ...`）。

只做「改一行数据库、不碰业务逻辑」这类事：需要重启才生效的，会提醒。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .app import config as _config

DAY = 86400.0


def _open_db():
    cfg = _config.load(reload=True)
    from .app import db as _db

    return _db.get_db(cfg), cfg


def cmd_allow_legacy(args: argparse.Namespace) -> int:
    """给旧密钥（`sha256(agent_token)`）开一个限时窗口。

    ISSUE-049 之后签发权回到电脑端，旧密钥默认不接受。老手机上那枚令牌
    是旧的，除非开窗口否则会直接 401。窗口到期自动关回去，不用再手动关。
    """
    days = float(args.days)
    if days <= 0:
        print("天数必须 > 0（要立刻关闭请用 close-legacy）", file=sys.stderr)
        return 2
    until = time.time() + days * DAY
    db, _cfg = _open_db()
    try:
        db.execute(
            "UPDATE devices SET allow_legacy_until = ? "
            "WHERE revoked = 0 AND ? > allow_legacy_until", (until, until))
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM devices WHERE allow_legacy_until > ?", (until - 1,))
        count = int(row["n"]) if row else 0
        print(f"已开窗 {days:g} 天（到 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(until))}），"
              f"当前 {count} 台设备可用旧密钥")
    finally:
        db.close()
    print("提示：正在运行的服务在下次请求时即按新值判定，无需重启。")
    return 0


def cmd_close_legacy(_args: argparse.Namespace) -> int:
    """立刻关掉旧密钥窗口。"""
    db, _cfg = _open_db()
    try:
        row = db.query_one(
            "SELECT COUNT(*) AS n FROM devices WHERE allow_legacy_until <> 0")
        count = int(row["n"]) if row else 0
        db.execute("UPDATE devices SET allow_legacy_until = 0 "
                   "WHERE allow_legacy_until <> 0")
        print(f"已关闭旧密钥窗口（{count} 台设备受影响）")
    finally:
        db.close()
    return 0


def cmd_legacy_status(_args: argparse.Namespace) -> int:
    """看每台设备的旧密钥窗口。"""
    db, _cfg = _open_db()
    now = time.time()
    try:
        rows = db.query(
            "SELECT id, name, revoked, allow_legacy_until FROM devices ORDER BY name")
    finally:
        db.close()
    if not rows:
        print("（库里没有设备）")
        return 0
    for row in rows:
        until = float(row["allow_legacy_until"] or 0)
        if until <= 0:
            state = "关闭"
        elif until <= now:
            state = "已过期"
        else:
            left = (until - now) / DAY
            state = f"开到 {time.strftime('%Y-%m-%d %H:%M', time.localtime(until))}（剩 {left:.1f} 天）"
        print(f"{row['id']}  {row['name']!r:24}  revoked={int(row['revoked'])}  旧密钥：{state}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pbctl", description="照片桥运维小命令")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("allow-legacy", help="给旧密钥开限时窗口")
    p.add_argument("days", type=float, help="窗口天数，例如 7")
    p.set_defaults(func=cmd_allow_legacy)

    p = sub.add_parser("close-legacy", help="立刻关闭旧密钥窗口")
    p.set_defaults(func=cmd_close_legacy)

    p = sub.add_parser("legacy-status", help="查看旧密钥窗口")
    p.set_defaults(func=cmd_legacy_status)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())