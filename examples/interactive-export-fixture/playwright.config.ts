import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  outputDir: './test-results',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: 'line',
  use: {
    browserName: 'chromium',
    locale: 'en-GB',
    timezoneId: 'Europe/London',
    trace: 'off',
    screenshot: 'off',
  },
});
