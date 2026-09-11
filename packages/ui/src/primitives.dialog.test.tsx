import { fireEvent, render, screen } from '@testing-library/react';
import { useState, type ReactNode, type SyntheticEvent } from 'react';
import { renderToString } from 'react-dom/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { Dialog, Select, TextArea, TextInput } from './index';

const originalShowModal = Object.getOwnPropertyDescriptor(
  HTMLDialogElement.prototype,
  'showModal',
);
const originalFocus = HTMLElement.prototype.focus;
const emulateBrowserDialogFocus = () => {
  vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(function (
    this: HTMLElement,
    options?: FocusOptions,
  ) {
    const dialog = this.closest('dialog');
    for (let current = this.parentElement; current && current !== dialog; current = current.parentElement) {
      if (current.hidden || current.hasAttribute('inert')) return;
      if (window.getComputedStyle(current).display === 'none') return;
    }
    if (window.getComputedStyle(this).visibility !== 'visible') return;
    originalFocus.call(this, options);
  });
  Object.defineProperty(HTMLDialogElement.prototype, 'showModal', {
    configurable: true,
    value(this: HTMLDialogElement) {
      this.setAttribute('open', '');
      this.querySelector<HTMLElement>('button')?.focus();
    },
  });
};

afterEach(() => {
  vi.restoreAllMocks();
  if (originalShowModal) {
    Object.defineProperty(HTMLDialogElement.prototype, 'showModal', originalShowModal);
  } else {
    delete (HTMLDialogElement.prototype as { showModal?: unknown }).showModal;
  }
});

describe('Dialog', () => {
  it('honours content autoFocus before the earlier Close button', () => {
    emulateBrowserDialogFocus();
    render(
      <Dialog open onClose={() => undefined} title="Edit trip">
        <button type="button">Earlier action</button>
        <TextInput autoFocus aria-label="Trip name" />
      </Dialog>,
    );
    expect(screen.getByRole('textbox', { name: 'Trip name' })).toHaveFocus();
  });

  it.each([
    ['TextInput', <TextInput autoFocus aria-label="Requested control" />],
    ['TextArea', <TextArea autoFocus aria-label="Requested control" />],
    ['Select', <Select autoFocus aria-label="Requested control"><option>One</option></Select>],
  ])('preserves valid %s autoFocus', (_, control) => {
    emulateBrowserDialogFocus();
    render(<Dialog open onClose={() => undefined} title="Edit trip">{control}</Dialog>);

    expect(screen.getByLabelText('Requested control')).toHaveFocus();
  });

  it('skips disabled autoFocus and uses the next enabled body control', () => {
    emulateBrowserDialogFocus();
    render(
      <Dialog open onClose={() => undefined} title="Edit trip">
        <TextInput autoFocus disabled aria-label="Unavailable control" />
        <button type="button">Continue</button>
      </Dialog>,
    );

    expect(screen.getByRole('button', { name: 'Continue' })).toHaveFocus();
  });

  it.each<[string, ReactNode]>([
    ['hidden', <div hidden><TextInput autoFocus aria-label="Hidden control" /></div>],
    ['inert', <div inert><TextInput autoFocus aria-label="Inert control" /></div>],
    ['unfocusable', <span data-lwp-autofocus="true">Marker</span>],
  ])('skips a %s autoFocus candidate', (_, candidate) => {
    emulateBrowserDialogFocus();
    render(
      <Dialog open onClose={() => undefined} title="Edit trip">
        {candidate}
        <button type="button">Continue</button>
      </Dialog>,
    );

    expect(screen.getByRole('button', { name: 'Continue' })).toHaveFocus();
  });

  it('allows modal focus to escape an inert ancestor outside the dialog', () => {
    emulateBrowserDialogFocus();
    render(
      <div inert>
        <Dialog open onClose={() => undefined} title="Edit trip">
          <TextInput autoFocus aria-label="Modal request" />
        </Dialog>
      </div>,
    );

    expect(screen.getByRole('textbox', { name: 'Modal request' })).toHaveFocus();
  });

  it('allows a visible autofocus target to override inherited hidden visibility', () => {
    emulateBrowserDialogFocus();
    render(
      <Dialog open onClose={() => undefined} title="Edit trip">
        <div style={{ visibility: 'hidden' }}>
          <TextInput autoFocus aria-label="Visible request" style={{ visibility: 'visible' }} />
        </div>
        <button type="button">Continue</button>
      </Dialog>,
    );

    expect(screen.getByRole('textbox', { name: 'Visible request' })).toHaveFocus();
  });

  it('uses Close as its no-scroll final fallback', () => {
    emulateBrowserDialogFocus();
    render(
      <Dialog open onClose={() => undefined} title="Edit trip">
        <TextInput autoFocus disabled aria-label="Unavailable control" />
        <div hidden><TextInput data-lwp-autofocus="true" aria-label="Hidden control" /></div>
        <span data-lwp-autofocus="true">Marker</span>
      </Dialog>,
    );

    expect(screen.getByRole('button', { name: 'Close' })).toHaveFocus();
    expect(HTMLElement.prototype.focus).toHaveBeenCalledWith({ preventScroll: true });
  });

  it('closes with Escape and returns focus to its opener', () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>Edit trip</button>
          <Dialog open={open} onClose={() => setOpen(false)} title="Edit trip">
            <TextInput aria-label="Trip name" />
          </Dialog>
        </>
      );
    }
    render(<Harness />);
    const opener = screen.getByRole('button', { name: 'Edit trip' });
    opener.focus();
    fireEvent.click(opener);

    fireEvent.keyDown(screen.getByRole('dialog', { name: 'Edit trip' }), { key: 'Escape' });

    expect(screen.queryByRole('dialog', { name: 'Edit trip' })).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it('closes from its backdrop and renders safely on the server', () => {
    const close = vi.fn();
    const { rerender } = render(
      <Dialog open onClose={close} title="Delete trip" description="This cannot be undone">
        Confirmation
      </Dialog>,
    );

    fireEvent.click(screen.getByRole('dialog', { name: 'Delete trip' }));

    expect(close).toHaveBeenCalledOnce();
    expect(() =>
      renderToString(<Dialog open={false} onClose={() => undefined} title="Server dialog">Body</Dialog>),
    ).not.toThrow();
    rerender(<Dialog open={false} onClose={close} title="Delete trip">Confirmation</Dialog>);
  });

  it('composes native cancel and lets the consumer veto controlled closure', () => {
    const close = vi.fn();
    const cancel = vi.fn((event: SyntheticEvent<HTMLDialogElement>) => {
      event.preventDefault();
    });
    render(<Dialog open onCancel={cancel} onClose={close} title="Edit trip">Body</Dialog>);

    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }));

    expect(cancel).toHaveBeenCalledOnce();
    expect(close).not.toHaveBeenCalled();
  });

  it('requests controlled closure exactly once for an unhandled native cancel', () => {
    const close = vi.fn();
    const cancel = vi.fn();
    render(<Dialog open onCancel={cancel} onClose={close} title="Edit trip">Body</Dialog>);

    fireEvent(screen.getByRole('dialog'), new Event('cancel', { cancelable: true }));

    expect(cancel).toHaveBeenCalledOnce();
    expect(close).toHaveBeenCalledOnce();
  });
});
