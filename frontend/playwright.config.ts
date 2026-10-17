import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  use: { baseURL: "http://localhost:5173", viewport: { width: 1366, height: 768 } },
  webServer: { command: "npm run dev", url: "http://localhost:5173", reuseExistingServer: true, timeout: 60_000 },
});
