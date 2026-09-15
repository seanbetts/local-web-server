import { fireEvent, render, screen } from '@testing-library/react';
import { type SyntheticEvent } from 'react';
import { renderToString } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';

import { Dialog, Select, TextArea, TextInput } from './index';

describe('Dialog', () => {
  it.each([
    ['TextInput', <TextInput autoFocus aria-label="Requested control" />],
    ['TextArea', <TextArea autoFocus aria-label="Requested control" />],
    ['Select', <Select autoFocus aria-label="Requested control"><option>One</option></Select>],
  ])('marks the %s autofocus request for dialog selection', (_name, control) => {
    render(control);

    expect(screen.getByLabelText('Requested control')).toHaveAttribute('data-lwp-autofocus', 'true');
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
