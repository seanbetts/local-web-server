import { mountInteractiveExport } from '@local-web/ui';
import '@local-web/ui/styles.css';
import './styles.css';
import { App } from './App';
import { recordContract } from './contract';

mountInteractiveExport({
  contract: recordContract,
  render: ({ snapshotData, viewState }) => <App snapshotData={snapshotData} initialViewState={viewState} />,
});
