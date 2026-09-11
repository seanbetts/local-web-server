# Local Web recovery runbook

This runbook separates private host-profile recovery, generated platform
installation, application release rollback, app-owned data recovery, and
externally managed ingress. Do not treat success in one layer as proof that the
others are healthy.

Use placeholders such as `/Users/example/Coding/local-web-server` and
`https://local.example.ts.net` in documentation and fixtures. On a real host,
derive paths, routes, service labels, and the public origin from the validated
private profile; do not copy example values into live state.

## Start with read-only status

From the framework repository:

```sh
bin/local-web host status
bin/local-web status
```

`host status` reports profile/revision metadata without registry contents.
Ordinary status checks the selected public origin and, for services, the direct
loopback health path. A loopback success with a failed public check is an
ingress failure, not an acceptable substitute.

Inspect exact managed LaunchAgent labels and log paths from generated state.
Avoid broad process-kill or file-removal commands. Application response bodies,
environment values, and database contents do not belong in diagnostic output.

## Recover an interrupted host-profile transaction

Preview first:

```sh
bin/local-web host recover
```

If the preview reports one of the documented deterministic actions, review it
and run the apply only with explicit authority:

```sh
bin/local-web host recover --apply
```

Unknown or modified state fails closed. Preserve the transaction and private
`.host-profile-recovery` residue for investigation. Do not edit the profile,
revision chain, marker, temporary files, or retained residue by hand. The
[host-profile guide](host-profile.md) defines the exact three recovery actions
and the retained-residue capacity boundary.

## Restore a missing or damaged profile

Choose a trusted `local-web-host-backup/v1` document and validate it through a
non-mutating preview:

```sh
bin/local-web host restore --from /path/to/local-web-host-backup.json
```

For an absent profile, rerun with `--apply` only after reviewing the preview.
Replacing a valid existing profile additionally requires `--replace` in both
preview and apply. Replacement first creates a current-state backup and appends
an explicit restoration revision; it never rewrites history.

If no backup exists but a prepared registry is authoritative, use
`local-web host init --from ...` only for an empty store. Do not fall back to the
public example registry.

## Preview platform installation

After profile health is established, validate generated platform output without
writing installed state:

```sh
python3 scripts/install_local_web.py --dry-run
```

Review the exact would-change summary and obtain separate approval. Then apply
with the plain installer invocation:

```sh
python3 scripts/install_local_web.py
```

There is no `--apply` flag: the plain invocation is the separate live operation.
It generates the Caddyfile, platform assets, LaunchAgents, and managed hooks;
none should be hand-edited. Validate the generated Caddyfile with the installed
Caddy 2 binary before loading it.

For a clean schema-version-1 rebuild, Caddy is a user LaunchAgent and binds the
trusted-LAN listener. For schema version 2, installation requires an already
managed Caddy and working canonical HTTPS preflight. A cold schema-version-2
rebuild therefore needs a separately reviewed ingress bootstrap; do not reopen
the LAN listener as an improvised fallback.

## Ingress diagnosis

### Trusted LAN

Schema version 1 uses plain HTTP and has no authentication or TLS. Probe exact
routes without bodies, using the validated host value. A failed route remains a
platform or app issue even if a direct release file exists.

### Tailscale Serve

Schema version 2 requires all of these independent facts:

- the exact canonical `https://<device>.<tailnet>.ts.net` origin has valid TLS;
- externally managed Serve persistently targets `http://127.0.0.1:8080`;
- Funnel is absent;
- the reviewed device/grant policy permits intended clients; and
- the managed Caddy PID listens only on the expected loopback application and
  admin sockets.

Read back Tailscale status, Serve status, Funnel status, the exact managed
LaunchAgent, and listeners. Local Web does not change Tailscale. A wrong Host on
loopback must be rejected. Direct LAN-IP HTTP, direct Tailscale-IP HTTP, and a
noncanonical host must not serve apps. Do not disable TLS verification or follow
redirects to make a health check pass.

Moving an existing version-1 host to loopback port 8080 is a separate three-step
operator migration: preview/apply a temporary dual-listener Caddy, separately
switch and read back Serve, then preview/apply normal installation to remove
port 80. Each apply has its own approval and recovery check. The temporary state
still exposes the LAN and is not the final security posture.

## Roll back an application release

Application releases are immutable. `current` identifies the active release and
`previous` retains the immediately preceding successful release. Do not move
these links or edit release contents manually.

Preview application status, then roll back one exact app when a valid previous
release exists:

```sh
bin/local-web rollback example-app
```

For a service, rollback restores the pointer pair, restarts the managed process,
and re-runs internal and public health gates. A failed rollback restores the
prior release/service state. Code rollback does not roll back a service database
or other app-owned durable data.

## Recover a failed deployment or activation

Read the bounded deployment log and fix the source, dependency, build, or health
failure in the app repository. Re-run the app's structural and local checks,
then preview the supported action:

```sh
bin/local-web app doctor --repository /Users/example/Coding/example-app
bin/local-web app check --repository /Users/example/Coding/example-app
bin/local-web app activate --repository /Users/example/Coding/example-app
```

Activation apply is separate. If a profile revision was already published
before installation or deployment failed, keep it as the recovery checkpoint;
a correct retry creates no duplicate revision. Intentional service-command and
public-base-path changes use their dedicated migration previews and applies,
not ordinary activation or profile editing.

## Full-machine restoration

Restore these inputs from trusted backups:

1. the framework repository and every app repository at the paths named by the
   private profile;
2. the complete ignored `config/local/` directory, including history and host
   backups;
3. app-owned environment files, databases, uploads, and other durable data; and
4. the externally managed ingress configuration and device policy.

The generated runtime is disposable and should be rebuilt. After source and
private data are present:

1. verify Git status and tool versions;
2. run the framework and app test suites;
3. run `local-web host status` and validate a fresh host backup;
4. run `python3 scripts/install_local_web.py --dry-run`;
5. obtain separate approval before installer apply;
6. run `python3 scripts/install_local_web.py` to apply the reviewed installation;
7. bootstrap only the exact generated user LaunchAgents required by the chosen
   ingress mode;
8. deploy or activate apps through supported commands; and
9. read back profile digest/revision, Caddy listeners, public routes, service
   health, release pointers, and app-owned data health.

Manual acceptance from a second intended device is required for a network
reachability claim. For `tailscale-serve`, also prove that access fails with
Tailscale disconnected and that direct HTTP paths remain unavailable. Automated
tests and same-host loopback probes do not establish that evidence.
