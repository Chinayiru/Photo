/**
 * 轻量 Toast —— 替代 alert()。
 *
 * 为什么不用 alert：iOS 的 alert 会阻塞 JS 线程，上传进行中弹一次
 * 会让所有 XHR 的进度回调停摆；而且 alert 的样式无法控制，在深色主题
 * 下是一块刺眼的白。
 */

import { reactive } from "vue";

export type ToastKind = "info" | "ok" | "warn" | "error";

export interface ToastItem {
  id: number;
  kind: ToastKind;
  text: string;
  /** 毫秒；0 表示不自动消失（用户要读完整句话，如错误详情）。 */
  ttl: number;
}

const state = reactive({
  items: [] as ToastItem[],
});

export const toasts = state;

let seq = 0;

export function toast(text: string, kind: ToastKind = "info", ttl = 2600): number {
  const id = ++seq;
  state.items.push({ id, kind, text, ttl });
  // 最多同时 3 条，多的挤掉最旧的（手机上叠满会盖住内容）
  while (state.items.length > 3) state.items.shift();
  if (ttl > 0) window.setTimeout(() => dismiss(id), ttl);
  return id;
}

export function dismiss(id: number): void {
  const i = state.items.findIndex((x) => x.id === id);
  if (i >= 0) state.items.splice(i, 1);
}

export const toastOk = (t: string): number => toast(t, "ok");
export const toastWarn = (t: string): number => toast(t, "warn", 4000);
export const toastError = (t: string): number => toast(t, "error", 5000);

/**
 * 把任意异常变成一句能给用户看的话。
 *
 * 优先用后端的 `user_hint`（`backend/app/errors.py` 里每个错误码都带了
 * 一句面向用户的中文），其次 message，最后才是 code。
 */
export function toastError2(e: unknown): number {
  const msg = e instanceof Error ? e.message : String(e);
  return toastError(msg || "操作失败");
}
