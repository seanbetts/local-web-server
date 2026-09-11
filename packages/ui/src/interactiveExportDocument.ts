import {
  InteractiveExportError,
  MAX_INTERACTIVE_EXPORT_ARTIFACT_BYTES,
  canonicalInteractiveExportJson,
  createPayloadContractId,
  detachInteractiveExportJson,
  validateInteractiveExportEnvelope,
} from './interactiveExportModel.js';
import type {
  ContextJson,
} from './contextExportModel.js';
import type {
  InteractiveExportCapture,
  InteractiveExportDefinition,
  InteractiveExportEnvelope,
  InteractiveExportTemplateDescriptor,
} from './interactiveExportModel.js';

export type InteractiveExportArtifact = {
  readonly filename: string;
  readonly html: string;
  readonly byteLength: number;
};

export type BuildInteractiveExportArtifactOptions<Data extends ContextJson, State extends ContextJson> = {
  readonly app: { readonly id: string; readonly name: string };
  readonly definition: InteractiveExportDefinition<Data, State>;
  readonly descriptor: InteractiveExportTemplateDescriptor;
  readonly effectiveColourMode: 'light' | 'dark';
  readonly fetchTemplate: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
  readonly now: () => Date;
  readonly signal?: AbortSignal;
};

type InteractiveExportTemplateIdentity = Omit<InteractiveExportTemplateDescriptor, 'templateUrl'>;

const DESCRIPTOR_META_NAME = 'local-web-interactive-export-descriptor';
const TEMPLATE_META_NAME = 'local-web-interactive-export-template';
const PAYLOAD_TEMPLATE_ATTRIBUTE = 'data-local-web-interactive-export-payload';
const encoder = new TextEncoder();
const decoder = new TextDecoder();

// Decode the opening '<' at runtime: constant string/character operations are folded
// by minifiers and would embed a second literal placeholder in offline UI bundles.
export const INTERACTIVE_EXPORT_PAYLOAD_MARKER = decoder.decode(new Uint8Array([60])) + '!--LOCAL_WEB_INTERACTIVE_EXPORT_PAYLOAD-->';

const captureFailed = (): never => { throw new InteractiveExportError('capture-failed', 'interactive export capture failed'); };
const invalidSnapshot = (): never => { throw new InteractiveExportError('invalid-snapshot', 'interactive export snapshot is invalid'); };
const templateUnavailable = (): never => { throw new InteractiveExportError('template-unavailable', 'interactive export template is unavailable'); };
const templateIncompatible = (): never => { throw new InteractiveExportError('template-incompatible', 'interactive export template is incompatible'); };
const downloadFailed = (): never => { throw new InteractiveExportError('download-failed', 'interactive export download failed'); };

const utf8Bytes = (value: string): number => encoder.encode(value).byteLength;

const decodeBase64Json = (value: string): unknown => {
  const binary = atob(value);
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  return JSON.parse(decoder.decode(bytes));
};

const encodeCanonicalJson = (value: string): string => {
  const bytes = encoder.encode(value);
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
};

const identityFrom = (value: InteractiveExportTemplateDescriptor | InteractiveExportTemplateIdentity): InteractiveExportTemplateIdentity => ({
  templateId: value.templateId,
  payloadContractId: value.payloadContractId,
  compatibilityId: value.compatibilityId,
  appId: value.appId,
  appVersion: value.appVersion,
  sourceRevision: value.sourceRevision,
});

const validateIdentity = (identity: InteractiveExportTemplateIdentity): InteractiveExportTemplateIdentity => {
  validateInteractiveExportEnvelope({
    schema: 'local-web-interactive-export/v1',
    template: identity,
    capture: { capturedAt: '2026-09-08T00:00:00.000Z', effectiveColourMode: 'light' },
    sensitivity: { classification: 'private', notice: 'Interactive export.' },
    snapshotData: {},
    viewState: {},
  });
  return identity;
};

const validateDescriptor = (value: InteractiveExportTemplateDescriptor): InteractiveExportTemplateDescriptor => {
  const identity = validateIdentity(identityFrom(value));
  if (typeof value.templateUrl !== 'string' || value.templateUrl.length === 0) templateIncompatible();
  const templateUrl = new URL(value.templateUrl, document.baseURI);
  const pageUrl = new URL(document.baseURI);
  if (templateUrl.origin !== pageUrl.origin) templateIncompatible();
  const pathSegments = templateUrl.pathname.split('/');
  const fileName = pathSegments.at(-1);
  const parentDirectory = pathSegments.at(-2);
  if (
    templateUrl.search.length > 0
    || templateUrl.hash.length > 0
    || parentDirectory !== 'assets'
    || fileName !== `local-web-interactive-export-${identity.templateId}.html`
  ) templateIncompatible();
  return { ...identity, templateUrl: value.templateUrl };
};

const readSingleMeta = (source: Document, name: string): string => {
  const elements = source.querySelectorAll(`meta[name="${name}"]`);
  if (elements.length !== 1) templateIncompatible();
  const content = elements[0]?.getAttribute('content');
  if (typeof content !== 'string' || content.length === 0) templateIncompatible();
  return content as string;
};

const readTemplateIdentity = (html: string): InteractiveExportTemplateIdentity => {
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  return validateIdentity(decodeBase64Json(readSingleMeta(parsed, TEMPLATE_META_NAME)) as InteractiveExportTemplateIdentity);
};

const templateMatchesDescriptor = (
  identity: InteractiveExportTemplateIdentity,
  descriptor: InteractiveExportTemplateDescriptor,
): boolean => (
  identity.templateId === descriptor.templateId
  && identity.payloadContractId === descriptor.payloadContractId
  && identity.compatibilityId === descriptor.compatibilityId
  && identity.appId === descriptor.appId
  && identity.appVersion === descriptor.appVersion
  && identity.sourceRevision === descriptor.sourceRevision
);

const sanitiseFilenamePart = (value: string): string => value
  .toLowerCase()
  .replace(/[^a-z0-9]+/g, '-')
  .replace(/^-+|-+$/g, '');

const interactiveExportFilename = (appId: string, title: string | undefined, capturedAt: string): string => {
  const app = sanitiseFilenamePart(appId) || 'interactive-export';
  const snapshotTitle = sanitiseFilenamePart(title ?? '') || 'snapshot';
  return `${app}--${snapshotTitle}--${capturedAt.slice(0, 10)}.html`;
};

const createEnvelope = <Data extends ContextJson, State extends ContextJson>(
  options: BuildInteractiveExportArtifactOptions<Data, State>,
  descriptor: InteractiveExportTemplateDescriptor,
  capture: InteractiveExportCapture<Data, State>,
): InteractiveExportEnvelope<Data, State> => {
  const capturedAt = options.now().toISOString();
  return {
    schema: 'local-web-interactive-export/v1',
    template: identityFrom(descriptor),
    capture: { capturedAt, effectiveColourMode: options.effectiveColourMode },
    sensitivity: options.definition.contract.sensitivity,
    ...(capture.title === undefined ? {} : { title: capture.title }),
    snapshotData: capture.snapshotData,
    viewState: capture.viewState,
  };
};

const insertCanonicalEncodedEnvelope = (template: string, encodedEnvelope: string): string => {
  const first = template.indexOf(INTERACTIVE_EXPORT_PAYLOAD_MARKER);
  if (first < 0 || template.indexOf(INTERACTIVE_EXPORT_PAYLOAD_MARKER, first + INTERACTIVE_EXPORT_PAYLOAD_MARKER.length) >= 0) templateIncompatible();
  const payload = `<template ${PAYLOAD_TEMPLATE_ATTRIBUTE}>${encodedEnvelope}</template>`;
  return `${template.slice(0, first)}${payload}${template.slice(first + INTERACTIVE_EXPORT_PAYLOAD_MARKER.length)}`;
};

const requireArtifactLimit = (html: string): void => {
  if (utf8Bytes(html) > MAX_INTERACTIVE_EXPORT_ARTIFACT_BYTES) {
    throw new InteractiveExportError('artifact-oversized', 'interactive export artifact is too large');
  }
};

const createSignal = (): AbortSignal => new AbortController().signal;

export function readInteractiveExportTemplateDescriptor(source: Document = document): InteractiveExportTemplateDescriptor {
  try {
    const descriptor = decodeBase64Json(readSingleMeta(source, DESCRIPTOR_META_NAME)) as InteractiveExportTemplateDescriptor;
    return validateDescriptor(descriptor);
  } catch (error) {
    if (error instanceof InteractiveExportError && error.code === 'template-incompatible') throw error;
    return templateIncompatible();
  }
}

export async function buildInteractiveExportArtifact<Data extends ContextJson, State extends ContextJson>(
  options: BuildInteractiveExportArtifactOptions<Data, State>,
): Promise<InteractiveExportArtifact> {
  const signal = options.signal ?? createSignal();
  try {
    signal.throwIfAborted();
    let builtValue: unknown;
    try {
      builtValue = await options.definition.buildSnapshot({ signal });
    } catch {
      if (signal.aborted) signal.throwIfAborted();
      return captureFailed();
    }
    let built: ContextJson;
    try {
      built = detachInteractiveExportJson(builtValue);
    } catch {
      if (signal.aborted) signal.throwIfAborted();
      return invalidSnapshot();
    }
    signal.throwIfAborted();
    let decoded: unknown;
    try {
      decoded = options.definition.contract.decodeSnapshot(built);
    } catch {
      if (signal.aborted) signal.throwIfAborted();
      return invalidSnapshot();
    }
    let capture: InteractiveExportCapture<Data, State>;
    try {
      capture = detachInteractiveExportJson(decoded) as InteractiveExportCapture<Data, State>;
    } catch {
      if (signal.aborted) signal.throwIfAborted();
      return invalidSnapshot();
    }
    signal.throwIfAborted();
    let descriptor: InteractiveExportTemplateDescriptor;
    try {
      descriptor = validateDescriptor(options.descriptor);
    } catch {
      return templateIncompatible();
    }
    const expectedPayloadContractId = createPayloadContractId(options.app.id, options.definition.contract);
    if (descriptor.appId !== options.app.id || descriptor.payloadContractId !== expectedPayloadContractId) templateIncompatible();
    const envelope = validateInteractiveExportEnvelope<Data, State>(createEnvelope(options, descriptor, capture));
    const encodedEnvelope = encodeCanonicalJson(canonicalInteractiveExportJson(envelope));
    const filename = interactiveExportFilename(options.app.id, envelope.title, envelope.capture.capturedAt);
    signal.throwIfAborted();
    let response: Response;
    try {
      response = await options.fetchTemplate(descriptor.templateUrl, { signal });
    } catch {
      if (signal.aborted) signal.throwIfAborted();
      return templateUnavailable();
    }
    signal.throwIfAborted();
    if (!response.ok) templateUnavailable();
    let template: string;
    try {
      template = await response.text();
    } catch {
      if (signal.aborted) signal.throwIfAborted();
      return templateUnavailable();
    }
    signal.throwIfAborted();
    let html: string;
    try {
      if (!templateMatchesDescriptor(readTemplateIdentity(template), descriptor)) templateIncompatible();
      html = insertCanonicalEncodedEnvelope(template, encodedEnvelope);
    } catch {
      return templateIncompatible();
    }
    requireArtifactLimit(html);
    return {
      html,
      filename,
      byteLength: utf8Bytes(html),
    };
  } catch (error) {
    if (signal.aborted) {
      signal.throwIfAborted();
    }
    if (error instanceof InteractiveExportError) throw error;
    return invalidSnapshot();
  }
}

export function downloadInteractiveExport(artifact: InteractiveExportArtifact): void {
  let objectUrl: string | undefined;
  let anchor: HTMLAnchorElement | undefined;
  try {
    const blob = new Blob([artifact.html], { type: 'text/html;charset=utf-8' });
    objectUrl = URL.createObjectURL(blob);
    anchor = document.createElement('a');
    anchor.href = objectUrl;
    anchor.download = artifact.filename;
    anchor.hidden = true;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    const urlToRevoke = objectUrl;
    setTimeout(() => URL.revokeObjectURL(urlToRevoke), 0);
  } catch {
    try {
      anchor?.remove();
    } catch {
      // The generic browser-download error intentionally hides implementation details.
    }
    try {
      if (objectUrl !== undefined) URL.revokeObjectURL(objectUrl);
    } catch {
      // The generic browser-download error intentionally hides implementation details.
    }
    downloadFailed();
  }
}
