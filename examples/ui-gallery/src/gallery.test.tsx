import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Gallery } from './gallery';

// Public components own their semantics; this fixture check catches broken frame wiring.
describe('UI compatibility gallery', () => {
  it.each(['app', 'index', 'app-page', 'immersive'] as const)('renders the %s fixture as one document', (frame) => {
    render(<Gallery frame={frame} />);
    expect(screen.getAllByRole('main')).toHaveLength(1);
    expect(screen.getAllByRole('navigation', { name: 'Location' })).toHaveLength(1);
  });
});
