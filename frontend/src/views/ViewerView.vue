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
  moveCurrent,
  renameCurrent,
  sizeText,
  takenText,
  viewerStore,
} from "../stores/viewer";
import { toastError, toastOk, toastWarn } from "../stores/toast";

const s = viewerStore.state;
const isOpen = computed(() => s.open);

/** 手势状态。 */
let startX = 0;
let startY = 0;
let startT = 0;
let tracking = false;
let locked: "h" | "v" | null = null;

/** 拖动中的水平位移（px），用于跟手动画。 */
const dx = ref(0);
const dragging = ref(false);
/** 信息面板是否展开（默认收起，不挡图）。 */
const infoOpen = ref(false);

const hasPrev = computed(() => s.detail?.prev_id != null);
const hasNext = computed(() => s.detail?.next_id != null);
const detail = computed(() => s.detail);

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

function onTouchStart(e: TouchEvent): void {
  if (e.touches.length !== 1) return;
  const t = e.touches[0]!;
  startX = t.clientX;
  startY = t.clientY;
  startT = Date.now();
  tracking = true;
  locked = null;
  dragging.value = false;
  dx.value = 0;
}

function onTouchMove(e: TouchEvent): void {
  if (!tracking || e.touches.length !== 1) return;
  const t = e.touches[0]!;
  const mx = t.clientX - startX;
  const my = t.clientY - startY;

  if (!locked) {
    // 前 12px 用来判断方向：手机上手总是有点斜着滑的
    if (Math.abs(mx) < 12 && Math.abs(my) < 12) return;
    locked = Math.abs(mx) > Math.abs(my) * 1.4 ? "h" : "v";
  }
  if (locked !== "h") return;

  // 到边界了给点阻尼感，让用户知道"到头了"
  const atEdge = (mx > 0 && !hasPrev.value) || (mx < 0 && !hasNext.value);
  dx.value = atEdge ? mx * 0.28 : mx;
  dragging.value = true;
}

function onTouchEnd(): void {
  if (!tracking) return;
  tracking = false;
  const dt = Date.now() - startT;
  const dist = dx.value;
  const vx = Math.abs(dist) / Math.max(dt, 1); // px/ms

  dragging.value = false;
  dx.value = 0;

  if (locked !== "h") return;
  // 阈值：要么滑够 1/4 屏宽，要么快速轻扫（手机上"甩"一下很常见）
  const w = window.innerWidth || 360;
  const far = Math.abs(dist) > w * 0.25;
  const flick = vx > 0.45 && Math.abs(dist) > 28;
  if (!far && !flick) return;

  // 左滑（dist<0）= 看下一张 = 更旧 = next_id
  if (dist < 0) {
    if (!goNext()) toastWarn("已经是最后一张了");
  } else if (!goPrev()) {
    toastWarn("已经是第一张了");
  }
}

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
          @touchcancel="onTouchEnd"
        >
          <!-- 占位：详情还没回来时先显示时间线里那张 320 缩略图，避免黑屏 -->
          <img
            v-if="!s.imageReady && currentThumbUrl"
            class="v-img thumb"
            :src="currentThumbUrl"
            alt=""
            decoding="async"
          />

          <img
            v-if="detail"
            :key="detail.id"
            class="v-img main"
            :class="[{ drag: dragging }, slideClass]"
            :style="dragging ? { transform: `translateX(${dx}px)` } : undefined"
            :src="currentPreviewUrl"
            :alt="detail.name"
            decoding="async"
            @load="s.imageReady = true"
            @error="s.error = '图片加载失败'"
          />

          <div v-if="!s.imageReady && !s.error" class="v-loading">
            <span class="spinner big" />
          </div>

          <div v-if="s.error" class="v-error">
            <p>{{ s.error }}</p>
            <button class="btn ghost" type="button" @click="close">返回</button>
          </div>

          <!-- 左右切换按钮：不习惯滑动的用户（以及鼠标用户）也有路走 -->
          <button
            v-if="hasPrev"
            class="nav prev"
            type="button"
            aria-label="上一张"
            @click="goPrev()"
          >‹</button>
          <button
            v-if="hasNext"
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
