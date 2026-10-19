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
    // `npm run dev`: proxy the API to a running agent on 8721 (log in on
    // 8721 first so the browser has the session cookie for 127.0.0.1).
    proxy: { "/api": "http://127.0.0.1:8721" },
  },
});
