import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { InteractiveExportControl } from './InteractiveExportControl.js';
import type { LocalWebContextV1 } from './contextExportModel.js';
import { INTERACTIVE_EXPORT_PAYLOAD_MARKER } from './interactiveExportDocument.js';
import {
  createCompatibilityId,
  defineInteractiveExportContract,
  type InteractiveExportCapture,
  type InteractiveExportDefinition,
  type InteractiveExportTemplateDescriptor,
} from './interactiveExportModel.js';

const app = {
  id: 'records',
  name: 'Records',
  icon: 'folders' as const,
  accent: '#315D80' as const,
};

const context: LocalWebContextV1 = {
  schema: 'local-web-context/v1',
  app: { id: app.id, name: app.name, version: '1.2.3', sourceRevision: 'abc1234' },
  context: {
    title: 'Records',
    scope: 'Current records.',
    activeRoute: '/',
    generatedAt: '2026-09-08T12:00:00.000Z',
    observedAt: null,
    dataRevision: null,
  },
  sensitivity: { classification: 'sensitive', notice: 'Contains client records.' },
  summary: 'Current records.',
  capabilities: ['context-export'],
  sections: [],
  data: {},
  provenance: { freshness: 'Current.', sources: [] },
  assumptions: [],
  decisions: [],
  caveats: [],
  omissions: [],
};

type RecordsData = { readonly records: readonly { readonly id: string }[] };
type RecordsState = { readonly selectedRecordId: string };

const contract = defineInteractiveExportContract<RecordsData, RecordsState>({
  id: 'record-list',
  version: 1,
  sensitivity: { classification: 'sensitive', notice: 'Contains client records.' },
  decodeSnapshot(value) {
    const capture = value as InteractiveExportCapture<RecordsData, RecordsState>;
    if (!capture.snapshotData.records.some(({ id }) => id === capture.viewState.selectedRecordId)) {
      throw new Error('private decoder detail');
    }
    return capture;
  },
});

const definition = (
  buildSnapshot: InteractiveExportDefinition<RecordsData, RecordsState>['buildSnapshot'] = async () => ({
    title: 'Current records',
    snapshotData: { records: [{ id: 'alpha' }] },
    viewState: { selectedRecordId: 'alpha' },
  }),
): InteractiveExportDefinition<RecordsData, RecordsState> => ({ contract, buildSnapshot });

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
};

const encode = (value: unknown): string => btoa(JSON.stringify(value));

const installTemplate = () => {
  const templateId = 'a'.repeat(64);
  const payloadContractId = 'records/record-list/v1';
  const descriptor: InteractiveExportTemplateDescriptor = {
    templateUrl: `/assets/local-web-interactive-export-${templateId}.html`,
    templateId,
    payloadContractId,
    compatibilityId: createCompatibilityId({ appId: app.id, payloadContractId, templateId }),
    appId: app.id,
    appVersion: '1.2.3',
    sourceRevision: 'abc1234',
  };
  const identity = {
    templateId: descriptor.templateId,
    payloadContractId: descriptor.payloadContractId,
    compatibilityId: descriptor.compatibilityId,
    appId: descriptor.appId,
    appVersion: descriptor.appVersion,
    sourceRevision: descriptor.sourceRevision,
  };
  document.head.insertAdjacentHTML(
    'beforeend',
    `<meta name="local-web-interactive-export-descriptor" content="${encode(descriptor)}">`,
  );
  vi.stubGlobal('fetch', vi.fn(async () => new Response(
    `<!doctype html><html><head><meta name="local-web-interactive-export-template" content="${encode(identity)}"></head><body>${INTERACTIVE_EXPORT_PAYLOAD_MARKER}</body></html>`,
  )));
};

describe('InteractiveExportControl', () => {
  const originalCreateObjectURL = URL.createObjectURL;
  const originalRevokeObjectURL = URL.revokeObjectURL;
  const originalAnchorClick = HTMLAnchorElement.prototype.click;

  afterEach(() => {
    document.head.querySelectorAll('meta[name^="local-web-interactive-export-"]').forEach((element) => element.remove());
    URL.createObjectURL = originalCreateObjectURL;
    URL.revokeObjectURL = originalRevokeObjectURL;
    HTMLAnchorElement.prototype.click = originalAnchorClick;
    vi.unstubAllGlobals();
  });

  it('adds no export markup when neither capability is supplied', () => {
    const { container } = render(<InteractiveExportControl app={app} />);

    expect(container).toBeEmptyDOMElement();
  });

  it('preserves the exact context-only control path', () => {
    render(<InteractiveExportControl app={app} buildContextExport={async () => context} />);

    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Export context' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByText('Contains client records.')).not.toBeInTheDocument();
  });

  it('shows the immutable warning before one-click interactive capture and suppresses duplicate activation', async () => {
    installTemplate();
    const capture = deferred<InteractiveExportCapture<RecordsData, RecordsState>>();
    let signal: AbortSignal | undefined;
    const buildSnapshot = vi.fn(({ signal: requestSignal }: { signal: AbortSignal }) => {
      signal = requestSignal;
      return capture.promise;
    });
    const download = vi.fn();
    URL.createObjectURL = vi.fn(() => 'blob:interactive-export');
    URL.revokeObjectURL = vi.fn();
    HTMLAnchorElement.prototype.click = download;

    render(<InteractiveExportControl app={app} interactiveExport={definition(buildSnapshot)} />);

    expect(screen.getByText('Sensitive')).toBeVisible();
    expect(screen.getByText('Contains client records.')).toBeVisible();
    const action = screen.getByRole('button', { name: 'Export interactive snapshot' });
    expect(action).toHaveAccessibleDescription('Sensitive Contains client records.');
    fireEvent.click(action);
    fireEvent.click(action);

    expect(buildSnapshot).toHaveBeenCalledTimes(1);
    expect(signal?.aborted).toBe(false);
    expect(screen.getByRole('button', { name: 'Exporting interactive snapshot' }))
      .toHaveAttribute('aria-busy', 'true');
    expect(download).not.toHaveBeenCalled();

    await act(async () => {
      capture.resolve({
        title: 'Current records',
        snapshotData: { records: [{ id: 'alpha' }] },
        viewState: { selectedRecordId: 'alpha' },
      });
      await capture.promise;
    });

    await waitFor(() => expect(download).toHaveBeenCalledTimes(1));
    expect(document.querySelector('a[download]')).not.toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:interactive-export');
    expect(screen.getByRole('status')).toHaveTextContent('Interactive snapshot exported.');
  });

  it('opens an accessible two-format chooser and returns focus to Export on close', async () => {
    const buildContextExport = vi.fn(async () => context);
    render(
      <InteractiveExportControl
        app={app}
        buildContextExport={buildContextExport}
        interactiveExport={definition()}
      />,
    );

    const trigger = screen.getByRole('button', { name: 'Export' });
    expect(trigger).not.toHaveFocus();
    fireEvent.click(trigger);

    const dialog = screen.getByRole('dialog', { name: 'Export' });
    expect(dialog).toBeVisible();
    const contextChoice = within(dialog).getByRole('button', { name: 'Context document' });
    expect(contextChoice).toBeEnabled();
    expect(contextChoice).toHaveAccessibleDescription('A script-free, structured, static document.');
    expect(within(dialog).getByText('A script-free, structured, static document.')).toBeVisible();
    const interactiveChoice = within(dialog).getByRole('button', { name: 'Interactive snapshot' });
    expect(interactiveChoice).toBeEnabled();
    expect(interactiveChoice).toHaveAccessibleDescription(
        'A packaged application view with selected private state that works offline. Sensitive Contains client records.',
      );
    expect(within(dialog).getByText('A packaged application view with selected private state that works offline.')).toBeVisible();
    expect(within(dialog).getByText('Sensitive')).toBeVisible();
    expect(within(dialog).getByText('Contains client records.')).toBeVisible();

    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it('downloads the context document selected from the chooser', async () => {
    const download = vi.fn();
    URL.createObjectURL = vi.fn(() => 'blob:context-export');
    URL.revokeObjectURL = vi.fn();
    HTMLAnchorElement.prototype.click = download;
    render(
      <InteractiveExportControl
        app={app}
        buildContextExport={async () => context}
        interactiveExport={definition()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Export' }));
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Context document' }));
    });

    expect(download).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('status')).toHaveTextContent('Context exported.');
  });

  it('shows only a generic public error when interactive generation fails', async () => {
    installTemplate();
    const buildSnapshot = vi.fn(() => Promise.reject(new Error('client Acme account 417')));
    render(<InteractiveExportControl app={app} interactiveExport={definition(buildSnapshot)} />);

    fireEvent.click(screen.getByRole('button', { name: 'Export interactive snapshot' }));

    const alert = await screen.findByRole('alert');
    expect(buildSnapshot).toHaveBeenCalledTimes(1);
    expect(alert).toHaveTextContent('Interactive snapshot could not be created. Review the app state and try again.');
    expect(alert).not.toHaveTextContent('Acme');
    expect(alert).not.toHaveTextContent('417');
  });

  it('silently aborts pending interactive work on unmount and never downloads a late result', async () => {
    installTemplate();
    const capture = deferred<InteractiveExportCapture<RecordsData, RecordsState>>();
    let signal: AbortSignal | undefined;
    const buildSnapshot = vi.fn(({ signal: requestSignal }: { signal: AbortSignal }) => {
      signal = requestSignal;
      return capture.promise;
    });
    const download = vi.fn();
    URL.createObjectURL = vi.fn(() => 'blob:interactive-export');
    URL.revokeObjectURL = vi.fn();
    HTMLAnchorElement.prototype.click = download;

    const view = render(<InteractiveExportControl app={app} interactiveExport={definition(buildSnapshot)} />);
    fireEvent.click(screen.getByRole('button', { name: 'Export interactive snapshot' }));
    view.unmount();

    expect(signal?.aborted).toBe(true);
    await act(async () => {
      capture.resolve({
        snapshotData: { records: [{ id: 'alpha' }] },
        viewState: { selectedRecordId: 'alpha' },
      });
      await capture.promise;
    });
    expect(download).not.toHaveBeenCalled();
  });

  it('keeps a restarted chooser export busy after the cancelled request settles', async () => {
    installTemplate();
    const first = deferred<InteractiveExportCapture<RecordsData, RecordsState>>();
    const second = deferred<InteractiveExportCapture<RecordsData, RecordsState>>();
    const signals: AbortSignal[] = [];
    const buildSnapshot = vi.fn(({ signal }: { signal: AbortSignal }) => {
      signals.push(signal);
      return signals.length === 1 ? first.promise : second.promise;
    });
    const download = vi.fn();
    URL.createObjectURL = vi.fn(() => 'blob:interactive-export');
    URL.revokeObjectURL = vi.fn();
    HTMLAnchorElement.prototype.click = download;
    render(
      <InteractiveExportControl
        app={app}
        buildContextExport={async () => context}
        interactiveExport={definition(buildSnapshot)}
      />,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Export' }));
    fireEvent.click(screen.getByRole('button', { name: 'Interactive snapshot' }));
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(signals[0]?.aborted).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Export' }));
    fireEvent.click(screen.getByRole('button', { name: 'Interactive snapshot' }));

    await act(async () => {
      first.resolve({
        snapshotData: { records: [{ id: 'alpha' }] },
        viewState: { selectedRecordId: 'alpha' },
      });
      await first.promise;
    });

    expect(screen.getByRole('button', { name: 'Interactive snapshot' }))
      .toHaveAttribute('aria-busy', 'true');
    expect(download).not.toHaveBeenCalled();

    await act(async () => {
      second.resolve({
        snapshotData: { records: [{ id: 'alpha' }] },
        viewState: { selectedRecordId: 'alpha' },
      });
      await second.promise;
    });
    await waitFor(() => expect(download).toHaveBeenCalledTimes(1));
  });
});
