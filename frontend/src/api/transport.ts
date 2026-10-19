/**
 * 双通路探测与切换（`docs/03` §5.1、`docs/09` P1-8）。
 *
 * ## 它解决什么问题
 *
 * 手机在家用 Wi-Fi 时应该直连电脑（`http://192.168.x.x:8787`，快、不占云带宽）；
 * 出了门切到 4G 就只能走云端中继（`https://域名/d/<id>`，所有请求经云端转给
 * 电脑，再由电脑连回云端）。
 *
 * 关键约束是**业务代码零分支**：两条通路走完全相同的 `/api/**` 路径，
 * 差别只有基址。所以本模块唯一的产物就是"当前该用哪个基址"这一个值，
 * 写进 `localStorage` 的 `pb.via`，由 `client.ts: currentBase()` 每次请求读一次。
 *
 * ## 状态机（照抄 `docs/03` §5.1.3 的转换表）
 *
 * ```text
 *   IDLE ──► PROBING ──200&id匹配──► DIRECT
 *              │                        │
 *              │失败                     │连续 3 次请求失败
 *              ▼                        ▼
 *        CLOUD_PROBING ──200──► CLOUD   RE-PROBE
 *              │                 │        │
 *              │失败             │503     │
 *              ▼                 ▼        ▼
 *           OFFLINE ◄────────────┴───（重新走 PROBING → CLOUD_PROBING）
 * ```
 *
 * ## 三个必须回答的问题
 *
 * **① 为什么不每次都试直连。** 直连探测有 1.5 秒超时成本。若每次请求都试，
 * 用户在 4G 上会持续感受到 1.5 秒延迟。所以"记住失败"：本会话内直连失败过
 * 就不再试，直到 `online` 事件或页面重新可见（`docs/03` §5.1.4）。
 *
 * **② 为什么蜂窝网络不试直连。** 局域网地址在蜂窝网络下**必然不可达**，
 * 试一次就是白等 1.5 秒。`navigator.connection.type === "cellular"` 能判就判，
 * 判不了（iOS Safari 没有这个 API）靠"失败记忆"兜底。
 *
 * **③ `RE-PROBE` 为什么要冷却。** 用户走进电梯、出电梯，网络抖动会连续
 * 触发"连续 3 次失败"。没有冷却就是探测风暴：每次探测 1.5 + 5 秒，
 * 手机发热、电量掉、而用户什么都没做。`RE_PROBE_COOLDOWN` 限 5 分钟 1 次。
 *
 * ## 与 `client.ts` 的关系
 *
 * 单向依赖：本模块 import `client.ts` 的类型与工具，`client.ts` **不** import
 * 本模块。请求失败通过 `netstate.ts` 这个中介上报，避免 ESM 循环依赖。
 */

import type { PingResponse, SystemInfo } from "./types";
import {
  activeBase,
  cloudBaseFromLocation,
  currentVia,
  getBase,
  getCloudBase,
  getDeviceId,
  getToken,
  saveCloudBase,
  setVia,
  type Via,
} from "./storage";
import {
  reportTransportOk,
  setFailureHandler,
  type FailureKind,
} from "./netstate";

/* ------------------------------------------------------------------ *
 * 常量
 * ------------------------------------------------------------------ */

/** 局域网探测超时（`docs/03` §5.1.3）。 */
export const LAN_PROBE_TIMEOUT_MS = 1500;
/** 云端探测超时（走公网 + 隧道往返，给足 5 秒）。 */
export const CLOUD_PROBE_TIMEOUT_MS = 5000;
/** 重探冷却：5 分钟内最多 1 次（防探测风暴）。 */
export const RE_PROBE_COOLDOWN_MS = 5 * 60 * 1000;

/** 状态（`docs/03` §5.1.3 那张表的左列）。 */
export type PathState =
  | "IDLE"
  | "PROBING"
  | "CLOUD_PROBING"
  | "DIRECT"
  | "CLOUD"
  | "RE_PROBE"
  | "OFFLINE";

/* ------------------------------------------------------------------ *
 * 模块状态
 * ------------------------------------------------------------------ */

interface Memory {
  /** 直连失败的时刻（会话内）；null = 没失败过。 */
  lanFailedAt: number | null;
  /** 云端失败的时刻。 */
  cloudFailedAt: number | null;
  /** 上次重探的时刻（`RE-PROBE` 冷却用）。 */
  lastReProbeAt: number;
  /** 上次问电脑端"我的云端基址是什么"的时刻（会话内，防每次请求都问）。 */
  cloudBaseAskedAt: number;
}

const memory: Memory = {
  lanFailedAt: null,
  cloudFailedAt: null,
  lastReProbeAt: 0,
  cloudBaseAskedAt: 0,
};

let state: PathState = "IDLE";
let probing = false;
/** 最近一次探测的失败原因（给状态条做 tooltip）。 */
let lastError = "";
/** 最近一次成功探测到的设备名/版本（状态条用）。 */
let deviceName = "";
let version = "";
/** "连到了另一台电脑"：基址通但 device_id 不是配对时那台。 */
let deviceMismatch = false;
/** 状态变化的订阅者。 */
const watchers = new Set<() => void>();

function setState(next: PathState): void {
  if (state === next) return;
  state = next;
  for (const w of watchers) {
    try {
      w();
    } catch {
      /* 单个订阅者抛错不能影响别的订阅者 */
    }
  }
}

/** 订阅状态变化。返回取消订阅函数。 */
export function onPathChange(h: () => void): () => void {
  watchers.add(h);
  return () => watchers.delete(h);
}

export function pathState(): PathState {
  return state;
}

export function pathError(): string {
  return lastError;
}

export function pathDeviceName(): string {
  return deviceName;
}

export function pathVersion(): string {
  return version;
}

export function pathMismatch(): boolean {
  return deviceMismatch;
}

/** 当前生效通路（读 localStorage，不读内存 —— 以落盘值为准）。 */
export { currentVia };

/* ------------------------------------------------------------------ *
 * 探测
 * ------------------------------------------------------------------ */

/**
 * 判断当前网络是不是蜂窝（判不了返回 false）。
 *
 * `navigator.connection` 只有 Chromium 系支持；iOS Safari 上根本没有这个
 * 属性。判不了时**当作 Wi-Fi**（返回 false）：多试一次直连最坏是白等
 * 1.5 秒，而误判成蜂窝会让用户在家也走云端 —— 那是在替用户花流量。
 */
function isCellular(): boolean {
  const conn = (navigator as unknown as {
    connection?: { type?: string };
  }).connection;
  return conn?.type === "cellular";
}

/**
 * 现在是否值得先试局域网（`docs/03` §5.1.4 的 `shouldTryLan()`）。
 *
 * 四个条件全部满足才试：在线、非蜂窝、有缓存地址、本会话没失败过
 * （或冷却期已过）。
 */
export function shouldTryLan(): boolean {
  if (!navigator.onLine) return false;
  // ★ 页面本身由**云端**伺服时（`https://域名/d/<id>/`），`getBase()` 里
  //   存的很可能就是云端基址（配对就发生在云端 origin 上），于是"试直连"
  //   实际是把同一个云端地址又打一遍 —— 白等 1.5 秒，还会把状态条标成
  //   "局域网直连"（用户在外面看到"直连"会以为令牌泄漏到了公网）。
  //   而真正的局域网地址在 https 页面里根本发不出去：`http://192.168.x.x`
  //   是 Mixed Content，浏览器在发出请求之前就拦掉了。
  if (cloudBaseFromLocation()) return false;
  if (isCellular()) return false;
  if (!getBase()) return false;
  if (memory.lanFailedAt !== null
      && Date.now() - memory.lanFailedAt < RE_PROBE_COOLDOWN_MS) {
    return false;
  }
  return true;
}

/** 现在是否有云端候选（没配云端就只能直连）。 */
export function hasCloud(): boolean {
  return Boolean(getCloudBase());
}

/** 探测某个基址：`GET {base}/api/system/ping`。成功返回 ping 体。 */
export async function probeAt(base: string, timeoutMs: number): Promise<PingResponse> {
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(`${base}/api/system/ping`, {
      method: "GET",
      // 探活不需要任何凭据：云端 `/d/**` 的 ping 是白名单路径，
      // 电脑端 `/api/system/ping` 也是 `PUBLIC_PATHS`。
      // 带 `cache: no-store` 是因为反向代理可能缓存 200，
      // 那会让"电脑离线"被缓存成一个"在线"结论。
      cache: "no-store",
      credentials: "omit",
      signal: ctrl.signal,
    });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    return (await res.json()) as PingResponse;
  } finally {
    window.clearTimeout(timer);
  }
}

/**
 * 探测某个具体基址（设置页"测一下这个地址"用）。
 *
 * 与 `probe()` 的区别：它**不改**任何落盘值，也不参与状态机 ——
 * 用户手动输了一个地址想验证连通性，验完了不该顺手把通路切过去。
 */
export async function testBase(base: string): Promise<PingResponse> {
  return probeAt(base, CLOUD_PROBE_TIMEOUT_MS);
}

/** 问电脑端"我的云端基址"的最小间隔（会话内）。 */
const CLOUD_BASE_ASK_INTERVAL_MS = 5 * 60 * 1000;

/**
 * 走局域网问一次电脑端：我在云端的基址是什么（`tunnel.public_base`）。
 *
 * ## 为什么需要这一步
 *
 * 云端基址形如 `https://域名/d/<云端 device_id>`，其中的 id 是**云端**
 * 分配的 32 位十六进制值，与电脑本地 `device_id`（16 位）是两个不同的
 * 东西。用户在配对的二维码/托盘里只看得到局域网地址，因此这个值唯一
 * 可信的来源就是电脑端自己 —— 而电脑端在 HELLO 时已经知道自己该出现
 * 在哪个前缀下（`backend/app/tunnel.py:public_base`）。
 *
 * ## 为什么在"直连成功"之后问
 *
 * 那是唯一一个既知道局域网地址、又拿得到令牌的时刻。手机第一次配对
 * 一定发生在家里（要扫码），所以这一次询问几乎必然成功；学到之后
 * 落盘到 `pb.cloud`，以后出门就靠它。
 *
 * ## 失败为什么静默
 *
 * 云端未配置是**正常状态**（一期形态），不该在状态条上留个错误。
 */
async function learnCloudBase(lanBase: string): Promise<void> {
  const token = getToken();
  if (!token) return;
  if (getCloudBase()) return;                     // 已经知道了
  const now = Date.now();
  if (now - memory.cloudBaseAskedAt < CLOUD_BASE_ASK_INTERVAL_MS) return;
  memory.cloudBaseAskedAt = now;
  const ctrl = new AbortController();
  const timer = window.setTimeout(() => ctrl.abort(), LAN_PROBE_TIMEOUT_MS);
  try {
    const res = await fetch(`${lanBase}/api/system/info`, {
      method: "GET",
      headers: { Authorization: `Bearer ${token}`, "X-PB-Client": "web" },
      cache: "no-store",
      credentials: "omit",
      signal: ctrl.signal,
    });
    if (!res.ok) return;
    const info = (await res.json()) as SystemInfo;
    const base = String(info.tunnel?.public_base || "").trim();
    if (base) saveCloudBase(base);
  } catch {
    // 静默：探测失败/未配置云端都不影响局域网使用
  } finally {
    window.clearTimeout(timer);
  }
}


interface Landing {
  via: Via;
  base: string;
  ping: PingResponse;
}

/**
 * 按状态机走一遍完整探测（不写落盘值，由调用方决定）。
 *
 * 分成"算出该去哪"与"落盘并广播"两步，是因为 `probe()` 与 `reProbe()`
 * 的落盘策略不同（后者要更新冷却时间），而探测逻辑必须只有一份 ——
 * 两份实现的漂移症状是"冷启动能连上、断网重连连不上"。
 */
async function detect(): Promise<Landing | null> {
  const expectDid = getDeviceId();
  const lanBase = getBase();
  const cloudBase = getCloudBase();

  const accept = (via: Via, base: string, ping: PingResponse): Landing | null => {
    if (expectDid && ping.device_id !== expectDid) {
      // 基址通但连到了另一台电脑：**不算**这条通路可用。
      // 直接返回会让用户看到别人的照片，而这正是配对时 device_id 校验
      // 要防的事（`docs/01` AC-5.2）。
      deviceMismatch = true;
      lastError = "连到了另一台电脑";
      return null;
    }
    deviceMismatch = false;
    return { via, base, ping };
  };

  // ---- ① 先试直连 ----
  if (shouldTryLan()) {
    setState("PROBING");
    try {
      const ping = await probeAt(lanBase, LAN_PROBE_TIMEOUT_MS);
      const hit = accept("lan", lanBase, ping);
      if (hit) return hit;
    } catch (e) {
      memory.lanFailedAt = Date.now();
      lastError = describe(e);
    }
  }

  // ---- ② 退到云端 ----
  if (cloudBase) {
    setState("CLOUD_PROBING");
    try {
      const ping = await probeAt(cloudBase, CLOUD_PROBE_TIMEOUT_MS);
      const hit = accept("cloud", cloudBase, ping);
      if (hit) return hit;
    } catch (e) {
      memory.cloudFailedAt = Date.now();
      lastError = describe(e);
    }
  }

  // ---- ③ 都不通 ----
  if (!lanBase && !cloudBase) {
    lastError = "尚未配对";
  } else if (!cloudBase && memory.lanFailedAt !== null) {
    // 只配了局域网且直连失败：这才是真正的"电脑离线"。
    // 如果配了云端却没试到（比如没配对），lastError 已经由上面写好。
    lastError = lastError || "无法连接到电脑";
  }
  setState("OFFLINE");
  return null;
}

function describe(e: unknown): string {
  const name = e instanceof Error ? e.name : "";
  if (name === "AbortError") return "连接超时";
  const msg = e instanceof Error ? e.message : String(e);
  // 浏览器的 fetch 失败信息是 `Failed to fetch`，对用户毫无意义
  if (/Failed to fetch|NetworkError|Load failed/i.test(msg)) {
    return "无法连接到电脑";
  }
  return msg || "无法连接到电脑";
}

/** 把探测结果落盘并广播。 */
function apply(hit: Landing): boolean {
  deviceName = hit.ping.device_name || "";
  version = hit.ping.version || "";
  lastError = "";
  // ★ 先落盘再改内存状态：`client.ts` 的 `currentBase()` 读的是 localStorage，
  //   若先广播 `DIRECT`，订阅者（App.vue）立刻发请求时会读到**旧的** via。
  setVia(hit.via);
  setState(hit.via === "cloud" ? "CLOUD" : "DIRECT");
  reportTransportOk();
  // 顺带学一次云端基址。不 await：它只是"下次出门能用"，与本次
  // 探测结果无关，等它会让状态条晚 1.5 秒才变绿。
  if (hit.via === "lan") void learnCloudBase(hit.base);
  return true;
}

/* ------------------------------------------------------------------ *
 * 对外入口
 * ------------------------------------------------------------------ */

/**
 * 冷启动 / 切回前台 / 用户点状态条时的探测。
 *
 * 不设冷却（用户显式点击要有反应），但仍然受 `shouldTryLan()` 的
 * "失败记忆"约束 —— 否则用户在 4G 上每点一次，就白等 1.5 秒。
 */
export async function probe(): Promise<boolean> {
  if (probing) return state === "DIRECT" || state === "CLOUD";
  if (!getToken() && !getBase() && !getCloudBase()) {
    setState("IDLE");
    return false;
  }
  probing = true;
  try {
    const hit = await detect();
    if (!hit) return false;
    return apply(hit);
  } finally {
    probing = false;
  }
}

/**
 * 请求失败触发的重探（`docs/03` §5.1.3 的 `RE-PROBE`）。
 *
 * 冷却 5 分钟；冷却期内**不探测**，但也不报错 —— 调用方是"请求已经失败"
 * 的路径，这里再抛异常只会把原始错误盖掉。
 */
export async function reProbe(): Promise<boolean> {
  const now = Date.now();
  if (now - memory.lastReProbeAt < RE_PROBE_COOLDOWN_MS) return false;
  if (probing) return false;
  memory.lastReProbeAt = now;
  // 重探时允许再试一次直连（用户可能刚回到家里 Wi-Fi），
  // 但不清 `cloudFailedAt` —— 云端不可达通常是"电脑端隧道断了"，
  // 立刻重试云端没有意义。
  memory.lanFailedAt = null;
  setState("RE_PROBE");
  return probe();
}

/**
 * 用户手动重探（点状态条）。
 *
 * 与 `probe()` 的区别是**清掉失败记忆**再探：用户点状态条最常见的场景
 * 是"刚才明明连着，现在不行了" —— 路由器换了 IP、电脑刚开机、手机从
 * 卧室走到客厅。若沿用 `shouldTryLan()` 的"5 分钟内不再试局域网"，
 * 他会在 4G 上白等 5 秒云端探测，然后看到"电脑离线"，而电脑其实就在
 * 同一个 Wi-Fi 里。
 */
export async function rescan(): Promise<boolean> {
  memory.lanFailedAt = null;
  memory.cloudFailedAt = null;
  return probe();
}

/**
 * 重置失败记忆（`online` 事件、网络切换、页面重新可见时）。
 *
 * `docs/03` §5.1.4：用户在家从 4G 切到 Wi-Fi 后，本次会话原本不会自动
 * 升级到直连 —— 这个函数就是那个"例外"的入口。
 */
export function resetMemory(reason: string): void {
  memory.lanFailedAt = null;
  memory.cloudFailedAt = null;
  lastError = "";
  if (reason) console.info("[transport] 重置探测记忆：", reason);
}

/** 当前基址（状态条/Settings 展示用）。 */
export function currentBaseUrl(): string {
  return activeBase();
}

/**
 * 安装全局钩子。`main.ts` 在挂载前调一次。
 *
 * 幂等：重复调用只是重复注册监听（浏览器会去重同名同引用的监听器）。
 */
export function installTransport(): void {
  setFailureHandler((kind: FailureKind) => {
    // 只有"当前通路失败"才值得重探。云端说 device_offline 时，
    // 若当前正是云端通路，就立刻退探；若当前是直连（不可能收到
    // cloud_offline），忽略。
    if (kind === "cloud_offline" && currentVia() !== "cloud") return;
    void reProbe();
  });

  window.addEventListener("online", () => {
    resetMemory("network_changed");
    // 断网重连后立刻探一次：用户最可能正在看"电脑离线"那一屏，
    // 等他自己点状态条是多余的。
    void probe();
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible") return;
    // `docs/03` §5.1.4：切回前台时，若当前是 OFFLINE 或已降级到 CLOUD，
    // 给一次重新探测直连的机会（用户可能刚走进家门）。
    if (state === "OFFLINE" || state === "CLOUD") {
      resetMemory("visible");
      void probe();
    }
  });
}
