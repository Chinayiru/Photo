<script setup lang="ts">
/**
 * 时间线 Tab：日期分组 + 无限滚动。
 *
 * 加载策略：`PhotoGrid` 的 IntersectionObserver 触底 → `loadMore()`。
 * 这里额外做两件事：
 *   ① 首次挂载时若一页撑不满屏，`poke()` 再要一页（否则用户看不到滚动条，
 *      以为"就这些照片"）
 *   ② 切回本 Tab 时如果上次加载失败，自动重试一次
 */
import { computed, onActivated, onDeactivated, onMounted, onBeforeUnmount, ref, watch } from "vue";
import PhotoGrid from "../components/PhotoGrid.vue";
import {
  groups,
  isEmpty,
  emptyHint,
  loadMore,
  removeItem,
  timelineStore,
} from "../stores/timeline";
import { systemStore } from "../stores/system";
import { open as openViewer } from "../stores/viewer";
import {
  enter as enterSelect,
  onRemoved,
  selectAll,
  selectedSet,
  selectionStore,
  toggle as toggleSelect,
} from "../stores/selection";

const grid = ref<InstanceType<typeof PhotoGrid> | null>(null);

const loading = computed(() => timelineStore.state.loading);
const error = computed(() => timelineStore.state.error);
const loaded = computed(() => timelineStore.state.loaded);
const hasMoreRef = computed(() => timelineStore.state.hasMore);
const totalRef = computed(() => timelineStore.state.totalInLibrary);

const selActive = computed(() => selectionStore.state.active);
const selected = selectedSet;

onMounted(() => {
  if (!loaded.value) void loadMore();
  // 首屏只有几张时，sentinel 一开始就在视口里；IntersectionObserver 会回调一次。
  // 但如果那次恰好 hasMore 还是 true 且请求未完成，就没人再触发了 —— 补一次。
  window.setTimeout(poke, 300);
  offRemoved = onRemoved(removeItem);
});

onActivated(() => {
  if (error.value) void loadMore();
});

/* ------------------------------------------------------------------ *
 * 多选（US-12「长按选中，点删除」）
 * ------------------------------------------------------------------ */

function onLongPress(id: number): void {
  enterSelect(id);
}

function onToggle(id: number): void {
  toggleSelect(id);
}

/** 全选当前**已加载**的条目（不是全库 —— 全库要几千次勾选，没有意义）。 */
function onSelectAll(): void {
  selectAll(timelineStore.state.items.map((x) => x.id));
}

/** 本视图对应的"移除一项"实现，注销函数在卸载时调用。 */
let offRemoved: (() => void) | null = null;

onBeforeUnmount(() => {
  offRemoved?.();
  offRemoved = null;
  window.clearTimeout(liveTimer);
  window.clearTimeout(pullResetTimer);
});

/** 列表变短后（例如只有 3 张）再探一次，避免"撑不满屏就再也加载不了"。 */
function poke(): void {
  if (!hasMoreRef.value || loading.value) return;
  grid.value?.poke();
}

function onOpen(id: number): void {
  openViewer(id);
}

function retry(): void {
  void loadMore();
}

/** 扫描完成后库里多了照片，用户下拉刷新能立刻看到。 */
function refresh(): Promise<void> {
  return loadMore(true).then(() => {
    window.setTimeout(poke, 200);
  });
}

defineExpose({ refresh });

/* ------------------------------------------------------------------ *
 * 下拉刷新（US-11「等进度跑完，手机时间线里就出现了」）
 * ------------------------------------------------------------------ */

/**
 * ★ 为什么必须自己实现手势。
 *
 * `styles/base.css` 里 `html, body { overflow: hidden; overscroll-behavior: none }`，
 * 滚动发生在 `.scroll` 容器上（`overscroll-behavior-y: contain`）——
 * 浏览器原生的"橡皮筋下拉刷新"在这个布局下**永远不会触发**，
 * 而 `SettingsView.vue` 早就写着「下拉刷新可手动更新」，
 * `docs/07-测试计划与用例.md` 的 TC-SSE-FUNC-006 也写着"无需手动刷新或下拉"。
 * 不实现的话，那句提示指向一个不存在的操作。
 */

/** 触发刷新所需的位移（px）。 */
const PULL_THRESHOLD = 60;
/** 最多能拉多远 —— 再往下一厘米和再往下十厘米手感一样，反而显得迟钝。 */
const PULL_MAX = 90;

const pullY = ref(0);
const pulling = ref(false);
const refreshing = ref(false);

let pullStartY = 0;
let pullArmed = false;
let pullResetTimer = 0;

const tlRef = ref<HTMLElement | null>(null);

/** 找到本次触摸所在的滚动容器（就是 `PhotoGrid` 的根节点）。 */
function scrollTopOf(): number {
  // ★ 不能取 `e.currentTarget.scrollTop`：监听器挂在 `.tl` 上，而真正滚动
  //   的是它的子节点 `PhotoGrid` 的 `.grid-root.scroll` —— `.tl` 是 flex
  //   容器、自己从不滚动，`scrollTop` 恒为 0。那样写等价于"任何位置都
  //   允许下拉"，用户翻到两百张的位置想往上回看，一滑就整页重载。
  const el = tlRef.value?.querySelector(".grid-root") as HTMLElement | null;
  return el ? el.scrollTop : 0;
}

function onPullStart(e: TouchEvent): void {
  if (refreshing.value) return;
  if (e.touches.length !== 1) return;
  // 只有停在顶部时才是"下拉刷新"；不在顶部就是普通滚动。
  pullArmed = scrollTopOf() <= 0;
  pullStartY = e.touches[0]!.clientY;
  if (!pullArmed) {
    pulling.value = false;
    pullY.value = 0;
  }
}

function onPullMove(e: TouchEvent): void {
  if (!pullArmed || refreshing.value) return;
  const t = e.touches[0];
  if (!t) return;
  const dy = t.clientY - pullStartY;
  if (dy <= 0) {
    // 反向推回：交还给正常的滚动，别把手指锁在刷新手势里
    pulling.value = false;
    pullY.value = 0;
    return;
  }
  if (scrollTopOf() > 0) {
    // 中途列表动过（例如同时有惯性滚动），取消本次下拉
    pullArmed = false;
    pulling.value = false;
    pullY.value = 0;
    return;
  }
  // 阻尼：手指移动 2px，指示器走 1px。1:1 会让"拉到底"来得太突然。
  pullY.value = Math.min(dy * 0.5, PULL_MAX);
  pulling.value = true;
  // ★ 只有我们自己接管的这一段才 preventDefault：否则用户在列表中间
  //   往上滑时也会被吞掉滚动。
  if (e.cancelable) e.preventDefault();
}

function onPullEnd(): void {
  const wasArmed = pullArmed;
  pullArmed = false;
  const shouldRefresh = wasArmed && pullY.value >= PULL_THRESHOLD;
  pulling.value = false;
  if (!shouldRefresh) {
    pullY.value = 0;
    return;
  }
  pullY.value = PULL_THRESHOLD;   // 收在阈值处，指示器留在屏幕上转圈
  void doPullRefresh();
}

/** 取消（来电、系统手势打断）：不要把它当成一次"松手"。 */
function onPullCancel(): void {
  pullArmed = false;
  pulling.value = false;
  pullY.value = 0;
}

async function doPullRefresh(): Promise<void> {
  refreshing.value = true;
  // ★ 即使请求几十毫秒就回来了，也让指示器至少留 500ms：
  //   不然它闪一下就消失，用户根本不知道刷没刷新。
  const started = Date.now();
  try {
    await refresh();
  } finally {
    const wait = Math.max(0, 500 - (Date.now() - started));
    pullResetTimer = window.setTimeout(() => {
      refreshing.value = false;
      pullY.value = 0;
    }, wait);
  }
}

watch(() => totalRef.value, () => {
  // 总数为 0 但本地有缓存条目（换电脑了）→ 清掉重来
  if (totalRef.value === 0 && timelineStore.state.items.length > 0) {
    void loadMore(true);
  }
});

/* ------------------------------------------------------------------ *
 * AC-2.15：新照片自动出现（不需要用户下拉刷新）
 * ------------------------------------------------------------------ */

/**
 * 防抖句柄。
 *
 * ★ 必须防抖：服务端 `media_changed` 是**每个根一条**，多根同时有
 * 变化时会连发几条；每条都触发一次全量重载的话，用户会看到时间线
 * 连闪几次、而且每次都把滚动位置冲掉。
 */
let liveTimer = 0;

/** 挂起中的媒体变化。Tab 被缓存（KeepAlive）时不能立刻重载。 */
let pendingLive = false;

watch(() => systemStore.state.mediaChangedAt, (v, old) => {
  if (!v || v === old) return;
  // 用户正在看查看器大图时重载时间线，会把他脚下的列表整个换掉；
  // 等他关掉查看器（onActivated）再刷新。
  if (document.visibilityState !== "visible") {
    pendingLive = true;
    return;
  }
  window.clearTimeout(liveTimer);
  liveTimer = window.setTimeout(() => {
    const gridEl = document.querySelector<HTMLElement>(".grid-root.scroll");
    // ★ 只在顶部附近才自动重载。用户翻了 200 张正在看老照片时，
    //   把列表重置回第一页等于把他刚翻到的地方弄丢了 —— 那比
    //   "没自动出现"更让人恼火。
    if (gridEl && gridEl.scrollTop > 240) {
      pendingLive = true;
      return;
    }
    refresh();
  }, 400);
});

onActivated(() => {
  // Tab 是 KeepAlive 缓存的：切走期间来的变化在这里补一次
  if (pendingLive) {
    pendingLive = false;
    refresh();
  }
});

onDeactivated(() => {
  window.clearTimeout(liveTimer);
});
</script>

<template>
  <div
    ref="tlRef"
    class="tl"
    :style="pullY > 0 ? { transform: `translateY(${pullY}px)` } : undefined"
    :class="{ 'tl-pulling': pulling || refreshing }"
    @touchstart.passive="onPullStart"
    @touchmove="onPullMove"
    @touchend="onPullEnd"
    @touchcancel="onPullCancel"
  >
    <!--
      下拉指示器。★ 必须让用户看到"我在下拉"与"松手会刷新"的区别，
      否则他只会觉得列表被拖动了一下。
    -->
    <div
      v-if="pullY > 0"
      class="pull"
      :class="{ ready: pullY >= PULL_THRESHOLD, busy: refreshing }"
      aria-live="polite"
    >
      <span v-if="refreshing" class="spinner" />
      <span v-else class="pull-arrow" aria-hidden="true">↓</span>
      <span class="pull-txt">
        {{ refreshing ? "正在刷新…" : (pullY >= PULL_THRESHOLD ? "松开刷新" : "下拉刷新") }}
      </span>
    </div>

    <div v-if="!loaded && loading" class="skeleton-wrap">
      <div v-for="i in 12" :key="i" class="skeleton sk-cell" />
    </div>

    <div v-else-if="isEmpty" class="empty">
      <div class="empty-icon" aria-hidden="true">📷</div>
      <p class="empty-title">{{ emptyHint }}</p>
      <p class="empty-sub">
        去「上传」把手机里的照片传过去，或者到电脑上把照片放进照片目录。
      </p>
      <button class="btn ghost" type="button" @click="refresh">重新加载</button>
    </div>

    <template v-else>
      <div v-if="error" class="err-bar">
        <span class="ellipsis">{{ error }}</span>
        <button class="btn ghost sm" type="button" @click="retry">重试</button>
      </div>
      <PhotoGrid
        ref="grid"
        :groups="groups"
        :loading="loading"
        :has-more="hasMoreRef"
        :selected="selected"
        :select-mode="selActive"
        @open="onOpen"
        @load-more="loadMore()"
        @toggle="onToggle"
        @longpress="onLongPress"
      />
    </template>

    <!--
      多选入口。
      ★ 长按是隐藏手势，用户第一次用不可能知道 —— 必须有一个看得见的入口。
        放在右下角悬浮，而不是页签栏（页签栏已被 App.vue 的 SelectionBar
        在选中时占用，两处都放会让人分不清主次）。
    -->
    <button
      v-if="!selActive && loaded && timelineStore.state.items.length > 0"
      class="sel-entry"
      type="button"
      aria-label="多选"
      @click="enterSelect()"
    >
      ☑
    </button>

    <!-- 多选模式下的"全选"（当前已加载的条目） -->
    <button
      v-if="selActive"
      class="sel-all"
      type="button"
      @click="onSelectAll"
    >
      全选已加载
    </button>
  </div>
</template>

<style scoped>
.tl {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  position: relative;
  /* 下拉时整体下移，露出上面的指示器。跟手，所以不加 transition */
  will-change: transform;
}

/*
 * 下拉指示器。
 * ★ 用 `top: -44px` 而不是常规流：常规流会把内容也往下挤，
 *   而外层 `.tl` 已经整体下移了 pullY —— 两边一起动就成了两倍位移。
 */
.pull {
  position: absolute;
  top: -44px;
  left: 0;
  right: 0;
  height: 44px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-size: 13px;
  color: var(--fg-2);
}

.pull.ready {
  color: var(--accent);
  font-weight: 600;
}

.pull-arrow {
  font-size: 15px;
  /* 未到阈值时箭头朝下（"还能再拉"），到了阈值 JS 会换成"松开刷新"文案 */
  transition: transform 0.15s;
}

.pull.ready .pull-arrow {
  transform: rotate(180deg);
}

/* 多选入口：右下角悬浮，避开页签栏与最后一行照片 */
.sel-entry {
  position: absolute;
  right: 14px;
  bottom: calc(var(--tabbar-h) + var(--safe-bottom) + 14px);
  z-index: 20;
  width: 44px;
  height: 44px;
  border-radius: 50%;
  border: 1px solid var(--line);
  background: var(--bg-1);
  color: var(--fg-1);
  font-size: 19px;
  line-height: 1;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.28);
}

.sel-entry:active {
  transform: scale(0.94);
}

/* 多选模式下的"全选"：贴着筛选/错误条下面一行 */
.sel-all {
  position: absolute;
  right: 12px;
  top: 6px;
  z-index: 20;
  padding: 5px 10px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: var(--bg-1);
  color: var(--fg-1);
  font-size: 12px;
}

.skeleton-wrap {
  display: grid;
  grid-template-columns: repeat(var(--grid-cols), 1fr);
  gap: var(--grid-gap);
  padding: 8px 0;
}

.sk-cell {
  aspect-ratio: 1 / 1;
}

.empty {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  padding: 40px 28px;
  text-align: center;
}

.empty-icon {
  font-size: 46px;
  opacity: 0.35;
}

.empty-title {
  font-size: 16px;
  font-weight: 600;
}

.empty-sub {
  color: var(--fg-2);
  font-size: 13px;
  line-height: 1.6;
  max-width: 26em;
}

.err-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 8px 10px 0;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--bad-dim);
  color: var(--bad);
  font-size: 13px;
}

.err-bar .ellipsis {
  flex: 1 1 auto;
  min-width: 0;
}
</style>
