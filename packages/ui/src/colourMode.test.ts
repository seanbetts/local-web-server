import { describe, expect, it } from 'vitest';

import {
  applyColourMode,
  COLOUR_MODE_STORAGE_KEY,
  readColourMode,
  setColourMode,
} from './colourMode';

const createStorage = () => {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    removeItem: (key: string) => values.delete(key),
    setItem: (key: string, value: string) => values.set(key, value),
  };
};

describe('colour mode', () => {
  it('falls back to system and removes an invalid stored value', () => {
    const storage = createStorage();
    storage.setItem(COLOUR_MODE_STORAGE_KEY, 'sepia');

    expect(readColourMode(storage)).toBe('system');
    expect(storage.getItem(COLOUR_MODE_STORAGE_KEY)).toBeNull();
  });

  it('applies a shared explicit mode to the document root', () => {
    const storage = createStorage();
    delete document.documentElement.dataset.lwpColourMode;

    setColourMode('dark', storage, document.documentElement);

    expect(document.documentElement.dataset.lwpColourMode).toBe('dark');
    expect(storage.getItem(COLOUR_MODE_STORAGE_KEY)).toBe('dark');
  });

  it('removes the explicit selector for system mode', () => {
    document.documentElement.dataset.lwpColourMode = 'dark';

    applyColourMode('system', document.documentElement);

    expect(document.documentElement).not.toHaveAttribute('data-lwp-colour-mode');
  });

  it('falls back without throwing when storage is unavailable', () => {
    const unreadableStorage = {
      getItem: () => {
        throw new Error('blocked');
      },
      removeItem: () => {
        throw new Error('blocked');
      },
    };
    const unwritableStorage = {
      setItem: () => {
        throw new Error('blocked');
      },
    };

    expect(readColourMode(unreadableStorage)).toBe('system');
    expect(() =>
      setColourMode('light', unwritableStorage, document.documentElement),
    ).not.toThrow();
    expect(document.documentElement).not.toHaveAttribute('data-lwp-colour-mode');
  });
});
