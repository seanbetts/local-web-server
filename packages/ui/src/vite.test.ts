import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';

import { afterEach, describe, expect, it, vi } from 'vitest';

import { deriveAccessibleAccent } from './colour.js';
import { COLOUR_MODE_BOOTSTRAP_SCRIPT } from './colourMode.js';
import { localWebApp } from './vite.js';
import * as publicVite from './vite.js';
import { readInteractiveExportTemplateDescriptor } from './interactiveExportDocument.js';
import { build, createServer } from 'vite';

type Handler = (
  request: { url?: string },
  response: {
    end: (body?: string) => void;
    setHeader: (name: string, value: string) => void;
  },
  next: () => void,
) => void;

const roots: string[] = [];

function interactiveFixture() {
  const root = temporaryRoot();
  writeFileSync(join(root, 'package.json'), JSON.stringify({ version: '1.2.3', type: 'module' }));
  writeFileSync(join(root, 'local-web.json'), JSON.stringify({ id: 'records' }));
  writeFileSync(join(root, 'contract.ts'), `export const contract = { id: 'record-list', version: 1,
    sensitivity: {classification: 'private', notice: 'Private records.'}, decodeSnapshot(value) { return value; } };`);
  writeFileSync(join(root, 'offline.tsx'), `import {contract} from './contract'; document.body.dataset.contract = contract.id;`);
  execFileSync('git', ['-C', root, 'init', '--quiet']);
  execFileSync('git', ['-C', root, '-c', 'core.hooksPath=/dev/null', '-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '--quiet', '--allow-empty', '-m', 'fixture']);
  return root;
}

describe('interactive export Vite integration', () => {
  it('fails closed on a deleted dev dependency and re-enables after restoration', async () => {
    const root = interactiveFixture();
    writeFileSync(join(root,'index.html'), '<html><head></head><body></body></html>');
    writeFileSync(join(root,'offline-only.ts'), 'export const label="Before deletion";');
    writeFileSync(join(root,'offline.tsx'), 'import {contract} from "./contract"; import {label} from "./offline-only"; document.body.textContent=contract.id+label;');
    const server = await createServer({configFile:false,root,base:'/records/',logLevel:'silent',
      plugins:[publicVite.localWebInteractiveExport({appId:'records',contract:'./contract.ts',entry:'./offline.tsx'})],
      server:{host:'127.0.0.1',port:0}});
    try {
      await server.listen();
      const origin = server.resolvedUrls!.local[0];
      const descriptor = async () => readInteractiveExportTemplateDescriptor(new DOMParser().parseFromString(await (await fetch(origin)).text(),'text/html'));
      const before = await descriptor();
      expect((await fetch(new URL(before.templateUrl, origin))).status).toBe(200);
      rmSync(join(root,'offline-only.ts'));
      await vi.waitFor(async () => expect((await fetch(origin)).status).toBeGreaterThanOrEqual(400), {timeout:4000});
      expect((await fetch(new URL(before.templateUrl, origin))).status).toBeGreaterThanOrEqual(400);
      writeFileSync(join(root,'offline-only.ts'), 'export const label="Restored dependency";');
      await vi.waitFor(async () => expect((await descriptor()).templateId).not.toBe(before.templateId), {timeout:4000});
      const after = await descriptor();
      const response = await fetch(new URL(after.templateUrl, origin));
      expect(response.status).toBe(200);
      const html = await response.text();
      expect(html).toContain('Restored dependency');
      const metadata = new DOMParser().parseFromString(html,'text/html').querySelector('meta[name="local-web-interactive-export-template"]')!;
      const {templateUrl: _url,...identity} = after;
      expect(JSON.parse(atob(metadata.getAttribute('content')!))).toEqual(identity);
    } finally { await server.close(); }
  }, 10000);

  it('fails build-watch on dependency deletion and publishes a coherent restored build', async () => {
    const root = interactiveFixture();
    writeFileSync(join(root,'index.html'), '<html><head></head><body><script type="module" src="/hosted.ts"></script></body></html>');
    writeFileSync(join(root,'hosted.ts'), 'import {contract} from "./contract"; import descriptor from "virtual:local-web-interactive-export"; document.body.textContent=JSON.stringify({contract,descriptor});');
    writeFileSync(join(root,'offline-only.ts'), 'export const label="Before deletion";');
    writeFileSync(join(root,'offline.tsx'), 'import {contract} from "./contract"; import {label} from "./offline-only"; document.body.textContent=contract.id+label;');
    const plugin = publicVite.localWebInteractiveExport({appId:'records',contract:'./contract.ts',entry:'./offline.tsx'});
    const watcher = await build({configFile:false,root,base:'/records/',logLevel:'silent',plugins:[plugin],build:{watch:{}}});
    if (Array.isArray(watcher) || !('close' in watcher)) throw new Error('expected watcher');
    const errors: unknown[] = [];
    watcher.on('event', (event) => {if (event.code === 'ERROR') errors.push(event.error);});
    try {
      const descriptor = () => readInteractiveExportTemplateDescriptor(new DOMParser().parseFromString(readFileSync(join(root,'dist/index.html'),'utf8'),'text/html'));
      await vi.waitFor(() => expect(descriptor().appId).toBe('records'), {timeout:4000});
      const before = descriptor();
      rmSync(join(root,'offline-only.ts'));
      await vi.waitFor(() => expect(errors.length).toBeGreaterThan(0), {timeout:4000});
      const transform = plugin.transformIndexHtml as {handler:(html:string)=>unknown};
      expect(() => transform.handler('<html></html>')).toThrow(/^interactive export packaging failed$/);
      writeFileSync(join(root,'offline-only.ts'), 'export const label="Restored dependency";');
      await vi.waitFor(() => expect(descriptor().templateId).not.toBe(before.templateId), {timeout:4000});
      const after = descriptor();
      expect(readFileSync(join(root,'dist',after.templateUrl.slice('/records/'.length)),'utf8')).toContain('Restored dependency');
      const index = new DOMParser().parseFromString(readFileSync(join(root,'dist/index.html'),'utf8'),'text/html');
      const hosted = readFileSync(join(root,'dist',index.querySelector('script[src]')!.getAttribute('src')!.slice('/records/'.length)),'utf8');
      expect(hosted).toContain(after.templateId);
      expect(hosted).toContain(after.compatibilityId);
    } finally { await watcher.close(); }
  }, 10000);
  it('refreshes dev descriptor and served bytes when an offline-only dependency changes', async () => {
    const root = interactiveFixture();
    writeFileSync(join(root, 'index.html'), '<html><head></head><body></body></html>');
    writeFileSync(join(root, 'offline-only.ts'), 'export const label="Before edit";');
    writeFileSync(join(root, 'offline.tsx'), `import {contract} from './contract'; import {label} from './offline-only'; document.body.textContent=contract.id+label;`);
    const server = await createServer({configFile:false,root,base:'/records/',logLevel:'silent',
      plugins:[publicVite.localWebInteractiveExport({appId:'records',contract:'./contract.ts',entry:'./offline.tsx'})],
      server:{host:'127.0.0.1',port:0}});
    try {
      await server.listen();
      const origin = server.resolvedUrls!.local[0];
      const descriptor = async () => readInteractiveExportTemplateDescriptor(new DOMParser().parseFromString(await (await fetch(origin)).text(), 'text/html'));
      const first = await descriptor();
      const before = await (await fetch(new URL(first.templateUrl, origin))).text();
      expect(before).toContain('Before edit');
      writeFileSync(join(root, 'offline-only.ts'), 'export const label="After edit";');
      await vi.waitFor(async () => expect((await descriptor()).templateId).not.toBe(first.templateId), {timeout:4000});
      const second = await descriptor();
      const after = await (await fetch(new URL(second.templateUrl, origin))).text();
      expect(after).toContain('After edit');
      expect(after).not.toContain('Before edit');
      expect(second.compatibilityId).not.toBe(first.compatibilityId);
      expect(second.payloadContractId).toBe(first.payloadContractId);
      const meta = new DOMParser().parseFromString(after, 'text/html').querySelector('meta[name="local-web-interactive-export-template"]')!;
      const {templateUrl: _url, ...identity} = second;
      expect(JSON.parse(atob(meta.getAttribute('content')!))).toEqual(identity);
      expect(await descriptor()).toEqual(second);
    } finally { await server.close(); }
  }, 10000);

  it('refreshes build-watch virtual descriptor and emitted asset after an offline-only dependency edit', async () => {
    const root = interactiveFixture();
    writeFileSync(join(root, 'index.html'), '<html><head></head><body><script type="module" src="/hosted.ts"></script></body></html>');
    writeFileSync(join(root, 'hosted.ts'), `import {contract} from './contract'; import descriptor from 'virtual:local-web-interactive-export'; document.body.textContent=JSON.stringify({id:contract.id,descriptor});`);
    writeFileSync(join(root, 'offline-only.ts'), 'export const label="Before edit";');
    writeFileSync(join(root, 'offline.tsx'), `import {contract} from './contract'; import {label} from './offline-only'; document.body.textContent=contract.id+label;`);
    const watcher = await build({configFile:false,root,base:'/records/',logLevel:'silent',
      plugins:[publicVite.localWebInteractiveExport({appId:'records',contract:'./contract.ts',entry:'./offline.tsx'})],build:{watch:{}}});
    if (Array.isArray(watcher) || !('close' in watcher)) throw new Error('expected build watcher');
    try {
      const descriptor = () => readInteractiveExportTemplateDescriptor(new DOMParser().parseFromString(readFileSync(join(root, 'dist/index.html'),'utf8'), 'text/html'));
      await vi.waitFor(() => expect(descriptor().appId).toBe('records'), {timeout:4000});
      const first = descriptor();
      writeFileSync(join(root, 'offline-only.ts'), 'export const label="After edit";');
      await vi.waitFor(() => expect(descriptor().templateId).not.toBe(first.templateId), {timeout:4000});
      const second = descriptor();
      const after = readFileSync(join(root,'dist',second.templateUrl.slice('/records/'.length)),'utf8');
      expect(after).toContain('After edit');
      expect(second.compatibilityId).not.toBe(first.compatibilityId);
      const index = new DOMParser().parseFromString(readFileSync(join(root,'dist/index.html'),'utf8'), 'text/html');
      const hosted = readFileSync(join(root,'dist',index.querySelector('script[src]')!.getAttribute('src')!.slice('/records/'.length)),'utf8');
      expect(hosted).toContain(second.templateId);
      expect(hosted).toContain(second.compatibilityId);
      expect(descriptor()).toEqual(second);
    } finally { await watcher.close(); }
  }, 10000);
  it('makes the virtual descriptor type available from a dist-only published package', async () => {
    const root = temporaryRoot();
    const compilerPath = '../../../node_modules/typescript/bin/tsc';
    const configPath = '../vite.config.ts';
    const tsconfigPath = '../tsconfig.json';
    const packageDist = join(root, 'package/dist');
    const modulesPath = '../../../node_modules';
    // Keep the caller's prepared production package intact for downstream checks.
    await build({
      configFile: new URL(configPath, import.meta.url).pathname,
      logLevel: 'silent',
      build: { outDir: packageDist },
    });
    execFileSync(process.execPath, [
      new URL(compilerPath, import.meta.url).pathname,
      '--project', new URL(tsconfigPath, import.meta.url).pathname,
      '--emitDeclarationOnly', '--outDir', packageDist,
    ]);
    symlinkSync(new URL(modulesPath, import.meta.url).pathname, join(root, 'node_modules'), 'dir');
    writeFileSync(join(root, 'consumer.ts'), `import type {} from './package/dist/vite.js';
      import descriptor from 'virtual:local-web-interactive-export';
      const id: string = descriptor.templateId;
      // @ts-expect-error descriptor identities are strings, not numbers
      const wrong: number = descriptor.templateId;
      void id; void wrong;`);
    const result = spawnSync(process.execPath, [
      new URL(compilerPath, import.meta.url).pathname,
      '--noEmit', '--moduleResolution', 'bundler', '--module', 'esnext', '--target', 'es2022',
      '--jsx', 'react-jsx', '--strict', '--skipLibCheck', join(root, 'consumer.ts'),
    ], {encoding: 'utf8'});
    expect(result.status, result.stdout + result.stderr).toBe(0);
  }, 15000);
  it('packages through a real hosted build with the same virtual and meta descriptor', async () => {
    const root = interactiveFixture();
    writeFileSync(join(root, 'index.html'), '<html><head></head><body><script type="module" src="/hosted.ts"></script></body></html>');
    writeFileSync(join(root, 'hosted.ts'), `import {contract} from './contract'; import descriptor from 'virtual:local-web-interactive-export'; document.body.textContent=JSON.stringify({contractId:contract.id, descriptor});`);
    const result = await build({configFile:false, root, base:'/records/', logLevel:'silent',
      plugins:[publicVite.localWebInteractiveExport({appId:'records',contract:'./contract.ts',entry:'./offline.tsx'})], build:{write:false}});
    if (Array.isArray(result) || !('output' in result)) throw new Error('expected one hosted output');
    const html = result.output.find((item) => item.type === 'asset' && item.fileName === 'index.html');
    if (!html || html.type !== 'asset') throw new Error('missing hosted HTML');
    const descriptor = readInteractiveExportTemplateDescriptor(new DOMParser().parseFromString(String(html.source), 'text/html'));
    const template = result.output.find((item) => item.fileName === descriptor.templateUrl.slice('/records/'.length));
    expect(template?.type).toBe('asset');
    const hosted = result.output.find((item) => item.type === 'chunk');
    if (!hosted || hosted.type !== 'chunk') throw new Error('missing hosted executable');
    expect(hosted.code).toContain(descriptor.templateId);
    expect(hosted.code).toContain(descriptor.compatibilityId);
    expect(result.output.some((item) => item.fileName.endsWith('.map'))).toBe(false);
  });

  it('rejects an existing descriptor rather than creating ambiguous hosted metadata', async () => {
    const root = interactiveFixture();
    const plugin = publicVite.localWebInteractiveExport({appId:'records',contract:'./contract.ts',entry:'./offline.tsx'});
    await resolvePlugin(plugin, root, '/records/');
    const transform = plugin.transformIndexHtml as {handler: (html:string) => unknown};
    expect(() => transform.handler('<meta name="local-web-interactive-export-descriptor" content="stale">')).toThrow(/^interactive export packaging failed$/);
  });
  it('uses identical identities and HTML for hosted descriptor, exact development route and emitted asset', async () => {
    const root = interactiveFixture();
    const plugin = publicVite.localWebInteractiveExport({ appId: 'records', contract: './contract.ts', entry: './offline.tsx' });
    await resolvePlugin(plugin, root, '/records/');
    const { result } = await transformTags(plugin) as { result: Array<{ tag: string; attrs: Record<string, string> }> };
    expect(result).toHaveLength(1);
    const tag = result[0];
    expect(tag.tag).toBe('meta');
    expect(tag.attrs.name).toBe('local-web-interactive-export-descriptor');
    const source = new DOMParser().parseFromString(`<meta name="${tag.attrs.name}" content="${tag.attrs.content}">`, 'text/html');
    const descriptor = readInteractiveExportTemplateDescriptor(source);
    expect(descriptor.templateUrl).toBe(`/records/assets/local-web-interactive-export-${descriptor.templateId}.html`);
    expect(descriptor.appVersion).toBe('1.2.3');
    expect(descriptor.sourceRevision).toBe(execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], {encoding: 'utf8'}).trim());
    let handler: Handler | undefined;
    await plugin.configureServer?.({ watcher: {add: () => undefined}, middlewares: { use: (registered: Handler) => { handler = registered; } } } as never);
    const invoke = (url: string) => {
      let body: string | undefined;
      let next = false;
      const headers = new Map<string, string>();
      handler?.({url}, { end: (value) => { body = value; }, setHeader: (key, value) => { headers.set(key, value); } }, () => {next = true;});
      return {body, next, headers};
    };
    const response = invoke(descriptor.templateUrl);
    expect(response.next).toBe(false);
    expect(response.headers.get('Content-Type')).toBe('text/html; charset=utf-8');
    expect(invoke(`${descriptor.templateUrl}?x=1`).next).toBe(true);
    expect(invoke('/assets/latest.html').next).toBe(true);
    expect(invoke(descriptor.templateUrl.replace('/records/', '/other/')).next).toBe(true);
    const emitted: Array<{ fileName: string; source: string }> = [];
    await plugin.generateBundle!.call({
      emitFile: (asset: { fileName: string; source: string }) => { emitted.push(asset); return '1'; },
      getModuleIds: () => [realpathSync(join(root, 'contract.ts'))][Symbol.iterator](),
    } as never, {} as never, {} as never);
    expect(emitted).toHaveLength(1);
    expect(emitted[0].fileName).toBe(descriptor.templateUrl.slice('/records/'.length));
    expect(emitted[0].source).toBe(response.body);
    const metadata = new DOMParser().parseFromString(response.body!, 'text/html').querySelector('meta[name="local-web-interactive-export-template"]')!;
    const {templateUrl: _url, ...identity} = descriptor;
    expect(JSON.parse(atob(metadata.getAttribute('content')!))).toEqual(identity);
  });

  it.each(['app disagreement', 'invalid contract', 'missing offline import', 'missing package', 'missing revision', 'external base'])('rejects %s safely', async (failure) => {
    const root = interactiveFixture();
    if (failure === 'app disagreement') writeFileSync(join(root, 'local-web.json'), '{"id":"other"}');
    if (failure === 'invalid contract') writeFileSync(join(root, 'contract.ts'), 'export const contract = {id:"bad", version:0};');
    if (failure === 'missing offline import') writeFileSync(join(root, 'offline.tsx'), 'document.body.textContent="No contract";');
    if (failure === 'missing package') rmSync(join(root, 'package.json'));
    if (failure === 'missing revision') rmSync(join(root, '.git'), {recursive: true});
    const plugin = publicVite.localWebInteractiveExport({appId:'records', contract:'./contract.ts', entry:'./offline.tsx'});
    await expect(resolvePlugin(plugin, root, failure === 'external base' ? 'https://example.test/' : '/records/')).rejects.toThrow(/^interactive export packaging failed$/);
  });

  it('rejects a hosted build that does not share the configured contract module', async () => {
    const root = interactiveFixture();
    const plugin = publicVite.localWebInteractiveExport({appId:'records', contract:'./contract.ts', entry:'./offline.tsx'});
    await resolvePlugin(plugin, root, '/records/');
    await expect(plugin.generateBundle!.call({getModuleIds: () => [][Symbol.iterator](), emitFile: () => '1'} as never, {} as never, {} as never)).rejects.toThrow(/^interactive export packaging failed$/);
  });
});
const ACCENT = '#8EA7C6';
const LIGHT_SURFACE = '#FFFFFF';
const DARK_SURFACE = '#19202B';

afterEach(() => {
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

const temporaryRoot = (): string => {
  const root = mkdtempSync(join(tmpdir(), 'lwp-vite-'));
  roots.push(root);
  return root;
};

const resolvePlugin = async (
  plugin: ReturnType<typeof localWebApp>,
  root: string,
  base = '/recipes/',
) => {
  await plugin.configResolved?.({ root, base } as never);
};

const transformTags = async (plugin: ReturnType<typeof localWebApp>) => {
  const transform = plugin.transformIndexHtml as {
    order: string;
    handler: (html: string) => Promise<unknown> | unknown;
  } | undefined;
  if (!transform || typeof transform === 'function') {
    throw new Error('expected an HTML transform hook');
  }
  return { transform, result: await transform.handler('<html><head></head></html>') };
};

describe('localWebApp', () => {
  it('rejects a noncanonical public base path', () => {
    for (const basePath of ['recipes/', '/recipes', '//recipes/', '/recipes/../']) {
      expect(() => localWebApp({ appId: 'recipes', basePath })).toThrow(
        'basePath must be / or a canonical path prefix ending in /',
      );
    }
  });

  it('rejects a noncanonical explicit accent', () => {
    for (const accent of ['#8ea7c6', '#8EA7C', '8EA7C6', '#8EA7C6FF']) {
      expect(() => localWebApp({ appId: 'recipes', basePath: '/', accent })).toThrow(
        'accent must be a canonical uppercase hex colour',
      );
    }
  });

  it('requires a matching canonical manifest accent when no explicit accent is provided', async () => {
    const missingManifestRoot = temporaryRoot();
    await expect(resolvePlugin(
      localWebApp({ appId: 'recipes', basePath: '/recipes/' }),
      missingManifestRoot,
    )).rejects.toThrow('could not read local-web.json');

    const mismatchedManifestRoot = temporaryRoot();
    writeFileSync(
      join(mismatchedManifestRoot, 'local-web.json'),
      JSON.stringify({ id: 'other-app', home: { accent: ACCENT } }),
    );
    await expect(resolvePlugin(
      localWebApp({ appId: 'recipes', basePath: '/recipes/' }),
      mismatchedManifestRoot,
    )).rejects.toThrow('local-web.json id must match appId');

    const lowercaseAccentRoot = temporaryRoot();
    writeFileSync(
      join(lowercaseAccentRoot, 'local-web.json'),
      JSON.stringify({ id: 'recipes', home: { accent: '#8ea7c6' } }),
    );
    await expect(resolvePlugin(
      localWebApp({ appId: 'recipes', basePath: '/recipes/' }),
      lowercaseAccentRoot,
    )).rejects.toThrow('local-web.json home.accent must be a canonical uppercase hex colour');
  });

  it('serves fallback theme content only from the reserved development route', () => {
    const root = temporaryRoot();
    const fallbackThemePath = join(root, 'theme.css');
    writeFileSync(fallbackThemePath, ':root { --lwp-example: yes; }\n');
    const plugin = localWebApp({
      appId: 'recipes',
      basePath: '/recipes/',
      accent: ACCENT,
      fallbackThemePath,
    });
    let handler: Handler | undefined;
    plugin.configureServer?.({
      middlewares: { use: (registered: Handler) => { handler = registered; } },
    } as never);
    expect(handler).toBeTypeOf('function');

    const invoke = (url: string) => {
      const headers = new Map<string, string>();
      let body: string | undefined;
      let next = false;
      handler?.(
        { url },
        {
          end: (value) => { body = value; },
          setHeader: (name, value) => headers.set(name, value),
        },
        () => { next = true; },
      );
      return { body, headers, next };
    };

    const theme = invoke('/_local-web/platform/theme.css');
    expect(theme.body).toBe(':root { --lwp-example: yes; }\n');
    expect(theme.headers.get('Content-Type')).toBe('text/css; charset=utf-8');
    expect(theme.headers.get('Cache-Control')).toBe('no-store');
    expect(theme.next).toBe(false);
    expect(invoke('/recipes/_local-web/platform/theme.css').next).toBe(true);
    expect(invoke('/_local-web/platform/theme.css?x=1').next).toBe(true);
    expect(invoke('/anything-else').next).toBe(true);
  });

  it('serves only dedicated cosmetic tokens from the default fallback', () => {
    const plugin = localWebApp({ appId: 'recipes', basePath: '/', accent: ACCENT });
    let handler: Handler | undefined;
    plugin.configureServer?.({
      middlewares: { use: (registered: Handler) => { handler = registered; } },
    } as never);
    let body: string | undefined;
    handler?.(
      { url: '/_local-web/platform/theme.css' },
      { end: (value) => { body = value; }, setHeader: () => undefined },
      () => undefined,
    );

    expect(body).toContain('--lwp-colour-canvas');
    expect(body).toContain(':root[data-lwp-colour-mode="dark"]');
    expect(body).not.toContain('.lwp-app-shell');
    expect(body).not.toContain('.lwp-button');
    expect(body).not.toContain('@import');
  });

  it('externalizes the colour-mode bootstrap and app accent stylesheet', async () => {
    const root = temporaryRoot();
    writeFileSync(
      join(root, 'local-web.json'),
      JSON.stringify({ id: 'recipes', home: { accent: ACCENT } }),
    );
    const plugin = localWebApp({ appId: 'recipes', basePath: '/recipes/' });
    await resolvePlugin(plugin, root);
    const { transform, result } = await transformTags(plugin);

    expect(transform.order).toBe('post');
    expect(result).toEqual(expect.arrayContaining([
      expect.objectContaining({
        tag: 'script',
        injectTo: 'head-prepend',
        attrs: {
          src: expect.stringMatching(/^\/recipes\/assets\/local-web-colour-mode-[0-9a-f]{12}\.js$/),
        },
      }),
      expect.objectContaining({
        tag: 'link',
        attrs: {
          rel: 'stylesheet',
          href: expect.stringMatching(/^\/recipes\/assets\/local-web-app-accent-[0-9a-f]{12}\.css$/),
        },
      }),
    ]));
    expect(JSON.stringify(result)).not.toContain('children');
  });

  it('uses the final resolved base path for external asset URLs', async () => {
    const plugin = localWebApp({ appId: 'system-index', basePath: '/', accent: ACCENT });
    await resolvePlugin(plugin, temporaryRoot(), '/_local-web/platform/index/');
    const { result } = await transformTags(plugin) as {
      result: Array<{ tag: string; attrs?: Record<string, string> }>;
    };

    expect(result).toEqual(expect.arrayContaining([
      expect.objectContaining({
        tag: 'script',
        attrs: {
          src: expect.stringMatching(/^\/_local-web\/platform\/index\/assets\/local-web-colour-mode-[0-9a-f]{12}\.js$/),
        },
      }),
      expect.objectContaining({
        tag: 'link',
        attrs: {
          href: expect.stringMatching(/^\/_local-web\/platform\/index\/assets\/local-web-app-accent-[0-9a-f]{12}\.css$/),
          rel: 'stylesheet',
        },
      }),
    ]));
  });

  it('derives and serves the same app accent and bootstrap asset bodies it emits', async () => {
    const root = temporaryRoot();
    writeFileSync(
      join(root, 'local-web.json'),
      JSON.stringify({ id: 'recipes', home: { accent: ACCENT } }),
    );
    const plugin = localWebApp({ appId: 'recipes', basePath: '/recipes/' });
    await resolvePlugin(plugin, root);
    const { result } = await transformTags(plugin) as {
      result: Array<{ tag: string; attrs?: Record<string, string> }>;
    };
    const scriptUrl = result.find((tag) => tag.tag === 'script')?.attrs?.src;
    const accentUrl = result.find((tag) => tag.tag === 'link')?.attrs?.href;
    if (!scriptUrl || !accentUrl) throw new Error('expected external asset tags');

    const expectedAccent = `:where([data-lwp-app-id="recipes"]) {\n  --lwp-app-accent-light: ${deriveAccessibleAccent(ACCENT, LIGHT_SURFACE)};\n  --lwp-app-accent-dark: ${deriveAccessibleAccent(ACCENT, DARK_SURFACE)};\n}\n`;
    let handler: Handler | undefined;
    plugin.configureServer?.({
      middlewares: { use: (registered: Handler) => { handler = registered; } },
    } as never);
    expect(handler).toBeTypeOf('function');

    const invoke = (url: string) => {
      const headers = new Map<string, string>();
      let body: string | undefined;
      handler?.(
        { url },
        {
          end: (value) => { body = value; },
          setHeader: (name, value) => headers.set(name, value),
        },
        () => undefined,
      );
      return { body, headers };
    };

    const bootstrap = invoke(scriptUrl);
    const accent = invoke(accentUrl);
    expect(bootstrap.body).toBe(COLOUR_MODE_BOOTSTRAP_SCRIPT);
    expect(bootstrap.headers.get('Content-Type')).toBe('application/javascript; charset=utf-8');
    expect(bootstrap.headers.get('Cache-Control')).toBe('no-store');
    expect(accent.body).toBe(expectedAccent);
    expect(accent.headers.get('Content-Type')).toBe('text/css; charset=utf-8');
    expect(accent.headers.get('Cache-Control')).toBe('no-store');

    if (typeof plugin.generateBundle !== 'function') {
      throw new Error('expected a generateBundle hook');
    }
    const emitted: Array<{ fileName: string; source: string }> = [];
    await plugin.generateBundle.call({
      emitFile(asset: { fileName: string; source: string }) {
        emitted.push(asset);
        return String(emitted.length);
      },
    } as never, {} as never, {} as never);
    expect(emitted).toEqual(expect.arrayContaining([
      expect.objectContaining({
        fileName: scriptUrl.slice('/recipes/'.length),
        source: COLOUR_MODE_BOOTSTRAP_SCRIPT,
      }),
      expect.objectContaining({
        fileName: accentUrl.slice('/recipes/'.length),
        source: expectedAccent,
      }),
    ]));
  });

  it('applies the hosted path prefix to independent development', () => {
    const plugin = localWebApp({ appId: 'recipes', basePath: '/recipes/', accent: ACCENT });
    const configure = plugin.config;
    if (typeof configure !== 'function') throw new Error('expected a config hook');

    expect(configure({}, { command: 'serve', mode: 'development' })).toEqual({
      base: '/recipes/',
    });
  });
});
