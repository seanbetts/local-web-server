import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  type PropsWithChildren,
  type ReactNode,
} from 'react';
import { createRoot, type Root } from 'react-dom/client';

import type { ContextJson } from './contextExportModel.js';
import {
  applyColourMode,
  COLOUR_MODE_CHANGE_EVENT,
  normaliseColourMode,
  type ColourMode,
} from './colourMode.js';
import {
  canonicalInteractiveExportJson,
  createPayloadContractId,
  InteractiveExportError,
  validateInteractiveExportEnvelope,
} from './interactiveExportModel.js';
import type {
  InteractiveExportCapture,
  InteractiveExportContract,
  InteractiveExportEnvelope,
} from './interactiveExportModel.js';

export type InteractiveExportEnvironment<Data extends ContextJson = ContextJson, State extends ContextJson = ContextJson> = {
  readonly kind: 'interactive-export';
  readonly envelope: InteractiveExportEnvelope<Data, State>;
};

const EnvironmentContext = createContext<InteractiveExportEnvironment | null>(null);
const OfflineColourModeContext = createContext<ColourMode | null>(null);

/** Mount one provider for the lifetime of an offline document. */
export function InteractiveExportEnvironmentProvider({ envelope, children }: PropsWithChildren<{
  envelope: InteractiveExportEnvelope;
}>) {
  const environment = useMemo<InteractiveExportEnvironment>(
    () => ({ kind: 'interactive-export', envelope }),
    [envelope],
  );
  const [mode, setMode] = useState<ColourMode>(envelope.capture.effectiveColourMode);

  useLayoutEffect(() => {
    applyColourMode(mode, document.documentElement);
  }, [mode]);

  useEffect(() => {
    const synchroniseMode = (event: Event) => {
      if (event instanceof CustomEvent) setMode(normaliseColourMode(event.detail));
    };
    window.addEventListener(COLOUR_MODE_CHANGE_EVENT, synchroniseMode);
    return () => window.removeEventListener(COLOUR_MODE_CHANGE_EVENT, synchroniseMode);
  }, []);

  return (
    <EnvironmentContext.Provider value={environment}>
      <OfflineColourModeContext.Provider value={mode}>{children}</OfflineColourModeContext.Provider>
    </EnvironmentContext.Provider>
  );
}

export function useInteractiveExportEnvironment(): InteractiveExportEnvironment | null {
  return useContext(EnvironmentContext);
}

/** Internal theme seam: null means the hosted storage-backed control. */
export function useOfflineColourMode(): ColourMode | null {
  return useContext(OfflineColourModeContext);
}

export function OfflineSnapshotNotice() {
  const environment = useInteractiveExportEnvironment();
  if (!environment) return null;
  const { envelope } = environment;
  const capturedAt = new Intl.DateTimeFormat('en-GB', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    timeZone: 'UTC',
  }).format(new Date(envelope.capture.capturedAt));
  return (
    <aside className="lwp-offline-notice" aria-label="Offline snapshot details">
      <span>
        <strong>{envelope.sensitivity.classification === 'sensitive' ? 'Sensitive' : 'Private'}</strong>
        {' — '}{envelope.sensitivity.notice}
      </span>
      <span>
        <time dateTime={envelope.capture.capturedAt}>Captured {capturedAt} UTC</time>
        {' · Source '}{envelope.template.sourceRevision}
      </span>
      <span>Content is frozen at capture time. Changes stay in this document.</span>
    </aside>
  );
}

export type MountInteractiveExportOptions<Data extends ContextJson, State extends ContextJson> = {
  readonly contract: InteractiveExportContract<Data, State>;
  readonly render: (capture: InteractiveExportCapture<Data, State>) => ReactNode;
  readonly root?: HTMLElement | null;
};

const invalidSnapshot = (): never => { throw new InteractiveExportError('invalid-snapshot', 'interactive export snapshot is invalid'); };
const templateIncompatible = (): never => { throw new InteractiveExportError('template-incompatible', 'interactive export template is incompatible'); };

const decodeEmbeddedJson = (encoded: string): unknown => {
  const bytes = Uint8Array.from(atob(encoded), (character) => character.charCodeAt(0));
  return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
};

function readEmbeddedInteractiveExportEnvelope(): InteractiveExportEnvelope {
  const payloads = document.querySelectorAll<HTMLTemplateElement>(
    'template[data-local-web-interactive-export-payload]',
  );
  if (payloads.length !== 1) invalidSnapshot();
  let envelope: InteractiveExportEnvelope;
  try {
    envelope = validateInteractiveExportEnvelope(decodeEmbeddedJson(payloads[0].content.textContent ?? ''));
  } catch (error) {
    if (error instanceof InteractiveExportError && (
      error.code === 'template-incompatible' || error.code === 'snapshot-oversized'
    )) throw error;
    return invalidSnapshot();
  }
  const metadata = document.querySelectorAll<HTMLMetaElement>(
    'meta[name="local-web-interactive-export-template"]',
  );
  if (metadata.length !== 1) templateIncompatible();
  try {
    if (canonicalInteractiveExportJson(decodeEmbeddedJson(metadata[0].content))
      !== canonicalInteractiveExportJson(envelope.template)) templateIncompatible();
  } catch {
    return templateIncompatible();
  }
  return envelope;
}

function validateEnvelopeForContract<Data extends ContextJson, State extends ContextJson>(
  envelope: InteractiveExportEnvelope,
  contract: InteractiveExportContract<Data, State>,
): void {
  if (envelope.template.payloadContractId !== createPayloadContractId(envelope.template.appId, contract)
    || envelope.sensitivity.classification !== contract.sensitivity.classification
    || envelope.sensitivity.notice !== contract.sensitivity.notice) templateIncompatible();
}

export function mountInteractiveExport<Data extends ContextJson, State extends ContextJson>({
  contract,
  render,
  root = document.getElementById('root'),
}: MountInteractiveExportOptions<Data, State>): Root {
  let envelope: InteractiveExportEnvelope;
  let capture: InteractiveExportCapture<Data, State>;
  try {
    envelope = readEmbeddedInteractiveExportEnvelope();
    validateEnvelopeForContract(envelope, contract);
  } catch (error) {
    if (error instanceof InteractiveExportError) throw error;
    return invalidSnapshot();
  }
  try {
    capture = contract.decodeSnapshot({
      snapshotData: envelope.snapshotData,
      viewState: envelope.viewState,
      ...(envelope.title === undefined ? {} : { title: envelope.title }),
    });
  } catch {
    // App decoder errors may include private data, including framework-shaped errors.
    return invalidSnapshot();
  }
  if (!root) return invalidSnapshot();
  const content = render(capture);
  applyColourMode(envelope.capture.effectiveColourMode, document.documentElement);
  const rootHandle = createRoot(root);
  rootHandle.render(
    <InteractiveExportEnvironmentProvider envelope={envelope}>{content}</InteractiveExportEnvironmentProvider>,
  );
  return rootHandle;
}
