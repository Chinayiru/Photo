/* ------------------------------------------------------------------ *
 * 把异常变成一句**人话**
 *
 * 为什么要有这个文件：错误体里有四个字段，只有 `user_hint` 是给人看的，
 * `message` 是错误码的英文名（`token_resource_mismatch`）、`detail` 是给
 * 运维排查用的、`code` 是 `E2003` 这种编号。它们**都不该出现在界面上**。
 *
 * 之前的散落写法是 `e.userHint || e.friendly || "…"`，而 `friendly` 的第二
 * 个候选正是 `message` —— 于是只要后端某条路径没填 `user_hint`（比如游客
 * 越权的 403），用户就会看到裸的英文代码。真机实测到的原话：
 *
 *     游客点「回收站」→ 提示条上写着 token_resource_mismatch
 *
 * 所以这里做两件事：
 * 1. **按 code 给中文**：常见拒绝原因各有一句明确的话，游客场景说清楚
 *    "游客只能看和传"，而不是笼统的"无权访问该设备"。
 * 2. **绝不回落到 code/message/detail**：不在表里的情况，一律用调用方给的
 *    兜底文案。宁可说"删除失败"，也不把技术串甩给用户。
 * ------------------------------------------------------------------ */

import { ApiError } from "./client";

/** 当前浏览器是不是游客身份（游客不能删/改名/移动）。 */
function isGuest(): boolean {
  try {
    return localStorage.getItem("pb.guest") === "1";
  } catch {
    return false;
  }
}

/** 是不是一句中文 —— 用来把"写给人看的提示"和英文技术串分开。 */
function isChinese(text: string | null): boolean {
  return !!text && /[\u4e00-\u9fa5]/.test(text);
}

/** 码 → 人话。只放"用户能据此决定下一步"的话，不复述技术原因。 */
const BY_CODE: Record<string, string> = {
  // 越权/身份
  E2003: "这个操作只对自己的照片有效", // 覆盖不到时下面按游客场景改写
  E2004: "配对已失效，请重新扫电脑上的二维码",
  E2010: "这个操作只有这台电脑的主人能做",
  E2011: "这个凭证不属于这台电脑，请重新配对",
  E2012: "当前登录的账号无权访问这台电脑",
  E1026: "这位游客已经不在线了",
  E1024: "当前访问人数已满，请稍后再试",
  // 电脑端的写开关（改一次全关：删除/改名/移动共用 E1015）
  E1015: "电脑端没开「允许手机修改照片」，请到电脑上打开这个开关",
  E1013: "文件校验没通过，请重新上传",
  E1017: "不支持这种文件格式",
  // 常见冲突
  E1004: "这项已经不在原处了，可能刚被删掉或移走",
  E1001: "请求有点问题，请重试一次",
  E2002: "配对已失效，请重新扫电脑上的二维码",
  E3001: "电脑端出错了，稍后再试",
  E3004: "这张图的缩略图生成失败",
};

/**
 * 一句能直接弹给用户的话。
 *
 * @param e 任意异常（多半是 `ApiError`）
 * @param fallback 兜底文案，**必须是完整的中文句子**，不要用 `e.message`
 */
export function errorText(e: unknown, fallback: string): string {
  if (e instanceof ApiError) {
    // 游客的写操作被拒：`detail` 里写着"游客只能查看与上传，不能修改或删除"，
    // 但那是给运维看的；界面要说的是一句用户能懂的话。
    if (e.code === "E2003" && isGuest()) {
      return "游客只能查看和上传，不能删除、改名或移动";
    }
    // 本地构造的错误（`status === 0`：表单校验、网络层）自带整句中文，
    // 比通用映射更具体 ——「请填写电脑地址」不该被 E1001 的
    // 「请求有点问题」盖掉。只有服务端返回的错误才带真实状态码。
    if (e.status === 0 && isChinese(e.userHint)) return e.userHint ?? fallback;
    const mapped = BY_CODE[e.code];
    if (mapped) return mapped;
    // 后端的 `user_hint` 也是写给人看的中文，但**映射表优先**：同一个
    // code 前端已有专门文案时以表为准，避免两端各说各话。
    if (isChinese(e.userHint)) return e.userHint ?? fallback;
    if (e.status === 0) return "连不上电脑，请检查网络";
    return fallback;
  }
  if (e instanceof Error && /[\u4e00-\u9fa5]/.test(e.message)) {
    // 只有自带中文的异常才配直接展示；英文系统错误一律换兜底。
    return e.message;
  }
  return fallback;
}
