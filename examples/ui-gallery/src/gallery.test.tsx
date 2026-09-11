import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { PLATFORM_ICON_NAMES } from '@local-web/ui';
import { Gallery } from './gallery';

describe('UI compatibility gallery', () => {
  it('renders the System Index example as one independent landmark document', () => {
    render(<Gallery frame="index" />);

    expect(screen.getAllByRole('main')).toHaveLength(1);
    expect(screen.getAllByRole('navigation', { name: 'Location' })).toHaveLength(1);
    expect(screen.getByRole('navigation', { name: 'Location' })).toHaveTextContent(
      'Local/System Index',
    );
    expect(screen.getByRole('link', { name: 'Skip to System Index content' })).toHaveAttribute(
      'href',
      '#lwp-main',
    );
    expect(screen.queryByRole('button', { name: 'Export context' })).not.toBeInTheDocument();
  });

  it('renders every Phase 1 public component and meaningful static state', () => {
    render(<Gallery frame="app" />);

    expect(screen.getAllByRole('main')).toHaveLength(1);
    const locations = screen.getAllByRole('navigation', { name: 'Location' });
    expect(locations).toHaveLength(1);
    expect(locations[0]).toHaveTextContent('Local/System Index/UI Gallery');
    expect(screen.getByRole('link', { name: 'System Index' })).toHaveAttribute('href', '/');
    for (const section of [
      'Layout',
      'Content',
      'Actions',
      'Forms',
      'Feedback',
      'Overlays',
      'Icons',
      'Accent validation',
      'Fallback tokens',
    ]) {
      expect(screen.getByRole('heading', { name: section })).toBeInTheDocument();
    }
    expect(screen.getByRole('button', { name: 'Primary action' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Export context' })).toBeInTheDocument();
    expect(document.querySelector('.lwp-platform-shell__header [aria-label="Export context"]')).toBeNull();
    expect(screen.getByRole('navigation', { name: 'Gallery sections' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Application health comparison' }))
      .toHaveAttribute('tabindex', '0');
    expect(screen.getByLabelText('Example application metrics').querySelectorAll('dt'))
      .toHaveLength(3);
    expect(screen.getByRole('button', { name: 'Busy action' })).toHaveAttribute('aria-busy', 'true');
    expect(screen.getByRole('button', { name: 'Disabled action' })).toBeDisabled();
    expect(screen.getByRole('textbox', { name: 'App name' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Description' })).toHaveAccessibleDescription('Describe what the app does.');
    expect(screen.getByRole('textbox', { name: 'Required field' })).toHaveAccessibleDescription('A value is required.');
    expect(screen.getByRole('status', { name: 'Online' })).toBeInTheDocument();
    expect(screen.getByRole('alert', { name: 'Validation failed' })).toBeInTheDocument();
    expect(screen.getAllByTestId('gallery-icon')).toHaveLength(PLATFORM_ICON_NAMES.length);
    expect(screen.getByTestId('fallback-token-swatch')).toBeInTheDocument();
  });

  it('renders the app-page location as a linked app breadcrumb and current page', () => {
    render(<Gallery frame="app-page" />);

    expect(screen.getByRole('link', { name: 'Sample Workspace' }))
      .toHaveAttribute('href', '/samplebeta/');
    expect(screen.getByText('David Platt')).toHaveAttribute('aria-current', 'page');
  });

  it('renders the immersive frame edge-to-edge around its deterministic canvas', () => {
    render(<Gallery frame="immersive" />);

    expect(screen.getByRole('main')).toHaveClass('lwp-platform-shell__main--edge-to-edge');
    expect(screen.getByTestId('immersive-canvas')).toBeInTheDocument();
  });

  it('exercises action and dialog states without bypassing public components', () => {
    render(<Gallery frame="app" />);

    fireEvent.click(screen.getByRole('button', { name: 'Run action' }));
    expect(screen.getByRole('status', { name: 'Action completed' })).toBeInTheDocument();

    const trigger = screen.getByRole('button', { name: 'Open dialog' });
    fireEvent.click(trigger);
    const dialog = screen.getByRole('dialog', { name: 'Gallery dialog' });
    expect(within(dialog).getByText('This validates the shared overlay contract.')).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog', { name: 'Gallery dialog' })).not.toBeInTheDocument();
  });
});
