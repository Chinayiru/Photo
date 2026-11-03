/**
 * TC-XSS-SEC-007 的可执行探针：`safeUrl()` 对危险协议必须返回空串。
 *
 * 由 `backend/tests/test_frontend_hardening.py` 调用（`node --experimental-strip-types`）。
 * 之所以不用 vitest：`frontend/package.json` 里没有任何测试运行器，
 * 为一条判据引入整套 node 测试栈不划算（SEC-24）。
 *
 * 2026-11-03 扩充：`blob:` 改成**来源校验**之后，这里同时验证
 * 「可信放行 / 未登记拒绝 / 伪造拒绝 / revoke 后拒绝」四个方向
 * （任务书 F-01、T1、T2）。断言的是真实实现，不是字符串匹配。
 */
import { safeUrl } from "../src/lib/safeUrl.ts";
import {
  createTrustedBlobUrl,
  describeTrustedBlobs,
  revokeTrustedBlobUrl,
} from "../src/lib/trustedBlob.ts";

let failed = 0;

function check(name: string, input: unknown, want: string): void {
  const got = safeUrl(input as string);
  if (got !== want) {
    console.error(`FAIL ${name}: got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);
    failed += 1;
  }
}

const cases: Array<[string, unknown, string]> = [
  ["javascript: 协议", "javascript:alert(1)", ""],
  ["javascript: 前导空格", "  javascript:alert(1)  ", ""],
  ["大小写混写", "JaVaScRiPt:alert(1)", ""],
  ["data: 协议", "data:text/html,<script>alert(1)</script>", ""],
  ["vbscript: 协议", "vbscript:msgbox(1)", ""],
  ["CR/LF 拆行", "java\nscript:alert(1)", ""],
  ["BIDI 覆盖符", "\u202Ehttp://evil.example/x", ""],
  ["非字符串", undefined, ""],
  ["空串", "", ""],
  ["相对路径放行", "/api/items/12/thumb", "/api/items/12/thumb"],
  ["http 放行", "http://192.168.0.2:8787/api/x", "http://192.168.0.2:8787/api/x"],
  ["https 放行", "https://example.com/a.jpg", "https://example.com/a.jpg"],
  ["协议相对网址按当前 origin 解析", "//evil.example/x", "//evil.example/x"],
];

for (const [name, input, want] of cases) check(name, input, want);

/* ------------------------------------------------------------------ *
 * 可信 Blob：四个方向逐一验证
 * ------------------------------------------------------------------ */

const png = new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a])], {
  type: "image/png",
});

// ① 经登记表创建的 blob: 放行（原图层与二维码走的就是这条路）
const trusted = createTrustedBlobUrl(png, "raw:1");
if (!trusted.startsWith("blob:")) {
  console.error(`FAIL 可信 Blob 不是 blob: 地址：${trusted}`);
  failed += 1;
}
check("可信 blob 放行", trusted, trusted);

// ② 未经登记表、直接 createObjectURL 出来的 blob: 必须拒绝 ——
//    这条正是"协议白名单"与"来源校验"的分水岭。
const unregistered = URL.createObjectURL(png);
check("未登记的 blob 被拒", unregistered, "");
URL.revokeObjectURL(unregistered);

// ③ 伪造的 blob: 串（从 URL 参数/localStorage 读到的）必须拒绝
check("伪造 blob 被拒", "blob:https://evil.example/forged-uuid", "");
check("伪造 blob 被拒（同源前缀）", `blob:${"https://example.com/"}/x`, "");

// ④ revoke 之后同一个地址必须立刻失效
revokeTrustedBlobUrl(trusted);
check("已 revoke 的 blob 被拒", trusted, "");

// ⑤ 登记表上限：不应无限增长（超过上限会淘汰最旧的并 revoke）
const created: string[] = [];
for (let i = 0; i < 40; i++) created.push(createTrustedBlobUrl(png, `raw:${i}`));
const snapshot = describeTrustedBlobs();
if (snapshot.length > 16) {
  console.error(`FAIL 登记表无上限：${snapshot.length} 条`);
  failed += 1;
}
if (safeUrl(created[0]) !== "") {
  console.error("FAIL 被淘汰的最旧 blob 仍然放行");
  failed += 1;
}

// ⑥ T2：登记表**全文**输出（不截断），验收时逐条核对
const liveUrl = created[created.length - 1];
const table = describeTrustedBlobs();
const hit = table.find((e) => e.url === liveUrl);
if (!hit || !hit.origin.startsWith("raw:") || typeof hit.createdAt !== "number") {
  console.error(`FAIL 登记表缺少来源信息：${JSON.stringify(table)}`);
  failed += 1;
}
console.log(JSON.stringify({ trusted: table }, null, 2));
for (const e of table) revokeTrustedBlobUrl(e.url);

if (failed > 0) {
  console.error(`${failed} 条未通过`);
  process.exit(1);
}
console.log(`OK ${cases.length + 9} 条`);
