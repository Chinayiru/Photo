from pathlib import Path
p=Path(r'D:\Photos\report\PhotoBridge-e9805a7-终极测试报告-2026-11-03.md')
s='''# PhotoBridge 终极测试报告

> 验证提交：`e9805a76f3fe29766a2d7876fd1a99bf13106326`  
> 来源分支：`perf/relay-head-progress`  
> 执行日期：2026-11-03（北京时间）  
> 性质：最终回归与收尾评估；不修改产品代码，不修复、不提交、不合并。

## 一、执行结论

**本轮不能给出“全部通过、无条件收尾”的结论。** 环境准备完毕后的全量 pytest 为 **2341 通过、0 失败、8 跳过**；静态检查、前端构建、上传续传、文件整理、目录监听、EXE 打包验证、绝对路径环境中的真实隧道短测均通过。但真实浏览器补充测试确认 **3 个功能缺陷**，旧 Chrome 长流程与综合验收脚本也尚未形成全绿结果。

确认的产品问题：

1. **局域网查看器放大后，预取的原图 Blob URL 被 `safeUrl()` 拦掉，原图层无法显示。** 正常 JPEG 被误报为浏览器不支持原格式。
2. **“我的”页入口二维码请求不带认证头，返回 401，显示破图。** 手动复制入口链接仍可用。
3. **显式传入相对 `data_dir` 时，已验证文件读取将相对路径与绝对路径直接比较，导致缩略图正文中断。** 绝对路径对照环境通过。

本轮未新发现需要立即中止的越权读写、沙箱逃逸、认证绕过或云端持久化媒体问题；已有相关回归用例执行通过。此结论只覆盖本轮代码与隔离环境，**不构成公网部署、真机兼容性或未知漏洞的保证**。

**收尾建议：完成上述 3 项修复及定向复测，再作最终验收；保留真实 4G、长期稳定性与部署检查为明确的未完成项。** 不把“已有自动化回归全绿”替代“产品所有流程已验收”。

### 1.1 主要执行结果

| 项目 | 结果 | 实际证据与边界 |
|---|---|---|
| 最终全量 `pytest backend/tests` | 2341 通过 / 0 失败 / 8 跳过 | 101 个测试文件，收集 2349 项；pytest 耗时 147.21s，55 条警告 |
| `check_lint.py` | 通过 | 21 条规则，0 违规 |
| `check_sandbox.py` | 通过 | 脚本断言全部通过；不替代所有平台文件系统场景 |
| `check_docs.py` | 通过 | 扫描 15 份 Markdown；另有非阻塞提示 |
| `check_frontend_api.py` | 通过 | 路径层检查；不能据此断言字段级契约完整一致 |
| 前端 ESLint | 通过 | `npm run lint`，0 warnings |
| 前端类型检查与构建 | 通过 | `vue-tsc` + Vite；72 modules，JS 205.91 kB / gzip 75.73 kB |
| ffmpeg 冻结校验 | 通过 | ffmpeg / ffprobe 大小、SHA-256 与清单一致 |
| HTTP 上传续传 E2E | 通过 | 断 socket、乱序、重复片、磁盘 offset、重启恢复、SHA-256、一致性与去重 |
| HTTP 改名 / 移动 E2E | 通过 | 同名加序号、已有文件内容不变、审计与重扫一致 |
| 文件监听 E2E | 通过 | 临时库真实文件新增、删除、改名、目录改名及 WebP 解码 |
| 隔离 LAN E2E | 通过 | 真实局域网地址监听与访问，页面、配对、分页、缩略图 |
| Firefox 浏览器脚本 | 通过 | Firefox 159.0；脚本断言通过，未覆盖的二维码破图由补测发现 |
| 原 Chrome 长流程 | 未通过 | 重新配对步骤前置失效，随后脚本异常中断 |
| Chrome 新文档导航补跑 | 未通过 | 已扩大实际覆盖；跨网络提示步骤未通过，后续中断 |
| Chrome 功能子集补跑 | 未通过 | 明确排除 4n，仍复现原图层失败；后段导航异常，非整轮通过 |
| 本地真实云端通路查看器 | 通过 | 未确认前无 raw；确认后解码；切图后重新确认；8 张合成 JPEG |
| 局域网原图定向验证 | **失败** | HTTP raw 已预取，2× 放大后 img 的 src 为空，见 F-01 |
| “我的”页二维码定向验证 | **失败** | 浏览器 DOM 破图、无认证头 401 / 带头 200 对照，见 F-02 |
| 相对路径隧道短测 | **失败** | 576 请求中 144 个缩略图正文中断，见 F-03 |
| 绝对路径隧道短测 | 通过 | 576 请求，0 bad；请求峰值在途 4，最终 0 |
| LAN 短时稳定性 | 通过 | 180s 负载，34 业务轮全部成功；非 24h soak |
| PyInstaller onedir 构建 | 通过 | 从本提交重新构建，不沿用旧 EXE |
| 打包 EXE E2E | 通过 | GUI 子系统、端口占用退出、可移植数据目录、裁剪 Python 环境后仍能配对与出图 |
| 旧综合验收脚本 | 未通过 | 5 条失败均逐条复核，见第五节；原失败记录保留 |

统计口径：pytest 是参数化收集项；E2E 日志中的 `ok` 是脚本检查点；请求数是网络请求。三者不相加，不用于宣称独立用户用例总数或 100% 接口覆盖。

## 二、基线、环境与数据保护

### 2.1 验证对象

- 源仓库：`D:\\Photos`；开始时工作区干净。
- 本轮专用 checkout：`C:\\Users\\alice\\.codex\\worktrees\\final-validation-e9805a7\\远程照片`，固定上述提交。
- 验证范围包括 backend、cloud/relay、shared、frontend、packaging 与已有 scripts。
- Python 3.12.10 x64；Node v24.15.0；Vite 6.4.5；Pillow 13.0.0。
- Chrome 156.0.8078.80 与 Firefox 159.0；Windows 本机、实际 LAN 地址与本机独立 Relay 进程。
- ffmpeg 冻结版本：`9.0.2-essentials_build-www.gyan.dev`。
- 依赖复用现有 `.venv` / `node_modules`，没有在本轮重新建立干净依赖安装环境；打包元数据与锁文件哈希另附。

### 2.2 保护范围与已发生的测试副作用

写入、上传、删除、移动、重命名的验证对象均为脚本生成的临时媒体库；没有对真实主库照片执行写删操作。部分旧脚本会读取现有库的索引快照或路径 / 大小 / mtime 摘要，首次 LAN 脚本还自动发现了现有图库，因此不能表述为“全轮从未读取真实库”。后续 LAN 补跑已固定为临时 8 张 JPEG。

旧脚本中的“真实库一个字节未变”文案强于其判据：路径 / 大小 / mtime 摘要与数据库快照一致，**不等同于逐文件内容 SHA-256 校验**。本报告不沿用该过度表述。

旧综合验收脚本测试开机自启时修改了 HKCU Run，仅恢复了启用布尔状态，留下指向验收 worktree 的命令。发现后已将入口纠正回 `D:\\Photos`，并确认不再指向本轮 worktree。起始命令全文没有预先捕获，不能声称注册表值逐字节恢复；见 `autostart-cleanup.json`。这是测试工具副作用，不是本轮产品代码变更。

## 三、已确认的产品缺陷

### F-01 [重要] 局域网原图预取成功后，Blob URL 被 URL 守卫拒绝

- **位置**：`frontend/src/lib/safeUrl.ts:27`、`:51`；`frontend/src/stores/viewer.ts:188`、`:238`；`frontend/src/views/ViewerView.vue:967`、`:970`。
- **前置**：Chrome，已配对的 LAN 页面；正常可解码 JPEG；原图大小满足预取条件。
- **复现步骤**：进入时间线 → 打开正常 JPEG → 等预览解码与 raw 预取完成 → 真实触摸双击放大到 2×。
- **请求**：浏览器已发出 `GET /api/items/8/raw`，随后没有需要用户确认的云端流程；本用例为局域网。
- **期望**：预览图之上叠加可解码的原图层，复用预取的 Blob URL，两层 transform 一致。
- **实际**：预览图已放大至 `translate(0px, 0px) scale(2)`；原图 img 触发 error，事件捕获值为：

```json
{"src":"","currentSrc":"","complete":true,"naturalWidth":0}
```

  随后原图节点被移除，页面出现“这张的原始格式浏览器无法显示，正在用预览图（最多 1600px）”。测试材料为正常 JPEG，非 HEIC 或损坏图。
- **代码链**：`prefetchRaw()` 将 JPEG 字节存为 `URL.createObjectURL(blob)` → `rawSrcFor()` 返回缓存 Blob URL → 模板执行 `safeUrl(rawSrc)` → 只允许 `http:` / `https:` 的守卫返回空串 → img 解码失败 → `rawFailed=true` 移除原图层。
- **影响**：符合预取条件的局域网图片放大后仍只有预览质量，并产生错误格式提示；原图下载与云端手动查看原图不因此失效。未观察到原件损坏或数据丢失。
- **建议**：为应用自己创建并管理的图片 Blob URL 建立专用的可信渲染路径；保留外部 / 服务端 URL 的严格校验。不要为所有链接、导航和下载场景全面放开 `blob:`。补正常 JPEG 的真实浏览器“预取 → 放大 → 原图解码 → 关闭释放”回归。
- **置信度**：确认。旧 Chrome 流程与独立临时服务均重现；云端 HTTP 原图对照成功。
- **证据**：[局域网结果](验收证据-e9805a7-2026-11-03/lan-viewer-evidence.json)、[复现日志](验收证据-e9805a7-2026-11-03/e2e-lan-viewer-evidence.log)、[页面截图](验收证据-e9805a7-2026-11-03/lan-viewer-original.png)。该取证脚本 exit 0 代表成功证明缺陷，JSON 中 `passed=false`，不能计为产品通过。

### F-02 [重要] “我的”页入口二维码缺少认证，显示破图

- **位置**：`frontend/src/views/SettingsView.vue:136`、`:328`；`backend/app/routers/__init__.py:38`；`backend/app/auth.py:230`、`:794`。
- **复现步骤**：完成正常配对 → 打开“我的” → 滚动至“入口”二维码。
- **请求**：`GET /api/system/entrances/qr?kind=lan`，由原生 img 发起，无 Authorization。即使浏览器有配对 cookie，该接口也不在 cookie 读取回退白名单中，且不属于匿名入口。
- **期望**：已配对用户看到可解码 PNG 二维码。
- **实际**：DOM 为 `complete=true, naturalWidth=0`。同一浏览器请求对照：

```http
GET /api/system/entrances/qr?kind=lan
Authorization: （无）

HTTP/1.1 401
```

```json
{"error":{"code":"E2002","message":"client_token_invalid","detail":"缺少凭证：Authorization 头","retryable":false,"request_id":"20bf173f6e174b37","user_hint":"配对已失效，请重新配对"}}
```

```http
GET /api/system/entrances/qr?kind=lan
Authorization: Bearer <同一会话令牌，已脱敏>

HTTP/1.1 200
Content-Type: image/png

（922 字节 PNG）
```

- **影响**：无法通过页面上的入口二维码让另一台手机扫码进入；复制入口链接仍可用。没有观察到凭证泄漏或服务端认证绕过。
- **建议**：在设置页使用已认证 API 请求取 PNG，再用应用自身受控的 Blob URL 渲染并在卸载 / 替换时释放；同时处理 F-01 所述的可信 Blob 边界。若改变服务端认证方式，应先明确该端点的独立契约，不能用放宽整个 `/api/system/**` 解决。
- **置信度**：确认。Chrome 独立 DOM、网络对照和截图一致；Firefox 原日志也记录相同接口 401。
- **证据**：[请求与 DOM](验收证据-e9805a7-2026-11-03/focused-qr-results.json)、[破图截图](验收证据-e9805a7-2026-11-03/settings-qr-confirmed.png)。

### F-03 [一般；相对路径配置下影响核心出图] 验证读取误将相对路径判为文件换位

- **位置**：`backend/app/fsopen.py:82`、`:129`、`:137`；`backend/app/config.py:408`；`backend/app/handlers/common.py:507`、`:536`；`scripts/soak_tunnel.py:155`、`:233`。
- **复现步骤**：以 `--workdir build/final-tunnel` 启动 `soak_tunnel.py`，由其将相对 `data_dir` 传给 Config；保持其他参数一致，与绝对 workdir 环境对照。
- **触发链**：相对 workdir → 相对 data_dir / 缩略图缓存路径 → `GET /d/{device}/api/items/{id}/thumb?size=320` → `iter_file()` → `open_verified()` → `assert_same_path(realpath(p), p)`。
- **期望**：同一真实文件以相对路径或等价绝对路径打开，身份检查一致；缩略图响应正文完整。
- **实际**：左侧 realpath 是绝对路径，右侧仍是相对路径，直接比较必不相等，抛出 `HandleMismatch('mismatch')`；外层转换为 E1009。部分响应已经发送 HTTP 头，客户端收到 `IncompleteRead`，而不是完整错误 JSON。
- **最小对照**：同一文件 `open_verified(Path('build/final-relative-input.bin'))` 失败；`open_verified(绝对路径)` 读取相同字节成功。
- **实测**：相对 workdir，36 轮 / 576 请求中 **144 bad**，均为缩略图正文中断；绝对 workdir 对照同样 36 轮 / 576 请求，**0 bad**。不能将其归因为公网质量或隧道流控失败。
- **影响**：显式采用相对 data_dir 的嵌入调用 / 验证环境不能正常出缩略图；默认绝对路径环境通过。没有证据显示越权或文件内容被破坏。
- **建议**：在 Config / workdir 调用边界收敛为绝对路径，并在 `open_verified()` 明确输入契约与一致的比较形式，保留路径和句柄身份复核。缓存读取失败尽量在提交响应头之前转成结构化错误。增加相对与绝对路径等价用例。
- **置信度**：确认；若项目决定不支持相对 data_dir，也应明确拒绝并让 CLI 规范化，不能留下正文中断。
- **证据**：[失败结果](验收证据-e9805a7-2026-11-03/tunnel-relative-path.json)、[成功对照](验收证据-e9805a7-2026-11-03/tunnel-absolute-path.json)、[最小复现](验收证据-e9805a7-2026-11-03/focused-qr-results.json)。

## 四、已通过的关键流程与测量

### 4.1 核心功能与数据一致性

| 流程 | 实际完成的验证 | 证据 |
|---|---|---|
| 续传 | 3.5 MiB 独立负载；半片 socket 断开、重复片、乱序 416、磁盘 offset 优先、重启恢复；最终 SHA-256 一致 | `e2e-resume.log` |
| 去重与清理 | init 秒传、完成去重；4 个不同负载最终仅 4 份归档；无 `.part` 残留 | `e2e-resume.log` |
| 改名 / 移动 | 同名文件保留；自动加序号；目标文件 hash / size 对照；审计与重扫一致 | `e2e-organize.log` |
| 文件监听 | copy 约 3.0s、子目录新增约 3.0s、删除 missing 约 1.0s、改名约 3.1s、目录改名约 2.6s | `e2e-watch.log` |
| 正常图并发缩略图 | 60 张合法 JPEG、3 轮真实线程并发，每轮 60 请求；180 次均 200 且 RIFF / WEBP 字节正确 | `focused-qr-results.json` |
| 损坏图 | 服务不崩溃；缩略图返回 500 E3004，符合当前 04 错误表 | `focused-qr-results.json` |
| LAN 接入 | 实际 `192.0.2.10` 地址，页面、匿名 / 认证边界、配对、分页、真 WebP | `e2e-lan-isolated.log` |
| 云端原图许可 | 页面状态为“云端中继”；浏览与放大未确认前 raw 请求数为 0；点击确认后 raw 解码、transform 一致；下一张重新出现许可按钮 | `cloud-viewer-results.json` |
| Chrome 已到达的功能段 | 长按多选、临时照片删除到回收站、下拉刷新、浏览器上传、后台事件模拟、实际下载落盘与内容对照、Range 206、断网恢复、横屏、非安全上下文复制回退 | `e2e-browser-functional-subset.log`，仅对其中逐条 `ok` 的行为成立 |

续传脚本含以 JPEG 头构造的字节负载，适用于完整性与分片恢复判定，不能把它的成功当作真实照片解码兼容性证明。后者由合法 JPEG / 真 WebP 及浏览器用例提供部分覆盖。

“后台”用例在浏览器中模拟 visibility 事件，网络限速由 CDP 设置；不等同于安卓系统真实杀后台或 4G 无线链路。

### 4.2 已有关键安全回归

以下项目来自本轮最终 JUnit 的实际通过项，不是只读代码后推断：

| 主题 | 本轮已通过的证据示例 | 限制 |
|---|---|---|
| 游客互不可见、游客不能读主库 | `test_guest_escalation`、`test_guest_isolation`、`test_guest_media_access`、`test_audit_07f03d::[REDACTED-TEMP-TOKEN]` | 多为测试客户端 / 组件级，不等同于真实公网游客页面全流程 |
| 游客仅云端 | `test_guest_token::test_guest_token_from_lan_is_rejected` / `test_guest_token_on_cloud_path_is_admitted` | 未在手机移动数据上重新执行 |
| 游客上传归属 | `test_guest_upload::test_guest_bytes_land_in_the_guest_root_only` / `test_guest_cannot_choose_a_different_root` | 临时库验证 |
| 在线上限并发 | `test_guest_limits::test_two_simultaneous_guests_never_exceed_the_cap` | 具体并发数量 / 轮数以测试代码为准，非高负载容量认证 |
| 播放票据撤销 | `test_streamticket` 的 session kicked、invite revoked、session expires 三项 | 不将存量票据兼容行为扩展成未知场景保证 |
| 面板边界 | `test_tc_switch_func_006::test_panel_paths_are_404_over_the_lan`、`test_p1_pages` 隧道不可达面板 | 未检查现网所有反代配置 |
| 隧道重连与数据驻留 | `test_p1_tunnel::test_tunnel_survives_cloud_restart`、`test_tunnel_survives_agent_restart`、`test_cloud_never_stores_media_bytes` | 本机隔离 Relay，不代表已检查公网主机磁盘与日志 |
| 沙箱与 junction | sandbox 回归及 `test_watcher::test_real_junction_created_in_library_is_not_followed` | 有一项符号链接测试因权限跳过；跨卷场景未覆盖 |
| CORS、帧协议、流控 | `test_relay_cors`、`test_protocol`、`test_p1_mux`、`test_relay_head_progress` | 通过用例范围内成立，不宣称所有入口 / 所有报文穷尽 |

本轮为最终回归，没有额外开展针对公网地址的攻击或扫描，也没有重新完成 CVE / 依赖供应链审计。

### 4.3 短时稳定性与性能观测

**LAN：** 临时 200 张图，180 秒业务负载，34 轮全部成功；每类请求含基线采样 n=35。脚本 JSON `hours=0.058` 含退出阶段，退出耗时 30.2s、服务线程最终停止。峰值 RSS **61.0 MiB**；暖机后均值增长约 **0.58%**；线程尾段 / 头段均值差约 +1.48、任务差 0、句柄差约 -0.73，脚本平台判据通过。线程首尾净差实际 +4，不能把均值差写成总线程净增。

| 请求 | 脚本 `p95_last_ms` |
|---|---:|
| ping | 28.7 |
| timeline | 23.7 |
| thumb | 48.5 |
| search | 24.8 |
| info | 28.2 |
| folders | 26.0 |

**真实 WS 隧道、绝对路径环境：** 约 180 秒、40 张图、4 个并发请求组、36 轮、576 请求、0 bad；在线设备持续为 1，峰值在途 4，结束在途归零，session_count=1，无重连 attempt 增长。另观察到一次缓存写入 warning 后 HTTP 回退成功，不把它计为请求失败。

隧道脚本记录的云端 RSS 约 4.3 MB，采样的是启动进程 PID；Windows venv 启动器与实际 worker 的进程树未核实，且负载不是大文件下载，**不采用该数字证明“大文件云端内存峰值 <200 MB”**。

以上负载期间有其他验证并行运行，只作为本机短时观测，不用于证明公网吞吐、真实 4G PATCH 2–3 秒或长时间无资源泄漏。

### 4.4 构建与交付形态

从本提交重新构建 onedir EXE，`build_info.json` 记录 Python 3.12.10、Pillow 13.0.0 与 requirements 锁文件 SHA-256。EXE SHA-256 另附。

验证包括 PE Subsystem=2、端口占用返回码 2 及中文错误提示、数据目录位置正确；剔除 PATH 中的 Python 与常见 Python 环境变量后，EXE 仍可返回页面、完成配对、显示 3 张图并生成真 WebP。该用例模拟无系统 Python 环境，不是另一台干净 Windows 的完整安装认证。

## 五、旧脚本失败的复核及测试可信度

### 5.1 首轮环境不完整

首轮 pytest 为 2332 通过、4 失败、13 跳过。启动时前端 dist 尚未生成、ffmpeg 复制尚未完成，4 个失败均为页面 / 非 API 路径测试收到 404。准备完成后全量重跑，最终 2341 通过、8 跳过、0 失败。首轮记录仍保留，不能将两轮相加。

原 LAN 脚本第一次同样在页面断言失败；构建完成后使用固定临时库补跑通过。初次 node_modules 复制遇 Windows 长路径，使用长路径前缀复制完成。这些是准备过程问题，未计为产品缺陷。

### 5.2 旧综合验收的 5 条失败

| 旧失败判据 / 位置 | 复核结果 | 分类 |
|---|---|---|
| `verify_acceptance_e2e.py:377`，info roots.path 必须等于完整配置路径 | `handlers/system.py:209` 明确返回脱敏路径；切换库后时间线实际读取新库文件 | 旧判据不适用，不能以泄漏完整路径作为验收要求 |
| `verify_acceptance_e2e.py:1389` 附近，混合 23 个缩略图一律 200、不能有 5xx（两条失败） | 前面故意上传了损坏 JPEG；返回 500 E3004，与 04 错误表一致；独立 180 次正常 JPEG 并发全过 | 测试材料混用 / 文档口径漂移，不是已确认的正常图并发缺陷 |
| `verify_acceptance_e2e.py:1675`，注册表字符串必须含 `--tray` | 实际入口是 `run_tray.py`，该脚本 `:73` 注入 `args=["--tray"]` | 字符串判据过期 |
| `verify_acceptance_e2e.py:1737`，托盘只能有 8 项 | 实际 9 项，新增“显示配对二维码”，每项均有回调 | 数量判据过期 |

原脚本退出码仍为 1，没有修改产品或旧脚本去“凑全绿”。应另轮更新这些判据，并修正其自启注册表恢复逻辑。

### 5.3 Chrome 长流程仍未闭环

- 原脚本清 localStorage 后只改变同一文档的 URL fragment，未重建 Vue 内存身份状态；扫码错设备 / 正设备两步没有重新进入配对页，随后取不到元素中断。
- 独立新文档复测：错 did 显示“另一台电脑”且不落 token；正确 did 能进入 61 项临时时间线。此结果不能证明原脚本整体通过，但排除了该步骤被错误归因为服务端配对绕过。
- 用外置 wrapper 强制新文档导航后，4f 与更多功能段实际通过；4n 跨网络不可达地址场景未出现预期提示，随后 localStorage 访问遭 SecurityError 中断。该场景缺少稳定页面与完整网络定位，**列为需确认，不判通过，也不直接认定为认证漏洞**。
- 再次补充运行明确排除了 4n，未改产品与其他断言；其中局域网原图层失败已由 F-01 独立确认。后段 4q 又因 CDP 导航卡住、离开可访问文档而中断，重启后免配对及最后清状态段未完整执行。
- 这三次 Chrome 长流程都不是 ALL PASS。下载、上传、交互等已到达且有明确断言的段落可单独计入证据，未到达的尾段不计通过。
- Firefox 脚本 exit 0，但没有断言入口二维码可解码，所以未检出 F-02。这说明不能只依据脚本退出码替代用户界面验收。

### 5.4 文档不一致

`docs/03-详细设计说明书.md:2140`（及后续失败路径说明）写损坏图返回内置占位而非 500；`docs/04-接口设计文档.md:4887`、`:4888` 明确规定 E3004 / E3005 为 500，当前行为符合后者。应由接口契约负责人统一口径，再调整综合脚本；本轮不把两个矛盾文档都作为强制通过条件。

## 六、跳过、未执行与待澄清

### 6.1 最终 pytest 的 8 项跳过

| 项数 | 位置 | 原因 |
|---:|---|---|
| 1 | `backend/tests/test_indexer.py:222` | 当前权限不能创建符号链接 |
| 1 | `backend/tests/test_p1_perf.py:37` | 缺少 `_p1_mem_2048mb.json` 历史探针 |
| 3 | `backend/tests/test_p1_perf.py:37` | 缺少 `_p1_mem_100mb.json` 历史探针 |
| 1 | `backend/tests/test_sandbox.py:979` | 源文件与临时目录同卷，不能构造该跨卷用例 |
| 2 | `backend/tests/test_soak.py:312`、`:345` | 缺少历史约 10h 的原始 soak JSON |

实际 junction 用例已执行通过，不能因一个 symlink 用例跳过就写成“整个重解析点防护都没测”；同样也不能反过来将跳过项计为通过。

### 6.2 本轮不能给出通过结论的场景

| 场景 | 缺少的验证条件 / 证据 |
|---|---|
| 真实手机 4G，10 MB 照片 128 / 256 / 512 KiB 三档矩阵 | 本轮没有可核验的手机移动网络录制、逐 PATCH 计时及对应服务端请求链；不能复用旧会话域名冒充当前部署 |
| 单次 PATCH 稳定 2–3s、全程无 504 E3009 | 仅有本地回归与 head-progress 用例，未在真实公网移动链路测量 |
| 云端时间线 backlog 容量上限 | 浏览器已证明原图未自动请求；没有在低带宽公网测量 backlog 长时间曲线或全并发容量 |
| 真实公网域名 / TLS / 反代部署 | 本轮使用隔离本机 Relay；未验现网证书、容器用户、端口暴露、日志轮转与代理超时 |
| 安卓 / iPhone 真机、扫码摄像头、系统后台与整机重启 | 本轮为桌面真实浏览器 + 移动视口 / 触摸 / 事件模拟，不等同于真手机系统行为 |
| 游客完整浏览器邀请 → 兑换 → 上传 → 踢出流程 | 服务端游客相关自动化通过，未完成真实公网游客页面全链路及抓包 |
| 主人管理全部游客库的真实页面路径 | 本轮未对 Q2 建立独立浏览器验收；不能从游客隔离测试推断主人全部管理操作都已通过 |
| 24h / 10h 稳定性 | 本轮各短测约 3 分钟，历史原始长测缺失 |
| 100 MB / 2 GB 大下载云端内存 <200 MB | 历史探针缺失，本轮未执行正确进程树口径的大文件内存探针 |
| 每个接口“正常 / 缺参 / 非法 / 越权 / 未认证”五类逐项矩阵 | 本轮执行已有测试集与补充场景，未建立全路由逐状态的运行时覆盖映射；不输出全接口覆盖率 |
| 全字段级前后端契约检查 | 路径检查与部分响应断言已执行，缺少完整生成 schema / 字段级对照证据 |
| Chrome 4n 网络不可达提示与 4q 尾段 | 长流程导航不稳定；需要独立、可控的浏览器场景与完整网络证据 |

## 七、修复与收尾顺序

1. **先修 F-01：** 恢复正常 JPEG 在局域网放大后的原图叠加，补带解码断言的浏览器用例；同时验证 URL 安全守卫对不可信地址仍有效。
2. **修 F-02：** 让已配对页面的二维码经过明确认证并可解码；复测入口链接复制、二维码扫描结果、换设备与过期会话。
3. **修 F-03：** 统一 data_dir / 文件读取路径契约，复跑相对与绝对 workdir 两组，要求无正文中断。
4. **修测试基础设施：** 综合验收跟随当前契约，注册表值全文恢复；Chrome 使用可控的新文档导航，拆开网络不可达与重启尾段；不要通过删除失败断言伪造通过。
5. **补环境验收：** 当前提交部署到实际云端后完成真实 4G 三档矩阵、实际大下载内存及更长 soak；真机未执行项保持未完成。

前 3 项修复前，本提交应标记为：**自动化基础回归通过，最终用户验收未通过，存在已确认遗留功能问题。** 本轮不把它判为代码已可无条件收尾，也未执行任何修复。

## 八、证据与可追溯性

- [证据说明与脱敏范围](验收证据-e9805a7-2026-11-03/README.md)
- [完整执行命令 / 退出码 / 耗时](验收证据-e9805a7-2026-11-03/runs-summary.json)
- [最终 JUnit XML](验收证据-e9805a7-2026-11-03/pytest-final.xml)
- [pytest 按文件统计](验收证据-e9805a7-2026-11-03/pytest-files.json)
- [最终全量原始输出（已脱敏）](验收证据-e9805a7-2026-11-03/pytest-final.log)
- [LAN 短测明细](验收证据-e9805a7-2026-11-03/lan-soak.json)
- [绝对路径隧道短测明细](验收证据-e9805a7-2026-11-03/tunnel-absolute-path.json)
- [云端查看原图结果](验收证据-e9805a7-2026-11-03/cloud-viewer-results.json)
- [打包 EXE SHA-256](验收证据-e9805a7-2026-11-03/packaged-exe-sha256.json)
- [证据 SHA-256 清单](验收证据-e9805a7-2026-11-03/manifest.json)

源码保持原提交。新增交付只有本报告与证据附件；临时配置、媒体、构建产物与辅助脚本位于隔离 checkout / ignored build 目录，没有提交或合并。
'''
p.write_text(s,encoding='utf-8')
print(str(p));print('lines',len(s.splitlines()))
