import { defineConfig, devices } from "@playwright/test";

// `make screenshots`: documentation images from the demo backend (fake Plex and
// Discord data only). Not part of `make e2e`.
const port = Number(process.env.SHOTS_PORT ?? 31341);

export default defineConfig({
  testDir: "e2e",
  testMatch: "screenshots.shots.ts",
  workers: 1,
  reporter: "list",
  timeout: 120_000,
  use: {
    ...devices["Desktop Chrome"],
    baseURL: `http://127.0.0.1:${port}`,
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
  },
  webServer: {
    command: "sh e2e/server.sh",
    url: `http://127.0.0.1:${port}/api/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: { E2E_PORT: String(port) },
  },
});
