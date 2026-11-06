# Mac-built Local Web on a NAS: proposed design

Date: 2026-11-06. Status: proposed for review; not authority to deploy or migrate data.

## Decisions and scope

The selected approach keeps development, source repositories, verification and
production builds on the Mac. The NAS runs Caddy, the System Index, two static
apps and five app backends, with persistent app data on its SSD-backed application
storage. Deployed apps must work with the Mac switched off.

The existing Local Web HTTPS hostname must be preserved. The NAS's existing
identity and media-service addresses remain separate. Real hostnames, paths,
credentials, service URLs and identity handoff records belong in private host
configuration, not this public document. Examples use `https://local.example.ts.net`.

Add two visible System Index sections: **Local Web apps** and **NAS services**.
NAS services initially contain NAS Administration, Jellyfin, qBittorrent,
Prowlarr, Radarr, Sonarr and slskd. They are ordinary links to the already
configured HTTPS addresses, retain each service's login and have no availability
polling. This feature can ship independently before the hosting migration.

## Approaches considered

| Approach | Trade-off | Decision |
| --- | --- | --- |
| NAS front door, Mac backends | Smaller change; apps still depend on Mac availability | Rejected |
| Mac builds; NAS runtime and data | Needs remote release/data operations; preserves native build speed | Selected |
| NAS builds and runtime | Avoids sending build outputs; adds NAS toolchains and build contention | Not selected |

The isolated two-app comparison supports the selected approach: native frontend
builds and changed-file transfers were fast, while NAS builders stayed within
2 CPU / 2 GiB limits. Those observations are not seven-app capacity or playback
load certification. The delta experiment transferred bytes; it did not implement
production publication, obsolete-file handling or rollback.

## Proposed architecture

```text
Mac: app repositories -> checks -> native-platform frontend/pure-code build
                              -> cached Linux/amd64 runtime dependency build
                              -> immutable release descriptor + changed bytes
                                         |
                         fixed NAS adapter over retained SSH authentication
                                         |
NAS: persistent release/data storage -> dedicated Local Web Compose project
     private Tailscale HTTPS -> loopback Caddy -> static files / private APIs
```

The platform owns the public release contract, rendering and Mac-side lifecycle.
The NAS administration repository owns the installation-specific SSH/Keychain
adapter, fixed host operations and backup integration. Connect these through a
configured executable with a narrow JSON protocol; do not import private NAS
code into the public platform or create a generic remote shell service.

The Mac remains authoritative for source and the private host-profile revision
chain. The NAS stores the active deployment descriptor, recovery checkpoints,
current/previous releases and app data. A lost acknowledgement is reconciled
against that descriptor; it must not replay activation blindly.

### Build and cache contract

- Build exact committed revisions in source exports, excluding ignored state,
  credentials, local virtual environments and installed dependencies.
- Frontend and platform-independent bundling run on the native build platform;
  do not force them through amd64 emulation on an ARM Mac.
- Linux/amd64 runtime dependencies are built on the Mac in isolation and cached
  by dependency-lock digest, runtime/base-image digest, ABI/architecture and build
  recipe version. Never ship a macOS virtual environment or native Node addon.
- A dependency change rebuilds that dependency artifact; a source/CSS change
  reuses it. Record exact versions and image digests in every release descriptor.
- The NAS installs verified prepared runtime artifacts/images; it runs no npm,
  pip, compiler or source-build step during publication.

Each service declares a separate `ContainerRuntimeSpec` (image/dependency recipe
and direct-argument start command) rather than reusing a Mac executable path.
Use stable container paths `/app/current` for code and `/state` for durable data.
Its command supports only `{port}`, `{state}`, `{publicOrigin}`, `{bindHost}` and
`{proxyAddress}` substitutions. Existing Mac start-command expansion is unchanged;
NAS commands must not expand `{repository}` into a Mac checkout or mount that
checkout. An absent container recipe is a clear compatibility blocker.

### Incremental publication contract

Use a versioned complete file manifest, not an in-place synchronization of the
serving directory. `ReleaseDescriptorV1` contains app ID, source commit, recipe
version, runtime image digest, dependency artifact digest and canonical relative
file entries (SHA-256, size and mode). Its digest identifies the whole release.

The NAS reports which required blobs/dependency images are already present. Batch
missing app blobs into one compressed bundle over the existing SCP/SSH route;
keep metadata in JSON and large artifact bytes out of JSON/base64. Reuse verified
completed objects after interruption; a partial or corrupt object is resent.
Complete image archives are acceptable on a runtime/dependency cache miss; ordinary
app changes do not resend base images, unchanged assets or Python dependencies.

Materialize a new release from exactly the descriptor's files, verify it, then
switch a stable `current` parent binding and restart only the selected backend.
Unchanged files may be reused from immutable releases, but must never be modified
through shared hardlinks. Missing files in the new manifest are absent from the
new release; do not delete files from the live release while uploading.

Retain `current` and `previous`, their dependency artifacts and recovery-referenced
inputs. Prune only unreferenced platform-owned objects after successful publication.
Do not prune Docker globally. Failed transfer/build leaves the serving release
untouched. Automatic failed-health rollback is permitted only for an app-owned,
tested backward-compatible data contract. Normal deployments must not run
incompatible startup migrations. A release marked `migration-required` is refused
by automatic hooks/fleet publication and needs an explicit app migration procedure,
writer fence, consistent snapshot and reviewed data recovery before activation.
If startup unexpectedly changes data incompatibly, keep writers fenced and report
recovery required rather than starting old code on a newer store. Never restore
an older snapshot automatically to make health green.

### Runtime and ingress

Use one dedicated Compose project: Caddy, a dedicated Local Web Tailscale sidecar
and one container per backend. Static apps need no backend containers. Runtime
images and code are read-only; each backend receives only its own durable-data
and necessary secret bindings. No Docker socket is mounted into app containers.

Caddy shares the Tailscale sidecar's network namespace, listening on loopback
port 8080, with no NAS-host web ports published. The sidecar uses persistent
private state and userspace networking where supported by the validated image.
The NAS's existing Tailscale container, VPNs and media networks remain unchanged.

Backend containers use a dedicated private Docker bridge, with no host-published
ports. Unlike the Mac target, they listen on a configurable container interface.
Keep strict same-origin validation. Trust forwarded headers only from the exact
configured Caddy/sidecar address; preserve loopback defaults on the Mac. Allocate
the bridge and fixed proxy address after checking actual subnet conflicts.

This requires an explicit NAS target contract; current Mac-only listener and
LaunchAgent checks must not be disabled or silently bypassed. Initial conservative
per-app limits are proposed at 1 CPU / 512 MiB for Node services and 2 CPU / 1 GiB
for Python services, then adjusted from per-app shadow observations. They are
ceilings, not reserved RAM, and the two-app benchmark is not evidence for all apps.

### Hostname preservation

Stage a new, persistent Local Web Tailscale identity under a temporary name.
Rehearse with valid HTTPS, the intended owner access and copied/synthetic state.
Before live handoff, capture the existing Mac node ID, name, Serve settings,
grants and other uses of that identity. Preserve the Mac's network access.

At approved cutover, free the existing Local Web machine name through a reviewed
rename, assign that exact name to the staged NAS node, and verify MagicDNS, valid
TLS, Serve destination, canonical Host and exact access policy. Do not copy
macOS Tailscale state into Linux, delete the old device, accept a suffixed name,
enable Funnel or reopen LAN HTTP to make the check pass. DNS/certificate readiness
is a real handoff gate, not an assumed zero-downtime operation. Retain the old Mac
identity under its reviewed rollback name until acceptance; reversing the name
handoff must be rehearsed. If name reassignment cannot preserve the origin safely,
stop at the Mac deployment and bring the specific blocker back for a decision.

## Data and app workflows

| App | Durable state / adaptation to prove before cutover |
| --- | --- |
| Shirts; Cowork Architecture | Static outputs; confirm there are no unrecorded runtime dependencies |
| routine app | Routine store/backups; browser mutations and agent/check-in route reach NAS |
| ticket app | SQLite state/backups; explicit NAS state path, no automatic legacy-state adoption |
| travel app | Trips, media, backups and maintenance locks; configure existing API-backed trip CLI for NAS |
| career app | Classify source-controlled strategy/documents versus mutable tracker records; move runtime records only and update direct-file agent workflows |
| finance app | SQLite, snapshots and refresh evidence; preserve calculations and preview/apply monthly-refresh workflow through an app-owned NAS operation |

NAS becomes the only writable production store. Local development uses isolated
fixtures/copies. Remove implicit production fallbacks to Mac repository data.
No bidirectional file sync or two active writers. Remote APIs are preferred;
file-only operations need narrow app-owned adapters preserving their existing
locks, validations, dry runs and recovery. Do not add a generic SQL/shell endpoint.

Before copying live data, fence all app writers, including Mac CLI/agent writers;
stop only the managed Local Web backends. Create consistent SQLite backups using
the supported backup mechanism, not a live main-file copy that ignores WAL.
Transfer and verify selected files, schema/revisions, records and media references.
Keep the originals read-only until acceptance. Back up new NAS state, secrets,
Compose configuration, Tailscale state and recovery records through the existing
essentials mechanism. Include a supported export of the Mac's canonical private
host-profile history; the NAS repository's existing Mac backup selection does not
automatically cover a sibling platform checkout. Exclude caches and rebuildable
releases. All app-owned uploads/media require a verified backup route too; if they
exceed the essentials size budget, use an explicit separate app-data backup rather
than silently omitting them or including unrelated media libraries.

After NAS writes begin, rollback must fence NAS writers, preserve its latest
consistent state and return that state through the app's validated restore route
before re-enabling Mac writers. Merely restarting the old Mac database is unsafe.
Schema incompatibility is an explicit recovery decision, not automatic data loss.

## Standard workflow and acceptance

Keep the existing `app create/init`, `app doctor`, `app update`, `app check`,
`app activate`, `deploy`, `status`, `rollback` and fleet-update entry points.
Target routing selects Mac launchd or NAS Compose. Update generated manifests,
templates, provenance, the installed skill and existing app guides together.
Do not require agents to know SSH paths, image IDs or bespoke deployment commands.

Suspend managed deployment hooks during cutover and restore their existing
approved policy only after they route to the NAS. A future main-branch hook runs
the same Mac build/publish path and must fail visibly when the NAS is unavailable.
No silent fallback to deploying locally. Hostname/host-profile changes and live
data cutover remain separately reviewed operations.

Completion means: all seven apps work at the original HTTPS origin with Mac
Local Web services stopped; data is authoritative and backed up on NAS; existing
Mac CLI/agent workflows target it; one normal warm update and code rollback are
verified; post-write data rollback is rehearsed on copies; NAS tiles are separate
and correct; media-service isolation and private-ingress checks pass. Verify
normal rendered behavior and actual context/offline downloads where supported.
Physical-device access and a cold NAS restart require observed evidence when
performed, and are never inferred from container health or the earlier benchmark.

## References

- [Architecture](../architecture.md), [host profile](host-profile.md), [recovery](recovery.md).
- [Docker multi-platform builds](https://docs.docker.com/build/building/multi-platform/).
- [Docker Compose networking](https://docs.docker.com/compose/how-tos/networking/).
- [Tailscale container configuration](https://tailscale.com/docs/features/containers/docker/docker-params).
- [Tailscale machine names](https://tailscale.com/docs/concepts/machine-names).
