# Application contract

## Ownership

Read `AGENTS.md` and `local-web.json` before editing an existing app. Preserve the app's domain-owned structure and its explicit architecture. Domain code remains app-owned, normally under `src/`; shared shell, theme behavior, and generic primitives come only from `@local-web/ui` public exports.

After resolving `LOCAL_WEB` as the skill prescribes, use `"$LOCAL_WEB" app
identity --search` before invoking `app init` or `app create` with an explicit
icon and accent. Use `"$LOCAL_WEB" app update` for an existing repository.
Never copy or reconstruct platform template files in this skill or an agent
response. Activation is the supported boundary for registering, installing,
deploying, and verifying an eligible app; do not hand-edit the registry, Caddy,
releases, or live services.

## Context exports

`src/contextExport.ts` and its `buildContextExport()` are app-owned domain
tooling. When a feature changes active state, decisions, evidence, freshness, assumptions, or omissions, update the adapter and its app-local tests in the same change.
Return only `LocalWebContextV1` to the shared shell; the app owns its field
semantics, freshness, and deliberate inclusion decisions.

Do not add automatic redaction, all media (including screenshots and images),
whole-app archives, or executable export scripts. Prefer one coherent same-origin service snapshot when a service has related current state. The local download requires no `service.frontendSecurity` and must not broaden CSP for download. Serving-app external origins never enter the downloaded document.

Context-export semantics stay in app-local tooling. Do not make Doctor parse context-export semantics or make a platform diagnostic depend on the payload.

## Optional interactive exports

Interactive export is additive and does not change `LocalWebContextV1`. The app
supplies one shared contract module with a positive semantic version, explicit
`private` or `sensitive` classification and notice, and a decoder used by both
hosted capture and offline bootstrap. The decoder validates `snapshotData`,
`viewState`, and their relationship. A service capture must validate the actual
coherent backend response; template compatibility alone is insufficient.

Keep snapshot data distinct from view state. Compiled static content may use an
empty snapshot object, and an app with no restorable presentation position may
use an empty view-state object. Only strict plain JSON is supported. Import the
same contract from the hosted builder and offline entry, and configure
`localWebInteractiveExport()` by module path alongside `localWebApp()`.

The framework owns the content-addressed template, compatibility identities,
asset inlining, size bounds, CSP, download lifecycle, sensitivity display, and
offline shell. The app owns capture inclusion decisions, controlled-state
restoration, read-only/disabled/local behavior for backend actions, and domain
acceptance tests. Offline artifacts have no automatic network dependency,
recursive export, backend reconnection, or mutation replay. Follow the public
repository README for the exact API, CSP, identity semantics, and fixture.

## Manifest

Treat `local-web.json` as the app-owned hosting contract. Preserve its id, route, kind, build, health, home presentation, platform versions, and declared capabilities unless the requested change requires a reviewed contract update. Keep environment names explicit and keep values out of the manifest.

Home presentation is required for generated and activated apps. Choose its icon
from the platform identity search and its uppercase accent from the approved
palette. The `apps` icon is reserved for the platform. Do not add a local icon,
invent an arbitrary colour, or accept a generic fallback merely to complete
initialization.

Static is the default. A local backend uses the supported service shape in [service-apps.md](service-apps.md); do not hide service requirements inside static build scripts.

A split service frontend that needs browser access to an external endpoint may
declare the narrow `service.frontendSecurity` contract documented in
[service-apps.md](service-apps.md). Grant only the exact origins and directives
the browser actually needs. Do not weaken application code, proxy external
traffic merely to evade the frontend policy, or edit generated Caddy state.

Use the platform's generated base-path and theme integration. Application source must not replace platform-owned shell or shared UI behavior with private copies.

For an already registered service, the supported public-base-path migration owns
only `VITE_PUBLIC_BASE_PATH`. The platform derives its value only from the
committed manifest route plus a trailing slash; callers do not supply either a
variable name or value. Read [service-apps.md](service-apps.md) for the
bounded preview, approval, and apply workflow. Do not hand-edit release
environment, registration, or registry state to emulate it.

## Existing applications

### Routine registered-fleet refresh

Use the plural command only when a reviewed platform or shared-UI release needs
the ordinary compatible refresh applied across every already registered app. It
does not replace the existing single-app route for adoption or application work:

```sh
"$LOCAL_WEB" apps update --dry-run
```

Review every final app status, the planned repository-relative platform paths,
and each reported source commit and deployed commit. `CURRENT` proves those
commits are equal and needs no write; `SKIPPED` means a
legacy adoption, foundation adoption or domain migration needs the deliberate
single-app route; `BLOCKED` needs its app-specific issue resolved. The fleet
coordinator creates focused app commits, runs the app checks and activates only
the compatible refreshes. After the review, obtain separate explicit approval
before running:

```sh
"$LOCAL_WEB" apps update --apply
```

Never use this command to change capabilities, kind, build, route, health,
service command, React/Vite foundation, domain code or application data.
Review the final status and live commit for every app after apply; a non-zero
exit is expected when any app is `SKIPPED`, `BLOCKED` or recovered from a
failure, and is not by itself a substitute for that review.

`live-release-not-current` is deliberately `BLOCKED`: the fleet never silently
repairs a missing, malformed or stale live release. Use `"$LOCAL_WEB" app
activate --repository .` and its separately approved `--apply` form for that
single-app repair, then rerun the fleet preview.

Follow the repository's own instructions, tests, and architecture. When Doctor
reports a legacy or updateable platform diagnostic, preview and apply the
central updater. For a legacy Supabase app, use this sequence:

```sh
"$LOCAL_WEB" app update --repository . --capability supabase --dry-run
"$LOCAL_WEB" app update --repository . --capability supabase
git diff -- local-web.json .local-web-platform.json vendor/local-web-ui.tgz \
  package.json package-lock.json index.html
# Make a focused app-owned commit, then:
"$LOCAL_WEB" app doctor --repository .
npm run check
npm run test:e2e
"$LOCAL_WEB" app check --repository .
```

Supply capabilities only during legacy adoption. Omit `--capability` for an
app without that capability and for every current-platform refresh. The real
update leaves reviewable uncommitted changes limited to the platform contract
files shown above. Review them and make a focused app-owned commit; the updater
does not stage or commit them.

Platform adoption does not integrate app-owned React, Vite, CSS, or test code.
Design the requested source integration before implementation, preserve domain
structure, and keep application-specific components and state in the app rather
than expanding the platform for one consumer.

### Existing service without a root frontend foundation

Use this route only when an existing non-empty service repository has no root
React/Vite foundation and Doctor identifies it as legacy. Work in an isolated
feature branch or worktree. Preview and apply only through the platform updater:

```sh
"$LOCAL_WEB" app update --repository . --foundation react-vite --dry-run
"$LOCAL_WEB" app update --repository . --foundation react-vite
git diff -- local-web.json .local-web-platform.json vendor/local-web-ui.tgz \
  package.json package-lock.json index.html src tests \
  eslint.config.js playwright.config.ts tsconfig.app.json tsconfig.json \
  tsconfig.node.json vite.config.ts
"$LOCAL_WEB" app doctor --repository .
npm run check
npm run test:e2e
"$LOCAL_WEB" app check --repository .
```

The dry run must precede apply, and the focused diff must precede Doctor. Review
and commit the updater's exact planned files before changing application-owned
source or release composition. Adoption preserves the legacy build, backend,
frontend and repository data; it does not activate the application. Doctor,
`npm run check` and `npm run test:e2e` can pass while the full app check is
expected to fail until app-owned release integration makes the declared service
output coherent.

Platform adoption and domain/frontend integration are separate changes. After
the focused adoption commit, design and implement the app-owned migration,
including exact `build.release` entries, while preserving legacy behaviour
until parity is proven. Rerun the full matrix against the coherent release and
merge it to clean `main` before any separately authorised activation.

Do not create placeholder `package.json` or `index.html` files to trick the
normal updater. Copied templates and manual provenance are unsupported. Never
edit the host registry, Caddy, releases or live services, and never activate the
intermediate foundation commit.

When a valid app architecture exceeds the platform contract, stop at the ownership boundary and report the missing platform capability. Do not weaken the domain design, bypass doctor, or hand-edit host configuration to make a diagnostic disappear.
