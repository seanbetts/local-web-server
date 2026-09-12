import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import react from '@vitejs/plugin-react';
import { defineConfig } from 'vitest/config';

import { localWebApp } from '@local-web/ui/vite';

const INDEX_REGISTRY_ROUTE = '/_local-web/platform/index/registry-v1.json';
const PLATFORM_BASE = '/_local-web/platform/index/';

function productionBasePath() {
  return {
    config(_config: unknown, environment: { command: 'build' | 'serve' }) {
      return environment.command === 'build' ? { base: PLATFORM_BASE } : undefined;
    },
    configurePreviewServer(server: {
      middlewares: {
        use: (
          handler: (
            request: { url?: string },
            response: unknown,
            next: () => void,
          ) => void,
        ) => void;
      };
    }) {
      server.middlewares.use((request, _response, next) => {
        if (request.url?.startsWith(PLATFORM_BASE)) {
          request.url = `/${request.url.slice(PLATFORM_BASE.length)}`;
        }
        next();
      });
    },
    name: 'system-index-production-base-path',
  };
}

function developmentRegistryFixture() {
  return {
    apply: 'serve' as const,
    configureServer(server: {
      middlewares: {
        use: (
          handler: (
            request: { url?: string },
            response: {
              end: (body?: string) => void;
              setHeader: (name: string, value: string) => void;
            },
            next: () => void,
          ) => void,
        ) => void;
      };
    }) {
      server.middlewares.use((request, response, next) => {
        if (request.url?.split('?')[0] !== INDEX_REGISTRY_ROUTE) {
          next();
          return;
        }
        response.setHeader('Content-Type', 'application/json; charset=utf-8');
        response.setHeader('Cache-Control', 'no-store');
        response.end(readFileSync(new URL('./fixtures/registry-v1.json', import.meta.url), 'utf8'));
      });
    },
    name: 'system-index-development-registry-fixture',
  };
}

export default defineConfig({
  base: PLATFORM_BASE,
  plugins: [
    react(),
    localWebApp({ appId: 'system-index', basePath: '/', accent: '#8EA7C6' }),
    developmentRegistryFixture(),
    productionBasePath(),
  ],
  root: fileURLToPath(new URL('.', import.meta.url)),
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['./src/**/*.test.ts', './src/**/*.test.tsx'],
    setupFiles: ['./src/test/setup.ts'],
  },
});
