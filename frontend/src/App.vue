<script setup lang="ts">
/**
 * 应用外壳。
 *
 * ★ 不用 vue-router（任务硬约束）：4 个 Tab 就是一个 `ref`。
 *   这个 App 里没有"深链接"需求 —— 照片库的唯一入口是时间线，
 *   浏览器地址栏在手机上几乎不可见，为了 4 个平级页面引入一整套
 *   路由（含 history 管理）不划算。
 *
 * 但"返回手势"还是要能用：查看器打开时压一条 history（见 ViewerView），
 * 用户在安卓上左滑返回时关掉查看器，而不是直接退出页面。
 */
import { computed, onMounted, onBeforeUnmount, ref, watch } from "vue";import StatusBar from "./components/StatusBar.vue";
import Toast from "./components/Toast.vue";
import ConfirmDialog from "./components/ConfirmDialog.vue";
import PairView from "./views/PairView.vue";
import TimelineView from "./views/TimelineView.vue";
import SearchView from "./views/SearchView.vue";
import UploadView from "./views/UploadView.vue";
import SettingsView from "./views/SettingsView.vue";
import ViewerView from "./views/ViewerView.vue";
import { onAuthLost } from "./api/client";
import { authStore, isPaired, probe } from "./stores/auth";
import { requeueInterrupted, uploadStore } from "./stores/upload";

type Tab = "timeline" | "search" | "upload" | "settings";

const TABS: { key: Tab; label: string; icon: string }[] = [
  { key: "timeline", label: "照片", icon: "🖼" },
  { key: "search", label: "搜索", icon: "🔍" },
  { key: "upload", label: "上传", icon: "⬆" },
  { key: "settings", label: "我的", icon: "☰" },
];

const tab = ref<Tab>("timeline");

/**
 * 用 `<KeepAlive>` 缓存 Tab 面板：切到「上传」再切回来时，
 * 时间线的滚动位置必须还在 —— 重新挂载会滚回顶部，
 * 用户翻到第 400 张再切一下 Tab 就全丢了，这体验很差。
 */
const VIEWS = {
  timeline: TimelineView,
  search: SearchView,
  upload: UploadView,
  settings: SettingsView,
} as const;

const currentView = computed(() => VIEWS[tab.value]);

const paired = ref(isPaired());
const title = computed(() => TABS.find((t) => t.key === tab.value)?.label ?? "");

/** 上传中有任务时，Tab 上挂个小圆点，切走了也知道还在传。 */
const uploading = computed(() => uploadStore.state.active > 0);

function switchTab(k: Tab): void {
  tab.value = k;
}

/** 配对成功 / 退出配对时切换根视图。 */
watch(
  () => [authStore.state.conn, authStore.state.deviceMismatch] as const,
  () => {
    paired.value = isPaired() && !authStore.state.deviceMismatch;
  },
);

let onVisible: (() => void) | null = null;

onMounted(() => {
  // token 失效（401 E2002 / 403 E2004）→ client 会回调这里，踢回配对页
  onAuthLost(() => {
    paired.value = false;
    authStore.state.conn = "UNAUTHORIZED";
  });

  if (isPaired()) {
    void probe();
    requeueInterrupted();
  }

  // 切回前台探活一次：手机锁屏/切 App 回来，局域网 IP 可能已经变了
  onVisible = () => {
    if (document.visibilityState !== "visible") return;
    if (isPaired()) void probe();
  };
  document.addEventListener("visibilitychange", onVisible);
});

onBeforeUnmount(() => {
  if (onVisible) document.removeEventListener("visibilitychange", onVisible);
});
</script>

<template>
  <PairView v-if="!paired" />

  <template v-else>
    <StatusBar :title="title" />

    <main class="body">
      <KeepAlive>
        <component :is="currentView" />
      </KeepAlive>
    </main>

    <nav class="tabbar">
      <button
        v-for="t in TABS"
        :key="t.key"
        class="tab"
        type="button"
        :class="{ on: tab === t.key }"
        :aria-current="tab === t.key ? 'page' : undefined"
        @click="switchTab(t.key)"
      >
        <span class="tab-i" aria-hidden="true">
          {{ t.icon }}
          <span v-if="t.key === 'upload' && uploading" class="dot" />
        </span>
        <span class="tab-t">{{ t.label }}</span>
      </button>
    </nav>
  </template>

  <ViewerView />
  <ConfirmDialog />
  <Toast />
</template>

<style scoped>
.body {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  position: relative;
  overflow: hidden;
}

.tabbar {
  flex: 0 0 auto;
  height: calc(var(--tabbar-h) + var(--safe-bottom));
  padding-bottom: var(--safe-bottom);
  display: flex;
  background: var(--bg-1);
  border-top: 1px solid var(--line);
  /* 底部固定，内容从下面穿过时不会闪 */
  position: relative;
  z-index: 30;
}

.tab {
  flex: 1 1 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  color: var(--fg-3);
  transition: color var(--dur) var(--ease);
}

.tab.on {
  color: var(--accent);
}

.tab:active {
  background: var(--bg-2);
}

.tab-i {
  position: relative;
  font-size: 19px;
  line-height: 1;
}

.tab-t {
  font-size: 10px;
}

.dot {
  position: absolute;
  top: -2px;
  right: -6px;
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 0 2px var(--bg-1);
}
</style>
