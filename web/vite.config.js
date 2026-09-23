import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 8080,
    strictPort: true,
    proxy: {
      "/api": {
        target: process.env.AGENT_E2E_API_URL || "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    environmentOptions: {
      jsdom: {
        url: "http://localhost:8080/",
      },
    },
    setupFiles: ["src/test/setup.js"],
  },
});
