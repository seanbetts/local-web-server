import { defineConfig } from 'vite';
import { readFileSync } from 'node:fs';

export default defineConfig({
  root: new URL('.', import.meta.url).pathname,
  plugins: [{
    name: 'local-web-vite-descriptor-types',
    generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'vite-env.d.ts',
        source: readFileSync(new URL('./src/vite-env.d.ts', import.meta.url), 'utf8') });
    },
  }],
  build: {
    cssCodeSplit: true,
    emptyOutDir: true,
    lib: {
      entry: {
        'fallback-theme': 'src/fallback-theme.css',
        index: 'src/index.ts',
        styles: 'src/styles.css',
        vite: 'src/vite.ts',
      },
      formats: ['es'],
      fileName: (_format, entryName) => `${entryName}.js`,
      cssFileName: 'styles',
    },
    rollupOptions: {
      output: { chunkFileNames: '[name].js' },
      external: [
        'node:child_process',
        'node:crypto',
        'node:fs',
        'node:path',
        'react',
        'react-dom',
        'react-dom/client',
        'react/jsx-runtime',
        'vite',
      ],
    },
  },
  test: {
    environment: 'jsdom',
    environmentOptions: {
      jsdom: {
        url: 'http://localhost/',
      },
    },
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
  },
});
