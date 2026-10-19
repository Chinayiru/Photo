<script setup lang="ts">
/**
 * 顶部状态条：显示通路与设备名。
 *
 * 四个视觉态（`docs/03` §5.1.3）：
 *
 * | 通路 | 点 | 文案 |
 * |---|---|---|
 * | 局域网直连 | 🟢 绿 | 局域网直连 |
 * | 云端中继 | 🔵 蓝 | 云端中继 |
 * | 正在探测 | 🟡 闪 | 正在连接… |
 * | 不通/配对失效 | 🔴 红 | 电脑离线 / 配对已失效 |
 *
 * 为什么云端要单独一个颜色而不是也用绿：两种通路都"能用"，但延迟、
 * 手机流量、是否经过公网完全不同。用同一个绿点，用户在家里 Wi-Fi 没连上、
 * 悄悄走了 4G 云端时完全看不出来 —— 而这正是最该让他知道的一件事。
 * 同理，云端态的设备名后面加一枚 📶 之类的标记反而不如直接换色清楚。
 *
 * 点击 = 手动重新探测（`transport.rescan()`，会清掉"局域网失败记忆"，
 * 是用户手动纠正"刚才明明连着"的唯一入口）。
 */
import { computed } from "vue";
import {
  authStore, connClass, connText, deviceLabel, deviceMismatch, reconnect,
} from "../stores/auth";

defineProps<{ title: string }>();

/**
 * 设备名只在确认连上的态显示。
 * 探测中/离线时那个名字是**上一次**的缓存值，显示出来会让人以为还连着。
 */
const showDevice = computed<boolean>(() => {
  const s = authStore.state.conn;
  return (s === "DIRECT" || s === "CLOUD") && Boolean(deviceLabel.value);
});

const hint = computed<string>(() => {
  if (deviceMismatch.value) return "连到了另一台电脑，点击重新探测";
  return `连接状态：${connText.value}，点击重新探测`;
});
</script>

<template>
  <header class="topbar">
    <div class="title ellipsis">{{ title }}</div>
    <button
      class="conn"
      :class="connClass"
      type="button"
      :aria-label="hint"
      :title="hint"
      @click="reconnect"
    >
      <span class="dot" aria-hidden="true" />
      <span class="conn-text ellipsis">{{ connText }}</span>
      <span v-if="showDevice" class="dev ellipsis">
        {{ deviceLabel }}
      </span>
    </button>
  </header>
</template>

<style scoped>
.topbar {
  flex: 0 0 auto;
  height: var(--topbar-h);
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0 10px 0 14px;
  background: var(--bg);
  border-bottom: 1px solid var(--line);
  /* 状态条永远在最上层，滚动内容从它下面穿过 */
  position: relative;
  z-index: 20;
}

.title {
  font-size: 17px;
  font-weight: 600;
  flex: 1 1 auto;
  min-width: 0;
}

.conn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: 999px;
  background: var(--bg-2);
  font-size: 12px;
  max-width: 62%;
}

.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--fg-3);
  flex: 0 0 auto;
}

.conn.ok .dot {
  background: var(--ok);
  box-shadow: 0 0 6px var(--ok);
}

/* 云端中继：蓝点 + 淡蓝底。不加动画 —— 它是稳态，不是过渡态；
   动起来会让人以为"还在连"，而实际已经可以用了 */
.conn.cloud {
  background: var(--cloud-dim);
}

.conn.cloud .dot {
  background: var(--cloud);
  box-shadow: 0 0 6px var(--cloud);
}

.conn.cloud .conn-text {
  color: var(--cloud);
}

.conn.busy .dot {
  background: var(--warn);
  animation: pb-blink 1s ease-in-out infinite;
}

.conn.bad .dot {
  background: var(--bad);
}

@keyframes pb-blink {
  50% { opacity: 0.35; }
}

.conn-text {
  color: var(--fg-1);
}

.conn.bad .conn-text {
  color: var(--bad);
}

.dev {
  color: var(--fg-3);
  max-width: 8em;
  border-left: 1px solid var(--line-strong);
  padding-left: 6px;
}
</style>
