import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: 'line',
  updateSnapshots: 'none',
  snapshotPathTemplate: '{testDir}/snapshots/{arg}{ext}',
  use: {
    baseURL: 'http://127.0.0.1:4180',
    browserName: 'chromium',
    locale: 'en-GB',
    timezoneId: 'Europe/London',
    reducedMotion: 'reduce',
    screenshot: 'off',
    trace: 'off',
  },
  webServer: {
    command: 'npm run build:ui && vite --config apps/system-index/vite.config.ts --host 127.0.0.1 --port 4180 --strictPort',
    cwd: new URL('../..', import.meta.url).pathname,
    url: 'http://127.0.0.1:4180/',
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
