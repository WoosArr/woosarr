import { defineConfig, devices } from "@playwright/test";

const port = Number(process.env.E2E_PORT ?? 31338);
const wizardPort = Number(process.env.E2E_WIZARD_PORT ?? 31340);

export default defineConfig({
  testDir: "e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: { trace: "retain-on-failure" },
  projects: [
    // Demo backend: fake Plex/Discord data, every page.
    { name: "app", testMatch: "app.spec.ts", use: { ...devices["Desktop Chrome"], baseURL: `http://127.0.0.1:${port}` } },
    // Real app with fake plex.tv/Discord: the setup wizards.
    { name: "wizards", testMatch: "wizards.spec.ts", use: { ...devices["Desktop Chrome"], baseURL: `http://127.0.0.1:${wizardPort}` } },
  ],
  webServer: [
    {
      command: "sh e2e/server.sh",
      url: `http://127.0.0.1:${port}/api/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { E2E_PORT: String(port) },
    },
    {
      command: "sh e2e/wizard-server.sh",
      url: `http://127.0.0.1:${wizardPort}/api/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { E2E_WIZARD_PORT: String(wizardPort) },
    },
  ],
});
