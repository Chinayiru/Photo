<script setup lang="ts">
/**
 * 上传 Tab。
 *
 * 两个入口：相册多选（`accept="image/*" multiple`）与直接拍照
 * （`capture="environment"` 让手机后置摄像头直接打开）。
 *
 * 上传逻辑全在 `stores/upload.ts`，本文件只负责选择文件与画进度。
 * 分片/续传/秒传/409 重对齐/断网退避/取消都在 store 里，
 * 因为那些逻辑必须在用户切走 Tab、甚至切到后台时继续跑 —— 挂在组件
 * 生命周期上的话，一离开 Tab 就被打断了。
 */
import { computed, onMounted, ref } from "vue";
import {
  cancelTask,
  clearFinished,
  enqueue,
  fmtBytes,
  fmtRate,
  hasActive,
  isEmptyQueue,
  overall,
  pauseAll,
  requeueInterrupted,
  resumeAll,
  retryTask,
  statusClass,
  statusText,
  summaryText,
  taskPercent,
  taskSent,
  uploadStore,
} from "../stores/upload";
import { toastOk, toastWarn } from "../stores/toast";

const fileInput = ref<HTMLInputElement | null>(null);
const cameraInput = ref<HTMLInputElement | null>(null);
const showDone = ref(false);

const s = uploadStore.state;

const tasks = computed(() => {
  const all = s.tasks;
  return showDone.value ? all : all.filter((t) => t.status !== "DONE" && t.status !== "DEDUPED");
});
const doneCount = computed(() =>
  s.tasks.filter((t) => t.status === "DONE" || t.status === "DEDUPED").length);

onMounted(() => {
  // 上次上传被系统杀掉（切后台 / 浏览器回收）后，任务还停在 UPLOADING。
  // 这里统一重置为待传，走"问服务端 offset"的续传路径（docs/03 §5.5.6）。
  requeueInterrupted();
});

function pickFiles(): void {
  fileInput.value?.click();
}

function pickCamera(): void {
  cameraInput.value?.click();
}

function onFiles(e: Event): void {
  const el = e.target as HTMLInputElement;
  const files = el.files;
  if (!files || files.length === 0) return;
  const n = enqueue(files);
  if (n === 0) {
    toastWarn("没有可上传的图片（只支持图片文件，已跳过重复项）");
  } else {
    toastOk(`已加入 ${n} 个文件`);
  }
  // 清空 value，否则同一个文件再选一次不会触发 change
  el.value = "";
}

function onCancel(id: string): void {
  void cancelTask(id);
}

function clearAllDone(): void {
  clearFinished();
  showDone.value = false;
}
</script>

<template>
  <div class="up scroll">
    <!-- 总进度：只在有任务时出现，平时保持界面干净 -->
    <div v-if="hasActive || s.tasks.length > 0" class="card total">
      <div class="total-head">
        <span class="total-label">{{ summaryText }}</span>
        <span class="total-pct">{{ Math.round(overall.percent) }}%</span>
      </div>
      <div class="bar">
        <div class="bar-in" :style="{ width: `${overall.percent}%` }" />
      </div>
      <div class="total-foot">
        <span>{{ fmtBytes(overall.sent) }} / {{ fmtBytes(overall.total) }}</span>
        <span v-if="overall.rate > 0">{{ fmtRate(overall.rate) }}</span>
      </div>
      <div class="total-btns">
        <button v-if="s.paused" class="btn ghost sm" type="button" @click="resumeAll">
          继续全部
        </button>
        <button v-else-if="hasActive" class="btn ghost sm" type="button" @click="pauseAll">
          暂停全部
        </button>
        <button
          v-if="doneCount > 0"
          class="btn ghost sm"
          type="button"
          @click="clearAllDone"
        >
          清除已完成
        </button>
      </div>
    </div>

    <!-- 队列 -->
    <ul v-if="tasks.length > 0" class="queue">
      <li v-for="t in tasks" :key="t.id" class="task card">
        <div class="t-head">
          <span class="t-name ellipsis">{{ t.name }}</span>
          <button
            v-if="t.status === 'FAILED' || t.status === 'CANCELED'"
            class="t-x retry"
            type="button"
            aria-label="重试"
            @click="retryTask(t.id)"
          >↻</button>
          <button
            v-else
            class="t-x"
            type="button"
            :aria-label="`取消 ${t.name}`"
            @click="onCancel(t.id)"
          >✕</button>
        </div>

        <div class="bar thin">
          <div
            class="bar-in"
            :class="{ bad: t.status === 'FAILED' }"
            :style="{ width: `${taskPercent(t)}%` }"
          />
        </div>

        <div class="t-foot">
          <span class="t-status" :class="statusClass(t)">{{ statusText(t) }}</span>
          <span class="t-size">
            {{ fmtBytes(taskSent(t)) }} / {{ fmtBytes(t.size) }}
            <template v-if="t.rate > 0"> · {{ fmtRate(t.rate) }}</template>
          </span>
        </div>
        <p v-if="t.error" class="t-err">{{ t.error }}</p>
      </li>
    </ul>

    <!-- 空状态 -->
    <div v-else-if="isEmptyQueue" class="hero">
      <div class="hero-icon" aria-hidden="true">⬆</div>
      <p class="hero-title">把手机里的照片传上来</p>
      <p class="hero-sub">
        传完后照片就在电脑上了，手机可以放心清理空间。
        传输走局域网，不花流量。
      </p>
    </div>

    <div v-else class="hidden-hint">
      <span>{{ doneCount }} 个已完成</span>
      <button class="btn ghost sm" type="button" @click="showDone = true">显示</button>
    </div>

    <div class="spacer" />

    <!-- 选择入口：固定在底部 Tab 栏之上，单手可达 -->
    <div class="picker">
      <button class="btn ghost lg grow" type="button" @click="pickCamera">
        <span aria-hidden="true">📷</span> 拍照
      </button>
      <button class="btn primary lg grow" type="button" @click="pickFiles">
        <span aria-hidden="true">🖼</span> 从相册选
      </button>
    </div>

    <input
      ref="fileInput"
      class="sr"
      type="file"
      accept="image/*,video/mp4,video/quicktime,video/webm"
      multiple
      @change="onFiles"
    />
    <input
      ref="cameraInput"
      class="sr"
      type="file"
      accept="image/*"
      capture="environment"
      @change="onFiles"
    />
  </div>
</template>

<style scoped>
.up {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 10px 10px 0;
  padding-bottom: calc(var(--tabbar-h) + var(--safe-bottom) + 74px);
}

.total {
  padding: 12px 14px;
}

.total-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 9px;
}

.total-label {
  font-size: 14px;
  color: var(--fg);
}

.total-pct {
  font-size: 17px;
  font-weight: 700;
  color: var(--accent);
  font-variant-numeric: tabular-nums;
}

.bar {
  height: 7px;
  border-radius: 999px;
  background: var(--bg-3);
  overflow: hidden;
}

.bar.thin {
  height: 4px;
}

.bar-in {
  height: 100%;
  background: linear-gradient(90deg, var(--accent-press), var(--accent));
  border-radius: 999px;
  transition: width 200ms linear;
}

.bar-in.bad {
  background: var(--bad);
}

.total-foot,
.t-foot {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  margin-top: 7px;
  font-size: 12px;
  color: var(--fg-2);
}

.total-btns {
  display: flex;
  gap: 8px;
  margin-top: 11px;
}

.queue {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.task {
  padding: 11px 13px;
}

.t-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.t-name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 13px;
  color: var(--fg);
}

.t-x {
  flex: 0 0 auto;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: var(--bg-3);
  color: var(--fg-2);
  font-size: 13px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.t-x.retry {
  color: var(--accent);
}

.t-status.ok { color: var(--ok); }
.t-status.warn { color: var(--warn); }
.t-status.bad { color: var(--bad); }
.t-status.busy { color: var(--accent); }

.t-size {
  font-variant-numeric: tabular-nums;
}

.t-err {
  margin-top: 6px;
  font-size: 12px;
  color: var(--bad);
  word-break: break-word;
}

.hero {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
  text-align: center;
  padding: 30px 24px;
}

.hero-icon {
  font-size: 44px;
  opacity: 0.35;
}

.hero-title {
  font-size: 16px;
  font-weight: 600;
}

.hero-sub {
  color: var(--fg-2);
  font-size: 13px;
  line-height: 1.65;
  max-width: 24em;
}

.hidden-hint {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 4px 6px;
  color: var(--fg-2);
  font-size: 13px;
}

.spacer {
  flex: 1 1 auto;
  min-height: 10px;
}

.picker {
  position: sticky;
  bottom: 0;
  display: flex;
  gap: 10px;
  padding: 10px 0 6px;
  /* 从下方渐入，避免滚动内容从按钮底下"硬切"出来 */
  background: linear-gradient(rgba(11, 11, 13, 0), var(--bg) 32%);
}

.grow {
  flex: 1 1 0;
}

/* 隐藏原生 file input，但不用 display:none —— iOS 上 display:none 的
   input 有时点不动，用 1px + clip 更稳 */
.sr {
  position: absolute;
  width: 1px;
  height: 1px;
  opacity: 0;
  pointer-events: none;
  overflow: hidden;
  clip: rect(0 0 0 0);
}
</style>
