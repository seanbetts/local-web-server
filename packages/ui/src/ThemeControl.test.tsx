import { fireEvent, render, screen, within } from '@testing-library/react';
import { act } from 'react';
import { hydrateRoot } from 'react-dom/client';
import { renderToString } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { COLOUR_MODE_STORAGE_KEY, ThemeControl } from './index';
import * as ui from './index.js';
import { offlineEnvelope } from './test/interactiveExport.js';

describe('ThemeControl', () => {
  it('keeps offline choices in memory, synchronises controls, ignores storage, and resets on reload', () => {
    expect(ui.InteractiveExportEnvironmentProvider).toBeTypeOf('function');
    const storage = window.localStorage;
    storage.setItem(COLOUR_MODE_STORAGE_KEY, 'light');
    const storageDescriptor = Object.getOwnPropertyDescriptor(window, 'localStorage')!;
    const storageAccess = vi.fn(() => { throw new Error('Storage must not be accessed'); });
    Object.defineProperty(window, 'localStorage', { configurable: true, get: storageAccess });
    const envelope = offlineEnvelope();
    const content = (second = true) => <ui.InteractiveExportEnvironmentProvider envelope={envelope}><ThemeControl />{second ? <ThemeControl /> : null}</ui.InteractiveExportEnvironmentProvider>;
    try {
      const view = render(content());
      expect(document.documentElement.dataset.lwpColourMode).toBe('dark');
      for (const [label, mode] of [['System', undefined], ['Light', 'light'], ['Dark', 'dark'], ['System', undefined]] as const) {
        fireEvent.click(screen.getAllByRole('button', { name: `${label} colour mode` })[0]);
        expect(document.documentElement.dataset.lwpColourMode).toBe(mode);
        for (const button of screen.getAllByRole('button', { name: `${label} colour mode` })) expect(button).toHaveAttribute('aria-pressed', 'true');
      }
      view.rerender(content(false));
      view.rerender(content());
      expect(screen.getAllByRole('button', { name: 'System colour mode' }).every((button) => button.getAttribute('aria-pressed') === 'true')).toBe(true);
      fireEvent(window, new StorageEvent('storage', { key: COLOUR_MODE_STORAGE_KEY, newValue: 'light' }));
      expect(document.documentElement).not.toHaveAttribute('data-lwp-colour-mode');
      view.unmount();
      render(content());
      expect(document.documentElement.dataset.lwpColourMode).toBe('dark');
      expect(screen.getAllByRole('button', { name: 'Dark colour mode' }).every((button) => button.getAttribute('aria-pressed') === 'true')).toBe(true);
      expect(storageAccess).not.toHaveBeenCalled();
      expect(storage.getItem(COLOUR_MODE_STORAGE_KEY)).toBe('light');
    } finally {
      Object.defineProperty(window, 'localStorage', storageDescriptor);
    }
  });
  beforeEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute('data-lwp-colour-mode');
  });

  afterEach(() => {
    window.localStorage.clear();
    document.documentElement.removeAttribute('data-lwp-colour-mode');
  });

  it('persists and applies each explicit choice, then returns to system', () => {
    render(<ThemeControl />);

    fireEvent.click(screen.getByRole('button', { name: 'Light colour mode' }));
    expect(document.documentElement.dataset.lwpColourMode).toBe('light');
    expect(window.localStorage.getItem(COLOUR_MODE_STORAGE_KEY)).toBe('light');

    fireEvent.click(screen.getByRole('button', { name: 'Dark colour mode' }));
    expect(document.documentElement.dataset.lwpColourMode).toBe('dark');
    expect(window.localStorage.getItem(COLOUR_MODE_STORAGE_KEY)).toBe('dark');

    fireEvent.click(screen.getByRole('button', { name: 'System colour mode' }));
    expect(document.documentElement).not.toHaveAttribute('data-lwp-colour-mode');
    expect(window.localStorage.getItem(COLOUR_MODE_STORAGE_KEY)).toBe('system');
  });

  it('synchronises controls mounted in the same document after either control changes mode', () => {
    render(
      <>
        <ThemeControl />
        <ThemeControl />
      </>,
    );

    const controls = screen.getAllByRole('group', { name: 'Colour mode' });
    fireEvent.click(within(controls[0]).getByRole('button', { name: 'Dark colour mode' }));

    expect(document.documentElement.dataset.lwpColourMode).toBe('dark');
    for (const control of controls) {
      expect(within(control).getByRole('button', { name: 'Dark colour mode' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    }

    fireEvent.click(within(controls[1]).getByRole('button', { name: 'Light colour mode' }));

    expect(document.documentElement.dataset.lwpColourMode).toBe('light');
    for (const control of controls) {
      expect(within(control).getByRole('button', { name: 'Light colour mode' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    }
  });

  it('starts from the shared stored preference', () => {
    window.localStorage.setItem(COLOUR_MODE_STORAGE_KEY, 'dark');

    render(<ThemeControl />);

    expect(screen.getByRole('button', { name: 'Dark colour mode' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(document.documentElement.dataset.lwpColourMode).toBe('dark');
  });

  it('synchronises the exact shared storage event and detaches on unmount', () => {
    const view = render(<ThemeControl />);
    const event = (newValue: string | null, key = COLOUR_MODE_STORAGE_KEY) => {
      const storageEvent = new window.Event('storage') as StorageEvent;
      Object.defineProperties(storageEvent, {
        key: { value: key },
        newValue: { value: newValue },
        storageArea: { value: window.localStorage },
      });
      return storageEvent;
    };

    fireEvent(window, event('dark', 'unrelated'));
    expect(document.documentElement).not.toHaveAttribute('data-lwp-colour-mode');
    fireEvent(window, event('dark'));
    expect(document.documentElement.dataset.lwpColourMode).toBe('dark');
    expect(screen.getByRole('button', { name: 'Dark colour mode' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    fireEvent(window, event('sepia'));
    expect(document.documentElement).not.toHaveAttribute('data-lwp-colour-mode');
    expect(screen.getByRole('button', { name: 'System colour mode' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );

    fireEvent(window, event('dark'));
    view.unmount();
    fireEvent(window, event('light'));
    expect(document.documentElement.dataset.lwpColourMode).toBe('dark');
  });

  it('falls back to system when browser storage itself is inaccessible', () => {
    const storageDescriptor = Object.getOwnPropertyDescriptor(window, 'localStorage');
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get: () => {
        throw new Error('blocked');
      },
    });

    try {
      expect(() => render(<ThemeControl />)).not.toThrow();
      expect(screen.getByRole('button', { name: 'System colour mode' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
      expect(() =>
        fireEvent.click(screen.getByRole('button', { name: 'Light colour mode' })),
      ).not.toThrow();
      expect(screen.getByRole('button', { name: 'System colour mode' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
    } finally {
      Object.defineProperty(window, 'localStorage', storageDescriptor!);
    }
  });

  it('returns to system when a changed preference cannot be persisted', () => {
    const storageDescriptor = Object.getOwnPropertyDescriptor(window, 'localStorage');
    const values = new Map([[COLOUR_MODE_STORAGE_KEY, 'dark']]);
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        clear: () => values.clear(),
        getItem: (key: string) => values.get(key) ?? null,
        key: (index: number) => [...values.keys()][index] ?? null,
        get length() {
          return values.size;
        },
        removeItem: (key: string) => values.delete(key),
        setItem: () => {
          throw new Error('blocked');
        },
      } satisfies Storage,
    });

    try {
      render(<ThemeControl />);
      fireEvent.click(screen.getByRole('button', { name: 'Light colour mode' }));

      expect(screen.getByRole('button', { name: 'System colour mode' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
      expect(document.documentElement).not.toHaveAttribute('data-lwp-colour-mode');
    } finally {
      Object.defineProperty(window, 'localStorage', storageDescriptor!);
    }
  });

  it('hydrates deterministically before reconciling the persisted mode', async () => {
    const serverMarkup = renderToString(<ThemeControl />);
    expect(serverMarkup).toContain('aria-label="System colour mode" aria-pressed="true"');
    window.localStorage.setItem(COLOUR_MODE_STORAGE_KEY, 'dark');
    const container = document.createElement('div');
    container.innerHTML = serverMarkup;
    document.body.append(container);
    const consoleErrors: unknown[][] = [];
    const errorSpy = vi.spyOn(console, 'error').mockImplementation((...arguments_) => {
      consoleErrors.push(arguments_);
    });
    let root: ReturnType<typeof hydrateRoot> | undefined;

    try {
      await act(async () => {
        root = hydrateRoot(container, <ThemeControl />);
      });

      expect(container.querySelector('[aria-label="System colour mode"]')).toHaveAttribute(
        'aria-pressed',
        'false',
      );
      expect(container.querySelector('[aria-label="Dark colour mode"]')).toHaveAttribute(
        'aria-pressed',
        'true',
      );
      expect(document.documentElement.dataset.lwpColourMode).toBe('dark');
      expect(consoleErrors.flat().join(' ')).not.toContain('hydration');
    } finally {
      if (root) {
        await act(async () => root?.unmount());
      }
      errorSpy.mockRestore();
      container.remove();
    }
  });
});
