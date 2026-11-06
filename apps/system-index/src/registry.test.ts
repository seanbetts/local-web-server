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
    expect(parseIndexRegistry(registry)).toEqual({ apps: registry.apps, services: [] });
  });

  it('requires a supported schema version and an apps array', () => {
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
    }).apps[0].icon).toBe('stretching');
    expect(parseIndexRegistry({
      schemaVersion: 1,
      apps: [{ ...registry.apps[0], id: 'careers', icon: 'briefcase', backendHealthPath: null }],
    }).apps[0].icon).toBe('briefcase');
    expect(parseIndexRegistry({
      schemaVersion: 1,
      apps: [{
        ...registry.apps[0],
        id: 'meals',
        icon: 'tools-kitchen-2',
        backendHealthPath: null,
      }],
    }).apps[0].icon).toBe('tools-kitchen-2');
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

    expect(parseIndexRegistry(producerCompatibleRegistry)).toEqual({ apps: producerCompatibleRegistry.apps, services: [] });
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

const service = {
  id: 'jellyfin', title: 'Jellyfin', url: 'https://local.example.ts.net:8444',
  icon: 'photo', accent: '#75A7FF',
};
const v2 = { schemaVersion: 2, apps: registry.apps, services: [service] };

describe('schema 2 service links', () => {
  it('preserves service URLs and keeps services separate from managed apps', () => {
    expect(parseIndexRegistry(v2)).toEqual({ apps: registry.apps, services: [service] });
    expect(parseIndexRegistry({ ...v2, apps: [], services: [] })).toEqual({ apps: [], services: [] });
  });

  it('rejects duplicate IDs within services and across both collections', () => {
    invalidRegistry({ ...v2, services: [service, service] });
    invalidRegistry({ ...v2, services: [{ ...service, id: registry.apps[0].id }] });
  });

  it('requires exact public service fields and canonical metadata', () => {
    for (const invalid of [
      { ...service, backendHealthPath: '/healthz' }, { ...service, privateValue: 'private' },
      { ...service, id: 'Jellyfin' }, { ...service, title: '' },
      { ...service, title: 'Jellyfin\u0080' }, { ...service, title: 'Jellyfin\n' },
      { ...service, icon: 'car' }, { ...service, accent: '#75a7ff' },
    ]) invalidRegistry({ ...v2, services: [invalid] });
    invalidRegistry({ schemaVersion: 2, apps: [] });
    invalidRegistry({ ...v2, services: {} });
  });

  it('rejects non-HTTPS, credentials, whitespace, controls and malformed service URLs', () => {
    for (const url of [
      'http://local.example.ts.net:8444', '//local.example.ts.net:8444',
      'https://user:secret@local.example.ts.net:8444', 'https://@local.example.ts.net',
      ' https://local.example.ts.net', 'https://local.example.ts.net/path with space',
      'https://local.example.ts.net/\\path', 'https://local.example.ts.net/\u0000',
      'https://local.example.ts.net/\n', 'https://', 'javascript:alert(1)', 'https://local.example.ts.net/?token=secret',
      'https://local.example.ts.net/#fragment', 'https://LOCAL.example.test:8444',
      'https://%6cocal.example.test:8444', 'https://local_example.test:8444',
      'https://local.example.ts.net.:8444', 'https://local.example.ts.net:0',
      'https://local.example.ts.net:65536', 'https://local.example.ts.net:',
      'https://local.example.ts.net:08444', 'https://0x7f.1', 'https://example.123', 'https://999.999.999.999', 'https://[::1]:8444',
      'https://xn--a', 'https://xn--abc', 'https://xn--bcher-kva.example',
    ]) invalidRegistry({ ...v2, services: [{ ...service, url }] });
  });
});
