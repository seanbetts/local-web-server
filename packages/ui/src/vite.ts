/// <reference types="./vite-env.d.ts" preserve="true" />

import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import type { Plugin } from 'vite';

import { deriveAccessibleAccent } from './colour.js';
import { COLOUR_MODE_BOOTSTRAP_SCRIPT } from './colourMode.js';

export { localWebInteractiveExport } from './interactiveExportVite.js';
export type { InteractiveExportViteOptions } from './interactiveExportVite.js';

const THEME_ROUTE = '/_local-web/platform/theme.css';
const CANONICAL_BASE_PATH = /^\/(?:[a-z0-9]+(?:-[a-z0-9]+)*\/)*$/;
const CANONICAL_APP_ID = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
const CANONICAL_ACCENT = /^#[0-9A-F]{6}$/;
const PACKAGED_THEME = 'fallback-theme.css';
const LIGHT_SURFACE = '#FFFFFF';
const DARK_SURFACE = '#19202B';

type GeneratedAsset = {
  body: string;
  contentType: string;
  fileName: string;
  url: string;
};

const digest = (body: string): string =>
  createHash('sha256').update(body).digest('hex').slice(0, 12);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null;

const manifestAccent = (root: string, appId: string): string => {
  let manifest: unknown;
  try {
    manifest = JSON.parse(readFileSync(join(root, 'local-web.json'), 'utf-8'));
  } catch {
    throw new Error('could not read local-web.json');
  }
  if (!isRecord(manifest) || manifest.id !== appId) {
    throw new Error('local-web.json id must match appId');
  }
  const accent = isRecord(manifest.home) ? manifest.home.accent : undefined;
  if (typeof accent !== 'string' || !CANONICAL_ACCENT.test(accent)) {
    throw new Error('local-web.json home.accent must be a canonical uppercase hex colour');
  }
  return accent;
};

const generatedAssets = (appId: string, basePath: string, accent: string): GeneratedAsset[] => {
  const accentStylesheet = `:where([data-lwp-app-id="${appId}"]) {\n  --lwp-app-accent-light: ${deriveAccessibleAccent(accent, LIGHT_SURFACE)};\n  --lwp-app-accent-dark: ${deriveAccessibleAccent(accent, DARK_SURFACE)};\n}\n`;
  const sources = [
    {
      body: COLOUR_MODE_BOOTSTRAP_SCRIPT,
      contentType: 'application/javascript; charset=utf-8',
      prefix: 'local-web-colour-mode',
      suffix: 'js',
    },
    {
      body: accentStylesheet,
      contentType: 'text/css; charset=utf-8',
      prefix: 'local-web-app-accent',
      suffix: 'css',
    },
  ];

  return sources.map(({ body, contentType, prefix, suffix }) => {
    const fileName = `assets/${prefix}-${digest(body)}.${suffix}`;
    return { body, contentType, fileName, url: `${basePath}${fileName}` };
  });
};

export function localWebApp(options: {
  appId: string;
  basePath: string;
  accent?: string;
  fallbackThemePath?: string;
}): Plugin {
  if (!CANONICAL_APP_ID.test(options.appId)) {
    throw new Error('appId must be a canonical lowercase identifier');
  }
  if (!CANONICAL_BASE_PATH.test(options.basePath)) {
    throw new Error('basePath must be / or a canonical path prefix ending in /');
  }
  if (options.accent !== undefined && !CANONICAL_ACCENT.test(options.accent)) {
    throw new Error('accent must be a canonical uppercase hex colour');
  }
  const fallbackThemePath = options.fallbackThemePath
    ?? new URL(PACKAGED_THEME, import.meta.url);
  let assets = options.accent === undefined
    ? undefined
    : generatedAssets(options.appId, options.basePath, options.accent);

  const resolvedAssets = (): GeneratedAsset[] => {
    if (!assets) throw new Error('local-web app assets have not been configured');
    return assets;
  };

  return {
    name: `local-web-app:${options.appId}`,
    config() {
      return { base: options.basePath };
    },
    configResolved(config) {
      assets = generatedAssets(
        options.appId,
        config.base,
        options.accent ?? manifestAccent(config.root, options.appId),
      );
    },
    transformIndexHtml: {
      order: 'post',
      handler() {
        const [bootstrap, accent] = resolvedAssets();
        return [
          {
            tag: 'script',
            attrs: { src: bootstrap.url },
            injectTo: 'head-prepend',
          },
          {
            tag: 'link',
            attrs: { rel: 'stylesheet', href: accent.url },
            injectTo: 'head-prepend',
          },
        ];
      },
    },
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const asset = assets?.find(({ url }) => request.url === url);
        if (asset) {
          response.setHeader('Content-Type', asset.contentType);
          response.setHeader('Cache-Control', 'no-store');
          response.end(asset.body);
          return;
        }
        if (request.url !== THEME_ROUTE) {
          next();
          return;
        }
        response.setHeader('Content-Type', 'text/css; charset=utf-8');
        response.setHeader('Cache-Control', 'no-store');
        response.end(readFileSync(fallbackThemePath, 'utf-8'));
      });
    },
    generateBundle() {
      for (const asset of resolvedAssets()) {
        this.emitFile({
          type: 'asset',
          fileName: asset.fileName,
          source: asset.body,
        });
      }
    },
  };
}
