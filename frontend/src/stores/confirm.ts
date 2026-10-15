/**
 * 二次确认对话框的状态与 API。
 *
 * 为什么把状态放在 store 而不是组件里：`<script setup>` 里**不能有
 * ES module 导出**（vue 编译器直接报错），所以 `confirmDialog()` 这个
 * 函数没法从 `ConfirmDialog.vue` 里导出去给别的模块用。
 * 放到普通 .ts 模块里，两边都干净：组件只负责画，调用方只负责问。
 */

import { reactive } from "vue";

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

export const confirmState = reactive({
  open: false,
  busy: false,
  opts: {
    title: "",
    message: "",
    okText: "确定",
    cancelText: "取消",
    danger: false,
    note: "",
  } as Required<ConfirmOptions>,
});

let resolver: ((v: boolean) => void) | null = null;

/**
 * 弹出确认框，resolve 用户的选择。
 *
 * 为什么不用 `window.confirm`：iOS 上它的按钮文案只能是"确定/取消"，
 * 而删除照片必须让用户看清"是进回收站，不是永久删除"——
 * 这直接决定了用户按下去时有多犹豫。自定义弹窗才能把这句话写进按钮。
 */
export function confirmDialog(o: ConfirmOptions): Promise<boolean> {
  // 前一个还没关（连点两次）→ 先把前一个当作取消，避免 Promise 泄漏
  resolver?.(false);
  confirmState.opts = {
    okText: "确定",
    cancelText: "取消",
    danger: false,
    note: "",
    ...o,
  };
  confirmState.open = true;
  confirmState.busy = false;
  return new Promise<boolean>((resolve) => {
    resolver = resolve;
  });
}

/** 组件调用：用户点了某个按钮。 */
export function settleConfirm(v: boolean): void {
  if (confirmState.busy) return;
  confirmState.open = false;
  const r = resolver;
  resolver = null;
  r?.(v);
}

/** 调用方在异步操作（如删除请求）期间标记忙碌，防止连点。 */
export function setConfirmBusy(v: boolean): void {
  confirmState.busy = v;
}
