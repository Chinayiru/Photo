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
import { computed, onActivated, onMounted, ref, watch } from "vue";
import PhotoGrid from "../components/PhotoGrid.vue";
import {
  groups,
  isEmpty,
  emptyHint,
  loadMore,
  timelineStore,
} from "../stores/timeline";
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
