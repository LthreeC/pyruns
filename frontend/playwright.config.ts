import { defineConfig, devices } from '@playwright/test'

const port = Number.parseInt(process.env.PYRUNS_E2E_PORT || '8765', 10)
const browserExecutable = process.env.PYRUNS_E2E_BROWSER_PATH
const localBrowser = browserExecutable ? { launchOptions: { executablePath: browserExecutable } } : {}

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    headless: true,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium-desktop',
      use: { ...devices['Desktop Chrome'], ...localBrowser },
    },
    {
      name: 'chromium-mobile',
      use: { ...devices['Pixel 7'], ...localBrowser },
    },
  ],
  webServer: {
    command: 'node e2e/start-server.mjs',
    url: `http://127.0.0.1:${port}/`,
    reuseExistingServer: false,
    timeout: 30_000,
  },
})
