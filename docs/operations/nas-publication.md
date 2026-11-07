# Mac-built NAS publication

The NAS lifecycle is a source implementation. Installing its private runtime,
changing the host profile, copying production state and handing over the public
hostname remain separately reviewed operations in the migration plan. The
uninstalled private adapter deliberately has no production runtime callbacks.

## Source checks

App-local development stays on the Mac. Check the explicit production recipe
without selecting or modifying a host profile:

```sh
local-web app doctor --target nas-docker --repository .
local-web app check --target nas-docker --repository .
```

These commands verify source compatibility and the app's local check/browser
matrix. They do not establish Linux startup, data compatibility, installed NAS
health or a completed migration.

Platform installation apply requires clean committed framework `main`, for both
Mac and NAS targets. Pure preparation and dry-run previews can inspect committed
source branches without publishing them.

## Publication and status

NAS service host entries require a port; their runtime command comes from the
app-owned production recipe. A Mac `startCommand` is optional and inert on this
target. Registration preserves an existing Mac command for later recovery, while
new NAS registrations do not resolve Mac executables.

The configured host profile chooses the lifecycle. A `nas-docker` profile routes
ordinary `deploy`, `rollback` and `status` to its fixed NAS adapter. It never
interprets `runtimeRoot` as a Mac serving directory or falls back to launchd.
Managed apps and external NAS service links remain separate collections.

Production preparation exports an exact main commit. Frontends build natively
on the Mac; keyed native dependencies are restored from a persistent cache.
Linux dependency images build on the Mac only on a cache miss, through the
explicit local Unix-socket Docker context named by `target.buildContext`.
Ordinary publication runs no package installer or source build on the NAS.

The publisher transfers checksum-bound missing app blobs and required runtime
archives. Complete immutable descriptors determine release contents, so removed
assets do not leak from an earlier release. Runtime artifacts bind Linux/amd64,
ABI, locked inputs and both supported Docker image identity forms.

Status reads remote current/previous descriptors, fresh internal readiness and
the canonical public HTTPS routes. Historical journal health is not fresh
readiness. Unknown observations remain unhealthy in the CLI.

## Original-operation recovery

A private Mac receipt is durable before preparing the remote operation and
before requesting a serving transition. The NAS also fences each app to its
original operation. A new publication cannot bypass an unresolved receipt.
Receipts bind the original profile authority. A fresh process using changed
host settings cannot resume its mutations or complete its receipt; retain the
original files and reconcile that authority first. Activation errors preserve
the bounded operation identifier and supported recovery command.

When publication reports an operation requiring reconciliation, use that exact
identifier:

```sh
local-web deploy example --recover ORIGINAL_OPERATION_ID
```

The same route recovers an interrupted rollback; `local-web rollback example
--recover ORIGINAL_OPERATION_ID` is also accepted. Recovery reuses the prepared
source and original upload selection. Its deterministic bundle permits verified
staged bytes to be reused after partial consumption. It does not rebuild the
app, select a newer main commit or replay an uncertain activation.

Fresh internal readiness and a public HTTPS gate must pass before finish
releases the app fence. Public failure can restore the previous code under the
same operation only when both releases have the same explicitly compatible data
contract. Successful recovery of old code still reports the requested change as
failed. Unknown or unhealthy recovery remains fenced for investigation.

Receipts and prepared code live in the Mac's private Local Web application
support directory, separately from the NAS runtime root. Do not remove them to
bypass recovery. The NAS retains current/previous releases and their dependency
artifacts; cleanup never invokes a global Docker prune.

Before a NAS client transition, export a private recovery artifact:

```sh
local-web host recovery-backup --output /private/path/new-client-recovery.tar
local-web host recovery-verify --from /private/path/new-client-recovery.tar
```

The existing destination must not exist. The export includes the supported
profile/history backup, installation context, operation receipts and the exact
prepared bytes needed by pending operations. Completed app code and rebuildable
caches are excluded. Capture refuses busy or changing client state and verifies
the new artifact before reporting success. Verification uses a disposable local
directory and needs neither the current host profile nor NAS access; it performs
no restore. The artifact contains private paths and recovery metadata, has mode
0600 and a 2 GiB payload limit, and belongs in private backup storage. Preserve
its original path mappings for a separately reviewed recovery. It does not back
up NAS app data, secrets or Tailscale state; those need NAS backup coverage.

## Data and hostname boundaries

Ordinary deployment refuses a changed data contract or `migration-required`
recipe. Code rollback never restores a database, JSON state, uploaded media or
secrets. After production writes have moved to the NAS, returning to Mac service
requires the app's fenced latest-state copy/restore procedure.

Prepared `host configure` profiles may pause or restore managed apps’
`autoDeploy` flags while preserving all app identities and execution settings.
The operation retains the previous profile and requires the exact preview
revision and candidate digest.

NAS ingress defaults to a write fence (`target.writesEnabled: false`): GET,
HEAD and OPTIONS remain available, while other methods receive an uncached 503
before reaching the app. Read-only production acceptance must finish before an
explicit reviewed configuration enables writes. Disposable shadow fixtures may
use their own explicitly enabled profile.

This lifecycle does not rename Tailscale nodes, change grants, enable Funnel,
adopt legacy state or authorize a second writer. Follow the concrete migration
preview for those separately reviewed effects.

## Prepared NAS platform installation

`prepare_nas_platform` builds only the System Index and UI gallery on the Mac,
loads the committed theme, and returns one immutable `local-web-platform`
release plus a proposed directory/ownership plan. It never starts a NAS service.
`preview_nas_installation` wraps these exact bytes in a versioned approval
capsule: platform/config digests, expected current and previous config digest,
private profile revision, permission-plan digest, prerequisite evidence digest,
and optional full exact selected app descriptor. The capsule digest is the review/apply
identity. Missing Docker-feature, subnet, private-storage, permission, backup,
host-profile-export or hostname-inventory evidence is listed as missing; source
tests and generated files do not verify those prerequisites.

`apply_nas_installation` accepts that exact approved capsule digest and an
explicitly injected publication manager. `NasPlatformInstallationSession` adds
a separate durable reserved-platform operation receipt, exact original files
and capsule, and original upload selection. Recovery checks original status,
resumes a still-prepared upload, or reconciles an uncertain activation without
replaying it. An installer invocation that recovers a pending platform operation
returns that original result without starting another publication. Platform
failure does not create a selected app deployment receipt. These library APIs
are not a privileged bootstrap command. The private
adapter's production callbacks remain disabled by default.

The existing six-action protocol carries an optional `installationApproval`
only on reserved-platform `prepare`. The private engine saves it under the
original platform fence and permits only its exact retry. Its separately
configured approval callback validates the original fence, expected current,
materialized descriptor and complete fixed config bytes before writing an
immutable config-digest-keyed approval. It starts no services and switches no
serving pointer. Prior approvals are retained so code rollback can authorize the
previous configuration; ordinary publication cannot rewrite a mutable root
approval marker. A reviewed baseline marker authorizes only its exact config.

After an approved bootstrap has seeded the private Mac client context,
`install_platform(..., selected_candidate=prepared_app)` dispatches a NAS target
before any Mac installer/LaunchAgent/skill route. The default refresh uses the
saved pinned frontdoor images and observed prerequisite identities. For every
unselected app it loads the manifest at the actual remote descriptor's source
commit and resolves runtime metadata only from retained versions whose exact
`.local-web-runtime.json` bytes match that descriptor. It never builds apps or
adopts their unselected main-branch recipes. The selected prepared app replaces
only its own runtime slot. Unknown metadata/configuration or an absent bootstrap
context stops with a preparation error. A healthy confirmed platform publication
saves the next context, retaining prior and candidate runtime versions for app
rollback. The context and platform operation artifacts belong in the private Mac
client backup alongside the supported `local-web host backup` profile/history
export; they contain metadata and secret names, not secret values.

Ordinary refresh rejects changes to an existing app's runtime user, selected
secret names, port, CPU/memory ceilings, health runtime, route or health/proxy
policy. Such changes need their own reviewed migration and recovery policy.
Source/image/command changes with unchanged policy retain old app runtime
compatibility under the new platform configuration. App rollback never blindly
rolls the whole platform back over other apps' changes.

The preparation report includes essentials coverage for dedicated Tailscale
identity/state, selected secrets, versioned approvals, operation receipts and
release descriptors/generation pointers; a consistent app backup/restore route
must cover authoritative state, uploads/media and reference integrity. Verified
code/image/build caches are rebuildable exclusions. This is a coverage plan,
not a claim that the NAS backup selection has been applied or restore tested.
The hostname procedure uses three explicit distinct names: current production
Mac, retained Mac and temporary dedicated NAS. It preserves existing identities
and NAS media access, rehearses TLS/grants/Serve/no-Funnel on the temporary node,
and specifies forward/reverse renames and public health checks. Live installation,
backup changes, identity handoff and writer cutover remain separately approved.

The private bootstrap source route is `nas_local_web_install.py` with fixed
preview/apply/status/input-stage, retained authentication, an original pre-root journal and
immutable installed source bundle. It requires exact preprovisioned dedicated
Tailscale state/secrets and externally reviewed prerequisite observations; it
performs no Tailnet login or service start. Its only success claim is
`installed-configured`. The reviewed host-profile adapter command must explicitly
select `nas_local_web.py --installed` to enable the verified installed callbacks.
Initial public preparation seeds `SavedNasInstallationContext` through the
reserved-platform session's `next_context`; missing context never falls back to
Mac service installation. Preview inspects exact pinned Caddy/Tailscale image cache state. Initial bootstrap
may add only explicitly reviewed missing official vendor references by digest
with `--platform linux/amd64`; it records and reconciles the original import
intent. Ordinary publication retains `--pull never`.


The bootstrap default preflight is a fixed live read-only observation producer,
not a set of caller assertions: it checks Docker/Compose features, selected
project/subnet conflicts, exact volume/storage/UID identities and capacity.
Stable facts are digest-bound; memory/free-space checks use reviewed minima,
while the full observation snapshot gets an audit digest. Its explicit input
stage validates only declared private local files and SCPs missing bytes to the
fixed original-operation directory. It never enrolls a Tailscale identity.

A standard generated none-data service with user `10001:10001` and no secrets
can use ordinary activation after bootstrap. Its full prepared descriptor and
exact permission plan authorize only its missing empty state directory under
the separate platform operation fence. Authoritative data apps and policy or
contract changes still require their explicit migration route. Existing state
is preserved; unknown directories are not adopted.

The platform session checks the same profile authority before remote mutations,
transition markers, health/finish and saved-context publication. If authority
changes after prepare, its original receipt remains and recovery may observe
status but cannot issue another mutation under the stale profile. A completed
healthy rollback is reported as failure of the requested platform change, so app
publication stops. The maintained installer entry point formats NAS preview and
publication results directly; it does not expect Mac installer/skill results.


The public source contract for a reviewed temporary-to-canonical origin handoff
is `nas_origin_transition.py`. It does not rename nodes, apply profiles, change
grants, admit writers, start services or move data. Actual private orchestration
and production approval remain separate. Bootstrap provenance remains immutable.

`preview_nas_origin_transition` freezes the exact source/destination profile
hashes, installed marker hash, current platform/configuration observations,
prepared destination platform, unchanged application descriptors/runtime metadata,
and original/destination client directories. Both directions use the recorded
NAS temporary/production name pair within the same Tailnet suffix. The retained
Mac name is excluded. Target adapter, storage, network, image pins, application
registration and write policy cannot change; both profiles keep writes disabled.

`NasOriginTransitionSession.begin` persists the approved original plan. `apply`
checks current authority through an injected callback and records remote intent
before invoking the private transition callback once. Any lost acknowledgement
requires `status` with the same original operation. Status uses the frozen plan
and recovery directory and does not validate the active profile or invoke the
transition callback. The private driver must separately journal each uncertain
runtime effect and provide fresh destination HTTPS/application checks.

`promote` accepts only an exact healthy `origin-ready` observation matching the
original operation, plan, destination origin, platform/configuration digests and
complete application digest mapping. It atomically saves the next context in the
historical directory and publishes a direct destination namespace binding.
Receipts are never copied or relabelled. Interrupted local promotion can finish
with the original observation and receipt; it cannot replay runtime activation.
Normal namespace resolution refuses pending transitions, alias chains, foreign
collisions, changed baseline metadata and stale origins. Subsequent ordinary
context updates may only extend immutable runtime/platform history. Repeated
forward/reverse transitions compare the exact previously completed binding before
replacement; reversing retains every latest observed runtime/context record.

An origin reversal preserves NAS data. Returning Mac writer authority after NAS
writes still requires the independently tested latest-data transfer/restore
route. Configuration rollback alone cannot provide that guarantee.

The NAS client recovery backup includes original transition receipts, the
pending selector and required empty lock files. Selected destination bindings
are stored under `client/origin-namespaces/<destination-path-sha256>/` and
validated against their original plans. Verification reads only disposable
archive copies; it never follows archived absolute paths. A busy transition,
foreign binding or missing completed namespace refuses the backup. This remains
an artifact/verification route, with no live restore or hostname effect.

The private helper's fixed
source-upgrade preview/apply/status route can install later reviewed helper
code: it binds exact old/new three-file bundles, retains the old bundle, refuses
pending operations, and switches the verified marker atomically under the
installation gate. It preserves target, configuration approvals, identity and
state policy, and runs no service or Docker operation. This source-maintenance
route does not perform hostname handoff. First live bootstrap still requires
real prerequisite evidence and separate operational approval; source checks
alone do not establish readiness.

## Staging before a data or origin handoff

Installation-owned cutover tools may call `NasDeploymentManager.stage()` to
prepare and upload the exact release under its original operation without
activation, public health checks or finish. Keep the ordinary durable local
receipt, frozen inputs and upload selection. After the separately fenced data
copy, `activate_staged()` requests the explicit first activation with the usual
transition, public HTTPS and finish gates. It is not a recovery shortcut:
interrupted activation uses original-operation status reconciliation.
