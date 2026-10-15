<script setup lang="ts">
/**
 * 顶部状态条：显示通路与设备名。
 *
 * 一期只有局域网直连，所以只有 🟢 与 🔴 两态；
 * 二期接云隧道时这里再加 🔵 云端中继（docs/03 §5.1.3）。
 */
import { authStore, connClass, connText, deviceLabel } from "../stores/auth";

defineProps<{ title: string }>();

async function retry(): Promise<void> {
  const { probe } = await import("../stores/auth");
  await probe();
}
</script>

<template>
  <header class="topbar">
    <div class="title ellipsis">{{ title }}</div>
    <button
      class="conn"
      :class="connClass"
      type="button"
      :aria-label="`连接状态：${connText}，点击重新探测`"
      @click="retry"
    >
      <span class="dot" aria-hidden="true" />
      <span class="conn-text ellipsis">{{ connText }}</span>
      <span v-if="authStore.state.conn === 'DIRECT'" class="dev ellipsis">
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
