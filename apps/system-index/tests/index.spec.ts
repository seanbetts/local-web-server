import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { fileURLToPath } from 'node:url';

const indexUrl = '/_local-web/platform/index/';
const registryFixture = fileURLToPath(new URL('../fixtures/registry-v1.json', import.meta.url));

// Probe policy, deadlines, retries and body avoidance belong to statusController.test.ts.
// These journeys prove the real page wires the controller to accessible app links.
async function openIndex(page: Page, tasksOnline = true) {
  await page.route(`**${indexUrl}registry-v1.json`, (route) => route.fulfill({
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
