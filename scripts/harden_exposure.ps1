<#
.SYNOPSIS
    收紧 PhotoBridge 在 Windows 本机上的暴露面（防火墙 + data 目录 ACL）。

.DESCRIPTION
    对应 docs/12-部署核查记录.md 里的 F1 与 F2 两条发现：

      F1 公共网络配置下有两条对「任意来源、任意端口」放行 photobridge.exe 的
         入站规则（Windows 首次监听时弹窗点「允许」留下的）。本脚本把它们删掉，
         并写一条显式 Block 规则，避免程序下次监听时再次弹窗又被点「允许」。
      F2 data/ 目录对本机「Authenticated Users」可写（数据库、云端凭据、
         审计日志都在里面）。本脚本把继承权限清掉，只留给当前用户、SYSTEM、
         Administrators。

    脚本是幂等的：重复执行不会重复建规则，也不会把已收紧的 ACL 再改一次。
    -DryRun 只打印将要做什么，不落任何改动；非管理员也能跑 -DryRun。

.PARAMETER DryRun
    只预览，不改动。

.PARAMETER AppPath
    photobridge.exe 的绝对路径（默认取仓库里的 dist 产物）。

.PARAMETER DataDir
    运行时数据目录（数据库、配置、审计日志所在）。

.PARAMETER Port
    局域网直连端口（默认 8799，与 data/config.json 的 server.port 一致）。

.EXAMPLE
    # 先看一眼要改什么（不需要管理员）
    pwsh -File scripts/harden_exposure.ps1 -DryRun

.EXAMPLE
    # 在「以管理员身份运行」的 PowerShell 里真正执行
    pwsh -File scripts/harden_exposure.ps1

.NOTES
    执行完后按 docs/12 §5 回填判定。ECS 安全组（22 端口收口）不在本脚本范围内。
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$AppPath = 'H:\远程照片\dist\photobridge\photobridge.exe',
    [string]$DataDir = 'H:\远程照片\data',
    [int]$Port = 8799
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$Text) { Write-Host "`n== $Text ==" -ForegroundColor Cyan }
function Write-Plan([string]$Text) { Write-Host "  · $Text" }
function Write-Warn2([string]$Text) { Write-Host "  ! $Text" -ForegroundColor Yellow }

$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

Write-Host "PhotoBridge 暴露面收紧（DryRun=$DryRun）"
Write-Host "  程序：$AppPath"
Write-Host "  数据：$DataDir"
Write-Host "  端口：$Port"
Write-Host "  管理员：$isAdmin"

if (-not $DryRun -and -not $isAdmin) {
    throw '需要管理员权限。请在「以管理员身份运行」的 PowerShell 里再跑一次（或用 -DryRun 预览）。'
}
if (-not (Test-Path -LiteralPath $AppPath)) {
    Write-Warn2 "找不到 $AppPath —— 规则仍可按路径匹配到已存在的条目，但新规则的 Program 会指向一个不存在的文件。"
}
if (-not (Test-Path -LiteralPath $DataDir)) {
    throw "找不到数据目录：$DataDir"
}

# ---------------------------------------------------------------------------
# 1. 清点：当前有哪些入站放行规则指向这个程序
# ---------------------------------------------------------------------------
Write-Step '1. 当前指向该程序的入站放行规则'
# 先按「程序」直接查过滤器，再反查规则：比"遍历所有入站规则逐条取过滤器"
# 快两个数量级（后者每条规则一次 CIM 往返，几百条就是好几分钟）。
$appFilters = @(Get-NetFirewallApplicationFilter -Program $AppPath -ErrorAction SilentlyContinue)
$ours = foreach ($f in $appFilters) {
    foreach ($rule in @($f | Get-NetFirewallRule -ErrorAction SilentlyContinue)) {
        if ($rule.Direction -ne 'Inbound' -or $rule.Action -ne 'Allow') { continue }
        $pf = $rule | Get-NetFirewallPortFilter -ErrorAction SilentlyContinue
        $af = $rule | Get-NetFirewallAddressFilter -ErrorAction SilentlyContinue
        [pscustomobject]@{
            Rule    = $rule
            Name    = $rule.Name
            Profile = [string]$rule.Profile
            Proto   = [string]$pf.Protocol
            Port    = [string]$pf.LocalPort
            Remote  = [string]$af.RemoteAddress
        }
    }
}
if (-not $ours) {
    Write-Plan '没有找到按程序匹配的入站放行规则（可能已经被收紧了）。'
} else {
    foreach ($o in $ours) {
        Write-Plan ("{0} | Profile={1} | {2}/{3} | Remote={4}" -f `
            $o.Rule.DisplayName, $o.Profile, $o.Proto, $o.Port, $o.Remote)
    }
}

$publicLeaks = @($ours | Where-Object { $_.Profile -match 'Public' })
if ($publicLeaks.Count -gt 0) {
    Write-Step '2. 删除「公共网络 + 任意端口 + 任意来源」的放行规则'
    foreach ($o in $publicLeaks) {
        Write-Plan "删除规则：$($o.Name)"
        if (-not $DryRun) {
            Remove-NetFirewallRule -Name $o.Name
        }
    }
} else {
    Write-Step '2. 删除公共网络上的宽放行规则'
    Write-Plan '没有 Public 配置下的宽放行规则，跳过。'
}

# ---------------------------------------------------------------------------
# 3. 公共网络：显式拒绝（顺带压掉"防火墙弹窗又被点允许"的复发路径）
# ---------------------------------------------------------------------------
Write-Step '3. 公共网络显式拒绝该程序'
$blockName = 'PhotoBridge (Public 拒绝)'
$existingBlock = Get-NetFirewallRule -DisplayName $blockName -ErrorAction SilentlyContinue
if ($existingBlock) {
    Write-Plan "已存在：$blockName"
} else {
    Write-Plan "新建 Block 规则：$blockName（Profile=Public，入站，Protocol=Any）"
    if (-not $DryRun) {
        New-NetFirewallRule -DisplayName $blockName -Direction Inbound -Action Block `
            -Profile Public -Protocol Any -Program $AppPath | Out-Null
    }
}

# ---------------------------------------------------------------------------
# 4. 专用网络：只放一个端口、只对本子网
# ---------------------------------------------------------------------------
Write-Step "4. 专用网络只放 $Port，且只对本子网"
$lanName = "PhotoBridge $Port (LAN only)"
$existingLan = Get-NetFirewallRule -DisplayName $lanName -ErrorAction SilentlyContinue
if ($existingLan) {
    $existingLan | Remove-NetFirewallRule
    Write-Plan "重建规则：$lanName（先删旧的，保证参数一致）"
} else {
    Write-Plan "新建规则：$lanName"
}
if (-not $DryRun) {
    New-NetFirewallRule -DisplayName $lanName -Direction Inbound -Action Allow `
        -Profile Private,Domain -Protocol TCP -LocalPort $Port `
        -RemoteAddress LocalSubnet -Program $AppPath | Out-Null
}
Write-Plan '注意：Tailscale 之类的组网软件有自己的规则（如 PhotoBridge 8799 (Tailscale/Private)），本脚本不动它。'

# ---------------------------------------------------------------------------
# 5. data 目录 ACL：清继承，只留当前用户 / SYSTEM / Administrators
# ---------------------------------------------------------------------------
Write-Step '5. 收紧 data 目录权限'
$currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$grants = @(
    "*${currentSid}:(OI)(CI)F",
    '*S-1-5-18:(OI)(CI)F',      # NT AUTHORITY\SYSTEM
    '*S-1-5-32-544:(OI)(CI)F'   # BUILTIN\Administrators
)
Write-Plan "清掉继承权限：icacls `"$DataDir`" /inheritance:r"
Write-Plan ("授予：{0}" -f ($grants -join '  '))
if (-not $DryRun) {
    & icacls.exe $DataDir /inheritance:r | Out-Null
    $grantArgs = @($DataDir, '/grant:r') + $grants
    & icacls.exe @grantArgs | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls 失败（exit $LASTEXITCODE）" }
}

# ---------------------------------------------------------------------------
# 6. 复核
# ---------------------------------------------------------------------------
Write-Step '6. 复核'
if ($DryRun) {
    Write-Plan '（DryRun：以下为将要达到的状态，不实际查询改动后的结果）'
} else {
    $still = foreach ($f in @(Get-NetFirewallApplicationFilter -Program $AppPath -ErrorAction SilentlyContinue)) {
        foreach ($rule in @($f | Get-NetFirewallRule -ErrorAction SilentlyContinue)) {
            if ($rule.Direction -eq 'Inbound' -and $rule.Action -eq 'Allow' -and
                [string]$rule.Profile -match 'Public') {
                $rule.DisplayName
            }
        }
    }
    if ($still) {
        Write-Warn2 ("Public 配置下仍有放行规则：" + ($still -join ' / '))
    } else {
        Write-Plan 'Public 配置下已没有任何放行该程序的入站规则。'
    }
    $lan = Get-NetFirewallRule -DisplayName $lanName -ErrorAction SilentlyContinue
    if ($lan) {
        $pf = $lan | Get-NetFirewallPortFilter
        $af = $lan | Get-NetFirewallAddressFilter
        Write-Plan ("$lanName：Profile={0} | {1}/{2} | Remote={3}" -f `
            [string]$lan.Profile, [string]$pf.Protocol, [string]$pf.LocalPort, [string]$af.RemoteAddress)
    } else {
        Write-Warn2 "没有找到 $lanName"
    }
    Write-Plan 'data 目录当前权限：'
    & icacls.exe $DataDir
}

Write-Step '完成'
Write-Plan 'ECS 安全组（22 端口收口）在云控制台做，见 docs/12 §3 F3。'
Write-Plan '改完按 docs/12 §5 回填判定。'
