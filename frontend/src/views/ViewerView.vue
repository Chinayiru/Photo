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
import { ApiError } from "../api/client";
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
          <button
            class="act danger"
            type="button"
            :disabled="!detail"
            @click="doDelete"
          >
            <span class="act-i" aria-hidden="true">🗑</span>
            <span>移到回收站</span>
          </button>
        </footer>
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
