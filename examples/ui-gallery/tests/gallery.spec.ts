import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';

const galleryUrl = '/?theme=candidate';
const indexGalleryUrl = '/index-frame.html?theme=candidate';
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

const openIndexGallery = async (page: Page) => {
  const messages = trackConsole(page);
  const response = await page.goto(indexGalleryUrl);
  expect(response?.ok()).toBe(true);
  await expect(page).toHaveTitle('Local Web UI compatibility gallery');
  await expect(page.getByRole('heading', { name: 'System Index', level: 1 })).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Location' })).toHaveText('Local/System Index');
  await expect(page.locator('vite-error-overlay')).toHaveCount(0);
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

const colourContrast = async (
  locator: ReturnType<Page['locator']>,
  foregroundProperty: string,
) => locator.evaluate((element, property) => {
  type Rgba = { red: number; green: number; blue: number; alpha: number };
  const canvas = document.createElement('canvas');
  canvas.width = 1;
  canvas.height = 1;
  const context = canvas.getContext('2d', { willReadFrequently: true });
  if (!context) {
    throw new Error('Canvas colour context unavailable');
  }
  const parse = (value: string): Rgba => {
    context.clearRect(0, 0, 1, 1);
    context.fillStyle = value;
    context.fillRect(0, 0, 1, 1);
    const [red, green, blue, alpha] = context.getImageData(0, 0, 1, 1).data;
    return { red, green, blue, alpha: alpha / 255 };
  };
  const composite = (front: Rgba, back: Rgba): Rgba => {
    const alpha = front.alpha + back.alpha * (1 - front.alpha);
    const channel = (frontChannel: number, backChannel: number) =>
      alpha === 0
        ? 0
        : (frontChannel * front.alpha
          + backChannel * back.alpha * (1 - front.alpha)) / alpha;
    return {
      red: channel(front.red, back.red),
      green: channel(front.green, back.green),
      blue: channel(front.blue, back.blue),
      alpha,
    };
  };
  const layers: Rgba[] = [];
  for (let current: Element | null = element; current; current = current.parentElement) {
    layers.push(parse(getComputedStyle(current).backgroundColor));
  }
  const background = layers.reverse().reduce(
    (result, layer) => composite(layer, result),
    { red: 255, green: 255, blue: 255, alpha: 1 },
  );
  const foreground = parse(getComputedStyle(element).getPropertyValue(property));
  const luminance = (colour: Rgba) => {
    const linear = (channel: number) => {
      const normalised = channel / 255;
      return normalised <= 0.04045
        ? normalised / 12.92
        : ((normalised + 0.055) / 1.055) ** 2.4;
    };
    return 0.2126 * linear(colour.red)
      + 0.7152 * linear(colour.green)
      + 0.0722 * linear(colour.blue);
  };
  const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
  return (values[0] + 0.05) / (values[1] + 0.05);
}, foregroundProperty);

test('identifies the complete, accessible, nonblank gallery without runtime warnings', async ({ page }) => {
  const messages = await openGallery(page);
  for (const section of ['Content', 'Layout', 'Actions', 'Forms', 'Feedback', 'Overlays', 'Icons', 'Accent validation', 'Fallback tokens']) {
    await expect(page.getByRole('heading', { name: section })).toBeVisible();
  }
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
  await expectNoHorizontalOverflow(page);
  expect(messages).toEqual([]);
});

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
  expect(messages).toEqual([]);
});

test('uses native focus semantics to skip unusable dialog autofocus candidates', async ({ page }) => {
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

    const fallback = document.createElement('button');
    fallback.type = 'button';
    fallback.textContent = 'Browser focus fallback';
    body.prepend(disabled, hidden, inert, marker);
    body.append(fallback);
  });

  const trigger = page.getByRole('button', { name: 'Open dialog' });
  await trigger.click();
  await expect(page.getByRole('button', { name: 'Browser focus fallback' })).toBeFocused();
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

test('sizes the context export action to the full theme control height', async ({ page }) => {
  await openGallery(page);

  const exportControl = await box(page.getByRole('button', { name: 'Export context' }));
  const themeControl = await box(
    page.locator('.lwp-platform-shell__header').getByRole('group', { name: 'Colour mode' }),
  );

  expect(exportControl.width).toBe(exportControl.height);
  expect(exportControl.height).toBe(themeControl.height);
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

test('serves the query-selected candidate theme without depending on live theme state', async ({ request, page }) => {
  const candidate = await request.get('/candidate-theme.css');
  expect(candidate.ok()).toBe(true);
  expect(candidate.headers()['content-type']).toContain('text/css');
  expect(await candidate.text()).toContain(':root[data-lwp-colour-mode="dark"]');
  await openGallery(page);
  await expect(page.locator('link[data-lwp-gallery-theme]')).toHaveAttribute('href', '/candidate-theme.css');
  expect(await page.locator('html').evaluate((root) => getComputedStyle(root).getPropertyValue('--lwp-colour-canvas').trim())).not.toBe('');
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

test('keeps the app frame readable from desktop through mobile', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1200 });
  await openGallery(page);

  const headers = page.locator('.lwp-platform-shell__header');
  await expect(headers).toHaveCount(1);
  expect(await headers.evaluate((element) => getComputedStyle(element).display)).toBe('grid');
  await expect(page.getByRole('navigation', { name: 'Location' })).toHaveText(
    'Local/System Index/UI Gallery',
  );
  await expect(page.getByRole('link', { name: 'System Index', exact: true })).toHaveAttribute('href', '/');

  const desktopHeader = await box(headers);
  const desktopTheme = await box(headers.getByRole('group', { name: 'Colour mode' }));
  expect(Math.abs(desktopHeader.y - desktopTheme.y)).toBeLessThan(24);
  for (const button of await headers.getByRole('button').all()) {
    const target = await box(button);
    expect(target.width).toBeGreaterThanOrEqual(44);
    expect(target.height).toBeGreaterThanOrEqual(44);
  }

  await page.setViewportSize({ width: 390, height: 844 });
  const mobileHeader = await box(headers);
  const mobileLocation = await box(headers.getByRole('navigation', { name: 'Location' }));
  const mobileTheme = await box(headers.getByRole('group', { name: 'Colour mode' }));
  expect(mobileHeader.height).toBeGreaterThan(mobileTheme.height);
  expect(mobileTheme.y).toBeGreaterThan(mobileLocation.y);
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 320, height: 844 });
  await expectNoHorizontalOverflow(page);

  const sectionNavigation = page.getByRole('navigation', { name: 'Gallery sections' });
  for (const link of await sectionNavigation.getByRole('link').all()) {
    const target = await box(link);
    expect(target.height).toBeGreaterThanOrEqual(44);
  }

  const dataViewport = page.getByRole('region', { name: 'Application health comparison' });
  expect(await dataViewport.evaluate((element) => element.scrollWidth > element.clientWidth)).toBe(true);
  await dataViewport.focus();
  await expect(dataViewport).toBeFocused();

  const metrics = page.locator('.lwp-metric');
  const firstMetric = await box(metrics.nth(0));
  const secondMetric = await box(metrics.nth(1));
  const thirdMetric = await box(metrics.nth(2));
  expect(Math.abs(firstMetric.y - secondMetric.y)).toBeLessThanOrEqual(1);
  expect(thirdMetric.y).toBeGreaterThan(firstMetric.y);
});

test('keeps the index and app route landmarks and skip targets independent', async ({ page }) => {
  const appMessages = await openGallery(page);
  await expect(page.getByRole('main')).toHaveCount(1);
  const appSkipLink = page.getByRole('link', { name: 'Skip to UI Gallery content' });
  await appSkipLink.focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#lwp-main$/);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  expect(appMessages).toEqual([]);

  const indexMessages = await openIndexGallery(page);
  await expect(page.getByRole('main')).toHaveCount(1);
  const indexSkipLink = page.getByRole('link', { name: 'Skip to System Index content' });
  await indexSkipLink.focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#lwp-main$/);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  expect(indexMessages).toEqual([]);
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

test('keeps the immersive canvas full-bleed and accessible on desktop', async ({ page }) => {
  const viewport = { width: 1280, height: 720 };
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
  await expectNoHorizontalOverflow(page);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  expect(messages).toEqual([]);
});

for (const viewport of [
  { width: 390, height: 844 },
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

test('uses three-to-one foreground and state markers over restrained tints', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 1200 });
  await openGallery(page);

  for (const mode of ['light', 'dark'] as const) {
    const selected = appThemeChoice(page,
      `${mode === 'light' ? 'Light' : 'Dark'} colour mode`,
    );
    await selected.click();
    await expect(page.locator('html')).toHaveAttribute('data-lwp-colour-mode', mode);
    await expect(selected).toHaveAttribute('aria-pressed', 'true');
    const indicator = selected.locator('.lwp-segmented-control__indicator');
    expect(await indicator.evaluate((element) => getComputedStyle(element).backgroundColor))
      .not.toBe('rgba(0, 0, 0, 0)');
    await expect(indicator).toHaveCSS('opacity', '1');
    expect(await colourContrast(selected, 'color')).toBeGreaterThanOrEqual(3);
  }
});

for (const snapshot of [
  { name: 'gallery-light-desktop.png', mode: 'light', viewport: { width: 1280, height: 1200 } },
  { name: 'gallery-dark-desktop.png', mode: 'dark', viewport: { width: 1280, height: 1200 } },
  { name: 'gallery-light-mobile.png', mode: 'light', viewport: { width: 390, height: 844 } },
  { name: 'gallery-dark-mobile.png', mode: 'dark', viewport: { width: 390, height: 844 } },
  { name: 'gallery-light-compact.png', mode: 'light', viewport: { width: 320, height: 844 } },
  { name: 'gallery-dark-compact.png', mode: 'dark', viewport: { width: 320, height: 844 } },
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
    await expect(page).toHaveScreenshot(snapshot.name, { animations: 'disabled', fullPage: true });
    expect(messages).toEqual([]);
  });
}
