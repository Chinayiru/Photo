
---

### 场景 11：时间线顺序不对

**症状**：照片在时间线上的位置不对；今天拍的照片跑到几年前；时间线整体倒序或乱序。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **EXIF 时间没解析出来**（回退到文件 mtime） | 看 `taken_src` 字段 |
| 2 | **EXIF 时区处理错**（UTC vs 本地） | 差 8 小时就是时区问题 |
| 3 | EXIF 里没有拍摄时间 | 老照片/截图 |
| 4 | 文件 mtime 被改过（复制/同步导致） | `taken_src='mtime'` |
| 5 | 索引排序用了错的字段 | 看 SQL |
| 6 | 时区配置错 | 看服务器/电脑时区 |

**排查步骤**：

```powershell
# 步骤 1：看 taken_at 和 taken_src 的分布（★ 关键）
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
print('taken_src 分布:')
for r in c.execute('SELECT taken_src, COUNT(*) FROM media GROUP BY taken_src'):
    print(f'  {r[0]}: {r[1]}')
"
```

| `taken_src` 值 | 含义 | 是否正常 |
|---|---|---|
| `exif` | 从 EXIF 读到拍摄时间 | ✅ 最好 |
| `mtime` | 回退到文件修改时间 | ⚠️ 可能是对的，也可能不对 |
| `filename` | 从文件名推断 | ⚠️ 一般 |
| `null`/空 | 完全没有 | ❌ 会排到最后 |

```powershell
# 步骤 2：对比几个文件的 EXIF 时间和数据库记录
python -c "
import sqlite3
from PIL import Image

c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
rows = c.execute('SELECT rel_path, taken_at, taken_src FROM media ORDER BY id LIMIT 5').fetchall()
for rel, taken, src in rows:
    full = r'D:\Photos' + '\\' + rel.replace('/', '\\')
    print(f'\n文件: {rel}')
    print(f'  数据库: taken_at={taken}  src={src}')
    try:
        img = Image.open(full)
        exif = img.getexif()
        print(f'  EXIF DateTime={exif.get(306)}  DateTimeOriginal={exif.get(36867)}')
    except Exception as e:
        print(f'  读取 EXIF 失败: {e}')
"

# 步骤 3：看时间线 API 返回的顺序
curl.exe -sS "http://127.0.0.1:8787/api/timeline?limit=10" |
    ConvertFrom-Json | Select-Object -ExpandProperty items |
    Select-Object id, taken_at, rel_path
```

**解决方案**：

| 情况 | 解决 |
|---|---|
| `taken_src` 全是 `mtime` | ★ EXIF 解析器有问题，检查 `exif.py`；重新扫描 |
| 时间差正好 8 小时 | 时区处理错。EXIF 的 `DateTimeOriginal` 通常是**本地时间无时区**，不应该做 UTC 转换 |
| 老照片没 EXIF | 正常，回退到 mtime。可以接受 |
| 截图没有 EXIF | 同上 |
| 排序字段错 | 检查 SQL 是否用了 `ORDER BY taken_at DESC, id DESC`（[设计文档.md](../设计文档.md) §5，对应索引 `idx_media_timeline`） |
| 时区配置错 | `timedatectl` 检查服务器；Windows 检查时区设置 |

```powershell
# 强制重新解析 EXIF
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
c.execute(\"UPDATE media SET taken_src=NULL WHERE taken_src='mtime'\")
c.commit()
print('已重置，点托盘「重新扫描」')
"

# 验证排序索引是否存在
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
for r in c.execute(\"SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='media'\"):
    print(r[0]); print('   ', r[1])
"
# 预期包含：idx_media_timeline ON media(taken_at DESC, id DESC)
```

---

### 场景 12：中文文件名乱码

**症状**：中文文件名显示成 `æˆ¿é—´` 或 `?????`；下载时文件名乱码。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **HTTP 头没做 URL 编码** | 下载时乱码 |
| 2 | 前端解码方式不对 | 显示乱码 |
| 3 | 数据库编码（应该都是 UTF-8） | — |
| 4 | 文件系统编码（Windows 是 UTF-16） | 一般不是问题 |
| 5 | Python 文件 IO 没指定编码 | 读配置/日志时乱码 |
| 6 | 日志文件编码 | 看日志乱码 |

**排查步骤**：

```powershell
# 步骤 1：数据库里的中文对不对
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
for r in c.execute(\"SELECT rel_path FROM media WHERE rel_path GLOB '*[^ -~]*' LIMIT 5\"):
    print(repr(r[0]))
"
# 预期：正常显示中文，如 '2024/2024-05/青甘环线/青海湖.jpg'

# 步骤 2：API 返回的中文对不对
curl.exe -sS "http://127.0.0.1:8787/api/timeline?limit=3" | Out-File -Encoding utf8 H:\temp\api-test.json
Get-Content H:\temp\api-test.json -Encoding utf8

# 步骤 3：文件名在 HTTP 头里的编码
curl.exe -sS -I "http://127.0.0.1:8787/api/items/1/raw?download=1" | Select-String "Content-Disposition"
# 预期：filename*=UTF-8''%E9%9D%92%E6%B5%B7%E6%B9%96.jpg
```

**★ 关键区分：是数据乱码，还是终端显示乱码**

PowerShell 默认编码可能导致中文显示乱码，**但数据是好的**。先确认：

```powershell
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
curl.exe -sS "http://127.0.0.1:8787/api/timeline?limit=3"
```

**解决方案**：

| 层 | 正确做法 |
|---|---|
| **HTTP 响应头** | ★ 用 RFC 5987 格式：`Content-Disposition: attachment; filename="fallback.jpg"; filename*=UTF-8''%E4%B8%AD%E6%96%87.jpg` |
| **JSON 响应** | `Content-Type: application/json; charset=utf-8` |
| **HTML** | `<meta charset="utf-8">` |
| **Python 文件 IO** | 总是写 `encoding='utf-8'` |
| **日志** | 用 `logging.FileHandler(path, encoding='utf-8')` |
| **SQLite** | 默认就是 UTF-8，不用管 |
| **前端 fetch** | 浏览器自动按响应头解码 |
| **URL 参数** | 用 `encodeURIComponent()` 编码 |

```python
# 下载文件名乱码的修复代码（给开发看）
from urllib.parse import quote

def content_disposition(filename: str) -> str:
    """生成兼容中文的 Content-Disposition 头"""
    ascii_fallback = filename.encode('ascii', 'ignore').decode('ascii') or 'download'
    quoted = quote(filename, safe='')
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quoted}"
```

---

### 场景 13：上传到一半失败

**症状**：上传进度到 60% 就停了、提示"上传失败"、文件没出现在库目录。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **手机切后台/锁屏** | 切回来就断了 |
| 2 | 网络抖动（4G 切 WiFi） | 看日志 |
| 3 | **服务端分片校验失败** | 日志有校验错误 |
| 4 | 磁盘满（见场景 7） | `df -h` |
| 5 | 内存不足（见场景 8） | 看容器状态 |
| 6 | 请求超时 | 反代超时设置 |
| 7 | 上传临时区被清理 | 看 uploads_tmp |
| 8 | 会话过期 | 看 upload_sessions 表 |

**排查步骤**：

```powershell
# 步骤 1：看有没有半截的会话记录
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
print('未完成的上传会话:')
for r in c.execute('SELECT * FROM upload_sessions ORDER BY id DESC LIMIT 10'):
    print(r)
"

# 步骤 2：看临时区有什么
Get-ChildItem "D:\Apps\PhotoBridge\data\uploads_tmp" |
    Select-Object Name, Length, LastWriteTime | Sort-Object LastWriteTime -Descending | Select-Object -First 10

# 步骤 3：看日志里的错误
Get-Content "D:\Apps\PhotoBridge\data\logs\agent.log" -Tail 100 |
    Select-String "upload|分片|chunk|校验"
```

**★ 断点续传是怎么工作的**（[设计文档.md](../设计文档.md) §7.2）：

```
1. POST  /api/uploads/init          → 拿到 upload_id
2. PATCH /api/uploads/{id}          → 传分片（带 Content-Range）
3. GET   /api/uploads/{id}          → ★ 查已收到多少字节（offset）
4. 从 offset 继续传
5. POST  /api/uploads/{id}/complete → 完成
```

**关键**：`GET /api/uploads/{id}` 返回 `offset`，客户端应该**从 offset 续传**，而不是从头传。

**解决方案**：

| 情况 | 解决 |
|---|---|
| 手机切后台 | ★ 上传时保持屏幕常亮（`wakeLock`）；别切到别的 App |
| 网络抖动 | **应该自动续传**。如果没续传，检查客户端有没有正确调用 `GET /api/uploads/{id}` |
| 分片校验失败 | 检查 `Content-Range` 格式；检查 sha256 校验逻辑 |
| 磁盘满 | 清理（场景 7） |
| 内存不足 | 见场景 8 |
| 反代超时 | Caddyfile 里 `read_timeout 0`（已在模板里） |
| 会话过期 | 检查 `upload_sessions` 的 TTL 设置，可能需要加长 |
| 临时区被清理 | 检查清理逻辑是不是误删了活跃会话的文件 |

```powershell
# 手动清理失败的会话
# 1. 看临时区里哪些是孤儿（没有对应会话）
python -c "
import sqlite3, os, glob
db = r'D:\Apps\PhotoBridge\data\photobridge.db'
tmp = r'D:\Apps\PhotoBridge\data\uploads_tmp'
c = sqlite3.connect(db)
active = {str(r[0]) for r in c.execute('SELECT upload_id FROM upload_sessions')}
print('活跃会话:', len(active))
for f in glob.glob(os.path.join(tmp, '*')):
    name = os.path.basename(f).split('.')[0]
    if name not in active:
        print(f'孤儿: {f}')
"

# 2. 删除 7 天前的孤儿文件（★ 先 -WhatIf 看会删什么）
Get-ChildItem "D:\Apps\PhotoBridge\data\uploads_tmp" |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-7) } |
    Remove-Item -Force -WhatIf
```

---

### 场景 14：照片没出现在时间线

**症状**：库目录里明明有照片，但时间线上看不到。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **索引没扫到**（新加的文件） | 手动重新扫描 |
| 2 | **扩展名不在白名单** | 看扩展名 |
| 3 | **`.jpg-lx2` 被跳过**（★ 预期行为） | 见场景 15 |
| 4 | 文件在排除的目录里（`@eaDir` 等） | 看路径 |
| 5 | 文件损坏，索引时失败 | 看日志 |
| 6 | 库目录配错了 | 看 config.toml |
| 7 | 文件被标记为 `missing` | 查数据库 |
| 8 | 索引器卡住了 | 看状态 |

**排查步骤**：

```powershell
# 步骤 1：文件在不在数据库里
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
name = '%关键词%'    # ★ 改成你找的文件名关键词
rows = c.execute('SELECT rel_path, ext, missing, thumb_state FROM media WHERE rel_path LIKE ?', (name,)).fetchall()
print(rows if rows else '数据库里没有这个文件')
"

# 步骤 2：数据库里的扩展名分布
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
for r in c.execute('SELECT ext, COUNT(*) FROM media GROUP BY ext ORDER BY COUNT(*) DESC'):
    print(f'  {r[0]}: {r[1]}')
"

# 步骤 3：实际磁盘上有哪些扩展名
python -c "
import os, collections
root = r'D:\Photos'
cnt = collections.Counter()
for dp, dn, fn in os.walk(root):
    for f in fn:
        cnt[os.path.splitext(f)[1].lower().lstrip('.')] += 1
for ext, n in cnt.most_common(25):
    print(f'  {ext}: {n}')
"

# 步骤 4：★ 对比两者 —— 磁盘上有但数据库里没有的扩展名 = 被跳过的
```

**白名单**（[设计文档.md](../设计文档.md) §5.3）：

```
jpg jpeg png webp gif bmp tif tiff avif heic heif
```

**不在白名单的**：视频（mp4/mov/avi）、RAW（dng/cr2/nef/arw）、`.jpg-lx2`。

**解决方案**：

| 情况 | 解决 |
|---|---|
| 新文件没索引 | 点托盘「重新扫描」，或 `POST /api/index/rescan` |
| 扩展名不在白名单 | **正常**。视频/RAW 是二期功能（[11](11-风险登记册与决策记录.md) ADR-005） |
| `.jpg-lx2` | ★ **预期行为**，见场景 15 |
| 在 `@eaDir` 等排除目录 | 正常 |
| 文件损坏 | 用别的软件打开验证；损坏的跳过 |
| 库目录配错 | 检查 `config.toml` 的 `library.roots` |
| 标记为 `missing` | 文件被移走/删了，见场景 20 |
| 索引卡住 | 重启程序 |

```powershell
# 手动触发重新扫描
curl.exe -sS -X POST http://127.0.0.1:8787/api/index/rescan
```

---

### 场景 15：`.jpg-lx2` 文件被跳过（★ 这是预期的，不是 bug）

**症状**：索引日志显示跳过了 22 个 `.jpg-lx2` 文件；这些文件在时间线上看不到。

**★ 这是设计如此，不是故障。**

**`.jpg-lx2` 是什么**：

小米相册（MIUI/HyperOS）在删除照片时，会把它重命名为 `<原文件名>.jpg-lx2` 并放在隐藏的回收站目录里。
这是小米的"回收站"机制——**这些文件是已被删除但尚未彻底清除的照片残留**。

**为什么跳过它们**：

| # | 原因 |
|---|---|
| 1 | **它们是已删除的照片**——显示出来会让用户困惑（"我明明删了怎么还在"） |
| 2 | 它们是**待清理的临时文件**，随时可能被系统删除 |
| 3 | 显示它们会污染时间线 |
| 4 | 恢复它们应该是"小米相册回收站"的功能，不是本项目的职责 |

**设计文档的明确要求**（[设计文档.md](../设计文档.md) §5.3）：

> 图片白名单：`jpg jpeg png webp gif bmp tif tiff avif heic heif`
> **排除 `.jpg-lx2`**（小米相册回收站残留，22 个）

**验证确实跳过了**：

```powershell
# 磁盘上有多少 .jpg-lx2
python -c "
import os
root = r'D:\Photos'
n = 0
for dp, dn, fn in os.walk(root):
    for f in fn:
        if f.lower().endswith('.jpg-lx2'):
            n += 1
            if n <= 5:
                print(os.path.join(dp, f))
print(f'总计: {n} 个')
"
# 预期：22 个（与设计文档一致）

# 数据库里应该一个都没有
python -c "
import sqlite3
c = sqlite3.connect(r'D:\Apps\PhotoBridge\data\photobridge.db')
n = c.execute(\"SELECT COUNT(*) FROM media WHERE rel_path LIKE '%.jpg-lx2'\").fetchone()[0]
print(f'数据库里的 .jpg-lx2 数量: {n}')
"
# 预期：0 ✓
```

**你应该做什么**：

| 想做的事 | 做法 |
|---|---|
| 恢复这些照片 | 用**小米相册 App** 的回收站功能（30 天内可恢复） |
| 彻底清理 | 用小米相册清空回收站，或手动删掉这些文件 |
| 让本项目索引它们 | ❌ **不要**。这违背设计，会污染时间线 |
| 确认项目行为正确 | ✅ 跳过 22 个就是正确行为（[09](09-开发计划与任务分解.md) C2 DoD 明确要求） |

> **★ 提醒**：如果你看到"跳过 22 个 `.jpg-lx2`"的日志，
> 那不是错误，**是功能正常工作的证据**（[09](09-开发计划与任务分解.md) C2 DoD：
> "首次索引 4952 文件 <3 分钟且 .jpg-lx2 跳过(22个)"）。

---

### 场景 16：电脑 CPU 占用高

**症状**：风扇狂转、电脑变卡、任务管理器里 PhotoBridge 占用 50%+ CPU。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **缩略图批量生成**（正常，会结束） | 看索引状态 |
| 2 | 全量重新索引 | 日志有"全量索引" |
| 3 | 索引器死循环（反复扫同一目录） | CPU 持续高不降 |
| 4 | 某个大文件解码卡住 | 看日志停在哪个文件 |
| 5 | 缩略图生成没限制并发 | 看线程数 |
| 6 | 文件监视器（watchdog）风暴 | 大量文件变动事件 |

**排查步骤**：

```powershell
# 步骤 1：看进程 CPU 和线程
$p = Get-Process PhotoBridge
Write-Host "CPU 时间: $($p.CPU) 秒"
Write-Host "内存: $([math]::Round($p.WorkingSet64/1MB,1)) MB"
Write-Host "线程数: $($p.Threads.Count)"
Write-Host "句柄数: $($p.HandleCount)"

# 步骤 2：看索引状态
curl.exe -sS http://127.0.0.1:8787/api/system/info | ConvertFrom-Json |
    Select-Object index_state, index_progress, media_count

# 步骤 3：看日志正在处理什么
Get-Content "D:\Apps\PhotoBridge\data\logs\agent.log" -Tail 30
```

**★ 正常 vs 异常**：

| 场景 | CPU | 持续多久 | 判断 |
|---|---|---|---|
| 首次全量索引（4952 张） | 50–100%（单核满载） | **3–5 分钟** | ✅ 正常 |
| 增量索引（几张新照片） | 20–50% | 几秒 | ✅ 正常 |
| 生成缩略图 | 50–80% | 取决于数量 | ✅ 正常 |
| **空闲时 CPU > 10%** | 高 | 持续 | ❌ 异常 |
| **索引完成但 CPU 不降** | 高 | 持续 | ❌ 异常 |

**解决方案**：

| 情况 | 解决 |
|---|---|
| 正在索引 | **等它跑完**（4952 张约 3 分钟）。不要强制结束，会留下不完整的索引 |
| 索引完成后还在高 | 重启程序 |
| 死循环 | 检查日志有没有反复扫同一个目录；检查是否有循环软链接（`follow_symlinks` 应该是 `false`） |
| 某个文件卡住 | 找到那个文件，用别的软件打开测试；如果是损坏文件，把它移到库外 |
| 并发太高 | 限制缩略图生成的并发数（配置项，通常是 CPU 核数） |
| 文件监视器风暴 | ★ **不要用 OneDrive 同步目录**做库目录 |
| 长期方案 | 把索引/缩略图任务设为低优先级 |

```powershell
# 把 PhotoBridge 进程设为低优先级（重启后失效）
$p = Get-Process PhotoBridge
$p.PriorityClass = [System.Diagnostics.ProcessPriorityClass]::BelowNormal
Write-Host "已设为低优先级"

# 检查有没有软链接循环（不应该有）
Get-ChildItem "D:\Photos" -Recurse -Force -Directory |
    Where-Object { $_.LinkType -in @('SymbolicLink','Junction') } |
    Select-Object FullName, LinkType, Target
# ★ 库目录里不应该有软链接（沙箱会拒绝）
```

---

### 场景 17：手机 OOM 崩溃

**症状**：手机浏览器标签页突然刷新/白屏/闪退；看大图时特别容易发生。

**可能原因**：

| # | 原因 | 怎么判断 |
|---|---|---|
| 1 | **查看器同时解码太多图** | 快速滑动时崩 |
| 2 | 预览图太大（1600px 太大） | 大图才崩 |
| 3 | Service Worker 缓存占内存 | 用久了才崩 |
| 4 | 手机本身内存小（2–3 GB） | 低端机 |
| 5 | 同时开了太多标签页 | 关掉别的标签就好 |
| 6 | 浏览器没释放图片内存 | 长时间浏览后崩 |

**排查步骤**：

```
1. 看崩的时候在做什么
   - 快速滑动？→ 解码太多（原因 1）
   - 看大图？→ 预览尺寸太大（原因 2）
   - 浏览很久了？→ 内存泄漏（原因 6）

2. 换一台手机试
   - 别的手机不崩 → 是这台手机内存小（原因 4）

3. 换浏览器试
   - Chrome 崩、Edge 不崩 → 浏览器问题

4. 清空网站数据再试
   Chrome → 设置 → 网站设置 → 找到你的域名 → 清除数据
```

**解决方案**：

| 情况 | 解决 |
|---|---|
| 快速滑动崩溃 | ★ 前端限制**同时解码的图片数量**（通常 3–5 张），其余用占位图 |
| 看大图崩溃 | ★ 降低预览尺寸（1600px → 1200px）；或分块加载大图 |
| 长时间浏览崩溃 | 前端用 `URL.revokeObjectURL()` 释放；虚拟列表只渲染可见项 |
| 手机内存小 | 关掉别的标签页/App；用轻量浏览器 |
| 缓存占内存 | 清理网站数据 |
| 所有手机都崩 | 前端有内存泄漏，需要改代码 |

```javascript
// 前端应该做的优化（给开发看）
// 1. 虚拟列表：只渲染可见的几十张
// 2. 图片懒加载 + 解码控制
// <img loading="lazy" decoding="async" />
// 3. 限制同时解码数（用 IntersectionObserver + 队列）
// 4. 及时释放 Blob URL
const url = URL.createObjectURL(blob);
URL.revokeObjectURL(url);   // 用完后
// 5. 缩略图用 320px（约 20KB），不要用预览图当列表图
```

**验证内存占用**（电脑上模拟）：

```
Chrome 手机模式 → F12 → Performance monitor → 看 JS heap size
滑动 100 张后如果 heap 一直涨 → 有泄漏
```
