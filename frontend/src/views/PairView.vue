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
import { buildBase } from "../api/storage";
import { authStore, pair, pairParamsFromLocation, parsePairInput } from "../stores/auth";
import { toastError, toastOk } from "../stores/toast";

const host = ref("");
const port = ref("8787");
const code = ref("");
const pasteText = ref("");
const busy = ref(false);
const error = ref("");

const base = computed(() => buildBase(host.value, port.value));
const canSubmit = computed(() =>
  !!host.value.trim() && /^\d{6}$/.test(code.value.replace(/[\s-]/g, "")) && !busy.value);

onMounted(() => {
  // 扫码进来时 URL 里已经带好了参数，别让用户再输一遍
  const p = pairParamsFromLocation();
  if (p) {
    const u = safeUrl(p.base);
    if (u) {
      host.value = u.hostname;
      port.value = u.port || "8787";
    }
    code.value = p.code;
    void submit();
  }
});

function safeUrl(b: string): URL | null {
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

async function submit(): Promise<void> {
  if (busy.value) return;
  error.value = "";
  if (!host.value.trim()) {
    error.value = "请填写电脑的 IP 地址";
    return;
  }
  if (!/^\d{6}$/.test(code.value.replace(/[\s-]/g, ""))) {
    error.value = "配对码是 6 位数字";
    return;
  }
  busy.value = true;
  try {
    const r = await pair(base.value, code.value);
    toastOk(`已连接到 ${r.device_name}`);
  } catch (e) {
    error.value = e instanceof ApiError
      ? (e.userHint || e.friendly)
      : "配对失败，请检查地址与配对码";
  } finally {
    busy.value = false;
  }
}

/** 粘贴整条 URL / "IP 码" 文本。 */
async function submitPaste(): Promise<void> {
  const parsed = parsePairInput(pasteText.value);
  if (!parsed) {
    toastError("没认出这段内容，请粘贴电脑上显示的链接，或手动填写地址与配对码");
    return;
  }
  const u = safeUrl(parsed.base);
  if (u) {
    host.value = u.hostname;
    port.value = u.port || "8787";
  }
  code.value = parsed.code;
  await submit();
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
      <label class="field">
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
        :disabled="!canSubmit"
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
      手机和电脑要连同一个 Wi-Fi。照片只在局域网内传输，不会上传到互联网。
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
</style>
