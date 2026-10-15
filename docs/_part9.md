
---

## 9. 监控与告警

### 9.1 云端监控

#### 9.1.1 容器资源占用

```bash
# 实时看（类似 top，Ctrl+C 退出）
docker stats

# 只看一次（适合脚本/日志）
docker stats --no-stream

# 只看 relay
docker stats --no-stream pb-relay
```

**预期输出**：

```
CONTAINER ID   NAME       CPU %     MEM USAGE / LIMIT     MEM %     NET I/O           BLOCK I/O
a1b2c3d4e5f6   pb-relay   0.85%     118.4MiB / 957.5MiB   12.37%    1.24GB / 3.87GB   12.3MB / 4.1MB
f6e5d4c3b2a1   pb-caddy   0.12%     24.6MiB / 957.5MiB    2.57%    3.87GB / 1.24GB   1.2MB / 0B
```

**★ 关键监控指标**：

| 指标 | 正常范围 | 异常信号 | 含义 |
|---|---|---|---|
| **relay MEM** | 80–150 MB | **> 400 MB** | ⚠️ 流控可能失效，大文件在内存堆积（[11](11-风险登记册与决策记录.md) R-006） |
| relay CPU | < 5% | > 50% 持续 | 异常，可能有攻击或死循环 |
| caddy MEM | 20–50 MB | > 200 MB | 连接数异常 |
| NET I/O | 缓慢增长 | 突然暴涨 | 可能被刷流量 |

#### 9.1.2 系统资源

```bash
# htop（更好看的 top）
sudo apt install -y htop
htop

# 或 top
top -bn1 | head -20

# 内存
free -h

# 磁盘（★ 最重要的监控项）
df -h

# 磁盘 inode（小文件多时会先耗尽 inode）
df -i
```

**预期输出（df -h）**：

```
Filesystem      Size  Used Avail Use% Mounted on
/dev/vda1        20G  4.2G   15G  23% /
tmpfs           479M     0  479M   0% /dev/shm
```

| 指标 | 正常 | 警告 | 危险 |
|---|---|---|---|
| 磁盘使用率 | < 70% | 70–85% | **> 85%**（SQLite 写入会失败） |
| 内存使用 | < 70% | 70–85% | **> 90%**（可能触发 OOM Killer） |
| inode 使用 | < 70% | 70–85% | > 85% |

#### 9.1.3 一条命令看全貌

```bash
echo "=== $(date '+%F %T') ==="
echo "--- 磁盘 ---"
df -h / | tail -1
echo "--- 内存 ---"
free -m | awk 'NR==2 {printf "使用 %s MB / 总 %s MB (%.1f%%)\n", $3, $2, $3*100/$2}'
echo "--- 负载 ---"
uptime | awk -F'load average:' '{print $2}'
echo "--- 容器 ---"
docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
echo "--- 隧道 ---"
docker compose -f /opt/photobridge-cloud/docker-compose.yml logs --tail=5 relay 2>/dev/null | tail -3
```

### 9.2 隧道状态监控

#### 9.2.1 怎么确认电脑端在线

**方法 A：看云端日志**（最直接）

```bash
cd /opt/photobridge-cloud
docker compose logs --tail=50 relay | grep -iE "hello|agent|disconnect|reconnect"
```

**在线时的输出**：

```
[agent] dev_a1b2c3d4e5f6 已连接 (来自 203.0.113.45)
[agent] dev_a1b2c3d4e5f6 HELLO: name=家里的电脑 version=0.3.1 lan_ips=['192.0.2.10']
[agent] dev_a1b2c3d4e5f6 隧道就绪
```

**离线时的输出**：

```
[agent] dev_a1b2c3d4e5f6 连接断开 (code=1006, reason=)
[agent] dev_a1b2c3d4e5f6 等待重连...
```

**方法 B：看设备状态表**

```bash
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
c.row_factory = sqlite3.Row
for r in c.execute('SELECT device_id, name, last_seen, last_lan FROM devices'):
    print(f\"{r['device_id']}  {r['name']}  last_seen={r['last_seen']}  lan={r['last_lan']}\")
"
```

> **判断在线**：`last_seen` 距今 < 60 秒（心跳 25 秒一次）。

**方法 C：用 API**

```bash
curl -sS https://photos.example.com/api/devices/dev_xxx/status -b cookies.txt | python -m json.tool
```

**方法 D：从手机/浏览器看**

打开 `https://photos.example.com`，设备列表里 🟢 = 在线。

#### 9.2.2 隧道健康度指标

| 指标 | 正常 | 异常 |
|---|---|---|
| 心跳间隔 | 25 秒一次 | 超过 60 秒没心跳 = 可疑断线 |
| 重连次数 | 每天 0–2 次 | 频繁重连（> 10 次/天）= 网络不稳 |
| 隧道内存 | 80–150 MB | > 400 MB |
| 活跃连接数 | 1（单设备） | 多个（可能重复启动） |

**统计今天的重连次数**：

```bash
docker compose logs --since 24h relay | grep -c "重连\|reconnect" || echo 0
```

### 9.3 日志位置

#### 9.3.1 云端日志

| 日志 | 位置 | 查看命令 |
|---|---|---|
| **relay 应用日志** | 容器 stdout | `docker compose logs relay` |
| **Caddy 访问日志** | `/opt/photobridge-cloud/caddy_data/access.log` | `tail -f /opt/photobridge-cloud/caddy_data/access.log` |
| **Caddy 系统日志** | 容器 stdout | `docker compose logs caddy` |
| **Docker 服务日志** | journald | `sudo journalctl -u docker` |
| **SSH 登录日志** | `/var/log/auth.log` | `sudo tail -f /var/log/auth.log` |
| **fail2ban 日志** | `/var/log/fail2ban.log` | `sudo tail -f /var/log/fail2ban.log` |
| **ufw 日志** | `/var/log/ufw.log` | `sudo tail -f /var/log/ufw.log` |
| **内核日志**（OOM 信息在这） | dmesg | `sudo dmesg -T \| tail -50` |

**常用 journalctl 命令**：

```bash
sudo journalctl -u docker --since "1 hour ago"     # Docker 服务日志
sudo journalctl -b                                  # 本次启动日志
sudo journalctl -f                                  # 实时跟随
sudo journalctl -p err --since today                # 只看错误级别

# ★ 看 OOM Killer 有没有杀进程（内存不足时查这个）
sudo journalctl -k --since "24 hours ago" | grep -i "out of memory\|killed process"
```

#### 9.3.2 电脑端日志

| 日志 | 位置 |
|---|---|
| **Agent 主日志** | `D:\Apps\PhotoBridge\data\logs\agent.log` |
| **审计日志** | `D:\Apps\PhotoBridge\data\audit.log` |
| **错误日志**（如果分开） | `D:\Apps\PhotoBridge\data\logs\error.log` |

```powershell
# 实时跟随
Get-Content "D:\Apps\PhotoBridge\data\logs\agent.log" -Wait -Tail 20

# 找错误
Select-String -Path "D:\Apps\PhotoBridge\data\logs\agent.log" -Pattern "ERROR|Exception" | Select-Object -Last 20

# 看审计日志（谁删了什么）
Get-Content "D:\Apps\PhotoBridge\data\audit.log" -Tail 20

# 只看今天的
Select-String -Path "D:\Apps\PhotoBridge\data\logs\agent.log" -Pattern (Get-Date -Format "yyyy-MM-dd")
```

### 9.4 带宽用量查看

#### 9.4.1 云端 traffic 表

```bash
# 看表结构
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
print([r[1] for r in c.execute('PRAGMA table_info(traffic)')])
"

# 本月总流量
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
row = c.execute(\"SELECT SUM(bytes_in), SUM(bytes_out) FROM traffic WHERE ts >= date('now','start of month')\").fetchone()
bi = (row[0] or 0) / 1024 / 1024
bo = (row[1] or 0) / 1024 / 1024
print(f'本月入站: {bi:.1f} MB')
print(f'本月出站: {bo:.1f} MB')
print(f'合计: {bi+bo:.1f} MB ({(bi+bo)/1024:.2f} GB)')
"
```

**预期输出**：

```
本月入站: 2340.5 MB
本月出站: 2280.3 MB
合计: 4620.8 MB (4.51 GB)
```

> 与 [设计文档.md](../设计文档.md) §12 的预算（约 4.6 GB/月）吻合。
> RackNerd 给 3 TB/月，**用掉 0.15%**——完全不用担心。

#### 9.4.2 按天统计

```bash
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
print('日期        入站(MB)   出站(MB)')
print('-' * 36)
for r in c.execute('''
    SELECT date(ts) d, SUM(bytes_in)/1048576.0, SUM(bytes_out)/1048576.0
    FROM traffic GROUP BY d ORDER BY d DESC LIMIT 14
'''):
    print(f'{r[0]}  {r[1]:>8.1f}  {r[2]:>8.1f}')
"
```

#### 9.4.3 系统层面看流量

```bash
cat /proc/net/dev          # 网卡累计流量

# 用 vnstat 看历史（需要安装）
sudo apt install -y vnstat
sudo systemctl enable --now vnstat
vnstat -d        # 按天
vnstat -m        # 按月
```

> **注意**：`/proc/net/dev` 是**累计值**，重启或网卡重置时会清零。

### 9.5 可用性检查脚本

#### 9.5.1 完整探活脚本

创建 `/opt/photobridge-cloud/healthcheck.sh`：

```bash
#!/usr/bin/env bash
# ============================================================
#  PhotoBridge 可用性检查
#  检查：HTTPS 可达、证书有效期、容器状态、隧道在线、磁盘、内存、relay 内存
#  用法：./healthcheck.sh  → 退出码 0=正常，非 0=异常
# ============================================================

set -uo pipefail

DOMAIN="${DOMAIN:-photos.example.com}"
APP_DIR="/opt/photobridge-cloud"
WEBHOOK_URL="${WEBHOOK_URL:-}"     # 可选：异常时推送到这个 webhook

FAILED=0
MSGS=()

fail() { FAILED=$((FAILED + 1)); MSGS+=("FAIL $1"); }
ok()   { MSGS+=("OK   $1"); }

# ── 1. HTTPS 可达性 ───────────────────────────────────────
CODE=$(curl -sS -o /dev/null -w "%{http_code}" -m 10 "https://${DOMAIN}/healthz" 2>/dev/null || echo "000")
if [ "$CODE" = "200" ]; then
    ok "HTTPS 可达 (200)"
else
    fail "HTTPS 异常 (HTTP $CODE)"
fi

# ── 2. 证书有效期 ─────────────────────────────────────────
EXPIRY=$(echo | openssl s_client -connect "${DOMAIN}:443" -servername "${DOMAIN}" 2>/dev/null \
    | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -n "$EXPIRY" ]; then
    EXP_EPOCH=$(date -d "$EXPIRY" +%s 2>/dev/null || echo 0)
    NOW_EPOCH=$(date +%s)
    DAYS_LEFT=$(( (EXP_EPOCH - NOW_EPOCH) / 86400 ))
    if [ "$DAYS_LEFT" -gt 14 ]; then
        ok "证书剩余 ${DAYS_LEFT} 天"
    elif [ "$DAYS_LEFT" -gt 7 ]; then
        fail "证书即将过期（剩余 ${DAYS_LEFT} 天）—— 检查 Caddy 自动续期"
    else
        fail "证书 ${DAYS_LEFT} 天后过期！立即处理"
    fi
else
    fail "无法获取证书信息"
fi

# ── 3. 容器状态 ───────────────────────────────────────────
cd "$APP_DIR" || exit 2
for svc in relay caddy; do
    STATE=$(docker compose ps --format json "$svc" 2>/dev/null | grep -o '"State":"[^"]*"' | cut -d'"' -f4)
    if [ "$STATE" = "running" ]; then
        ok "容器 $svc 运行中"
    else
        fail "容器 $svc 状态异常 ($STATE)"
    fi
done

# ── 4. 隧道在线（最近 60 秒有心跳）────────────────────────
LAST=$(docker compose logs --since 60s relay 2>/dev/null | grep -c "PING\|heartbeat" || echo 0)
if [ "$LAST" -gt 0 ]; then
    ok "隧道活跃（近 60 秒有心跳）"
else
    fail "隧道可能断开（近 60 秒无心跳）—— 检查电脑端是否开机"
fi

# ── 5. 磁盘空间 ───────────────────────────────────────────
DISK_PCT=$(df / | awk 'NR==2 {gsub("%",""); print $5}')
if [ "$DISK_PCT" -lt 70 ]; then
    ok "磁盘使用 ${DISK_PCT}%"
elif [ "$DISK_PCT" -lt 85 ]; then
    fail "磁盘使用 ${DISK_PCT}%（偏高）"
else
    fail "磁盘使用 ${DISK_PCT}%（危险！）"
fi

# ── 6. 内存 ───────────────────────────────────────────────
MEM_PCT=$(free | awk 'NR==2 {printf "%.0f", $3*100/$2}')
if [ "$MEM_PCT" -lt 80 ]; then
    ok "内存使用 ${MEM_PCT}%"
else
    fail "内存使用 ${MEM_PCT}%（偏高）"
fi

# ── 7. relay 进程内存（★ 流控是否失效）──────────────────
RELAY_MEM=$(docker stats --no-stream --format "{{.MemUsage}}" pb-relay 2>/dev/null | cut -d/ -f1 | tr -d ' MiB')
if [ -n "$RELAY_MEM" ]; then
    if [ "${RELAY_MEM%.*}" -lt 300 ]; then
        ok "relay 内存 ${RELAY_MEM} MB"
    else
        fail "relay 内存 ${RELAY_MEM} MB（偏高，流控可能失效）"
    fi
fi

# ── 输出结果 ──────────────────────────────────────────────
echo "=========================================="
echo " PhotoBridge 健康检查  $(date '+%F %T')"
echo "=========================================="
for m in "${MSGS[@]}"; do
    echo "  $m"
done
echo "=========================================="

if [ "$FAILED" -gt 0 ]; then
    echo "结果: $FAILED 项异常"

    # ── 推送告警 ──────────────────────────────────────────
    if [ -n "$WEBHOOK_URL" ]; then
        BODY=$(printf '%s\n' "${MSGS[@]}" | sed 's/"/\\"/g' | tr '\n' ';')
        curl -sS -X POST "$WEBHOOK_URL" \
            -H "Content-Type: application/json" \
            -d "{\"text\":\"PhotoBridge 健康检查异常 ($FAILED 项): $BODY\"}" \
            > /dev/null 2>&1 || true
    fi

    exit 1
else
    echo "结果: 全部正常"
    exit 0
fi
```

**安装并测试**：

```bash
cd /opt/photobridge-cloud
chmod +x healthcheck.sh
./healthcheck.sh
echo "退出码: $?"
```

#### 9.5.2 用 webhook 告警（★ 推荐）

**方式 A：企业微信机器人**

```bash
# 1. 在企业微信群里添加「群机器人」，拿到 webhook URL
#    形如: https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx

# 2. 测试推送
curl -sS -X POST "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx" \
  -H "Content-Type: application/json" \
  -d '{"msgtype":"text","text":{"content":"PhotoBridge 测试消息"}}'

# 3. 配合脚本使用
export WEBHOOK_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx"
./healthcheck.sh
```

**方式 B：钉钉机器人**

```bash
curl -sS -X POST "https://oapi.dingtalk.com/robot/send?access_token=xxxx" \
  -H "Content-Type: application/json" \
  -d '{"msgtype":"text","text":{"content":"PhotoBridge 告警"}}'
```

**方式 C：Server酱（推送到微信个人）**

```bash
curl -sS -X POST "https://sctapi.ftqq.com/YOUR_KEY.send" \
  -d "title=PhotoBridge 告警" \
  -d "desp=检查发现异常"
```

**方式 D：Telegram Bot**

```bash
curl -sS -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d "chat_id=<CHAT_ID>" \
  -d "text=PhotoBridge 告警"
```

**方式 E：邮件（需要 MTA，麻烦）**

```bash
sudo apt install -y mailutils
# 需要配置 SMTP，个人用户不推荐
```

#### 9.5.3 定时执行

```bash
crontab -e

# 每 10 分钟检查一次，异常时推送到 webhook
*/10 * * * * WEBHOOK_URL="https://..." /opt/photobridge-cloud/healthcheck.sh > /dev/null 2>&1

# 每天 9 点发一次状态报告
0 9 * * * /opt/photobridge-cloud/healthcheck.sh 2>&1 | mail -s "PhotoBridge 日报" you@example.com
```

> ⚠️ **告警设计要点**：健康检查**只在异常时告警**（脚本如此设计）。
> 否则你会被正常状态的通知淹没，最后忽略所有通知——这是告警设计最常见的失败模式。

#### 9.5.4 外部监控（可选，更可靠）

**为什么需要外部监控**：如果**整个服务器挂了**，服务器上的脚本也没法告警。

| 服务 | 免费额度 | 说明 |
|---|---|---|
| **UptimeRobot** | 50 个监控，5 分钟间隔 | 最简单，网页配置 |
| **Better Stack** | 10 个监控，3 分钟间隔 | 界面漂亮 |
| **Healthchecks.io** | 20 个 check | 支持"心跳"模式（脚本主动上报） |
| Cloudflare Health Checks | 需付费 | 集成度高 |

**配置 UptimeRobot**：

```
1. 注册 uptimerobot.com
2. Add New Monitor
3. Monitor Type: HTTPS
4. Friendly Name: PhotoBridge
5. URL: https://photos.example.com/healthz
6. Monitoring Interval: 5 minutes
7. Alert Contacts: 填你的邮箱
8. 保存
```

> **★ 注意**：用外部监控时，`/healthz` **不要**依赖电脑端在线。
> 否则电脑一关机你就收到"服务挂了"的假警报。
> `/healthz` 只应检查 **relay 自身 + 数据库**，不检查隧道。

#### 9.5.5 电脑端监控（Windows）

```powershell
# 检查程序是否在运行
$proc = Get-Process PhotoBridge -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host "OK PhotoBridge 运行中 (PID $($proc.Id), 内存 $([math]::Round($proc.WorkingSet64/1MB,1)) MB)"
} else {
    Write-Host "FAIL PhotoBridge 未运行"
}

# 检查最近 5 分钟日志有没有 ERROR
$recent = Select-String -Path "D:\Apps\PhotoBridge\data\logs\agent.log" -Pattern "ERROR" |
    Where-Object { $_.Line -match (Get-Date -Format "yyyy-MM-dd") } | Select-Object -Last 5
if ($recent) { Write-Host "有错误日志:"; $recent } else { Write-Host "OK 无近期错误" }

# 检查隧道状态（看日志最后一次隧道建立时间）
Select-String -Path "D:\Apps\PhotoBridge\data\logs\agent.log" -Pattern "隧道已建立" | Select-Object -Last 3
```

**电脑端也可以主动上报**（可选）：程序每小时向云端 `/api/devices/{id}/heartbeat` 上报一次，
这样云端能知道电脑端的健康状况（而不只是"隧道连着"）。

### 9.6 §9 完成检查清单

| # | 检查项 | 验证 |
|---|---|---|
| 1 | 会看 `docker stats` | relay 内存 80–150 MB |
| 2 | 会看 `df -h` | 磁盘 < 70% |
| 3 | 会确认隧道在线 | §9.2.1 四种方法至少会一种 |
| 4 | 知道日志在哪 | §9.3 |
| 5 | 会查带宽用量 | §9.4.1 |
| 6 | healthcheck.sh 能跑 | `./healthcheck.sh` 返回 0 |
| 7 | 告警通道已配置 | webhook 能收到消息 |
| 8 | crontab 已配置 | `crontab -l` |
| 9 | 外部监控已配置（可选） | UptimeRobot 有监控 |
| 10 | 知道"电脑离线"不等于"服务挂了" | `/healthz` 不检查隧道 |
