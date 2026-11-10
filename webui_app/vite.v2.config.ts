import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The /v2 dashboard is built on its own, after the classic app, into the
// same ../webui_dist (v2.html + assets/v2-*). Two builds instead of one
// multi-page build so that adding /v2 leaves the classic bundle exactly as
// it was: the two share no chunk. `npm run build` runs both.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../webui_dist",
    emptyOutDir: false,
    assetsDir: "assets",
    sourcemap: false,
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      input: "v2.html",
      output: {
        entryFileNames: "assets/v2-[hash].js",
        chunkFileNames: "assets/v2-[name]-[hash].js",
        assetFileNames: "assets/v2-[hash][extname]",
      },
    },
  },
});
