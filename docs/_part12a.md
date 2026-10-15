
---

## 12. 应急处理

> **应急处理的原则**：**先止损，再定位，最后修复**。
> 不要一上来就深挖原因，先把服务恢复或把损失控制住。
>
> **★ 最重要的一条**：无论发生什么，**先确认 `D:\Photos` 的照片还在不在**。
> 其他一切（云端数据库、缩略图、容器、证书）都能重建，照片不能。

### 12.1 云服务器被入侵

**症状**：CPU 异常高、流量暴涨、有陌生进程/登录、网站被改。

#### 12.1.1 ★ 立即行动（按顺序）

```bash
# ── 第 1 步：确认谁在线 ──────────────────────────────────
who
sudo last -20

# 发现陌生 IP，立即封掉
sudo ufw deny from <恶意IP>
# 或用 fail2ban 立即封
sudo fail2ban-client set sshd banip <恶意IP>

# ★ 立即禁用密码登录（如果还没禁）
sudo sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' \
    /etc/ssh/sshd_config.d/99-hardening.conf
sudo sshd -t && sudo systemctl restart ssh

# ── 第 2 步：检查后门 ────────────────────────────────────
# SSH 密钥（★ 最关键）
cat ~/.ssh/authorized_keys
sudo cat /root/.ssh/authorized_keys
# ★ 有不认识的 key → 删掉它，然后换密钥、改密码

# crontab（挖矿木马最爱）
crontab -l; sudo crontab -l; cat /etc/crontab
sudo ls -la /etc/cron.*/

# 自启动服务
systemctl list-units --type=service --state=running
sudo ls -la /etc/systemd/system/

# 可疑进程
ps auxf | head -50

# ── 第 3 步：抓证据（可选，如果要做取证）────────────────
mkdir -p /root/incident-$(date +%Y%m%d)
sudo cp /var/log/auth.log /root/incident-$(date +%Y%m%d)/
ps auxf > /root/incident-$(date +%Y%m%d)/ps.txt
sudo ss -tunap > /root/incident-$(date +%Y%m%d)/netstat.txt
crontab -l > /root/incident-$(date +%Y%m%d)/crontab.txt 2>&1
```

#### 12.1.2 判断严重程度

| 情况 | 严重程度 | 处理 |
|---|---|---|
| 只是 SSH 暴力破解**失败**（`lastb` 很多，`last` 没有） | 🟢 低 | fail2ban 已在挡，无需动作 |
| 有陌生**成功**登录（`last` 里有） | 🟠 中 | 封 IP + 改密码 + 换密钥 |
| 有陌生进程 / crontab 任务 | 🔴 高 | **重装系统**（最稳妥） |
| 数据被加密 / 勒索 | 💀 极高 | 重装 + 从备份恢复 |

#### 12.1.3 修复方案

**方案 A：清理（只适用于轻度入侵）**

```bash
# 1. 杀掉恶意进程
sudo kill -9 <PID>

# 2. 删掉恶意 crontab
crontab -r; sudo crontab -r
sudo rm -f /etc/cron.d/<恶意文件>

# 3. 删掉恶意 systemd 服务
sudo systemctl disable --now <恶意服务>
sudo rm -f /etc/systemd/system/<恶意服务>.service
sudo systemctl daemon-reload

# 4. 删掉恶意 SSH key（编辑 authorized_keys，删掉不认识的）

# 5. 改密码 + 换密钥
passwd

# 6. 更新系统
sudo apt update && sudo apt upgrade -y

# 7. 重启
sudo reboot
```

**方案 B：重装（★ 推荐，最干净）**

```bash
# 1. 备份必要数据（★ 只备份数据，不备份系统文件！）
#    /opt/photobridge-cloud/data/cloud.db
#    /opt/photobridge-cloud/.env
#    /opt/photobridge-cloud/Caddyfile
#    /opt/photobridge-cloud/docker-compose.yml
#    /opt/photobridge-cloud/caddy_data/（证书，可选）

# 2. 从云服务商控制台重装系统（Ubuntu 24.04）

# 3. 按 §2 重新做系统初始化

# 4. 按 §4 重新部署

# 5. ★ 换掉所有密钥：
#    - SSH 密钥
#    - SECRET_KEY / JWT_SECRET（openssl rand -hex 32）
#    - ADMIN_PASSWORD
#    - agent_token（电脑端重新注册）

# 6. 恢复 cloud.db
```

#### 12.1.4 ★ 入侵后的必做清单

| # | 动作 | 为什么 |
|---|---|---|
| 1 | 换 SSH 密钥 | 旧的可能已泄露 |
| 2 | 换 `SECRET_KEY` / `JWT_SECRET` | 会话令牌全部失效（强制重新登录） |
| 3 | 换 `ADMIN_PASSWORD` | — |
| 4 | 电脑端重新注册（换 `agent_token`） | 旧令牌可能已泄露 |
| 5 | 踢掉所有手机客户端 | `DELETE /api/devices/{id}/clients/{cid}` |
| 6 | 检查 `audit.log` 有没有异常删除 | 确认照片没被动过 |
| 7 | **检查照片本体** | ★ 最重要 |
| 8 | 检查云服务商账单 | 防止被用来挖矿产生费用 |

#### 12.1.5 怎么防止再次发生

| 措施 | 命令 | 状态 |
|---|---|---|
| 禁用密码登录 | §2.7 | 部署时已做 |
| 禁用 root 登录 | §2.7 | 部署时已做 |
| 只开 22/80/443 | §2.8 | 部署时已做 |
| fail2ban | §2.9 | 部署时已做 |
| 自动安全更新 | §2.11 | 部署时已做 |
| 改用非标准 SSH 端口（可选） | `Port 2222` | 可选 |
| 用 SSH 密钥 + 密码短语 | — | 推荐 |
| 定期检查 | §11 | 持续 |

#### 12.1.6 云服务器到期 / 服务商跑路

**症状**：网站突然打不开，SSH 也连不上；收到服务商邮件说欠费/关停。

| 情况 | 处理 |
|---|---|
| **只是到期** | 登录服务商控制台续费，一般有几天宽限期 |
| 服务器被删了 | 数据没了（云端只有 `cloud.db`，可重建）→ 重新开一台，按 §2–§4 重来 |
| 服务商跑路 | 换一家（换 IP 后要改 DNS，见 §3.2） |
| **★ 关键** | 照片在**你自己的电脑**上，云端丢了不影响照片 |

**★ 提前做的准备**（降低损失）：

```bash
# 1. 记录部署配置（放到你自己的电脑/密码管理器里）
cat /opt/photobridge-cloud/.env > ~/photobridge-backup/env.txt
cat /opt/photobridge-cloud/Caddyfile >> ~/photobridge-backup/env.txt

# 2. 定期下载 cloud.db 到本地
scp deploy@198.51.100.42:/opt/photobridge-cloud/data/cloud.db ./cloud.db.backup

# 3. 记住续费日期，设日历提醒（★ 到期前 30 天）
```

> **重建云端需要多久**：按 §2–§4 走一遍，熟练后约 **1 小时**。
> 因为云端零存储（[11](11-风险登记册与决策记录.md) ADR-003），丢云端 = 丢"账号和配对记录"，不丢照片。
