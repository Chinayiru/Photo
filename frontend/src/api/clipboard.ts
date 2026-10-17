/**
 * 剪贴板 —— 局域网明文 HTTP 下的可用实现。
 *
 * ## 为什么需要这个封装
 *
 * `navigator.clipboard` 是 **secure context 专属** 的。手机访问
 * `http://192.0.2.10:8787` 时 `window.isSecureContext === false`，
 * `navigator.clipboard` 直接是 `undefined` —— 实测（Chrome 155，
 * `build/_probe_secure.py`）：
 *
 * | 页面 | isSecureContext | navigator.clipboard | crypto.randomUUID |
 * |---|---|---|---|
 * | `http://192.0.2.10:PORT` | **false** | **undefined** | **undefined** |
 * | `http://127.0.0.1:POR`     | true  | object    | function |
 *
 * 也就是说：开发机上（回环）测试"复制成功"，到手机上必然失败 ——
 * 这正是那种「本地全绿、真机全废」的缺陷。
 *
 * 原来的写法是 `navigator.clipboard?.writeText(b).then(...)`：可选链
 * 让整条链短路成 `undefined`，`.then` 不会执行，**成功与失败的回调
 * 一次都不跑**，用户点「复制服务地址」既无提示也无复制内容。
 *
 * ## 兜底方案
 *
 * `document.execCommand("copy")` 在非安全上下文里仍然可用（它没有
 * 安全上下文限制，只是被标记为过时）。流程：造一个覆盖在按钮位置上的
 * 透明 textarea → 选中 → execCommand → 移除。
 *
 * ★ 必须临时把焦点抢过来：安卓上如果 textarea 不在屏幕内或尺寸为 0，
 *   选中会失败。所以用 `position: fixed; top: 0; left: 0` 且**不隐藏**
 *   （用 `opacity: 0` 而不是 `display: none` / `visibility: hidden`）。
 * ★ 结束后要把滚动位置和选区还原：不然页面会跳到顶部。
 */

/** 复制是否成功。调用方负责给用户提示。 */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false;

  // 1. 首选异步剪贴板（安全上下文才有，将来上了 HTTPS 或回环就走到这里）
  const clip = (navigator as Navigator & {
    clipboard?: { writeText?: (s: string) => Promise<void> };
  }).clipboard;
  if (clip && typeof clip.writeText === "function") {
    try {
      await clip.writeText(text);
      return true;
    } catch {
      // 权限被拒（用户点了"阻止"）时继续走兜底，别直接判失败
    }
  }

  // 2. 兜底：execCommand
  return legacyCopy(text);
}

/** 同步版本，只在 `execCommand` 可用时成功。给不关心 Promise 的老代码用。 */
export function copyTextSync(text: string): boolean {
  if (!text) return false;
  return legacyCopy(text);
}

function legacyCopy(text: string): boolean {
  // execCommand 在极新的浏览器里仍存在；完全没有时只能返回 false
  if (typeof document.execCommand !== "function") return false;

  const active = document.activeElement as HTMLElement | null;
  const sel = document.getSelection();
  const savedRanges: Range[] = [];
  if (sel) {
    for (let i = 0; i < sel.rangeCount; i += 1) {
      const r = sel.getRangeAt(i);
      savedRanges.push(r.cloneRange());
    }
  }
  const scrollX = window.scrollX;
  const scrollY = window.scrollY;

  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  // 只读 + 视觉不可见 + 仍在布局里（display:none 会让选中失败）
  ta.style.position = "fixed";
  ta.style.top = "0";
  ta.style.left = "0";
  ta.style.width = "1px";
  ta.style.height = "1px";
  ta.style.padding = "0";
  ta.style.border = "none";
  ta.style.outline = "none";
  ta.style.boxShadow = "none";
  ta.style.background = "transparent";
  ta.style.opacity = "0";
  ta.style.fontSize = "12pt"; // < 16px 时 iOS 会放大页面
  document.body.appendChild(ta);

  let ok = false;
  try {
    ta.focus({ preventScroll: true });
    ta.select();
    ta.setSelectionRange(0, text.length);
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  } finally {
    document.body.removeChild(ta);
  }

  // 还原选区与滚动（select() 会把它们都改掉）
  try {
    if (sel) {
      sel.removeAllRanges();
      for (const r of savedRanges) sel.addRange(r);
    }
    active?.focus?.({ preventScroll: true });
    window.scrollTo(scrollX, scrollY);
  } catch {
    /* 还原失败不影响复制结果，不值得让用户看见 */
  }

  return ok;
}

/**
 * 当前环境能不能"可靠地"复制。给 UI 决定是否禁用按钮用。
 *
 * ★ 不用 `navigator.clipboard` 判断：正文里说过它在局域网 HTTP 下是
 *   `undefined`，但 `execCommand` 兜底仍然可用。真正决定能不能复制的是
 *   `execCommand` 是否存在。
 */
export function canCopy(): boolean {
  const clip = (navigator as Navigator & {
    clipboard?: { writeText?: unknown };
  }).clipboard;
  if (clip && typeof clip.writeText === "function") return true;
  return typeof document.execCommand === "function";
}
