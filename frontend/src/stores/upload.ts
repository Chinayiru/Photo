/**
 * 上传队列 store。
 *
 * 完整流程（任务硬约束 + `backend/app/handlers/uploads.py`）：
 *   ① `POST /api/uploads/init`  → `{upload_id, chunk_size, offset, exists, ...}`
 *   ② 循环 `PATCH /api/uploads/{id}`（`Content-Range: bytes start-end/total`，
 *      分片大小用 init 返回的 `chunk_size`）
 *   ③ `POST /api/uploads/{id}/complete`
 *
 * 必须处理的边界：
 *   - `exists:true`     秒传命中 → 一个字节都不传，直接标"已存在"
 *   - **416 + `X-Upload-Offset`** 乱序/跳号 → 按服务端 offset 重新对齐继续
 *     （⚠️ docs/03 §5.2.4 写的是读 body.current，但真实后端放在**响应头**
 *      里，见 `backend/app/handlers/uploads.py:502-508`，以代码为准）
 *   - 断网 → 指数退避重试，最多 3 次（`uploader.ts: uploadChunkWithRetry`）
 *   - 取消 → `DELETE /api/uploads/{id}` 并 abort 掉 XHR
 *
 * 并发：Wi-Fi 下 3、蜂窝下 2（docs/03 §5.5.2），与后端
 * `limits_info().upload_concurrency = 3` 对齐。
 */

import { computed } from "vue";
import { ApiError, api } from "../api/client";
import {
  OffsetMismatch,
  UploadAborted,
  uploadChunkWithRetry,
} from "../api/uploader";
import type {
  UploadAbortResponse,
  UploadCompleteResponse,
  UploadInitResponse,
  UploadStatusResponse,
} from "../api/types";
import { createStore } from "./index";

export type TaskStatus =
  | "PENDING"    // 等待调度
  | "UPLOADING"  // 正在传
  | "PAUSED"     // 用户暂停
  | "RETRY"      // 等待重试
  | "COMPLETING" // 校验中
  | "DONE"       // 已完成
  | "DEDUPED"    // 服务端已有（秒传或去重）
  | "FAILED"     // 失败
  | "CANCELED";  // 用户取消

export interface UploadTask {
  /** 本地 id。 */
  id: string;
  /** 服务端会话 id（init 之后才有）。 */
  uploadId: string | null;
  file: Blob;
  name: string;
  size: number;
  mtime: number;
  /** 已成功写入服务端的字节数（**永远以服务端返回为准**）。 */
  offset: number;
  chunkSize: number;
  status: TaskStatus;
  retries: number;
  error: string;
  /** 字节/秒。 */
  rate: number;
  /** 本片已发送字节（用来画单文件进度条：offset + 片内进度）。 */
  sentInChunk: number;
  startedAt: number;
  finishedAt: number;
  mediaId: number | null;
}

const initial = {
  tasks: [] as UploadTask[],
  /** 正在跑的并发数。 */
  active: 0,
  /** 队列是否被用户暂停。 */
  paused: false,
  /** 上传目标根（空 = 服务端默认）。 */
  root: "",
};

export const uploadStore = createStore(initial);

/** 每个任务一个 AbortController，取消时用。 */
const controllers = new Map<string, AbortController>();

/** 网络类型决定并发度：蜂窝 2，Wi-Fi 3（docs/03 §5.5.2）。 */
function concurrency(): number {
  const c = (navigator as Navigator & { connection?: { type?: string } }).connection;
  const t = c?.type ?? "";
  if (t === "cellular" || t === "2g" || t === "3g" || t === "4g" || t === "5g") return 2;
  return 3;
}

let seq = 0;
function localId(): string {
  seq += 1;
  return `t${Date.now().toString(36)}${seq.toString(36)}`;
}

/* ------------------------------------------------------------------ *
 * 入队
 * ------------------------------------------------------------------ */

/**
 * 添加文件到队列。
 *
 * `accept="image/*"` 已在 input 上限制，但安卓某些文件管理器会绕过；
 * 这里再按 MIME/扩展名筛一遍，避免用户误选了视频后传一个 2GB 失败。
 */
export function enqueue(files: FileList | File[] | null): number {
  if (!files) return 0;
  const s = uploadStore.state;
  let n = 0;
  for (const f of Array.from(files)) {
    if (!isImage(f)) continue;
    // 同名同大小同 mtime 已在队列里 → 跳过（用户重复选了同一张）
    if (s.tasks.some((t) => t.name === f.name && t.size === f.size
      && t.mtime === Math.floor(f.lastModified / 1000))) continue;
    s.tasks.push({
      id: localId(),
      uploadId: null,
      file: f,
      name: f.name,
      size: f.size,
      mtime: Math.floor(f.lastModified / 1000),
      offset: 0,
      chunkSize: 0,
      status: "PENDING",
      retries: 0,
      error: "",
      rate: 0,
      sentInChunk: 0,
      startedAt: 0,
      finishedAt: 0,
      mediaId: null,
    });
    n += 1;
  }
  if (n > 0) {
    s.paused = false;
    schedule();
  }
  return n;
}

function isImage(f: File): boolean {
  if (f.type.startsWith("image/")) return true;
  return /\.(jpe?g|png|webp|gif|bmp|heic|heif|avif|tiff?)$/i.test(f.name);
}

/* ------------------------------------------------------------------ *
 * 调度
 * ------------------------------------------------------------------ */

function pending(t: UploadTask): boolean {
  return t.status === "PENDING" || t.status === "RETRY";
}

/**
 * 读取任务状态。
 *
 * 为什么包一层函数：TypeScript 会把 `task.status` 的类型**收窄**到
 * 最后一次赋值的字面量（比如 `task.status = "UPLOADING"` 之后，
 * 编译器就认定它永远是 `"UPLOADING"`）。但 `doUpload()` 是异步的，
 * 它在 `await` 内部会改状态，编译器看不到 —— 于是
 * `if (task.status === "PAUSED")` 会被报成"类型没有重叠"的假错误。
 * 通过一个函数返回，收窄就被打断，拿到的是真实的联合类型。
 */
function statusOf(t: UploadTask): TaskStatus {
  return t.status;
}

/**
 * 调度器：把 PENDING/RETRY 的任务填到并发上限。
 * 每次任务状态变化后都要调一次（包括失败与完成）。
 */
function schedule(): void {
  const s = uploadStore.state;
  if (s.paused) return;
  const limit = concurrency();
  while (s.active < limit) {
    const t = s.tasks.find(pending);
    if (!t) break;
    void runTask(t);
  }
}

/* ------------------------------------------------------------------ *
 * 单任务
 * ------------------------------------------------------------------ */

async function runTask(task: UploadTask): Promise<void> {
  const s = uploadStore.state;
  if (task.status === "PAUSED" || task.status === "CANCELED") return;
  if (task.status === "DONE" || task.status === "DEDUPED" || task.status === "FAILED") return;

  s.active += 1;
  task.status = "UPLOADING";
  task.error = "";
  task.startedAt = task.startedAt || Date.now();

  const ctrl = new AbortController();
  controllers.set(task.id, ctrl);

  try {
    await doUpload(task, ctrl.signal);
    // doUpload 内部会把状态置成 DONE/DEDUPED/PAUSED，这里只兜底
    const st = statusOf(task);
    if (st === "UPLOADING" || st === "COMPLETING") task.status = "DONE";
    task.finishedAt = Date.now();
    task.rate = 0;
  } catch (e) {
    if (e instanceof UploadAborted || ctrl.signal.aborted) {
      task.status = "CANCELED";
      task.error = "";
    } else if (statusOf(task) !== "PAUSED") {
      const err = e instanceof ApiError ? e : null;
      task.status = "FAILED";
      task.error = err?.friendly ?? (e instanceof Error ? e.message : "上传失败");
    }
    task.rate = 0;
  } finally {
    controllers.delete(task.id);
    s.active = Math.max(0, s.active - 1);
    schedule();
  }
}

async function doUpload(task: UploadTask, signal: AbortSignal): Promise<void> {
  const s = uploadStore.state;

  /* ① init（或续传时问服务端 offset） */
  if (!task.uploadId) {
    const body: Record<string, unknown> = {
      name: task.name,
      size: task.size,
      mtime: task.mtime,
    };
    if (s.root) body.root = s.root;
    // ★ 一期不带 sha256（docs/03 §5.2.4：手机算 hash 太慢，500 张要 1-2 分钟），
    //   所以 exists 基本恒为 false，秒传路径主要留给二期的 Worker 版本
    const r = await api.post<UploadInitResponse>("/api/uploads/init", body, {
      retry: 2,
      timeoutMs: 30_000,
    });

    if (r.exists) {
      // 秒传命中：一个字节都不用传
      task.offset = task.size;
      task.mediaId = r.media_id ?? null;
      task.status = "DEDUPED";
      task.error = "";
      return;
    }
    task.uploadId = r.upload_id;
    // ★ 分片大小必须用服务端返回的（客户端传的 client_chunk_size 会被忽略）
    task.chunkSize = r.chunk_size || 2 * 1024 * 1024;
    // ★ 服务端可能已经有部分数据（复用了上次的会话）
    task.offset = r.offset || 0;
    task.sentInChunk = 0;
  } else {
    // 续传：先问服务端收到哪了，**不信本地 offset**
    const st = await api.get<UploadStatusResponse>(`/api/uploads/${task.uploadId}`, {
      retry: 2,
      timeoutMs: 15_000,
    });
    if (st.complete) {
      // 已经传完了，直接进 complete
      task.offset = st.total_size;
      if (st.chunk_size) task.chunkSize = st.chunk_size;
    } else {
      task.offset = st.offset;
      if (st.chunk_size) task.chunkSize = st.chunk_size;
    }
  }

  if (signal.aborted) throw new UploadAborted();
  if (statusOf(task) === "PAUSED") return;

  /* ② 循环发分片 */
  let guard = 0;
  while (task.offset < task.size) {
    if (signal.aborted) throw new UploadAborted();
    // 用户可能在传的过程中点了暂停 —— 状态是异步改的，必须重读
    if (statusOf(task) === "PAUSED") return;
    // 死循环保护：正常最多 ceil(size/chunk)+几次重对齐
    guard += 1;
    if (guard > 100_000) throw new Error("分片次数异常，已中止");

    const chunk = task.chunkSize || 2 * 1024 * 1024;
    const start = task.offset;
    const end = Math.min(start + chunk, task.size) - 1; // 闭区间

    try {
      const res = await uploadChunkWithRetry(
        task.uploadId,
        start,
        end,
        task.size,
        task.file,
        (p) => {
          task.rate = p.rate;
          task.sentInChunk = p.loaded;
        },
        signal,
      );
      // ★ 以服务端返回为准
      task.offset = res.offset;
      task.sentInChunk = 0;
      if (res.complete) break;
    } catch (e) {
      if (e instanceof OffsetMismatch) {
        // 服务端说 offset 不对：按它给的重新对齐继续，**不算失败**
        task.offset = Math.max(0, Math.min(e.offset, task.size));
        task.sentInChunk = 0;
        continue;
      }
      throw e;
    }
  }

  if (signal.aborted) throw new UploadAborted();

  /* ③ complete */
  task.status = "COMPLETING";
  task.sentInChunk = 0;
  const done = await api.post<UploadCompleteResponse>(
    `/api/uploads/${task.uploadId}/complete`,
    {},
    { retry: 2, timeoutMs: 120_000 },
  );
  task.mediaId = done.media_id ?? null;
  // `dedup:true` = 服务端按内容判重，文件已存在；与"秒传"是两件事，
  // 但对用户而言都是"没占新空间"
  task.status = done.dedup ? "DEDUPED" : "DONE";
  task.offset = task.size;
}

/* ------------------------------------------------------------------ *
 * 用户操作
 * ------------------------------------------------------------------ */

/** 取消单个任务。已传的分片会被服务端丢弃（`DELETE /api/uploads/{id}`）。 */
export async function cancelTask(id: string): Promise<void> {
  const s = uploadStore.state;
  const t = s.tasks.find((x) => x.id === id);
  if (!t) return;
  if (t.status === "DONE" || t.status === "DEDUPED") return;

  controllers.get(id)?.abort();
  controllers.delete(id);
  t.status = "CANCELED";

  if (t.uploadId) {
    try {
      await api.del<UploadAbortResponse>(`/api/uploads/${t.uploadId}`, { timeoutMs: 10_000 });
    } catch {
      // 会话本身可能已过期，取消失败不影响本地状态
    }
  }
  schedule();
}

/** 重试一个失败/取消的任务。 */
export function retryTask(id: string): void {
  const t = uploadStore.state.tasks.find((x) => x.id === id);
  if (!t) return;
  if (t.status !== "FAILED" && t.status !== "CANCELED" && t.status !== "PAUSED") return;
  t.status = "PENDING";
  t.error = "";
  t.retries = 0;
  t.finishedAt = 0;
  // ★ 保留 uploadId 与 offset：走"问服务端 offset"的续传路径
  schedule();
}

/** 全部暂停。正在传的分片会被 abort，但服务端已收的字节保留。 */
export function pauseAll(): void {
  const s = uploadStore.state;
  s.paused = true;
  for (const t of s.tasks) {
    if (t.status === "UPLOADING" || t.status === "COMPLETING") {
      controllers.get(t.id)?.abort();
      controllers.delete(t.id);
      t.status = "PAUSED";
      t.rate = 0;
    }
  }
}

/** 继续全部。 */
export function resumeAll(): void {
  const s = uploadStore.state;
  s.paused = false;
  for (const t of s.tasks) {
    if (t.status === "PAUSED") {
      t.status = "PENDING";
      t.error = "";
    }
  }
  schedule();
}

/** 清掉已完成/已取消/已去重的条目（失败的不清，用户还要重试）。 */
export function clearFinished(): void {
  const s = uploadStore.state;
  s.tasks = s.tasks.filter(
    (t) => t.status !== "DONE" && t.status !== "DEDUPED" && t.status !== "CANCELED",
  );
}

/** 退出配对时清空整个队列。 */
export function clearAll(): void {
  for (const t of uploadStore.state.tasks) controllers.get(t.id)?.abort();
  controllers.clear();
  uploadStore.reset();
}

/**
 * 切后台回来：所有"进行中"的状态一律回到 PENDING。
 *
 * 页面被回收时无法知道最后一次分片是否成功落盘（docs/03 §5.5.6），
 * 重置后走"先 GET /uploads/{id} 问 offset"的路径，以服务端为准，永远正确。
 */
export function requeueInterrupted(): void {
  const s = uploadStore.state;
  s.active = 0;
  for (const t of s.tasks) {
    if (t.status === "UPLOADING" || t.status === "COMPLETING" || t.status === "RETRY") {
      t.status = "PENDING";
      t.sentInChunk = 0;
      t.rate = 0;
    }
  }
  if (!s.paused) schedule();
}

/* ------------------------------------------------------------------ *
 * 派生值
 * ------------------------------------------------------------------ */

/** 已发送字节（含片内进度）。 */
function sentBytes(t: UploadTask): number {
  if (t.status === "DONE" || t.status === "DEDUPED") return t.size;
  return Math.min(t.size, t.offset + t.sentInChunk);
}

export const overall = computed<{ percent: number; sent: number; total: number; rate: number }>(() => {
  const tasks = uploadStore.state.tasks;
  let total = 0;
  let sent = 0;
  let rate = 0;
  for (const t of tasks) {
    total += t.size;
    sent += sentBytes(t);
    // 总速度 = 各任务速度之和（docs/03 §5.5.4：用总字节/总时间会被排队时间拉低）
    if (t.status === "UPLOADING") rate += t.rate;
  }
  return { percent: total ? (sent / total) * 100 : 0, sent, total, rate };
});

/** 队列里还有未完成的任务。 */
export const hasActive = computed<boolean>(() =>
  uploadStore.state.tasks.some((t) => pending(t) || t.status === "UPLOADING"
    || t.status === "COMPLETING"),
);

/** 队列为空（用来决定是否显示"全部完成"）。 */
export const isEmptyQueue = computed<boolean>(() => uploadStore.state.tasks.length === 0);

/** 汇总文案。 */
export const summaryText = computed<string>(() => {
  const ts = uploadStore.state.tasks;
  if (ts.length === 0) return "还没有选择文件";
  const done = ts.filter((t) => t.status === "DONE" || t.status === "DEDUPED").length;
  const failed = ts.filter((t) => t.status === "FAILED").length;
  const parts = [`${done}/${ts.length} 完成`];
  if (failed) parts.push(`${failed} 失败`);
  const r = overall.value.rate;
  if (r > 0) parts.push(`${fmtRate(r)}`);
  return parts.join(" · ");
});

/** 单文件进度（0-100）。 */
export function taskPercent(t: UploadTask): number {
  if (!t.size) return 0;
  return Math.min(100, (sentBytes(t) / t.size) * 100);
}

/** 单文件已传字节。 */
export function taskSent(t: UploadTask): number {
  return sentBytes(t);
}

export function fmtRate(bps: number): string {
  if (!Number.isFinite(bps) || bps <= 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let v = bps;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}/s`;
}

export function fmtBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

/** 状态文案。 */
export function statusText(t: UploadTask): string {
  switch (t.status) {
    case "PENDING": return "等待中";
    case "UPLOADING": return t.rate > 0 ? `上传中 ${fmtRate(t.rate)}` : "上传中";
    case "PAUSED": return "已暂停";
    case "RETRY": return "重试中";
    case "COMPLETING": return "校验中";
    case "DONE": return "已完成";
    case "DEDUPED": return "已存在，跳过";
    case "FAILED": return t.error || "失败";
    case "CANCELED": return "已取消";
    default: return "";
  }
}

export function statusClass(t: UploadTask): string {
  switch (t.status) {
    case "DONE": return "ok";
    case "DEDUPED": return "ok";
    case "FAILED": return "bad";
    case "CANCELED": return "idle";
    case "PAUSED": return "warn";
    default: return "busy";
  }
}
