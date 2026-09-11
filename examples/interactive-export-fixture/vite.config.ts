import { localWebApp, localWebInteractiveExport } from '@local-web/ui/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

const appId = 'interactive-export-fixture';

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  plugins: [
    react(),
    localWebApp({ appId, basePath: process.env.VITE_PUBLIC_BASE_PATH ?? '/' }),
    localWebInteractiveExport({ appId, contract: './src/contract.ts', entry: './src/offline.tsx' }),
  ],
  build: { emptyOutDir: true },
});
