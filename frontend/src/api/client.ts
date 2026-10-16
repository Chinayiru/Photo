/**
 * 统一 API client —— **所有网络请求的唯一出口**。
 *
 * 职责（任务硬约束）：
 *   1. 拼 base URL（局域网 `http://ip:8787`；二期云隧道 `https://域名/d/{id}`）
 *   2. 带 `Authorization: Bearer <token>` 与 `X-PB-Client`
 *   3. 解析统一错误体 `{"error":{code,message,detail,retryable,request_id,user_hint}}`
 *   4. token 失效（401/E2002 或 403/E2004）→ 清 token 并跳回配对页
 *   5. 可重试错误按指数退避自动重试
 *
 * ★ 关键设计：`baseUrl` 用 **getter 每次读取**（docs/03 §5.2.2）。
 *   反面写法 `API.setBase(computeBase())` 在模块加载时算一次，通路切换后失效。
 *   这里直接读 localStorage，天然永远是当前值。
 *
 * 注意：**上传分片不走这里**，走 `uploader.ts` 的 XHR 通道（fetch 拿不到
 * 上传进度）。这里只负责 init/complete/abort 这些小的 JSON 请求。
 */

import {
  getBase,
  getClientName,
  getToken,
  clearPairing,
  hasTokenCookie,
  setTokenCookie,
} from "./storage";
import type { ApiErrorBody, ApiErrorEnvelope } from "./types";

/* ------------------------------------------------------------------ *
 * 错误类型
 * ------------------------------------------------------------------ */

/** 后端返回的结构化错误，或网络层的等价物。 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string | null;
  readonly retryable: boolean;
  readonly requestId: string | null;
  /** 后端给的、可以直接展示给用户的中文提示。 */
  readonly userHint: string | null;

  constructor(init: {
    status: number;
    code: string;
    message: string;
    detail?: string | null;
    retryable?: boolean;
    requestId?: string | null;
    userHint?: string | null;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.detail = init.detail ?? null;
    this.retryable = init.retryable ?? false;
    this.requestId = init.requestId ?? null;
    this.userHint = init.userHint ?? null;
  }

  /** 面向用户的一句话（优先 user_hint，其次 message）。 */
  get friendly(): string {
    return this.userHint || this.message || this.code || "请求失败";
  }

  /** 网络不通 / 超时（status 0）。 */
  get isNetwork(): boolean {
    return this.status === 0;
  }

  /** 配对失效，需要重新配对。 */
  get isAuthLost(): boolean {
    return (this.status === 401 && this.code === "E2002")
      || (this.status === 403 && this.code === "E2004");
  }
}

/** 网络层错误（fetch 直接抛 TypeError，拿不到状态码）。 */
function networkError(cause: unknown): ApiError {
  const msg = cause instanceof Error && cause.name === "AbortError"
    ? "请求超时"
    : "无法连接到电脑，请确认手机与电脑在同一个 Wi-Fi";
  return new ApiError({
    status: 0,
    code: "NETWORK",
    message: msg,
    retryable: true,
    userHint: msg,
  });
}

/* ------------------------------------------------------------------ *
 * 认证失效回调
 * ------------------------------------------------------------------ */

type AuthLostHandler = (err: ApiError) => void;
let authLostHandler: AuthLostHandler | null = null;
/** 防止并发请求同时触发多次"跳回配对页"。 */
let authLostFiredAt = 0;

/**
 * 注册"配对失效"处理器（由 main.ts 注入，避免 client ↔ store 循环依赖）。
 *
 * 用回调而不是直接 import store：`client.ts` 是最底层模块，
 * 让它依赖 store 会形成 `store → client → store` 的循环。
 */
export function onAuthLost(handler: AuthLostHandler): void {
  authLostHandler = handler;
}

function handleAuthLost(err: ApiError): void {
  // 5 秒内只处理一次：时间线滚动时可能同时有 3 个请求返回 401
  const now = Date.now();
  if (now - authLostFiredAt < 5000) return;
  authLostFiredAt = now;
  clearPairing();
  authLostHandler?.(err);
}

/* ------------------------------------------------------------------ *
 * 工具
 * ------------------------------------------------------------------ */

export function sleep(ms: number): Promise<void> {
  return new Promise((r) => window.setTimeout(r, ms));
}

/**
 * 退避毫秒数（照抄 docs/03 §5.2.1）。
 * 有 `Retry-After` 时优先用它（服务端限流时算得比客户端准）。
 */
export function backoffMs(attempt: number, retryAfter?: string | null): number {
  if (retryAfter) {
    return Math.min(parseInt(retryAfter, 10) * 1000 || 1000, 30_000);
  }
  const base = Math.min(500 * 2 ** attempt, 8000);
  // ★ 抖动：防止所有并发请求在同一毫秒重试，把刚恢复的服务再打挂
  return base * (0.8 + Math.random() * 0.4);
}

/** 当前基地址（每次读 localStorage，不缓存）。 */
export function currentBase(): string {
  return getBase();
}

/**
 * 把相对 API 路径变成可直接放进 `<img src>` 的绝对 URL。
 *
 * ★ 必须带 `v`（thumb_v）：后端给缩略图发 `immutable, max-age=31536000`，
 *   不带版本串时用户换了同名文件，手机永远显示旧图。
 *   详见 `backend/app/handlers/common.py: thumb_etag()` 的注释。
 */
export function absUrl(path: string, params?: Record<string, string | number | null | undefined>): string {
  const base = currentBase();
  const url = `${base}${path.startsWith("/") ? path : `/${path}`}`;
  if (!params) return url;
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === "") continue;
    qs.set(k, String(v));
  }
  const s = qs.toString();
  return s ? `${url}?${s}` : url;
}

/** 缩略图 URL：`/api/items/{id}/thumb?size=320&v={thumb_v}` */
export function thumbUrl(id: number, v: string | null, size = 320): string {
  return absUrl(`/api/items/${id}/thumb`, { size, v });
}

/** 预览图 URL：`/api/items/{id}/preview?size=1600&v={thumb_v}` */
export function previewUrl(id: number, v: string | null, size = 1600): string {
  return absUrl(`/api/items/${id}/preview`, { size, v });
}

/** 原图 URL：`/api/items/{id}/raw?download=1` */
export function rawUrl(id: number, download = false): string {
  return absUrl(`/api/items/${id}/raw`, download ? { download: 1 } : undefined);
}

/**
 * 客户端标识头：电脑端用它写审计日志（`backend/app/handlers/auth.py: _client_name`）。
 *
 * ★★ 这个头**只能放 ASCII**。HTTP 头按 RFC 7230 是 ISO-8859-1 字节串，
 *   浏览器在 `Headers.set()` 阶段就拒绝非 Latin-1 码点：
 *
 *     TypeError: Failed to execute 'set' on 'Headers':
 *       String contains non ISO-8859-1 code point.
 *
 *   而 `once()` 是**所有**请求的必经之路（时间线、缩略图、上传 init…），
 *   所以这个头一旦非法，症状是**整个 App 一个请求都发不出去**，且
 *   报的是"无法连接到电脑，请确认手机与电脑在同一个 Wi-Fi" ——
 *   用户会去重启路由器、检查 Wi-Fi，永远不会想到是设备名的问题。
 *
 *   触发条件（实测）：桌面浏览器与非 iPhone/iPad 的设备上，平台名会
 *   落到默认分支的中文 `"浏览器"`；用户若在设置里把客户端名改成中文
 *   （`pb.client_name`），同样会炸。
 *
 *   修法：出口处统一把非 ASCII 字符替换掉，而不是信任上游（`getClientName()`
 *   来自 localStorage，用户可写任意字符串）。
 */
export function sanitizeHeaderValue(raw: string): string {
  // 先去掉换行：CR/LF 会构成响应头注入（HTTP 头不能含 CR LF）
  let s = raw.replace(/[\r\n]+/g, " ");
  // 再把非 Latin-1 码点（> 0xFF）替换成 "?"。逐码点判断而不是
  // 用正则区间，因为 `\u0100-\uFFFF` 之外还有代理对（emoji）。
  let out = "";
  for (const ch of s) {
    const cp = ch.codePointAt(0) ?? 0;
    out += cp <= 0xff ? ch : "?";
  }
  return out.trim();
}

function clientHeader(): string {
  const custom = getClientName();
  if (custom) return sanitizeHeaderValue(custom);
  // 不设置的话后端退化成 User-Agent 前 48 字符（一长串浏览器版本号），
  // 托盘设备列表里两行都认不出来
  const ua = navigator.userAgent;
  const ios = /iPhone|iPad|iPod/.test(ua);
  const android = /Android/.test(ua);
  const plat = ios ? "iPhone" : android ? "Android" : "Browser";
  return sanitizeHeaderValue(`PhotoBridge ${plat}`);
}

/* ------------------------------------------------------------------ *
 * 请求
 * ------------------------------------------------------------------ */

export interface RequestOptions {
  method?: string;
  /** 自动 JSON.stringify 并设 Content-Type。 */
  body?: unknown;
  /** 查询参数，值为 null/undefined/"" 时跳过。 */
  query?: Record<string, string | number | boolean | null | undefined>;
  /** 额外请求头。 */
  headers?: Record<string, string>;
  /** 自动重试次数（仅对 retryable 的错误生效）。默认 0。 */
  retry?: number;
  /** 超时毫秒；默认 20s。 */
  timeoutMs?: number;
  /** 不触发"配对失效"跳转（配对接口自身用）。 */
  skipAuthLost?: boolean;
  signal?: AbortSignal;
}

async function parseError(res: Response): Promise<ApiError> {
  let body: ApiErrorEnvelope | null = null;
  try {
    const text = await res.text();
    if (text) body = JSON.parse(text) as ApiErrorEnvelope;
  } catch {
    /* 非 JSON 响应（例如反向代理的 HTML 错误页）—— 走下面的兜底 */
  }
  const e: ApiErrorBody | undefined = body?.error;
  if (e) {
    return new ApiError({
      status: res.status,
      code: e.code || `HTTP_${res.status}`,
      message: e.message || `HTTP ${res.status}`,
      detail: e.detail,
      retryable: e.retryable,
      requestId: e.request_id,
      userHint: e.user_hint,
    });
  }
  return new ApiError({
    status: res.status,
    code: `HTTP_${res.status}`,
    message: `HTTP ${res.status} ${res.statusText || ""}`.trim(),
    retryable: res.status >= 500 || res.status === 408 || res.status === 429,
  });
}

async function once<T>(path: string, opt: RequestOptions): Promise<T> {
  const base = currentBase();
  if (!base) {
    throw new ApiError({
      status: 0,
      code: "NO_BASE",
      message: "尚未配对",
      userHint: "请先配对电脑",
    });
  }

  const url = new URL(`${base}${path.startsWith("/") ? path : `/${path}`}`);
  if (opt.query) {
    for (const [k, v] of Object.entries(opt.query)) {
      if (v === null || v === undefined || v === "") continue;
      url.searchParams.set(k, String(v));
    }
  }

  const headers = new Headers(opt.headers);
  const token = getToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
    // ★ 自愈：token 在 localStorage 里但 cookie 没了（用户手动清了
    // 站点数据只清了一部分、从别的浏览器导入过 localStorage、
    // 或早期版本从未写过 cookie）。这里补一次，否则 `<img>` 缩略图
    // 会全部 401 而 JS 请求一切正常 —— 用户看到的是"照片全是灰块"。
    // 后端收到 Authorization 头时也会顺手回一个 Set-Cookie，
    // 两条路互为兜底。
    if (!hasTokenCookie()) setTokenCookie(token);
  }
  headers.set("X-PB-Client", clientHeader());

  let payload: BodyInit | undefined;
  if (opt.body !== undefined) {
    headers.set("Content-Type", "application/json");
    payload = JSON.stringify(opt.body);
  }

  const ctrl = new AbortController();
  const timeoutMs = opt.timeoutMs ?? 20_000;
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs);
  if (opt.signal) {
    if (opt.signal.aborted) ctrl.abort();
    else opt.signal.addEventListener("abort", () => ctrl.abort(), { once: true });
  }

  let res: Response;
  try {
    res = await fetch(url.toString(), {
      method: opt.method ?? "GET",
      headers,
      body: payload,
      signal: ctrl.signal,
      // 局域网直连是明文 HTTP，凭据不进 cookie，这里无所谓；
      // 显式 omit 避免未来加 cookie 时出现跨源带凭据的问题
      credentials: "omit",
      cache: "no-store",
    });
  } catch (e) {
    throw networkError(e);
  } finally {
    window.clearTimeout(timer);
  }

  if (!res.ok) {
    const err = await parseError(res);
    if (err.isAuthLost && !opt.skipAuthLost) handleAuthLost(err);
    throw err;
  }

  // 204 / 空体
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}

/**
 * 发一个 API 请求。所有业务代码都走这里。
 *
 * 重试语义（照抄 docs/03 §5.2.1）：只重试 `retryable` 的错误
 * （5xx / 408 / 429 / 网络错），4xx 参数错误重试没有意义。
 */
export async function request<T>(path: string, opt: RequestOptions = {}): Promise<T> {
  const maxRetry = opt.retry ?? 0;
  let lastErr: unknown;
  for (let attempt = 0; attempt <= maxRetry; attempt++) {
    try {
      return await once<T>(path, opt);
    } catch (e) {
      lastErr = e;
      const err = e instanceof ApiError ? e : networkError(e);
      // 配对失效不重试：token 已经清了，重试只会再拿一个 401
      if (err.isAuthLost) throw err;
      if (!err.retryable || attempt === maxRetry) throw err;
      await sleep(backoffMs(attempt));
    }
  }
  throw lastErr instanceof Error ? lastErr : networkError(lastErr);
}

export const api = {
  get: <T>(path: string, opt?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...opt, method: "GET" }),

  post: <T>(path: string, body?: unknown, opt?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...opt, method: "POST", body }),

  patch: <T>(path: string, body?: unknown, opt?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...opt, method: "PATCH", body }),

  del: <T>(path: string, opt?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...opt, method: "DELETE" }),
};
