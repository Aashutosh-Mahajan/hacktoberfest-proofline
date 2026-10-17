import { defineConfig } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/**
 * Smoke test against a REAL backend. Start one first:
 *   cd backend && MAYDAY_GATEWAY=scripted python -m uvicorn mayday.api.app:app --port 8003
 * The webServer below runs vite on :5174 proxying /api and /ws to it (override with MAYDAY_BACKEND / E2E_PORT).
 */
const backend = process.env.MAYDAY_BACKEND ?? "http://localhost:8003";
const port = Number(process.env.E2E_PORT ?? 5174);

/** Use any locally installed Playwright chromium if the pinned revision is missing (offline machines). */
function findChrome(): string | undefined {
  if (process.env.PW_CHROME) return process.env.PW_CHROME;
  const root = path.join(process.env.PLAYWRIGHT_BROWSERS_PATH ?? path.join(os.homedir(), "AppData", "Local", "ms-playwright"));
  try {
    const dirs = fs.readdirSync(root);
    for (const d of dirs.filter((x) => x.startsWith("chromium_headless_shell-")).sort().reverse()) {
      const exe = path.join(root, d, "chrome-headless-shell-win64", "chrome-headless-shell.exe");
      if (fs.existsSync(exe)) return exe;
    }
    for (const d of dirs.filter((x) => x.startsWith("chromium-")).sort().reverse()) {
      const exe = path.join(root, d, "chrome-win64", "chrome.exe");
      if (fs.existsSync(exe)) return exe;
    }
  } catch { /* fall back to playwright's own download */ }
  return undefined;
}

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  retries: 0,
  workers: 1,
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    viewport: { width: 1366, height: 768 },
    launchOptions: { executablePath: findChrome() },
  },
  webServer: {
    command: `npx vite --host 127.0.0.1 --port ${port} --strictPort`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: true,
    timeout: 60_000,
    env: { MAYDAY_BACKEND: backend, MAYDAY_PORT: String(port) },
  },
});
