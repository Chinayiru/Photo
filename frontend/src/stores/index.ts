/**
 * 自写 store 工具 —— 替代 Pinia（docs/03 §5.7.1）。
 *
 * 为什么不用 Pinia：只有 4 个 Tab、5 个 store，Pinia 带来的 devtools 与
 * 插件体系用不上，却要多 10 KB 左右的运行时；而 `reactive()` 已经够用。
 *
 * 用法：
 * ```ts
 * const s = createStore({ count: 0 });
 * s.state.count++;            // 响应式
 * s.reset();                  // 回到初始值
 * ```
 */

import { reactive, readonly } from "vue";

export interface Store<T extends object> {
  /** 可写的响应式状态。 */
  state: T;
  /** 只读视图（组件里用这个，避免任意组件直接改别的 store）。 */
  readonly state$: Readonly<T>;
  /** 恢复初始值。 */
  reset: () => void;
}

export function createStore<T extends object>(initial: T): Store<T> {
  // ★ 必须深拷贝：直接把 initial 交给 reactive() 后，reset 时的
  //   structuredClone(initial) 会克隆"已经被改过的对象"，reset 变成空操作。
  const pristine = structuredClone(toPlain(initial));
  const state = reactive(toPlain(initial)) as T;
  const reset = (): void => {
    Object.assign(state, structuredClone(pristine));
  };
  return {
    state,
    // readonly() 只挡顶层赋值，够用且零成本
    state$: readonly(state) as Readonly<T>,
    reset,
  };
}

/**
 * 深拷贝，但**只递归普通对象与数组**，其他值（Date/Map/Blob/函数…）
 * 按引用保留。
 *
 * 为什么不用 `structuredClone`：它不能克隆 Proxy（`reactive()` 返回的
 * 就是 Proxy），会抛 `DataCloneError`；也不能克隆函数。
 * 为什么不用 JSON 往返：upload store 的初始值里有 `Blob`，
 * JSON 往返会把它变成 `{}` —— 队列里的文件就丢了。
 */
function toPlain<T extends object>(o: T): T {
  if (Array.isArray(o)) return o.map((v) => clone(v)) as unknown as T;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(o)) out[k] = clone(v);
  return out as T;
}

function clone(v: unknown): unknown {
  if (v === null || typeof v !== "object") return v;
  if (Array.isArray(v)) return v.map((x) => clone(x));
  const proto = Object.getPrototypeOf(v);
  // 只深拷贝纯对象（Object.create(null) 与 {}），其他一律按引用传
  if (proto !== Object.prototype && proto !== null) return v;
  const out: Record<string, unknown> = {};
  for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
    out[k] = clone(val);
  }
  return out;
}
