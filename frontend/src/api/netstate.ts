/**
 * 通路失败的"上报口"—— 一个只有十几行的中介模块。
 *
 * ## 为什么单独一个文件，而不是让 `client.ts` 直接 import `transport.ts`
 *
 * `transport.ts`（双通路状态机）必须读 `client.ts` 的错误类型来区分
 * "网络断了"和"电脑离线"；而 `client.ts` 又必须在请求失败时通知
 * 状态机。两个文件互相 import 会形成 ESM 循环依赖 —— 在 Vite 打包下
 * 通常能跑，但**初始化顺序**一变就会变成 `undefined is not a function`，
 * 且只在冷启动的某条路径上出现。
 *
 * 所以把"通知"这一步抽成单向边：
 *
 * ```text
 *   client.ts ──► netstate.ts ◄── transport.ts
 *   （只发不收）      （无依赖）        （只收不发）
 * ```
 *
 * ## 为什么要"连续 3 次"才判定
 *
 * 单次失败非常常见：用户切到后台、路由器换信道、手机休眠唤醒的一瞬间。
 * 每失败一次就重探一次，等于把 1.5 秒的探测成本乘上请求数。`docs/03`
 * §5.1.3 的判据是 `DIRECT → RE-PROBE` 需要**连续 3 次请求失败（非 4xx）**，
 * 计数就放在这里。
 *
 * 计数器在任何一次成功请求后清零（`reportTransportOk`）。
 */

/** 失败种类。 */
export type FailureKind =
  /** 网络层失败（fetch 抛错 / 超时）—— 两条通路都可能。 */
  | "network"
  /** 云端明确回了 `503 E3003 device_offline`（电脑端隧道断了）。 */
  | "cloud_offline";

type FailureHandler = (kind: FailureKind) => void;

let handler: FailureHandler | null = null;
let consecutive = 0;

/**: 连续失败多少次触发重探。与 `docs/03` §5.1.3 一致。 */
export const FAILURE_THRESHOLD = 3;

/**
 * 注册重探回调（由 `transport.ts` 在模块初始化时注册）。
 *
 * 注册之前的失败**不计数**：那段时间通常还没配对完，或者页面刚加载
 * 还没建立状态机，计数只会让首次探测被一个陈旧的 3 顶掉。
 */
export function setFailureHandler(h: FailureHandler | null): void {
  handler = h;
  consecutive = 0;
}

/** 上报一次失败。达到阈值时回调一次，并把计数清零（不重复触发）。 */
export function reportTransportFailure(kind: FailureKind): void {
  if (!handler) return;
  // `cloud_offline` 是**确定性**信号（云端亲口说"电脑离线"），
  // 不需要攒够 3 次 —— 攒的话用户要白等两个失败请求。
  if (kind === "cloud_offline") {
    consecutive = 0;
    handler(kind);
    return;
  }
  consecutive += 1;
  if (consecutive < FAILURE_THRESHOLD) return;
  consecutive = 0;
  handler(kind);
}

/** 上报一次成功（清零计数）。 */
export function reportTransportOk(): void {
  consecutive = 0;
}

/** 当前连续失败数（测试与状态条调试用）。 */
export function failureCount(): number {
  return consecutive;
}
