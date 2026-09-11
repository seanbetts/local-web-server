import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { SegmentedControl } from './index';

const options = [
  { id: 'system', label: 'System', icon: 'device-desktop' as const },
  { id: 'light', label: 'Light', icon: 'sun' as const },
  { id: 'dark', label: 'Dark', icon: 'moon' as const },
];

describe('SegmentedControl', () => {
  it('renders labelled options with one selected value and an animated indicator', () => {
    render(
      <SegmentedControl
        aria-label="Colour mode"
        options={options}
        value="system"
        onChange={() => undefined}
      />,
    );

    const group = screen.getByRole('group', { name: 'Colour mode' });
    expect(within(group).getAllByRole('button')).toHaveLength(3);
    expect(within(group).getByRole('button', { name: 'System' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(within(group).getByRole('button', { name: 'Dark' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
    expect(group.querySelector('.lwp-segmented-control__indicator')).toBeInTheDocument();
  });

  it('emits the selected option and does not allow an empty selection', () => {
    const onChange = vi.fn();
    render(
      <SegmentedControl
        aria-label="Colour mode"
        options={options}
        value="system"
        onChange={onChange}
      />,
    );

    const group = screen.getByRole('group', { name: 'Colour mode' });
    fireEvent.click(within(group).getByRole('button', { name: 'Dark' }));
    expect(onChange).toHaveBeenCalledWith('dark');
    fireEvent.click(within(group).getByRole('button', { name: 'Dark' }));
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it('does not render React Aria pressable markers that require a runtime style injection', () => {
    render(
      <SegmentedControl
        aria-label="Colour mode"
        options={options}
        value="system"
        onChange={() => undefined}
      />,
    );

    expect(screen.getByRole('group', { name: 'Colour mode' }).querySelector('[data-react-aria-pressable]')).toBeNull();
  });

  it('moves horizontal focus without changing selection until a choice is activated', () => {
    const onChange = vi.fn();
    render(
      <SegmentedControl
        aria-label="Colour mode"
        options={options}
        value="system"
        onChange={onChange}
      />,
    );

    const group = screen.getByRole('group', { name: 'Colour mode' });
    const system = within(group).getByRole('button', { name: 'System' });
    const light = within(group).getByRole('button', { name: 'Light' });
    const dark = within(group).getByRole('button', { name: 'Dark' });

    system.focus();
    fireEvent.keyDown(system, { key: 'ArrowRight' });
    expect(light).toHaveFocus();
    fireEvent.keyDown(light, { key: 'End' });
    expect(dark).toHaveFocus();
    fireEvent.keyDown(dark, { key: 'Home' });
    expect(system).toHaveFocus();
    fireEvent.keyDown(system, { key: 'ArrowLeft' });
    expect(dark).toHaveFocus();
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.click(light);
    expect(onChange).toHaveBeenCalledWith('light');
  });

  it('keeps only the controlled selection in the Tab sequence', () => {
    render(
      <SegmentedControl
        aria-label="Colour mode"
        options={options}
        value="system"
        onChange={() => undefined}
      />,
    );

    const group = screen.getByRole('group', { name: 'Colour mode' });
    expect(within(group).getByRole('button', { name: 'System' })).toHaveAttribute('tabindex', '0');
    expect(within(group).getByRole('button', { name: 'Light' })).toHaveAttribute('tabindex', '-1');
    expect(within(group).getByRole('button', { name: 'Dark' })).toHaveAttribute('tabindex', '-1');
  });
});
