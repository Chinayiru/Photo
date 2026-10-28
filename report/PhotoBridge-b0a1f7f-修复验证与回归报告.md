# PhotoBridge 分支 `b0a1f7f` 修复验证 + 回归报告

- **验证对象**：`codex/fix-report-findings-4b3d1e4` @ `b0a1f7f`（「fix(security): close relay and client auth gaps」，父提交 `4b3d1e4`，23 文件 +458/−62）
- **基线**：`4b3d1e4`（上一轮 410 例：通过 386 / 失败 21 / 不可执行 3，缺陷 F01–F17）
- **方法**：同一套用例、同一个隔离测试台，对分支代码**重跑全部套件**，逐条与基线结果比对（按用例 id 对齐，输出「修复 / 回归 / 未变」迁移表）
- **结论**：**23 条用例由失败转通过，0 条回归**；修复项 12/17 已确认闭合，2 项部分闭合，3 项未改动（含 2 项文档类）。**建议合并，合并前处理 §5 的 4 条残项**

---

## 0. 测试台与可复现性

| 组件 | 地址 | 说明 |
|---|---|---|
| 电脑端（分支代码） | `127.0.0.1:8877` | `ref_b0a1f7f`（`git archive b0a1f7f` 导出）+ 独立临时库 |
| 云端中继（分支代码） | `127.0.0.1:8888` | 独立 `cloud.db`，隧道在线 `online_devices=1` |
| 面板实例 | `127.0.0.1:8878` | `server.panel_http=true` |
| CORS 实例 | `127.0.0.1:8879` | `server.cors_origins=["http://allowed.example"]` |
| 非法绑定实例 | `0.0.0.0:8880` | 期望启动即拒绝 |
| 浏览器代理 | `127.0.0.1:8787` | → 8877，供 Chrome CDP 用例 |

真实主库（`D:\Photos`、`C:\tools\游客访问照片`）全程只读，未写入任何文件。基线结果保存在 `build/_v4b3/results_baseline_4b3d1e4/`，分支结果在 `build/_v4b3/results/`。

---

## 1. 执行摘要

| 项 | 数字 |
|---|---|
| 分支重跑用例记录 | 464 条 |
| 直接判定通过 | 419 |
| 直接判定失败 | 45 |
| 复核后**真正未闭合** | **4**（M7-023、M10-004、M10-010、M10-011） |
| 复核后「判据过时 / 环境限制 / 已定性」 | 41 |
| 与基线逐条比对 | 共同 432 例：**修复 23 / 回归 0 / 未变 409** |

分组（分支重跑）：A 全流程 16/16、B 契约 18/18、C 隔离 8/8、D 通路 23/29、E 安全 156/164、F 一致性 4/4、M 接口 153/171、其余基础设施与探针组全通过。

---

## 2. 修复项逐条验证（对照上一轮 F01–F17）

| 编号 | 上一轮缺陷 | 分支实测 | 判定 |
|---|---|---|---|
| F01 | 仅凭 `pb_token` Cookie 即可认证任意接口（`auth.py:777`） | Cookie 认证改为**方法+路径双白名单**：仅 `GET/HEAD` 且 `/api/events`、`/api/items/{id}/(thumb\|preview\|raw)`。9 条探针：`thumb/raw/preview/events` 仅 Cookie → 200；`/api/system/stats`、`/api/timeline`、`DELETE /api/uploads/*`、`POST /api/items/delete` 仅 Cookie → **401 E2002** | **已闭合** |
| F02 | CSRF：`Cookie + Origin: https://evil.example` 可删除文件 | 同一请求 → **401**，目标文件仍在库（`file_gone=False`）；`Origin/Referer` 校验虽仍未做，但 Cookie 已不能驱动写操作 | **已闭合** |
| F03 | `Set-Cookie` 无 `Secure` | 隧道通路：`pb_token=…; Path=/; Max-Age=…; SameSite=Lax; HttpOnly; **Secure**`；局域网 http 通路仍无 `Secure`（明文 http 下加 Secure 会让 Cookie 失效，属合理取舍） | **部分闭合（可接受）** |
| F04 | CORS 白名单预检返回 405 且自相矛盾 | `OPTIONS` + 白名单 Origin → **204** + `access-control-allow-origin: http://allowed.example` + `allow-headers`/`expose-headers`/`max-age`；非白名单 Origin 不回显任何 CORS 头；未配置 CORS 的实例与中继均不发 CORS 头；预检不进隧道 | **已闭合** |
| F05 | 云端 `/api/**`、`/d/**` 完全不校验 Host | 中继新增 `HostAllowlistMiddleware`：`Host: evil.example.com` → **404（E1004 JSON）**，`/api/health` 与 `/d/{id}/api/**` 一致；`/agent/ws` 握手仍校验 | **已闭合** |
| F06 | 实测 `0.0.0.0:8877` 通配绑定，与「只监听私网」表述冲突 | 未改动：`check_bind_safety` 在 `allow_lan + 已配 roots + require_pairing + !panel_http` 下仍允许通配；`/panel/**` 由回环门控兜住（局域网 404） | **未闭合（一般，契约表述问题）** |
| F07 | 云端缺 `/api/clients/revoked`、无 `/api/clients/token/refresh` | `revoked`（agent_token）→ 200 含 `device_id + clients[]`；`me`（client_token）→ 200 含 `client_id/device_name/lan_ips`，account_token → 401；`token/refresh` → **400 E1001「令牌尚未进入续期窗口」**（统一 JSON 体），不再 405 text/plain | **已闭合** |
| F08 | 中继错误体与直连不一致、`request_id` 为空串 | 中继所有鉴权拒绝统一为 `E2002 / 配对已失效，请重新配对` + 非空 `request_id`（如 `8e458d2dcf3b4784`）；404/405 也改成 JSON 错误体。**残留**：匿名请求由中继在转发前短路，文案与电脑端 `缺少凭证：Authorization 头` 仍不同（M7-023） | **大部分闭合，1 条残项** |
| F09 | 中继生成错误体 `request_id` 恒空 | 鉴权拒绝路径已带 `request_id`；**转发失败路径**（`E3003 电脑离线`）仍为 `""` | **部分闭合** |
| F10 | 游客 redeem 与配对码共用 10 次/分钟/IP 桶 | 新增 `PAIR_RATE_PATHS={"/api/pair"}`，redeem 走 `PUBLIC_RATE_PER_SEC=300`；p4 游客套件 **39/39** 一次通过，不再出现 E4001 干扰 | **已闭合** |
| F11 | 踢出后 `403 E1023` 的 `detail` 泄露内部 `client_id` | `detail` 不再含 `client_id`（仅 `E1023 guest_session_revoked`）；p4 全套 39/39 | **已闭合** |
| F12 | `--config` 指向不存在文件 → 未捕获 `AttributeError` + 完整堆栈 | 实跑：`配置错误：指定的配置文件不存在`，**exit=2，无堆栈** | **已闭合** |
| F13 | 前端 CSP 把端口硬编码 8787，自定义端口界面不可用 | CSP 随配置变化：`connect-src 'self' https: http://*:8877`。**残留**：非 https 的自建中继 origin（`http://127.0.0.1:8888`）仍被 CSP 拦（M10-004） | **部分闭合** |
| F14 | 移动端「我的」无两条入口链接、无二维码 | 未改动：页面仍是设备管理 + roots + GuestPanel；二维码只在桌面托盘 `tray.py:240`（A3 已用函数层验证：`pair_qr_url` 生成 `#code=…&did=…`，`_qr_pil_image` 148×148 可渲染） | **未闭合（体验项）** |
| F15 | `move target_dir="\\\\srv\\share"` 被归一化为库内目录 | 现在 **400 E1001**（拒绝 UNC / 盘符 / `//` 前缀）；`rename`、`move` 全部穿越载荷 400/403 | **已闭合** |
| F16 | docs/04 三处对 `E3012` 状态码自相矛盾 | 文档未改（实现符合 §4.5 表格：`on_conflict=fail` 记 `failed` 并返回 200） | **未闭合（文档项）** |
| F17 | `/agent/stream`、`/agent/frames` 降级通路 | 仍 `GET /agent/stream` → 404、`POST /agent/frames` → 405；契约写「若实现」→ 记为不可执行 | **不可执行** |

---

## 3. 回归检查

逐条比对 432 个共同用例：**修复 23 条，回归 0 条，未变 409 条**。

重点回归面复核结果：

| 面 | 结果 |
|---|---|
| 上传（init/PATCH/complete/DELETE、分片乱序、断点续传、sha256） | p1/p2/p8 全绿：43/43、43/43、8/8 |
| 浏览（timeline/items/thumb/preview/raw Range·ETag·304·416/timeline 游标） | p1 全绿 |
| 管理（delete/move/rename/rescan、同名加序号、dedup） | p2 全绿，无覆盖、无数据丢失 |
| 游客 M9（邀请/兑换/可见范围/上传/踢出/并发上限） | p4 **39/39** |
| 隧道 M8（握手负例/HELLO/心跳/CREDIT/重连） | p7 **20/20**、p7b **13/13**（修复后） |
| 跨身份/跨设备隔离 C 组 | 8/8（游客对主库 id 与不存在 id 响应体除 `request_id` 外逐字一致） |
| 前端 M10 + A3/A5 | 14/17（3 条见 §5） |

---

## 4. 需要知会的行为变化（与 `4b3d1e4` 不同，请确认是否为预期）

1. **账号令牌不再能走 `/d/` 转发通路**。`4b3d1e4` 上 `account_token` 可代理 `/d/**`；分支只认 `pbc1` 客户端令牌，`account_token` 打受保护路径 → **401 E2002**。公开路径（`/api/system/ping`、`/api/pair`、`/api/guest/redeem`）不受影响。与 docs/04「`/d/` 用 client_token」一致，前端页面也只用 `pbc1`，**兼容性无影响**，但若有第三方脚本用账号令牌代理，会失效。
2. **Cookie 认证面收窄**：仅 `GET/HEAD` + `events`/`thumb`/`preview`/`raw`。`<img>`/`<video>`/SSE 仍可用 Cookie，其余必须带 `Authorization`。
3. **中继鉴权拒绝文案统一**为「配对已失效，请重新配对」，且 404/405 也返回 JSON 错误体（不再是 `text/plain`）。
4. **隧道通路 `Set-Cookie` 增加 `Secure`**（浏览器只在 https 页面回传，符合预期）。
5. **`move target_dir` 拒绝 UNC / 盘符 / 双斜杠前缀**（400 E1001），不再静默归一化为库内目录。
6. **`token/refresh` 有窗口语义**：距过期 > 7 天时返回 `400 E1001`，不是「刷新成功」。

---

## 5. 仍未闭合（4 条，建议合并前处理或明确接受）

| 编号 | 级别 | 问题 | 复现 | 期望 vs 实际 | 初步定位 |
|---|---|---|---|---|---|
| M7-023 | 轻微 | 匿名请求经隧道与直连的错误体文案不同 | `GET /api/timeline?limit=5` 经 `/d/{id}/` 与直连各一次 | 期望逐字段一致；实际直连 `message="缺少凭证：Authorization 头"`，隧道 `message="配对已失效，请重新配对"`（状态码/错误码/`request_id` 均一致） | 传输（中继转发前短路鉴权） |
| M10-004 | 轻微 | 非 https 的自建中继 origin 被前端 CSP 拦 | 隧道 `cloud_url=http://127.0.0.1:8888`，浏览器打开设备页面点「云端」入口 | 期望可连；实际 `violates CSP: connect-src 'self' https: http://*:8877`。生产 https 域名不受影响，仅自建 http 中继或反代端口不同时触发 | 前端（CSP `connect-src` 只放行 `self`/`https`/本机端口） |
| M10-010 | 一般 | 移动端「我的」页没有局域网/云端两条入口链接（A3 契约） | 手机浏览器 →「☰ 我的」 | 期望两条可点入口；实际只有设备管理、roots、GuestPanel | 前端 |
| M10-011 | 一般 | 移动端「我的」页没有二维码 | 同上 | 期望二维码；实际 `qr=0`（二维码仅在桌面托盘窗口） | 前端 / 产品定义 |

另有 2 条文档类未闭合（不阻塞代码合并）：

- **F06/D1**：`server.host=0.0.0.0` 与 docs/04 §8.5「电脑端本地服务只监听私网」表述冲突。实现是门控允许（需配对 + `panel_http=false`），`/panel/**` 由回环门控兜住，局域网访问实测 404。建议改文档措辞（「默认回环，允许局域网时需配对」）。
- **F16/B3**：docs/04 `:241` 状态码表、`:4317` move 参数表、`:4806` 错误码表对 `E3012` 的表述互相矛盾。实现符合 `:4317`。

---

## 6. 41 条「判据过时 / 环境限制」明细（不构成缺陷）

| 类别 | 条数 | 说明 |
|---|---|---|
| 沙箱判据应看「不泄露库外内容」而非 4xx | 17 | `folders` 对 16 条穿越载荷 + `..%2f` 变体，实测均 200 + 空列表（body 回显入参路径，不含库外数据） |
| 探针工具限制（httpx 拒发非法头 / 读 SSE 写法错） | 6 | 伪造 Host 的 502、`Bearer ` 空值 `LocalProtocolError`、`ResponseNotRead`；已用裸 socket 与流式写法复测通过（`t_fixup_p11`、`t_sec_p5b`） |
| Windows 文件名限制 | 3 | `<script>`/`<img …>` 类载荷因 `<>:"/\|?*` 无法落盘（`OSError [Errno 22]`），改用 `'`、`&`、反引号载荷后实测可落盘并被 API 原样返回（转义责任在前端） |
| 用例取错字段/端点 | 5 | `panel/devices` 键是 `items`；`search` 结果不含 `exif`（要看 `/api/items/{id}`）；`folders` 只列目录；`move` 源=目标走 `skipped` |
| 内存测量口径 | 3 | 旧值 540.9MB 把测试脚本自己算进 RSS；按监听 PID 复测：单路 23.8MB 下载峰值 **40.8MB**、4 路并发 **38.9MB**，判据（<200MB）成立 |
| 设计与文档冲突 | 2 | `E3012` 三处矛盾（F16）；`?order=asc → 400 E1001` 为 docs/04 `:2400` 明示行为（M2-012 判据过时） |
| 设计如此 | 2 | 设备端私网字面量 Host 放行（Radmin/Tailscale 直连）；`panel/pair-code` 返回 201（文档未规定状态码） |
| 用例自身缺陷（脚本崩溃/断言取错） | 3 | `ZZ-073`、`ZZ-096`、`E9-114`（`Config.load` 不存在，默认值应读 `DEFAULT_CONFIG`） |

---

## 7. 复跑方式

```powershell
cd D:\Photos\build\_v4b3
# 起三件套（中继 8888 / 设备 8877 / 面板 8878 / CORS 8879）
.\.venv\Scripts\python.exe branch_ctl.py start
# 跑全部套件
.\.venv\Scripts\python.exe run_branch_suite.py
# 与基线逐条比对（修复/回归/未变）
.\.venv\Scripts\python.exe compare_branch.py
# 汇总
.\.venv\Scripts\python.exe final_tally.py
```

日志：`build/_v4b3/logs_branch/*.log`；结构化结果：`build/_v4b3/results/t_*.json`（含每条用例的请求、状态码、响应体、期望与实际）。
