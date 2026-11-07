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

## Publication and status

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

## Data and hostname boundaries

Ordinary deployment refuses a changed data contract or `migration-required`
recipe. Code rollback never restores a database, JSON state, uploaded media or
secrets. After production writes have moved to the NAS, returning to Mac service
requires the app's fenced latest-state copy/restore procedure.

This lifecycle does not rename Tailscale nodes, change grants, enable Funnel,
adopt legacy state or authorize a second writer. Follow the concrete migration
preview for those separately reviewed effects.
