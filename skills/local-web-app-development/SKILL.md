---
name: local-web-app-development
description: Use when creating a new local-web app, developing an existing local-web app, diagnosing compatibility or tooling failures, verifying an app, or activating an eligible app.
---

# Local Web App Development

## Core principle

Invoke the platform commands; do not reproduce their templates or verification implementation. The platform owns the foundation and shared shell. Each app owns its domain code and app-local checks.

Read [references/app-contract.md](references/app-contract.md) before changing an existing app or its manifest. Read [references/verification.md](references/verification.md) before diagnosing, verifying, or activating an app.
Read [references/service-apps.md](references/service-apps.md) when creating, converting, checking, or activating a service app.

## Resolve the CLI

Resolve and verify one command before following any route:

```sh
if command -v local-web >/dev/null 2>&1; then
  LOCAL_WEB="$(command -v local-web)"
else
  LOCAL_WEB="$(python3 -c 'from pathlib import Path; skill = (Path.home() / ".codex/skills/local-web-app-development").resolve(strict=True); command = (skill.parents[1] / "bin/local-web").resolve(strict=True); assert command.is_file(); print(command)')"
fi
"$LOCAL_WEB" --help >/dev/null
```

When the PATH lookup fails, use only this installed-skill fallback; do not search for another checkout or infer a repository path.

## Route the intent

| Intent | Route |
| --- | --- |
| Empty opened folder | Run `"$LOCAL_WEB" app identity --search "<subject and synonyms>"`; choose the most specific meaningful non-reserved icon and an intentional approved accent; run `"$LOCAL_WEB" app init . --title "<title>" --icon <icon> --accent '<accent>'`; then run `"$LOCAL_WEB" app doctor --repository .`, `"$LOCAL_WEB" app activate --repository .`, and `"$LOCAL_WEB" app activate --repository . --apply`. This publishes the generated placeholder and its System Index tile before creative app-specific brainstorming. Domain implementation still waits for design approval. |
| Existing app | Read `AGENTS.md` and `local-web.json`, then run the structural doctor. If it reports a legacy or updateable platform diagnostic, follow the matching existing-app route in [references/app-contract.md](references/app-contract.md), including the explicit foundation-adoption route for a service with no root React/Vite foundation. For an intentional existing-service command conflict or public-base-path migration, follow [references/service-apps.md](references/service-apps.md). The latter is a separately approved live migration for an already registered service; preserve domain structure and keep source integration app-owned. |
| Routine shared platform/UI refresh for the registered fleet | From the platform repository, run `"$LOCAL_WEB" apps update --dry-run`, review every final app status, planned platform paths, source commit and deployed commit, then obtain separate explicit approval before `"$LOCAL_WEB" apps update --apply`. It creates focused app commits, checks and activates only compatible registered apps. A legacy/foundation/domain migration is `SKIPPED`: use the single-app route instead. |
| Diagnose | Run the structural doctor first. Use the app's standard TypeScript, ESLint, test, and Vite tooling for source-level diagnosis. |
| Verify | Run the complete matrix in [references/verification.md](references/verification.md). |
| Activate | After the app is eligible and every required check passes, preview with `"$LOCAL_WEB" app activate --repository .`, then apply with `"$LOCAL_WEB" app activate --repository . --apply`. Do not hand-edit the host registry or choose a service port. |

For a new app outside an already-opened empty folder, use the same identity
search first, then run `"$LOCAL_WEB" app create <app-id> --title "<title>"
--icon <icon> --accent '<accent>'`. Enter the created repository, run Doctor,
preview activation, and apply activation before beginning domain design.

Treat identity selection as design work, not a mechanical default. Search the
app's subject plus close synonyms, inspect current assignments, choose the most
specific semantic icon, and select an approved accent that fits the app while
remaining distinguishable from nearby tiles. Never use the reserved `apps`
icon or a generic substitute. If no suitable semantic icon exists, stop and
extend the central identity catalogue rather than disguising the gap.

Supply route or capabilities only when the user or app design requires them.
Static is the default. Use explicit `--kind service` only when the approved architecture needs a local backend.

## Exports

Generated apps own `buildContextExport()` as domain code. Keep it useful when a
feature changes; follow the ownership rules in
[references/app-contract.md](references/app-contract.md) and prove the app's
records and download through [references/verification.md](references/verification.md).

Interactive HTML export is a separate optional capability. The framework owns
packaging and offline shell behavior; the app owns one coherent capture, a
shared decoder, snapshot data, view state, its offline entry, and adaptations
for backend-dependent actions. Read the exact contract in
[references/app-contract.md](references/app-contract.md) before wiring it.

## Boundaries

- Host ingress is platform-owned. Schema version 1 retains trusted-LAN HTTP; version 2 uses `tailscale-serve` and the canonical HTTPS `publicOrigin`, with externally managed Tailscale Serve proxying to loopback-only Caddy. App work never edits Tailscale, host ingress, Caddy, or LaunchAgents. Funnel and a LAN fallback are unsupported.
- Origin-bearing service commands use the existing `app migrate-service-command` preview and separately approved live apply; see [references/service-apps.md](references/service-apps.md). App activation, deployment, rollback and fleet updates never select or migrate host ingress.
- A schema version 2 HTTPS failure is a platform ingress blocker. Preserve the failed public health gate; never bypass it with direct registry, Caddy or LaunchAgent edits, direct HTTP probes, or disabled TLS verification. Host-registry migration and installer apply require separate explicit live approval; implementation or passing local tests does not establish activation.
- Keep `npm run check` app-local. Never make it invoke `local-web`.
- For foundation adoption, never create placeholder `package.json` or `index.html` files, use copied templates or manual provenance, edit the host registry, or activate before the app owns a coherent release.
- Keep the structural doctor structural; do not extend it to parse arbitrary TypeScript, TSX, CSS, or Vite source.
- Prefer standard TypeScript and ESLint tooling. Add no custom parser or plugin without a demonstrated app-specific need.
- Keep secrets in ignored local environment files and never print private values.
- If a legitimate architecture cannot be expressed by the manifest or platform commands, treat it as a platform capability blocker. Do not weaken the app, bypass doctor, or claim full integration; report the missing capability at the platform boundary.
- Keep trusted-local apps simple. Add deep adversarial safeguards only for a demonstrated threat model.
- Do not use `apps update` for legacy adoption, foundation adoption, capability changes, domain migration or an app-specific repair. Those remain deliberate single-app work.
