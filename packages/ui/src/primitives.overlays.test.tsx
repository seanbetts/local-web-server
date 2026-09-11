import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Dialog, Tooltip } from './index';

const rectangle = (left: number, width: number): DOMRect => ({
  bottom: 32,
  height: 24,
  left,
  right: left + width,
  top: 8,
  width,
  x: left,
  y: 8,
  toJSON: () => ({}),
});
const mockThemeLengths = (rootSize: number) => {
  vi.spyOn(window, 'getComputedStyle').mockImplementation((element) => ({
    fontSize: element === document.documentElement ? `${rootSize}px` : '',
    getPropertyValue: (name: string) => name === '--lwp-space-2' ? '.5rem' : '',
  }) as CSSStyleDeclaration);
};

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Tooltip', () => {
  it('preserves the trigger name, description, and handlers', () => {
    const focusHandler = vi.fn();
    render(
      <>
        <span id="download-context">Exports the current route</span>
        <Tooltip content="Creates a PNG file">
          <button type="button" aria-describedby="download-context" onFocus={focusHandler}>
            Download
          </button>
        </Tooltip>
      </>,
    );
    const trigger = screen.getByRole('button', { name: 'Download' });

    fireEvent.focus(trigger);

    expect(trigger).toHaveAccessibleName('Download');
    expect(trigger).toHaveAccessibleDescription('Exports the current route Creates a PNG file');
    expect(screen.getByRole('tooltip')).toHaveTextContent('Creates a PNG file');
    expect(focusHandler).toHaveBeenCalledOnce();
  });

  it('dismisses with Escape, composes key handling, and resets after focus leaves', () => {
    const keyHandler = vi.fn();
    render(
      <Tooltip content="Creates a PNG file">
        <button type="button" onKeyDown={keyHandler}>Download</button>
      </Tooltip>,
    );
    const trigger = screen.getByRole('button', { name: 'Download' });
    fireEvent.focus(trigger);

    fireEvent.keyDown(trigger, { key: 'Escape' });

    expect(keyHandler).toHaveBeenCalledOnce();
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    expect(trigger).toHaveAccessibleName('Download');
    expect(trigger).toHaveAttribute('aria-describedby');

    fireEvent.blur(trigger);
    fireEvent.focus(trigger);
    expect(screen.getByRole('tooltip')).toBeInTheDocument();
  });

  it('dismisses before Escape reaches a containing dialog', () => {
    const close = vi.fn();
    render(
      <Dialog open onClose={close} title="Export map">
        <Tooltip content="Creates a PNG file"><button type="button">Download</button></Tooltip>
      </Dialog>,
    );
    const trigger = screen.getByRole('button', { name: 'Download' });
    fireEvent.focus(trigger);

    fireEvent.keyDown(trigger, { key: 'Escape' });

    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: 'Export map' })).toBeInTheDocument();
    expect(close).not.toHaveBeenCalled();

    fireEvent.keyDown(trigger, { key: 'Escape' });
    expect(close).toHaveBeenCalledOnce();
  });

  it('stays dismissed while focus remains and resets after hover and focus both leave', () => {
    render(<Tooltip content="Creates a PNG file"><button type="button">Download</button></Tooltip>);
    const trigger = screen.getByRole('button', { name: 'Download' });
    const container = trigger.parentElement!;
    fireEvent.mouseEnter(container);
    fireEvent.focus(trigger);
    fireEvent.keyDown(trigger, { key: 'Escape' });

    fireEvent.mouseLeave(container);
    fireEvent.mouseEnter(container);
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

    fireEvent.blur(trigger);
    fireEvent.mouseLeave(container);
    fireEvent.mouseEnter(container);
    expect(screen.getByRole('tooltip')).toBeInTheDocument();
  });

  it('caps its normal width to the viewport minus both token gutters', () => {
    const css = readFileSync(resolve(process.cwd(), 'packages/ui/src/primitives.css'), 'utf8');
    const declarations = css.match(/\.lwp-tooltip__content\s*\{([^}]*)\}/)?.[1];

    expect(declarations).toContain(
      'max-width: min(calc(var(--lwp-space-8) * 8), calc(100vw - (var(--lwp-space-2) * 2)))',
    );
    expect(declarations).toContain('overflow-wrap: anywhere');
  });

  it.each([
    [320, 16, 'left', -120, 256, '128px'],
    [320, 16, 'right', 184, 256, '-128px'],
    [390, 16, 'left', -120, 256, '128px'],
    [390, 16, 'right', 254, 256, '-128px'],
    [320, 20, 'left', -140, 300, '150px'],
    [320, 20, 'right', 160, 300, '-150px'],
    [390, 20, 'left', -150, 320, '160px'],
    [390, 20, 'right', 220, 320, '-160px'],
  ])('contains a %ipx-root-%ipx %s-edge tooltip', (viewport, root, _, left, width, shift) => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
      return this.classList.contains('lwp-tooltip__content')
        ? rectangle(left, width)
        : rectangle(0, 40);
    });
    vi.spyOn(window, 'innerWidth', 'get').mockReturnValue(viewport);
    mockThemeLengths(root);
    render(<Tooltip content="Creates a PNG file"><button type="button">Download</button></Tooltip>);

    fireEvent.focus(screen.getByRole('button', { name: 'Download' }));

    expect(screen.getByRole('tooltip').style.getPropertyValue('--lwp-tooltip-shift')).toBe(shift);
  });

  it('remeasures a visible tooltip on resize without compounding its previous shift', () => {
    let left = -150;
    let width = 320;
    let viewport = 390;
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function () {
      const appliedShift = Number.parseFloat(
        this.style.getPropertyValue('--lwp-tooltip-shift'),
      ) || 0;
      return this.classList.contains('lwp-tooltip__content')
        ? rectangle(left + appliedShift, width)
        : rectangle(0, 40);
    });
    vi.spyOn(window, 'innerWidth', 'get').mockImplementation(() => viewport);
    mockThemeLengths(20);
    render(<Tooltip content="Creates a PNG file"><button type="button">Download</button></Tooltip>);
    fireEvent.focus(screen.getByRole('button', { name: 'Download' }));
    expect(screen.getByRole('tooltip').style.getPropertyValue('--lwp-tooltip-shift')).toBe('160px');

    viewport = 320;
    left = -140;
    width = 300;
    fireEvent(window, new Event('resize'));
    expect(screen.getByRole('tooltip').style.getPropertyValue('--lwp-tooltip-shift')).toBe('150px');

    left = 10;
    fireEvent(window, new Event('resize'));

    expect(screen.getByRole('tooltip').style.getPropertyValue('--lwp-tooltip-shift')).toBe('');
  });
});
