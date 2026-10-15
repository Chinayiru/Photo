<script setup lang="ts">
/** 轻提示。Teleport 到 body，避免被父级的 overflow/transform 裁掉。 */
import { dismiss, toasts } from "../stores/toast";
</script>

<template>
  <Teleport to="body">
    <div class="toasts" role="status" aria-live="polite">
      <TransitionGroup name="toast">
        <div
          v-for="t in toasts.items"
          :key="t.id"
          class="toast"
          :class="t.kind"
          @click="dismiss(t.id)"
        >
          {{ t.text }}
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<style scoped>
.toasts {
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  /* 放在底部 Tab 栏之上，别盖住按钮 */
  bottom: calc(var(--tabbar-h) + var(--safe-bottom) + 16px);
  z-index: 9000;
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: center;
  width: min(92vw, 420px);
  pointer-events: none;
}

.toast {
  pointer-events: auto;
  max-width: 100%;
  padding: 10px 14px;
  border-radius: var(--radius);
  background: var(--bg-3);
  color: var(--fg);
  font-size: 14px;
  line-height: 1.4;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.5);
  border: 1px solid var(--line-strong);
  word-break: break-word;
}

.toast.ok {
  border-color: var(--ok);
  color: #d8ffe9;
  background: #123324;
}

.toast.warn {
  border-color: var(--warn);
  color: #fff3d0;
  background: #3a2f0d;
}

.toast.error {
  border-color: var(--bad);
  color: #ffdede;
  background: #3a1414;
}

.toast-enter-active,
.toast-leave-active {
  transition: opacity 200ms var(--ease), transform 200ms var(--ease);
}

.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(8px) scale(0.97);
}
</style>
