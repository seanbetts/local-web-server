import type { PropsWithChildren, ReactElement, ReactNode } from 'react';

import type { PlatformIconName } from './Icon.js';
import { ThemeControl } from './ThemeControl.js';
import { InteractiveExportControl } from './InteractiveExportControl.js';
import type { ContextExportBuilder, ContextJson } from './contextExportModel.js';
import type { InteractiveExportDefinition } from './interactiveExportModel.js';
import { OfflineSnapshotNotice, useInteractiveExportEnvironment } from './interactiveExportEnvironment.js';

declare const __LOCAL_WEB_UI_VERSION__: string;

export type AppIdentity = {
  id: string;
  name: string;
  icon: PlatformIconName;
  accent: `#${string}`;
};

export type PlatformContentMode = 'contained' | 'edge-to-edge';

export type PlatformLocation =
  | { kind: 'index' }
  | { kind: 'app'; app: AppIdentity }
  | {
      kind: 'app-page';
      app: AppIdentity;
      appHref: string;
      pageLabel: string;
    };

export type AppPageLocation = {
  appHref: string;
  label: string;
};

export type PlatformShellProps = PropsWithChildren<{
  location: PlatformLocation;
  contentMode?: PlatformContentMode;
  headerUtility?: ReactNode;
  buildContextExport?: ContextExportBuilder;
  interactiveExport?: InteractiveExportDefinition<ContextJson, ContextJson>;
}>;

export function PlatformVersion(): ReactElement {
  const label = `Local Web UI version ${__LOCAL_WEB_UI_VERSION__}`;
  return (
    <span className="lwp-platform-version" aria-label={label} title={label}>
      v{__LOCAL_WEB_UI_VERSION__}
    </span>
  );
}

const exportControlOwner = (location: PlatformLocation): string => {
  if (location.kind === 'index') {
    return JSON.stringify(['index']);
  }
  if (location.kind === 'app') {
    return JSON.stringify(['app', location.app.id]);
  }
  return JSON.stringify([
    'app-page',
    location.app.id,
    location.appHref,
    location.pageLabel,
  ]);
};

export function PlatformShell({
  location,
  contentMode = 'contained',
  headerUtility,
  buildContextExport,
  interactiveExport,
  children,
}: PlatformShellProps): ReactElement {
  const offline = useInteractiveExportEnvironment() !== null;
  const indexCurrent = location.kind === 'index';
  const contentLabel = indexCurrent
    ? 'System Index'
    : location.kind === 'app'
      ? location.app.name
      : location.pageLabel;
  return (
    <div
      className={`lwp-root lwp-platform-shell lwp-platform-shell--${contentMode}`}
      data-lwp-app-id={indexCurrent ? undefined : location.app.id}
    >
      <a className="lwp-skip-link" href="#lwp-main">
        Skip to {contentLabel} content
      </a>
      <header className="lwp-platform-shell__header">
        <nav aria-label="Location">
          <ol className="lwp-platform-shell__crumbs">
            <li>Local</li>
            <li aria-hidden="true">/</li>
            <li>
              {offline ? (
                <span>Offline snapshot</span>
              ) : indexCurrent ? (
                <span aria-current="page">System Index</span>
              ) : (
                <a href="/">System Index</a>
              )}
            </li>
            {location.kind === 'app' ? (
              <>
                <li aria-hidden="true">/</li>
                <li><span aria-current="page">{location.app.name}</span></li>
              </>
            ) : location.kind === 'app-page' ? (
              <>
                <li aria-hidden="true">/</li>
                <li>
                  {offline ? <span>{location.app.name}</span> : <a href={location.appHref}>{location.app.name}</a>}
                </li>
                <li aria-hidden="true">/</li>
                <li><span aria-current="page">{location.pageLabel}</span></li>
              </>
            ) : null}
          </ol>
        </nav>
        <div className="lwp-platform-shell__header-actions">
          <PlatformVersion />
          {!offline ? headerUtility : null}
          {!offline && (buildContextExport || interactiveExport) && !indexCurrent ? (
            <InteractiveExportControl
              key={exportControlOwner(location)}
              app={location.app}
              buildContextExport={buildContextExport}
              interactiveExport={interactiveExport}
            />
          ) : null}
          <ThemeControl />
        </div>
        <OfflineSnapshotNotice />
      </header>
      <main
        className={`lwp-platform-shell__main lwp-platform-shell__main--${contentMode}`}
        id="lwp-main"
      >
        {children}
      </main>
    </div>
  );
}
