<script setup lang="ts">
/**
 * 多选操作条 —— 选中若干张照片后**替换掉底部页签栏**。
 *
 * ## 为什么替换而不是叠在上面
 *
 * 手机上底部只有一条能放东西的横带。叠上去会盖住"上传/我的"页签，
 * 用户想切 Tab 就必须先退出多选；替换则让"当前处于一个特殊模式"这件事
 * 一眼可见，退出多选页签自然回来。
 *
 * ## 为什么删除要弹确认框
 *
 * 删除走的是 Windows 回收站（`backend/app/handlers/browse.py:731` 只调
 * `send2trash`，代码里不存在永久删除路径），所以它不是灾难；
 * 但一次删 20 张之后要去资源管理器里挑回来同样很麻烦 —— 值一次确认。
 *
 * ## 为什么失败的那几张不退出多选
 *
 * `deleteSelected()` 会把 `failed` 里的 id **留在选中态**，这条操作条
 * 于是立刻显示"还剩 N 张"，用户可以直接重试。全部成功才自动退出。
 */
import { computed } from "vue";
import { ApiError } from "../api/client";
import { confirmDialog, setConfirmBusy } from "../stores/confirm";
import {
  count,
  deleteErrorMessage,
  deleteSelected,
  deleteSummary,
  exit,
  selectionStore,
} from "../stores/selection";
import { toastError } from "../stores/toast";

const n = count;
const busy = computed(() => selectionStore.state.busy);

async function onDelete(): Promise<void> {
  if (busy.value) return;
  const total = n.value;
  if (total === 0) {
    // 一张没选就删 = 让用户对着确认框想"我刚才选了啥"。
    // 直接给一句话，比弹一个删 0 张的框诚实。
    toastError("还没有选中照片");
    return;
  }

  const ok = await confirmDialog({
    title: `删除 ${total} 张照片？`,
    message:
      `这些照片会被移到电脑的「回收站」，不会永久删除。\n`
      + `在电脑上打开回收站就能还原。`,
    okText: "移到回收站",
    cancelText: "取消",
    danger: true,
  });
  if (!ok) return;

  selectionStore.state.busy = true;
  setConfirmBusy(true);
  try {
    const r = await deleteSelected();
    deleteSummary(r);
  } catch (e) {
    // E1015（电脑端没开删除开关）是最常见的一种，文案单独给
    const msg = deleteErrorMessage(e);
    if (e instanceof ApiError && e.code !== "E1015") {
      console.warn("删除失败", e.code, e.detail);
    }
    toastError(msg);
  } finally {
    selectionStore.state.busy = false;
    setConfirmBusy(false);
  }
}
</script>

<template>
  <nav class="selbar">
    <button class="sb-btn" type="button" @click="exit">退出</button>

    <span class="sb-count" aria-live="polite">
      已选 <b>{{ n }}</b> 张
    </span>

    <button
      class="sb-btn danger"
      type="button"
      :disabled="busy"
      @click="onDelete"
    >
      <span v-if="busy" class="spinner sb-spin" />
      {{ busy ? "删除中…" : "移到回收站" }}
    </button>
  </nav>
</template>

<style scoped>
/*
 * 高度与 `.tabbar` 一致（`--tabbar-h` + 安全区），这样替换时
 * 下面的照片网格不会跳一下。
 */
.selbar {
  flex: 0 0 auto;
  height: calc(var(--tabbar-h) + var(--safe-bottom));
  padding: 0 10px;
  padding-bottom: var(--safe-bottom);
  display: flex;
  align-items: center;
  gap: 10px;
  background: var(--bg-1);
  border-top: 1px solid var(--line);
  position: relative;
  z-index: 30;
}

.sb-btn {
  flex: 0 0 auto;
  padding: 7px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  background: var(--bg-2);
  color: var(--fg-1);
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 6px;
}

.sb-btn:active {
  transform: scale(0.97);
}

.sb-btn.danger {
  margin-left: auto;
  border-color: transparent;
  background: var(--bad-dim);
  color: var(--bad);
  font-weight: 600;
}

.sb-btn:disabled {
  opacity: 0.6;
}

.sb-count {
  font-size: 13px;
  color: var(--fg-2);
}

.sb-count b {
  color: var(--fg-1);
  /* 数字变化时不要带着整行抖动 */
  font-variant-numeric: tabular-nums;
}

.sb-spin {
  width: 14px;
  height: 14px;
  border-width: 2px;
}
</style>
