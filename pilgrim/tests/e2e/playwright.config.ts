import { defineConfig, devices } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

const root = resolve(__dirname, "../../..");
const stationJSON = process.env.PILGRIM_E2E_STATION || execFileSync(`${root}/.venv/bin/python`, [
  "-c", `import socket
from pilgrim.config import load_config
station = load_config().station
# Isolated test server: let the OS choose a free port, never reuse live radio.
with socket.socket() as listener:
    listener.bind((station.host, 0))
    station.port = listener.getsockname()[1]
print(station.model_dump_json())`,
], { cwd: root, encoding: "utf8" });
process.env.PILGRIM_E2E_STATION = stationJSON;
const station = JSON.parse(stationJSON);
// SPLIT_PLAN §7.2: when the split stack is managed externally (make
// e2e-split), baseURL is the EDGE and Playwright must not spawn the e2e
// server itself (the script already started it). Without the env var the
// config behaves exactly as before.
const externalBaseURL = process.env.PILGRIM_E2E_BASE_URL;
const baseURL = externalBaseURL || `http://${station.host}:${station.port}`;

export default defineConfig({
  testDir: "./",
  timeout: 60_000,
  retries: 0,
  workers: 1,
  use: { baseURL, viewport: { width: 480, height: 400 }, headless: true, channel: "chrome" },
  webServer: externalBaseURL ? undefined : {
    command: `.venv/bin/python -m uvicorn pilgrim.tests.e2e.e2e_server:app --host ${station.host} --port ${station.port}`,
    cwd: root,
    url: `${baseURL}/api/health`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
