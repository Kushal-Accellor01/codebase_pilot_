import { defineConfig, devices } from "@playwright/test";

const API = 8321; // unusual ports: a developer may already have the real stack on 8000 / 5173
const WEB = 5174;

// The smoke test runs the real stack offline: the demo server (API + worker + scripted model) and the Vite dev server.
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: { baseURL: `http://127.0.0.1:${WEB}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: `python scripts/demo_server.py --port ${API}`,
      cwd: "../backend",
      url: `http://127.0.0.1:${API}/api/health`,
      timeout: 120_000,
      reuseExistingServer: false,
      env: { PYTHONUTF8: "1" },
    },
    {
      command: "npm run dev -- --host 127.0.0.1",
      url: `http://127.0.0.1:${WEB}`,
      env: { PILOT_API_URL: `http://127.0.0.1:${API}`, PILOT_WEB_PORT: String(WEB) },
      timeout: 60_000,
      reuseExistingServer: false,
    },
  ],
});
