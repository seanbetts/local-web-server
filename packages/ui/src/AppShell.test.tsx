import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AppShell } from './AppShell';
import * as ui from './index.js';
import { offlineContract, offlineEnvelope } from './test/interactiveExport.js';
import type { ContextExportBuilder } from './contextExportModel.js';
import { defineInteractiveExportContract } from './interactiveExportModel.js';

const villaIdentity = {
  id: 'villa-shirt-collection',
  name: 'Sample Workspace',
  icon: 'shirt-sport' as const,
  accent: '#7A1735' as const,
};

describe('AppShell', () => {
  it.each([false, true])('suppresses hosted shell exports and navigation offline (legacy: %s)', (legacy) => {
    expect(ui.InteractiveExportEnvironmentProvider).toBeTypeOf('function');
    render(
      <ui.InteractiveExportEnvironmentProvider envelope={offlineEnvelope()}>
        <AppShell app={villaIdentity} page={{ appHref: '/samplebeta/', label: 'Briefing' }}
          {...(legacy ? { navigation: <a href="/samplebeta/">Sample Workspace</a>, actions: <button>Hosted export</button> } : {})}
          buildContextExport={buildContextExport}
          interactiveExport={{ contract: offlineContract, buildSnapshot: async () => offlineEnvelope() }}>
          Frozen content
        </AppShell>
      </ui.InteractiveExportEnvironmentProvider>,
    );
    expect(screen.queryAllByRole('link', { name: /^(System Index|All apps|Sample Workspace|Sample Workspace)$/ })).toHaveLength(0);
    expect(screen.queryByText('System Index')).not.toBeInTheDocument();
    expect(screen.queryByText('All apps')).not.toBeInTheDocument();
    expect(screen.queryAllByRole('button', { name: /export/i })).toHaveLength(0);
    expect(screen.getByLabelText('Local Web UI version 0.7.2')).toHaveTextContent('v0.7.2');
    expect(screen.getByText('Offline snapshot')).toBeVisible();
    expect(screen.getByText(/Sensitive/)).toBeVisible();
    expect(screen.getByText(/Captured 8 September 2026/)).toBeVisible();
    expect(screen.getByText(/abc1234/)).toBeVisible();
    expect(screen.getByText(/Contains private briefing data/)).toBeVisible();
    expect(screen.getByText(/frozen/i, { selector: '.lwp-offline-notice *' })).toBeVisible();
    expect(screen.getByRole('main')).toHaveTextContent('Frozen content');
  });
  const buildContextExport: ContextExportBuilder = async () => ({
    schema: 'local-web-context/v1', app: villaIdentity, context: { title: 'Collection', scope: 'Current state.', activeRoute: '/', generatedAt: '2026-08-22T10:00:00.000Z', observedAt: null, dataRevision: null }, sensitivity: { classification: 'private', notice: 'For local use only.' }, summary: 'Ready.', capabilities: [], sections: [], data: {}, provenance: { freshness: 'Current.', sources: [] }, assumptions: [], decisions: [], caveats: [], omissions: [],
  });
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

  it('forwards the edge-to-edge content mode to the shared frame', () => {
    render(
      <AppShell app={villaIdentity} contentMode="edge-to-edge">
        Collection
      </AppShell>,
    );

    expect(screen.getByRole('main')).toHaveClass('lwp-platform-shell__main--edge-to-edge');
  });

  it('forwards context export to the modern shared frame', () => {
    render(<AppShell app={villaIdentity} buildContextExport={buildContextExport}>Collection</AppShell>);

    expect(screen.getByRole('button', { name: 'Export context' })).toBeInTheDocument();
  });

  it('offers both export formats in the modern shared frame', () => {
    render(
      <AppShell
        app={villaIdentity}
        buildContextExport={buildContextExport}
        interactiveExport={interactiveExport}
      >
        Collection
      </AppShell>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Export' }));
    const dialog = screen.getByRole('dialog', { name: 'Export' });
    expect(within(dialog).getByRole('button', { name: 'Context document' })).toBeEnabled();
    expect(within(dialog).getByRole('button', { name: 'Interactive snapshot' })).toBeEnabled();
    expect(within(dialog).getByText('Contains client records.')).toBeVisible();
  });

  it('offers the direct interactive export in the compatibility frame', () => {
    render(
      <AppShell app={villaIdentity} navigation={null} interactiveExport={interactiveExport}>
        Collection
      </AppShell>,
    );

    expect(screen.getByRole('button', { name: 'Export interactive snapshot' })).toBeInTheDocument();
    expect(screen.getByText('Contains client records.')).toBeVisible();
  });

  it('adapts a page to linked app and current page breadcrumbs', () => {
    render(
      <AppShell
        app={villaIdentity}
        page={{ appHref: '/samplebeta/', label: 'David Platt' }}
      >
        <h1>David Platt</h1>
      </AppShell>,
    );

    const location = screen.getByRole('navigation', { name: 'Location' });
    expect(within(location).getByRole('link', { name: 'Sample Workspace' }))
      .toHaveAttribute('href', '/samplebeta/');
    expect(within(location).getByText('David Platt')).toHaveAttribute('aria-current', 'page');
  });

  it('preserves deprecated navigation, actions, and shell hooks for existing consumers', () => {
    const { container } = render(
      <AppShell
        app={villaIdentity}
        navigation={<a href="#samplebeta">Sample Workspace</a>}
        actions={<button type="button">Export</button>}
      >
        Collection
      </AppShell>,
    );

    const shell = container.querySelector('.lwp-app-shell');
    expect(shell).toHaveClass('lwp-root');
    expect(shell).toHaveAttribute('data-lwp-app-id', 'villa-shirt-collection');
    expect(shell).not.toHaveAttribute('style');
    expect(screen.getByRole('link', { name: 'All apps' })).toHaveAttribute('href', '/');
    expect(screen.getByRole('navigation', { name: 'Sample Workspace navigation' }))
      .toHaveTextContent('Sample Workspace');
    expect(screen.getByRole('group', { name: 'Sample Workspace actions' }))
      .toHaveTextContent('Export');
    const end = container.querySelector('.lwp-app-shell__end');
    const version = screen.getByLabelText('Local Web UI version 0.7.2');
    const actions = screen.getByRole('group', { name: 'Sample Workspace actions' });
    expect(version).toHaveTextContent('v0.7.2');
    expect(version.parentElement).toBe(end);
    expect([...end!.children].slice(0, 2)).toEqual([version, actions.parentElement]);

    const main = screen.getByRole('main');
    expect(main).toHaveTextContent('Collection');
    expect(main).not.toHaveAttribute('class');
  });

  it('keeps the compatibility frame mounted while a supplied action becomes empty and returns', () => {
    const { container, rerender } = render(
      <AppShell app={villaIdentity} actions={<button type="button">Export</button>}>
        Collection
      </AppShell>,
    );

    const compatibilityHeader = container.querySelector('.lwp-app-shell__header');
    expect(compatibilityHeader).not.toBeNull();

    rerender(
      <AppShell app={villaIdentity} actions={null}>
        Collection
      </AppShell>,
    );

    expect(container.querySelector('.lwp-app-shell__header')).toBe(compatibilityHeader);
    expect(container.querySelector('.lwp-platform-shell__header')).not.toBeInTheDocument();
    expect(screen.queryByRole('group', { name: 'Sample Workspace actions' }))
      .not.toBeInTheDocument();

    rerender(
      <AppShell app={villaIdentity} actions={<button type="button">Share</button>}>
        Collection
      </AppShell>,
    );

    expect(container.querySelector('.lwp-app-shell__header')).toBe(compatibilityHeader);
    expect(screen.getByRole('group', { name: 'Sample Workspace actions' }))
      .toHaveTextContent('Share');
  });

  it('applies contained content mode with legacy slots when explicitly requested', () => {
    render(
      <AppShell
        app={villaIdentity}
        contentMode="contained"
        navigation={<a href="#samplebeta">Sample Workspace</a>}
        actions={<button type="button">Export</button>}
      >
        Collection
      </AppShell>,
    );

    expect(screen.getByRole('main')).toHaveClass('lwp-platform-shell__main--contained');
    expect(screen.getByRole('main')).not.toHaveClass('lwp-platform-shell__main--edge-to-edge');
  });

  it('applies edge-to-edge content mode with legacy slots when explicitly requested', () => {
    render(
      <AppShell
        app={villaIdentity}
        contentMode="edge-to-edge"
        navigation={<a href="#samplebeta">Sample Workspace</a>}
        actions={<button type="button">Export</button>}
      >
        Collection
      </AppShell>,
    );

    expect(screen.getByRole('main')).toHaveClass('lwp-platform-shell__main--edge-to-edge');
    expect(screen.getByRole('main')).not.toHaveClass('lwp-platform-shell__main--contained');
  });
});
