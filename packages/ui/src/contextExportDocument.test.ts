import {
  downloadContextExport,
  renderContextExport,
} from './contextExportDocument.js';
import type { ContextExportArtifact } from './contextExportDocument.js';
import { MAX_CONTEXT_EXPORT_BYTES } from './contextExportModel.js';
import type { LocalWebContextV1 } from './contextExportModel.js';

const DOWNLOAD_ERROR = 'Context export could not be downloaded.';
const CLOSED_DOCUMENT_CSP = "default-src 'none'; connect-src 'none'; img-src 'none'; font-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; script-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'";
const STATIC_SNAPSHOT_NOTICE = 'This is a static snapshot. Changes do not sync to the source app.';
const PRIVATE_VALUE = 'private-value-8f5a1cb2';

function validContext(): LocalWebContextV1 {
  return {
    schema: 'local-web-context/v1',
    app: {
      id: 'recipes',
      name: 'Recipe Planner',
      version: '1.2.3',
      sourceRevision: 'abc1234',
    },
    context: {
      title: 'Current overview',
      scope: 'The current local application state.',
      activeRoute: '/overview',
      generatedAt: '2026-08-22T10:00:00.000Z',
      observedAt: null,
      dataRevision: null,
    },
    sensitivity: {
      classification: 'private',
      notice: 'This export is for private use.',
    },
    summary: 'A concise, current account of the application.',
    capabilities: ['context-export', 'local-data'],
    sections: [
      {
        id: 'overview',
        title: 'Overview',
        blocks: [
          { type: 'paragraph', text: 'The app is ready.' },
          {
            type: 'key-values',
            items: [
              { label: 'Status', value: 'ready' },
              { label: 'Retries', value: 0 },
              { label: 'Enabled', value: true },
              { label: 'Owner', value: null },
            ],
          },
          { type: 'list', ordered: false, items: ['First item', 'Second item'] },
          {
            type: 'table',
            columns: [
              { key: 'name', label: 'Name' },
              { key: 'state', label: 'State' },
            ],
            rows: [{ name: 'Foundation', state: 'ready' }],
          },
        ],
      },
    ],
    data: {
      foundation: {
        state: 'ready',
        nested: [null, true, 3, { label: 'canonical' }],
      },
    },
    provenance: {
      freshness: 'Current at generation time.',
      sources: [
        { label: 'Application runtime', observedAt: null, revision: null },
      ],
    },
    assumptions: ['The local network is trusted.'],
    decisions: [{ statement: 'Keep data local.', reasoning: 'The app is trusted-LAN only.' }],
    caveats: ['No remote synchronisation is included.'],
    omissions: ['Screenshots are not included.'],
  };
}

function parseDocument(html: string): Document {
  return new DOMParser().parseFromString(html, 'text/html');
}

function textOf(document: Document, selector: string): string {
  return document.querySelector(selector)?.textContent ?? '';
}

describe('renderContextExport', () => {
  it('renders every context field as a complete inert document with canonical JSON', () => {
    const context = validContext();
    const artifact = renderContextExport(context);
    const parsed = parseDocument(artifact.html);
    const payload = parsed.querySelector('script[type="application/json"]');

    expect(parsed.title).toBe('Current overview — Recipe Planner');
    expect(textOf(parsed, '[data-context-export-sensitivity]')).toBe('PRIVATE: This export is for private use.');
    expect(textOf(parsed, '[data-context-export-classification]')).toBe('PRIVATE');
    expect(textOf(parsed, '[data-context-export-snapshot]')).toBe(STATIC_SNAPSHOT_NOTICE);
    expect(parsed.body.textContent).toContain('Recipe Planner');
    expect(parsed.body.textContent).toContain('1.2.3');
    expect(parsed.body.textContent).toContain('abc1234');
    expect(parsed.body.textContent).toContain('The current local application state.');
    expect(parsed.body.textContent).toContain('/overview');
    expect(parsed.body.textContent).toContain('A concise, current account of the application.');
    expect(textOf(parsed, '[data-context-export-capabilities]')).toContain('context-export');
    expect(textOf(parsed, '[data-context-export-capabilities]')).toContain('local-data');
    expect(textOf(parsed, '[data-context-export-section="overview"]')).toContain('The app is ready.');
    expect(textOf(parsed, '[data-context-export-section="overview"]')).toContain('Status');
    expect(textOf(parsed, '[data-context-export-section="overview"]')).toContain('ready');
    expect(textOf(parsed, '[data-context-export-section="overview"]')).toContain('First item');
    expect(textOf(parsed, '[data-context-export-section="overview"]')).toContain('Foundation');
    expect(textOf(parsed, '[data-context-export-provenance]')).toContain('Current at generation time.');
    expect(textOf(parsed, '[data-context-export-assumptions]')).toContain('The local network is trusted.');
    expect(textOf(parsed, '[data-context-export-decisions]')).toContain('Keep data local.');
    expect(textOf(parsed, '[data-context-export-decisions]')).toContain('The app is trusted-LAN only.');
    expect(textOf(parsed, '[data-context-export-caveats]')).toContain('No remote synchronisation is included.');
    expect(textOf(parsed, '[data-context-export-omissions]')).toContain('Screenshots are not included.');
    expect(JSON.parse(payload?.textContent ?? '')).toEqual(context);
    expect(artifact.byteLength).toBe(new TextEncoder().encode(artifact.html).byteLength);
  });

  it('renders each sensitivity classification as a prominent uppercase warning', () => {
    for (const [classification, label] of [
      ['private', 'PRIVATE'],
      ['sensitive', 'SENSITIVE'],
    ] as const) {
      const context = validContext();
      context.sensitivity.classification = classification;
      const parsed = parseDocument(renderContextExport(context).html);

      expect(textOf(parsed, '[data-context-export-classification]')).toBe(label);
      expect(textOf(parsed, '[data-context-export-sensitivity]')).toBe(
        `${label}: This export is for private use.`,
      );
    }
  });

  it('preserves negative zero in a visible scalar block and nested canonical data', () => {
    const context = validContext();
    context.sections[0].blocks[1] = {
      type: 'key-values',
      items: [{ label: 'Offset', value: -0 }],
    };
    context.data = { nested: { negativeZero: -0 } };

    const parsed = parseDocument(renderContextExport(context).html);
    const payload = JSON.parse(parsed.querySelector('script[type="application/json"]')?.textContent ?? '') as {
      data: { nested: { negativeZero: number } };
    };

    expect(textOf(parsed, '[data-context-export-section="overview"]')).toContain('Offset');
    expect(textOf(parsed, '[data-context-export-section="overview"]')).toContain('-0');
    expect(Object.is(payload.data.nested.negativeZero, -0)).toBe(true);
  });

  it('keeps visible and canonical hostile strings as inert text', () => {
    const context = validContext();
    const hostile = '</script><img src="https://example.test/private.png" onerror="alert(1)"><b>unsafe</b>&\u2028\u2029';
    context.app.name = hostile;
    context.summary = hostile;
    context.sections[0].blocks[0] = { type: 'paragraph', text: hostile };
    context.data = { hostile, nested: { hostile } };

    const parsed = parseDocument(renderContextExport(context).html);
    const payload = parsed.querySelector('script[type="application/json"]');

    expect(parsed.querySelectorAll('script')).toHaveLength(1);
    expect(payload?.getAttribute('type')).toBe('application/json');
    expect(parsed.querySelectorAll('img, iframe, form, svg, link[rel="stylesheet"], link[rel="preload"], link[rel="icon"]')).toHaveLength(0);
    expect([...parsed.querySelectorAll('*')].flatMap((element) => [...element.attributes].map((attribute) => attribute.name))).not.toContainEqual(expect.stringMatching(/^on/i));
    expect(parsed.body.textContent).toContain(hostile);
    expect(JSON.parse(payload?.textContent ?? '')).toEqual(context);
    expect(parsed.querySelector('meta[http-equiv="Content-Security-Policy"]')?.getAttribute('content')).toBe(CLOSED_DOCUMENT_CSP);
    expect(parsed.documentElement.outerHTML).not.toContain('http://localhost');
  });

  it('uses canonical lexical object-key order independently of insertion order', () => {
    const left = validContext();
    const right: LocalWebContextV1 = {
      omissions: left.omissions,
      caveats: left.caveats,
      decisions: left.decisions,
      assumptions: left.assumptions,
      provenance: left.provenance,
      data: {
        foundation: {
          state: 'ready',
          nested: [{ label: 'canonical' }, 3, true, null],
        },
      },
      sections: left.sections,
      capabilities: left.capabilities,
      summary: left.summary,
      sensitivity: left.sensitivity,
      context: {
        dataRevision: null,
        observedAt: null,
        generatedAt: '2026-08-22T10:00:00.000Z',
        activeRoute: '/overview',
        scope: 'The current local application state.',
        title: 'Current overview',
      },
      app: {
        sourceRevision: 'abc1234',
        version: '1.2.3',
        name: 'Recipe Planner',
        id: 'recipes',
      },
      schema: 'local-web-context/v1',
    };
    right.data = {
      zeta: { second: 2, first: 1 },
      alpha: { zebra: 'z', apple: 'a' },
    };
    left.data = {
      alpha: { apple: 'a', zebra: 'z' },
      zeta: { first: 1, second: 2 },
    };

    expect(renderContextExport(left).html).toBe(renderContextExport(right).html);
    expect(renderContextExport(left).html).toContain('"alpha":{"apple":"a","zebra":"z"},"zeta":{"first":1,"second":2}');
  });

  it('derives a stable filename from the app, title, and generated date', () => {
    const context = validContext();
    context.context.title = 'Recipe Planner / Summer';

    expect(renderContextExport(context).filename).toBe('recipes--recipe-planner-summer--2026-08-22.html');
  });

  it('uses context as the filename title when title characters do not survive sanitisation', () => {
    const context = validContext();
    context.context.title = '／／／';

    expect(renderContextExport(context).filename).toBe('recipes--context--2026-08-22.html');
  });

  it('allows exactly 5 MiB and rejects one additional UTF-8 byte without private data', () => {
    const exact = validContext();
    exact.data = { padding: 'x' };
    const wrapperBytes = renderContextExport(exact).byteLength;
    exact.data = { padding: 'x'.repeat(MAX_CONTEXT_EXPORT_BYTES - wrapperBytes + 1) };

    expect(renderContextExport(exact).byteLength).toBe(MAX_CONTEXT_EXPORT_BYTES);

    exact.data = { padding: `${exact.data.padding}x` };
    expect(() => renderContextExport(exact)).toThrowError(
      expect.objectContaining({
        code: 'oversized',
        message: 'Context export is larger than 5 MiB. Reduce the app-owned context data.',
      }),
    );

    exact.data = { padding: `${exact.data.padding}`, private: PRIVATE_VALUE };
    expect(() => renderContextExport(exact)).not.toThrowError(
      expect.objectContaining({ message: expect.stringContaining(PRIVATE_VALUE) }),
    );
  });
});

describe('downloadContextExport', () => {
  const artifact: ContextExportArtifact = {
    filename: 'recipes--current-overview--2026-08-22.html',
    html: '<!doctype html><title>Context export</title>',
    byteLength: 45,
  };
  let originalBlob: typeof Blob;
  let originalCreateObjectURL: typeof URL.createObjectURL | undefined;
  let originalRevokeObjectURL: typeof URL.revokeObjectURL | undefined;

  beforeEach(() => {
    vi.useFakeTimers();
    originalBlob = globalThis.Blob;
    originalCreateObjectURL = URL.createObjectURL;
    originalRevokeObjectURL = URL.revokeObjectURL;
  });

  afterEach(() => {
    vi.useRealTimers();
    Object.defineProperty(globalThis, 'Blob', { configurable: true, value: originalBlob });
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: originalCreateObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: originalRevokeObjectURL });
    document.body.replaceChildren();
  });

  it('downloads the inert HTML once, removes its anchor, and revokes its URL after the click', () => {
    const createdBlobs: Blob[] = [];
    const createObjectURL = vi.fn((blob: Blob) => {
      createdBlobs.push(blob);
      return 'blob:context-export';
    });
    const revokeObjectURL = vi.fn();
    const clickedAnchors: HTMLAnchorElement[] = [];
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function captureAnchor() {
      clickedAnchors.push(this);
    });
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeObjectURL });

    downloadContextExport(artifact);

    expect(createdBlobs).toHaveLength(1);
    expect(createdBlobs[0]?.type).toBe('text/html;charset=utf-8');
    expect(createObjectURL).toHaveBeenCalledWith(createdBlobs[0]);
    expect(click).toHaveBeenCalledTimes(1);
    expect(clickedAnchors[0]?.download).toBe('recipes--current-overview--2026-08-22.html');
    expect(clickedAnchors[0]?.href).toBe('blob:context-export');
    expect(document.querySelector('a[download]')).toBeNull();
    expect(revokeObjectURL).not.toHaveBeenCalled();

    vi.runAllTimers();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:context-export');
  });

  it.each([
    ['Blob construction', () => {
      Object.defineProperty(globalThis, 'Blob', { configurable: true, value: class { constructor() { throw new Error(PRIVATE_VALUE); } } });
    }],
    ['Blob URL creation', () => {
      Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: () => { throw new Error(PRIVATE_VALUE); } });
    }],
    ['anchor click', () => {
      Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: () => 'blob:context-export' });
      Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() });
      vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => { throw new Error(PRIVATE_VALUE); });
    }],
  ])('fails safely when %s fails', (_name, arrange) => {
    arrange();

    expect(() => downloadContextExport(artifact)).toThrowError(
      expect.objectContaining({ code: 'download', message: DOWNLOAD_ERROR }),
    );
    expect(() => downloadContextExport(artifact)).not.toThrowError(
      expect.objectContaining({ message: expect.stringContaining(PRIVATE_VALUE) }),
    );
    expect(document.querySelector('a[download]')).toBeNull();
  });
});
