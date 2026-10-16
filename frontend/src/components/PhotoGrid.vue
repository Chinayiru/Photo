<script setup lang="ts">
/**
 * 照片网格 + 按日期分组。
 *
 * ★ 事件委托（任务硬约束）：整个网格容器上只有 **一个** click 监听，
 *   靠 `data-id` 反查被点的格子。不给每个 `PhotoCell` 挂监听 ——
 *   翻到 1500 张时那是 1500 个监听器，内存与派发成本都不必要。
 *
 * ★ 无限滚动用 `IntersectionObserver`（`rootMargin: "1200px"`），
 *   而不是监听 scroll 事件：scroll 每帧都触发，手机上会掉帧；
 *   而 IntersectionObserver 由浏览器在合成线程算，零主线程开销。
 */
import { onBeforeUnmount, onMounted, ref } from "vue";
import type { MediaItem } from "../api/types";
import PhotoCell from "./PhotoCell.vue";

const props = defineProps<{
  groups: { key: string; label: string; items: MediaItem[] }[];
  loading: boolean;
  hasMore: boolean;
  /** 选中的 id 集合（多选模式）。 */
  selected?: Set<number>;
  /** 是否处于多选模式 —— 点击变成切换选中，而不是打开大图。 */
  selectMode?: boolean;
}>();

const emit = defineEmits<{
  (e: "open", id: number): void;
  (e: "load-more"): void;
  /** 多选模式下点了一下某个格子（切换选中）。 */
  (e: "toggle", id: number): void;
  /** 长按某一个格子（进入多选）。 */
  (e: "longpress", id: number): void;
}>();

const rootEl = ref<HTMLElement | null>(null);
const sentinel = ref<HTMLElement | null>(null);
let io: IntersectionObserver | null = null;

/**
 * 事件委托：从事件目标往上找带 data-id 的格子。
 *
 * ★ 多选模式下**不**发 `open`：用户是在勾选，弹出全屏大图会让整个
 *   多选流程断掉（还得先关掉查看器才能继续选）。改成发 `toggle`。
 */
function onClick(e: MouseEvent): void {
  const el = (e.target as HTMLElement | null)?.closest?.("[data-id]") as HTMLElement | null;
  if (!el) return;
  const id = Number(el.dataset.id);
  if (!Number.isFinite(id) || id <= 0) return;
  if (props.selectMode) emit("toggle", id);
  else emit("open", id);
}

/**
 * 长按进入多选。
 *
 * `PhotoCell` 在 `touchend` 上已经 `preventDefault()` 掉了长按之后的
 * 那次 click，所以正常路径下这里不会再收到 click。但**长按触发的那一帧
 * 到 touchend 之间**如果用户手指抬起得极快（脚本派发的合成事件就会这样），
 * 兼容 click 仍可能到达 —— 多选模式下它只会 `toggle` 一次（取消选中），
 * 所以这里再兜一道：刚进入多选后的 500ms 内忽略点击。
 */
let suppressUntil = 0;

function onLongPress(id: number): void {
  suppressUntil = Date.now() + 500;
  emit("longpress", id);
}

function onClickGuarded(e: MouseEvent): void {
  if (suppressUntil && Date.now() < suppressUntil) return;
  onClick(e);
}

function setupObserver(): void {
  if (!sentinel.value) return;
  io = new IntersectionObserver(
    (entries) => {
      for (const en of entries) {
        if (!en.isIntersecting) continue;
        // hasMore=false 时父组件不会真的发请求（loadMore 内部会短路），
        // 但这里也判一次，省掉一次函数调用
        if (!props.hasMore || props.loading) continue;
        emit("load-more");
      }
    },
    {
      // 提前 1200px 触发（docs/03 §5.3.2）：手机上快速滑动时，
      // 等到真的看到底部再加载会明显"卡一下"
      rootMargin: "1200px",
      threshold: 0,
    },
  );
  io.observe(sentinel.value);
}

onMounted(setupObserver);

onBeforeUnmount(() => {
  io?.disconnect();
  io = null;
});

/**
 * 供父组件在列表变短后手动再探一次。
 * 场景：第一页只有 5 张、撑不满一屏 → sentinel 一开始就在视口里，
 * IntersectionObserver 会触发一次；但如果那次请求失败，之后
 * sentinel 一直处于 intersecting 状态就不会再回调了。
 */
function poke(): void {
  if (props.hasMore && !props.loading) emit("load-more");
}

defineExpose({ poke, rootEl });
</script>

<template>
  <div ref="rootEl" class="grid-root scroll" @click="onClickGuarded">
    <section v-for="g in props.groups" :key="g.key" class="day">
      <h2 class="day-h">
        <span>{{ g.label }}</span>
        <span class="day-n">{{ g.items.length }}</span>
      </h2>
      <div class="grid">
        <PhotoCell
          v-for="it in g.items"
          :key="it.id"
          :item="it"
          :selected="props.selected?.has(it.id)"
          :select-mode="props.selectMode"
          @longpress="onLongPress"
        />
      </div>
    </section>

    <!-- 触底哨兵 -->
    <div ref="sentinel" class="sentinel" aria-hidden="true" />

    <div v-if="props.loading" class="foot">
      <span class="spinner" />
      <span>加载中…</span>
    </div>
    <div v-else-if="!props.hasMore && props.groups.length > 0" class="foot done">
      没有更多了
    </div>
  </div>
</template>

<style scoped>
.grid-root {
  flex: 1 1 auto;
  min-height: 0;
  /* 底部留出 Tab 栏 + 安全区，否则最后一行会被挡住 */
  padding-bottom: calc(var(--tabbar-h) + var(--safe-bottom) + 8px);
}

.day {
  margin-bottom: 14px;
}

.day-h {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 10px 12px 6px;
  font-size: 14px;
  font-weight: 600;
  color: var(--fg-1);
  /* 吸顶：滑动时始终知道自己在哪天 */
  position: sticky;
  top: 0;
  z-index: 5;
  background: linear-gradient(var(--bg) 70%, rgba(11, 11, 13, 0));
}

.day-n {
  font-size: 11px;
  font-weight: 400;
  color: var(--fg-3);
}

.grid {
  display: grid;
  grid-template-columns: repeat(var(--grid-cols), 1fr);
  gap: var(--grid-gap);
}

.sentinel {
  height: 1px;
}

.foot {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 18px;
  color: var(--fg-2);
  font-size: 13px;
}

.foot.done {
  color: var(--fg-3);
}
</style>
