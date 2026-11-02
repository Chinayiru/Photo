/** 全局唯一的「字节数 → 人类可读」实现。
 *
 * ## 为什么单独一份
 *
 * 同一段代码原来写了三遍：`stores/system.ts:294 fmtBytes`（接受 null，5 档到 PB）、
 * `stores/upload.ts:610 fmtBytes`（4 档）、`stores/viewer.ts:584 formatBytes`（4 档，
 * 与 upload 那份逐字相同）。三份的差别只在"要不要缩略单位表"上，谁也不会去比对
 * 它们 —— 于是上传队列显示 "1.2 MB"、查看器显示 "1.2 MB"、系统面板显示 "1.2 MB"，
 * 看着一样纯属运气。
 *
 * 取并集口径：接受 `null`（没数据时显示 "—"），单位表到 PB —— 对原来那两处
 * 只传 4 档以内的调用方，输出逐字不变。
 */

/** 字节数 → `"1.2 MB"` / `"812 B"` / `"—"`（非有限值或负数）。 */
export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n) || n < 0) return "—";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB", "PB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}
