/**
 * 与后端对齐的 TypeScript interface。
 *
 * 权威来源（按优先级）：
 *   1. `backend/app/handlers/common.py`   —— row_to_item / row_to_detail 的字段
 *   2. `backend/app/handlers/{system,browse,auth,uploads}.py` —— 各 handler 的响应体
 *   3. `backend/app/errors.py`            —— 统一错误体
 *   4. `docs/04-接口设计文档.md` §4        —— 文档（与代码冲突时以代码为准）
 *
 * ⚠️ 这些类型是**手工照抄**的，不是从后端生成的。改后端字段时这里要同步。
 */

/* ------------------------------------------------------------------ *
 * 通用
 * ------------------------------------------------------------------ */

/** `backend/app/errors.py: to_response()` 的 `error` 子对象。 */
export interface ApiErrorBody {
  code: string;
  message: string;
  detail: string | null;
  retryable: boolean;
  request_id: string | null;
  user_hint: string | null;
}

export interface ApiErrorEnvelope {
  error: ApiErrorBody;
}

/** 分页游标：`backend/app/handlers/common.py` 里 timeline/search 共用。 */
export interface PageInfo {
  next_cursor: string | null;
  has_more: boolean;
  server_time: number;
  tz_offset_minutes: number;
}

/* ------------------------------------------------------------------ *
 * §4.1 系统
 * ------------------------------------------------------------------ */

/** `GET /api/system/ping` —— `backend/app/handlers/system.py: ping()` */
export interface PingResponse {
  pong: boolean;
  ts: number;
  device_id: string;
  device_name: string;
  version: string;
  /** "lan" | "relay" | "local"，一期恒为 "lan"。 */
  via: string;
}

/** `GET /api/system/info` → `roots`，来自 `backend/app/state.py: roots_info()` */
export interface RootInfo {
  id: string;
  label: string;
  /** 脱敏路径提示（盘符 + 末级目录），不是主机上的完整路径。 */
  path: string;
  /** "readonly" | "readwrite" */
  mode: string;
  recursive: boolean;
  count: number;
  enabled: boolean;
  /** 字节数；拿不到磁盘信息时为 null。 */
  disk_free: number | null;
  disk_total: number | null;
}

/** `GET /api/system/info` → `index`，来自 `backend/app/state.py: index_info()` */
export interface IndexInfo {
  state: "scanning" | "idle" | "never";
  total: number;
  pending_thumbs: number;
  scanned?: number;
  added?: number;
  updated?: number;
  removed?: number;
  duration_sec?: number;
  last_scan_at?: number | null;
}

/** `GET /api/system/info` → `watch`，来自 `backend/app/state.py: watch_info()`
 *
 * ★ 存在的理由：`index.watch` 配置默认为 true，但服务端必须如实报告
 * 监听器**是否真的在跑**。watchdog 未安装、没有可用根目录、或用户
 * 在设置里关掉了监听时，`running` 会是 false 而 `reason` 给出原因。
 * 展示型字段：拿它做 UI 提示，不要用它做逻辑判断。
 */
export interface WatchInfo {
  enabled: boolean;
  running: boolean;
  reason?: string;
  roots?: string[];
  pending?: number;
  dropped?: number;
  debounce_sec?: number;
  events?: number;
  indexed?: number;
  missing?: number;
  skipped?: number;
  errors?: number;
}

/** `GET /api/system/info` → `limits`，来自 `backend/app/state.py: limits_info()` */
export interface LimitsInfo {
  max_file_bytes: number;
  chunk_size: number;
  upload_concurrency: number;
  session_ttl_hours: number;
}

/** `GET /api/system/info` 的完整响应（`backend/app/handlers/system.py: info()`） */
export interface SystemInfo {
  app: {
    name: string;
    version: string;
    build: string;
    started_at: number;
    uptime_sec: number;
  };
  tunnel: {
    mode: string;
    cloud_url: string | null;
    connected: boolean;
    session_id: string | null;
    connected_at: number | null;
    last_error: string | null;
    /**
     * 手机端在云端的基址 `https://域名/d/<云端 device_id>`；未配云端时为空串。
     *
     * 可选：老服务端（二期之前）不返回它，`_assert_fields` 只查必需字段。
     */
    public_base?: string;
  };
  device: { device_id: string; device_name: string };
  roots: RootInfo[];
  index: IndexInfo;
  /**
   * 文件系统监听状态。★ 后端在 `index` 之后新增，老服务端不返回它，
   * 所以是可选字段（`_assert_fields` 只查必需字段）。
   */
  watch?: WatchInfo;
  thumbs: {
    cache_bytes: number;
    cache_files: number;
    sizes: number[];
    quality: number;
    format: string;
  };
  server: {
    host: string;
    port: number;
    lan_ips: string[];
    bind_scope: string;
  };
  limits: LimitsInfo;
  /**
   * 电脑端外壳（托盘/自启）的状态，`GET /api/system/info` 的 `host_app`。
   *
   * ★ 每个字段都可能缺失：服务端 `_host_app_info()` 把每项都用
   * try/except 包住（注册表读不到、pystray 没装等），因为这是**展示型**
   * 字段 —— 读失败不该让整个 `/api/system/info` 返回 500，
   * 那会让手机端首页白屏。所以前端消费时也要按可选处理。
   */
  host_app?: {
    /** 是否运行在 PyInstaller 打包的 exe 里（false = 源码/命令行运行）。 */
    packaged: boolean;
    /** 系统托盘是否在运行（用户能看到右下角图标）。 */
    tray: boolean;
    autostart: {
      supported: boolean;
      enabled: boolean;
      /** 手机端不返回注册表命令行，固定为 null。 */
      command: string | null;
      /** 手机端不返回程序完整路径，固定为 null。 */
      expected: string | null;
      /** command 与 expected 不一致 —— 说明程序被移动过，自启会静默失效。 */
      stale: boolean;
    };
  };
  time: { server_time: number; tz_offset_minutes: number };
}

/** `GET /api/system/stats`（`backend/app/handlers/system.py: stats()`） */
export interface SystemStats {
  media: {
    total: number;
    images: number;
    videos: number;
    missing: number;
    by_kind: Record<string, number>;
  };
  thumbs: {
    pending: number;
    ok: number;
    failed: number;
    cache_bytes: number;
    cache_files: number;
  };
  db: {
    size_bytes: number;
    page_count: number;
    freelist_count: number;
    page_size: number;
  };
  uploads: { open_sessions: number; received_bytes: number };
  ts: number;
}

/* ------------------------------------------------------------------ *
 * §4.2 浏览
 * ------------------------------------------------------------------ */

/**
 * 时间线/搜索结果里的一项 —— `backend/app/handlers/common.py: row_to_item()`
 * （common.py:66-86）。
 *
 * ⚠️ 注意命名：这里用 `w`/`h` 而不是 `width`/`height`，`name` 而不是
 * `file_name` —— 照抄后端实际输出的 JSON 键名，不要"顺手改好看"。
 */
export interface MediaItem {
  id: number;
  name: string;
  /** `"image"` | `"video"`（ISSUE-055）。 */
  kind: string;
  /** 视频时长（秒）；图片为 null。 */
  duration: number | null;
  /** Unix 秒（不是毫秒）。 */
  taken_at: number;
  /** "exif" | "mtime" | "filename" | "none" */
  taken_src: string;
  w: number | null;
  h: number | null;
  /**
   * 缩略图版本串，来自 `thumb_etag(320, row)` = `w320-{mtime_ms}-{size}`。
   * `thumb_state !== "ok"` 时为 null。
   *
   * ★ 必须拼进图片 URL（`?v=`）：后端对缩略图发
   *   `Cache-Control: immutable, max-age=31536000`，不带版本串时
   *   用户替换了同名文件，手机也永远看旧图。
   *
   * ★ 后端**恒给值**（不因 thumb_state 是 pending 而为 null）。
   *   它是"源文件版本"而非"生成好了没"——若 pending 时为 null，
   *   前端就不发请求，而服务端恰恰靠请求按需生成，会死锁。
   *   能不能长期缓存由响应头决定（占位图 `max-age=60`）。
   */
  thumb_v: string | null;
  size: number;
  /** "ok" | "pending" | "failed" | "none" */
  thumb_state: string;
  has_gps: boolean;
  root: string;
  dir: string;
}

/** `GET /api/timeline`、`GET /api/search` 的响应。 */
export interface TimelineResponse extends PageInfo {
  items: MediaItem[];
}

/**
 * `GET /api/search` 的响应。
 *
 * 结构与时间线完全一致（`backend/app/handlers/browse.py: search()` 复用
 * 同一套分页与 `row_to_item`），单独起个名字只是为了让调用点读起来更清楚。
 */
export type SearchResponse = TimelineResponse;

/** `backend/app/handlers/common.py: row_to_detail()`（common.py:99-146）。 */
export interface ExifInfo {
  camera: string | null;
  lens: string | null;
  iso: number | null;
  fnum: number | null;
  exposure: string | null;
  focal: number | null;
}

/**
 * 视频播放元信息（`common.py: _video_block()`）；图片为 `null`。
 *
 * 两个字段回答的是不同问题，别混用：
 * - `playable`：**Chrome 桌面**确定能播吗（容器 + 编码都在白名单里）。
 * - `hint`：该不该把地址递给 `<video>`。`ok` 放心给；`maybe` 是"容器对、
 *   编码看客户端"（HEVC：iOS Safari 能播，Windows Chrome 多半不能）——
 *   应当**试一下**并挂 `error` 兜底；`no` 是连容器都不对，直接给下载链接。
 *
 * 只按 `playable` 决定渲染的话，iPhone 录的 HEVC 在 iOS 上会被判"放不了"，
 * 而它其实能播 —— 主客户端上白退一次。
 */
export interface VideoInfo {
  duration: number | null;
  container: string;
  vcodec: string;
  acodec: string;
  playable: boolean;
  hint: "ok" | "maybe" | "no";
  /** 给用户看的一句中文：为什么可能播不了（能播时为空串）。由服务端出。 */
  reason: string;
  /**
   * `moov` 是否在 `mdat` 之前。`false` = 未做 faststart：
   * 播放器必须先读到文件尾才能起播，本地无感、跨网络就是"转圈十几秒"。
   * `null` = 没探到（老库行、非 MP4 容器）。
   */
  faststart: boolean | null;
}

export interface MediaDetail {
  id: number;
  root: string;
  rel_path: string;
  name: string;
  ext: string;
  kind: string;
  size: number;
  mtime: number;
  sha256: string | null;
  taken_at: number;
  /** 带本地时区偏移的 ISO8601；由 `common.py: _iso()` 生成。 */
  taken_at_iso: string;
  taken_src: string;
  width: number | null;
  height: number | null;
  orientation: number | null;
  /** 视频元信息；图片为 null。 */
  video: VideoInfo | null;
  exif: ExifInfo;
  gps: { lat: number; lon: number } | null;
  thumb_state: string;
  /** 见 `MediaItem.thumb_v` 的说明：恒有值，不因 pending 而为 null。 */
  thumb_v: string | null;
  /** true 表示"记录在但文件被移动了"，后端仍返回 200。 */
  missing: boolean;
  indexed_at: number;
  urls: {
    thumb: string;
    preview: string;
    raw: string;
    /** 视频播放流（仅 `kind === "video"` 时有值，Range/206 复用 `/raw`）。 */
    video: string | null;
  };
  /**
   * ⚠️ 语义与直觉相反（`backend/app/handlers/browse.py:177-190`）：
   *   - `prev_id` = **更新**的那张（taken_at 更大，或同秒时 id 更大）
   *   - `next_id` = **更旧**的那张
   * 时间线是倒序（新的在前），所以"下一张"= 更旧 = `next_id`。
   */
  prev_id: number | null;
  next_id: number | null;
}

/** `GET /api/folders` → `backend/app/handlers/browse.py: folders()` */
export interface FolderEntry {
  /** 目录名（最后一层）。 */
  name: string;
  /** 相对根的路径，可直接当 `dir` 回传。 */
  path: string;
  /** 递归条目数（含所有子孙目录）。点进去看到的"本层直属"数量是 `direct`。 */
  count: number;
  /** 本层直属条目数（不含子目录）。 */
  direct: number;
  has_children: boolean;
  cover_id: number | null;
}

export interface FoldersResponse {
  root: string;
  path: string;
  folders: FolderEntry[];
  /** 子目录个数。 */
  total: number;
  /** 当前这一层**直属**的条目数（面包屑旁的数字用它）。 */
  files: number;
}

/** `POST /api/items/delete` → `backend/app/handlers/browse.py: delete_items()` */
export interface DeleteResponse {
  deleted: number;
  /** ⚠️ 不是数字，是 `[{id, error}]`（browse.py:756-767）。 */
  failed: { id: number; error: string }[];
}

/**
 * `POST /api/items/move` → `backend/app/handlers/browse.py: move_items()`。
 *
 * ⚠️ 三态而不是"成功/失败"两态：`skipped` 是**有意**跳过的
 * （目标目录就是当前目录，或 `on_conflict: "skip"`），与 `failed`
 * 的"真的没搬成"完全不同 —— 前端提示语必须分开写。
 */
export interface MoveResponse {
  moved: { id: number; from: string; to: string; renamed: boolean }[];
  /** 有意跳过的 id 列表（不是错误）。 */
  skipped: number[];
  failed: { id: number; code: string; message: string }[];
  target_dir: string;
}

/** `POST /api/items/rename` → `backend/app/handlers/browse.py: rename_item()` */
export interface RenameResponse {
  id: number;
  from: string;
  to: string;
  /**
   * ⚠️ 改名可能改变 `taken_at`：原来的时间来自文件名时，新名字里
   * 没有日期了就得重算（EXIF → 文件名 → mtime）。所以前端要用
   * 响应里的值回写时间线，而不是保留原值。
   */
  taken_at: number;
  taken_src: string;
}

/** `POST /api/index/rescan` → `backend/app/handlers/browse.py: rescan()`，HTTP 202 */
export interface RescanResponse {
  started: boolean;
  root: string | null;
  ts: number;
}

/* ------------------------------------------------------------------ *
 * §4.3 上传
 * ------------------------------------------------------------------ */

export interface UploadInitRequest {
  name: string;
  size: number;
  mtime?: number;
  /** 一期不算 sha256（docs/03 §5.2.4），留空以走"文件名+大小+mtime"判重。 */
  sha256?: string;
  root?: string;
  target_dir?: string;
  declared_taken_at?: number;
}

/**
 * `POST /api/uploads/init` 的响应
 * （`backend/app/handlers/uploads.py: init_upload()`，201 新建 / 200 秒传或复用）。
 *
 * ⚠️ docs/04 §4.3.1 声称秒传时 `upload_id` 为 null，但真实代码
 * （uploads.py:79）**无条件**返回 `result["upload_id"]` —— 以代码为准，
 * 前端不要假设 `exists:true` 时 `upload_id` 不存在。
 */
export interface UploadInitResponse {
  upload_id: string;
  /** 服务端权威分片大小（直连 8 MiB），客户端传的 client_chunk_size 会被忽略。 */
  chunk_size: number;
  /** 服务端已有的字节数；续传从这里开始。 */
  offset: number;
  /** true = 秒传命中，一个字节都不用传。 */
  exists: boolean;
  media_id: number | null;
  expires_at: number;
  max_size: number;
  /** 一期恒为 false。 */
  sha256_required: boolean;
  dedup: boolean;
  final_rel?: string | null;
}

/**
 * `PATCH /api/uploads/{upload_id}` 的响应，HTTP 206
 * （`backend/app/handlers/uploads.py: patch_upload()`）。
 */
export interface UploadPatchResponse {
  upload_id: string;
  /** ★ 以这个为准更新本地 offset，不要自己加。 */
  offset: number;
  received: number;
  total_size: number;
  chunk_size: number;
  /**
   * 会话过期时间。**每次成功分片都会刷新**为 `max(created_at + ttl, now + ttl)`。
   *
   * ★ 审计 F18：实现早先只写 `received` 不写 `expires_at`，24 小时是
   * "从 init 起算的绝对期限" —— 跨天续传会 409 `E1012`（`retryable=false`），
   * 前端 `upload.ts` 见不可重试就 `releaseSession()`，重试从 0 开始，
   * 已传的字节全部白费。现在续期了，客户端也拿得到这个值。
   */
  expires_at: number;
  complete: boolean;
  /** 整片重复上传时的幂等标记。 */
  duplicate?: boolean;
}

/** `GET /api/uploads/{upload_id}`（`backend/app/handlers/uploads.py: get_upload()`） */
export interface UploadStatusResponse {
  upload_id: string;
  offset: number;
  received: number;
  total_size: number;
  chunk_size: number;
  status: string;
  expires_at: number;
  complete: boolean;
  sha256: string | null;
  /** 客户端在 `init` 时声明的原始文件名（只作记录，归档时会重新命名）。 */
  declared_name: string | null;
  /** 目标目录（相对库根）。 */
  target_rel: string | null;
  /** 实际落盘路径，`status === "completed"` 时才有值。 */
  final_rel: string | null;
  /**
   * 已入库的媒体 id，`status === "completed"` 时才有值。
   *
   * ★ 审计 F13/F19：这是客户端**判断"其实已经成功了"的唯一依据**。
   * `complete` 成功后临时文件被删、磁盘 0 字节，早先 `_reconcile_offset`
   * 会把 `received` 写回 0，客户端于是读到 `offset=0, complete=false`、
   * 按 0 重传、拿 `409 E1012` 不可重试，白白丢掉整个文件。
   */
  media_id: number | null;
  /** 秒传（内容哈希命中）时为 true；还没完成时为 null。 */
  dedup: boolean | null;
}

/**
 * `POST /api/uploads/{upload_id}/complete`，201（已 complete 过则 200）
 * （`backend/app/handlers/uploads.py: complete_upload()`）。
 */
export interface UploadCompleteResponse {
  upload_id: string;
  /** ⚠️ 可能为 null：文件已落盘但还没入库，仍算成功。 */
  media_id: number | null;
  rel_path: string | null;
  dedup: boolean;
  already_completed: boolean;
  sha256: string | null;
}

/** `DELETE /api/uploads/{upload_id}`（`backend/app/handlers/uploads.py: abort_upload()`） */
export interface UploadAbortResponse {
  upload_id: string;
  aborted: boolean;
}

/* ------------------------------------------------------------------ *
 * §4 配对与设备
 * ------------------------------------------------------------------ */

export interface PairRequest {
  code: string;
}

/** `POST /api/pair`，HTTP **201**（`backend/app/handlers/auth.py: pair()`）。 */
export interface PairResponse {
  client_token: string;
  client_id: string;
  device_id: string;
  device_name: string;
  expires_at: number;
  lan_ips: string[];
  server_time: number;
}

/** `GET /api/clients/me`（`backend/app/handlers/auth.py: me()`）。 */
export interface MeResponse {
  client_id: string;
  client_name: string;
  device_id: string;
  device_name: string;
  lan_ips: string[];
  server_time: number;
}

/**
 * `GET /api/devices`（`backend/app/handlers/auth.py: list_devices()`）。
 *
 * ⚠️ 这个列表对 owner 与 viewer 是**两种形状**（ISSUE-034 ③）：
 * 权限信息不该描述别的设备，所以 viewer 拿到的响应里没有
 * `created_at`（配对时刻）与 `role`。两者都标成可选，
 * 否则前端会以为它们永远在，`viewer` 那一侧静默拿到 `undefined`。
 */
export interface DeviceEntry {
  client_id: string;
  name: string;
  /** 只有 owner 的响应里有。 */
  created_at?: number;
  last_seen: number;
  revoked: boolean;
  /** 只有 owner 的响应里有：`"owner"` / `"viewer"`。 */
  role?: "owner" | "viewer";
}

export interface DevicesResponse {
  items: DeviceEntry[];
}

