/**
 * 上传通道（XHR）—— **分片 PATCH 必须走这里，不能走 client.ts 的 fetch**。
 *
 * 为什么必须用 XMLHttpRequest（docs/03 §5.2.4）：
 *   `fetch` 的 `Request` 没有 `upload.onprogress`，拿不到"已发送字节数"，
 *   上传进度条就无从实现；`ReadableStream` 请求体在 Safari/iOS 上支持极差。
 *
 * 本文件只负责"发一片"，不管重试与队列 —— 重试在 `stores/upload.ts` 里，
 * 因为重试需要知道 `Content-Range` 与 offset，那是队列的状态。
 */

import { ApiError, backoffMs, currentBase, sanitizeHeaderValue, sleep } from "./client";
import { getClientName, getToken } from "./storage";
import type { UploadPatchResponse } from "./types";

export interface UploadProgress {
  /** 已发送字节（本次分片）。 */
  loaded: number;
  /** 本次分片总字节。 */
  total: number;
  /** 字节/秒（指数滑动平均）。 */
  rate: number;
}

/**
 * 服务端说"你给的 offset 不对"。
 *
 * 后端有两条路径会这样回（`backend/app/handlers/uploads.py`）：
 *   - 乱序/跳号 → **416** `E1011` + 响应头 `X-Upload-Offset: <当前 offset>`
 *   - 没传完就 complete → 409 `E1014`（这里不涉及）
 *
 * ⚠️ docs/03 §5.2.4 写的是"读 `body.current`"，但真实后端把 offset 放在
 *    **响应头** `X-Upload-Offset` 里（uploads.py:502-508），body 里没有
 *    `current` 字段。以代码为准。
 */
export class OffsetMismatch extends Error {
  readonly offset: number;
  readonly status: number;
  constructor(offset: number, status: number) {
    super(`offset 不匹配，服务端已有 ${offset} 字节`);
    this.name = "OffsetMismatch";
    this.offset = offset;
    this.status = status;
  }
}

/** 上传被用户取消。 */
export class UploadAborted extends Error {
  constructor() {
    super("已取消");
    this.name = "UploadAborted";
  }
}

function headerNumber(xhr: XMLHttpRequest, name: string): number {
  const raw = xhr.getResponseHeader(name);
  if (raw === null) return -1;
  const n = parseInt(raw, 10);
  return Number.isFinite(n) ? n : -1;
}

/**
 * 发一个分片。
 *
 * @param uploadId 服务端会话 id
 * @param blob     本片字节（调用方已 slice）
 * @param contentRange `bytes {start}-{end}/{total}`（end 是**闭区间**）
 * @param onProgress 进度回调（250ms 节流由 XHR 自己保证大致频率）
 * @param signal   取消信号；abort 时 XHR 会 abort 并 reject UploadAborted
 *
 * 成功 → 解析 206 响应体；416 → 抛 `OffsetMismatch`（携带服务端 offset）。
 */
export function uploadChunk(
  uploadId: string,
  blob: Blob,
  contentRange: string,
  onProgress: (p: UploadProgress) => void,
  signal: AbortSignal,
): Promise<UploadPatchResponse> {
  return new Promise<UploadPatchResponse>((resolve, reject) => {
    if (signal.aborted) {
      reject(new UploadAborted());
      return;
    }

    const xhr = new XMLHttpRequest();
    let lastLoaded = 0;
    let lastTs = performance.now();
    let rate = 0;

    // ★ base 在这里读一次即可：单片最长 120s，中途换通路会让这一片白传，
    //   而换通路时调用方会 abort 整个队列重排
    const url = `${currentBase()}/api/uploads/${encodeURIComponent(uploadId)}`;
    xhr.open("PATCH", url, true);
    xhr.setRequestHeader("Authorization", `Bearer ${getToken()}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("Content-Range", contentRange);
    const cname = getClientName();
    // ★ 必须过一遍 sanitizeHeaderValue：`X-PB-Client` 里的非 Latin-1
    //   字符会让 `setRequestHeader` 抛 `SyntaxError`（Chromium 报
    //   "String contains non ISO-8859-1 code point"），整个分片发不出去。
    //   与 client.ts 的 fetch 路径是同一个坑，两处都要挡。
    if (cname) xhr.setRequestHeader("X-PB-Client", sanitizeHeaderValue(cname));
    // 服务端返回体是 JSON；不带这个头某些代理会改写
    xhr.setRequestHeader("Accept", "application/json");
    // 2 分钟：云带宽 5Mbps 下 2MB 分片约 3.2s，留 30 倍余量。
    // 无限等待会让"卡死"的上传永远占着队列（docs/03 §5.2.4）
    xhr.timeout = 120_000;
    xhr.responseType = "text";

    xhr.upload.onprogress = (e) => {
      const now = performance.now();
      const dt = (now - lastTs) / 1000;
      // 每 250ms 才算一次速度：避免每帧触发 Vue 响应式更新
      if (dt > 0.25 && e.lengthComputable) {
        const inst = (e.loaded - lastLoaded) / dt;
        // 指数滑动平均，α=0.3：抖动小、可读性好
        rate = rate === 0 ? inst : rate * 0.7 + inst * 0.3;
        lastLoaded = e.loaded;
        lastTs = now;
      }
      onProgress({
        loaded: e.lengthComputable ? e.loaded : 0,
        total: e.lengthComputable ? e.total : blob.size,
        rate,
      });
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText || "{}") as UploadPatchResponse);
        } catch {
          reject(new ApiError({
            status: xhr.status,
            code: "BAD_JSON",
            message: "服务端返回了无法解析的响应",
            retryable: true,
          }));
        }
        return;
      }

      // 416 E1011：乱序/跳号。服务端在 X-Upload-Offset 里给出真实 offset
      if (xhr.status === 416) {
        const off = headerNumber(xhr, "X-Upload-Offset");
        reject(new OffsetMismatch(off >= 0 ? off : 0, 416));
        return;
      }

      // 409 E1014/E1012：状态冲突。E1014 也带 X-Upload-Offset，可续传
      if (xhr.status === 409) {
        const off = headerNumber(xhr, "X-Upload-Offset");
        if (off >= 0) {
          reject(new OffsetMismatch(off, 409));
          return;
        }
      }

      reject(parseXhrError(xhr));
    };

    xhr.onerror = () => reject(new ApiError({
      status: 0,
      code: "NETWORK",
      message: "上传中断，网络不可用",
      retryable: true,
      userHint: "网络中断，稍后会自动重试",
    }));

    xhr.ontimeout = () => reject(new ApiError({
      status: 0,
      code: "TIMEOUT",
      message: "上传超时",
      retryable: true,
      userHint: "上传超时，稍后会自动重试",
    }));

    xhr.onabort = () => reject(new UploadAborted());

    signal.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(blob);
  });
}

/** 从 XHR 的响应体里抠出统一错误体。 */
function parseXhrError(xhr: XMLHttpRequest): ApiError {
  try {
    const body = JSON.parse(xhr.responseText || "{}") as {
      error?: {
        code?: string; message?: string; detail?: string | null;
        retryable?: boolean; request_id?: string | null; user_hint?: string | null;
      };
    };
    const e = body.error;
    if (e) {
      return new ApiError({
        status: xhr.status,
        code: e.code || `HTTP_${xhr.status}`,
        message: e.message || `HTTP ${xhr.status}`,
        detail: e.detail ?? null,
        retryable: e.retryable ?? (xhr.status >= 500 || xhr.status === 429),
        requestId: e.request_id ?? null,
        userHint: e.user_hint ?? null,
      });
    }
  } catch {
    /* 落到下面的兜底 */
  }
  return new ApiError({
    status: xhr.status,
    code: `HTTP_${xhr.status}`,
    message: `上传失败（HTTP ${xhr.status}）`,
    retryable: xhr.status >= 500 || xhr.status === 408 || xhr.status === 429,
  });
}

/**
 * 带重试地发一片。
 *
 * 重试策略（docs/03 §5.5.2）：分片幂等，可安全重试；3 次，退避 1s/3s/9s
 * （这里用 `backoffMs` 的抖动版本，避免多文件并发时同时重试）。
 *
 * `OffsetMismatch` **不算失败**：它携带服务端的真实 offset，
 * 调用方按它重新对齐后继续，用户看不到任何错误。
 */
export async function uploadChunkWithRetry(
  uploadId: string,
  start: number,
  end: number, // 闭区间
  total: number,
  file: Blob,
  onProgress: (p: UploadProgress) => void,
  signal: AbortSignal,
  maxRetry = 3,
): Promise<UploadPatchResponse> {
  const blob = file.slice(start, end + 1);
  const range = `bytes ${start}-${end}/${total}`;
  let lastErr: unknown;

  for (let attempt = 0; attempt <= maxRetry; attempt++) {
    if (signal.aborted) throw new UploadAborted();
    try {
      return await uploadChunk(uploadId, blob, range, onProgress, signal);
    } catch (e) {
      if (e instanceof OffsetMismatch || e instanceof UploadAborted) throw e;
      lastErr = e;
      const err = e instanceof ApiError ? e : null;
      if (err && !err.retryable) throw err;
      if (attempt === maxRetry) throw e;
      await sleep(backoffMs(attempt));
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error("上传失败");
}
