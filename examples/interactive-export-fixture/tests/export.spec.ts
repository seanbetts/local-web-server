import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Page } from '@playwright/test';
import { existsSync } from 'node:fs';
import { mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import type { PreviewServer } from 'vite';

const fixtureRoot = fileURLToPath(new URL('..', import.meta.url));

// Records belong only to this runtime service response, never to an app entry or template.
const runtimeCapture = () => ({
  snapshotData: {
    records: [
      { id: 'alpha', label: 'Runtime Alpha client', amount: 12 },
      { id: 'bravo', label: 'Runtime Bravo client', amount: 42 },
      { id: 'charlie', label: 'Runtime Charlie client', amount: 7 },
    ],
    calculatedTotal: 61,
  },
  viewState: { selectedRecordId: 'alpha', sort: { field: 'label', direction: 'ascending' } },
});

async function expectRecords(page: Page, labels: string[]) {
  await expect(page.getByRole('list', { name: 'Records' }).getByRole('button')).toHaveText(labels);
}

async function selectColourMode(page: Page, mode: 'Light' | 'Dark') {
  await page.getByRole('button', { name: `${mode} colour mode`, exact: true }).click();
  await expect(page.locator('html')).toHaveAttribute('data-lwp-colour-mode', mode.toLowerCase());
  // Check the settled theme, not the 120 ms background-colour transition.
  await page.evaluate(() => Promise.all(document.getAnimations().map((animation) => animation.finished)));
}

test.describe('real hosted download and offline reader', () => {
  let disposable: string;
  let server: PreviewServer;
  let hostedUrl: string;

  test.beforeAll(async () => {
    expect(existsSync(join(fixtureRoot, 'index.html')), 'the hosted runtime fixture must exist').toBe(true);
    disposable = await mkdtemp(join(tmpdir(), 'local-web-interactive-fixture-'));
    const { build, preview } = await import('vite');
    await build({ configFile: join(fixtureRoot, 'vite.config.ts'), build: { outDir: join(disposable, 'dist') }, logLevel: 'warn' });
    server = await preview({ configFile: false, root: fixtureRoot, build: { outDir: join(disposable, 'dist') }, preview: { host: '127.0.0.1', port: 0, open: false } });
    const address = server.httpServer.address();
    if (address === null || typeof address === 'string') throw new Error('fixture preview did not bind');
    hostedUrl = `http://127.0.0.1:${address.port}`;
  });

  test.afterAll(async () => {
    await server?.close();
    if (disposable) await rm(disposable, { recursive: true, force: true });
  });

  test('restores runtime records, total, selection and sort in one locally interactive file with no network attempts', async ({ page, browser }, testInfo) => {
    const hostedErrors: string[] = [];
    page.on('pageerror', (error) => hostedErrors.push(error.message));
    page.on('console', (message) => {
      if (['error', 'warning'].includes(message.type())) hostedErrors.push(message.text());
    });
    await page.route('**/api/records', (route) => route.fulfill({ json: runtimeCapture() }));
    await page.goto(hostedUrl);
    await expect(page).toHaveTitle('Runtime record snapshot fixture');
    await expect(page.getByRole('heading', { name: 'Runtime records', exact: true })).toBeVisible();
    await expect(page.locator('vite-error-overlay')).toHaveCount(0);
    await expect(page.getByRole('link', { name: 'System Index', exact: true })).toBeVisible();
    await page.getByRole('button', { name: 'Runtime Bravo client — 42' }).click();
    await page.getByLabel('Sort field').selectOption('amount');
    await page.getByLabel('Sort direction').selectOption('descending');
    await selectColourMode(page, 'Dark');
    await expectRecords(page, ['Runtime Bravo client — 42', 'Runtime Alpha client — 12', 'Runtime Charlie client — 7']);
    const exportButton = page.getByRole('button', { name: 'Export interactive snapshot', exact: true });
    await expect(exportButton).toHaveAccessibleDescription('Sensitive Contains fixture client records.');
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    const downloading = page.waitForEvent('download');
    await exportButton.focus();
    await page.keyboard.press('Enter');
    const download = await downloading;
    expect(download.suggestedFilename()).toMatch(/\.html$/);
    const downloadFolder = join(disposable, 'download.html');
    await download.saveAs(downloadFolder);
    expect(await download.failure()).toBeNull();
    expect(hostedErrors).toEqual([]);
    const html = await readFile(downloadFolder, 'utf8');
    const templates = (await readdir(join(disposable, 'dist/assets'))).filter((name) => name.startsWith('local-web-interactive-export-'));
    expect(templates).toHaveLength(1);
    const template = await readFile(join(disposable, 'dist/assets', templates[0]), 'utf8');
    for (const record of runtimeCapture().snapshotData.records) expect(template).not.toContain(record.label);

    const offline = await browser.newContext({ offline: true, serviceWorkers: 'block', viewport: { width: 1280, height: 900 } });
    const requests: string[] = [];
    const errors: string[] = [];
    offline.on('request', (request) => { if (/^(https?|wss?):/.test(request.url())) requests.push(request.url()); });
    await offline.addInitScript(() => {
      const attempts: string[] = [];
      Object.assign(window, { networkAttempts: attempts });
      for (const name of ['fetch', 'WebSocket', 'EventSource'] as const) {
        const original = window[name];
        Object.defineProperty(window, name, { value: new Proxy(original, {
          apply(target, receiver, args) { attempts.push(name); return Reflect.apply(target, receiver, args); },
          construct(target, args) { attempts.push(name); return Reflect.construct(target, args); },
        }) });
      }
      const open = XMLHttpRequest.prototype.open;
      XMLHttpRequest.prototype.open = new Proxy(open, {
        apply(target, receiver, args) { attempts.push('XMLHttpRequest'); return Reflect.apply(target, receiver, args); },
      });
      const beacon = navigator.sendBeacon;
      navigator.sendBeacon = function (...args) { attempts.push('sendBeacon'); return Reflect.apply(beacon, this, args); };
      window.addEventListener('securitypolicyviolation', (event) => {
        if (/^(https?|wss?):/.test(event.blockedURI)) attempts.push(`CSP: ${event.blockedURI}`);
      });
    });
    try {
      const file = await offline.newPage();
      file.on('websocket', (socket) => requests.push(socket.url()));
      file.on('pageerror', (error) => errors.push(error.message));
      file.on('console', (message) => {
        // Chromium documents that frame-ancestors cannot be enforced from a meta CSP.
        if (['error', 'warning'].includes(message.type()) && !message.text().includes("'frame-ancestors' is ignored when delivered via a <meta> element")) errors.push(message.text());
      });
      await file.goto(pathToFileURL(downloadFolder).href);
      expect(new URL(file.url()).protocol).toBe('file:');
      await expect(file).toHaveTitle('Offline snapshot');
      await expect(file.locator('vite-error-overlay')).toHaveCount(0);
      await expect(file.getByRole('heading', { name: 'Runtime records', exact: true })).toBeVisible();
      await expect(file.getByLabel('Calculated total')).toHaveText('61');
      await expect(file.getByRole('button', { name: 'Runtime Bravo client — 42' })).toHaveAttribute('aria-pressed', 'true');
      await expect(file.getByLabel('Sort field')).toHaveValue('amount');
      await expect(file.getByLabel('Sort direction')).toHaveValue('descending');
      await expect(file.locator('html')).toHaveAttribute('data-lwp-colour-mode', 'dark');
      await expectRecords(file, ['Runtime Bravo client — 42', 'Runtime Alpha client — 12', 'Runtime Charlie client — 7']);
      await expect(file.getByRole('complementary', { name: 'Offline snapshot details' })).toContainText('Sensitive — Contains fixture client records.');
      await expect(file.getByRole('button', { name: /export/i })).toHaveCount(0);
      await expect(file.getByRole('link', { name: /System Index|All apps/ })).toHaveCount(0);
      await file.getByRole('button', { name: 'Runtime Charlie client — 7' }).focus();
      await file.keyboard.press('Enter');
      await expect(file.getByRole('button', { name: 'Runtime Charlie client — 7' })).toHaveAttribute('aria-pressed', 'true');
      await file.getByLabel('Sort direction').selectOption('ascending');
      await expectRecords(file, ['Runtime Charlie client — 7', 'Runtime Alpha client — 12', 'Runtime Bravo client — 42']);
      await file.getByLabel('Sort field').selectOption('label');
      await expectRecords(file, ['Runtime Alpha client — 12', 'Runtime Bravo client — 42', 'Runtime Charlie client — 7']);
      await file.getByLabel('Sort field').selectOption('amount');
      await expect(file.getByLabel('Calculated total')).toHaveText('61');
      for (const width of [1280, 390, 320]) {
        await file.setViewportSize({ width, height: 900 });
        for (const mode of ['Light', 'Dark'] as const) {
          await selectColourMode(file, mode);
          expect((await new AxeBuilder({ page: file }).analyze()).violations).toEqual([]);
          expect(await file.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
          await file.screenshot({ path: testInfo.outputPath(`offline-${width}-${mode.toLowerCase()}.png`), fullPage: true });
        }
      }
      await file.getByRole('button', { name: 'System colour mode', exact: true }).click();
      await expect(file.getByRole('button', { name: 'System colour mode', exact: true })).toHaveAttribute('aria-pressed', 'true');
      expect(requests).toEqual([]);
      expect(await file.evaluate(() => (window as unknown as { networkAttempts: string[] }).networkAttempts)).toEqual([]);
      await file.reload();
      await expect(file.getByRole('button', { name: 'Runtime Bravo client — 42' })).toHaveAttribute('aria-pressed', 'true');
      await expect(file.getByLabel('Sort direction')).toHaveValue('descending');
      await expect(file.getByLabel('Sort field')).toHaveValue('amount');
      await expect(file.locator('html')).toHaveAttribute('data-lwp-colour-mode', 'dark');
      expect(requests).toEqual([]);
      expect(await file.evaluate(() => (window as unknown as { networkAttempts: string[] }).networkAttempts)).toEqual([]);
      expect(errors).toEqual([]);

      // Mutating only the non-executable envelope leaves the runtime hash intact:
      // the offline call of the same app decoder must reject broken relationships.
      for (const field of ['selection', 'total']) {
        const malformed = html.replace(/(<template data-local-web-interactive-export-payload>)([^<]+)(<\/template>)/, (_, start, encoded, end) => {
          const envelope = JSON.parse(Buffer.from(encoded, 'base64').toString('utf8'));
          if (field === 'selection') envelope.viewState.selectedRecordId = 'absent';
          else envelope.snapshotData.calculatedTotal = 60;
          return start + Buffer.from(JSON.stringify(envelope)).toString('base64') + end;
        });
        expect(malformed).not.toBe(html);
        const invalidPath = join(disposable, `invalid-${field}.html`);
        await writeFile(invalidPath, malformed);
        const rejected = await offline.newPage();
        const rejection = rejected.waitForEvent('pageerror');
        await rejected.goto(pathToFileURL(invalidPath).href);
        expect((await rejection).message).toBe('interactive export snapshot is invalid');
        await expect(rejected.getByRole('heading', { name: 'Runtime records', exact: true })).toHaveCount(0);
        await rejected.close();
      }
      expect(requests).toEqual([]);
      console.log(JSON.stringify({ hostedUrl, fileUrl: file.url(), artifactBytes: Buffer.byteLength(html), automaticRequests: requests, networkApiAttempts: await file.evaluate(() => (window as unknown as { networkAttempts: string[] }).networkAttempts) }));
      console.log('Evidence: file:// download restored 3 runtime records, total 61, selection bravo and amount/descending; local interactions and 6 responsive/theme axe checks passed; HTTP/HTTPS/WS requests=0; network API/CSP attempts=0; invalid selection/total rejected offline.');
    } finally {
      await offline.close();
    }
  });
});
