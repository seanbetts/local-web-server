export const LOCAL_WEB_CONTEXT_SCHEMA = 'local-web-context/v1' as const;
export const MAX_CONTEXT_EXPORT_BYTES = 5 * 1024 * 1024;

export type ContextSensitivity = 'private' | 'sensitive';
export type ContextScalar = null | boolean | number | string;
export type ContextJson = ContextScalar | readonly ContextJson[] | {
  readonly [key: string]: ContextJson;
};
export type ContextBlock =
  | { readonly type: 'paragraph'; readonly text: string }
  | { readonly type: 'key-values'; readonly items: readonly { readonly label: string; readonly value: ContextScalar }[] }
  | { readonly type: 'list'; readonly ordered: boolean; readonly items: readonly string[] }
  | { readonly type: 'table'; readonly columns: readonly { readonly key: string; readonly label: string }[]; readonly rows: readonly Readonly<Record<string, ContextScalar>>[] };
export type LocalWebContextV1 = {
  readonly schema: typeof LOCAL_WEB_CONTEXT_SCHEMA;
  readonly app: { readonly id: string; readonly name: string; readonly version: string; readonly sourceRevision: string };
  readonly context: { readonly title: string; readonly scope: string; readonly activeRoute: string | null; readonly generatedAt: string; readonly observedAt: string | null; readonly dataRevision: string | null };
  readonly sensitivity: { readonly classification: ContextSensitivity; readonly notice: string };
  readonly summary: string;
  readonly capabilities: readonly string[];
  readonly sections: readonly { readonly id: string; readonly title: string; readonly blocks: readonly ContextBlock[] }[];
  readonly data: Readonly<Record<string, ContextJson>>;
  readonly provenance: { readonly freshness: string; readonly sources: readonly { readonly label: string; readonly observedAt: string | null; readonly revision: string | null }[] };
  readonly assumptions: readonly string[];
  readonly decisions: readonly { readonly statement: string; readonly reasoning: string }[];
  readonly caveats: readonly string[];
  readonly omissions: readonly string[];
};
export type ContextExportRequest = { readonly signal: AbortSignal };
export type ContextExportBuilder = (request: ContextExportRequest) => Promise<LocalWebContextV1>;

export class ContextExportError extends Error {
  readonly code: 'invalid' | 'oversized' | 'download';

  constructor(code: 'invalid' | 'oversized' | 'download', message: string) {
    super(message);
    this.name = 'ContextExportError';
    this.code = code;
  }
}

type UnknownRecord = Record<string, unknown>;

const fail = (): never => {
  throw new Error('invalid context');
};

const requireNonEmptyString = (value: unknown): string => {
  if (typeof value !== 'string') {
    fail();
  }
  const string = value as string;
  if (string.trim().length === 0) {
    fail();
  }
  return string;
};

const isFiniteScalar = (value: unknown): value is ContextScalar =>
  value === null
  || typeof value === 'boolean'
  || typeof value === 'string'
  || (typeof value === 'number' && Number.isFinite(value));

const requireScalar = (value: unknown): ContextScalar => {
  if (!isFiniteScalar(value)) {
    fail();
  }
  return value as ContextScalar;
};

const requireStrictTimestamp = (value: unknown): string => {
  const timestamp = requireNonEmptyString(value);
  try {
    if (new Date(timestamp).toISOString() !== timestamp) {
      fail();
    }
  } catch {
    fail();
  }
  return timestamp;
};

const requireNullableString = (value: unknown): string | null =>
  value === null ? null : requireNonEmptyString(value);

const requireNullableTimestamp = (value: unknown): string | null =>
  value === null ? null : requireStrictTimestamp(value);

const requireArray = (value: unknown): unknown[] => {
  if (!Array.isArray(value)) {
    fail();
  }
  const array = value as unknown[];
  const keys = Reflect.ownKeys(array);
  if (
    keys.length !== array.length + 1
    || keys.some((key) =>
      key !== 'length'
      && (
        typeof key !== 'string'
        || !Number.isInteger(Number(key))
        || Number(key) < 0
        || Number(key) >= array.length
        || String(Number(key)) !== key
      ))
  ) {
    fail();
  }
  return array;
};

const isPlainRecord = (value: unknown): value is UnknownRecord =>
  value !== null && typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype;

const requireRecord = (value: unknown): UnknownRecord => {
  if (!isPlainRecord(value)) {
    fail();
  }
  const record = value as UnknownRecord;
  return record;
};

const requireExactRecord = (
  value: unknown,
  keys: readonly string[],
): UnknownRecord => {
  const record = requireRecord(value);
  const actual = Reflect.ownKeys(record);
  if (
    actual.length !== keys.length
    || actual.some((key) => typeof key !== 'string' || !keys.includes(key))
  ) {
    fail();
  }
  return record;
};

const withContainer = <T>(
  value: object,
  seen: WeakSet<object>,
  validate: () => T,
): T => {
  if (seen.has(value)) {
    fail();
  }
  seen.add(value);
  try {
    return validate();
  } finally {
    seen.delete(value);
  }
};

const withArray = <T>(
  value: unknown,
  seen: WeakSet<object>,
  validate: (array: unknown[]) => T,
): T => {
  const array = requireArray(value);
  return withContainer(array, seen, () => validate(array));
};

const withRecord = <T>(
  value: unknown,
  seen: WeakSet<object>,
  validate: (record: UnknownRecord) => T,
): T => {
  const record = requireRecord(value);
  return withContainer(record, seen, () => validate(record));
};

const withExactRecord = <T>(
  value: unknown,
  keys: readonly string[],
  seen: WeakSet<object>,
  validate: (record: UnknownRecord) => T,
): T => {
  const record = requireExactRecord(value, keys);
  return withContainer(record, seen, () => validate(record));
};

const validateStringArray = (value: unknown, seen: WeakSet<object>): void => {
  withArray(value, seen, (items) => {
    for (const item of items) {
      requireNonEmptyString(item);
    }
  });
};

const validateContextJson = (value: unknown, seen: WeakSet<object>): void => {
  if (isFiniteScalar(value)) {
    return;
  }
  if (Array.isArray(value)) {
    withArray(value, seen, (items) => {
      for (const item of items) {
        validateContextJson(item, seen);
      }
    });
    return;
  }
  withRecord(value, seen, (record) => {
    for (const key of Reflect.ownKeys(record)) {
      if (typeof key === 'string') {
        validateContextJson(record[key], seen);
        continue;
      }
      fail();
    }
  });
};

const validateBlock = (value: unknown, seen: WeakSet<object>): void => {
  if (!isPlainRecord(value)) {
    fail();
  }
  const type = (value as UnknownRecord).type;

  if (type === 'paragraph') {
    withExactRecord(value, ['type', 'text'], seen, (block) => {
      requireNonEmptyString(block.text);
    });
    return;
  }
  if (type === 'key-values') {
    withExactRecord(value, ['type', 'items'], seen, (block) => {
      withArray(block.items, seen, (items) => {
        for (const item of items) {
          withExactRecord(item, ['label', 'value'], seen, (keyValue) => {
            requireNonEmptyString(keyValue.label);
            requireScalar(keyValue.value);
          });
        }
      });
    });
    return;
  }
  if (type === 'list') {
    withExactRecord(value, ['type', 'ordered', 'items'], seen, (block) => {
      if (typeof block.ordered !== 'boolean') {
        fail();
      }
      validateStringArray(block.items, seen);
    });
    return;
  }
  if (type === 'table') {
    withExactRecord(value, ['type', 'columns', 'rows'], seen, (block) => {
      const keys = new Set<string>();
      withArray(block.columns, seen, (columns) => {
        for (const column of columns) {
          withExactRecord(column, ['key', 'label'], seen, (tableColumn) => {
            const key = requireNonEmptyString(tableColumn.key);
            requireNonEmptyString(tableColumn.label);
            if (keys.has(key)) {
              fail();
            }
            keys.add(key);
          });
        }
      });
      withArray(block.rows, seen, (rows) => {
        for (const row of rows) {
          withRecord(row, seen, (tableRow) => {
            const rowKeys = Reflect.ownKeys(tableRow);
            if (
              rowKeys.length !== keys.size
              || rowKeys.some((key) => typeof key !== 'string' || !keys.has(key))
            ) {
              fail();
            }
            for (const key of keys) {
              requireScalar(tableRow[key]);
            }
          });
        }
      });
    });
    return;
  }
  fail();
};

const validateLocalWebContextOrThrow = (value: unknown): LocalWebContextV1 => {
  const seen = new WeakSet<object>();
  return withExactRecord(value, [
    'schema',
    'app',
    'context',
    'sensitivity',
    'summary',
    'capabilities',
    'sections',
    'data',
    'provenance',
    'assumptions',
    'decisions',
    'caveats',
    'omissions',
  ], seen, (context) => {
    if (context.schema !== LOCAL_WEB_CONTEXT_SCHEMA) {
      fail();
    }

    withExactRecord(context.app, ['id', 'name', 'version', 'sourceRevision'], seen, (app) => {
      requireNonEmptyString(app.id);
      requireNonEmptyString(app.name);
      requireNonEmptyString(app.version);
      requireNonEmptyString(app.sourceRevision);
    });

    withExactRecord(context.context, [
      'title',
      'scope',
      'activeRoute',
      'generatedAt',
      'observedAt',
      'dataRevision',
    ], seen, (detail) => {
      requireNonEmptyString(detail.title);
      requireNonEmptyString(detail.scope);
      requireNullableString(detail.activeRoute);
      requireStrictTimestamp(detail.generatedAt);
      requireNullableTimestamp(detail.observedAt);
      requireNullableString(detail.dataRevision);
    });

    withExactRecord(context.sensitivity, ['classification', 'notice'], seen, (sensitivity) => {
      if (sensitivity.classification !== 'private' && sensitivity.classification !== 'sensitive') {
        fail();
      }
      requireNonEmptyString(sensitivity.notice);
    });
    requireNonEmptyString(context.summary);
    validateStringArray(context.capabilities, seen);

    const sectionIds = new Set<string>();
    withArray(context.sections, seen, (sections) => {
      for (const section of sections) {
        withExactRecord(section, ['id', 'title', 'blocks'], seen, (sectionRecord) => {
          const id = requireNonEmptyString(sectionRecord.id);
          if (sectionIds.has(id)) {
            fail();
          }
          sectionIds.add(id);
          requireNonEmptyString(sectionRecord.title);
          withArray(sectionRecord.blocks, seen, (blocks) => {
            if (blocks.length === 0) {
              fail();
            }
            for (const block of blocks) {
              validateBlock(block, seen);
            }
          });
        });
      }
    });

    withRecord(context.data, seen, (data) => {
      for (const key of Reflect.ownKeys(data)) {
        if (typeof key === 'string') {
          validateContextJson(data[key], seen);
          continue;
        }
        fail();
      }
    });

    withExactRecord(context.provenance, ['freshness', 'sources'], seen, (provenance) => {
      requireNonEmptyString(provenance.freshness);
      withArray(provenance.sources, seen, (sources) => {
        for (const source of sources) {
          withExactRecord(source, ['label', 'observedAt', 'revision'], seen, (sourceRecord) => {
            requireNonEmptyString(sourceRecord.label);
            requireNullableTimestamp(sourceRecord.observedAt);
            requireNullableString(sourceRecord.revision);
          });
        }
      });
    });

    validateStringArray(context.assumptions, seen);
    withArray(context.decisions, seen, (decisions) => {
      for (const decision of decisions) {
        withExactRecord(decision, ['statement', 'reasoning'], seen, (decisionRecord) => {
          requireNonEmptyString(decisionRecord.statement);
          requireNonEmptyString(decisionRecord.reasoning);
        });
      }
    });
    validateStringArray(context.caveats, seen);
    validateStringArray(context.omissions, seen);

    return value as LocalWebContextV1;
  });
};

export function validateLocalWebContext(value: unknown): LocalWebContextV1 {
  try {
    return validateLocalWebContextOrThrow(value);
  } catch {
    throw new ContextExportError('invalid', 'context export is invalid');
  }
}
