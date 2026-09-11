import {
  InteractiveExportError,
  canonicalInteractiveExportJson,
  createCompatibilityId,
  createPayloadContractId,
  defineInteractiveExportContract,
  MAX_INTERACTIVE_EXPORT_ARTIFACT_BYTES,
  MAX_INTERACTIVE_EXPORT_SNAPSHOT_BYTES,
  validateInteractiveExportEnvelope,
} from './interactiveExportModel.js';
import type { InteractiveExportCapture, InteractiveExportEnvelope } from './interactiveExportModel.js';

function validEnvelope(): InteractiveExportEnvelope<{ records: readonly { readonly id: string }[] }, { readonly selectedRecordId: string }> {
  return {
    schema: 'local-web-interactive-export/v1',
    template: {
      templateId: 'a'.repeat(64),
      payloadContractId: 'records/record-list/v1',
      compatibilityId: 'fac6e72e902d61576ffcef22631c4586d17e8214c6e44f02a099217f4aee5b4e',
      appId: 'records',
      appVersion: '1.2.3',
      sourceRevision: 'abc1234',
    },
    capture: {
      capturedAt: '2026-09-08T12:34:56.789Z',
      effectiveColourMode: 'light',
    },
    sensitivity: { classification: 'private', notice: 'Private working material.' },
    title: 'Records',
    snapshotData: { records: [{ id: 'alpha' }] },
    viewState: { selectedRecordId: 'alpha' },
  };
}

function cloneEnvelope(): Record<string, any> {
  return structuredClone(validEnvelope()) as Record<string, any>;
}

describe('interactive export model', () => {
  it('defines a canonical payload contract and preserves relationship validation', () => {
    const contract = defineInteractiveExportContract({
      id: 'record-list',
      version: 1,
      sensitivity: { classification: 'sensitive', notice: 'Contains client records.' },
      decodeSnapshot(value) {
        const capture = value as Record<string, unknown>;
        const data = capture.snapshotData as { records: Array<{ id: string }> };
        const state = capture.viewState as { selectedRecordId: string };
        if (!data.records.some(({ id }) => id === state.selectedRecordId)) throw new Error('selection');
        return capture as InteractiveExportCapture<
          { records: Array<{ id: string }> },
          { selectedRecordId: string }
        >;
      },
    });

    expect(createPayloadContractId('records', contract)).toBe('records/record-list/v1');
    expect(() => contract.decodeSnapshot({
      snapshotData: { records: [{ id: 'alpha' }] },
      viewState: { selectedRecordId: 'missing' },
    })).toThrow('selection');
  });

  it.each([
    ['noncanonical app ID', () => createPayloadContractId('Records', { id: 'record-list', version: 1, sensitivity: { classification: 'private', notice: 'Private.' }, decodeSnapshot: (value) => value as never })],
    ['noncanonical contract ID', () => defineInteractiveExportContract({ id: 'Record-list', version: 1, sensitivity: { classification: 'private', notice: 'Private.' }, decodeSnapshot: (value) => value as never })],
    ['zero version', () => defineInteractiveExportContract({ id: 'record-list', version: 0, sensitivity: { classification: 'private', notice: 'Private.' }, decodeSnapshot: (value) => value as never })],
    ['non-integer version', () => defineInteractiveExportContract({ id: 'record-list', version: 1.5, sensitivity: { classification: 'private', notice: 'Private.' }, decodeSnapshot: (value) => value as never })],
    ['missing notice', () => defineInteractiveExportContract({ id: 'record-list', version: 1, sensitivity: { classification: 'private', notice: undefined as never }, decodeSnapshot: (value) => value as never })],
    ['blank sensitive notice', () => defineInteractiveExportContract({ id: 'record-list', version: 1, sensitivity: { classification: 'sensitive', notice: '  ' }, decodeSnapshot: (value) => value as never })],
  ])('rejects %s contract metadata without exposing implementation detail', (_name, build) => {
    expect(build).toThrowError(expect.objectContaining({
      code: 'invalid-snapshot',
      message: 'interactive export snapshot is invalid',
    }));
  });

  it('accepts empty JSON objects and both supported sensitivity classifications', () => {
    const privateEnvelope = validEnvelope();
    privateEnvelope.snapshotData = {} as { records: readonly { readonly id: string }[] };
    privateEnvelope.viewState = {} as { selectedRecordId: string };
    delete (privateEnvelope as { title?: string }).title;
    expect(validateInteractiveExportEnvelope(privateEnvelope)).toEqual(privateEnvelope);

    const sensitiveEnvelope = cloneEnvelope();
    sensitiveEnvelope.sensitivity = { classification: 'sensitive', notice: 'Contains client records.' };
    expect(validateInteractiveExportEnvelope(sensitiveEnvelope)).toEqual(sensitiveEnvelope);
  });

  it('uses a root proxy data descriptor without reading its later schema getter', () => {
    const target = validEnvelope();
    const schemaGetter = vi.fn(() => { throw new Error('private schema getter'); });
    const proxy = new Proxy(target, {
      get(targetValue, property, receiver) {
        if (property === 'schema') return schemaGetter();
        return Reflect.get(targetValue, property, receiver);
      },
    });

    expect(validateInteractiveExportEnvelope(proxy)).toEqual(target);
    expect(schemaGetter).not.toHaveBeenCalled();
  });

  it('detaches nested proxy records and arrays from their stable descriptors', () => {
    const candidate = validEnvelope();
    const record = new Proxy({ id: 'alpha' }, {});
    const records = new Proxy([record], {});
    candidate.snapshotData = { records };

    const accepted = validateInteractiveExportEnvelope(candidate);
    expect(accepted).toEqual(validEnvelope());
    expect(accepted).not.toBe(candidate);
    expect(accepted.snapshotData.records).not.toBe(records);
    expect(accepted.snapshotData.records[0]).not.toBe(record);
  });

  it('preserves legitimate parsed JSON values in a detached envelope', () => {
    const parsed = JSON.parse(JSON.stringify(validEnvelope()));
    const accepted = validateInteractiveExportEnvelope(parsed);

    expect(accepted).toEqual(parsed);
    expect(accepted).not.toBe(parsed);
  });

  it.each(['schema', 'template', 'capture', 'sensitivity', 'title'] as const)(
    'rejects a common-envelope %s accessor without invoking it', (field) => {
      const candidate = cloneEnvelope();
      const original = candidate[field];
      const accessor = vi.fn(() => original);
      Object.defineProperty(candidate, field, { configurable: true, enumerable: true, get: accessor });

      expect(() => validateInteractiveExportEnvelope(candidate)).toThrowError(
        expect.objectContaining({ code: 'invalid-snapshot', message: 'interactive export snapshot is invalid' }),
      );
      expect(accessor).not.toHaveBeenCalled();
    },
  );

  it.each([
    ['template', (candidate: Record<string, any>) => { candidate.template = Object.assign(Object.create(null), candidate.template); }],
    ['capture', (candidate: Record<string, any>) => { candidate.capture = Object.assign(Object.create(null), candidate.capture); }],
    ['sensitivity', (candidate: Record<string, any>) => { candidate.sensitivity = Object.assign(Object.create(null), candidate.sensitivity); }],
    ['title', (candidate: Record<string, any>) => { candidate.title = Object.assign(Object.create(null), { value: candidate.title }); }],
  ])('rejects a prototype-bearing common-envelope %s value', (_field, mutate) => {
    const candidate = cloneEnvelope();
    mutate(candidate);

    expect(() => validateInteractiveExportEnvelope(candidate)).toThrowError(
      expect.objectContaining({ code: 'invalid-snapshot', message: 'interactive export snapshot is invalid' }),
    );
  });

  it.each([
    ['envelope', (candidate: Record<string, any>) => { candidate[Symbol('envelope')] = 'unexpected'; }],
    ['template', (candidate: Record<string, any>) => { candidate.template[Symbol('template')] = 'unexpected'; }],
    ['capture', (candidate: Record<string, any>) => { candidate.capture[Symbol('capture')] = 'unexpected'; }],
    ['sensitivity', (candidate: Record<string, any>) => { candidate.sensitivity[Symbol('sensitivity')] = 'unexpected'; }],
  ])('rejects a symbol on the common-envelope %s boundary', (_field, mutate) => {
    const candidate = cloneEnvelope();
    mutate(candidate);

    expect(() => validateInteractiveExportEnvelope(candidate)).toThrowError(
      expect.objectContaining({ code: 'invalid-snapshot', message: 'interactive export snapshot is invalid' }),
    );
  });

  it.each([
    ['extra envelope key', (candidate: Record<string, any>) => { candidate.extra = true; }],
    ['missing template key', (candidate: Record<string, any>) => { delete candidate.template.templateId; }],
    ['extra capture key', (candidate: Record<string, any>) => { candidate.capture.extra = true; }],
    ['blank notice', (candidate: Record<string, any>) => { candidate.sensitivity.notice = ' '; }],
    ['unsafe JSON function', (candidate: Record<string, any>) => { candidate.snapshotData.value = () => undefined; }],
    ['unsafe JSON date', (candidate: Record<string, any>) => { candidate.viewState.value = new Date(); }],
    ['unsafe JSON sparse array', (candidate: Record<string, any>) => { candidate.snapshotData.value = new Array(1); }],
    ['unsafe JSON cycle', (candidate: Record<string, any>) => { candidate.snapshotData.self = candidate.snapshotData; }],
  ])('rejects %s with a safe framework error', (_name, mutate) => {
    const candidate = cloneEnvelope();
    mutate(candidate);
    candidate.snapshotData.privateValue = 'internal-only-7d1b7e3a';

    expect(() => validateInteractiveExportEnvelope(candidate)).toThrowError(
      expect.objectContaining(_name === 'missing template key'
        ? { code: 'template-incompatible', message: 'interactive export template is incompatible' }
        : { code: 'invalid-snapshot', message: 'interactive export snapshot is invalid' }),
    );
    expect(() => validateInteractiveExportEnvelope(candidate)).not.toThrowError(
      expect.objectContaining({ message: expect.stringContaining('internal-only-7d1b7e3a') }),
    );
  });

  it('serializes object keys lexically, independent of insertion order', () => {
    expect(canonicalInteractiveExportJson({ z: [{ b: 2, a: 1 }], a: true }))
      .toBe('{"a":true,"z":[{"a":1,"b":2}]}');
  });

  it('rejects an envelope whose compatibility identity does not bind its template metadata', () => {
    const candidate = cloneEnvelope();
    candidate.template.compatibilityId = 'b'.repeat(64);

    expect(() => validateInteractiveExportEnvelope(candidate)).toThrowError(
      expect.objectContaining({ code: 'template-incompatible', message: 'interactive export template is incompatible' }),
    );
  });

  it('rejects a payload contract that belongs to another application even when its digest is recomputed', () => {
    const candidate = cloneEnvelope();
    candidate.template.payloadContractId = 'other-records/record-list/v1';
    candidate.template.compatibilityId = createCompatibilityId({
      appId: candidate.template.appId,
      payloadContractId: candidate.template.payloadContractId,
      templateId: candidate.template.templateId,
    });

    expect(() => validateInteractiveExportEnvelope(candidate)).toThrowError(
      expect.objectContaining({ code: 'template-incompatible', message: 'interactive export template is incompatible' }),
    );
  });

  it('enforces the 5 MiB canonical snapshot boundary', () => {
    const withinLimit = validEnvelope();
    delete (withinLimit as { title?: string }).title;
    withinLimit.viewState = {} as { selectedRecordId: string };
    withinLimit.snapshotData = { text: 'x'.repeat(MAX_INTERACTIVE_EXPORT_SNAPSHOT_BYTES - 43) } as never;
    expect(validateInteractiveExportEnvelope(withinLimit)).toEqual(withinLimit);

    const aboveLimit = structuredClone(withinLimit) as Record<string, any>;
    aboveLimit.snapshotData.text += 'x';
    expect(() => validateInteractiveExportEnvelope(aboveLimit)).toThrowError(
      expect.objectContaining({ code: 'snapshot-oversized', message: 'interactive export snapshot is too large' }),
    );
    expect(MAX_INTERACTIVE_EXPORT_ARTIFACT_BYTES).toBe(20 * 1024 * 1024);
  });

  it('derives deterministic but distinct compatibility identities from every input', () => {
    const input = {
      appId: 'records',
      payloadContractId: 'records/record-list/v1',
      templateId: 'a'.repeat(64),
    };
    const identity = createCompatibilityId(input);

    expect(identity).toMatch(/^[a-f0-9]{64}$/);
    expect(createCompatibilityId(input)).toBe(identity);
    expect(createCompatibilityId({ ...input, appId: 'other-records' })).not.toBe(identity);
    expect(createCompatibilityId({ ...input, payloadContractId: 'records/record-list/v2' })).not.toBe(identity);
    expect(createCompatibilityId({ ...input, templateId: 'b'.repeat(64) })).not.toBe(identity);
  });
});
