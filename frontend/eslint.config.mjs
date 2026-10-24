import vue from "eslint-plugin-vue";
import vueParser from "vue-eslint-parser";
import tsParser from "@typescript-eslint/parser";

// 只做「安全相关」的静态检查，不引入格式化与风格规则。
//
// 存在理由：`docs/07` 的 TC-XSS-SEC-006 要求「`vue/no-v-html` 必须开启」，
// 而一个没有 linter 的仓库里这条判据等于空判据 —— 谁来开？开在哪？跑不跑？
// 这份配置就是那个「开在哪」，`npm run lint` 就是「跑不跑」。
const JS_SECURITY_RULES = {
  "no-eval": "error",
  "no-implied-eval": "error",
  "no-new-func": "error",
  "no-script-url": "error",
};

const VUE_SECURITY_RULES = {
  ...JS_SECURITY_RULES,
  // TC-XSS-SEC-006 / -007：模板里任何 `v-html` 都是一个 XSS 口子。
  // 全仓目前零使用，这条规则保证「以后也不会有」。
  "vue/no-v-html": "error",
  "vue/no-v-text-v-html-on-component": "error",
};

export default [
  {
    files: ["src/**/*.vue"],
    languageOptions: {
      parser: vueParser,
      parserOptions: {
        parser: tsParser,
        ecmaVersion: 2022,
        sourceType: "module",
        extraFileExtensions: [".vue"],
      },
    },
    plugins: { vue },
    rules: VUE_SECURITY_RULES,
  },
  {
    files: ["src/**/*.ts"],
    languageOptions: {
      parser: tsParser,
      parserOptions: { ecmaVersion: 2022, sourceType: "module" },
    },
    rules: JS_SECURITY_RULES,
  },
  {
    ignores: ["dist/**", "node_modules/**"],
  },
];