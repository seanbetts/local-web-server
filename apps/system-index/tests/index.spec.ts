import AxeBuilder from '@axe-core/playwright';
import { expect, test, type ConsoleMessage, type Page, type Route } from '@playwright/test';

const INDEX_URL = '/';
const NOTES_BACKEND_HEALTH_PATH = '/_local-web/health/example-notes/backend';
const NOTES_BACKEND_HEALTH_URL = `http://127.0.0.1:4180${NOTES_BACKEND_HEALTH_PATH}`;
const FAILED_RESOURCE_503_MESSAGE =
  'Failed to load resource: the server responded with a status of 503 (Service Unavailable)';
const FINAL_STATUS_LABELS = ['Online', 'Frontend only', 'Online'] as const;
const VIEWPORTS = [
  { height: 1100, name: 'desktop', width: 1440 },
  { height: 1100, name: 'tablet', width: 820 },
  { height: 1100, name: 'breakpoint edge', width: 769 },
  { height: 844, name: 'mobile', width: 390 },
  { height: 844, name: 'narrow', width: 320 },
] as const;

const expectedApps = [
  { colour: 'rgb(117, 167, 255)', href: '/example-archive/', status: 'Online', title: 'Example Archive' },
  { colour: 'rgb(118, 211, 155)', href: '/example-notes/', status: 'Frontend only', title: 'Example Notes' },
  { colour: 'rgb(122, 23, 53)', href: '/example-tasks/', status: 'Online', title: 'Example Tasks' },
] as const;

const expectedProbes = [
  { path: '/example-archive/', status: 204 },
  { path: '/example-notes/', status: 204 },
  { path: NOTES_BACKEND_HEALTH_PATH, status: 503 },
  { path: '/example-tasks/', status: 204 },
  { path: '/example-tasks/healthz', status: 204 },
] as const;

type BrowserIssues = {
  console: string[];
  expectedNotesHealthResourceErrors: string[];
  externalRequests: string[];
  probeBodyReads: string[];
};

type ProbeObservation = {
  count: number;
  method: string;
  path: string;
  status: number;
};

type ReadyIndex = {
  issues: BrowserIssues;
  probes: ProbeObservation[];
};

const sorted = <Value extends { path: string }>(values: readonly Value[]) =>
  [...values].sort((left, right) => left.path.localeCompare(right.path));

const expectedProbeObservations = sorted(expectedProbes.map((probe) => ({
  ...probe,
  count: 1,
  method: 'HEAD',
})));

const expectNoHorizontalOverflow = async (page: Page) => {
  expect(await page.evaluate(() =>
    document.documentElement.scrollWidth <= document.documentElement.clientWidth,
  )).toBe(true);
};

const box = async (locator: ReturnType<Page['locator']>) => {
  const value = await locator.boundingBox();
  expect(value).not.toBeNull();
  return value!;
};

const isExpectedNotesHealthResourceError = (message: ConsoleMessage) => (
  message.type() === 'error'
  && message.text() === FAILED_RESOURCE_503_MESSAGE
  && message.location().url === NOTES_BACKEND_HEALTH_URL
);

const interceptHealthChecks = async (page: Page, delayMs = 0) => {
  const counts = new Map<string, number>();
  const probes: ProbeObservation[] = [];
  const fulfill = (path: string, status: number) => async (route: Route) => {
    const request = route.request();
    if (request.method() !== 'HEAD') {
      await route.continue();
      return;
    }
    const requestedPath = new URL(request.url()).pathname;
    expect(requestedPath).toBe(path);
    const count = (counts.get(path) ?? 0) + 1;
    counts.set(path, count);
    probes.push({ count, method: request.method(), path, status });
    if (delayMs > 0) {
      await new Promise<void>((resolve) => setTimeout(resolve, delayMs));
    }
    await route.fulfill({ status });
  };

  for (const probe of expectedProbes) {
    await page.route(`**${probe.path}`, fulfill(probe.path, probe.status));
  }
  return probes;
};

const trackBrowserIssues = async (page: Page): Promise<BrowserIssues> => {
  const issues: BrowserIssues = {
    console: [],
    expectedNotesHealthResourceErrors: [],
    externalRequests: [],
    probeBodyReads: [],
  };
  page.on('console', (message) => {
    if (isExpectedNotesHealthResourceError(message)) {
      issues.expectedNotesHealthResourceErrors.push(`${message.location().url}: ${message.text()}`);
      return;
    }
    if (message.type() === 'error' || message.type() === 'warning') {
      issues.console.push(`${message.type()}: ${message.text()}`);
    }
  });
  page.on('pageerror', (error) => issues.console.push(`pageerror: ${error.message}`));
  page.on('request', (request) => {
    if (!request.url().startsWith('http://127.0.0.1:4180/')) {
      issues.externalRequests.push(request.url());
    }
  });
  await page.addInitScript((probePaths) => {
    const nativeFetch = window.fetch.bind(window);
    const bodyReaders = new Set(['arrayBuffer', 'blob', 'formData', 'json', 'text']);
    window.fetch = async (input, init) => {
      const response = await nativeFetch(input, init);
      const requestedUrl = input instanceof Request ? input.url : input.toString();
      const path = new URL(requestedUrl, window.location.origin).pathname;
      if (!probePaths.includes(path)) {
        return response;
      }
      return new Proxy(response, {
        get(target, property) {
          if (typeof property === 'string' && bodyReaders.has(property)) {
            (window as Window & { __systemIndexProbeBodyReads: string[] })
              .__systemIndexProbeBodyReads.push(path);
            throw new Error(`Probe response body read: ${path}`);
          }
          return Reflect.get(target, property, target);
        },
      });
    };
    (window as Window & { __systemIndexProbeBodyReads: string[] })
      .__systemIndexProbeBodyReads = [];
  }, expectedProbes.map((probe) => probe.path));
  return issues;
};

const waitForFinalStatuses = async (page: Page) => {
  await expect(page.getByRole('status')).toHaveCount(FINAL_STATUS_LABELS.length);
  await expect.poll(async () => page.getByRole('status').evaluateAll((statuses) =>
    statuses.map((status) => status.getAttribute('aria-label')),
  )).toEqual(FINAL_STATUS_LABELS);
};

const waitForVerifiedReadyState = async (page: Page, probes: ProbeObservation[]) => {
  await waitForFinalStatuses(page);
  await expect.poll(() => sorted(probes)).toEqual(expectedProbeObservations);
};

const openReadyIndex = async (page: Page, delayMs = 0): Promise<ReadyIndex> => {
  const issues = await trackBrowserIssues(page);
  const probes = await interceptHealthChecks(page, delayMs);
  const response = await page.goto(INDEX_URL);
  expect(response?.ok()).toBe(true);
  await expect(page).toHaveTitle('System Index');
  await expect(page.getByRole('region', { name: 'System Index' })).toBeVisible();
  await expect(page.locator('h1.system-index__visually-hidden')).toHaveCount(1);
  await expect(page.locator('vite-error-overlay')).toHaveCount(0);
  await waitForVerifiedReadyState(page, probes);
  expect(issues.expectedNotesHealthResourceErrors).toEqual([
    `${NOTES_BACKEND_HEALTH_URL}: ${FAILED_RESOURCE_503_MESSAGE}`,
  ]);
  return { issues, probes };
};

const assertBrowserHealth = async (page: Page, issues: BrowserIssues) => {
  await expectNoHorizontalOverflow(page);
  expect(issues.console).toEqual([]);
  expect(issues.externalRequests).toEqual([]);
  expect(await page.evaluate(() =>
    (window as Window & { __systemIndexProbeBodyReads: string[] }).__systemIndexProbeBodyReads,
  )).toEqual([]);
};

const assertIndexSemantics = async (page: Page) => {
  const location = page.getByRole('navigation', { name: 'Location' });
  await expect(location).toHaveText('Local/System Index');
  await expect(location.getByRole('link')).toHaveCount(0);
  await expect(location.locator('[aria-current="page"]')).toHaveText('System Index');
  await expect(page.getByText(/apps$/i)).toHaveCount(0);

  const cards = page.locator('a.system-index-card');
  await expect(cards).toHaveCount(expectedApps.length);
  for (const [index, app] of expectedApps.entries()) {
    const card = cards.nth(index);
    await expect(card.getByRole('heading', { level: 2, name: app.title })).toBeVisible();
    await expect(card).toHaveAttribute('aria-label', `Open ${app.title}`);
    await expect(card).toHaveAttribute('href', app.href);
    await expect(card.getByText('STATUS', { exact: true })).toBeVisible();
    await expect(card.getByRole('status')).toHaveAttribute('aria-label', app.status);
  }
};

const assertThemeControl = async (page: Page) => {
  const theme = page.getByRole('group', { name: 'Colour mode' });
  const controls = theme.getByRole('button');
  await expect(controls).toHaveCount(3);
  expect(await controls.evaluateAll((buttons) => buttons.map((button) => button.getAttribute('aria-label'))))
    .toEqual(['System colour mode', 'Light colour mode', 'Dark colour mode']);
  for (const control of await controls.all()) {
    const target = await box(control);
    expect(target.width).toBeGreaterThanOrEqual(44);
    expect(target.height).toBeGreaterThanOrEqual(44);
    await expect(control).toHaveText('');
    await expect(control.locator('svg')).toHaveCount(1);
  }
};

const assertCardAnatomy = async (page: Page) => {
  const cards = page.locator('a.system-index-card');
  await expect(cards).toHaveCount(expectedApps.length);
  for (const [index, app] of expectedApps.entries()) {
    const card = cards.nth(index);
    expect(await card.evaluate((element) => ({
      boxShadow: getComputedStyle(element).boxShadow,
      children: [...element.children].map((child) => ({
        className: child.className,
        tagName: child.tagName,
      })),
      tagName: element.tagName,
    }))).toEqual({
      boxShadow: 'none',
      children: [
        { className: 'system-index-card__meta', tagName: 'DIV' },
        { className: 'system-index-card__body', tagName: 'DIV' },
      ],
      tagName: 'A',
    });
    await expect(card.locator('.system-index-card__header, .system-index-card__footer, .system-index-card__link, .system-index-card__open, .system-index-card__open-icon')).toHaveCount(0);
    const meta = card.locator(':scope > .system-index-card__meta');
    const body = card.locator(':scope > .system-index-card__body');
    const number = meta.locator('.system-index-card__number');
    const status = meta.getByRole('status');
    const icon = body.locator('.system-index-card__icon');
    const title = body.getByRole('heading', { level: 2, name: app.title });
    await expect(meta.locator('svg')).toHaveCount(0);
    await expect(number).toHaveText(String(index + 1).padStart(2, '0'));
    await expect(meta.getByText('STATUS', { exact: true })).toBeVisible();
    await expect(status).toHaveAttribute('aria-label', app.status);
    const numberBox = await box(number);
    const statusBox = await box(status);
    expect(numberBox.y + numberBox.height).toBeGreaterThan(statusBox.y);
    expect(statusBox.y + statusBox.height).toBeGreaterThan(numberBox.y);
    await expect(body).toHaveCSS('align-items', 'center');
    await expect(body).toHaveCSS('justify-content', 'center');
    await expect(body).toHaveCSS('text-align', 'center');
    await expect(icon).toHaveCSS('color', app.colour);
    const iconBox = await box(icon);
    const bodyBox = await box(body);
    const titleBox = await box(title);
    expect(iconBox.width).toBeGreaterThanOrEqual(72);
    expect(iconBox.height).toBeGreaterThanOrEqual(72);
    expect(Math.abs((iconBox.x + iconBox.width / 2) - (bodyBox.x + bodyBox.width / 2))).toBeLessThanOrEqual(1);
    expect(Math.abs((titleBox.x + titleBox.width / 2) - (bodyBox.x + bodyBox.width / 2))).toBeLessThanOrEqual(1);
  }
};

const assertResponsiveLayout = async (page: Page, width: number) => {
  const header = page.locator('.lwp-platform-shell__header');
  const location = page.getByRole('navigation', { name: 'Location' });
  const gallery = page.getByRole('link', { name: 'UI Gallery' });
  const theme = page.getByRole('group', { name: 'Colour mode' });
  const cards = page.locator('.system-index-card');
  const locationBox = await box(location);
  const galleryBox = await box(gallery);
  const themeBox = await box(theme);
  const cardBoxes = await Promise.all([0, 1, 2].map((index) => box(cards.nth(index))));
  await expect(header).toHaveCount(1);
  expect(Math.abs(galleryBox.width - galleryBox.height)).toBeLessThanOrEqual(1);
  expect(Math.abs(galleryBox.height - themeBox.height)).toBeLessThanOrEqual(1);

  if (width > 768) {
    expect(Math.abs(
      (locationBox.y + locationBox.height / 2) - (themeBox.y + themeBox.height / 2),
    )).toBeLessThanOrEqual(1);
    expect(themeBox.x).toBeGreaterThan(locationBox.x + locationBox.width);
    expect(cardBoxes[0]?.y).toBeCloseTo(cardBoxes[1]?.y ?? 0, 0);
    expect(cardBoxes[1]?.y).toBeCloseTo(cardBoxes[2]?.y ?? 0, 0);
    expect(cardBoxes[0]?.x).toBeLessThan(cardBoxes[1]?.x ?? 0);
    expect(cardBoxes[1]?.x).toBeLessThan(cardBoxes[2]?.x ?? 0);
    for (const card of cardBoxes) {
      expect(card?.height).toBeGreaterThanOrEqual(250);
      expect(card?.height).toBeLessThanOrEqual(260);
    }
  } else {
    const verticalGap = themeBox.y - (locationBox.y + locationBox.height);
    expect(verticalGap).toBeGreaterThanOrEqual(0);
    expect(verticalGap).toBeLessThanOrEqual(24);
    expect(cardBoxes[0]?.y).toBeLessThan(cardBoxes[1]?.y ?? 0);
    expect(cardBoxes[1]?.y).toBeLessThan(cardBoxes[2]?.y ?? 0);
    expect(cardBoxes[0]?.x).toBeCloseTo(cardBoxes[1]?.x ?? 0, 0);
    expect(cardBoxes[1]?.x).toBeCloseTo(cardBoxes[2]?.x ?? 0, 0);
    for (const card of cardBoxes) {
      expect(card?.height).toBeGreaterThanOrEqual(212);
      expect(card?.height).toBeLessThanOrEqual(216);
    }
  }
  await expectNoHorizontalOverflow(page);
};

const assertThemePersistenceAndSystemMode = async (page: Page) => {
  const theme = page.getByRole('group', { name: 'Colour mode' });
  await theme.getByRole('button', { name: 'Dark colour mode' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-lwp-colour-mode', 'dark');
  await page.reload();
  await waitForFinalStatuses(page);
  await expect(page.locator('html')).toHaveAttribute('data-lwp-colour-mode', 'dark');
  await expect(theme.getByRole('button', { name: 'Dark colour mode' })).toHaveAttribute('aria-pressed', 'true');
  await theme.getByRole('button', { name: 'System colour mode' }).click();
  await expect(page.locator('html')).not.toHaveAttribute('data-lwp-colour-mode');
  await page.emulateMedia({ colorScheme: 'light' });
  await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(246, 247, 249)');
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(page.locator('html')).not.toHaveAttribute('data-lwp-colour-mode');
  await expect(page.locator('body')).toHaveCSS('background-color', 'rgb(17, 21, 28)');
};

for (const viewport of VIEWPORTS) {
  test(`validates the complete ready-state contract at ${viewport.width}px`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const { issues } = await openReadyIndex(page);
    await assertIndexSemantics(page);
    await assertThemeControl(page);
    await assertCardAnatomy(page);
    await assertResponsiveLayout(page, viewport.width);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    await assertThemePersistenceAndSystemMode(page);
    await assertBrowserHealth(page, issues);
  });
}

test('does not run Axe until delayed probes reach the verified final state', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const issues = await trackBrowserIssues(page);
  const probes = await interceptHealthChecks(page, 400);
  await page.goto(INDEX_URL);
  await expect(page.getByRole('region', { name: 'System Index' })).toBeVisible();
  await expect(page.locator('h1.system-index__visually-hidden')).toHaveCount(1);
  await expect(page.getByRole('status')).toHaveCount(3);
  expect(await page.getByRole('status').evaluateAll((statuses) =>
    statuses.map((status) => status.getAttribute('aria-label')),
  )).toEqual(['Checking', 'Checking', 'Checking']);
  let axeRan = false;
  const axeAfterReady = waitForVerifiedReadyState(page, probes).then(async () => {
    axeRan = true;
    return new AxeBuilder({ page }).analyze();
  });
  expect(axeRan).toBe(false);
  expect((await axeAfterReady).violations).toEqual([]);
  expect(axeRan).toBe(true);
  await assertBrowserHealth(page, issues);
});

test('detects a GET probe response-body read from a deliberate harness mutation', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const { issues } = await openReadyIndex(page);
  expect(await page.evaluate(async () => {
    try {
      const response = await fetch('/example-archive/', { method: 'GET' });
      await response.text();
      return 'body read escaped detector';
    } catch (error) {
      return error instanceof Error ? error.message : String(error);
    }
  })).toBe('Probe response body read: /example-archive/');
  expect(await page.evaluate(() =>
    (window as Window & { __systemIndexProbeBodyReads: string[] }).__systemIndexProbeBodyReads,
  )).toEqual(['/example-archive/']);
  expect(issues.console).toEqual([]);
  expect(issues.externalRequests).toEqual([]);
});

test('shows a separate offline state when the Tasks frontend probe fails', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const issues = await trackBrowserIssues(page);
  await interceptHealthChecks(page);
  await page.route('**/example-tasks/', (route) => route.fulfill({ status: 503 }));
  await page.goto(INDEX_URL);
  await expect(page.getByRole('region', { name: 'System Index' })).toBeVisible();
  await expect(page.locator('h1.system-index__visually-hidden')).toHaveCount(1);
  await expect(
    page.getByRole('link', { name: 'Open Example Tasks' }).getByRole('status'),
  ).toHaveAttribute('aria-label', 'Offline');
  await expectNoHorizontalOverflow(page);
  expect(issues.console).toEqual([`error: ${FAILED_RESOURCE_503_MESSAGE}`]);
  expect(issues.externalRequests).toEqual([]);
  expect(await page.evaluate(() =>
    (window as Window & { __systemIndexProbeBodyReads: string[] }).__systemIndexProbeBodyReads,
  )).toEqual([]);
});

test('keeps a visible keyboard focus outline on a full-card link', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const { issues } = await openReadyIndex(page);
  const cards = page.locator('a.system-index-card');

  for (let index = 0; index < 4; index += 1) {
    await page.keyboard.press('Tab');
  }

  const card = cards.first();
  await expect(card).toBeFocused();
  const outline = await card.evaluate((element) => {
    const style = getComputedStyle(element);
    return {
      color: style.outlineColor,
      style: style.outlineStyle,
      width: Number.parseFloat(style.outlineWidth),
    };
  });
  expect(outline.color).not.toBe('rgba(0, 0, 0, 0)');
  expect(outline.style).not.toBe('none');
  expect(outline.width).toBeGreaterThan(0);
  await assertBrowserHealth(page, issues);
});

test('navigates from a retained card body point through the full-card link', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const { issues } = await openReadyIndex(page);
  const card = page.getByRole('link', { name: 'Open Example Archive' });
  const body = card.locator(':scope > .system-index-card__body');
  const bodyBox = await box(body);
  const target = {
    x: bodyBox.x + bodyBox.width - 12,
    y: bodyBox.y + bodyBox.height - 12,
  };

  await Promise.all([
    page.waitForURL((url) => url.pathname === '/example-archive/'),
    page.mouse.click(target.x, target.y),
  ]);

  expect(new URL(page.url()).pathname).toBe('/example-archive/');
  expect(issues.console).toEqual([]);
  expect(issues.externalRequests).toEqual([]);
});

for (const snapshot of [
  { name: 'index-light-desktop.png', mode: 'light', viewport: { width: 1440, height: 1100 } },
  { name: 'index-dark-desktop.png', mode: 'dark', viewport: { width: 1440, height: 1100 } },
  { name: 'index-light-mobile.png', mode: 'light', viewport: { width: 390, height: 844 } },
  { name: 'index-dark-mobile.png', mode: 'dark', viewport: { width: 390, height: 844 } },
] as const) {
  test(`matches ${snapshot.name}`, async ({ page }) => {
    await page.setViewportSize(snapshot.viewport);
    await page.emulateMedia({ colorScheme: snapshot.mode });
    const { issues } = await openReadyIndex(page);
    await page.getByRole('button', {
      name: `${snapshot.mode === 'light' ? 'Light' : 'Dark'} colour mode`,
    }).click();
    await expect(page.locator('html')).toHaveAttribute('data-lwp-colour-mode', snapshot.mode);
    await expect(page).toHaveScreenshot(snapshot.name, { animations: 'disabled', fullPage: true });
    await assertBrowserHealth(page, issues);
  });
}
