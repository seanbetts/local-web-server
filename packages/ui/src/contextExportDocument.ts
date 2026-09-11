import {
  ContextExportError,
  MAX_CONTEXT_EXPORT_BYTES,
  validateLocalWebContext,
} from './contextExportModel.js';
import type {
  ContextBlock,
  ContextJson,
  ContextScalar,
  LocalWebContextV1,
} from './contextExportModel.js';

export type ContextExportArtifact = {
  readonly filename: string;
  readonly html: string;
  readonly byteLength: number;
};

const DOCUMENT_CSP = "default-src 'none'; connect-src 'none'; img-src 'none'; font-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; script-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'";
const DOWNLOAD_ERROR_MESSAGE = 'Context export could not be downloaded.';
const OVERSIZED_ERROR_MESSAGE = 'Context export is larger than 5 MiB. Reduce the app-owned context data.';
const STATIC_SNAPSHOT_NOTICE = 'This is a static snapshot. Changes do not sync to the source app.';
const DOCUMENT_STYLE = 'body{font-family:system-ui,sans-serif;line-height:1.5;max-width:72rem;margin:2rem auto;padding:0 1rem;color:#161616;background:#fff}header,section{margin-block:2rem}.sensitivity{border:1px solid #7a4d00;background:#fff6dc;padding:1rem;font-weight:600}dl{display:grid;grid-template-columns:max-content 1fr;gap:.35rem 1rem}dt{font-weight:600}dd{margin:0}table{border-collapse:collapse;width:100%}th,td{border:1px solid #bbb;padding:.45rem;text-align:left;vertical-align:top}code{white-space:pre-wrap;overflow-wrap:anywhere}';

const htmlEscape = (value: string): string => value.replace(/[&<>'"]/g, (character) => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  "'": '&#39;',
  '"': '&quot;',
}[character] ?? character));

const scalarText = (value: ContextScalar): string => {
  if (typeof value === 'number' && Object.is(value, -0)) {
    return '-0';
  }
  return value === null ? 'null' : String(value);
};

const canonicalJson = (value: ContextJson): string => {
  if (typeof value === 'number') {
    return Object.is(value, -0) ? '-0' : JSON.stringify(value);
  }
  if (value === null || typeof value === 'boolean' || typeof value === 'string') {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(',')}]`;
  }
  const record = value as { readonly [key: string]: ContextJson };
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key]!)}`).join(',')}}`;
};

const jsonForHtml = (value: ContextJson): string => canonicalJson(value).replace(/[<>&\u2028\u2029]/g, (character) => ({
  '<': '\\u003C',
  '>': '\\u003E',
  '&': '\\u0026',
  '\u2028': '\\u2028',
  '\u2029': '\\u2029',
}[character] ?? character));

const list = (items: readonly string[], attribute: string): string =>
  `<ul ${attribute}>${items.map((item) => `<li>${htmlEscape(item)}</li>`).join('')}</ul>`;

const metadataRow = (label: string, value: string | null): string =>
  `<dt>${htmlEscape(label)}</dt><dd>${htmlEscape(value ?? 'Not recorded')}</dd>`;

const renderBlock = (block: ContextBlock): string => {
  if (block.type === 'paragraph') {
    return `<p>${htmlEscape(block.text)}</p>`;
  }
  if (block.type === 'key-values') {
    return `<dl>${block.items.map((item) => metadataRow(item.label, scalarText(item.value))).join('')}</dl>`;
  }
  if (block.type === 'list') {
    const tag = block.ordered ? 'ol' : 'ul';
    return `<${tag}>${block.items.map((item) => `<li>${htmlEscape(item)}</li>`).join('')}</${tag}>`;
  }
  return `<table><thead><tr>${block.columns.map((column) => `<th scope="col">${htmlEscape(column.label)}</th>`).join('')}</tr></thead><tbody>${block.rows.map((row) => `<tr>${block.columns.map((column) => `<td>${htmlEscape(scalarText(row[column.key]!))}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
};

const renderSections = (context: LocalWebContextV1): string => context.sections.map((section) =>
  `<section data-context-export-section="${htmlEscape(section.id)}"><h2>${htmlEscape(section.title)}</h2>${section.blocks.map(renderBlock).join('')}</section>`,
).join('');

const renderProvenance = (context: LocalWebContextV1): string =>
  `<section data-context-export-provenance><h2>Provenance</h2><p>${htmlEscape(context.provenance.freshness)}</p>${context.provenance.sources.map((source) => `<dl>${metadataRow('Source', source.label)}${metadataRow('Observed at', source.observedAt)}${metadataRow('Revision', source.revision)}</dl>`).join('')}</section>`;

const renderDecisions = (context: LocalWebContextV1): string =>
  `<section data-context-export-decisions><h2>Decisions</h2>${context.decisions.map((decision) => `<dl><dt>Statement</dt><dd>${htmlEscape(decision.statement)}</dd><dt>Reasoning</dt><dd>${htmlEscape(decision.reasoning)}</dd></dl>`).join('')}</section>`;

const sanitiseFilenamePart = (value: string): string => value
  .toLowerCase()
  .replace(/[^a-z0-9]+/g, '-')
  .replace(/^-+|-+$/g, '');

const filenameFor = (context: LocalWebContextV1): string => {
  const app = sanitiseFilenamePart(context.app.id) || 'context';
  const title = sanitiseFilenamePart(context.context.title) || 'context';
  return `${app}--${title}--${context.context.generatedAt.slice(0, 10)}.html`;
};

const renderDocument = (context: LocalWebContextV1): string => {
  const title = `${context.context.title} — ${context.app.name}`;
  const metadata = [
    metadataRow('Application', context.app.name),
    metadataRow('Application ID', context.app.id),
    metadataRow('Version', context.app.version),
    metadataRow('Source revision', context.app.sourceRevision),
    metadataRow('Scope', context.context.scope),
    metadataRow('Active route', context.context.activeRoute),
    metadataRow('Generated at', context.context.generatedAt),
    metadataRow('Observed at', context.context.observedAt),
    metadataRow('Data revision', context.context.dataRevision),
  ].join('');

  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="${DOCUMENT_CSP}"><meta name="viewport" content="width=device-width, initial-scale=1"><title>${htmlEscape(title)}</title><style>${DOCUMENT_STYLE}</style></head><body><header><h1>${htmlEscape(context.context.title)}</h1><p class="sensitivity" data-context-export-sensitivity><strong data-context-export-classification>${htmlEscape(context.sensitivity.classification.toUpperCase())}</strong>: ${htmlEscape(context.sensitivity.notice)}</p><p data-context-export-snapshot>${STATIC_SNAPSHOT_NOTICE}</p><dl data-context-export-metadata>${metadata}</dl></header><main><section data-context-export-summary><h2>Summary</h2><p>${htmlEscape(context.summary)}</p></section><section data-context-export-capabilities><h2>Capabilities</h2>${list(context.capabilities, 'data-context-export-capabilities-list')}</section>${renderSections(context)}${renderProvenance(context)}<section data-context-export-assumptions><h2>Assumptions</h2>${list(context.assumptions, 'data-context-export-assumptions-list')}</section>${renderDecisions(context)}<section data-context-export-caveats><h2>Caveats</h2>${list(context.caveats, 'data-context-export-caveats-list')}</section><section data-context-export-omissions><h2>Omissions</h2>${list(context.omissions, 'data-context-export-omissions-list')}</section></main><script type="application/json" data-context-export-json>${jsonForHtml(context as ContextJson)}</script></body></html>`;
};

export function renderContextExport(value: unknown): ContextExportArtifact {
  const context = validateLocalWebContext(value);
  const html = renderDocument(context);
  const byteLength = new TextEncoder().encode(html).byteLength;
  if (byteLength > MAX_CONTEXT_EXPORT_BYTES) {
    throw new ContextExportError('oversized', OVERSIZED_ERROR_MESSAGE);
  }
  return { filename: filenameFor(context), html, byteLength };
}

export function downloadContextExport(artifact: ContextExportArtifact): void {
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
      // The browser-download error below intentionally hides implementation details.
    }
    try {
      if (objectUrl !== undefined) {
        URL.revokeObjectURL(objectUrl);
      }
    } catch {
      // The browser-download error below intentionally hides implementation details.
    }
    throw new ContextExportError('download', DOWNLOAD_ERROR_MESSAGE);
  }
}
