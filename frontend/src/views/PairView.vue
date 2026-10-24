<script setup lang="ts">
/**
 * 配对页：未配对时全屏显示。
 *
 * 三条路径都能配对，按用户实际处境排序：
 *   ① 电脑上显示二维码 → 手机扫码后浏览器直接带 `?code=` 打开本页（自动填）
 *   ② 手动输入 IP + 6 位码（最常见）
 *   ③ 直接粘贴一整条带参数的 URL（用户从聊天窗口复制过来的）
 */
import { computed, onMounted, ref } from "vue";
import { ApiError } from "../api/client";
import { buildBase, cloudBaseFromLocation } from "../api/storage";
import { pair, pairParamsFromLocation, pairWithAccount, parsePairInput } from "../stores/auth";
import { toastError, toastOk } from "../stores/toast";

/**
 * 页面是不是由**云端**伺服的（`https://域名/d/<id>/`）。
 *
 * 是的话这里没有"电脑地址"可填 —— 基址就是当前 location，配对请求要打到
 * 云端的 `/d/<id>/api/pair`（`cloud/relay/forward.py` 的匿名白名单）。
 * 让用户在这种情况下还去填一个 IP 是自相矛盾的：他人在外面，根本不知道
 * 家里电脑今天被 DHCP 分到了哪个地址。
 */
const cloudPage = ref(cloudBaseFromLocation() !== "");

const host = ref("");
const port = ref("8787");
const code = ref("");
const pasteText = ref("");
const busy = ref(false);
const error = ref("");
/**
 * 二维码里带的电脑 `device_id`（AC-5.2）。空串 = 手动输入，无从校验。
 *
 * 它防的是"静态二维码 + 动态 IP"的错配：二维码是截图/打印出来的，
 * 而局域网 IP 由 DHCP 分配。隔几天再扫，同一个 IP 可能已经属于另一台
 * 也跑着 PhotoBridge 的电脑 —— 不校验的话手机会绑到别人的电脑上，
 * 还会把对方的照片显示出来。
 */
const expectedDid = ref("");

/**
 * 账号登录（云端页的**第二条**配对路径，`docs/04` §3.5「自动配对」）。
 *
 * 只在云端页显示：局域网直连时请求打的是家里电脑，而登录/自动配对打的是
 * **中继根**上的 `/api/auth/login` 与 `/api/devices/{id}/pair`，那条路上
 * 没有这台电脑的位置。把它放在局域网页只会让用户点了报错。
 */
const email = ref("");
const password = ref("");
const accountBusy = ref(false);
const accountError = ref("");
const canSubmitAccount = computed(() =>
  email.value.trim().length > 3 && password.value.length > 0 && !accountBusy.value);

const base = computed(() => buildBase(host.value, port.value));
const canSubmit = computed(() =>
  (cloudPage.value || !!host.value.trim())
  && /^\d{6}$/.test(code.value.replace(/[\s-]/g, "")) && !busy.value);

onMounted(() => {
  // 扫码进来时 URL 里已经带好了参数，别让用户再输一遍
  const p = pairParamsFromLocation();
  if (p) {
    const u = parseBaseUrl(p.base);
    if (u) {
      host.value = u.hostname;
      port.value = u.port || "8787";
    }
    code.value = p.code;
    expectedDid.value = p.did;
    void submit();
    return;
  }
  // 局域网页是从电脑自己身上打开的，地址就是当前 location——
  // 让用户手输一遍 IP 只会多一个出错点（ISSUE-040：真机上按钮"点不动"
  // 的一种情形就是地址栏空着、按钮又是灰的，谁也说不清缺什么）。
  if (!cloudPage.value && !host.value.trim() && window.location.hostname) {
    host.value = window.location.hostname;
    port.value = window.location.port || "8787";
  }
});

/**
 * 把粘贴来的基址解析成 `URL`，解析不了返回 `null`。
 *
 * 名字不再叫 `safeUrl`：`docs/07` TC-XSS-SEC-007 要求 `safeUrl()` 返回
 * **空串**（那是绑到 `:src`/`:href` 的守卫，见 `utils/safeUrl.ts`），
 * 两个语义用一个名字会让判据与人同时糊涂（SEC-24）。
 */
function parseBaseUrl(b: string): URL | null {
  try {
    return new URL(b);
  } catch {
    return null;
  }
}

/** 输入框里把 `192.0.2.10:8787` 一次性粘进 host 时自动拆出端口。 */
function onHostBlur(): void {
  const t = host.value.trim();
  const m = t.match(/^([\w.\-]+):(\d+)/);
  if (m) {
    host.value = m[1]!;
    port.value = m[2]!;
  }
}

/**
 * 真正发配对请求（两条入口共用）。
 *
 * 收的是**完整基址字符串**而不是读数 ref：粘贴链接时那个基址的协议与端口
 * 已经在 `parsePairInput()` 里按白名单规则定好了（ISSUE-033），再用
 * `buildBase(host, port)` 重拼一遍就会把 https 降级成 http。
 */
async function runPair(target: string): Promise<void> {
  if (busy.value) return;
  error.value = "";
  // 云端页面上没有"电脑地址"可填：基址由 `location` 决定（见 `pair()`）
  if (!target && !cloudPage.value) {
    error.value = "请填写电脑的 IP 地址";
    return;
  }
  if (!/^\d{6}$/.test(code.value.replace(/[\s-]/g, ""))) {
    error.value = "配对码是 6 位数字";
    return;
  }
  busy.value = true;
  try {
    const r = await pair(target, code.value, expectedDid.value);
    toastOk(`已连接到 ${r.device_name}`);
  } catch (e) {
    error.value = e instanceof ApiError
      ? (e.userHint || e.friendly)
      : "配对失败，请检查地址与配对码";
  } finally {
    busy.value = false;
  }
}

async function submit(): Promise<void> {
  await runPair(base.value);
}

/** 粘贴整条 URL / "IP 码" 文本。 */
async function submitPaste(): Promise<void> {
  let parsed: { base: string; code: string; did: string } | null;
  try {
    parsed = parsePairInput(pasteText.value);
  } catch (e) {
    // ★ 识别出来了但主机不在白名单（ISSUE-033）：要把拒绝理由说清楚，
    //   否则用户会以为"没粘对"，把同一段攻击者链接再粘一次。
    toastError(e instanceof ApiError ? (e.userHint || e.friendly) : "这段内容不能用来配对");
    return;
  }
  if (!parsed) {
    toastError("没认出这段内容，请粘贴电脑上显示的链接，或手动填写地址与配对码");
    return;
  }
  // 回填输入框只是让用户看到"打到了哪儿"，实际用的是 `parsed.base`
  const u = parseBaseUrl(parsed.base);
  if (u) {
    host.value = u.hostname;
    port.value = u.port || (u.protocol === "https:" ? "" : "8787");
  }
  code.value = parsed.code;
  expectedDid.value = parsed.did;
  await runPair(parsed.base);
}

/**
 * 账号登录 → 自动配对（云端页专用）。
 *
 * 与 `runPair()` 的区别：这条路上没有 6 位码，也没有"电脑地址"——身份由
 * 账号证明，设备由 URL 里的 `/d/<id>` 决定。所以错误发生在**登录**阶段，
 * 文案要说"邮箱或密码"，说"配对码错了"会把人引向错误的排查方向。
 */
async function submitAccount(): Promise<void> {
  if (accountBusy.value) return;
  accountError.value = "";
  if (!canSubmitAccount.value) {
    accountError.value = "请填写账号邮箱与密码";
    return;
  }
  accountBusy.value = true;
  try {
    const r = await pairWithAccount(email.value, password.value);
    password.value = "";
    toastOk(`已连接到 ${r.device_name}`);
  } catch (e) {
    accountError.value = e instanceof ApiError
      ? (e.userHint || e.friendly)
      : "登录失败，请检查邮箱与密码";
  } finally {
    accountBusy.value = false;
  }
}

function onCodeInput(e: Event): void {
  const el = e.target as HTMLInputElement;
  // 只留数字，最长 6 位 —— 手机上打字容易多按，直接吃掉多余字符
  const v = el.value.replace(/\D/g, "").slice(0, 6);
  code.value = v;
  el.value = v;
  error.value = "";
}
</script>

<template>
  <div class="pair scroll">
    <div class="brand">
      <div class="logo" aria-hidden="true">🖼</div>
      <h1 class="app-name">PhotoBridge</h1>
      <p class="tagline">把手机里的照片，传到自己的电脑上</p>
    </div>

    <div class="card">
      <label v-if="!cloudPage" class="field">
        <span class="lbl">电脑地址</span>
        <div class="addr">
          <input
            v-model="host"
            class="input-lg grow"
            type="text"
            inputmode="decimal"
            autocapitalize="off"
            autocorrect="off"
            spellcheck="false"
            placeholder="192.0.2.10"
            @blur="onHostBlur"
          />
          <span class="colon">:</span>
          <input
            v-model="port"
            class="input-lg port"
            type="text"
            inputmode="numeric"
            placeholder="8787"
          />
        </div>
        <span class="hint">电脑上打开 PhotoBridge 就能看到这个地址</span>
      </label>
      <p v-else class="cloud-note">
        正在通过中继连接家里的电脑。输入电脑上显示的配对码即可。
      </p>

      <template v-if="cloudPage">
        <label class="field">
          <span class="lbl">账号邮箱</span>
          <input
            v-model="email"
            class="input-lg"
            type="email"
            inputmode="email"
            autocomplete="username"
            autocapitalize="off"
            autocorrect="off"
            spellcheck="false"
            placeholder="you@example.com"
          />
        </label>
        <label class="field">
          <span class="lbl">密码</span>
          <input
            v-model="password"
            class="input-lg"
            type="password"
            autocomplete="current-password"
            placeholder="登录密码"
            @keyup.enter="submitAccount"
          />
          <span class="hint">登录后自动匹配这台电脑，不必再输配对码</span>
        </label>

        <p v-if="accountError" class="err">{{ accountError }}</p>

        <button
          class="btn primary block lg"
          type="button"
          :disabled="accountBusy"
          :class="{ dim: !canSubmitAccount }"
          @click="submitAccount"
        >
          <span v-if="accountBusy" class="spinner" />
          {{ accountBusy ? "正在登录…" : "登录并连接" }}
        </button>

        <div class="or-line"><span>或输入电脑上显示的配对码</span></div>
      </template>

      <label class="field">
        <span class="lbl">配对码</span>
        <input
          class="input-lg code"
          type="text"
          inputmode="numeric"
          autocomplete="one-time-code"
          maxlength="6"
          placeholder="······"
          :value="code"
          @input="onCodeInput"
        />
        <span class="hint">电脑窗口上显示的 6 位数字</span>
      </label>

      <p v-if="error" class="err">{{ error }}</p>

      <button
        class="btn primary block lg"
        type="button"
        :disabled="busy"
        :class="{ dim: !canSubmit }"
        @click="submit"
      >
        <span v-if="busy" class="spinner" />
        {{ busy ? "正在连接…" : "连接电脑" }}
      </button>
    </div>

    <details class="paste">
      <summary>扫码打不开？粘贴链接试试</summary>
      <textarea
        v-model="pasteText"
        class="paste-ta"
        rows="2"
        placeholder="http://192.0.2.10:8787/?code=123456"
        autocapitalize="off"
        autocorrect="off"
        spellcheck="false"
      />
      <button class="btn ghost block" type="button" :disabled="busy" @click="submitPaste">
        用这段内容配对
      </button>
    </details>

    <p class="foot-note">
      <template v-if="cloudPage">
        配对码由电脑上的 PhotoBridge 显示。照片不经过中继保存，只在传输时路过。
      </template>
      <template v-else>
        手机和电脑要连同一个 Wi-Fi。照片只在局域网内传输，不会上传到互联网。
      </template>
    </p>
  </div>
</template>

<style scoped>
.pair {
  flex: 1 1 auto;
  min-height: 0;
  padding: 24px 18px calc(var(--safe-bottom) + 32px);
  display: flex;
  flex-direction: column;
  gap: 18px;
  /* 顶部刘海 */
  padding-top: calc(var(--safe-top) + 24px);
}

.brand {
  text-align: center;
  padding: 22px 0 6px;
}

.logo {
  font-size: 44px;
  line-height: 1;
  margin-bottom: 12px;
}

.app-name {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: 0.3px;
}

.tagline {
  margin-top: 8px;
  color: var(--fg-2);
  font-size: 13px;
}

.card {
  background: var(--bg-1);
  border: 1px solid var(--line);
  border-radius: var(--radius-lg);
  padding: 18px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.lbl {
  font-size: 13px;
  color: var(--fg-1);
}

.addr {
  display: flex;
  align-items: center;
  gap: 4px;
}

.grow {
  flex: 1 1 auto;
  min-width: 0;
}

.colon {
  color: var(--fg-3);
}

.port {
  width: 5.2em;
  text-align: center;
  flex: 0 0 auto;
}

.code {
  /* 配对码要一眼能看清、能核对，字距拉开 */
  text-align: center;
  font-size: 26px;
  letter-spacing: 0.42em;
  text-indent: 0.42em;
  font-variant-numeric: tabular-nums;
}

.hint {
  font-size: 12px;
  color: var(--fg-3);
}

.err {
  color: var(--bad);
  font-size: 13px;
  background: var(--bad-dim);
  border-radius: var(--radius-sm);
  padding: 9px 11px;
}

.paste {
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 12px;
  background: var(--bg-1);
}

.paste summary {
  font-size: 13px;
  color: var(--fg-1);
  cursor: pointer;
}

.paste-ta {
  width: 100%;
  margin: 10px 0;
  padding: 10px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--line-strong);
  background: var(--bg);
  color: var(--fg);
  /* 16px：小于 16px 时 iOS Safari 聚焦会自动放大页面，很讨厌 */
  font-size: 16px;
  font-family: ui-monospace, monospace;
  resize: none;
}

.foot-note {
  margin-top: auto;
  padding-top: 10px;
  font-size: 12px;
  color: var(--fg-3);
  line-height: 1.6;
  text-align: center;
}

.cloud-note {
  font-size: 13px;
  color: var(--fg-2);
  line-height: 1.6;
  background: var(--bg-2, var(--bg-1));
  border-radius: var(--radius-sm);
  padding: 10px 12px;
}

/* 「或」分隔线：两条配对路径并列时的视觉切换（云端页才有） */
.or-line {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 14px 0 4px;
  font-size: 12px;
  color: var(--fg-3);
}

.or-line::before,
.or-line::after {
  content: "";
  flex: 1;
  height: 1px;
  background: var(--line, var(--bg-2, #ddd));
}

.or-line span {
  white-space: nowrap;
}

/*
 * 条件不足时按钮仍然可点（`disabled` 只留给"请求进行中"）。
 * 灰掉但点不动的按钮在手机上毫无解释力——用户只会反复戳，然后报
 * "按钮点不上"（ISSUE-040）。这里只降低不透明度做提示，点下去由
 * `submit()` / `submitAccount()` 把缺什么用文字说出来。
 */
.btn.dim {
  opacity: 0.55;
}
</style>
