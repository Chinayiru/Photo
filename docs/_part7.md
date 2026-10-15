
---

## 7. 升级与回滚

### 7.1 版本号约定

四个版本号，**必须能对应上**：

| 组件 | 在哪看 | 示例 |
|---|---|---|
| **云端 relay** | `GET /api/system/info` | `0.3.1` |
| **电脑端 Agent** | 托盘「关于」或 `GET /api/system/info` | `0.3.1` |
| **隧道协议版本** | `protocol.py` 里的 `PROTOCOL_VERSION` | `1` |
| **前端** | 页面底部或 `/api/system/info` | `0.3.1` |

**兼容规则**（[设计文档.md](../设计文档.md) §4）：

| 组合 | 是否兼容 |
|---|---|
| 三个组件版本完全相同 | ✅ 最佳 |
| 协议版本相同，应用版本差一个小版本 | ✅ 兼容（协议向后兼容） |
| **协议版本不同** | ❌ **不兼容**，隧道拒绝握手 |

> **实用原则**：**升级时先升云端，再升电脑端**。云端通常向后兼容旧协议，反之不行。

### 7.2 云端升级

#### 7.2.1 标准升级流程

```bash
# ── 0. 升级前先备份（★ 必做）──────────────────────────────
cd /opt/photobridge-cloud
mkdir -p /opt/backups
cp data/cloud.db /opt/backups/cloud.db.$(date +%Y%m%d-%H%M%S)
ls -lh /opt/backups/

# ── 1. 拉取新代码 ─────────────────────────────────────────
cd /opt/photobridge-cloud/relay
git pull                       # 或手动 scp 覆盖
git log --oneline -5

# ── 2. 记录当前版本（回滚要用）────────────────────────────
docker compose exec relay python -c "import version; print(version.__version__)"

# ── 3. 重新构建镜像 ───────────────────────────────────────
cd /opt/photobridge-cloud
docker compose build relay

# ── 4. 重建容器 ───────────────────────────────────────────
docker compose up -d relay

# ── 5. 看日志确认启动成功 ─────────────────────────────────
docker compose logs -f --tail=50 relay
```

**预期看到**：

```
INFO:     PhotoBridge Relay v0.3.1 启动
INFO:     [db] 数据库已连接: /data/cloud.db
INFO:     [db] 迁移检查: schema_version 1 → 2
INFO:     [db] ✓ 迁移完成
INFO:     [server] 监听 0.0.0.0:8000
```

#### 7.2.2 用预构建镜像升级

```bash
cd /opt/photobridge-cloud
cp data/cloud.db /opt/backups/cloud.db.$(date +%Y%m%d-%H%M%S)

# 改 compose 里的 tag: image: photobridge/relay:0.3.1
nano docker-compose.yml

docker compose pull relay
docker compose up -d relay
docker compose logs -f --tail=50 relay
```

#### 7.2.3 数据库迁移

**★ 硬约束**（[00](00-文档索引与开发流程.md) §3.3 ⑤）：**改表必须给迁移脚本，不允许删库重建**。

迁移是**自动的**：relay 启动时检查 `schema_version`，低于代码期望版本就自动迁移。

```bash
# 查看迁移状态
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
print('schema_version:', c.execute('PRAGMA user_version').fetchone()[0])
print('表:', [r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")])
"
```

**迁移失败时**：

```bash
docker compose stop relay
cp /opt/backups/cloud.db.YYYYMMDD-HHMMSS data/cloud.db
cd relay && git checkout <旧版本tag>
cd .. && docker compose up -d --build relay
```

#### 7.2.4 Caddyfile 升级

```bash
# 1. 改配置
nano /opt/photobridge-cloud/Caddyfile

# 2. ★ 先校验语法（避免 Caddy 起不来）
docker run --rm -v /opt/photobridge-cloud/Caddyfile:/etc/caddy/Caddyfile:ro \
  caddy:2 caddy validate --config /etc/caddy/Caddyfile

# 3. 热重载（不中断连接）
docker compose exec caddy caddy reload --config /etc/caddy/Caddyfile

# 或直接重启
docker compose restart caddy
```

> `caddy reload` 是热重载：不中断正在进行的连接，比 `restart` 更温和。

#### 7.2.5 升级后验证

```bash
curl -sS -o /dev/null -w "%{http_code}\n" https://photos.example.com/healthz
curl -sS https://photos.example.com/api/system/info
docker compose logs --tail=30 relay | grep -i "agent\|hello"
docker compose ps
```

### 7.3 前端升级

#### 7.3.1 前端产物放哪（两种方案）

| 方案 | 谁托管 | Caddyfile | 优点 | 缺点 |
|---|---|---|---|---|
| **A. Caddy 静态托管** | Caddy | `root * /srv/web` + `file_server` | 静态文件不占 relay 资源；Caddy 缓存/压缩更好 | 每次升级要传文件 |
| **B. relay 托管** | relay | 删掉静态段，转发到 relay | 一个镜像搞定 | 静态文件占 relay 的 worker |

**本项目推荐 A**：`--workers 1` 的 relay 不应浪费在处理静态文件上。

#### 7.3.2 方案 A：更新 Caddy 托管的产物

**在开发机上构建**：

```powershell
cd D:\Photos\frontend
pnpm install
pnpm build
dir dist
```

**上传到服务器**：

```powershell
ssh deploy@198.51.100.42 "rm -rf /opt/photobridge-cloud/web/*"
scp -r D:\Photos\frontend\dist\* deploy@198.51.100.42:/opt/photobridge-cloud/web/
```

**服务器上确认**：

```bash
ls -la /opt/photobridge-cloud/web/
# 应看到 index.html、assets/、manifest.webmanifest、sw.js
```

**★ Service Worker 缓存问题**

PWA 的 SW 会缓存旧版本，用户可能还看到旧页面。

| 措施 | 做法 |
|---|---|
| 文件名带哈希 | Vite 默认给 `assets/index-abc123.js` 加哈希，天然绕过缓存 |
| `sw.js` 不缓存 | Caddyfile 里给 `sw.js` 加 `Cache-Control: no-cache` |
| 提示用户刷新 | 检测到新 SW 时弹「有新版本，点击刷新」 |

**给 sw.js 加不缓存头**（加进 Caddyfile）：

```caddyfile
    # Service Worker 不缓存，保证能拿到新版本
    @sw path /sw.js
    header @sw Cache-Control "no-cache, no-store, must-revalidate"

    # 带哈希的静态资源长期缓存
    @hashed path /assets/*
    header @hashed Cache-Control "public, max-age=31536000, immutable"
```

> **不需要重启容器**——Caddy 直接读文件系统。

#### 7.3.3 方案 B：relay 托管

```powershell
scp -r D:\Photos\frontend\dist\* deploy@198.51.100.42:/opt/photobridge-cloud/relay/static/
```

```bash
cd /opt/photobridge-cloud
docker compose build relay
docker compose up -d relay
```

> ⚠️ 如果 compose 里把 `./relay:/app` 挂载了（开发期配置），静态文件改动**不需要重建镜像**，重启即可。

#### 7.3.4 前端升级验证

```powershell
curl.exe -sS -o NUL -w "%{http_code}\n" https://photos.example.com/
curl.exe -sS https://photos.example.com/ | Select-String -Pattern "version|v0\."
# 手机上：长按刷新按钮 → 「清空缓存并硬性重新加载」
```

### 7.4 电脑端升级

#### 7.4.1 标准升级流程

```powershell
# ── 0. 升级前备份（★ 必做）─────────────────────────────
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
New-Item -ItemType Directory -Force -Path "D:\backups" | Out-Null
Copy-Item "D:\Apps\PhotoBridge\data\photobridge.db" "D:\backups\photobridge.db.$ts"
Copy-Item "D:\Apps\PhotoBridge\backend\config.toml" "D:\backups\config.toml.$ts"

# ── 1. 退出程序（托盘右键 → 退出）──────────────────────
Get-Process PhotoBridge -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

# ── 2. 备份整个目录（最保险）────────────────────────────
Copy-Item -Recurse "D:\Apps\PhotoBridge" "D:\backups\PhotoBridge.$ts"

# ── 3. 替换 exe（保留 data 和 config）───────────────────
Copy-Item "D:\Photos\dist\PhotoBridge\PhotoBridge.exe" "D:\Apps\PhotoBridge\" -Force
Copy-Item -Recurse -Force "D:\Photos\dist\PhotoBridge\_internal" "D:\Apps\PhotoBridge\"

# ── 4. 启动 ─────────────────────────────────────────────
Start-Process "D:\Apps\PhotoBridge\PhotoBridge.exe"

# ── 5. 看日志确认 ───────────────────────────────────────
Get-Content "D:\Apps\PhotoBridge\data\logs\agent.log" -Tail 30
```

#### 7.4.2 ★ 升级时千万不能覆盖的东西

| 文件/目录 | 为什么 | 丢了会怎样 |
|---|---|---|
| `data\photobridge.db` | 索引数据库（4952 条 + EXIF + 缩略图状态） | 要重新全量索引，上传会话丢失 |
| `backend\config.toml` | 含 `agent_token`、库目录配置 | **要重新注册设备、手机重新配对** |
| `data\audit.log` | 审计日志 | 历史操作记录丢失 |
| `data\thumbs\` | 缩略图缓存 | 可重建，等几分钟 |
| `data\uploads_tmp\` | 上传临时区 | 未完成的上传丢失，要重传 |

**安全做法**：**只替换 `PhotoBridge.exe` 和 `_internal\`**，`data\` 和 `config.toml` 原地不动。

#### 7.4.3 电脑端数据库迁移

```powershell
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
print('schema_version:', c.execute('PRAGMA user_version').fetchone()[0])
print('media 行数:', c.execute('SELECT COUNT(*) FROM media').fetchone()[0])
"
```

**迁移失败时**：

```powershell
Get-Process PhotoBridge -ErrorAction SilentlyContinue
Copy-Item "D:\backups\photobridge.db.$ts" "D:\Apps\PhotoBridge\data\photobridge.db" -Force
Copy-Item "D:\backups\PhotoBridge.$ts\PhotoBridge.exe" "D:\Apps\PhotoBridge\" -Force
```

> ⚠️ `photobridge.db` 有 WAL 文件（`-wal`、`-shm`）。复制时**要么三个一起复制，要么先让程序正常退出**（退出时会 checkpoint）。

#### 7.4.4 开发模式升级（更简单）

```powershell
cd D:\Photos
.\.venv\Scripts\Activate.ps1
git pull
pip install -r backend\requirements.txt --upgrade
# 重启程序
```

### 7.5 回滚

#### 7.5.1 云端回滚

```bash
cd /opt/photobridge-cloud

# ── 方式 A：回滚 git 版本 ────────────────────────────────
cd relay
git log --oneline -10
git checkout <commit-hash>
cd ..

# ── 方式 B：回滚镜像 tag ─────────────────────────────────
# 改 docker-compose.yml: image: photobridge/relay:0.3.0

# ── 恢复数据库（如果新版本改了表结构）───────────────────
docker compose stop relay
cp /opt/backups/cloud.db.YYYYMMDD-HHMMSS data/cloud.db

# ── 重建启动 ─────────────────────────────────────────────
docker compose up -d --build relay
docker compose logs -f --tail=50 relay
```

**验证**：`curl -sS https://photos.example.com/api/system/info` 确认版本号变回旧版。

#### 7.5.2 电脑端回滚

```powershell
Get-Process PhotoBridge -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 2

Copy-Item "D:\backups\PhotoBridge.$ts\PhotoBridge.exe" "D:\Apps\PhotoBridge\" -Force
Copy-Item -Recurse -Force "D:\backups\PhotoBridge.$ts\_internal" "D:\Apps\PhotoBridge\"

Copy-Item "D:\backups\photobridge.db.$ts" "D:\Apps\PhotoBridge\data\photobridge.db" -Force
Remove-Item "D:\Apps\PhotoBridge\data\photobridge.db-wal" -ErrorAction SilentlyContinue
Remove-Item "D:\Apps\PhotoBridge\data\photobridge.db-shm" -ErrorAction SilentlyContinue

Start-Process "D:\Apps\PhotoBridge\PhotoBridge.exe"
```

#### 7.5.3 前端回滚

```bash
cd /opt/photobridge-cloud
rm -rf web/*
tar -xzf /opt/backups/web.YYYYMMDD.tar.gz -C web/
```

> **建议**：前端产物在服务器上也留一份 tar 备份。

### 7.6 版本兼容性说明

#### 7.6.1 协议版本不匹配

**症状**——电脑端日志：

```
[tunnel] 连接被拒绝: protocol_version_mismatch (云端=2, 本地=1)
```

云端日志：

```
[agent] 拒绝握手: client protocol 1 != server protocol 2
```

**原因**：`protocol.py` 里的 `PROTOCOL_VERSION` 不一致。这个文件**由云端和电脑端各拷一份**（[设计文档.md](../设计文档.md) §11），升级时容易漏掉一边。

**解决**：

| 场景 | 做法 |
|---|---|
| 云端已升到协议 2，电脑端还是 1 | **升级电脑端**（或让云端临时兼容协议 1） |
| 电脑端升到协议 2，云端还是 1 | **升级云端**（云端应优先支持新协议） |
| 想临时共存 | 云端同时接受协议 1 和 2（需代码支持） |

**★ 正确做法**：**先升云端，且云端要能同时兼容新旧协议**，然后逐个升电脑端。

#### 7.6.2 版本兼容矩阵

| 云端 | 电脑端 | 协议 | 结果 |
|---|---|---|---|
| 0.3.0 | 0.3.0 | 1/1 | ✅ |
| 0.3.1 | 0.3.0 | 1/1 | ✅ 云端向后兼容 |
| 0.3.1 | 0.3.1 | 1/1 | ✅ |
| 0.3.1 | 0.4.0 | 1/2 | ❌ 云端不认识协议 2 |
| 0.4.0 | 0.3.1 | 2/1 | ✅ 若云端保留协议 1 支持 |
| 0.4.0 | 0.4.0 | 2/2 | ✅ |

#### 7.6.3 检查所有组件版本

```powershell
# 电脑端
curl.exe -sS http://127.0.0.1:8787/api/system/info | ConvertFrom-Json | Select-Object version, protocol_version

# 云端
curl.exe -sS https://photos.example.com/api/system/info | ConvertFrom-Json | Select-Object version, protocol_version

# 通过云端看电脑端（验证整条链路）
curl.exe -sS https://photos.example.com/d/dev_xxx/api/system/info | ConvertFrom-Json
```

### 7.7 §7 完成检查清单

| # | 检查项 | 说明 |
|---|---|---|
| 1 | 升级前备份了 `cloud.db` | §7.2.1 步骤 0 |
| 2 | 升级前备份了 `photobridge.db` 和 `config.toml` | §7.4.1 步骤 0 |
| 3 | 云端升级后 `/healthz` 返回 200 | — |
| 4 | 云端升级后隧道重连成功 | 看日志 |
| 5 | 电脑端升级后托盘变 🟢 | — |
| 6 | 四个版本号已核对 | §7.6.3 |
| 7 | 回滚路径已验证（至少演练过一次） | — |
| 8 | 前端 Service Worker 已更新 | 手机强制刷新后是新版 |
