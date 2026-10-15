
---

### 场景 18：直连探测失败（★ 本机有 5 个 IPv4，要选对）

**症状**：手机在家（同一 WiFi）也显示 🔵 云端，而不是 🟢 直连；传输速度慢。

**★★ 这是本项目的关键坑：本机有 5 个 IPv4 地址，其中 4 个是虚拟网卡。**

**先看你的网络接口**（★ 实测结果）：

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne "127.0.0.1" } |
    Select-Object IPAddress, InterfaceAlias, PrefixOrigin
```

**实测输出**：

| IP 地址 | 接口名 | 类型 | 是否应该上报 |
|---|---|---|---|
| **192.0.2.10** | **以太网** | **真实局域网** | ✅ **就是这个** |
| 26.26.192.40 | Radmin VPN | 虚拟网卡 | ❌ 排除 |
| 192.168.64.1 | VMware Network Adapter VMnet1 | 虚拟网卡 | ❌ 排除 |
| 192.168.226.1 | VMware Network Adapter VMnet8 | 虚拟网卡 | ❌ 排除 |
| 192.168.32.1 | vEthernet (WSL / Hyper-V firewall) | 虚拟网卡 | ❌ 排除 |

> **★ 5 个 IPv4 里只有 192.0.2.10 是真的**，其余 4 个都是虚拟网卡（Radmin VPN、VMware ×2、WSL/Hyper-V）。
> 如果程序上报了 `192.168.64.1`（VMnet1），手机永远探测不通 → 永远走云端。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **上报了虚拟网卡 IP** | ★ 看 HELLO 里的 lan_ips |
| 2 | **防火墙没放行 8787** | 见 §5.7 |
| 3 | 手机和电脑不在同一 WiFi | 看手机连的是哪个 WiFi |
| 4 | 路由器 AP 隔离 | 同 WiFi 但 ping 不通 |
| 5 | 电脑休眠 | 唤醒后重试 |
| 6 | 手机用了流量而不是 WiFi | 看状态栏 |

**排查步骤**：

```powershell
# 步骤 1：看程序上报了哪些 IP（★ 关键）
Get-Content "D:\Apps\PhotoBridge\data\logs\agent.log" -Tail 100 | Select-String "lan_ips|局域网地址|HELLO"
# 预期：lan_ips=['192.0.2.10']
# ★ 如果包含 26.26.x.x / 192.168.64.x / 192.168.226.x / 192.168.32.x → 是 bug
```

```bash
# 步骤 2：云端看电脑端上报的 IP
cd /opt/photobridge-cloud
docker compose logs --tail=50 relay | grep -i "lan_ips\|hello"
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
for r in c.execute('SELECT device_id, last_lan FROM devices'):
    print(r)
"
```

```powershell
# 步骤 3：本机测试服务是否真的监听在 192.0.2.10
netstat -ano | findstr :8787
# ★ 正确：TCP  192.0.2.10:8787  LISTENING
# ★ 错误：TCP  0.0.0.0:8787  LISTENING（监听所有网卡，包括虚拟网卡）
# ★ 错误：TCP  127.0.0.1:8787  LISTENING（只有本机能访问）

# 步骤 4：防火墙规则
Get-NetFirewallRule -DisplayName "*PhotoBridge*" | Select-Object DisplayName, Enabled, Profile, Direction, Action

# 步骤 5：用手机测（手机浏览器打开）
# http://192.0.2.10:8787/api/system/ping
# 有 JSON = 通了
```

**解决方案**：

| 情况 | 解决 |
|---|---|
| **上报了虚拟网卡 IP** | ★ **这是 bug，必须修**。选 IP 的逻辑应该：①排除已知虚拟网卡名（Radmin/VMware/Hyper-V/WSL/VirtualBox/TAP）②优先选有默认网关的接口 ③优先选 `192.168.x.x` / `10.x.x.x` |
| 防火墙没放行 | §5.7 的 `New-NetFirewallRule`（只勾**专用网络**） |
| 不在同一 WiFi | 连同一个 WiFi（注意别连 5G 和 2.4G 的不同 SSID） |
| AP 隔离 | ★ 路由器设置里关掉「AP 隔离」/「客户端隔离」/「访客网络隔离」 |
| 电脑休眠 | 改电源设置（场景 2） |
| 用了流量 | 切到 WiFi |

**★ 选 IP 的正确逻辑**（给开发看）：

```python
import psutil
import socket

# 已知的虚拟网卡关键词（排除）
VIRTUAL_KEYWORDS = [
    'radmin', 'vmware', 'vmnet', 'hyper-v', 'vethernet', 'wsl',
    'virtualbox', 'vbox', 'tap', 'tun', 'loopback', 'bluetooth',
    'docker', 'veth', 'br-', 'hamachi', 'zerotier', 'tailscale',
]

def pick_lan_ip() -> str | None:
    """挑一个真实的局域网 IPv4 地址"""
    candidates = []

    # 方法 A：用 socket 连外部地址，看系统选了哪个出口 IP（★ 最可靠）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))          # 不会真的发包
        primary = s.getsockname()[0]
        s.close()
        if not primary.startswith('127.'):
            return primary
    except Exception:
        pass

    # 方法 B：遍历网卡，排除虚拟的
    for name, addrs in psutil.net_if_addrs().items():
        low = name.lower()
        if any(k in low for k in VIRTUAL_KEYWORDS):
            continue
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            ip = a.address
            if ip.startswith('127.') or ip.startswith('169.254.'):
                continue
            if ip.startswith(('192.168.', '10.', '172.')):   # 私有网段
                candidates.append((name, ip))

    # 优先 192.168.x.x
    for name, ip in candidates:
        if ip.startswith('192.168.'):
            return ip
    return candidates[0][1] if candidates else None
```

**★ 验证方法 A 是否选对**：

```powershell
python -c "
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.connect(('8.8.8.8', 80))
print('系统默认出口 IP:', s.getsockname()[0])
s.close()
"
# 预期：192.0.2.10
```

---

### 场景 19：配对码输错锁定

**症状**：手机输配对码提示"配对码错误"；连续几次后提示"已锁定，请稍后再试"。

**这是安全机制，不是故障。**

**配对码规则**（[设计文档.md](../设计文档.md)）：

| 项 | 值 |
|---|---|
| 格式 | 6 位数字 |
| 有效期 | **5 分钟** |
| 最大错误次数 | **5 次** |
| 锁定时长 | **10 分钟** |
| 存储 | 云端 `pair_codes` 表 |
| 校验 | 云端校验 |

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | 配对码过期（超过 5 分钟） | 托盘倒计时结束了 |
| 2 | 输错了 | 看清楚了没 |
| 3 | **错满 5 次被锁** | 提示"已锁定" |
| 4 | 托盘显示的码已经刷新了 | 重新显示配对码 |
| 5 | 云端 pair_codes 表有残留 | 查数据库 |

**排查步骤**：

```bash
# 步骤 1：看当前配对码状态
cd /opt/photobridge-cloud
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
c.row_factory = sqlite3.Row
for r in c.execute('SELECT * FROM pair_codes'):
    print(dict(r))
"
```

```
# 步骤 2：重新显示配对码（★ 最常用）
# 电脑托盘图标 → 右键 → 「显示配对码」
# 会生成一个全新的 6 位码，5 分钟有效
```

**解决方案**：

| 情况 | 解决 |
|---|---|
| 码过期了 | 托盘右键 →「显示配对码」，拿新码 |
| 输错了 | 仔细看托盘显示的 6 位数字 |
| **被锁了** | **等 10 分钟**，或见下方「立即解锁」 |
| 一直失败 | 检查手机连的是不是对的域名；检查云端时间 |
| 托盘不显示码 | 重启程序；或检查托盘菜单（§5.5） |

**★ 立即解锁**（不想等 10 分钟）：

```bash
cd /opt/photobridge-cloud
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
n = c.execute('DELETE FROM pair_codes').rowcount
c.commit()
print(f'已清除 {n} 条配对码记录')
"
# 然后托盘重新显示配对码
```

> **★ 注意**：清除配对码**不影响已配对的手机**（那些记录在 `clients` 表）。
> 只影响"正在进行的配对流程"。

**★ 安全性说明**：为什么 6 位数字够用？

| 防护层 | 作用 |
|---|---|
| 有效期 5 分钟 | 攻击窗口极小 |
| 错 5 次锁 10 分钟 | 暴力破解速度上限：每 10 分钟 5 次 |
| **必须先登录** | 攻击者得先有账号密码 |
| 6 位 = 100 万种组合 | 按上面速度，穷举需要约 38 年 |

---

### 场景 20：数据不一致（文件被手动删了）

**症状**：时间线上有照片，但点开显示 404；或缩略图在但原图打不开。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **文件在资源管理器里被手动删了** | 去库目录看 |
| 2 | 文件被移到别的目录 | 搜索文件名 |
| 3 | 磁盘/网络驱动器断开 | 检查盘符 |
| 4 | 索引器还没发现删除 | 手动重新扫描 |
| 5 | 文件被别的程序改名 | 看目录 |
| 6 | 文件系统损坏 | `chkdsk` |

**排查步骤**：

```powershell
# 步骤 1：看有多少文件被标记为 missing
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
n = c.execute('SELECT COUNT(*) FROM media WHERE missing=1').fetchone()[0]
total = c.execute('SELECT COUNT(*) FROM media').fetchone()[0]
print(f'标记 missing: {n} / 总计 {total}')
for r in c.execute('SELECT rel_path FROM media WHERE missing=1 LIMIT 10'):
    print('  ', r[0])
"

# 步骤 2：验证文件是不是真的不在了
python -c "
import sqlite3, os
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
root = r'D:\Photos'
for (rel,) in c.execute('SELECT rel_path FROM media WHERE missing=1 LIMIT 5'):
    full = os.path.join(root, rel.replace('/', os.sep))
    print(f'{rel}: 存在={os.path.exists(full)}')
"

# 步骤 3：看审计日志，是不是本项目删的（★ 重要）
Get-Content "D:\Apps\PhotoBridge\data\audit.log" -Tail 50

# 步骤 4：检查盘符在不在
Get-PSDrive -PSProvider FileSystem
```

**★ 审计日志是关键证据**：

本项目**所有删除都写 `data/audit.log`**（[设计文档.md](../设计文档.md) §8.4 硬约束 ④）。

| 审计日志有记录 | 结论 |
|---|---|
| 有删除记录 | 是通过本项目删的 → 应该进了**回收站**，去回收站恢复 |
| **没有删除记录** | ★ 是**手动**在资源管理器里删的 → 本项目不知道，去回收站找 |

**解决方案**：

| 情况 | 解决 |
|---|---|
| 手动删的 | 去**回收站**找（Windows 回收站或小米相册回收站） |
| 程序删的 | 去**回收站**找（本项目用 `send2trash`，一定进回收站） |
| 文件移走了 | 重新扫描，索引器会更新路径 |
| 盘符断了 | 接上磁盘/网络驱动器，重新扫描 |
| 索引没更新 | 点托盘「重新扫描」 |
| 想清理 missing 记录 | 见下 |
| 文件系统损坏 | `chkdsk E: /f`（★ 先备份！） |

```powershell
# 清理 missing 记录（确认文件真的不要了）
# ★ 先备份数据库！
Copy-Item "D:\Apps\PhotoBridge\data\photobridge.db" "D:\Apps\PhotoBridge\data\photobridge.db.bak"

# 看会删多少
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
print('会删除', c.execute('SELECT COUNT(*) FROM media WHERE missing=1').fetchone()[0], '条记录')
"

# 确认后执行
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
n = c.execute('DELETE FROM media WHERE missing=1').rowcount
c.commit()
print(f'已删除 {n} 条')
"
```

> **★ 注意**：这只是删数据库记录，**不动照片文件**。
> 如果文件其实还在（只是路径变了），先重新扫描而不是删记录。

---

### 场景 21：云端被入侵的迹象

**症状**：CPU 一直 100%、流量异常大、有陌生进程、日志有奇怪的登录。

**★ 立即按 §12.1 处理。**

**排查步骤**：

```bash
# 1. 看有没有陌生进程
ps aux --sort=-%cpu | head -20

# 2. 看登录记录（★ 关键）
sudo last -20
sudo lastb -20        # 失败登录
sudo grep "Accepted\|Failed" /var/log/auth.log | tail -30

# 3. 看网络连接
sudo ss -tunap | head -30

# 4. 看 crontab（挖矿木马常在这里）
crontab -l; sudo crontab -l; sudo ls -la /etc/cron.*/; cat /etc/crontab

# 5. 看有没有异常的 SSH 密钥
cat ~/.ssh/authorized_keys
sudo cat /root/.ssh/authorized_keys

# 6. 看流量
vnstat -d

# 7. 看 fail2ban 有没有在工作
sudo fail2ban-client status sshd
```

**危险信号**：

| 信号 | 含义 |
|---|---|
| `last` 里有陌生 IP | 有人登录了 |
| `authorized_keys` 里有不认识的 key | ★ 后门 |
| crontab 里有奇怪的任务 | ★ 挖矿/持久化 |
| CPU 100% 但有陌生进程 | ★ 挖矿 |
| 流量暴涨 | 被当跳板/挖矿 |
| `/tmp` 里有可执行文件 | ★ 常见木马位置 |

---

### 场景 22：Caddy 反复重启

**症状**：`docker compose ps` 显示 caddy 一直 Restarting；网站 502。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **Caddyfile 语法错** | 看日志 |
| 2 | 端口被占用 | `ss -tlnp` |
| 3 | 证书卷权限问题 | 看日志 |
| 4 | 内存不足 | 场景 8 |
| 5 | Caddyfile 挂载路径错 | `docker inspect` |

**排查步骤**：

```bash
# 步骤 1：看为什么退出
docker compose logs --tail=100 caddy
docker inspect pb-caddy --format '{{.State.ExitCode}} {{.State.Error}}'

# 步骤 2：校验 Caddyfile 语法（★ 最有用）
docker run --rm -v /opt/photobridge-cloud/Caddyfile:/etc/caddy/Caddyfile caddy:2 \
    caddy validate --config /etc/caddy/Caddyfile

# 步骤 3：看端口占用
sudo ss -tlnp | grep -E ':80|:443'

# 步骤 4：看挂载
docker inspect pb-caddy --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} ({{.Mode}}){{"\n"}}{{end}}'
```

**解决方案**：

| 情况 | 解决 |
|---|---|
| Caddyfile 语法错 | 按 `caddy validate` 的报错改，然后 `docker compose up -d caddy` |
| 80/443 被占 | `sudo systemctl stop nginx apache2`；或杀掉占用进程 |
| 挂载路径错 | 检查 compose 的 volumes 路径 |
| 内存不足 | 加 swap（场景 8） |

**★ Caddyfile 常见语法错**：

| 错误 | 正确 |
|---|---|
| `reverse_proxy /agent/ws relay:8000` 写成多行没闭合 | 用 `{ }` 块 |
| `handle /agent/ws` 里没有 handler | 必须有一个 handler（如 `reverse_proxy`） |
| 用了 `$DOMAIN` 但环境变量没传 | 检查 compose 的 `environment` |
| 花括号不匹配 | `caddy fmt` 自动格式化 |

```bash
# 格式化 Caddyfile（自动修缩进，能发现括号问题）
docker run --rm -v /opt/photobridge-cloud/Caddyfile:/etc/caddy/Caddyfile caddy:2 \
    caddy fmt --overwrite /etc/caddy/Caddyfile
```

---

### 场景 23：relay 健康检查不通过

**症状**：`docker compose ps` 显示 `pb-relay` 是 `unhealthy`；或一直 `starting`。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | `/healthz` 接口没实现 | 日志有 404 |
| 2 | 数据库打不开 | 日志有 SQLite 错误 |
| 3 | 端口没监听 | `ss -tlnp` |
| 4 | 启动慢（健康检查超时太短） | 等一会就好 |
| 5 | 依赖缺失（import 失败） | 日志有 ImportError |
| 6 | 权限问题（非 root 用户读不了 /data） | 日志有 PermissionError |

**排查步骤**：

```bash
cd /opt/photobridge-cloud

# 步骤 1：看日志（★ 一定先看这个）
docker compose logs --tail=100 relay

# 步骤 2：手动测 healthz
docker compose exec relay python -c "
import urllib.request
try:
    print(urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3).read().decode())
except Exception as e:
    print('FAIL:', e)
"

# 步骤 3：看容器里的端口
docker compose exec relay netstat -tlnp 2>/dev/null || echo "无 netstat"

# 步骤 4：看数据目录权限
docker compose exec relay ls -la /data
docker compose exec relay id

# 步骤 5：看健康检查配置与状态
docker inspect pb-relay --format '{{json .State.Health}}' | python -m json.tool
```

**解决方案**：

| 情况 | 解决 |
|---|---|
| `ImportError` | 依赖没装全，检查 `requirements.txt`，重建镜像 |
| `PermissionError` on /data | `sudo chown -R 1000:1000 data/`（Dockerfile 里 relay 用户 uid=1000） |
| 数据库损坏 | 见场景 9 |
| 启动慢 | 加大 healthcheck 的 `start_period`（如 60s） |
| 端口没监听 | 检查 `uvicorn` 的 `--host 0.0.0.0` |
| healthz 没实现 | 需要在代码里加 `@app.get("/healthz")` |

```bash
# 修复 /data 权限
sudo chown -R 1000:1000 /opt/photobridge-cloud/data
docker compose restart relay
```

---

### 场景 24：下载时文件名不对

**症状**：下载原图后文件名变成 `1.jpg`、`download`、`file` 之类的，丢失了原文件名。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **`Content-Disposition` 没设** | 看响应头 |
| 2 | 前端用了 `<a download>` 没指定名字 | 看代码 |
| 3 | 跨域导致 download 属性失效 | 用 blob 下载 |
| 4 | 中文名导致降级 | 见场景 12 |

```powershell
# 排查：看响应头
curl.exe -sS -I "http://127.0.0.1:8787/api/items/1/raw?download=1"
# 找 Content-Disposition
```

**解决方案**：

```python
# 服务端：正确设置 Content-Disposition（见场景 12 的 content_disposition 函数）
from fastapi.responses import FileResponse

return FileResponse(
    path=full_path,
    media_type="application/octet-stream",
    headers={"Content-Disposition": content_disposition(original_name)},
)
```

```javascript
// 前端：如果服务端头不对，可以自己控制文件名
const resp = await fetch(url);
const blob = await resp.blob();
const a = document.createElement('a');
a.href = URL.createObjectURL(blob);
a.download = originalName;    // ★ 指定名字
a.click();
URL.revokeObjectURL(a.href);  // ★ 释放
```

> **★ 跨域注意**：如果前端和 API 不同源，`<a download>` 的 `download` 属性会被忽略（浏览器安全限制）。
> 这时必须用上面的 **blob 下载**方式。

---

### 10.25 故障速查表（★ 打印出来贴墙上）

| 症状 | 最可能的原因 | 第一条命令 |
|---|---|---|
| 手机打不开网站 | DNS / 防火墙 | `nslookup 域名 8.8.8.8` |
| 电脑显示离线 | 电脑关机 / 程序没跑 | `Get-Process PhotoBridge` |
| 上传慢 | 手机信号 / 走了云端 | 看手机图标 🟢🔵 |
| 缩略图不出来 | 索引没跑完 / HEIC | `SELECT thumb_state, COUNT(*)` |
| 证书报错 | 80 端口 / 限流 | `docker compose logs caddy` |
| WebSocket 连不上 | Caddyfile 顺序 | `curl -i ... /agent/ws` |
| 磁盘满 | Docker 日志 | `df -h` + `docker system df` |
| 内存爆 | 流控失效 | `docker stats` |
| 数据库锁 | WAL 没开 | `PRAGMA journal_mode` |
| 手机一直重连 | 隧道不稳 / 省电 | 看电脑端日志 |
| 时间线乱序 | EXIF 解析 | `SELECT taken_src, COUNT(*)` |
| 中文乱码 | Content-Disposition | `curl -I .../raw` |
| 上传中断 | 手机切后台 | 看 `upload_sessions` |
| 照片不在时间线 | 扩展名不在白名单 | 对比扩展名分布 |
| `.jpg-lx2` 被跳过 | ★ **预期行为** | 无（正常） |
| CPU 占用高 | 正在索引 | 看 `index_state` |
| 手机 OOM | 解码太多图 | 减少滑动速度 |
| 直连探测失败 | ★ **选错虚拟网卡 IP** | `Get-NetIPAddress` |
| 配对码锁定 | 错 5 次 | 等 10 分钟 |
| 数据不一致 | 手动删了文件 | 看 `audit.log` |
| 云端异常 | 可能被入侵 | `ps aux --sort=-%cpu` |
| Caddy 重启 | Caddyfile 语法 | `caddy validate` |
| relay unhealthy | /healthz 或权限 | `docker compose logs relay` |
| 下载名不对 | Content-Disposition | `curl -I` |

---

### 10.26 §10 完成检查清单

| # | 检查项 |
|---|---|
| 1 | 知道五段链路（手机→Caddy→relay→隧道→电脑端→文件） |
| 2 | 会用 §10.0.2 的一键诊断脚本 |
| 3 | 24 个场景都能按症状找到 |
| 4 | ★ 知道 `.jpg-lx2` 跳过是正常的 |
| 5 | ★ 知道本机 5 个 IPv4 里要选 192.0.2.10 |
| 6 | 知道速查表在哪（§10.25） |
