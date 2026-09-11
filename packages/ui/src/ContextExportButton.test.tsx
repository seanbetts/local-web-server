import { StrictMode } from 'react';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { LocalWebContextV1 } from './contextExportModel.js';
import { ContextExportButton } from './ContextExportButton';

const context: LocalWebContextV1 = {
  schema: 'local-web-context/v1',
  app: { id: 'gallery', name: 'UI Gallery', version: '1.0.0', sourceRevision: 'abc1234' },
  context: {
    title: 'Gallery actions',
    scope: 'The current Gallery action state.',
    activeRoute: '/',
    generatedAt: '2026-08-22T10:00:00.000Z',
    observedAt: null,
    dataRevision: null,
  },
  sensitivity: { classification: 'private', notice: 'For local use only.' },
  summary: 'The shared component gallery is available.',
  capabilities: ['context-export'],
  sections: [],
  data: {},
  provenance: { freshness: 'Current at generation time.', sources: [] },
  assumptions: [],
  decisions: [],
  caveats: [],
  omissions: [],
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
};

describe('ContextExportButton', () => {
  const originalCreateObjectURL = URL.createObjectURL;
  const originalRevokeObjectURL = URL.revokeObjectURL;
  const originalAnchorClick = HTMLAnchorElement.prototype.click;

  afterEach(() => {
    URL.createObjectURL = originalCreateObjectURL;
    URL.revokeObjectURL = originalRevokeObjectURL;
    HTMLAnchorElement.prototype.click = originalAnchorClick;
  });

  it('runs one live export, suppresses duplicate clicks, downloads only after success, and returns to idle', async () => {
    const result = deferred<LocalWebContextV1>();
    const buildContextExport = vi.fn(({ signal }: { signal: AbortSignal }) => {
      expect(signal).toBeInstanceOf(AbortSignal);
      expect(signal.aborted).toBe(false);
      return result.promise;
    });
    const download = vi.fn();
    URL.createObjectURL = vi.fn(() => 'blob:context-export');
    URL.revokeObjectURL = vi.fn();
    HTMLAnchorElement.prototype.click = download;

    render(<ContextExportButton buildContextExport={buildContextExport} />);

    const action = screen.getByRole('button', { name: 'Export context' });
    fireEvent.click(action);
    fireEvent.click(action);

    expect(buildContextExport).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Exporting context' })).toHaveAttribute('aria-busy', 'true');
    expect(download).not.toHaveBeenCalled();

    await act(async () => {
      result.resolve(context);
      await result.promise;
    });

    expect(download).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('status')).toHaveTextContent('Context exported.');
    expect(screen.getByRole('button', { name: 'Export context' })).not.toHaveAttribute('aria-busy');
  });

  it('keeps private failures out of the alert', async () => {
    const buildContextExport = vi.fn(() => Promise.reject(new Error('private export detail')));
    render(<ContextExportButton buildContextExport={buildContextExport} />);

    fireEvent.click(screen.getByRole('button', { name: 'Export context' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Context export could not be created. Review the app context and try again.',
    );
    expect(screen.getByRole('alert')).not.toHaveTextContent('private export detail');
  });

  it('aborts pending work on unmount and never downloads its late completion', async () => {
    const result = deferred<LocalWebContextV1>();
    let signal: AbortSignal | undefined;
    const buildContextExport = vi.fn(({ signal: requestSignal }: { signal: AbortSignal }) => {
      signal = requestSignal;
      return result.promise;
    });
    const download = vi.fn();
    URL.createObjectURL = vi.fn(() => 'blob:context-export');
    URL.revokeObjectURL = vi.fn();
    HTMLAnchorElement.prototype.click = download;

    const view = render(<ContextExportButton buildContextExport={buildContextExport} />);
    fireEvent.click(screen.getByRole('button', { name: 'Export context' }));
    view.unmount();

    expect(signal?.aborted).toBe(true);
    await act(async () => {
      result.resolve(context);
      await result.promise;
    });
    expect(download).not.toHaveBeenCalled();
  });

  it('remains live after StrictMode replays its effect setup and cleanup', async () => {
    const buildContextExport = vi.fn(async () => context);
    const download = vi.fn();
    URL.createObjectURL = vi.fn(() => 'blob:context-export');
    URL.revokeObjectURL = vi.fn();
    HTMLAnchorElement.prototype.click = download;

    render(
      <StrictMode>
        <ContextExportButton buildContextExport={buildContextExport} />
      </StrictMode>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Export context' }));

    await waitFor(() => expect(download).toHaveBeenCalledTimes(1));
    expect(screen.getByRole('button', { name: 'Export context' })).not.toHaveAttribute('aria-busy');
  });
});
