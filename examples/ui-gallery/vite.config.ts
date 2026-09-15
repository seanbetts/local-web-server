import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { defineConfig } from 'vite';

const candidateRoute = '/candidate-theme.css';
const uiPackage = JSON.parse(
  readFileSync(new URL('../../packages/ui/package.json', import.meta.url), 'utf8'),
) as { version: string };

const candidateTheme = () => ({
  name: 'local-web-candidate-theme',
  configureServer(server: { middlewares: { use: (route: string, handler: (request: unknown, response: { setHeader: (name: string, value: string) => void; end: (body?: string | Buffer) => void }) => void) => void } }) {
    server.middlewares.use(candidateRoute, (_request, response) => {
      const candidate = process.env.LWP_THEME_CANDIDATE;
      if (!candidate) {
        response.end();
        return;
      }
      response.setHeader('Content-Type', 'text/css; charset=utf-8');
      response.setHeader('Cache-Control', 'no-store');
      response.end(readFileSync(candidate));
    });
  },
});

export default defineConfig(({ command }) => ({
  root: new URL('.', import.meta.url).pathname,
  base: command === 'build' ? '/_local-web/platform/ui-gallery/' : '/',
  define: {
    __LOCAL_WEB_UI_VERSION__: JSON.stringify(uiPackage.version),
  },
  plugins: [candidateTheme()],
  resolve: {
    alias: [
      {
        find: /^@local-web\/ui\/styles\.css$/,
        replacement: fileURLToPath(new URL('../../packages/ui/src/styles.css', import.meta.url)),
      },
      {
        find: /^@local-web\/ui$/,
        replacement: fileURLToPath(new URL('../../packages/ui/src/index.ts', import.meta.url)),
      },
    ],
  },
  build: {
    emptyOutDir: true,
    outDir: 'dist',
    rollupOptions: {
      input: {
        app: fileURLToPath(new URL('./index.html', import.meta.url)),
        indexFrame: fileURLToPath(new URL('./index-frame.html', import.meta.url)),
        immersiveFrame: fileURLToPath(new URL('./immersive-frame.html', import.meta.url)),
      },
    },
  },
  server: {
    host: '127.0.0.1',
    hmr: false,
    port: 4179,
    strictPort: true,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.tsx'],
    setupFiles: ['../../packages/ui/src/test/setup.ts'],
  },
}));
