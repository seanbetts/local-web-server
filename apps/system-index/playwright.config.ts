import { defineConfig } from '@playwright/test';

const prepareUi = process.env.LWP_UI_PREPARED === '1' ? '' : 'npm run build:ui && ';
const prepareIndex = process.env.LWP_INDEX_PREPARED === '1'
  ? ''
  : 'npm run build:index:prepared && ';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: 'line',
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
    command: `${prepareUi}${prepareIndex}vite preview --config apps/system-index/vite.config.ts --host 127.0.0.1 --port 4180 --strictPort`,
    cwd: new URL('../..', import.meta.url).pathname,
    url: 'http://127.0.0.1:4180/_local-web/platform/index/',
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
