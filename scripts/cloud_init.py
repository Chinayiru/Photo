#!/usr/bin/env python
"""云端初始化：建账号、登记电脑、打印一次性的 `agent_token`。

## 为什么没有公开注册接口

这是"自己的照片自己看"的产品，账号是随服务一起建好的。开放注册只会
带来垃圾账号与"谁都能先占个邮箱"，而对真正的用户没有任何好处。所以
建账号这件事由运维**在服务器上跑一次本脚本**完成（`docs/04` §3.2 的
"补充"一节也是这个结论）。

## 为什么 `agent_token` 只打印一次

库里存的是 sha256（`devices.agent_token`），明文只在这一刻存在。
丢掉就只能重新登记一台设备——这是刻意的：明文 token 一旦落在某个
`install.log` 里，就等于把整个照片库的入口留在磁盘上。

用法（在云端仓库根目录）：

```
python -m scripts.cloud_init --email me@example.com --password '...' \\
    --device-name '书房的台式机'

# 重建一台设备的令牌（旧令牌立刻失效）：
python -m scripts.cloud_init --rotate --device-id <32位hex>
```
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from cloud.relay import security                     # noqa: E402
from cloud.relay.auth import create_account          # noqa: E402
from cloud.relay.db import CloudDB                   # noqa: E402


def _db_path() -> str:
    return os.environ.get("PB_CLOUD_DB", "cloud.db")


def cmd_init(args: argparse.Namespace) -> int:
    db = CloudDB(_db_path())
    db.init()
    email = args.email.strip().lower()
    row = db.query_one("SELECT id FROM accounts WHERE email = ?", (email,))
    if row is not None:
        print(f"账号已存在：{email}（id={row['id']}）", file=sys.stderr)
        account_id = str(row["id"])
    else:
        account = create_account(db, email, args.password)
        account_id = account["id"]
        print(f"已建账号：{email} id={account_id}")

    device_id = security.new_device_id()
    token = security.new_token()
    db.execute(
        "INSERT INTO devices(id, account_id, name, agent_token, version, "
        "last_lan, created_at, revoked) VALUES (?, ?, ?, ?, ?, '[]', ?, 0)",
        (device_id, account_id, args.device_name, security.hash_token(token),
         args.version, __import__("time").time()))
    print(json.dumps({
        "account_id": account_id,
        "device_id": device_id,
        "agent_token": token,
        "next_step": (
            "把下面三行写进电脑端 data/config.json 的 cloud 段，"
            "并把 server.transport 改成 \"cloud_tunnel\"：\n"
            f'  "base_url": "{args.base_url}",\n'
            f'  "device_id": "{device_id}",\n'
            f'  "agent_token": "{token}"\n'
            "（device_id 是**云端**分配的 32 位 id，与电脑本地那个 16 位 "
            "device_id 不是一回事：云端握手要用这一个。）"
        ),
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    db = CloudDB(_db_path())
    db.init()
    row = db.query_one("SELECT id, name FROM devices WHERE id = ?",
                       (args.device_id.strip().lower(),))
    if row is None:
        print("没有这台设备", file=sys.stderr)
        return 1
    token = security.new_token()
    db.execute("UPDATE devices SET agent_token = ? WHERE id = ?",
               (security.hash_token(token), str(row["id"])))
    print(json.dumps({"device_id": str(row["id"]), "name": row["name"],
                      "agent_token": token}, ensure_ascii=False, indent=2))
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    db = CloudDB(_db_path())
    db.init()
    rows = db.query(
        "SELECT d.id, d.name, d.version, d.last_seen, d.revoked, "
        "a.email FROM devices AS d JOIN accounts AS a ON a.id = d.account_id "
        "ORDER BY d.created_at ASC")
    for r in rows:
        print(f"{r['id']}  {r['name']:<20} {r['email']:<24} "
              f"revoked={int(r['revoked'] or 0)} last_seen={r['last_seen']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="云端初始化（建账号/登记电脑）")
    sub = parser.add_subparsers(dest="cmd")

    p_init = sub.add_parser("init", help="建账号并登记一台电脑")
    p_init.add_argument("--email", required=True)
    p_init.add_argument("--password", required=True)
    p_init.add_argument("--device-name", default="我的电脑")
    p_init.add_argument("--version", default="0.1.0")
    p_init.add_argument("--base-url", default="https://your-domain.com")
    p_init.set_defaults(func=cmd_init)

    p_rot = sub.add_parser("rotate", help="重建某台设备的 agent_token")
    p_rot.add_argument("--device-id", required=True)
    p_rot.set_defaults(func=cmd_rotate)

    p_ls = sub.add_parser("list", help="列出所有设备")
    p_ls.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
