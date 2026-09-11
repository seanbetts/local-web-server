import {
  INTERACTIVE_EXPORT_PAYLOAD_MARKER,
  buildInteractiveExportArtifact,
  downloadInteractiveExport,
  readInteractiveExportTemplateDescriptor,
} from './interactiveExportDocument.js';
import { act } from '@testing-library/react';
import type { Root } from 'react-dom/client';
import * as publicUi from './index.js';
import {
  MAX_INTERACTIVE_EXPORT_ARTIFACT_BYTES,
  MAX_INTERACTIVE_EXPORT_SNAPSHOT_BYTES,
  createCompatibilityId,
  createPayloadContractId,
  defineInteractiveExportContract,
} from './interactiveExportModel.js';
import type {
  InteractiveExportDefinition,
  InteractiveExportTemplateDescriptor,
} from './interactiveExportModel.js';

const TEMPLATE_META_NAME = 'local-web-interactive-export-template';
const DESCRIPTOR_META_NAME = 'local-web-interactive-export-descriptor';
const PRIVATE_VALUE = 'private-record-value-51da08';

type RecordsData = { readonly records: readonly { readonly id: string }[] };
type RecordsState = { readonly selectedRecordId: string };

const encode = (value: unknown): string => btoa(JSON.stringify(value));

function descriptor(templateId = 'a'.repeat(64)): InteractiveExportTemplateDescriptor {
  const payloadContractId = 'records/record-list/v1';
  return {
    templateUrl: `/assets/local-web-interactive-export-${templateId}.html`,
    templateId,
    payloadContractId,
    compatibilityId: createCompatibilityId({ appId: 'records', payloadContractId, templateId }),
    appId: 'records',
    appVersion: '1.2.3',
    sourceRevision: 'abc1234',
  };
}

function template(value = descriptor()): string {
  const identity = {
    templateId: value.templateId,
    payloadContractId: value.payloadContractId,
    compatibilityId: value.compatibilityId,
    appId: value.appId,
    appVersion: value.appVersion,
    sourceRevision: value.sourceRevision,
  };
  return `<!doctype html><html><head><meta name="${TEMPLATE_META_NAME}" content="${encode(identity)}"></head><body>${INTERACTIVE_EXPORT_PAYLOAD_MARKER}</body></html>`;
}

function definition(): InteractiveExportDefinition<RecordsData, RecordsState> {
  const contract = defineInteractiveExportContract<RecordsData, RecordsState>({
    id: 'record-list',
    version: 1,
    sensitivity: { classification: 'sensitive', notice: 'Contains client records.' },
    decodeSnapshot(value) {
      const capture = value as { snapshotData: RecordsData; viewState: RecordsState; title?: string };
      if (!capture.snapshotData.records.some(({ id }) => id === capture.viewState.selectedRecordId)) {
        throw new Error('the selected record is absent');
      }
      return capture;
    },
  });
  expect(createPayloadContractId('records', contract)).toBe('records/record-list/v1');
  return {
    contract,
    async buildSnapshot() {
      return {
        title: 'Current / Records',
        snapshotData: { records: [{ id: 'alpha' }] },
        viewState: { selectedRecordId: 'alpha' },
      };
    },
  };
}

function decodeTemplatePayload(html: string): unknown {
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  const encoded = parsed.querySelector<HTMLTemplateElement>('template[data-local-web-interactive-export-payload]')?.content.textContent;
  if (encoded === undefined || encoded === null) throw new Error('payload was not embedded');
  return JSON.parse(atob(encoded));
}

async function expectArtifactMountsOffline(
  artifact: { readonly html: string },
  exportDefinition: InteractiveExportDefinition<RecordsData, RecordsState>,
): Promise<void> {
  const parsed = new DOMParser().parseFromString(artifact.html, 'text/html');
  const payload = parsed.querySelector('template[data-local-web-interactive-export-payload]');
  if (!payload) throw new Error('artifact payload was not embedded');
  document.head.innerHTML = parsed.head.innerHTML;
  document.body.innerHTML = `<div id="root"></div>${payload.outerHTML}`;
  let root: Root | undefined;
  try {
    await act(async () => {
      root = publicUi.mountInteractiveExport({
        contract: exportDefinition.contract,
        render: (capture) => `${capture.snapshotData.records[0]?.id}:${capture.viewState.selectedRecordId}`,
      });
    });
    expect(document.body).toHaveTextContent('alpha:alpha');
  } finally {
    await act(async () => root?.unmount());
    document.head.replaceChildren();
    document.body.replaceChildren();
  }
}

function options(overrides: Partial<Parameters<typeof buildInteractiveExportArtifact<RecordsData, RecordsState>>[0]> = {}) {
  return {
    app: { id: 'records', name: 'Records' },
    definition: definition(),
    descriptor: descriptor(),
    effectiveColourMode: 'dark' as const,
    fetchTemplate: async () => new Response(template()),
    now: () => new Date('2026-09-08T12:00:00.000Z'),
    ...overrides,
  };
}

describe('readInteractiveExportTemplateDescriptor', () => {
  afterEach(() => document.head.replaceChildren());

  it('reads one exact framework descriptor meta element', () => {
    const expected = descriptor();
    document.head.innerHTML = `<meta name="${DESCRIPTOR_META_NAME}" content="${encode(expected)}">`;

    expect(readInteractiveExportTemplateDescriptor()).toEqual(expected);
  });

  it.each([
    ['missing', ''],
    ['duplicated', `<meta name="${DESCRIPTOR_META_NAME}" content="${encode(descriptor())}"><meta name="${DESCRIPTOR_META_NAME}" content="${encode(descriptor())}">`],
    ['malformed', `<meta name="${DESCRIPTOR_META_NAME}" content="not-base64">`],
  ])('rejects a %s hosted descriptor safely', (_name, markup) => {
    document.head.innerHTML = markup;

    expect(() => readInteractiveExportTemplateDescriptor()).toThrowError(
      expect.objectContaining({ code: 'template-incompatible', message: 'interactive export template is incompatible' }),
    );
  });

  it.each([
    ['a mutable latest path', '/assets/interactive-export-latest.html'],
    ['a query string', `/assets/local-web-interactive-export-${'a'.repeat(64)}.html?version=next`],
    ['a fragment', `/assets/local-web-interactive-export-${'a'.repeat(64)}.html#next`],
  ])('rejects a same-origin template URL with %s', (_name, templateUrl) => {
    const mutable = { ...descriptor(), templateUrl };
    document.head.innerHTML = `<meta name="${DESCRIPTOR_META_NAME}" content="${encode(mutable)}">`;

    expect(() => readInteractiveExportTemplateDescriptor()).toThrowError(
      expect.objectContaining({ code: 'template-incompatible', message: 'interactive export template is incompatible' }),
    );
  });

  it('accepts the content-addressed asset below an application base path', () => {
    const expected = { ...descriptor(), templateUrl: `/records/assets/local-web-interactive-export-${'a'.repeat(64)}.html` };
    document.head.innerHTML = `<meta name="${DESCRIPTOR_META_NAME}" content="${encode(expected)}">`;

    expect(readInteractiveExportTemplateDescriptor()).toEqual(expected);
  });
});

describe('buildInteractiveExportArtifact', () => {
  it('decodes the capture before fetching its exact template and embeds a canonical envelope', async () => {
    const fetchTemplate = vi.fn(async () => new Response(template()));
    const artifact = await buildInteractiveExportArtifact(options({ fetchTemplate }));

    expect(fetchTemplate).toHaveBeenCalledOnce();
    expect(fetchTemplate).toHaveBeenCalledWith('/assets/local-web-interactive-export-'.concat('a'.repeat(64), '.html'), expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(artifact.html).not.toContain(INTERACTIVE_EXPORT_PAYLOAD_MARKER);
    expect(decodeTemplatePayload(artifact.html)).toMatchObject({
      capture: { effectiveColourMode: 'dark', capturedAt: '2026-09-08T12:00:00.000Z' },
      snapshotData: { records: [{ id: 'alpha' }] },
      viewState: { selectedRecordId: 'alpha' },
    });
    expect(artifact.filename).toBe('records--current-records--2026-09-08.html');
    expect(artifact.byteLength).toBe(new TextEncoder().encode(artifact.html).byteLength);
  });

  it('does not fetch when the application decoder rejects its live capture', async () => {
    const fetchTemplate = vi.fn(async () => new Response(template()));
    const invalid = definition();
    invalid.buildSnapshot = async () => ({
      snapshotData: { records: [{ id: 'alpha' }] },
      viewState: { selectedRecordId: 'missing' },
    });

    await expect(buildInteractiveExportArtifact(options({ definition: invalid, fetchTemplate }))).rejects.toThrowError(
      expect.objectContaining({ code: 'invalid-snapshot', message: 'interactive export snapshot is invalid' }),
    );
    expect(fetchTemplate).not.toHaveBeenCalled();
  });

  it.each([
    ['buildSnapshot', (candidate: InteractiveExportDefinition<RecordsData, RecordsState>) => {
      candidate.buildSnapshot = async () => { throw new publicUi.InteractiveExportError('artifact-oversized', PRIVATE_VALUE); };
    }, 'capture-failed', 'interactive export capture failed'],
    ['decodeSnapshot', (candidate: InteractiveExportDefinition<RecordsData, RecordsState>) => {
      candidate.contract = { ...candidate.contract, decodeSnapshot: () => { throw new publicUi.InteractiveExportError('capture-failed', PRIVATE_VALUE); } };
    }, 'invalid-snapshot', 'interactive export snapshot is invalid'],
  ] as const)('sanitizes framework-shaped private %s failures at the app boundary', async (_name, mutate, code, message) => {
    const candidate = definition();
    mutate(candidate);
    const fetchTemplate = vi.fn(async () => new Response(template()));

    let thrown: unknown;
    try {
      await buildInteractiveExportArtifact(options({ definition: candidate, fetchTemplate }));
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toEqual(expect.objectContaining({ code, message }));
    expect(JSON.stringify(thrown)).not.toContain(PRIVATE_VALUE);
    expect((thrown as Error).stack).not.toContain(PRIVATE_VALUE);
    expect((thrown as Error & { cause?: unknown }).cause).toBeUndefined();
    expect(fetchTemplate).not.toHaveBeenCalled();
  });

  it('embeds the pre-fetch valid capture when template retrieval mutates its record relationship', async () => {
    const exportDefinition = definition();
    const liveCapture = {
      snapshotData: { records: [{ id: 'alpha' }] },
      viewState: { selectedRecordId: 'alpha' },
    };
    exportDefinition.buildSnapshot = async () => liveCapture;

    const artifact = await publicUi.buildInteractiveExportArtifact(options({
      definition: exportDefinition,
      fetchTemplate: async () => {
        liveCapture.snapshotData.records[0]!.id = 'bravo';
        return new Response(template());
      },
    }));

    expect(decodeTemplatePayload(artifact.html)).toMatchObject({
      snapshotData: { records: [{ id: 'alpha' }] },
      viewState: { selectedRecordId: 'alpha' },
    });
    await expectArtifactMountsOffline(artifact, exportDefinition);
  });

  it('uses a proxy record descriptor once instead of serializing its later bravo value', async () => {
    const exportDefinition = definition();
    let recordReads = 0;
    const record = new Proxy({ id: 'alpha' }, {
      get(target, property, receiver) {
        if (property === 'id') {
          recordReads += 1;
          return recordReads === 1 ? 'alpha' : 'bravo';
        }
        return Reflect.get(target, property, receiver);
      },
    });
    exportDefinition.buildSnapshot = async () => ({
      snapshotData: { records: [record] },
      viewState: { selectedRecordId: 'alpha' },
    });

    const artifact = await publicUi.buildInteractiveExportArtifact(options({ definition: exportDefinition }));

    expect(decodeTemplatePayload(artifact.html)).toMatchObject({
      snapshotData: { records: [{ id: 'alpha' }] },
      viewState: { selectedRecordId: 'alpha' },
    });
    expect(recordReads).toBe(0);
    await expectArtifactMountsOffline(artifact, exportDefinition);
  });

  it('uses a proxy padding descriptor once instead of serializing later 5,242,880-byte growth', async () => {
    const exportDefinition = definition();
    let paddingReads = 0;
    const snapshotData = new Proxy({ records: [{ id: 'alpha' }], padding: '' }, {
      get(target, property, receiver) {
        if (property === 'padding') {
          paddingReads += 1;
          return paddingReads === 1 ? '' : 'x'.repeat(MAX_INTERACTIVE_EXPORT_SNAPSHOT_BYTES);
        }
        return Reflect.get(target, property, receiver);
      },
    });
    exportDefinition.buildSnapshot = async () => ({
      snapshotData,
      viewState: { selectedRecordId: 'alpha' },
    });

    const artifact = await publicUi.buildInteractiveExportArtifact(options({ definition: exportDefinition }));

    expect(decodeTemplatePayload(artifact.html)).toMatchObject({
      snapshotData: { records: [{ id: 'alpha' }], padding: '' },
      viewState: { selectedRecordId: 'alpha' },
    });
    expect(paddingReads).toBe(0);
  });

  it('never embeds post-fetch growth beyond the 5,242,880-byte snapshot limit', async () => {
    const exportDefinition = definition();
    const liveCapture = {
      snapshotData: { records: [{ id: 'alpha' }] } as { records: { id: string }[]; padding?: string },
      viewState: { selectedRecordId: 'alpha' },
    };
    exportDefinition.buildSnapshot = async () => liveCapture;

    const artifact = await publicUi.buildInteractiveExportArtifact(options({
      definition: exportDefinition,
      fetchTemplate: async () => {
        liveCapture.snapshotData.padding = 'x'.repeat(MAX_INTERACTIVE_EXPORT_SNAPSHOT_BYTES);
        return new Response(template());
      },
    }));

    expect(decodeTemplatePayload(artifact.html)).toMatchObject({
      snapshotData: { records: [{ id: 'alpha' }] },
      viewState: { selectedRecordId: 'alpha' },
    });
    expect(decodeTemplatePayload(artifact.html)).not.toHaveProperty('snapshotData.padding');
  });

  it('stops before capture and template work when aborted', async () => {
    const controller = new AbortController();
    controller.abort();
    const fetchTemplate = vi.fn(async () => new Response(template()));
    const buildSnapshot = vi.fn(async () => ({ snapshotData: { records: [{ id: 'alpha' }] }, viewState: { selectedRecordId: 'alpha' } }));
    const aborted = definition();
    aborted.buildSnapshot = buildSnapshot;

    await expect(buildInteractiveExportArtifact(options({ definition: aborted, fetchTemplate, signal: controller.signal }))).rejects.toThrow();
    expect(buildSnapshot).not.toHaveBeenCalled();
    expect(fetchTemplate).not.toHaveBeenCalled();
  });

  it('maps template HTTP failures to a safe framework error', async () => {
    await expect(buildInteractiveExportArtifact(options({
      fetchTemplate: async () => new Response(PRIVATE_VALUE, { status: 404 }),
    }))).rejects.toThrowError(expect.objectContaining({ code: 'template-unavailable', message: 'interactive export template is unavailable' }));
    await expect(buildInteractiveExportArtifact(options({
      fetchTemplate: async () => new Response(PRIVATE_VALUE, { status: 404 }),
    }))).rejects.not.toThrow(PRIVATE_VALUE);
  });

  it('maps a thrown template fetch to an unavailable-template error without private detail', async () => {
    let thrown: unknown;
    try {
      await buildInteractiveExportArtifact(options({
        fetchTemplate: async () => { throw new Error(PRIVATE_VALUE); },
      }));
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toEqual(expect.objectContaining({ code: 'template-unavailable', message: 'interactive export template is unavailable' }));
    expect(JSON.stringify(thrown)).not.toContain(PRIVATE_VALUE);
    expect((thrown as Error).stack).not.toContain(PRIVATE_VALUE);
  });

  it('maps a template body read failure to an unavailable-template error', async () => {
    await expect(buildInteractiveExportArtifact(options({
      fetchTemplate: async () => ({ ok: true, text: async () => { throw new Error(PRIVATE_VALUE); } } as Response),
    }))).rejects.toThrowError(expect.objectContaining({
      code: 'template-unavailable', message: 'interactive export template is unavailable',
    }));
  });

  it.each([
    ['no marker', template().replace(INTERACTIVE_EXPORT_PAYLOAD_MARKER, '')],
    ['two markers', template().replace(INTERACTIVE_EXPORT_PAYLOAD_MARKER, `${INTERACTIVE_EXPORT_PAYLOAD_MARKER}${INTERACTIVE_EXPORT_PAYLOAD_MARKER}`)],
  ])('requires exactly one payload marker in a template with %s', async (_name, html) => {
    await expect(buildInteractiveExportArtifact(options({ fetchTemplate: async () => new Response(html) }))).rejects.toThrowError(
      expect.objectContaining({ code: 'template-incompatible', message: 'interactive export template is incompatible' }),
    );
  });

  it.each(['templateId', 'payloadContractId', 'compatibilityId'] as const)('rejects a fetched template whose %s does not match the descriptor', async (key) => {
    const expected = descriptor();
    const inconsistent = { ...expected };
    if (key === 'templateId' || key === 'compatibilityId') {
      inconsistent.templateId = 'b'.repeat(64);
    }
    if (key === 'payloadContractId') {
      inconsistent.payloadContractId = 'records/other/v1';
    }
    inconsistent.compatibilityId = createCompatibilityId({
      appId: inconsistent.appId,
      payloadContractId: inconsistent.payloadContractId,
      templateId: inconsistent.templateId,
    });

    await expect(buildInteractiveExportArtifact(options({
      descriptor: expected,
      fetchTemplate: async () => new Response(template(inconsistent)),
    }))).rejects.toThrowError(expect.objectContaining({ code: 'template-incompatible', message: 'interactive export template is incompatible' }));
  });

  it('rejects a fetched template with only a mismatched compatibility identity', async () => {
    const expected = descriptor();
    const inconsistent = { ...expected, compatibilityId: 'b'.repeat(64) };

    await expect(buildInteractiveExportArtifact(options({
      descriptor: expected,
      fetchTemplate: async () => new Response(template(inconsistent)),
    }))).rejects.toThrowError(expect.objectContaining({ code: 'template-incompatible', message: 'interactive export template is incompatible' }));
  });

  it('rejects a newer template when an open hosted page retains an old descriptor', async () => {
    const old = descriptor('a'.repeat(64));
    const current = descriptor('b'.repeat(64));

    await expect(buildInteractiveExportArtifact(options({
      descriptor: old,
      fetchTemplate: async () => new Response(template(current)),
    }))).rejects.toThrowError(expect.objectContaining({ code: 'template-incompatible', message: 'interactive export template is incompatible' }));
  });

  it('keeps snapshot and complete-artifact limits independent', async () => {
    const oversizedSnapshot = definition();
    oversizedSnapshot.buildSnapshot = async () => ({
      snapshotData: { records: [{ id: 'alpha' }], padding: 'x'.repeat(MAX_INTERACTIVE_EXPORT_SNAPSHOT_BYTES) } as RecordsData,
      viewState: { selectedRecordId: 'alpha' },
    });
    const noFetch = vi.fn(async () => new Response(template()));

    await expect(buildInteractiveExportArtifact(options({ definition: oversizedSnapshot, fetchTemplate: noFetch }))).rejects.toThrowError(
      expect.objectContaining({ code: 'snapshot-oversized', message: 'interactive export snapshot is too large' }),
    );
    expect(noFetch).not.toHaveBeenCalled();

    const oversizedTemplate = template().replace('</body>', `${'x'.repeat(MAX_INTERACTIVE_EXPORT_ARTIFACT_BYTES)}</body>`);
    await expect(buildInteractiveExportArtifact(options({ fetchTemplate: async () => new Response(oversizedTemplate) }))).rejects.toThrowError(
      expect.objectContaining({ code: 'artifact-oversized', message: 'interactive export artifact is too large' }),
    );
  });
});

describe('downloadInteractiveExport', () => {
  const artifact = { filename: 'records--current-records--2026-09-08.html', html: '<!doctype html><title>Records</title>', byteLength: 40 };
  let originalCreateObjectURL: typeof URL.createObjectURL | undefined;
  let originalRevokeObjectURL: typeof URL.revokeObjectURL | undefined;

  beforeEach(() => {
    vi.useFakeTimers();
    originalCreateObjectURL = URL.createObjectURL;
    originalRevokeObjectURL = URL.revokeObjectURL;
  });

  afterEach(() => {
    vi.useRealTimers();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: originalCreateObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: originalRevokeObjectURL });
    document.body.replaceChildren();
  });

  it('removes the temporary anchor and revokes its object URL once', () => {
    const createObjectURL = vi.fn(() => 'blob:interactive-export');
    const revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });

    downloadInteractiveExport(artifact);

    expect(click).toHaveBeenCalledOnce();
    expect(document.querySelector('a[download]')).toBeNull();
    vi.runAllTimers();
    expect(revokeObjectURL).toHaveBeenCalledTimes(1);
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:interactive-export');
  });

  it('sanitizes browser download failures while retaining cleanup', () => {
    const createObjectURL = vi.fn(() => 'blob:interactive-export');
    const revokeObjectURL = vi.fn();
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => { throw new Error(PRIVATE_VALUE); });
    click.mockClear();
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });

    expect(() => downloadInteractiveExport(artifact)).toThrowError(expect.objectContaining({
      code: 'download-failed', message: 'interactive export download failed',
    }));
    expect(document.querySelector('a[download]')).toBeNull();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:interactive-export');
    expect(click).toHaveBeenCalledOnce();
  });
});
