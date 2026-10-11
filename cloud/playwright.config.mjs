import { defineConfig } from '@playwright/test';
import { ORIGIN } from './browser-tests/static-server.mjs';

export default defineConfig({
  testDir: './browser-tests',
  testMatch: '**/*.spec.mjs',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  timeout: 30_000,
  globalTimeout: 5 * 60_000,
  expect: { timeout: 5_000 },
  reporter: 'line',
  outputDir: './test-results',
  use: {
    baseURL: ORIGIN,
    browserName: 'chromium',
    viewport: { width: 1440, height: 1000 },
    locale: 'zh-CN',
    timezoneId: 'UTC',
    serviceWorkers: 'block',
    acceptDownloads: true,
    // Only the two explicit clean-demo PNGs are collected, never provider tests.
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
  webServer: {
    command: 'node browser-tests/static-server.mjs',
    url: ORIGIN,
    reuseExistingServer: false,
    timeout: 15_000,
    stdout: 'ignore',
    stderr: 'pipe',
  },
});
