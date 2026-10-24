<#
PhotoBridge 云端中继（relay）部署脚本 —— Windows Server 单机版

设计原则：
  · 只用 Windows 自带能力（PowerShell + 计划任务），不装 Docker；
  · 每一步都可重复执行：已经装过 Python、已经建过设备就跳过，不会报错；
  · 出问题时打印清楚停在哪一步，而不是静默失败。

用法（在解压后的目录里，管理员 PowerShell）：
    .\deploy.ps1
    .\deploy.ps1 -BaseUrl "https://你的域名" -Email "you@example.com" -DeviceName "家里的电脑"

口令不写在命令行上：脚本会用 `Read-Host -AsSecureString` 交互式问一次，
再经 stdin 交给 `cloud_init.py`（写进命令行会被进程列表和 PSReadLine
历史双双记下 —— 见 docs/11 ISSUE-032）。
#>
[CmdletBinding()]
param(
    [string]$BaseUrl    = "http://203.0.113.10:8000",
    [string]$Email      = "me@example.com",
    # ★ 口令**没有默认值**。曾经这里是 `"PbCloud2026!"`，而它同时是线上
    #   云账号的真实口令 —— 等于把凭据写进仓库，还被一条测试钉住了
    #   （`test_windows_deploy_script_does_not_hardcode_a_password` 的断言
    #   却是"必须等于这个硬编码值"，名字与断言相反，与 ISSUE-018 同型）。
    #   现在留空则交互式询问（`Read-Host -AsSecureString`），口令只经
    #   stdin 交给 `cloud_init.py`，不进 argv、不进命令历史。
    [string]$Password   = "",
    [string]$DeviceName = "家里的电脑",
    [string]$Port       = "8000",
    [string]$InstallDir = "C:\PhotoBridgeCloud",
    # 受信代理：只有从这些地址连进来的请求，其 `X-Forwarded-For` 才采信。
    # cloudflared（以及旧方案的 80 端口转发）都从回环连入，所以默认回环。
    # ★ 设了它就必须保证 8000 不再直接对公网开放，否则任何人都能伪造来源。
    [string]$TrustedProxies = "127.0.0.1,::1",
    # 额外允许的 Host（逗号分隔）。relay 的 `/agent/ws` 会校验 Host 头
    # （防 DNS rebinding），而"对外主机名"与"监听地址"是两件事：走
    # Cloudflare 隧道 / 反向代理 / 自有域名时，电脑端握手带的是**公网主机名**，
    # 不在白名单里就会被 403 拒掉，现象是"手机能开页面、电脑永远不在线"。
    # 例：-ExtraHosts "pb.example.com,xxx.trycloudflare.com"
    [string]$ExtraHosts = ""
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

# ── 口令：不落命令行 ─────────────────────────────────────────────────────
# `-Password` 仍然接受（无人值守场景），但默认走交互式输入。清掉变量里
# 的明文痕迹是做不到的（.NET 字符串不可变），能做的是**不让它出现在
# 任何会被记录的地方**：argv、事件 4688、PSReadLine 历史。
function Resolve-Password {
    # ★ 只在**真要登记新设备**时调用（第 4 步的 else 分支）。早先这段写在
    #   脚本开头无条件执行，于是无人值守升级（SSH / 计划任务 / 没有控制台）
    #   会卡在 `Read-Host -AsSecureString` 上：脚本连第 1 步都进不去，
    #   而且因为预检输出还没打出来，现场看起来像"什么都没发生"——
    #   `docs/11` ISSUE-037。
    #
    #   无人值守的编排走环境变量：口令放在调用方进程的环境变量里，不进
    #   argv、不进脚本、不进 PSReadLine 历史，读完立刻从进程环境里抹掉。
    if ($Password) { return $Password }
    if ($env:PB_DEPLOY_PASSWORD) {
        $plain = $env:PB_DEPLOY_PASSWORD
        Remove-Item Env:\PB_DEPLOY_PASSWORD -ErrorAction SilentlyContinue
        return $plain
    }
    $sec = Read-Host -AsSecureString "请输入云端账号口令（输入不回显）"
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    if (-not $plain) { Fail "口令为空" }
    return $plain
}
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
# 脚本通常在包根目录下运行（把 deploy.ps1 拷到根，然后右键“以管理员身份运行”）。
# 但也允许留在 cloud\ 里跑（.\cloud\deploy.ps1），这里统一回退到包根。
if (-not (Test-Path (Join-Path $here "cloud\relay\main.py"))) {
    $up = Split-Path -Parent $here
    if (Test-Path (Join-Path $up "cloud\relay\main.py")) { $here = $up }
}
Ok "包根目录 $here"
foreach ($need in @("cloud\relay\main.py", "shared\mux.py", "scripts\cloud_init.py", "scripts\cloud_run.py", "frontend\dist\index.html")) {
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

# ── 2b. 收紧目录权限 ──────────────────────────────────────────────────────
Step "2b" "收紧 $InstallDir 的 ACL"
# ★ 为什么必须做：`C:\` 的默认继承把 `BUILTIN\Users:(RX)` 带到每个子目录
#   上，于是**本机任何账号**都能读 `data\cloud.db`（账号口令 hash、全部
#   设备与客户端记录）和 `logs\relay.log`（配对码、搜索词）。SYSTEM 身份
#   跑的计划任务 + 一个全机可读的目录 = 提权链条的第二跳。
#   实证：2026-10-23 在服务器上 `icacls C:\PhotoBridgeCloud` 看到
#   `BUILTIN\Users:(RX)(AD)(WD)`（docs/11 ISSUE-032）。
#   用 SID 而不是 `SYSTEM`/`Administrators` 字面量：中文/英文系统的组名
#   不一样，SID 一样。
$aclOk = $true
try {
    icacls "$InstallDir" /inheritance:r `
        /grant:r "*S-1-5-18:(OI)(CI)F" `
        /grant:r "*S-1-5-32-544:(OI)(CI)F" 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { $aclOk = $false }
} catch { $aclOk = $false }
if ($aclOk) { Ok "只有 SYSTEM 与 Administrators 可访问" }
else { Warn "ACL 没收紧成功，请手工执行：icacls `"$InstallDir`" /inheritance:r /grant:r `"*S-1-5-18:(OI)(CI)F`" /grant:r `"*S-1-5-32-544:(OI)(CI)F`"" }

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

# ★ 重装/升级：已有安装里有现成的 device_id 与 agent_token，必须**沿用**。
#   否则 `cloud_init init` 撞上"账号已存在"直接退出，整个部署在第 4 步断掉 ——
#   这是升级路径上最典型的一次失败：第一次安装很顺，第二次永远装不上。
#   （agent_token 在库中只存 sha256，拿不回来，所以只能从上次生成的启动器里读。）
$reuseDid = ""
$reuseTok = ""
$prevRun  = Join-Path $InstallDir "run_relay.ps1"
if (Test-Path $prevRun) {
    $prevText = Get-Content $prevRun -Raw -Encoding UTF8
    $reuseDid = ([regex]::Match($prevText, 'PB_CLOUD_DEVICE_ID"\s*=\s*"([0-9a-f]{32})"')).Groups[1].Value
    $reuseTok = ([regex]::Match($prevText, 'PB_CLOUD_AGENT_TOKEN"\s*=\s*"([A-Za-z0-9_\-]+)"')).Groups[1].Value
}
$dbExists = Test-Path $env:PB_CLOUD_DB
if ($reuseDid -and $reuseTok) {
    Ok "检测到已有安装，沿用 device_id = $reuseDid（不重新登记设备）"
    $deviceId = $reuseDid
    $agentTok = $reuseTok
} elseif ($dbExists) {
    # ★ 升级路径上最典型的一次失败就在这里：第一次安装很顺，第二次永远装不上。
    #   库已经存在说明账号与设备都已登记（agent_token 在库里只存 sha256，
    #   本来就取不回来），此时再跑 `cloud_init init` 只会撞「账号已存在」。
    #   中继进程本身不需要 device_id/agent_token —— 令牌是家用 agent 端持有的。
    Ok "检测到已有云端库：沿用库中设备，不重新登记"
    $deviceId = "(沿用库中已有设备)"
    $agentTok = "(不重发；新增设备请手工运行 scripts\cloud_init.py init ...)"
} else {

    # 到这里才需要口令：上面两个分支（沿用上次的启动器 / 库已存在）都用不到它。
    $Password = Resolve-Password
    Ok "口令已就绪（未写入命令行）"

    Push-Location $InstallDir
    $prevEap4 = $ErrorActionPreference
    # ★ `$ErrorActionPreference = "Stop"` 下，**原生命令往 stderr 写一行**就会
    #   变成 terminating NativeCommandError（同样的事故见 docs/11 ISSUE-027）。
    #   cloud_init 的正常提示（例如「账号已存在」）走 stderr，所以这一小段单独放宽。
    $ErrorActionPreference = "Continue"
    try {
        # ★ 口令走 stdin（`--password-stdin`），不进 argv：进程命令行对
        #   本机任何用户可见，而这条命令会活几秒钟（`docs/11` ISSUE-032）。
        $out = $Password | & $py "scripts\cloud_init.py" "init" `
            "--email" $Email "--password-stdin" `
            "--device-name" $DeviceName "--base-url" $BaseUrl 2>&1
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
        $ErrorActionPreference = $prevEap4
    }

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
        Fail "库已存在但没拿到新设备。新增设备请手工执行：python scripts\cloud_init.py init --email ... --password-stdin --device-name ..."
    }
    Ok "device_id   = $deviceId"
    Ok "agent_token = $agentTok"
}

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
# 对外主机名 = 监听主机 + 额外分配（去重、保序）
$allHosts = @($hostName)
foreach ($h in ($ExtraHosts -split ",")) {
    $h = $h.Trim()
    if ($h) { $allHosts += $h }
}
$hostsValue = (($allHosts | Select-Object -Unique) -join ",")

$runBody = @"
# PhotoBridge 云端中继 —— 由 deploy.ps1 生成。
# 改端口 / Host 白名单请改这个文件，然后 schtasks /End + /Run 重启任务。
`$env:PYTHONIOENCODING = "utf-8"
`$env:PB_CLOUD_DB       = "$dataDir\cloud.db"
`$env:PB_CLOUD_HOST     = "0.0.0.0"
`$env:PB_CLOUD_PORT     = "$Port"
`$env:PB_CLOUD_HOSTS    = "$hostsValue"
`$env:PB_CLOUD_WEB_DIR  = "$InstallDir\frontend\dist"
# 受信代理：cloudflared 从回环连入，只有它写的 X-Forwarded-For 才采信。
# 不设的后果是"所有手机共用 127.0.0.1 一个限流桶"——一个人刷满登录次数，
# 主人自己也登不上（docs/11 ISSUE-030）。
`$env:PB_CLOUD_TRUSTED_PROXIES = "$TrustedProxies"
# `-m` 会把当前目录放进 sys.path[0]：能在 `$InstallDir` 里写文件的人就能
# 用同名模块劫持 SYSTEM 进程。关掉隐式 CWD，改用显式 PYTHONPATH。
`$env:PYTHONSAFEPATH   = "1"
`$env:PYTHONPATH       = "$InstallDir"
Set-Location "$InstallDir"
# ★ 入口是 `scripts\cloud_run.py`（脚本文件），不是 `-m cloud.relay.main`：
#   embeddable 版 Python 的 `python312._pth` 会让 `PYTHONPATH` 整条失效，
#   此时 `-m` 找不到 `cloud` 包（实测 `ModuleNotFoundError: No module named
#   'cloud'`，2026-10-23）。脚本文件形式自带 `__file__`，自己把包根塞进
#   `sys.path`，MSI / embeddable 两种发行都成立。
#   同理**不能**退回 `-m uvicorn …:create_app --factory`：uvicorn 的命令行
#   入口默认 `--proxy-headers` + `forwarded_allow_ips=127.0.0.1`（把
#   `scope["client"]` 改写成可伪造的 XFF）、access log 全开（把
#   `?code=NNNNNN` 写进日志）、并回 `Server: uvicorn`。这些只能在 `main()`
#   里一次设对（`docs/11` ISSUE-030 / ISSUE-032）。
& "$py" "$InstallDir\scripts\cloud_run.py" *>> "$logDir\relay.log"
"@
Set-Content -Path $runScript -Value $runBody -Encoding UTF8
Ok "$runScript"

# ── 6. 防火墙 ─────────────────────────────────────────────────────────────
# ★ 只在本机**直接**对外提供 HTTP 时才开入站口。
#   `-BaseUrl` 是 https 时，说明公网入口是隧道（cloudflared 出站长连接），
#   本机不需要任何入站放行 —— 那正是我们要的状态：安全审计里 8000 明文
#   入口与 80 转发器都因为"多开了一个口"而变成了攻击面。
$ruleName = "PhotoBridge relay $Port"
if ($BaseUrl -match '^https://') {
    Step 6 "防火墙：入口是隧道（https），本机不放行任何入站端口"
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
        Remove-NetFirewallRule -ErrorAction SilentlyContinue
    netsh advfirewall firewall delete rule name="$ruleName" 2>&1 | Out-Null
    Ok "未改动入站规则（中继只需出站）"
} else {
    Step 6 "防火墙放行 TCP $Port"
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
}

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
# ★ 不叫 $args：那是 PowerShell 的自动变量（脚本参数），赋值会静默失败。
$stArgs = @(
    "/Create", "/TN", $taskName, "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST",
    "/TR", "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runScript`"", "/F"
)
# ★ 外部命令 + `$ErrorActionPreference = "Stop"` 是个坑（实测踩到，2026-10-23）：
#   首次部署时任务还不存在，`schtasks /Delete` 会把
#   "ERROR: The system cannot find the file specified." 写到 stderr；
#   PowerShell 5.1 会把它升级成**终止性** NativeCommandError，`2>$null`
#   拦不住。症状：前 7 步全绿、第 8 步直接中断，开机自启没注册，
#   第 9 步的交付信息也不打印。
#   这里临时降回 Continue，并用 `*>&1` 把各个流合进成功流。
$prevEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    schtasks /Delete /TN $taskName /F *>&1 | Out-Null
    schtasks @stArgs *>&1 | Out-Null
    $taskRc = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEap
}
if ($taskRc -ne 0) { Warn "注册计划任务失败（退出码 $taskRc），可手工在「任务计划程序」里加一条：powershell -File $runScript" }
else { Ok "已注册计划任务「$taskName」（开机自动运行）" }

# ★ 注册完必须**真的启动**，并再验一次健康 —— 否则会出现「部署 9 步全绿、
#   过一会儿外网全 502」：第 7 步那个试跑进程是本脚本的子进程，SSH 会话
#   一断就被一起带走；而任务只被 `/Create` 过，从未 `/Run`
#   （实测 `Last Result` 停在 267011 = SCHED_S_TASK_HAS_NOT_RUN，
#   2026-10-23）。
#   还要先按端口杀掉旧进程：任务里的新实例撞上 10048 会立刻退出，
#   而表面上「任务已经跑过了」。
$startOk = $false
foreach ($attempt in 1..2) {
    $owners2 = @()
    try {
        $owners2 = (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
                    Select-Object -ExpandProperty OwningProcess -Unique)
    } catch { }
    foreach ($procId in $owners2) {
        if ($procId) { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
    }
    Start-Sleep -Milliseconds 600
    $prevEap3 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        schtasks /Run /TN $taskName *>&1 | Out-Null
        $runRc = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prevEap3
    }
    foreach ($i in 1..20) {
        Start-Sleep -Milliseconds 800
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3 -UseBasicParsing
            if ($r.StatusCode -eq 200) { $startOk = $true; break }
        } catch { }
    }
    if ($startOk) { break }
    Warn "第 $attempt 次启动没起来（schtasks 退出码 $runRc），重试"
}
if ($startOk) {
    Ok "计划任务已启动，/api/health 200（SSH 断开后仍继续跑）"
} else {
    Warn "任务没起来，日志最后 20 行："
    if (Test-Path "$logDir\relay.log") { Get-Content "$logDir\relay.log" -Tail 20 }
    Fail "开机自启已注册，但没启动成功"
}

# ── 8b. 日志轮转 ──────────────────────────────────────────────────────────
Step "8b" "注册日志轮转（每天 03:30，超过 20 MB 才转）"
# ★ 为什么必须做：`*>> relay.log` 是**裸追加**，没有上限。1 vCPU / 小盘
#   实例上，访问日志迟早把盘写满 —— 那时 relay 不会报错，只会静默挂掉
#   （`docs/08` §4.1.2 只给了 Docker 的 `daemon.json` 方案，Windows 这条路
#   一直没有对应物）。实证见 `docs/11` ISSUE-032。
# ★ Windows 的 `*>>` 会**占住文件句柄**，运行中改名必然失败（不像 Linux
#   可以先 rename 再 reopen）。所以只能「停 → 转 → 启」，代价是每天凌晨
#   约 2 秒的不可用。
$rotateScript = Join-Path $InstallDir "rotate-relay.ps1"
$rotateBody = @"
# PhotoBridge 云端中继日志轮转 —— 由 deploy.ps1 生成。
`$ErrorActionPreference = "Continue"
`$logFile = "$logDir\relay.log"
if (Test-Path `$logFile) {
    `$sizeMb = [math]::Round((Get-Item `$logFile).Length / 1MB, 1)
    if (`$sizeMb -ge 20) {
        schtasks /End /TN "$taskName" *>&1 | Out-Null
        Start-Sleep -Seconds 2
        `$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        Move-Item -Force `$logFile "$logDir\relay-`$stamp.log"
        Get-ChildItem "$logDir\relay-*.log" |
            Sort-Object LastWriteTime -Descending |
            Select-Object -Skip 7 |
            Remove-Item -Force -ErrorAction SilentlyContinue
        schtasks /Run /TN "$taskName" *>&1 | Out-Null
    }
}
"@
Set-Content -Path $rotateScript -Value $rotateBody -Encoding UTF8
$prevEap2 = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    schtasks /Create /TN "PhotoBridge Rotate Logs" /SC DAILY /ST 03:30 `
        /RU SYSTEM /RL HIGHEST `
        /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$rotateScript`"" `
        /F *>&1 | Out-Null
    $rotRc = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $prevEap2
}
if ($rotRc -eq 0) { Ok "已注册计划任务「PhotoBridge Rotate Logs」" }
else { Warn "日志轮转任务没注册上（退出码 $rotRc），可手工加：powershell -File $rotateScript" }

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
