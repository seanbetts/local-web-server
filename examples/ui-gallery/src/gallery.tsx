import { useState } from 'react';

import {
  Badge,
  Button,
  Cluster,
  ContextExportButton,
  DataViewport,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Grid,
  Icon,
  IconButton,
  InlineNotice,
  LoadingState,
  Metric,
  MetricGroup,
  PLATFORM_ICON_NAMES,
  PlatformShell,
  Select,
  SectionNav,
  Stack,
  StatusDot,
  Surface,
  TextArea,
  TextInput,
  Tooltip,
  ViewHeader,
} from '@local-web/ui';
import type { ContextExportBuilder } from '@local-web/ui';

const villaIdentity = {
  id: 'villa-shirt-collection',
  name: 'Sample Workspace',
  icon: 'shirt-sport' as const,
  accent: '#7A1735' as const,
};

const galleryIdentity = {
  id: 'ui-gallery',
  name: 'UI Gallery',
  icon: 'apps' as const,
  accent: '#52677F' as const,
};

const plotterIdentity = {
  id: 'plotter',
  name: 'Plotter',
  icon: 'route' as const,
  accent: '#25799C' as const,
};

const galleryContext: ContextExportBuilder = async () => ({
  schema: 'local-web-context/v1',
  app: {
    id: 'ui-gallery',
    name: 'UI Gallery',
    version: '1.0.0',
    sourceRevision: 'gallery-demo',
  },
  context: {
    title: 'UI compatibility gallery actions',
    scope: 'The current action demonstration.',
    activeRoute: '/',
    generatedAt: '2026-08-22T10:00:00.000Z',
    observedAt: null,
    dataRevision: null,
  },
  sensitivity: { classification: 'private', notice: 'For local demonstration only.' },
  summary: 'The shared action contract is ready to export.',
  capabilities: ['context-export'],
  sections: [],
  data: {},
  provenance: { freshness: 'Current at generation time.', sources: [] },
  assumptions: [],
  decisions: [],
  caveats: [],
  omissions: [],
});

const Section = ({ children, title }: { children: React.ReactNode; title: string }) => (
  <section aria-labelledby={`gallery-${title.toLowerCase().replaceAll(' ', '-')}`}>
    <h2 id={`gallery-${title.toLowerCase().replaceAll(' ', '-')}`}>{title}</h2>
    {children}
  </section>
);

export type GalleryFrame = 'index' | 'app' | 'app-page' | 'immersive';

export function Gallery({ frame = 'app' }: { frame?: GalleryFrame }) {
  const [actionComplete, setActionComplete] = useState(false);
  const [dialogOpen, setDialogOpen] = useState(false);

  if (frame === 'index') {
    return (
      <PlatformShell location={{ kind: 'index' }}>
        <div className="gallery-index-page">
          <Badge tone="success">Platform frame</Badge>
          <h1>System Index</h1>
          <p>The shared Local platform frame gives every hosted application one familiar starting point.</p>
        </div>
      </PlatformShell>
    );
  }

  if (frame === 'immersive') {
    return (
      <PlatformShell
        location={{ kind: 'app', app: plotterIdentity }}
        contentMode="edge-to-edge"
      >
        <div className="gallery-immersive-canvas" data-testid="immersive-canvas">
          <h1 className="gallery-immersive-canvas__title">Immersive application canvas</h1>
          <span>Immersive application canvas</span>
        </div>
      </PlatformShell>
    );
  }

  return (
    <PlatformShell
      location={frame === 'app-page'
        ? {
            kind: 'app-page',
            app: villaIdentity,
            appHref: '/samplebeta/',
            pageLabel: 'David Platt',
          }
        : { kind: 'app', app: galleryIdentity }}
    >
        <div className="gallery-page">
          <ViewHeader
            eyebrow="Shared interface contract"
            title="UI compatibility gallery"
            description="One deterministic surface for the shared shell, primitives, theme, and accessibility contract."
            actions={(
              <Button onClick={() => document.getElementById('gallery-layout')?.scrollIntoView()}>
                Browse primitives
              </Button>
            )}
          />

          <Stack className="gallery-sections">
            <Section title="Content">
              <SectionNav label="Gallery sections">
                <a href="#gallery-content" aria-current="page">Content</a>
                <a href="#gallery-layout">Layout</a>
                <a href="#gallery-actions">Actions</a>
                <a href="#gallery-forms">Forms</a>
              </SectionNav>
              <MetricGroup aria-label="Example application metrics">
                <Metric label="Active apps" value="4" detail="One service app" />
                <Metric label="Checks passing" value="12" detail="Current platform run" />
                <Metric label="Last recovery" value="18s" detail="Within the bounded window" />
              </MetricGroup>
              <DataViewport label="Application health comparison">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">Application</th>
                      <th scope="col">Kind</th>
                      <th scope="col">Frontend</th>
                      <th scope="col">Service</th>
                      <th scope="col">Health</th>
                      <th scope="col">Release</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <th scope="row">Season dashboard</th>
                      <td>Service</td>
                      <td>Ready</td>
                      <td>Ready</td>
                      <td>Healthy</td>
                      <td>Current</td>
                    </tr>
                    <tr>
                      <th scope="row">Shirt collection</th>
                      <td>Static</td>
                      <td>Ready</td>
                      <td>Not required</td>
                      <td>Healthy</td>
                      <td>Current</td>
                    </tr>
                  </tbody>
                </table>
              </DataViewport>
            </Section>

            <Section title="Layout">
              <Grid>
                <Surface><strong>Grid surface one</strong><span>Shared spacing and elevation.</span></Surface>
                <Surface><strong>Grid surface two</strong><span>Responsive without app assumptions.</span></Surface>
              </Grid>
              <Cluster className="gallery-cluster-demo">
                <Badge>Cluster</Badge><Badge tone="success">Aligned</Badge><Badge tone="warning">Wrapping</Badge>
              </Cluster>
            </Section>

            <Section title="Actions">
              <Cluster>
                <Button variant="primary" aria-label="Primary action">Primary</Button>
                <Button>Secondary</Button>
                <Button variant="danger">Danger</Button>
                <Button busy aria-label="Busy action">Saving</Button>
                <Button disabled aria-label="Disabled action">Disabled</Button>
                <Button onClick={() => setActionComplete(true)}>Run action</Button>
                <IconButton icon="download" label="Download example" />
                <ContextExportButton buildContextExport={galleryContext} />
              </Cluster>
              {actionComplete ? (
                <InlineNotice tone="success" title="Action completed" message="The shared action state updated." />
              ) : null}
            </Section>

            <Section title="Forms">
              <Grid>
                <Field label="App name" hint="Use a short, recognisable name."><TextInput defaultValue="Gallery" /></Field>
                <Field label="App type"><Select defaultValue="static"><option value="static">Static</option><option value="service">Service</option></Select></Field>
                <Field label="Description" hint="Describe what the app does."><TextArea defaultValue="Validates the platform UI." /></Field>
                <Field label="Required field" error="A value is required."><TextInput /></Field>
              </Grid>
            </Section>

            <Section title="Feedback">
              <Cluster>
                <Badge>Neutral</Badge><Badge tone="success">Success</Badge><Badge tone="warning">Warning</Badge><Badge tone="danger">Danger</Badge>
                <StatusDot label="Online" tone="success" />
                <StatusDot label="Degraded" tone="warning" />
                <StatusDot label="Offline" tone="danger" />
              </Cluster>
              <Grid>
                <InlineNotice title="Information" message="Shared feedback is ready." />
                <InlineNotice tone="warning" title="Review needed" message="Check this state before continuing." />
                <InlineNotice tone="error" title="Validation failed" message="Correct the highlighted value." />
                <LoadingState title="Loading apps" message="Checking the current catalogue." />
                <EmptyState title="No results" message="Try another filter." action={<Button>Clear filter</Button>} />
                <ErrorState title="Unable to load" message="The service did not respond." action={<Button>Try again</Button>} />
              </Grid>
            </Section>

            <Section title="Overlays">
              <Cluster>
                <Button onClick={() => setDialogOpen(true)}>Open dialog</Button>
                <Tooltip content="Shared tooltip content">
                  <IconButton icon="circle-alert" label="More information" />
                </Tooltip>
              </Cluster>
              <Dialog
                open={dialogOpen}
                onClose={() => setDialogOpen(false)}
                title="Gallery dialog"
                description="This validates the shared overlay contract."
                actions={<Button variant="primary" onClick={() => setDialogOpen(false)}>Confirm</Button>}
              >
                <Field label="Dialog note"><TextInput autoFocus defaultValue="Ready" /></Field>
              </Dialog>
            </Section>

            <Section title="Icons">
              <div className="gallery-icons" aria-label="Canonical icon catalogue">
                {PLATFORM_ICON_NAMES.map((name) => (
                  <div className="gallery-icon" key={name} title={name}>
                    <Icon name={name} label={name} data-testid="gallery-icon" />
                    <span>{name}</span>
                  </div>
                ))}
              </div>
            </Section>

            <Section title="Accent validation">
              <Surface className="gallery-accent">
                <Icon name="route" label="Derived application accent" />
                <span>The Villa seed is derived for each colour-mode surface.</span>
              </Surface>
            </Section>

            <Section title="Fallback tokens">
              <Surface className="gallery-token-card" data-testid="fallback-token-swatch">
                <span className="gallery-token-card__swatch" aria-hidden="true" />
                <span>Package fallbacks keep the gallery usable before the central theme arrives.</span>
              </Surface>
            </Section>
          </Stack>
        </div>
    </PlatformShell>
  );
}
