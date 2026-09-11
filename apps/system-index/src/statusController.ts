import type { IndexApp } from './registry';

export type AppStatus = 'checking' | 'online' | 'frontend-only' | 'offline';

export type StatusAbortSignal = {
  readonly aborted: boolean;
};

export type StatusEnvironment = {
  AbortController: new () => {
    abort: () => void;
    signal: StatusAbortSignal;
  };
  clearInterval: (timer: unknown) => void;
  clearTimeout: (timer: unknown) => void;
  document: {
    addEventListener: (event: 'visibilitychange', listener: () => void) => void;
    removeEventListener: (event: 'visibilitychange', listener: () => void) => void;
    visibilityState: 'hidden' | 'visible';
  };
  fetch: (path: string, options: {
    cache: 'no-store';
    method: 'HEAD';
    redirect: 'error';
    signal: StatusAbortSignal;
  }) => Promise<{ ok: boolean }>;
  setInterval: (callback: () => void, delay: number) => unknown;
  setTimeout: (callback: () => void, delay: number) => unknown;
};

const PROBE_TIMEOUT_MS = 3000;
const CHECK_INTERVAL_MS = 30000;

async function probe(environment: StatusEnvironment, path: string): Promise<boolean> {
  const controller = new environment.AbortController();
  const timeout = environment.setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
  try {
    const response = await environment.fetch(path, {
      method: 'HEAD',
      cache: 'no-store',
      redirect: 'error',
      signal: controller.signal,
    });
    return response.ok;
  } catch {
    return false;
  } finally {
    environment.clearTimeout(timeout);
  }
}

export function createStatusController(
  environment: StatusEnvironment,
  apps: readonly IndexApp[],
  update: (appId: string, status: AppStatus) => void,
): { checkAll(): Promise<void>; start(): () => void } {
  let stopped = false;
  let sweepActive = false;
  let pendingVisibleRefresh = false;

  async function checkApp(app: IndexApp): Promise<void> {
    update(app.id, 'checking');
    if (!await probe(environment, app.frontendHealthPath)) {
      update(app.id, 'offline');
      return;
    }
    if (app.backendHealthPath === null || await probe(environment, app.backendHealthPath)) {
      update(app.id, 'online');
      return;
    }
    update(app.id, 'frontend-only');
  }

  async function checkAll(): Promise<void> {
    if (stopped || sweepActive) {
      return;
    }
    sweepActive = true;
    try {
      await Promise.all(apps.map(checkApp));
    } finally {
      const refreshAfterSweep = pendingVisibleRefresh;
      pendingVisibleRefresh = false;
      sweepActive = false;
      if (!stopped && refreshAfterSweep && environment.document.visibilityState === 'visible') {
        await checkAll();
      }
    }
  }

  function start(): () => void {
    stopped = false;
    const runSweep = () => {
      if (!stopped) {
        void checkAll();
      }
    };
    const visibilityListener = () => {
      if (stopped || environment.document.visibilityState !== 'visible') {
        return;
      }
      if (sweepActive) {
        pendingVisibleRefresh = true;
        return;
      }
      runSweep();
    };

    runSweep();
    const interval = environment.setInterval(() => {
      if (environment.document.visibilityState === 'visible') {
        runSweep();
      }
    }, CHECK_INTERVAL_MS);
    environment.document.addEventListener('visibilitychange', visibilityListener);

    return () => {
      stopped = true;
      pendingVisibleRefresh = false;
      environment.clearInterval(interval);
      environment.document.removeEventListener('visibilitychange', visibilityListener);
    };
  }

  return { checkAll, start };
}
