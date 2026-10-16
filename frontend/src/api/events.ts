/**
 * SSE 事件流消费（`GET /api/events`）。
 *
 * 对应需求：
 *   - **AC-2.15**：电脑上往库目录拷入新图，手机时间线 **30 秒内可见**，
 *     且**不需要任何手动操作**（用户不能"下拉刷新一下就好"）。
 *   - **AC-2.16**：手动重扫时有进度推送（不是只有一个静止的"扫描中"标签）。
 *
 * ## 为什么用原生 `EventSource`
 *
 * 它是浏览器内建的 SSE 客户端：自动重连、自动解析 `event:`/`data:`/`id:`、
 * 逐帧回调。自己用 `fetch` + `ReadableStream` 解析反而要手写缓冲区
 * 拼接（一次 `read()` 可能只拿到半帧，也可能是 3 帧半），且**不会**
 * 自动重连。原生 `EventSource` 唯一做不到的是发送自定义请求头。
 *
 * ## ★ 鉴权：靠 cookie，绝不把 token 拼进 URL
 *
 * `EventSource` 和原生 `<img>` 一样**无法发送 `Authorization` 头**。
 * 所以这里的鉴权完全依赖 `pb_token` cookie（浏览器同源请求自动携带）。
 * 见 `frontend/src/api/storage.ts: setTokenCookie()`。
 *
 * 为什么**不能**用 `?token=`：
 *   1. token 会进浏览器缓存键、服务器访问日志、以及任何 `Referer`；
 *   2. `docs/04-接口设计文档.md:2848` 明确写"服务端只接受 `?token=`
 *      用于 `/api/events` 这一个路径"，而**一期服务端根本没实现**这条
 *      旁路（`backend/app/core/ctx.py: bearer_token()` 只认 `authorization`
 *      头），所以拼了也没用，只会让人以为能用。
 *
 * ## ★ 不能设 `withCredentials`
 *
 * `withCredentials = true` 是给**跨源**请求用的：它要求服务端回
 * `Access-Control-Allow-Credentials: true`，而当前 CORS 配置
 * （`backend/app/core/dispatch.py: enable_cors()`）没有这一项 ——
 * 设了反而会让请求被浏览器拒掉。同源请求本来就会带 cookie。
 *
 * ## 重连策略
 *
 * `EventSource` 自己会重连，但它的重连间隔是浏览器定的、且**遇到
 * HTTP 错误状态码（如 401）会直接放弃**（`readyState === CLOSED`，
 * 永不重试）。所以这里自己接管：
 *   - `onerror` 里看 `readyState`：`CLOSED` 才需要我们手动重连；
 *   - 退避用 `client.ts: backoffMs()`（含抖动，避免所有手机同一毫秒重连）；
 *   - **未配对 / 鉴权失效时不重连** —— 那时连 `/api/system/ping` 都过不去，
 *     每秒重连一次只是把手机电池耗光，且配对页上的用户看不到任何反馈；
 *   - 配对状态变化（重新配对、换电脑）时**重建**连接：cookie 是按
 *     host:port 隔离的，换一台电脑就是换一个 host，旧连接必然 401。
 */
import { absUrl, backoffMs, sleep } from "./client";
import { getBase, getToken } from "./storage";

/** 事件类型（与 `backend/app/events.py: EVENT_TYPES` 对齐）。 */
export type EventType =
  | "hello"
  | "ping"
  | "scan_start"
  | "scan_progress"
  | "scan_done"
  | "thumb_progress"
  | "upload_done"
  | "media_changed"
  | "index_changed";

export interface PbEvent {
  type: EventType;
  ts?: number;
  root?: string;
  // scan_progress
  seen?: number;
  added?: number;
  updated?: number;
  unchanged?: number;
  removed?: number;
  errors?: number;
  // scan_done
  stats?: Record<string, number | string | null>;
  // media_changed
  // added / updated 同上
  // thumb_progress
  done?: number;
  total?: number;
  failed?: number;
  // hello
  device_id?: string;
  via?: string;
}

type Handler = (e: PbEvent) => void;

/** 允许的最大重连次数（之后彻底停下并暴露错误）。 */
const MAX_RETRIES = 8;
/** 两次"连接成功但立刻断开"之间的最短间隔，防止服务端立刻关闭时打转。 */
const MIN_STABLE_MS = 1000;

let es: EventSource | null = null;
let retries = 0;
let stopped = true;
let currentUrl = "";
const handlers = new Set<Handler>();
const stateHandlers = new Set<(connected: boolean) => void>();
let connected = false;

function setConnected(v: boolean): void {
  if (connected === v) return;
  connected = v;
  for (const h of stateHandlers) {
    try {
      h(v);
    } catch {
      /* 单个订阅者抛错不能影响其它订阅者 */
    }
  }
}

function emit(e: PbEvent): void {
  for (const h of handlers) {
    try {
      h(e);
    } catch (err) {
      // 订阅者（store）里抛错不该把整个事件流拖死，
      // 否则一个 bug 会让所有后续事件都收不到。
      console.warn("[events] 订阅者抛错", err);
    }
  }
}

/** 订阅事件。返回取消订阅函数。 */
export function onEvent(h: Handler): () => void {
  handlers.add(h);
  return () => handlers.delete(h);
}

/** 订阅连接状态变化（设置页的"实时"指示灯用）。 */
export function onConnection(h: (connected: boolean) => void): () => void {
  stateHandlers.add(h);
  // 立刻回放当前状态：订阅者可能在连接建立之后才注册
  try {
    h(connected);
  } catch {
    /* 同上 */
  }
  return () => stateHandlers.delete(h);
}

export function isConnected(): boolean {
  return connected;
}

/**
 * 建立连接（幂等：已在连同一个 URL 时什么都不做）。
 *
 * 为什么幂等很重要：`App.vue` 在 `onMounted` 与 `visibilitychange`
 * 都会调用它，不幂等的话每次切回前台都会多开一条 SSE 长连接，
 * 而服务端每条连接都占一个订阅者队列（`QUEUE_MAX` = 256 条事件）。
 */
export function connectEvents(): void {
  const base = getBase();
  const url = `${absUrl("/api/events")}`;

  // 没有 base 说明还没配对：连了也是 401，直接不连。
  if (!base) {
    return;
  }
  // 已经连着同一个地址就不重复开
  if (es && currentUrl === url && es.readyState !== EventSource.CLOSED) {
    return;
  }

  closeEvents();
  stopped = false;
  currentUrl = url;

  try {
    // ★ 不传 withCredentials：见文件头注释（同源本就带 cookie，
    //   设了反而要求服务端回 Access-Control-Allow-Credentials）。
    es = new EventSource(url);
  } catch (err) {
    // 构造 EventSource 本身极少失败（URL 非法时才会），但不能让它
    // 把调用方（App.vue 的 onMounted）整个炸掉。
    console.warn("[events] 无法建立 SSE 连接", err);
    setConnected(false);
    return;
  }

  const openedAt = { t: 0 };

  es.onopen = () => {
    openedAt.t = Date.now();
    retries = 0;
    setConnected(true);
  };

  // 服务端用 `event: <type>` 命名帧，所以必须逐类型注册监听；
  // 只挂 `onmessage` 会**一条都收不到**（命名事件不触发 onmessage）。
  for (const t of [
    "hello",
    "ping",
    "scan_start",
    "scan_progress",
    "scan_done",
    "thumb_progress",
    "upload_done",
    "media_changed",
    "index_changed",
  ] as const) {
    es.addEventListener(t, (ev: MessageEvent) => {
      let data: PbEvent;
      try {
        data = JSON.parse(ev.data) as PbEvent;
      } catch {
        // 半帧/坏帧：SSE 规范保证按 `\n\n` 分帧，但服务端若被截断
        // 仍可能给出非法 JSON。丢掉这一条比让整个流崩掉好。
        return;
      }
      // 服务端已在 data 里放了 type，但以事件名为准更可靠
      // （`data` 里偶尔可能没有 type，比如手写的 hello）。
      if (!data.type) data.type = t;
      emit(data);
    });
  }

  es.onerror = () => {
    const closed = es?.readyState === EventSource.CLOSED;
    setConnected(false);

    // 连接建立后很快断开 → 服务端可能立刻关了（如 401、代理超时）。
    // 这时必须退避，否则会变成紧密重连打转。
    const stable = Date.now() - openedAt.t > MIN_STABLE_MS;

    if (!closed) {
      // 浏览器自己在重连（readyState === CONNECTING），不用我们插手
      return;
    }

    if (stopped) return;

    // 鉴权失效：重连没有意义，而且会掩盖"请重新配对"的提示。
    if (!getToken()) {
      return;
    }

    if (!stable) retries++;
    if (retries >= MAX_RETRIES) {
      console.warn("[events] 重连次数已达上限，停止");
      closeEvents();
      return;
    }

    const wait = backoffMs(retries);
    retries++;
    void (async () => {
      await sleep(wait);
      if (stopped) return;
      // 地址可能在这期间变了（用户换了电脑）
      if (currentUrl !== url) return;
      es = null;
      connectEvents();
    })();
  };
}

/** 主动关闭并停止重连（配对失效、页面卸载时用）。 */
export function closeEvents(): void {
  stopped = true;
  retries = 0;
  setConnected(false);
  if (es) {
    es.onerror = null;
    es.onopen = null;
    es.close();
    es = null;
  }
}

/** 重新连接（配对变化、切回前台时用）。 */
export function reconnectEvents(): void {
  closeEvents();
  connectEvents();
}
