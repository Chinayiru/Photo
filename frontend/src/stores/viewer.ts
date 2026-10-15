/**
 * 查看器 store：全屏看大图 + 左右滑动切换。
 *
 * ★ 切换**不重新查时间线**（任务硬约束）。详情接口
 *   `GET /api/items/{id}` 会带回 `prev_id` / `next_id`
 *   （`backend/app/handlers/browse.py:177-190`）。
 *
 * ⚠️ 语义坑：`prev_id` 是**更新**的那张、`next_id` 是**更旧**的那张。
 *   时间线是倒序（新 → 旧），所以在时间线里"往下翻"= 更旧 = `next_id`。
 *   手势上：左滑 = 看下一张（更旧）= `next_id`；右滑 = 上一张（更新）= `prev_id`。
 */

import { computed } from "vue";
import { ApiError, api, previewUrl, rawUrl, thumbUrl } from "../api/client";
import type { DeleteResponse, MediaDetail } from "../api/types";
import { createStore } from "./index";
import { removeItem as removeFromTimeline } from "./timeline";

const initial = {
  open: false,
  /** 当前项 id（可能只有 id，详情还没回来）。 */
  id: 0 as number,
  detail: null as MediaDetail | null,
  loading: false,
  error: "" as string,
  /** 滑动方向，用于切换动画：`next` = 往更旧（左滑），`prev` = 往更新（右滑）。 */
  dir: "next" as "next" | "prev" | "none",
  /** 大图是否已加载完（显示 loading 遮罩）。 */
  imageReady: false,
  /** 详情请求序号：快速连滑时丢弃过期响应。 */
  seq: 0,
};

export const viewerStore = createStore(initial);

/** 打开查看器。传 id 即可，详情异步拉。 */
export function open(id: number): void {
  const s = viewerStore.state;
  s.open = true;
  s.id = id;
  s.detail = null;
  s.error = "";
  s.imageReady = false;
  s.dir = "none";
  void fetchDetail(id);
}

export function close(): void {
  const s = viewerStore.state;
  s.open = false;
  s.detail = null;
  s.error = "";
  // 立刻释放大图引用，让浏览器回收解码后的位图（手机内存敏感）
  s.id = 0;
}

/** 拉详情。用序号丢弃过期响应，避免快速连滑时"跳回上一张"。 */
export async function fetchDetail(id: number): Promise<void> {
  const s = viewerStore.state;
  const my = ++s.seq;
  s.loading = true;
  s.error = "";
  s.imageReady = false;
  try {
    const d = await api.get<MediaDetail>(`/api/items/${id}`, {
      retry: 1,
      timeoutMs: 15_000,
    });
    if (my !== s.seq) return; // 用户已经滑走了
    s.detail = d;
    s.id = d.id;
  } catch (e) {
    if (my !== s.seq) return;
    const err = e instanceof ApiError ? e : null;
    s.error = err?.friendly ?? "加载失败";
  } finally {
    if (my === s.seq) s.loading = false;
  }
}

/**
 * 切到更旧的一张（左滑）。
 * 没有 `next_id` 时返回 false —— 调用方据此做"到头了"的边界反馈。
 */
export function goNext(): boolean {
  const s = viewerStore.state;
  const nid = s.detail?.next_id;
  if (!nid) return false;
  s.dir = "next";
  void fetchDetail(nid);
  return true;
}

/** 切到更新的一张（右滑）。 */
export function goPrev(): boolean {
  const s = viewerStore.state;
  const pid = s.detail?.prev_id;
  if (!pid) return false;
  s.dir = "prev";
  void fetchDetail(pid);
  return true;
}

/** 大图 URL（带 v 防止看旧图）。 */
export const currentPreviewUrl = computed<string>(() => {
  const s = viewerStore.state;
  if (!s.detail) return "";
  // 详情里的 thumb_v 是 320 档的签名；preview 的 ETag 是另一套
  // （p{size}-{int(mtime)}-{int(size)}），所以用 mtime+size 自己拼版本串，
  // 效果等价：源文件一变，v 就变。
  const v = `p${s.detail.mtime}-${s.detail.size}`;
  return previewUrl(s.detail.id, v, 1600);
});

/** 加载大图时的低清占位（320 缩略图先顶上，避免白屏）。 */
export const currentThumbUrl = computed<string>(() => {
  const s = viewerStore.state;
  if (!s.detail) return "";
  return thumbUrl(s.detail.id, s.detail.thumb_v, 320);
});

/** 拍摄时间（本地时区，秒级）。 */
export const takenText = computed<string>(() => {
  const d = viewerStore.state.detail;
  if (!d) return "";
  const dt = new Date(d.taken_at * 1000);
  if (Number.isNaN(dt.getTime())) return "时间未知";
  const p = (n: number): string => `${n}`.padStart(2, "0");
  const src = d.taken_src === "exif" ? "" : "（按文件时间推测）";
  return `${dt.getFullYear()}年${dt.getMonth() + 1}月${dt.getDate()}日 `
    + `${p(dt.getHours())}:${p(dt.getMinutes())}:${p(dt.getSeconds())}${src}`;
});

/** 相机 + 镜头。 */
export const cameraText = computed<string>(() => {
  const ex = viewerStore.state.detail?.exif;
  if (!ex) return "";
  const parts = [ex.camera, ex.lens].filter((x): x is string => Boolean(x));
  return parts.join(" · ");
});

/** 尺寸 + 文件大小。 */
export const sizeText = computed<string>(() => {
  const d = viewerStore.state.detail;
  if (!d) return "";
  const dim = d.width && d.height ? `${d.width} × ${d.height}` : "尺寸未知";
  return `${dim} · ${formatBytes(d.size)}`;
});

/** EXIF 参数行：ISO / 光圈 / 快门 / 焦距。 */
export const exifText = computed<string>(() => {
  const ex = viewerStore.state.detail?.exif;
  if (!ex) return "";
  const parts: string[] = [];
  if (ex.focal) parts.push(`${ex.focal}mm`);
  if (ex.fnum) parts.push(`f/${ex.fnum}`);
  if (ex.exposure) parts.push(`${ex.exposure}s`);
  if (ex.iso) parts.push(`ISO ${ex.iso}`);
  return parts.join(" · ");
});

/** 下载原图：直接触发浏览器下载（`?download=1` 让后端发 Content-Disposition）。 */
export function downloadRaw(): void {
  const d = viewerStore.state.detail;
  if (!d) return;
  const a = document.createElement("a");
  a.href = rawUrl(d.id, true);
  a.download = d.name;
  // 局域网直连同源，不需要 target=_blank；但某些安卓浏览器要求真实点击，
  // 这里用 DOM 插入 + click 保证事件是"用户触发"的
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

/**
 * 删除（移到电脑的系统回收站）。
 *
 * ⚠️ 后端默认**关闭**删除（`auth.allow_delete` 默认 False），
 *   会返回 403 `E1015`「此操作已被电脑端设置禁用」。这不是 bug，
 *   调用方要把这个提示原样给用户，而不是显示"删除失败"。
 *
 * 成功后自动切到下一张；没有下一张就关掉查看器。
 */
export async function deleteCurrent(): Promise<DeleteResponse> {
  const s = viewerStore.state;
  const d = s.detail;
  if (!d) throw new ApiError({ status: 0, code: "NO_ITEM", message: "没有可删除的照片" });

  const r = await api.post<DeleteResponse>("/api/items/delete", { ids: [d.id] }, {
    retry: 0,
    timeoutMs: 30_000,
  });
  if (r.deleted > 0) {
    removeFromTimeline(d.id);
    // 删掉后停在原地看下一张（更旧的那张）；没有就往前一张
    if (d.next_id) void fetchDetail(d.next_id);
    else if (d.prev_id) void fetchDetail(d.prev_id);
    else close();
  }
  return r;
}

/** 字节数 → 人类可读。 */
export function formatBytes(n: number): string {
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
