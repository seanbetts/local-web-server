import { describe, expect, it } from 'vitest';

import { parseIndexRegistry } from './registry';

const registry = {
  schemaVersion: 1,
  apps: [
    {
      id: 'plotter',
      title: 'Plotter',
      route: '/plotter/',
      icon: 'route',
      accent: '#75A7FF',
      frontendHealthPath: '/plotter/',
      backendHealthPath: '/_local-web/health/plotter/backend',
    },
    {
      id: 'samplebeta',
      title: 'Sample Workspace',
      route: '/samplebeta/',
      icon: 'shirt-sport',
      accent: '#7A1735',
      frontendHealthPath: '/samplebeta/',
      backendHealthPath: null,
    },
  ],
};

function invalidRegistry(value: unknown) {
  expect(() => parseIndexRegistry(value)).toThrowError('System Index registry is invalid');
}

describe('parseIndexRegistry', () => {
  it('returns the public apps in registry order', () => {
    expect(parseIndexRegistry(registry)).toEqual(registry.apps);
  });

  it('requires schema version 1 and an apps array', () => {
    invalidRegistry({ schemaVersion: 2, apps: [] });
    invalidRegistry({ schemaVersion: 1, apps: {} });
  });

  it('rejects extra or missing registry and app keys', () => {
    invalidRegistry({ ...registry, privateDetail: 'do-not-render' });
    invalidRegistry({ schemaVersion: 1, apps: [{ ...registry.apps[0], privateDetail: 'do-not-render' }] });
    invalidRegistry({ schemaVersion: 1, apps: [{ id: 'plotter' }] });
  });

  it('accepts only canonical app paths and canonical manifest icons', () => {
    expect(parseIndexRegistry({
      schemaVersion: 1,
      apps: [{ ...registry.apps[0], id: 'movement', icon: 'stretching', backendHealthPath: null }],
    })[0].icon).toBe('stretching');
    expect(parseIndexRegistry({
      schemaVersion: 1,
      apps: [{ ...registry.apps[0], id: 'careers', icon: 'briefcase', backendHealthPath: null }],
    })[0].icon).toBe('briefcase');
    expect(parseIndexRegistry({
      schemaVersion: 1,
      apps: [{
        ...registry.apps[0],
        id: 'meals',
        icon: 'tools-kitchen-2',
        backendHealthPath: null,
      }],
    })[0].icon).toBe('tools-kitchen-2');
    invalidRegistry({
      schemaVersion: 1,
      apps: [{ ...registry.apps[0], route: 'https://example.test/plotter/' }],
    });
    invalidRegistry({
      schemaVersion: 1,
      apps: [{ ...registry.apps[0], frontendHealthPath: '/plotter/?refresh=true' }],
    });
    invalidRegistry({
      schemaVersion: 1,
      apps: [{ ...registry.apps[0], backendHealthPath: '/plotter//health' }],
    });
    invalidRegistry({
      schemaVersion: 1,
      apps: [{ ...registry.apps[0], icon: 'car' }],
    });
  });

  it('accepts the complete canonical domain emitted by the authoritative producer', () => {
    const producerCompatibleRegistry = {
      schemaVersion: 1,
      apps: [
        {
          ...registry.apps[0],
          id: 'a--',
          route: '/tools/app/',
          frontendHealthPath: '/tools/app/',
          backendHealthPath: '/tools/app/Health.v1~',
        },
        {
          ...registry.apps[1],
          id: 'a-',
          route: '/1/edge/',
          frontendHealthPath: '/1/edge/',
          backendHealthPath: '/_local-web/health/a-/backend',
        },
      ],
    };

    expect(parseIndexRegistry(producerCompatibleRegistry)).toEqual(producerCompatibleRegistry.apps);
  });

  it('rejects traversal, authorities, invalid encoding, and control characters in canonical paths', () => {
    for (const app of [
      { ...registry.apps[0], route: '/tools/../app/', frontendHealthPath: '/tools/../app/' },
      { ...registry.apps[0], route: '//example.test/app/', frontendHealthPath: '//example.test/app/' },
      { ...registry.apps[0], backendHealthPath: '/plotter/%2E' },
      { ...registry.apps[0], backendHealthPath: '/plotter/healthy\u0000' },
    ]) {
      invalidRegistry({ schemaVersion: 1, apps: [app] });
    }
  });

  it('requires canonical identifiers, uppercase six-digit accents, and unique IDs', () => {
    invalidRegistry({ schemaVersion: 1, apps: [{ ...registry.apps[0], id: 'Plotter' }] });
    invalidRegistry({ schemaVersion: 1, apps: [{ ...registry.apps[0], accent: '#75a7ff' }] });
    invalidRegistry({ schemaVersion: 1, apps: [registry.apps[0], registry.apps[0]] });
  });

  it('never includes rejected registry values in its errors', () => {
    const privateValue = 'private-origin-and-token-value';
    try {
      parseIndexRegistry({
        schemaVersion: 1,
        apps: [{ ...registry.apps[0], privateValue }],
      });
    } catch (error) {
      expect(error).toEqual(new Error('System Index registry is invalid'));
      expect(String(error)).not.toContain(privateValue);
      return;
    }
    throw new Error('Expected invalid registry to throw');
  });
});
