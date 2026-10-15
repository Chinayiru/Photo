
---

## 8. 备份与恢复

> **★ 本章最重要的一句话**：
> **照片本体（`D:\Photos`）是唯一不可再生的数据。其他所有东西——云端数据库、电脑端数据库、缩略图缓存、上传临时区——全部可以重建。**

### 8.1 数据分类：什么必须备份，什么可以重建

#### 8.1.1 ★ 必须备份（丢了就真丢了）

| # | 数据 | 位置 | 大小 | 丢了会怎样 | 备份频率 |
|---|---|---|---|---|---|
| **1** | **照片本体** | `D:\Photos` | **4952 个文件** | **★ 永久丢失，无法恢复** | **每天** |
| 2 | 云端数据库 | 服务器 `/opt/photobridge-cloud/data/cloud.db` | < 1 MB | 账号、设备、配对关系全丢。**要重新注册设备、手机重新配对** | 每周 |
| 3 | 电脑端数据库 | `data\photobridge.db` | 几 MB ~ 几十 MB | 索引全丢，要重新全量索引。**上传会话丢失** | 每周 |
| 4 | 电脑端配置 | `backend\config.toml` | < 10 KB | **`agent_token` 丢失 = 要重新注册设备、手机重新配对** | 配置变更时 |
| 5 | 审计日志 | `data\audit.log` | 小 | 删除/移动的历史记录丢失 | 每月 |
| 6 | Caddy 证书 | 服务器 `caddy_data/` | 小 | 会自动重签，但**频繁重签会触发限流** | 每周（可选） |

#### 8.1.2 可以重建（丢了不心疼）

| 数据 | 位置 | 重建方式 | 耗时 |
|---|---|---|---|
| **缩略图缓存** | `data\thumbs\` | 程序启动时自动重新生成 | 4952 张约 3–5 分钟 |
| **上传临时区** | `data\uploads_tmp\` | 用户重新上传 | 取决于用户 |
| 前端构建产物 | `frontend\dist\` | `pnpm build` | 30 秒 |
| Docker 镜像 | 服务器 | `docker compose build` | 1–2 分钟 |
| relay 源码 | 服务器 | 从代码仓库拉取 | 秒级 |
| Python 虚拟环境 | `.venv\` | `python -m venv` + `pip install` | 2 分钟 |
| 日志文件 | `data\logs\` | 会重新生成 | — |

#### 8.1.3 ★ 一句话总结

```
┌──────────────────────────────────────────────────────────────┐
│                                                               │
│   照片本体 = 不可再生   →  ★ 备份策略的核心                   │
│   其他一切 = 可再生     →  备份只是为了省事，不是为了保命      │
│                                                               │
└──────────────────────────────────────────────────────────────┘
```

> **推论**：如果只有时间做一件事，就做**照片的备份**（且必须**异地多份**）。
> 云端 `cloud.db` 丢了最多重新配对一次；照片丢了就是真的丢了。

### 8.2 备份策略建议（3-2-1 原则）

| 层级 | 内容 | 介质 | 频率 |
|---|---|---|---|
| **第 1 份** | 照片本体 | `D:\Photos`（工作副本） | 实时 |
| **第 2 份** | 照片本体 | **另一块物理硬盘**（如 `F:\相册备份`） | 每天 |
| **第 3 份** | 照片本体 | **异地**（移动硬盘带出门 / 云盘 / 另一台电脑） | 每周–每月 |
| 附加 | 数据库 + 配置 | 同第 2 份介质 | 每周 |

> ⚠️ **不要只靠"另一块硬盘在同一个机箱里"**：火灾、雷击、失窃会同时毁掉两份。
> 真正安全的备份**必须有一份在物理上离开这个房间**。

### 8.3 备份脚本

#### 8.3.1 Windows 批处理版本

创建 `D:\Photos\scripts\backup.bat`：

```batch
@echo off
REM ============================================================
REM  PhotoBridge 备份脚本（Windows）
REM  用途：备份照片本体 + 电脑端数据库 + 配置
REM  用法：双击运行，或由任务计划程序调用
REM ============================================================

setlocal enabledelayedexpansion

REM ── 配置区（★ 按你的实际情况修改）────────────────────────
set SRC_PHOTOS=D:\Photos
set SRC_APPDIR=D:\Apps\PhotoBridge
set DST_ROOT=F:\相册备份
set LOGFILE=%DST_ROOT%\_backup.log
set KEEP_DAYS=30

REM ── 时间戳 ────────────────────────────────────────────────
for /f "tokens=1-4 delims=/ " %%a in ('date /t') do set D=%%a%%b%%c
for /f "tokens=1-2 delims=:." %%a in ('echo %time%') do set T=%%a%%b
set TS=%D%-%T%

REM ── 检查源目录 ────────────────────────────────────────────
if not exist "%SRC_PHOTOS%" (
    echo [ERROR] 照片目录不存在: %SRC_PHOTOS%
    echo [%date% %time%] ERROR 源目录不存在: %SRC_PHOTOS% >> "%LOGFILE%"
    exit /b 1
)

REM ── 创建目标目录 ──────────────────────────────────────────
if not exist "%DST_ROOT%" mkdir "%DST_ROOT%"
if not exist "%DST_ROOT%\photos" mkdir "%DST_ROOT%\photos"
if not exist "%DST_ROOT%\db" mkdir "%DST_ROOT%\db"

echo ============================================================
echo  PhotoBridge 备份  %date% %time%
echo ============================================================
echo [%date% %time%] ===== 备份开始 ===== >> "%LOGFILE%"

REM ── 1. 备份照片本体 ───────────────────────────────────────
echo.
echo [1/3] 正在备份照片本体...
echo        源:   %SRC_PHOTOS%
echo        目标: %DST_ROOT%\photos
echo        这可能需要较长时间，请勿关闭窗口...
robocopy "%SRC_PHOTOS%" "%DST_ROOT%\photos" /MIR /R:2 /W:5 /NP /NDL /NFL /LOG+:"%LOGFILE%"
set RC=%ERRORLEVEL%

REM robocopy 返回码：0=无变化 1=有复制 2=有额外文件 3=1+2 ... 8 及以上才是真错误
if %RC% GEQ 8 (
    echo [ERROR] robocopy 失败，返回码 %RC%
    echo [%date% %time%] ERROR robocopy 返回码 %RC% >> "%LOGFILE%"
    exit /b %RC%
)

REM ── 2. 备份电脑端数据库与配置 ─────────────────────────────
echo.
echo [2/3] 正在备份数据库与配置...
if exist "%SRC_APPDIR%\data\photobridge.db" (
    copy /Y "%SRC_APPDIR%\data\photobridge.db" "%DST_ROOT%\db\photobridge.db" >nul
    echo        OK photobridge.db
) else (
    echo        ! 未找到 photobridge.db
)

if exist "%SRC_APPDIR%\backend\config.toml" (
    copy /Y "%SRC_APPDIR%\backend\config.toml" "%DST_ROOT%\db\config.toml" >nul
    echo        OK config.toml
) else (
    echo        ! 未找到 config.toml
)

REM ── 3. 清理过期日志 ───────────────────────────────────────
echo.
echo [3/3] 清理 %KEEP_DAYS% 天前的旧日志...
forfiles /P "%DST_ROOT%" /M "_backup_*.log" /D -%KEEP_DAYS% /C "cmd /c del @path" 2>nul

REM ── 完成 ──────────────────────────────────────────────────
echo.
echo ============================================================
echo  备份完成  %date% %time%
echo  返回码: %RC%
echo ============================================================
echo [%date% %time%] ===== 备份完成，返回码 %RC% ===== >> "%LOGFILE%"

endlocal
exit /b 0
```

**关键参数**：

| 参数 | 含义 |
|---|---|
| `/MIR` | **镜像模式**：目标与源完全一致（会**删除**目标端多余文件） |
| `/R:2 /W:5` | 失败重试 2 次，每次等 5 秒（默认重试上百万次会卡死） |
| `/NP /NDL /NFL` | 不显示进度百分比/目录名/文件名（日志更干净） |
| `/LOG+:` | 追加写日志 |
| `forfiles /D -30` | 删除 30 天前的文件 |

> ⚠️ **`/MIR` 的风险**：如果源目录被误删，**下次备份会把备份端的文件也删掉**！
> **更安全的替代**：用 `/E`（只增不减）+ 定期手动清理，或使用带版本历史的备份工具。

#### 8.3.2 Linux Shell 版本（服务器上备份云端数据）

创建 `/opt/photobridge-cloud/backup.sh`：

```bash
#!/usr/bin/env bash
# ============================================================
#  PhotoBridge 云端备份脚本（Linux）
#  用途：备份 cloud.db + Caddyfile + .env + 证书
#  用法：./backup.sh  或 crontab 定时调用
# ============================================================

set -euo pipefail

# ── 配置区 ────────────────────────────────────────────────
APP_DIR="/opt/photobridge-cloud"
BACKUP_DIR="/opt/backups"
KEEP_DAYS=30
TS=$(date +%Y%m%d-%H%M%S)
LOG="${BACKUP_DIR}/backup.log"

# ── 准备 ──────────────────────────────────────────────────
mkdir -p "${BACKUP_DIR}"
echo "[$(date '+%F %T')] ===== 备份开始 =====" >> "${LOG}"

# ── 1. 备份 cloud.db ──────────────────────────────────────
# ★ 直接 cp 一个正在写入的 SQLite 可能得到损坏的副本
#    正确做法是用 sqlite3 的 .backup 命令（在线备份）
if [ -f "${APP_DIR}/data/cloud.db" ]; then
    if command -v sqlite3 > /dev/null 2>&1; then
        sqlite3 "${APP_DIR}/data/cloud.db" ".backup '${BACKUP_DIR}/cloud.db.${TS}'"
        echo "[$(date '+%F %T')] OK cloud.db 已备份（sqlite3 .backup）" >> "${LOG}"
    else
        docker compose -f "${APP_DIR}/docker-compose.yml" stop relay > /dev/null 2>&1 || true
        cp "${APP_DIR}/data/cloud.db" "${BACKUP_DIR}/cloud.db.${TS}"
        docker compose -f "${APP_DIR}/docker-compose.yml" start relay > /dev/null 2>&1 || true
        echo "[$(date '+%F %T')] OK cloud.db 已备份（停服 cp）" >> "${LOG}"
    fi
else
    echo "[$(date '+%F %T')] ! 未找到 cloud.db" >> "${LOG}"
fi

# ── 2. 备份配置文件（.env 含密钥，注意权限）──────────────
tar -czf "${BACKUP_DIR}/config.${TS}.tar.gz" \
    -C "${APP_DIR}" Caddyfile docker-compose.yml .env \
    2>> "${LOG}" || echo "[$(date '+%F %T')] ! 配置打包失败" >> "${LOG}"
chmod 600 "${BACKUP_DIR}/config.${TS}.tar.gz"
echo "[$(date '+%F %T')] OK 配置文件已备份" >> "${LOG}"

# ── 3. 备份 Caddy 证书 ────────────────────────────────────
if [ -d "${APP_DIR}/caddy_data" ]; then
    tar -czf "${BACKUP_DIR}/caddy_data.${TS}.tar.gz" \
        -C "${APP_DIR}" caddy_data 2>> "${LOG}" || true
    echo "[$(date '+%F %T')] OK Caddy 证书已备份" >> "${LOG}"
fi

# ── 4. 清理过期备份 ───────────────────────────────────────
find "${BACKUP_DIR}" -name "cloud.db.*" -mtime +${KEEP_DAYS} -delete 2>/dev/null || true
find "${BACKUP_DIR}" -name "config.*.tar.gz" -mtime +${KEEP_DAYS} -delete 2>/dev/null || true
find "${BACKUP_DIR}" -name "caddy_data.*.tar.gz" -mtime +${KEEP_DAYS} -delete 2>/dev/null || true
echo "[$(date '+%F %T')] OK 已清理 ${KEEP_DAYS} 天前的备份" >> "${LOG}"

# ── 5. 磁盘占用报告 ───────────────────────────────────────
SIZE=$(du -sh "${BACKUP_DIR}" | cut -f1)
AVAIL=$(df -h "${BACKUP_DIR}" | awk 'NR==2 {print $4}')
echo "[$(date '+%F %T')] 备份目录占用: ${SIZE}，剩余空间: ${AVAIL}" >> "${LOG}"
echo "[$(date '+%F %T')] ===== 备份完成 =====" >> "${LOG}"

# ── 6. 输出最近 5 行（方便手动运行看结果）────────────────
tail -5 "${LOG}"
```

**安装并测试**：

```bash
cd /opt/photobridge-cloud
chmod +x backup.sh
sudo apt install -y sqlite3     # 让 .backup 方式可用

./backup.sh
ls -lh /opt/backups/
```

**设置每日自动执行**：

```bash
crontab -e
# 添加一行（每天凌晨 3:30 执行）
30 3 * * * /opt/photobridge-cloud/backup.sh > /dev/null 2>&1
crontab -l
```

#### 8.3.3 ★ 备份脚本的自检要点

| # | 自检项 | 为什么 |
|---|---|---|
| 1 | **备份后能恢复** | 没演练过的备份 = 没有备份（§8.5） |
| 2 | 备份目录在**另一块物理盘** | 同一块盘坏了两份都没 |
| 3 | 备份有**版本**（带时间戳） | 覆盖式备份会把好数据也覆盖成坏的 |
| 4 | 备份**失败会告警** | 静默失败的备份最危险（§9.5） |
| 5 | 定期**检查备份完整性** | 文件大小、能打开、能列出内容 |
| 6 | **异地一份** | 火灾/失窃场景 |

### 8.4 自动备份：Windows 任务计划程序

#### 8.4.1 图形界面配置步骤

```
1. Win + R → 输入 taskschd.msc → 回车
2. 右侧「创建任务」（不是"创建基本任务"，功能更全）
3. 【常规】标签
   - 名称: PhotoBridge 照片备份
   - 描述: 每天备份照片本体与数据库到 F 盘
   - ☑ 不管用户是否登录都要运行
   - ☑ 使用最高权限运行
   - 配置: Windows 10
4. 【触发器】标签 → 新建
   - 开始任务: 按预定计划
   - 设置: 每天
   - 开始: 03:00:00
   - ☑ 启用
   - 确定
5. 【操作】标签 → 新建
   - 操作: 启动程序
   - 程序或脚本: D:\Photos\scripts\backup.bat
   - 起始于: D:\Photos\scripts
   - 确定
6. 【条件】标签
   - ☐ 只有在计算机使用交流电源时才启动此任务（笔记本要取消勾选）
7. 【设置】标签
   - ☑ 允许按需运行任务
   - ☑ 如果任务失败，按以下频率重新启动: 10 分钟，最多 3 次
   - ☑ 如果任务运行时间超过以下时间，停止任务: 4 小时  ← ★ 防止卡死
   - 如果任务已在运行，则: 不启动新实例
8. 确定，输入密码（如果选"不管用户是否登录都要运行"）
```

#### 8.4.2 PowerShell 配置（一条命令搞定）

```powershell
# ★ 用管理员 PowerShell

$action = New-ScheduledTaskAction `
    -Execute "D:\Photos\scripts\backup.bat" `
    -WorkingDirectory "D:\Photos\scripts"

$trigger = New-ScheduledTaskTrigger -Daily -At "03:00"

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName "PhotoBridge 照片备份" `
    -Description "每天备份照片本体与数据库到 F 盘" `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -Force
```

#### 8.4.3 验证任务配置

```powershell
Get-ScheduledTask -TaskName "PhotoBridge 照片备份" | Select-Object TaskName, State

# 手动触发一次（测试）
Start-ScheduledTask -TaskName "PhotoBridge 照片备份"

# ★ 重点看 LastTaskResult，0 表示成功
Get-ScheduledTaskInfo -TaskName "PhotoBridge 照片备份" |
    Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime
```

**预期输出**：

```
TaskName              LastRunTime          LastTaskResult NextRunTime
--------              -----------          -------------- -----------
PhotoBridge 照片备份  2026/2/15 3:00:00    0              2026/2/16 3:00:00
```

| `LastTaskResult` | 含义 |
|---|---|
| `0` | ✅ 成功 |
| `0x1` | 脚本返回了非 0（看脚本日志） |
| `0x2` | 找不到文件 |
| `0x41301` | 任务正在运行 |
| `0x41303` | 任务从未运行 |

**看历史记录**：

```powershell
Get-WinEvent -LogName "Microsoft-Windows-TaskScheduler/Operational" -MaxEvents 20 |
    Where-Object { $_.Message -like "*PhotoBridge*" } |
    Select-Object TimeCreated, Message
```

### 8.5 恢复演练

> **★ 铁律：没有演练过的备份，等于没有备份。**

#### 8.5.1 演练清单（建议每季度做一次）

| # | 演练项 | 步骤 | 预期 |
|---|---|---|---|
| 1 | 恢复单个照片文件 | 从备份盘复制一张照片回来，打开看 | 能正常显示 |
| 2 | 恢复整个目录 | 从备份复制一个月的目录到临时位置 | 文件数、大小一致 |
| 3 | 恢复电脑端数据库 | 复制 `photobridge.db` 覆盖，重启程序 | 索引还在，不用重扫 |
| 4 | 恢复云端数据库 | 停 relay → 恢复 `cloud.db` → 启动 | 设备列表还在，隧道能连 |
| 5 | 全新电脑恢复 | 在干净电脑上装 exe + 恢复 data | 能跑起来 |
| 6 | 记录恢复耗时 | 计时 | 知道真实恢复要多久 |

#### 8.5.2 演练 A：恢复单张照片

```powershell
# 1. 从备份盘找一个文件
$sample = Get-ChildItem "F:\相册备份\photos" -Recurse -Include *.jpg | Select-Object -First 1
Write-Host "样本: $($sample.FullName)"

# 2. 复制到临时位置
New-Item -ItemType Directory -Force -Path "H:\temp\restore-test" | Out-Null
Copy-Item $sample.FullName "H:\temp\restore-test\"

# 3. 验证大小一致
$restored = Get-ChildItem "H:\temp\restore-test\*.jpg" | Select-Object -First 1
Write-Host "恢复后大小: $($restored.Length) 字节"
Write-Host "原文件大小: $($sample.Length) 字节"
if ($restored.Length -eq $sample.Length) {
    Write-Host "OK 大小一致" -ForegroundColor Green
} else {
    Write-Host "FAIL 大小不一致！备份可能损坏" -ForegroundColor Red
}

# 4. 用 Python 验证图片能解码
python -c "
from PIL import Image
img = Image.open(r'$($restored.FullName)')
img.verify()
print('OK 图片完整')
"
```

#### 8.5.3 演练 B：恢复电脑端数据库

```powershell
# 1. 退出程序
Get-Process PhotoBridge -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Seconds 3

# 2. 把当前数据库挪走（不删，作为对照）
Move-Item "D:\Apps\PhotoBridge\data\photobridge.db" "D:\Apps\PhotoBridge\data\photobridge.db.bak" -Force

# 3. 从备份恢复
Copy-Item "F:\相册备份\db\photobridge.db" "D:\Apps\PhotoBridge\data\photobridge.db"

# 4. ★ 清掉 WAL（避免不一致）
Remove-Item "D:\Apps\PhotoBridge\data\photobridge.db-wal" -ErrorAction SilentlyContinue
Remove-Item "D:\Apps\PhotoBridge\data\photobridge.db-shm" -ErrorAction SilentlyContinue

# 5. 验证数据库可读
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
print('media 行数:', c.execute('SELECT COUNT(*) FROM media').fetchone()[0])
print('schema_version:', c.execute('PRAGMA user_version').fetchone()[0])
print('完整性:', c.execute('PRAGMA integrity_check').fetchone()[0])
"

# 6. 启动程序，看是否需要重新索引
Start-Process "D:\Apps\PhotoBridge\PhotoBridge.exe"
Start-Sleep -Seconds 10
Get-Content "D:\Apps\PhotoBridge\data\logs\agent.log" -Tail 20
```

#### 8.5.4 演练 C：恢复云端数据库

```bash
# 1. 停 relay
cd /opt/photobridge-cloud
docker compose stop relay

# 2. 备份当前（对照）
mv data/cloud.db data/cloud.db.bak

# 3. 从备份恢复
cp /opt/backups/cloud.db.YYYYMMDD-HHMMSS data/cloud.db

# 4. 验证可读
sqlite3 data/cloud.db "SELECT COUNT(*) FROM accounts; SELECT COUNT(*) FROM devices;"

# 5. 启动
docker compose start relay
docker compose logs --tail=20 relay

# 6. 验证隧道能连上（看日志有 Agent 握手）
docker compose logs --tail=50 relay | grep -i hello
```

#### 8.5.5 演练记录表（建议填写存档）

| 演练日期 | 演练项 | 耗时 | 结果 | 发现问题 | 改进措施 |
|---|---|---|---|---|---|
| 2026-03-01 | 恢复单张照片 | 2 分钟 | ✅ | — | — |
| 2026-03-01 | 恢复电脑端数据库 | 8 分钟 | ✅ | WAL 文件没清理导致首次启动报错 | 已加入脚本 |
| 2026-03-01 | 恢复云端数据库 | 5 分钟 | ✅ | — | — |
| 2026-03-01 | 全新电脑恢复 | 25 分钟 | ⚠️ | config.toml 没在备份里 | 已加入备份脚本 |

### 8.6 备份的十个常见错误

| # | 错误 | 后果 | 正确做法 |
|---|---|---|---|
| 1 | **只备份数据库，不备份照片** | 最核心的数据没备份 | 照片优先 |
| 2 | **备份在同一个硬盘** | 硬盘坏了全没 | 另一块物理盘 |
| 3 | **备份在同一台电脑** | 电脑被偷/烧了全没 | 异地一份 |
| 4 | **用 `/MIR` 且源被误删** | 下次备份把备份端也删了 | 用 `/E` 或带版本的备份工具 |
| 5 | **从没演练过恢复** | 真出事时发现备份是坏的 | 每季度演练 |
| 6 | **备份脚本静默失败** | 以为在备份，其实几个月没备 | 加告警（§9.5） |
| 7 | **备份目标盘满了** | 备份中断，只有部分文件 | 监控磁盘空间 |
| 8 | **复制正在写入的 SQLite** | 得到损坏的副本 | 用 `.backup` 或先停服务 |
| 9 | **把 `.env` 备份提交到 git** | 密钥泄露 | `.gitignore` |
| 10 | **备份文件没设权限** | 别人能读到密钥 | `chmod 600` |

### 8.7 §8 完成检查清单

| # | 检查项 | 验证 |
|---|---|---|
| 1 | 照片本体有第 2 份（另一块盘） | 看 `F:\相册备份\photos` |
| 2 | 照片本体有第 3 份（异地） | 移动硬盘 / 云盘 |
| 3 | 备份脚本能跑通 | 手动执行一次 |
| 4 | 任务计划已配置 | `Get-ScheduledTask` |
| 5 | 任务执行结果为 0 | `Get-ScheduledTaskInfo` |
| 6 | 云端 `cloud.db` 有备份 | `ls /opt/backups/` |
| 7 | 云端 crontab 已配置 | `crontab -l` |
| 8 | **恢复演练做过至少一次** | 有演练记录 |
| 9 | 备份目录磁盘空间充足 | `df -h` / 看 F 盘 |
| 10 | 备份失败会告警 | §9.5 已配置 |
