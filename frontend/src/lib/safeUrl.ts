/**
 * URL 守卫：**只放行 http/https**，其余一律返回空串。
 *
 * 路径与签名以 `docs/06-安全设计文档.md` §8.2 为准（那里写的是
 * `frontend/src/lib/safeUrl.ts` + `safeUrl(raw: string): string`）。
 *
 * `docs/07` 的 TC-XSS-SEC-007（P0）写的是"`safeUrl()` 返回 `""`"，而原来
 * 仓库里同名函数在 `PairView.vue` 里返回 `URL | null` —— 名字对不上判据，
 * 语义也不是"守卫"。这个文件是它的正式实现。
 *
 * ## 为什么要拦这些
 *
 * * `javascript:` —— `:src`/`:href` 绑上去就是一次 XSS（Vue 不会替你拦，
 *   它只转义文本插值，属性里的 URL 原样下发给浏览器）。
 * * `data:` —— `data:text/html,<script>` 在 top-level 导航里会执行，一律不放。
 *
 * ## `blob:` 按**来源**放行，不按协议白名单
 *
 * 把 `blob:` 加进 `ALLOWED_PROTOCOLS` 等于对所有 blob 地址全面放开：
 * 攻击者自己 `createObjectURL` 一份 HTML 就得到一个"看起来合法"的串。
 * 所以这里只认 `lib/trustedBlob.ts` 登记过的 —— 它们由本应用用已认证
 * 请求取回的二进制创建，创建即登记、用完即 revoke。判据是**成员资格**，
 * 与字符串长相无关 ⇒ 伪造的、未登记的、已 revoke 的必然被拒。
 * * 控制字符与 BIDI 覆盖符 —— `java\nscript:`、`\u202E` 这类写法能让
 *   肉眼与"看起来像 URL"的检查同时失效，直接判空。
 *
 * 允许**相对路径**（`/api/items/12/thumb`）：解析基准是当前 origin，同源
 * 请求本来就安全，而且缩略图正是这样拼出来的。
 */

// ★ 显式 `.ts` 扩展名：`backend/tests/test_frontend_hardening.py` 用
//   `node --experimental-strip-types` 真跑这个文件，Node 的 ESM 解析
//   不接受无扩展名的相对导入（vite/vue-tsc 两者都接受带扩展名写法）。
import { isTrustedBlobUrl } from "./trustedBlob.ts";

/** 控制字符（含 CR/LF/TAB）与 BIDI 覆盖符：都能把 URL 拆成两截。 */
const UNSAFE_CHARS = /[\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/;

const ALLOWED_PROTOCOLS = ["http:", "https:"];

/**
 * 把任意值收敛成一个可以安全绑到 `:src` / `:href` 的字符串。
 *
 * @param raw  可能是服务端给的 URL、用户粘贴的地址、或 `undefined`
 * @param base 相对路径的解析基准；默认 `location.href`（无 location 时用
 *             `http://localhost/` 占位，只为让 `new URL` 能跑）
 * @returns 安全则返回**原字符串**（相对路径保持相对，减少 diff），否则 `""`
 */
export function safeUrl(raw: unknown, base?: string): string {
  if (typeof raw !== "string") return "";
  const value = raw.trim();
  if (!value) return "";
  if (UNSAFE_CHARS.test(value)) return "";
  const anchor =
    base
    ?? (typeof location !== "undefined" && location.href ? location.href : "http://localhost/");
  let parsed: URL;
  try {
    parsed = new URL(value, anchor);
  } catch {
    return "";
  }
  if (parsed.protocol === "blob:") {
    // ★ 来源校验：只有本应用登记过的 blob URL 才放行。这里**不是**
    //   "协议白名单多一项"，成员判断不通过仍然返回空串。
    return isTrustedBlobUrl(value) ? value : "";
  }
  if (!ALLOWED_PROTOCOLS.includes(parsed.protocol)) return "";
  return value;
}

export default safeUrl;
