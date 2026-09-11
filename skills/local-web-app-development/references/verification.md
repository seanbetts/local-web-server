# Verification and activation

## Host-ingress verification boundary

The host registry selects schema version 1 trusted-LAN HTTP or schema version 2
`tailscale-serve` HTTPS. The latter derives Host from its canonical
`publicOrigin` (public examples use `https://local.example.ts.net`). Public
status, deployment, rollback and served-tile checks use that exact origin.
Internal service checks remain loopback HTTP with canonical Host; success there
does not prove HTTPS ingress health.

A schema version 2 HTTPS failure is a platform ingress blocker. Do not replace
the public check with LAN or direct Tailscale-IP HTTP, disable TLS verification,
or edit the registry, Caddy or LaunchAgents. App work never configures Tailscale
or migrates host ingress. Funnel is prohibited, and there is no LAN fallback.
Origin-bearing service commands use the preview and separately approved live
`app migrate-service-command` apply described in [service-apps.md](service-apps.md).

Host installation is a separate operator workflow. Version 2 dry run makes no
ingress request. Apply requires an already loaded managed Caddy and a valid-TLS,
body-free HTTPS preflight; after loading, it requires a successful public
platform response and verified Caddy isolation to `127.0.0.1:8080`, even
on a byte-identical reinstall. A missing Caddy blocks version 2 activation retry;
do not use the version 1 bootstrap recovery as a bypass. Before reinstalling,
the operator must read back the effective Serve mapping and prove Funnel
absent. These live checks and any host change require their own approval.

If normal installation reports that port migration preparation is required,
stop app activation and hand off to the host operator. The separately approved
host sequence is explicit dual-listener preparation, operator Serve migration
from port 80 to 8080, then normal installation to remove wildcard port 80.
Preparation is temporary, not final security activation; only its explicit
verification accepts `*:80` plus `127.0.0.1:8080`. App work must not invoke the
preparation flag or switch Serve. Port 8080 is reserved for Caddy in schema
version 2. See the platform recovery runbook's high-port handover procedure.

Registry and installed listener state must be read back, not inferred from the schema alone.
Local tests do not establish Tailscale activation, an app's temporary dual-origin
or final HTTPS-only command migration, or physical-device acceptance. The latter
needs operator-observed evidence from another enrolled and permitted device,
including failure with Tailscale disconnected and rejection of direct HTTP.

## Fleet refresh verification

For an approved routine shared platform/UI refresh, first run
`"$LOCAL_WEB" apps update --dry-run` from the platform repository. Review every
final app status, planned path list, source commit and deployed commit; obtain separate
explicit approval before `"$LOCAL_WEB" apps update --apply`. After apply,
review every final status and both commits again. `CURRENT` is valid only when
the source and deployed commits are equal. `live-release-not-current` routes to
the explicit single-app `app activate` workflow; the fleet does not repair it.
A process exit alone is not
evidence that each application was safely refreshed: `SKIPPED`, `BLOCKED` and
`FAILED_RECOVERED` intentionally produce a non-zero aggregate result.

Use the existing single-app matrix below for legacy adoption, foundation
adoption, capability changes, domain migration and all app-specific work.

After an existing-app update, first review only the updater-owned contract
surface:

```sh
git diff -- local-web.json .local-web-platform.json vendor/local-web-ui.tgz \
  package.json package-lock.json index.html
```

Make a focused app-owned commit before running the complete matrix. The updater
leaves changes uncommitted and does not perform source integration.

## Matrix

Run these checks from the application repository after installing its lockfile dependencies when needed:

| Check | Purpose |
| --- | --- |
| `"$LOCAL_WEB" app doctor --repository .` | Inspect Git metadata and known platform artifacts without running app code or parsing arbitrary TypeScript, TSX, CSS, or Vite source. |
| `npm run check` | Run the app-local lint, unit-test, type/build contract. This script must not invoke `local-web`. |
| `npm run test:e2e` | Exercise the rendered application at its real base path. |
| `"$LOCAL_WEB" app check --repository .` | Require a clean structural doctor, run bounded app-local and browser checks, then validate any declarative release. |

Use standard TypeScript, ESLint, Vitest, Playwright, and Vite diagnostics for source failures. Do not add a custom parser or custom ESLint plugin unless a concrete app requirement demonstrates that the standard tooling cannot express the check.

## Context-export acceptance

Keep context-export semantics app-local: unit-test `buildContextExport()` for
the app's active state, decisions, evidence, assumptions, and omissions; use
the real E2E download to verify the rendered artifact. Do not print a context
payload body in verification output. Doctor remains structural and does not
parse export semantics.

During framework or app diagnosis, an intentional existing-service command
change may use `"$LOCAL_WEB" app migrate-service-command --repository .` for a
read-only bounded preview after Doctor and full app check. Its `--apply` form is
a live-state mutation and requires separate explicit approval; follow the
complete workflow and ownership boundaries in [service-apps.md](service-apps.md).

An intentional existing-service public-base-path migration likewise follows
Doctor and the full app check before its read-only
`"$LOCAL_WEB" app migrate-public-base-path --repository .` preview. Its
`--apply` form requires separate explicit approval; follow the complete
fresh-release, recovery, and ownership boundaries in
[service-apps.md](service-apps.md). Normal activation remains strict and does
not substitute for either migration.

## Foundation-adoption boundary

For an existing service without a root React/Vite foundation, adopt in an
isolated feature branch or worktree using the ordered commands in
[app-contract.md](app-contract.md). Review and commit only the updater's planned
files. Platform adoption does not activate, replace the legacy build, migrate
backend/domain code or perform app-owned release integration.

After adoption, Doctor, `npm run check` and `npm run test:e2e` prove the
foundation itself. The full app check is expected to fail its release contract
until the application build composes the React output into the declared service
frontend. Preserve that validation; do not add a placeholder `package.json` or
`index.html`, copied templates, manual provenance or host registry changes to
make it pass.

After the application owns a coherent release, rerun every matrix row. Prove
backend behaviour and rendered parity before removing the legacy frontend,
commit the complete integration on clean `main`, and only then proceed to the
separate activation gate below.

## Activation gate

Activation follows verification; it never substitutes for it. Confirm the app
is healthy and every row above passes. Preview the full lifecycle first with
`"$LOCAL_WEB" app activate --repository .`; it is read-only and reports the
planned stages plus any automatically selected service port. Apply only after
the preview is clean with `"$LOCAL_WEB" app activate --repository . --apply`.

Activation registers the app and publishes a
private host-profile registration revision. That revision is immutable.
Activation then installs platform state, deploys the app, recovers its service
when applicable, and verifies the exact live System Index tile. JSON apply
output reports a newly published revision's content-addressed ID as
`registryRevision`, or `null` when registration is already current. If a later
stage fails, that immutable revision remains the retry checkpoint; rerunning
activation consumes it without duplicating the revision. Activation does not
commit app-owned changes. Never hand-edit the registry or choose a service port
to bypass eligibility. A generated placeholder follows the immediate foundation
route in the main skill; subsequent app development uses this full verification
gate.

## Rendered acceptance

Automated accessibility and no-overflow checks are necessary but not sufficient. Inspect desktop, 390 px, and 320 px layouts in light and dark modes. Preserve the approved information hierarchy; avoid duplicate app mastheads; keep tap targets comfortable; progressively disclose dense supporting data; check chart labels for clipping and tables for bounded internal scrolling. When an approved concept exists, compare the rendered result directly with it before claiming completion.
