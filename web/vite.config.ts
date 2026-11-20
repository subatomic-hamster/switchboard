import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The FastAPI server. Override with SWITCHBOARD_API when it runs on another port.
const api = process.env.SWITCHBOARD_API ?? "http://127.0.0.1:8787";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: api, changeOrigin: true },
      "/events": { target: api, changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    css: false,
  },
});
