/**
 * 入口。
 *
 * 顺序很重要：先引样式，再挂载 —— 否则首帧会闪一下无样式的白底
 * （深色主题下那一闪特别刺眼）。
 */
import { createApp } from "vue";
import App from "./App.vue";

import "./styles/theme.css";
import "./styles/base.css";

createApp(App).mount("#app");
