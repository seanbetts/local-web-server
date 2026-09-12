# Contributing

Local Web is deliberately small: framework code owns host lifecycle, generated
configuration, shared UI, and reusable app contracts; application repositories
own domain behavior and data. Keep changes narrow, add a focused failing test
first, and use disposable fixtures for every host, Caddy, release, or recovery
workflow.

## Prerequisites

- macOS for installation and LaunchAgent integration;
- Python 3.14 or a compatible supported Python 3;
- Node.js in a range declared by `package.json`, plus npm;
- Caddy 2 for configuration validation and macOS workflow checks; and
- Git.

Install JavaScript dependencies without lifecycle scripts:

```sh
npm ci --ignore-scripts
```

## Core checks

Run the focused tests for the code you changed, then the complete relevant
matrix. The portable baseline is:

```sh
PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -v
python3 -m compileall -q local_web_server scripts tests
npm run check:ui
npm run check:ui:consumer
npm run test:ui -- --run
npm run build:ui
npm run check:index
npm run build:gallery
npm run test:gallery -- --run
npm run verify:public-release
npm audit
npm audit --omit=dev
git diff --check
```

Rendered changes also require the applicable Playwright suites:

```sh
npm run test:index:e2e
npm run test:gallery:e2e
npm run test:interactive-export:e2e
```

## Disposable framework workflows

These commands create temporary repositories and private profiles. They must
not read or modify the checkout's real `config/local/` state:

```sh
npm run verify:host-profile
npm run verify:new-app
npm run verify:new-service-app
npm run verify:foundation-adoption
npm run verify:repository-service-transition
npm run verify:service-command-migration
npm run verify:public-base-path-migration
npm run verify:fleet-update
```

The host-profile workflow validates a generated Caddyfile with real Caddy when
available, while keeping all configuration and runtime paths disposable.

## Public CI

The public workflow runs for pull requests and explicit manual dispatches. It
has only `contents: read` permission, does not retain checkout credentials, and
does not consume repository secrets or run on push or deployment events.

macOS 15 runs the complete Python 3.14 suite because the framework's filesystem
and lifecycle contracts use macOS primitives. The job installs the locked Node
dependencies without lifecycle scripts, Chromium, and Caddy 2 before running
the suite and the disposable host-profile workflow. Its private profile,
revision, backup, rendered Caddy configuration, and runtime fixtures remain
beneath disposable workflow paths. It never invokes platform
installation, LaunchAgent or Tailscale changes, application activation,
deployment, or a real `config/local/apps.json`.

Ubuntu 24.04 runs the static, shared-UI, System Index, gallery, public-release,
and dependency-audit checks on each supported Node.js major: 22, 24, and 26.
npm's download cache is keyed by the committed lockfile; dependencies are
installed with lifecycle scripts disabled. The packed shared-UI consumer check
uses a new empty cache and installs only the local package tarball in offline
mode, so it cannot rely on packages fetched by an earlier CI step.

## Change discipline

- Never put credentials or real host/application state in fixtures or output.
- Preserve the fail-closed profile, revision, backup, CSP, and ingress rules.
- Do not make app-local `npm run check` scripts invoke `local-web`.
- Do not hand-edit generated Caddy, LaunchAgents, release pointers, or the host
  profile to make a test pass.
- Treat commit, merge, installation, activation, deployment, remote creation,
  and push as separate operations.

Before submitting a change, describe the RED/GREEN evidence, commands run,
remaining platform-specific limitations, and any state-changing operation. A
passing disposable workflow is not evidence about a live host.
