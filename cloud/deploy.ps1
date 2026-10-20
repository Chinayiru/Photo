<#
PhotoBridge 云端中继（relay）部署脚本 —— Windows Server 单机版

设计原则：
  · 只用 Windows 自带能力（PowerShell + 计划任务），不装 Docker；
  · 每一步都可重复执行：已经装过 Python、已经建过设备就跳过，不会报错；
  · 出问题时打印清楚停在哪一步，而不是静默失败。

用法（在解压后的目录里，管理员 PowerShell）：
    .\deploy.ps1
    .\deploy.ps1 -BaseUrl "http://203.0.113.10:8000" -Email "me@example.com" -Password "..." -DeviceName "家里的电脑"
#>
[CmdletBinding()]
param(
    [string]$BaseUrl    = "http://203.0.113.10:8000",
    [string]$Email      = "me@example.com",
    [string]$Password   = "PbCloud2026!",
    [string]$DeviceName = "家里的电脑",
    [string]$Port       = "8000",
    [string]$InstallDir = "C:\PhotoBridgeCloud"
)

$ErrorActionPreference = "Stop"

function Step($n, $text) { Write-Host "`n[$n] $text" -ForegroundColor Cyan }
function Ok($text)       { Write-Host "  OK  $text" -ForegroundColor Green }
function Warn($text)     { Write-Host "  !!  $text" -ForegroundColor Yellow }
function Fail($text)     { Write-Host "  XX  $text" -ForegroundColor Red; exit 1 }

# ── 0. 前置检查 ───────────────────────────────────────────────────────────
Step 0 "前置检查"
# 中文输出：Python 在英文 locale 的 Server 上写中文到 stdout 会抛
# UnicodeEncodeError（不是显示乱码，是直接失败），所以两头都钉成 UTF-8。
$env:PYTHONIOENCODING = "utf-8"
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "请以【管理员】身份运行 PowerShell（右键 → 以管理员身份运行）"
}
Ok "管理员权限"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Ok "脚本目录 $here"
foreach ($need in @("cloud\relay\main.py", "shared\mux.py", "scripts\cloud_init.py", "frontend\dist\index.html")) {
    if (-not (Test-Path (Join-Path $here $need))) { Fail "缺少文件 $need —— 解压不完整？" }
}
Ok "代码与前端产物齐全"

# ── 1. Python ─────────────────────────────────────────────────────────────
Step 1 "Python 3.12"
$py = $null
foreach ($cand in @("python", "C:\Python312\python.exe", "C:\Program Files\Python312\python.exe")) {
    try {
        $v = & $cand -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($v -eq "3.12") { $py = $cand; break }
    } catch { }
}
if ($py) {
    Ok "已有 Python 3.12（$py）"
} else {
    Warn "未找到 Python 3.12，开始安装（约 30 MB，从阿里云镜像下载）"
    $exe = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
    if (-not (Test-Path $exe)) {
        $url = "https://registry.npmmirror.com/-/binary/python/3.12.10/python-3.12.10-amd64.exe"
        Write-Host "  下载 $url"
        try {
            # 用 .NET 而不是 Invoke-WebRequest：后者在 Server 上常因 IE 引擎配置报错
            $wc = New-Object System.Net.WebClient
            $wc.DownloadFile($url, $exe)
        } catch {
            Fail "下载 Python 失败：$($_.Exception.Message)。可手工下载后放到 $exe 再重跑本脚本。"
        }
    }
    Ok "已下载 $(('{0:N1}' -f ((Get-Item $exe).Length/1MB))) MB"
    Write-Host "  静默安装（约 1-2 分钟）..."
    $p = Start-Process -FilePath $exe -Wait -PassThru -ArgumentList `
        "/quiet", "InstallAllUsers=1", "PrependPath=1", "Include_pip=1", "Include_test=0", "Shortcuts=0"
    if ($p.ExitCode -ne 0) { Fail "Python 安装失败，退出码 $($p.ExitCode)" }
    # 安装器改了 PATH，但当前进程的环境变量是旧的 —— 直接用它装到哪就找哪
    foreach ($cand in @("C:\Program Files\Python312\python.exe", "C:\Python312\python.exe")) {
        if (Test-Path $cand) { $py = $cand; break }
    }
    if (-not $py) { Fail "装完了却找不到 python.exe，请手工确认安装路径" }
    Ok "安装完成（$py）"
}
& $py --version

# ── 2. 铺开代码 ───────────────────────────────────────────────────────────
Step 2 "铺开代码到 $InstallDir"
if (-not (Test-Path $InstallDir)) { New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null }
if ($here -ne $InstallDir) {
    foreach ($d in @("cloud", "shared", "scripts", "frontend")) {
        if (Test-Path (Join-Path $here $d)) {
            Copy-Item -Recurse -Force (Join-Path $here $d) $InstallDir
        }
    }
    Copy-Item -Force (Join-Path $here "requirements.txt") $InstallDir
}
Ok "代码已就位"

# ── 3. 依赖 ───────────────────────────────────────────────────────────────
Step 3 "安装依赖（relay 只需要 3 个包）"
& $py -m pip install --quiet --disable-pip-version-check `
    -i https://mirrors.aliyun.com/pypi/simple/ `
    --trusted-host mirrors.aliyun.com `
    "starlette>=0.37" "uvicorn>=0.30" "websockets>=12.0"
if ($LASTEXITCODE -ne 0) { Fail "pip 安装失败（退出码 $LASTEXITCODE）" }
& $py -c "import starlette, uvicorn, websockets; print('  starlette', starlette.__version__, '| uvicorn', uvicorn.__version__, '| websockets', websockets.__version__)"
Ok "依赖就绪"

# ── 4. 云端库与设备 ───────────────────────────────────────────────────────
Step 4 "建云端库、登记设备"
$dataDir = Join-Path $InstallDir "data"
if (-not (Test-Path $dataDir)) { New-Item -ItemType Directory -Path $dataDir -Force | Out-Null }
$env:PB_CLOUD_DB = Join-Path $dataDir "cloud.db"

Push-Location $InstallDir
try {
    $out = & $py "scripts\cloud_init.py" "init" `
        "--email" $Email "--password" $Password `
        "--device-name" $DeviceName "--base-url" $BaseUrl 2>&1
    $code = $LASTEXITCODE
} finally { Pop-Location }

if ($code -ne 0) {
    Write-Host $out
    Fail "cloud_init 失败（退出码 $code）"
}
$text = ($out | Out-String)

# 从 JSON 里抠出两个值（不用 ConvertFrom-Json：输出前面还有一行中文日志）
$deviceId  = ([regex]::Match($text, '"device_id"\s*:\s*"([0-9a-f]{32})"')).Groups[1].Value
$agentTok  = ([regex]::Match($text, '"agent_token"\s*:\s*"([A-Za-z0-9_\-]+)"')).Groups[1].Value

if (-not $deviceId) {
    # 设备已存在时 cloud_init 会走「账号已存在」分支；重新登记一台即可
    Warn "没拿到新设备，可能已登记过。下面列出已有设备："
    Push-Location $InstallDir
    try { & $py "scripts\cloud_init.py" "list" } finally { Pop-Location }
    Fail "请手工执行：python scripts\cloud_init.py init --email ... --password ... --device-name ... 来新增设备"
}
Ok "device_id   = $deviceId"
Ok "agent_token = $agentTok"

# ── 5. 启动配置（含环境变量）───────────────────────────────────────────────
Step 5 "写启动脚本"
# ★ 用 .ps1 而不是 .cmd 当启动器：cmd 的默认码页是 936/437，
#   Set-Content -Encoding ASCII 会把路径里的中文写成 "????"
#   （实测 "D:\Photos\.venv" → "H:\????\.venv"，然后报
#   "The system cannot find the path specified" 而不说清是哪个路径）。
#   PowerShell 脚本按 UTF-8 读，这条坑不存在。
$runScript = Join-Path $InstallDir "run_relay.ps1"
$logDir    = Join-Path $InstallDir "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
$hostName = ([uri]$BaseUrl).Host

$runBody = @"
# PhotoBridge 云端中继 —— 由 deploy.ps1 生成。
# 改端口 / Host 白名单请改这个文件，然后 schtasks /End + /Run 重启任务。
`$env:PYTHONIOENCODING = "utf-8"
`$env:PB_CLOUD_DB       = "$dataDir\cloud.db"
`$env:PB_CLOUD_HOST     = "0.0.0.0"
`$env:PB_CLOUD_PORT     = "$Port"
`$env:PB_CLOUD_HOSTS    = "$hostName"
`$env:PB_CLOUD_WEB_DIR  = "$InstallDir\frontend\dist"
Set-Location "$InstallDir"
& "$py" -m uvicorn cloud.relay.main:create_app --factory --host 0.0.0.0 --port $Port --workers 1 --ws websockets *>> "$logDir\relay.log"
"@
Set-Content -Path $runScript -Value $runBody -Encoding UTF8
Ok "$runScript"

# ── 6. 防火墙 ─────────────────────────────────────────────────────────────
Step 6 "防火墙放行 TCP $Port"
$ruleName = "PhotoBridge relay $Port"
$fwOk = $false
if (Get-Command New-NetFirewallRule -ErrorAction SilentlyContinue) {
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP `
        -LocalPort $Port -Action Allow -Profile Any | Out-Null
    $fwOk = $true
} else {
    # Server 2008/2012 早期版本没有 NetSecurity 模块，退回 netsh
    netsh advfirewall firewall delete rule name="$ruleName" | Out-Null
    netsh advfirewall firewall add rule name="$ruleName" dir=in action=allow protocol=TCP localport=$Port | Out-Null
    if ($LASTEXITCODE -eq 0) { $fwOk = $true }
}
if ($fwOk) { Ok "已放行 $Port" } else { Warn "防火墙规则没加成，请手工放行 TCP $Port" }

# ── 7. 试跑并自检 ─────────────────────────────────────────────────────────
Step 7 "试跑并自检"
# ★ 按端口找占用者再杀，不要按进程名 python 全杀 ——
#   服务器上可能还有别的东西在跑 Python。
$owners = @()
try {
    $owners = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
               Select-Object -ExpandProperty OwningProcess -Unique)
} catch { }
foreach ($procId in $owners) {
    if ($procId) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
}
Start-Sleep -Milliseconds 500
Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile","-ExecutionPolicy","Bypass","-File","`"$runScript`"" -WindowStyle Hidden
$healthy = $false
foreach ($i in 1..20) {
    Start-Sleep -Milliseconds 800
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3 -UseBasicParsing
        if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
}
if (-not $healthy) {
    Warn "relay 没起来，日志最后 20 行："
    if (Test-Path "$logDir\relay.log") { Get-Content "$logDir\relay.log" -Tail 20 }
    Fail "自检失败"
}
Ok "/api/health 返回 200"

# 页面能不能取到
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -TimeoutSec 5 -UseBasicParsing
    if ($r.Content -match "id=""app""") { Ok "首页 index.html 正常（含 #app）" }
    else { Warn "首页回来了但不是预期内容" }
} catch { Warn "首页取不到：$($_.Exception.Message)" }

# ── 8. 开机自启 ───────────────────────────────────────────────────────────
Step 8 "注册开机自启"
$taskName = "PhotoBridge Cloud Relay"
schtasks /Delete /TN $taskName /F 2>$null | Out-Null
# ★ 不叫 $args：那是 PowerShell 的自动变量（脚本参数），赋值会静默失败。
$stArgs = @(
    "/Create", "/TN", $taskName, "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST",
    "/TR", "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runScript`"", "/F"
)
schtasks @stArgs | Out-Null
if ($LASTEXITCODE -ne 0) { Warn "注册计划任务失败，可手工在「任务计划程序」里加一条：powershell -File $runScript" }
else { Ok "已注册计划任务「$taskName」（开机自动运行）" }

# ── 9. 交付信息 ───────────────────────────────────────────────────────────
Write-Host "`n============================================================" -ForegroundColor Green
Write-Host " 部署完成 —— 把下面四行抄给助手" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Green
Write-Host "BASE_URL    = $BaseUrl"
Write-Host "DEVICE_ID   = $deviceId"
Write-Host "AGENT_TOKEN = $agentTok"
Write-Host "本机自检    = /api/health 200"
Write-Host ""
Write-Host "外网验证（在任意浏览器打开）：" -ForegroundColor Yellow
Write-Host "  $BaseUrl/api/health"
Write-Host "  $BaseUrl/d/$deviceId/"
Write-Host ""
Write-Host "日志：$logDir\relay.log" -ForegroundColor Gray
Write-Host "重启 relay：Restart-Service 不行（不是服务），用 schtasks /Run /TN `"$taskName`"" -ForegroundColor Gray
