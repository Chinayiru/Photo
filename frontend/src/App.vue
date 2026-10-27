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
import SelectionBar from "./components/SelectionBar.vue";
import { ApiError, onAuthLost } from "./api/client";
import { closeEvents, connectEvents, reconnectEvents } from "./api/events";
import { authStore, guestInviteFromLocation, isPaired, probe, redeemGuest } from "./stores/auth";
import { isGuest } from "./api/storage";
import { toastError, toastOk } from "./stores/toast";
import { exit as exitSelect, selectionStore } from "./stores/selection";
import { openFolder } from "./stores/timeline";
import { startEvents, stopEvents } from "./stores/system";
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
 * 游客身份：只读 + 只能往"游客上传"里加。
 * 界面层面收起「我的」（那里全是主人操作：设备管理、邀请管理、退出配对）。
 */
const guest = ref(isGuest());
const tabs = computed(() => (guest.value ? TABS.filter((t) => t.key !== "settings") : TABS));

/** 正在用邀请链接换令牌：这期间别闪配对页，否则用户会以为链接失效了。 */
const redeeming = ref(false);

/**
 * 邀请链接兑换失败的提示（空串 = 没有失败）。
 *
 * ★ 为什么要有这个态：兑换失败时 `redeeming` 变回 false，模板就掉回
 *   `<PairView>` —— 用户拿着一条**过期/被撤销**的邀请链接，却看到"请输入
 *   配对码"，会以为要问主人要那 6 位数字。正确表现是"这条链接不能用了"
 *   并且**不再给配对入口**（他本来就不是主人）。
 */
const guestFail = ref("");

/**
 * 处理地址栏里的游客邀请。
 *
 * ★ 与配对码同一条通路（`#guest=`），但**必须先兑换再判 `paired`**：
 *   `paired` 由 `isPaired()` 算，游客兑换成功后令牌才落盘，
 *   顺序反了会先渲染配对页再跳走，手机上看起来像闪了一下。
 */
async function handleGuestLink(): Promise<void> {
  const invite = guestInviteFromLocation();
  if (!invite) return;
  redeeming.value = true;
  guestFail.value = "";
  try {
    const r = await redeemGuest(invite);
    guest.value = true;
    paired.value = true;
    startEvents();
    connectEvents();
    void probe();
    toastOkGuest(r.label);
  } catch (exc) {
    guestFail.value = guestErrorMessage(exc);
    toastError(guestFail.value);
  } finally {
    redeeming.value = false;
    // 成功才把码从地址栏抹掉：失败时留着用户才能核对/重试（也才能截图给主人看）。
    if (!guestFail.value) {
      const url = window.location.pathname + window.location.search;
      window.history.replaceState(null, "", url);
    }
  }
}

function toastOkGuest(label: string): void {
  toastOk(label ? `已进入「${label}」的相册` : "已进入相册（游客）");
}

function guestErrorMessage(exc: unknown): string {
  if (exc instanceof ApiError) {
    // 无痕模式常见 `SecurityError`；限额/过期是服务端的 E102x。
    if (exc.code === "E1021") return "这条邀请已过期，请让主人重新发一条";
    if (exc.code === "E1022") return "这条邀请已经被用过了（一次性）";
    if (exc.code === "E1023") return "主人已收回这条邀请";
    if (exc.code === "E1020") return "邀请链接不完整，请让主人重新复制";
    return exc.message || "打不开这条邀请链接";
  }
  return "打不开这条邀请链接";
}

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

/** 多选中：底部页签栏换成操作条。 */
const selActive = computed(() => selectionStore.state.active);

/**
 * 切 Tab。
 *
 * ★ 必须**先退出多选**：选中集是跨视图的（时间线与搜索页共用一套），
 *   带着 5 个选中项从时间线切到「搜索」，操作条会一直挂着、数字还在，
 *   用户会以为搜索页里那 5 张也被选上了 —— 而它们根本不在这一页。
 */
function switchTab(k: Tab): void {
  exitSelect();
  tab.value = k;
}

/** 配对成功 / 退出配对时切换根视图。 */
watch(
  () => [authStore.state.conn, authStore.state.deviceMismatch] as const,
  () => {
    const next = isPaired() && !authStore.state.deviceMismatch;
    const justPaired = next && !paired.value;
    paired.value = next;

    // ★ 重新配对后必须建流。onMounted 里那次连接是在"还没配对"时
    //   调用的（直接返回），此后没有任何地方会再连 —— 用户配对成功了
    //   却永远收不到推送，界面上也看不出区别，只会觉得"新照片不出现"。
    if (justPaired) {
      startEvents();
      connectEvents();
    } else if (!next) {
      stopEvents();
      closeEvents();
    }
  },
);

let onVisible: (() => void) | null = null;

/**
 * 跳到某个文件夹（由搜索页广播）。
 *
 * `detail` 是相对根的目录路径；空串/非字符串一律忽略 —— 宁可什么都不做，
 * 也不要切到一个"根层"把用户从搜索结果里莫名其妙踢走。
 */
function onGotoFolder(e: Event): void {
  const dir = (e as CustomEvent<unknown>).detail;
  if (typeof dir !== "string" || dir === "") return;
  openFolder(dir);
  tab.value = "timeline";
}

onMounted(() => {
  // 搜索页的「只看这个文件夹」→ 切到照片 Tab 并下钻到那个目录。
  // ★ 用 window 事件而不是共享 store：Tab 的选择权在 App 自己手里，
  //   子视图不该反向 import App 的 ref（会绕成环）。
  window.addEventListener("pb:goto-folder", onGotoFolder as EventListener);
  // token 失效（401 E2002 / 403 E2004）→ client 会回调这里，踢回配对页
  onAuthLost(() => {
    // ★ 游客的凭证过期/被踢，不能掉回配对页：他手里没有、也不该有配对码。
    //   给一句能照做的提示（"让主人重发邀请"），比一个他填不了的输入框有用。
    if (isGuest()) {
      guestFail.value = "游客会话已结束，请让主人重新发一条邀请链接";
      stopEvents();
      closeEvents();
      return;
    }
    paired.value = false;
    authStore.state.conn = "UNAUTHORIZED";
    // ★ 鉴权都没了就别再维持 SSE 长连接：服务端会一直重试推送、
    //   浏览器会一直重连，而用户看到的是配对页，白耗电。
    stopEvents();
    closeEvents();
  });

  // SSE 事件流（AC-2.15 新照片自动出现 / AC-2.16 扫描进度推送）。
  // startEvents() 是幂等的，且在没有配对时 connectEvents() 会直接返回。
  startEvents();
  if (isPaired()) {
    void probe();
    requeueInterrupted();
    connectEvents();
  }

  // 切回前台探活一次：手机锁屏/切 App 回来，局域网 IP 可能已经变了
  void handleGuestLink();

  onVisible = () => {
    if (document.visibilityState !== "visible") return;
    if (isPaired()) void probe();
    // ★ 后台时浏览器会冻结 SSE（省电），切回来必须重连 —— 否则
    //   用户切出去再回来，界面就再也不更新了，且没有任何提示。
    if (isPaired()) reconnectEvents();
  };
  document.addEventListener("visibilitychange", onVisible);
});

onBeforeUnmount(() => {
  window.removeEventListener("pb:goto-folder", onGotoFolder as EventListener);
  if (onVisible) document.removeEventListener("visibilitychange", onVisible);
  stopEvents();
  closeEvents();
});
</script>

<template>
  <!-- 邀请链接没换到会话：明确说"这条链接不能用了"，不给配对码入口 -->
  <section v-if="!paired && !redeeming && guestFail" class="linkfail">
    <h2>打不开这条邀请链接</h2>
    <p class="lf-msg">{{ guestFail }}</p>
    <p class="lf-hint">邀请链接是一次性的，过期或被主人收回后就不能再用。请让主人重新生成一条。</p>
  </section>
  <PairView v-else-if="!paired && !redeeming" />

  <template v-else>
    <StatusBar :title="title" />

    <div v-if="guest" class="guestbar" role="status">
      游客身份 · 只能查看与上传，不能删除或修改
    </div>

    <main class="body">
      <KeepAlive>
        <component :is="currentView" />
      </KeepAlive>
    </main>

    <nav v-if="!selActive" class="tabbar">
      <button
        v-for="t in tabs"
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

    <!--
      多选时用操作条**替换**页签栏（而不是叠在上面）：底部只有一条横带，
      叠上去会盖住「上传/我的」，用户想切 Tab 就得先退出多选。
    -->
    <SelectionBar v-else />
  </template>

  <ViewerView />
  <ConfirmDialog />
  <Toast />
</template>

<style scoped>
.guestbar {
  flex: 0 0 auto;
  padding: 6px 12px;
  font-size: 12px;
  line-height: 1.4;
  color: #7a4b00;
  background: #fff4d6;
  border-bottom: 1px solid #f0dcae;
}

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

/* 邀请链接失败态：与配对页区分开，别让人以为要输配对码 */
.linkfail {
  max-width: 30rem;
  margin: 18vh auto 0;
  padding: 0 1.25rem;
  text-align: center;
}
.linkfail h2 { font-size: 1.05rem; margin-bottom: .6rem; }
.lf-msg { font-size: .95rem; line-height: 1.5; margin-bottom: .5rem; }
.lf-hint { font-size: .82rem; color: var(--muted, #8a8a8a); line-height: 1.5; }
</style>
