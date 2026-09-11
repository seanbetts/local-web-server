import { createCompatibilityId, defineInteractiveExportContract } from '../interactiveExportModel.js';
import type { InteractiveExportCapture, InteractiveExportEnvelope } from '../interactiveExportModel.js';

export const offlineContract = defineInteractiveExportContract<{ message: string }, { tab: string }>({
  id: 'briefing',
  version: 1,
  sensitivity: { classification: 'sensitive', notice: 'Contains private briefing data.' },
  decodeSnapshot(value) {
    const capture = value as InteractiveExportCapture<{ message: string }, { tab: string }>;
    const keys = Object.keys(capture).sort();
    if (keys.join(',') !== (capture.title === undefined ? 'snapshotData,viewState' : 'snapshotData,title,viewState')
      || typeof capture.snapshotData.message !== 'string' || capture.viewState.tab !== 'overview') {
      throw new Error('private decoder detail');
    }
    return { ...capture, snapshotData: { message: `Decoded: ${capture.snapshotData.message}` } };
  },
});

export function offlineEnvelope(): InteractiveExportEnvelope<{ message: string }, { tab: string }> {
  const identity = { appId: 'briefing', payloadContractId: 'briefing/briefing/v1', templateId: 'a'.repeat(64) };
  return {
    schema: 'local-web-interactive-export/v1',
    template: { ...identity, compatibilityId: createCompatibilityId(identity), appVersion: '1.0.0', sourceRevision: 'abc1234' },
    capture: { capturedAt: '2026-09-08T12:30:00.000Z', effectiveColourMode: 'dark' },
    sensitivity: offlineContract.sensitivity,
    title: 'Briefing snapshot',
    snapshotData: { message: 'Frozen briefing — café' },
    viewState: { tab: 'overview' },
  };
}

export const encodeOfflineJson = (value: unknown): string => btoa(
  Array.from(new TextEncoder().encode(JSON.stringify(value)), (byte) => String.fromCharCode(byte)).join(''),
);
