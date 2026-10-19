/**
 * localStorage 读写封装 —— 配对凭证的唯一入口。
 *
 * 键名是硬约束（任务要求，与 docs/03 §5.2.3 写的 `pb.client_token`/`pb.lan_url`
 * 不一致，**以任务要求为准**）：
 *   - `pb.token`      client_token
 *   - `pb.device_id`  配对时那台电脑的 device_id（用来判断"是不是同一台电脑"）
 *   - `pb.base`       局域网基地址，如 `http://192.0.2.10:8787`
 *
 * 为什么用 localStorage 而不是 IndexedDB：token 是小字符串，localStorage
 * 同步读取，冷启动路径更短（少一次 await）。代价是 XSS 可读 —— 本应用无
 * 第三方脚本、单用户自建，风险可接受。
 */

export const K_TOKEN = "pb.token";
export const K_DEVICE_ID = "pb.device_id";
/** 局域网基址，如 `http://192.0.2.10:8787`（**候选**，不是当前生效值）。 */
export const K_BASE = "pb.base";
/** 云端基址，如 `https://pb.example.com/d/<云端 device_id>`（候选）。 */
export const K_CLOUD = "pb.cloud";
/** 当前生效通路：`"lan"` | `"cloud"`。缺省按 `"lan"`。 */
export const K_VIA = "pb.via";
/** 电脑端显示名，仅用于界面文案（不是鉴权信息）。 */
export const K_DEVICE_NAME = "pb.device_name";
/** 本机自定义的客户端名，作为 `X-PB-Client` 头发给电脑端审计日志。 */
export const K_CLIENT_NAME = "pb.client_name";

/** 两条通路。命名与后端 `ctx.via`（`lan`/`cloud`）对齐。 */
export type Via = "lan" | "cloud";

function read(key: string): string {
  try {
    return window.localStorage.getItem(key) ?? "";
  } catch {
    // Safari 无痕模式下 localStorage 可能直接抛异常
    return "";
  }
}

function write(key: string, value: string): void {
  try {
    if (value) window.localStorage.setItem(key, value);
    else window.localStorage.removeItem(key);
  } catch {
    /* 配额满或被禁用时静默失败：内存里的状态仍然可用 */
  }
}

export function getToken(): string {
  return read(K_TOKEN);
}

export function getDeviceId(): string {
  return read(K_DEVICE_ID);
}

export function getBase(): string {
  return read(K_BASE);
}

/** 云端候选基址（`https://域名/d/<id>`）；没配云端时为空串。 */
export function getCloudBase(): string {
  return read(K_CLOUD);
}

/**
 * 当前生效的通路。
 *
 * 为什么要把它落盘：`client.ts` 的 `currentBase()` 每次请求都读一次
 * （`docs/03` §5.2.2 的硬要求），而它必须在**同步**路径里算出基址 ——
 * `fetch` 之前不能 await。所以"哪条通路"只能是一个同步可读的值。
 *
 * ⚠️ 不能"两条都存着、请求时再挑"：一次时间线请求打 A、下一张缩略图
 * 打 B，用户会看到一半照片来自云端、一半来自局域网，而缓存键（`v=`）
 * 是同一个 —— 排查时完全对不上。
 */
export function getVia(): Via {
  return read(K_VIA) === "cloud" ? "cloud" : "lan";
}

export function setVia(via: Via): void {
  write(K_VIA, via);
}

/**
 * 当前**实际生效**的通路。
 *
 * 与 `getVia()` 的区别只在一种情况下可见：落盘值是 `cloud` 但云端地址
 * 是空的（用户清了站点数据的一部分、或从旧版本升级上来）。那时请求
 * 会回退到局域网（见 `activeBase()`），所以"生效通路"必须是 `lan` ——
 * 否则状态条会显示"云端中继"而实际走的是直连，排查时对不上。
 */
export function currentVia(): Via {
  // 页面由云端伺服 ⇒ 就是云端通路，与落盘值无关（见 `cloudBaseFromLocation`）。
  if (cloudBaseFromLocation()) return "cloud";
  if (getVia() === "cloud" && getCloudBase()) return "cloud";
  return "lan";
}

/**
 * 当前应该用的基址。
 *
 * 云端通路选了但云端地址是空的（用户清了 localStorage 的一部分、
 * 或从旧版本升级上来）时**回退到局域网**而不是返回空串：返回空串会让
 * `client.ts` 抛 `NO_BASE`，界面显示"尚未配对"，而其实局域网是通的。
 */
export function activeBase(): string {
  // ★ 页面本身由云端伺服时（`https://域名/d/<id>/`），**当前域名就是
  //   基址**，比任何落盘值都权威：用户可能从家里那台电脑的局域网地址
  //   打开过同一个 App，`pb.base` 里存的是 `http://192.168.x.x:8787`，
  //   而这句话在 4G 上根本不可达。
  const here = cloudBaseFromLocation();
  if (here) return here;
  if (getVia() === "cloud") {
    const c = getCloudBase();
    if (c) return c;
  }
  return getBase();
}

/**
 * 如果当前页面是被**云端**伺服的，返回它的基址；否则空串。
 *
 * 云端把手机端页面挂在 `/d/<云端 device_id>/` 下（`cloud/relay/pages.py`），
 * 于是 `window.location` 本身就带着基址的全部信息：
 *
 * ```
 * https://pb.example.com/d/9f3c...e1/assets/index-abc.js
 * └──────── origin ────┘└─ 这里 ──┘
 * ```
 *
 * ## 为什么必须从 location 推，而不能只信 `pb.cloud`
 *
 * `pb.cloud` 是**另一个源**上的 localStorage —— 用户在
 * `http://192.0.2.10:8787` 配对时写进去的那条云端基址，在
 * `https://pb.example.com` 这个源上**读不到**（同源策略）。反过来也一样。
 * 所以"页面从云端来"这件事只能靠 URL 判断，靠落盘值会在第一次
 * 从云端打开时判定失败，随后 `activeBase()` 回落到局域网地址，
 * 局域网的 `http://192.168...` 在 4G 上又必然超时 —— 症状是
 * "手机在外面打开云端页面，一直显示连不上电脑"。
 *
 * ## 为什么只认 32 位十六进制
 *
 * `/d/<id>` 里的 id 是云端分配的 128 位能力值（32 位十六进制，
 * `cloud/relay/security.py: new_device_id()`）。收紧到"必须像 id"
 * 可以避免把 `/d/../api/timeline` 这类畸形路径当成基址 —— 那会把
 * 请求发到 `https://域名/d/../api/timeline`，浏览器和中间代理对
 * 路径规范化的处理并不一致。
 */
export function cloudBaseFromLocation(): string {
  try {
    const { protocol, host, pathname } = window.location;
    // 只认 http/https：`file://`、`chrome-extension://` 下没有可用的
    // fetch 基址，强行拼出来只会得到一堆 "Failed to fetch"。
    if (protocol !== "https:" && protocol !== "http:") return "";
    const m = /^\/d\/([0-9a-fA-F]{32})(?:\/|$)/.exec(pathname);
    if (!m) return "";
    return `${protocol}//${host}/d/${m[1]!.toLowerCase()}`;
  } catch {
    return "";
  }
}

/** 配对成功后一次性落盘。 */
export function savePairing(p: {
  token: string;
  deviceId: string;
  deviceName: string;
  base: string;
  /** 云端基址（`POST /api/devices/{id}/pair` 的返回值里带；直连配对时没有）。 */
  cloudBase?: string;
}): void {
  write(K_TOKEN, p.token);
  write(K_DEVICE_ID, p.deviceId);
  write(K_DEVICE_NAME, p.deviceName);
  write(K_BASE, normalizeBase(p.base));
  write(K_CLOUD, normalizeBase(p.cloudBase ?? ""));
  // 刚配对完的通路：在电脑边扫码是直连；**页面由云端伺服时就是云端**
  // （P1-13 之后二维码主入口指向云端，这是最常见的情形）。写死 `"lan"`
  // 会让状态条在第一次探测完成前显示"局域网直连"，而请求其实一直走中继
  // —— 用户在外面看到"直连"会以为令牌泄到了公网。
  write(K_VIA, cloudBaseFromLocation() ? "cloud" : "lan");
  // ★ 同一浏览器里 `base` 变了就必须重发 cookie：cookie 是**按
  // host:port 隔离**的，换一台电脑（或换了端口）时旧 cookie 不会
  // 跟着走，但也不会被覆盖，于是新电脑上缩略图全部 401 而 JS
  // 请求用新 Bearer 头一切正常 —— 症状正是"时间线有照片但全灰"。
  setTokenCookie(p.token);
}

/** 只改基地址（例如用户在设置页手动改了 IP/端口）。 */
export function saveBase(base: string): void {
  write(K_BASE, normalizeBase(base));
}

/** 只改云端基地址（配对响应里带回来，或用户在设置页填的）。 */
export function saveCloudBase(base: string): void {
  write(K_CLOUD, normalizeBase(base));
}

/** 退出配对：清掉全部凭证，但保留客户端名（下次配对不用重输）。 */
export function clearPairing(): void {
  write(K_TOKEN, "");
  write(K_DEVICE_ID, "");
  write(K_DEVICE_NAME, "");
  write(K_BASE, "");
  write(K_CLOUD, "");
  write(K_VIA, "");
  clearTokenCookie();
}

export function getDeviceName(): string {
  return read(K_DEVICE_NAME);
}

export function setClientName(name: string): void {
  write(K_CLIENT_NAME, name);
}

export function getClientName(): string {
  return read(K_CLIENT_NAME);
}

/* ------------------------------------------------------------------ *
 * token cookie
 * ------------------------------------------------------------------ */

/**
 * cookie 名，与后端 `backend/app/auth.py: CLIENT_COOKIE` **必须一致**。
 * 改一边不改另一边，症状是缩略图全部 401（灰块）而 JS 请求全部正常。
 */
export const COOKIE_NAME = "pb_token";

/**
 * ★★ 为什么 token 除了 localStorage 还要放一份到 cookie：
 *
 * 浏览器原生 `<img>` 与 `EventSource` **无法发送自定义请求头**，
 * `Authorization: Bearer` 根本传不进去。而缩略图走的就是
 * `<img src=".../thumb">`（`PhotoCell.vue`），于是每一张都 401
 * E2002，`onError` 把格子标成 failed 渲染灰块 ——
 * 表现为"照片一张都加载不出来"。
 *
 * 否决掉的替代方案：
 *   - `?token=` 进 URL：token 会落进浏览器缓存键、服务器访问日志、
 *     `Referer`，且与 `Vary: Authorization` 的长缓存策略冲突；
 *   - `fetch` + `URL.createObjectURL`：摧毁 `loading="lazy"` 与
 *     HTTP 缓存（懒加载是 `PhotoCell.vue` 的硬约束）；
 *   - Service Worker 加头：SW 是 **secure context** 专属 API，
 *     `http://192.168.x.x` 上根本注册不了。
 *
 * ⚠️ 不设 `Secure`：局域网是明文 HTTP，带 Secure 的 cookie 会被
 * 浏览器**直接丢弃**，症状是"改了代码但缩略图仍然 401"。
 * ⚠️ 不设 `SameSite=Strict`：那会让用户从别的 App 点分享链接进来时
 * 不携带 cookie，显示未配对。`Lax` 与后端下发的值保持一致。
 */
/**
 * ★ 为什么 `Max-Age` 传 400 天而不是 90 天：
 *
 * cookie 的寿命**必须不短于**服务端 token 的寿命。两个方向都会出错：
 *
 *   - cookie 活得比 token 长 → 浏览器一直带着一枚服务端必然拒绝的凭证，
 *     每次请求都是 401 再清掉，用户看到缩略图间歇性变灰块；
 *   - cookie 短于 token → 到第 90 天 `<img>` 突然全部灰掉，而 JS 请求
 *     靠 Bearer 头一切正常。
 *
 * 服务端才是真正的权威：`auth.token_ttl_days` 可配置（`0` = 永不过期），
 * 前端**读不到**这个配置，所以这里用一个「足够长」的值，
 * 让服务端的判定永远先于 cookie 的自然过期生效。
 * 400 天是 RFC 6265bis 建议的浏览器上限（超过会被静默夹到 400 天），
 * 与 `backend/app/auth.py: COOKIE_MAX_AGE` 一致。
 */
export function setTokenCookie(token: string): void {
  if (!token) return clearTokenCookie();
  try {
    document.cookie =
      `${COOKIE_NAME}=${encodeURIComponent(token)}; Path=/; ` +
      `Max-Age=${400 * 86400}; SameSite=Lax`;
  } catch {
    /* 禁用 cookie 时静默失败：JS 请求仍走 Authorization 头 */
  }
}

export function clearTokenCookie(): void {
  try {
    document.cookie = `${COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Lax`;
  } catch {
    /* 同上 */
  }
}

/** 浏览器当前是否带着 token cookie。用于 `client.ts` 的自愈判断。 */
export function hasTokenCookie(): boolean {
  try {
    return document.cookie
      .split(";")
      .some((c) => c.trim().startsWith(`${COOKIE_NAME}=`)
        && c.trim().length > COOKIE_NAME.length + 1);
  } catch {
    // cookie 被禁用时返回 true：让调用方不要反复重写（写也写不进去）
    return true;
  }
}

/**
 * 归一化基地址：去掉尾部斜杠、补上 `http://`。
 *
 * 用户在手机输入法里很难打出 `://`，所以允许他只输 `192.0.2.10:8787`。
 * ⚠️ 不能无脑补 `https://`：局域网直连是明文 HTTP，补 https 会直接连不上。
 */
export function normalizeBase(raw: string): string {
  let s = raw.trim();
  if (!s) return "";
  // 用户可能直接粘贴了带路径的 URL（如 http://ip:8787/d/abc123），保留原样
  if (!/^https?:\/\//i.test(s)) s = `http://${s}`;
  return s.replace(/\/+$/, "");
}

/**
 * 从 `host`、`port` 两个输入框拼出基地址。
 * 端口留空时按 `8787`（后端 `server.port` 默认值）。
 */
export function buildBase(host: string, port: string): string {
  const h = host.trim().replace(/^https?:\/\//i, "").replace(/\/.*$/, "");
  const p = port.trim();
  if (!h) return "";
  if (/:\d+$/.test(h)) return normalizeBase(h); // 用户把端口写在 host 里了
  return normalizeBase(`${h}:${p || "8787"}`);
}
