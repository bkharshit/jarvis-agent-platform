import { defineConfig } from "@playwright/test";

// E2e smoke (decision 11). Vite is started here via webServer; the backend
// is a documented prerequisite: `uv run jarvis serve` against local
// Postgres on :8000 (the Vite dev proxy forwards /v1 and /healthz — there
// is no CORS middleware). No Docker on this machine.
export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
    // Human-speed runs: every action pauses so a --headed session is
    // watchable. 0 on CI; E2E_SLOWMO=<ms> overrides (0 = full speed).
    launchOptions: {
      slowMo: Number(process.env.E2E_SLOWMO ?? (process.env.CI ? 0 : 500)),
    },
  },
  webServer: {
    command: "npm run dev",
    url: "http://localhost:5173",
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});