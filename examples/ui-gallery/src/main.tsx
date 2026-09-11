import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import '@local-web/ui/styles.css';
import './gallery.css';
import { Gallery } from './gallery';

const search = new URLSearchParams(window.location.search);
const theme = search.get('theme');
const frame = window.location.pathname.endsWith('/index-frame.html')
  ? 'index'
  : window.location.pathname.endsWith('/immersive-frame.html')
    ? 'immersive'
    : search.get('frame') === 'app-page'
      ? 'app-page'
      : 'app';
if (theme !== 'fallback') {
  const themeLink = document.createElement('link');
  themeLink.rel = 'stylesheet';
  themeLink.href = theme === 'candidate'
    ? '/candidate-theme.css'
    : '/_local-web/platform/theme.css';
  themeLink.dataset.lwpGalleryTheme = '';
  document.head.append(themeLink);
}

const root = document.getElementById('root');
if (!root) {
  throw new Error('Gallery root is unavailable');
}

createRoot(root).render(
  <StrictMode>
    <Gallery frame={frame} />
  </StrictMode>,
);
