import { useEffect, useId, useRef, useState, type ReactElement } from 'react';

import { Button, IconButton } from './actions.js';
import { ContextExportButton } from './ContextExportButton.js';
import { downloadContextExport, renderContextExport } from './contextExportDocument.js';
import type { ContextExportBuilder, ContextJson } from './contextExportModel.js';
import {
  buildInteractiveExportArtifact,
  downloadInteractiveExport,
  readInteractiveExportTemplateDescriptor,
} from './interactiveExportDocument.js';
import type { InteractiveExportDefinition } from './interactiveExportModel.js';
import { Dialog } from './overlays.js';

const CONTEXT_ERROR = 'Context export could not be created. Review the app context and try again.';
const INTERACTIVE_ERROR = 'Interactive snapshot could not be created. Review the app state and try again.';

type ExportKind = 'context' | 'interactive';

type ExportOperation = {
  readonly announcement: string | null;
  readonly busy: ExportKind | null;
  readonly cancel: () => void;
  readonly error: ExportKind | null;
  readonly run: (kind: ExportKind, operation: (signal: AbortSignal) => Promise<void>) => Promise<void>;
};

const successMessage = (kind: ExportKind): string => (
  kind === 'context' ? 'Context exported.' : 'Interactive snapshot exported.'
);

function useExportOperation(): ExportOperation {
  const controller = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const [busy, setBusy] = useState<ExportKind | null>(null);
  const [announcement, setAnnouncement] = useState<string | null>(null);
  const [error, setError] = useState<ExportKind | null>(null);

  const cancel = () => {
    controller.current?.abort();
    controller.current = null;
    if (mounted.current) setBusy(null);
  };

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      controller.current?.abort();
      controller.current = null;
    };
  }, []);

  const run = async (
    kind: ExportKind,
    operation: (signal: AbortSignal) => Promise<void>,
  ) => {
    if (controller.current) return;

    const request = new AbortController();
    controller.current = request;
    setBusy(kind);
    setAnnouncement(null);
    setError(null);

    try {
      await operation(request.signal);
      if (!request.signal.aborted && mounted.current) {
        setAnnouncement(successMessage(kind));
      }
    } catch {
      if (!request.signal.aborted && mounted.current) setError(kind);
    } finally {
      if (controller.current === request) {
        controller.current = null;
        if (mounted.current) setBusy(null);
      }
    }
  };

  return { announcement, busy, cancel, error, run };
}

const effectiveColourMode = (): 'light' | 'dark' => {
  const selected = document.documentElement.dataset.lwpColourMode;
  if (selected === 'light' || selected === 'dark') return selected;
  return typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light';
};

type InteractiveExportApp = {
  readonly id: string;
  readonly name: string;
};

export type InteractiveExportControlProps = {
  readonly app: InteractiveExportApp;
  readonly buildContextExport?: ContextExportBuilder;
  readonly interactiveExport?: InteractiveExportDefinition<ContextJson, ContextJson>;
};

type InteractiveOperationProps = {
  readonly app: InteractiveExportApp;
  readonly definition: InteractiveExportDefinition<ContextJson, ContextJson>;
  readonly operation: ExportOperation;
};

const runInteractiveExport = async ({ app, definition, operation }: InteractiveOperationProps) => {
  await operation.run('interactive', async (signal) => {
    const artifact = await buildInteractiveExportArtifact({
      app,
      definition,
      descriptor: readInteractiveExportTemplateDescriptor(),
      effectiveColourMode: effectiveColourMode(),
      fetchTemplate: (input, init) => fetch(input, init),
      now: () => new Date(),
      signal,
    });
    signal.throwIfAborted();
    downloadInteractiveExport(artifact);
  });
};

function SensitivityNotice({ definition, id }: {
  readonly definition: InteractiveExportDefinition<ContextJson, ContextJson>;
  readonly id: string;
}): ReactElement {
  const { classification, notice } = definition.contract.sensitivity;
  return (
    <p className="lwp-interactive-export__sensitivity" id={id}>
      <strong>{classification === 'sensitive' ? 'Sensitive' : 'Private'}</strong>
      {' '}
      <span>{notice}</span>
    </p>
  );
}

function ExportFeedback({ operation }: { readonly operation: ExportOperation }): ReactElement | null {
  if (operation.announcement) {
    return <span className="lwp-context-export__status" role="status">{operation.announcement}</span>;
  }
  if (operation.error) {
    return (
      <span className="lwp-context-export__status" role="alert">
        {operation.error === 'context' ? CONTEXT_ERROR : INTERACTIVE_ERROR}
      </span>
    );
  }
  return null;
}

function InteractiveExportButton({ app, definition }: {
  readonly app: InteractiveExportApp;
  readonly definition: InteractiveExportDefinition<ContextJson, ContextJson>;
}): ReactElement {
  const operation = useExportOperation();
  const sensitivityId = `lwp-interactive-export-sensitivity-${useId()}`;
  const busy = operation.busy === 'interactive';
  return (
    <span className="lwp-interactive-export">
      <SensitivityNotice definition={definition} id={sensitivityId} />
      <IconButton
        aria-describedby={sensitivityId}
        busy={busy}
        icon="download"
        label={busy ? 'Exporting interactive snapshot' : 'Export interactive snapshot'}
        onClick={() => runInteractiveExport({ app, definition, operation })}
      />
      <ExportFeedback operation={operation} />
    </span>
  );
}

function ExportChooser({ app, buildContextExport, interactiveExport }: {
  readonly app: InteractiveExportApp;
  readonly buildContextExport: ContextExportBuilder;
  readonly interactiveExport: InteractiveExportDefinition<ContextJson, ContextJson>;
}): ReactElement {
  const [open, setOpen] = useState(false);
  const operation = useExportOperation();
  const trigger = useRef<HTMLButtonElement>(null);
  const restoreTriggerFocus = useRef(false);
  const contextDescriptionId = `lwp-context-export-description-${useId()}`;
  const interactiveDescriptionId = `lwp-interactive-export-description-${useId()}`;
  const sensitivityId = `lwp-interactive-export-sensitivity-${useId()}`;
  const close = () => {
    operation.cancel();
    restoreTriggerFocus.current = true;
    setOpen(false);
  };
  useEffect(() => {
    if (!open && restoreTriggerFocus.current) {
      restoreTriggerFocus.current = false;
      trigger.current?.focus();
    }
  }, [open]);
  const runContextExport = () => operation.run('context', async (signal) => {
    const context = await buildContextExport({ signal });
    signal.throwIfAborted();
    downloadContextExport(renderContextExport(context));
  });

  return (
    <span className="lwp-context-export lwp-interactive-export-chooser">
      <IconButton ref={trigger} icon="download" label="Export" onClick={() => setOpen(true)} />
      <Dialog open={open} onClose={close} title="Export">
        <div className="lwp-interactive-export-chooser__choices">
          <section className="lwp-interactive-export-chooser__choice">
            <Button
              aria-describedby={contextDescriptionId}
              busy={operation.busy === 'context'}
              aria-disabled={operation.busy !== null && operation.busy !== 'context'}
              onClick={runContextExport}
            >
              Context document
            </Button>
            <p id={contextDescriptionId}>A script-free, structured, static document.</p>
          </section>
          <section className="lwp-interactive-export-chooser__choice">
            <Button
              aria-describedby={`${interactiveDescriptionId} ${sensitivityId}`}
              busy={operation.busy === 'interactive'}
              aria-disabled={operation.busy !== null && operation.busy !== 'interactive'}
              onClick={() => runInteractiveExport({
                app,
                definition: interactiveExport,
                operation,
              })}
            >
              Interactive snapshot
            </Button>
            <p id={interactiveDescriptionId}>A packaged application view with selected private state that works offline.</p>
            <SensitivityNotice definition={interactiveExport} id={sensitivityId} />
          </section>
        </div>
        <ExportFeedback operation={operation} />
      </Dialog>
    </span>
  );
}

export function InteractiveExportControl({
  app,
  buildContextExport,
  interactiveExport,
}: InteractiveExportControlProps): ReactElement | null {
  if (buildContextExport && interactiveExport) {
    return (
      <ExportChooser
        app={app}
        buildContextExport={buildContextExport}
        interactiveExport={interactiveExport}
      />
    );
  }
  if (interactiveExport) {
    return <InteractiveExportButton app={app} definition={interactiveExport} />;
  }
  return buildContextExport
    ? <ContextExportButton buildContextExport={buildContextExport} />
    : null;
}
