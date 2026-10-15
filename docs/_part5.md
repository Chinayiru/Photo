
---

## 5. 电脑端部署

> **前置条件**：§4 云端已跑起来、`https://域名` 返回 200、账号已创建。
> **本章目标**：电脑端托盘常驻、隧道连上云端、后台索引完成、手机能看到照片。

### 5.1 电脑端的两种运行方式

| 方式 | 用途 | 优点 | 缺点 |
|---|---|---|---|
| **开发模式**（源码 + Python） | 开发调试、C1–C5 阶段 | 改代码立即生效、能看完整报错 | 需要装 Python、依赖 |
| **打包模式**（exe） | 日常使用、C6 之后 | 免安装、双击就跑 | 改代码要重新打包 |

**日常使用装打包模式**；开发调试用开发模式。两者**共用同一个 `data/` 目录**，但**不要同时运行**（单实例锁会拒绝第二个）。

### 5.2 开发模式运行

#### 5.2.1 环境准备

**已验证的环境**（[09](09-开发计划与任务分解.md) §4.1）：

| 项 | 实测值 |
|---|---|
| Python | **3.12.10** |
| 安装路径 | `C:\Users\alice\AppData\Local\Programs\Python\Python312` |
| Node.js | **v24.15.0** |
| pnpm | 12.11.2 |

```powershell
# 确认 Python 版本
python --version

# 确认 pip
python -m pip --version
```

#### 5.2.2 创建虚拟环境

**★ 注意**：[09](09-开发计划与任务分解.md) §4.1 指出 `.venv` 目前是**空的**（之前安装被中断），需要重建。

```powershell
# 进入项目目录
cd D:\Photos

# 删除可能存在的空 .venv
if (Test-Path .venv) { Remove-Item -Recurse -Force .venv }

# 创建虚拟环境
python -m venv .venv

# 激活（PowerShell）
.\.venv\Scripts\Activate.ps1
```

**如果报错** `无法加载文件 ... Activate.ps1，因为在此系统上禁止运行脚本`：

```powershell
# 临时放宽当前会话的执行策略
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

# 再激活
.\.venv\Scripts\Activate.ps1
```

**激活成功的标志**：命令行前面出现 `(.venv)`：

```
(.venv) PS D:\Photos>
```

#### 5.2.3 安装依赖

```powershell
# 确认 pip 是最新的
python -m pip install --upgrade pip

# 安装后端依赖
pip install -r backend\requirements.txt
```

**如果还没有 requirements.txt**，手动装（与设计文档 §11 对应）：

```powershell
pip install fastapi "uvicorn[standard]" websockets httpx python-multipart `
            pillow pillow-heif send2trash pyjwt bcrypt `
            exifread pystray pywin32 platformdirs
```

| 包 | 用途 |
|---|---|
| `fastapi` / `uvicorn` | 本地 HTTP 服务 |
| `websockets` | 隧道 WSS 客户端 |
| `httpx` | HTTP 客户端（调用云端 API） |
| `pillow` | 缩略图生成 |
| **`pillow-heif`** | **HEIC 解码（★ 必需，否则 iPhone 照片看不了）** |
| **`send2trash`** | **删除进回收站（★ 硬约束③：代码里不许有 `os.unlink`）** |
| `exifread` | EXIF 时间/GPS/相机参数提取 |
| `pystray` | 系统托盘图标 |
| `pywin32` | Windows 开机自启、单实例锁 |
| `python-multipart` | 文件上传表单解析 |
| `bcrypt` / `pyjwt` | 密码哈希 / 令牌 |

**验证 HEIC 支持**（★ 关键，iPhone 照片全靠它）：

```powershell
python -c "import pillow_heif; pillow_heif.register_heif_opener(); from PIL import Image; print('pillow-heif OK', pillow_heif.__version__)"
```

**预期输出**：`pillow-heif OK 0.20.0`

> 如果报错 `ImportError: cannot import name '_imaging'` 之类，先 `pip install --upgrade pillow`。

#### 5.2.4 配置

创建配置文件（**不要提交到 git**）：

```powershell
cd D:\Photos

# 从模板复制
Copy-Item backend\config.example.toml backend\config.toml
notepad backend\config.toml
```

**`backend\config.toml` 模板**：

```toml
# PhotoBridge 电脑端配置
# ★ 本文件含 agent_token，不要提交到 git

[cloud]
# 云端地址（必须与 §4 的域名一致）
base_url = "https://photos.example.com"
# 账号（用于首次注册设备，注册后可留空）
email = "you@example.com"

[device]
# 设备显示名（手机上看到的就是这个名字）
name = "家里的电脑"
# ★ 首次注册后云端会返回 agent_token，自动写入这里
agent_token = ""
device_id = ""

[server]
# 本地 HTTP 服务监听地址
host = "0.0.0.0"
port = 8787
# ★ 安全：只允许私网访问（见 §5.8）
allow_public_bind = false

[library]
# ★ 库目录（照片所在地，可配多个 root）
[[library.roots]]
id = "main"
path = "D:\\Photos"
mode = "readwrite"       # readonly | readwrite | upload
enabled = true

[index]
# 索引器
scan_on_start = true
follow_symlinks = false   # ★ 必须 false（沙箱要求）
# 图片白名单（设计文档 §5.3）
extensions = ["jpg","jpeg","png","webp","gif","bmp","tif","tiff","avif","heic","heif"]
# ★ 明确排除（预期行为，不是 bug）
exclude_extensions = ["jpg-lx2"]
exclude_patterns = ["**/@eaDir/**", "**/.thumbnails/**"]

[thumbs]
# 缩略图尺寸
thumb_size = 320
preview_size = 1600
quality = 82

[upload]
# 分片大小（设计文档 §7.2）
chunk_size_lan = 8388608      # 8 MB（局域网直连）
chunk_size_cloud = 2097152    # 2 MB（走云端）

[log]
level = "INFO"
dir = "data/logs"
```

> **★ 注意路径写法**：TOML 里反斜杠要写两个 `\\`，或用正斜杠 `E:/software/相册`。

#### 5.2.5 启动

```powershell
# 确保虚拟环境已激活（命令行前有 (.venv)）
cd D:\Photos

# 启动（开发模式）
python -m backend.app.main
```

**或者用 uvicorn 直接启动**（便于调试）：

```powershell
uvicorn backend.app.main:app --host 0.0.0.0 --port 8787 --reload
```

**预期输出**：

```
INFO:     PhotoBridge Agent v0.3.0 启动中...
INFO:     [config] 库目录: D:\Photos (mode=readwrite)
INFO:     [sandbox] 沙箱初始化完成，root=main
INFO:     [db] 数据库已连接: data/photobridge.db
INFO:     [db] 迁移检查: schema_version=1 (最新)
INFO:     [net] 检测到本机 IPv4: 192.0.2.10, 26.26.192.40, 192.168.64.1, ...
INFO:     [net] ★ 选定局域网地址: 192.0.2.10
INFO:     [net] 排除虚拟网卡: Radmin VPN, VMware VMnet1, VMware VMnet8, Hyper-V
INFO:     [tunnel] 连接云端 wss://photos.example.com/agent/ws ...
INFO:     [tunnel] HELLO 已发送: device_id=dev_xxx, lan_ips=['192.0.2.10']
INFO:     [tunnel] HELLO_ACK 收到: heartbeat=25s, max_frame=65536, credit_window=8MB
INFO:     [tunnel] ✓ 隧道已建立
INFO:     [server] 本地服务已启动: http://192.0.2.10:8787
INFO:     [index] 后台索引已启动...
INFO:     [tray] 托盘图标已显示
```

**★ 逐行确认**：

| 日志行 | 检查什么 |
|---|---|
| `选定局域网地址: 192.0.2.10` | ★ **必须是 192.0.2.10**，不是 26.26.192.40 等虚拟网卡（§10 场景 18） |
| `隧道已建立` | ★ 云端连上了。没这行说明隧道不通 |
| `本地服务已启动` | 局域网直连可用 |
| `托盘图标已显示` | 托盘正常 |

#### 5.2.6 本地验证

```powershell
# 1. 本地健康检查
curl.exe -sS http://127.0.0.1:8787/api/system/ping

# 2. 用局域网 IP 访问（模拟手机直连）
curl.exe -sS http://192.0.2.10:8787/api/system/ping

# 3. 从云端绕一圈（★ 这条最重要，验证整条链路）
curl.exe -sS https://photos.example.com/d/你的device_id/api/system/ping
```

**预期输出**（第 3 条）：

```json
{"ok":true,"device":"家里的电脑","version":"0.3.0","via":"tunnel"}
```

> `"via":"tunnel"` 说明请求**从云端经隧道转发到了你的电脑**——这就是整个架构的核心链路打通了。

### 5.3 打包模式（PyInstaller）

#### 5.3.1 安装 PyInstaller

```powershell
.\.venv\Scripts\Activate.ps1
pip install pyinstaller
```

#### 5.3.2 打包命令

```powershell
cd D:\Photos

pyinstaller `
  --noconfirm `
  --clean `
  --onedir `
  --windowed `
  --name PhotoBridge `
  --icon backend\assets\photobridge.ico `
  --add-data "frontend\dist;frontend/dist" `
  --add-data "backend\assets;backend/assets" `
  --hidden-import pillow_heif `
  --hidden-import pystray._win32 `
  --hidden-import win32timezone `
  --collect-all pillow_heif `
  backend\app\main.py
```

**参数逐个解释**：

| 参数 | 为什么 |
|---|---|
| `--onedir` | ★ **用目录模式不用单文件**。单文件每次启动要解压到临时目录（慢 3–5 秒），且杀软误报率高 |
| `--windowed` | 不弹黑色控制台窗口（托盘应用不需要） |
| `--name PhotoBridge` | 产物名 |
| `--add-data "frontend\dist;..."` | 把前端构建产物打进去（relay/本地服务要托管） |
| `--hidden-import pillow_heif` | ★ pillow-heif 是动态加载的，不显式声明会被漏掉，导致 **HEIC 照片全挂** |
| `--hidden-import pystray._win32` | pystray 的后端也是动态加载 |
| `--hidden-import win32timezone` | pywin32 的常见缺失模块 |
| `--collect-all pillow_heif` | 把 pillow-heif 的原生 DLL 一起打包（★ 只写 hidden-import 不够） |

#### 5.3.3 产物结构

```
D:\Photos\dist\PhotoBridge\
├── PhotoBridge.exe              # ★ 双击这个
├── _internal\                   # 运行时依赖（别动）
│   ├── python312.dll
│   ├── base_library.zip
│   ├── pillow_heif\
│   │   └── libheif.dll          # ★ HEIC 解码靠它
│   ├── frontend\dist\           # 前端产物
│   └── ...
└── ...
```

**★ 关键：`data/` 目录不在 dist 里**

运行时数据（数据库、日志、缩略图）默认放在**用户数据目录**或**exe 同级目录**：

| 方案 | 路径 | 说明 |
|---|---|---|
| **exe 同级**（本项目采用） | `D:\Photos\dist\PhotoBridge\data\` | 便携，拷走整个目录就能迁移 |
| 用户目录 | `%LOCALAPPDATA%\PhotoBridge\` | 更规范，但不好备份 |

> **建议**：把整个 `PhotoBridge\` 目录拷到固定位置（如 `D:\Apps\PhotoBridge\`），
> 这样 `data\photobridge.db` 路径固定，备份脚本好写。

#### 5.3.4 打包验证清单

| # | 验证项 | 怎么验证 |
|---|---|---|
| 1 | 双击 exe 能启动 | 托盘出现图标 |
| 2 | 没有控制台黑窗 | — |
| 3 | **没装 Python 的机器能跑** | ★ 在另一台电脑上试（或临时改 PATH 验证） |
| 4 | HEIC 能显示 | 打开一张 iPhone 的 .heic 照片 |
| 5 | 托盘菜单完整 | 右键图标看菜单 |
| 6 | 数据库能创建 | 看 `data\photobridge.db` 是否生成 |
| 7 | 日志能写 | 看 `data\logs\` 是否有文件 |

### 5.4 首次运行向导

第一次启动 exe 时会弹出**配置向导**（[09](09-开发计划与任务分解.md) C6 DoD）。

#### 5.4.1 向导四步

```
┌─────────────────────────────────────────────────────────┐
│  步骤 1/4：连接云端                                      │
│                                                          │
│  云端地址: [https://photos.example.com          ]        │
│  邮箱:     [you@example.com                     ]        │
│  密码:     [••••••••••••                        ]        │
│                                                          │
│           [测试连接]          [下一步 >]                 │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│  步骤 2/4：注册设备                                      │
│                                                          │
│  设备名称: [家里的电脑                          ]        │
│                                                          │
│  ⏳ 正在向云端注册...                                    │
│  ✓ 注册成功                                              │
│    device_id:  dev_a1b2c3d4e5f6                          │
│    agent_token: 已保存到 config.toml（不在界面显示）      │
│                                                          │
│                              [下一步 >]                  │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│  步骤 3/4：确认照片库目录                                │
│                                                          │
│  库目录: [D:\Photos              ] [浏览...]      │
│                                                          │
│  ✓ 目录存在                                              │
│  ✓ 可读写（当前权限: 完全控制）                          │
│  ✓ 找到 4952 个文件                                      │
│  ⚠ 其中 22 个 .jpg-lx2 将被跳过（小米相册残留，预期行为）│
│                                                          │
│  访问模式:  ○ 只读   ● 读写（允许删除到回收站）          │
│                                                          │
│                              [下一步 >]                  │
└─────────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────────┐
│  步骤 4/4：完成                                          │
│                                                          │
│  ✓ 隧道已建立（云端在线）                                │
│  ✓ 本地地址: http://192.0.2.10:8787                   │
│  ⏳ 后台索引已启动（预计 1–3 分钟）                      │
│                                                          │
│  □ 开机自动启动（默认不勾）                              │
│                                                          │
│                    [完成]                                │
└─────────────────────────────────────────────────────────┘
```

#### 5.4.2 每一步在做什么

| 步骤 | 后台动作 | 失败会怎样 |
|---|---|---|
| 1 | `POST /api/auth/login` 拿会话 | 报"账号或密码错误" |
| 2 | `POST /api/devices` 注册设备，云端返回 `device_id` + `agent_token` | 报"注册失败"，检查云端是否可用 |
| 3 | 校验目录存在 + 权限 + 试算文件数 | 报"目录不可写"，检查权限 |
| 4 | 用 `agent_token` 建立 WSS 隧道 + 启动索引 | 隧道失败会重连（指数退避） |

#### 5.4.3 向导完成后手动验证

```powershell
# 1. 云端能看到这台设备
curl.exe -sS https://photos.example.com/api/devices -b cookies.txt

# 2. 设备状态是 online
curl.exe -sS https://photos.example.com/api/devices/dev_xxx/status -b cookies.txt
```

**预期输出**：

```json
{"device_id":"dev_a1b2c3d4e5f6","name":"家里的电脑","online":true,"last_seen":"2026-02-15T14:30:00+08:00","lan_ips":["192.0.2.10"]}
```

### 5.5 托盘菜单说明

右键托盘图标，菜单如下（[设计文档.md](../设计文档.md) §10.2）：

| 菜单项 | 作用 | 什么时候用 |
|---|---|---|
| **打开面板** | 在浏览器打开本地面板 `http://127.0.0.1:8787` | 想在本机看照片、检查状态 |
| **显示配对码** | 弹窗显示 6 位配对码 + 二维码 | ★ **手机配对时用**（§6.2） |
| **复制局域网地址** | 把 `http://192.0.2.10:8787` 复制到剪贴板 | 手机在同一 WiFi 时直接粘贴访问 |
| **重新扫描** | 触发一次全量/增量索引 | 手动加了照片、改了目录结构 |
| **查看审计日志** | 打开 `data\audit.log` | 想知道"谁在什么时候删了什么" |
| **退出** | 关闭程序 | 需要彻底停止时 |

**托盘图标状态含义**：

| 图标 | 含义 |
|---|---|
| 🟢 绿色 | 隧道已连接，云端在线，一切正常 |
| 🟡 黄色 | 正在连接 / 重连中 |
| 🔴 红色 | 隧道断开（云端不可达或网络问题） |
| ⚫ 灰色 | 已退出 / 索引未完成 |

**菜单里没有的**（有意不做）：暂停上传、修改库目录、切换账号——改这些要编辑 `config.toml` 后重启。

### 5.6 开机自启配置

#### 5.6.1 默认行为

**默认是关闭的**（[设计文档.md](../设计文档.md) §10.2）。原因：你可能不想让它在你不注意时后台跑。

#### 5.6.2 手动开启（注册表）

**注册表位置**：

```
HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run
```

**方法 A：用 PowerShell 添加**（推荐）

```powershell
# ★ 把路径换成你的实际 exe 路径
$exePath = "D:\Apps\PhotoBridge\PhotoBridge.exe"

# 写入自启项
New-ItemProperty `
  -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
  -Name "PhotoBridge" `
  -Value "`"$exePath`"" `
  -PropertyType String `
  -Force

# 验证
Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" | Select-Object PhotoBridge
```

**预期输出**：

```
PhotoBridge
-----------
"D:\Apps\PhotoBridge\PhotoBridge.exe"
```

**方法 B：用注册表编辑器**

```
1. Win + R → 输入 regedit → 回车
2. 导航到 HKEY_CURRENT_USER\Software\Microsoft\Windows\CurrentVersion\Run
3. 右键右侧空白 → 新建 → 字符串值
4. 名称: PhotoBridge
5. 双击，数值数据填: "D:\Apps\PhotoBridge\PhotoBridge.exe"   ← 注意要有引号
6. 确定
```

**方法 C：用任务计划程序**（可延迟启动，更温和）

```powershell
# 创建开机启动任务（延迟 30 秒，等网络就绪）
$action = New-ScheduledTaskAction -Execute "D:\Apps\PhotoBridge\PhotoBridge.exe"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = "PT30S"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "PhotoBridge" -Action $action -Trigger $trigger -Settings $settings -Force
```

> **为什么推荐延迟 30 秒**：开机时网络还没就绪，立即启动会导致隧道连接失败（虽然会自动重连，但会刷一堆错误日志）。

#### 5.6.3 关闭自启

```powershell
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "PhotoBridge"
```

#### 5.6.4 验证自启生效

```powershell
# 重启电脑后检查进程
Get-Process PhotoBridge -ErrorAction SilentlyContinue

# 或看托盘图标
```

### 5.7 防火墙放行 8787

#### 5.7.1 ★ 先理解：这一步只影响局域网直连

| 访问路径 | 需要放行 8787 吗 |
|---|---|
| 手机走**云端中继**（4G/外网） | ❌ **不需要**。流量走的是 443 出站 |
| 手机走**局域网直连**（同 WiFi） | ✅ **需要** |

> **换句话说**：即使防火墙没放行 8787，**功能也是完整的**，只是手机在家时不能走高速直连，
> 会退化成走云端（慢一些、费流量）。见 §10 场景 18。

#### 5.7.2 首次监听时的提示

程序第一次监听 8787 时，Windows 会弹出防火墙对话框：

```
Windows 安全中心警报
允许 PhotoBridge 在这些网络上通信：
  ☑ 专用网络        ← ★ 只勾这个
  ☐ 公用网络        ← ★ 不要勾！
[允许访问]  [取消]
```

**★ 只勾"专用网络"**。勾了"公用网络"意味着在咖啡厅、机场的 WiFi 上也会监听，有安全风险。

#### 5.7.3 用命令放行（如果错过了弹窗）

**PowerShell（管理员权限）**：

```powershell
# ★ 用管理员身份打开 PowerShell
# 只放行专用网络
New-NetFirewallRule `
  -DisplayName "PhotoBridge LAN (TCP 8787)" `
  -Direction Inbound `
  -Protocol TCP `
  -LocalPort 8787 `
  -Action Allow `
  -Profile Private `
  -Description "PhotoBridge 局域网直连，仅专用网络"
```

**用 netsh（兼容老系统）**：

```cmd
netsh advfirewall firewall add rule ^
  name="PhotoBridge LAN (TCP 8787)" ^
  dir=in ^
  action=allow ^
  protocol=TCP ^
  localport=8787 ^
  profile=private
```

#### 5.7.4 验证

```powershell
# 1. 看规则是否存在
Get-NetFirewallRule -DisplayName "PhotoBridge LAN*" | Select-Object DisplayName, Enabled, Profile, Direction

# 2. 看端口是否在监听
netstat -ano | findstr :8787
```

**预期输出**：

```
TCP    0.0.0.0:8787           0.0.0.0:0              LISTENING       12345
```

**★ 重要检查**：`0.0.0.0:8787` 表示监听所有网卡（含公网）。程序启动时会检测并**拒绝绑定公网 IP**
（[设计文档.md](../设计文档.md) §8.5），所以正常情况你会看到：

```
TCP    192.0.2.10:8787     0.0.0.0:0              LISTENING       12345
```

| 看到什么 | 说明 |
|---|---|
| `192.0.2.10:8787` | ✅ 正确，只监听私网 |
| `0.0.0.0:8787` | ⚠️ 监听所有网卡，检查 `config.toml` 的 `host` 和 `allow_public_bind` |
| 没有输出 | 程序没在跑，或没监听 8787 |

#### 5.7.5 从手机验证局域网直连

在手机浏览器打开：

```
http://192.0.2.10:8787/api/system/ping
```

| 结果 | 说明 |
|---|---|
| 返回 JSON | ✅ 局域网直连可用 |
| 一直转圈/超时 | 检查：手机是否同一 WiFi、防火墙是否放行、IP 是否正确 |

### 5.8 库目录权限说明

#### 5.8.1 为什么需要写权限

直觉上"看照片"只需要读权限，但这个项目**需要写权限**，原因有三个：

| # | 为什么需要写 | 对应功能 |
|---|---|---|
| 1 | **上传要落盘** | 手机拍的照片要写进 `D:\Photos\2024\2024-05\` |
| 2 | **删除要进回收站** | `send2trash` 需要写权限才能把文件移到 `$Recycle.Bin` |
| 3 | **移动/改名要写** | 归档整理功能需要改文件路径 |

#### 5.8.2 四种访问模式

`config.toml` 里 `mode` 有四种取值，**本项目用 `readwrite`**：

| mode | 读 | 写 | 上传 | 删除 | 适用场景 |
|---|---|---|---|---|---|
| `readonly` | ✅ | ❌ | ❌ | ❌ | 只想看，不想让程序改任何东西 |
| `upload` | ✅ | ✅（仅新增） | ✅ | ❌ | **最安全**：能上传但不能删 |
| **`readwrite`** | ✅ | ✅ | ✅ | ✅（进回收站） | **本项目采用** |
| `disabled` | ❌ | ❌ | ❌ | ❌ | 临时禁用某个 root |

> **如果你更保守**：把 `mode` 改成 `upload`。这样程序只能新增文件，**永远不可能删你的照片**。
> 代价是"删除照片"功能不可用（手机上看得到但删不掉）。

#### 5.8.3 ★ 四条硬约束（[设计文档.md](../设计文档.md) §8.4）

这四条是**代码层面强制**的，不是"建议"：

| # | 约束 | 具体含义 | 为什么 |
|---|---|---|---|
| **①** | **所有文件操作必须过沙箱** | 任何路径先经 `sandbox.resolve(root_cfg, rel_path, need)` 七步校验 | 防路径穿越（`..\..\Windows\System32`）导致任意文件读写/删除 |
| **②** | **上传只新增，绝不覆盖** | 同名文件加 `_1`/`_2` 后缀；`sha256` 命中判重直接秒传；**绝不 rename 到已存在的文件** | 覆盖 = 数据丢失。你的照片是不可再生的 |
| **③** | **删除只进回收站** | 用 `send2trash`；**代码里不存在 `os.unlink`**（有静态检查） | 误删可恢复 |
| **④** | **删除/移动写审计日志** | 写 `data\audit.log`，含时间、操作、路径、来源 IP | 出问题能追溯 |

**验证这四条是否生效**：

```powershell
# ① 沙箱：测试路径穿越被拒
curl.exe -sS "http://127.0.0.1:8787/api/items/..%2F..%2FWindows%2Fwin.ini"
# 预期：403 Forbidden 或 400 Bad Request

# ③ 检查代码里没有 os.unlink（在项目目录执行）
Select-String -Path "backend\app\*.py" -Pattern "os\.unlink|os\.remove|shutil\.rmtree"
# 预期：无输出（或只在注释里）

# ④ 审计日志存在
Get-Content D:\Photos\data\audit.log -Tail 5
```

#### 5.8.4 怎么检查/设置目录权限

**检查当前权限**：

```powershell
icacls "D:\Photos"
```

**预期输出**（关键是有你的用户 + `(M)` 或 `(F)`）：

```
D:\Photos BUILTIN\Administrators:(OI)(CI)(F)
                 NT AUTHORITY\SYSTEM:(OI)(CI)(F)
                 DESKTOP-XXX\18240:(OI)(CI)(M)      ← ★ (M) = 修改权限
```

| 权限字母 | 含义 | 够用吗 |
|---|---|---|
| `(F)` | 完全控制 | ✅ |
| `(M)` | 修改（读+写+删） | ✅ |
| `(RX)` | 读取和执行 | ❌ 不能上传/删除 |
| `(R)` | 只读 | ❌ |

**给当前用户加修改权限**：

```powershell
# 用管理员 PowerShell
icacls "D:\Photos" /grant "$env:USERNAME:(OI)(CI)(M)" /T
```

> `/T` 表示递归应用到所有子目录。**对 4952 个文件执行需要一点时间。**

**注意**：

| 坑 | 说明 |
|---|---|
| 目录在移动硬盘上 | 移动硬盘的权限行为不同，且**拔了盘程序就找不到照片** |
| 目录是 OneDrive 同步目录 | ★ **不要用**！OneDrive 的"按需下载"会让文件变成占位符，读取时卡顿甚至失败 |
| 目录在网络共享（NAS）上 | 支持但慢，且 `send2trash` 对网络路径可能不工作 |

#### 5.8.5 库目录的读写测试

程序启动时会自己做一次读写测试。你也可以手动验证：

```powershell
# 测试写权限
$testFile = "D:\Photos\.photobridge_write_test"
"test" | Out-File -FilePath $testFile -Encoding utf8
if (Test-Path $testFile) { 
    Write-Host "✓ 写权限正常" -ForegroundColor Green
    Remove-Item $testFile -Force 
} else { 
    Write-Host "✗ 没有写权限" -ForegroundColor Red 
}
```

### 5.9 §5 完成检查清单

| # | 检查项 | 验证命令/方法 | 预期 |
|---|---|---|---|
| 1 | Python 环境正常 | `python --version` | 3.12.10 |
| 2 | 虚拟环境已激活 | 命令行前有 `(.venv)` | ✅ |
| 3 | 依赖已安装 | `pip list \| findstr pillow-heif` | 有输出 |
| 4 | **HEIC 支持正常** | `python -c "import pillow_heif..."` | `pillow-heif OK` |
| 5 | config.toml 已配 | 打开看 `library.roots` | 指向 `D:\Photos` |
| 6 | 程序能启动 | `python -m backend.app.main` | 无报错 |
| 7 | **选中了正确的局域网 IP** | 看启动日志 | `192.0.2.10` |
| 8 | 隧道已建立 | 日志有 `隧道已建立` | ✅ |
| 9 | 本地 ping 通 | `curl http://127.0.0.1:8787/api/system/ping` | JSON |
| 10 | **云端链路通** | `curl https://域名/d/{dev}/api/system/ping` | `"via":"tunnel"` |
| 11 | 托盘图标显示 | 看任务栏 | 🟢 |
| 12 | 库目录可读写 | §5.8.5 测试 | ✓ 写权限正常 |
| 13 | 索引已完成 | 本地面板看数量 | ≈ 4952 张 |
| 14 | 防火墙已放行 8787 | `Get-NetFirewallRule` | 存在，Profile=Private |
| 15 | 只监听私网 | `netstat -ano \| findstr :8787` | `192.0.2.10:8787` |

---

## 6. 手机端

> **前置条件**：电脑端在线（托盘 🟢）、云端可用。
> **本章目标**：手机 4G 能看照片墙、能上传、能添加到主屏幕。

### 6.1 手机端不需要装任何 App

这是本项目的核心设计之一（[11](11-风险登记册与决策记录.md) ADR-009 对比 Tailscale 时的关键论点）：
**手机端就是一个 PWA 网页**。

| 对比项 | 本项目（PWA 网页） | 传统 App |
|---|---|---|
| 安装 | 浏览器打开网址 → 添加到主屏幕 | 应用商店下载安装 |
| 更新 | **自动**（刷新即最新） | 要重新下载 |
| 跨平台 | 安卓/iOS 都能用 | 要分别开发 |
| 上架 | **不需要**（不用审核、不用备案） | 要开发者账号 |
| 能力 | 相机、文件、离线缓存、全屏 | 更多（后台任务等） |

### 6.2 首次配对流程

#### 6.2.1 完整流程

```
┌──────────────────────────────────────────────────────────────┐
│  步骤 1：电脑端显示配对码                                     │
│                                                               │
│  在电脑上：右键托盘图标 → 显示配对码                          │
│                                                               │
│  ┌────────────────────────────┐                              │
│  │  配对码                     │                              │
│  │                             │                              │
│  │      4 8 2 9 1 7            │  ← 6 位数字                  │
│  │                             │                              │
│  │  ▓▓▓▓▓▓▓▓▓▓▓▓▓              │  ← 二维码                    │
│  │  ▓▓▓▓▓▓▓▓▓▓▓▓▓              │                              │
│  │                             │                              │
│  │  有效期：4:32               │  ← 5 分钟倒计时              │
│  └────────────────────────────┘                              │
│                                                               │
│  ★ 配对码 5 分钟有效，过期后重新点一次即可                    │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  步骤 2：手机打开网址                                         │
│                                                               │
│  手机浏览器（推荐 Chrome）打开：                              │
│      https://photos.example.com                               │
│                                                               │
│  看到登录页                                                   │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  步骤 3：登录                                                 │
│                                                               │
│  邮箱: [you@example.com          ]                            │
│  密码: [••••••••••••             ]                            │
│                                                               │
│  [登录]                                                       │
│                                                               │
│  □ 记住我（30 天）                                            │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  步骤 4：选择设备                                             │
│                                                               │
│  选择要连接的电脑：                                           │
│                                                               │
│  ┌──────────────────────────────────────────┐                │
│  │  🟢 家里的电脑                            │                │
│  │     在线 · 4952 张照片                    │                │
│  │                          [选择]           │                │
│  └──────────────────────────────────────────┘                │
│                                                               │
│  （如果设备显示 🔴 离线，检查电脑是否开机）                   │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  步骤 5：输入配对码                                           │
│                                                               │
│  请输入电脑上显示的 6 位配对码：                              │
│                                                               │
│    [4] [8] [2] [9] [1] [7]                                    │
│                                                               │
│  [配对]                                                       │
│                                                               │
│  ⚠ 输错 5 次将锁定 10 分钟                                    │
└───────────────────────────┬──────────────────────────────────┘
                            │
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  步骤 6：配对成功                                             │
│                                                               │
│  ✓ 配对成功！                                                 │
│                                                               │
│  正在跳转到照片墙...                                          │
└──────────────────────────────────────────────────────────────┘
```

#### 6.2.2 配对码的规则（[设计文档.md](../设计文档.md) §3）

| 属性 | 值 |
|---|---|
| 格式 | **6 位数字** |
| 有效期 | **5 分钟**（300 秒） |
| 错误次数上限 | **5 次** |
| 超限锁定 | **10 分钟**（600 秒） |
| 存储 | 云上 `pair_codes` 表 |
| 校验 | 云端校验（电脑端只负责显示） |

> **安全设计说明**：为什么 6 位数字够用？
> 因为 ① 有效期只有 5 分钟；② 5 次错误就锁 10 分钟；③ 攻击者还得先登录你的账号。
> 三者叠加后爆破概率极低（[11](11-风险登记册与决策记录.md) R-014）。

#### 6.2.3 配对完成后云端记录了什么

```bash
# 在服务器上查看
docker compose exec relay python -c "
import sqlite3
c = sqlite3.connect('/data/cloud.db')
for row in c.execute('SELECT id, device_id, name, created_at, last_seen FROM clients'):
    print(row)
"
```

| 表 | 记录什么 |
|---|---|
| `clients` | 哪个手机（client）绑定了哪个设备（device） |
| `pair_codes` | 配对码已标记为已使用 |

**你可以在云端踢掉某个手机**（手机丢了就这么做）：

```bash
# 用 API 删除某个 client
curl -X DELETE https://photos.example.com/api/devices/dev_xxx/clients/client_yyy -b cookies.txt
```

### 6.3 添加到主屏幕（安卓 Chrome）

#### 6.3.1 完整操作步骤

```
1. 用 Chrome 打开 https://photos.example.com
2. 登录并完成配对
3. 点击右上角「⋮」（三个点菜单）
4. 选择「添加到主屏幕」或「安装应用」
   ┌─────────────────────┐
   │  ☆ 添加到收藏夹      │
   │  ⬇ 下载              │
   │  ➕ 添加到主屏幕  ←──┼── 点这个
   │  🔄 刷新             │
   └─────────────────────┘
5. 在弹窗里修改名称（可选）：
   名称: [照片]
6. 点击「添加」
7. 回到桌面，会看到一个新的图标
```

#### 6.3.2 如果没看到「添加到主屏幕」

| 原因 | 解决 |
|---|---|
| 用的不是 Chrome | 换 Chrome，或 Edge（也有类似功能） |
| 页面不是 HTTPS | ★ 必须是 HTTPS（这也是要域名的原因之一） |
| 没有 `manifest.json` | 检查前端是否生成了 PWA manifest |
| Service Worker 未注册 | 检查浏览器控制台（`chrome://inspect` 或页面内 F12） |
| 已经是 PWA 安装状态 | 菜单里会显示「卸载应用」 |

#### 6.3.3 验证 PWA 是否正确

**在电脑上验证**（更方便）：

```powershell
# 用 Chrome 打开网站，按 F12 → Application 标签
# 检查：
#   1. Manifest: 有名称、图标、start_url
#   2. Service Workers: 状态 activated and is running
#   3. Storage: 有缓存条目
```

**在手机上验证**：

```
1. Chrome 地址栏右侧如果有「安装」图标（⊕ 或 ⬇），说明 PWA 正常
2. 安装后打开，应该没有浏览器地址栏（全屏显示）
3. 关掉网络，重新打开——应该能看到缓存的界面（离线外壳）
```

#### 6.3.4 添加到主屏幕后有什么不同

| 项 | 浏览器标签页 | 添加到主屏幕 |
|---|---|---|
| 显示 | 有地址栏、标签栏 | **全屏**，像原生 App |
| 图标 | 浏览器图标 | **独立图标**（你的应用名） |
| 启动 | 要开浏览器找标签页 | 直接点图标 |
| 离线 | 白屏 | 显示离线外壳 |
| 上传保活 | 息屏容易断 | `wakeLock` 更有效 |

### 6.4 权限说明

#### 6.4.1 会请求哪些权限

| 权限 | 什么时候请求 | 用来干什么 | 可以拒绝吗 |
|---|---|---|---|
| **相机** | 点「拍照上传」时 | 直接拍照并上传 | ✅ 可以（用相册选图代替） |
| **文件/存储** | 点「选择照片」时 | 从相册选图上传 | ✅ 可以（但就不能上传了） |
| **通知** | 首次访问时（可选） | 上传完成提醒 | ✅ 可以 |
| **保持唤醒** | 上传大文件时 | `wakeLock` 防息屏中断 | ⚠️ 建议允许（否则上传容易断） |

#### 6.4.2 权限被拒绝了怎么办

**Chrome（安卓）**：

```
1. 打开 Chrome
2. 点右上角「⋮」→ 设置
3. 网站设置 → 找到 photos.example.com
4. 权限 → 相机/存储 → 改为「允许」
5. 刷新页面
```

**更快的办法**：

```
1. 在地址栏左侧点 🔒 或 ⓘ 图标
2. 点「权限」
3. 修改对应权限
4. 刷新页面
```

#### 6.4.3 ★ 相机权限的特殊说明

**为什么需要相机权限**：让你在手机上拍完照直接上传，不用先存到相册再选。

**Web 相机的限制**：

| 限制 | 说明 |
|---|---|
| **必须 HTTPS** | HTTP 下 `getUserMedia` 直接被浏览器拒绝（这是浏览器的硬规定） |
| 后台不能用 | 切到别的 App 相机就停 |
| 画质受浏览器控制 | 一般能拿到 1080p 或更高，但不是原生相机的全部能力 |

> **实用建议**：如果你想要最高画质，**用系统相机拍照 → 再从相册选图上传**。
> 网页相机的画质通常不如原生相机。

### 6.5 双通路状态说明

这是本项目的一个关键体验设计（[设计文档.md](../设计文档.md) §6，[09](09-开发计划与任务分解.md) C5）。

#### 6.5.1 三种状态

| 状态 | 图标 | 含义 | 走什么路径 | 速度 |
|---|---|---|---|---|
| **直连** | 🟢 | 手机和电脑在**同一个 WiFi** | 手机 → `http://192.0.2.10:8787` | **最快**（千兆内网） |
| **云端** | 🔵 | 手机在外网（4G/别的 WiFi） | 手机 → 云 → 隧道 → 电脑 | 受家宽上行限制 |
| **离线** | 🔴 | 电脑不在线 / 云端不可达 | 无 | — |

#### 6.5.2 界面上的位置

界面顶部有一条状态栏：

```
┌────────────────────────────────────────────────┐
│  ← 照片            🟢 直连        4952 张  ⚙  │
└────────────────────────────────────────────────┘
```

点击状态图标可以看到详情：

```
┌──────────────────────────────────────┐
│  连接状态                             │
│                                       │
│  🟢 局域网直连                        │
│  地址: http://192.0.2.10:8787      │
│  延迟: 3 ms                           │
│  速度: 约 80 MB/s                     │
│                                       │
│  切换策略: 自动（直连优先）            │
└──────────────────────────────────────┘
```

#### 6.5.3 探测与切换逻辑（[09](09-开发计划与任务分解.md) C5 DoD）

```
手机打开页面
    │
    ▼
读取电脑端在 HELLO 里上报的 lan_ips（如 ["192.0.2.10"]）
    │
    ▼
并发探测每个 IP 的 /api/system/ping，超时 1.5 秒
    │
    ├── 有响应 ──→ 🟢 直连模式（后续请求全部走局域网）
    │
    └── 全部超时 ──→ 🔵 云端模式（走 https://域名/d/{device_id}/）
                          │
                          ▼
                     定期重试直连探测（每 30 秒）
                          │
                          └── 一旦探测成功 ──→ 自动切回 🟢
```

#### 6.5.4 怎么判断当前走的哪条路

**在手机上**：看状态栏图标（🟢 / 🔵）。

**在电脑上验证**（看请求从哪来）：

```powershell
# 看本地服务日志，直连的请求会记 192.168.x.x
Get-Content D:\Photos\data\logs\agent.log -Tail 20 | Select-String "192.168"
```

**在云端验证**（走云端的请求会在 relay 日志里）：

```bash
docker compose logs --tail=50 relay | grep "/d/"
```

#### 6.5.5 ★ 直连探测失败的常见原因

见 §10 场景 18（这是本项目实测踩过的坑——**本机有 5 个 IPv4，4 个是虚拟网卡**）。

| 原因 | 现象 |
|---|---|
| 选错了 IP（选了虚拟网卡） | 手机永远探测不到，一直 🔵 |
| 防火墙没放行 8787 | 同上 |
| 手机和电脑不在同一 WiFi | 正常，应该走 🔵 |
| 路由器开了「AP 隔离」 | 同一 WiFi 也互相 ping 不通 |
| 电脑休眠了 | 🔴 离线 |

### 6.6 §6 完成检查清单

| # | 检查项 | 怎么验证 | 预期 |
|---|---|---|---|
| 1 | 手机 4G 能打开网站 | 关 WiFi，用流量打开 `https://域名` | 显示登录页 |
| 2 | 能登录 | 输邮箱密码 | 进入设备列表 |
| 3 | 设备显示在线 | 看设备列表 | 🟢 家里的电脑 |
| 4 | 配对成功 | 输 6 位码 | 跳转照片墙 |
| 5 | 照片墙能看 | 浏览时间线 | 缩略图正常 |
| 6 | 首屏够快 | 计时 | ≤ 2.5 秒（C3 DoD） |
| 7 | 双指缩放正常 | 手势 | 流畅 |
| 8 | 左右滑动正常 | 手势 | 流畅 |
| 9 | 能添加到主屏幕 | Chrome 菜单 | 有「添加到主屏幕」 |
| 10 | 主屏幕图标独立 | 看桌面 | 有独立图标 |
| 11 | 全屏显示 | 打开图标 | 无地址栏 |
| 12 | 相机权限正常 | 点拍照上传 | 能调起相机 |
| 13 | 能上传照片 | 选一张上传 | 成功落到库目录 |
| 14 | 直连状态正确 | 连家里 WiFi | 🟢 直连 |
| 15 | 云端状态正确 | 用 4G | 🔵 云端 |
