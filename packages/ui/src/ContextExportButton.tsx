import { useEffect, useRef, useState, type ReactElement } from 'react';

import { IconButton } from './actions.js';
import { downloadContextExport, renderContextExport } from './contextExportDocument.js';
import type { ContextExportBuilder } from './contextExportModel.js';

const EXPORT_ERROR = 'Context export could not be created. Review the app context and try again.';

export type ContextExportButtonProps = {
  readonly buildContextExport: ContextExportBuilder;
};

export function ContextExportButton({ buildContextExport }: ContextExportButtonProps): ReactElement {
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const [busy, setBusy] = useState(false);
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort();
      controller.current = null;
    };
  }, []);

  const exportContext = async () => {
    if (controller.current) {
      return;
    }

    const request = new AbortController();
    controller.current = request;
    setBusy(true);
    setAnnouncement(null);
    setError(false);

    try {
      const context = await buildContextExport({ signal: request.signal });
      if (request.signal.aborted || !mounted.current) {
        return;
      }
      downloadContextExport(renderContextExport(context));
      if (!request.signal.aborted && mounted.current) {
        setAnnouncement('Context exported.');
      }
    } catch {
      if (!request.signal.aborted && mounted.current) {
        setError(true);
      }
    } finally {
      if (controller.current === request) {
        controller.current = null;
      }
      if (mounted.current) {
        setBusy(false);
      }
    }
  };

  return (
    <span className="lwp-context-export">
      <IconButton
        busy={busy}
        icon="download"
        label={busy ? 'Exporting context' : 'Export context'}
        onClick={exportContext}
      />
      {announcement ? <span className="lwp-context-export__status" role="status">{announcement}</span> : null}
      {error ? <span className="lwp-context-export__status" role="alert">{EXPORT_ERROR}</span> : null}
    </span>
  );
}
