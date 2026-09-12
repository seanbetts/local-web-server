import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

import { recordContract } from './contract';

const fixtureRoot = new URL('..', import.meta.url);

const runtimeCapture = () => ({
  snapshotData: {
    records: [
      { id: 'alpha', label: 'Runtime Alpha client', amount: 12 },
      { id: 'bravo', label: 'Runtime Bravo client', amount: 42 },
      { id: 'charlie', label: 'Runtime Charlie client', amount: 7 },
    ],
    calculatedTotal: 61,
  },
  viewState: {
    selectedRecordId: 'alpha',
    sort: { field: 'label' as const, direction: 'ascending' as const },
  },
});

describe('interactive export fixture contract', () => {
  it('declares the public UI 0.7.0 interactive export contract', () => {
    const manifest = JSON.parse(readFileSync(new URL('local-web.json', fixtureRoot), 'utf8'));
    expect(manifest.platform.uiVersion).toBe('0.7.0');
  });

  it('accepts runtime data and both supported sort dimensions', () => {
    const capture = runtimeCapture();
    expect(recordContract.decodeSnapshot(capture)).toEqual(capture);
    const amountDescending = {
      ...capture,
      viewState: {
        selectedRecordId: 'bravo',
        sort: { field: 'amount' as const, direction: 'descending' as const },
      },
    };
    expect(recordContract.decodeSnapshot(amountDescending).viewState).toEqual(
      amountDescending.viewState,
    );
  });

  const invalidCaptures: [string, (capture: ReturnType<typeof runtimeCapture>) => unknown][] = [
    ['malformed record', (capture) => ({ ...capture, snapshotData: { ...capture.snapshotData, records: [null] } })],
    ['blank id', (capture) => { capture.snapshotData.records[0].id = ' '; return capture; }],
    ['blank label', (capture) => { capture.snapshotData.records[0].label = ''; return capture; }],
    ['non-finite amount', (capture) => { capture.snapshotData.records[0].amount = Number.NaN; return capture; }],
    ['wrong amount type', (capture) => ({ ...capture, snapshotData: { records: [{ id: 'alpha', label: 'Alpha', amount: '12' }], calculatedTotal: 12 } })],
    ['duplicate record id', (capture) => { capture.snapshotData.records[1].id = 'alpha'; return capture; }],
    ['inconsistent total', (capture) => { capture.snapshotData.calculatedTotal = 60; return capture; }],
    ['missing selection', (capture) => { capture.viewState.selectedRecordId = 'absent'; return capture; }],
    ['unsupported sort field', (capture) => ({ ...capture, viewState: { ...capture.viewState, sort: { ...capture.viewState.sort, field: 'id' } } })],
    ['unsupported sort direction', (capture) => ({ ...capture, viewState: { ...capture.viewState, sort: { ...capture.viewState.sort, direction: 'random' } } })],
    ['extra capture field', (capture) => ({ ...capture, extra: true })],
    ['extra data field', (capture) => ({ ...capture, snapshotData: { ...capture.snapshotData, extra: true } })],
    ['extra state field', (capture) => ({ ...capture, viewState: { ...capture.viewState, extra: true } })],
    ['extra sort field', (capture) => ({ ...capture, viewState: { ...capture.viewState, sort: { ...capture.viewState.sort, extra: true } } })],
    ['extra record field', (capture) => ({ ...capture, snapshotData: { ...capture.snapshotData, records: capture.snapshotData.records.map((record, index) => index === 0 ? { ...record, extra: true } : record) } })],
    ['missing state', (capture) => ({ snapshotData: capture.snapshotData })],
  ];

  it.each(invalidCaptures)('rejects %s', (_reason, mutate) => {
    expect(() => recordContract.decodeSnapshot(mutate(runtimeCapture()))).toThrow();
  });
});
