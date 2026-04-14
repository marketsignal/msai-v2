import { defineConfig, devices } from "@playwright/test";

// Port 3301 deliberately does NOT collide with the Docker Compose dev
// frontend on 3300 — Playwright spawns its own `next dev` so e2e runs
// don't depend on `docker compose up` or interfere with manual browsing.
const PORT = 3301;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 1,
  // Serialize: `next dev` compiles pages on first request.  Parallel
  // routes hammering the compiler cause intermittent "element detached"
  // hydration races that no amount of waiting fully eliminates.  The
  // whole suite still runs in well under a minute with serial workers,
  // and flake noise is not worth the ~30% time saving.  Use
  // `next build && next start` upstream of this config if you need
  // parallelism back — the ahead-of-time build removes the contention.
  workers: 1,
  // Per-test budget.  First-compile of a cold route in `next dev` can
  // take 20s+ under contention; the default 30s leaves no headroom for
  // the assertion itself.
  timeout: 60_000,
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: {
    command: `pnpm exec next dev --hostname 127.0.0.1 --port ${PORT}`,
    cwd: ".",
    url: `http://127.0.0.1:${PORT}`,
    reuseExistingServer: !process.env.CI,
    stdout: "ignore",
    stderr: "pipe",
    timeout: 120_000,
    env: {
      ...process.env,
      // Dev-mode auth bypass (AppShell) + API-key header auth (lib/api.ts)
      // together give us a fully authed browser without MSAL login.
      NODE_ENV: "development",
      NEXT_PUBLIC_MSAI_API_KEY:
        process.env.NEXT_PUBLIC_MSAI_API_KEY ?? "msai-dev-key",
      // Point at the Docker Compose backend.  Tests expect it to be up
      // (docker-compose.dev.yml) — each spec that talks to the backend
      // documents the precondition; UI-only specs work regardless.
      NEXT_PUBLIC_API_URL:
        process.env.NEXT_PUBLIC_API_URL ?? "http://127.0.0.1:8800",
      // Disable the live WebSocket stream in tests — the useLiveStream
      // hook will spin up a reconnecting socket that fails noisily when
      // the backend isn't running.  Tests targeting live trading pages
      // opt in via route mocks.
      NEXT_PUBLIC_LIVE_STREAM_ENABLED: "false",
    },
  },
});
