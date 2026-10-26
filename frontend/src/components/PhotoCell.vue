<script setup lang="ts">
/**
 * 缩略图单元格。
 *
 * 硬约束：
 *   - `loading="lazy"` + `decoding="async"`（任务要求 + docs/03 §5.3.3）
 *   - 显式 `width`/`height`：不写的话图片解码完会撑开格子，
 *     整个网格重排（CLS 抖动），手机上表现为"照片一格格跳"
 *   - `thumb_state === "failed"` → 灰块占位，**不是破图**
 *     （pending 照常发请求：服务端靠"被请求"按需生成，见下面 showImage）
 *
 * 为什么用 `aspect-ratio` 而不是后端给的 w/h：后端的 w/h 是**原图**尺寸，
 * 而缩略图是等比缩放后裁到正方形的，直接用会拉伸变形。
 *
 * ★ 长按多选（US-12「长按选中，点删除」）：`touchstart/move/end` 三个
 *   监听器挂在这里而不是委托到 `PhotoGrid` —— 长按是"按住这个格子"的
 *   手势，交给容器就没法知道手指压的是哪一个；而这三个监听器不查 DOM、
 *   不分配对象，翻到 1500 张也不构成负担（`click` 仍然走委托，另一回事）。
 */
import { computed, onBeforeUnmount, ref } from "vue";
import type { MediaItem } from "../api/types";
import { thumbUrl } from "../api/client";
import { safeUrl } from "../lib/safeUrl";

const props = defineProps<{
  item: MediaItem;
  /** 选中态（长按多选时用）。 */
  selected?: boolean;
  /** 是否处于多选模式 —— 决定"点击"是打开大图还是切换选中。 */
  selectMode?: boolean;
}>();

const emit = defineEmits<{ (e: "longpress", id: number): void }>();

/**
 * ★ 刻意**不**在这里监听 click。
 *
 * 100 张缩略图 = 100 个 addEventListener；翻到 1500 张就是 1500 个。
 * 由父组件 `PhotoGrid` 在网格容器上挂**一个** click，靠 `data-id`
 * 反查是哪个格子（事件委托）。键盘可达性也由父组件统一处理。
 */

/**
 * 长按判定阈值。
 *
 * 450ms 取在 iOS（约 500ms）与安卓（约 400ms）之间：短了会把"点开大图前
 * 手指稍微停一下"误判成多选，长了用户以为没反应就松手了。
 */
const LONG_PRESS_MS = 450;
/** 手指离起点超过这个位移就取消长按（用户其实在滚动列表）。 */
const MOVE_SLOP = 10;

let timer = 0;
let sx = 0;
let sy = 0;
/** 本次触摸是否已经触发过长按。 */
let fired = false;

function clearTimer(): void {
  if (timer) {
    window.clearTimeout(timer);
    timer = 0;
  }
}

function onTouchStart(e: TouchEvent): void {
  if (e.touches.length !== 1) return;  // 双指是缩放，不是长按
  const t = e.touches[0]!;
  sx = t.clientX;
  sy = t.clientY;
  fired = false;
  clearTimer();
  timer = window.setTimeout(() => {
    timer = 0;
    fired = true;
    emit("longpress", props.item.id);
  }, LONG_PRESS_MS);
}

function onTouchMove(e: TouchEvent): void {
  const t = e.touches[0];
  if (!t) return;
  if (Math.abs(t.clientX - sx) > MOVE_SLOP
      || Math.abs(t.clientY - sy) > MOVE_SLOP) {
    clearTimer();
  }
}

function onTouchEnd(e: TouchEvent): void {
  const wasFired = fired;
  fired = false;
  clearTimer();
  // ★ 长按之后浏览器还会补一个 click，那个 click 会被网格的委托监听当成
  //   "用户点了一下"，在多选模式下就是把刚选中的那张**取消选中**。
  //   在 touchend 上 preventDefault 能阻止这一串兼容鼠标事件的派发，
  //   这也是唯一可靠的时机（等 click 到了再判断已经晚了）。
  if (wasFired) e.preventDefault();
}

function onTouchCancel(): void {
  fired = false;
  clearTimer();
}

onBeforeUnmount(clearTimer);

const failed = ref(false);
const loaded = ref(false);

/**
 * 显示条件：**只**排除 failed，不排除 pending。
 *
 * ★ 早期版本写的是 `thumb_state === "ok" && !!thumb_v`，那会造成死锁：
 *   pending 的项连 `<img>` 都不渲染 → 永远不会请求缩略图 URL →
 *   而服务端恰恰是靠"被请求"来按需生成的。后台队列满时 `enqueue`
 *   会**刻意丢弃**任务（不阻塞索引器），被丢的项就永远停在 pending，
 *   前端也永远不请求 → 永久灰块。
 *
 * 现在 pending 也发请求：服务端 `_serve_thumb` 会在引擎没生成出来时
 * 当场同步生成并返回真图。代价是首屏可能多几次真实生成（每张约 30ms，
 * 且只发生一次），换来"照片一定会出现"。
 *
 * failed 仍然不请求 —— 那种图服务端会直接回 500，请求了也是白费，
 * 还会让浏览器控制台刷错误。
 */
const showImage = computed(() => props.item.thumb_state !== "failed" && !failed.value);

const src = computed(() => thumbUrl(props.item.id, props.item.thumb_v, 320));

/** 视频角标（ISSUE-055）：封面同样是缩略图，但要让人分得清"这是段片子"。 */
const isVideo = computed(() => props.item.kind === "video");
const durText = computed(() => {
  const d = props.item.duration;
  if (d === null || !Number.isFinite(d) || d <= 0) return "";
  const total = Math.round(d);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? `${h}:` : "") + `${mm}:${String(s).padStart(2, "0")}`;
});

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
    :class="{ selected, 'has-img': showImage, multi: selectMode }"
    :data-id="item.id"
    role="button"
    tabindex="-1"
    :aria-label="item.name"
    @touchstart.passive="onTouchStart"
    @touchmove.passive="onTouchMove"
    @touchend="onTouchEnd"
    @touchcancel="onTouchCancel"
    @contextmenu.prevent
  >
    <!--
      灰块占位：只在缩略图**加载失败**或后端标记 failed 时显示。
      比 <img> 破图好得多 —— 破图图标会让用户以为程序坏了。
      pending 不显示占位块：它已经发了请求，服务端会当场生成。
    -->
    <span v-if="!showImage" class="ph" aria-hidden="true">
      <span class="ph-icon">{{ isVideo ? "🎬" : "🖼" }}</span>
      <span v-if="item.thumb_state === 'failed'" class="ph-tag">缩略图失败</span>
    </span>

    <img
      v-else
      :src="safeUrl(src)"
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

    <!-- 视频角标：右下角，与左下角的 📍、右上角的选中圈互不打架 -->
    <span v-if="isVideo" class="badge dur" :aria-label="`视频 ${durText}`">
      <span class="play" aria-hidden="true">▶</span>
      <span v-if="durText">{{ durText }}</span>
    </span>

    <!--
      选中标记：
        - 多选模式下**已选** → ✓ 实心角标
        - 多选模式下**未选** → 空心圈（告诉用户"每张都能选"，
          否则用户看不出哪些格子是可操作的）
    -->
    <span v-if="selected" class="badge check" aria-hidden="true">✓</span>
    <span
      v-else-if="selectMode"
      class="badge check empty"
      aria-hidden="true"
    />
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

.dur {
  right: 3px;
  bottom: 3px;
  display: flex;
  align-items: center;
  gap: 3px;
  padding: 2px 4px;
  background: rgba(0, 0, 0, 0.62);
  color: #fff;
  /* 等宽数字：几百个格子一起出现时时间不会左右跳 */
  font-variant-numeric: tabular-nums;
}

.play {
  font-size: 8px;
  line-height: 1;
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

/* 多选模式下**未选中**的圈：半透明黑底 + 白描边，在任何照片上都看得见 */
.check.empty {
  background: rgba(0, 0, 0, 0.35);
  border: 1.5px solid rgba(255, 255, 255, 0.85);
}

.cell.selected {
  outline: 3px solid var(--accent);
  outline-offset: -3px;
}

/* 多选模式下取消按压缩放：用户是在"勾选"，格子跳动会让人以为点错了 */
.cell.multi:active {
  transform: none;
}
</style>
