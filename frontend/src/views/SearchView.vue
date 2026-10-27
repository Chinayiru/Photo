<script setup lang="ts">
/**
 * 搜索 Tab。
 *
 * ⚠️ 后端硬约束：`GET /api/search` **至少要有一个筛选条件**，
 *   否则 400 `E1001`「至少需要一个筛选条件」（`backend/app/handlers/browse.py`）。
 *   所以这里禁止"空条件搜索"——按钮在没条件时是禁用的，
 *   而不是发出去再让用户看报错。
 *
 * 结果按日期分组复用时间线的分组函数，点开也走同一个查看器 ——
 * 查看器的左右滑动靠 `prev_id`/`next_id`，与来源列表无关，天然可用。
 */
import { computed, onBeforeUnmount, ref } from "vue";
import PhotoGrid from "../components/PhotoGrid.vue";
import { api } from "../api/client";
import type { MediaItem, SearchResponse } from "../api/types";
import { groupByDay } from "../stores/timeline";
import { open as openViewer } from "../stores/viewer";
import {
  enter as enterSelect,
  onRemoved,
  selectAll,
  selectedSet,
  selectionStore,
  toggle as toggleSelect,
} from "../stores/selection";
import { errorText } from "../api/errorText";

const q = ref("");
const from = ref("");
const to = ref("");
const camera = ref("");
const hasGps = ref(false);

const items = ref<MediaItem[]>([]);
const nextCursor = ref<string | null>(null);
const hasMore = ref(false);
const loading = ref(false);
const searched = ref(false);
const error = ref("");

/**
 * 已搜出的结果分布在哪些文件夹（ISSUE-060 C）。
 *
 * 用户"找一张照片"时的真实困境往往不是搜不到，而是**搜到一堆**：文件名
 * 都有"IMG"，翻了三屏还没找到。给出目录分布，一眼就能看出"我要的都在
 * 「青甘环线/第一天」里"，点一下就直接下钻过去 —— 比继续翻页快得多。
 *
 * 只取前 8 个（按条数降序）：再多就失去"一眼看清"的作用。
 */
const dirStats = computed(() => {
  const m = new Map<string, number>();
  for (const it of items.value) {
    const d = it.dir ?? "";
    if (!d) continue;
    m.set(d, (m.get(d) ?? 0) + 1);
  }
  return Array.from(m.entries())
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
    .map(([dir, count]) => ({
      dir,
      count,
      // 只显示最后一层目录名：完整路径太长，横排会挤成省略号
      name: dir.split("/").filter(Boolean).pop() ?? dir,
    }));
});

/** 切到「照片」Tab 并下钻到这个目录（由 App.vue 监听）。 */
function gotoFolder(dir: string): void {
  window.dispatchEvent(new CustomEvent("pb:goto-folder", { detail: dir }));
}

const groups = computed(() => groupByDay(items.value));

/* ------------------------------------------------------------------ *
 * 多选（US-12「长按选中，点删除」）
 * ------------------------------------------------------------------ */

const selActive = computed(() => selectionStore.state.active);
const selected = selectedSet;

function onLongPress(id: number): void {
  enterSelect(id);
}

function onToggle(id: number): void {
  toggleSelect(id);
}

/** 全选当前**已搜出**的结果（不是全库 —— 全库要几千次勾选，没有意义）。 */
function onSelectAll(): void {
  selectAll(items.value.map((x) => x.id));
}

/** 搜索页有自己的一份列表，删除后要摘的是这里的。 */
function removeItem(id: number): void {
  const i = items.value.findIndex((x) => x.id === id);
  if (i >= 0) items.value.splice(i, 1);
}

let offRemoved: (() => void) | null = null;

onBeforeUnmount(() => {
  offRemoved?.();
  offRemoved = null;
});

/**
 * 挂上"删除后摘格子"的回调。
 *
 * ★ 与时间线共用同一个槽位：哪个视图当前在显示，哪个视图的回调就生效
 *   （见 `stores/selection.ts: onRemoved` ）。两个视图都挂的话后挂的
 *   会覆盖先挂的，于是"在搜索页删照片，时间线里那张还在" ——
 *   而 KeepAlive 让两个视图**同时**活着，这不是假想的问题。
 */
offRemoved = onRemoved(removeItem);

const canSearch = computed(() =>
  Boolean(q.value.trim() || from.value || to.value || camera.value.trim() || hasGps.value));

const emptyText = computed(() => {
  if (!searched.value) return "";
  if (items.value.length > 0) return "";
  return "没有匹配的照片";
});

/** 把日期输入框的 `2024-05-12` 变成时间戳（本地时区）。 */
function dayStart(s: string): number | null {
  if (!s) return null;
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (!m) return null;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), 0, 0, 0, 0);
  return Number.isNaN(d.getTime()) ? null : Math.floor(d.getTime() / 1000);
}

/** `to` 是**不含**端点（后端用 `<`），所以加一天。 */
function dayEnd(s: string): number | null {
  const t = dayStart(s);
  return t === null ? null : t + 86400;
}

async function run(reset = true): Promise<void> {
  if (!canSearch.value) return;
  if (loading.value) return;
  if (!reset && !hasMore.value) return;

  loading.value = true;
  error.value = "";
  try {
    const query: Record<string, string | number | boolean> = {
      limit: 100,
    };
    if (q.value.trim()) query.q = q.value.trim();
    const f = dayStart(from.value);
    const t = dayEnd(to.value);
    if (f !== null) query.from = f;
    if (t !== null) query.to = t;
    if (camera.value.trim()) query.camera = camera.value.trim();
    if (hasGps.value) query.has_gps = 1;
    if (!reset && nextCursor.value) query.cursor = nextCursor.value;

    const r = await api.get<SearchResponse>("/api/search", { query, retry: 1 });
    items.value = reset ? r.items : [...items.value, ...r.items];
    nextCursor.value = r.next_cursor ?? null;
    hasMore.value = Boolean(r.has_more) && Boolean(r.next_cursor);
    searched.value = true;
  } catch (e) {
    error.value = errorText(e, "搜索失败");
  } finally {
    loading.value = false;
  }
}

function reset(): void {
  q.value = "";
  from.value = "";
  to.value = "";
  camera.value = "";
  hasGps.value = false;
  items.value = [];
  nextCursor.value = null;
  hasMore.value = false;
  searched.value = false;
  error.value = "";
}

function onOpen(id: number): void {
  openViewer(id);
}
</script>

<template>
  <div class="search">
    <div class="form">
      <div class="q-row">
        <input
          v-model="q"
          class="input-lg grow"
          type="search"
          enterkeyhint="search"
          placeholder="按文件名找，例如 IMG_2024"
          autocapitalize="off"
          autocorrect="off"
          spellcheck="false"
          @keyup.enter="run(true)"
        />
        <button class="btn primary" type="button" :disabled="!canSearch || loading" @click="run(true)">
          搜索
        </button>
      </div>

      <details class="more">
        <summary>更多条件</summary>
        <div class="grid2">
          <label class="f">
            <span class="lbl">开始日期</span>
            <input v-model="from" class="input-lg" type="date" />
          </label>
          <label class="f">
            <span class="lbl">结束日期</span>
            <input v-model="to" class="input-lg" type="date" />
          </label>
          <label class="f span2">
            <span class="lbl">相机</span>
            <input
              v-model="camera"
              class="input-lg"
              type="text"
              placeholder="例如 iPhone 或 Canon"
              autocapitalize="off"
              autocorrect="off"
              spellcheck="false"
            />
          </label>
          <label class="f span2 chk">
            <input v-model="hasGps" type="checkbox" />
            <span>只看有位置信息的照片</span>
          </label>
        </div>
        <div class="more-btns">
          <button class="btn ghost sm" type="button" @click="reset">清空</button>
        </div>
      </details>

      <p v-if="error" class="err">{{ error }}</p>
    </div>

    <div v-if="groups.length > 0 && dirStats.length > 0" class="dirs">
      <span class="dirs-label">这些结果在这些文件夹：</span>
      <button
        v-for="d in dirStats"
        :key="d.dir"
        class="dir-chip"
        type="button"
        :title="d.dir"
        @click="gotoFolder(d.dir)"
      >{{ d.name }} · {{ d.count }}</button>
    </div>

    <PhotoGrid
      v-if="groups.length > 0"
      :groups="groups"
      :loading="loading"
      :has-more="hasMore"
      :selected="selected"
      :select-mode="selActive"
      @open="onOpen"
      @load-more="run(false)"
      @toggle="onToggle"
      @longpress="onLongPress"
    />

    <!-- 多选模式下的"全选"（当前已搜出的结果） -->
    <button v-if="selActive && groups.length > 0" class="sel-all" type="button" @click="onSelectAll">
      全选已搜出
    </button>

    <!-- 多选入口：长按是隐藏手势，必须有一个看得见的入口 -->
    <button
      v-if="!selActive && groups.length > 0"
      class="sel-entry"
      type="button"
      aria-label="多选"
      @click="enterSelect()"
    >
      ☑
    </button>

    <div v-else-if="searched && !loading" class="empty">
      <div class="empty-icon" aria-hidden="true">🔍</div>
      <p class="empty-title">{{ emptyText }}</p>
      <p class="empty-sub">换个文件名片段，或者放宽日期范围试试。</p>
    </div>

    <div v-else-if="!searched" class="empty">
      <div class="empty-icon" aria-hidden="true">🔍</div>
      <p class="empty-title">找一张照片</p>
      <p class="empty-sub">
        可以按文件名、拍摄日期、相机型号来找。至少要填一个条件。
      </p>
    </div>
  </div>
</template>

<style scoped>
.search {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  /* 多选入口/全选按钮是 absolute，需要这里有定位上下文 */
  position: relative;
}

/* 结果里的目录胶囊（ISSUE-060 C）：横向滚动，不换行占高度 */
.dirs {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  gap: 6px;
  overflow-x: auto;
  padding: 8px 10px 2px;
  scrollbar-width: none;
}

.dirs::-webkit-scrollbar {
  display: none;
}

.dirs-label {
  flex: 0 0 auto;
  font-size: 11px;
  color: var(--fg-3);
}

.dir-chip {
  flex: 0 0 auto;
  border: 1px solid var(--line);
  background: var(--bg-2);
  color: var(--fg-1);
  font-size: 12px;
  padding: 5px 10px;
  border-radius: 999px;
  white-space: nowrap;
}

.form {
  flex: 0 0 auto;
  padding: 10px 10px 8px;
  border-bottom: 1px solid var(--line);
  background: var(--bg);
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.q-row {
  display: flex;
  gap: 8px;
}

.grow {
  flex: 1 1 auto;
  min-width: 0;
}

.more summary {
  font-size: 13px;
  color: var(--fg-2);
  cursor: pointer;
}

.grid2 {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin: 12px 0 10px;
}

.span2 {
  grid-column: 1 / -1;
}

.f {
  display: flex;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
}

.lbl {
  font-size: 12px;
  color: var(--fg-3);
}

.chk {
  flex-direction: row;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--fg-1);
}

.chk input {
  width: 18px;
  height: 18px;
  accent-color: var(--accent);
}

.more-btns {
  display: flex;
  justify-content: flex-end;
}

.err {
  color: var(--bad);
  font-size: 13px;
}

.empty {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 9px;
  padding: 40px 28px calc(var(--tabbar-h) + var(--safe-bottom) + 40px);
  text-align: center;
}

.empty-icon {
  font-size: 42px;
  opacity: 0.3;
}

.empty-title {
  font-size: 16px;
  font-weight: 600;
}

.empty-sub {
  color: var(--fg-2);
  font-size: 13px;
  line-height: 1.6;
  max-width: 24em;
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
</style>
