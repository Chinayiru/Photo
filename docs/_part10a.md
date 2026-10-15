
---

## 10. 故障排查手册

> **怎么用这一章**：按**症状**查找，不要按原因查找。
> 每个场景都是「症状 → 可能原因 → 排查步骤 → 解决方案」四段式。
>
> **排查心法**：先确定**哪一段断了**，再深挖。
> ```
> 手机 ──①──> 云端(Caddy) ──②──> relay ──③──> 隧道 ──④──> 电脑端 ──⑤──> 照片文件
> ```
> 每一段都有自己的验证命令（见 §10.0）。**不要瞎猜，逐段验证。**

### 10.0 通用排查工具箱（★ 先看这里）

#### 10.0.1 逐段验证命令

| 段 | 验证什么 | 命令 | 通过标准 |
|---|---|---|---|
| **①** | 手机到 Caddy | 手机浏览器开 `https://域名/healthz` | 显示 JSON |
| **①** | DNS 解析 | `nslookup 域名 8.8.8.8` | 返回 VPS IP |
| **①** | 端口开放 | `curl -m 5 http://域名/healthz` | 有响应（301 到 https） |
| **②** | Caddy 到 relay | 服务器上 `docker compose exec caddy wget -qO- http://relay:8000/healthz` | 返回 JSON |
| **③** | relay 与隧道 | `docker compose logs --tail=50 relay \| grep -i hello` | 有 Agent 握手 |
| **④** | 电脑端在线 | 电脑上 `curl http://127.0.0.1:8787/api/system/ping` | 返回 JSON |
| **④** | 隧道连接 | 电脑端日志有 `隧道已建立` | ✅ |
| **⑤** | 照片文件可读 | 电脑上 `curl "http://127.0.0.1:8787/api/timeline?limit=1"` | 返回照片列表 |

#### 10.0.2 一条命令定位断点（在电脑上执行）

```powershell
Write-Host "=== PhotoBridge 链路诊断 ===" -ForegroundColor Cyan

# 1. 本地服务
Write-Host "`n[1] 本地服务 127.0.0.1:8787" -ForegroundColor Yellow
try {
    $r = Invoke-RestMethod "http://127.0.0.1:8787/api/system/ping" -TimeoutSec 5
    Write-Host "    OK $($r | ConvertTo-Json -Compress)" -ForegroundColor Green
} catch { Write-Host "    FAIL $($_.Exception.Message)" -ForegroundColor Red }

# 2. 进程
Write-Host "`n[2] 进程" -ForegroundColor Yellow
$p = Get-Process PhotoBridge -ErrorAction SilentlyContinue
if ($p) { Write-Host "    OK PID $($p.Id), 内存 $([math]::Round($p.WorkingSet64/1MB,1)) MB" -ForegroundColor Green }
else { Write-Host "    FAIL 进程未运行" -ForegroundColor Red }

# 3. 云端可达
Write-Host "`n[3] 云端可达" -ForegroundColor Yellow
$domain = "photos.example.com"     # ★ 改成你的域名
try {
    $r = Invoke-WebRequest "https://$domain/healthz" -TimeoutSec 10 -UseBasicParsing
    Write-Host "    OK HTTP $($r.StatusCode)" -ForegroundColor Green
} catch { Write-Host "    FAIL $($_.Exception.Message)" -ForegroundColor Red }

# 4. 隧道日志
Write-Host "`n[4] 隧道状态（最近日志）" -ForegroundColor Yellow
$log = "D:\Apps\PhotoBridge\data\logs\agent.log"    # ★ 改成你的路径
if (Test-Path $log) {
    Select-String -Path $log -Pattern "隧道已建立|连接断开|重连" | Select-Object -Last 3 |
        ForEach-Object { Write-Host "    $($_.Line)" }
} else { Write-Host "    ! 日志文件不存在: $log" -ForegroundColor Red }

# 5. 本机 IP
Write-Host "`n[5] 本机 IPv4" -ForegroundColor Yellow
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne "127.0.0.1" } |
    ForEach-Object { Write-Host "    $($_.IPAddress)  ($($_.InterfaceAlias))" }

Write-Host "`n=== 诊断完成 ===" -ForegroundColor Cyan
```

---

### 场景 1：手机打不开网站

**症状**：手机浏览器打开 `https://域名` 一直转圈、显示"无法访问此网站"或"连接超时"。

**可能原因**（按概率排序）：

| # | 原因 | 概率 |
|---|---|---|
| 1 | DNS 没生效或配错 | 高 |
| 2 | 防火墙没开 80/443 | 高 |
| 3 | Caddy 容器没起来 | 中 |
| 4 | 证书签发失败 | 中 |
| 5 | 云服务器挂了/欠费 | 低 |
| 6 | 手机网络问题（本地代理/VPN） | 低 |

**排查步骤**：

```powershell
# 步骤 1：DNS 是否解析到你的 VPS
nslookup photos.example.com 8.8.8.8
# 预期：返回你的 VPS IP

# 步骤 2：端口是否开放（从你的电脑测）
Test-NetConnection photos.example.com -Port 443
Test-NetConnection photos.example.com -Port 80
# 预期：TcpTestSucceeded : True

# 步骤 3：HTTP 是否响应
curl.exe -v -m 10 https://photos.example.com/healthz
```

```bash
# 步骤 4：服务器上检查容器
ssh deploy@198.51.100.42
cd /opt/photobridge-cloud
docker compose ps
# 预期：pb-caddy Up，pb-relay Up (healthy)

# 步骤 5：检查 ufw
sudo ufw status
# 预期：22/80/443 都是 ALLOW

# 步骤 6：检查 Caddy 日志
docker compose logs --tail=50 caddy
# 找 "certificate obtained successfully" 或错误
```

**解决方案**：

| 排查发现 | 解决 |
|---|---|
| DNS 没生效 | 等 10 分钟，或检查 A 记录配置（§3.2） |
| 解析到错的 IP | 改 A 记录，清本地 DNS 缓存 `ipconfig /flushdns` |
| 443 端口不通 | `sudo ufw allow 443/tcp && sudo ufw allow 80/tcp` |
| Caddy 没起来 | `docker compose up -d caddy`，看日志报什么错 |
| Caddy 起来了但证书失败 | 见**场景 5** |
| 容器都在跑但外网不通 | 检查云服务商的安全组（有些服务商有额外防火墙） |
| 手机装了 VPN/代理 | 关掉 VPN 再试 |

---

### 场景 2：显示"电脑离线"

**症状**：手机打开网站能登录，但设备列表显示 🔴 离线；或打开后提示"电脑不在线"。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | 电脑关机/睡眠了 | 电脑屏幕是不是黑的 |
| 2 | Agent 程序没启动 | 托盘有没有图标 |
| 3 | 隧道断了（网络问题） | 电脑端日志有"连接断开" |
| 4 | `agent_token` 失效/被撤销 | 日志有 401/403 |
| 5 | 企业/学校网络拦 WebSocket | 日志反复"连接失败" |
| 6 | 云端 relay 挂了 | 云端容器状态 |
| 7 | 电脑端程序崩溃 | 进程不在 |

**排查步骤**：

```powershell
# 步骤 1：电脑开着吗？进程在吗？
Get-Process PhotoBridge -ErrorAction SilentlyContinue
# 没有输出 = 程序没跑

# 步骤 2：本地服务正常吗？
curl.exe -sS http://127.0.0.1:8787/api/system/ping
# 有 JSON = 程序在跑

# 步骤 3：看隧道日志（★ 最关键）
Get-Content "D:\Apps\PhotoBridge\data\logs\agent.log" -Tail 50
```

**日志对照表**：

| 日志内容 | 原因 | 解决 |
|---|---|---|
| `隧道已建立` 但没有更新的时间戳 | 可能卡死了 | 重启程序 |
| `连接云端 wss://... 失败: getaddrinfo failed` | DNS 解析失败 | 检查网络、换 DNS |
| `连接云端 ... 失败: [WinError 10061] 目标计算机积极拒绝` | 云端没起来 | 检查云端容器 |
| `HTTP 401 Unauthorized` | **`agent_token` 失效** | 重新注册设备（见下） |
| `HTTP 403 Forbidden` | 设备被撤销 | 在云端重新启用 |
| `WebSocket 握手失败: 502` | Caddy 没转发对 | 检查 Caddyfile 的 `/agent/ws` 规则 |
| `连接超时` 反复出现 | 网络被拦（企业网） | 见**场景 6** |
| `正在重连... (第 N 次)` | 正常退避重连 | 等；N 大说明网络持续不通 |
| 完全没有新日志 | 程序卡死或没启动 | 重启程序 |

```bash
# 步骤 4：云端看这台设备的状态
cd /opt/photobridge-cloud
docker compose logs --tail=50 relay | grep -i "dev_xxx\|agent"
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
c.row_factory = sqlite3.Row
for r in c.execute('SELECT device_id, name, last_seen, revoked FROM devices'):
    print(dict(r))
"
```

**解决方案**：

| 情况 | 解决 |
|---|---|
| 电脑关机了 | 开机（这是架构固有，见 [11](11-风险登记册与决策记录.md) R-002） |
| 程序没跑 | 启动 `PhotoBridge.exe`，或检查自启配置（§5.6） |
| 程序崩溃了 | 看 `data\logs\` 的崩溃日志，重启 |
| `agent_token` 失效 | ★ 见下方「重新注册设备」 |
| 企业网拦 WebSocket | 见**场景 6** |
| 云端 relay 挂了 | `docker compose restart relay` |
| 电脑睡眠 | 电源设置改为"从不睡眠"（★ 推荐） |

**★ 重新注册设备（`agent_token` 失效时）**：

```powershell
# 1. 备份配置
Copy-Item "D:\Apps\PhotoBridge\backend\config.toml" "D:\Apps\PhotoBridge\backend\config.toml.bak"

# 2. 清空 token 和 device_id
notepad "D:\Apps\PhotoBridge\backend\config.toml"
# 把 agent_token = "" 和 device_id = "" 都清空

# 3. 重启程序，会重新走注册流程
Get-Process PhotoBridge -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Process "D:\Apps\PhotoBridge\PhotoBridge.exe"

# 4. 手机需要重新配对（因为设备 ID 变了）
```

**★ 防止电脑睡眠**（推荐，否则电脑一睡着就离线）：

```powershell
# 用管理员 PowerShell
powercfg /change standby-timeout-ac 0      # 交流电下永不睡眠
powercfg /change hibernate-timeout-ac 0    # 永不休眠
powercfg /change monitor-timeout-ac 20     # 显示器 20 分钟关（省电，不影响程序）
powercfg /change disk-timeout-ac 0         # 硬盘永不关闭

# 验证
powercfg /query SCHEME_CURRENT SUB_SLEEP
```

---

### 场景 3：上传很慢

**症状**：手机上传一张照片要几十秒甚至几分钟。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **手机信号差**（4G/5G 弱） | 测手机网速 |
| 2 | **家宽上行小** | 测家里上传速度 |
| 3 | **走了云端而非直连** | 看状态图标是 🔵 还是 🟢 |
| 4 | **分片大小不合适** | 看日志的分片大小 |
| 5 | 云 VPS 带宽小 | 看云端流量监控 |
| 6 | 手机后台限速/省电模式 | 看手机设置 |
| 7 | 照片太大（HEIC 单张 5–10 MB） | 看文件大小 |

**排查步骤**：

```powershell
# 步骤 1：测家里上行速度（★ 最关键）
# 打开 https://www.speedtest.net/ 或 https://fast.com/，看【上传】速度

# 步骤 2：看当前走的是直连还是云端
# 手机上：看状态栏图标 🟢 还是 🔵
# 电脑上：看日志有没有来自手机的局域网请求
Get-Content "D:\Apps\PhotoBridge\data\logs\agent.log" -Tail 50 | Select-String "192.168"
```

**★ 速度对照表**（上传一张 4 MB 照片）：

| 路径 | 瓶颈 | 理论耗时 | 实测预期 |
|---|---|---|---|
| 🟢 **局域网直连** | 内网（千兆） | < 1 秒 | 1–2 秒 |
| 🔵 走云端（家宽上行 30 Mbps） | 家宽上行 | ≈ 1 秒 | 2–5 秒 |
| 🔵 走云端（家宽上行 10 Mbps） | 家宽上行 | ≈ 3 秒 | 5–10 秒 |
| 🔵 走云端（手机 4G 上行 5 Mbps） | **手机上行** | ≈ 6 秒 | 10–20 秒 |
| 🔵 走云端（手机信号弱，1 Mbps） | 手机上行 | ≈ 32 秒 | 30–60 秒 |

> **结论**：**上传慢 90% 是手机信号问题**，不是你的服务器问题。
> 云端只是转发管道（[VPS选购建议.md](../VPS选购建议.md) 的核心结论）。

**解决方案**：

| 情况 | 解决 |
|---|---|
| 手机信号差 | 换 WiFi 上传；或等信号好的时候传 |
| **在家却走了云端** | ★ 检查直连探测（**场景 18**）——这是最值得优化的 |
| 家宽上行 < 10 Mbps | 考虑升级宽带套餐；或接受现状（[11](11-风险登记册与决策记录.md) R-008） |
| 分片太小 | 走云端分片 2 MB、局域网 8 MB（[设计文档.md](../设计文档.md) §7.2），确认配置没写错 |
| 手机省电模式 | 关掉省电模式；上传时保持屏幕常亮 |
| 云 VPS 带宽小 | 换更高带宽的套餐（但通常不是瓶颈） |

**验证分片大小是否正确**：

```powershell
Select-String -Path "D:\Apps\PhotoBridge\backend\config.toml" -Pattern "chunk_size"
# 预期：chunk_size_lan = 8388608 (8MB)，chunk_size_cloud = 2097152 (2MB)
```

---

### 场景 4：缩略图加载不出来

**症状**：照片墙显示灰色方块、破图图标，或一直转圈。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **索引还没跑完** | 看索引进度 |
| 2 | 缩略图生成失败 | 看 `thumb_state` 字段 |
| 3 | **HEIC 解码失败**（pillow-heif 没装好） | 只有 HEIC 挂，JPG 正常 |
| 4 | 缩略图缓存目录没权限 | 看目录权限 |
| 5 | 磁盘满了 | `df -h` |
| 6 | 图片文件损坏 | 直接打开原图看看 |
| 7 | 前端缓存问题 | 换浏览器/清缓存 |

**排查步骤**：

```powershell
# 步骤 1：索引跑完了吗？
curl.exe -sS http://127.0.0.1:8787/api/system/info | ConvertFrom-Json | Select-Object media_count, index_state
# 预期：media_count ≈ 4952，index_state = "idle"

# 步骤 2：缩略图状态分布（★ 关键）
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
print('thumb_state 分布:')
for r in c.execute('SELECT thumb_state, COUNT(*) FROM media GROUP BY thumb_state'):
    print(f'  {r[0]}: {r[1]}')
"
# 预期：ok 占绝大多数，failed 应该很少

# 步骤 3：看失败的具体是哪些
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
for r in c.execute(\"SELECT rel_path, ext FROM media WHERE thumb_state='failed' LIMIT 20\"):
    print(r)
"
# ★ 如果全是 .heic/.heif → 是 HEIC 解码问题（步骤 4）
# ★ 如果各种格式都有 → 可能是文件本身问题

# 步骤 4：验证 HEIC 支持
python -c "import pillow_heif; pillow_heif.register_heif_opener(); print('OK', pillow_heif.__version__)"

# 步骤 5：手动测一张 HEIC 能不能生成缩略图
python -c "
import pillow_heif
pillow_heif.register_heif_opener()
from PIL import Image
import glob
files = glob.glob(r'D:\Photos\**\*.heic', recursive=True)[:1]
if files:
    img = Image.open(files[0])
    print('HEIC 打开成功:', img.size, img.mode)
    img.thumbnail((320,320))
    img.convert('RGB').save(r'H:\temp\test_thumb.jpg', 'JPEG')
    print('缩略图生成成功')
else:
    print('没找到 HEIC 文件')
"

# 步骤 6：缩略图目录
Get-ChildItem "D:\Apps\PhotoBridge\data\thumbs" | Measure-Object
# 预期：有几千个文件
```

**解决方案**：

| 排查发现 | 解决 |
|---|---|
| 索引还在跑 | 等（4952 张约 3 分钟） |
| `failed` 全是 HEIC | ★ `pip install --upgrade pillow-heif`，重启；打包版检查 `_internal\pillow_heif\libheif.dll` 是否存在 |
| `failed` 少量随机 | 文件本身损坏，跳过即可 |
| `pending` 很多但不动了 | 索引器卡住了，点托盘「重新扫描」 |
| 缩略图目录空 | 没有写权限，检查 `data\thumbs` 权限 |
| 磁盘满 | 清理（见**场景 7**） |
| 前端显示问题 | 强制刷新（Ctrl+Shift+R）；手机上清缓存 |
| 单个文件打不开但缩略图在 | 原图损坏 |

**重新生成失败的缩略图**：

```powershell
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
n = c.execute(\"UPDATE media SET thumb_state='pending' WHERE thumb_state='failed'\").rowcount
c.commit()
print(f'已重置 {n} 条')
"
# 然后点托盘「重新扫描」
```

---

### 场景 5：证书报错

**症状**：浏览器显示"您的连接不是私密连接"、"NET::ERR_CERT_AUTHORITY_INVALID"、"证书无效"。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **域名解析错**（指向了别的 IP） | `dig` 看解析 |
| 2 | **80 端口被占**（Let's Encrypt 验证失败） | 看 Caddy 日志 |
| 3 | **Let's Encrypt 限流**（反复失败） | 日志有 `too many failed authorizations` |
| 4 | Caddy 还没签完 | 等 1–2 分钟 |
| 5 | Caddyfile 里域名写错 | 看配置文件 |
| 6 | `caddy_data` 卷没挂载 | 每次重启都重签 |
| 7 | 服务器时间不对 | `timedatectl` |

**排查步骤**：

```bash
# 步骤 1：看 Caddy 日志（★ 最重要）
cd /opt/photobridge-cloud
docker compose logs --tail=100 caddy | grep -iE "certificate|error|challenge|obtain"
```

**日志对照表**：

| 日志内容 | 原因 | 解决 |
|---|---|---|
| `certificate obtained successfully` | ✅ 成功 | 检查浏览器是不是缓存 |
| `obtaining certificate` 卡住 | 在等 ACME 挑战 | 检查 80 端口 |
| `challenge failed` / `could not get certificate` | 验证失败 | 看下面 |
| `dial tcp: lookup ... no such host` | DNS 没生效 | 等 DNS（§3.4） |
| `too many failed authorizations` | ★ **触发限流** | **必须等一周** |
| `connection refused` on port 80 | 80 被占 | 见下方 |
| `no such file or directory: /data/caddy` | 卷没挂 | 检查 compose |

```bash
# 步骤 2：域名解析对不对
dig +short photos.example.com @8.8.8.8
# 必须返回你的 VPS IP

# 步骤 3：80 端口是否被别的程序占用
sudo ss -tlnp | grep -E ':80|:443'
# 预期：只有 docker-proxy 占用 80/443

# 步骤 4：服务器时间对不对（★ 时间错会导致 ACME 失败）
timedatectl
# 预期：System clock synchronized: yes

# 步骤 5：caddy_data 卷有没有挂对
docker compose exec caddy ls -la /data/caddy/certificates/ 2>/dev/null || echo "目录不存在"
docker inspect pb-caddy --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

**解决方案**：

| 情况 | 解决 |
|---|---|
| DNS 没生效 | 等，然后 `docker compose restart caddy` |
| 80 端口被占（如 Nginx/Apache） | `sudo systemctl stop nginx apache2 && sudo systemctl disable nginx apache2`，然后重启 caddy |
| 服务器时间不对 | `sudo timedatectl set-ntp true && sudo systemctl restart systemd-timesyncd` |
| **触发限流** | ★ **必须等 7 天**。用 staging 环境验证配置（见下） |
| `caddy_data` 没挂载 | 检查 compose 的 volumes |
| Caddyfile 域名写错 | 改成正确的，`caddy reload` |

**★ 用 Let's Encrypt 测试环境避免限流**（调试期强烈推荐）：

```caddyfile
photos.example.com {
    # 加这一行，用 staging 环境（不会消耗生产配额）
    tls {
        ca https://acme-staging-v02.api.letsencrypt.org/directory
    }
    ...
}
```

> staging 证书**不被浏览器信任**（会报错），但能验证**配置是否正确**。
> 确认没问题后，删掉 `tls` 块，`caddy reload` 就会签正式证书。

**限流后怎么办**：

```bash
# 1. 停掉 Caddy，避免继续失败
docker compose stop caddy

# 2. 等 7 天（Let's Encrypt 的失败配额是每域名每小时 5 次、每周 5 次失败）

# 3. 7 天后，先用 staging 验证配置（改 Caddyfile 加 tls { ca ...staging... }）
docker compose up -d caddy
docker compose logs -f caddy
# 看到 "certificate obtained successfully" 说明配置对了

# 4. 删掉 staging 配置，正式签
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile
```

**手动验证证书**：

```powershell
echo | openssl s_client -connect photos.example.com:443 -servername photos.example.com 2>$null |
    openssl x509 -noout -issuer -subject -dates

# 预期：
# issuer=C = US, O = Let's Encrypt, CN = R11
# subject=CN = photos.example.com
# notAfter=...（90 天后）
```

---

### 场景 6：WebSocket 连不上

**症状**：电脑端日志反复 `连接失败`、`握手失败`、`502`；手机端设备一直显示离线，但云端网站能打开。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **Caddy 的 `/agent/ws` 规则写错** | 云端日志没有 agent 连接 |
| 2 | Caddy 的 `handle` 顺序不对 | 请求被静态规则截走 |
| 3 | **响应缓冲没关**（`flush_interval`） | 能连上但消息不通 |
| 4 | **超时设置太短** | 连上几秒后断 |
| 5 | **Cloudflare 代理**（100 秒空闲超时） | 用 CF 才出现 |
| 6 | 企业/学校防火墙拦 WebSocket | 换网络就好 |
| 7 | relay 的 `--workers` > 1 | 请求被路由到没有隧道的进程 |

**排查步骤**：

```bash
# 步骤 1：云端有没有收到 agent 连接
cd /opt/photobridge-cloud
docker compose logs --tail=100 relay | grep -iE "agent|ws|hello"
# 完全没输出 = 请求根本没到 relay → 是 Caddy 的问题

# 步骤 2：从服务器本地测 WebSocket 路径
curl -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
     -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
     https://photos.example.com/agent/ws
# 预期：HTTP/1.1 101 Switching Protocols
```

**★ 返回码对照表**：

| 返回 | 原因 | 解决 |
|---|---|---|
| `101 Switching Protocols` | ✅ 正常 | 问题在别处 |
| `404 Not Found` | Caddy 没有 `/agent/ws` 规则，或被静态规则截走 | 检查 Caddyfile 的 handle 顺序 |
| `200 OK`（返回 HTML） | ★ 被静态文件规则截走了 | 把 `handle /agent/ws` **移到前面** |
| `502 Bad Gateway` | Caddy 连不上 relay | 检查服务名、network |
| `504 Gateway Timeout` | relay 没响应 | 检查 relay 日志 |
| `400 Bad Request` | 握手头被改了 | 检查有没有 `header_up` 改坏 Upgrade 头 |

```bash
# 步骤 3：Caddy 能不能连到 relay
docker compose exec caddy wget -qO- http://relay:8000/healthz
# 有 JSON = 网络通

# 步骤 4：检查 Caddyfile 的 handle 顺序
grep -n "handle" /opt/photobridge-cloud/Caddyfile
# ★ /agent/ws 必须在通配 handle 之前

# 步骤 5：检查 relay 的 worker 数
docker compose exec relay ps aux | grep uvicorn
# ★ 必须是 --workers 1（多 worker 会导致隧道状态丢失）
```

**解决方案**：

| 情况 | 解决 |
|---|---|
| Caddyfile 顺序错 | 把 `handle /agent/ws {...}` 移到所有其他 `handle` 之前，`caddy reload` |
| 缓冲没关 | 在 `reverse_proxy` 里加 `flush_interval -1` |
| 超时太短 | 加 `transport http { read_timeout 0 \n write_timeout 0 }` |
| `--workers` 不是 1 | 改成 1，重建镜像 |
| **Cloudflare 代理** | ★ 见下方 |
| 企业网拦 WebSocket | 换网络；或用 443 端口（本项目已用 443，通常能过） |

**★ Cloudflare 代理的问题**：

如果你在域名前面加了 Cloudflare 代理（小云朵是橙色的），会有两个问题：

| 问题 | 说明 |
|---|---|
| **免费版 100 秒空闲超时** | WebSocket 空闲 100 秒会被 CF 断开。本项目有心跳（25 秒），**通常不会触发**，但网络抖动时可能 |
| 需要正确配置 | CF 默认支持 WebSocket，但要确保没开"Rocket Loader"之类的优化 |

**解决**：

```
1. 登录 Cloudflare
2. 选择你的域名 → DNS
3. 找到那条 A 记录
4. ★ 把云朵点成【灰色】（DNS only），绕过 CF 代理
5. 等 1–2 分钟生效
```

> **为什么要绕过**：本项目**云端零存储**是隐私底线（[11](11-风险登记册与决策记录.md) ADR-003）。
> 加上 Cloudflare 后，照片流量会经过第三方，与这条底线冲突。

**验证 WebSocket 是否真的通了**：

```powershell
python -c "
import asyncio, websockets

async def test():
    uri = 'wss://photos.example.com/agent/ws'
    try:
        async with websockets.connect(uri, open_timeout=10) as ws:
            print('OK WebSocket 连接成功')
            await ws.send(b'test')
            print('OK 能发送')
    except Exception as e:
        print(f'FAIL {type(e).__name__}: {e}')

asyncio.run(test())
"
```

---

### 场景 7：磁盘满了

**症状**：服务写入失败、`database or disk is full`、容器重启、`df -h` 显示 100%。

**可能原因**：

| # | 原因 | 怎么查 |
|---|---|---|
| 1 | **Docker 日志没限制**（最常见） | `du -sh /var/lib/docker` |
| 2 | 系统日志（journald）太大 | `journalctl --disk-usage` |
| 3 | 缩略图缓存太大（电脑端） | 看 `data\thumbs` 大小 |
| 4 | 上传临时区没清理 | 看 `data\uploads_tmp` |
| 5 | 备份文件堆积 | 看 `/opt/backups` |
| 6 | Docker 镜像/悬空层堆积 | `docker system df` |
| 7 | apt 缓存 | `du -sh /var/cache/apt` |

**排查步骤**：

```bash
# 步骤 1：看整体
df -h

# 步骤 2：找大目录（★ 最有用）
sudo du -h --max-depth=1 / 2>/dev/null | sort -hr | head -15

# 步骤 3：Docker 占用
docker system df
sudo du -sh /var/lib/docker/* 2>/dev/null | sort -hr | head -10

# 步骤 4：日志占用
journalctl --disk-usage
sudo du -sh /var/log

# 步骤 5：容器日志大小
sudo du -sh /var/lib/docker/containers/*/*-json.log 2>/dev/null | sort -hr | head -5
```

**解决方案**：

```bash
# ── 1. 清理容器日志（★ 先做这个，最常见）────────────────
sudo truncate -s 0 /var/lib/docker/containers/*/*-json.log

# 然后配置日志轮转（防止再发生，§4.1.2）
sudo tee /etc/docker/daemon.json > /dev/null <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
EOF
sudo systemctl restart docker

# ── 2. 清理 journald ──────────────────────────────────────
sudo journalctl --vacuum-size=100M
sudo journalctl --vacuum-time=7d

# 永久限制
sudo sed -i 's/^#SystemMaxUse=.*/SystemMaxUse=100M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald

# ── 3. 清理 Docker 悬空资源 ───────────────────────────────
docker system prune -f              # 悬空镜像、停止的容器、未使用网络
docker image prune -a -f            # ★ 会删所有未使用镜像，慎用
docker builder prune -f             # 构建缓存

# ── 4. 清理 apt 缓存 ──────────────────────────────────────
sudo apt clean
sudo apt autoremove -y

# ── 5. 清理旧备份 ─────────────────────────────────────────
ls -lh /opt/backups/
find /opt/backups -name "*.tar.gz" -mtime +30 -delete
find /opt/backups -name "cloud.db.*" -mtime +30 -delete
```

**电脑端清理**：

```powershell
# 看各目录大小
Get-ChildItem "D:\Apps\PhotoBridge\data" -Directory | ForEach-Object {
    $size = (Get-ChildItem $_.FullName -Recurse -File | Measure-Object Length -Sum).Sum
    Write-Host ("{0,-20} {1,10:N1} MB" -f $_.Name, ($size/1MB))
}

# 清理缩略图缓存（可重建）
Remove-Item "D:\Apps\PhotoBridge\data\thumbs\*" -Recurse -Force
# ★ 然后需要重新扫描生成

# 清理上传临时区（★ 确认没有正在进行的上传）
Get-ChildItem "D:\Apps\PhotoBridge\data\uploads_tmp" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } | Remove-Item -Force
```

**★ 预防措施**（做完清理后一定要配）：

| 措施 | 命令 |
|---|---|
| Docker 日志轮转 | §4.1.2 |
| journald 限制 | `SystemMaxUse=100M` |
| 定期清理备份 | backup.sh 里已有 `find -mtime +30 -delete` |
| 磁盘告警 | healthcheck.sh 里已有磁盘检查 |
| 上传临时区自动清理 | 程序应该有 TTL 清理（确认配置） |

---

### 场景 8：内存爆了

**症状**：relay 容器被 OOM Killer 杀掉、`dmesg` 有 `Out of memory: Killed process`、容器反复重启。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **流控失效**（大文件全读进内存） | relay 内存持续增长 |
| 2 | 并发上传太多 | 看连接数 |
| 3 | 请求被恶意刷 | 看流量异常 |
| 4 | 内存泄漏 | 内存只增不减 |
| 5 | VPS 内存太小（512 MB） | `free -h` |

**排查步骤**：

```bash
# 步骤 1：看内存现状
free -h
docker stats --no-stream

# 步骤 2：看有没有 OOM Killer 记录（★ 关键证据）
sudo journalctl -k --since "24 hours ago" | grep -i "out of memory\|killed process"
sudo dmesg -T | grep -i "oom\|killed" | tail -20

# 步骤 3：看容器重启历史
docker inspect pb-relay --format '{{.RestartCount}} 次重启'
docker compose ps

# 步骤 4：看 relay 内存趋势
while true; do
  docker stats --no-stream --format "{{.Name}} {{.MemUsage}} {{.CPUPerc}}" pb-relay
  sleep 5
done
```

**★ 内存基准值**（[09](09-开发计划与任务分解.md) C1/C7 DoD）：

| 场景 | 正常内存 | 危险值 |
|---|---|---|
| 空闲 | 80–120 MB | — |
| 传 100 MB 文件 | 增量 **< 50 MB** | > 200 MB |
| 传 2 GB 文件 | 峰值 **< 200 MB** | > 400 MB |
| 24 小时运行 | 无增长 | 持续增长 |

**解决方案**：

```bash
# ── 立即缓解 ──────────────────────────────────────────────
# 1. 重启 relay（临时）
docker compose restart relay

# 2. 加 swap（小内存 VPS 的救命稻草）
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -h
```

```yaml
# 3. 限制容器内存（加到 docker-compose.yml 的 relay 服务）
services:
  relay:
    # ...
    deploy:
      resources:
        limits:
          memory: 512M
        reservations:
          memory: 128M
```

**根本解决**：

| 原因 | 解决 |
|---|---|
| **流控失效** | ★ 检查 `credit_window` 是否生效（应 8 MB）。这是 [11](11-风险登记册与决策记录.md) R-006 的核心风险 |
| 并发太高 | 限制 `MAX_UPLOAD_CONCURRENCY=3`（已在 .env 模板里） |
| 恶意刷流量 | 检查 `RATE_LIMIT_PER_CLIENT=120`；用 fail2ban 封 IP |
| 内存泄漏 | 需要改代码；先加 swap + 内存限制兜底 |
| VPS 内存太小 | 升级到 2 GB（$35.99/年） |

**验证流控是否生效**（★ 这是关键测试）：

```bash
# 在手机上开始上传一个 100 MB+ 的文件，然后在服务器上监控
watch -n 2 'docker stats --no-stream --format "{{.MemUsage}}" pb-relay'
```

| 观察结果 | 结论 |
|---|---|
| 内存稳定在 150–250 MB | ✅ 流控生效 |
| 内存随文件增长到 500 MB+ | ❌ **流控失效**，必须修 |

---

### 场景 9：数据库锁

**症状**：日志有 `database is locked`、`SQLITE_BUSY`；请求变慢或失败。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **WAL 模式没开** | `PRAGMA journal_mode` |
| 2 | 长事务没提交 | 看有没有卡住的连接 |
| 3 | 并发写太多 | 看请求量 |
| 4 | 数据库文件在慢速存储上 | — |
| 5 | 有别的进程也在写 | 检查 |
| 6 | 磁盘满（见场景 7） | `df -h` |

**排查步骤**：

```bash
# 步骤 1：检查 WAL 模式（★ 应该返回 wal）
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
print('journal_mode:', c.execute('PRAGMA journal_mode').fetchone()[0])
print('busy_timeout:', c.execute('PRAGMA busy_timeout').fetchone()[0])
print('synchronous:', c.execute('PRAGMA synchronous').fetchone()[0])
"
```

**推荐配置**：

| PRAGMA | 推荐值 | 为什么 |
|---|---|---|
| `journal_mode` | **`wal`** | ★ 允许读写并发（读不阻塞写） |
| `busy_timeout` | **`5000`**（5 秒） | 遇到锁时等待而不是立即失败 |
| `synchronous` | **`normal`** | WAL 下性能与安全的平衡点 |
| `foreign_keys` | **`on`** | 外键约束生效 |

```bash
# 步骤 2：看 WAL 状态
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
for r in c.execute('PRAGMA wal_checkpoint'):
    print('wal_checkpoint:', r)
"

# 步骤 3：看 WAL 文件大小（过大说明 checkpoint 不及时）
ls -lh /opt/photobridge-cloud/data/
```

**解决方案**：

```bash
# ── 1. 确认 WAL 已开启（应该在代码里做）──────────────────
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA busy_timeout=5000')
print('已设置')
"

# ── 2. 手动 checkpoint（清理 WAL）─────────────────────────
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
print(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
"

# ── 3. 如果数据库锁死了，重启 relay ───────────────────────
docker compose restart relay

# ── 4. 极端情况：备份 + 重建（★ 最后手段）────────────────
docker compose stop relay
cp data/cloud.db /opt/backups/cloud.db.before-rebuild
sqlite3 data/cloud.db ".dump" | sqlite3 data/cloud.db.new
mv data/cloud.db.new data/cloud.db
rm -f data/cloud.db-wal data/cloud.db-shm
docker compose start relay
```

**电脑端同样检查**：

```powershell
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
print('journal_mode:', c.execute('PRAGMA journal_mode').fetchone()[0])
print('busy_timeout:', c.execute('PRAGMA busy_timeout').fetchone()[0])
"
```

**根本解决**：

| 措施 | 说明 |
|---|---|
| 开启 WAL | 代码里连接时执行 `PRAGMA journal_mode=WAL` |
| 设置 busy_timeout | `PRAGMA busy_timeout=5000` |
| 单写者模式 | 所有写操作串行化（用一个队列） |
| 缩短事务 | 不在事务里做网络/文件 IO |
| 索引器批量提交 | 每 100 条提交一次，不要一个大事务跑完 4952 条 |

---

### 场景 10：手机端一直重连

**症状**：手机界面频繁显示"连接中"、状态在 🟢🔵🔴 之间跳、上传反复中断。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **直连探测超时太短** | 局域网下也跳 🔵 |
| 2 | 手机在 WiFi/4G 之间切换 | 走远了/走近了 |
| 3 | 电脑端隧道不稳 | 云端日志有频繁重连 |
| 4 | 手机省电模式杀后台 | 切后台就断 |
| 5 | Service Worker 缓存了旧版本 | 换浏览器试 |
| 6 | 云端负载高 | 看云端 CPU |

**排查步骤**：

```powershell
# 步骤 1：看电脑端隧道稳不稳
Get-Content "D:\Apps\PhotoBridge\data\logs\agent.log" -Tail 100 |
    Select-String "隧道已建立|连接断开|重连" | Select-Object -Last 10
# 如果频繁"连接断开" → 是隧道问题（场景 2/6）
# 如果一直稳定 → 是手机端问题
```

```bash
# 步骤 2：看云端有没有频繁重连
cd /opt/photobridge-cloud
docker compose logs --since 1h relay | grep -c "重连\|disconnect"
# > 10 次/小时 = 不稳
```

**解决方案**：

| 情况 | 解决 |
|---|---|
| 隧道不稳 | 见**场景 2** 和**场景 6** |
| 直连探测太敏感 | 调大探测超时（1.5 秒是 [09](09-开发计划与任务分解.md) C5 DoD 值，可按需调整） |
| 手机省电模式 | 关闭省电模式；把网站加入"不受限制"列表 |
| 后台被杀 | 上传时保持屏幕常亮（`wakeLock` 已实现）；别切到别的 App |
| WiFi/4G 切换 | 正常现象，会自动恢复 |
| SW 缓存旧版 | 手机 Chrome → 设置 → 网站设置 → 清除该站数据 |
| 云端负载高 | 看 `docker stats`，可能需要升级 VPS |

**★ 让手机不要杀后台（安卓）**：

```
1. 设置 → 应用 → Chrome
2. 电池 → 选择「不受限制」/「允许后台活动」
3. 或者在最近任务里长按 Chrome → 锁定
4. 小米/华为等国产 ROM 还需要：
   - 设置 → 电池 → 应用省电策略 → Chrome → 无限制
   - 设置 → 应用管理 → Chrome → 自启动 → 允许
```
