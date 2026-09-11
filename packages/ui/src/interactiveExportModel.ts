import type { ContextJson } from './contextExportModel.js';

export const LOCAL_WEB_INTERACTIVE_EXPORT_SCHEMA = 'local-web-interactive-export/v1' as const;
export const MAX_INTERACTIVE_EXPORT_SNAPSHOT_BYTES = 5 * 1024 * 1024;
export const MAX_INTERACTIVE_EXPORT_ARTIFACT_BYTES = 20 * 1024 * 1024;

export type InteractiveExportSensitivity = {
  readonly classification: 'private' | 'sensitive';
  readonly notice: string;
};

export type InteractiveExportCapture<Data extends ContextJson, State extends ContextJson> = {
  readonly snapshotData: Data;
  readonly viewState: State;
  readonly title?: string;
};

export type InteractiveExportContract<Data extends ContextJson, State extends ContextJson> = {
  readonly id: string;
  readonly version: number;
  readonly sensitivity: InteractiveExportSensitivity;
  readonly decodeSnapshot: (value: unknown) => InteractiveExportCapture<Data, State>;
};

export type InteractiveExportDefinition<Data extends ContextJson, State extends ContextJson> = {
  readonly contract: InteractiveExportContract<Data, State>;
  readonly buildSnapshot: (request: { readonly signal: AbortSignal }) =>
    Promise<InteractiveExportCapture<Data, State>>;
};

type InteractiveExportTemplateIdentity = {
  readonly templateId: string;
  readonly payloadContractId: string;
  readonly compatibilityId: string;
  readonly appId: string;
  readonly appVersion: string;
  readonly sourceRevision: string;
};

export type InteractiveExportTemplateDescriptor = InteractiveExportTemplateIdentity & {
  readonly templateUrl: string;
};

export type InteractiveExportEnvelope<Data extends ContextJson = ContextJson, State extends ContextJson = ContextJson> = {
  readonly schema: typeof LOCAL_WEB_INTERACTIVE_EXPORT_SCHEMA;
  readonly template: InteractiveExportTemplateIdentity;
  readonly capture: { readonly capturedAt: string; readonly effectiveColourMode: 'light' | 'dark' };
  readonly sensitivity: InteractiveExportSensitivity;
  readonly title?: string;
  readonly snapshotData: Data;
  readonly viewState: State;
};

export type InteractiveExportErrorCode =
  | 'capture-failed'
  | 'invalid-snapshot'
  | 'template-unavailable'
  | 'template-incompatible'
  | 'snapshot-oversized'
  | 'artifact-oversized'
  | 'packaging-failed'
  | 'download-failed';

export class InteractiveExportError extends Error {
  readonly code: InteractiveExportErrorCode;

  constructor(code: InteractiveExportErrorCode, message: string) {
    super(message);
    this.name = 'InteractiveExportError';
    this.code = code;
  }
}

type UnknownRecord = Record<string, unknown>;

const CANONICAL_ID = /^[a-z][a-z0-9-]*$/;
const DIGEST = /^[a-f0-9]{64}$/;
const PAYLOAD_CONTRACT_ID = /^[a-z][a-z0-9-]*\/[a-z][a-z0-9-]*\/v[1-9][0-9]*$/;
const encoder = new TextEncoder();

const fail = (): never => {
  throw new Error('invalid interactive export');
};

const requireNonEmptyString = (value: unknown): string => {
  if (typeof value !== 'string' || value.trim().length === 0) fail();
  return value as string;
};

const requireCanonicalId = (value: unknown): string => {
  const id = requireNonEmptyString(value);
  if (!CANONICAL_ID.test(id)) fail();
  return id;
};

const requirePositiveInteger = (value: unknown): number => {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1) fail();
  return value as number;
};

const requireExactKeys = (value: UnknownRecord, keys: readonly string[]): void => {
  const actual = Reflect.ownKeys(value);
  if (actual.length !== keys.length || actual.some((key) => typeof key !== 'string' || !keys.includes(key))) fail();
};

const requirePlainRecord = (value: unknown): UnknownRecord => {
  if (value === null || typeof value !== 'object' || Object.getPrototypeOf(value) !== Object.prototype) fail();
  return value as UnknownRecord;
};

const ARRAY_INDEX = /^(0|[1-9][0-9]*)$/;

const requireDataDescriptor = (value: PropertyDescriptor | undefined): PropertyDescriptor & { value: unknown } => {
  if (value === undefined || !('value' in value)) fail();
  return value as PropertyDescriptor & { value: unknown };
};

const detachJson = (value: unknown, seen: WeakSet<object>): ContextJson => {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return value;
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) fail();
    return value;
  }
  if (typeof value !== 'object') fail();

  const object = value as object;
  if (seen.has(object)) fail();
  const isArray = Array.isArray(object);
  const prototype = Object.getPrototypeOf(object);
  const descriptors = Object.getOwnPropertyDescriptors(object);
  seen.add(object);
  try {
    if (isArray) {
      if (prototype !== Array.prototype) fail();
      const lengthDescriptor = requireDataDescriptor(descriptors.length);
      const length = lengthDescriptor.value;
      if (!Number.isSafeInteger(length) || length < 0 || lengthDescriptor.enumerable) fail();
      const keys = Reflect.ownKeys(descriptors);
      if (
        keys.length !== length + 1
        || keys.some((key) => key !== 'length' && (
          typeof key !== 'string'
          || !ARRAY_INDEX.test(key)
          || Number(key) >= length
        ))
      ) fail();
      const clone: ContextJson[] = Array<ContextJson>(length);
      for (let index = 0; index < length; index += 1) {
        const descriptor = requireDataDescriptor(descriptors[String(index)]);
        if (!descriptor.enumerable) fail();
        clone[index] = detachJson(descriptor.value, seen);
      }
      return clone;
    }

    if (prototype !== Object.prototype) fail();
    const clone: Record<string, ContextJson> = {};
    for (const key of Reflect.ownKeys(descriptors)) {
      if (typeof key !== 'string') fail();
      const descriptor = requireDataDescriptor(descriptors[key as string]);
      if (!descriptor.enumerable) fail();
      Object.defineProperty(clone, key, {
        configurable: true,
        enumerable: true,
        value: detachJson(descriptor.value, seen),
        writable: true,
      });
    }
    return clone;
  } finally {
    seen.delete(object);
  }
};

const canonicalJson = (value: unknown): string => {
  if (value === null || typeof value === 'boolean' || typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') return Object.is(value, -0) ? '0' : JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map((item) => canonicalJson(item)).join(',')}]`;
  const record = value as UnknownRecord;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(',')}}`;
};

const validateSensitivity = (value: unknown): InteractiveExportSensitivity => {
  const sensitivity = requirePlainRecord(value);
  requireExactKeys(sensitivity, ['classification', 'notice']);
  if (sensitivity.classification !== 'private' && sensitivity.classification !== 'sensitive') fail();
  requireNonEmptyString(sensitivity.notice);
  return value as InteractiveExportSensitivity;
};

const validateTemplate = (value: unknown): InteractiveExportTemplateIdentity => {
  const template = requirePlainRecord(value);
  requireExactKeys(template, ['templateId', 'payloadContractId', 'compatibilityId', 'appId', 'appVersion', 'sourceRevision']);
  if (!DIGEST.test(requireNonEmptyString(template.templateId))) fail();
  if (!PAYLOAD_CONTRACT_ID.test(requireNonEmptyString(template.payloadContractId))) fail();
  if (!DIGEST.test(requireNonEmptyString(template.compatibilityId))) fail();
  requireCanonicalId(template.appId);
  requireNonEmptyString(template.appVersion);
  requireNonEmptyString(template.sourceRevision);
  return value as InteractiveExportTemplateIdentity;
};

const validateCapturedAt = (value: unknown): void => {
  const timestamp = requireNonEmptyString(value);
  try {
    if (new Date(timestamp).toISOString() !== timestamp) fail();
  } catch {
    fail();
  }
};

export function defineInteractiveExportContract<Data extends ContextJson, State extends ContextJson>(
  contract: InteractiveExportContract<Data, State>,
): InteractiveExportContract<Data, State> {
  try {
    const candidate = requirePlainRecord(contract);
    requireExactKeys(candidate, ['id', 'version', 'sensitivity', 'decodeSnapshot']);
    requireCanonicalId(candidate.id);
    requirePositiveInteger(candidate.version);
    validateSensitivity(candidate.sensitivity);
    if (typeof candidate.decodeSnapshot !== 'function') fail();
    return contract;
  } catch {
    throw new InteractiveExportError('invalid-snapshot', 'interactive export snapshot is invalid');
  }
}

export function createPayloadContractId<Data extends ContextJson, State extends ContextJson>(
  appId: string,
  contract: InteractiveExportContract<Data, State>,
): string {
  const defined = defineInteractiveExportContract(contract);
  try {
    return `${requireCanonicalId(appId)}/${defined.id}/v${defined.version}`;
  } catch {
    throw new InteractiveExportError('invalid-snapshot', 'interactive export snapshot is invalid');
  }
}

export function canonicalInteractiveExportJson(value: unknown): string {
  try {
    return canonicalJson(detachJson(value, new WeakSet<object>()));
  } catch {
    throw new InteractiveExportError('invalid-snapshot', 'interactive export snapshot is invalid');
  }
}

export function detachInteractiveExportJson(value: unknown): ContextJson {
  try {
    return detachJson(value, new WeakSet<object>());
  } catch {
    throw new InteractiveExportError('invalid-snapshot', 'interactive export snapshot is invalid');
  }
}

const rightRotate = (value: number, amount: number): number => (value >>> amount) | (value << (32 - amount));

const sha256 = (value: string): string => {
  const bytes = [...encoder.encode(value)];
  const bitLength = bytes.length * 8;
  bytes.push(0x80);
  while ((bytes.length % 64) !== 56) bytes.push(0);
  for (let index = 7; index >= 0; index -= 1) bytes.push(Math.floor(bitLength / 2 ** (index * 8)) & 0xff);

  const hash = [
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
    0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ];
  const constants = [
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ];
  for (let offset = 0; offset < bytes.length; offset += 64) {
    const words = Array<number>(64).fill(0);
    for (let index = 0; index < 16; index += 1) words[index] = ((bytes[offset + index * 4] << 24) | (bytes[offset + index * 4 + 1] << 16) | (bytes[offset + index * 4 + 2] << 8) | bytes[offset + index * 4 + 3]) >>> 0;
    for (let index = 16; index < 64; index += 1) {
      const x = words[index - 15];
      const y = words[index - 2];
      words[index] = (words[index - 16] + (rightRotate(x, 7) ^ rightRotate(x, 18) ^ (x >>> 3)) + words[index - 7] + (rightRotate(y, 17) ^ rightRotate(y, 19) ^ (y >>> 10))) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = hash;
    for (let index = 0; index < 64; index += 1) {
      const s1 = rightRotate(e, 6) ^ rightRotate(e, 11) ^ rightRotate(e, 25);
      const choose = (e & f) ^ (~e & g);
      const temp1 = (h + s1 + choose + constants[index] + words[index]) >>> 0;
      const s0 = rightRotate(a, 2) ^ rightRotate(a, 13) ^ rightRotate(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (s0 + majority) >>> 0;
      [h, g, f, e, d, c, b, a] = [g, f, e, (d + temp1) >>> 0, c, b, a, (temp1 + temp2) >>> 0];
    }
    hash[0] = (hash[0] + a) >>> 0; hash[1] = (hash[1] + b) >>> 0;
    hash[2] = (hash[2] + c) >>> 0; hash[3] = (hash[3] + d) >>> 0;
    hash[4] = (hash[4] + e) >>> 0; hash[5] = (hash[5] + f) >>> 0;
    hash[6] = (hash[6] + g) >>> 0; hash[7] = (hash[7] + h) >>> 0;
  }
  return hash.map((part) => part.toString(16).padStart(8, '0')).join('');
};

export function createCompatibilityId(input: {
  readonly appId: string;
  readonly payloadContractId: string;
  readonly templateId: string;
}): string {
  try {
    const identity = {
      schema: LOCAL_WEB_INTERACTIVE_EXPORT_SCHEMA,
      appId: requireCanonicalId(input.appId),
      payloadContractId: requireNonEmptyString(input.payloadContractId),
      templateId: requireNonEmptyString(input.templateId),
    };
    if (!PAYLOAD_CONTRACT_ID.test(identity.payloadContractId) || !DIGEST.test(identity.templateId)) fail();
    return sha256(canonicalInteractiveExportJson(identity));
  } catch {
    throw new InteractiveExportError('template-incompatible', 'interactive export template is incompatible');
  }
}

export function validateInteractiveExportEnvelope<Data extends ContextJson = ContextJson, State extends ContextJson = ContextJson>(
  value: unknown,
): InteractiveExportEnvelope<Data, State> {
  try {
    const envelope = requirePlainRecord(detachInteractiveExportJson(value));
    const keys = ['schema', 'template', 'capture', 'sensitivity', 'snapshotData', 'viewState'];
    if (Object.prototype.hasOwnProperty.call(envelope, 'title')) keys.push('title');
    requireExactKeys(envelope, keys);
    if (envelope.schema !== LOCAL_WEB_INTERACTIVE_EXPORT_SCHEMA) fail();
    let template: InteractiveExportTemplateIdentity;
    try {
      template = validateTemplate(envelope.template);
      if (!template.payloadContractId.startsWith(`${template.appId}/`)) fail();
      if (template.compatibilityId !== createCompatibilityId({
        appId: template.appId,
        payloadContractId: template.payloadContractId,
        templateId: template.templateId,
      })) fail();
    } catch (error) {
      if (error instanceof InteractiveExportError && error.code === 'template-incompatible') throw error;
      throw new InteractiveExportError('template-incompatible', 'interactive export template is incompatible');
    }
    const capture = requirePlainRecord(envelope.capture);
    requireExactKeys(capture, ['capturedAt', 'effectiveColourMode']);
    validateCapturedAt(capture.capturedAt);
    if (capture.effectiveColourMode !== 'light' && capture.effectiveColourMode !== 'dark') fail();
    validateSensitivity(envelope.sensitivity);
    if (Object.prototype.hasOwnProperty.call(envelope, 'title')) requireNonEmptyString(envelope.title);
    const snapshot = canonicalInteractiveExportJson({
      snapshotData: envelope.snapshotData,
      viewState: envelope.viewState,
      ...(Object.prototype.hasOwnProperty.call(envelope, 'title') ? { title: envelope.title } : {}),
    });
    if (encoder.encode(snapshot).byteLength > MAX_INTERACTIVE_EXPORT_SNAPSHOT_BYTES) {
      throw new InteractiveExportError('snapshot-oversized', 'interactive export snapshot is too large');
    }
    return envelope as InteractiveExportEnvelope<Data, State>;
  } catch (error) {
    if (error instanceof InteractiveExportError) throw error;
    throw new InteractiveExportError('invalid-snapshot', 'interactive export snapshot is invalid');
  }
}
