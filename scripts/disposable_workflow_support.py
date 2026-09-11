"""Shared private-state and subprocess boundaries for disposable workflows."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_backup import (
    MAX_BACKUP_BYTES,
    MAX_PROFILE_BYTES,
    MAX_REVISIONS,
    MAX_REVISION_BYTES,
)
from local_web_server.host_profile_store import HostProfileStore, INITIALISATION


class DisposableWorkflowSupportError(RuntimeError):
    """A disposable workflow boundary failed without exposing private state."""


def sanitized_subprocess_environment(
    base: Mapping[str, str] | None = None,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Copy an environment without ambient Git controls, then add trusted values."""

    source = os.environ if base is None else base
    environment = {
        name: value
        for name, value in source.items()
        if not name.startswith("GIT_")
    }
    if overrides is not None:
        environment.update(overrides)
    return environment


def initialise_disposable_host_profile(platform: Path, content: bytes) -> Path:
    """Initialise and prove one exact, clean, mode-correct private profile."""

    try:
        paths = HostProfilePaths.for_repository(
            Path(platform), allow_public_scaffold=True
        )
        store = HostProfileStore(paths, allow_public_scaffold=True)
        store.initialise(content)
        snapshot = store.read_snapshot(
            max_profile_bytes=MAX_PROFILE_BYTES,
            max_revision_bytes=MAX_REVISION_BYTES,
            max_revisions=MAX_REVISIONS,
            max_total_bytes=MAX_BACKUP_BYTES,
        )
        revision_files = tuple(sorted(paths.history.iterdir()))
        revision = snapshot.revisions[0] if len(snapshot.revisions) == 1 else None
        expected_revision_name = (
            None
            if revision is None
            else f"{1:020d}-{revision.revision_id}.json"
        )
        private_files = (paths.profile, *revision_files)
        if (
            paths.profile != paths.repository / "config/local/apps.json"
            or snapshot.profile_bytes != content
            or revision is None
            or revision.registry_bytes != content
            or revision.previous_registry_sha256 is not None
            or revision.operation != INITIALISATION
            or revision.app_id is not None
            or len(revision_files) != 1
            or revision_files[0].name != expected_revision_name
            or revision_files[0].read_bytes() != revision.envelope_bytes
            or any(
                not path.is_dir()
                or path.is_symlink()
                or stat.S_IMODE(path.stat().st_mode) != 0o700
                for path in (paths.local, paths.history, paths.backups)
            )
            or any(
                not path.is_file()
                or path.is_symlink()
                or stat.S_IMODE(path.stat().st_mode) != 0o600
                for path in private_files
            )
            or any(paths.backups.iterdir())
            or paths.transaction.exists()
            or paths.recovery_residue.exists()
            or any(path.exists() for path in paths.temporary_files)
            or store.inspect_recovery() is not None
        ):
            raise DisposableWorkflowSupportError(
                "disposable private profile contract failed"
            )
        return paths.profile
    except DisposableWorkflowSupportError:
        raise
    except Exception:
        raise DisposableWorkflowSupportError(
            "disposable private profile contract failed"
        ) from None
