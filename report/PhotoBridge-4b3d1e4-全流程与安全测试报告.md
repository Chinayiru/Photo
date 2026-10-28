# PhotoBridge 全流程 + 全接口安全测试报告

- **被测分支/提交**：`4b3d1e4`（= 测试时 `main` 的 HEAD，与任务书指定哈希一致）
- **测试性质**：验证（只记录，不修复）
- **被测形态**：电脑端（Windows 11，Python 3.12.10）+ 云端中继（`cloud/relay`）+ 隧道（WSS）+ 前端 SPA（Chrome 真实浏览器）
- **测试数据**：独立临时库 `build/_v4b3/lib`（25 张/含 23.8MB 大图、截断文件、HTML/MZ 伪装文件、超深目录、多格式）+ `build/_v4b3/guest`（游客根）+ `build/_v4b3/outside`（库外穿越目标）。**真实主库 `D:\Photos` 全程未写入**（真实实例仅用于线上云端只读探测）。
- **用例规模**：410 个唯一用例（含重跑共 500+ 次执行），13 个自动化套件 + 6 个定向探针
- **结果**：通过 386 / 失败 21（归并为 12 条实现缺陷）/ 不可执行 3

---

## 一、执行摘要

| 组 | 用例数 | 通过 | 失败 | 阻塞（前置未满足） |
|---|---:|---:|---:|---:|
| A 全流程（A1–A8） | 15 | 15 | 0 | 0 |
| B 接口契约一致性（B1–B6） | 18 | 18 | 0 | 0 |
| C 跨身份/跨设备隔离（C1–C8） | 8 | 8 | 0 | 0 |
| D 通路与绑定（D1–D5） | 29 | 23 | 6 | 0 |
| E 安全漏洞（E1–E9） | 170 | 162 | 6 | 2 |
| F 一致性与回归（F1–F4） | 4 | 4 | 0 | 0 |
| **接口覆盖（M1–M10）** | 163 | 153 | 9 | 1 |
| 基础设施/脚本自检（Z） | 3 | 3 | 0 | 0 |
| **合计** | **410** | **386** | **21** | **3** |

**接口覆盖：已测 50 / 54 个接口**（4 个未实现或不适用，见第五节）。

**一句话结论**：核心安全面（沙箱穿越、跨身份越权、跨设备越权、游客令牌跨通路、邀请重放、上传伪装/炸弹/完整性、限流）**全部被服务端正确拒绝**；发现的 12 条实现缺陷中，**没有一条达到任务书定义的「阻塞级」**（不存在越权读他人数据、不存在未授权写/删的实际可利用路径、令牌无法被绕过、云端不落媒体字节、沙箱不可穿越）。但两条**严重级认证/CSRF**缺陷（F01/F02）与云端 Host 校验缺失（F05）在特定配置下会升级，**建议在对外开放前修复**。

---

## 二、接口覆盖表

图例：正常 / 参数异常 / 越权 / 未认证 = 该五情形是否覆盖并符合预期；结论列「✅ 一致」= 与 `docs/04` 契约一致。

| 模块 | 接口 | 已测 | 正常 | 参数异常 | 越权 | 未认证 | 结论 |
|---|---|:--:|:--:|:--:|:--:|:--:|---|
| M1 | `GET /api/system/ping`（匿名） | ✅ | ✅ | ✅ | — | ✅ | ✅ 一致（匿名 200；带伪 Host 404） |
| M1 | `GET /api/system/info` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（含 tunnel.connected/heartbeat_sec） |
| M1 | `GET /api/system/entrances`（匿名） | ✅ | ✅ | ✅ | — | ✅ | ✅ 一致（lan/vpn/cloud 三组） |
| M1 | `GET /api/system/stats` | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ Cookie 可认证（F01） |
| M2 | `GET /api/timeline` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（`order=asc` →400 E1001 为文档明示） |
| M2 | `GET /api/items/{id}` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（EXIF 原样返回，转义由前端做） |
| M2 | `GET /api/items/{id}/thumb?size=320\|1600` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致 |
| M2 | `GET /api/items/{id}/preview` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致 |
| M2 | `GET /api/items/{id}/raw`（download/Range/INM） | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 206/304/416/ETag 全对 |
| M2 | `GET /api/folders`（root/path/depth） | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（只列目录，不回显文件名） |
| M2 | `GET /api/search`（12 参数） | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（`camera` 为全等匹配） |
| M2 | `GET /api/events`（SSE） | ✅ | ✅ | — | ✅ | ✅ | ✅ 一致（hello + 25s ping + 匿名 401） |
| M3 | `POST /api/uploads/init` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（201；第 4 个并发 429 E4002） |
| M3 | `PATCH /api/uploads/{id}`（Content-Range） | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（206；乱序/跳号/越界/重发按契约） |
| M3 | `GET /api/uploads/{id}` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（他人会话 404 且不可区分） |
| M3 | `POST /api/uploads/{id}/complete` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（201；sha256 不符不留半截） |
| M3 | `DELETE /api/uploads/{id}` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（200；重复 409 E1012） |
| M4 | `POST /api/items/delete` | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ 进回收站正确，但可被 CSRF 驱动（F02） |
| M4 | `POST /api/items/move` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（rename/skip/fail 三策略） |
| M4 | `POST /api/items/rename` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（白名单校验；含 `;` 仅当文件名） |
| M4 | `POST /api/index/rescan` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（202；重复 409 E3012） |
| M4 | `/api/albums`（若已实现） | ❌ | — | — | — | — | ⛔ 未实现（路由表无此端点）→ 不可执行 |
| M5 | `POST /panel/pair-code` | ✅ | ✅ | ✅ | — | — | ✅ 一致（201；非回环 404） |
| M5 | `GET /panel/devices` | ✅ | ✅ | ✅ | — | — | ✅ 一致（列表键 `items`；非回环 404） |
| M5 | `GET /panel/audit` | ✅ | ✅ | ✅ | — | — | ✅ 一致（不含令牌明文） |
| M6 | `POST /api/auth/login` | ✅ | ✅ | ✅ | — | ✅ | ✅ 一致（回 `account_token`） |
| M6 | `POST /api/auth/logout` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（登出后 me 401） |
| M6 | `GET /api/auth/me` | ✅ | ✅ | — | ✅ | ✅ | ✅ 一致 |
| M6 | `GET /api/devices` | ✅ | ✅ | — | ✅ | ✅ | ✅ 一致 |
| M6 | `GET /api/devices/{id}/status` | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ 缺 credit 字段（F17） |
| M6 | `POST /api/devices/{id}/pair`（auto:true） | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（200 回 pbc1；无令牌 401；未知设备 404） |
| M6 | `DELETE /api/devices/{id}/clients/{cid}` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致 |
| M6 | `GET /api/clients/me` | ✅ | ✅ | — | ✅ | ✅ | ⚠️ 云端根 404（经 `/d/` 200）→ F07 |
| M6 | `GET /api/clients/revoked` | ✅ | ✅ | — | ✅ | ✅ | ✅ 一致（需 agent_token；account_token 401） |
| M6 | `POST /api/clients/token/refresh` | ✅ | ❌ | — | — | ✅ | ⛔ 未实现（405 text/plain）→ F07 |
| M6 | `GET /api/health` | ✅ | ✅ | — | — | ✅ | ✅ 一致 |
| M7 | `ANY /d/{device}/api/**`（覆盖 M1–M4 15 条路径） | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ 未认证错误体与直连不同（F08） |
| M8 | `WSS /agent/ws` | ✅ | ✅ | ✅ | — | ✅ | ✅ 一致（HELLO/心跳/CREDIT/重连 3.9s/0.9s） |
| M8 | `GET /agent/stream`、`POST /agent/frames`（降级） | ❌ | — | — | — | — | ⛔ 未实现（404/405）→ 不可执行 |
| M9 | 邀请生成/使用/列表/撤销 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（一次性、fragment 携带、列表无明文） |
| M9 | 在线列表 / 踢出 / 人数上限 | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ 踢出错误体泄露内部 id（F11） |
| M9 | 游客可见范围 / 游客上传 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 一致（忽略 root/target_dir，固定落游客根） |
| M10 | 配对页 / 时间线 / 查看器 / 搜索 / 上传 | ✅ | ✅ | ✅ | — | — | ⚠️ CSP 端口硬编码（F13） |
| M10 | 「我的」页 / 状态条 | ✅ | ⚠️ | — | — | — | ⚠️ 无两条入口链接与二维码（F14） |
| M10 | 游客页面 / 云端托管页面 | ✅ | ✅ | — | ✅ | — | ✅ 一致 |

---

## 三、失败用例明细

> 等级口径：**阻塞** = 越权读他人数据 / 未授权写删 / 令牌可绕过 / 云端落媒体字节 / 沙箱可穿越；
> **严重** = 在特定配置或组合条件下可升级为阻塞；**一般** = 契约/可用性/信息暴露；**轻微** = 体验与一致性。
> 本轮**没有**阻塞级。以下 12 条为实现缺陷（F01–F17 中 5 条为「通过但仍需记录」的观察项）。

### F01【严重】Cookie 被接受为合法凭证（认证面被扩大）
- **复现步骤**
  1. 用合法主人令牌配对一次，取响应头 `Set-Cookie: pb_token=pbc1.…`；
  2. 只带该 Cookie、**不带** `Authorization` 头：
     `curl -H 'Cookie: pb_token=<owner_token>' http://<设备>:8877/api/system/stats`
- **期望 vs 实际**
  - 期望：`401 E2002`（契约把 `client_token` 定义为 `Authorization: Bearer`，Cookie 只应服务于 `<img>/<video>` 的播放场景，见 `docs/04` §2.13）。
  - 实际：**200**，返回完整统计（`media.total=29` 等）。
- **证据**：`results/t_sec_p5c.json` → `E2-007`（另 `t_sec_p5b.json` `E2-037` 同样复现）
  ```
  REQ  GET /api/system/stats   Cookie: pb_token=<owner_token>
  RES  200 {"media":{"total":29,...},"ts":1793171635.7}
  ```
  定位点：`backend/app/auth.py:777` `token = ctx.bearer_token() or ctx.cookie_token("pb_token") or ""`。
- **影响面**：不构成「读他人数据」（Cookie 里仍是受害者自己的令牌），但把**所有写接口的 CSRF 面打开**（见 F02），并使「令牌只在 Authorization 里」的审计假设失效。
- **初步定位**：auth
- **等级说明**：当前 `SameSite=Lax` 使纯浏览器跨站 POST 不携带该 Cookie，故未升为阻塞；一旦有人把 SameSite 改 `None`（为跨站嵌入场景）、或站点出现 XSS/同站子域，**立即升级为阻塞级未授权写/删**。

### F02【严重】写操作不校验 Origin/Referer，仅凭 Cookie 即可删除照片（CSRF 实证）
- **复现步骤**
  1. 取 `pb_token` Cookie（主人）；挑一张临时库照片 `lib/csrf_victim.png`（id=33）；
  2. `POST /api/items/delete`，同时带
     `Cookie: pb_token=<owner>`、`Origin: https://evil.example`、`Referer: https://evil.example/attack.html`，body `{"ids":[33]}`。
- **期望 vs 实际**
  - 期望：跨站来源的写请求被拒（403/CSRF 校验失败），或至少要求 `Authorization` 头。
  - 实际：**200 `{"deleted":1,"failed":[]}`**，文件**确实离开库**（`file_gone=True`，进入回收站）。
- **证据**：`results/t_sec_p5c.json` → `E2-008`
  ```
  POST /api/items/delete  Cookie: pb_token=<owner>  Origin: https://evil.example
  → 200 {"deleted":1,"failed":[]}      file_gone=True
  ```
- **影响面**：影响主人数据完整性（可被跨站删除/移动/改名，前提是 Cookie 被带上）；不影响游客与跨设备隔离。
- **初步定位**：auth（缺 Origin/Referer 校验）+ handler（写接口未强制 Authorization）
- **等级说明**：浏览器侧被 `SameSite=Lax` 挡住 → 暂列严重；F01 任一条件变化即升级阻塞。

### F03【轻微】`pb_token` Cookie 缺 `Secure`，有效期 90 天
- **证据**：`results/t_sec_p5c.json` → `E2-006`
  ```
  Set-Cookie: pb_token=pbc1.…; Path=/; Max-Age=7773677; SameSite=Lax; HttpOnly
  ```
- **期望 vs 实际**：期望 HTTPS 场景下带 `Secure`；实际无。局域网 http 明文通路下该 Cookie 可被同网段嗅探。
- **影响面**：局域网被动嗅探可拿到 90 天的长期凭据。**初步定位**：auth/传输

### F04【一般】CORS 白名单「配置了但不可用」：预检返回 405
- **复现步骤**：配置 `server.cors_origins=["http://allowed.example"]` 起实例（`config_cors.json`，127.0.0.1:8879），发预检：
  `OPTIONS /api/system/ping`，头 `Origin: http://allowed.example`、`Access-Control-Request-Method: GET`
- **期望 vs 实际**
  - 期望：**2xx**（204/200）+ `Access-Control-Allow-Origin`；
  - 实际：**405 E1018 `method_not_allowed`**，但响应却带了全套 CORS 头（`allow-origin`/`allow-headers`/`expose-headers`/`max-age`）。浏览器要求预检 2xx，因此**白名单 CORS 实际不可用**（自相矛盾）。
- **证据**：`results/t_sec_p5e.json` → `D3-006`、`D3-007`
  ```
  OPTIONS /api/system/ping  Origin: http://allowed.example
  → 405  {"error":{"code":"E1018",...}}
     access-control-allow-origin: http://allowed.example
     access-control-allow-headers: Authorization, Content-Type, Content-Range, X-Request-Id
  ```
  定位点：`backend/app/core/dispatch.py:205-260 enable_cors()` + `:436-455` 只做「响应头装饰」，无预检短路分支。
- **影响面**：不影响默认配置（默认不启用 CORS，实测未配实例不给任何 ACAO），仅影响「显式配置白名单」的部署。
- **初步定位**：配置/handler

### F05【一般】云端中继 HTTP 侧不校验 Host（DNS rebinding 面）
- **复现步骤**（本地中继 `127.0.0.1:8888`，`PB_CLOUD_HOSTS=127.0.0.1,localhost`）
  - `GET /api/health` + `Host: evil.example` → **200**
  - `GET /d/{device}/api/system/ping` + `Host: evil.example` → **200**
  - 对照：`WSS /agent/ws` 的 Host 校验**生效**（`tunnel.py:148`）
- **期望 vs 实际**：期望非白名单 Host 一律 4xx；实际 HTTP 路由（`/api/**`、`/d/**`）完全不校验，`main.py:256` 仅打 warning。
- **证据**：`results/t_sec_p5c.json` → `D4-003`、`D4-004`（线上云端由 Cloudflare 挡下伪造 Host：`403`，见 `results/t_online_probe.json`）
- **影响面**：仅当云端被**直连**（无反代/无 CF）时构成 rebinding 面；配合 F01（Cookie 认证）可放大为「内网 DNS rebinding → 借 Cookie 读写」链路。
- **初步定位**：配置/传输

### F06【一般】电脑端默认通配绑定 `0.0.0.0`，与文档「只监听私网」表述冲突
- **证据**：`netstat -ano` → `TCP 0.0.0.0:8877 LISTENING`；`config.check_bind_safety('0.0.0.0')` 返回允许（`"'局域网可访问（需配对）'"`）
- **期望 vs 实际**：`docs/04` §8.5 与 ISSUE-⑫ 写「只监听私网」；实际是通配绑定，由 `allow_lan + 已配 roots + require_pairing=true + panel_http=false` 门控放行。
- **影响面**：`/panel/**` 已被回环门控兜住（局域网访问实测 404），未配对请求一律 401；属**契约表述与实现不一致**，非漏洞。
- **初步定位**：配置

### F07【一般】两个契约要求的云端接口缺失，且错误体不是统一结构
- **复现步骤**
  - `POST /api/clients/token/refresh`（带 client_token）→ **405 `Method Not Allowed`**，`content-type: text/plain`
  - `GET /api/clients/me`（云端根）→ **404 `not found`**，`text/plain`（同样的功能经 `/d/{device}/api/clients/me` → 200 正常）
- **期望 vs 实际**：`docs/04` §3.8/§3.9/§3.10 把两者列为「必需」；实际未实现，且 405/404 响应体不是 `{code,message,request_id}`（违反 B4）。
- **证据**：`results/t_cloud_p3.json` → `M6-011`；`results/t_cloud_p12.json` → `M6-011b`/`M6-026`/`M6-024`
- **影响面**：`client_token` 90 天到期后只能重新配对（体验问题），无安全影响。
- **初步定位**：handler（云端路由缺失）

### F08【一般】同一请求「经隧道」与「直连」的错误响应体不一致（B2/F4 不成立项）
- **复现步骤**：匿名 `GET /api/timeline?limit=5`，分别打直连与 `/d/{device}/`
- **期望 vs 实际**
  - 直连：`401 {"error":{"code":"E2002","message":"client_token_invalid","detail":"缺少凭证：Authorization 头或 pb_token cookie","request_id":"…"}}`
  - 隧道：`401 {"error":{"code":"E2002","message":"缺少配对令牌","detail":null,"request_id":""}}`
  - 除 `request_id` 外 message/detail 也不同 → **判据要求「逐字段一致」不成立**；根因是中继在转发前自己短路鉴权（`forward.py::_reject_auth`）。
- **证据**：`results/t_cloud_p3.json` → `M7-023`
- **影响面**：不影响安全边界；影响客户端错误处理与可观测性（隧道侧 `request_id` 恒为空串，无法与服务端日志对齐）。
- **初步定位**：传输（relay 鉴权短路）

### F09【一般】中继自造错误体 `request_id` 恒为空串（B4 缺口）
- **证据**：`{"error":{"code":"E2001",...,"request_id":""}}`（线上 `/api/auth/me` 匿名、`/api/clients/revoked` 错令牌、`/d/` 未认证均如此）
- **期望 vs 实际**：契约要求 `request_id` 可用于排障；实际空串，无法关联日志。
- **影响面**：仅排障。**初步定位**：传输

### F10【一般】游客兑换与配对码共用同一个限流桶（10 次/分钟/IP），互相拖累
- **复现步骤**：同一 IP 连续 `POST /api/guest/redeem`（或先做几次配对尝试）→ 很快 `429 E4001`
  ```json
  {"error":{"code":"E4001","message":"请求太频繁","detail":null,"retryable":true,"request_id":"","user_hint":"慢一点"}}
  ```
- **期望 vs 实际**：期望游客兑换走 `PUBLIC_RATE_PER_SEC=300` 的宽松桶；实际 `forward.py::_is_pair()` 用 `PUBLIC_POST_PATHS={"/api/pair","/api/guest/redeem"}` 判定 → **redeem 落到 `PAIR_RATE_PER_MIN=10` 的防爆破桶**。
- **证据**：`results/t_guest_p4.json` 多轮 429（最终通过「按用例轮换来源 IP」绕过）；代码定位 `cloud/relay/forward.py` `_handle_public()`。
- **影响面**：同一 NAT 下多名游客（或游客重试）会把桶打满，**连带影响主人给新手机配对**。可用性问题。
- **初步定位**：传输/handler

### F11【一般】踢出游客后的错误体泄露内部 client_id，且状态码用 403 而非 401
- **证据**：`results/t_guest_p4.json` → `E7-033`
  ```
  403 {"error":{"code":"E1023","message":"guest_session_revoked",
       "detail":"guest session revoked client=f0ec83d9e0b20097a669c3…"}}
  ```
- **期望 vs 实际**：`docs/04` §2.13 表格把 `E1023` 定为 403（状态码一致），但 `detail` 含**内部 client_id**，属信息暴露；且未登录/已撤销场景在与「不存在」结合时会形成可区分信号。
- **影响面**：轻微信息泄露（内部标识符）；不影响数据访问。**初步定位**：auth/handler

### F12【一般】`--config` 指向不存在文件时抛未捕获异常并打印完整堆栈（含绝对路径）
- **复现步骤**：`python -m backend.app.main --config data/definitely_missing.json --no-tray`
- **期望 vs 实际**：期望 `ConfigError` 干净退出（如 `panel_http+0.0.0.0` 那样 exit 2）；实际 `exit 1` + 完整 traceback：
  ```
  File "D:\Photos\backend\app\config.py", line 920, in _merge
      for k, v in override.items():
  AttributeError: 'NoneType' object has no attribute 'items'
  ```
- **证据**：`results/t_sec_p5d.json` → `E9-020`；`results/t_sec_p5e.json` → `E9-011`
- **影响面**：本地控制台信息暴露（绝对路径/源码行号），非远程可利用。**初步定位**：配置

### F13【一般】前端 CSP 把端口硬编码为 8787，且 `http:` 云端入口被自身 CSP 拦死
- **复现步骤**
  1. 设备跑在非 8787 端口（如 8877）→ 配对页控制台：
     `Connecting to 'http://192.0.2.10:8877/api/pair' violates the following Content Security Policy directive: "connect-src 'self' https: http://*:8787"`
     → **浏览器界面在该端口完全不可用**；
  2. 云端入口为 `http://host:8888/d/<id>/`（自建/本地中继）时，页面探测云端入口同样被 CSP 拦：
     `Fetch API cannot load http://127.0.0.1:8888/d/<id>/api/system/ping. Refused to connect…`
- **证据**：`results/t_ui_p9.json` → `M10-004`（另 `probe_ui.py` 首轮 CSP 报错记录）
- **影响面**：生产（8787 + https 云端）不受影响；自定义端口或 http 云端部署时**功能不可用**（非安全漏洞，CSP 本身是有效的加固）。
- **初步定位**：前端/配置

### F14【一般】移动端「我的」页没有「两条入口链接 + 二维码」（与 A3 契约不符）
- **复现步骤**：浏览器配对后点「☰ 我的」，读 DOM 与文本。
- **期望 vs 实际**
  - 期望（任务书 A3 / 产品设想）：主入口区展示局域网 + 云端两条链接，并可扫二维码进入主账号视图；
  - 实际：`SettingsView` 只给设备信息、两个 root、游客面板、技术信息（含一条「复制服务地址」）；`hasCloud=False`、二维码元素 `qr=0`。
  - 桌面托盘窗口（`backend/app/tray.py`）**有**这两样：函数层验证通过 —— `pair_qr_url()` 把配对码放进 **fragment**（`http://192.0.2.10:8877/#code=123456&did=…`），`_entry_rows()` 列出「扫码地址 + 在家直连」，`_qr_pil_image()` 生成 148×148 可解码二维码（`results/a3_qr.png`）。
- **证据**：`results/t_ui_p9.json` → `M10-010`/`M10-011`；`results/a3_probe.json`（4/4 通过）
- **影响面**：可用性/一致性；移动端用户拿不到云端入口提示。**初步定位**：前端

### F15【轻微】`move` 目标为 UNC 路径（`\\srv\share`）时被「归一化成库内相对目录」而非拒绝
- **证据**：`results/t_sec_p5.json` → `E1-065`
  ```
  POST /api/items/move {"ids":[2],"target_dir":"\\\\srv\\share"}
  → 200 {"moved":[{"id":2,"from":"2026-12/IMG_BIG.jpg","to":"srv/share/IMG_BIG.jpg",...}]}
  ```
- **期望 vs 实际**：期望 400（非法目录）；实际把 `\\srv\share` 压成库内 `lib/srv/share` 子目录 —— **未越界（安全）**，但未按契约拒绝，且留下 `lib/srv/` 残目录。
- **初步定位**：sandbox（路径归一化策略）

### F16【文档缺陷，非实现】`E3012` 的状态码在 `docs/04` 三处自相矛盾
- `:241` 状态码表：「409 = 目标目录已存在同名且策略为拒绝」
- `:4317` move 参数表：「`fail` 时该条记 `failed`」
- `:4806` 错误码表：「E3012 → 409」
- 实测（`results/t_sec_p5b.json` → `B3-007`）：`200 {"moved":[],"failed":[{"id":31,"code":"E3012","message":"目标目录已有同名文件"}]}`
  → **实现与 `:4317` 一致**，与「E3012→409」矛盾。需修文档而非代码。

### F17【轻微】中继 `/api/devices/{id}/status` 不含 CREDIT 用量字段
- **证据**：`results/t_tunnel_p7b.json` → `M8-010`：status 的键为
  `[clients_count, created_at, id, indexed_count, last_lan, last_seen, name, online, revoked, traffic_today, tunnel_mode, version]`
- CREDIT 实际在设备 `/api/system/info` 的 `tunnel` 块（`credit_used_in` 单调递增 2934→4180，已验）。
- **影响面**：云端后台看不到信用流控指标。**初步定位**：handler

---

## 四、安全结论

| 项 | 结论 | 关键证据 |
|---|---|---|
| **隔离：跨身份（主人 ↔ 游客）** | **成立** | 游客用主库 `media_id` 取 `items/thumb/raw/preview` 全 **404**，与「不存在 id」响应体除 `request_id` 外逐字一致（`C2-014`）；游客 timeline 的 `root` 集合只含 guest；游客搜主库文件名 0 命中；9 个主人接口对游客全拒（403 E2010 / 404）；游客上传忽略 `root=library`、`target_dir=../../outside`，实落 `游客上传/2026/2026-10/…`（`t_guest_p4` 39/39） |
| **隔离：跨设备** | **成立** | 设备 A 的 `client_token` 访问设备 B：中继按 `dev` claim 判 401 E2011/E2012，且响应与令牌不存在一致；同账号另一台与别账号错误码可区分但**均不泄露设备内容**（`C6`、`p3`） |
| **通路限制：游客仅云端** | **成立** | 同一邀请码在**局域网直连**兑换 → 401（`E2001`）；云端 `/d/` 兑换 → 200；游客令牌打局域网通路全被拒（`C7`） |
| **通路限制：面板仅回环** | **成立** | `/panel/**` 在 `panel_http=false` 时一律 404（含局域网 IP）；`panel_http=true` + `0.0.0.0` → 启动即 `ConfigError` exit 2；开启后经局域网访问不可达（`D1`、`M5`、`E9`） |
| **越权（垂直 + 水平）** | **全部被拒** | 游客→主人接口、游客删除/改名/移动、游客指定上传目标主库、A 设备 token 访 B、`../` 越权读取，全部服务端拒绝；越权响应与「不存在」不可区分（`E3`、`C1`、`C2`、`C6`） |
| **注入** | **全部被拒** | SQL 7 条（`' OR 1=1--`、`UNION`、`; DROP TABLE` 等）hits=0、total 不变；`order/root/kind` 注入 → 400 E1001 或安全降级；XSS：文件名/EXIF/搜索词的载荷**原样入库**但浏览器端 `dialogs=[]`（无弹窗），JSON 层转义正确，CSP 生效；命令注入不成立（`;touch pwned.jpg` 仅作文件名，未产生 `pwned*` 文件；`$()`/反引号/`|` 在 init/complete 阶段被 400 拒）；模板注入 `{{7*7}}` → 400，无渲染（`E4`、`p11` 6/6） |
| **令牌与邀请机制可绕过？** | **不可绕过**（但认证面被放大，见 F01/F02） | 无/空/错格式/错 scheme/篡改签名/篡改 `sub`/`alg=none`/URL 传 token/过期/已撤销 → 全 401 且可区分；WS 握手 6 类负例全拒；邀请码一次性、伪造/过期/撤销/重放全拒；邀请 token 只在 URL **fragment**，`performance.getEntriesByType('resource')` 中 0 条含 `pbg1`，`Referer` 为空；审计与 SSE 帧不含令牌 |
| **上传安全（伪装/炸弹/完整性）** | **成立** | HTML 伪装 `.jpg`、MZ 头伪装 `.jpg`、SVG 伪装 `.png` 全拒且不落盘；声明超大像素/损坏 EXIF/截断文件不崩、不 OOM；分片乱序/跳号/重发/超长/越界按契约（400/409/416）；`sha256` 不符拒绝且不留半截；同名（不同内容）加 `_1/_2`，原文件不变；内容相同 → `dedup:true` 复用同一文件；>2GB 与白名单外扩展名拒绝 |
| **限流与资源耗尽** | **成立** | 设备侧 `MAX_OPEN_SESSIONS=3` → 第 4 个并发上传 429 E4002；真并发 4 路上传 3 成功 1 限流；中继 `RATE_LIMIT_PER_SEC=120`/`MAX_INFLIGHT_TOTAL=512` 生效，失败洪泛 429 带 `Retry-After`；200 并发请求服务不崩；**经隧道下载 23.8MB：中继峰值 RSS 26.2MB（单路）/26.6MB（4 路并发），远低于 200MB 判据**（`t_mem_p13`） |
| **信息泄露** | **基本成立，有 3 处轻微项** | 错误体统一 `{code,message,request_id}`、无绝对路径/堆栈/SQL/内部文件名（除 F12 本地控制台堆栈、F11 内部 client_id、F09 空 `request_id`）；响应头不泄露 uvicorn/python 版本（线上仅 `server: cloudflare`）；SSE/审计/日志均不含令牌；目录遍历类错误只回「空列表」或 404，不暴露目录结构 |
| **沙箱（路径穿越）** | **成立** | 16 类载荷（`../..`、绝对路径、盘符、UNC、设备命名空间 `\\?\`、NTFS 数据流 `::$DATA`、保留名 `CON/NUL`、尾随点空格、超长路径、Unicode 控制符 0x00/202E）+ 符号链接/junction 指向库外：`folders/search/timeline/rename/move/raw` 全部拒绝或返回空结果，**无一成功读到库外文件**（`E1`） |

**是否具备对公网开放条件**：核心数据安全面（隔离、越权、沙箱、上传、限流）成立，**不构成「当前不具备对公网开放条件」的数据安全理由**；但存在 3 个必须先处理的**认证/传输面**问题（F01 Cookie 即凭证、F02 无 CSRF 校验的写接口、F05 云端 HTTP 无 Host 校验）。**建议：在修复 F01/F02 之前，不要把电脑端暴露在任何不可信浏览器可达的位置（包括云端域名下的同源页面）**。

---

## 五、不可执行项

| 编号 | 用例/范围 | 缺什么 |
|---|---|---|
| N1 | `M4 /api/albums`（GET/POST、`POST /api/albums/{id}/items`） | **代码未实现**（路由表无 albums 端点，仅 DB 有 `albums`/`album_items` 表）→ 契约「若已实现」不满足 |
| N2 | `M8 GET /agent/stream`、`POST /agent/frames`（降级通路） | **未实现**（404 text/plain / 405）→ 契约「若实现」不满足 |
| N3 | `E9 云端容器运行形态`（非 root / 端口不直暴露 / 日志轮转 / 镜像与编排） | **无容器部署环境**：本轮云端只有本机 `python -m cloud.relay.main` 进程与线上 Cloudflare 托管实例，拿不到 Dockerfile/编排/主机权限 |
| N4 | `A3 桌面托盘窗口截图（两条链接 + 二维码）` | **无 GUI 会话**：托盘窗口是 tkinter，无头环境无法截屏；已改用**函数层验证**（`a3_probe.py` 4/4 通过，含二维码 PNG 证据）替代，窗口视觉效果本身未验 |
| N5 | 线上真实实例的写操作全流程（A2 的写部分） | 按要求不触碰真实主库；线上仅做只读探测（13 项，12 通过，1 项为「HTTP 未强制跳 HTTPS」观察） |

---

## 六、修复优先级建议（按数据安全 > 越权 > 功能 > 体验）

**P0 — 数据安全（对外暴露前必须修）**
1. **F02 写接口 CSRF**：所有写操作（delete/move/rename/rescan/guest 管理）强制 `Authorization: Bearer`，并校验 `Origin`/`Referer` 与配置白名单一致；或改用双提交 token / 自定义头。
2. **F01 Cookie 认证面**：`auth.py:777` 只在**只读、且路径属于 `<img>/<video>` 播放**的场景接受 `pb_token` Cookie；其余接口忽略 Cookie。若保留 Cookie 认证，则必须同时做 CSRF 防护（与 1 联动）。
3. **F05 云端 Host 校验**：把 `/agent/ws` 已有的 `host_allowlist` 逻辑复用到 `/api/**` 与 `/d/**`；未配 `PB_CLOUD_HOSTS` 时默认拒绝而不是只打 warning（线上由 CF 兜住，自建反代没有）。
4. **F03 Cookie `Secure`**：HTTPS 场景加 `Secure`；并把 TTL 从 90 天改为可续期 + 更短有效期（与 F07 的 refresh 一起做）。

**P1 — 越权/隔离（当前已成立，防回归）**
5. 为 C1/C2/C6/C7 建立**常驻回归套件**（本报告的 `t_guest_p4`/`t_sec_p5c` 可直接作为基线）：「越权响应与不存在不可区分」用递归剥离 `request_id` 后逐字比对。
6. **F11** 去掉错误体里的内部 `client_id`，改用与资源无关的固定文案。

**P2 — 功能/契约**
7. **F07** 实现 `POST /api/clients/token/refresh` 与云端 `GET /api/clients/me`；**所有 404/405 也必须回统一错误体**（F07/F09 同源）。
8. **F08/F09** 中继鉴权短路时复用设备侧错误体（同 code/message/detail），并透传/生成 `request_id`（当前恒空串）。
9. **F04** CORS 预检短路：`OPTIONS` 命中白名单直接 204 + 头，不再走 405。
10. **F10** 把 `/api/guest/redeem` 从 `PAIR_RATE` 桶拆出来，改用 `PUBLIC_RATE` 或独立桶。
11. **F06** 统一文档与实现：或把默认绑定改为私网（并保留显式 `0.0.0.0` 开关），或在 `docs/04` §8.5 明确「通配绑定在门控满足时允许」。

**P3 — 体验/一致性**
12. **F12** 配置缺失/非法 JSON 统一走 `ConfigError` 干净退出（不打印 traceback）。
13. **F13** 前端 CSP 的 `connect-src` 改为按实际 `location.port` 生成，或同时放行 `http://*:*`（局域网 http 场景），避免自定义端口/自建云端不可用。
14. **F14** 移动端「我的」补齐「局域网 + 云端」两条入口链接；二维码可复用托盘同源实现（码进 fragment）。
15. **F15** `move` 目标含 UNC/盘符/设备命名空间时直接 400，不要静默归一化；清理 `lib/srv/`、`lib/conflict_*` 等测试残留。
16. **F16** 修文档：`E3012` 在 move 场景是「条目级 `failed` + 200」，不要写成「409」。
17. **F17** 中继 `device_status` 透出 `credit_used_in/out`（设备 `/api/system/info` 的 `tunnel` 块已有）。

---

## 附：可复现材料（`build/_v4b3/`）

| 文件 | 用途 |
|---|---|
| `env_setup.py` | 生成临时库（多格式/大图/截断/伪装/深目录）与 guest/outside 目录 |
| `relay_ctl.py` / `dev_ctl.py` / `extra_ctl.py` | 已建中继(8888)、被测设备(8877)、附加实例(面板 8878 / CORS 8879 / 非法绑定 8880) |
| `proxy8787.py` | 8787→8877 转发（规避前端 CSP 硬编码端口，见 F13） |
| `cdp.py` + `t_ui_p9.py` | Chrome CDP 驱动的真实浏览器用例（配对/时间线/查看器/搜索/上传/我的/游客页/XSS） |
| `t_local_p1/p2.py` | M1–M5、A1 主流程（86 例） |
| `t_cloud_p3.py` / `t_cloud_p12.py` | M6/M7 云端与 `/d/` 转发（48 例） |
| `t_guest_p4.py` | M9 游客全链路 + C7 + E3 + E7 + F1（39 例） |
| `t_sec_p5/p5b/p5c/p5d/p5e.py` | 安全面 D3/D4、E1/E2/E4/E6/E8/E9（217 例） |
| `t_tunnel_p7/p7b.py` | M8 隧道（握手/HELLO/心跳/CREDIT/重连）+ A8（27 例） |
| `t_flow_p8.py` | A6 断网续传 + A7 真并发（8 例） |
| `t_online_p10.py` | 线上云端只读探测（T2 证据，13 项） |
| `t_mem_p13.py` | 中继内存峰值精确测量（按监听 PID） |
| `t_fixup_p11.py` / `probe_a3.py` | 令牌矩阵补测 + 模板注入补测；A3 函数层（二维码/入口清单） |
| `results/*.json` | 全部用例的请求/响应/期望/实际/证据；`final_tally.json` 为最终判定汇总 |
| `results/shots/*.png` | 浏览器截图证据 |

*报告生成时间：2026-10-28；被测提交 `4b3d1e4`；本轮只记录不修复。*
