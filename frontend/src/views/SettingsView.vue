<script setup lang="ts">
/**
 * 设置 Tab。
 *
 * 这里显示的都是"用户想确认一切正常"的信息：电脑上到底认了多少张照片、
 * 缩略图还在不在生成、磁盘还剩多少。加上两个动作：重新扫描、退出配对。
 *
 * 退出配对要二次确认 —— 手机上重新输入 IP 和 6 位码并不轻松，
 * 误触的代价比多点一次大。
 */
import { computed, onActivated, onMounted } from "vue";
import { confirmDialog, setConfirmBusy } from "../stores/confirm";
import { ApiError } from "../api/client";
import { getClientName, getDeviceId, getToken } from "../api/storage";
import { authStore, currentBase, unpair } from "../stores/auth";
import {
  diskFree,
  diskTotal,
  fmtBytes,
  fmtUptime,
  indexText,
  pendingThumbs,
  photoCount,
  refresh,
  rescan,
  systemStore,
} from "../stores/system";
import { clearTimeline } from "../stores/timeline";
import { toastError, toastOk, toastWarn } from "../stores/toast";

const s = systemStore.state;
const info = computed(() => s.info);
const stats = computed(() => s.stats);
const app = computed(() => info.value?.app);
const server = computed(() => info.value?.server);
const roots = computed(() => info.value?.roots ?? []);
const limits = computed(() => info.value?.limits);
const thumbs = computed(() => info.value?.thumbs);
const db = computed(() => stats.value?.db);

/** 磁盘占用比例，用来画一条细进度条。 */
const diskUsedPct = computed(() => {
  const free = diskFree.value;
  const total = diskTotal.value;
  if (free === null || !total) return 0;
  return Math.max(0, Math.min(100, ((total - free) / total) * 100));
});

const diskClass = computed(() => {
  const free = diskFree.value;
  if (free === null) return "";
  if (free < 1024 ** 3) return "bad";
  if (free < 5 * 1024 ** 3) return "warn";
  return "ok";
});

/** 缩略图缓存大小。 */
const thumbCache = computed(() => {
  const t = thumbs.value;
  if (!t) return "—";
  return `${fmtBytes(t.cache_bytes)} · ${t.cache_files} 个文件`;
});

const version = computed(() => app.value?.version || authStore.state.version || "—");
const scanning = computed(() => s.scanning || info.value?.index.state === "scanning");

onMounted(() => {
  if (!s.lastLoadedAt) void refresh();
});

onActivated(() => {
  // 每次切回设置页刷一次：用户来这里就是想看"现在怎么样了"
  void refresh();
});

async function onRescan(): Promise<void> {
  try {
    await rescan();
    toastOk("已开始扫描，照片多的话要等一会儿");
  } catch (e) {
    if (e instanceof ApiError && e.code === "E3012") {
      toastWarn("电脑上已经有一个扫描在进行中了");
    } else if (e instanceof ApiError && e.message === "刚刚已经触发过了") {
      toastWarn("刚刚已经触发过了，稍等一下");
    } else {
      toastError(e instanceof Error ? e.message : "扫描失败");
    }
  }
}

async function onUnpair(): Promise<void> {
  const name = authStore.state.deviceName || info.value?.device.device_name || "这台电脑";
  const ok = await confirmDialog({
    title: "退出配对？",
    message: `退出后需要重新输入电脑地址和配对码才能连接「${name}」。\n\n电脑上的照片不会被删除。`,
    okText: "退出配对",
    danger: true,
  });
  if (!ok) return;
  setConfirmBusy(true);
  try {
    unpair();
    clearTimeline();
    systemStore.reset();
    toastOk("已退出配对");
  } finally {
    setConfirmBusy(false);
  }
}

function copyBase(): void {
  const b = currentBase();
  void navigator.clipboard?.writeText(b).then(
    () => toastOk("已复制"),
    () => toastWarn("复制失败"),
  );
}

/** 配对信息（本地存的，用来对账）。 */
const pairing = computed(() => {
  const token = getToken();
  return {
    base: currentBase() || "—",
    deviceId: getDeviceId() || "—",
    client: getClientName() || "—",
    // 只显示头尾，避免截图时把完整 token 泄出去
    token: token ? `${token.slice(0, 6)}…${token.slice(-4)}` : "—",
  };
});
</script>

<template>
  <div class="mine scroll">
    <!-- 设备卡片 -->
    <div class="card hero-card">
      <div class="hero-top">
        <div class="hero-ico" aria-hidden="true">💻</div>
        <div class="hero-txt">
          <div class="hero-name ellipsis">
            {{ info?.device.device_name || authStore.state.deviceName || "未连接" }}
          </div>
          <div class="hero-sub">
            PhotoBridge {{ version }}
            <template v-if="app?.build"> · {{ app.build }}</template>
          </div>
        </div>
        <span class="pill" :class="scanning ? 'busy' : 'ok'">
          {{ scanning ? "扫描中" : "就绪" }}
        </span>
      </div>

      <div v-if="s.error" class="err">{{ s.error }}</div>

      <dl class="kv">
        <div class="r">
          <dt>照片</dt>
          <dd>{{ photoCount }} 张</dd>
        </div>
        <div class="r">
          <dt>缩略图</dt>
          <dd>
            待生成 {{ pendingThumbs }}
            <span v-if="stats" class="dim">· 已就绪 {{ stats.thumbs.ok }}</span>
            <span v-if="stats && stats.thumbs.failed > 0" class="bad">
              · 失败 {{ stats.thumbs.failed }}
            </span>
          </dd>
        </div>
        <div class="r">
          <dt>磁盘剩余</dt>
          <dd :class="diskClass">
            {{ fmtBytes(diskFree) }}
            <span v-if="diskTotal" class="dim">/ {{ fmtBytes(diskTotal) }}</span>
          </dd>
        </div>
        <div class="r">
          <dt>索引</dt>
          <dd>{{ indexText }}</dd>
        </div>
        <div v-if="app" class="r">
          <dt>已运行</dt>
          <dd>{{ fmtUptime(app.uptime_sec) }}</dd>
        </div>
      </dl>

      <div v-if="diskTotal" class="diskbar" :title="`已用 ${diskUsedPct.toFixed(1)}%`">
        <div class="diskbar-in" :class="diskClass" :style="{ width: `${diskUsedPct}%` }" />
      </div>
    </div>

    <!-- 照片目录 -->
    <div v-if="roots.length" class="card">
      <h3 class="sec">照片目录</h3>
      <div v-for="r in roots" :key="r.id" class="root">
        <div class="root-top">
          <span class="root-label ellipsis">{{ r.label || r.id }}</span>
          <span class="root-count">{{ r.count }} 张</span>
        </div>
        <div class="root-path mono ellipsis">{{ r.path }}</div>
        <div class="root-meta">
          <span v-if="r.mode === 'ro'" class="tag">只读</span>
          <span v-if="!r.enabled" class="tag warn">已停用</span>
          <span class="dim">剩余 {{ fmtBytes(r.disk_free) }}</span>
        </div>
      </div>
    </div>

    <!-- 技术细节（折叠，默认不打扰） -->
    <details class="card tech">
      <summary>技术信息</summary>
      <dl class="kv">
        <div class="r"><dt>服务地址</dt><dd class="mono">{{ pairing.base }}</dd></div>
        <div v-if="server?.lan_ips?.length" class="r">
          <dt>局域网 IP</dt>
          <dd class="mono">{{ server.lan_ips.join(", ") }}</dd>
        </div>
        <div class="r"><dt>监听端口</dt><dd class="mono">{{ server?.port ?? "—" }}</dd></div>
        <div class="r"><dt>设备 ID</dt><dd class="mono small">{{ pairing.deviceId }}</dd></div>
        <div class="r"><dt>本机标识</dt><dd>{{ pairing.client }}</dd></div>
        <div class="r"><dt>令牌</dt><dd class="mono">{{ pairing.token }}</dd></div>
        <div v-if="limits" class="r">
          <dt>上传限制</dt>
          <dd>单文件 ≤ {{ fmtBytes(limits.max_file_bytes) }}，分片 {{ fmtBytes(limits.chunk_size) }}</dd>
        </div>
        <div v-if="thumbs" class="r">
          <dt>缩略图缓存</dt>
          <dd>{{ thumbCache }}</dd>
        </div>
        <div v-if="db" class="r">
          <dt>数据库</dt>
          <dd>{{ fmtBytes(db.size_bytes) }} · {{ db.page_count }} 页</dd>
        </div>
        <div v-if="stats" class="r">
          <dt>未找到</dt>
          <dd :class="stats.media.missing > 0 ? 'warn' : ''">
            {{ stats.media.missing }} 个文件已不在磁盘上
          </dd>
        </div>
      </dl>
    </details>

    <!-- 动作 -->
    <div class="card acts">
      <button
        class="row-btn"
        type="button"
        :disabled="scanning"
        @click="onRescan"
      >
        <span class="row-ico" aria-hidden="true">🔄</span>
        <span class="row-txt">
          <span class="row-title">{{ scanning ? "正在扫描…" : "重新扫描照片" }}</span>
          <span class="row-sub">电脑上新增或删除了照片时用</span>
        </span>
        <span v-if="scanning" class="spinner" />
      </button>

      <button class="row-btn" type="button" @click="refresh">
        <span class="row-ico" aria-hidden="true">↻</span>
        <span class="row-txt">
          <span class="row-title">刷新信息</span>
          <span class="row-sub">重新读取设备与统计</span>
        </span>
      </button>

      <button class="row-btn" type="button" @click="copyBase">
        <span class="row-ico" aria-hidden="true">🔗</span>
        <span class="row-txt">
          <span class="row-title">复制服务地址</span>
          <span class="row-sub mono">{{ pairing.base }}</span>
        </span>
      </button>

      <button class="row-btn danger" type="button" @click="onUnpair">
        <span class="row-ico" aria-hidden="true">🚪</span>
        <span class="row-txt">
          <span class="row-title">退出配对</span>
          <span class="row-sub">清除本机保存的配对信息</span>
        </span>
      </button>
    </div>

    <p class="ver">PhotoBridge 手机端 · 一期（局域网直连）</p>
  </div>
</template>

<style scoped>
.mine {
  flex: 1 1 auto;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 10px 10px 0;
  padding-bottom: calc(var(--tabbar-h) + var(--safe-bottom) + 16px);
}

.hero-card {
  padding: 14px;
}

.hero-top {
  display: flex;
  align-items: center;
  gap: 11px;
  margin-bottom: 14px;
}

.hero-ico {
  font-size: 26px;
  width: 44px;
  height: 44px;
  border-radius: 12px;
  background: var(--bg-3);
  display: flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 auto;
}

.hero-txt {
  flex: 1 1 auto;
  min-width: 0;
}

.hero-name {
  font-size: 16px;
  font-weight: 600;
}

.hero-sub {
  margin-top: 3px;
  font-size: 12px;
  color: var(--fg-3);
}

.pill {
  flex: 0 0 auto;
  font-size: 11px;
  padding: 3px 9px;
  border-radius: 999px;
  background: var(--ok-dim);
  color: var(--ok);
}

.pill.busy {
  background: var(--warn-dim);
  color: var(--warn);
}

.kv {
  display: flex;
  flex-direction: column;
  gap: 8px;
  font-size: 13px;
}

.r {
  display: flex;
  gap: 10px;
  align-items: baseline;
}

.r dt {
  flex: 0 0 6em;
  color: var(--fg-3);
  font-size: 12px;
}

.r dd {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--fg-1);
  word-break: break-word;
}

.dim {
  color: var(--fg-3);
}

.bad { color: var(--bad); }
.warn { color: var(--warn); }
.ok { color: var(--ok); }

.small {
  font-size: 11px;
}

.mono {
  font-family: ui-monospace, monospace;
  font-size: 12px;
}

.err {
  margin-bottom: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  background: var(--bad-dim);
  color: var(--bad);
  font-size: 12px;
}

.diskbar {
  margin-top: 12px;
  height: 4px;
  border-radius: 999px;
  background: var(--bg-3);
  overflow: hidden;
}

.diskbar-in {
  height: 100%;
  background: var(--ok);
}

.diskbar-in.warn { background: var(--warn); }
.diskbar-in.bad { background: var(--bad); }

.sec {
  font-size: 13px;
  color: var(--fg-2);
  font-weight: 500;
  margin-bottom: 10px;
}

.root + .root {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}

.root-top {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 13px;
}

.root-label {
  color: var(--fg);
  min-width: 0;
}

.root-count {
  color: var(--fg-2);
  flex: 0 0 auto;
}

.root-path {
  margin-top: 3px;
  color: var(--fg-3);
}

.root-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  font-size: 12px;
}

.tag {
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--bg-3);
  color: var(--fg-2);
  font-size: 11px;
}

.tag.warn {
  background: var(--warn-dim);
  color: var(--warn);
}

.tech summary {
  font-size: 13px;
  color: var(--fg-2);
  cursor: pointer;
  margin-bottom: 10px;
}

.acts {
  padding: 4px 0;
  overflow: hidden;
}

.row-btn {
  width: 100%;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 13px 14px;
  text-align: left;
  color: var(--fg);
}

.row-btn + .row-btn {
  border-top: 1px solid var(--line);
}

.row-btn:active {
  background: var(--bg-2);
}

.row-btn:disabled {
  opacity: 0.6;
}

.row-ico {
  font-size: 18px;
  width: 22px;
  text-align: center;
  flex: 0 0 auto;
}

.row-txt {
  flex: 1 1 auto;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.row-title {
  font-size: 14px;
}

.row-sub {
  font-size: 12px;
  color: var(--fg-3);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row-btn.danger .row-title {
  color: var(--bad);
}

.ver {
  text-align: center;
  font-size: 11px;
  color: var(--fg-3);
  padding: 8px 0 4px;
}
</style>
