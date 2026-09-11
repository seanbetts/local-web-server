# Private host-profile operations

The host profile is the repository-local, ignored control plane for one Local
Web installation. Normal CLI and installer entrypoints resolve only
`config/local/apps.json`; they never fall back to `config/apps.example.json` or
the former tracked registry.

## Files and permissions

```text
config/local/
  apps.json
  history/
  backups/
  .host-profile-transaction.json       # only while incomplete
  .host-profile-recovery/               # retained audit residue, when present
  publication-policy.json               # optional local publication policy
```

Private directories use mode `0700`. The profile, revisions, transaction files,
backups, checksum/policy files, and fixed framework temporaries use `0600`.
Symlinks, permissive modes, ambiguous filesystem identities, unexpected files,
or state outside the canonical repository fail closed. Ordinary commands do not
accept an environment-variable override for the profile.

## Command contract

All state-changing lifecycle operations except backup are preview-first. Run a
preview, review its bounded labels and digests, then rerun with `--apply`.

### Initialize from a prepared registry

```sh
bin/local-web host init --from /path/to/prepared-registry.json
bin/local-web host init --from /path/to/prepared-registry.json --apply
```

The source must be a bounded, mode-`0600`, regular single-link file reached
without symlinks. Initialization requires an absent profile, empty history, and
no transaction. Apply validates the registry, writes the exact profile bytes,
creates the initial revision, and creates a verified backup. If only that final
backup step fails, the valid initialized store is retained and the command
reports failure; run `local-web host backup` explicitly. Initialization itself
is not idempotent.

### Migrate a legacy tracked registry

```sh
bin/local-web host migrate-registry
bin/local-web host migrate-registry --apply
```

The one-time migration reads exactly `config/apps.json`, validates without
editing it, and creates a byte-identical private profile, initial revision, and
backup. Only this command accepts `--platform-repository PATH`, for a reviewed
transition executed by another framework checkout. An exact-equal retry is
idempotent. If an earlier migration initialized the profile but missed its
backup, the retry creates/revalidates the backup without another revision.

### Inspect state

```sh
bin/local-web host status
bin/local-web host status --json
```

Status reports only the resolved profile path, registry schema, profile digest,
tip revision ID, revision count, latest managed backup timestamp, transaction
state, and legacy-file presence. It never prints registry fields, app IDs,
origins, repositories, runtime paths, ports, commands, or policy values.

### Create a backup

```sh
bin/local-web host backup
bin/local-web host backup --output /safe/new/local-web-host-backup.json
```

Backup is an intentional write and therefore has no `--apply` flag. The default
filename is `local-web-host-YYYYMMDDTHHMMSS.ffffffZ.json` beneath `backups/`.
Publication is no-clobber and mode `0600`. An explicit destination must be a new
safe regular-file name in an existing safe parent.

The canonical `local-web-host-backup/v1` JSON document contains base64-encoded
exact profile and revision-envelope bytes, their fixed filenames, SHA-256
digests, and creation time. It does not contain environment-file contents,
databases, app data, logs, runtime releases, or a private Git bundle. Parsing is
bounded to a 64 MiB document, a 5 MiB profile, 8 MiB per revision envelope, and
10,000 revisions. Integrity digests do not authenticate or encrypt a backup.

### Restore

```sh
bin/local-web host restore --from /path/to/local-web-host-backup.json
bin/local-web host restore --from /path/to/local-web-host-backup.json --apply
```

The complete document, digests, registry, revision envelopes, chain, and profile
tip are validated before preview. Restoring into an absent store publishes the
backup's exact chain and profile. An existing profile is refused unless preview
uses `--replace` and apply uses both `--replace --apply`:

```sh
bin/local-web host restore --from /path/to/backup.json --replace
bin/local-web host restore --from /path/to/backup.json --replace --apply
```

Replacement first creates and revalidates a backup of current state, then
appends one `backup-restoration` revision to the current immutable chain. It
does not replace or rewrite history. A candidate whose profile bytes already
match is a no-op.

### Recover an interrupted transaction

```sh
bin/local-web host recover
bin/local-web host recover --apply
```

Recovery inspects exact marker, profile, planned revision, chain, and candidate
identities. It permits only three deterministic actions:

1. remove an untouched transaction whose previous profile remains current and
   whose planned revision is absent;
2. publish the candidate when the previous profile remains current and the
   complete planned revision exists; or
3. clear the marker when profile and revision already match the candidate.

Any mismatch, external modification, non-prefix restore state, or unknown file
is preserved for investigation and fails closed.

## Revision and transaction semantics

Each `local-web-host-revision/v1` envelope contains a fixed operation type,
optional validated app ID, UTC timestamp, previous/current profile digests, and
the exact current registry bytes. Its revision ID is the SHA-256 of the complete
canonical envelope. A no-op adds no revision; an explicit restoration remains a
new auditable event even when its profile digest matches an older state.

The mode-`0600` transaction marker binds exact previous/candidate bytes and the
planned revision. It is removed only after chain tip and profile both match the
candidate. Downstream installation or deployment failure does not erase a
successful profile revision; retry consumes that checkpoint without duplicating
it.

## Retained recovery residue

Safe recovery cannot conditionally unlink a pathname by expected inode on the
supported macOS interface. Local Web therefore moves exact recovery candidates
without clobbering into the private `.host-profile-recovery` audit area and
retains them. The transaction marker moves last as the recovery commit point.

This residue is private, is excluded from revisions and backups, and is bounded
to **128 batches** and **256 MiB** in total. There is **no automatic pruning**.
Capacity exhaustion, a collision, an unknown batch, or mismatched residue fails
closed and preserves the pending state. Deliberate operator handling is required
after investigation; do not delete or rewrite residue merely to make recovery
continue.

## Machine backup policy

Back up the entire ignored `config/local/` directory with the framework and app
repositories. Also back up each service app's durable data and environment files
under that app's own procedure. If copying host backups to an untrusted
destination, use encrypted storage. Validate a restored document through
`local-web host restore` preview before relying on it.
