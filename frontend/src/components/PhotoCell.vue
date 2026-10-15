<script setup lang="ts">
/**
 * 缩略图单元格。
 *
 * 硬约束：
 *   - `loading="lazy"` + `decoding="async"`（任务要求 + docs/03 §5.3.3）
 *   - 显式 `width`/`height`：不写的话图片解码完会撑开格子，
 *     整个网格重排（CLS 抖动），手机上表现为"照片一格格跳"
 *   - `thumb_state !== "ok"` → 灰块占位，**不是破图**
 *
 * 为什么用 `aspect-ratio` 而不是后端给的 w/h：后端的 w/h 是**原图**尺寸，
 * 而缩略图是等比缩放后裁到正方形的，直接用会拉伸变形。
 */
import { computed, ref } from "vue";
import type { MediaItem } from "../api/types";
import { thumbUrl } from "../api/client";

const props = defineProps<{
  item: MediaItem;
  /** 选中态（长按多选时用）。 */
  selected?: boolean;
}>();

/**
 * ★ 刻意**不**在这里监听 click。
 *
 * 100 张缩略图 = 100 个 addEventListener；翻到 1500 张就是 1500 个。
 * 由父组件 `PhotoGrid` 在网格容器上挂**一个** click，靠 `data-id`
 * 反查是哪个格子（事件委托）。键盘可达性也由父组件统一处理。
 */
const failed = ref(false);
const loaded = ref(false);

/** thumb_state === "ok" 且没加载失败，才真的显示图。 */
const showImage = computed(() =>
  props.item.thumb_state === "ok" && !!props.item.thumb_v && !failed.value);

const src = computed(() => thumbUrl(props.item.id, props.item.thumb_v, 320));

function onError(): void {
  failed.value = true;
}

function onLoad(): void {
  loaded.value = true;
}
</script>

<template>
  <div
    class="cell"
    :class="{ selected, 'has-img': showImage }"
    :data-id="item.id"
    role="button"
    tabindex="-1"
    :aria-label="item.name"
  >
    <!--
      灰块占位：thumb_state 是 pending/failed/none 时显示。
      比 <img> 破图好得多 —— 破图图标会让用户以为程序坏了。
    -->
    <span v-if="!showImage" class="ph" aria-hidden="true">
      <span class="ph-icon">🖼</span>
      <span v-if="item.thumb_state === 'failed'" class="ph-tag">缩略图失败</span>
      <span v-else-if="item.thumb_state === 'pending'" class="ph-tag">生成中</span>
    </span>

    <img
      v-else
      :src="src"
      :alt="item.name"
      class="img"
      :class="{ in: loaded }"
      width="320"
      height="320"
      loading="lazy"
      decoding="async"
      draggable="false"
      @load="onLoad"
      @error="onError"
    />

    <span v-if="item.has_gps" class="badge gps" aria-label="有位置信息">📍</span>
    <span v-if="selected" class="badge check" aria-hidden="true">✓</span>
  </div>
</template>

<style scoped>
.cell {
  position: relative;
  display: block;
  width: 100%;
  /* 正方形：与后端缩略图 320×320 的裁切一致 */
  aspect-ratio: 1 / 1;
  background: var(--bg-2);
  overflow: hidden;
  padding: 0;
  border-radius: 0;
  transition: transform 120ms var(--ease);
  /* 父组件用事件委托，这里只需要能被命中 */
  cursor: pointer;
  -webkit-user-select: none;
  user-select: none;
}

.cell:active {
  transform: scale(0.96);
  z-index: 1;
}

.img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
  opacity: 0;
  transition: opacity 220ms var(--ease);
}

/* 图片解码完才淡入：避免"从上往下刷"的半张图 */
.img.in {
  opacity: 1;
}

.ph {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  background: linear-gradient(135deg, var(--bg-2), var(--bg-3));
  color: var(--fg-3);
}

.ph-icon {
  font-size: 22px;
  opacity: 0.5;
}

.ph-tag {
  font-size: 10px;
  color: var(--fg-3);
}

.badge {
  position: absolute;
  font-size: 10px;
  line-height: 1;
  border-radius: 999px;
}

.gps {
  left: 3px;
  bottom: 3px;
  padding: 2px 3px;
  background: rgba(0, 0, 0, 0.55);
}

.check {
  right: 4px;
  top: 4px;
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--accent);
  color: #1a0e04;
  font-weight: 700;
  font-size: 12px;
}

.cell.selected {
  outline: 3px solid var(--accent);
  outline-offset: -3px;
}
</style>
