import { AppShell, Button, Field, Select, type AppIdentity, type InteractiveExportDefinition } from '@local-web/ui';
import { useState } from 'react';
import { recordContract, type RecordData, type RecordViewState } from './contract';

const recordApp: AppIdentity = {
  id: 'interactive-export-fixture', name: 'Runtime Records', icon: 'checklist', accent: '#3859D6',
};

export function App({ snapshotData, initialViewState }: {
  snapshotData: RecordData;
  initialViewState: RecordViewState;
}) {
  const [viewState, setViewState] = useState(initialViewState);
  const definition: InteractiveExportDefinition<RecordData, RecordViewState> = {
    contract: recordContract,
    async buildSnapshot({ signal }) {
      signal.throwIfAborted();
      return { snapshotData, viewState };
    },
  };
  const ordered = [...snapshotData.records].sort((left, right) => {
    const order = viewState.sort.field === 'amount'
      ? left.amount - right.amount : left.label.localeCompare(right.label);
    return viewState.sort.direction === 'ascending' ? order : -order;
  });

  return (
    <AppShell app={recordApp} interactiveExport={definition}>
      <section className="record-fixture" aria-labelledby="record-heading">
        <h1 id="record-heading">Runtime records</h1>
        <p>Calculated total: <output aria-label="Calculated total">{snapshotData.calculatedTotal}</output></p>
        <div className="record-fixture__sort">
          <Field label="Sort field">
            <Select value={viewState.sort.field} onChange={(event) => {
              const field = event.target.value as RecordViewState['sort']['field'];
              setViewState((previous) => ({ ...previous, sort: { ...previous.sort, field } }));
            }}>
              <option value="label">Label</option>
              <option value="amount">Amount</option>
            </Select>
          </Field>
          <Field label="Sort direction">
            <Select value={viewState.sort.direction} onChange={(event) => {
              const direction = event.target.value as RecordViewState['sort']['direction'];
              setViewState((previous) => ({ ...previous, sort: { ...previous.sort, direction } }));
            }}>
              <option value="ascending">Ascending</option>
              <option value="descending">Descending</option>
            </Select>
          </Field>
        </div>
        <ul aria-label="Records">
          {ordered.map((record) => (
            <li key={record.id}>
              <Button aria-pressed={viewState.selectedRecordId === record.id}
                onClick={() => setViewState((previous) => ({ ...previous, selectedRecordId: record.id }))}>
                {record.label} — {record.amount}
              </Button>
            </li>
          ))}
        </ul>
      </section>
    </AppShell>
  );
}
