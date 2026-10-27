<script setup lang="ts">
/**
 * 「游客访问」面板（主人侧）。
 *
 * ## 这一块在解决什么
 *
 * 朋友想看一次旅行照片，传统的做法是给对方开一个账号 —— 那等于把
 * 「我的库」的钥匙给出去。这里给的是**一次性的、能随时收回的、只能看和传**
 * 的邀请链接：点开就进，不用密码，主人随时能踢。
 *
 * ## 为什么列表要显示"状态"而不是只显示过期时间
 *
 * 主人真正关心的是"这条还能不能用"：已经用过的一次性邀请、被自己撤销的、
 * 还没人点的，长着一样的链接。只给时间戳的话，主人得自己算 —— 而且
 * 「用过一次」在时间戳上完全看不出来。
 *
 * ## 为什么撤销邀请还要连带踢会话
 *
 * 邀请被撤销时，已经用它进来的那个人手里握着的**会话令牌还有效**。
 * 只标记邀请的话，主人点了"撤销"、界面上写着已撤销，而对方手机里照片
 * 照样能翻 —— 这是最糟糕的一类安全 bug：状态显示与实际能力不一致。
 * 服务端（`backend/app/guest.py: revoke_invite`）在撤销时会把会话一起标掉。
 */
import { computed, onMounted, ref } from "vue";
import { ApiError, api } from "../api/client";
import { copyText } from "../api/clipboard";
import { toastError, toastOk, toastWarn } from "../stores/toast";
import { errorText } from "../api/errorText";

interface InviteRow {
  invite_id: string;
  label: string;
  state: "active" | "used" | "revoked" | "expired";
  created_at: number;
  expires_at: number;
  first_used_at: number | null;
  /** 只在 `POST /api/guest/invites` 的响应里有；列表接口拿不到（库里只存哈希） */
  link?: string;
  client_id: string;
}

/** `POST /api/guest/invites` 的响应。`link` 是**唯一一次**能拿到明文的地方。 */
interface CreateResp {
  invite_id: string;
  token: string;
  link: string;
  label: string;
  minutes: number;
  expires_at: number;
}

/**
 * 服务端 `guest.list_sessions()` 的字段。
 *
 * ★ BUG-3 的根因就在这里：以前写的是 `revoked: boolean`，而接口从来没发
 *   过这个字段（发的是 `revoked_at` 与现算的 `online`），于是 `s.revoked`
 *   恒 undefined → 每一行都画成"在线 + 踢出按钮" → 点已撤销的那行必然
 *   命中 0 行更新 → 后端只能回 not_found。字段名对不上，症状跑到后端去
 *   了。
 */
interface SessionRow {
  client_id: string;
  invite_id: string;
  label: string;
  created_at: number;
  expires_at: number;
  last_seen: number | null;
  revoked_at: number | null;
  online: boolean;
}

const invites = ref<InviteRow[]>([]);
const sessions = ref<SessionRow[]>([]);
const loading = ref(false);
const busy = ref(false);
const label = ref("");
const minutes = ref(1440);

/** 刚生成的那一条：明文链接只在这一刻存在，得让用户看得见、复制得到。 */
const created = ref<{ invite_id: string; link: string; expires_at: number } | null>(null);
/** 展开过完整链接的 invite_id 集合（默认折叠为"前 26 + … + 后 8"）。 */
const expanded = ref<Set<string>>(new Set());

/**
 * 生成过的链接留在本机，按 invite_id 索引。
 *
 * 为什么需要它：服务端只存 `token_hash`，`GET /api/guest/invites` 永远
 * 给不回明文。没有这份缓存，列表里那条"复制链接"就只能是复制 `undefined`。
 * 存本机也符合直觉 —— 令牌本来就该由发出去的那台设备保管。
 */
const LINKS_KEY = "pb.guestLinks";

function readSavedLinks(): Record<string, string> {
  try {
    const raw = localStorage.getItem(LINKS_KEY);
    const obj = raw ? JSON.parse(raw) : {};
    return obj && typeof obj === "object" ? obj as Record<string, string> : {};
  } catch {
    return {};
  }
}

function saveLink(inviteId: string, link: string): void {
  try {
    const all = readSavedLinks();
    all[inviteId] = link;
    localStorage.setItem(LINKS_KEY, JSON.stringify(all));
  } catch {
    // 隐私模式下 localStorage 会抛；不影响本次会话（内存里还有 created）
  }
}

function linkOf(it: InviteRow): string {
  return it.link || readSavedLinks()[it.invite_id] || "";
}

/** 截断显示：前 26 + … + 后 8。足够辨认，又不至于把手机屏幕撑爆。 */
function maskLink(u: string): string {
  if (u.length <= 36) return u;
  return `${u.slice(0, 26)}…${u.slice(-8)}`;
}

function toggle(id: string): void {
  const s = new Set(expanded.value);
  if (s.has(id)) s.delete(id);
  else s.add(id);
  expanded.value = s;
}

/** 复制失败时：把能展开的都展开，用户至少能长按选中。 */
function revealAll(): void {
  const s = new Set(expanded.value);
  if (created.value) s.add(created.value.invite_id);
  for (const it of invites.value) if (linkOf(it)) s.add(it.invite_id);
  expanded.value = s;
}

const MINUTES: { v: number; t: string }[] = [
  { v: 60, t: "1 小时" },
  { v: 360, t: "6 小时" },
  { v: 1440, t: "1 天" },
  { v: 10080, t: "7 天" },
];

const stateText: Record<string, string> = {
  active: "可使用",
  used: "已使用（一次性）",
  revoked: "已撤销",
  expired: "已过期",
};

const online = computed(() => sessions.value.filter((s) => s.online).length);

/**
 * 列表一次铺几条（`guest.max_visible`，由 `/api/system/info` 回显）。
 *
 * ★ BUG-2：折叠是**展示层**的事，后端照旧返回全部会话（含已退出）。
 *   主人拍了 P1 = "在线上限与展示上限都是 3"：在线人数永远撞不到折叠线，
 *   但列表里还有历史会话（实测 17 行），所以折叠照样看得见。
 */
const maxVisible = ref(3);
const showAllSessions = ref(false);
const visibleSessions = computed(() =>
  showAllSessions.value ? sessions.value : sessions.value.slice(0, maxVisible.value),
);
const hiddenSessions = computed(() =>
  Math.max(0, sessions.value.length - visibleSessions.value.length),
);

async function load(): Promise<void> {
  loading.value = true;
  try {
    const [inv, ses, sys] = await Promise.all([
      api.get<{ items: InviteRow[] }>("/api/guest/invites"),
      api.get<{ items: SessionRow[] }>("/api/guest/sessions"),
      api.get<{ guest?: { max_visible?: number } }>("/api/system/info"),
    ]);
    invites.value = inv.items;
    sessions.value = ses.items;
    const vis = Number(sys.guest?.max_visible ?? 3);
    maxVisible.value = Number.isFinite(vis) && vis > 0 ? Math.floor(vis) : 3;
  } catch (e) {
    // 没配游客根时这里会是 400 E1001：主人还没准备好，不该当错误弹。
    if (!(e instanceof ApiError) || e.code !== "E1001") {
      toastError("读取游客列表失败，请稍后重试");
    }
  } finally {
    loading.value = false;
  }
}

async function create(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    const r = await api.post<CreateResp>("/api/guest/invites", {
      label: label.value.trim(),
      minutes: minutes.value,
    });
    label.value = "";
    // ★ 明文链接只在这一次响应里，此后服务端再也给不回来
    created.value = { invite_id: r.invite_id, link: r.link, expires_at: r.expires_at };
    saveLink(r.invite_id, r.link);
    expanded.value = new Set([...expanded.value, r.invite_id]);
    toastOk("邀请链接已生成，下面是完整链接");
    await load();
  } catch (e) {
    toastError(guestError(e, "生成邀请失败"));
  } finally {
    busy.value = false;
  }
}

async function revoke(id: string): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    await api.del(`/api/guest/invites/${encodeURIComponent(id)}`);
    toastOk("已撤销，对方的会话也一起失效了");
    await load();
  } catch (e) {
    toastError(guestError(e, "撤销失败"));
  } finally {
    busy.value = false;
  }
}

async function kick(clientId: string): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  try {
    await api.del(`/api/guest/sessions/${encodeURIComponent(clientId)}`);
    toastOk("已让这台设备退出");
    await load();
  } catch (e) {
    // E1026 = 这位游客已经不在线（列表是几秒前的快照）。这不是操作失败，
    // 刷一次列表把他挪到"已退出"就行，别弹一句让人以为按钮坏了。
    if (e instanceof ApiError && e.code === "E1026") {
      toastOk("这位游客已经不在线了");
      await load();
      return;
    }
    toastError(guestError(e, "踢出失败"));
  } finally {
    busy.value = false;
  }
}

/**
 * 复制一条链接。
 *
 * ★ 必须走 `api/clipboard.ts` 的 `copyText()`：它先试 `navigator.clipboard`，
 *   再退回 `document.execCommand("copy")`。局域网明文 HTTP 下前者是
 *   `undefined`（非安全上下文），只有后者能用；自己写 `navigator.clipboard`
 *   等于在手机上必然失败 —— 这正是本 bug 的成因。
 * ★ 用户名下的 `execCommand` 兜底必须在**点击手势的同步栈**里：`copyText`
 *   在 clipboard 不存在时**不会 await**，因此兜底是同步执行的。
 */
async function copy(text: string): Promise<void> {
  if (!text) {
    toastWarn("这条邀请的链接已无法取回（只在生成时显示），请重新生成一条");
    return;
  }
  const ok = await copyText(text);
  if (ok) {
    toastOk(`已复制：${text.slice(0, 32)}…`);
    return;
  }
  // 失败不静默：明确告诉用户要怎么做，并把链接摊开给他长按
  revealAll();
  toastError("复制失败：请长按下面的链接手动复制（已为你展开）");
}

function guestError(e: unknown, fallback: string): string {
  if (!(e instanceof ApiError)) return fallback;
  if (e.code === "E1012") return "还没配置游客目录，请先在电脑上设好 visibility=guest 的根";
  return errorText(e, fallback);
}

function fmtTime(sec: number | null): string {
  if (!sec) return "—";
  const d = new Date(sec * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

onMounted(() => {
  void load();
});
</script>

<template>
  <div class="card">
    <h3 class="sec">游客访问</h3>
    <p class="hint">
      生成一次性链接发给朋友：点开就能看，不用密码；
      他们只能<b>查看</b>和<b>上传到「游客上传」</b>，不能删除或修改你的照片。
    </p>

    <div class="row">
      <input
        v-model="label"
        class="inp"
        type="text"
        maxlength="40"
        placeholder="给这条邀请起个名字（可选）"
      >
    </div>
    <div class="row">
      <select v-model.number="minutes" class="inp sel">
        <option v-for="m in MINUTES" :key="m.v" :value="m.v">
          有效期 {{ m.t }}
        </option>
      </select>
      <button class="btn primary" type="button" :disabled="busy" @click="create">
        {{ busy ? "处理中…" : "生成邀请链接" }}
      </button>
    </div>

    <div v-if="created" class="newlink">
      <div class="inv-top">
        <span class="inv-label">刚生成的链接</span>
        <span class="tag">仅此一次可见</span>
      </div>
      <div
        class="link-show"
        :class="{ full: expanded.has(created.invite_id) }"
        @click="toggle(created.invite_id)"
      >
        <span class="link-text">{{
          expanded.has(created.invite_id) ? created.link : maskLink(created.link)
        }}</span>
      </div>
      <div class="inv-acts">
        <button class="btn primary" type="button" @click="copy(created.link)">
          复制链接
        </button>
        <button class="btn" type="button" @click="toggle(created.invite_id)">
          {{ expanded.has(created.invite_id) ? "收起" : "展开" }}
        </button>
      </div>
      <div class="hint sm">
        有效期至 {{ fmtTime(created.expires_at) }}。关掉本页后这条链接在这里
        只能看到缩略；手机复制失败时可长按上面那行手动选中。
      </div>
    </div>

    <div v-if="loading" class="empty">读取中…</div>
    <div v-else-if="!invites.length" class="empty">还没有邀请</div>

    <div v-for="it in invites" :key="it.invite_id" class="inv">
      <div class="inv-top">
        <span class="inv-label ellipsis">{{ it.label || "未命名邀请" }}</span>
        <span class="tag" :class="{ warn: it.state !== 'active' }">
          {{ stateText[it.state] || it.state }}
        </span>
      </div>
      <div class="inv-meta">
        <span>有效期至 {{ fmtTime(it.expires_at) }}</span>
        <span v-if="it.first_used_at">首次使用 {{ fmtTime(it.first_used_at) }}</span>
      </div>
      <div
        v-if="linkOf(it)"
        class="link-show"
        :class="{ full: expanded.has(it.invite_id) }"
        @click="toggle(it.invite_id)"
      >
        <span class="link-text">{{
          expanded.has(it.invite_id) ? linkOf(it) : maskLink(linkOf(it))
        }}</span>
      </div>
      <div class="inv-acts">
        <button v-if="linkOf(it)" class="btn" type="button" @click="copy(linkOf(it))">
          复制链接
        </button>
        <span v-else class="hint sm">
          链接只在生成时显示（服务端只存哈希），要再发请重新生成
        </span>
        <button
          v-if="it.state === 'active'"
          class="btn danger"
          type="button"
          :disabled="busy"
          @click="revoke(it.invite_id)"
        >
          撤销
        </button>
      </div>
    </div>

    <h3 class="sec sub">正在访问的游客 · {{ online }}</h3>
    <div v-if="!sessions.length" class="empty">暂时没有游客在线</div>
    <div v-for="s in visibleSessions" :key="s.client_id" class="inv">
      <div class="inv-top">
        <span class="inv-label ellipsis">{{ s.label || "游客" }}</span>
        <span v-if="!s.online" class="tag warn">已退出</span>
      </div>
      <div class="inv-meta">
        <span>进来于 {{ fmtTime(s.created_at) }}</span>
        <span>最近活动 {{ fmtTime(s.last_seen) }}</span>
      </div>
      <div v-if="s.online" class="inv-acts">
        <button class="btn danger" type="button" :disabled="busy" @click="kick(s.client_id)">
          让这台设备退出
        </button>
      </div>
    </div>
    <div v-if="hiddenSessions" class="fold">
      <span class="hint sm">还有 {{ hiddenSessions }} 个未展开</span>
      <button class="btn" type="button" @click="showAllSessions = !showAllSessions">
        {{ showAllSessions ? "收起" : "展开全部" }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.sec {
  margin: 0 0 6px;
  font-size: 14px;
  font-weight: 600;
}

.sec.sub {
  margin-top: 16px;
}

/* 折叠条：只在"超出展示上限"时出现（BUG-2）。 */
.fold {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 6px;
}

.hint.sm {
  margin: 0;
  font-size: 12px;
  color: var(--fg-2);
}

.hint {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--fg-2);
}

.row {
  display: flex;
  gap: 8px;
  margin-bottom: 8px;
}

.inp {
  flex: 1 1 auto;
  min-width: 0;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  background: var(--bg-2);
  color: var(--fg-1);
  font-size: 13px;
}

.sel {
  flex: 0 0 auto;
  max-width: 45%;
}

.btn {
  flex: 0 0 auto;
  padding: 8px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  background: var(--bg-2);
  color: var(--fg-1);
  font-size: 13px;
}

.btn.primary {
  border-color: transparent;
  background: var(--accent, #2f6feb);
  color: #fff;
  font-weight: 600;
}

.btn.danger {
  border-color: transparent;
  background: var(--bad-dim);
  color: var(--bad);
}

.btn:disabled {
  opacity: 0.6;
}

.inv {
  margin-top: 10px;
  padding: 10px;
  border-radius: var(--radius-sm);
  background: var(--bg-2);
}

.inv-top {
  display: flex;
  align-items: center;
  gap: 8px;
}

.inv-label {
  flex: 1 1 auto;
  font-size: 13px;
  font-weight: 600;
}

.inv-meta {
  margin-top: 4px;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 12px;
  color: var(--fg-2);
}

.inv-acts {
  margin-top: 8px;
  display: flex;
  gap: 8px;
}

.empty {
  padding: 10px 0;
  font-size: 12px;
  color: var(--fg-2);
}

.tag {
  flex: 0 0 auto;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11px;
  background: var(--bg-3, rgba(0, 0, 0, 0.06));
  color: var(--fg-2);
}

.tag.warn {
  background: var(--bad-dim);
  color: var(--bad);
}

.newlink {
  margin-top: 12px;
  padding: 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--acc, #3b82f6);
  background: var(--bg-2);
}

/**
 * 链接就必须"看得见、选得中"。
 * 不用 `display:none` / `opacity:0`：看不见的文本等于没给用户兜底。
 */
.link-show {
  margin-top: 6px;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--bg-3, rgba(0, 0, 0, 0.06));
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  line-height: 1.5;
  color: var(--fg-1);
  cursor: pointer;
  user-select: text;
  -webkit-user-select: text;
  overflow-wrap: anywhere;
  word-break: break-all;
}

.link-show.full {
  background: var(--bg-3, rgba(0, 0, 0, 0.09));
}

.user-select-none { user-select: none; }

.hint.sm {
  margin-top: 6px;
  font-size: 12px;
  color: var(--fg-2);
}

.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
