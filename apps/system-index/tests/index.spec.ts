import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const indexUrl = '/_local-web/platform/index/';
const registryFixture = fileURLToPath(new URL('../fixtures/registry-v2.json', import.meta.url));

// Probe policy, deadlines, retries and body avoidance belong to statusController.test.ts.
// These journeys prove the real page wires the controller to accessible app links.
async function openIndex(page: Page, tasksOnline = true) {
  await page.route(`**${indexUrl}registry-v2.json`, (route) => route.fulfill({
    contentType: 'application/json', path: registryFixture,
  }));
  for (const [path, status] of [
    ['/example-archive/', 204],
    ['/example-notes/', 204],
    ['/_local-web/health/example-notes/backend', 503],
    ['/example-tasks/', tasksOnline ? 204 : 503],
    ['/example-tasks/healthz', 204],
  ] as const) {
    await page.route(`**${path}`, (route) => route.request().method() === 'HEAD'
      ? route.fulfill({ status })
      : route.fulfill({ contentType: 'text/html', body: '<h1>Opened application</h1>' }));
  }
  await page.goto(indexUrl);
  await expect(page).toHaveTitle('System Index');
  for (const [name, status] of [
    ['Example Archive', 'Online'], ['Example Notes', 'Frontend only'],
    ['Example Tasks', tasksOnline ? 'Online' : 'Offline'],
  ]) {
    await expect(page.getByRole('link', { name: `Open ${name}` }).getByRole('status'))
      .toHaveAttribute('aria-label', status);
  }
}

test('shows app health and opens an app with the keyboard', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await openIndex(page);
  await expect(page.getByRole('link', { name: 'UI Gallery' }))
    .toHaveAttribute('href', '/_local-web/platform/ui-gallery/');
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  const card = page.getByRole('link', { name: 'Open Example Archive' });
  await card.focus();
  await page.keyboard.press('Tab');
  await page.keyboard.press('Shift+Tab');
  await expect(card).toBeFocused();
  expect(await card.evaluate((element) => Number.parseFloat(getComputedStyle(element).outlineWidth)))
    .toBeGreaterThan(0);
  await card.press('Enter');
  await expect(page).toHaveURL(/\/example-archive\/$/);
  await expect(page.getByRole('heading', { name: 'Opened application' })).toBeVisible();
  expect(errors).toEqual([]);
});

test('separates the shared UI version from the header controls', async ({ page }) => {
  await openIndex(page);

  const header = page.locator('.lwp-platform-shell__header-actions');
  const version = header.locator('.lwp-platform-version');
  const gallery = header.getByRole('link', { name: 'UI Gallery' });
  const theme = header.getByRole('group', { name: 'Colour mode' });
  const [versionBox, galleryBox, themeBox] = await Promise.all([
    version.boundingBox(),
    gallery.boundingBox(),
    theme.boundingBox(),
  ]);

  expect(versionBox).not.toBeNull();
  expect(galleryBox).not.toBeNull();
  expect(themeBox).not.toBeNull();
  const versionGap = galleryBox!.x - (versionBox!.x + versionBox!.width);
  const controlGap = themeBox!.x - (galleryBox!.x + galleryBox!.width);
  expect(versionGap).toBeGreaterThan(controlGap);
});

test('keeps failed apps accessible on a narrow screen and opens the full card', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await openIndex(page, false);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth))
    .toBe(true);
  const card = page.getByRole('link', { name: 'Open Example Tasks' });
  await card.click({ position: { x: 12, y: 12 } });
  await expect(page).toHaveURL(/\/example-tasks\/$/);
  await expect(page.getByRole('heading', { name: 'Opened application' })).toBeVisible();
});


test('services never create health probes and their links open with the keyboard', async ({ page }) => {
  const serviceRequests: string[] = [];
  await page.context().route('https://local.example.ts.net:*/**', (route) => {
    serviceRequests.push(route.request().method());
    return route.fulfill({ contentType: 'text/html', body: '<h1>Opened service</h1>' });
  });
  await openIndex(page);
  const services = page.getByRole('region', { name: 'NAS services' });
  await expect(services.getByRole('link')).toHaveCount(7);
  await expect(services.getByRole('status')).toHaveCount(0);
  await expect(page.getByRole('heading', { level: 2, name: 'Local Web apps' })).toBeVisible();
  await expect(services).not.toContainText('External service');
  for (const [title, port] of [
    ['NAS Administration', 8443], ['Jellyfin', 8444], ['qBittorrent', 8080],
    ['Prowlarr', 9696], ['Radarr', 7878], ['Sonarr', 8989], ['slskd', 5030],
  ]) {
    await expect(services.getByRole('link', { name: `Open ${title} (opens in a new tab)`, exact: true }))
      .toHaveAttribute('href', `https://local.example.ts.net:${port}`);
  }
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  expect(serviceRequests).toEqual([]);
  const link = services.getByRole('link', { name: 'Open Jellyfin (opens in a new tab)', exact: true });
  await link.focus();
  await expect(link).toBeFocused();
  const popupPromise = page.waitForEvent('popup');
  await link.press('Enter');
  const popup = await popupPromise;
  await expect(popup).toHaveURL('https://local.example.ts.net:8444/');
  await expect(popup.getByRole('heading', { name: 'Opened service' })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${indexUrl}$`));
  expect(await popup.evaluate(() => window.opener === null)).toBe(true);
  expect(serviceRequests).toEqual(['GET']);
});

for (const colourScheme of ['light', 'dark'] as const) {
  for (const width of [1280, 390, 320]) {
    test(`two sections remain accessible at ${width}px in ${colourScheme} mode`, async ({ page }, testInfo) => {
      await page.emulateMedia({ colorScheme: colourScheme });
      await page.setViewportSize({ width, height: 900 });
      const errors: string[] = [];
      page.on('pageerror', (error) => errors.push(error.message));
      const consoleMessages: string[] = [];
      page.on('console', (message) => {
        if (message.type() === 'error' || message.type() === 'warning') consoleMessages.push(message.text());
      });
      await openIndex(page);
      await expect(page.getByRole('heading', { name: 'NAS services', level: 2 })).toBeVisible();
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
      const service = page.getByRole('region', { name: 'NAS services' }).getByRole('link').first();
      const serviceBox = await service.boundingBox();
      expect(serviceBox!.height).toBeGreaterThanOrEqual(100);
      expect(serviceBox!.height).toBeLessThan(130);
      const serviceBoxes = await page.getByRole('region', { name: 'NAS services' }).getByRole('link')
        .evaluateAll(elements => elements.map(element => ({ width: element.getBoundingClientRect().width, height: element.getBoundingClientRect().height })));
      for (const box of serviceBoxes) {
        expect(box.width).toBeCloseTo(serviceBox!.width, 0);
        expect(box.height).toBe(serviceBox!.height);
      }
      expect(await service.evaluate(element => getComputedStyle(element).textDecorationLine)).toBe('none');
      expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
      expect(errors).toEqual([]);
      // The fixture deliberately returns 503 for the Example Notes backend health probe.
      expect(consoleMessages).toEqual(['Failed to load resource: the server responded with a status of 503 (Service Unavailable)']);
      await page.screenshot({ path: testInfo.outputPath(`index-${colourScheme}-${width}.png`), fullPage: true });
    });
  }
}
