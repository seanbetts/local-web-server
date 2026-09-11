import { createRoot } from 'react-dom/client';
import '@local-web/ui/styles.css';
import './styles.css';
import { App } from './App';
import { recordContract } from './contract';

const root = createRoot(document.getElementById('root')!);
root.render(<p role="status">Loading runtime records…</p>);

// The fixture's service boundary is provided at runtime by the browser harness.
// Neither this entry nor the shared offline entry contains the response records.
fetch(new URL('api/records', document.baseURI))
  .then(async (response) => {
    if (!response.ok) throw new Error('records unavailable');
    return recordContract.decodeSnapshot(await response.json());
  })
  .then(({ snapshotData, viewState }) => root.render(<App snapshotData={snapshotData} initialViewState={viewState} />))
  .catch(() => root.render(<p role="alert">Runtime records are unavailable.</p>));
