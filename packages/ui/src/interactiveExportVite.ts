import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync, realpathSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';
import type { Plugin } from 'vite';

import { INTERACTIVE_EXPORT_PAYLOAD_MARKER } from './interactiveExportDocument.js';
import {
  canonicalInteractiveExportJson,
  createCompatibilityId,
  defineInteractiveExportContract,
  InteractiveExportError,
  LOCAL_WEB_INTERACTIVE_EXPORT_SCHEMA,
  MAX_INTERACTIVE_EXPORT_ARTIFACT_BYTES,
} from './interactiveExportModel.js';
import type { InteractiveExportContract, InteractiveExportTemplateDescriptor } from './interactiveExportModel.js';
import type { ContextJson } from './contextExportModel.js';

type TemplateIdentity = Omit<InteractiveExportTemplateDescriptor, 'templateUrl'>;
type ContractMetadata = Pick<InteractiveExportContract<ContextJson, ContextJson>, 'id' | 'version'>;

export type InteractiveExportViteOptions = {
  readonly appId: string;
  /** Module exporting the same named `contract` (or default) used by both entries. */
  readonly contract: string;
  readonly entry: string;
};

type TemplateOptions = {
  readonly appId: string;
  readonly appVersion: string;
  readonly sourceRevision: string;
  readonly contract: ContractMetadata;
  readonly entry: string;
  readonly root: string;
  readonly contractModule?: string;
};

type PackagedTemplate = {
  readonly html: string;
  readonly fileName: string;
  readonly identity: TemplateIdentity;
  readonly dependencies: readonly string[];
};

const fail = (): never => { throw new InteractiveExportError('packaging-failed', 'interactive export packaging failed'); };
const digest = (value: string): string => createHash('sha256').update(value).digest('hex');
const encoded = (value: unknown): string => Buffer.from(canonicalInteractiveExportJson(value)).toString('base64');
const canonicalId = /^[a-z][a-z0-9-]*$/;
const canonicalBase = /^\/(?:[a-z0-9]+(?:-[a-z0-9]+)*\/)*$/;
const virtualId = 'virtual:local-web-interactive-export';
const resolvedVirtualId = `\0${virtualId}`;
const packagedTheme = 'fallback-theme.css';

function requireSize(value: string): void {
  if (Buffer.byteLength(value, 'utf8') > MAX_INTERACTIVE_EXPORT_ARTIFACT_BYTES) {
    throw new InteractiveExportError('artifact-oversized', 'interactive export artifact is too large');
  }
}

function requireMetadata(options: TemplateOptions): string {
  if (!canonicalId.test(options.appId) || !canonicalId.test(options.contract.id)
    || !Number.isSafeInteger(options.contract.version) || options.contract.version < 1
    || typeof options.appVersion !== 'string' || !options.appVersion.trim()
    || typeof options.sourceRevision !== 'string' || !options.sourceRevision.trim()) fail();
  return `${options.appId}/${options.contract.id}/v${options.contract.version}`;
}

function requireInlineUrl(value: string): void {
  if (!/^(?:data:|blob:|#)/i.test(value.trim())) fail();
}

function requireInlineAttribute(key: string, value: string): void {
  const candidates = key.toLowerCase() === 'srcset'
    ? value.split(/\s+[0-9.]+[wx]\s*,\s*|,\s+/)
    : [value];
  candidates.forEach(requireInlineUrl);
}

/** Dependency checks complement (not replace) the artifact's runtime CSP. */
function validateCss(value: string): void {
  if (/sourceMappingURL|sourceURL/i.test(value)) fail();
  for (const match of value.matchAll(/url\(\s*(['"]?)(.*?)\1\s*\)/gi)) requireInlineUrl(match[2]);
  if (/@import\b/i.test(value)) fail();
}

function validateHtml(value: string): void {
  for (const match of value.matchAll(/<(script|link|img|source|video|audio|iframe|object|embed|input|image|use|feImage)(?=[\s/>])(?:[^"'<>]|"[^"]*"|'[^']*')*>/gi)) {
    // Consume whole attributes, including inert values, before selecting resources.
    // Word-boundary searches also match data-href or href-shaped quoted content.
    const attributes = match[0].slice(match[1].length + 1, -1);
    for (const attribute of attributes.matchAll(/([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) {
      if (/^(src|href|xlink:href|poster|data|srcset)$/i.test(attribute[1])) {
        requireInlineAttribute(attribute[1], attribute[2] ?? attribute[3] ?? attribute[4] ?? '');
      }
    }
  }
  for (const match of value.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style>/gi)) validateCss(match[1]);
  for (const match of value.matchAll(/<[^>]+\sstyle\s*=\s*(?:"([^"]*)"|'([^']*)')/gi)) validateCss(match[1] ?? match[2]);
}

function validateScript(code: string, parseAst: typeof import('vite').parseAst): void {
  // Use Vite's standard parser so escaped literals are checked as actual values.
  type AstNode = Record<string, unknown>;
  const literal = (value: unknown): string | undefined => {
    const node = value as AstNode | undefined;
    if (node?.type === 'Literal' && typeof node.value === 'string') return node.value;
    if (node?.type === 'TemplateLiteral' && (node.expressions as unknown[]).length === 0) {
      return ((node.quasis as AstNode[])[0].value as {cooked: string}).cooked;
    }
    return undefined;
  };
  const name = (value: unknown): string | undefined => {
    const node = value as AstNode | undefined;
    return node?.type === 'Identifier' ? node.name as string : literal(node);
  };
  const elementFactory = (value: unknown): boolean => {
    const callee = value as AstNode;
    if (callee.type === 'SequenceExpression') return elementFactory((callee.expressions as unknown[]).at(-1));
    const method = callee.type === 'MemberExpression' ? name(callee.property) : name(callee);
    return method !== undefined && /^(jsx|jsxs|jsxDEV|createElement)$/.test(method);
  };
  type Element = {tag: string; attributes: Map<string, string[]>};
  type Bindings = Map<string, Element | undefined>;
  const elements: Element[] = [];
  const createElement = (tag: string): Element => {
    const element = {tag: tag.toLowerCase(), attributes: new Map<string, string[]>()};
    elements.push(element);
    return element;
  };
  const elementFor = (value: unknown, bindings: Bindings): Element | undefined => {
    const node = value as AstNode | undefined;
    if (node?.type === 'Identifier') return bindings.get(node.name as string);
    if (node?.type === 'CallExpression') {
      const method = name((node.callee as AstNode).property);
      const args = node.arguments as unknown[];
      const tag = method === 'createElement' ? literal(args[0])
        : method === 'createElementNS' && literal(args[0]) === 'http://www.w3.org/2000/svg' ? literal(args[1])?.split(':').at(-1) : undefined;
      if (tag !== undefined) return createElement(tag);
    }
    return undefined;
  };
  const attribute = (element: Element | undefined, key: string | undefined, value: unknown): void => {
    const text = literal(value);
    if (!key || text === undefined) return;
    if (key === 'innerHTML' || key === 'outerHTML') validateHtml(text);
    if (element?.tag === 'style' && /^(innerHTML|textContent|innerText)$/.test(key)) validateCss(text);
    if (key === 'style') validateCss(text);
    if (element) {
      const normalized = key.toLowerCase();
      element.attributes.set(normalized, [...element.attributes.get(normalized) ?? [], text]);
    }
  };
  const visit = (value: unknown, inherited: Bindings): void => {
    if (value === null || typeof value !== 'object') return;
    const node = value as AstNode;
    const bindings = /^(FunctionExpression|FunctionDeclaration|ArrowFunctionExpression)$/.test(String(node.type))
      ? new Map(inherited) : inherited;
    for (const param of (node.params as AstNode[] | undefined) ?? []) {
      if (param.type === 'Identifier') bindings.set(param.name as string, undefined);
    }
    if (node.type === 'ImportExpression' || node.type === 'ImportDeclaration') fail();
    if (node.type === 'VariableDeclarator' && (node.id as AstNode).type === 'Identifier') {
      bindings.set((node.id as AstNode).name as string, elementFor(node.init, bindings));
    }
    if (node.type === 'AssignmentExpression') {
      const left = node.left as AstNode;
      if (left.type === 'Identifier') bindings.set(left.name as string, elementFor(node.right, bindings));
      else {
        attribute(elementFor(left.object, bindings), name(left.property), node.right);
        // SVG href is an SVGAnimatedString, commonly assigned through baseVal.
        const object = left.object as AstNode | undefined;
        if (name(left.property) === 'baseVal' && object?.type === 'MemberExpression') {
          attribute(elementFor(object.object, bindings), name(object.property), node.right);
        }
        if (name((left.object as AstNode | undefined)?.property) === 'style') {
          const text = literal(node.right);
          if (text !== undefined) validateCss(text);
        }
      }
    }
    if (node.type === 'CallExpression') {
      const args = node.arguments as unknown[];
      const callee = node.callee as AstNode;
      if (name(callee.property) === 'setAttribute') attribute(elementFor(callee.object, bindings), literal(args[0]), args[1]);
      if (name(callee.property) === 'setAttributeNS') {
        const namespace = args[0] as AstNode | undefined;
        if (literal(namespace) === 'http://www.w3.org/1999/xlink' || literal(namespace) === ''
          || (namespace?.type === 'Literal' && namespace.value === null)) {
          const qualifiedName = literal(args[1]);
          const key = literal(namespace) === 'http://www.w3.org/1999/xlink' ? qualifiedName?.split(':').at(-1) : qualifiedName;
          attribute(elementFor(callee.object, bindings), key, args[2]);
        }
      }
      if (name(callee.property) === 'insertAdjacentHTML') {
        const html = literal(args[1]);
        if (html !== undefined) validateHtml(html);
      }
      // Only established JSX/createElement calls carry element props; tag-shaped
      // arguments to unrelated application functions remain ordinary content.
      const tag = literal(args[0]);
      const props = args[1] as AstNode | undefined;
      if (elementFactory(callee) && tag && /^[a-z][a-zA-Z0-9-]*$/.test(tag) && props?.type === 'ObjectExpression') {
        const element = createElement(tag);
        for (const prop of props.properties as AstNode[]) {
          const key = name(prop.key);
          attribute(element, key, prop.value);
          if (key === 'style' && (prop.value as AstNode)?.type === 'ObjectExpression') {
            for (const style of (prop.value as AstNode).properties as AstNode[]) {
              const text = literal(style.value);
              if (text !== undefined) validateCss(text);
            }
          }
          if (key === 'dangerouslySetInnerHTML' && (prop.value as AstNode)?.type === 'ObjectExpression') {
            for (const html of (prop.value as AstNode).properties as AstNode[]) {
              if (name(html.key) === '__html') attribute(element, 'innerHTML', html.value);
            }
          }
        }
      }
    }
    for (const child of Object.values(node)) {
      if (Array.isArray(child)) child.forEach((item) => visit(item, bindings));
      else if (typeof child === 'object') visit(child, bindings);
    }
  };
  if (/sourceMappingURL|sourceURL/.test(code)) fail();
  visit(parseAst(code), new Map());
  for (const element of elements) {
    if (/^(image|use|feimage)$/.test(element.tag)) {
      for (const key of ['href', 'xlink:href', 'xlinkhref']) {
        for (const value of element.attributes.get(key) ?? []) requireInlineUrl(value);
      }
    }
    if (/^(script|img|source|video|audio|iframe|object|embed|input)$/.test(element.tag)) {
      for (const key of ['src', 'srcset', 'poster', 'data']) {
        for (const value of element.attributes.get(key) ?? []) requireInlineAttribute(key, value);
      }
    }
    if (element.tag === 'link' && (element.attributes.get('rel') ?? []).some((rel) => /^(stylesheet|preload|modulepreload|icon|manifest)$/i.test(rel))) {
      for (const value of element.attributes.get('href') ?? []) requireInlineUrl(value);
    }
  }
}

/** Internal packaging boundary; apps configure the public plugin with a contract module. */
export async function buildInteractiveExportTemplate(options: TemplateOptions): Promise<PackagedTemplate> {
  try {
    const payloadContractId = requireMetadata(options);
    const entry = realpathSync(resolve(options.root, options.entry));
    const contractModule = options.contractModule && realpathSync(options.contractModule);
    const dependencies = new Set<string>([entry, realpathSync(new URL(packagedTheme, import.meta.url))]);
    let resolveCssAsset: (id: string, importer?: string) => Promise<string | undefined>;
    const {build, parseAst} = await import('vite');
    const output = await build({
      configFile: false,
      envFile: false,
      envPrefix: [],
      root: options.root,
      publicDir: false,
      logLevel: 'silent',
      mode: 'production',
      define: { 'process.env.NODE_ENV': '"production"', 'import.meta.env.DEV': 'false', 'import.meta.env.PROD': 'true' },
      oxc: { jsx: { development: false } },
      css: {postcss: {plugins: [{
        postcssPlugin: 'local-web-offline-css-dependencies',
        async OnceExit(root, {result}) {
          for (const message of result.messages) {
            if (message.type === 'dependency' && typeof message.file === 'string') dependencies.add(realpathSync(message.file));
          }
          const assets: Promise<void>[] = [];
          root.walkDecls((declaration) => {
            const importer = declaration.source?.input.file;
            if (!importer) return;
            dependencies.add(realpathSync(importer));
            for (const match of declaration.value.matchAll(/url\(\s*(['"]?)(.*?)\1\s*\)/gi)) {
              const url = match[2];
              if (/^(?:data:|blob:|#|[a-z]+:|\/\/)/i.test(url)) continue;
              assets.push((async () => {
                const file = await resolveCssAsset(decodeURI(url).split(/[?#]/)[0], importer);
                if (file) dependencies.add(realpathSync(file));
              })());
            }
          });
          await Promise.all(assets);
        },
      }]}},
      plugins: [{
        name: 'local-web-offline-dependencies',
        configResolved(config) { resolveCssAsset = config.createResolver({preferRelative: true, extensions: []}); },
        buildEnd() {
          for (const id of this.getModuleIds()) {
            try { dependencies.add(realpathSync(id.split('?')[0])); } catch { /* Ignore virtual module IDs. */ }
          }
        },
      }],
      build: {
        write: false,
        sourcemap: false,
        cssCodeSplit: false,
        assetsInlineLimit: Number.MAX_SAFE_INTEGER,
        lib: { entry, formats: ['iife'], name: 'LocalWebInteractiveExport', fileName: 'offline', cssFileName: 'offline' },
        rolldownOptions: { output: { codeSplitting: false } },
      },
    });
    if (Array.isArray(output) && output.length !== 1) return fail();
    const result = Array.isArray(output) ? output[0] : output;
    if (!('output' in result)) return fail();
    const chunks = result.output.filter((item) => item.type === 'chunk');
    const assets = result.output.filter((item) => item.type === 'asset');
    if (chunks.length !== 1 || assets.length > 1 || assets.some((item) => !item.fileName.endsWith('.css'))) fail();
    const chunk = chunks[0];
    if (!chunk.isEntry || chunk.imports.length || chunk.dynamicImports.length || chunk.map || chunk.sourcemapFileName
      || (contractModule && !Object.keys(chunk.modules).includes(contractModule))) fail();
    requireSize(chunk.code);
    validateScript(chunk.code, parseAst);
    // HTML raw-text end tags must not terminate the exact script whose bytes are hashed.
    const script = chunk.code.replace(/<\/script/gi, '<\\/script');
    const appCss = assets.length ? (typeof assets[0].source === 'string' ? assets[0].source : Buffer.from(assets[0].source).toString('utf8')) : '';
    const css = `${readFileSync(new URL(packagedTheme, import.meta.url), 'utf8')}\n${appCss}`;
    requireSize(css);
    validateCss(css);
    if (/<\/style/i.test(css)) fail();
    const scriptHash = createHash('sha256').update(script).digest('base64');
    const csp = `default-src 'none'; connect-src 'none'; script-src 'sha256-${scriptHash}'; script-src-attr 'none'; style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; worker-src blob:; child-src blob:; object-src 'none'; frame-src 'none'; manifest-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`;
    const metadata = { appId: options.appId, appVersion: options.appVersion,
      sourceRevision: options.sourceRevision, payloadContractId };
    const htmlFor = (identity: object): string => `<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="Content-Security-Policy" content="${csp}"><meta name="local-web-interactive-export-template" content="${encoded(identity)}"><title>Offline snapshot</title><style>${css}</style></head><body><div id="root"></div>${INTERACTIVE_EXPORT_PAYLOAD_MARKER}<script>${script}</script></body></html>\n`;
    const normalized = htmlFor({ ...metadata, templateId: 'LOCAL_WEB_TEMPLATE_ID', compatibilityId: 'LOCAL_WEB_COMPATIBILITY_ID' });
    const templateId = digest(canonicalInteractiveExportJson({ schema: LOCAL_WEB_INTERACTIVE_EXPORT_SCHEMA, template: normalized }));
    const identity: TemplateIdentity = { ...metadata, templateId,
      compatibilityId: createCompatibilityId({appId: options.appId, payloadContractId, templateId}) };
    const html = htmlFor(identity);
    requireSize(html);
    if (html.split(INTERACTIVE_EXPORT_PAYLOAD_MARKER).length !== 2) fail();
    return { html, identity, fileName: `assets/local-web-interactive-export-${templateId}.html`, dependencies: [...dependencies].sort() };
  } catch (error) {
    if (error instanceof InteractiveExportError && error.code === 'artifact-oversized') throw error;
    return fail();
  }
}

export function localWebInteractiveExport(options: InteractiveExportViteOptions): Plugin {
  type PackagedState = { template: PackagedTemplate; descriptor: InteractiveExportTemplateDescriptor; dependencies: Set<string> };
  let state: PackagedState | undefined;
  let contractModule: string;
  let settings: {root: string; base: string};
  let requestedRevision = 0;
  let completedRevision = -1;
  let inFlight: Promise<void> | undefined;
  let watched = new Set<string>();
  let server: import('vite').ViteDevServer | undefined;
  const configured = () => {
    if (!state) return fail();
    return state;
  };
  const rebuild = async (): Promise<PackagedState> => {
    const config = settings;
    try {
        const manifestFile = join(config.root, 'local-web.json');
        const packageFile = join(config.root, 'package.json');
        const manifest = JSON.parse(readFileSync(manifestFile, 'utf8'));
        if (manifest.id !== options.appId) fail();
        const appPackage = JSON.parse(readFileSync(packageFile, 'utf8'));
        const sourceRevision = execFileSync('git', ['-C', config.root, 'rev-parse', 'HEAD'], {
          encoding: 'utf8', timeout: 5000, maxBuffer: 1024, stdio: ['ignore', 'pipe', 'pipe'],
        }).trim();
        if (!/^[a-f0-9]{40,64}$/.test(sourceRevision)) fail();
        contractModule = realpathSync(resolve(config.root, options.contract));
        const {runnerImport} = await import('vite');
        const imported = await runnerImport<{ contract?: InteractiveExportContract<ContextJson, ContextJson>; default?: InteractiveExportContract<ContextJson, ContextJson> }>(contractModule, {
          configFile: false, envFile: false, envPrefix: [], root: config.root, publicDir: false, logLevel: 'silent',
        });
        const contract = defineInteractiveExportContract((imported.module.contract ?? imported.module.default)!);
        const template = await buildInteractiveExportTemplate({ appId: options.appId, appVersion: appPackage.version,
          sourceRevision, root: config.root, entry: options.entry, contract, contractModule });
        const dependencies = new Set([...template.dependencies, ...imported.dependencies, contractModule, manifestFile, packageFile].map((file) => realpathSync(file)));
        return {template, descriptor: { ...template.identity, templateUrl: `${config.base}${template.fileName}` }, dependencies};
      } catch (error) {
        if (error instanceof InteractiveExportError && error.code === 'artifact-oversized') throw error;
        return fail();
      }
  };
  // One in-flight build, with all intervening edits coalesced into one pending revision.
  // A candidate superseded while building is never published to any consumer.
  const refresh = (): Promise<void> => {
    if (inFlight) return inFlight;
    if (completedRevision === requestedRevision) return Promise.resolve();
    inFlight = (async () => {
      try {
        while (completedRevision !== requestedRevision) {
          const revision = requestedRevision;
          let candidate: PackagedState;
          try { candidate = await rebuild(); } catch (error) {
            if (revision !== requestedRevision) continue;
            throw error;
          }
          if (revision !== requestedRevision) continue;
          state = candidate;
          watched = candidate.dependencies;
          completedRevision = revision;
          server?.watcher.add([...watched]);
        }
      } catch (error) {
        state = undefined;
        throw error;
      } finally { inFlight = undefined; }
    })();
    return inFlight;
  };
  const relevant = (id: string): boolean => {
    let file = resolve(id);
    try { file = realpathSync(file); } catch {
      // The file can be absent while a symlinked parent still needs normalization.
      try { file = join(realpathSync(dirname(file)), basename(file)); } catch { /* Keep the known path. */ }
    }
    return watched.has(file);
  };
  return {
    name: 'local-web-interactive-export',
    async configResolved(config) {
      if (!canonicalBase.test(config.base) || !canonicalId.test(options.appId)) fail();
      settings = {root: config.root, base: config.base};
      await refresh();
    },
    watchChange(id, change) {
      if (relevant(id)) {
        requestedRevision += 1;
        if (change.event === 'delete') state = undefined;
      }
    },
    async buildStart() {
      await refresh();
      for (const file of watched) this.addWatchFile(file);
    },
    async hotUpdate(context) {
      if (this.environment.name !== 'client' || !relevant(context.file)) return;
      requestedRevision += 1;
      if (context.type === 'delete') state = undefined;
      await refresh();
      const virtual = this.environment.moduleGraph.getModuleById(resolvedVirtualId);
      if (virtual) this.environment.moduleGraph.invalidateModule(virtual);
      this.environment.hot.send({type: 'full-reload'});
      return [];
    },
    resolveId(id) { if (id === virtualId) return resolvedVirtualId; },
    load(id) { if (id === resolvedVirtualId) return `export default ${canonicalInteractiveExportJson(configured().descriptor)};`; },
    transformIndexHtml: {
      order: 'post',
      handler(html) {
        if (/local-web-interactive-export-(?:descriptor|template)/i.test(html)) fail();
        return [{tag: 'meta', attrs: {name: 'local-web-interactive-export-descriptor', content: encoded(configured().descriptor)}, injectTo: 'head'}];
      },
    },
    configureServer(devServer) {
      server = devServer;
      devServer.watcher.add([...watched]);
      devServer.middlewares.use((request, response, next) => {
        const result = configured();
        if (request.url !== result.descriptor.templateUrl) { next(); return; }
        response.setHeader('Content-Type', 'text/html; charset=utf-8');
        response.setHeader('Cache-Control', 'no-store');
        response.end(result.template.html);
      });
    },
    async generateBundle() {
      const result = configured();
      if (![...this.getModuleIds()].includes(contractModule)) fail();
      this.emitFile({type: 'asset', fileName: result.template.fileName, source: result.template.html});
    },
  };
}
