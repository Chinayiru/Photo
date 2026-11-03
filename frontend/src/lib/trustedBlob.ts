/**
 * 可信 Blob 登记表：`blob:` URL 的**来源校验**唯一入口。
 *
 * ## 为什么需要它
 *
 * `blob:` 与 `http:` 不同：它没有"地址"这回事 —— 同一串 `blob:https://x/abc`
 * 只在本页面进程内有效，且**任何拿到这串字符的人都能读到那份字节**。
 * 于是 `safeUrl()` 原先一律拒绝 `blob:`（`docs/06` §8.2），代价是原图层
 * 与二维码这类"先取字节再显示"的功能全部不可用。
 *
 * 天真的修法是把 `blob:` 加进 `ALLOWED_PROTOCOLS`。那等于**对所有 blob:
 * 全面放开**：只要攻击者能让某个属性变成 `blob:` 开头（比如他自己先
 * `createObjectURL` 一份 HTML），就会被当成合法图片地址绑上去。
 *
 * 正确做法是这批字节**由谁创建**说了算：
 *
 * 1. **创建**：只在本模块里调 `URL.createObjectURL`，来源必须是已认证
 *    请求（带 `Authorization`）取回的二进制；
 * 2. **登记**：创建时连同来源说明一起进表，`safeUrl()` 只认表里的；
 * 3. **校验**：`isTrustedBlobUrl()` 是纯成员判断，不看字符串长相；
 * 4. **revoke**：不再使用时立刻 `revokeObjectURL` 并移出表；
 * 5. **上限**：表长度封顶，超出时按创建时间淘汰最旧的（防长期泄漏）。
 *
 * ★ 表里**只存** URL 与来源标签（`raw:12`、`qr:lan`）—— 不存令牌。
 *   来源标签会全文写进验收日志（`docs/07` T2），所以它自己不能敏感。
 */

/** 表项：一条已登记的可信 Blob URL。 */
export interface TrustedBlobEntry {
  /** `URL.createObjectURL()` 返回的绝对串（`blob:https://host/uuid`）。 */
  url: string;
  /** 来源标签，如 `raw:12` / `qr:lan`。不含令牌，可全文记录。 */
  origin: string;
  /** 创建时刻（毫秒），用于上限淘汰。 */
  createdAt: number;
}

/**
 * 表的容量上限。
 *
 * 原图层自己还有一层 `RAW_KEEP`（几张），这里兜的是"任何调用方忘了
 * revoke"的情况：漏一次就永久泄漏几 MB，而用户不会看到任何异常 ——
 * 只会觉得"越用越卡"。16 足够覆盖同时打开两张码 + 几张小图的场景。
 */
export const MAX_TRUSTED_BLOBS = 16;

const registry = new Map<string, TrustedBlobEntry>();

/**
 * 创建一条可信 Blob URL 并登记。
 *
 * @param blob   已认证请求取回的二进制
 * @param origin 来源标签（`raw:{id}` / `qr:{kind}`），**不放令牌**
 */
export function createTrustedBlobUrl(blob: Blob, origin: string): string {
  const url = URL.createObjectURL(blob);
  registry.set(url, { url, origin, createdAt: Date.now() });
  // 超上限：按创建时间从旧到新淘汰。淘汰时**真的 revoke**，
  // 否则表里的记录没了、字节还挂着，问题从"泄漏"变成"看不见的泄漏"。
  while (registry.size > MAX_TRUSTED_BLOBS) {
    let oldest: TrustedBlobEntry | null = null;
    for (const entry of registry.values()) {
      if (oldest === null || entry.createdAt < oldest.createdAt) oldest = entry;
    }
    if (oldest === null) break;
    revokeTrustedBlobUrl(oldest.url);
  }
  return url;
}

/**
 * 撤销一条可信 Blob URL。
 *
 * 未登记的 URL 一律不碰：`revokeObjectURL` 对来路不明的串虽然无害，
 * 但"未登记也能被 revoke"会让人误以为它也进了表。
 */
export function revokeTrustedBlobUrl(url: string): void {
  if (!registry.delete(url)) return;
  URL.revokeObjectURL(url);
}

/**
 * 该 URL 是否为本应用创建且仍有效的可信 Blob。
 *
 * ★ 纯成员判断：不做前缀匹配、不做正则。看起来像 `blob:` 的伪造串
 *   （比如从 URL 参数里读到的）不可能是本表成员，因此必然为假。
 */
export function isTrustedBlobUrl(url: unknown): boolean {
  return typeof url === "string" && registry.has(url);
}

/**
 * 登记表快照（**全文**，不截断）。
 *
 * 验收要逐条核对"某个 URL 是否在表里、来源是什么、是否已 revoke"，
 * 截断会把"已 revoke 但仍在表里"这类问题藏起来（任务书 T2）。
 */
export function describeTrustedBlobs(): TrustedBlobEntry[] {
  return [...registry.values()]
    .slice()
    .sort((a, b) => a.createdAt - b.createdAt)
    .map((e) => ({ ...e }));
}

/** 当前登记条数（验收断言"集合不随时间单调增长"用）。 */
export function trustedBlobCount(): number {
  return registry.size;
}

/**
 * 验收钩子：把登记表全文挂到 `window` 上。
 *
 * 真实浏览器验收脚本（`scripts/verify_browser_e2e.py`）通过 CDP 读它，
 * 才能判断"渲染前 URL 在可信集合内"以及"切图后旧 URL 被移出"。
 * 只暴露只读快照，没有写入入口 ⇒ 不构成绕过守卫的通道。
 */
declare global {
  interface Window {
    __pbTrustedBlobs?: () => TrustedBlobEntry[];
  }
}

if (typeof window !== "undefined") {
  window.__pbTrustedBlobs = () => describeTrustedBlobs();
}
