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
export const K_BASE = "pb.base";
/** 电脑端显示名，仅用于界面文案（不是鉴权信息）。 */
export const K_DEVICE_NAME = "pb.device_name";
/** 本机自定义的客户端名，作为 `X-PB-Client` 头发给电脑端审计日志。 */
export const K_CLIENT_NAME = "pb.client_name";

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

export function getDeviceName(): string {
  return read(K_DEVICE_NAME);
}

export function setClientName(name: string): void {
  write(K_CLIENT_NAME, name);
}

export function getClientName(): string {
  return read(K_CLIENT_NAME);
}

/** 配对成功后一次性落盘。 */
export function savePairing(p: {
  token: string;
  deviceId: string;
  deviceName: string;
  base: string;
}): void {
  write(K_TOKEN, p.token);
  write(K_DEVICE_ID, p.deviceId);
  write(K_DEVICE_NAME, p.deviceName);
  write(K_BASE, normalizeBase(p.base));
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

/** 退出配对：清掉全部凭证，但保留客户端名（下次配对不用重输）。 */
export function clearPairing(): void {
  write(K_TOKEN, "");
  write(K_DEVICE_ID, "");
  write(K_DEVICE_NAME, "");
  write(K_BASE, "");
  clearTokenCookie();
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
