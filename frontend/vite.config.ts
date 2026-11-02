import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 关键配置：把 /api、/runs、/testsite 都代理到后端 8000。
// 为什么不直接在前端写 http://127.0.0.1:8000：
// 那样就是跨源请求，要处理 CORS、cookie、预检；走代理后前端只认相对路径，
// 部署时前后端同域也不用改一行代码。
//
// ⚠️ /testsite 这条不能漏（踩过）：
// 漏了它，前端拼出来的默认起点就是 localhost:5173/testsite/xxx，
// Vite 找不到这个静态路径就按 SPA 规则回退 index.html ——
// Agent 于是把自己这个界面当成了目标页面，在搜索框里乱填还"成功了"。代理补上才对。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/runs": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/testsite": { target: "http://127.0.0.1:8000", changeOrigin: true },
      // 顶栏那两个链接也走代理，否则点「接口文档」会掉回自己这个页面
      "/docs": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/openapi.json": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
});
