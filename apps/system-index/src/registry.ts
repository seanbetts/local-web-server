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

const APP_KEYS = [
  'id',
  'title',
  'route',
  'icon',
  'accent',
  'frontendHealthPath',
  'backendHealthPath',
] as const;
const REGISTRY_KEYS = ['schemaVersion', 'apps'] as const;
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

export function parseIndexRegistry(value: unknown): readonly IndexApp[] {
  if (!isObject(value) || !hasExactKeys(value, REGISTRY_KEYS) || value.schemaVersion !== 1) {
    return invalidRegistry();
  }
  if (!Array.isArray(value.apps)) {
    return invalidRegistry();
  }

  const apps = value.apps.map(parseApp);
  if (new Set(apps.map((app) => app.id)).size !== apps.length) {
    return invalidRegistry();
  }
  return apps;
}
