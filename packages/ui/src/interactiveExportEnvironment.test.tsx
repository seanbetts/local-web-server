import { act, render, screen } from '@testing-library/react';
import type { Root } from 'react-dom/client';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as ui from './index.js';
import { encodeOfflineJson, offlineContract, offlineEnvelope } from './test/interactiveExport.js';

function embed(envelope = offlineEnvelope()) {
  document.head.innerHTML = `<meta name="local-web-interactive-export-template" content="${encodeOfflineJson(envelope.template)}">`;
  document.body.innerHTML = `<div id="root"></div><template data-local-web-interactive-export-payload>${encodeOfflineJson(envelope)}</template>`;
}

afterEach(() => {
  document.head.replaceChildren();
  document.body.replaceChildren();
  document.documentElement.removeAttribute('data-lwp-colour-mode');
});

describe('interactive export environment', () => {
  it('exposes hosted absence and the offline envelope to application content', () => {
    expect(ui.useInteractiveExportEnvironment).toBeTypeOf('function');
    function Consumer() {
      const environment = ui.useInteractiveExportEnvironment();
      return <p>{environment ? `${environment.kind}: ${environment.envelope.title}` : 'Hosted'}</p>;
    }
    const view = render(<Consumer />);
    expect(screen.getByText('Hosted')).toBeVisible();
    view.rerender(<ui.InteractiveExportEnvironmentProvider envelope={offlineEnvelope()}><Consumer /></ui.InteractiveExportEnvironmentProvider>);
    expect(screen.getByText('interactive-export: Briefing snapshot')).toBeVisible();
  });

  it.each([true, false])('mounts only the decoded capture with optional title %s', async (withTitle) => {
    expect(ui.mountInteractiveExport).toBeTypeOf('function');
    const envelope = offlineEnvelope();
    if (!withTitle) delete (envelope as { title?: string }).title;
    embed(envelope);
    let root: Root | undefined;
    try {
      await act(async () => {
        root = ui.mountInteractiveExport({ contract: offlineContract, render: (capture) => <p>{capture.snapshotData.message} / {capture.viewState.tab} / {capture.title ?? 'Untitled'}</p> });
      });
      expect(screen.getByText(`Decoded: Frozen briefing — café / overview / ${withTitle ? 'Briefing snapshot' : 'Untitled'}`)).toBeVisible();
      expect(document.documentElement.dataset.lwpColourMode).toBe('dark');
    } finally {
      await act(async () => root?.unmount());
    }
  });

  it.each(['templateId', 'payloadContractId', 'compatibilityId', 'appId', 'appVersion', 'sourceRevision'] as const)(
    'rejects a mismatched template %s before decoder or render', (field) => {
      expect(ui.mountInteractiveExport).toBeTypeOf('function');
      embed();
      document.head.querySelector('meta')!.content = encodeOfflineJson({ ...offlineEnvelope().template, [field]: 'different' });
      const decodeSnapshot = vi.fn(offlineContract.decodeSnapshot);
      const renderer = vi.fn(() => <p>Must not render</p>);
      expect(() => ui.mountInteractiveExport({ contract: { ...offlineContract, decodeSnapshot }, render: renderer })).toThrowError(expect.objectContaining({ code: 'template-incompatible', message: 'interactive export template is incompatible' }));
      expect(decodeSnapshot).not.toHaveBeenCalled();
      expect(renderer).not.toHaveBeenCalled();
      expect(document.getElementById('root')).toBeEmptyDOMElement();
    },
  );

  it('categorizes an invalid envelope template identity without exposing its payload', () => {
    const envelope = offlineEnvelope();
    envelope.template.compatibilityId = 'b'.repeat(64);
    embed(envelope);
    const renderer = vi.fn(() => <p>Must not render</p>);

    expect(() => ui.mountInteractiveExport({ contract: offlineContract, render: renderer })).toThrowError(expect.objectContaining({
      code: 'template-incompatible', message: 'interactive export template is incompatible',
    }));
    expect(renderer).not.toHaveBeenCalled();
  });

  it('preserves the model snapshot-size code before decoder or renderer run', () => {
    const envelope = offlineEnvelope();
    (envelope.snapshotData as { message: string; padding?: string }).padding = 'x'.repeat(ui.MAX_INTERACTIVE_EXPORT_SNAPSHOT_BYTES);
    let modelError: unknown;
    try {
      ui.validateInteractiveExportEnvelope(envelope);
    } catch (error) {
      modelError = error;
    }
    embed(envelope);
    const decodeSnapshot = vi.fn(offlineContract.decodeSnapshot);
    const renderer = vi.fn(() => <p>Must not render</p>);
    let mountError: unknown;
    try {
      ui.mountInteractiveExport({ contract: { ...offlineContract, decodeSnapshot }, render: renderer });
    } catch (error) {
      mountError = error;
    }

    expect(mountError).toEqual(expect.objectContaining({
      code: 'snapshot-oversized', message: 'interactive export snapshot is too large',
    }));
    expect(modelError).toEqual(expect.objectContaining({
      code: 'snapshot-oversized', message: 'interactive export snapshot is too large',
    }));
    expect((mountError as ui.InteractiveExportError).code).toBe((modelError as ui.InteractiveExportError).code);
    expect(decodeSnapshot).not.toHaveBeenCalled();
    expect(renderer).not.toHaveBeenCalled();
  });

  it.each(['missing payload', 'duplicate payload', 'malformed payload', 'invalid envelope', 'missing metadata', 'duplicate metadata', 'wrong contract', 'wrong sensitivity', 'invalid snapshot', 'decoder error detail', 'missing root'])(
    'fails closed with a generic error for %s', (failure) => {
      expect(ui.mountInteractiveExport).toBeTypeOf('function');
      embed();
      let contract = offlineContract;
      const payload = document.querySelector('template')!;
      if (failure === 'missing payload') payload.remove();
      if (failure === 'duplicate payload') document.body.append(payload.cloneNode(true));
      if (failure === 'malformed payload') payload.innerHTML = 'not base64';
      if (failure === 'invalid envelope') payload.innerHTML = encodeOfflineJson({ ...offlineEnvelope(), schema: 'wrong' });
      if (failure === 'missing metadata') document.head.replaceChildren();
      if (failure === 'duplicate metadata') document.head.append(document.head.firstChild!.cloneNode(true));
      if (failure === 'wrong contract') contract = { ...offlineContract, version: 2 };
      if (failure === 'wrong sensitivity') contract = { ...offlineContract, sensitivity: { ...offlineContract.sensitivity, classification: 'private' } };
      if (failure === 'invalid snapshot') payload.innerHTML = encodeOfflineJson({ ...offlineEnvelope(), viewState: { tab: 'absent' } });
      if (failure === 'decoder error detail') contract = { ...offlineContract, decodeSnapshot: () => { throw new ui.InteractiveExportError('capture-failed', 'private decoder detail'); } };
      if (failure === 'missing root') document.getElementById('root')!.remove();
      const renderer = vi.fn(() => <p>Must not render</p>);
      const templateFailure = ['missing metadata', 'duplicate metadata', 'wrong contract', 'wrong sensitivity'].includes(failure);
      expect(() => ui.mountInteractiveExport({ contract, render: renderer })).toThrowError(expect.objectContaining(
        templateFailure
          ? { code: 'template-incompatible', message: 'interactive export template is incompatible' }
          : { code: 'invalid-snapshot', message: 'interactive export snapshot is invalid' },
      ));
      expect(renderer).not.toHaveBeenCalled();
    },
  );

  it('sanitizes a framework-shaped decoder failure without retaining private properties', () => {
    embed();
    const privateDetail = 'offline-private-decoder-detail';
    const contract = {
      ...offlineContract,
      decodeSnapshot: () => { throw new ui.InteractiveExportError('template-unavailable', privateDetail); },
    };
    let thrown: unknown;
    try {
      ui.mountInteractiveExport({ contract, render: () => <p>Must not render</p> });
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toEqual(expect.objectContaining({ code: 'invalid-snapshot', message: 'interactive export snapshot is invalid' }));
    expect(JSON.stringify(thrown)).not.toContain(privateDetail);
    expect((thrown as Error).stack).not.toContain(privateDetail);
    expect((thrown as Error & { cause?: unknown }).cause).toBeUndefined();
  });
});
