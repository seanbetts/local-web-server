import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import '@local-web/ui/styles.css';
import { SystemIndex } from './SystemIndex';

const root = document.getElementById('root');
if (!root) {
  throw new Error('System Index root is unavailable');
}

createRoot(root).render(
  <StrictMode>
    <SystemIndex />
  </StrictMode>,
);
