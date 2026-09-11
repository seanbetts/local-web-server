import type { CSSProperties } from 'react';
import { useEffect, useState } from 'react';

import {
  ErrorState,
  Icon,
  LoadingState,
  PlatformShell,
  StatusDot,
} from '@local-web/ui';

import type { IndexApp } from './registry';
import { parseIndexRegistry } from './registry';
import {
  createStatusController,
  type AppStatus,
  type StatusEnvironment,
} from './statusController';
import './styles.css';

const INDEX_REGISTRY_ROUTE = '/_local-web/platform/index/registry-v1.json';
const APP_TITLE_COLLATOR = new Intl.Collator('en-GB', { sensitivity: 'base' });

type IndexState =
  | { kind: 'loading' }
  | { kind: 'ready'; apps: readonly IndexApp[] }
  | { kind: 'error' };

type StatusPresentation = {
  label: string;
  tone: 'danger' | 'neutral' | 'success' | 'warning';
};

const STATUS_PRESENTATION: Record<AppStatus, StatusPresentation> = {
  checking: { label: 'Checking', tone: 'neutral' },
  online: { label: 'Online', tone: 'success' },
  'frontend-only': { label: 'Frontend only', tone: 'warning' },
  offline: { label: 'Offline', tone: 'danger' },
};

function browserStatusEnvironment(): StatusEnvironment {
  return {
    AbortController,
    clearInterval: (timer) => window.clearInterval(timer as number),
    clearTimeout: (timer) => window.clearTimeout(timer as number),
    document: {
      addEventListener: (_event, listener) => document.addEventListener('visibilitychange', listener),
      removeEventListener: (_event, listener) => document.removeEventListener('visibilitychange', listener),
      get visibilityState() {
        return document.visibilityState === 'visible' ? 'visible' : 'hidden';
      },
    },
    fetch: (path, options) => window.fetch(path, {
      cache: options.cache,
      method: options.method,
      redirect: options.redirect,
      signal: options.signal as AbortSignal,
    }),
    setInterval: (callback, delay) => window.setInterval(callback, delay),
    setTimeout: (callback, delay) => window.setTimeout(callback, delay),
  };
}

function cardStyle(accent: string): CSSProperties {
  return { '--system-index-accent': accent } as CSSProperties;
}

function sortAppsForDisplay(apps: readonly IndexApp[]): readonly IndexApp[] {
  return [...apps].sort((left, right) => {
    const titleOrder = APP_TITLE_COLLATOR.compare(left.title, right.title);
    if (titleOrder !== 0) {
      return titleOrder;
    }
    return left.id < right.id ? -1 : left.id > right.id ? 1 : 0;
  });
}

function SystemIndexCard({ app, index, status }: {
  app: IndexApp;
  index: number;
  status: AppStatus;
}) {
  const presentation = STATUS_PRESENTATION[status];
  return (
    <a
      className="system-index-card"
      href={app.route}
      aria-label={`Open ${app.title}`}
      data-state={status}
      style={cardStyle(app.accent)}
    >
      <div className="system-index-card__meta">
        <span className="system-index-card__number">{String(index + 1).padStart(2, '0')}</span>
        <span className="system-index-card__status">
          <span className="system-index-card__status-label">STATUS</span>
          <StatusDot label={presentation.label} tone={presentation.tone} />
          <span className="system-index__visually-hidden">{presentation.label}</span>
        </span>
      </div>
      <div className="system-index-card__body">
        <Icon className="system-index-card__icon" name={app.icon} />
        <h2 className="system-index-card__title">{app.title}</h2>
      </div>
    </a>
  );
}

export function SystemIndex() {
  const [state, setState] = useState<IndexState>({ kind: 'loading' });
  const [statuses, setStatuses] = useState<Record<string, AppStatus>>({});

  useEffect(() => {
    let active = true;
    let stop: () => void = () => undefined;

    async function loadRegistry() {
      try {
        const response = await fetch(INDEX_REGISTRY_ROUTE, { cache: 'no-store' });
        if (!response.ok) {
          throw new Error('Registry request failed');
        }
        const apps = parseIndexRegistry(await response.json());
        if (!active) {
          return;
        }
        setStatuses(Object.fromEntries(apps.map((app) => [app.id, 'checking'] as const)));
        setState({ kind: 'ready', apps: sortAppsForDisplay(apps) });
        stop = createStatusController(browserStatusEnvironment(), apps, (appId, status) => {
          if (active) {
            setStatuses((current) => ({ ...current, [appId]: status }));
          }
        }).start();
      } catch {
        if (active) {
          setState({ kind: 'error' });
        }
      }
    }

    void loadRegistry();
    return () => {
      active = false;
      stop();
    };
  }, []);

  return (
    <PlatformShell
      location={{ kind: 'index' }}
      headerUtility={
        <a
          className="lwp-button lwp-button--secondary lwp-icon-button system-index__gallery-link"
          href="/_local-web/platform/ui-gallery/"
          aria-label="UI Gallery"
          title="UI Gallery"
        >
          <Icon name="apps" />
        </a>
      }
    >
      {state.kind === 'loading' ? (
        <LoadingState title="Loading System Index" message="Loading registered applications." />
      ) : null}
      {state.kind === 'error' ? (
        <ErrorState title="System Index unavailable" message="Refresh the page to try again." />
      ) : null}
      {state.kind === 'ready' ? (
        <section className="system-index" aria-label="System Index">
          <h1 className="system-index__visually-hidden">System Index</h1>
          <div className="system-index__grid">
            {state.apps.map((app, index) => (
              <SystemIndexCard
                key={app.id}
                app={app}
                index={index}
                status={statuses[app.id] ?? 'checking'}
              />
            ))}
          </div>
        </section>
      ) : null}
    </PlatformShell>
  );
}
