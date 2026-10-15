<script setup lang="ts">
/**
 * 通用二次确认对话框。
 *
 * 为什么不用 `window.confirm`：iOS 上 confirm 的按钮文案只能是"确定/取消"，
 * 而删除照片必须让用户看清"是进回收站，不是永久删除"——
 * 这直接决定了用户按下去时有多犹豫。自定义弹窗才能把这句话写在按钮上。
 */
import { ref } from "vue";

export interface ConfirmOptions {
  title: string;
  /** 支持多行；用 `\n` 分段。 */
  message: string;
  /** 确认按钮文案。默认"确定"。 */
  okText?: string;
  cancelText?: string;
  /** true 时确认按钮是红色（危险操作）。 */
  danger?: boolean;
  /** 强调提示（黄字），用于"进回收站"这类关键说明。 */
  note?: string;
}

const open = ref(false);
const opts = ref<ConfirmOptions>({
  title: "",
  message: "",
  okText: "确定",
  cancelText: "取消",
  danger: false,
});
const busy = ref(false);

let resolver: ((v: boolean) => void) | null = null;

/** 弹出确认框，resolve 用户的选择。 */
export function confirmDialog(o: ConfirmOptions): Promise<boolean> {
  // 前一个还没关（连点两次）→ 先把前一个当作取消
  resolver?.(false);
  opts.value = {
    okText: "确定",
    cancelText: "取消",
    danger: false,
    ...o,
  };
  open.value = true;
  busy.value = false;
  return new Promise<boolean>((resolve) => {
    resolver = resolve;
  });
}

function finish(v: boolean): void {
  if (busy.value) return;
  open.value = false;
  const r = resolver;
  resolver = null;
  r?.(v);
}

/** 让调用方在异步确认后标记忙碌（例如删除请求进行中）。 */
function setBusy(v: boolean): void {
  busy.value = v;
}

defineExpose({ setBusy });
</script>

<template>
  <Teleport to="body">
    <Transition name="fade">
      <div v-if="open" class="mask" @click.self="finish(false)">
        <div class="dlg" role="dialog" aria-modal="true">
          <h3 class="dlg-title">{{ opts.title }}</h3>
          <p class="dlg-msg">{{ opts.message }}</p>
          <p v-if="opts.note" class="dlg-note">{{ opts.note }}</p>
          <div class="dlg-actions">
            <button class="btn ghost grow" type="button" @click="finish(false)">
              {{ opts.cancelText }}
            </button>
            <button
              class="btn grow"
              :class="opts.danger ? 'danger' : 'primary'"
              type="button"
              :disabled="busy"
              @click="finish(true)"
            >
              {{ busy ? "处理中…" : opts.okText }}
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
  /* 弹窗打开时不让背后的内容跟着滚 */
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
