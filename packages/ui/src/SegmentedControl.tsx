import { useEffect, useRef } from 'react';

import { Icon, type PlatformIconName } from './Icon.js';

export type SegmentedControlOption = {
  id: string;
  label: string;
  icon?: PlatformIconName;
  title?: string;
};

export type SegmentedControlProps = {
  options: readonly SegmentedControlOption[];
  value: string;
  onChange: (value: string) => void;
  'aria-label': string;
  className?: string;
};

const classes = (...values: Array<string | undefined>) => values.filter(Boolean).join(' ');

export function SegmentedControl({
  options,
  value,
  onChange,
  className,
  'aria-label': ariaLabel,
}: SegmentedControlProps) {
  const lastSelected = useRef(value);
  const choices = useRef(new Map<string, HTMLButtonElement>());

  useEffect(() => {
    lastSelected.current = value;
  }, [value]);

  const moveFocus = (currentId: string, key: string) => {
    const currentIndex = options.findIndex((option) => option.id === currentId);
    if (currentIndex === -1 || options.length === 0) return;

    let targetIndex: number | undefined;
    if (key === 'ArrowLeft') targetIndex = (currentIndex - 1 + options.length) % options.length;
    if (key === 'ArrowRight') targetIndex = (currentIndex + 1) % options.length;
    if (key === 'Home') targetIndex = 0;
    if (key === 'End') targetIndex = options.length - 1;
    if (targetIndex !== undefined) choices.current.get(options[targetIndex].id)?.focus();
  };

  return (
    <div
      className={classes('lwp-segmented-control', className)}
      role="group"
      aria-label={ariaLabel}
    >
      <div className="lwp-segmented-control__group">
        {options.map((option) => (
          <button
            key={option.id}
            ref={(element) => {
              if (element) choices.current.set(option.id, element);
              else choices.current.delete(option.id);
            }}
            type="button"
            className="lwp-segmented-control__choice"
            aria-label={option.label}
            aria-pressed={option.id === value}
            data-selected={option.id === value ? '' : undefined}
            tabIndex={option.id === value ? 0 : -1}
            title={option.title}
            onClick={() => {
              if (option.id !== lastSelected.current) {
                lastSelected.current = option.id;
                onChange(option.id);
              }
            }}
            onKeyDown={(event) => {
              if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
                event.preventDefault();
                moveFocus(option.id, event.key);
              }
            }}
          >
            <span className="lwp-segmented-control__indicator" aria-hidden="true" />
            {option.icon ? <Icon name={option.icon} /> : null}
          </button>
        ))}
      </div>
    </div>
  );
}
