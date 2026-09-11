import { canonicalInteractiveExportJson, defineInteractiveExportContract } from '@local-web/ui';

export type RecordData = {
  records: { id: string; label: string; amount: number }[];
  calculatedTotal: number;
};

export type RecordViewState = {
  selectedRecordId: string;
  sort: { field: 'label' | 'amount'; direction: 'ascending' | 'descending' };
};

function exactRecord(value: unknown, keys: string[]): Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).length !== keys.length || keys.some((key) => !Object.hasOwn(value, key))) {
    throw new Error('invalid record capture');
  }
  return value as Record<string, unknown>;
}

function nonempty(value: unknown): string {
  if (typeof value !== 'string' || !value.trim()) throw new Error('invalid record text');
  return value;
}

export const recordContract = defineInteractiveExportContract<RecordData, RecordViewState>({
  id: 'record-list',
  version: 1,
  sensitivity: { classification: 'sensitive', notice: 'Contains fixture client records.' },
  decodeSnapshot(value) {
    // Reuse the public JSON boundary before app-owned exact shapes and relationships.
    canonicalInteractiveExportJson(value);
    const capture = exactRecord(value, ['snapshotData', 'viewState']);
    const data = exactRecord(capture.snapshotData, ['records', 'calculatedTotal']);
    const state = exactRecord(capture.viewState, ['selectedRecordId', 'sort']);
    const sort = exactRecord(state.sort, ['field', 'direction']);
    if (!Array.isArray(data.records)) throw new Error('invalid records');
    const records = data.records.map((value) => {
      const item = exactRecord(value, ['id', 'label', 'amount']);
      if (typeof item.amount !== 'number' || !Number.isFinite(item.amount)) throw new Error('invalid amount');
      return { id: nonempty(item.id), label: nonempty(item.label), amount: item.amount };
    });
    const ids = new Set(records.map(({ id }) => id));
    const selectedRecordId = nonempty(state.selectedRecordId);
    if (ids.size !== records.length || !ids.has(selectedRecordId)) throw new Error('invalid record selection');
    if (typeof data.calculatedTotal !== 'number' || !Number.isFinite(data.calculatedTotal)
      || records.reduce((sum, item) => sum + item.amount, 0) !== data.calculatedTotal) throw new Error('invalid total');
    if ((sort.field !== 'label' && sort.field !== 'amount')
      || (sort.direction !== 'ascending' && sort.direction !== 'descending')) throw new Error('invalid sort');
    return {
      snapshotData: { records, calculatedTotal: data.calculatedTotal },
      viewState: { selectedRecordId, sort: { field: sort.field, direction: sort.direction } },
    };
  },
});

// The public Vite plugin loads the default contract; both entries import this same value.
export default recordContract;
