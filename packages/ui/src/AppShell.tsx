import type { FocusEvent, PropsWithChildren, ReactNode } from 'react';

import {
  PlatformShell,
  PlatformVersion,
  type AppIdentity,
  type AppPageLocation,
  type PlatformContentMode,
  type PlatformLocation,
} from './PlatformShell.js';
import { Icon } from './Icon.js';
import { ThemeControl } from './ThemeControl.js';
import { InteractiveExportControl } from './InteractiveExportControl.js';
import type { ContextExportBuilder, ContextJson } from './contextExportModel.js';
import type { InteractiveExportDefinition } from './interactiveExportModel.js';
import { OfflineSnapshotNotice, useInteractiveExportEnvironment } from './interactiveExportEnvironment.js';

export type { AppIdentity } from './PlatformShell.js';

export type AppShellProps = PropsWithChildren<{
  app: AppIdentity;
  contentMode?: PlatformContentMode;
  page?: AppPageLocation;
  /** @deprecated Use app content navigation with PlatformShell instead. */
  navigation?: ReactNode;
  /** @deprecated Use app content actions with PlatformShell instead. */
  actions?: ReactNode;
  buildContextExport?: ContextExportBuilder;
  /** Supplies the hosted interactive snapshot capture for the shared export control. */
  interactiveExport?: InteractiveExportDefinition<ContextJson, ContextJson>;
}>;

const revealFocusedAction = (event: FocusEvent<HTMLDivElement>) => {
  event.target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
};

function LegacyAppShell({
  app,
  contentMode,
  navigation,
  actions,
  buildContextExport,
  interactiveExport,
  children,
}: AppShellProps) {
  const offline = useInteractiveExportEnvironment() !== null;
  return (
    <div className="lwp-root lwp-app-shell" data-lwp-app-id={app.id}>
      <a className="lwp-skip-link" href="#lwp-main">
        Skip to {app.name} content
      </a>
      <header className="lwp-app-shell__header">
        {offline ? (
          <span className="lwp-app-shell__all-apps">Offline snapshot</span>
        ) : (
          <a className="lwp-app-shell__all-apps" href="/" aria-label="All apps">
            <Icon name="apps" />
            <span>All apps</span>
          </a>
        )}
        <div className="lwp-app-shell__identity">
          <Icon className="lwp-app-shell__identity-icon" name={app.icon} />
          <span>{app.name}</span>
        </div>
        {navigation && !offline ? (
          <nav className="lwp-app-shell__navigation" aria-label={`${app.name} navigation`}>
            {navigation}
          </nav>
        ) : null}
        <div className="lwp-app-shell__end">
          <PlatformVersion />
          {actions && !offline ? (
            <div
              className="lwp-app-shell__actions-viewport"
              onFocusCapture={revealFocusedAction}
            >
              <div className="lwp-app-shell__actions" role="group" aria-label={`${app.name} actions`}>
                {actions}
              </div>
            </div>
          ) : null}
          {!offline ? (
            <InteractiveExportControl
              app={app}
              buildContextExport={buildContextExport}
              interactiveExport={interactiveExport}
            />
          ) : null}
          <ThemeControl />
        </div>
      </header>
      <OfflineSnapshotNotice />
      <main
        className={
          contentMode === undefined
            ? undefined
            : `lwp-platform-shell__main lwp-platform-shell__main--${contentMode}`
        }
        id="lwp-main"
      >
        {children}
      </main>
    </div>
  );
}

export function AppShell(props: AppShellProps) {
  const { app, contentMode, page, navigation, actions, buildContextExport, interactiveExport, children } = props;
  const usesLegacySlots = Object.hasOwn(props, 'navigation') || Object.hasOwn(props, 'actions');

  if (usesLegacySlots) {
    return (
      <LegacyAppShell
        app={app}
        contentMode={contentMode}
        navigation={navigation}
        actions={actions}
        buildContextExport={buildContextExport}
        interactiveExport={interactiveExport}
      >
        {children}
      </LegacyAppShell>
    );
  }

  const location: PlatformLocation = page
    ? { kind: 'app-page', app, appHref: page.appHref, pageLabel: page.label }
    : { kind: 'app', app };

  return (
    <div className="lwp-root lwp-app-shell" data-lwp-app-id={app.id}>
      <PlatformShell
        location={location}
        contentMode={contentMode ?? 'contained'}
        buildContextExport={buildContextExport}
        interactiveExport={interactiveExport}
      >
        {children}
      </PlatformShell>
    </div>
  );
}
