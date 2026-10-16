/**
 * 系统信息 store（设置页用）。
 *
 * 数据来源：
 *   - `GET /api/system/info`  → `backend/app/handlers/system.py: info()`
 *   - `GET /api/system/stats` → `backend/app/handlers/system.py: stats()`
 *   - `POST /api/index/rescan` → `backend/app/handlers/browse.py: rescan()`（HTTP 202）
 */

import { computed } from "vue";
import { ApiError, api } from "../api/client";
import { onConnection, onEvent } from "../api/events";
import type { RescanResponse, SystemInfo, SystemStats } from "../api/types";
import { createStore } from "./index";

const initial = {
  info: null as SystemInfo | null,
  stats: null as SystemStats | null,
  loading: false,
  error: "",
  /** 已触发扫描，等待 indexer 把 state 变成 "scanning"。 */
  scanning: false,
  /** "重新扫描" 的按钮节流，避免连点。 */
  lastRescanAt: 0,
  lastLoadedAt: 0,
  /** SSE 长连接是否活着（设置页"实时"指示灯）。 */
  live: false,
  /** 扫描进度（来自 `scan_progress` 事件；AC-2.16）。 */
  progress: null as {
    root: string;
    seen: number;
    added: number;
    updated: number;
    unchanged: number;
    removed: number;
    errors: number;
    total?: number;
    percent: number;
    startedAt: number;
  } | null,
  /** 最近一次 `media_changed`（AC-2.15：有新照片进来了）。 */
  mediaChangedAt: 0,
  /** 最近一次 `scan_done` 的摘要。 */
  lastScan: null as Record<string, number | string | null> | null,
  /** 最近一次扫描的失败原因（成功/未扫描时为空串）。 */
  scanError: "",
};

export const systemStore = createStore(initial);

/** 拉取信息。两个接口并发，任一失败不阻塞另一个。 */
export async function refresh(): Promise<void> {
  const s = systemStore.state;
  s.loading = true;
  s.error = "";
  const [info, stats] = await Promise.allSettled([
    api.get<SystemInfo>("/api/system/info", { retry: 1, timeoutMs: 15_000 }),
    api.get<SystemStats>("/api/system/stats", { retry: 1, timeoutMs: 15_000 }),
  ]);

  if (info.status === "fulfilled") s.info = info.value;
  if (stats.status === "fulfilled") s.stats = stats.value;

  const firstErr = [info, stats].find(
    (r): r is PromiseRejectedResult => r.status === "rejected",
  );
  if (firstErr) {
    const e = firstErr.reason;
    s.error = e instanceof ApiError ? e.friendly : "获取设备信息失败";
  } else {
    s.lastLoadedAt = Date.now();
    // 服务端说正在扫描 → 同步本地标志
    if (s.info?.index.state === "scanning") s.scanning = true;
    else if (s.info) s.scanning = false;
  }
  s.loading = false;
}

/**
 * 触发重新扫描。
 *
 * 返回 **202**（不是等扫描结束）：真实库首轮约 20 秒，等下去会超时，
 * 用户应该在设置页看到进度而不是转圈（browse.py: rescan 的注释）。
 */
export async function rescan(root?: string): Promise<RescanResponse> {
  const s = systemStore.state;
  const now = Date.now();
  if (now - s.lastRescanAt < 3000) {
    throw new ApiError({ status: 0, code: "E1001", message: "刚刚已经触发过了" });
  }
  const r = await api.post<RescanResponse>(
    "/api/index/rescan",
    root ? { root } : {},
    { retry: 0, timeoutMs: 15_000 },
  );
  s.lastRescanAt = now;
  s.scanning = true;
  // 立即拉一次让 index.state 尽快变成 scanning
  window.setTimeout(() => void refresh(), 800);
  return r;
}

/** 索引状态文案。 */
export const indexText = computed<string>(() => {
  const ix = systemStore.state.info?.index;
  if (!ix) return "—";
  if (ix.state === "scanning") {
    const n = ix.scanned ? `已扫描 ${ix.scanned} 个文件` : "正在扫描…";
    return n;
  }
  if (ix.state === "never") return "还没扫描过";
  const at = ix.last_scan_at
    ? new Date(ix.last_scan_at * 1000).toLocaleString()
    : "";
  const dur = ix.duration_sec ? `，耗时 ${ix.duration_sec.toFixed(1)} 秒` : "";
  return at ? `上次扫描：${at}${dur}` : "空闲";
});

/* ------------------------------------------------------------------ *
 * SSE 事件接入（AC-2.15 / AC-2.16）
 * ------------------------------------------------------------------ */

/**
 * 扫描进度文案。
 *
 * ★ 与 `indexText` 的区别：`indexText` 读的是 `GET /api/system/info`
 * 里的**快照**（`scanned`），只有轮询才更新；这里是 SSE 推来的**实时**
 * 值。AC-2.16 要求"有进度推送"，所以设置页在扫描中要显示这个而不是快照。
 *
 * 百分比用 `total`（库里的照片总数）算。★ 首轮扫描时 `total` 是 0
 * （库里还什么都没有），此时**不能**显示 "0%" 或除零的 NaN ——
 * 那会让用户以为卡住了。改为只显示已扫描数量。
 */
export const progressText = computed<string>(() => {
  const p = systemStore.state.progress;
  if (!p) return "";
  const parts = [`已扫描 ${p.seen}`];
  if (p.added) parts.push(`新增 ${p.added}`);
  if (p.updated) parts.push(`更新 ${p.updated}`);
  if (p.removed) parts.push(`移除 ${p.removed}`);
  if (p.errors) parts.push(`错误 ${p.errors}`);
  if (p.total && p.total > 0) {
    return `${parts.join("，")}（${p.percent}%）`;
  }
  return parts.join("，");
});

/** 是否正在扫描（SSE 优先，快照兜底）。 */
export const isScanning = computed<boolean>(() => {
  const s = systemStore.state;
  if (s.progress) return true;
  return s.scanning || s.info?.index.state === "scanning";
});

let started = false;
let offEvent: (() => void) | null = null;
let offConn: (() => void) | null = null;

/**
 * 接入 SSE 事件流。**幂等**，可以重复调用。
 *
 * 由 `main.ts` 在启动时调一次，并在配对成功后重连
 * （cookie 是按 host:port 隔离的，换电脑必须重连）。
 */
export function startEvents(): void {
  if (started) return;
  started = true;
  const s = systemStore.state;

  offConn = onConnection((live) => {
    s.live = live;
  });

  offEvent = onEvent((e) => {
    switch (e.type) {
      case "scan_start":
        s.scanError = "";
        s.progress = {
          root: String(e.root ?? ""),
          seen: 0,
          added: 0,
          updated: 0,
          unchanged: 0,
          removed: 0,
          errors: 0,
          percent: 0,
          startedAt: Date.now(),
        };
        s.scanning = true;
        break;

      case "scan_progress": {
        const prev = s.progress;
        const seen = Number(e.seen ?? 0);
        // 扫描期间照片总数会涨（正在往里加），所以百分比的分母取
        // "当前已知总数"与"已扫描数"的较大者 —— 否则会出现 103%
        // 这种明显错误的读数。
        const total = Math.max(Number(e.total ?? 0), seen);
        s.progress = {
          root: String(e.root ?? prev?.root ?? ""),
          seen,
          added: Number(e.added ?? 0),
          updated: Number(e.updated ?? 0),
          unchanged: Number(e.unchanged ?? 0),
          removed: Number(e.removed ?? 0),
          errors: Number(e.errors ?? 0),
          total,
          percent: total > 0 ? Math.min(100, Math.round((seen / total) * 100)) : 0,
          startedAt: prev?.startedAt ?? Date.now(),
        };
        s.scanning = true;
        break;
      }

      case "scan_done":
        s.progress = null;
        s.scanning = false;
        s.lastScan = (e.stats as Record<string, number | string | null>) ?? null;
        // ★ 扫描**失败**必须说出来。
        //
        // `stats.aborted` 装的就是"根目录不可访问：library"这类原因
        // （移动硬盘没插、盘符变了 —— 最常见的一类故障）。不显示的话
        // 用户只看到进度条消失、照片数没变，完全无从判断发生了什么，
        // 只会以为软件坏了。成功时置空，免得上一轮的错一直挂在界面上。
        s.scanError = s.lastScan?.aborted ? String(s.lastScan.aborted) : "";
        // 扫描结束库里的照片数变了，拉一次快照让设置页的数字对上
        void refresh();
        break;

      case "media_changed":
        // AC-2.15：不刷新页面、不需要用户下拉，时间线自己长出新照片。
        s.mediaChangedAt = Date.now();
        break;

      case "upload_done":
        // 同上：手机上刚传完的照片也要立刻出现在时间线里
        s.mediaChangedAt = Date.now();
        break;

      case "index_changed":
        // 库结构变了（增删根目录等），快照必须重取
        void refresh();
        break;

      default:
        break;
    }
  });
}

/** 断开（退出配对时用）。 */
export function stopEvents(): void {
  offEvent?.();
  offConn?.();
  offEvent = null;
  offConn = null;
  started = false;
  systemStore.state.live = false;
  systemStore.state.progress = null;
}

/** 照片总数（优先用 stats 的实时值）。 */
export const photoCount = computed<number>(() => {
  const st = systemStore.state.stats;
  if (st) return st.media.total;
  return systemStore.state.info?.index.total ?? 0;
});

/** 缩略图待生成数。 */
export const pendingThumbs = computed<number>(() => {
  const st = systemStore.state.stats;
  if (st) return st.thumbs.pending;
  return systemStore.state.info?.index.pending_thumbs ?? 0;
});

/** 磁盘剩余（取所有根的最小值，那才是真正的瓶颈）。 */
export const diskFree = computed<number | null>(() => {
  const roots = systemStore.state.info?.roots ?? [];
  const vals = roots
    .map((r) => r.disk_free)
    .filter((v): v is number => typeof v === "number");
  return vals.length ? Math.min(...vals) : null;
});

export const diskTotal = computed<number | null>(() => {
  const roots = systemStore.state.info?.roots ?? [];
  const vals = roots
    .map((r) => r.disk_total)
    .filter((v): v is number => typeof v === "number");
  return vals.length ? Math.max(...vals) : null;
});

export function fmtBytes(n: number | null): string {
  if (n === null || !Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

/** 秒 → "3 天 2 小时"。 */
export function fmtUptime(sec: number): string {
  if (!Number.isFinite(sec) || sec < 0) return "—";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d} 天 ${h} 小时`;
  if (h > 0) return `${h} 小时 ${m} 分`;
  return `${m} 分`;
}
