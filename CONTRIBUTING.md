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

## Choosing checks

For routine changes, run the focused tests for the code you changed, then:

```sh
PYTHONWARNINGS=error::ResourceWarning npm run check
```

This runs focused Python validation/state tests and the frontend type, consumer,
unit, and build checks. Python uses standard unittest duration reporting and a
private temporary directory with short paths for macOS Unix sockets.

Before merging framework changes, run the retained required surface:

```sh
LOCAL_WEB_REQUIRE_CADDY_INTEGRATION=1 PYTHONWARNINGS=error::ResourceWarning npm run check:all
python3 -m compileall -q local_web_server scripts tests
npm run verify:public-release
npm audit
npm audit --omit=dev
git diff --check
```

`check:all` runs both Python groups, including real Caddy and disposable
migration/recovery journeys, then builds the shared UI once and runs frontend
checks and Chromium journeys. Those journeys cover homepage navigation/status,
native shell interaction, context downloads, and interactive exports opened
offline. Four gallery snapshots remain part of the theme-release contract.

Useful focused entrypoints:

```sh
npm run test:python
npm run test:python:acceptance
npm run test:python:all
python3 scripts/run_tests.py --suite acceptance --list
npm run check:frontend
npm run check:frontend:browser
```

The default Python group contains focused component checks, including small Git
fixtures where committed-source authority is the behavior under test.
`@acceptance` selects real toolchain, Caddy, and process lifecycle integration.
Their union is `all`. The separately invoked `npm run test:python:stress` checks
the published 10,000-revision/5 MiB profile restore scale. Small chains and exact
limit rejection stay in routine coverage. Ordinary discovery also runs stress:

```sh
PYTHONWARNINGS=error::ResourceWarning python3 -m unittest discover -s tests -v
```

Standalone commands prepare their prerequisites. Commands ending in `:prepared`
are composition steps and require the preceding builds.

| Guarantee | Primary test owner; integration above it |
| --- | --- |
| Invalid manifests, unsafe routes, CSP policy | `test_config`, `test_render`; real Caddy routing |
| Failed builds, deployment/rollback and application-data preservation | `test_git_build`, `test_deploy`; representative migration/fleet recovery |
| Profile integrity, exclusive publication and interrupted recovery | `test_host_profile_store`, `test_host_profile_backup`, `test_host_profile_restore`, `test_host_profile_recovery`; host-profile round trip |
| Secret exclusion and public-release privacy | `test_public_release`; committed-tree release verifier |
| Bounded execution and cleanup | `test_process_runner`, `test_git_runner`; small migration fixture process tests |
| Generated app/package contract | `test_app_template`, `test_ui_package`; one real generated-app check and reproducible package build |
| Probe policy and UI state | `statusController.test.ts`, shared component tests; Index navigation/status journey |
| Context/export schema, compatibility, size limits | export model/document tests; actual download and offline-browser journeys |

Keep each policy at the cheapest layer that proves it. Above that owner, retain
representative integration rather than repeating every invalid value, injected
phase, or build failure. Do not freeze prose, implementation spelling, progress
messages, dependency versions, or CI job layout in tests. CI permission and
private-state boundaries remain checked.

## Disposable framework workflows

Run the matching workflow when changing its implementation or the contracts it
exercises. These commands prove integration that the unit tests' injected
executors cannot establish; `check:all` does not replace them:

| Changed contract | Required disposable workflow |
| --- | --- |
| Generated app lifecycle or template | `verify:new-app` and `verify:new-service-app` |
| Host profiles, restore, or generated Caddy configuration | `verify:host-profile` |
| Foundation adoption | `verify:foundation-adoption` |
| Repository-backed service transition | `verify:repository-service-transition` |
| Service command migration | `verify:service-command-migration` |
| Public base path migration | `verify:public-base-path-migration` |
| Fleet update orchestration | `verify:fleet-update` |

The foundation, public-base-path, and fleet workflow modules already retain real
acceptance within the full Python suite. When that run exercises the changed
workflow, use its evidence rather than repeating the same workflow standalone.

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

Pull requests run Python 3.14 on macOS 15 and Node 24 frontend checks on Ubuntu
24.04. Python includes real Caddy and disposable profile/migration integration;
the frontend job installs Chromium and runs the critical browser journeys.
Both use locked dependencies installed without lifecycle scripts. The packed UI
consumer check installs the local tarball with an empty offline cache.

Node 22 and 26 remain supported by `package.json`. Select **compatibility** on a
manual workflow dispatch to run frontend and browser checks on those majors.
They are not repeated for every pull request.

The workflow has only `contents: read` permission, retains no checkout
credentials, and consumes no repository secrets. It never installs the platform,
changes LaunchAgents or Tailscale, activates applications, deploys, or reads a
real host profile. All host and runtime fixtures remain disposable.

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
