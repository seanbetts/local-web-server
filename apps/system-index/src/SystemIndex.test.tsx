import { render, screen } from '@testing-library/react';
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

});
