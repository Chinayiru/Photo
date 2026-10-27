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
import { api } from "../api/client";
import type { FolderEntry, FoldersResponse, MediaItem, TimelineResponse } from "../api/types";
import { createStore } from "./index";
import { errorText } from "../api/errorText";

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

/**
 * 浏览方式（ISSUE-060 A）。
 *
 * - `all`：时间线，全库按拍摄时间倒序（原来的行为，默认）
 * - `folder`：按文件夹，**逐层下钻**，只列当前这一层的条目
 *
 * 两种方式共用同一个条目数组与同一套照片墙组件：切换时清空重取，
 * 这样用户看到的永远是"一个列表"在换内容，而不是两套界面。
 */
export type BrowseMode = "all" | "folder";

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
  /** 当前浏览方式。 */
  mode: "all" as BrowseMode,
  /** 文件夹模式下的当前目录（相对根的路径，`""` = 根层）。 */
  folder: "",
  /** 当前层的子目录（文件夹模式才请求）。 */
  folders: [] as FolderEntry[],
  foldersLoading: false,
  /** 当前层（含子目录）的条目总数，来自 `/api/folders/media` 的 `total`。 */
  folderTotal: -1,
};

export const timelineStore = createStore(initial);

/** 防止并发触发同一页（滚动事件很密集）。 */
let inflight: Promise<void> | null = null;

/**
 * 列表"代号"：换目录 / 换浏览方式时 +1。
 *
 * 为什么必须有它（审查 R-M6-02）：`inflight` 是模块级的，只表示"有一个请求
 * 在路上"，完全不区分请求属于哪个目录。用户点 A 目录、照片还没回来就点 B，
 * B 的 `loadMore` 直接命中 `if (inflight) return inflight` 拿回 A 的 Promise；
 * 就算绕过了共享，A 的响应晚到也会 `s.items = s.items.concat(A的照片)`，
 * 屏幕上就是"B 目录里显示着 A 的照片"——用户会以为文件被搬错了。
 */
let generation = 0;

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
  // 请求发出时就把"我是哪一代、哪个目录"钉住：await 之后 `s` 可能已经变了。
  const gen = generation;
  const folderMode = s.mode === "folder";
  const folderAtStart = s.folder;

  inflight = (async () => {
    try {
      const r = folderMode
        ? await api.get<TimelineResponse>("/api/folders/media", {
            query: { dir: folderAtStart, limit: PAGE_SIZE, cursor: cursor ?? undefined },
            retry: 1,
            timeoutMs: 20_000,
          })
        : await api.get<TimelineResponse>("/api/timeline", {
            query: { limit: PAGE_SIZE, cursor: cursor ?? undefined },
            // 时间线是主路径：允许 1 次重试，避免地铁里一次抖动就白屏
            retry: 1,
            timeoutMs: 20_000,
          });
      if (gen !== generation) return;   // 已经换了目录/模式：这包不属于当前列表
      s.items = s.items.concat(r.items ?? []);
      // ★ 以服务端返回为准；某些实现会返回空 next_cursor 但 has_more=true
      s.nextCursor = r.next_cursor ?? null;
      s.hasMore = Boolean(r.has_more) && Boolean(r.next_cursor);
      if (folderMode) {
        const t = (r as unknown as { total?: number }).total;
        if (typeof t === "number") s.folderTotal = t;
      }
      s.loaded = true;
      s.error = "";
      trim();
    } catch (e) {
      if (gen !== generation) return;
      s.error = errorText(e, "加载失败");
      // 失败时不要置 hasMore=false：用户滚到底还能再试
      s.loaded = true;
    } finally {
      // 只有"当前这一代"才有权清状态：旧请求晚回来时新请求已经占着
      // `inflight`，这时清掉会让下一次 loadMore 再发一个同游标请求。
      if (gen === generation) {
        s.loading = false;
        inflight = null;
      }
    }
  })();

  return inflight;
}

/* ------------------------------------------------------------------ *
 * 浏览方式：全部图片 ⇄ 按文件夹（ISSUE-060 A）
 * ------------------------------------------------------------------ */

/** 切换浏览方式。用户正在看的列表会被换掉，所以必须清空重取。 */
export function setMode(mode: BrowseMode): void {
  const s = timelineStore.state;
  if (s.mode === mode) return;
  s.mode = mode;
  s.folder = "";
  s.folders = [];
  s.folderTotal = -1;
  changeFolder("", { reload: true });
  if (mode === "folder") void refreshFolders();
}

/**
 * 进入某个子目录（`rel` 为相对根的路径，`""` = 回到根层）。
 *
 * 为什么 `loadMore` 里不重新读 `folders`：下钻时那一层的子目录是新请求
 * （`/api/folders?dir=`），和媒体列表是两个接口、两次往返。先发出媒体
 * 那一次（用户最关心的是照片什么时候出来），目录列表随后补上。
 */
export function openFolder(rel: string): void {
  const s = timelineStore.state;
  if (s.mode !== "folder") s.mode = "folder";
  s.folderTotal = -1;
  changeFolder(rel, { reload: true });
  void refreshFolders();
}

/** 只改目录状态并（可选）触发重取，不动 `mode`。 */
function changeFolder(rel: string, opts: { reload: boolean }): void {
  const s = timelineStore.state;
  s.folder = rel;
  if (!opts.reload) return;
  s.items = [];
  s.nextCursor = null;
  s.hasMore = true;
  s.loaded = false;
  s.error = "";
  // 换列表 = 换一代，并放开 `inflight`：否则新的目录会被"已有在途请求"
  // 挡回去，界面上永远停在空白（审查 R-M6-02）。
  generation += 1;
  inflight = null;
  s.loading = false;
  void loadMore(true);
}

/** 取当前层的子目录列表。失败**不**影响照片列表，只是不显示目录。 */
export async function refreshFolders(): Promise<void> {
  const s = timelineStore.state;
  if (s.mode !== "folder") return;
  s.foldersLoading = true;
  try {
    const r = await api.get<FoldersResponse>("/api/folders", {
      query: { dir: s.folder },
      retry: 1,
      timeoutMs: 20_000,
    });
    // 请求回来时用户可能已经又下钻了一层：旧响应不能覆盖新状态。
    if (s.folder !== (r.path ?? "")) return;
    s.folders = r.folders ?? [];
  } catch {
    s.folders = [];
  } finally {
    s.foldersLoading = false;
  }
}

/** 面包屑：`[{label:"全部", path:""}, {label:"青甘环线", path:"青甘环线"}, …]` */
export const crumbs = computed<{ label: string; path: string }[]>(() => {
  const s = timelineStore.state;
  const out = [{ label: "全部", path: "" }];
  if (!s.folder) return out;
  const segs = s.folder.split("/").filter(Boolean);
  let acc = "";
  for (const seg of segs) {
    acc = acc ? `${acc}/${seg}` : seg;
    out.push({ label: seg, path: acc });
  }
  return out;
});

/** 内存上限：丢最旧的（数组尾部，因为是 desc）。 */
function trim(): void {
  const s = timelineStore.state;
  if (s.items.length > MAX_ITEMS) {
    s.items = s.items.slice(0, MAX_ITEMS);
    // ★ 触到上限就**停止翻页**（审查 R-M6-01）。
    //
    // 原来这里保留 `hasMore` 原样，于是：游标跟着新取的一页前进了 100，但
    // 那 100 条当场被 `slice` 丢掉 —— 用户滚到底看到的最后一条永远停在第
    // 1500 条，`lastVisible` 也就永远推不动，而加载还在继续，每滚一次白丢
    // 100 条。要么停，要么给"只增不减"的虚拟列表，这里选停（1500 条已
    // 远超任何人的实际浏览深度）。
    s.hasMore = false;
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

/**
 * 移动 / 改名后就地更新一项。
 *
 * 为什么不能像删除那样 `removeItem()` 了事：移动与改名**没有改变
 * 照片的身份**，用户刚在查看器里把照片移进「精选」，返回列表发现
 * 它凭空消失（其实还在，只是换了目录）会以为操作把照片弄丢了。
 *
 * 两个字段必须一起更新：
 *   - `dir`：列表分组/副标题显示的就是它，不改则显示旧目录
 *   - `taken_at`：改名可能让"文件名日期"这个来源失效从而重算时间，
 *     不改则这张照片留在旧的日期分组里，与时间线排序自相矛盾
 *
 * 用 `s.items[i] = { ...old, ...patch }` 整体替换而不是逐字段赋值：
 * 后者在 Vue 3 的 reactive 数组里虽然也能触发，但整体替换能保证
 * 任何一次改动都只触发一次更新，且不会漏掉未来新增的字段。
 */
export function patchItem(id: number, patch: Partial<MediaItem>): void {
  const s = timelineStore.state;
  const i = s.items.findIndex((x) => x.id === id);
  if (i < 0) return;
  s.items[i] = { ...s.items[i]!, ...patch };
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
  if (s.mode === "folder") {
    // 文件夹模式下"空"有两种：这层确实是空的，或者这层只有子目录
    // （条目数是 0 但目录块还在上面）—— 后者不需要再说一遍。
    return s.folders.length > 0 ? "这个文件夹里没有照片，看看上面的子文件夹" : "这个文件夹里没有照片";
  }
  if (s.totalInLibrary === 0) return "电脑上的照片库还是空的";
  return "没有照片";
});
