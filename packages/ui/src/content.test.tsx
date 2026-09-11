import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  Button,
  DataViewport,
  Metric,
  MetricGroup,
  SectionNav,
  ViewHeader,
} from './index';

describe('content primitives', () => {
  it('renders one page heading with optional supporting content and actions', () => {
    render(
      <ViewHeader
        eyebrow="Season overview"
        title="Campaign dashboard"
        description="Compare the current season with the selected benchmark."
        actions={<Button>Export</Button>}
        className="domain-header"
      />,
    );

    const header = screen.getByRole('banner');
    expect(header).toHaveClass('lwp-view-header', 'domain-header');
    expect(within(header).getByRole('heading', { name: 'Campaign dashboard', level: 1 }))
      .toBeInTheDocument();
    expect(within(header).getByText('Season overview')).toHaveClass('lwp-view-header__eyebrow');
    expect(within(header).getByText(/Compare the current season/))
      .toHaveClass('lwp-view-header__description');
    expect(within(header).getByRole('button', { name: 'Export' }))
      .toBeInTheDocument();
  });

  it('keeps internal navigation native and marks the app-owned current link', () => {
    render(
      <SectionNav label="Dashboard sections" className="domain-navigation">
        <a href="#overview" aria-current="page">Overview</a>
        <a href="#matches">Matches</a>
        <a href="#players">Players</a>
      </SectionNav>,
    );

    const navigation = screen.getByRole('navigation', { name: 'Dashboard sections' });
    expect(navigation).toHaveClass('lwp-section-nav', 'domain-navigation');
    expect(within(navigation).getByRole('link', { name: 'Overview' }))
      .toHaveAttribute('aria-current', 'page');
    expect(within(navigation).getAllByRole('link')).toHaveLength(3);
  });

  it('uses description-list semantics for compact labelled metrics', () => {
    const { container } = render(
      <MetricGroup aria-label="Season metrics" className="domain-metrics">
        <Metric label="Points" value="74" detail="After 38 matches" />
        <Metric label="Position" value="4th" />
      </MetricGroup>,
    );

    const group = container.querySelector('dl');
    expect(group).toHaveClass('lwp-metric-group', 'domain-metrics');
    expect(group).toHaveAttribute('aria-label', 'Season metrics');
    expect(container.querySelectorAll('dt')).toHaveLength(2);
    expect(container.querySelectorAll('dd')).toHaveLength(2);
    expect(screen.getByText('After 38 matches')).toHaveClass('lwp-metric__detail');
  });

  it('provides an accessible keyboard-scrollable boundary without owning table markup', () => {
    render(
      <DataViewport label="Player comparison" className="domain-table">
        <table>
          <thead><tr><th>Player</th><th>Goals</th></tr></thead>
          <tbody><tr><td>Forward</td><td>18</td></tr></tbody>
        </table>
      </DataViewport>,
    );

    const viewport = screen.getByRole('region', { name: 'Player comparison' });
    expect(viewport).toHaveClass('lwp-data-viewport', 'domain-table');
    expect(viewport).toHaveAttribute('tabindex', '0');
    expect(within(viewport).getByRole('table')).toBeInTheDocument();
  });
});
