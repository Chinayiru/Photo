
---

## 11. 日常运维清单

### 11.1 每日（5 分钟）

| # | 检查项 | 命令 / 操作 | 通过标准 |
|---|---|---|---|
| 1 | 网站能打开 | 手机或电脑开 `https://域名` | 正常显示 |
| 2 | 电脑端在线 | 看设备列表 🟢 | 在线 |
| 3 | 容器在跑 | `docker compose ps` | 都 Up |
| 4 | 磁盘空间 | `df -h /` | < 70% |
| 5 | relay 内存 | `docker stats --no-stream pb-relay` | 80–150 MB |
| 6 | 备份任务跑了 | `Get-ScheduledTaskInfo -TaskName "PhotoBridge 备份"` | LastTaskResult = 0 |

> **偷懒版**：跑一次 `./healthcheck.sh`，全绿就没事。

```bash
# 每天早上花 1 分钟跑这个
cd /opt/photobridge-cloud && ./healthcheck.sh
```

### 11.2 每周（15 分钟）

| # | 检查项 | 命令 | 通过标准 |
|---|---|---|---|
| 1 | 磁盘趋势 | `df -h` + `du -sh /var/lib/docker` | 增长可控 |
| 2 | 带宽用量 | §9.4.1 的 traffic 查询 | < 100 GB/月 |
| 3 | 证书有效期 | `healthcheck.sh` 里的证书检查 | > 14 天 |
| 4 | 日志有没有错误 | `docker compose logs --since 7d relay \| grep -i error` | 无严重错误 |
| 5 | fail2ban 封了谁 | `sudo fail2ban-client status sshd` | 正常 |
| 6 | 备份文件完整性 | 看备份目录大小 + 抽查一个文件 | 能打开 |
| 7 | 云端数据库备份 | `cp data/cloud.db /opt/backups/...` | 成功 |
| 8 | 电脑端数据库备份 | 用 §8.2 的脚本 | 成功 |

```bash
# 每周一条命令
cd /opt/photobridge-cloud
echo "=== 周报 $(date +%F) ==="
echo "--- 磁盘 ---"; df -h / | tail -1
echo "--- Docker ---"; docker system df
echo "--- 容器 ---"; docker compose ps --format "table {{.Name}}\t{{.Status}}"
echo "--- 隧道重连次数（近7天）---"; docker compose logs --since 168h relay 2>/dev/null | grep -c "重连\|disconnect" || echo 0
echo "--- 错误数（近7天）---"; docker compose logs --since 168h relay 2>/dev/null | grep -ci error || echo 0
echo "--- fail2ban ---"; sudo fail2ban-client status sshd | tail -5
echo "--- 备份 ---"; ls -lh /opt/backups/ | tail -5
```

### 11.3 每月（30 分钟）

| # | 检查项 | 操作 | 通过标准 |
|---|---|---|---|
| 1 | **恢复演练** | §8.6 演练 A/B/C | 能恢复 |
| 2 | 系统更新 | `sudo apt update && sudo apt upgrade -y` | 成功 |
| 3 | Docker 镜像更新 | `docker compose pull caddy && docker compose up -d caddy` | 成功 |
| 4 | 清理旧备份 | backup.sh 自动做的 | — |
| 5 | 磁盘深度清理 | §10 场景 7 | 空间充裕 |
| 6 | 密码/密钥轮换评估 | 是否该换 `SECRET_KEY` | — |
| 7 | 检查依赖安全 | `pip list --outdated`（电脑端） | — |
| 8 | 更新风险登记册 | [11](11-风险登记册与决策记录.md) §2 | — |
| 9 | 续费提醒 | VPS 到期日、域名到期日 | — |

### 11.4 磁盘空间检查（★ 最重要）

**为什么重要**：磁盘满了会导致 SQLite 写入失败、服务崩溃（[11](11-风险登记册与决策记录.md) R-022）。

```bash
# 云端
df -h /
df -i /
sudo du -h --max-depth=1 / 2>/dev/null | sort -hr | head -10
docker system df
```

```powershell
# 电脑端
Get-PSDrive -PSProvider FileSystem | Select-Object Name,
    @{n='已用GB';e={[math]::Round($_.Used/1GB,1)}},
    @{n='剩余GB';e={[math]::Round($_.Free/1GB,1)}}

# PhotoBridge 各目录
Get-ChildItem "D:\Apps\PhotoBridge\data" -Directory | ForEach-Object {
    $s = (Get-ChildItem $_.FullName -Recurse -File -EA SilentlyContinue | Measure-Object Length -Sum).Sum
    Write-Host ("{0,-20} {1,10:N1} MB" -f $_.Name, ($s/1MB))
}
```

**阈值与动作**：

| 使用率 | 状态 | 动作 |
|---|---|---|
| < 70% | ✅ 正常 | 无需动作 |
| 70–85% | ⚠️ 注意 | 清理日志/备份 |
| **> 85%** | ❌ 危险 | **立即清理**（SQLite 会写失败） |
| 100% | 💀 故障 | 服务已不可用 |

### 11.5 证书到期检查

**Caddy 会自动续期**（在证书到期前 30 天开始尝试），但**你必须验证它真的在工作**。

```bash
# 方法 1：用 healthcheck.sh（推荐，每天自动检查）
/opt/photobridge-cloud/healthcheck.sh

# 方法 2：手动看证书剩余天数
echo | openssl s_client -connect photos.example.com:443 -servername photos.example.com 2>/dev/null |
    openssl x509 -noout -dates

# 方法 3：看 Caddy 日志里的续期记录
docker compose logs caddy | grep -iE "certificate obtained|renew"
```

**★ 续期失败时的日志特征**：

| 日志 | 含义 |
|---|---|
| `certificate obtained successfully` | ✅ 签/续成功 |
| `renewing certificate` | 正在续期 |
| `could not get certificate` | ❌ 失败，需要处理 |
| `too many failed authorizations` | ❌ 触发限流 |

> **★ 到期前 7 天还是没续**：按 §10 场景 5 处理。

```bash
# 验证证书链完整
openssl s_client -connect photos.example.com:443 -servername photos.example.com -showcerts </dev/null 2>/dev/null |
    grep -c "BEGIN CERTIFICATE"
# 预期：≥ 2（叶子证书 + 中间证书）
```

### 11.6 日志轮转检查

```bash
# Docker 日志（应该在 daemon.json 里限制）
cat /etc/docker/daemon.json
# 预期：max-size 10m, max-file 3

# 实际日志大小
sudo du -sh /var/lib/docker/containers/*/*-json.log | sort -hr | head -5
# 单个应该 < 10 MB（×3 = 30 MB）

# journald
journalctl --disk-usage
# 预期：< 100 MB

# Caddy 访问日志（Caddyfile 里配了 roll_size 10mb roll_keep 5）
ls -lh /opt/photobridge-cloud/caddy_data/*.log*
# 预期：单个 < 10 MB，最多 5 个
```

```powershell
# 电脑端日志
Get-ChildItem "D:\Apps\PhotoBridge\data\logs" | Sort-Object Length -Descending |
    Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,2)}}
# 单个应该 < 10 MB
```

**如果日志没轮转**：

```bash
# Docker：检查 daemon.json 并重启 docker
sudo systemctl restart docker

# journald：限制大小
sudo journalctl --vacuum-size=100M
sudo sed -i 's/^#SystemMaxUse=.*/SystemMaxUse=100M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
```

### 11.7 月度运维脚本

创建 `/opt/photobridge-cloud/monthly-check.sh`：

```bash
#!/usr/bin/env bash
# 每月运维检查
set -uo pipefail
cd /opt/photobridge-cloud

echo "=========================================="
echo " PhotoBridge 月度检查  $(date '+%F %T')"
echo "=========================================="

echo -e "\n【1】磁盘"
df -h / | tail -1
echo "inode:"; df -i / | tail -1

echo -e "\n【2】Docker 占用"
docker system df

echo -e "\n【3】容器状态"
docker compose ps --format "table {{.Name}}\t{{.Status}}"

echo -e "\n【4】证书有效期"
echo | openssl s_client -connect "${DOMAIN:-localhost}:443" -servername "${DOMAIN:-localhost}" 2>/dev/null \
    | openssl x509 -noout -dates 2>/dev/null || echo "  无法获取"

echo -e "\n【5】本月流量"
docker compose exec -T relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
r = c.execute(\"SELECT SUM(bytes_in), SUM(bytes_out) FROM traffic WHERE ts >= date('now','start of month')\").fetchone()
bi, bo = (r[0] or 0)/1048576, (r[1] or 0)/1048576
print(f'  入站 {bi:.0f} MB / 出站 {bo:.0f} MB / 合计 {(bi+bo)/1024:.2f} GB')
" 2>/dev/null || echo "  查询失败"

echo -e "\n【6】错误日志统计（近30天）"
docker compose logs --since 720h relay 2>/dev/null | grep -ci "error" || echo "  0"

echo -e "\n【7】备份目录"
ls -lh /opt/backups/ 2>/dev/null | tail -5 || echo "  无备份目录"

echo -e "\n【8】系统更新（只列出）"
apt list --upgradable 2>/dev/null | tail -n +2 | head -10 || echo "  无"

echo -e "\n【9】运行时长"
uptime

echo -e "\n【10】SSH 登录失败统计"
sudo grep -c "Failed password" /var/log/auth.log 2>/dev/null || echo "  0"

echo -e "\n=========================================="
echo " 检查完成"
echo "=========================================="
```

```bash
chmod +x monthly-check.sh
./monthly-check.sh

# 每月 1 号 9 点自动跑
crontab -e
# 0 9 1 * * /opt/photobridge-cloud/monthly-check.sh > /opt/photobridge-cloud/logs/monthly-$(date +\%Y\%m).log 2>&1
```

### 11.8 日常运维检查清单

| 频率 | 项数 | 耗时 | 关键动作 |
|---|---|---|---|
| 每日 | 6 | 5 分钟 | 跑 healthcheck.sh |
| 每周 | 8 | 15 分钟 | 看磁盘/带宽/证书/备份 |
| 每月 | 9 | 30 分钟 | ★ **恢复演练** + 系统更新 |
| 每季 | 3 | 1 小时 | 密码轮换评估、依赖升级、风险登记册复盘 |
| 每年 | 2 | — | VPS/域名续费、架构复盘 |

**★ 千万别跳过的三项**：

| # | 项目 | 为什么 |
|---|---|---|
| 1 | **每月恢复演练** | 没有演练过的备份等于没有备份 |
| 2 | **每日磁盘检查** | 磁盘满是本项目最常见的故障 |
| 3 | **每周证书检查** | 证书过期 = 网站完全不可用 |

### 11.9 运维日历（建议贴墙上）

| 时间 | 做什么 |
|---|---|
| 每天 09:00 | 跑 `healthcheck.sh`（或看 webhook 有没有告警） |
| 每周日 | 跑 §11.2 的周报命令 |
| 每月 1 号 | 跑 `monthly-check.sh` + **恢复演练** |
| 每季首月 | 密码轮换评估、依赖升级检查、复盘风险登记册 |
| 到期前 30 天 | VPS 续费、域名续费 |
| 到期前 14 天 | 证书检查（healthcheck 会自动告警） |
