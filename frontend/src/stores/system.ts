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
