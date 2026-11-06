import { PLATFORM_MANIFEST_ICON_NAMES } from '@local-web/ui';
import type { PlatformIconName } from '@local-web/ui';

export type IndexApp = {
  id: string;
  title: string;
  route: string;
  icon: PlatformIconName;
  accent: `#${string}`;
  frontendHealthPath: string;
  backendHealthPath: string | null;
};

export type IndexService = Pick<IndexApp, 'id' | 'title' | 'icon' | 'accent'> & { url: string };
export type IndexRegistry = { apps: readonly IndexApp[]; services: readonly IndexService[] };

const APP_KEYS = [
  'id',
  'title',
  'route',
  'icon',
  'accent',
  'frontendHealthPath',
  'backendHealthPath',
] as const;
const SERVICE_KEYS = ['id', 'title', 'url', 'icon', 'accent'] as const;
const REGISTRY_V1_KEYS = ['schemaVersion', 'apps'] as const;
const REGISTRY_V2_KEYS = ['schemaVersion', 'apps', 'services'] as const;
const APP_ID = /^[a-z][a-z0-9-]*$/;
const APP_ROUTE = /^\/[a-z0-9][a-z0-9/-]*\/$/;
const SERVICE_HEALTH_PATH = /^\/[A-Za-z0-9._~/-]*$/;
const ACCENT = /^#[0-9A-F]{6}$/;
const INDEX_ICON_NAMES = new Set<PlatformIconName>(PLATFORM_MANIFEST_ICON_NAMES);

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const valueKeys = Object.keys(value);
  return valueKeys.length === keys.length && keys.every((key) => valueKeys.includes(key));
}

function isObject(value: unknown): value is Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return false;
  }
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasCanonicalPathSegments(path: string): boolean {
  return !path.includes('//')
    && !path.includes('..')
    && !path.split('/').includes('.');
}

function isCanonicalAppRoute(path: string): boolean {
  return APP_ROUTE.test(path) && hasCanonicalPathSegments(path);
}

function isCanonicalBackendHealthPath(path: string, app: IndexApp): boolean {
  if (path === `/_local-web/health/${app.id}/backend`) {
    return true;
  }
  if (!SERVICE_HEALTH_PATH.test(path) || !hasCanonicalPathSegments(path)) {
    return false;
  }
  const appPath = app.route.slice(0, -1);
  return path === appPath
    || path.startsWith(`${appPath}/`);
}

function invalidRegistry(): never {
  throw new Error('System Index registry is invalid');
}

function parseApp(value: unknown): IndexApp {
  if (!isObject(value) || !hasExactKeys(value, APP_KEYS)) {
    return invalidRegistry();
  }
  const {
    id,
    title,
    route,
    icon,
    accent,
    frontendHealthPath,
    backendHealthPath,
  } = value;
  if (
    typeof id !== 'string'
    || !APP_ID.test(id)
    || typeof title !== 'string'
    || title.length === 0
    || typeof route !== 'string'
    || !isCanonicalAppRoute(route)
    || typeof icon !== 'string'
    || !INDEX_ICON_NAMES.has(icon as PlatformIconName)
    || typeof accent !== 'string'
    || !ACCENT.test(accent)
    || typeof frontendHealthPath !== 'string'
    || frontendHealthPath !== route
    || !isCanonicalAppRoute(frontendHealthPath)
    || (backendHealthPath !== null && typeof backendHealthPath !== 'string')
  ) {
    return invalidRegistry();
  }

  const app: IndexApp = {
    id,
    title,
    route,
    icon: icon as PlatformIconName,
    accent: accent as `#${string}`,
    frontendHealthPath,
    backendHealthPath,
  };
  if (app.backendHealthPath !== null && !isCanonicalBackendHealthPath(app.backendHealthPath, app)) {
    return invalidRegistry();
  }
  return app;
}

function hasControlCharacters(value: string): boolean {
  return Array.from(value).some((character) => character.charCodeAt(0) < 32
    || (character.charCodeAt(0) >= 127 && character.charCodeAt(0) <= 159));
}

function isServiceUrl(value: unknown): value is string {
  if (typeof value !== 'string' || /[\s\\]/u.test(value) || hasControlCharacters(value)) {
    return false;
  }
  try {
    const url = new URL(value);
    const authority = value.split('/')[2];
    const [hostname, port] = (authority ?? '').split(':');
    const dnsLabel = /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/;
    if (!hostname || !hostname.split('.').every((label) => dnsLabel.test(label) && !label.startsWith('xn--'))
      || (port !== undefined && (!/^[1-9]\d*$/.test(port) || Number(port) > 65535))) {
      return false;
    }
    return value.startsWith('https://') && url.protocol === 'https:' && url.hostname.length > 0
      && url.hostname === hostname
      && url.username === '' && url.password === '' && !authority?.includes('@')
      && !value.includes('?') && !value.includes('#');
  } catch {
    return false;
  }
}

function parseService(value: unknown): IndexService {
  if (!isObject(value) || !hasExactKeys(value, SERVICE_KEYS)) {
    return invalidRegistry();
  }
  const { id, title, url, icon, accent } = value;
  if (typeof id !== 'string' || !APP_ID.test(id)
    || typeof title !== 'string' || title.length === 0 || hasControlCharacters(title)
    || !isServiceUrl(url)
    || typeof icon !== 'string' || !INDEX_ICON_NAMES.has(icon as PlatformIconName)
    || typeof accent !== 'string' || !ACCENT.test(accent)) {
    return invalidRegistry();
  }
  return { id, title, url, icon: icon as PlatformIconName, accent: accent as `#${string}` };
}

export function parseIndexRegistry(value: unknown): IndexRegistry {
  if (!isObject(value) || (value.schemaVersion !== 1 && value.schemaVersion !== 2)
    || !hasExactKeys(value, value.schemaVersion === 1 ? REGISTRY_V1_KEYS : REGISTRY_V2_KEYS)
    || !Array.isArray(value.apps)
    || (value.schemaVersion === 2 && !Array.isArray(value.services))) {
    return invalidRegistry();
  }
  const apps = value.apps.map(parseApp);
  const services = value.schemaVersion === 2 ? (value.services as unknown[]).map(parseService) : [];
  const ids = [...apps, ...services].map((item) => item.id);
  if (new Set(ids).size !== ids.length) {
    return invalidRegistry();
  }
  return { apps, services };
}
