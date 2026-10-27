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
import { toastError, toastOk } from "../stores/toast";

interface InviteRow {
  invite_id: string;
  label: string;
  state: "active" | "used" | "revoked" | "expired";
  created_at: number;
  expires_at: number;
  first_used_at: number | null;
  link: string;
  client_id: string;
}

interface SessionRow {
  client_id: string;
  invite_id: string;
  label: string;
  created_at: number;
  expires_at: number;
  last_seen: number | null;
  revoked: boolean;
}

const invites = ref<InviteRow[]>([]);
const sessions = ref<SessionRow[]>([]);
const loading = ref(false);
const busy = ref(false);
const label = ref("");
const minutes = ref(1440);

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

const online = computed(() => sessions.value.filter((s) => !s.revoked).length);

async function load(): Promise<void> {
  loading.value = true;
  try {
    const [inv, ses] = await Promise.all([
      api.get<{ items: InviteRow[] }>("/api/guest/invites"),
      api.get<{ items: SessionRow[] }>("/api/guest/sessions"),
    ]);
    invites.value = inv.items;
    sessions.value = ses.items;
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
    await api.post("/api/guest/invites", {
      label: label.value.trim(),
      minutes: minutes.value,
    });
    label.value = "";
    toastOk("邀请链接已生成，发给朋友即可");
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
    toastError(guestError(e, "踢出失败"));
  } finally {
    busy.value = false;
  }
}

async function copy(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    toastOk("链接已复制");
  } catch {
    // 非 HTTPS 或用户拒绝授权时会抛；把文本塞进输入框让用户手选。
    toastError("复制失败，请长按链接手动复制");
  }
}

function guestError(e: unknown, fallback: string): string {
  if (!(e instanceof ApiError)) return fallback;
  if (e.code === "E1012") return "还没配置游客目录，请先在电脑上设好 visibility=guest 的根";
  return e.message || fallback;
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
      <div class="inv-acts">
        <button class="btn" type="button" @click="copy(it.link)">复制链接</button>
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
    <div v-for="s in sessions" :key="s.client_id" class="inv">
      <div class="inv-top">
        <span class="inv-label ellipsis">{{ s.label || "游客" }}</span>
        <span v-if="s.revoked" class="tag warn">已退出</span>
      </div>
      <div class="inv-meta">
        <span>进来于 {{ fmtTime(s.created_at) }}</span>
        <span>最近活动 {{ fmtTime(s.last_seen) }}</span>
      </div>
      <div v-if="!s.revoked" class="inv-acts">
        <button class="btn danger" type="button" :disabled="busy" @click="kick(s.client_id)">
          让这台设备退出
        </button>
      </div>
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

.ellipsis {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
