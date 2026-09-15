import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';

const galleryUrl = '/?theme=candidate';
const appPageGalleryUrl = '/?theme=candidate&frame=app-page';
const immersiveGalleryUrl = '/immersive-frame.html?theme=candidate';
const closedExportCsp = "default-src 'none'; connect-src 'none'; img-src 'none'; font-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; script-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'";
const caddyCsp = "default-src 'self'; connect-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'";
const extendedCaddyCsp = "default-src 'self'; connect-src 'self' https://api.maptiler.com; img-src 'self' data: https://images.example.test; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'";

const trackConsole = (page: Page) => {
  const messages: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error' || message.type() === 'warning') {
      messages.push(`${message.type()}: ${message.text()}`);
    }
  });
  page.on('pageerror', (error) => messages.push(`pageerror: ${error.message}`));
  return messages;
};

const openGallery = async (page: Page, url = galleryUrl) => {
  const messages = trackConsole(page);
  const response = await page.goto(url);
  expect(response?.ok()).toBe(true);
  await expect(page).toHaveTitle('Local Web UI compatibility gallery');
  await expect(page.getByRole('heading', { name: 'UI compatibility gallery', level: 1 })).toBeVisible();
  await expect(page.getByRole('link', { name: 'System Index', exact: true })).toBeVisible();
  await expect(page.locator('vite-error-overlay')).toHaveCount(0);
  expect(await page.locator('body').innerText()).not.toHaveLength(0);
  expect(messages).toEqual([]);
  return messages;
};

const appThemeChoice = (page: Page, name: string) =>
  page.locator('.lwp-platform-shell').getByRole('button', { name });

const expectNoHorizontalOverflow = async (page: Page) => {
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth),
  ).toBe(true);
};

const box = async (locator: ReturnType<Page['locator']>) => {
  const value = await locator.boundingBox();
  expect(value).not.toBeNull();
  return value!;
};

test('supports keyboard focus, actions, forms, tooltip, and dialog focus return', async ({ page }) => {
  const messages = await openGallery(page);
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to UI Gallery content' })).toBeFocused();

  await page.getByRole('button', { name: 'Run action' }).click();
  await expect(page.getByRole('status', { name: 'Action completed' })).toBeVisible();
  await page.getByRole('textbox', { name: 'App name' }).fill('Roadbook');
  await expect(page.getByRole('textbox', { name: 'App name' })).toHaveValue('Roadbook');
  await page.getByLabel('App type').selectOption('service');
  await expect(page.getByLabel('App type')).toHaveValue('service');

  const help = page.getByRole('button', { name: 'More information' });
  await help.focus();
  await expect(page.getByRole('tooltip')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('tooltip')).toBeHidden();

  const trigger = page.getByRole('button', { name: 'Open dialog' });
  await trigger.click();
  await expect(page.getByRole('dialog', { name: 'Gallery dialog' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Dialog note' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog', { name: 'Gallery dialog' })).toBeHidden();
  await expect(trigger).toBeFocused();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  expect(messages).toEqual([]);
});

test('skips unusable autofocus candidates but accepts a visible child of a hidden ancestor', async ({ page }) => {
  const messages = await openGallery(page);
  await page.evaluate(() => {
    const dialog = document.querySelector<HTMLDialogElement>('dialog');
    const body = dialog?.querySelector<HTMLElement>('.lwp-dialog__body');
    const existingInput = body?.querySelector<HTMLInputElement>('input');
    if (!body || !existingInput) throw new Error('gallery dialog fixture is unavailable');

    existingInput.disabled = true;
    existingInput.removeAttribute('autofocus');
    existingInput.removeAttribute('data-lwp-autofocus');

    const disabled = document.createElement('input');
    disabled.autofocus = true;
    disabled.disabled = true;
    disabled.setAttribute('aria-label', 'Disabled autofocus');

    const hidden = document.createElement('div');
    hidden.hidden = true;
    hidden.innerHTML = '<input autofocus aria-label="Hidden autofocus">';

    const inert = document.createElement('div');
    inert.inert = true;
    inert.innerHTML = '<input autofocus aria-label="Inert autofocus">';

    const marker = document.createElement('span');
    marker.dataset.lwpAutofocus = 'true';
    marker.textContent = 'Unfocusable marker';

    const visibilityHidden = document.createElement('div');
    visibilityHidden.style.visibility = 'hidden';
    const visible = document.createElement('input');
    visible.autofocus = true;
    visible.dataset.lwpAutofocus = 'true';
    visible.style.visibility = 'visible';
    visible.setAttribute('aria-label', 'Visible autofocus');
    visibilityHidden.append(visible);

    body.prepend(disabled, hidden, inert, marker, visibilityHidden);
  });

  const trigger = page.getByRole('button', { name: 'Open dialog' });
  await trigger.click();
  await expect(page.getByRole('textbox', { name: 'Visible autofocus' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(trigger).toBeFocused();

  await page.evaluate(() => {
    const visible = document.querySelector<HTMLInputElement>('[aria-label="Visible autofocus"]');
    if (!visible) throw new Error('visible autofocus fixture is unavailable');
    visible.autofocus = false;
    visible.removeAttribute('autofocus');
    visible.removeAttribute('data-lwp-autofocus');
  });
  await trigger.click();
  await expect(page.getByRole('textbox', { name: 'Visible autofocus' })).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(trigger).toBeFocused();
  expect(messages).toEqual([]);
});

test('uses Close without scrolling when no dialog body control can receive focus', async ({ page }) => {
  const messages = await openGallery(page);
  await page.evaluate(() => {
    const dialog = document.querySelector<HTMLDialogElement>('dialog');
    const body = dialog?.querySelector<HTMLElement>('.lwp-dialog__body');
    const existingInput = body?.querySelector<HTMLInputElement>('input');
    const action = dialog?.querySelector<HTMLButtonElement>('.lwp-dialog__actions button');
    if (!body || !existingInput || !action) throw new Error('gallery dialog fixture is unavailable');

    existingInput.disabled = true;
    existingInput.removeAttribute('autofocus');
    existingInput.removeAttribute('data-lwp-autofocus');
    action.disabled = true;

    const hidden = document.createElement('div');
    hidden.hidden = true;
    hidden.innerHTML = '<input data-lwp-autofocus="true" aria-label="Hidden autofocus">';
    const marker = document.createElement('span');
    marker.dataset.lwpAutofocus = 'true';
    marker.textContent = 'Unfocusable marker';
    body.prepend(hidden, marker);
  });

  const trigger = page.getByRole('button', { name: 'Open dialog' });
  await trigger.scrollIntoViewIfNeeded();
  await trigger.focus();
  const scrollBeforeOpen = await page.evaluate(() => window.scrollY);
  await trigger.press('Enter');

  await expect(page.getByRole('dialog', { name: 'Gallery dialog' })
    .getByRole('button', { name: 'Close' })).toBeFocused();
  expect(await page.evaluate(() => window.scrollY)).toBe(scrollBeforeOpen);
  await page.keyboard.press('Escape');
  await expect(trigger).toBeFocused();
  expect(messages).toEqual([]);
});

test('keeps a native modal focusable inside an inert app ancestor', async ({ page }) => {
  const messages = await openGallery(page);
  const trigger = page.getByRole('button', { name: 'Open dialog' });
  await trigger.focus();
  await page.evaluate(() => {
    const shell = document.querySelector<HTMLElement>('.lwp-platform-shell');
    const opener = [...document.querySelectorAll<HTMLButtonElement>('button')]
      .find((button) => button.textContent === 'Open dialog');
    if (!shell || !opener) throw new Error('gallery dialog fixture is unavailable');
    shell.inert = true;
    opener.click();
  });

  await expect(page.getByRole('textbox', { name: 'Dialog note' })).toBeFocused();
  await page.evaluate(() => {
    const shell = document.querySelector<HTMLElement>('.lwp-platform-shell');
    if (shell) shell.inert = false;
  });
  await page.keyboard.press('Escape');
  await expect(trigger).toBeFocused();
  expect(messages).toEqual([]);
});

for (const servingCsp of [caddyCsp, extendedCaddyCsp]) {
  test(`downloads the same closed context document under ${servingCsp === caddyCsp ? 'default' : 'extended'} serving CSP`, async ({ page }) => {
    await page.route('**/*', async (route) => {
      const response = await route.fetch();
      await route.fulfill({
        response,
        headers: { ...response.headers(), 'content-security-policy': servingCsp },
      });
    });
    const response = await page.goto(galleryUrl);
    expect(response?.headers()['content-security-policy']).toBe(servingCsp);
    await expect(page.getByRole('heading', { name: 'UI compatibility gallery', level: 1 })).toBeVisible();
    await expect(page.locator('.lwp-platform-shell__header').getByRole('button', { name: 'Export context' }))
      .toHaveCount(0);

    const downloadReady = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Export context' }).click();
    const download = await downloadReady;
    const path = await download.path();
    expect(path).not.toBeNull();
    const html = await readFile(path!, 'utf8');

    expect(download.suggestedFilename()).toBe('ui-gallery--ui-compatibility-gallery-actions--2026-08-22.html');
    expect(html).toContain(`<meta http-equiv="Content-Security-Policy" content="${closedExportCsp}">`);
    expect(html).not.toContain('https://api.maptiler.com');
    expect(html).not.toContain('https://images.example.test');
    const payload = JSON.parse(
      html.match(/<script type="application\/json" data-context-export-json>(.*?)<\/script>/)?.[1] ?? '',
    ) as { schema: string; app: { id: string; name: string } };
    expect(payload).toMatchObject({
      schema: 'local-web-context/v1',
      app: { id: 'ui-gallery', name: 'UI Gallery' },
    });
  });
}

test('persists colour mode across reload and synchronises it across tabs', async ({ page, context }) => {
  const messages = await openGallery(page);
  await appThemeChoice(page, 'Dark colour mode').click();
  await expect(page.locator('html')).toHaveAttribute('data-lwp-colour-mode', 'dark');
  await page.reload();
  await expect(page.locator('html')).toHaveAttribute('data-lwp-colour-mode', 'dark');
  await expect(appThemeChoice(page, 'Dark colour mode')).toHaveAttribute('aria-pressed', 'true');

  const second = await context.newPage();
  const secondMessages = trackConsole(second);
  await second.goto(galleryUrl);
  await expect(second.locator('html')).toHaveAttribute('data-lwp-colour-mode', 'dark');
  await appThemeChoice(page, 'Light colour mode').click();
  await expect(second.locator('html')).toHaveAttribute('data-lwp-colour-mode', 'light');
  expect(messages).toEqual([]);
  expect(secondMessages).toEqual([]);
});

test('remains usable from package fallback tokens when no host theme is available', async ({ page }) => {
  const messages = trackConsole(page);
  const response = await page.goto('/?theme=fallback');
  expect(response?.ok()).toBe(true);
  await expect(page.getByRole('heading', { name: 'Fallback tokens' })).toBeVisible();
  await expect(page.locator('link[data-lwp-gallery-theme]')).toHaveCount(0);
  expect(
    await page.locator('html').evaluate((root) =>
      getComputedStyle(root).getPropertyValue('--lwp-colour-canvas').trim(),
    ),
  ).not.toBe('');
  await expectNoHorizontalOverflow(page);
  expect(messages).toEqual([]);
});

test('removes component motion when reduced motion is requested', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await openGallery(page);
  expect(await page.getByRole('button', { name: 'Run action' }).evaluate((element) => getComputedStyle(element).transitionDuration)).toBe('0s');
});

test('keeps the complete app-page breadcrumb readable at 320px', async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 844 });
  const messages = trackConsole(page);
  const response = await page.goto(appPageGalleryUrl);
  expect(response?.ok()).toBe(true);
  await expect(page.getByRole('navigation', { name: 'Location' })).toHaveText(
    'Local/System Index/Sample Workspace/David Platt',
  );
  await expect(page.getByRole('link', { name: 'Sample Workspace' }))
    .toHaveAttribute('href', '/samplebeta/');
  await expect(page.getByText('David Platt', { exact: true })).toHaveAttribute('aria-current', 'page');
  const appCrumb = await box(page.getByRole('link', { name: 'Sample Workspace' }));
  const pageCrumb = await box(page.getByText('David Platt', { exact: true }));
  expect(pageCrumb.y).toBeGreaterThan(appCrumb.y);
  await expectNoHorizontalOverflow(page);
  expect(messages).toEqual([]);
});

for (const viewport of [
  { width: 320, height: 844 },
] as const) {
  test(`keeps the immersive canvas within the ${viewport.width}x${viewport.height} viewport`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const messages = trackConsole(page);
    const response = await page.goto(immersiveGalleryUrl);
    expect(response?.ok()).toBe(true);
    await expect(page).toHaveTitle('Local Web UI compatibility gallery');
    await expect(page.locator('vite-error-overlay')).toHaveCount(0);

    const shellBox = await box(page.locator('.lwp-platform-shell'));
    const headerBox = await box(page.locator('.lwp-platform-shell__header'));
    const canvasBox = await box(page.getByTestId('immersive-canvas'));
    expect(Math.abs(shellBox.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(shellBox.width - viewport.width)).toBeLessThanOrEqual(1);
    await expect(page.locator('.lwp-platform-shell')).toHaveCSS('border-top-width', '0px');
    expect(canvasBox.y).toBeGreaterThanOrEqual(headerBox.y + headerBox.height - 1);
    expect(Math.abs(shellBox.height - viewport.height)).toBeLessThanOrEqual(1);
    expect(Math.abs(canvasBox.height - (viewport.height - headerBox.height))).toBeLessThanOrEqual(1);
    expect(
      await page.evaluate(() => document.documentElement.scrollHeight <= document.documentElement.clientHeight),
    ).toBe(true);
    await expectNoHorizontalOverflow(page);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    expect(messages).toEqual([]);
  });
}

// Hosted and developer macOS Chromium builds have a small glyph-rasterisation
// variance at desktop scale. Bound that variance by an absolute pixel count;
// the smaller mobile baselines remain pixel-exact.
for (const snapshot of [
  { name: 'gallery-light-desktop.png', mode: 'light', viewport: { width: 1280, height: 1200 }, maxDiffPixels: 4_000 },
  { name: 'gallery-dark-desktop.png', mode: 'dark', viewport: { width: 1280, height: 1200 }, maxDiffPixels: 4_000 },
  { name: 'gallery-light-mobile.png', mode: 'light', viewport: { width: 390, height: 844 }, maxDiffPixels: 0 },
  { name: 'gallery-dark-mobile.png', mode: 'dark', viewport: { width: 390, height: 844 }, maxDiffPixels: 0 },
] as const) {
  test(`matches ${snapshot.name}`, async ({ page }) => {
    await page.setViewportSize(snapshot.viewport);
    await page.emulateMedia({ colorScheme: snapshot.mode, reducedMotion: 'reduce' });
    const messages = await openGallery(page);
    await appThemeChoice(
      page,
      `${snapshot.mode === 'light' ? 'Light' : 'Dark'} colour mode`,
    ).click();
    await expectNoHorizontalOverflow(page);
    await expect(page).toHaveScreenshot(snapshot.name, {
      animations: 'disabled',
      fullPage: true,
      maxDiffPixels: snapshot.maxDiffPixels,
    });
    expect(messages).toEqual([]);
  });
}
