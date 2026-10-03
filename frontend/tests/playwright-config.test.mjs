// Run with: node --experimental-strip-types --test tests/playwright-config.test.mjs
// These are configuration contract checks; fresh-server/browser proof is separate.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";

const configURL = new URL("../playwright.config.ts", import.meta.url).href;

function loadConfig(overrides = {}) {
  const env = { ...process.env, ...overrides };
  if (!("CI" in overrides)) delete env.CI;
  if (!("PLAYWRIGHT_BASE_URL" in overrides)) delete env.PLAYWRIGHT_BASE_URL;
  const result = spawnSync(
    process.execPath,
    [
      "--experimental-strip-types",
      "--input-type=module",
      "--eval",
      `const {default: config} = await import(${JSON.stringify(configURL)});
       console.log(JSON.stringify({baseURL: config.use.baseURL, webServer: config.webServer}));`,
    ],
    { env, encoding: "utf8" },
  );
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
}

test("default target manages a matching local server with valid pnpm arguments", () => {
  const config = loadConfig();
  assert.equal(config.baseURL, "http://localhost:3300");
  assert.equal(config.webServer.url, config.baseURL);
  assert.equal(config.webServer.command, "pnpm dev --port 3300");
  assert.equal(config.webServer.reuseExistingServer, true);
  assert.equal(config.webServer.env.NEXT_PUBLIC_E2E_AUTH_BYPASS, "1");
});

test("CI default requires its own local server", () => {
  const config = loadConfig({ CI: "1" });
  assert.equal(config.webServer.reuseExistingServer, false);
});

for (const target of ["http://localhost:4400", "https://example.invalid"]) {
  test(`explicit target ${target} does not manage a local server`, () => {
    const config = loadConfig({ PLAYWRIGHT_BASE_URL: target, CI: "1" });
    assert.equal(config.baseURL, target);
    assert.equal(config.webServer, undefined);
  });
}
