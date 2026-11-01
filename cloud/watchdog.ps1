<#
PhotoBridge 中继 / 隧道的看门狗 —— 让"计划任务死了"从"第二天才发现"变成"5 分钟内自愈"

## 为什么需要它

中继与 cloudflared 都是靠**计划任务**常驻的（`deploy.ps1` 第 8 步注册）。
计划任务只保证"开机跑一次"，**不保证"一直活着"**：

  · relay 进程被 OOM / 未捕获异常杀掉 → 任务状态停在 Ready，没有人重启；
  · cloudflared 断网重连失败退出 → 隧道消失，手机端全部 502；
  · 两者都不会自愈，也不会有任何提示 —— 用户看到的是"打开就转圈"。

2026-10-30 12:54 就是这样断的：两个计划任务都还在，进程都没了，
直到第二天人工发现（`docs/11` R-002 / R-010 的"已记录未修"部分）。
这条看门狗把"等人工发现"换成"每 5 分钟检查一次，坏了就拉起来"。

## 用法

    # 查一次状态（不需要管理员，不改任何东西）
    .\watchdog.ps1 -Status -BaseUrl http://127.0.0.1:8799

    # 装成每 5 分钟一次的计划任务（需要管理员）
    .\watchdog.ps1 -Install

    # 手动跑一轮（等价于计划任务干的事）
    .\watchdog.ps1

    # 卸载
    .\watchdog.ps1 -Uninstall

## 它刻意不做的事

  · 不碰数据库、不碰照片、不删业务日志（只按大小轮转自己的日志）；
  · 不改防火墙、不改 Host 白名单、不重启整台机器；
  · **不解决快隧道域名漂移**：`*.trycloudflare.com` 每次重启都换名字，
    这是 quick tunnel 的固有行为，只能换命名隧道（见 `docs/08` §5）。
    看门狗能做的是把**当前**公网地址记下来，地址一变就写一条 WARN ——
    于是"昨天还能用的链接今天失效"有据可查，而不是当成服务挂了。

## 判定口径（为什么不用 schtasks 的状态列）

`schtasks /Query` 的输出**是本地化的**：中文 Windows 上是"状态: 正在运行"，
英文机器上是 "Status: Running"。按列名解析会在另一种语言的机器上静默失效
（正好是"部署到别的机器才坏"的那类缺陷）。所以这里只认两件跨语言的事实：

  1. 中继：`GET /api/health` 回 200（这是它唯一的对外承诺，见 `cloud/relay/main.py`）；
  2. 隧道：`cloudflared` 进程存在（它没有可匿名探测的本机端点）。

重启仍然用 `/End` + `/Run`，但**不看返回文本**，只看退出码。
#>
[CmdletBinding()]
param(
    # 中继的本机地址。默认就是 deploy.ps1 装出来的那个端口。
    [string]$BaseUrl = "http://127.0.0.1:8000",
    # 健康探测路径。relay 是 `/api/health`；电脑端 agent 是 `/api/system/ping`
    # —— 本机演练时指向后者即可，不必先有一台真的 relay。
    [string]$HealthPath = "/api/health",
    # 计划任务名必须与 deploy.ps1 第 8 步、cloudflared 注册脚本一致。
    [string]$RelayTask = "PhotoBridge Cloud Relay",
    [string]$TunnelTask = "PhotoBridge Cloudflared",
    [string]$InstallDir = "C:\PhotoBridgeCloud",
    [int]$IntervalMinutes = 5,
    # 自己这条日志的轮转阈值（KB）。与业务日志分开，互不影响。
    [int]$MaxLogKB = 1024,
    # cloudflared 的日志：只用来读"当前公网地址"，不参与健康判定。
    [string]$CloudflaredLog = "C:\PhotoBridgeCloud\logs\cloudflared.log",
    # 只看不改。
    [switch]$Status,
    [switch]$Install,
    [switch]$Uninstall,
    # 检查但绝不重启（演练 / 排障时用）。
    [switch]$NoRestart,
    # 计划任务模式：不往 stdout 打，只写日志。
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

$LogDir    = Join-Path $InstallDir "logs"
$LogFile   = Join-Path $LogDir "watchdog.log"
$StateDir  = Join-Path $InstallDir "state"
$StateFile = Join-Path $StateDir "public_url.txt"
$TaskName  = "PhotoBridge Watchdog"

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    if (-not $Quiet) {
        if ($Level -eq "ERROR") { Write-Host $line -ForegroundColor Red }
        elseif ($Level -eq "WARN") { Write-Host $line -ForegroundColor Yellow }
        else { Write-Host $line }
    }
    try {
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
        Add-Content -Path $LogFile -Value $line -Encoding UTF8
        $size = (Get-Item $LogFile).Length
        if ($size -gt ($MaxLogKB * 1024)) {
            # 只留一份旧的：看门狗日志用来回答"昨晚它干过什么"，
            # 不需要一个月的历史（要历史去看业务日志）。
            Move-Item -Force $LogFile "$LogFile.1"
        }
    } catch {
        Write-Host "  !! 写看门狗日志失败：$($_.Exception.Message)" -ForegroundColor Yellow
    }
}

function Test-Relay {
    param([string]$Url, [string]$Path)
    try {
        $resp = Invoke-WebRequest -Uri ($Url.TrimEnd('/') + $Path) `
            -UseBasicParsing -TimeoutSec 5
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Get-RunningProcess {
    param([string]$Name)
    # -ErrorAction SilentlyContinue 同时挡住"没装这个程序"和"没权限看"两种失败。
    return @(Get-Process -Name $Name -ErrorAction SilentlyContinue)
}

function Restart-ScheduledTask {
    param([string]$Name)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        # /End 对"已经停了"的任务会返回非 0，这是正常的，不当失败。
        schtasks /End /TN "$Name" *>&1 | Out-Null
        Start-Sleep -Milliseconds 500
        schtasks /Run /TN "$Name" *>&1 | Out-Null
        $rc = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    return ($rc -eq 0)
}

function Get-PublicUrl {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return $null }
    try {
        # 只读尾部 256 KB：cloudflared 的日志会一直长，全量读在长期运行的
        # 机器上是几百 MB 的 IO，而我们要的信息永远在最后几行。
        $fs = [System.IO.File]::Open($Path, "Open", "Read", "ReadWrite")
        try {
            $len = $fs.Length
            $take = [Math]::Min(262144, $len)
            [void]$fs.Seek(($len - $take), "Begin")
            $buf = New-Object byte[] $take
            [void]$fs.Read($buf, 0, $take)
            $text = [System.Text.Encoding]::UTF8.GetString($buf)
        } finally {
            $fs.Dispose()
        }
    } catch {
        return $null
    }
    $m = [regex]::Matches($text, "https://[a-z0-9-]+\.trycloudflare\.com")
    if ($m.Count -eq 0) { return $null }
    return $m[$m.Count - 1].Value
}

function Show-Status {
    param([bool]$RelayHealthy, [int]$TunnelCount, [string]$PublicUrl)
    $relayText = if ($RelayHealthy) { "正常（$HealthPath 200）" } else { "不可用（$HealthPath 不通）" }
    $tunnelText = if ($TunnelCount -gt 0) { "运行中（$TunnelCount 个进程）" } else { "未运行" }
    Write-Host ""
    Write-Host "PhotoBridge 看门狗状态" -ForegroundColor Cyan
    Write-Host "  中继   ($BaseUrl)  : $relayText"
    Write-Host "  隧道   (cloudflared) : $tunnelText"
    if ($PublicUrl) {
        Write-Host "  公网地址            : $PublicUrl"
        Write-Host "                        （trycloudflare 的地址每次重启都会变）" -ForegroundColor DarkGray
    } else {
        Write-Host "  公网地址            : 未知（cloudflared 日志里没有可识别的地址）"
    }
    Write-Host "  日志                : $LogFile"
    Write-Host ""
}

# ── 安装 / 卸载 ───────────────────────────────────────────────────────────

function Install-Watchdog {
    $self = $PSCommandPath
    if (-not $self) { $self = $MyInvocation.MyCommand.Path }
    if (-not $self) { Write-Log "找不到自己的路径，无法注册计划任务" "ERROR"; exit 1 }

    # 参数要跟着一起带过去：计划任务里没有"当前目录"这个概念，
    # 而 InstallDir / BaseUrl 决定了脚本读哪个日志、探哪个端口。
    $action = 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "{0}" -Quiet -BaseUrl "{1}" -HealthPath "{2}" -InstallDir "{3}" -CloudflaredLog "{4}"' -f `
        $self, $BaseUrl, $HealthPath, $InstallDir, $CloudflaredLog

    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        schtasks /Create /TN "$TaskName" /SC MINUTE /MO $IntervalMinutes `
            /RU SYSTEM /RL HIGHEST /TR $action /F *>&1 | Out-Null
        $rc = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($rc -eq 0) {
        Write-Log "已注册计划任务「$TaskName」：每 $IntervalMinutes 分钟检查一次"
        Write-Host "  提示：想立刻验证，跑  schtasks /Run /TN `"$TaskName`"" -ForegroundColor Gray
        return 0
    }
    Write-Log "注册计划任务失败（退出码 $rc）—— 需要管理员权限" "ERROR"
    return 1
}

function Uninstall-Watchdog {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        schtasks /End /TN "$TaskName" *>&1 | Out-Null
        schtasks /Delete /TN "$TaskName" /F *>&1 | Out-Null
        $rc = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($rc -eq 0) { Write-Log "已删除计划任务「$TaskName」" } else { Write-Log "删除计划任务失败（退出码 $rc）" "WARN" }
    return 0
}

# ── 主流程 ───────────────────────────────────────────────────────────────

if ($Install)   { exit (Install-Watchdog) }
if ($Uninstall) { exit (Uninstall-Watchdog) }

$relayHealthy = Test-Relay $BaseUrl $HealthPath
$tunnelProcs  = Get-RunningProcess "cloudflared"
$publicUrl    = Get-PublicUrl $CloudflaredLog

if ($Status) {
    Show-Status -RelayHealthy $relayHealthy -TunnelCount $tunnelProcs.Count -PublicUrl $publicUrl
    if ($relayHealthy -and $tunnelProcs.Count -gt 0) { exit 0 } else { exit 1 }
}

$healthy = $true

# --- 中继 ---
if ($relayHealthy) {
    Write-Log "中继正常（$BaseUrl$HealthPath 200）"
} else {
    Write-Log "中继健康检查失败：$BaseUrl$HealthPath" "WARN"
    if ($NoRestart) {
        $healthy = $false
    } else {
        $restarted = Restart-ScheduledTask $RelayTask
        if (-not $restarted) {
            Write-Log "重启计划任务失败：$RelayTask（任务名对吗？）" "ERROR"
        }
        # 给它时间起来：Python 进程 + SQLite 初始化在本机约 2–5 秒，
        # 留 15 秒是给"磁盘很慢 / 首次建库"的情况。
        Start-Sleep -Seconds 15
        if (Test-Relay $BaseUrl $HealthPath) {
            Write-Log "中继已恢复" "WARN"
        } else {
            Write-Log "中继仍未恢复，需要人工检查 $LogDir\relay.log" "ERROR"
            $healthy = $false
        }
    }
}

# --- 隧道 ---
# 进程名不用 "cloudflared.exe"：Get-Process 的 -Name 是不带扩展名的。
if ($tunnelProcs.Count -gt 0) {
    Write-Log "隧道正常（cloudflared 进程在）"
} else {
    Write-Log "cloudflared 未运行" "WARN"
    if ($NoRestart) {
        $healthy = $false
    } else {
        $restarted = Restart-ScheduledTask $TunnelTask
        if (-not $restarted) {
            Write-Log "重启计划任务失败：$TunnelTask（任务名对吗？）" "ERROR"
        }
        Start-Sleep -Seconds 8
        if ((Get-RunningProcess "cloudflared").Count -gt 0) {
            Write-Log "隧道已恢复" "WARN"
        } else {
            Write-Log "隧道仍未恢复，需要人工检查 $CloudflaredLog" "ERROR"
            $healthy = $false
        }
    }
}

# --- 公网地址漂移 ---
# 快隧道每次重启都换域名。把"当前地址"落盘，下一次变了就写一条 WARN：
# 这条日志就是"用户说链接打不开"时的第一手证据。
if ($publicUrl) {
    $previous = $null
    if (Test-Path $StateFile) { $previous = (Get-Content $StateFile -Raw).Trim() }
    if ($previous -and $previous -ne $publicUrl) {
        Write-Log "公网地址已变化：$previous -> $publicUrl（快隧道重启必然换名，旧链接失效）" "WARN"
    }
    try {
        if (-not (Test-Path $StateDir)) { New-Item -ItemType Directory -Path $StateDir -Force | Out-Null }
        Set-Content -Path $StateFile -Value $publicUrl -Encoding ASCII
    } catch {
        Write-Log "记录公网地址失败：$($_.Exception.Message)" "WARN"
    }
}

if ($healthy) { exit 0 } else { exit 1 }
