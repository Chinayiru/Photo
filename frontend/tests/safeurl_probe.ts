/**
 * TC-XSS-SEC-007 的可执行探针：`safeUrl()` 对危险协议必须返回空串。
 *
 * 由 `backend/tests/test_frontend_hardening.py` 调用（`node --experimental-strip-types`）。
 * 之所以不用 vitest：`frontend/package.json` 里没有任何测试运行器，
 * 为一条判据引入整套 node 测试栈不划算（SEC-24）。
 */
import { safeUrl } from "../src/lib/safeUrl.ts";

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

let failed = 0;
for (const [name, input, want] of cases) {
  const got = safeUrl(input as string);
  if (got !== want) {
    console.error(`FAIL ${name}: got ${JSON.stringify(got)} want ${JSON.stringify(want)}`);
    failed += 1;
  }
}
if (failed > 0) {
  console.error(`${failed} 条未通过`);
  process.exit(1);
}
console.log(`OK ${cases.length} 条`);