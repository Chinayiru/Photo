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
  };
  device: { device_id: string; device_name: string };
  roots: RootInfo[];
  index: IndexInfo;
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
  exif: ExifInfo;
  gps: { lat: number; lon: number } | null;
  thumb_state: string;
  /** 见 `MediaItem.thumb_v` 的说明：恒有值，不因 pending 而为 null。 */
  thumb_v: string | null;
  /** true 表示"记录在但文件被移动了"，后端仍返回 200。 */
  missing: boolean;
  indexed_at: number;
  urls: { thumb: string; preview: string; raw: string };
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
  name: string;
  path: string;
  count: number;
  has_children: boolean;
  cover_id: number | null;
}

export interface FoldersResponse {
  root: string | null;
  path: string;
  folders: FolderEntry[];
  total: number;
}

/** `POST /api/items/delete` → `backend/app/handlers/browse.py: delete_items()` */
export interface DeleteResponse {
  deleted: number;
  /** ⚠️ 不是数字，是 `[{id, error}]`（browse.py:756-767）。 */
  failed: { id: number; error: string }[];
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

/** `GET /api/devices`（`backend/app/handlers/auth.py: list_devices()`）。 */
export interface DeviceEntry {
  client_id: string;
  name: string;
  created_at: number;
  last_seen: number;
  revoked: boolean;
}

export interface DevicesResponse {
  items: DeviceEntry[];
}
