import { defineConfig, devices } from "@playwright/test";

/**
 * PILGRIM DOT FARM — client gap test (RADIO.md §9, §15, M9).
 * Launches the fake-backed e2e server, then a headed browser joins the
 * committed program and we assert gapless, underrun-free playout.
 */
export default defineConfig({
  testDir: "./",
  timeout: 60_000,
  retries: 0,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:5000",
    viewport: { width: 480, height: 400 },
    // Headed: mirrors the VNC-visible browser so audio actually flows.
    launchOptions: { headless: false },
  },
  webServer: {
    command: ".venv/bin/python pilgrim/tests/e2e/e2e_server.py",
    url: "http://127.0.0.1:5000/api/health",
    reuseExistingServer: false,
    timeout: 30_000,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
