/**
 * 配对 / 连接状态 store。
 *
 * ## 一期到二期的变化
 *
 * 一期只有局域网直连，`probe()` 里就一处 ping。二期加了云端中继之后，
 * "连接状态"变成一台**双通路状态机**（`docs/03` §5.1、`docs/09` P1-8），
 * 实现搬到 `api/transport.ts`：
 *
 * ```text
 *   IDLE ─► PROBING ─200─► DIRECT         （局域网 http://ip:8787）
 *              └─失败─► CLOUD_PROBING ─200─► CLOUD   （https://域名/d/<id>）
 *                            └─失败─► OFFLINE
 * ```
 *
 * 本模块仍然持有 `ConnState`（界面读它），但**不再自己做探测** ——
 * 它订阅 `transport.ts` 的状态变化并镜像进来。依赖方向是单向的：
 *
 * ```text
 *   auth.ts ──► transport.ts ──► client.ts / storage.ts / netstate.ts
 * ```
 *
 * 为什么不把状态机直接写在这个 store 里：`transport.ts` 要在请求失败时
 * 被调用（`netstate.ts` 的中介回调），而请求层不该 import 一个 Vue store ——
 * 那会让 `client.ts` 间接依赖 Vue 运行时，且形成 `store → client → store`
 * 的循环。
 *
 * 职责：
 *   - 冷启动探活（交给 `transport.probe()`）
 *   - 校验 `device_id` 是否仍是同一台电脑（不一致要明确提示）
 *   - 配对（`POST /api/pair` 或云端的 `POST /api/devices/{id}/pair`）
 *   - 退出配对（清 localStorage）
 */

import { computed } from "vue";
import { ApiError, api } from "../api/client";
import type { PairResponse, PingResponse } from "../api/types";
import {
  activeBase,
  clearPairing,
  getBase,
  getCloudBase,
  getDeviceId,
  getDeviceName,
  getToken,
  normalizeBase,
  saveBase,
  savePairing,
} from "../api/storage";
import {
  installTransport,
  onPathChange,
  pathDeviceName,
  pathError,
  pathMismatch,
  pathState,
  pathVersion,
  probe as probeTransport,
  rescan,
  testBase,
} from "../api/transport";
import { createStore } from "./index";

/**
 * `IDLE`          未探测（冷启动瞬间）
 * `PROBING`       正在 ping 局域网
 * `CLOUD_PROBING` 正在 ping 云端
 * `DIRECT`        局域网直连可用
 * `CLOUD`         云端中继可用
 * `RE_PROBE`      通路失败后的重探测（`docs/03` §5.1.3）
 * `OFFLINE`       两条通路都不通
 * `UNAUTHORIZED`  配对失效，需要重新配对
 */
export type ConnState =
  | "IDLE"
  | "PROBING"
  | "CLOUD_PROBING"
  | "DIRECT"
  | "CLOUD"
  | "RE_PROBE"
  | "OFFLINE"
  | "UNAUTHORIZED";

const initial = {
  conn: "IDLE" as ConnState,
  /** 本次探活拿到的设备名（可能与配对时不同 —— 用户重命名了电脑）。 */
  deviceName: "",
  version: "",
  /** 电脑端 device_id，与 localStorage 里记的不一致时置位。 */
  serverDeviceId: "",
  /** "连到了另一台电脑"：base 能通，但 device_id 不是配对时那台。 */
  deviceMismatch: false,
  lastError: "",
  probing: false,
  /** 上次成功探活的时间戳（ms）。 */
  lastOkAt: 0,
};

export const authStore = createStore(initial);

export const isPaired = (): boolean => Boolean(getToken() && (getBase() || getCloudBase()));
export const currentBase = (): string => activeBase();

/** 供模板用的派生值。 */
export const connText = computed<string>(() => {
  switch (authStore.state.conn) {
    case "DIRECT": return "局域网直连";
    case "CLOUD": return "云端中继";
    case "PROBING":
    case "CLOUD_PROBING":
    case "RE_PROBE": return "正在连接…";
    case "OFFLINE": return "电脑离线";
    case "UNAUTHORIZED": return "配对已失效";
    default: return "未连接";
  }
});

/** 状态条的 CSS 类名。 */
export const connClass = computed<string>(() => {
  switch (authStore.state.conn) {
    case "DIRECT": return "ok";
    case "CLOUD": return "cloud";
    case "PROBING":
    case "CLOUD_PROBING":
    case "RE_PROBE": return "busy";
    case "OFFLINE": return "bad";
    case "UNAUTHORIZED": return "bad";
    default: return "idle";
  }
});

/**
 * 探活。冷启动、切回前台、用户点状态条、以及请求连续失败时调用。
 *
 * 真正的探测在 `api/transport.ts`（双通路状态机）。这里只做三件事：
 *   1. 把状态机的进展镜像进 `authStore.state`（界面只读这一个 store）；
 *   2. 校验 `device_id` —— 用户家里两台电脑跑同一个程序、手机连过 A 之后
 *      DHCP 把 IP 分给了 B，此时基址能通、token 却属于 A。后端会返回
 *      401 E2002，看起来像"配对失效"，用户会一头雾水地重新配对。所以
 *      探测时就核对 device_id，不一致直接告诉他"连到了另一台电脑"；
 *   3. 配对失效（401 E2002 / 403 E2004）时把状态置成 `UNAUTHORIZED`。
 *
 * 注意 `transport.probe()` 自己已经做了 device_id 校验，这里再读一次
 * 是为了把值填进 store（`serverDeviceId` / `deviceName` / `version`）。
 */
export async function probe(): Promise<boolean> {
  const s = authStore.state;
  if (!getBase() && !getCloudBase()) {
    s.conn = "IDLE";
    return false;
  }
  s.probing = true;
  try {
    const ok = await probeTransport();
    syncFromTransport();
    s.probing = false;
    return ok;
  } catch (e) {
    const err = e instanceof ApiError ? e : null;
    s.lastError = err?.friendly ?? "无法连接";
    s.conn = err?.isAuthLost ? "UNAUTHORIZED" : "OFFLINE";
    s.probing = false;
    return false;
  }
}

/** 把 `transport.ts` 的状态镜像进本 store。 */
function syncFromTransport(): void {
  const s = authStore.state;
  const st = pathState();
  s.conn = st as ConnState;
  s.deviceMismatch = pathMismatch();
  if (s.deviceMismatch) s.lastError = "连到了另一台电脑";
  else s.lastError = pathError() || s.lastError;
  if (pathDeviceName()) s.deviceName = pathDeviceName();
  if (pathVersion()) s.version = pathVersion();
  if (st === "DIRECT" || st === "CLOUD") {
    s.serverDeviceId = getDeviceId();
    s.lastOkAt = Date.now();
  }
}

/**
 * 安装双通路状态机（`main.ts` 在挂载前调一次，幂等）。
 *
 * 注册之后，`transport.ts` 每次状态变化都会镜像进 store —— 于是
 * **状态条不需要轮询**，也不需要任何组件去订阅 `transport.ts`。
 */
export function setupConnection(): void {
  installTransport();
  onPathChange(syncFromTransport);
  // 冷启动时若已经有落盘的通路，立刻把状态显示成"正在连接…"而不是
  // "未连接"：后者会让用户在首屏看到一秒的"未连接"，像没配对成功。
  if (getBase() || getCloudBase()) authStore.state.conn = "PROBING";
}

/** 探测一个用户手输的基址（设置页"测试这个地址"用）。 */
export { testBase };

/** 是否连到了"另一台电脑"（token 属于 A、当前基址是 B）。 */
export const deviceMismatch = computed<boolean>(() => authStore.state.deviceMismatch);

/**
 * 用户手动重探（点状态条）。
 *
 * 与 `probe()` 的差别：它会**清掉记忆**（"5 分钟内不再试局域网"那条），
 * 然后再走完整状态机。这是用户唯一能手动纠正"刚才明明连着"的入口 ——
 * 没有它，DHCP 换了 IP 之后用户可能要在 5 分钟内反复点都没反应。
 */
export function reconnect(): Promise<boolean> {
  authStore.state.lastError = "";
  return rescan();
}

/**
 * 配对。
 *
 * @param base 形如 `http://192.0.2.10:8787`（会归一化）
 * @param code 6 位数字，中间的空格/连字符会被后端也清一遍，这里先清更友好
 * @param expectedDid 二维码里带的 `device_id`（AC-5.2）。给了就先 ping
 *   核对，**不一致直接拒绝**，一个字节的配对请求都不发。
 */
export async function pair(base: string, code: string,
                           expectedDid = ""): Promise<PairResponse> {
  const s = authStore.state;
  const cleanBase = normalizeBase(base);
  if (!cleanBase) throw new ApiError({ status: 0, code: "E1001", message: "请填写电脑地址" });

  const cleanCode = code.replace(/[\s-]/g, "");
  if (!/^\d{6}$/.test(cleanCode)) {
    throw new ApiError({
      status: 0,
      code: "E1001",
      message: "配对码是 6 位数字",
      userHint: "配对码是电脑上显示的 6 位数字",
    });
  }

  // ★ 先把 base 落到 localStorage，再发请求 —— client.ts 的 baseUrl 是
  //   每次读 localStorage 的 getter，不写进去就发到了旧地址
  saveBase(cleanBase);

  // ★ 二维码校验必须在**消费配对码之前**。若先 POST /api/pair 再发现
  //   device_id 不对，服务端已经把那一枚码用掉了（`used_at` 已写），
  //   用户拿着正确的电脑也无法重试，只能回去重新生成一枚码 ——
  //   而"连错电脑"恰恰是最需要能立刻改正的场景。
  if (expectedDid) {
    let actual = "";
    try {
      const ping = await api.get<PingResponse>("/api/system/ping", {
        timeoutMs: 5000, skipAuthLost: true,
      });
      actual = ping.device_id;
    } catch {
      throw new ApiError({
        status: 0, code: "E1001", message: "无法连接电脑",
        userHint: "连不上这个地址。请确认手机与电脑在同一个 Wi-Fi，且电脑端程序正在运行",
      });
    }
    if (actual !== expectedDid) {
      throw new ApiError({
        status: 0, code: "E2003", message: "这台电脑不是二维码对应的电脑",
        userHint: "扫码连到的是另一台电脑（IP 被重新分配了）。"
          + "请在电脑托盘里重新显示二维码再扫，或手动填写地址与配对码",
      });
    }
  }

  s.probing = true;
  try {
    const r = await api.post<PairResponse>("/api/pair", { code: cleanCode }, {
      // 配对本身是未鉴权的，不要因为它 401 就触发"配对失效"逻辑
      skipAuthLost: true,
      timeoutMs: 15_000,
    });
    savePairing({
      token: r.client_token,
      deviceId: r.device_id,
      deviceName: r.device_name,
      base: cleanBase,
    });
    s.serverDeviceId = r.device_id;
    s.deviceName = r.device_name;
    s.deviceMismatch = false;
    s.lastError = "";
    s.conn = "DIRECT";
    s.lastOkAt = Date.now();
    return r;
  } finally {
    s.probing = false;
  }
}

/** 退出配对：清空 localStorage 与内存状态。 */
export function unpair(): void {
  clearPairing();
  authStore.reset();
  authStore.state.conn = "IDLE";
}

/**
 * 用带参数的 URL 直接配对（托盘二维码扫出来就是这种链接）。
 *
 * 支持的形式：
 *   `http://192.0.2.10:8787/?code=123456`
 *   `http://192.0.2.10:8787/#/pair?code=123456`
 *   `http://192.0.2.10:8787/pair?c=123456&h=192.0.2.10&p=8787`
 * 也接受直接粘贴 `192.0.2.10:8787 123456` 这种"地址 + 码"。
 */
export function parsePairInput(raw: string):
{ base: string; code: string; did: string } | null {
  const text = raw.trim();
  if (!text) return null;

  // ① 形如 URL：从中抠 code 与 host
  if (/^(https?:\/\/)?[\d.a-zA-Z-]+(:\d+)?(\/|$)/.test(text) && /[?&#]/.test(text)) {
    try {
      const u = new URL(/^https?:\/\//i.test(text) ? text : `http://${text}`);
      const code = u.searchParams.get("code")
        || u.searchParams.get("c")
        || u.searchParams.get("pair")
        || "";
      const host = u.searchParams.get("h") || u.hostname;
      const port = u.searchParams.get("p") || u.port || "8787";
      if (code) {
        return {
          base: normalizeBase(`${host}:${port}`),
          code: code.replace(/[\s-]/g, ""),
          // 粘贴整条链接时也带上（`#/pair?code=..&did=..` 形式从 hash 里找）
          did: (u.searchParams.get("did")
            || new URLSearchParams(u.hash.replace(/^#\/?[^?]*\??/, "")).get("did")
            || "").trim(),
        };
      }
    } catch {
      /* 落到下面的文本解析 */
    }
  }

  // ② 形如 "192.0.2.10:8787 123456" / "192.0.2.10 123456"
  const m = text.match(/(\d{1,3}(?:\.\d{1,3}){3})(?::(\d+))?[\s,，]+(\d{6})/);
  if (m) {
    return { base: normalizeBase(`${m[1]}:${m[2] || "8787"}`), code: m[3]!, did: "" };
  }

  // ③ 只有 6 位码（地址用当前页面的 host）
  if (/^\d{6}$/.test(text.replace(/[\s-]/g, ""))) {
    return {
      base: normalizeBase(window.location.host || ""),
      code: text.replace(/[\s-]/g, ""),
      did: "",
    };
  }
  return null;
}

/** 冷启动：从 URL 里看看有没有配对参数。
 *
 * ★ 还会读 `did`（电脑的 device_id，`tray.pair_qr_url` 带上的）。
 * 它防的是一种很隐蔽的错配：二维码是静态的（用户截图存着、或打印出来
 * 贴在机箱上），而局域网 IP 是 DHCP 分的。隔几天再扫，同一个 IP 可能
 * 已经分给了另一台也跑着 PhotoBridge 的电脑 —— 于是"扫码配对"会把
 * 手机绑到别人的电脑上，还会把对方的照片显示出来。
 */
export function pairParamsFromLocation():
{ base: string; code: string; did: string } | null {
  const search = new URLSearchParams(window.location.search);
  const hash = new URLSearchParams(window.location.hash.replace(/^#\/?[^?]*\??/, ""));
  const code = search.get("code") || hash.get("code");
  if (!code) return null;
  const host = window.location.hostname;
  const port = window.location.port || "8787";
  if (!host) return null;
  return {
    base: normalizeBase(`${host}:${port}`),
    code: code.replace(/[\s-]/g, ""),
    did: (search.get("did") || hash.get("did") || "").trim(),
  };
}

export const deviceLabel = computed<string>(() => {
  const s = authStore.state;
  return s.deviceName || getDeviceName() || "电脑";
});
