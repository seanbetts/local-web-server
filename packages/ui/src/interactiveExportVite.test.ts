import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { runInNewContext } from 'node:vm';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as vite from 'vite';
import * as publicVite from './vite.js';
import { INTERACTIVE_EXPORT_PAYLOAD_MARKER } from './interactiveExportDocument.js';

vi.mock('vite', async (original) => {
  const actual = await original<typeof import('vite')>();
  return { ...actual, build: vi.fn(actual.build) };
});

const roots: string[] = [];
const packagerModule = './interactiveExportVite.js';
afterEach(() => {
  vi.mocked(vite.build).mockClear();
  vi.unstubAllEnvs();
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

function fixture(source?: string) {
  const root = mkdtempSync(join(tmpdir(), 'lwp-interactive-build-'));
  roots.push(root);
  writeFileSync(join(root, 'offline.tsx'), source ?? `
    /** @jsxRuntime classic */
    /** @jsx h */
    function h(tag, props, ...children) { return {tag, props, children}; }
    import './offline.css';
    document.getElementById('root').textContent = JSON.stringify(<p>Frozen records</p>);
  `);
  writeFileSync(join(root, 'offline.css'), '.records { color: red; background: url("data:image/png;base64,AA=="); }');
  return { root, entry: join(root, 'offline.tsx'), appId: 'records', appVersion: '1.2.3',
    sourceRevision: 'abc1234', contract: { id: 'record-list', version: 1 } };
}

// Mutations caught: absent public integration, non-classic/missing runtime, wrong hash/marker,
// dependency leakage, identity drift, or unsafe build output accepted at the packaging boundary.
describe('interactive export packager', () => {
  it('exports the supported Vite integration', () => {
    expect(publicVite.localWebInteractiveExport).toBeTypeOf('function');
  });

  it('packages executable TSX, inline CSS, exact CSP and one marker deterministically', async () => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    const options = fixture();
    const first = await buildInteractiveExportTemplate(options);
    expect(await buildInteractiveExportTemplate(options)).toEqual(first);
    expect(first.fileName).toMatch(/^assets\/local-web-interactive-export-[a-f0-9]{64}\.html$/);
    expect(first.html.split(INTERACTIVE_EXPORT_PAYLOAD_MARKER)).toHaveLength(2);
    const parsed = new DOMParser().parseFromString(first.html, 'text/html');
    expect(parsed.querySelectorAll('script')).toHaveLength(1);
    const script = parsed.querySelector('script')!;
    expect(script.hasAttribute('src')).toBe(false);
    expect(script.hasAttribute('type')).toBe(false);
    const hash = createHash('sha256').update(script.textContent!).digest('base64');
    expect(parsed.querySelector('meta[http-equiv="Content-Security-Policy"]')!.getAttribute('content')).toBe(
      `default-src 'none'; connect-src 'none'; script-src 'sha256-${hash}'; script-src-attr 'none'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; worker-src blob:; child-src blob:; object-src 'none'; frame-src 'none'; manifest-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`,
    );
    const root = { textContent: '' };
    runInNewContext(script.textContent!, { document: { getElementById: () => root } });
    expect(JSON.parse(root.textContent).children).toEqual(['Frozen records']);
    expect(parsed.querySelector('style')!.textContent).toContain('--lwp-colour-canvas');
    expect(parsed.querySelector('style')!.textContent).toContain('data:image/png;base64,AA==');
    expect(first.html).not.toContain('sourceMappingURL');
    expect(first.html).not.toContain(options.root);
    expect(JSON.parse(atob(parsed.querySelector('meta[name="local-web-interactive-export-template"]')!.getAttribute('content')!))).toEqual(first.identity);
    const expectedCompatibility = createHash('sha256').update(JSON.stringify({
      appId: 'records', payloadContractId: 'records/record-list/v1',
      schema: 'local-web-interactive-export/v1', templateId: first.identity.templateId,
    })).digest('hex');
    expect(first.identity.compatibilityId).toBe(expectedCompatibility);
    expect((await buildInteractiveExportTemplate({ ...options, sourceRevision: 'def5678' })).identity.templateId).not.toBe(first.identity.templateId);
  });

  it.each([
    ['app id', { appId: 'Other App' }], ['contract id', { contract: { id: '../bad', version: 1 } }],
    ['contract version', { contract: { id: 'record-list', version: 0 } }],
    ['version', { appVersion: '' }], ['revision', { sourceRevision: '' }],
    ['missing entry', { entry: '/missing/private/entry.tsx' }],
  ])('rejects invalid %s with a safe error', async (_name, changes) => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    await expect(buildInteractiveExportTemplate({ ...fixture(), ...changes })).rejects.toThrowError(
      expect.objectContaining({ code: 'packaging-failed', message: 'interactive export packaging failed' }),
    );
  });

  it.each([
    ['external import', 'import "https://example.test/private.js";'],
    ['protocol-relative image', 'document.body.innerHTML = \'<img src="//example.test/pixel">\';'],
    ['absolute image', 'document.body.innerHTML = \'<img src="/private/pixel">\';'],
    ['source map reference', 'document.body.textContent = "sourceMappingURL=private.map";'],
  ])('rejects %s', async (_name, source) => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    await expect(buildInteractiveExportTemplate(fixture(source))).rejects.toThrow(/^interactive export packaging failed$/);
  });

  it('allows deliberate evidence hyperlinks', async () => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    const result = await buildInteractiveExportTemplate(fixture('document.body.innerHTML = \'<a href="https://example.test/evidence">Evidence</a>\';'));
    expect(result.html).toContain('https://example.test/evidence');
  });

  it.each([
    'document.body.textContent = JSON.stringify({src:"paper record",poster:"Alice"});',
    'document.body.innerHTML = \'<a href="https://example.test/url(foo)">Evidence</a>\';',
    'const a=document.createElement("a"); a.href="https://example.test/url(foo)"; document.body.append(a);',
    'document.body.textContent = "The CSS url(foo) syntax is documented here.";',
    'const contract={id:"record-list"}; window.record("img", {src:"paper record", poster:"Alice", contract:contract.id});',
  ])('allows inert content and evidence navigation: %s', async (source) => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    await expect(buildInteractiveExportTemplate(fixture(source))).resolves.toHaveProperty('identity');
  });

  it.each([
    'const link=document.createElement("link"); link.rel="stylesheet"; link.href="https://example.test/theme.css"; document.head.append(link);',
    'const link=document.createElement("link"); link.setAttribute("rel","stylesheet"); link.setAttribute("href","https://example.test/theme.css"); document.head.append(link);',
  ])('rejects constructed external stylesheets: %s', async (source) => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    await expect(buildInteractiveExportTemplate(fixture(source))).rejects.toThrow(/^interactive export packaging failed$/);
  });

  // Removing SVG consumer recognition, namespace construction, or href spelling
  // normalization must make these real builds accept an automatic dependency.
  it.each([
    ['HTML image href', 'document.body.innerHTML=\'<svg><image href="RESOURCE"/></svg>\';'],
    ['HTML image adjacent attribute', 'document.body.innerHTML=\'<svg><image data-label="safe"href="RESOURCE"/></svg>\';'],
    ['HTML image slash-separated attribute', 'document.body.innerHTML=\'<svg><image/href="RESOURCE"/></svg>\';'],
    ['HTML use xlink:href', 'document.body.insertAdjacentHTML("beforeend",\'<svg><use xlink:href="RESOURCE"/></svg>\');'],
    ['HTML filter image', 'document.body.innerHTML=\'<svg><filter><feImage href="RESOURCE"/></filter></svg>\';'],
    ['DOM image property', 'const el=document.createElementNS("http://www.w3.org/2000/svg","image"); el.href="RESOURCE"; document.body.append(el);'],
    ['DOM use animated href', 'const el=document.createElementNS("http://www.w3.org/2000/svg","use"); el.href.baseVal="RESOURCE"; document.body.append(el);'],
    ['DOM image setAttribute', 'const el=document.createElementNS("http://www.w3.org/2000/svg","image"); el.setAttribute("href","RESOURCE"); document.body.append(el);'],
    ['DOM use xlink attribute', 'const el=document.createElementNS("http://www.w3.org/2000/svg","use"); el.setAttribute("xlink:href","RESOURCE"); document.body.append(el);'],
    ['DOM filter namespaced href', 'const el=document.createElementNS("http://www.w3.org/2000/svg","feImage"); el.setAttributeNS("http://www.w3.org/1999/xlink","href","RESOURCE"); document.body.append(el);'],
    ['DOM image namespaced xlink:href', 'const el=document.createElementNS("http://www.w3.org/2000/svg","image"); el.setAttributeNS("http://www.w3.org/1999/xlink","xlink:href","RESOURCE"); document.body.append(el);'],
    ['DOM use null namespace', 'const el=document.createElementNS("http://www.w3.org/2000/svg","use"); el.setAttributeNS(null,"href","RESOURCE"); document.body.append(el);'],
    ['DOM image empty namespace', 'const el=document.createElementNS("http://www.w3.org/2000/svg","image"); el.setAttributeNS("","href","RESOURCE"); document.body.append(el);'],
    ['React JSX image', 'document.body.test=<svg><image href="RESOURCE"/></svg>;'],
    ['React JSX use xlinkHref', 'document.body.test=<svg><use xlinkHref="RESOURCE"/></svg>;'],
    ['React JSX filter image', 'document.body.test=<svg><filter><feImage href="RESOURCE"/></filter></svg>;'],
    ['compiled JSX image', 'import {jsx as make} from "react/jsx-runtime"; document.body.test=make("image",{href:"RESOURCE"});'],
    ['React createElement use', 'import {createElement as make} from "react"; document.body.test=make("use",{"xlink:href":"RESOURCE"});'],
  ])('rejects non-inline SVG resources and preserves safe references: %s', async (_name, source) => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    const options = fixture(source);
    const modulesPath = '../../../node_modules';
    symlinkSync(new URL(modulesPath, import.meta.url).pathname, join(options.root,'node_modules'),'dir');
    for (const resource of ['https://example.test/diagram.svg', '//example.test/diagram.svg', '/diagram.svg', 'diagram.svg']) {
      writeFileSync(options.entry, source.replace('RESOURCE', resource));
      await expect(buildInteractiveExportTemplate(options).then(() => undefined)).rejects.toThrow(/^interactive export packaging failed$/);
    }
    for (const resource of ['#symbol', 'data:image/svg+xml;base64,PHN2Zy8+', 'blob:offline-image']) {
      writeFileSync(options.entry, source.replace('RESOURCE', resource));
      await expect(buildInteractiveExportTemplate(options)).resolves.toHaveProperty('identity');
    }
  });

  describe.each([false, true])('SVG qualified names (minified: %s)', (minify) => {
    it.each([
      ['qualified SVG tag', 'const el=document.createElementNS("http://www.w3.org/2000/svg","svg:image"); el.href.baseVal="RESOURCE"; document.body.append(el);'],
      ['qualified XLink attribute', 'const el=document.createElementNS("http://www.w3.org/2000/svg","image"); el.setAttributeNS("http://www.w3.org/1999/xlink","other:href","RESOURCE"); document.body.append(el);'],
    ])('rejects external %s without rejecting safe references', async (_name, source) => {
      const { buildInteractiveExportTemplate } = await import(packagerModule);
      const actual = await vi.importActual<typeof import('vite')>('vite');
      const build = (resource: string) => {
        vi.mocked(vite.build).mockImplementationOnce((config) => actual.build({...config, build:{...config?.build, minify}}));
        return buildInteractiveExportTemplate(fixture(source.replace('RESOURCE', resource)));
      };
      await expect(build('https://example.test/diagram.svg').then(() => undefined)).rejects.toThrow(/^interactive export packaging failed$/);
      for (const resource of ['#symbol', 'data:image/svg+xml;base64,PHN2Zy8+', 'blob:offline-image']) {
        await expect(build(resource)).resolves.toHaveProperty('identity');
      }
    });
  });

  it.each([
    'const el=document.createElementNS("urn:inert","svg:image"); el.setAttribute("href","https://example.test/evidence"); document.body.append(el);',
    'const el=document.createElement("svg:image"); el.setAttribute("href","https://example.test/evidence"); document.body.append(el);',
    'const el=document.createElementNS("http://www.w3.org/2000/svg","image"); el.setAttributeNS("urn:inert","other:href","https://example.test/evidence"); document.body.append(el);',
    'document.body.innerHTML=\'<svg><image href="#safe" data-href="evidence label"/></svg>\';',
    'document.body.innerHTML=\'<svg><image href="#safe" other:href="evidence label" data-xlink:href="label" aria-data="label"/></svg>\';',
    'document.body.innerHTML=\'<img src="data:image/png;base64,AA==" data-src="evidence label" custom:src="label" data-poster="Alice" data-srcset="label"/>\';',
    'document.body.innerHTML=\'<svg><image href="#safe" data-label="example href=external"/></svg>\';',
    'document.body.innerHTML=\'<svg><svg:image href="https://example.test/evidence"/><image:label href="https://example.test/evidence"/></svg>\';',
  ])('preserves inert namespace and complete attribute names: %s', async (source) => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    await expect(buildInteractiveExportTemplate(fixture(source))).resolves.toHaveProperty('identity');
  });

  it.each([
    'document.body.innerHTML=\'<svg><a xlink:href="https://example.test/url(foo)">Evidence</a></svg>\';',
    'const el=document.createElementNS("http://www.w3.org/2000/svg","a"); el.href.baseVal="https://example.test/evidence"; el.setAttributeNS("http://www.w3.org/1999/xlink","href","https://example.test/evidence"); document.body.append(el);',
    'document.body.test=<svg><a href="https://example.test/evidence" xlinkHref="https://example.test/evidence">Evidence</a></svg>;',
    'window.record("image", {href:"paper record", xlinkHref:"Alice"}); window.record("use", {href:"paper record"});',
  ])('preserves SVG evidence links and inert tag-shaped content: %s', async (source) => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    const options = fixture(source);
    const modulesPath = '../../../node_modules';
    symlinkSync(new URL(modulesPath, import.meta.url).pathname, join(options.root,'node_modules'),'dir');
    await expect(buildInteractiveExportTemplate(options)).resolves.toHaveProperty('identity');
  });

  it.each([
    'import {jsx as make} from "react/jsx-runtime"; document.body.test=make("img",{src:"https://example.test/pixel"});',
    'import {createElement as make} from "react"; document.body.test=make("img",{src:"https://example.test/pixel"});',
    'document.body.test=<img src="https://example.test/pixel"/>;',
  ])('rejects actual React element resources after bundling: %s', async (source) => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    const options = fixture(source);
    const modulesPath = '../../../node_modules';
    symlinkSync(new URL(modulesPath, import.meta.url).pathname, join(options.root,'node_modules'),'dir');
    await expect(buildInteractiveExportTemplate(options)).rejects.toThrow(/^interactive export packaging failed$/);
    writeFileSync(options.entry, source.replace('https://example.test/pixel', 'data:image/png;base64,AA=='));
    await expect(buildInteractiveExportTemplate(options)).resolves.toHaveProperty('identity');
  });

  it.each([
    ['JSX resource property', '/** @jsxRuntime classic */ /** @jsx window.jsx */ document.body.test = <img src="//example.test/pixel"/>;'],
    ['assigned image source', 'document.createElement("img").src = "https://example.test/pixel";'],
    ['setAttribute source', 'document.createElement("img").setAttribute("src", "/pixel");'],
    ['relative image source', 'document.createElement("img").src="sibling.png";'],
    ['external stylesheet property', '/** @jsxRuntime classic */ /** @jsx window.jsx */ document.body.test = <link rel="stylesheet" href="https://example.test/theme.css"/>;'],
    ['mixed srcset resource', '/** @jsxRuntime classic */ /** @jsx window.jsx */ document.body.test = <img srcSet="data:image/png;base64,AA== 1x, https://example.test/pixel 2x"/>;'],
    ['dynamic external module', 'import(/* @vite-ignore */ "https://example.test/module.js");'],
    ['encoded image resource', 'document.body.innerHTML = \'<img src="&#104;ttps://example.test/pixel">\';'],
  ])('rejects automatic %s', async (_name, source) => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    await expect(buildInteractiveExportTemplate(fixture(source))).rejects.toThrow(/^interactive export packaging failed$/);
  });

  it.each(['url(https://example.test/font.woff)', 'url(//example.test/pixel)', 'url(/private/pixel)', 'url(sibling.png)', '@import "https://example.test/theme.css";'])('rejects CSS dependency %s', async (resource) => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    const options = fixture();
    writeFileSync(join(options.root, 'offline.css'), resource.startsWith('@') ? resource : `.image {background: ${resource};}`);
    await expect(buildInteractiveExportTemplate(options)).rejects.toThrow(/^interactive export packaging failed$/);
  });

  it('does not load app config, public files or environment and does not hash build paths', async () => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    const source = 'document.body.textContent = JSON.stringify(import.meta.env);';
    const options = fixture(source);
    writeFileSync(join(options.root, 'vite.config.ts'), 'throw new Error("App config must not execute");');
    writeFileSync(join(options.root, '.env'), 'VITE_PRIVATE_BUILD_VALUE=private-sentinel');
    vi.stubEnv('VITE_PRIVATE_PROCESS_VALUE', 'private-process-sentinel');
    const first = await buildInteractiveExportTemplate(options);
    const second = await buildInteractiveExportTemplate(fixture(source));
    expect(second.html).toBe(first.html);
    expect(first.html).not.toContain('private-sentinel');
    expect(first.html).not.toContain('private-process-sentinel');
  });

  it('keeps a script closing-tag string inside the hashed executable', async () => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    const result = await buildInteractiveExportTemplate(fixture('document.body.textContent = "</script><p>Still a string</p>";'));
    const parsed = new DOMParser().parseFromString(result.html, 'text/html');
    expect(parsed.querySelectorAll('script')).toHaveLength(1);
    expect(parsed.querySelector('p')).toBeNull();
    const body = {textContent: ''};
    runInNewContext(parsed.querySelector('script')!.textContent!, {document: {body}});
    expect(body.textContent).toBe('</script><p>Still a string</p>');
  });

  it('packages a real React offline reader and imported visual asset using the framework runtime', async () => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    const runtimePath = './interactiveExportEnvironment.tsx';
    const modelPath = './interactiveExportModel.ts';
    const stylesPath = './styles.css';
    const options = fixture(`
      import {mountInteractiveExport} from ${JSON.stringify(new URL(runtimePath, import.meta.url).pathname)};
      import {contract} from './contract';
      import pixel from './pixel.png';
      import ${JSON.stringify(new URL(stylesPath, import.meta.url).pathname)};
      mountInteractiveExport({contract, render: () => <p>Records<img src={pixel} alt="" /></p>});
    `);
    const modulesPath = '../../../node_modules';
    symlinkSync(new URL(modulesPath, import.meta.url).pathname, join(options.root, 'node_modules'), 'dir');
    writeFileSync(join(options.root, 'contract.ts'), `import {defineInteractiveExportContract} from ${JSON.stringify(new URL(modelPath, import.meta.url).pathname)};
      export const contract = defineInteractiveExportContract({id:'record-list', version:1,
      sensitivity:{classification:'private',notice:'Private records.'},decodeSnapshot(value){return value;}});`);
    writeFileSync(join(options.root, 'pixel.png'), Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=', 'base64'));
    const result = await buildInteractiveExportTemplate({...options, contractModule:join(options.root, 'contract.ts')});
    expect(result.html).toContain('data:image/png;base64,');
    expect(result.html).not.toContain(options.root);
    expect(result.html).not.toContain('sourceMappingURL');
    expect(new DOMParser().parseFromString(result.html, 'text/html').querySelectorAll('script')).toHaveLength(1);
  });

  it('packages an offline AppShell through the public UI entry with exactly one payload marker', async () => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    const publicPath = './index.ts';
    const publicEntry = JSON.stringify(new URL(publicPath, import.meta.url).pathname);
    const options = fixture(`
      import {AppShell, mountInteractiveExport} from ${publicEntry};
      import {contract} from './contract';
      mountInteractiveExport({contract, render: () =>
        <AppShell app={{id:'records', name:'Records', icon:'book'}}><p>Frozen records</p></AppShell>});
    `);
    const modulesPath = '../../../node_modules';
    symlinkSync(new URL(modulesPath, import.meta.url).pathname, join(options.root, 'node_modules'), 'dir');
    writeFileSync(join(options.root, 'contract.ts'), `import {defineInteractiveExportContract} from ${publicEntry};
      export const contract = defineInteractiveExportContract({id:'record-list', version:1,
      sensitivity:{classification:'private',notice:'Private records.'},decodeSnapshot(value){return value;}});`);

    const result = await buildInteractiveExportTemplate({...options, contractModule:join(options.root, 'contract.ts')});
    expect(INTERACTIVE_EXPORT_PAYLOAD_MARKER).toBe('<!--LOCAL_WEB_INTERACTIVE_EXPORT_PAYLOAD-->');
    expect(result.html.split(INTERACTIVE_EXPORT_PAYLOAD_MARKER)).toHaveLength(2);
    const scripts = new DOMParser().parseFromString(result.html, 'text/html').querySelectorAll('script');
    expect(scripts).toHaveLength(1);
    expect(scripts[0].textContent).not.toContain(INTERACTIVE_EXPORT_PAYLOAD_MARKER);
  });

  it('tracks nested stylesheet and CSS visual dependencies for invalidation', async () => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    const options = fixture();
    writeFileSync(join(options.root, 'offline.css'), '@import "./nested.css"; .records {background:url("./pixel.png")}');
    writeFileSync(join(options.root, 'nested.css'), '.records {color:blue}');
    writeFileSync(join(options.root, 'pixel.png'), new Uint8Array([1,2,3]));
    const result = await buildInteractiveExportTemplate(options);
    expect(result.dependencies).toEqual(expect.arrayContaining([
      realpathSync(join(options.root, 'nested.css')), realpathSync(join(options.root, 'pixel.png')),
    ]));
  });

  it.each(['successful', 'failing'])('coalesces a superseded %s rebuild and atomically publishes the latest valid state', async (superseded) => {
    const options = fixture();
    const root = options.root;
    writeFileSync(join(root,'package.json'), '{"version":"1.2.3","type":"module"}');
    writeFileSync(join(root,'local-web.json'), '{"id":"records"}');
    writeFileSync(join(root,'contract.ts'), 'export const contract={id:"record-list",version:1,sensitivity:{classification:"private",notice:"Private records."},decodeSnapshot(value){return value}};');
    writeFileSync(join(root,'label.ts'), 'export const label="Initial";');
    writeFileSync(options.entry, 'import {contract} from "./contract"; import {label} from "./label"; document.body.textContent=contract.id+label;');
    execFileSync('git', ['-C',root,'init','--quiet']);
    execFileSync('git', ['-C',root,'-c','core.hooksPath=/dev/null','-c','user.name=Test','-c','user.email=test@example.test','commit','--quiet','--allow-empty','-m','fixture']);
    const plugin = publicVite.localWebInteractiveExport({appId:'records',contract:'./contract.ts',entry:'./offline.tsx'});
    await plugin.configResolved!({root,base:'/records/'} as never);
    const descriptor = async () => {
      const transform = plugin.transformIndexHtml as {handler:(html:string)=>Array<{attrs:{content:string}}>};
      return JSON.parse(atob((await transform.handler('<html></html>'))[0].attrs.content));
    };
    const initial = await descriptor();
    const environment = {name:'client',moduleGraph:{getModuleById:()=>undefined},hot:{send:()=>undefined}};
    const update = () => plugin.hotUpdate!.call({environment} as never, {type:'update',file:join(root,'label.ts')} as never);
    const actual = await vi.importActual<typeof import('vite')>('vite');
    let release!: () => void;
    let started!: () => void;
    const gate = new Promise<void>((resolve) => {release=resolve;});
    const began = new Promise<void>((resolve) => {started=resolve;});
    vi.mocked(vite.build).mockImplementationOnce(async (config) => {
      let output;
      let error;
      try { output = await actual.build(config); } catch (failure) { error = failure; }
      started();
      await gate;
      if (error) throw error;
      return output!;
    });
    const before = vi.mocked(vite.build).mock.calls.length;
    writeFileSync(join(root,'label.ts'), superseded === 'failing' ? 'export const label=;' : 'export const label="Superseded";');
    const first = update();
    await began;
    writeFileSync(join(root,'label.ts'), 'export const label="Latest";');
    const second = update();
    expect(await descriptor()).toEqual(initial);
    release();
    await Promise.all([first, second]);
    expect(vi.mocked(vite.build).mock.calls.length-before).toBe(2);
    const latest = await descriptor();
    expect(latest.templateId).not.toBe(initial.templateId);
    const emitted: Array<{source:string}> = [];
    await plugin.generateBundle!.call({getModuleIds:()=>[realpathSync(join(root,'contract.ts'))][Symbol.iterator](),emitFile:(value:{source:string})=>{emitted.push(value);return '1';}} as never, {} as never, {} as never);
    expect(emitted[0].source).toContain('Latest');
    expect(emitted[0].source).not.toContain('Superseded');
    const meta = new DOMParser().parseFromString(emitted[0].source,'text/html').querySelector('meta[name="local-web-interactive-export-template"]')!;
    const {templateUrl: _url,...identity} = latest;
    expect(JSON.parse(atob(meta.getAttribute('content')!))).toEqual(identity);
    writeFileSync(join(root,'label.ts'), 'export const label=;');
    await expect(update()).rejects.toThrow(/^interactive export packaging failed$/);
    await expect(descriptor()).rejects.toThrow(/^interactive export packaging failed$/);
    writeFileSync(join(root,'label.ts'), 'export const label="Latest";');
    await update();
    expect(await descriptor()).toEqual(latest);
  });

  it.each(['multiple chunks', 'external imports', 'dynamic imports', 'emitted asset', 'source map', 'source map filename', 'oversized'])('fails closed for %s build output', async (shape) => {
    const { buildInteractiveExportTemplate } = await import(packagerModule);
    const options = fixture();
    await buildInteractiveExportTemplate(options);
    const result = await vi.mocked(vite.build).mock.results.at(-1)!.value;
    const output = structuredClone(Array.isArray(result) ? result[0] : result);
    const chunk = output.output.find((item) => item.type === 'chunk');
    if (shape === 'multiple chunks') output.output.push({ ...chunk, fileName: 'second.js' });
    if (shape === 'external imports') chunk.imports = ['https://example.test/external.js'];
    if (shape === 'dynamic imports') chunk.dynamicImports = ['sibling.js'];
    if (shape === 'emitted asset') output.output.push({ type: 'asset', fileName: 'photo.png', source: new Uint8Array([0]), names: [], originalFileNames: [] });
    if (shape === 'source map') chunk.map = { version: 3, sources: ['private.ts'] };
    if (shape === 'source map filename') chunk.sourcemapFileName = 'private.js.map';
    if (shape === 'oversized') chunk.code = 'x'.repeat(20 * 1024 * 1024);
    vi.mocked(vite.build).mockResolvedValueOnce(output);
    if (shape === 'oversized') {
      await expect(buildInteractiveExportTemplate(options)).rejects.toThrowError(expect.objectContaining({
        code: 'artifact-oversized', message: 'interactive export artifact is too large',
      }));
    } else {
      await expect(buildInteractiveExportTemplate(options)).rejects.toThrowError(expect.objectContaining({
        code: 'packaging-failed', message: 'interactive export packaging failed',
      }));
    }
  });
});
