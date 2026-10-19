"""云端中继进程（P1-1..P1-7）。

入口是 `cloud/relay/main.py`。这个包的模块划分：

| 文件 | 职责 |
|---|---|
| `db.py` | 云端五张表（`docs/05` §2.2）。**没有 media 表**——云端不存照片 |
| `security.py` | scrypt 口令哈希、令牌生成与哈希、常量时间比较 |
| `devices.py` | 设备注册表（**内存 map**，不查库）+ 客户端令牌表 |
| `tunnel.py` | `/agent/ws` 入口、多路复用、帧转发、流控 |
| `forward.py` | `/d/{device_id}/**` HTTP → 帧的桥 |
| `auth.py` | 账号登录、设备/客户端管理 API |
| `pairing.py` | 配对码 API |
| `main.py` | Starlette 应用装配 + 启动 |
"""

__all__: list[str] = []
