# Local Web architecture

Local Web turns a set of independent Git repositories into a small application
catalogue on one macOS account. The framework owns ingress, release composition,
service supervision, shared UI contracts, and recovery. Each application owns
its source, manifest, domain behavior, durable data, and authorization rules.

## Components

```text
browser
  -> trusted LAN HTTP, or externally managed Tailscale Serve HTTPS
  -> Caddy
       -> immutable static release
       -> immutable split-service frontend + loopback API
       -> loopback legacy service
  -> System Index registry and shared theme

framework checkout
  -> ignored private host profile
  -> HostProfileStore revision chain
  -> generated runtime, Caddyfile, LaunchAgents, hooks, and releases
```

The Python CLI and installer are the only supported writers of platform-owned
state. Generated Caddyfiles, LaunchAgents, Git hooks, release pointers, and the
host profile are outputs, not operator editing surfaces.

## Host profile and trust boundary

The canonical registry is `config/local/apps.json` beneath the framework
repository. It is private host configuration, not a secret store. It records
the ingress shape, runtime root, application repositories, allowed build inputs,
and service ports/commands. Environment values may be referenced, but secrets
remain in ignored app-owned environment files and are never printed.

`HostProfileStore` publishes exact registry bytes with an immutable,
content-addressed revision envelope. A transaction marker makes an interruption
explicit. Mutating operations refuse dirty or non-`main` framework source and
refuse unknown profile, history, permission, symlink, or recovery state. See the
[host-profile contract](operations/host-profile.md).

The public `config/apps.example.json` is documentation only. It is never an
automatic fallback for a missing private profile.

## Ingress modes

Host registry schema version 1 uses `trusted-lan`: Caddy listens on HTTP port 80.
There is no authentication or TLS, so every device that can reach the host can
reach the applications.

Schema version 2 uses `tailscale-serve`: an exact canonical
`https://<device>.<tailnet>.ts.net` origin is terminated by externally managed
Tailscale Serve and forwarded to Caddy on `127.0.0.1:8080`. Caddy accepts only
the canonical Host. Funnel, wildcard application listeners, direct-IP HTTP, and
a LAN fallback are unsupported. Local Web does not administer Tailscale devices,
grants, certificates, Serve, or daemon state.

Ingress controls reachability, not application authorization. An app that
changes data still validates its own request origin, credentials, content type,
and domain rules.

## Application and release model

Every app commits `local-web.json`. The manifest declares identity, public route,
static or service kind, direct-argument build commands, explicit environment
names, output, health path, and optional home/platform metadata. Service apps
also declare loopback health, and can declare a split frontend plus exact proxy
paths and a language-neutral start command.

Builds run from a committed `main` revision into a staging area. The framework
validates outputs, composes a new immutable release, switches the `current` and
`previous` pointers atomically, and then applies health gates. Static apps need
their frontend. Service apps additionally need a running managed process,
loopback health, and public backend health. Failure restores the prior pointer
pair and prior service state; app-owned database recovery is a separate concern.

Managed `post-commit` and `post-merge` hooks deploy only from `main`. Hook errors
are reported without turning a successful Git operation into a failure. Manual
deployment, activation, and fleet update remain explicit commands.

## Split-service frontend policy

Split frontends receive a restrictive Content Security Policy by default. A
manifest may independently extend `connect-src`, `img-src`, `worker-src`, and
`child-src` through `service.frontendSecurity`. Exact HTTPS origins are supported
only where documented; `blob:` is accepted only for the image/worker/child cases
defined by the contract. Wildcards, arbitrary CSP expressions, unsafe schemes,
paths, credentials, queries, fragments, duplicates, and empty lists fail closed.
API and health proxy handlers do not receive the frontend policy.

## Shared UI and exports

`@local-web/ui` owns the platform shell, theme tokens, app identity primitives,
context download, and optional interactive-export packaging. Domain components
stay in each app.

Every platform shell renders the version of its compiled `@local-web/ui` package
immediately before the right-hand header controls. The package metadata is the
single version source, so applications do not pass or maintain this value. The
label identifies the shared UI reader—including inside an offline snapshot—and
does not claim an application version, source revision, or live-release commit.

Context export is an inert, validated `LocalWebContextV1` document. Interactive
export is optional and packages one validated runtime snapshot into a dedicated
self-contained offline reader. Snapshot data and view state remain distinct;
the app supplies one decoder used during hosted capture and offline bootstrap.
Compatibility identities bind the capture contract and reader so an old page
cannot silently combine state with a newly deployed template. The offline
artifact has no automatic network dependency and uses its own restrictive CSP.

## Runtime ownership

The runtime root contains generated configuration, platform assets, logs, and
immutable releases. It is disposable and can be rebuilt from framework/app
repositories plus the private host profile. It is not a backup for:

- `config/local/` history or host backups;
- app environment files;
- service databases, uploads, or other durable data; or
- externally managed ingress configuration.

The [recovery runbook](operations/recovery.md) restores these layers separately
and requires live readback before making a host-health claim.
