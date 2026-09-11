import { render, screen } from '@testing-library/react';
import iconCatalogue from '../../../platform_assets/icons.json';
import { describe, expect, it } from 'vitest';

import { Icon, PLATFORM_ICON_NAMES, PLATFORM_MANIFEST_ICON_NAMES } from './Icon';

describe('Icon', () => {
  it('keeps the explicit React allowlist aligned with the generated catalogue', () => {
    const expected = [
      ...iconCatalogue.manifest,
      ...iconCatalogue.actions,
      'device-desktop',
      'sun',
      'moon',
    ].sort();

    expect([...PLATFORM_ICON_NAMES].sort()).toEqual(expected);
    expect(PLATFORM_MANIFEST_ICON_NAMES).toEqual(iconCatalogue.manifest);
  });

  it('renders every allowlisted Tabler icon with safe shared geometry', () => {
    for (const name of PLATFORM_ICON_NAMES) {
      const { unmount } = render(<Icon name={name} data-testid={name} />);
      const svg = screen.getByTestId(name);

      expect(svg).toHaveAttribute('aria-hidden', 'true');
      expect(svg).toHaveAttribute('focusable', 'false');
      expect(svg).toHaveAttribute('viewBox', '0 0 24 24');
      expect(svg.querySelectorAll('path').length).toBeGreaterThan(0);
      const catalogueIcon = iconCatalogue.icons[name];
      if (catalogueIcon) {
        const expected = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        expected.innerHTML = catalogueIcon.svg;
        expect([...svg.querySelectorAll('path')].map((path) => path.getAttribute('d'))).toEqual(
          [...expected.querySelectorAll('path')].map((path) => path.getAttribute('d')),
        );
      }
      unmount();
    }
  });

  it('uses an accessible label only when one is supplied', () => {
    const { rerender } = render(<Icon name="route" label="Route" />);
    expect(screen.getByRole('img', { name: 'Route' })).toBeInTheDocument();

    rerender(<Icon name="route" />);
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
  });

  it('renders the briefcase manifest icon from the shared catalogue', () => {
    render(<Icon name={'briefcase' as (typeof PLATFORM_ICON_NAMES)[number]} data-testid="briefcase" />);

    expect(screen.getByTestId('briefcase').querySelectorAll('path').length).toBeGreaterThan(0);
  });

  it.each(['device-desktop', 'sun', 'moon'] as const)(
    'renders the %s theme icon as decorative SVG',
    (name) => {
      render(<Icon name={name} data-testid="theme-icon" />);

      const icon = screen.getByTestId('theme-icon');
      expect(icon).toHaveAttribute('aria-hidden', 'true');
      expect(icon).toHaveAttribute('focusable', 'false');
      expect(icon.querySelectorAll('path').length).toBeGreaterThan(0);
    },
  );
});
