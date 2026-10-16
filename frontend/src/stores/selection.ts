/**
 * 多选（长按进入）与批量删除。
 *
 * ## 为什么要有这个 store
 *
 * `PhotoGrid.vue` 早就声明了 `selected?: Set<number>` prop，
 * `PhotoCell.vue` 也早就画好了 `.selected` 描边与 ✓ 角标，但两个调用点
 * （`TimelineView.vue`、`SearchView.vue`）**都没传值** —— 这套 UI 一直是
 * 死代码。而 `docs/01-需求规格说明书.md` 的 US-12 明确写着"长按选中，
 * 点删除"，批量删除的后端接口 `POST /api/items/delete` 也是**一次最多
 * 200 个 id**（`backend/app/handlers/browse.py`），本来就是为多选设计的。
 *
 * ## 为什么 ids 是数组而不是 Set
 *
 * `stores/index.ts` 的深拷贝只认**普通对象与数组**：`Set` 的原型不是
 * `Object.prototype`，会被 `clone()` 按引用直接返回，于是 `pristine` 与
 * `state` 指向**同一个 Set** —— `reset()` 变成空操作。数组虽然查找是
 * O(n)，但选中数上限 200，一帧内最多几百次比较，代价可以忽略。
 *
 * ## 为什么用全局 store 而不是视图内 ref
 *
 * 操作条要显示在**底部 Tab 栏的位置**（选中时替换掉 Tab 栏），而 Tab 栏
 * 在 `App.vue`。把选中集放在 store 里，视图只管渲染格子、`App.vue` 只管
 * 渲染操作条，两边不需要互相引用。
 */

import { computed } from "vue";
import { ApiError, api } from "../api/client";
import type { DeleteResponse } from "../api/types";
import { createStore } from "./index";
import { toastError, toastOk, toastWarn } from "./toast";

/**
 * 一次最多能选多少张。
 *
 * ★ 与后端硬约束保持一致：`POST /api/items/delete` 的 `ids` 超过 200
 * 一律 400（`backend/app/handlers/browse.py`：一次删太多会让"部分成功"
 * 变得难以解释，也让审计日志失去可读性）。前端不拦的话，用户选了 300 张、
 * 确认、然后收到一个 400 —— 那是最糟的失败方式：动作已经承诺了。
 */
export const MAX_SELECT = 200;

const initial = {
  active: false,
  ids: [] as number[],
  busy: false,
};

export const selectionStore = createStore(initial);

/** 选中的 id 集合（给 `PhotoGrid` 的 `selected` prop 用）。 */
export const selectedSet = computed<Set<number>>(
  () => new Set(selectionStore.state.ids),
);

export const count = computed<number>(() => selectionStore.state.ids.length);

/** 进入多选，并把被长按的那张选上（不传 id 表示"空手进入"）。 */
export function enter(id?: number): void {
  const s = selectionStore.state;
  s.active = true;
  if (id !== undefined && !s.ids.includes(id)) s.ids = [...s.ids, id];
}

export function toggle(id: number): void {
  const s = selectionStore.state;
  if (s.ids.includes(id)) {
    s.ids = s.ids.filter((x) => x !== id);
    return;
  }
  if (s.ids.length >= MAX_SELECT) {
    toastWarn(`一次最多选 ${MAX_SELECT} 张`);
    return;
  }
  s.ids = [...s.ids, id];
}

export function exit(): void {
  const s = selectionStore.state;
  s.active = false;
  s.ids = [];
  s.busy = false;
}

/**
 * 全选。
 *
 * ★ 列表由调用方传进来，而不是 store 自己缓存一份：时间线与搜索页各有
 * 一份列表，缓存一份就意味着"照着搜索结果全选、实际选中时间线"这类
 * 串台；而且两份列表都可能被分页与删除改动，缓存必然过期。
 */
export function selectAll(ids: number[]): void {
  if (ids.length > MAX_SELECT) toastWarn(`一次最多选 ${MAX_SELECT} 张`);
  selectionStore.state.ids = ids.slice(0, MAX_SELECT);
}

/* ------------------------------------------------------------------ *
 * 删除后把格子从列表里摘掉
 * ------------------------------------------------------------------ */

/**
 * 注册"移除一项"的回调，返回注销函数。
 *
 * 时间线与搜索各有一份列表（搜索页用的是自己的 `items` ref，不是
 * timeline store），所以留一个可注册的钩子，由**当前显示的**视图挂上去。
 * 用钩子而不是让 store 直接 import 两个列表，是为了不让 store 依赖视图层。
 */
let remover: ((id: number) => void) | null = null;

export function onRemoved(fn: (id: number) => void): () => void {
  remover = fn;
  return () => {
    if (remover === fn) remover = null;
  };
}

/**
 * 删除选中的照片（进电脑的系统回收站）。
 *
 * 不弹确认框 —— 确认框由操作条弹，因为它知道用户点的是哪个按钮、
 * 要不要显示"正在删除"。
 */
export async function deleteSelected(): Promise<DeleteResponse> {
  const s = selectionStore.state;
  const ids = s.ids.slice();
  if (ids.length === 0) return { deleted: 0, failed: [] };

  const r = await api.post<DeleteResponse>("/api/items/delete", { ids }, {
    retry: 0,
    timeoutMs: 60_000,
  });

  // 后端返回的是**数量**而不是 id 列表（`browse.py:782`），所以"哪几张
  // 成功"只能按"没出现在 failed 里"来推断。这不完美，但比"删了 20 张
  // 只摘掉 18 个格子"强。
  const failedIds = new Set((r.failed ?? []).map((f) => f.id));
  for (const id of ids) {
    if (!failedIds.has(id)) remover?.(id);
  }

  // ★ 失败的那几张**继续留在选中态**：用户一眼看到"剩下的就是没删掉的"，
  //   可以直接再试（或取消）。全部成功才退出多选。
  const keep = ids.filter((id) => failedIds.has(id));
  if (keep.length === 0) exit();
  else s.ids = keep;

  return r;
}

/** 把异常变成一句能给用户看的话（各调用点文案一致）。 */
export function deleteErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.code === "E1015") {
      return "电脑端没开「允许手机删除」，请到电脑上打开这个开关";
    }
    return e.userHint || e.friendly || "删除失败";
  }
  return e instanceof Error ? e.message : "删除失败";
}

/** 删除结果的一句话总结。 */
export function deleteSummary(r: DeleteResponse): void {
  if (r.deleted > 0 && (r.failed?.length ?? 0) === 0) {
    toastOk(`已把 ${r.deleted} 张移到回收站`);
    return;
  }
  if (r.deleted > 0) {
    toastWarn(`已移走 ${r.deleted} 张，${r.failed.length} 张失败`);
    return;
  }
  toastError("一张都没删掉");
}
