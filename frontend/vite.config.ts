import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

/**
 * PhotoBridge 手机端前端构建配置。
 *
 * ★ `base: "./"` 是硬约束：后端 `backend/app/static.py` 把 `frontend/dist`
 *   挂在**任意路径**下（二期云隧道是 `/d/{device_id}/`，局域网是 `/`），
 *   绝对路径 `/assets/...` 在 `/d/abc/` 下必然 404。
 */
export default defineConfig({
  base: "./",
  plugins: [vue()],
  build: {
    target: "es2018",
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true,
    // 单文件 CSS：手机端首屏少一次往返
    cssCodeSplit: false,
    reportCompressedSize: true,
    chunkSizeWarningLimit: 1024,
    rollupOptions: {
      output: {
        // 稳定的文件名便于 Service Worker / 长缓存，不做 hash 拆包
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
  server: {
    host: true,
    port: 5173,
  },
});
