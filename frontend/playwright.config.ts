import { existsSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { defineConfig, devices } from "@playwright/test";

function findCachedChromium() {
  const explicitPath = process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH;
  if (explicitPath) {
    return explicitPath;
  }

  const browserCache = join(homedir(), ".cache", "ms-playwright");
  if (!existsSync(browserCache)) {
    return undefined;
  }

  const browserFolders = readdirSync(browserCache)
    .filter((name) => /^chromium-\d+$/.test(name))
    .sort((left, right) => {
      const leftRevision = Number(left.split("-")[1]);
      const rightRevision = Number(right.split("-")[1]);
      return rightRevision - leftRevision;
    });

  for (const browserFolder of browserFolders) {
    for (const relativePath of ["chrome-linux64/chrome", "chrome-linux/chrome"]) {
      const executable = join(browserCache, browserFolder, relativePath);
      if (existsSync(executable)) {
        return executable;
      }
    }
  }

  return undefined;
}

const chromiumExecutable = findCachedChromium();

export default defineConfig({
  testDir: "./e2e",
  outputDir: "./test-results",
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  timeout: 30_000,
  expect: { timeout: 7_000 },
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:3100",
    launchOptions: chromiumExecutable
      ? { executablePath: chromiumExecutable }
      : undefined,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
  },
  webServer: [
    {
      command: "node e2e/fixtures/mock-assistant-api.mjs",
      port: 3101,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: "npm run dev -- --hostname 127.0.0.1 --port 3100",
      env: {
        ...process.env,
        NEXT_PUBLIC_API_BASE_URL: "http://127.0.0.1:3101",
      },
      port: 3100,
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
  ],
});
