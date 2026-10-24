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
import { getToken } from "../api/storage";
import type {
  DeleteResponse,
  MediaDetail,
  MoveResponse,
  RenameResponse,
} from "../api/types";
import { createStore } from "./index";
import { patchItem as patchTimeline, removeItem as removeFromTimeline } from "./timeline";

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

/* ------------------------------------------------------------------ *
 * AC-2.10 第三级：空闲时静默预取原图
 * ------------------------------------------------------------------ */

/**
 * 已预取的原图，存成 **Blob URL**。
 *
 * ## 为什么要预取
 *
 * 前两级（320 模糊占位 → 1600 预览）在**不放大**时已经完全够看，
 * 用户甚至看不出差别。原图只在双指放大到 1:1 时才有意义 ——
 * 而那一刻恰恰是最不能等的：手指已经捏在屏幕上，等 4 MB 下完再变清晰，
 * 中间是几百毫秒到几秒的糊图。预取让这一刻只花**本地解码**的时间。
 *
 * ## 为什么存 Blob URL 而不是 `createImageBitmap` 的位图
 *
 * 本想让"放大即清晰"完全无等待，但一张 12MP 的 JPEG 解码后是
 * `4000×3000×4 ≈ 48 MB`，按 §5.4.3 的"最多留 3 张"就是 **144 MB** 常驻 ——
 * 安卓 Chrome 渲染进程上限约 300–500 MB，低端机更低，这是拿 OOM 换
 * 一个 200ms 的体感收益，不划算。
 *
 * Blob URL 只持有**压缩后的字节**（一张 4 MB，三张 12 MB），只有在真正
 * 显示时才解码。而且 `<img src="blob:...">` 与现有的预览图用的是同一个
 * `src` 属性 —— 不需要为"原图"另造一套 canvas 渲染路径。
 * 代价是首次放大仍有约 100–300ms 的本地解码，但没有网络等待。
 *
 * ## 三条硬约束（`docs/03-详细设计说明书.md:3660-3678` §5.4.3）
 *
 * ① 同时挂载的图片**最多 3 张**（当前 ±1）→ `RAW_KEEP = 3`；
 * ② 原图预取并发**最多 1 个**，切换时用 `AbortController` 取消上一个；
 * ③ 原图**仅当用户放大到 > 1.5× 才用**（见 `ViewerView.vue` 的 `useRaw`），
 *    预取只是"提前把字节拿到手"，不改变画质决策。
 */
const RAW_KEEP = 3;
/**
 * 超过这个大小就不预取。
 *
 * 一期支持的上传上限是 2 GB（`docs/01` AC-3.10），但**浏览**不该为了
 * 一张 800 MB 的 TIFF 在后台默默吃掉用户的流量与内存 —— 预取的收益是
 * "放大快 200ms"，而代价是"毫无提示地下了 800 MB"。24 MB 覆盖了
 * 手机与相机的绝大多数 JPEG（iPhone 48MP ProRAW 转出的 JPEG 约 5–10 MB）；
 * 更大的图仍然能看、能放大，只是走一次网络而已。
 */
const RAW_PREFETCH_MAX_BYTES = 8 * 1024 * 1024;
const rawBlobs = new Map<number, string>();
/** 正在预取的那一张的 id（0 = 空闲）。同时只允许一个。 */
let rawFetching = 0;
let rawController: AbortController | null = null;
/** 预取任务的序号：fetch 回来时若已不是最新任务就丢弃。 */
let rawSeq = 0;

/**
 * 最近一次「主图（1600px 预览）已经画出来」的那张 id。
 *
 * ★ 预取原图必须等它 —— 这是 `docs/08` 附录 B.12 记的教训：
 *   打开大图时如果立刻在后台拉原图（8192×6144 的 JPEG 有 22 MB），
 *   它会和**用户正等着看的预览**抢同一条上行；家宽/中继链路上
 *   预览从 1 秒变成 10 秒，用户看到的就是「点进去加载大图太慢」。
 *   先让预览落地，再预取原图，用户的等待只由预览的 400 KB 决定。
 */
let previewLoadedId = 0;

/** 原图预取是否值得做：省流量模式下、或明确是慢网时直接放弃。 */
function prefetchWorthIt(): boolean {
  const c = (navigator as unknown as {
    connection?: { saveData?: boolean; effectiveType?: string };
  }).connection;
  if (!c) return true;                      // Safari/Firefox 没有这个 API
  if (c.saveData === true) return false;    // 用户开了省流量
  const t = c.effectiveType ?? "";
  return !(t === "slow-2g" || t === "2g" || t === "3g");
}

/** 预取入口：切图后调用，内部自行空闲调度。 */
export function schedulePrefetchRaw(id: number): void {
  if (!id) return;
  // ★ 两条前置判据（顺序即语义）：预览还没画出来就不抢带宽；
  //   `markPreviewLoaded()` 会在预览 onload 后回头再调一次。
  if (previewLoadedId !== id) return;
  if (!prefetchWorthIt()) return;
  if (rawBlobs.has(id) || rawFetching === id) return;
  if (typeof URL.createObjectURL !== "function") return;  // 极老的浏览器
  idle(() => void prefetchRaw(id));
}

/**
 * 预览图画完之后由视图层调用：登记"这张的主图已经在屏幕上了"，
 * 并回头触发一次被上面判据挡住的预取。
 */
export function markPreviewLoaded(id: number): void {
  if (!id) return;
  previewLoadedId = id;
  schedulePrefetchRaw(id);
}

/** 空闲调度：优先 requestIdleCallback，退回 300ms 定时器。 */
function idle(fn: () => void): void {
  const ric = (window as unknown as {
    requestIdleCallback?: (cb: () => void, opt?: { timeout: number }) => number;
  }).requestIdleCallback;
  if (typeof ric === "function") {
    ric(fn, { timeout: 1500 });
    return;
  }
  window.setTimeout(fn, 300);
}

async function prefetchRaw(id: number): Promise<void> {
  // 同时只跑一个：先取消上一个（约束 ②）。
  rawController?.abort();
  const ctrl = new AbortController();
  rawController = ctrl;
  const my = ++rawSeq;
  rawFetching = id;

  const token = getToken();
  try {
    const res = await fetch(rawUrl(id), {
      method: "GET",
      // ★ 会话走 cookie，与 `<img>` 完全同一条路（见 api/client.ts 的注释：
      //   `fetch` 加 credentials 是为了自愈 cookie，服务端头优先、cookie 兜底）。
      credentials: "same-origin",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      signal: ctrl.signal,
    });
    if (!res.ok) return;
    // ★ 先看声明的长度再决定要不要把字节收下来：8 MB 以上直接放弃。
    //   没有这一步时，一张 22 MB 的原图会被完整下载、再在 `blob.size`
    //   处被丢弃 —— 流量已经花掉了。
    const declared = Number(res.headers.get("content-length") ?? "0");
    if (declared > RAW_PREFETCH_MAX_BYTES) return;
    // ★ 检查 Content-Type：某些错误页（如反代返回的 HTML 502）也是 200，
    //   把它当原图存下来会让放大后显示一坨乱码 —— 那种失败比"没预取"更糟。
    const ct = res.headers.get("content-type") ?? "";
    if (ct && !ct.startsWith("image/")) return;
    const blob = await res.blob();
    if (my !== rawSeq) return;       // 期间用户又滑了，这次结果作废
    if (blob.size === 0) return;
    rawBlobs.set(id, URL.createObjectURL(blob));
    evictRaw();
  } catch {
    // ★ 静默失败：预取是"锦上添花"，任何错误（断网、取消、解码失败）
    //   都不该冒泡到界面上 —— 用户没做任何操作，弹一个错误框会让他
    //   以为照片坏了。真正要原图时（放大到 1.5×）会再走一次网络。
  } finally {
    if (my === rawSeq) {
      rawFetching = 0;
      rawController = null;
    }
  }
}

/** 淘汰超出 RAW_KEEP 的，保留最近插入的。 */
function evictRaw(): void {
  while (rawBlobs.size > RAW_KEEP) {
    // Map 保持插入顺序，第一个就是最老的
    const oldest = rawBlobs.keys().next();
    if (oldest.done) break;
    releaseRaw(oldest.value);
  }
}

/** 主动释放某一个的原图（切图、关闭查看器时调用）。 */
export function releaseRaw(id: number): void {
  const url = rawBlobs.get(id);
  if (!url) return;
  // ★ 必须 revoke：Blob URL 的字节会一直被 hold 住直到 revoke 或页面卸载，
  //   只从 Map 里删掉是**内存泄漏**（而且泄漏的是几 MB 的整张原图）。
  URL.revokeObjectURL(url);
  rawBlobs.delete(id);
}

/** 已预取到的原图 Blob URL（没有则空串）。 */
export function cachedRaw(id: number): string {
  return rawBlobs.get(id) ?? "";
}

/**
 * 放大后应该用哪张图的 URL。
 *
 * ★ 这是 §5.4.3 约束 ③「原图仅当用户放大到 > 1.5× 才用」的唯一落点。
 *   在放大之前用原图是纯亏：4 MB 的原图与 1600px 预览在屏幕上
 *   **一模一样**（手机屏幕只有 400px 宽），却要多下 20 倍字节、
 *   多解码 48 MB。
 *
 * 预取到了本地 Blob 就返回它（零网络等待），否则退回原图 URL
 * （走一次 Range 请求，服务端支持断点）。
 */
export function rawSrcFor(id: number): string {
  return cachedRaw(id) || rawUrl(id);
}

/** 是否已经有这张的原图（给界面决定要不要提示"放大即清晰"）。 */
export function hasRaw(id: number): boolean {
  return rawBlobs.has(id);
}

/** 当前这张的详情变化后，重新安排一次预取（`open` / 连滑都会调）。 */
function prefetchCurrent(): void {
  const s = viewerStore.state;
  if (s.open && s.id) schedulePrefetchRaw(s.id);
}

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
  prefetchCurrent();
}

export function close(): void {
  const s = viewerStore.state;
  s.open = false;
  s.detail = null;
  s.error = "";
  // 立刻释放大图引用，让浏览器回收解码后的位图（手机内存敏感）
  s.id = 0;
  // ★ 预取的原图也必须一起放掉。不取消的话用户在查看器里翻了 20 张
  //   再关掉，后台还在继续下原图，而他已经不看了 —— 白耗流量与电量。
  rawController?.abort();
  rawController = null;
  rawFetching = 0;
  rawSeq++;
  for (const id of [...rawBlobs.keys()]) releaseRaw(id);
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
    // ★ 详情落地后再预取（而不是在 `open`/`goNext` 里）：
    //   这里是**所有**切换路径的唯一汇合点（open / goNext / goPrev /
    //   从时间线点进来），在别处挂就会漏掉某一条；而且拿到 detail 才
    //   知道这张到底多大 —— 几十 MB 的图不该无脑预取。
    if (d.size <= RAW_PREFETCH_MAX_BYTES) prefetchCurrent();
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

/**
 * GPS 坐标文本。没有坐标时返回空串。
 *
 * ⚠️ 返回空串而不是 `"--"` 之类占位符：调用方用 `v-if` 决定整行是否渲染，
 * 显示"GPS: --"会让绝大多数（没开定位的）照片都多一行废话。
 * 模板里的 `v-if` 依赖"空串即假值"这个约定。
 *
 * 小数位取 6 位：约 0.11 米精度，足够定位到具体某栋楼；
 * 再多的位数只是 EXIF 原始浮点的噪声。
 */
export const gpsText = computed<string>(() => {
  const g = viewerStore.state.detail?.gps;
  if (!g) return "";
  // 0 是合法坐标（赤道/本初子午线），不能用 falsy 判断
  if (typeof g.lat !== "number" || typeof g.lon !== "number") return "";
  return `${g.lat.toFixed(6)}, ${g.lon.toFixed(6)}`;
});

/**
 * 地图链接（OpenStreetMap）。
 *
 * 选 OSM 而不是高德/百度：后者需要 API key，而这是个局域网自托管工具，
 * 不该要求用户去申请第三方密钥；且 OSM 的链接是纯 URL 参数，
 * 在没有 JS 的旧浏览器上也能打开。
 *
 * ⚠️ 用 `#map=16/lat/lon` 而不是 `?mlat=..&mlon=..` 单独使用：
 * 前者同时指定缩放级别，否则 OSM 会用一个很远的视角打开，
 * 用户看到的是整个洲而不是他家楼下。
 */
export const gpsMapUrl = computed<string>(() => {
  const g = viewerStore.state.detail?.gps;
  if (!g) return "";
  if (typeof g.lat !== "number" || typeof g.lon !== "number") return "";
  const lat = g.lat.toFixed(6);
  const lon = g.lon.toFixed(6);
  return `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=16/${lat}/${lon}`;
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

/**
 * 改当前这张照片的名字。
 *
 * 后端语义（`backend/app/handlers/browse.py: rename_item()`）：
 *   - `new_name` **必须含扩展名**，且扩展名要还在图片白名单里
 *     （否则能把 `.jpg` 改成 `.exe`，手机点一下就变成下载可执行文件）
 *   - 只允许改名字，**不允许带路径**（`a/b.jpg` 会被拒；换目录走 move）
 *   - 同名时后端幂等成功，不写审计
 *   - ★ `taken_at` 可能变：原来时间来自文件名日期时，新名字里没日期
 *     了就得按 EXIF → 文件名 → mtime 重算，所以要用返回值回写
 *
 * 失败一律抛 ApiError，由调用方决定文案 —— 这里不吞异常，因为
 * "改成什么名字"这件事用户必须知道结果。
 */
export async function renameCurrent(newName: string): Promise<RenameResponse> {
  const d = viewerStore.state.detail;
  if (!d) throw new ApiError({ status: 0, code: "NO_ITEM", message: "没有可改名的照片" });

  const r = await api.post<RenameResponse>("/api/items/rename", {
    id: d.id,
    new_name: newName,
  }, { retry: 0, timeoutMs: 20_000 });

  // 后端返回的是 rel_path（含目录），列表里显示的是文件名
  const slash = r.to.lastIndexOf("/");
  const fileName = slash >= 0 ? r.to.slice(slash + 1) : r.to;
  const dir = slash >= 0 ? r.to.slice(0, slash) : "";

  // 详情与时间线各更新一份：详情是当前看到的，时间线是返回列表后看到的，
  // 漏掉任一个都会出现"改完名字返回列表还是旧名字"
  viewerStore.state.detail = {
    ...d,
    name: fileName,
    rel_path: r.to,
    taken_at: r.taken_at,
    taken_src: r.taken_src,
  };
  patchTimeline(d.id, {
    name: fileName,
    dir,
    taken_at: r.taken_at,
    taken_src: r.taken_src,
  });
  return r;
}

/**
 * 把当前这张照片移到另一个目录。
 *
 * 后端语义（`backend/app/handlers/browse.py: move_items()`）：
 *   - `target_dir` 是相对库根的路径，空串 = 库根；不存在会自动创建
 *   - 目标目录已有同名文件时默认 `rename`（追加 `_1`），**绝不覆盖**；
 *     所以响应里要读 `moved[0].renamed` 才知道名字变了没有
 *   - 目标就是当前目录时记 `skipped` 而不是报错
 *
 * 返回 null 表示"没动"（目标就是当前目录），调用方据此给不同提示。
 */
export async function moveCurrent(targetDir: string): Promise<MoveResponse["moved"][number] | null> {
  const d = viewerStore.state.detail;
  if (!d) throw new ApiError({ status: 0, code: "NO_ITEM", message: "没有可移动的照片" });

  const r = await api.post<MoveResponse>("/api/items/move", {
    ids: [d.id],
    target_dir: targetDir,
  }, { retry: 0, timeoutMs: 60_000 });

  const item = r.moved[0];
  if (!item) {
    // ★ 三种情况都会没有 `moved[0]`：有意跳过、**真的失败**、
    //   以及响应本身空。早先一律 `return null`，于是失败被
    //   `ViewerView.vue:311` 当成"照片已经在这个目录里了"提示 ——
    //   用户看到一句与事实相反的话，而照片其实没动。
    const bad = r.failed?.[0];
    if (bad) {
      throw new ApiError({
        status: 0,
        code: bad.code,
        message: bad.message,
        retryable: false,
      });
    }
    return null;                   // 目标就是当前目录 → skipped
  }

  const slash = item.to.lastIndexOf("/");
  const fileName = slash >= 0 ? item.to.slice(slash + 1) : item.to;
  const dir = slash >= 0 ? item.to.slice(0, slash) : "";

  viewerStore.state.detail = { ...d, name: fileName, rel_path: item.to };
  patchTimeline(d.id, { name: fileName, dir });
  return item;
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
