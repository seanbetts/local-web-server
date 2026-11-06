# Mac-built Local Web NAS Migration Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` for platform implementation, with focused independent review at the transport, data and cutover boundaries. Follow each repository's own development contract; the NAS administration repository explicitly excludes Superpowers workflows.

**Goal:** Run the seven Local Web apps and their authoritative data on NAS, preserve the existing HTTPS hostname, keep builds on Mac, and add a separate NAS services section.

**Architecture:** Native-platform builds on Mac produce immutable Linux/amd64-compatible releases and cached dependency artifacts. A narrow NAS adapter transfers missing bytes, materializes exact releases and manages a dedicated Compose runtime. Local Web's existing lifecycle commands remain the user/agent interface.

**Tech stack:** Existing Python CLI, React/Vite shared UI, Node/Python app runtimes, Docker/Compose, BuildKit on Mac, Caddy, Tailscale Serve, retained NAS SSH/Keychain authentication.

**Spec:** [Proposed migration design](nas-migration-design.md). Status: reviewable plan, not implementation or live-change approval.

## Global constraints

- Development and production builds stay on Mac; ordinary NAS publication runs no source builds or package installation.
- Preserve the existing Local Web HTTPS hostname and app paths. Real identity/path/URL values stay in private profiles.
- Keep the NAS's existing identity, media networks, service configuration and durable media outside Local Web's mutation scope.
- NAS is the only writable production store after cutover; local development uses fixtures/copies.
- Native-platform frontend builds; Linux/amd64 native runtime dependencies; exact committed source and digest-bound caches/releases.
- Preview/apply for activation and migration; no new confirmation per internal step of an already approved action.
- Public plans live under `docs/operations/`: the publication verifier excludes private process-artifact directories.

## Review focus

| Failure/input | Owning task and required proof |
| --- | --- |
| Cached dependency artifact has wrong ABI/base/architecture | Task 3 rejects it and rebuilds only the missing dependency artifact |
| A prior hashed asset was removed, or an upload is interrupted | Task 4 composes exactly the new manifest; current remains intact |
| An agent/CLI still writes to Mac state | Tasks 6–7 fence/refuse it before NAS writer activation |
| NAS has accepted writes before rollback | Tasks 6–7 preserve and repatriate latest state on copies, never reactivate stale Mac data |
| Hostname resolves to old node, wrong Host or unready certificate | Tasks 5/7 retain the failed public gate and safe writer fences |

## Ownership and interfaces

The public platform repository owns target models, release descriptors, build/cache
logic, Caddy/Compose rendering and app lifecycle dispatch. The existing NAS/media
repository owns its fixed SSH adapter and backup integration. Apps own runtime
entry points, domain data migrations and API/CLI adaptations.

New names below are proposed interfaces to implement, not currently available commands.

- `NasTarget`: adapter executable reference, canonical origin, runtime root and private network/proxy settings. Private profile schema **3** supports `target.kind = macos | nas-docker` and optional `services`, retaining readers for existing schemas 1/2. This lets the Index feature run on the Mac before NAS migration. Profile migration uses the existing revision/backup mechanism, never hand edits.
- `ExternalServiceLink`: `id`, `title`, `url`, `icon`, `accent`, held separately from managed `apps`. Public catalogue schema **2** adds `services`; serve it at `registry-v2.json` and keep the existing `registry-v1.json` unchanged. The new reader accepts schemas 1/2; publish its reference only with a verified compatible index bundle.
- `ReleaseDescriptorV1`: app/source/recipe/runtime/dependency identities, app-owned data-contract version and `rollbackPolicy = compatible | migration-required`, plus complete file manifest. `plan_transfer(descriptor, available) -> TransferPlan`; `TransferPlan` lists only missing verified blobs/images and total bytes. `compatible` requires app-owned fixture proof; it is not inferred from static health or manifest shape.
- `ContainerRuntimeSpec`: app-owned image/dependency recipe and argv using only `{port}`, `{state}`, `{publicOrigin}`, `{bindHost}`, `{proxyAddress}`. Code lives at `/app/current`, state at `/state`; no Mac executable or `{repository}` expansion. Existing Mac `startCommand` remains unchanged; missing NAS metadata blocks eligibility.
- `NasTransport.request(action, payload) -> result`: fixed actions `prepare`, `upload`, `activate`, `status`, `rollback`, `finish`. The configured local adapter consumes JSON, supplies retained authentication and invokes fixed NAS helper operations. No arbitrary host-command action.
- `NasDeploymentManager`: target-specific implementation behind existing deploy/status/rollback APIs; integrates existing app activation and fleet update without bypassing their source/provenance checks.

## Task 1 — Freeze the migration inventory and target contract

**Files:** modify `local_web_server/models.py`, `config.py`, `host_commands.py`, `host_profile_store.py`, `public_release.py` and relevant profile/public-example tests; add `local_web_server/nas_models.py`, `tests/test_nas_target.py`; update `docs/operations/host-profile.md`.

**Consumes:** current private profile, app manifests, deployment pointers and existing revision chain. **Produces:** validated `NasTarget` and a private per-app readiness inventory.

- [ ] Inspect each current app's actual durable paths, secrets, native dependencies, schema/version, writer entry points and backup procedure. Include travel app media and career app's source/runtime data distinction. Record private facts in the existing private task location.
- [ ] Add `test_schema3_macos_preserves_existing_origin`, `test_nas_target_rejects_lan_fallback` and schema-1/2 regression fixtures. Assert target dispatch and service links do not alter existing app identities/paths.
- [ ] Implement explicit profile migration preview/apply with backup/revision publication; do not apply it during development.
- [ ] Define per-app readiness: source build, Linux runtime, data copy/restore, origin/proxy trust, CLI/agent route and backup coverage. No app is eligible by frontend health alone.
- [ ] Run `python3 scripts/run_tests.py tests.test_nas_target tests.test_host_profile_store tests.test_config`; review the focused diff and commit.

**Gate:** agree on exact data/command inventory and target model before building the publisher. This is source/design review, not permission for live data changes.

## Task 2 — Ship the independent NAS services section

**Files:** modify `local_web_server/index_registry.py`, `config.py`, `models.py`, `system_index_bundle.py`; `apps/system-index/src/{registry.ts,SystemIndex.tsx,styles.css}`; their existing unit/E2E tests; `config/apps.example.json`; `docs/architecture.md`. Extend `local_web_server/icons.py`, `packages/ui/src/Icon.tsx` and icon tests only if the central semantic catalogue lacks a suitable service icon.

**Consumes:** `ExternalServiceLink` and current managed app catalogue. **Produces:** two labelled sections with independent collection semantics.

- [ ] Test schema-1 compatibility, schema-2 service links, invalid/credential-bearing/non-HTTPS URLs and duplicate IDs. `test_services_never_create_health_probes` must assert no probe requests for service URLs; registration and fleet tests must select only managed `apps`.
- [ ] Render **Local Web apps** and **NAS services** using existing tile primitives; retain managed-app status and omit service availability badges. Add an external-link cue, clear accessible names, responsive layout and empty-section behavior. Use section headings with tile titles at the next heading level.
- [ ] Resolve actual links from the canonical NAS guide during private configuration preparation: NAS Administration 8443, Jellyfin 8444, qBittorrent 8080, Prowlarr 9696, Radarr 7878, Sonarr 8989, slskd 5030. These ports are current documented inputs to recheck, not new listener assignments.
- [ ] Version the catalogue route/bundle coherently so an old open index does not receive an incompatible document. Build and validate the new bundle before publishing its catalogue reference.
- [ ] Run `python3 scripts/run_tests.py tests.test_index_registry tests.test_config`; `npm run check:index`; `npm run test:index:e2e`. Inspect light/dark desktop and 390/320 px layouts; verify all seven link destinations without exposing credentials.
- [ ] Commit the independently shippable feature. Its live installation is a separately approved platform operation and need not wait for migration.

## Task 3 — Add Mac production builds and persistent dependency caches

**Files:** add `local_web_server/production_build.py`, `release_manifest.py`, `templates/container-runtime/`; modify `git_build.py`, `release_composer.py`, `app_template.py`, `app_doctor.py`, `app_provenance.py`, `app_update.py`; add `tests/test_production_build.py`, `tests/test_release_manifest.py` and relevant template/update fixtures.

**Consumes:** exact app commit, app-owned production recipe and target ABI. **Produces:** `ReleaseDescriptorV1`, immutable app files and digest-bound Linux dependency artifacts/images.

- [ ] Add `test_source_only_change_reuses_dependency_digest` (zero installer/runtime-image replacement calls) and `test_abi_change_invalidates_dependency_cache`. Cover lockfile/base-image changes and secrets/state excluded from inputs.
- [ ] Run frontend/pure bundling on the native Mac build platform. Use isolated Linux/amd64 dependency stages on the Mac only on cache misses; persist BuildKit/package caches outside disposable source exports.
- [ ] Define app-owned container entry/dependency recipes through the manifest/provenance contract and generator. Keep `build.release` as the existing release composer. Do not infer recipes by parsing arbitrary source.
- [ ] Record source commit, recipe version and exact dependency/base-image digests; reject macOS binaries/venvs from Linux runtime artifacts. Verify the final target in an isolated Linux runtime.
- [ ] Re-run the two-app cold/warm acceptance only to verify the implemented cache path: warm source change must install zero dependencies and prepare zero replacement runtime images. Record build time, transfer plan bytes and cache hits.
- [ ] Run the focused new tests plus existing composer/build/template/update tests; commit. Do not perform production publication.

## Task 4 — Implement incremental, recoverable NAS publication

**Platform files:** add `local_web_server/nas_transport.py`, `nas_deploy.py`; modify `deploy.py`, `status.py`, `app_activation.py`, `fleet_live_release.py`, `fleet_update.py`; add `tests/test_nas_transport.py`, `tests/test_nas_deploy.py`.

**NAS repository files:** add `scripts/nas_local_web.py`, `scripts/nas_local_web_remote.py`, `tests/nas/test_nas_local_web.py`; update its NAS operating guide and relevant skill routes under that repository's contract.

**Consumes:** `ReleaseDescriptorV1` and `NasTarget`. **Produces:** `NasTransport`/`NasDeploymentManager`, exact current/previous descriptors and bounded recovery status.

- [ ] Implement fixed prepare/upload/activate/status/rollback/finish operations using existing retained NAS authentication; no new credential store, Docker socket API or generic command runner.
- [ ] Add `test_removed_asset_absent_from_new_release`, `test_interrupted_upload_keeps_current` and `test_lost_activation_ack_uses_status_without_replay`. Cover duplicate/traversal/link paths, checksum mismatch, missing dependencies, concurrent selected-app deployment and changed expected current release.
- [ ] Stage immutable objects and compose an exact candidate release. Upload failure or interruption cannot modify current. A retry transfers only still-missing verified bytes.
- [ ] Preserve a stable parent-directory binding for release pointers so switching host symlinks is observed inside containers; do not bind-mount a symlink target once and assume Docker follows later switches.
- [ ] Activate only the selected service/static release. Automatic public-health rollback requires a proven compatible app data contract. `test_startup_migration_requires_reviewed_recovery` must refuse `migration-required` through ordinary hooks/fleet apply and preserve fenced state if an incompatible mutation is detected. Restore prior code/configuration automatically only when data remains compatible; never silently restore an older database. Lost acknowledgements use original status without replay.
- [ ] Test garbage collection protects current/previous dependency artifacts, pending recovery and unrelated Docker resources. Finish consumes acknowledged operation records; unfinished outcomes remain recoverable.
- [ ] Run focused publisher/transport tests and synthetic transfer/recovery journeys. Independently review authentication, root execution, scoped cleanup and recovery before committing or live installation.

## Task 5 — Prepare the dedicated NAS runtime and hostname rehearsal

**Platform files:** add `templates/nas/compose.yaml`, `local_web_server/nas_render.py`, `local_web_server/nas_install.py`, `tests/test_nas_render.py`, `tests/test_nas_platform_installation.py`, `tests/test_nas_ingress.py`; modify `platform_installation.py`, `install.py`, `render.py`, `service_command.py` and `docs/operations/recovery.md`.

**App files:** each service's existing manifest, entry-point parser and origin/proxy tests. **NAS files:** dedicated deployment recipe and backup guide; `scripts/nas_backup_capture.py` and its tests.

**Consumes:** validated target and runtime recipes. **Produces:** generated Compose/ingress configuration and a privately reviewed hostname-handoff procedure.

- [ ] Add `test_only_caddy_loopback_listener_in_sidecar_namespace` and `test_untrusted_forwarded_proto_rejected`; cover zero published backend/host web ports, private backend bridge, read-only code and per-app state/secret bindings.
- [ ] Add explicit target-aware listen/proxy/origin settings to service contracts. Mac defaults remain loopback. NAS services accept forwarded scheme only from the configured proxy address, and retain origin/header/content-type protections.
- [ ] Verify actual installed Docker/Compose features, subnet availability, service UID permissions, resource limits and private storage before generating a live candidate. Start conservative and observe each app; avoid assuming all services fit from two measurements.
- [ ] Make platform installation target-aware: build System Index, theme and gallery on Mac, publish a verified platform release to NAS, and preserve platform rollback/health. NAS installation must not invoke launchctl or depend on Mac paths. Test theme/index failure preserves the prior platform release.
- [ ] Add essentials backup coverage for new data, configuration, secrets, Tailscale state and recovery records, plus a supported Mac host-profile/history backup export. Provide an app-data backup route for uploads/media outside the essentials size budget; document exclusions and verify restore on disposable copies.
- [ ] Prepare a concrete preview for a dedicated temporary Local Web Tailscale node. Inventory the current Mac identity and all its uses; define exact forward/reverse rename, Serve, grant and certificate checks without deleting that identity or altering existing NAS media access.
- [ ] Source tests precede the separate approval to install this shadow runtime. Rehearse valid HTTPS and hostname handoff with disposable identities/copies before final production naming.

## Task 6 — Adapt app data and Mac-side operations

**Files:** per-app manifests, production recipes, relevant entry points/CLI clients, domain tests, README and AGENTS guidance. Platform skill references under `skills/local-web-app-development/references/` describe the shared boundary; app skills remain app-owned.

**Consumes:** Task 1's private inventory and Tasks 3–5's target. **Produces:** seven independently eligible app releases plus app-owned copy/restore and writer-fencing procedures.

- [ ] Prepare static apps first, then routine app, ticket app, travel app, career app and finance app. Resolve blockers in an app's domain scope; do not weaken the platform contract.
- [ ] Use copies/synthetic state to prove records, ETags/revisions, schema compatibility, media/reference integrity and read/write behavior. Disable automatic legacy-state adoption on NAS.
- [ ] Route travel app's existing API CLI to the target. Update routine check-in, career app tracker and Finance refresh/read workflows to use supported app-owned NAS routes. Block implicit local production-file fallback; retain preview/apply and original recovery semantics.
- [ ] Separate source-controlled documents from mutable runtime records. Keep secrets and source repositories on their intended machines; give containers only required selected inputs.
- [ ] Rehearse consistent backup/copy and both rollback directions, including NAS writes made after cutover, on disposable stores. Never copy a live SQLite main file alone or permit simultaneous Mac/NAS writers.
- [ ] Run each app's Doctor, local check, E2E and composed app check against isolated fixtures. Verify desktop/mobile, themes, normal mutations and actual context/offline downloads where supported. Commit focused app changes, preserving unrelated work.

## Task 7 — Controlled live cutover and acceptance

**Files:** private host-profile revisions, generated target configuration and original cutover/recovery record; update `docs/operations/recovery.md` and NAS/app operating guides with final supported routes.

**Consumes:** eligible releases, tested restore procedures, verified backups and the concrete handoff preview. **Produces:** NAS authority at the original origin and verified recovery evidence.

- [ ] Obtain live approval for the concrete host/identity/data cutover after the reviewed preview. Suspend managed deployment hooks; fence all Mac app/CLI writers and stop only Local Web backends. Keep NAS source/code builds disabled.
- [ ] Take final consistent snapshots, transfer selected state, and independently verify revisions/schema/records/media. Start NAS backends against that state with production writes fenced until read-only acceptance passes.
- [ ] Perform the reviewed Tailscale name/grant/Serve handoff. Verify the exact original hostname, valid TLS, correct destination, allowed client access, Funnel absence and rejected direct HTTP/wrong Host. Unready naming/TLS leaves writes fenced and uses the reviewed rollback; never substitute a different hostname.
- [ ] Make NAS the sole writer and route Mac CLI/agent commands to it. Production write acceptance uses an explicitly approved legitimate operation; otherwise report isolated write proof separately from read-only live validation.
- [ ] Verify all seven apps at their original paths, both Index sections and service links, selected API/data readbacks and actual exports. Stop Mac Local Web services and demonstrate continued access. Obtain another enrolled-device observation; do not infer it.
- [ ] Demonstrate one normal warm Mac-built update and NAS code rollback. Verify transfer bytes/cache reuse, current/previous state and truthful health. Rehearse post-write data return on copies; a real rollback must use latest NAS state, never stale Mac originals.
- [ ] Restore existing approved main-branch deployment-hook policy pointing to NAS, plus fleet/status/rollback routing. Read back media service preservation against the exact pre-cutover snapshot and distinguish unrelated concurrent changes.

**Failure boundary:** before NAS writes, restore original hostname/configuration and untouched Mac data. After NAS writes, fence NAS, capture/return latest data through the app restore procedure, verify compatibility, then reactivate Mac authority. Code rollback alone does not roll data back.

## Task 8 — Standardize the ongoing workflow and retire the Mac runtime

**Files:** `skills/local-web-app-development/SKILL.md` and its references; `templates/react-app/AGENTS.md.template`, generated app README/manifest templates, `README.md`, `docs/architecture.md`, `docs/operations/{host-profile,recovery}.md`; corresponding generator/skill/template tests. Add `scripts/verify_nas_app_workflow.py` and `tests/test_nas_app_workflow.py` for disposable target acceptance.

- [ ] Update the standard create/init/update/doctor/check/activate/deploy/status/rollback/fleet workflows together. A new static and a new service app must work through those commands without hand-edited NAS paths or bespoke agent instructions.
- [ ] Prove native build/cache reuse, remote NAS unavailability, fleet partial failure, unsupported legacy app refusal and no automatic local deployment fallback.
- [ ] Update installed skill guidance through the existing installer, then existing app guides through focused app changes. Generated references must match maintained commands; source checks stay app-local.
- [ ] Verify ordinary NAS startup restores the stack without a Mac login; a full host restart is a separately approved operational test. Preserve Tailscale state and originals needed by the review period.
- [ ] After explicit acceptance, retire only obsolete Mac LaunchAgents/hooks/runtime outputs and redundant staging through supported scoped cleanup. Keep source repositories, local build caches, app-required backups and current rollback configuration.
- [ ] Run the platform's routine, relevant integration and release gates per CONTRIBUTING, plus `python3 scripts/verify_nas_app_workflow.py --kind static` and `--kind service` against disposable targets. Review documentation and the integrated diff; commit final workflow updates. Source integration, push and live installation remain distinct actions.

## Order, stopping points and execution

Task 2 is independently shippable. Recommended sequence: Task 1 contract, Task 2
Index, Tasks 3–4 publisher, Tasks 5–6 shadow runtime/app readiness, Task 7 live
cutover, Task 8 final standardization. Prepare Task 8's generator/skill changes
alongside Tasks 3–6; its final acceptance/retirement follows live verification.

Plan approval authorizes no live effects by itself. Implementation should proceed
in these milestones with source-level review, then a concrete cutover preview.
Recommended execution is lead-owned implementation with focused independent
review for transport, data handling and ingress/cutover, following repository rules.
