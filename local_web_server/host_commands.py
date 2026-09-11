"""Preview-first private host lifecycle operations and bounded public models.

Only migration may select a platform repository in the CLI. No command reads
ambient Git/profile overrides, installs files, deploys apps, or runs services.
Backup is an explicit no-clobber write; every other mutation consumes and
revalidates an exact frozen preview.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace as replace_plan
from datetime import datetime, timezone
from functools import wraps
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Callable

from .config import ConfigError, load_registry
from .install import InstallError
from .host_profile import HostProfileError, HostProfilePaths, profile_digest
from .host_profile_backup import (
    HostProfileBackupError, MAX_BACKUP_BYTES, MAX_PROFILE_BYTES,
    MAX_REVISION_BYTES, MAX_REVISIONS, _parent,
    parse_host_backup, write_host_backup,
)
from .host_profile_store import (
    HostProfileSnapshot, HostProfileStore, HostProfileStoreError,
    RecoveryPlan, _RegistryText, parse_revision,
)


DEFAULT_PLATFORM_REPOSITORY = Path(__file__).parents[1]
_UNSAFE = "private host command state is invalid or unsafe"
_CHANGED = "private host command state changed since preview"
_MODES = ("directories=0700", "files=0600")
_FAMILIES = ("host", "app", "apps", "deploy", "rollback", "theme", "status", "install")
_BACKUP_NAME = re.compile(r"local-web-host-([0-9]{8}T[0-9]{6}\.[0-9]{6}Z)\.json\Z")
_LIMITS = dict(max_profile_bytes=MAX_PROFILE_BYTES, max_revision_bytes=MAX_REVISION_BYTES,
               max_revisions=MAX_REVISIONS, max_total_bytes=MAX_BACKUP_BYTES)


class HostCommandError(RuntimeError):
    """A fixed, bounded diagnostic without user paths or registry details."""


class HostInitialBackupError(HostCommandError):
    """The valid initial profile was retained, but its backup is incomplete."""


def _bounded(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except HostCommandError:
            raise
        except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError,
                ConfigError, InstallError, HostProfileError, HostProfileStoreError, HostProfileBackupError,
                subprocess.SubprocessError):
            raise HostCommandError(_UNSAFE) from None
    return call


@dataclass(frozen=True, slots=True)
class HostCommandPlan:
    operation: str
    action: str
    source_label: str
    destination_label: str
    profile_sha256: str | None
    previous_sha256: str | None
    revision_id: str | None
    revision_count: int
    source_sha256: str | None = None
    candidate_sha256: str | None = None
    planned_modes: tuple[str, ...] = _MODES
    affected_command_families: tuple[str, ...] = _FAMILIES
    paths: HostProfilePaths | None = field(default=None, repr=False)
    source_path: Path | None = field(default=None, repr=False)
    source_identity: tuple[int, ...] = field(default=(), repr=False)
    source_bytes: bytes | None = field(default=None, repr=False)
    snapshot: HostProfileSnapshot | None = field(default=None, repr=False)
    candidate: HostProfileSnapshot | None = field(default=None, repr=False)
    state_identity: tuple = field(default=(), repr=False)
    recovery: RecoveryPlan | None = field(default=None, repr=False)
    replace: bool = field(default=False, repr=False)

    def as_dict(self):
        return {"operation": self.operation, "action": self.action,
                "source": self.source_label, "destination": self.destination_label,
                "sourceSha256": self.source_sha256,
                "profileSha256": self.profile_sha256, "previousProfileSha256": self.previous_sha256,
                "candidateProfileSha256": self.candidate_sha256,
                "revisionId": self.revision_id, "revisionCount": self.revision_count,
                "modes": list(self.planned_modes), "affectedCommands": list(self.affected_command_families)}


@dataclass(frozen=True, slots=True)
class HostCommandResult:
    plan: HostCommandPlan
    applied: bool
    revision_id: str | None = None
    backup_sha256: str | None = None
    backup_path: Path | None = field(default=None, repr=False)

    def as_dict(self):
        return self.plan.as_dict() | {"applied": self.applied,
                "publishedRevisionId": self.revision_id,
                "backupSha256": self.backup_sha256}


@dataclass(frozen=True, slots=True)
class HostStatus:
    profile_path: str
    registry_schema: int | None
    profile_sha256: str | None
    revision_id: str | None
    revision_count: int
    last_backup_at: str | None
    transaction_state: str
    legacy_file_present: bool

    def as_dict(self):
        return {"profilePath": self.profile_path, "registrySchema": self.registry_schema,
                "profileSha256": self.profile_sha256, "revisionId": self.revision_id,
                "revisionCount": self.revision_count, "lastBackupAt": self.last_backup_at,
                "transactionState": self.transaction_state, "legacyFilePresent": self.legacy_file_present}


def _metadata(path):
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _identity(metadata):
    return HostProfileStore._identity(metadata)


def _read_source(path, *, legacy=False, limit=MAX_PROFILE_BYTES):
    """Read one regular inode with descriptor/no-follow checks on every ancestor."""
    with _parent(path) as (directory, selected):
        descriptor = os.open(selected.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_size > limit
                    or before.st_nlink != 1
                    or (not legacy and stat.S_IMODE(before.st_mode) != 0o600)
                    or (legacy and stat.S_IMODE(before.st_mode) & 0o022)):
                raise HostCommandError(_UNSAFE)
            content = handle.read(limit + 1)
            after = os.stat(selected.name, dir_fd=directory, follow_symlinks=False)
            if (len(content) > limit or _identity(before) != _identity(after)
                    or _identity(before) != _identity(os.fstat(handle.fileno()))):
                raise HostCommandError(_UNSAFE)
            return selected, content, _identity(before)


class HostCommands:
    @_bounded
    def __init__(self, repository: Path | None = None, *, clock: Callable[[], datetime] | None = None):
        self.paths = HostProfilePaths.for_repository(
            DEFAULT_PLATFORM_REPOSITORY if repository is None else repository,
            allow_public_scaffold=True)
        self.store = HostProfileStore(
            self.paths, clock=clock, allow_public_scaffold=True
        )
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _validate_paths(self):
        if HostProfilePaths.for_repository(
                self.paths.repository, allow_public_scaffold=True) != self.paths:
            raise HostCommandError(_CHANGED)

    def _state_identity(self):
        """Bind previews to file identities as well as exact validated bytes."""
        self._validate_paths()
        result = []
        for path in (self.paths.local, self.paths.history, self.paths.backups, self.paths.recovery_residue,
                     self.paths.profile, self.paths.transaction, *self.paths.temporary_files):
            metadata = _metadata(path)
            value = None if metadata is None else _identity(metadata)
            if metadata is not None and stat.S_ISDIR(metadata.st_mode):
                value = (metadata.st_dev, metadata.st_ino, metadata.st_mode)
            # The backup scratch is owned by the independent backup writer;
            # backup creation must not invalidate the profile/history preview.
            if path != self.paths.backup_temporary:
                result.append((str(path.relative_to(self.paths.local)), value))
        if _metadata(self.paths.history) is not None:
            with _parent(self.paths.history / "unused") as (directory, _):
                with os.scandir(directory) as entries:
                    names = []
                    for entry in entries:
                        if len(names) >= MAX_REVISIONS + 1:
                            raise HostCommandError(_UNSAFE)
                        names.append(entry.name)
                for name in sorted(names):
                    metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    result.append(("history/" + name, _identity(metadata)))
        return tuple(result)

    def _absent(self, *, init=False):
        self._validate_paths()
        if any(_metadata(path) is not None for path in (
                self.paths.profile, self.paths.transaction, self.paths.profile_temporary,
                self.paths.transaction_temporary, self.paths.revision_temporary)):
            raise HostCommandError(_UNSAFE)
        if _metadata(self.paths.history) is not None:
            if init:
                raise HostCommandError(_UNSAFE)
            with _parent(self.paths.history / "unused") as (directory, _):
                with os.scandir(directory) as entries:
                    if next(entries, None) is not None:
                        raise HostCommandError(_UNSAFE)
        if _metadata(self.paths.recovery_residue) is not None and self.store.inspect_recovery() is not None:
            raise HostCommandError(_UNSAFE)

    def _snapshot(self):
        return self.store.read_snapshot(**_LIMITS)

    def _plan(self, operation, action, source_label, *, content=None, snapshot=None,
              source_path=None, source_identity=(), source_document=None,
              candidate=None, recovery=None, replace=False,
              destination_label="private-profile", modes=_MODES):
        previous = snapshot.profile_bytes if snapshot is not None else None
        profile = content
        revision = snapshot.revisions[-1].revision_id if snapshot is not None else None
        count = len(snapshot.revisions) if snapshot is not None else 0
        if recovery is not None:
            profile, previous = recovery.current_bytes, recovery.previous_bytes
            source_document = recovery.marker_bytes
            revision = recovery.revision_id
            count = len(recovery.history_ids)
        return HostCommandPlan(operation, action, source_label, destination_label,
                profile_digest(profile) if profile is not None else None,
                profile_digest(previous) if previous is not None else None,
                revision, count,
                source_sha256=profile_digest(source_document if source_document is not None else content)
                    if source_document is not None or content is not None else None,
                candidate_sha256=profile_digest(recovery.candidate_bytes) if recovery is not None else None,
                planned_modes=modes, paths=self.paths, source_path=source_path,
                source_identity=source_identity, source_bytes=content,
                snapshot=snapshot, candidate=candidate, recovery=recovery,
                replace=replace, state_identity=self._state_identity())

    @_bounded
    def preview_init(self, source: Path) -> HostCommandPlan:
        self._absent(init=True)
        selected, content, identity = _read_source(source)
        protected = tuple(metadata for path in (self.paths.history, self.paths.backups)
                          if (metadata := _metadata(path)) is not None)
        # Case aliases on macOS must not bypass the history/backup source ban.
        # Every source ancestor was already opened with O_NOFOLLOW.
        for ancestor in selected.parents:
            metadata = ancestor.lstat()
            if any((metadata.st_dev, metadata.st_ino) == (item.st_dev, item.st_ino)
                   for item in protected):
                raise HostCommandError(_UNSAFE)
        load_registry(_RegistryText(content))
        return self._plan("init", "initialise", "prepared-registry", content=content,
                          source_path=selected, source_identity=identity)

    @_bounded
    def preview_migration(self) -> HostCommandPlan:
        self._validate_paths()
        selected, content, identity = _read_source(self.paths.config / "apps.json", legacy=True)
        load_registry(_RegistryText(content))
        snapshot = None
        action = "initialise"
        if _metadata(self.paths.profile) is not None:
            snapshot = self._snapshot()
            if snapshot.profile_bytes != content:
                raise HostCommandError("legacy and private registry bytes do not match")
            backup = self._latest_backup()
            action = "already-current" if backup is not None and self._backup_matches(snapshot, backup) else "backup-required"
        else:
            self._absent(init=True)
        return self._plan("migrate-registry", action, "legacy-registry", content=content,
                          source_path=selected, source_identity=identity, snapshot=snapshot)

    @_bounded
    def preview_restore(self, source: Path, *, replace: bool = False) -> HostCommandPlan:
        self._validate_paths()
        selected, document, identity = _read_source(source, limit=MAX_BACKUP_BYTES)
        backup = parse_host_backup(selected)
        if backup.document_bytes != document:
            raise HostCommandError(_CHANGED)
        candidate = HostProfileSnapshot(backup.profile_bytes,
                    tuple(parse_revision(item.content) for item in backup.revisions))
        snapshot = None
        action = "restore-absent"
        if _metadata(self.paths.profile) is not None:
            if not replace:
                raise HostCommandError("existing private profile requires --replace and --apply")
            snapshot = self._snapshot()
            action = "already-current" if snapshot.profile_bytes == candidate.profile_bytes else "replace-profile"
        else:
            self._absent()
        return self._plan("restore", action, "host-backup", content=candidate.profile_bytes,
                          snapshot=snapshot, candidate=candidate, source_path=selected,
                          source_identity=identity, source_document=document, replace=replace)

    @_bounded
    def preview_recovery(self) -> HostCommandPlan:
        self._validate_paths()
        if _metadata(self.paths.local) is None:
            self._absent()
            recovery = None
        else:
            recovery = self.store.inspect_recovery()
        return self._plan("recover", recovery.action if recovery else "no-recovery", "private-transaction",
                          content=recovery.candidate_bytes if recovery else None, recovery=recovery)

    def _repreview(self, plan):
        if not isinstance(plan, HostCommandPlan) or plan.paths != self.paths:
            raise HostCommandError(_CHANGED)
        if plan.operation == "init":
            actual = self.preview_init(plan.source_path)
        elif plan.operation == "migrate-registry":
            actual = self.preview_migration()
        elif plan.operation == "restore":
            actual = self.preview_restore(plan.source_path, replace=plan.replace)
        elif plan.operation == "recover":
            actual = self.preview_recovery()
        else:
            raise HostCommandError(_UNSAFE)
        if actual != plan:
            raise HostCommandError(_CHANGED)

    def _verified_backup(self, snapshot, output=None):
        path = write_host_backup(self.paths, output, self.clock)
        parsed = parse_host_backup(path)
        if not self._backup_matches(snapshot, parsed):
            raise HostCommandError(_CHANGED)
        return path, profile_digest(parsed.document_bytes)

    @staticmethod
    def _backup_matches(snapshot, backup):
        return (backup.profile_bytes == snapshot.profile_bytes
                and tuple(item.content for item in backup.revisions)
                == tuple(item.envelope_bytes for item in snapshot.revisions))

    @_bounded
    def apply(self, plan: HostCommandPlan) -> HostCommandResult:
        self._repreview(plan)
        self.store._require_git(True)
        self._repreview(plan)
        if plan.action in ("already-current", "no-recovery"):
            return HostCommandResult(plan, False)
        if plan.operation == "migrate-registry" and plan.action == "backup-required":
            path, digest = self._verified_backup(plan.snapshot)
            # The sole permitted state change is our newly verified backup.
            # Recheck the exact legacy source and profile/chain after that I/O.
            self._repreview(replace_plan(plan, action="already-current"))
            return HostCommandResult(plan, True, backup_sha256=digest, backup_path=path)
        if plan.operation in ("init", "migrate-registry"):
            revision = self.store.initialise(plan.source_bytes)
            snapshot = self._snapshot()
            if snapshot.profile_bytes != plan.source_bytes:
                raise HostCommandError(_CHANGED)
            try:
                path, digest = self._verified_backup(snapshot)
            except (HostProfileBackupError, HostCommandError):
                raise HostInitialBackupError("initial private profile backup is incomplete") from None
            return HostCommandResult(plan, True, revision, digest, path)
        if plan.operation == "recover":
            revision = self.store.recover(plan.recovery)
            return HostCommandResult(plan, True, revision)
        if plan.operation == "restore":
            if plan.snapshot is None:
                revision = self.store.restore_snapshot(plan.candidate)
                return HostCommandResult(plan, True, revision)
            path, digest = self._verified_backup(plan.snapshot)
            self._repreview(plan)
            # Verify the backup immediately before the store's own expected-
            # before comparison and recoverable profile publication.
            parsed = parse_host_backup(path)
            if profile_digest(parsed.document_bytes) != digest:
                raise HostCommandError(_CHANGED)
            revision = self.store.publish_backup_restoration(plan.snapshot.profile_bytes,
                    plan.candidate.profile_bytes, expected_revisions=plan.snapshot.revisions)
            return HostCommandResult(plan, True, revision, digest, path)
        raise HostCommandError(_UNSAFE)

    @_bounded
    def backup(self, output: Path | None = None) -> HostCommandResult:
        snapshot = self._snapshot()
        self.store._require_git(True)
        plan = self._plan("backup", "write-backup", "private-profile", content=snapshot.profile_bytes,
                          snapshot=snapshot, modes=("files=0600",),
                          destination_label="private-backups" if output is None else "explicit-backup")
        path, digest = self._verified_backup(snapshot, output)
        if self._snapshot() != snapshot or self._state_identity() != plan.state_identity:
            raise HostCommandError(_CHANGED)
        return HostCommandResult(plan, True, backup_sha256=digest, backup_path=path)

    def _latest_backup(self):
        if _metadata(self.paths.backups) is None:
            return None
        latest, count = None, 0
        with _parent(self.paths.backups / "unused") as (directory, _):
            with os.scandir(directory) as entries:
                for entry in entries:
                    count += 1
                    if count > MAX_REVISIONS:
                        raise HostCommandError(_UNSAFE)
                    if _BACKUP_NAME.fullmatch(entry.name) and (latest is None or entry.name > latest):
                        latest = entry.name
        if latest is None:
            return None
        backup = parse_host_backup(self.paths.backups / latest)
        timestamp = datetime.fromisoformat(backup.created_at.replace("Z", "+00:00"))
        if timestamp.strftime("%Y%m%dT%H%M%S.%fZ") != _BACKUP_NAME.fullmatch(latest)[1]:
            raise HostCommandError(_UNSAFE)
        return backup

    @_bounded
    def status(self) -> HostStatus:
        self._validate_paths()
        if len(str(self.paths.profile)) > 4096:
            raise HostCommandError(_UNSAFE)
        snapshot = self.store.read_status_snapshot()
        profile = snapshot.profile_bytes
        count = len(snapshot.history_ids)
        revision = snapshot.history_ids[-1] if snapshot.history_ids else None
        state = (
            snapshot.recovery.action
            if snapshot.recovery is not None
            else "clean" if profile is not None else "missing"
        )
        registry = load_registry(_RegistryText(profile)) if profile is not None else None
        backup = self._latest_backup()
        return HostStatus(str(self.paths.profile), registry.schema_version if registry else None,
                          profile_digest(profile) if profile is not None else None,
                          revision, count, backup.created_at if backup else None, state,
                          _metadata(self.paths.config / "apps.json") is not None)
