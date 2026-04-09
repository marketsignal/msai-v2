import { defineConfig, devices } from "@playwright/test";

const PORT = 3401;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "on-first-retry",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: `pnpm exec next dev --hostname 127.0.0.1 --port ${PORT}`,
    cwd: "/Users/pablomarin/Code/msai-v2/codex-version/frontend",
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: !process.env.CI,
    stdout: "ignore",
    stderr: "pipe",
    timeout: 120_000,
    env: {
      ...process.env,
      NEXT_PUBLIC_AUTH_MODE: "api-key",
      NEXT_PUBLIC_E2E_API_KEY: process.env.NEXT_PUBLIC_E2E_API_KEY ?? "msai-dev-key",
      NEXT_PUBLIC_LIVE_STREAM_ENABLED: "false",
      NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8400",
    },
  },
});
