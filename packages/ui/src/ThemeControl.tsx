import { useEffect, useState } from 'react';

import {
  applyColourMode,
  COLOUR_MODE_CHANGE_EVENT,
  COLOUR_MODE_STORAGE_KEY,
  normaliseColourMode,
  readColourMode,
  setColourMode,
  type ColourMode,
} from './colourMode.js';
import { SegmentedControl, type SegmentedControlOption } from './SegmentedControl.js';
import { useOfflineColourMode } from './interactiveExportEnvironment.js';

const choices = [
  {
    mode: 'system',
    label: 'System colour mode',
    title: 'Use system colour mode',
    icon: 'device-desktop',
  },
  { mode: 'light', label: 'Light colour mode', title: 'Use light colour mode', icon: 'sun' },
  { mode: 'dark', label: 'Dark colour mode', title: 'Use dark colour mode', icon: 'moon' },
] as const satisfies readonly (Omit<SegmentedControlOption, 'id'> & { mode: ColourMode })[];

const browserStorage = (): Storage | null => {
  if (typeof window === 'undefined') {
    return null;
  }
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};

export function ThemeControl() {
  const offlineMode = useOfflineColourMode();
  if (offlineMode !== null) {
    return <ColourModeChoices mode={offlineMode} selectMode={(selected) => {
      window.dispatchEvent(new CustomEvent<ColourMode>(COLOUR_MODE_CHANGE_EVENT, { detail: selected }));
    }} />;
  }
  return <HostedThemeControl />;
}

function HostedThemeControl() {
  const [mode, updateMode] = useState<ColourMode>('system');

  useEffect(() => {
    const storage = browserStorage();
    const persistedMode = storage ? readColourMode(storage) : 'system';
    const synchroniseMode = (receivedMode: ColourMode) => {
      applyColourMode(receivedMode, document.documentElement);
      updateMode(receivedMode);
    };
    synchroniseMode(persistedMode);

    const synchroniseSameDocumentMode = (event: Event) => {
      if (!(event instanceof CustomEvent)) {
        return;
      }
      synchroniseMode(normaliseColourMode(event.detail));
    };

    window.addEventListener(COLOUR_MODE_CHANGE_EVENT, synchroniseSameDocumentMode);

    if (!storage) {
      return () => window.removeEventListener(COLOUR_MODE_CHANGE_EVENT, synchroniseSameDocumentMode);
    }

    const synchroniseStoredMode = (event: StorageEvent) => {
      if (event.key !== COLOUR_MODE_STORAGE_KEY || event.storageArea !== storage) {
        return;
      }
      const receivedMode = normaliseColourMode(event.newValue);
      synchroniseMode(receivedMode);
    };

    window.addEventListener('storage', synchroniseStoredMode);
    return () => {
      window.removeEventListener(COLOUR_MODE_CHANGE_EVENT, synchroniseSameDocumentMode);
      window.removeEventListener('storage', synchroniseStoredMode);
    };
  }, []);

  const selectMode = (selected: ColourMode) => {
    const storage = browserStorage();
    if (!storage) {
      window.dispatchEvent(
        new CustomEvent<ColourMode>(COLOUR_MODE_CHANGE_EVENT, { detail: 'system' }),
      );
      return;
    }
    setColourMode(selected, storage, document.documentElement);
    const persistedMode = readColourMode(storage);
    const effectiveMode = persistedMode === selected ? selected : 'system';
    applyColourMode(effectiveMode, document.documentElement);
    window.dispatchEvent(
      new CustomEvent<ColourMode>(COLOUR_MODE_CHANGE_EVENT, { detail: effectiveMode }),
    );
  };

  return <ColourModeChoices mode={mode} selectMode={selectMode} />;
}

function ColourModeChoices({ mode, selectMode }: { mode: ColourMode; selectMode: (mode: ColourMode) => void }) {
  return (
    <SegmentedControl
      className="lwp-theme-control"
      aria-label="Colour mode"
      options={choices.map(({ mode: id, ...choice }) => ({ id, ...choice }))}
      value={mode}
      onChange={(selected) => selectMode(selected as ColourMode)}
    />
  );
}
