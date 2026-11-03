# PhotoBridge 防御性代码审计报告

- 仓库：`D:\Photos`，分支 `perf/relay-head-progress`，HEAD `3ba34bb`
- 审计性质：**只检测，不改代码**（本轮零修改，工作区保持干净）
- 审计日期：2026-11-03
- 结论一句话：**防线的主体是可靠的（沙箱七步、魔术字节、令牌、限流、绑定门控全部实测拦得住），但存在 1 处可实测绕过的边界缺陷（沙箱 TOCTOU）与 4 处「看起来在防、实际靠别的机制兜着」的误导性防御；另有 1 处防线单薄到只剩传输层（面板接口）。**

---

## 一、总览

| 类别 | 数量 | 占比 | 说明 |
| --- | --- | --- | --- |
| 有效防御（D1，实测拦住） | 49 | 79.0% | 每条都构造了触发输入并实跑，见第六节 |
| 过度防御（D2，多余但无害） | 4 | 6.5% | 同一条件被检查两次及以上，去掉一层仍有兜底 |
| 防御缺失（D3，已实测可利用） | 2 | 3.2% | 下载文件名未净化 bidi；面板接口零令牌 |
| 误导性防御（D4，看着能拦实际拦不住/拦错对象） | 5 | 8.1% | 含 1 处**实测绕过成功**（沙箱 TOCTOU） |
| 位置不当（D5） | 2 | 3.2% | 审计 fsync 在请求路径；面板鉴权只在传输层 |
| 兜底不安全 / 静默失败（D6，标注项） | 3 | — | 不重复计入分母（与上面同一批防御点的属性） |
| 断言问题（D7，标注项） | 9 处 | — | 生产 9 处 assert 全是内部不变量，`-O` 下会被剥离 |

分母口径：**62 个逐条核对的防御点**（人工清单，见第六节），另有 AST 机器识别的 **149 个校验点**作为基数（`backend/app` 88 / `cloud/relay` 22 / `shared` 1 / `scripts` 38）。分母不含 D6/D7，它们是同一批点的属性标注。

实测规模：四批探针共 **156 条断言**，通过 **151**；5 条未通过全部是探针自身缺陷（逐条说明见附录 B），复核后**没有一条是被绕过的防御**——真正被绕过的只有单独构造的 TOCTOU 用例（D4-1，探针设计正确并通过，即"确认绕过了"）。

---

## 二、按模块拆分

| 模块 | 防御点 | 有效 | 过度 | 缺失 | 误导 |
| --- | --- | --- | --- | --- | --- |
| sandbox / 路径安全 | 12 | 10 | 0 | 0 | 2 |
| uploads / 归档落盘 | 9 | 8 | 0 | 0 | 1 |
| auth / 令牌 / 会话 | 14 | 12 | 0 | 0 | 1 |
| tunnel / 协议帧 | 6 | 6 | 0 | 0 | 0 |
| cloud（中继：Host / CORS / 限流 / 转发） | 13 | 9 | 2 | 1 | 1 |
| config / 绑定 / 审计 | 8 | 4 | 2 | 1（面板） | 0 |
| frontend | 0 | — | — | — | — |

前端一列写 0 不是"无冗余"，而是**本轮未测**：所有结论都要求"真实浏览器行为"，本轮没有开浏览器会话，因此前端防御（按钮隐藏、CSP 生效、localStorage 令牌）**一律不下结论**。

---

## 三、误导性防御明细（优先级最高）

### D4-1 沙箱：`resolve()` 检查完再按路径重新打开（TOCTOU）

- 位置：`backend/app/sandbox.py`（`resolve` 七步校验）→ 调用方 `open()`/`os.replace()` 时**按同一个字符串路径重新打开**
- 形态：检查存在性/归属后又按路径重新打开（TOCTOU）
- 绕过构造（实测，`build/_def_audit/probe2.py` §12）：
  1. 库内建 `lib/race/ok.jpg`（内容 `inside`），`sandbox.resolve(root, "race/ok.jpg", "read")` → **通过**
  2. 删掉文件 → 删空目录 `race` → `mklink /J lib\race <库外目录>`（junction 指向库外）
  3. 用**已经解析好的同一个 Path** 读：`p.read_bytes()` → **`b'LOOT'`**
- 实测结果：**绕过了**（读到库外内容）
- 影响面：越过"库根边界"读任意本地文件。前提是**攻击者能在库目录里创建/替换目录**（同机同用户进程、或被同步/解压工具写进库内）。远程不可直接利用：`rel_path` 本身仍被七步校验，`..`、盘符、UNC、保留名、bidi 全拦（见第六节矩阵）。
- 建议修法：把"检查"与"使用"合并到同一个句柄上——`os.open(..., O_NOFOLLOW)` 打开后再 `os.fstat` 比对 `st_ino/st_dev` 与 `realpath` 是否仍在根内；或用 `os.open` + `os.fdopen` 全程用 fd，不再二次按路径打开。

### D4-2 播放票：文档写"一次性"，实现可复用 300 秒且不绑客户端

- 位置：`backend/app/streamticket.py`（`mint`/`verify`），模块 docstring 首行「给 `<video>` 用的**一次性**入口凭证」
- 形态：校验了但没拦住应有的语义（没有单次消费记录；票内不含 `client_id`）
- 绕过构造（实测，`probe3.py` §14）：同一张票 `verify()` 连续两次均 `True`；换 `media_id` 校验 → `False`（条目绑定有效）；过期/篡改签名 → `False`（有效）
- 实测结果：**"一次性"不成立**，其余（条目绑定、短命、HMAC 常量时间比较）成立
- 影响面：票泄漏（浏览器历史、外部反代日志、截图分享）后 300 秒内任何客户端可用；因 `?t=` 只放行 `/api/items/*/stream` 且票绑条目，横向影响仅限同一条视频
- 建议：要么把 docstring 改成「5 分钟内可复用的单条目票」，要么落一张短命"已消费"表（一次性语义）。两者选其一，不要留"文案说一次性、代码不是"。

### D4-3 `_size_allowed()` 形同虚设（定义了但没接线）

- 位置：`backend/app/handlers/browse.py:286` 定义，**AST 统计 0 个调用点**（`probe4.py` §17）
- 形态：校验了但没用返回值 / 校验函数根本没被调用
- 现状兜底：真正生效的是 `backend/app/handlers/common.py:parse_size()`——实测 `size=99999` / `-1` / `abc` 全部 `AppError`，`size=320/1600` 通过
- 实测结果：**没有绕过**（有效校验在别处），但这是典型的"读代码以为这里在防"陷阱
- 建议：删除该函数，或在注释里点明"尺寸白名单在 `common.parse_size`"。

### D4-4 下载文件名不剥 RTL 覆盖字符（与应用自己的 `strip_bidi` 不一致）

- 位置：`backend/app/handlers/common.py:download_filename()`（只剥 `\ / : * ? " < > | \r \n \t` 与前导点）；`filename_disposition()` 直接把它写进头
- 形态：有净化工具（`sandbox.strip_bidi`）但没接到这一处
- 绕过构造（实测）：`download_filename("photo" + U+202E + "gnp.exe")` → 原样保留 `\u202e`，下发头 `filename*=UTF-8''photo%E2%80%AEgnp.exe`
- 实测结果：**文件名欺骗成立**（客户端渲染为 `photoexe.png` 一类）
- 影响面：仅"下载时的显示名"（`Content-Disposition`），不改变落盘内容；触发条件是库内**已存在**带 bidi 的文件名（沙箱只拒新写入的名字，别处同步进来的不拦）
- 建议：`download_filename` 里接 `strip_bidi()`。

### D4-5 CORS 的"局域网源"包含 link-local 段

- 位置：`cloud/relay/cors.py:is_lan_origin()`
- 形态：白名单判据比语义更宽（`169.254.0.0/16` 也当内网）
- 实测：`http://169.254.169.254` → `True`（`probe4.py` §20）；`http://evil.com`、`null`、`192.168.1.5.evil.com` → `False`（后缀欺骗挡住了）
- 影响面：低。云主机上的 169.254.169.254 是元数据服务，但那是**服务端 SSRF** 面，不是浏览器 `Origin`；放宽成"任何 169.254.x.x 网页都能读响应"在实践中没有对应场景
- 建议：改为只认 RFC1918 + 回环；link-local 单独排除。

---

## 四、防御缺失明细

### D3-1 下载文件名未净化（与 D4-4 同一处的缺失面）

- 汇点：`download_filename()` → `Content-Disposition`
- 缺失的防御：bidi/零宽字符过滤（工具已有，未接入）
- 可能被利用的路径：库内已有 `photo\u202Egnp.exe` → 用户点"下载原图"得到看起来像 `.png` 的可执行文件
- 建议：同 D4-4

### D3-2 面板接口零令牌，边界只剩"回环"

- 汇点：`/panel/pair-code`、`/panel/devices`、`/panel/audit`（`backend/app/handlers/panel.py`）
- 缺失的防御：这些接口**没有任何身份校验**（实测：回环请求即刻返回 6 位配对码与设备列表）
- 可能被利用的路径：同机任意进程（含被诱导运行的脚本、浏览器扩展的本地代理）→ 签发配对码 → 换 `pbc1` 令牌 → 读整库。这不是理论：配对码本质是一次性登录凭据
- 现状兜底：`config.check_bind_safety()` 禁止 `panel_http` 与通配绑定共存（**实测拦得住**，`config_panel_bad` 启动即 exit 2），因此外部网络打不到
- 建议：给面板加一次性 token（写在 `data/panel.token`，启动时打印到控制台），或至少校验调用进程的 SID == 服务进程 SID。

---

## 五、过度防御明细

| 编号 | 位置对（谁先校验 / 谁重复校验） | 能否安全去掉一层 | 去掉后谁兜底 |
| --- | --- | --- | --- |
| D2-1 | `config.py:check_bind_safety()` 与 `Config` 构造期对"`panel_http` × 通配绑定""`allow_lan` × 通配"各判一次 | 可（保留 `check_bind_safety`） | 构造期抛 `ConfigError`（实测 8880 实例 exit 2） |
| D2-2 | `cloud/relay/auth.py:284`、`forward.py:919`、`pairing.py:632` 三个 `_client_ip()` 完全同形的壳，最终都委托 `netutil.client_ip()` | 可（三处合并为一处 import） | `netutil.client_ip()`（实测 10 例全对） |
| D2-3 | `handlers/browse.py:_size_allowed()` 与 `handlers/common.py:parse_size()` 两份尺寸判定（前者死代码） | 可（删前者） | `parse_size()`（实测拒 99999/-1/abc） |
| D2-4 | `netguard.is_host_allowed()`（电脑端）与 `cloud/relay/headers.py:HostAllowlistMiddleware`（云端）对同一语义各写一份 | **不建议**合并 | 两个进程是独立部署单元，合并会把云端与电脑端编译期绑死；且两者默认集合不同（电脑端含本机网卡，云端含 `testserver`） |

---

## 六、有效防御清单（D1 + D8 测试覆盖）

"拦住的输入"全部来自实测（`build/_def_audit/probe*.json`），不是读代码推断。

| 防御点 | 位置 | 拦住的输入（实测） | 有测试 |
| --- | --- | --- | --- |
| 路径穿越 / 绝对路径 / 盘符 | `sandbox._steps_1_to_3` | `../outside/secret.txt`→`parent_traversal`；`/etc/passwd`→`leading_separator`；`C:\Windows\win.ini`→`drive_letter`；`sub/../a.jpg`→`parent_traversal` | ✅ 14 文件含沙箱用例 |
| UNC / 设备命名空间 | 同上 | `\\srv\share\x.jpg`→`unc_path`；`\\?\C:\…`→`device_path` | ✅ |
| NTFS 数据流 / 保留设备名 / 尾随点空格 | 同上 | `a.jpg:stream`→`ntfs_data_stream`；`CON.jpg`→`reserved_name`；`a.jpg `、`a.jpg.`→`trailing_space_or_dot` | ✅ |
| bidi 控制字符（写入侧） | 同上 | `x\u202Eegap.png`→`bidi_control` | ✅ 3 文件（reparse/junction 组） |
| 超长段/超长路径 | 同上 | 300 字符段→`segment_too_long`；10×200 段→`path_too_long` | ✅ |
| junction / 符号链接指向库外 | `_step_5_reparse`（`st_file_attributes & 0x400`，不只看 `islink`） | 库内 `junc → 库外`→`reparse_point`（实测 `islink=False` 但 reparse 位为真，证明"只看 islink"会漏） | ✅ 3 文件 |
| 上传临时名白名单 | `sandbox.tmp_path` / `_UPLOAD_ID_RE` | `../evil`、短 id、超长 id、含斜杠 → `bad_upload_id` | ✅ |
| 静态文件穿越 | `static._resolve` | `/../config.json`、`/assets/../../config.json`、`\..\config.json` 全 `None` | ✅ |
| 上传扩展名白名单 | `uploads.check_extension` + `archive.normalize_ext` | `a.exe`、`a`、`.jpg` 拒；`a.JPG`、`ba.exe.jpg` 放行（双扩展名按末段判） | ✅ |
| 魔术字节 | `uploads.check_magic`（前 32 字节） | HTML 改名 jpg、MZ 头、SVG 改名 png、空文件、2 字节、12 字节、`<png 声明实为 gif>` 全部 `E1017` | ✅ 5 文件含 magic |
| 上传总量上限 | `uploads.create_session`（2 GiB） | 超限 → `E1006` | ✅ 19 文件含 sha256/上传 |
| 上传并发闸门 | `uploads.MAX_OPEN_SESSIONS=3`（按"未完成会话"计） | 第 4 个 `init` → `429 E4002` | ✅ |
| 分片 `Content-Range` 校验 | `uploads` PATCH | 缺头→`E1010`；跳号/越界/超大→400/416 | ✅ |
| 令牌格式与签名 | `shared/client_token` | 错误 secret、设备不匹配、过期、截断签名、改 payload 保签名、`pbc2` 前缀、空串 → 全拒 | ✅ 42 文件含 token/auth |
| 令牌校验常量时间 | `streamticket.verify`（`hmac.compare_digest`）、`client_token` | 篡改签名 → `False` | ✅ 2–3 文件 |
| 播放票绑定条目 | `streamticket.mint/verify` | 票(media 7) 校验 media 8 → `False` | ✅ |
| Cookie 认证面收窄 | `auth.py`（仅 `GET/HEAD` 且 `/api/events` 或 `/api/items/{id}/(thumb|preview|raw)`） | 写入类接口带 Cookie 无 Authorization → 401（回归用例已锁） | ✅ 6 文件含 `uploader_client_id` |
| 游客数据面隔离 | `common.check_media_visible` + `is_guest` + `visible_root_ids` | 游客用主库 id → 404，与不存在不可区分（修复后由 `uploader_of` 回查兜住） | ✅ 1 文件含 `is_guest`（**偏少**，见 D8-1） |
| 游客 SSE 过滤 | `handlers/events.py`（按 `root` 过滤） | 游客只收 guest root 事件 | ✅ |
| 中继 Host 白名单 | `headers.HostAllowlistMiddleware` | `127.0.0.1.evil.com`、`evil-127.0.0.1`、`127.0.0.1@evil.com`、`127.0.0.1\r\nX-A: 1`、`http://evil.com`、空/缺 Host → 404（14 例全对） | ✅ 2 文件含 `host_allow` |
| 电脑端 Host 校验 | `netguard.is_host_allowed` | `evil.example.com`→False；空 Host→**False**；私网/组网字面量→True（有意放行 Radmin/Tailscale） | ✅ |
| CORS 默认关闭 + 白名单精确匹配 | `dispatch.enable_cors` / `cors.cors_headers` | 未命中不回任何 `Access-Control-*`；命中不带 `allow-credentials` | ✅ 3 文件含 preflight |
| 中继预检短路 | `headers.CorsMiddleware` | `OPTIONS + Origin + ACRM` → 204 + `Vary: Origin` | ✅ |
| 中继访问日志不记 query | `headers.AccessLogMiddleware` | 只记 `method + path + status`（配对码 `?code=`、搜索词 `?q=` 不入日志） | ✅ |
| 令牌不进日志/审计 | `audit.py` 字段白名单 + 面板审计实测 | `/panel/audit` 输出不含 `pbc1.`/`pbg1.` | ✅ 11 文件含 audit |
| 来源识别（XFF） | `netutil.client_ip`（默认受信集空） | 非受信对端无视 XFF；受信时右→左取第一个不受信跳；伪造最左跳不影响限流桶 | ✅ 8 文件含 limiter |
| 限流与 `Retry-After` | `limiter.py` / `auth._reject_auth` | 认证失败洪泛 → 429 + `Retry-After` | ✅ 1 文件（**偏少**，见 D8-2） |
| 绑定门控（朝拒绝倒） | `config.check_bind_safety` | 通配 + `allow_lan=False` / 无 roots / 关配对 / 面板开启 → 全 False（5 例） | ✅ 4 文件 |
| 子进程无 shell + 超时 + 并发闸门 | `video._run` | 参数数组、`CREATE_NO_WINDOW`、`timeout`、`_slots()` 限并发、`stdin=DEVNULL` | ✅ |
| 帧长度上限（解码侧） | `protocol.decode_frame` / `FrameParser` | `payload > MAX_FRAME`→`ProtocolError`；声明长度超限即"毒化"解析器（防吃内存） | ✅ 21 文件含 tunnel/frame |
| 中继请求体上限 | `relay/body.read_json_object` | `Content-Length` 超限或实际字节超限→`RequestBodyTooLarge` | ✅ |
| wss 证书校验 | `tunnel.py:259` `ssl.create_default_context()` | 不降级为不校验 | ✅ |
| 原子占名 | `archive.reserve`（`O_CREAT|O_EXCL`） | 并发同名只有一个成功，另一个 `FileExistsError`→换名 | ✅ |
| 审计先文件后表 + fsync | `audit.record` / `_append_file` | 索引表不可用时文件记录仍落盘（实测 `audit.log` 有行） | ✅ |
| 审计动作白名单 | `audit.ACTIONS` | 未登记动作只写文件并 WARNING（实测非白名单动作不写表） | ✅ |

**D8 覆盖缺口**

- D8-1 `is_guest` / 游客数据面：只有 **1 个** 测试文件名字里带该关键词（另有 6 个文件覆盖 `uploader_client_id`）。游客越权是历史上出过事的区域，建议把"游客视角的每个媒体端点"做成参数化用例。
- D8-2 `Retry-After` / 限流响应头：只有 **1 个** 文件覆盖，而限流是本项目防爆破的主手段。
- D8-3 **`toctou` 关键词 0 个测试文件**——D4-1 的窗口没有任何回归保护。
- D8-4 `is_lan_origin` 的 link-local 分支无测试。

---

## 七、兜底与断言（D6 + D7）

### D6 兜底方向与静默失败

| 位置 | 失败时朝哪倒 | 是否静默 |
| --- | --- | --- |
| `config.check_bind_safety` | **朝拒绝倒**（不安全绑定 → 拒绝启动，实测 exit 2） | 否（打印中文原因） |
| CORS 默认 | **朝拒绝倒**（默认空 = 不启用） | 否 |
| `/panel/**` 默认 | **朝拒绝倒**（`panel_http` 默认 false → 全 404） | 否 |
| `uploads.MAX_OPEN_SESSIONS` | **朝拒绝倒**（429） | 否 |
| `audit.record` 索引表失败（D6-1） | 朝"继续业务"倒（合理），但只记 **DEBUG** | **是**（生产默认级别看不到）→ 建议升 WARNING |
| `thumbs` 生成失败（D6-2） | 朝"不阻塞浏览"倒（合理），仅日志 | 部分（INFO/WARNING） |
| 配置默认值（D6-3） | `server.host=127.0.0.1`、`require_pairing=True` 朝安全倒；但 **`server.allow_lan=True`、`auth.allow_delete=True`** 朝**放行**倒——配置缺项时默认允许局域网与删除 | 否（但属"默认值放宽"） |

### D7 断言

生产代码 9 处 `assert`：`backend/app/archive.py:390`、`static.py:153`、`static.py:195`、`transcode.py:323`、`watcher.py:744`、`handlers/browse.py:787`、`handlers/browse.py:1068`、`transports/local_http.py:428`、`cloud/relay/pages.py:258`。

- 实测 `python -O` 下 assert 被剥离（`ret 1`），因此**若有人把这些 assert 当安全校验，`-O` 启动即失效**。
- 逐条核对：9 处断言的都是"调用方保证过"的内部不变量（`root`/`index`/`bus`/`stdout` 非 None），不是权限或路径校验。→ 属"过度防御 + 断言选错工具"，不构成漏洞；建议改成显式 `if x is None: raise` 以便在 `-O` 下也有确定行为。

---

## 八、结论

**哪些层可靠**：路径与文件层（沙箱七步 + 归档白名单 + 原子占名）是本仓最扎实的一层，21 类穿越载荷全部按名字拦下并带 rule；上传层（扩展名 + 魔术字节 + 总量 + 分片 + 并发闸门）同样扎实；令牌层（格式/签名/过期/设备绑定/常量时间比较）无短板；中继的 Host 白名单、访问日志不记 query、请求体上限、预检短路都实测有效。

**哪些层形同虚设**：
1. **沙箱的"检查"与"使用"是两段代码**（D4-1）——这不是理论风险，实测读到了库外内容。虽然前提是本地写权限，但它推翻的是"库根边界"这条最基本的保证。
2. **播放票的"一次性"只写在注释里**（D4-2）。
3. **面板接口在鉴权上是空的**（D3-2），只剩回环这一道传输层边界；`check_bind_safety` 挡的是"暴露到网络"，挡不住"同机进程"。

**必须优先处理的 3 项（按影响面，不按数量）**：

1. **D4-1 沙箱 TOCTOU** —— 影响"库根边界"这一条根本保证；修法明确（open 后 fstat 复核/全程用 fd）。它是本次唯一**实测绕过**的项。
2. **D3-2 面板零令牌** —— 一旦本机任意进程能发配对码，等于本地即拿到全库读权限；且面板还提供 `audit` 与 `devices`。
3. **D4-2 播放票非一次性 + 不绑 client** —— 定策略：改文案或加一次性消费；这是"文档与实现不一致"里唯一带凭据语义的一处。

**次要但建议一并处理**：D4-4（bidi 下载名，接一下现成的 `strip_bidi` 即可）、D6-1（审计索引失败升 WARNING）、D8-3（给 TOCTOU 补一条回归用例，否则下次重构又会退回去）。

**不建议动的防御**：
- `netguard` 与云端 `HostAllowlistMiddleware` 的两份 Host 判定（D2-4）：两个独立部署单元，各有不同的默认集合，合并只会把部署耦合死。
- `sandbox` 的"多层校验"（形态 → reparse → realpath → 权限）：看似重复，实际每一层拦的是不同类别的输入（形态类 vs 链接类 vs 逃逸类 vs 授权类），删任一层都会掉一类用例。
- `transport` 的 Ticket 与 Cookie 双通道：两条都是"浏览器限制逼出来的"补丁，缺一条就有机型放不出视频。

---

## 九、检测方法附录（可复现）

所有脚本在 `build/_def_audit/`（不进版本库），产物在 `build/_def_audit/out/`。

```powershell
# 0) 环境
cd D:\Photos
$env:PYTHONPATH="D:\Photos"; $env:PYTHONIOENCODING="utf-8"

# 1) AST 盘点：危险汇点 / 校验点 / 静默 except / assert / 默认值 / 显式抛出
.\.venv\Scripts\python.exe build\_def_audit\inventory.py
#   → out/inventory.json（这份数字支撑第一、二节的"基数"与第七节 assert 清单）

# 2) 批次一：沙箱矩阵、静态穿越、Host、CORS、上传、令牌、审计、配置默认值
.\.venv\Scripts\python.exe build\_def_audit\probe.py
#   → out/probe.json（95 条，90 通过；5 条未通过的解释见附录 B）

# 3) 批次二：来源识别、绑定门控、原子占名、TOCTOU、assert 与 -O
.\.venv\Scripts\python.exe build\_def_audit\probe2.py
#   → out/probe2.json（23 条全通过；§12 是 TOCTOU 绕过的那一条）

# 4) 批次三：播放票语义、审计索引静默失败、转码/流式路由 audience
.\.venv\Scripts\python.exe build\_def_audit\probe3.py
#   → out/probe3.json（14 条全通过）

# 5) 批次四：尺寸参数、Host 空值、下载文件名、CORS 局域网判定
.\.venv\Scripts\python.exe build\_def_audit\probe4.py
#   → out/probe4.json（24 条全通过）

# 6) 测试覆盖分布（D8）
.\.venv\Scripts\python.exe build\_def_audit\test_cov.py
.\.venv\Scripts\python.exe build\_def_audit\peek_cov.py
```

**口径说明（避免数字被误读）**

- "防御点"是**人工清单**（62 个），不是 AST 计数；AST 的 149 个"校验点"是它的超集（含大量参数类型/范围判断），两者不能相加。
- "汇点 245"含 `scripts/` 的 215 处（测试与运维脚本），生产三段只有 30 处（`backend/app` 24、`cloud/relay` 4、`shared` 2）。
- "有效 49"只统计**本轮构造了输入并实跑**的点；只读代码判断可靠但没构造输入的，本报告不下"有效"结论。

### 附录 B：批次一 5 条未通过的逐条说明（均为探针缺陷，不是产品缺陷）

| 项 | 实际 | 结论 |
| --- | --- | --- |
| `%2e%2e/outside/secret.txt` | 返回 `lib\%2e%2e\...` 字面目录 | `%` 未被解码，路径落在库内且不存在 → 不构成逃逸（判据写错） |
| 静态 `/..%2fconfig.json` | 同上，字面路径 | 同上 |
| 审计"无日志" | 我监听的 logger 名是 `app.audit`，真实名 `backend.app.audit` | 测法错（probe3 已纠正并通过） |
| `archive.place(dir_rel=...)` | `TypeError` | 签名里没有该参数（probe3 改用 `subdir=` 并通过） |
| junction 属性断言 | `st_file_attributes & 0x400` 断言失效 | 断言写法问题；读取结果 `b'LOOT'` 已证明换位成功（D4-1） |

---

*本报告只做检测。所有绕过结论都附了构造输入与实测输出；所有"过度"结论都写明了"去掉后谁兜底"；所有未测项（前端、云容器运行形态）都标了「未测」而不是「无问题」。*
