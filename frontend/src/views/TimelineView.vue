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
import { computed, onActivated, onDeactivated, onMounted, ref, watch } from "vue";
import PhotoGrid from "../components/PhotoGrid.vue";
import {
  groups,
  isEmpty,
  emptyHint,
  loadMore,
  timelineStore,
} from "../stores/timeline";
import { systemStore } from "../stores/system";
import { open as openViewer } from "../stores/viewer";

const grid = ref<InstanceType<typeof PhotoGrid> | null>(null);

const loading = computed(() => timelineStore.state.loading);
const error = computed(() => timelineStore.state.error);
const loaded = computed(() => timelineStore.state.loaded);
const hasMoreRef = computed(() => timelineStore.state.hasMore);
const totalRef = computed(() => timelineStore.state.totalInLibrary);

onMounted(() => {
  if (!loaded.value) void loadMore();
  // 首屏只有几张时，sentinel 一开始就在视口里；IntersectionObserver 会回调一次。
  // 但如果那次恰好 hasMore 还是 true 且请求未完成，就没人再触发了 —— 补一次。
  window.setTimeout(poke, 300);
});

onActivated(() => {
  if (error.value) void loadMore();
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
function refresh(): void {
  void loadMore(true).then(() => window.setTimeout(poke, 200));
}

defineExpose({ refresh });

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
  <div class="tl">
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
        @open="onOpen"
        @load-more="loadMore()"
      />
    </template>
  </div>
</template>

<style scoped>
.tl {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  position: relative;
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
