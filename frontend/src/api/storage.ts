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
