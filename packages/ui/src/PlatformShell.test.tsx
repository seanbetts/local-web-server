import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { PlatformShell } from './PlatformShell';
import type { LocalWebContextV1 } from './contextExportModel.js';
import { defineInteractiveExportContract } from './interactiveExportModel.js';
import * as ui from './index.js';
import { offlineContract, offlineEnvelope } from './test/interactiveExport.js';

const villaIdentity = {
  id: 'villa-shirt-collection',
  name: 'Sample Workspace',
  icon: 'shirt-sport' as const,
  accent: '#7A1735' as const,
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
};

describe('PlatformShell', () => {
  const interactiveExport = {
    contract: defineInteractiveExportContract({
      id: 'collection',
      version: 1,
      sensitivity: { classification: 'sensitive' as const, notice: 'Contains client records.' },
      decodeSnapshot: (value: unknown) => value as {
        snapshotData: Record<string, never>;
        viewState: Record<string, never>;
      },
    }),
    buildSnapshot: async () => ({ snapshotData: {}, viewState: {} }),
  };

  it('omits the context export action when no builder is supplied', () => {
    render(<PlatformShell location={{ kind: 'app', app: villaIdentity }}><h1>Collection</h1></PlatformShell>);

    expect(screen.queryByRole('button', { name: 'Export context' })).not.toBeInTheDocument();
  });

  it('renders the direct interactive export for an app location', () => {
    render(
      <PlatformShell location={{ kind: 'app', app: villaIdentity }} interactiveExport={interactiveExport}>
        Collection
      </PlatformShell>,
    );

    expect(screen.getByRole('button', { name: 'Export interactive snapshot' })).toBeInTheDocument();
    expect(screen.getByText('Contains client records.')).toBeVisible();
  });

  it('aborts a pending export when the shell location changes and never downloads its late result', async () => {
    const result = deferred<LocalWebContextV1>();
    let signal: AbortSignal | undefined;
    const build = vi.fn(({ signal: requestSignal }: { signal: AbortSignal }) => {
      signal = requestSignal;
      return result.promise;
    });
    const download = vi.fn();
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    const originalAnchorClick = HTMLAnchorElement.prototype.click;
    URL.createObjectURL = vi.fn(() => 'blob:context-export');
    URL.revokeObjectURL = vi.fn();
    HTMLAnchorElement.prototype.click = download;

    try {
      const view = render(
        <PlatformShell location={{ kind: 'app', app: villaIdentity }} buildContextExport={build}>
          Collection
        </PlatformShell>,
      );
      fireEvent.click(screen.getByRole('button', { name: 'Export context' }));
      view.rerender(
        <PlatformShell
          location={{
            kind: 'app-page',
            app: villaIdentity,
            appHref: '/samplebeta/',
            pageLabel: 'David Platt',
          }}
          buildContextExport={build}
        >
          David Platt
        </PlatformShell>,
      );

      expect(signal?.aborted).toBe(true);
      await act(async () => {
        result.resolve({
          schema: 'local-web-context/v1',
          app: { id: 'villa-shirt-collection', name: 'Sample Workspace', version: '1.0.0', sourceRevision: 'abc1234' },
          context: { title: 'Collection', scope: 'Current state.', activeRoute: '/', generatedAt: '2026-08-22T10:00:00.000Z', observedAt: null, dataRevision: null },
          sensitivity: { classification: 'private', notice: 'For local use only.' },
          summary: 'Ready.', capabilities: [], sections: [], data: {}, provenance: { freshness: 'Current.', sources: [] }, assumptions: [], decisions: [], caveats: [], omissions: [],
        });
        await result.promise;
      });
      expect(download).not.toHaveBeenCalled();
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
      HTMLAnchorElement.prototype.click = originalAnchorClick;
    }
  });

  it('aborts a pending export between distinct app pages whose old owner strings collided', async () => {
    const result = deferred<LocalWebContextV1>();
    let signal: AbortSignal | undefined;
    const build = vi.fn(({ signal: requestSignal }: { signal: AbortSignal }) => {
      signal = requestSignal;
      return result.promise;
    });
    const download = vi.fn();
    const originalCreateObjectURL = URL.createObjectURL;
    const originalRevokeObjectURL = URL.revokeObjectURL;
    const originalAnchorClick = HTMLAnchorElement.prototype.click;
    URL.createObjectURL = vi.fn(() => 'blob:context-export');
    URL.revokeObjectURL = vi.fn();
    HTMLAnchorElement.prototype.click = download;

    try {
      const view = render(
        <PlatformShell
          location={{
            kind: 'app-page',
            app: villaIdentity,
            appHref: '/samplebeta',
            pageLabel: 'David:Platt',
          }}
          buildContextExport={build}
        >
          David Platt
        </PlatformShell>,
      );
      fireEvent.click(screen.getByRole('button', { name: 'Export context' }));
      view.rerender(
        <PlatformShell
          location={{
            kind: 'app-page',
            app: villaIdentity,
            appHref: '/samplebeta:David',
            pageLabel: 'Platt',
          }}
          buildContextExport={build}
        >
          David Platt
        </PlatformShell>,
      );

      expect(signal?.aborted).toBe(true);
      await act(async () => {
        result.resolve({
          schema: 'local-web-context/v1',
          app: { id: 'villa-shirt-collection', name: 'Sample Workspace', version: '1.0.0', sourceRevision: 'abc1234' },
          context: { title: 'Collection', scope: 'Current state.', activeRoute: '/', generatedAt: '2026-08-22T10:00:00.000Z', observedAt: null, dataRevision: null },
          sensitivity: { classification: 'private', notice: 'For local use only.' },
          summary: 'Ready.', capabilities: [], sections: [], data: {}, provenance: { freshness: 'Current.', sources: [] }, assumptions: [], decisions: [], caveats: [], omissions: [],
        });
        await result.promise;
      });
      expect(download).not.toHaveBeenCalled();
    } finally {
      URL.createObjectURL = originalCreateObjectURL;
      URL.revokeObjectURL = originalRevokeObjectURL;
      HTMLAnchorElement.prototype.click = originalAnchorClick;
    }
  });

  it('marks System Index as the current location without a redundant link', () => {
    const { container } = render(
      <PlatformShell location={{ kind: 'index' }} headerUtility={<a href="/help">Help</a>}>
        <h1>System Index</h1>
      </PlatformShell>,
    );

    expect(screen.getByRole('link', { name: 'Help' })).toHaveAttribute('href', '/help');
    const location = screen.getByRole('navigation', { name: 'Location' });
    expect(within(location).getByText('Local')).toBeInTheDocument();
    expect(within(location).getByText('System Index')).toHaveAttribute('aria-current', 'page');
    expect(within(location).queryByRole('link')).not.toBeInTheDocument();
    expect(container.querySelector('.lwp-platform-shell'))
      .toHaveClass('lwp-platform-shell--contained');
    expect(container.querySelector('.lwp-platform-shell')).not.toHaveAttribute('data-lwp-app-id');
    expect(container.querySelector('.lwp-platform-shell')).not.toHaveAttribute('style');
    expect(screen.getByRole('main')).toHaveClass('lwp-platform-shell__main--contained');
  });

  it('links from an app location back to System Index and marks the app current', () => {
    const { container } = render(
      <PlatformShell
        location={{
          kind: 'app',
          app: villaIdentity,
        }}
      >
        <h1>Collection</h1>
      </PlatformShell>,
    );

    expect(screen.getByRole('link', { name: 'System Index' })).toHaveAttribute('href', '/');
    expect(screen.getByText('Sample Workspace')).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(container.querySelector('.lwp-platform-shell'))
      .toHaveAttribute('data-lwp-app-id', 'villa-shirt-collection');
    expect(container.querySelector('.lwp-platform-shell')).not.toHaveAttribute('style');
  });

  it('links an app-page location through the app root and marks the page current', () => {
    const { container } = render(
      <PlatformShell
        location={{
          kind: 'app-page',
          app: villaIdentity,
          appHref: '/samplebeta/',
          pageLabel: 'David Platt',
        }}
      >
        <h1>David Platt</h1>
      </PlatformShell>,
    );

    const location = screen.getByRole('navigation', { name: 'Location' });
    expect(within(location).getByRole('link', { name: 'System Index' })).toHaveAttribute('href', '/');
    expect(within(location).getByRole('link', { name: 'Sample Workspace' }))
      .toHaveAttribute('href', '/samplebeta/');
    expect(within(location).getByText('David Platt')).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'Skip to David Platt content' })).toHaveAttribute(
      'href',
      '#lwp-main',
    );
    expect(container.querySelector('.lwp-platform-shell'))
      .toHaveAttribute('data-lwp-app-id', 'villa-shirt-collection');
    expect(container.querySelector('.lwp-platform-shell')).not.toHaveAttribute('style');
  });

  it('applies edge-to-edge mode to the root and main frame', () => {
    const { container } = render(
      <PlatformShell location={{ kind: 'index' }} contentMode="edge-to-edge">
        <h1>System Index</h1>
      </PlatformShell>,
    );

    expect(container.querySelector('.lwp-platform-shell'))
      .toHaveClass('lwp-platform-shell--edge-to-edge');
    expect(screen.getByRole('main')).toHaveClass('lwp-platform-shell__main--edge-to-edge');
  });
});
it('makes offline platform location inert and removes hosted utility/export controls', () => {
  expect(ui.InteractiveExportEnvironmentProvider).toBeTypeOf('function');
  render(
    <ui.InteractiveExportEnvironmentProvider envelope={offlineEnvelope()}>
      <ui.PlatformShell location={{ kind: 'app-page', app: villaIdentity, appHref: '/samplebeta/', pageLabel: 'Briefing' }}
        headerUtility={<button>Hosted utility</button>}
        buildContextExport={async () => { throw new Error('Must not run'); }}
        interactiveExport={{ contract: offlineContract, buildSnapshot: async () => offlineEnvelope() }}>
        Frozen content
      </ui.PlatformShell>
    </ui.InteractiveExportEnvironmentProvider>,
  );
  expect(within(screen.getByRole('navigation', { name: 'Location' })).queryAllByRole('link')).toHaveLength(0);
  expect(screen.queryAllByRole('button', { name: /export|Hosted utility/i })).toHaveLength(0);
  expect(screen.getByText('Offline snapshot')).toBeVisible();
  expect(screen.getByRole('link', { name: 'Skip to Briefing content' })).toHaveAttribute('href', '#lwp-main');
});
