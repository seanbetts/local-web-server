# Contributing

Local Web is a single-user macOS framework. Framework code owns host lifecycle,
routing, shared UI and reusable app contracts; applications own their domain
behaviour and durable data. Give each guarantee a primary test owner and keep
integration journeys only where they prove the components work together.

## Prerequisites

Use macOS, Python 3.14, a supported Node version from `package.json`, npm,
Chromium and Caddy 2 for integration. Routine CI uses Node 24. Install locked
JavaScript dependencies without lifecycle scripts:

```sh
npm ci --ignore-scripts
npx playwright install chromium
```

## Choosing checks

| Tier | When | Command |
| --- | --- | --- |
| Routine | Every PR; after a coherent local change | `PYTHONWARNINGS=error::ResourceWarning npm run check` |
| Integration | Deployment, storage, registration, security, ingress or update changes | `PYTHONWARNINGS=error::ResourceWarning npm run check:integration` |
| Release | Before publishing a release; shared verification/tooling changes | `PYTHONWARNINGS=error::ResourceWarning npm run check:release` |
| Maintenance | Scale or toolchain changes; deliberate periodic checks | `npm run test:python:stress`; manual CI compatibility input |

Routine runs focused Python component contracts, frontend types/lint/unit/build
checks, the packed UI consumer, functional browser journeys and the actual
public-tree verifier. It includes deployment recovery component tests and export
privacy. `verify:public-release` validates committed source and refuses staged
changes: commit the coherent change before that final gate. A passing committed
scan does not certify uncommitted files.

Integration is an **additional** group: profile integrity/publication/restore,
registration/update orchestration, installer behaviour and real Caddy routing.
It requires Caddy and fails if Caddy is missing. Run routine plus integration for
these changes. The macOS metadata, case-alias, descriptor-publication, interrupted
recovery and process-cleanup regressions remain covered here or in routine.

Release runs all retained Python tests except stress, the frontend and browser
checks, four pinned theme screenshots, public-tree verification and the additional
new-static-app, new-service-app and repository-service-transition workflows.
`npm run check:all` runs the full retained automated surface without those three
additional standalone workflows. Use a passing journey from the same revision
instead of rerunning it under another wrapper.

During editing, select the affected module, class or method directly:

```sh
PYTHONWARNINGS=error::ResourceWarning python3 scripts/run_tests.py tests.test_deploy
PYTHONWARNINGS=error::ResourceWarning python3 scripts/run_tests.py tests.test_host_profile_backup.BackupTests.test_explicit_output_never_clobbers_existing_content_or_symlink_target
python3 scripts/run_tests.py --suite integration --list
```

The small runner keeps private short temporary paths for macOS Unix sockets and
uses unittest's duration reporting. Tier membership is explicit in
`scripts/run_tests.py`: unknown modules default to integration. `routine`,
`integration` and `release` are disjoint; `all` is their union. The old direct
`fast` and `acceptance` selections remain available with their previous meaning,
but are not the routine policy. Ordinary discovery also includes stress:

```sh
python3 -m unittest discover -s tests -v
```

### Feedback budgets

With dependencies and Chromium already installed, target 1–2 minutes locally and
2–4 minutes hosted for routine checks. Relevant integration groups target 3–8
minutes locally and 6–16 minutes hosted; focused modules should be much quicker.
The initial local release budget is 15 minutes. These are investigation triggers,
not timeout settings or permission to weaken assertions.

The pre-policy evidence was 7m14s local / 13m08s hosted for the old “fast” Python
partition, 11m34s for the complete merged local Python run, and about 28s for local
frontend plus browser checks. These runs used different execution arrangements;
do not describe their sums as a controlled benchmark. Report actual tier times,
setup costs and hosted results separately when the policy changes.

## Coverage ownership

| Guarantee | Primary owner; representative integration |
| --- | --- |
| Manifest, safe paths, CSP and routing | `test_config`, `test_render`; real Caddy routing |
| Failed builds and data-preserving deployment/rollback | `test_git_build`, `test_deploy`; fleet recovery/continuation |
| App files and exact commit/restoration ownership | `test_app_update_transaction`, `test_app_platform_commit`; fleet update |
| Profile corruption, no-clobber publication, interrupted recovery | `test_host_profile_store`, `test_host_profile_backup`, `test_host_profile_restore`, `test_host_profile_recovery`; one profile round trip |
| Private output and public tree | `test_public_release`, actual public-release verifier; narrow repository-specific private-marker check |
| Bounded processes and cleanup | `test_process_runner`, `test_git_runner`; fixture worker containment checks |
| Generated app/package contract | Template/package focused tests; real generated-app check and public package build |
| macOS service state and installation | `test_services`, `test_install`; retain both while their parsers are separate implementations |
| Probe policy and visible state | `statusController.test.ts`, shared components; Index navigation/status journey |
| Context and offline export privacy/compatibility | Export model/document tests; actual download and self-contained offline interaction |

Remove a test only when its distinct regression has another owner or its coverage
tradeoff is explicit. Keep known regressions. Do not freeze prose, private method
inventories, exact internal commands, CI job layouts or recording-fake behaviour.
Avoid recreating whole repositories/installations to test formatting or one
injected result. Do not replace the existing runner with another test framework.

## Disposable framework workflows

When changing a lifecycle or verifier, run its matching journey even if it usually
belongs to the release tier. All paths and service processes must be disposable.

| Changed contract | Focused workflow |
| --- | --- |
| Generated app/template | `npm run verify:new-app`, `npm run verify:new-service-app` |
| Host profiles and recovery | `npm run verify:host-profile` (already covered by integration's real round trip) |
| Foundation adoption | `npm run verify:foundation-adoption` |
| Repository service transition | `npm run verify:repository-service-transition` |
| Service command migration | `npm run verify:service-command-migration` |
| Public base path migration | `npm run verify:public-base-path-migration` |
| Fleet update | `npm run verify:fleet-update` |

The foundation, two migrations and fleet journeys are already in the Python
release group. Do not repeat their standalone commands after equivalent passing
evidence from that revision. Private `config/local/`, live application data,
LaunchAgents and Tailscale are never test fixtures.

## Browser and visual checks

Routine browser checks cover navigation/status, native modal focus, context
transfers, theme controls, narrow layouts and offline exports. Four `@visual`
gallery tests compare light/dark desktop/mobile themes only for theme/shell
changes and releases:

```sh
npm run build:ui
npm run test:gallery:visual
```

`test:gallery:e2e` continues to include all gallery cases for the production theme
release gate. Functional composition uses `test:gallery:functional`. Neither
browser retries nor desktop pixel allowances are enabled. A screenshot failure
requires inspecting expected/actual/diff images and browser/OS/font evidence.
Do not accept new baselines or attribute differences to rasterisation without
that diagnosis. CI retains failed comparison PNGs for seven days. A blocked
visual gate remains blocked; functional success does not replace it.

## Public CI

Every PR runs the routine tier on macOS 15, Python 3.14 and Node 24. The workflow
contains a small path policy: docs/frontend changes run routine; Python tests,
framework and templates add integration; shared scripts, dependency/configuration
and unknown paths add release verification. Theme/shell changes add visual checks.
Lifecycle implementation/template changes also add release journeys. Changes to
selection or shared verification infrastructure therefore exercise the full gate.

Manual dispatch selects routine/integration/release depth, plus optional Node
22/26 compatibility and supported-scale restoration. Compatibility runs functional
frontend/browser checks; visual correctness has one primary toolchain. Additional
majors are not repeated on every routine change.

CI has only `contents: read`, retains no checkout credentials and consumes no
repository secrets. It does not install a live host, activate, deploy or push.
The inherited Homebrew npm provision remains confined to disposable release
checks because production process runners select fixed executable paths.

## Change discipline

Keep app-local `npm run check` independent of `local-web`. Preserve fail-closed
profile/CSP/ingress boundaries. Never edit live configuration, pointers or user
files to make a check pass. Commit, merge, activation, deployment and push are
separate operations. Tests do not establish live-host or physical-device health.

Before review, run relevant checks, `python3 -m compileall -q local_web_server scripts tests`,
`git diff --check` and `npm run verify:public-release`. Run `npm audit` for dependency
changes/releases; `npm audit --omit=dev` gives the production-only view. Report
measured commands, intentional deferrals and platform limitations. Reuse valid
unchanged expensive-workflow evidence, and never add retries or increase timeouts
to obtain a green run without diagnosing the failure.
