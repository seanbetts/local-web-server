import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  Badge,
  Button,
  Cluster,
  EmptyState,
  ErrorState,
  Field,
  Grid,
  IconButton,
  InlineNotice,
  LoadingState,
  Select,
  Stack,
  StatusDot,
  Surface,
  TextArea,
  TextInput,
} from './index';
describe('platform primitives', () => {
  it('composes layout primitives without taking ownership of app class names', () => {
    const { container } = render(
      <Stack className="trip-layout" data-testid="stack">
        <Cluster data-testid="cluster">
          <Grid data-testid="grid">
            <Surface data-testid="surface">Trip</Surface>
          </Grid>
        </Cluster>
      </Stack>,
    );
    expect(screen.getByTestId('stack')).toHaveClass('lwp-stack', 'trip-layout');
    expect(screen.getByTestId('cluster')).toHaveClass('lwp-cluster');
    expect(screen.getByTestId('grid')).toHaveClass('lwp-grid');
    expect(screen.getByTestId('surface')).toHaveClass('lwp-surface');
    expect(container.querySelector('[style]')).not.toBeInTheDocument();
  });
  it('gives icon-only actions a stable accessible name and click behaviour', () => {
    const handler = vi.fn();
    render(<IconButton label="Download map" icon="download" onClick={handler} />);
    fireEvent.click(screen.getByRole('button', { name: 'Download map' }));
    expect(handler).toHaveBeenCalledOnce();
    const icon = screen.getByRole('button', { name: 'Download map' }).querySelector('svg');
    expect(icon).toHaveAttribute('aria-hidden', 'true');
    expect(screen.getByRole('button', { name: 'Download map' }).querySelectorAll('svg')).toHaveLength(1);
  });
  it('keeps disabled and busy button semantics distinct while blocking both actions', () => {
    const disabledHandler = vi.fn();
    const busyHandler = vi.fn();
    render(
      <>
        <Button disabled onClick={disabledHandler}>
          Disabled
        </Button>
        <Button busy onClick={busyHandler}>
          Saving
        </Button>
      </>,
    );
    const disabled = screen.getByRole('button', { name: 'Disabled' });
    const busy = screen.getByRole('button', { name: 'Saving' });
    fireEvent.click(disabled);
    fireEvent.click(busy);
    expect(disabled).toBeDisabled();
    expect(disabled).not.toHaveAttribute('aria-busy');
    expect(busy).not.toBeDisabled();
    expect(busy).toHaveAttribute('aria-busy', 'true');
    expect(busy).toHaveAttribute('aria-disabled', 'true');
    expect(disabledHandler).not.toHaveBeenCalled();
    expect(busyHandler).not.toHaveBeenCalled();
  });
  it('shows one decorative busy indicator in an icon button', () => {
    render(<IconButton busy label="Generating trip map" icon="download" />);
    const button = screen.getByRole('button', { name: 'Generating trip map' });
    expect(button.querySelectorAll('svg')).toHaveLength(1);
  });
  it('honours an explicit aria-disabled state without removing the button from focus order', () => {
    const handler = vi.fn();
    render(<Button aria-disabled="true" onClick={handler}>Unavailable</Button>);
    const button = screen.getByRole('button', { name: 'Unavailable' });
    fireEvent.click(button);
    expect(button).not.toBeDisabled();
    expect(button).toHaveAttribute('aria-disabled', 'true');
    expect(handler).not.toHaveBeenCalled();
  });
  it('links a field label, existing description, hint, and error without replacing child props', () => {
    const changeHandler = vi.fn();
    render(
      <>
        <p id="trip-context">Used on the map</p>
        <Field label="Trip name" hint="Use a memorable name" error="Trip name is required">
          <TextInput
            id="trip-name"
            aria-describedby="trip-context"
            onChange={changeHandler}
          />
        </Field>
      </>,
    );
    const input = screen.getByRole('textbox', { name: 'Trip name' });
    fireEvent.change(input, { target: { value: 'North Coast' } });
    expect(input).toHaveAttribute('id', 'trip-name');
    expect(input).toHaveAccessibleDescription(
      'Used on the map Use a memorable name Trip name is required',
    );
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(changeHandler).toHaveBeenCalledOnce();
  });
  it('generates stable field ids and labels every supported native control', () => {
    const { rerender } = render(
      <Field label="Destination">
        <TextInput />
      </Field>,
    );
    const generatedId = screen.getByRole('textbox', { name: 'Destination' }).id;
    rerender(
      <Field label="Destination">
        <TextInput />
      </Field>,
    );
    expect(generatedId).not.toBe('');
    expect(screen.getByRole('textbox', { name: 'Destination' })).toHaveAttribute('id', generatedId);
    rerender(
      <>
        <Field label="Notes"><TextArea /></Field>
        <Field label="Vehicle"><Select><option>Camper</option></Select></Field>
      </>,
    );
    expect(screen.getByRole('textbox', { name: 'Notes' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Vehicle' })).toBeInTheDocument();
  });
  it('provides non-colour status meaning and appropriate notice live regions', () => {
    render(
      <>
        <Badge tone="success">Synced</Badge>
        <StatusDot tone="warning" label="Frontend only" />
        <InlineNotice tone="info" title="Map saved" message="The export is ready." />
        <InlineNotice tone="error" title="Export failed" />
      </>,
    );
    expect(screen.getByText('Synced')).toBeInTheDocument();
    expect(screen.getByRole('status', { name: 'Frontend only' })).toBeInTheDocument();
    expect(screen.getByRole('status', { name: 'Map saved' })).toHaveAttribute(
      'aria-live',
      'polite',
    );
    expect(screen.getByRole('alert')).toHaveTextContent('Export failed');
  });
  it('exposes loading, empty, and error states with their real announcement semantics', () => {
    render(
      <>
        <LoadingState title="Loading trips" message="Checking storage" />
        <EmptyState title="No trips" message="Create your first trip" />
        <ErrorState title="Trip storage unavailable" action={<Button>Retry</Button>} />
      </>,
    );
    const statuses = screen.getAllByRole('status');
    expect(statuses[0]).toHaveAttribute('aria-busy', 'true');
    expect(statuses[0]).toHaveAttribute('aria-live', 'polite');
    expect(statuses[1]).toHaveTextContent('No trips');
    expect(screen.getByRole('alert')).toHaveTextContent('Trip storage unavailable');
    expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
  });
  it('keeps primitive styling token-driven and free of inline colours', () => {
    const structureCss = readFileSync(
      resolve(process.cwd(), 'packages/ui/src', 'structure.css'),
      'utf8',
    );
    const primitivesCss = readFileSync(
      resolve(process.cwd(), 'packages/ui/src', 'primitives.css'),
      'utf8',
    );
    const neutralFallbackDeclarations = [
      '--lwp-app-accent-light: #8097B3;',
      '--lwp-app-accent-dark: #8EA7C6;',
    ];
    render(
      <Surface>
        <Button variant="danger">Delete</Button>
        <TextInput aria-invalid="true" />
        <Badge tone="warning">Review</Badge>
      </Surface>,
    );
    expect(document.querySelector('[style*="#"], [style*="rgb"], [style*="hsl"]')).toBeNull();
    for (const declaration of neutralFallbackDeclarations) {
      expect(structureCss.split(declaration)).toHaveLength(3);
    }
    const structuralCssWithoutNeutralFallbacks = neutralFallbackDeclarations.reduce(
      (css, declaration) => css.replaceAll(declaration, ''),
      structureCss,
    );
    expect(`${structuralCssWithoutNeutralFallbacks}\n${primitivesCss}`)
      .not.toMatch(/#[\dA-F]{3,8}|(?:rgb|hsl)a?\(/i);
  });
});
