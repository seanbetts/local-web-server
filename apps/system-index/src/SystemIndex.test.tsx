import { render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { SystemIndex } from './SystemIndex';

const controller = vi.hoisted(() => {
  const stop = vi.fn();
  const start = vi.fn(() => stop);
  return {
    create: vi.fn(() => ({ checkAll: vi.fn(), start })),
    start,
    stop,
  };
});

vi.mock('./statusController', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./statusController')>();
  return { ...actual, createStatusController: controller.create };
});

const registry = {
  schemaVersion: 1,
  apps: [
    {
      id: 'example-archive',
      title: 'Example Archive',
      route: '/example-archive/',
      icon: 'database',
      accent: '#75A7FF',
      frontendHealthPath: '/example-archive/',
      backendHealthPath: null,
    },
    {
      id: 'example-notes',
      title: 'Example Notes',
      route: '/example-notes/',
      icon: 'book',
      accent: '#76D39B',
      frontendHealthPath: '/example-notes/',
      backendHealthPath: '/_local-web/health/example-notes/backend',
    },
    {
      id: 'example-tasks',
      title: 'Example Tasks',
      route: '/example-tasks/',
      icon: 'checklist',
      accent: '#7A1735',
      frontendHealthPath: '/example-tasks/',
      backendHealthPath: '/example-tasks/healthz',
    },
  ],
};

afterEach(() => {
  controller.create.mockClear();
  controller.start.mockClear();
  controller.stop.mockClear();
  vi.unstubAllGlobals();
});

describe('SystemIndex', () => {
  it('shows a loading state inside the shared System Index frame', () => {
    const registryFetch = vi.fn(() => new Promise(() => undefined));
    vi.stubGlobal('fetch', registryFetch);

    render(<SystemIndex />);

    expect(screen.getAllByRole('main')).toHaveLength(1);
    expect(screen.getByRole('navigation', { name: 'Location' })).toHaveTextContent(
      'Local/System Index',
    );
    expect(screen.getByRole('status', { name: 'Loading System Index' })).toBeInTheDocument();
  });

  it('renders a concise sanitized registry error inside the shared frame', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      json: async () => ({
        schemaVersion: 1,
        apps: [],
        privateDetail: 'private-origin-and-token-value',
      }),
    }));

    render(<SystemIndex />);

    const alert = await screen.findByRole('alert', { name: 'System Index unavailable' });
    expect(alert).toHaveTextContent('Refresh the page to try again.');
    expect(alert).not.toHaveTextContent('private-origin-and-token-value');
  });

  it('renders application cards alphabetically by visible title as ordinary explicitly named anchors', async () => {
    const registryFetch = vi.fn().mockResolvedValue({ ok: true, json: async () => registry });
    vi.stubGlobal('fetch', registryFetch);

    render(<SystemIndex />);

    const links = await screen.findAllByRole('link', { name: /Open / });
    expect(screen.getByRole('region', { name: 'System Index' })).toBeInTheDocument();
    expect(screen.getByText('System Index', { selector: 'h1' })).toHaveClass(
      'system-index__visually-hidden',
    );
    expect(links).toHaveLength(3);
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      '/example-archive/',
      '/example-notes/',
      '/example-tasks/',
    ]);
    expect(links.map((link) => link.getAttribute('aria-label'))).toEqual([
      'Open Example Archive',
      'Open Example Notes',
      'Open Example Tasks',
    ]);
    expect(controller.create).toHaveBeenCalledWith(
      expect.anything(),
      registry.apps,
      expect.any(Function),
    );
    expect(registryFetch).toHaveBeenCalledWith('/_local-web/platform/index/registry-v1.json', {
      cache: 'no-store',
    });
    expect(screen.queryByText(/apps$/i)).not.toBeInTheDocument();
    const gallery = screen.getByRole('link', { name: 'UI Gallery' });
    expect(gallery).toHaveAttribute('href', '/_local-web/platform/ui-gallery/');
    expect(gallery).toHaveAttribute('title', 'UI Gallery');
    expect(gallery.querySelector('svg')).toBeInTheDocument();
    expect(gallery.className).toContain('lwp-icon-button');
  });

  it('sorts titles case-insensitively and resolves equivalent titles by app ID', async () => {
    const caseRegistry = {
      schemaVersion: 1,
      apps: [
        {
          ...registry.apps[0],
          id: 'zeta',
          title: 'alpha',
          route: '/zeta/',
          frontendHealthPath: '/zeta/',
          backendHealthPath: null,
        },
        {
          ...registry.apps[1],
          id: 'alpha',
          title: 'Alpha',
          route: '/alpha/',
          frontendHealthPath: '/alpha/',
          backendHealthPath: null,
        },
        {
          ...registry.apps[2],
          id: 'beta',
          title: 'beta',
          route: '/beta/',
          frontendHealthPath: '/beta/',
          backendHealthPath: null,
        },
      ],
    };
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      json: async () => caseRegistry,
    }));

    render(<SystemIndex />);

    const links = await screen.findAllByRole('link', { name: /Open / });
    expect(links.map((link) => link.getAttribute('aria-label'))).toEqual([
      'Open Alpha',
      'Open alpha',
      'Open beta',
    ]);
  });

  it('keeps each card accent and labelled status in the card-owned order', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => registry }));

    render(<SystemIndex />);

    const card = await screen.findByRole('link', { name: 'Open Example Archive' });
    expect(card).toHaveClass('system-index-card');
    expect(card).toHaveStyle({ '--system-index-accent': '#75A7FF' });
    expect(within(card).getByText('01')).toBeInTheDocument();

    const statusLabel = within(card).getByText('STATUS');
    const statusDot = within(card).getByRole('status', { name: 'Checking' });
    const hiddenMeaning = within(card).getByText('Checking');
    expect(statusLabel.compareDocumentPosition(statusDot)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(hiddenMeaning).toHaveClass('system-index__visually-hidden');
    expect(statusDot).toHaveClass('lwp-tone--neutral');
    expect(controller.create).toHaveBeenCalledOnce();
    expect(controller.start).toHaveBeenCalledOnce();
  });

  it('renders each card with the approved meta and centred body anatomy', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => registry }));

    render(<SystemIndex />);

    const card = await screen.findByRole('link', { name: 'Open Example Archive' });
    expect(Array.from(card.children).map((child) => child.className)).toEqual([
      'system-index-card__meta',
      'system-index-card__body',
    ]);
    expect(card.querySelector('.system-index-card__open')).not.toBeInTheDocument();
    expect(card.querySelector('.system-index-card__open-icon')).not.toBeInTheDocument();

    const meta = card.children[0] as HTMLElement;
    expect(within(meta).getByText('01')).toHaveClass('system-index-card__number');
    expect(within(meta).getByText('STATUS')).toHaveClass('system-index-card__status-label');
    expect(within(meta).getByRole('status', { name: 'Checking' })).toBeInTheDocument();

    const body = card.children[1] as HTMLElement;
    expect(body.querySelector('.system-index-card__icon')).toBeInTheDocument();
    expect(within(body).getByRole('heading', { level: 2, name: 'Example Archive' })).toHaveClass(
      'system-index-card__title',
    );

  });
});
