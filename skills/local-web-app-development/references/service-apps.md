# Service applications

## Create deliberately

After selecting an identity as the main skill prescribes, use `"$LOCAL_WEB" app
init . --title "<title>" --icon <icon> --accent '<accent>' --kind service` in
an empty opened folder, or add the same identity and kind options to `app
create`. The generated reference service is intentionally small and
language-neutral at the hosting boundary. Replace its app-owned implementation
only after the domain architecture is approved.

## Contract

A supported service manifest declares:

- `kind: service`;
- a public health path and internal loopback health path;
- a frontend output within the immutable release;
- explicit proxy paths;
- a service module identifier and optional language-neutral `startCommand`;
- an optional frontend-only security declaration for exact external origins;
- explicit non-secret build environment names; and
- either a complete build output or exact `build.release` source-to-target entries.

The service must bind `127.0.0.1`, accept the registered port, and keep secrets out of the manifest and release. `build.release` is the ordinary platform-owned composer: list exact frontend, service-code, and immutable-data paths instead of building another app-specific packaging framework. Manifests without it retain their existing complete-output contract.

Keep language setup, dependency locks, lint, type checks, unit tests, and service tests app-local. Extended `npm run check` scripts are valid, but they must not invoke `local-web`.

### Frontend Content Security Policy

When a split service frontend needs browser access to external APIs, images, or
blob-backed workers, declare only the required directives and sources:

```json
"frontendSecurity": {
  "connectSources": [
    "https://api.maptiler.com",
    "https://api.openrouteservice.org"
  ],
  "imgSources": [
    "blob:"
  ],
  "workerSources": [
    "blob:"
  ],
  "childSources": [
    "blob:"
  ]
}
```

All four keys are independent and optional, and every supplied list must be
non-empty. `connectSources` accepts only exact HTTPS origins. `imgSources`
accepts exact HTTPS origins plus the exact token `blob:`. `workerSources` and
`childSources` accept only the exact token `blob:`; they do not accept external
origins or `data:`.

HTTPS origins use a DNS name or IP address and may include a valid non-default
port. DNS names must already use canonical lowercase form; IP literals are
canonicalized. Paths, trailing slashes, queries, fragments, credentials,
wildcards, HTTP, `file:`,
`javascript:`, arbitrary schemes, arbitrary CSP expressions, normalized
duplicates, empty lists, and unknown keys are invalid. The platform sorts
accepted sources before rendering them into the split frontend's Caddy handler.
It never adds `blob:` to `default-src`, never adds `unsafe-inline` or
`unsafe-eval`, and does not attach the frontend CSP to health or API proxy
handlers. Omit the object to retain the platform's exact default policy.

## Integrate an adopted frontend

Foundation adoption preserves an existing service's legacy build and creates an
independently testable React frontend; it does not make the service release use
that frontend. Keep backend and domain migration app-owned. In a later focused
change, make the build produce the React `dist` output and declare exact
`build.release` entries that compose `dist` into `service.frontendOutput` and
service code into its immutable target. Repository-backed data remains outside
those entries.

Until that app-owned release integration is coherent, the full app check is
expected to fail and activation is unavailable. Do not remove legacy assets
until service behaviour plus rendered, accessibility and browser parity are
proven. Never replace this integration with placeholder files, copied templates,
manual provenance or a host registry edit.

## Repository-backed data

When a service needs persistent app-owned data in its source repository, use
the canonical repository placeholder in the declared command:

```json
"startCommand": [
  "/usr/bin/env", "node", "{release}/server/service.mjs",
  "--port", "{port}", "--data-dir", "{repository}/data"
]
```

`{repository}` is expanded by registration to the canonical validated source
repository as host-private argv construction. Executable code and frontend
assets remain in the immutable release; persistent data stays outside releases
and is unaffected by deployment or rollback. The app validates the supplied
path, containment and schemas, and owns atomic writes, recovery, and backups.

Hard-coded host paths, manual registry edits, writes into the immutable
release, and arbitrary runtime path injection are unsupported. Do not put
persistent repository data in `build.release`.

## Change an existing service command

For an intentional command change on an already registered service, commit the
app-owned manifest change, then follow this ordered workflow:

```sh
"$LOCAL_WEB" app doctor --repository .
"$LOCAL_WEB" app check --repository .
"$LOCAL_WEB" app migrate-service-command --repository .
```

Inspect the bounded preview. It reports the application, service-command
status, unchanged identity, repository, route and port, and service action. It
is eligible only for the committed manifest; target argv comes only from the
committed manifest. The ID, canonical repository, route, and port remain fixed;
normal activate remains strict and continues to reject the command conflict.

After the preview is clean, obtain explicit approval for the live change, then
run:

```sh
"$LOCAL_WEB" app migrate-service-command --repository . --apply
```

The live apply requires separate explicit approval. The platform recovers only
its service-command transition; application data migration is application-owned
and retry-safe, so the service must validate, recover, and retry its own durable
data work; registry editing is forbidden.

### Origin-bearing commands and host ingress

An app command that names its public origin uses this same supported migration;
never hand-edit the host registry or a LaunchAgent to change it. Version 1 uses
the host's HTTP origin; version 2 uses the canonical HTTPS `publicOrigin`
through externally managed Tailscale Serve and loopback-only Caddy. Application
work does not select an ingress mode or configure Tailscale, Serve, grants,
certificates, or Funnel. Funnel and LAN fallback are prohibited.

An app that validates an exact origin may need a temporary two-origin command
during a separately reviewed HTTP-to-HTTPS host transition. Implement and test
that app-owned state first. Host and write Origin must match the same configured
entry, retaining existing credential and content-type checks. Preview and
separately approve the temporary command migration before host cutover; verify
reads, authorized writes, and recovery through both origins. Only after the host
cutover is verified may a second preview and separately approved apply select
the final HTTPS-only command. This preserves a recovery target compatible with
the active host on each side. Neither migration grants database authority or
changes app identity, repository, route, port, data path, or release pointers.

Any app that embeds the old origin is an explicit migration blocker. A failed
HTTPS gate remains a platform ingress blocker even when loopback service health
passes; never bypass it with HTTP, direct registry edits, or changes to Caddy or
LaunchAgents.

## Change an existing service public base path

Use this only for an already registered service whose committed manifest route
requires its release-time public base path to change. The platform manages one
key, `VITE_PUBLIC_BASE_PATH`, and derives its value only from the committed
manifest route plus a trailing slash. Callers cannot provide a variable name or
value. Commit the app-owned manifest change, then follow this ordered workflow:

```sh
"$LOCAL_WEB" app doctor --repository .
"$LOCAL_WEB" app check --repository .
"$LOCAL_WEB" app migrate-public-base-path --repository .
```

Inspect the bounded preview. It accepts only an existing registered service and
requires a fresh immutable target release for the change. The identity,
canonical repository, route, port, and expanded command remain unchanged;
normal activation remains strict, registration remains strict, and this does
not replace the separate service-command migration.

After the preview is clean, obtain explicit approval for the live change, then
run:

```sh
"$LOCAL_WEB" app migrate-public-base-path --repository . --apply
```

The live apply requires separate explicit approval. It recovers the exact prior
`current`/`previous` pointer pair and service state if the transition fails. It
has no application database authority: application data migration, recovery,
and retry remain application-owned. Registry editing is forbidden.

## Verify and activate

Run the full matrix in [verification.md](verification.md). `local-web app check`
assembles and validates a declared release after app-local checks. Activation
requires a clean committed `main` and matching provenance. Preview first, then
apply:

```sh
"$LOCAL_WEB" app activate --repository .
"$LOCAL_WEB" app activate --repository . --apply
```

The service port is assigned automatically from the platform range after the
registry and real loopback availability are checked. Do not choose or persist a
port in the app workflow. Activation owns registration, publication of an
immutable private host-profile registration revision, installation, deployment,
service recovery, and live System Index tile verification. JSON apply output
reports a newly published revision's content-addressed ID as `registryRevision`,
or `null` when registration is already current. If installation or deployment
fails after publication, the immutable revision remains the retry checkpoint;
rerunning activation consumes it without duplicating the revision.
If the app requires a service shape the manifest cannot express, report a
platform capability blocker rather than weakening the app or bypassing
verification.

To convert an already registered static app, commit the service manifest, run
`"$LOCAL_WEB" app activate --repository .` for the read-only preview, and then
run `"$LOCAL_WEB" app activate --repository . --apply`. Do not hand-edit the
registry to perform the conversion.
