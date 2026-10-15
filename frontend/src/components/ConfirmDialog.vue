<script setup lang="ts">
/**
 * 二次确认对话框（纯展示）。
 *
 * 状态与 `confirmDialog()` 都在 `src/stores/confirm.ts` ——
 * 因为 `<script setup>` 里不能写 ES module 导出，函数没法从这里导出去。
 * 本组件只负责画，并且**必须挂在 App 根上**（全局只有一个）。
 */
import { confirmState, settleConfirm } from "../stores/confirm";

const s = confirmState;
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div v-if="s.open" class="mask" @click.self="settleConfirm(false)">
        <div class="dlg" role="alertdialog" aria-modal="true">
          <h3 class="dlg-title">{{ s.opts.title }}</h3>
          <p class="dlg-msg">{{ s.opts.message }}</p>
          <p v-if="s.opts.note" class="dlg-note">{{ s.opts.note }}</p>
          <div class="dlg-actions">
            <button class="btn ghost grow" type="button" @click="settleConfirm(false)">
              {{ s.opts.cancelText }}
            </button>
            <button
              class="btn grow"
              :class="s.opts.danger ? 'danger' : 'primary'"
              type="button"
              :disabled="s.busy"
              @click="settleConfirm(true)"
            >
              {{ s.busy ? "处理中…" : s.opts.okText }}
            </button>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.mask {
  position: fixed;
  inset: 0;
  z-index: 9500;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  backdrop-filter: blur(2px);
}

.dlg {
  width: min(100%, 380px);
  background: var(--bg-1);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-lg);
  padding: 20px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.6);
}

.dlg-title {
  font-size: 17px;
  margin-bottom: 10px;
}

.dlg-msg {
  color: var(--fg-1);
  font-size: 14px;
  line-height: 1.55;
  /* 调用方用 \n 分段，这里保留换行 */
  white-space: pre-line;
}

.dlg-note {
  margin-top: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--warn-dim);
  color: var(--warn);
  font-size: 13px;
}

.dlg-actions {
  display: flex;
  gap: 10px;
  margin-top: 20px;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 160ms var(--ease);
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
