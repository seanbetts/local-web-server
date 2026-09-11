import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  forbidOnly: true,
  retries: 0,
  workers: 1,
  reporter: 'line',
  snapshotPathTemplate: '{testDir}/snapshots/{arg}{ext}',
  use: {
    baseURL: 'http://127.0.0.1:4179',
    browserName: 'chromium',
    locale: 'en-GB',
    timezoneId: 'Europe/London',
    reducedMotion: 'no-preference',
    screenshot: 'off',
    trace: 'off',
  },
  webServer: {
    command: 'vite --config examples/ui-gallery/vite.config.ts',
    cwd: new URL('../..', import.meta.url).pathname,
    url: 'http://127.0.0.1:4179/?theme=candidate',
    reuseExistingServer: false,
    timeout: 30_000,
    env: {
      LWP_THEME_CANDIDATE:
        process.env.LWP_THEME_CANDIDATE
        ?? new URL('../../platform_assets/theme.css', import.meta.url).pathname,
    },
  },
});
