import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built files land in ../webui_dist, which IS committed: end users run the
// agent with plain Python and never need Node. `webui.py` serves
// webui_dist/index.html for /setup and /dash, and /assets/* from disk.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../webui_dist",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
    chunkSizeWarningLimit: 700,
  },
  server: {
    // `npm run dev`: open http://127.0.0.1:5173/dash after logging in on
    // 8721 once (cookies ignore ports, so the session carries over). The
    // proxy presents the agent's own Host/Origin so its anti-CSRF guard lets
    // the calls through.
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8721",
        changeOrigin: true,
        configure: (proxy) => {
          proxy.on("proxyReq", (req) => {
            if (req.getHeader("origin")) req.setHeader("origin", "http://127.0.0.1:8721");
          });
        },
      },
    },
  },
});
