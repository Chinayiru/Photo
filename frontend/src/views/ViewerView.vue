<script setup lang="ts">
/**
 * 全屏查看器。
 *
 * 三个关键点：
 *  ① 左右滑动切图**不重新查时间线** —— 详情接口已经带回 `prev_id`/`next_id`
 *     （`backend/app/handlers/browse.py:177-190`）。重新查时间线会丢掉
 *     滚动位置，而且在翻到第 300 张时等于白白拉 100 条数据。
 *  ② 手势只在水平位移明显大于垂直位移时才接管，否则用户想上下拖动
 *     放大后的图片、或者只是想滑一下，都会被误判成切图。
 *  ③ 删除前必须二次确认，且文案说清"进回收站"—— 用户按下去前得知道
 *     照片还在电脑上，只是被移走了。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { confirmDialog, setConfirmBusy } from "../stores/confirm";
import { ApiError, api } from "../api/client";
import type { FolderEntry, FoldersResponse } from "../api/types";
import {
  cameraText,
  close,
  currentPreviewUrl,
  currentThumbUrl,
  deleteCurrent,
  downloadRaw,
  exifText,
  goNext,
  goPrev,
  gpsMapUrl,
  gpsText,
  markPreviewLoaded,
  moveCurrent,
  rawSrcFor,
  renameCurrent,
  sizeText,
  takenText,
  viewerStore,
} from "../stores/viewer";
import { toastError, toastOk, toastWarn } from "../stores/toast";
import { safeUrl } from "../lib/safeUrl";

const s = viewerStore.state;
const isOpen = computed(() => s.open);

/* ------------------------------------------------------------------ *
 * AC-2.10 第三级：放大到 1.5× 以上才切原图
 * ------------------------------------------------------------------ */

/**
 * 放大到多少倍才用原图。
 *
 * ★ 这是 `docs/03-详细设计说明书.md:3660-3678` §5.4.3 约束 ③「原图仅当
 *   用户放大到 > 1.5× 才用」的唯一落点。在放大之前用原图是纯亏：
 *   1600px 预览在手机屏幕上与 4 MB 的原图**完全一样**（屏幕只有 400px 宽），
 *   却要多下二十倍字节、多解码 48 MB —— 而安卓渲染进程上限只有 300–500 MB。
 *
 * 1.5 而不是 1.2：1600px 预览放到 1.5× 是 2400px 有效，已经超过多数
 * 手机屏幕的物理像素；再往下调只会让"用户还没看出模糊就先背上了原图的代价"。
 */
const RAW_ZOOM = 1.5;
const useRaw = computed(() => zoom.value > RAW_ZOOM);

/**
 * 主图当前该用哪张。
 *
 * ★ 这里**不**改主图的 `src`，而是在它上面再叠一层原图。
 *   原因：`<img>` 的 `src` 变了以后浏览器会先把旧图留在屏幕上再慢慢解新图，
 *   用户看到的是"放大之后卡住不动"；换成"换 key 重建节点"又会先闪黑一下
 *   （旧节点已经销毁、新节点还在解码）。两层叠着最稳 —— 预览图一直在，
 *   原图解码完成后盖上去，全程没有白屏与黑闪。
 */
const mainSrc = computed(() => currentPreviewUrl.value);

/** 原图层是否该挂着（用户放大到 1.5× 以上且原图没失败过）。 */
const rawSrc = computed(() => {
  if (!s.detail || isVideo.value || !useRaw.value || rawFailed.value) return "";
  return rawSrcFor(s.detail.id);
});

/**
 * 原图解码失败的标记。
 *
 * ★ 必须单独记、且**不能**写进 `s.error`：库里有一批 HEIC
 *   （`backend/app/config.py` 的 `ALLOWED_IMAGE_EXTS` 含 heic/heif），
 *   而它们经 `/raw` 拿回来的是**原样的 HEIC 字节**，Chrome 解不了。
 *   把这种失败显示成"图片加载失败"，用户会以为照片坏了 ——
 *   实际上预览图一直好好的，只是"放大到 1:1"这条路对 HEIC 不通。
 */
const rawFailed = ref(false);

watch(() => s.id, () => {
  rawFailed.value = false;
});

/** 手势状态。 */
let startX = 0;
let startY = 0;
let startT = 0;
let tracking = false;
let locked: "h" | "v" | null = null;

/**
 * 双指缩放。
 *
 * ⚠️ 用 `ref` 而不是普通变量：`transform` 直接绑在模板上，
 * 普通变量改了不会触发重渲染（`dx` 当初就踩过这个）。
 */
/**
 * 主图（1600px 预览）画完：登记给 viewer store，让它开始预取原图。
 *
 * ★ 预取必须晚于这一步 —— 否则 8–22 MB 的原图会和用户正等着的预览
 *   抢带宽（`docs/08` 附录 B.12）。
 */
function onMainLoad(): void {
  s.imageReady = true;
  if (s.id) markPreviewLoaded(s.id);
}

const zoom = ref(1);
/** 双指起始间距。0 表示"本次手势不是双指"，用来区分单指/双指两条路径。 */
let pinchStart = 0;
/** 双指开始时的缩放倍数。缩放是**相对**的，必须记住起点。 */
let pinchBase = 1;
/**
 * 缩放后单指拖动查看的偏移量。
 *
 * 放大到 2x 之后画面比屏幕大，用户必须能拖着看局部，
 * 否则"放大"这个功能只能看到左上角，等于没用。
 */
const panX = ref(0);
const panY = ref(0);

/**
 * 双击检测。
 *
 * ⚠️ 不能用 `@dblclick`：那个事件在**触屏上不可靠** ——
 * 移动端 Safari/Chrome 对双击是否派发 `dblclick` 不一致，
 * 而且它和"双击缩放页面"的浏览器默认行为会打架。
 * 自己用两次 `touchend` 的时间差判断最稳。
 */
let lastTapAt = 0;
let lastTapX = 0;
let lastTapY = 0;
/** 双击判定窗口。300ms 是移动端的经验值（也是旧版浏览器 click 延迟的来源）。 */
const DOUBLE_TAP_MS = 300;
/** 双击时允许的位移：手指按下去总会抖，不设阈值会把"快速两次点按"误判成双击。 */
const DOUBLE_TAP_SLOP = 30;

/** 缩放上下限。上限 4x 是因为再大就只是看马赛克了。 */
const ZOOM_MIN = 1;
const ZOOM_MAX = 4;
/** 松手后离 1 多近就吸附回去。留 1.05 是因为"想放大一点点"没有意义。 */
const ZOOM_SNAP = 1.05;

/** 拖动中的水平位移（px），用于跟手动画。 */
const dx = ref(0);
/** 下滑关闭的垂直位移（px）。 */
const dy = ref(0);
const dragging = ref(false);
/** 是否正在双指缩放（用于禁用翻页动画）。 */
const pinching = ref(false);

/** 是否处于放大状态。放大后所有翻页/关闭手势都必须让路。 */
const zoomed = computed(() => zoom.value > ZOOM_SNAP);

/** 把缩放/平移合并成一个 transform 字符串。 */
const imageTransform = computed(() => {
  if (pinching.value || zoomed.value) {
    return `translate(${panX.value}px, ${panY.value}px) scale(${zoom.value})`;
  }
  if (dragging.value) {
    // 下滑关闭时两个方向都要跟手（用户很少滑得笔直）
    return `translate(${dx.value}px, ${dy.value}px)`;
  }
  return "";
});
/** 信息面板是否展开（默认收起，不挡图）。 */
const infoOpen = ref(false);

const hasPrev = computed(() => s.detail?.prev_id != null);
const hasNext = computed(() => s.detail?.next_id != null);
const detail = computed(() => s.detail);

/* ------------------------------------------------------------------ *
 * 视频（ISSUE-055）
 *
 * 视频与图片在查看器里的差别只有一处：主画面换成 `<video>`。缩放/原图这套
 * 全部不适用（视频的像素密度固定，放大只是变糊），因此：
 *   - `useRaw` / 原图层对视频直接短路
 *   - 双指捏合、双击放大、下滑关闭都不接管（交给 `<video>` 的原生控件）
 *   - 左右滑动仍然翻页（换条视频/换张图都很自然）
 * ------------------------------------------------------------------ */
const isVideo = computed(() => detail.value?.kind === "video");
/** 浏览器试着播之后失败了（编码不被支持、文件损坏等）。 */
const videoBroken = ref(false);
/**
 * 服务端的播放建议：`ok` / `maybe` / `no`（老后端没有 `hint` 时退回 `playable`）。
 *
 * ★ 不能只用 `playable`：它是"Chrome 桌面确定能播"的结论，而 iPhone 录的
 *   HEVC 在 iOS Safari 里是能播的。只按它判会让主客户端（手机）白退一次。
 */
const videoHint = computed(() => {
  const v = detail.value?.video;
  if (!v) return "no";
  return v.hint ?? (v.playable ? "ok" : "no");
});
/** 值得递给 `<video>` 的条件：容器对 + 有播放地址 + 之前没试崩过。 */
const videoPlayable = computed(() => isVideo.value
  && videoHint.value !== "no" && !!detail.value?.urls.video);
const videoSrc = computed(() => (videoPlayable.value && !videoBroken.value
  ? detail.value?.urls.video ?? ""
  : ""));

/** 换条目时清掉"上一条播崩了"的标记（否则下一条视频会被误判）。 */
watch(() => detail.value?.id, () => { videoBroken.value = false; });

/**
 * 视频元信息到手（`loadedmetadata`）：等价于图片的 `onMainLoad`。
 *
 * ★ 也必须调 `markPreviewLoaded`：否则 viewer store 会一直以为"预览还没好"，
 *   后续那张图的 idle 预取会被无限推迟（预取队列是全局的）。
 */
function onVideoMeta(): void {
  s.imageReady = true;
  if (s.id) markPreviewLoaded(s.id);
}

/* ------------------------------------------------------------------ *
 * 改名 / 移动
 *
 * 两个操作都改的是**电脑上真实的文件**，所以：
 *  ① 都要二次确认（改名会改磁盘上的文件名，不是"显示名"）
 *  ② 都要能取消（弹窗里取消是安全的，什么都没发生）
 *  ③ 结果必须如实告诉用户，尤其是"名字被自动改了"
 *     —— 移动遇到同名时后端会追加 _1，不告诉用户他会以为照片丢了
 * ------------------------------------------------------------------ */

/** 改名弹窗。 */
const renameOpen = ref(false);
const renameValue = ref("");
const renameBusy = ref(false);
const renameError = ref("");
const renameInput = ref<HTMLInputElement | null>(null);

/** 移动面板。 */
const moveOpen = ref(false);
const moveBusy = ref(false);
const moveError = ref("");
/** 当前浏览到的目录（相对库根，空串 = 库根）。 */
const moveBrowsePath = ref("");
const moveFolders = ref<FolderEntry[]>([]);
const moveLoading = ref(false);

function openRename(): void {
  const d = s.detail;
  if (!d) return;
  renameValue.value = d.name;
  renameError.value = "";
  renameOpen.value = true;
  // nextTick 后自动聚焦并选中主文件名（不含扩展名）—— 用户十有八九
  // 只想改名字不想改扩展名，全选会让改完变成没有扩展名的文件
  requestAnimationFrame(() => {
    const el = renameInput.value;
    if (!el) return;
    el.focus();
    const dot = el.value.lastIndexOf(".");
    if (dot > 0) el.setSelectionRange(0, dot);
    else el.select();
  });
}

async function doRename(): Promise<void> {
  const d = s.detail;
  if (!d) return;
  const name = renameValue.value.trim();
  if (!name) {
    renameError.value = "名字不能为空";
    return;
  }
  if (name === d.name) {
    renameOpen.value = false;
    return;
  }

  renameBusy.value = true;
  renameError.value = "";
  try {
    const r = await renameCurrent(name);
    renameOpen.value = false;
    // ★ 时间可能被重算：原时间来自文件名日期时，新名字里没有日期了，
    //   后端会退回 EXIF 或 mtime。这一点必须说，否则用户会发现
    //   "改了个名字照片跑到别的日期去了"
    if (r.taken_src !== d.taken_src) {
      toastOk("已改名（拍摄时间按文件重新推算）");
    } else {
      toastOk("已改名");
    }
  } catch (e) {
    // 弹窗内显示错误而不是关掉弹窗：用户输的名字还在，改一下就能重试
    if (e instanceof ApiError) {
      renameError.value = e.userHint || e.friendly || "改名失败";
    } else {
      renameError.value = e instanceof Error ? e.message : "改名失败";
    }
  } finally {
    renameBusy.value = false;
  }
}

async function loadFolders(path: string): Promise<void> {
  moveLoading.value = true;
  moveError.value = "";
  try {
    const r = await api.get<FoldersResponse>("/api/folders", {
      query: { path: path || undefined, depth: 1 },
      retry: 0,
      timeoutMs: 15_000,
    });
    moveBrowsePath.value = r.path;
    moveFolders.value = r.folders;
  } catch (e) {
    moveError.value = e instanceof ApiError ? (e.friendly || "读取目录失败") : "读取目录失败";
    moveFolders.value = [];
  } finally {
    moveLoading.value = false;
  }
}

async function openMove(): Promise<void> {
  if (!s.detail) return;
  moveError.value = "";
  moveOpen.value = true;
  // 从当前照片所在目录开始浏览，而不是从库根 —— 用户多半是想挪到
  // 旁边那个目录，从根开始点三层很烦
  const cur = s.detail.rel_path;
  const slash = cur.lastIndexOf("/");
  await loadFolders(slash >= 0 ? cur.slice(0, slash) : "");
}

/** 进入子目录。 */
function moveInto(f: FolderEntry): void {
  void loadFolders(f.path);
}

/** 回到上一级。 */
function moveUp(): void {
  const p = moveBrowsePath.value;
  const slash = p.lastIndexOf("/");
  void loadFolders(slash >= 0 ? p.slice(0, slash) : "");
}

async function doMoveHere(): Promise<void> {
  const d = s.detail;
  if (!d) return;
  const target = moveBrowsePath.value;
  const curDir = d.rel_path.includes("/")
    ? d.rel_path.slice(0, d.rel_path.lastIndexOf("/"))
    : "";
  if (target === curDir) {
    moveError.value = "照片已经在这个目录里了";
    return;
  }

  moveBusy.value = true;
  moveError.value = "";
  try {
    const item = await moveCurrent(target);
    moveOpen.value = false;
    if (!item) {
      toastWarn("照片已经在这个目录里了");
    } else if (item.renamed) {
      // ★ 关键提示：目标目录有同名文件时后端**绝不覆盖**，而是加 _1。
      //   不说的话用户去找原来的名字会找不到，以为照片没了
      toastOk(`已移动，因重名改为「${item.to.slice(item.to.lastIndexOf("/") + 1)}」`);
    } else {
      toastOk("已移动");
    }
  } catch (e) {
    if (e instanceof ApiError) {
      moveError.value = e.userHint || e.friendly || "移动失败";
    } else {
      moveError.value = e instanceof Error ? e.message : "移动失败";
    }
  } finally {
    moveBusy.value = false;
  }
}

/** 切图时把两个弹窗都关掉（否则会对着上一张操作）。 */
watch(() => s.id, () => {
  renameOpen.value = false;
  moveOpen.value = false;
  renameError.value = "";
  moveError.value = "";
});

/** 切图动画类名。 */
const slideClass = computed(() => (s.dir === "none" ? "" : `slide-${s.dir}`));

/** 两指间距。 */
function touchDist(e: TouchEvent): number {
  const a = e.touches[0]!;
  const b = e.touches[1]!;
  return Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
}

/**
 * 手势总入口。
 *
 * 三条手势的优先级（从高到低）：
 *   双指缩放 > 单指（放大后）拖动 > 单指（未放大）翻页 / 下滑关闭
 *
 * ⚠️ 这个优先级不是审美选择，是必须的：双指按下时 `touches.length` 会
 * 从 1 变 2，如果不在 `touchstart` 里立刻切到缩放分支，
 * 第二根手指的落点会被当成"单指拖动"的起点，于是缩放和翻页同时发生。
 */
function onTouchStart(e: TouchEvent): void {
  if (e.touches.length >= 2) {
    // ★ 视频不接管双指：捏合应该留给"整页缩放"或什么都不做。
    //   接管之后把 <video> 缩放到 2 倍，画面糊了也没有任何收益 ——
    //   视频的像素密度是固定的，不像照片可以看细节。
    if (isVideo.value) return;
    // 双指：接管本次手势。清掉单指的跟踪状态，防止松掉一根手指后
    // 残留的 startX/startY 触发一次莫名其妙的翻页。
    tracking = false;
    locked = null;
    dragging.value = false;
    dx.value = 0;
    dy.value = 0;
    pinching.value = true;
    pinchStart = touchDist(e);
    pinchBase = zoom.value;
    return;
  }

  const t = e.touches[0]!;
  startX = t.clientX;
  startY = t.clientY;
  startT = Date.now();
  tracking = true;
  locked = null;
  dragging.value = false;
  dx.value = 0;
  dy.value = 0;
}

function onTouchMove(e: TouchEvent): void {
  // --- 双指缩放 ---
  if (pinching.value) {
    // ⚠️ 手指从 2 根变回 1 根时 `touchDist` 会读到 undefined 而抛错，
    // 所以这里必须挡一道（用户松开一根手指继续滑是很常见的动作）。
    if (e.touches.length < 2) return;
    const d = touchDist(e);
    if (pinchStart <= 0) return;
    const next = pinchBase * (d / pinchStart);
    // 夹在上下限之间：不夹的话能缩到 0.1x（图变成一个小点）或 20x
    zoom.value = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next));
    return;
  }

  if (!tracking || e.touches.length !== 1) return;
  const t = e.touches[0]!;
  const mx = t.clientX - startX;
  const my = t.clientY - startY;

  // --- 已放大：单指拖动查看局部，不翻页、不关闭 ---
  if (zoomed.value) {
    panX.value += t.clientX - startX;
    panY.value += t.clientY - startY;
    // 起点跟着手指走，否则每帧都会重复累加同一段位移
    startX = t.clientX;
    startY = t.clientY;
    return;
  }

  if (!locked) {
    // 前 12px 用来判断方向：手机上手总是有点斜着滑的
    if (Math.abs(mx) < 12 && Math.abs(my) < 12) return;
    locked = Math.abs(mx) > Math.abs(my) * 1.4 ? "h" : "v";
  }

  if (locked === "h") {
    // 到边界了给点阻尼感，让用户知道"到头了"
    const atEdge = (mx > 0 && !hasPrev.value) || (mx < 0 && !hasNext.value);
    dx.value = atEdge ? mx * 0.28 : mx;
    dragging.value = true;
    return;
  }

  // --- 垂直：下滑关闭 ---
  // ⚠️ 只认向下（my > 0）。向上滑是很自然的"想看看图片下半部分"的动作，
  // 若也跟手会让用户以为图片被拖走了。
  // ★ 视频不接管：底部就是原生播放控制条，下滑关闭会和"拖进度条"打架。
  if (my > 0 && !isVideo.value) {
    dy.value = my;
    dragging.value = true;
  }
}

function onTouchEnd(e: TouchEvent): void {
  // --- 双指缩放收尾 ---
  if (pinching.value) {
    // 还有手指留在屏幕上（2→1）时不结束缩放，
    // 否则用户松开一根手指会立刻触发一次翻页判定。
    if (e.touches.length > 0) return;
    pinching.value = false;
    pinchStart = 0;

    // 吸附：缩到接近 1 就回正。不吸附的话会留下一个"歪斜的缩放状态"，
    // 用户既看不出自己在缩放，又发现翻页失灵了（因为 zoomed 仍为真）。
    if (zoom.value <= ZOOM_SNAP) {
      zoom.value = 1;
      panX.value = 0;
      panY.value = 0;
    }
    return;
  }

  if (!tracking) return;
  tracking = false;

  // --- 双击检测 ---
  // ⚠️ 必须在翻页判定**之前**做，并且用 return 短路：
  // 双击的第一次点按在 touchend 时位移≈0、时长≈0，会被下面的
  // flick 判定当成"快速轻扫"（vx 很大）而误翻一页。
  const t = e.changedTouches[0];
  if (t && !dragging.value && !isVideo.value) {
    const now = Date.now();
    const nearLast =
      Math.abs(t.clientX - lastTapX) < DOUBLE_TAP_SLOP &&
      Math.abs(t.clientY - lastTapY) < DOUBLE_TAP_SLOP;
    if (now - lastTapAt < DOUBLE_TAP_MS && nearLast) {
      lastTapAt = 0; // 消费掉，避免三连击被当成两次双击
      if (zoomed.value) {
        zoom.value = 1;
        panX.value = 0;
        panY.value = 0;
      } else {
        // 放大到 2x。用固定的 2 而不是"上次的倍数"，
        // 因为双击的语义是"放大看细节"，用户预期每次结果一样。
        zoom.value = 2;
      }
      dx.value = 0;
      dy.value = 0;
      return;
    }
    lastTapAt = now;
    lastTapX = t.clientX;
    lastTapY = t.clientY;
  }

  const dt = Date.now() - startT;
  const hDist = dx.value;
  const vDist = dy.value;
  const vx = Math.abs(hDist) / Math.max(dt, 1); // px/ms
  const vy = vDist / Math.max(dt, 1);

  dragging.value = false;
  dx.value = 0;
  dy.value = 0;

  // --- 下滑关闭 ---
  if (locked === "v") {
    const h = window.innerHeight || 640;
    // 阈值：下滑够 1/4 屏高，或者向下甩得够快
    if (vDist > h * 0.25 || (vy > 0.5 && vDist > 60)) {
      close();
    }
    return;
  }

  if (locked !== "h") return;
  // 阈值：要么滑够 1/4 屏宽，要么快速轻扫（手机上"甩"一下很常见）
  const w = window.innerWidth || 360;
  const far = Math.abs(hDist) > w * 0.25;
  const flick = vx > 0.45 && Math.abs(hDist) > 28;
  if (!far && !flick) return;

  // 左滑（dist<0）= 看下一张 = 更旧 = next_id
  if (hDist < 0) {
    if (!goNext()) toastWarn("已经是最后一张了");
  } else if (!goPrev()) {
    toastWarn("已经是第一张了");
  }
}

/**
 * 手势被系统打断（来电、通知下拉、手势返回）。
 *
 * ⚠️ 不能复用 `onTouchEnd`：那里会根据位移判定"翻页/关闭"，
 * 而 touchcancel 恰恰是**用户没做完这个动作**。用 touchend 处理的话，
 * 用户滑到一半来电话，挂掉之后就发现照片莫名其妙换了一张。
 * 这里只做状态复位，不触发任何动作。
 */
function onTouchCancel(): void {
  tracking = false;
  locked = null;
  dragging.value = false;
  pinching.value = false;
  pinchStart = 0;
  dx.value = 0;
  dy.value = 0;
  // 缩放保留（用户可能是被打断的缩放），但吸附回正
  if (zoom.value <= ZOOM_SNAP) {
    zoom.value = 1;
    panX.value = 0;
    panY.value = 0;
  }
}

/** 缩放/平移必须随图片切换一起重置，否则下一张会带着上一张的放大状态。 */
watch(() => s.id, () => {
  zoom.value = 1;
  panX.value = 0;
  panY.value = 0;
  pinching.value = false;
  lastTapAt = 0;
});

function onKey(e: KeyboardEvent): void {
  if (!s.open) return;
  if (e.key === "Escape") close();
  else if (e.key === "ArrowLeft") goNext();
  else if (e.key === "ArrowRight") goPrev();
  else if (e.key === "d" || e.key === "D") void doDownload();
}

function onPopState(): void {
  if (s.open) close();
}

onMounted(() => {
  window.addEventListener("keydown", onKey);
  window.addEventListener("popstate", onPopState);
});

onBeforeUnmount(() => {
  window.removeEventListener("keydown", onKey);
  window.removeEventListener("popstate", onPopState);
});

/** 打开时往历史里压一条，让手机上的"返回"手势能关掉查看器而不是退出页面。 */
watch(() => s.open, (v, old) => {
  if (v && !old) {
    infoOpen.value = false;
    try {
      history.pushState({ pbViewer: 1 }, "");
    } catch {
      /* 某些内嵌浏览器禁用 history，忽略 */
    }
  }
});

async function doDownload(): Promise<void> {
  if (!s.detail) return;
  try {
    downloadRaw();
    toastOk("已开始下载原图");
  } catch (e) {
    toastError(e instanceof Error ? e.message : "下载失败");
  }
}

async function doDelete(): Promise<void> {
  const d = s.detail;
  if (!d) return;
  const ok = await confirmDialog({
    title: "移到回收站？",
    message: `「${d.name}」会从照片库里移除。\n\n照片不会被永久删除 —— 它会进电脑上的回收站，之后还能找回来。`,
    note: "如果电脑端没开「允许手机删除」，这一步会被拒绝。",
    okText: "移到回收站",
    danger: true,
  });
  if (!ok) return;

  setConfirmBusy(true);
  try {
    const r = await deleteCurrent();
    if (r.deleted > 0) {
      toastOk("已移到回收站");
    } else {
      const why = r.failed?.[0]?.error;
      toastWarn(why === "not_found" ? "这张照片已经不在了" : "删除失败");
    }
  } catch (e) {
    if (e instanceof ApiError && e.code === "E1015") {
      toastError("电脑端没开「允许手机删除」，请到电脑上打开这个开关");
    } else {
      toastError(e instanceof Error ? e.message : "删除失败");
    }
  } finally {
    setConfirmBusy(false);
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="viewer">
      <div
        v-if="isOpen"
        class="viewer"
        role="dialog"
        aria-modal="true"
        :aria-label="detail?.name ?? '照片查看'"
      >
        <header class="v-bar">
          <button class="v-btn" type="button" aria-label="返回" @click="close">✕</button>
          <div class="v-title ellipsis">{{ detail?.name ?? "" }}</div>
          <button
            class="v-btn"
            type="button"
            :aria-label="infoOpen ? '隐藏信息' : '显示信息'"
            @click="infoOpen = !infoOpen"
          >
            ⓘ
          </button>
        </header>

        <div
          class="stage"
          @touchstart.passive="onTouchStart"
          @touchmove.passive="onTouchMove"
          @touchend="onTouchEnd"
          @touchcancel="onTouchCancel"
        >
          <!-- 占位：详情还没回来时先显示时间线里那张 320 缩略图，避免黑屏 -->
          <img
            v-if="!s.imageReady && currentThumbUrl"
            key="thumb"
            class="v-img thumb"
            :src="safeUrl(currentThumbUrl)"
            alt=""
            decoding="async"
          />

          <!--
            视频（ISSUE-055）：`preload="metadata"` 只要头几 KB（时长/尺寸/
            首帧），不白等整段下载；`playsinline` 是 iOS 上不自动全屏的关键；
            `controls` 交给浏览器 —— 自己写进度条要处理缓冲、音轨、字幕、
            全屏、画中画，而这些原生控件在两端都做得更好。
            `:key` 带 id：换视频时必须重建节点，否则会继续播上一条。
          -->
          <video
            v-if="videoPlayable && !videoBroken"
            :key="`video-${detail?.id}`"
            class="v-img video"
            :class="{ drag: dragging }"
            :style="imageTransform ? { transform: imageTransform } : undefined"
            :src="safeUrl(videoSrc)"
            :poster="safeUrl(currentThumbUrl)"
            controls
            playsinline
            preload="metadata"
            @loadedmetadata="onVideoMeta"
            @error="videoBroken = true"
          />

          <img
            v-else-if="detail"
            :key="detail.id"
            class="v-img main"
            :class="[{ drag: dragging }, slideClass]"
            :style="imageTransform ? { transform: imageTransform } : undefined"
            :src="safeUrl(mainSrc)"
            :alt="detail.name"
            decoding="async"
            @load="onMainLoad"
            @error="s.error = '图片加载失败'"
          />

          <!--
            原图层：只在放大到 1.5× 以上才挂（§5.4.3 约束 ③）。
            ★ 与主图**同时存在**，解码完成后自然盖在上面，全程无白屏无黑闪。
            ★ key 带 id 与 URL 两段：切图或"预取完成 / 预取失败退回网络"
              变化时必须换节点，否则浏览器会沿用旧的原图像素。
          -->
          <img
            v-if="rawSrc"
            :key="`raw-${detail?.id}-${rawSrc}`"
            class="v-img raw"
            :class="{ drag: dragging }"
            :style="imageTransform ? { transform: imageTransform } : undefined"
            :src="safeUrl(rawSrc)"
            alt=""
            decoding="async"
            @error="rawFailed = true"
          />

          <div
            v-if="!s.imageReady && !s.error && !(isVideo && (!videoPlayable || videoBroken))"
            key="loading"
            class="v-loading"
          >
            <span class="spinner big" />
          </div>

          <!--
            编码放不了（HEVC/mkv/avi 很常见）：不能只显示"播放失败"，得给出
            可执行的动作 —— 原片是好的，只是这个浏览器解不了。
            用 `/raw?download=1` 下载：attachment 语义才能让手机"存到相册/文件"。
          -->
          <div
            v-if="isVideo && (!videoPlayable || videoBroken) && detail"
            key="vnote"
            class="v-rawnote"
          >
            {{ detail.video?.reason || `这个视频的编码（${detail.video?.vcodec || "未知"}）浏览器放不了` }}，
            <a class="v-dl" :href="safeUrl(`${detail.urls.raw}?download=1`)">下载原片</a>
            到手机上看
          </div>

          <!--
            未做 faststart：文件尾才放索引，播放器必须先读到尾部才起播。
            本地毫秒级察觉不到，走 VPN/5G 时这就是"点了没反应"的真实原因 ——
            不说清楚用户会以为软件坏了。
          -->
          <div
            v-if="isVideo && videoPlayable && !videoBroken && detail?.video?.faststart === false"
            key="vslow"
            class="v-rawnote"
          >
            这个视频没做 faststart，起播要先读文件尾，跨网络会更慢
          </div>

          <!--
            原图解码失败的提示（放大到 1.5× 以上时才有意义）。
            ★ 必须说出来：用户捏到 1:1 却发现"照片没变清楚"，而他什么都
              没做错 —— 库里真的有 HEIC/TIFF/AVIF 这类浏览器解不了的格式
              （`backend/app/config.py` 的 ALLOWED_IMAGE_EXTS 里有 heic/heif/tif）。
              不说的话他会以为照片坏了或者软件有问题。
            ★ 不写进 `s.error`：那会把整个查看器换成错误页，而预览图明明好好的。
          -->
          <div v-if="rawFailed && useRaw" key="rawfail" class="v-rawnote">
            这张的原始格式浏览器无法显示，正在用预览图（最多 1600px）
          </div>

          <div v-if="s.error" key="error" class="v-error">
            <p>{{ s.error }}</p>
            <button class="btn ghost" type="button" @click="close">返回</button>
          </div>

          <!-- 左右切换按钮：不习惯滑动的用户（以及鼠标用户）也有路走 -->
          <button
            v-if="hasPrev"
            key="prev"
            class="nav prev"
            type="button"
            aria-label="上一张"
            @click="goPrev()"
          >‹</button>
          <button
            v-if="hasNext"
            key="next"
            class="nav next"
            type="button"
            aria-label="下一张"
            @click="goNext()"
          >›</button>
        </div>

        <Transition name="sheet">
          <div v-if="infoOpen && detail" class="sheet">
            <dl class="meta">
              <div class="row"><dt>拍摄时间</dt><dd>{{ takenText }}</dd></div>
              <div class="row"><dt>相机</dt><dd>{{ cameraText }}</dd></div>
              <div class="row"><dt>尺寸</dt><dd>{{ sizeText }}</dd></div>
              <div v-if="exifText" class="row"><dt>参数</dt><dd>{{ exifText }}</dd></div>
              <!-- GPS 只在真的有坐标时才出现：绝大多数照片没有定位信息，
                   常驻一行 "GPS: --" 只是噪音（见 viewer.ts:gpsText） -->
              <div v-if="gpsText" class="row">
                <dt>位置</dt>
                <dd>
                  <a
                    class="geo"
                    :href="safeUrl(gpsMapUrl)"
                    target="_blank"
                    rel="noopener noreferrer"
                  >{{ gpsText }}</a>
                </dd>
              </div>
              <div class="row"><dt>路径</dt><dd class="mono">{{ detail.rel_path }}</dd></div>
            </dl>
          </div>
        </Transition>

        <footer class="v-actions">
          <button class="act" type="button" :disabled="!detail" @click="doDownload">
            <span class="act-i" aria-hidden="true">⬇</span>
            <span>下载原图</span>
          </button>
          <button class="act" type="button" :disabled="!detail" @click="openRename">
            <span class="act-i" aria-hidden="true">✏️</span>
            <span>改名</span>
          </button>
          <button class="act" type="button" :disabled="!detail" @click="openMove">
            <span class="act-i" aria-hidden="true">📁</span>
            <span>移动</span>
          </button>
          <button
            class="act danger"
            type="button"
            :disabled="!detail"
            @click="doDelete"
          >
            <span class="act-i" aria-hidden="true">🗑</span>
            <span>回收站</span>
          </button>
        </footer>

        <!-- 改名弹窗。放查看器内部（而不是用 confirmDialog）是因为要
             输入文本；confirmDialog 只有确定/取消两个按钮。 -->
        <div v-if="renameOpen" class="v-modal" @click.self="renameOpen = false">
          <div class="v-card" role="dialog" aria-modal="true" aria-label="重命名">
            <h3 class="v-card-title">重命名</h3>
            <p class="v-card-sub">会同时改电脑上的文件名。扩展名要保留。</p>
            <input
              ref="renameInput"
              v-model="renameValue"
              class="v-input"
              type="text"
              maxlength="200"
              autocapitalize="off"
              autocorrect="off"
              spellcheck="false"
              enterkeyhint="done"
              @keydown.enter.prevent="doRename"
            />
            <p v-if="renameError" class="v-card-err">{{ renameError }}</p>
            <div class="v-card-actions">
              <button class="btn ghost grow" type="button" :disabled="renameBusy" @click="renameOpen = false">
                取消
              </button>
              <button class="btn primary grow" type="button" :disabled="renameBusy" @click="doRename">
                {{ renameBusy ? "处理中…" : "保存" }}
              </button>
            </div>
          </div>
        </div>

        <!-- 移动面板：显示当前目录 + 子目录列表，底部"移到这里" -->
        <div v-if="moveOpen" class="v-modal" @click.self="moveOpen = false">
          <div class="v-card" role="dialog" aria-modal="true" aria-label="移动到">
            <h3 class="v-card-title">移动到</h3>
            <p class="v-card-sub mono ellipsis">{{ moveBrowsePath || "（照片库根目录）" }}</p>

            <div class="v-folders">
              <button
                v-if="moveBrowsePath"
                class="v-folder up"
                type="button"
                @click="moveUp"
              >
                <span class="v-folder-i" aria-hidden="true">↰</span>
                <span class="v-folder-n">上一级</span>
              </button>
              <button
                v-for="f in moveFolders"
                :key="f.path"
                class="v-folder"
                type="button"
                @click="moveInto(f)"
              >
                <span class="v-folder-i" aria-hidden="true">📁</span>
                <span class="v-folder-n ellipsis">{{ f.name }}</span>
                <span class="v-folder-c">{{ f.count }}</span>
              </button>
              <p v-if="moveLoading" class="v-empty">读取中…</p>
              <p v-else-if="!moveFolders.length && !moveBrowsePath" class="v-empty">
                照片库根目录下没有子目录
              </p>
              <p v-else-if="!moveFolders.length" class="v-empty">
                这个目录下没有子目录，可以移到这里
              </p>
            </div>

            <p v-if="moveError" class="v-card-err">{{ moveError }}</p>
            <div class="v-card-actions">
              <button class="btn ghost grow" type="button" :disabled="moveBusy" @click="moveOpen = false">
                取消
              </button>
              <button class="btn primary grow" type="button" :disabled="moveBusy" @click="doMoveHere">
                {{ moveBusy ? "处理中…" : "移到这里" }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.viewer {
  position: fixed;
  inset: 0;
  z-index: 8000;
  background: #000;
  display: flex;
  flex-direction: column;
  /* 查看器里不允许选中文字，否则长按会弹出系统菜单打断手势 */
  -webkit-user-select: none;
  user-select: none;
  touch-action: pan-y;
}

.v-bar {
  flex: 0 0 auto;
  height: calc(var(--topbar-h) + var(--safe-top));
  padding-top: var(--safe-top);
  display: flex;
  align-items: center;
  gap: 8px;
  padding-left: 6px;
  padding-right: 6px;
  background: linear-gradient(rgba(0, 0, 0, 0.75), rgba(0, 0, 0, 0));
  position: relative;
  z-index: 2;
}

.v-title {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 14px;
  color: #e8e8ee;
  text-align: center;
}

.v-btn {
  width: 38px;
  height: 38px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  color: #e8e8ee;
  background: rgba(255, 255, 255, 0.08);
  flex: 0 0 auto;
}

.v-btn:active {
  background: rgba(255, 255, 255, 0.18);
}

.stage {
  flex: 1 1 auto;
  min-height: 0;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.v-img {
  position: absolute;
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

/* 缩略图垫底：模糊一点，避免和大图抢注意力 */
.v-img.thumb {
  filter: blur(14px);
  opacity: 0.5;
  transform: scale(1.04);
}

.v-img.main {
  will-change: transform;
}

/*
 * 原图层（AC-2.10 第三级）。
 *
 * ★ 必须与 `.main` 用**同一个 transform**：只加在第一层上的话，
 *   用户放大+拖动时会出现"清晰的那张不动、模糊的那张在动" ——
 *   因为两层位置一致，视觉上像是图片在重影。
 *   这里刻意不做任何"代替主图"的优化（比如隐藏 `.main`），
 *   两层渲染同一张图的开销只在客户端合成，没有额外解码。
 */
.v-img.raw {
  pointer-events: none;
  /* 盖在预览之上（两者都是 absolute，靠 z-index 决定谁在上） */
  z-index: 1;
}

/* 视频：铺满舞台中心，原生控件在底部 */
.v-img.video {
  z-index: 1;
  max-height: 100%;
  background: #000;
}

.v-dl {
  color: var(--accent);
  text-decoration: underline;
}

/* 原图格式不支持时的提示条：贴在底部，不挡图片中心 */
.v-rawnote {
  position: absolute;
  left: 50%;
  bottom: 18px;
  transform: translateX(-50%);
  max-width: 88%;
  padding: 7px 12px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.66);
  color: #e8e8ee;
  font-size: 12px;
  text-align: center;
  z-index: 3;
}

.v-img.drag {
  transition: none;
}

/* 切图动画：往左滑出去、新图从右边进来 */
.v-img.slide-next {
  animation: in-right var(--dur) var(--ease);
}

.v-img.slide-prev {
  animation: in-left var(--dur) var(--ease);
}

@keyframes in-right {
  from { transform: translateX(28%); opacity: 0.4; }
  to { transform: translateX(0); opacity: 1; }
}

@keyframes in-left {
  from { transform: translateX(-28%); opacity: 0.4; }
  to { transform: translateX(0); opacity: 1; }
}

.v-loading {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}

.spinner.big {
  width: 30px;
  height: 30px;
  border-width: 3px;
}

.v-error {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  color: var(--fg-1);
  font-size: 14px;
}

.nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  width: 40px;
  height: 68px;
  font-size: 28px;
  color: rgba(255, 255, 255, 0.7);
  background: rgba(0, 0, 0, 0.28);
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.nav.prev { left: 4px; }
.nav.next { right: 4px; }

.nav:active {
  background: rgba(0, 0, 0, 0.55);
}

.sheet {
  flex: 0 0 auto;
  background: rgba(18, 18, 22, 0.96);
  border-top: 1px solid var(--line);
  padding: 12px 16px;
  max-height: 40vh;
  overflow: auto;
}

.meta {
  display: flex;
  flex-direction: column;
  gap: 7px;
  font-size: 13px;
}

.row {
  display: flex;
  gap: 10px;
}

.row dt {
  flex: 0 0 5.2em;
  color: var(--fg-3);
}

.row dd {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--fg-1);
  word-break: break-all;
}

.mono {
  font-family: ui-monospace, monospace;
  font-size: 12px;
}

/* GPS 坐标做成链接：点了跳 OSM 看这是哪儿。
   颜色用链接色 + 下划线，否则用户看不出它能点（它是纯文本坐标，
   形状上完全不像按钮）。 */
.geo {
  color: var(--accent, #6cf);
  text-decoration: underline;
  text-underline-offset: 2px;
  font-family: ui-monospace, monospace;
  font-size: 12px;
}

.v-actions {
  flex: 0 0 auto;
  display: flex;
  gap: 1px;
  padding: 8px 10px calc(8px + var(--safe-bottom));
  background: rgba(14, 14, 17, 0.96);
  border-top: 1px solid var(--line);
}

.act {
  flex: 1 1 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 3px;
  padding: 8px 4px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  color: var(--fg-1);
}

.act-i {
  font-size: 18px;
}

.act:active {
  background: var(--bg-2);
}

.act.danger {
  color: var(--bad);
}

.act:disabled {
  opacity: 0.4;
}

/* ------------------------------------------------------------------ *
 * 改名 / 移动弹窗
 *
 * 不用 confirmDialog：那个只有确定/取消，改名需要输入框，移动需要
 * 目录列表。两个弹窗都放在查看器内部（z-index 高于 .viewer 的 8000），
 * 而不是 Teleport 到 body —— 这样切图时它们随查看器一起消失。
 * ------------------------------------------------------------------ */

.v-modal {
  position: fixed;
  inset: 0;
  z-index: 8100;
  background: rgba(0, 0, 0, 0.66);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}

.v-card {
  width: 100%;
  max-width: 380px;
  background: var(--bg-1);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.v-card-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--fg);
}

.v-card-sub {
  font-size: 13px;
  color: var(--fg-2);
  /* 长路径换行而不是撑破卡片 */
  word-break: break-all;
}

.v-card-err {
  font-size: 13px;
  color: var(--bad);
  background: var(--bad-dim);
  padding: 8px 10px;
  border-radius: var(--radius-sm);
}

.v-card-actions {
  display: flex;
  gap: 8px;
  margin-top: 2px;
}

.v-input {
  width: 100%;
  padding: 11px 12px;
  /* 16px 是 iOS 的门槛：小于它聚焦时页面会被放大 */
  font-size: 16px;
  color: var(--fg);
  background: var(--bg-2);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-sm);
  outline: none;
}

.v-input:focus {
  border-color: var(--accent);
}

.v-folders {
  max-height: 46vh;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  display: flex;
  flex-direction: column;
  gap: 1px;
  margin: 0 -4px;
}

.v-folder {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 11px 8px;
  border-radius: var(--radius-sm);
  font-size: 15px;
  color: var(--fg);
  text-align: left;
}

.v-folder:active {
  background: var(--bg-2);
}

.v-folder-i {
  flex: 0 0 auto;
  font-size: 16px;
}

.v-folder-n {
  flex: 1 1 auto;
  min-width: 0;
}

.v-folder-c {
  flex: 0 0 auto;
  font-size: 12px;
  color: var(--fg-3);
}

.v-folder.up {
  color: var(--accent);
  border-bottom: 1px solid var(--line);
  margin-bottom: 4px;
}

.v-empty {
  padding: 14px 8px;
  font-size: 13px;
  color: var(--fg-3);
  text-align: center;
}

.viewer-enter-active,
.viewer-leave-active {
  transition: opacity 180ms var(--ease);
}

.viewer-enter-from,
.viewer-leave-to {
  opacity: 0;
}

.sheet-enter-active,
.sheet-leave-active {
  transition: transform 180ms var(--ease);
}

.sheet-enter-from,
.sheet-leave-to {
  transform: translateY(100%);
}
</style>
