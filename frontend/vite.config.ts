/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

const backend = process.env.MAYDAY_BACKEND ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@contract": path.resolve(__dirname, "../contracts/types.ts") } },
  server: {
    port: Number(process.env.MAYDAY_PORT ?? 5173),
    fs: { allow: [path.resolve(__dirname, "..")] },
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true, changeOrigin: true },
    },
  },
  // `npm run demo` serves the production build with the same /api and /ws proxy as the dev server
  preview: {
    port: Number(process.env.MAYDAY_PORT ?? 5173),
    proxy: {
      "/api": { target: backend, changeOrigin: true },
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true, changeOrigin: true },
    },
  },
  build: { target: "es2022", sourcemap: false, chunkSizeWarningLimit: 700 },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
