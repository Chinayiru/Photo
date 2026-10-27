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
  cloudBaseFromLocation,
  getBase,
  getCloudBase,
  getDeviceId,
  getDeviceName,
  getToken,
  isLocalIPv4,
  normalizeBase,
  relayRootBase,
  savePairing,
  setGuest,
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
/** 兑换游客邀请的响应（`POST /api/guest/redeem`）。 */
export interface GuestRedeemResponse {
  client_token: string;
  client_id: string;
  device_id: string;
  device_name: string;
  label: string;
  expires_at: number;
}

/**
 * 从地址栏 `#guest=pbg1.xxx.yyy` 里取邀请码。
 *
 * 放 fragment 而不是 query：fragment 不会发给服务器、不会进访问日志、
 * 也不会出现在 Referer 里（与配对码同一套理由，见 ISSUE-052）。
 */
export function guestInviteFromLocation(): string {
  const raw = String(window.location.hash || "").replace(/^#/, "");
  if (!raw) return "";
  const sp = new URLSearchParams(raw);
  const v = (sp.get("guest") || "").trim();
  return /^pbg1\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/.test(v) ? v : "";
}

/**
 * 游客：用邀请码换一枚会话令牌。
 *
 * ★ 必须 `auth: "none"`：这一步之前这台浏览器什么都还没有，带上任何旧令牌
 *   都会让电脑端按"另一个身份"处理（而且会 401 E2002 把人挡在门外）。
 */
export async function redeemGuest(invite: string): Promise<GuestRedeemResponse> {
  const here = cloudBaseFromLocation();
  const base = here || activeBase();
  if (!base) {
    throw new ApiError({
      status: 0, code: "E1001", message: "打不开这条邀请链接",
      userHint: "链接要在电脑的云端页面里打开（形如 https://域名/d/xxxx/#guest=…）",
    });
  }
  const r = await api.post<GuestRedeemResponse>(
    "/api/guest/redeem", { token: invite },
    { auth: "none", skipAuthLost: true, timeoutMs: 15_000, base },
  );
  savePairing({
    token: r.client_token,
    deviceId: r.device_id,
    deviceName: r.device_name,
    base,
    cloudBase: here || undefined,
  });
  setGuest(true);
  return r;
}

export async function pair(base: string, code: string,
                           expectedDid = ""): Promise<PairResponse> {
  const s = authStore.state;
  // ★ 页面由云端伺服时（`https://域名/d/<id>/`），基址必须取**当前
  //   location**，不能用调用方传进来的值：`PairView.onMounted` 是拿
  //   `window.location.hostname` + `port || 8787` 拼的，在云端页面上会
  //   拼成 `http://pb.example.com:8787` —— 既错了协议、也多了一个端口，
  //   请求直接打不到云端。
  const here = cloudBaseFromLocation();
  const cleanBase = here || normalizeBase(base);
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

  // ★ 这里**不能**先把 base 写进 localStorage。
  //   之前是"先落盘再请求"，于是用户填错一次地址、或者电脑没开，
  //   那个连不上的地址就长期留在 localStorage 里：下次冷启动所有请求都
  //   打到那儿，连"重新配对"的页面都得等它超时；而 `PairView` 每次挂载
  //   都会自动 `submit()`，失败一次就多攒一次限速额度，用户自己把自己
  //   锁在门外（`docs/11` SEC-23）。
  //   改法：基址只随这个请求走（`base` 选项），成功之后由 `savePairing()`
  //   一并落盘。

  // ★ 二维码校验必须在**消费配对码之前**。若先 POST /api/pair 再发现
  //   device_id 不对，服务端已经把那一枚码用掉了（`used_at` 已写），
  //   用户拿着正确的电脑也无法重试，只能回去重新生成一枚码 ——
  //   而"连错电脑"恰恰是最需要能立刻改正的场景。
  if (expectedDid) {
    let actual = "";
    try {
      const ping = await api.get<PingResponse>("/api/system/ping", {
        // 候选地址探活：不带任何凭据（`auth` 默认是 `"client"`，而这里的目标
        // 还不是已配对基址，显式写 `"none"` 让意图留在代码里）。
        auth: "none",
        timeoutMs: 5000, skipAuthLost: true, base: cleanBase,
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
      // 同上：目标不是已配对基址，客户端令牌一律不发
      auth: "none",
      timeoutMs: 15_000,
      // 候选地址只走这一次请求；落盘在下面 `savePairing()` 里发生
      base: cleanBase,
    });
    savePairing({
      token: r.client_token,
      deviceId: r.device_id,
      deviceName: r.device_name,
      base: cleanBase,
      // 页面本身就在云端时，云端基址就是它 —— 顺手落盘，下次冷启动
      // 即使落盘值被清掉也能从 URL 认回来（`cloudBaseFromLocation`）。
      cloudBase: here || undefined,
    });
    s.serverDeviceId = r.device_id;
    s.deviceName = r.device_name;
    s.deviceMismatch = false;
    s.lastError = "";
    // ★ 配对成功 ⇒ 立刻把 `?code=` / `?did=` 从地址栏与历史记录里抹掉
    //   （ISSUE-033）。配对码 300 秒内可重放，而 URL 会留在浏览器历史、
    //   用户截屏、Cloudflare 访问日志里 —— 抹掉是唯一能同时清掉这三处
    //   的办法。失败路径不走这里：用户要能拿着同一段 URL 重试。
    scrubPairQuery();
    // 页面由云端伺服时走的是中继，状态条不能显示"局域网直连"。
    s.conn = here ? "CLOUD" : "DIRECT";
    s.lastOkAt = Date.now();
    return r;
  } finally {
    s.probing = false;
  }
}

/**
 * 账号登录自动配对（`docs/04` §3.5「自动配对」）。
 *
 * ## 为什么需要它
 *
 * 家里那台电脑的托盘显示 6 位码 → 手机照抄，这条路要求"人此刻就在电脑
 * 旁边"。人在外面（正是云端通路存在的理由）时根本看不到那 6 位数字，
 * 于是"能不能配对上"取决于有没有人帮他念码。
 *
 * 自动配对把这一步换成**登录账号**：账号名下就这几台电脑，云端自己就能
 * 判断"这台是不是你的"，配对码对它没有任何额外信息量。
 *
 * ## 为什么必须打到 `origin` 根，而不是 `here`
 *
 * `here` 是 `https://域名/d/<32hex>`（设备前缀）。`/api/auth/login` 与
 * `/api/devices/{id}/pair` 都是**中继根**上的接口，拼在 `/d/<id>` 后面
 * 会被当成"转发给电脑的请求"，落到电脑端 404。
 *
 * ## 为什么登录与配对是两次请求
 *
 * 中继的 `account_token` 有 7 天寿命、可撤销（`cloud/relay/auth.py`）。
 * 把它换成一次性的配对动作之后就不必留在手机上——只落盘 `client_token`
 * （90 天、可按设备撤销）。少存一枚凭据，泄露面就少一层。
 */
export async function pairWithAccount(
  email: string,
  password: string,
): Promise<PairResponse> {
  const s = authStore.state;
  const here = cloudBaseFromLocation();
  if (!here) {
    throw new ApiError({
      status: 0,
      code: "E1001",
      message: "这个页面不支持账号登录",
      userHint: "当前是局域网直连页面，请用电脑上显示的 6 位配对码",
    });
  }
  const mail = email.trim();
  if (!mail || !password) {
    throw new ApiError({
      status: 0, code: "E1001", message: "请填写邮箱和密码",
      userHint: "请填写 PhotoBridge 账号的邮箱与密码",
    });
  }
  // `here` 形如 `https://域名/d/<32hex>`，最后一段就是设备 id
  const did = here.slice(here.lastIndexOf("/") + 1);
  const origin = new URL(here).origin;

  s.probing = true;
  try {
    const login = await api.post<{ account_token?: string }>(
      "/api/auth/login",
      { email: mail, password },
      // 登录接口自己就是发凭据的那一端，不能再带凭据
      { base: origin, skipAuthLost: true, auth: "none", timeoutMs: 15_000 },
    );
    const accountToken = String(login.account_token || "");
    if (!accountToken) {
      throw new ApiError({
        status: 0, code: "E2005", message: "登录失败",
        userHint: "邮箱或密码不正确",
      });
    }

    const r = await api.post<PairResponse>(
      `/api/devices/${did}/pair`,
      { auto: true, client_name: clientName() },
      {
        base: origin,
        skipAuthLost: true,
        timeoutMs: 15_000,
        // 账号三态：这条走的是账号令牌，不是客户端令牌
        auth: "account",
        accountToken,
      },
    );

    applyPaired(r, here);
    return r;
  } finally {
    s.probing = false;
  }
}


/** 中继首页列出来的那台电脑。 */
export interface AccountDevice {
  id: string;
  name: string;
  online: boolean;
}

/**
 * 配对成功后的统一收尾：两条账号入口（设备页 / 中继首页）共用。
 *
 * 必须落 `cloudBase` 并且把 `conn` 直接标成 `"CLOUD"`：页面由中继伺服时
 * 请求本来就一直走中继，写 `"lan"` 会让状态条显示"局域网直连"，
 * 而家里的 `192.168.x.x` 在 4G 上根本不可达。
 */
function applyPaired(r: PairResponse, base: string): void {
  const s = authStore.state;
  savePairing({
    token: r.client_token,
    deviceId: r.device_id,
    deviceName: r.device_name,
    // 落的是云端基址：以后每次请求都走中继，家里的 IP 变了也不影响。
    base,
    cloudBase: base,
  });
  s.serverDeviceId = r.device_id;
  s.deviceName = r.device_name;
  s.deviceMismatch = false;
  s.lastError = "";
  scrubPairQuery();
  s.conn = "CLOUD";
  s.lastOkAt = Date.now();
}

/**
 * 中继**首页**（`https://域名/`）上的账号登录：URL 里没有设备 id，
 * 所以登录后要把这个账号下的电脑列出来。
 *
 * 返回值故意把 `token` 也交给调用方：多台电脑时用户还要再点一次，
 * 不应该让他重新登录一遍（口令已经在这里用完就该从内存里消失）。
 */
export async function loginAccount(
  email: string,
  password: string,
  baseOverride?: string,
): Promise<{ token: string; devices: AccountDevice[] }> {
  const base = (baseOverride || relayRootBase()).replace(/\/+$/, "");
  if (!base) {
    throw new ApiError({
      status: 0,
      code: "E1001",
      message: "没有可用的服务器地址",
      userHint: "请填写电脑连着的云端地址，或改用电脑上显示的 6 位配对码",
    });
  }
  const mail = email.trim();
  if (!mail || !password) {
    throw new ApiError({
      status: 0, code: "E1001", message: "请填写邮箱和密码",
      userHint: "请填写 PhotoBridge 账号的邮箱与密码",
    });
  }
  const login = await api.post<{ account_token?: string }>(
    "/api/auth/login",
    { email: mail, password },
    { base, skipAuthLost: true, auth: "none", timeoutMs: 15_000 },
  );
  const token = String(login.account_token || "");
  if (!token) {
    throw new ApiError({
      status: 0, code: "E2005", message: "登录失败",
      userHint: "邮箱或密码不正确",
    });
  }
  const list = await api.get<{
    devices?: Array<{ id?: string; name?: string; online?: boolean }>;
  }>("/api/devices", {
    base,
    skipAuthLost: true,
    timeoutMs: 15_000,
    auth: "account",
    accountToken: token,
  });
  const devices: AccountDevice[] = (list.devices || []).map((d) => ({
    id: String(d.id || ""),
    name: String(d.name || "未命名电脑"),
    online: !!d.online,
  })).filter((d) => d.id.length > 0);
  return { token, devices };
}

/** 首页选完电脑之后，用账号令牌直接配（`auto:true`，不需要 6 位码）。 */
export async function pairAccountDevice(
  deviceId: string,
  token: string,
  baseOverride?: string,
): Promise<PairResponse> {
  const origin = (baseOverride || relayRootBase()).replace(/\/+$/, "");
  if (!origin) {
    throw new ApiError({
      status: 0, code: "E1001", message: "这个页面不支持账号登录",
      userHint: "请刷新页面后重试",
    });
  }
  const r = await api.post<PairResponse>(
    `/api/devices/${deviceId}/pair`,
    { auto: true, client_name: clientName() },
    {
      base: origin,
      skipAuthLost: true,
      timeoutMs: 15_000,
      auth: "account",
      accountToken: token,
    },
  );
  applyPaired(r, `${origin}/d/${deviceId}`);
  return r;
}

/**
 * 给云端看的设备名（会出现在「设备管理」里，用户靠它认出该撤销哪一台）。
 *
 * `navigator.userAgent` 只用来**分档**，不原样上报：一长串 UA 在设备列表
 * 里会把名字撑爆，而用户需要的是"这是哪台手机"。
 */
function clientName(): string {
  const ua = navigator.userAgent;
  const name = /iPhone/.test(ua) ? "iPhone 浏览器"
    : /iPad/.test(ua) ? "iPad 浏览器"
      : /Android/.test(ua) ? "Android 手机"
        : /Windows/.test(ua) ? "Windows 浏览器"
          : /Macintosh/.test(ua) ? "Mac 浏览器"
            : "网页浏览器";
  return name.slice(0, 64);
}

/**
 * 配对成功后把 URL 上的 `?code=` / `?did=` 抹掉（ISSUE-033）。
 *
 * ## 为什么必须抹
 *
 * 配对码的 TTL 是 300 秒（`backend/app/handlers/auth.py`）。配对成功之后
 * 这段 query 仍然留在浏览器历史、用户屏幕截图、以及 Cloudflare 的访问
 * 日志里 —— 三条都是**攻击者拿得到**的地方。5 分钟窗口内任何人重放一次
 * 就能拿到一枚自己的 client_token。（服务端侧"一码一用"只挡住同一枚码
 * 的第二次消费，挡不住这枚码在另一个浏览器里的第一次消费。）
 *
 * ## 为什么只动 query
 *
 * 保留 `pathname` 与 `hash`：路由用的是 hash（`#/pair?code=...`）时把
 * hash 一起抹掉会把用户从当前页弹回首页；`pathname` 是云端 `/d/<id>/`
 * 那种部署的基址来源（`cloudBaseFromLocation`），抹掉它等于把 App 自己
 * 弄丢。`replaceState` 而不是 `pushState`：不能在历史里留一条"返回键
 * 一按就回到带配对码的页面"的记录。
 *
 * 失败时不调用（`pair()` 抛异常就直接返回了）：用户要能拿着同一段 URL
 * 重试 —— 配对码没被消费时抹掉 URL 只会逼他重新扫码。
 */
function scrubPairQuery(): void {
  try {
    // fragment 里带过配对码（`#code=…`）就整个丢掉：留着等于把码写进
    // 浏览器历史。不带码的 fragment（路由）必须保留，否则会把用户
    // 弹回首页。
    const hash = /(?:^|[?&#])(?:code|c|pair|did)=/.test(window.location.hash)
      ? ""
      : window.location.hash;
    window.history.replaceState(null, "", window.location.pathname + hash);
  } catch {
    /* 极少数内嵌 WebView 禁用 replaceState：抹不掉也不能让配对算失败 */
  }
}

/** 退出配对：清空 localStorage 与内存状态。 */
export function unpair(): void {
  clearPairing();
  authStore.reset();
  authStore.state.conn = "IDLE";
}

/* ------------------------------------------------------------------ *
 * 配对输入的可信主机判定（ISSUE-033）
 * ------------------------------------------------------------------ */

/**
 * 「像局域网的地址」——**判定只有一份**，在 `api/storage.ts: isLocalIPv4()`。
 *
 * 这里保留这个名字是为了让调用点读起来仍然是"这是不是一台局域网机器"，
 * 实现直接委托过去。曾经这里和 `isLanHost()` 各写了一份私有网段判断，
 * 两份都只认 RFC1918 —— 于是 Tailscale 的 `100.x` 地址在一处被当成公网
 * （`schemeFor()` 补 https，连不上）、在另一处被当成中继首页
 * （`relayRootBase()` 不为空，配对页渲染成"账号登录"，还去找电脑上并不
 * 存在的 `/api/auth/login`，报"内容不存在或已被删除"）。两份判断分开写，
 * 就必然会在某天只修好其中一份。
 */
function isPrivateIPv4(host: string): boolean {
  return isLocalIPv4(host);
}

/** 用户**已经配对过**的 base 主机（`pb.base` / `pb.cloud` 里那两个）。 */
function pairedHosts(): Set<string> {
  const out = new Set<string>();
  for (const b of [getBase(), getCloudBase()]) {
    if (!b) continue;
    try {
      out.add(new URL(b).hostname.toLowerCase());
    } catch {
      // 落盘值畸形（用户手改过 localStorage）就不进白名单：
      // 白名单只能收窄，不能因为一条脏数据把门开大
    }
  }
  return out;
}

/**
 * 这个主机能不能当配对目标（ISSUE-033）。
 *
 * ## 为什么要有白名单
 *
 * 配对 = 把手机上的 client_token 交给这个地址，之后**所有照片**都从它那里
 * 取。所以"接受任意主机"等于"社工一条链接就能把相册指向攻击者服务器"，
 * 而且攻击者可以顺手拿到配对码。收窄成"只可能是自己电脑的地址"之后，
 * 一条贴进聊天窗口的链接就不再是攻击面。
 *
 * 允许的四类（外加"当前页面的源"）：
 *   - 私有网段 / 回环 IPv4 —— 局域网直连；
 *   - `localhost` —— 真机调试；
 *   - `*.trycloudflare.com` —— 一期推荐的云端隧道；
 *   - 用户自己配对过的主机 —— 自建域名、固定公网 IP、或换过端口的同一台；
 *   - 当前页面自己的 hostname —— 用户已经打开了这个源上的 App，配对目标
 *     本来就被它决定了；自建域名的部署也只能靠这一条把"只输 6 位码"留住。
 */
function isPairHostAllowed(host: string): boolean {
  const h = host.trim().toLowerCase();
  if (!h) return false;
  if (h === "localhost" || h.endsWith(".localhost")) return true;
  if (h.endsWith(".trycloudflare.com")) return true;
  if (isPrivateIPv4(h)) return true;
  if (pairedHosts().has(h)) return true;
  try {
    if (h === window.location.hostname.toLowerCase()) return true;
  } catch {
    /* 拿不到 location（极少数内嵌 WebView）就当没这条，按拒绝处理 */
  }
  return false;
}

/**
 * 拼 base 用哪个协议（ISSUE-033）。
 *
 * 两条硬规则：
 *   - 用户写了 `https://` 就**绝不**降级成 `http://`（明文降级等于把令牌
 *     和照片放到同 Wi-Fi 的可嗅探链路上）；
 *   - 没写 scheme 时按主机补：私有网段补 `http://`（局域网直连是明文，
 *     补 https 会直接连不上），其他一律补 `https://`。
 *
 * 用户明确写了 `http://` 时保留明文，但只限局域网主机 —— 公网主机上写
 * `http://` 基本等于把明文协议写进链接，升级成 https 不会有任何损失。
 */
function schemeFor(host: string, rawScheme: string): string {
  const h = host.trim().toLowerCase();
  if (rawScheme === "https") return "https";
  const lan = isPrivateIPv4(h) || h === "localhost" || h.endsWith(".localhost");
  return lan ? "http" : "https";
}

/** 用 host + port 拼出基址（`host` 只放主机名，端口单独给）。 */
function pairBase(host: string, port: string, rawScheme: string): string {
  const h = host.trim().replace(/\/+$/, "");
  const p = port.trim();
  const withPort = /:\d+$/.test(h) ? h : (p ? `${h}:${p}` : h);
  return normalizeBase(`${schemeFor(h, rawScheme)}://${withPort}`);
}

/**
 * 拒绝一个配对目标时的错误（ISSUE-033）。
 *
 * ★ 不能返回 null 让调用方显示"没认出这段内容"：那样用户只会重试同一段
 * 链接。这里要说清楚"地址不被接受"，并指向正确入口（扫二维码 / 粘贴电脑
 * 给出的链接），否则一个自建了域名却还没配对过的用户会以为 App 坏了。
 */
function rejectPairHost(host: string): ApiError {
  const shown = host || "（空地址）";
  return new ApiError({
    status: 0,
    code: "E1001",
    message: `不接受的配对地址：${shown}`,
    userHint: `没有把「${shown}」当作电脑地址：它不在局域网里，也不像自己的云端。`
      + "请扫描电脑上显示的二维码，或粘贴电脑给出的那条链接",
  });
}

/**
 * 用带参数的 URL 直接配对（托盘二维码扫出来就是这种链接）。
 *
 * 支持的形式：
 *   `http://192.0.2.10:8787/?code=123456`
 *   `http://192.0.2.10:8787/#/pair?code=123456`
 *   `http://192.0.2.10:8787/pair?c=123456&h=192.0.2.10&p=8787`
 * 也接受直接粘贴 `192.0.2.10:8787 123456` 这种"地址 + 码"。
 *
 * ★ 解析不出内容返回 `null`（调用方提示"没认出这段内容"）；**识别出来了
 *   但主机不可信**则抛 `ApiError`（ISSUE-033）—— 两者是不同的处境，
 *   混成同一个 null 会让"被拒绝"看起来像"没看懂"。
 */
export function parsePairInput(raw: string):
{ base: string; code: string; did: string } | null {
  const text = raw.trim();
  if (!text) return null;

  // ① 形如 URL：从中抠 code 与 host
  if (/^(https?:\/\/)?[\d.a-zA-Z-]+(:\d+)?(\/|$)/.test(text) && /[?&#]/.test(text)) {
    const rawScheme = /^https:\/\//i.test(text) ? "https"
      : /^http:\/\//i.test(text) ? "http" : "";
    try {
      const u = new URL(/^https?:\/\//i.test(text) ? text : `http://${text}`);
      // fragment 里的参数也算：扫码链接是 `#code=…`（不进服务端日志）
      const hashParamsOfUrl = hashParams(u.hash);
      const code = u.searchParams.get("code")
        || hashParamsOfUrl.get("code")
        || u.searchParams.get("c")
        || u.searchParams.get("pair")
        || "";
      const host = (u.searchParams.get("h") || u.hostname).trim();
      const explicitPort = u.searchParams.get("p") || u.port;
      if (code) {
        // ★ 主机白名单必须在**返回之前**过一遍（ISSUE-033）：这条路径
        //   原本接受任意主机，于是一条社工链接就能把相册指向别处。
        if (!isPairHostAllowed(host)) throw rejectPairHost(host);
        const scheme = schemeFor(host, rawScheme);
        // 端口缺省只在明文（局域网）下补 8787；https 走默认 443，
        // 补上 `:8787` 会把云端链接打到中继根本不监听的口上。
        const port = explicitPort || (scheme === "http" ? "8787" : "");
        return {
          base: pairBase(host, port, rawScheme),
          code: code.replace(/[\s-]/g, ""),
          // 粘贴整条链接时也带上（`#/pair?code=..&did=..` 形式从 hash 里找）
          did: (u.searchParams.get("did")
            || hashParamsOfUrl.get("did")
            || "").trim(),
        };
      }
    } catch (e) {
      // ★ 白名单拒绝的 ApiError 必须原样抛出：被这个"落到文本解析"的
      //   catch 吞掉，就等于静默接受了攻击者给的主机（ISSUE-033 的原症状）。
      if (e instanceof ApiError) throw e;
      /* 解析不出 URL：落到下面的文本解析 */
    }
  }

  // ② 形如 "192.0.2.10:8787 123456" / "192.0.2.10 123456"
  const m = text.match(/(\d{1,3}(?:\.\d{1,3}){3})(?::(\d+))?[\s,，]+(\d{6})/);
  if (m) {
    const host = m[1]!;
    // 这种形态的地址只可能是局域网电脑：公网 IP 一律拒绝
    if (!isPrivateIPv4(host)) throw rejectPairHost(host);
    return { base: pairBase(host, m[2] || "8787", ""), code: m[3]!, did: "" };
  }

  // ③ 只有 6 位码（地址用当前页面的 host）
  if (/^\d{6}$/.test(text.replace(/[\s-]/g, ""))) {
    const host = window.location.hostname;
    if (!isPairHostAllowed(host)) throw rejectPairHost(host);
    return {
      base: pairBase(host, window.location.port,
                     window.location.protocol === "https:" ? "https" : "http"),
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
/**
 * 把 `location.hash` 解析成参数表。
 *
 * 托盘二维码编的是 `#code=123456&did=…`（fragment，见 `tray.pair_qr_url`）；
 * 历史形态 `#/pair?code=123456` 也要继续认（已经发出去的截图、书签）。
 * 两种都归一到同一个 `URLSearchParams`，调用方不必关心是哪种。
 */
function hashParams(rawHash: string): URLSearchParams {
  const body = rawHash.replace(/^#/, "");
  const q = body.indexOf("?");
  return new URLSearchParams(q >= 0 ? body.slice(q + 1) : body);
}

export function pairParamsFromLocation():
{ base: string; code: string; did: string } | null {
  const search = new URLSearchParams(window.location.search);
  const hash = hashParams(window.location.hash);
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
