# Private host state

This directory belongs to one Local Web installation. Everything except this
file and `.gitignore` is intentionally ignored by Git. Never force-add its
contents to a commit, release, issue, or diagnostic attachment.

The framework may keep these private artefacts here:

- `apps.json`: the active host registry;
- `history/`: immutable host-profile revision envelopes;
- `.host-profile-transaction.json` and framework-owned temporary files used by
  atomic publication and recovery;
- `.host-profile-recovery/`: retained recovery audit residue;
- `backups/`: `local-web-host-*.json` backups and, when an operator creates
  them, a private Git `.bundle` and matching `.sha256` checksum; and
- `publication-policy.json`: an optional local exact-string deny policy for the
  publication verifier.

The directory, including registry history and backups, must travel with the
machine backups used to migrate or rebuild the host. It is not application-data
backup: each service app remains responsible for its own databases, uploads,
environment files, and other durable data.

Private directories must have mode `0700`; private files must have mode `0600`.
The framework rejects symlinks and permissive state. Backups are not encrypted,
so use encrypted storage when copying them outside the machine.

Create or recover the profile only through the supported preview-first commands:

```sh
bin/local-web host init --from /path/to/prepared-registry.json
bin/local-web host restore --from /path/to/local-web-host-backup.json
```

Review the preview before adding `--apply`. See
[`docs/operations/host-profile.md`](../../docs/operations/host-profile.md) for
the complete lifecycle and recovery contract.
