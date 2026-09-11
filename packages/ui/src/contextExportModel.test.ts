import {
  LOCAL_WEB_CONTEXT_SCHEMA,
  MAX_CONTEXT_EXPORT_BYTES,
  validateLocalWebContext,
} from './contextExportModel.js';
import type { LocalWebContextV1 } from './contextExportModel.js';

const PRIVATE_FIXTURE_VALUE = 'internal-only-7d1b7e3a';

function validContext(): LocalWebContextV1 {
  return {
    schema: 'local-web-context/v1',
    app: {
      id: 'example-app',
      name: 'Example App',
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
    assumptions: [],
    decisions: [{ statement: 'Keep data local.', reasoning: 'The app is trusted-LAN only.' }],
    caveats: ['No remote synchronisation is included.'],
    omissions: ['Screenshots are not included.'],
  };
}

function cloneContext(): Record<string, unknown> {
  return structuredClone(validContext()) as Record<string, unknown>;
}

describe('validateLocalWebContext', () => {
  it('returns a complete valid v1 context by identity', () => {
    const context = validContext();

    expect(validateLocalWebContext(context)).toBe(context);
    expect(LOCAL_WEB_CONTEXT_SCHEMA).toBe('local-web-context/v1');
    expect(MAX_CONTEXT_EXPORT_BYTES).toBe(5 * 1024 * 1024);
  });

  it('accepts an acyclic context that shares canonical data by reference', () => {
    const shared = { state: 'ready' };
    const context = {
      ...validContext(),
      data: { left: shared, right: shared },
    };

    expect(validateLocalWebContext(context)).toBe(context);
  });

  it.each([
    ['wrong schema', (candidate: Record<string, any>) => { candidate.schema = 'local-web-context/v2'; }],
    ['missing app group', (candidate: Record<string, any>) => { delete candidate.app; }],
    ['invalid sensitivity', (candidate: Record<string, any>) => { candidate.sensitivity.classification = 'public'; }],
    ['empty required string', (candidate: Record<string, any>) => { candidate.summary = '  '; }],
    ['malformed timestamp', (candidate: Record<string, any>) => { candidate.context.generatedAt = '2026-08-22'; }],
    ['duplicate section IDs', (candidate: Record<string, any>) => {
      candidate.sections.push(structuredClone(candidate.sections[0]));
    }],
    ['duplicate table keys', (candidate: Record<string, any>) => {
      candidate.sections[0].blocks[3].columns[1].key = 'name';
    }],
    ['undeclared table cell', (candidate: Record<string, any>) => {
      candidate.sections[0].blocks[3].rows[0].extra = 'not declared';
    }],
    ['empty blocks', (candidate: Record<string, any>) => { candidate.sections[0].blocks = []; }],
    ['NaN', (candidate: Record<string, any>) => { candidate.data.foundation.count = Number.NaN; }],
    ['infinity', (candidate: Record<string, any>) => { candidate.data.foundation.count = Infinity; }],
    ['undefined', (candidate: Record<string, any>) => { candidate.data.foundation.count = undefined; }],
    ['BigInt', (candidate: Record<string, any>) => { candidate.data.foundation.count = 1n; }],
    ['function', (candidate: Record<string, any>) => { candidate.data.foundation.count = () => undefined; }],
    ['symbol', (candidate: Record<string, any>) => { candidate.data.foundation.count = Symbol('private'); }],
    ['Date', (candidate: Record<string, any>) => { candidate.data.foundation.count = new Date(); }],
    ['DOM node', (candidate: Record<string, any>) => { candidate.data.foundation.count = document.createElement('div'); }],
    ['cycle', (candidate: Record<string, any>) => { candidate.data.foundation.self = candidate.data.foundation; }],
  ])('rejects %s without disclosing private input', (_name, mutate) => {
    const candidate = cloneContext();
    mutate(candidate);
    candidate.data.privateFixture = PRIVATE_FIXTURE_VALUE;

    expect(() => validateLocalWebContext(candidate)).toThrowError(
      expect.objectContaining({
        code: 'invalid',
        message: 'context export is invalid',
      }),
    );
    expect(() => validateLocalWebContext(candidate)).not.toThrowError(
      expect.objectContaining({ message: expect.stringContaining(PRIVATE_FIXTURE_VALUE) }),
    );
  });
});
