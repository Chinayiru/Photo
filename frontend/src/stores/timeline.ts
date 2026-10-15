/**
 * 时间线 store：游标分页 + 日期分组。
 *
 * 分页语义（`backend/app/handlers/browse.py: timeline()`）：
 *   - `limit` 默认 100，最大 500
 *   - 响应 `{items, next_cursor, has_more, server_time, tz_offset_minutes}`
 *   - `has_more === false` 时**必须停止**请求，否则会无限循环
 *   - `order` 只支持 `desc`（传 asc 是 400），所以数组天然"新 → 旧"
 *
 * ★ 为什么按本地日期分组而不是按 UTC：用户在中国看 5 月 12 日 23:50 拍的
 *   照片，按 UTC 会落到 5 月 12 日 15:50 —— 同一天；但 5 月 13 日 07:00 拍的
 *   按 UTC 会落到 5 月 12 日，用户会觉得"照片跑到了昨天"。
 *   用 `new Date(taken_at * 1000)` 的本地年月日分组即可。
 */

import { computed } from "vue";
import { ApiError, api } from "../api/client";
import type { MediaItem, TimelineResponse } from "../api/types";
import { createStore } from "./index";

/** 内存里最多保留的条目数（docs/03 §5.3.2）。超过就丢最旧的。 */
const MAX_ITEMS = 1500;
/** 每页条数。100 与 docs/03 §5.3 一致；再大首屏首字节变慢。 */
export const PAGE_SIZE = 100;

export interface DayGroup {
  /** 分组键，形如 `2024-05-12`。 */
  key: string;
  /** 显示标题，形如 `2024年5月12日`。 */
  label: string;
  items: MediaItem[];
}

const initial = {
  items: [] as MediaItem[],
  nextCursor: null as string | null,
  hasMore: true,
  /** 首屏是否已加载过（决定显示骨架还是空状态）。 */
  loaded: false,
  loading: false,
  error: "" as string,
  /** 总照片数（来自 /api/system/stats），空状态时用来提示"库里没照片"。 */
  totalInLibrary: -1,
};

export const timelineStore = createStore(initial);

/** 防止并发触发同一页（滚动事件很密集）。 */
let inflight: Promise<void> | null = null;

/**
 * 加载下一页。`reset` 为 true 时从头开始（下拉刷新 / 切 Tab 回来）。
 *
 * 幂等：并发调用会共享同一个 in-flight Promise，不会重复请求同一游标。
 */
export function loadMore(reset = false): Promise<void> {
  if (inflight) return inflight;

  const s = timelineStore.state;
  if (!reset && (!s.hasMore || s.loading)) return Promise.resolve();

  if (reset) {
    s.items = [];
    s.nextCursor = null;
    s.hasMore = true;
    s.error = "";
  }

  s.loading = true;
  const cursor = s.nextCursor;

  inflight = (async () => {
    try {
      const r = await api.get<TimelineResponse>("/api/timeline", {
        query: { limit: PAGE_SIZE, cursor: cursor ?? undefined },
        // 时间线是主路径：允许 1 次重试，避免地铁里一次抖动就白屏
        retry: 1,
        timeoutMs: 20_000,
      });
      s.items = s.items.concat(r.items ?? []);
      // ★ 以服务端返回为准；某些实现会返回空 next_cursor 但 has_more=true
      s.nextCursor = r.next_cursor ?? null;
      s.hasMore = Boolean(r.has_more) && Boolean(r.next_cursor);
      s.loaded = true;
      s.error = "";
      trim();
    } catch (e) {
      const err = e instanceof ApiError ? e : null;
      s.error = err?.friendly ?? "加载失败";
      // 失败时不要置 hasMore=false：用户滚到底还能再试
      s.loaded = true;
    } finally {
      s.loading = false;
      inflight = null;
    }
  })();

  return inflight;
}

/** 内存上限：丢最旧的（数组尾部，因为是 desc）。 */
function trim(): void {
  const s = timelineStore.state;
  if (s.items.length > MAX_ITEMS) {
    s.items = s.items.slice(0, MAX_ITEMS);
    // 被截断说明用户已经翻得很深了，但还有更多：保持 hasMore 原样
  }
}

/** 删除后从内存里摘掉，避免回列表还看得到。 */
export function removeItem(id: number): void {
  const s = timelineStore.state;
  const i = s.items.findIndex((x) => x.id === id);
  if (i >= 0) s.items.splice(i, 1);
}

/** 上传完成后插到最前面（如果它比现有第一张还新）。 */
export function prependItem(item: MediaItem): void {
  const s = timelineStore.state;
  if (s.items.some((x) => x.id === item.id)) return;
  s.items.unshift(item);
}

/** 重置（退出配对、切换电脑时用）。 */
export function clearTimeline(): void {
  timelineStore.reset();
  inflight = null;
}

/** 按本地日期分组。 */
export const groups = computed<DayGroup[]>(() =>
  groupByDay(timelineStore.state.items));

/**
 * 把一串媒体项按本地日期切成分组。
 * 抽出来给搜索页复用（搜索结果同样按天分组，用户视角一致）。
 */
export function groupByDay(items: MediaItem[]): DayGroup[] {
  const out: DayGroup[] = [];
  let cur: DayGroup | null = null;
  for (const it of items) {
    const key = dayKey(it.taken_at);
    if (!cur || cur.key !== key) {
      cur = { key, label: dayLabel(it.taken_at), items: [] };
      out.push(cur);
    }
    cur.items.push(it);
  }
  return out;
}

/** `2024-05-12`（本地时区）。 */
export function dayKey(ts: number): string {
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "unknown";
  const m = `${d.getMonth() + 1}`.padStart(2, "0");
  const day = `${d.getDate()}`.padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

/**
 * `2024年5月12日`（本地时区）。
 * 今天/昨天用相对说法，扫一眼就知道是新的。
 */
export function dayLabel(ts: number): string {
  const d = new Date(ts * 1000);
  if (Number.isNaN(d.getTime())) return "时间未知";
  const now = new Date();
  const sameDay = (a: Date, b: Date): boolean =>
    a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate();
  if (sameDay(d, now)) return "今天";
  const yest = new Date(now.getTime() - 86_400_000);
  if (sameDay(d, yest)) return "昨天";
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
}

export const isEmpty = computed<boolean>(
  () => timelineStore.state.loaded && timelineStore.state.items.length === 0,
);

/** 空状态文案：区分"库里就没照片"与"筛选/网络问题"。 */
export const emptyHint = computed<string>(() => {
  const s = timelineStore.state;
  if (s.error) return s.error;
  if (s.totalInLibrary === 0) return "电脑上的照片库还是空的";
  return "没有照片";
});
