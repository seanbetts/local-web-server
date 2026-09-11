export const COLOUR_MODE_STORAGE_KEY = 'local-web:colour-mode';
export const COLOUR_MODE_CHANGE_EVENT = 'local-web:colour-mode-change';

export const COLOUR_MODE_BOOTSTRAP_SCRIPT = `(() => {
  try {
    const mode = window.localStorage.getItem('${COLOUR_MODE_STORAGE_KEY}');
    if (mode === 'light' || mode === 'dark') {
      document.documentElement.dataset.lwpColourMode = mode;
    }
  } catch {
    // Browser storage can be blocked before the application loads.
  }
})();`;

export type ColourMode = 'system' | 'light' | 'dark';

type ReadableStorage = Pick<Storage, 'getItem' | 'removeItem'>;
type WritableStorage = Pick<Storage, 'setItem'>;

const isColourMode = (value: unknown): value is ColourMode =>
  value === 'system' || value === 'light' || value === 'dark';

export const normaliseColourMode = (value: unknown): ColourMode =>
  isColourMode(value) ? value : 'system';

/**
 * Reads the shared preference without allowing an unavailable browser storage
 * implementation to prevent an app from rendering.
 */
export function readColourMode(storage: ReadableStorage): ColourMode {
  try {
    const storedMode = storage.getItem(COLOUR_MODE_STORAGE_KEY);
    if (storedMode === null || isColourMode(storedMode)) {
      return storedMode ?? 'system';
    }

    try {
      storage.removeItem(COLOUR_MODE_STORAGE_KEY);
    } catch {
      // A stale preference is harmless when storage is not writable.
    }
  } catch {
    // Storage may be blocked in privacy-sensitive browsing contexts.
  }

  return 'system';
}

/** Applies only the explicit mode selector understood by the live theme. */
export function applyColourMode(mode: ColourMode, root: HTMLElement): void {
  const normalisedMode = normaliseColourMode(mode);
  if (normalisedMode === 'system') {
    root.removeAttribute('data-lwp-colour-mode');
    return;
  }

  root.dataset.lwpColourMode = normalisedMode;
}

/** Persists and applies a shared preference, degrading safely if storage fails. */
export function setColourMode(
  mode: ColourMode,
  storage: WritableStorage,
  root: HTMLElement,
): void {
  const normalisedMode = normaliseColourMode(mode);

  applyColourMode(normalisedMode, root);
  try {
    storage.setItem(COLOUR_MODE_STORAGE_KEY, normalisedMode);
  } catch {
    applyColourMode('system', root);
  }
}
