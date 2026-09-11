import { describe, expect, it } from 'vitest';

import type { IndexApp } from './registry';
import {
  createStatusController,
  type StatusEnvironment,
} from './statusController';

const frontendOnlyApp: IndexApp = {
  id: 'plotter',
  title: 'Plotter',
  route: '/plotter/',
  icon: 'route',
  accent: '#75A7FF',
  frontendHealthPath: '/plotter/',
  backendHealthPath: null,
};

const backendApp: IndexApp = {
  ...frontendOnlyApp,
  backendHealthPath: '/_local-web/health/plotter/backend',
};

type FetchResult = { ok: boolean };
type ProbeOptions = Parameters<StatusEnvironment['fetch']>[1];
type AbortSignalWithListener = ProbeOptions['signal'] & {
  addEventListener: (event: 'abort', listener: () => void) => void;
};

function createDeferred() {
  let resolve: (value: FetchResult) => void = () => undefined;
  let reject: (reason?: unknown) => void = () => undefined;
  const promise = new Promise<FetchResult>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, reject, resolve };
}

class FakeAbortController {
  private readonly listeners: Array<() => void> = [];

  readonly signal = {
    aborted: false,
    addEventListener: (event: 'abort', listener: () => void) => {
      if (event === 'abort') {
        this.listeners.push(listener);
      }
    },
  };

  abort() {
    this.signal.aborted = true;
    this.listeners.forEach((listener) => listener());
  }
}

function createHarness(
  fetchImplementation: (path: string, options: ProbeOptions) => Promise<FetchResult>,
  apps: readonly IndexApp[] = [frontendOnlyApp],
) {
  const fetchCalls: Array<{ options: ProbeOptions; path: string }> = [];
  const updates: Array<{ appId: string; status: string }> = [];
  const timeouts: Array<{ callback: () => void; cleared: boolean; delay: number }> = [];
  const interval: { callback: () => void; cleared: boolean; delay: number } = {
    callback: () => undefined,
    cleared: false,
    delay: 0,
  };
  let visibilityListener: () => void = () => undefined;
  let visibilityListenerRemoved = false;
  const document: StatusEnvironment['document'] = {
    visibilityState: 'visible',
    addEventListener: (event, listener) => {
      if (event === 'visibilitychange') {
        visibilityListener = listener;
      }
    },
    removeEventListener: (event, listener) => {
      if (event === 'visibilitychange' && listener === visibilityListener) {
        visibilityListenerRemoved = true;
        visibilityListener = () => undefined;
      }
    },
  };
  const environment: StatusEnvironment = {
    AbortController: FakeAbortController,
    clearInterval: (timer) => {
      expect(timer).toBe(interval);
      interval.cleared = true;
    },
    clearTimeout: (timer) => {
      (timer as { cleared: boolean }).cleared = true;
    },
    document,
    fetch: (path, options) => {
      fetchCalls.push({ options, path });
      return fetchImplementation(path, options);
    },
    setInterval: (callback, delay) => {
      interval.callback = callback;
      interval.delay = delay;
      return interval;
    },
    setTimeout: (callback, delay) => {
      const timer = { callback, cleared: false, delay };
      timeouts.push(timer);
      return timer;
    },
  };
  const controller = createStatusController(environment, apps, (appId, status) => {
    updates.push({ appId, status });
  });
  return {
    controller,
    document,
    fetchCalls,
    interval,
    latestStatus: (appId: string) => updates.filter((update) => update.appId === appId).at(-1)?.status,
    timeouts,
    visibilityListenerRemoved: () => visibilityListenerRemoved,
    visibilityListener: () => visibilityListener(),
  };
}

function expectHeadProbe(call: { options: ProbeOptions; path: string }, path: string) {
  expect(call.path).toBe(path);
  expect(call.options).toMatchObject({
    cache: 'no-store',
    method: 'HEAD',
    redirect: 'error',
  });
  expect(call.options.signal).toBeDefined();
}

async function flush() {
  await new Promise<void>((resolve) => {
    globalThis.setTimeout(resolve, 0);
  });
}

describe('createStatusController', () => {
  it('uses a bounded HEAD-only frontend probe without reading a response body', async () => {
    const harness = createHarness(async () => ({
      ok: true,
      json: () => { throw new Error('response body read'); },
    }));

    await harness.controller.checkAll();

    expect(harness.fetchCalls).toHaveLength(1);
    expectHeadProbe(harness.fetchCalls[0], '/plotter/');
    expect(harness.timeouts).toHaveLength(1);
    expect(harness.timeouts[0]?.delay).toBe(3000);
    expect(harness.timeouts[0]?.cleared).toBe(true);
    expect(harness.latestStatus('plotter')).toBe('online');
  });

  for (const outcome of ['rejection', 'non-2xx', 'redirect', 'timeout'] as const) {
    it(`classifies a ${outcome} backend probe as frontend-only`, async () => {
      const harness = createHarness((path, options) => {
        if (path === '/plotter/') {
          return Promise.resolve({ ok: true });
        }
        if (outcome === 'non-2xx') {
          return Promise.resolve({ ok: false });
        }
        if (outcome === 'timeout') {
          return new Promise<FetchResult>((_resolve, reject) => {
            (options.signal as AbortSignalWithListener).addEventListener('abort', () => {
              reject(new Error('timed out'));
            });
          });
        }
        return Promise.reject(new Error(outcome));
      }, [backendApp]);

      const sweep = harness.controller.checkAll();
      if (outcome === 'timeout') {
        await flush();
        harness.timeouts.at(-1)?.callback();
      }
      await sweep;

      expect(harness.fetchCalls).toHaveLength(2);
      expectHeadProbe(harness.fetchCalls[0], '/plotter/');
      expectHeadProbe(harness.fetchCalls[1], '/_local-web/health/plotter/backend');
      expect(harness.latestStatus('plotter')).toBe('frontend-only');
    });
  }

  for (const outcome of ['rejection', 'non-2xx', 'redirect', 'timeout'] as const) {
    it(`classifies a ${outcome} frontend probe as offline without probing backend`, async () => {
      const harness = createHarness((_path, options) => {
        if (outcome === 'non-2xx') {
          return Promise.resolve({ ok: false });
        }
        if (outcome === 'timeout') {
          return new Promise<FetchResult>((_resolve, reject) => {
            (options.signal as AbortSignalWithListener).addEventListener('abort', () => {
              reject(new Error('timed out'));
            });
          });
        }
        return Promise.reject(new Error(outcome));
      }, [backendApp]);

      const sweep = harness.controller.checkAll();
      if (outcome === 'timeout') {
        harness.timeouts[0]?.callback();
      }
      await sweep;

      expect(harness.fetchCalls).toHaveLength(1);
      expect(harness.latestStatus('plotter')).toBe('offline');
    });
  }

  it('settles each app independently when another app probe fails', async () => {
    const offlineApp = { ...backendApp, id: 'offline', frontendHealthPath: '/offline/' };
    const onlineApp = { ...frontendOnlyApp, id: 'online', frontendHealthPath: '/online/' };
    const harness = createHarness(
      async (path) => ({ ok: path === '/online/' }),
      [offlineApp, onlineApp],
    );

    await harness.controller.checkAll();

    expect(harness.latestStatus('offline')).toBe('offline');
    expect(harness.latestStatus('online')).toBe('online');
  });

  it('does not overlap sweeps and schedules one visible recovery after a 30-second interval', async () => {
    const firstResponse = createDeferred();
    let requests = 0;
    const harness = createHarness(() => {
      requests += 1;
      return requests === 1 ? firstResponse.promise : Promise.resolve({ ok: true });
    });

    const stop = harness.controller.start();
    expect(harness.fetchCalls).toHaveLength(1);
    expect(harness.interval.delay).toBe(30000);

    await harness.controller.checkAll();
    expect(harness.fetchCalls).toHaveLength(1);

    harness.document.visibilityState = 'hidden';
    harness.interval.callback();
    harness.document.visibilityState = 'visible';
    harness.visibilityListener();
    harness.visibilityListener();
    firstResponse.resolve({ ok: true });
    await flush();

    expect(harness.fetchCalls).toHaveLength(2);
    stop();
    expect(harness.interval.cleared).toBe(true);
  });

  it('consumes a queued visible refresh when the page hides before its active sweep settles', async () => {
    const firstResponse = createDeferred();
    let requests = 0;
    const harness = createHarness(() => {
      requests += 1;
      return requests === 1 ? firstResponse.promise : Promise.resolve({ ok: true });
    });

    const stop = harness.controller.start();
    harness.visibilityListener();
    harness.document.visibilityState = 'hidden';
    firstResponse.resolve({ ok: true });
    await flush();

    expect(harness.fetchCalls).toHaveLength(1);

    harness.document.visibilityState = 'visible';
    harness.visibilityListener();
    await flush();

    expect(harness.fetchCalls).toHaveLength(2);
    stop();
  });

  it('removes its visibility listener and does not run queued or post-stop sweeps', async () => {
    const firstResponse = createDeferred();
    let requests = 0;
    const harness = createHarness(() => {
      requests += 1;
      return requests === 1 ? firstResponse.promise : Promise.resolve({ ok: true });
    });

    const stop = harness.controller.start();
    harness.visibilityListener();
    stop();

    expect(harness.interval.cleared).toBe(true);
    expect(harness.visibilityListenerRemoved()).toBe(true);

    firstResponse.resolve({ ok: true });
    await flush();
    harness.interval.callback();
    harness.visibilityListener();
    await flush();

    expect(harness.fetchCalls).toHaveLength(1);
  });
});
