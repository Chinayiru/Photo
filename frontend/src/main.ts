/**
 * 入口。
 *
 * 顺序很重要：先引样式，再挂载 —— 否则首帧会闪一下无样式的白底
 * （深色主题下那一闪特别刺眼）。
 *
 * `setupConnection()` 必须在 `mount()` **之前**：它注册"请求失败 → 重探"
 * 的中介回调与 `online` / `visibilitychange` 监听。挂载后再注册，首屏那批
 * 请求（时间线第一页、SSE）的失败就没人接 —— 用户会停在一个空的照片墙，
 * 而电脑其实只是换了个 IP。
 */
import { createApp } from "vue";
import App from "./App.vue";

import "./styles/theme.css";
import "./styles/base.css";

import { setupConnection } from "./stores/auth";

setupConnection();

createApp(App).mount("#app");
