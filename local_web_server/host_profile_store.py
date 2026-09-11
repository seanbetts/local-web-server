"""Private exact-byte revision history and recoverable host-profile publication.

The store, not its callers, publishes the current profile. A durable marker
precedes the immutable revision, which precedes the current-profile replace.
No Git commits, installation, or deployment are performed here.
"""

from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import fcntl
from functools import wraps
import json
import os
from pathlib import Path
import re
import stat
from typing import Callable

from .config import ConfigError, load_registry
from .git_runner import GitRunnerError, run_git
from .host_profile import (
    HostProfileError,
    HostProfilePaths,
    _open_public_scaffold,
    canonical_json_bytes,
    profile_digest,
)
from .platform_installation import require_committed_installation_sources
from .install import InstallError


INITIALISATION = "initialisation"
REGISTRATION = "registration"
SERVICE_COMMAND_MIGRATION = "service-command-migration"
SERVICE_COMMAND_RESTORATION = "service-command-restoration"
PUBLIC_BASE_PATH_MIGRATION = "public-base-path-migration"
PUBLIC_BASE_PATH_RESTORATION = "public-base-path-restoration"
BACKUP_RESTORATION = "backup-restoration"
_OPERATIONS = frozenset((REGISTRATION, SERVICE_COMMAND_MIGRATION,
                        SERVICE_COMMAND_RESTORATION, PUBLIC_BASE_PATH_MIGRATION,
                        PUBLIC_BASE_PATH_RESTORATION))
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_APP_ID = re.compile(r"[a-z][a-z0-9-]*\Z")
_FILENAME = re.compile(r"([0-9]{20})-([0-9a-f]{64})\.json\Z")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?Z\Z")
_SCHEMA = "local-web-host-revision/v1"
_MARKER_SCHEMA = "local-web-host-transaction/v1"
_RESTORE_SCHEMA = "local-web-host-restore-transaction/v1"
_MAX_PROFILE = 5 * 1024 * 1024
_MAX_REVISION = 8 * 1024 * 1024
_MAX_REVISIONS = 10_000
_MAX_TOTAL = 64 * 1024 * 1024
# Exact canonical wrapper plus maximum base64 profile and fixed-length records.
# Each record's trailing newline accounts for its comma separator, except last.
_MAX_RESTORE_MARKER = (
    len(canonical_json_bytes({"schema": _RESTORE_SCHEMA,
        "candidateRegistrySha256": "0" * 64, "candidateRegistryBase64": "",
        "plannedRevisionId": "0" * 64, "revisions": []}))
    + 4 * ((_MAX_PROFILE + 2) // 3)
    + _MAX_REVISIONS * len(canonical_json_bytes({
        "filename": "0" * 20 + "-" + "0" * 64 + ".json", "sha256": "0" * 64})) - 1
)
_UNSAFE = "private host profile store state is unsafe"
_PENDING = "private host profile transaction requires recovery"


class HostProfileStoreError(RuntimeError):
    """A bounded, path-free host-profile persistence failure."""


def _bounded(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except HostProfileStoreError:
            raise
        except (OSError, ValueError, TypeError, KeyError, OverflowError,
                RecursionError, ConfigError, GitRunnerError, HostProfileError,
                InstallError):
            raise HostProfileStoreError(_UNSAFE) from None
    return call


def _hex(value):
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _decode(value):
    if not isinstance(value, str):
        raise HostProfileStoreError(_UNSAFE)
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise HostProfileStoreError(_UNSAFE) from None
    if base64.b64encode(decoded).decode("ascii") != value:
        raise HostProfileStoreError(_UNSAFE)
    return decoded


def _object(content, keys):
    if not isinstance(content, bytes):
        raise HostProfileStoreError(_UNSAFE)
    value = json.loads(content)
    if (not isinstance(value, dict) or set(value) != keys
            or canonical_json_bytes(value) != content):
        raise HostProfileStoreError(_UNSAFE)
    return value


@dataclass(frozen=True, slots=True)
class Revision:
    revision_id: str
    envelope_bytes: bytes
    registry_bytes: bytes
    previous_registry_sha256: str | None
    current_registry_sha256: str
    operation: str
    app_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True, repr=False)
class HostProfileSnapshot:
    """One validated exact profile/chain read under a single store lock."""
    profile_bytes: bytes
    revisions: tuple[Revision, ...]


@dataclass(frozen=True, slots=True, repr=False)
class HostProfileStatusSnapshot:
    """One coherent clean or recoverable profile/history status read."""

    profile_bytes: bytes | None
    history_ids: tuple[str, ...]
    recovery: RecoveryPlan | None


@_bounded
def parse_revision(content: bytes) -> Revision:
    """Parse a canonical envelope; store reads also validate registry semantics."""
    value = _object(content, {"schema", "appId", "createdAt", "operation",
                              "previousRegistrySha256", "currentRegistrySha256",
                              "registryBase64"})
    operation, app_id = value["operation"], value["appId"]
    previous = value["previousRegistrySha256"]
    if not isinstance(operation, str):
        raise HostProfileStoreError(_UNSAFE)
    if operation == INITIALISATION:
        valid_operation = app_id is None and previous is None
    elif operation == BACKUP_RESTORATION:
        valid_operation = app_id is None and _hex(previous)
    else:
        valid_operation = (operation in _OPERATIONS and isinstance(app_id, str)
                           and _APP_ID.fullmatch(app_id) is not None and _hex(previous))
    created = value["createdAt"]
    if (value["schema"] != _SCHEMA or not valid_operation
            or not isinstance(created, str) or not _TIMESTAMP.fullmatch(created)):
        raise HostProfileStoreError(_UNSAFE)
    datetime.fromisoformat(created.replace("Z", "+00:00"))
    registry = _decode(value["registryBase64"])
    if not _hex(value["currentRegistrySha256"]) or profile_digest(registry) != value["currentRegistrySha256"]:
        raise HostProfileStoreError(_UNSAFE)
    return Revision(profile_digest(content), content, registry, previous,
                    value["currentRegistrySha256"], operation, app_id, created)


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryPlan:
    """Opaque exact-state token to revalidate before executing a preview."""
    action: str
    revision_id: str
    marker_bytes: bytes
    previous_bytes: bytes | None
    candidate_bytes: bytes
    current_bytes: bytes | None
    history_ids: tuple[str, ...]
    restore_records: tuple[tuple[str, str], ...] = ()
    file_identities: tuple[tuple[str, tuple[int, ...]], ...] = ()
    residue_state: tuple = ()


class _RegistryText:
    """The registry loader's read_text interface, without filesystem writes."""

    def __init__(self, content):
        self.content = content

    def read_text(self, *, encoding):
        return self.content.decode(encoding)

    def __str__(self):
        return "private host registry"


class HostProfileStore:
    @_bounded
    def __init__(self, paths: HostProfilePaths, *, clock: Callable[[], datetime] | None = None,
                 allow_public_scaffold: bool = False):
        if type(allow_public_scaffold) is not bool or paths != HostProfilePaths.for_repository(
                paths.repository, allow_public_scaffold=allow_public_scaffold):
            raise HostProfileStoreError(_UNSAFE)
        self.paths = paths
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._allow_public_scaffold = allow_public_scaffold

    @contextmanager
    def _session(self, *, create=False):
        if HostProfilePaths.for_repository(
                self.paths.repository,
                allow_public_scaffold=self._allow_public_scaffold,
        ) != self.paths:
            raise HostProfileStoreError(_UNSAFE)
        descriptors = []
        try:
            root = os.open(self.paths.repository, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(root)
            metadata = os.fstat(root)
            if (metadata.st_dev, metadata.st_ino) != (
                self.paths.repository_device, self.paths.repository_inode
            ):
                raise HostProfileStoreError(_UNSAFE)
            config = self._open_directory(root, "config", private=False)
            descriptors.append(config)
            if create and self._allow_public_scaffold:
                local = self._open_initial_local_directory(config)
            else:
                local = self._open_directory(config, "local", create=create)
            descriptors.append(local)
            # Directory locks serialize cooperating stores without another file.
            fcntl.flock(local, fcntl.LOCK_EX | fcntl.LOCK_NB)
            history = self._open_directory(local, "history", create=create)
            descriptors.append(history)
            backups = self._open_directory(local, "backups", create=create)
            descriptors.append(backups)
            yield local, history
            # Reopen from the canonical root: a displaced ancestor is not success.
            self._verify_anchor(local, history)
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @staticmethod
    def _open_directory(parent, name, *, private=True, create=False):
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            else:
                os.chmod(name, 0o700, dir_fd=parent, follow_symlinks=False)
                os.fsync(parent)
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        if private and stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
            os.close(descriptor)
            raise HostProfileStoreError(_UNSAFE)
        return descriptor

    def _open_initial_local_directory(self, config):
        try:
            return self._open_directory(config, "local", create=True)
        except HostProfileStoreError:
            descriptor = _open_public_scaffold(
                self.paths.local, self.paths.repository
            )
            try:
                os.fchmod(descriptor, 0o700)
                os.fsync(descriptor)
                os.fsync(config)
                metadata = os.fstat(descriptor)
                if stat.S_IMODE(metadata.st_mode) != 0o700:
                    raise HostProfileStoreError(_UNSAFE)
                verified = _open_public_scaffold(
                    self.paths.local, self.paths.repository
                )
                try:
                    current = os.fstat(verified)
                    opened = os.fstat(descriptor)
                    if (current.st_dev, current.st_ino) != (
                        opened.st_dev, opened.st_ino
                    ):
                        raise HostProfileStoreError(_UNSAFE)
                finally:
                    os.close(verified)
                return descriptor
            except BaseException:
                os.close(descriptor)
                raise

    def _verify_anchor(self, local, history):
        paths = HostProfilePaths.for_repository(self.paths.repository)
        if paths != self.paths:
            raise HostProfileStoreError(_UNSAFE)
        for path, descriptor in ((paths.local, local), (paths.history, history)):
            actual, expected = path.lstat(), os.fstat(descriptor)
            if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
                raise HostProfileStoreError(_UNSAFE)

    @staticmethod
    def _read(directory, name, *, optional=False, limit=_MAX_TOTAL):
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        except FileNotFoundError:
            if optional:
                return None
            raise
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
                    or (limit is not None and metadata.st_size > limit)):
                raise HostProfileStoreError(_UNSAFE)
            content = handle.read() if limit is None else handle.read(limit + 1)
            if limit is not None and len(content) > limit:
                raise HostProfileStoreError(_UNSAFE)
            current = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if HostProfileStore._identity(current) != HostProfileStore._identity(metadata):
                raise HostProfileStoreError(_UNSAFE)
            return content

    @staticmethod
    def _identity(metadata):
        return (metadata.st_dev, metadata.st_ino, metadata.st_mode,
                metadata.st_nlink, metadata.st_size, metadata.st_mtime_ns,
                metadata.st_ctime_ns)

    @staticmethod
    def _create(directory, name, content):
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(descriptor)
        except BaseException:
            os.unlink(name, dir_fd=directory)
            raise
        finally:
            os.close(descriptor)

    def _atomic(self, directory, temporary, destination, content, *, no_clobber=False, readback_limit=None):
        limit = readback_limit if readback_limit is not None else (_MAX_REVISION if no_clobber else _MAX_TOTAL)
        if type(limit) is not int or not 0 < limit <= _MAX_TOTAL or len(content) > limit:
            raise HostProfileStoreError(_UNSAFE)
        if no_clobber:
            return self._immutable(directory, temporary, destination, content, readback_limit=limit)
        self._create(directory, temporary, content)
        try:
            os.replace(temporary, destination, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
            if self._read(directory, destination, limit=limit) != content:
                raise HostProfileStoreError(_UNSAFE)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def _immutable(self, directory, temporary, destination, content, *, readback_limit=_MAX_REVISION):
        # Reuse the supported descriptor-only, all-or-none no-clobber
        # publication primitive. Neither a source path nor archive filename
        # chooses the bytes published. Unsupported filesystems fail closed.
        from .host_profile_backup import _publish_from_fd
        if type(readback_limit) is not int or not 0 < readback_limit <= _MAX_TOTAL or len(content) > readback_limit:
            raise HostProfileStoreError(_UNSAFE)
        descriptor = os.open(temporary, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
        owned = os.fstat(descriptor)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(descriptor)
            _publish_from_fd(descriptor, directory, destination)
            os.fsync(directory)
            if self._read(directory, destination, limit=readback_limit) != content:
                raise HostProfileStoreError(_UNSAFE)
        finally:
            os.close(descriptor)
            current = os.stat(temporary, dir_fd=directory, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (owned.st_dev, owned.st_ino):
                raise HostProfileStoreError(_UNSAFE)
            os.unlink(temporary, dir_fd=directory)
            os.fsync(directory)

    def _validate_registry(self, local, content):
        if not isinstance(content, bytes) or len(content) > _MAX_PROFILE:
            raise HostProfileStoreError(_UNSAFE)
        load_registry(_RegistryText(content))

    @staticmethod
    def _filename(sequence, revision_id):
        if not 0 < sequence < 10**20:
            raise HostProfileStoreError(_UNSAFE)
        return f"{sequence:020d}-{revision_id}.json"

    def _chain(self, local, history, *, allow_temporary=False, limits=None):
        if limits is None:
            limits = (_MAX_PROFILE, _MAX_REVISION, _MAX_REVISIONS, _MAX_TOTAL)
        result = []
        names = []
        with os.scandir(history) as entries:
            for entry in entries:
                if limits is not None and len(names) >= limits[2]:
                    raise HostProfileStoreError(_UNSAFE)
                names.append(entry.name)
        names.sort()
        if allow_temporary:
            names = [name for name in names if name != self.paths.revision_temporary.name]
        seen = set()
        total = 0
        for sequence, name in enumerate(names, 1):
            match = _FILENAME.fullmatch(name)
            if not match or int(match[1]) != sequence:
                raise HostProfileStoreError(_UNSAFE)
            limit = min(limits[1], limits[3] - total) if limits is not None else None
            content = self._read(history, name, limit=limit)
            total += len(content)
            revision = parse_revision(content)
            if limits is not None and len(revision.registry_bytes) > limits[0]:
                raise HostProfileStoreError(_UNSAFE)
            if revision.revision_id != match[2] or revision.revision_id in seen:
                raise HostProfileStoreError(_UNSAFE)
            previous = result[-1].current_registry_sha256 if result else None
            if revision.previous_registry_sha256 != previous or (sequence > 1 and revision.operation == INITIALISATION):
                raise HostProfileStoreError(_UNSAFE)
            self._validate_registry(local, revision.registry_bytes)
            seen.add(revision.revision_id)
            result.append(revision)
        return tuple(result)

    def _require_git(self, require_main):
        private_paths = (
            self.paths.profile,
            self.paths.history,
            self.paths.backups,
            self.paths.recovery_residue,
            self.paths.transaction,
            self.paths.profile_temporary,
            self.paths.revision_temporary,
            self.paths.transaction_temporary,
            self.paths.backup_temporary,
        )
        for path in private_paths:
            relative = path.relative_to(self.paths.repository).as_posix()
            ignored = run_git(
                self.paths.repository,
                ("check-ignore", "--no-index", "--quiet", "--", relative),
                maximum_stdout=0,
            )
            if ignored.returncode != 0:
                raise HostProfileStoreError(_UNSAFE)
        require_committed_installation_sources(self.paths.repository, require_main=require_main)
        result = run_git(
            self.paths.repository,
            ("rev-parse", "--verify", "HEAD"),
            maximum_stdout=128,
        )
        if result.returncode != 0:
            raise HostProfileStoreError(_UNSAFE)

    def _no_marker(self, local):
        if self._read(local, self.paths.transaction.name, optional=True) is not None:
            raise HostProfileStoreError(_PENDING)
        from .host_profile_recovery import RecoveryArchive
        RecoveryArchive(self, local, None).inspect(None)

    def _no_temporaries(self, local, history):
        for directory, name in ((local, self.paths.profile_temporary.name),
                                (local, self.paths.transaction_temporary.name),
                                (history, self.paths.revision_temporary.name)):
            if self._read(directory, name, optional=True) is not None:
                raise HostProfileStoreError(_UNSAFE)

    def _current_chain(self, local, history):
        self._no_marker(local)
        self._no_temporaries(local, history)
        revisions = self._chain(local, history)
        current = self._read(local, self.paths.profile.name)
        if not revisions or revisions[-1].registry_bytes != current:
            raise HostProfileStoreError(_UNSAFE)
        return current, revisions

    @_bounded
    def require_clean(self, *, require_main=True):
        self._require_git(require_main)
        with self._session() as (local, history):
            self._current_chain(local, history)

    @_bounded
    def revisions(self) -> tuple[Revision, ...]:
        with self._session() as (local, history):
            return self._current_chain(local, history)[1]

    @_bounded
    def read_current(self) -> bytes:
        with self._session() as (local, history):
            return self._current_chain(local, history)[0]

    @_bounded
    def read_snapshot(self, *, max_profile_bytes: int, max_revision_bytes: int,
                      max_revisions: int, max_total_bytes: int) -> HostProfileSnapshot:
        """Read coherent exact state with caller-supplied positive allocation bounds.

        Total counts profile plus envelope bytes. File sizes and revision count
        are checked before reading; capped reads also catch growing files.
        Registry semantics validation is in memory and performs no writes.
        """
        limits = (max_profile_bytes, max_revision_bytes, max_revisions, max_total_bytes)
        if any(type(value) is not int or value <= 0 for value in limits):
            raise HostProfileStoreError(_UNSAFE)
        with self._session() as (local, history):
            # Refuse pending state by metadata alone, without reading an
            # arbitrarily large transaction or leftover temporary.
            for path in (self.paths.transaction, self.paths.profile_temporary,
                         self.paths.transaction_temporary,
                         self.paths.revision_temporary):
                directory = history if path.parent == self.paths.history else local
                try:
                    os.stat(path.name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise HostProfileStoreError(_PENDING if path == self.paths.transaction else _UNSAFE)
            self._no_marker(local)
            current = self._read(local, self.paths.profile.name,
                                 limit=min(max_profile_bytes, max_total_bytes))
            revisions = self._chain(local, history, limits=(
                max_profile_bytes, max_revision_bytes, max_revisions, max_total_bytes - len(current)))
            if not revisions or revisions[-1].registry_bytes != current:
                raise HostProfileStoreError(_UNSAFE)
            return HostProfileSnapshot(current, revisions)

    @_bounded
    def read_status_snapshot(self) -> HostProfileStatusSnapshot:
        """Read current bytes and history identity once under the store lock."""

        paths = HostProfilePaths.for_repository(
            self.paths.repository,
            allow_public_scaffold=self._allow_public_scaffold,
        )
        if paths != self.paths:
            raise HostProfileStoreError(_UNSAFE)
        try:
            local_metadata = self.paths.local.lstat()
        except FileNotFoundError:
            return HostProfileStatusSnapshot(None, (), None)
        if not stat.S_ISDIR(local_metadata.st_mode):
            raise HostProfileStoreError(_UNSAFE)
        if stat.S_IMODE(local_metadata.st_mode) != 0o700:
            if not self._allow_public_scaffold:
                raise HostProfileStoreError(_UNSAFE)
            return HostProfileStatusSnapshot(None, (), None)

        with self._session() as (local, history):
            marker = self._read(
                local, self.paths.transaction.name, optional=True, limit=_MAX_TOTAL
            )
            if marker is not None:
                recovery = self._inspect(local, history)
                if recovery is None:
                    raise HostProfileStoreError(_UNSAFE)
                return HostProfileStatusSnapshot(
                    recovery.current_bytes, recovery.history_ids, recovery
                )
            self._no_marker(local)
            self._no_temporaries(local, history)
            revisions = self._chain(local, history)
            current = self._read(
                local, self.paths.profile.name, optional=True, limit=_MAX_PROFILE
            )
            if current is None and not revisions:
                return HostProfileStatusSnapshot(None, (), None)
            if not revisions or revisions[-1].registry_bytes != current:
                raise HostProfileStoreError(_UNSAFE)
            return HostProfileStatusSnapshot(
                current,
                tuple(revision.revision_id for revision in revisions),
                None,
            )

    def _revision(self, operation, app_id, before, after):
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise HostProfileStoreError(_UNSAFE)
        created = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return parse_revision(canonical_json_bytes({
            "schema": _SCHEMA, "appId": app_id, "createdAt": created, "operation": operation,
            "previousRegistrySha256": profile_digest(before) if before is not None else None,
            "currentRegistrySha256": profile_digest(after),
            "registryBase64": base64.b64encode(after).decode("ascii"),
        }))

    @_bounded
    def initialise(self, registry_bytes: bytes) -> str:
        self._require_git(True)
        with self._session(create=True) as (local, history):
            self._no_marker(local)
            self._no_temporaries(local, history)
            if self._read(local, self.paths.profile.name, optional=True) is not None or os.listdir(history):
                raise HostProfileStoreError(_UNSAFE)
            self._validate_registry(local, registry_bytes)
            revision = self._revision(INITIALISATION, None, None, registry_bytes)
            return self._publish(local, history, (), revision, None)

    @_bounded
    def _change(self, operation, app_id, expected_before, expected_after, *, expected_revisions=None):
        valid_app = (app_id is None if operation == BACKUP_RESTORATION
                     else isinstance(app_id, str) and _APP_ID.fullmatch(app_id))
        if (not valid_app
                or not isinstance(expected_before, bytes) or not isinstance(expected_after, bytes)):
            raise HostProfileStoreError(_UNSAFE)
        self._require_git(True)
        with self._session() as (local, history):
            current, revisions = self._current_chain(local, history)
            if current != expected_before:
                raise HostProfileStoreError("private host profile changed since preview")
            if expected_revisions is not None and revisions != expected_revisions:
                raise HostProfileStoreError("private host profile changed since preview")
            if expected_before == expected_after:
                return None
            self._validate_registry(local, expected_after)
            revision = self._revision(operation, app_id, current, expected_after)
            if revision.revision_id in {item.revision_id for item in revisions}:
                raise HostProfileStoreError(_UNSAFE)
            return self._publish(local, history, revisions, revision, current)

    def publish_registration(self, app_id, expected_before, expected_after):
        return self._change(REGISTRATION, app_id, expected_before, expected_after)

    def publish_service_command_migration(self, app_id, expected_before, expected_after):
        return self._change(SERVICE_COMMAND_MIGRATION, app_id, expected_before, expected_after)

    def publish_service_command_restoration(self, app_id, expected_before, expected_after):
        return self._change(SERVICE_COMMAND_RESTORATION, app_id, expected_before, expected_after)

    def publish_public_base_path_migration(self, app_id, expected_before, expected_after):
        return self._change(PUBLIC_BASE_PATH_MIGRATION, app_id, expected_before, expected_after)

    def publish_public_base_path_restoration(self, app_id, expected_before, expected_after):
        return self._change(PUBLIC_BASE_PATH_RESTORATION, app_id, expected_before, expected_after)

    def publish_backup_restoration(self, expected_before, expected_after, *, expected_revisions=None):
        """Append one fixed restoration audit event; keep local history intact."""
        return self._change(BACKUP_RESTORATION, None, expected_before, expected_after,
                            expected_revisions=expected_revisions)

    @_bounded
    def restore_snapshot(self, snapshot: HostProfileSnapshot) -> str:
        """Install an exact validated backup chain only into an absent profile.

        A dedicated marker schema shares the one fixed transaction pathname.
        Revisions publish no-clobber in sequence; the profile publishes last.
        Interrupted exact prefixes roll back, complete chains roll forward.
        """
        self._require_git(True)
        if (not isinstance(snapshot, HostProfileSnapshot)
                or not isinstance(snapshot.profile_bytes, bytes)
                or not isinstance(snapshot.revisions, tuple)
                or not 1 <= len(snapshot.revisions) <= _MAX_REVISIONS):
            raise HostProfileStoreError(_UNSAFE)
        self._validate_registry(None, snapshot.profile_bytes)
        previous, total, records, seen = None, len(snapshot.profile_bytes), [], set()
        for sequence, item in enumerate(snapshot.revisions, 1):
            if not isinstance(item, Revision) or len(item.envelope_bytes) > _MAX_REVISION:
                raise HostProfileStoreError(_UNSAFE)
            total += len(item.envelope_bytes)
            revision = parse_revision(item.envelope_bytes)
            if (revision != item or revision.previous_registry_sha256 != previous
                    or (sequence == 1) != (revision.operation == INITIALISATION)
                    or revision.revision_id in seen or total > _MAX_TOTAL):
                raise HostProfileStoreError(_UNSAFE)
            self._validate_registry(None, revision.registry_bytes)
            previous = revision.current_registry_sha256
            seen.add(revision.revision_id)
            records.append({"filename": self._filename(sequence, revision.revision_id),
                            "sha256": revision.revision_id})
        if snapshot.revisions[-1].registry_bytes != snapshot.profile_bytes:
            raise HostProfileStoreError(_UNSAFE)
        marker = canonical_json_bytes({
            "schema": _RESTORE_SCHEMA, "candidateRegistrySha256": profile_digest(snapshot.profile_bytes),
            "candidateRegistryBase64": base64.b64encode(snapshot.profile_bytes).decode("ascii"),
            "plannedRevisionId": snapshot.revisions[-1].revision_id, "revisions": records,
        })
        with self._session(create=True) as (local, history):
            self._no_marker(local)
            self._no_temporaries(local, history)
            if self._read(local, self.paths.profile.name, optional=True) is not None or os.listdir(history):
                raise HostProfileStoreError(_UNSAFE)
            self._verify_anchor(local, history)
            self._atomic(local, self.paths.transaction_temporary.name, self.paths.transaction.name,
                         marker, no_clobber=True, readback_limit=_MAX_RESTORE_MARKER)
            for record, revision in zip(records, snapshot.revisions):
                self._verify_anchor(local, history)
                self._atomic(history, self.paths.revision_temporary.name, record["filename"],
                             revision.envelope_bytes, no_clobber=True)
            self._verify_anchor(local, history)
            self._atomic(local, self.paths.profile_temporary.name, self.paths.profile.name,
                         snapshot.profile_bytes, no_clobber=True, readback_limit=_MAX_PROFILE)
            self._verify_anchor(local, history)
            actual = self._inspect(local, history)
            if actual.action != "clear-marker":
                raise HostProfileStoreError(_UNSAFE)
            from .host_profile_recovery import RecoveryArchive
            RecoveryArchive(self, local, history).apply(actual)
            return snapshot.revisions[-1].revision_id

    def _publish(self, local, history, revisions, revision, before):
        marker = canonical_json_bytes({
            "schema": _MARKER_SCHEMA, "plannedRevisionId": revision.revision_id,
            "previousRegistrySha256": revision.previous_registry_sha256,
            "candidateRegistrySha256": revision.current_registry_sha256,
            "previousRegistryBase64": base64.b64encode(before).decode("ascii") if before is not None else None,
            "candidateRegistryBase64": base64.b64encode(revision.registry_bytes).decode("ascii"),
        })
        name = self._filename(len(revisions) + 1, revision.revision_id)
        if self._read(history, name, optional=True) is not None:
            raise HostProfileStoreError(_UNSAFE)
        self._verify_anchor(local, history)
        self._atomic(local, self.paths.transaction_temporary.name, self.paths.transaction.name, marker)
        self._atomic(history, self.paths.revision_temporary.name, name, revision.envelope_bytes)
        self._atomic(local, self.paths.profile_temporary.name, self.paths.profile.name, revision.registry_bytes)
        self._verify_anchor(local, history)
        os.unlink(self.paths.transaction.name, dir_fd=local)
        os.fsync(local)
        return revision.revision_id

    def _inspect(self, local, history):
        from .host_profile_recovery import RecoveryArchive
        plan = self._inspect_state(local, history)
        residue = RecoveryArchive(self, local, history).inspect(plan)
        return replace(plan, residue_state=residue) if plan is not None else None

    def _inspect_state(self, local, history):
        marker_bytes = self._read(local, self.paths.transaction.name, optional=True)
        if marker_bytes is None:
            self._no_temporaries(local, history)
            revisions = self._chain(local, history)
            current = self._read(local, self.paths.profile.name, optional=True)
            if current != (revisions[-1].registry_bytes if revisions else None):
                raise HostProfileStoreError(_UNSAFE)
            return None
        decoded = json.loads(marker_bytes)
        if isinstance(decoded, dict) and decoded.get("schema") == _RESTORE_SCHEMA:
            return self._inspect_restore(local, history, marker_bytes)
        marker = _object(marker_bytes, {"schema", "plannedRevisionId", "previousRegistrySha256",
                                       "candidateRegistrySha256", "previousRegistryBase64", "candidateRegistryBase64"})
        if marker["schema"] != _MARKER_SCHEMA or not _hex(marker["plannedRevisionId"]):
            raise HostProfileStoreError(_UNSAFE)
        before = _decode(marker["previousRegistryBase64"]) if marker["previousRegistryBase64"] is not None else None
        after = _decode(marker["candidateRegistryBase64"])
        if (marker["previousRegistrySha256"] != (profile_digest(before) if before is not None else None)
                or marker["candidateRegistrySha256"] != profile_digest(after) or before == after):
            raise HostProfileStoreError(_UNSAFE)
        self._validate_registry(local, after)
        revisions = self._chain(local, history, allow_temporary=True)
        planned = marker["plannedRevisionId"]
        matching = bool(revisions and revisions[-1].revision_id == planned)
        prior = revisions[:-1] if matching else revisions
        if ((prior[-1].registry_bytes if prior else None) != before
                or (matching and (revisions[-1].registry_bytes != after
                                  or revisions[-1].previous_registry_sha256 != marker["previousRegistrySha256"]))
                or (not matching and planned in {revision.revision_id for revision in revisions})):
            raise HostProfileStoreError(_UNSAFE)
        current = self._read(local, self.paths.profile.name, optional=True)
        if current == before:
            action = "publish-candidate" if matching else "remove-untouched-marker"
        elif current == after and matching:
            action = "clear-marker"
        else:
            raise HostProfileStoreError(_UNSAFE)
        self._validate_residue(
            local, history, marker_bytes, after, planned,
            allow_unpublished=action == "remove-untouched-marker",
        )
        return RecoveryPlan(action, planned, marker_bytes, before, after, current,
                            tuple(revision.revision_id for revision in revisions),
                            file_identities=self._recovery_identities(local, history, revisions))

    def _recovery_identities(self, local, history, revisions):
        identities = []
        names = [(local, self.paths.transaction.name), (local, self.paths.profile.name),
                 (local, self.paths.profile_temporary.name), (local, self.paths.transaction_temporary.name),
                 (history, self.paths.revision_temporary.name)]
        names.extend((history, self._filename(index, revision.revision_id))
                     for index, revision in enumerate(revisions, 1))
        for directory, name in names:
            try:
                metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                continue
            identities.append((name, self._identity(metadata)))
        return tuple(identities)

    def _inspect_restore(self, local, history, marker_bytes):
        if len(marker_bytes) > _MAX_RESTORE_MARKER:
            raise HostProfileStoreError(_UNSAFE)
        marker = _object(marker_bytes, {"schema", "plannedRevisionId", "candidateRegistrySha256",
                                       "candidateRegistryBase64", "revisions"})
        records = marker["revisions"]
        if not isinstance(records, list) or not 1 <= len(records) <= _MAX_REVISIONS:
            raise HostProfileStoreError(_UNSAFE)
        expected, seen = [], set()
        for sequence, record in enumerate(records, 1):
            if (not isinstance(record, dict) or set(record) != {"filename", "sha256"}
                    or not _hex(record["sha256"]) or record["sha256"] in seen
                    or record["filename"] != self._filename(sequence, record["sha256"])):
                raise HostProfileStoreError(_UNSAFE)
            seen.add(record["sha256"])
            expected.append((record["filename"], record["sha256"]))
        if marker["plannedRevisionId"] != expected[-1][1]:
            raise HostProfileStoreError(_UNSAFE)
        after = _decode(marker["candidateRegistryBase64"])
        if marker["candidateRegistrySha256"] != profile_digest(after):
            raise HostProfileStoreError(_UNSAFE)
        self._validate_registry(local, after)
        revisions = self._chain(local, history, allow_temporary=True)
        if (len(revisions) > len(expected)
                or any(revision.revision_id != expected[index][1] for index, revision in enumerate(revisions))):
            raise HostProfileStoreError(_UNSAFE)
        complete = len(revisions) == len(expected)
        if complete and revisions[-1].registry_bytes != after:
            raise HostProfileStoreError(_UNSAFE)
        current = self._read(local, self.paths.profile.name, optional=True, limit=_MAX_PROFILE)
        if current is None:
            action = "publish-candidate" if complete else "remove-untouched-marker"
        elif complete and current == after:
            action = "clear-marker"
        else:
            raise HostProfileStoreError(_UNSAFE)
        for name, content in ((self.paths.transaction_temporary.name, marker_bytes),
                              (self.paths.profile_temporary.name, after)):
            residue = self._read(local, name, optional=True)
            if residue is not None and residue != content:
                raise HostProfileStoreError(_UNSAFE)
        residue = self._read(history, self.paths.revision_temporary.name, optional=True, limit=_MAX_REVISION)
        if residue is not None:
            revision = parse_revision(residue)
            candidates = {item[1] for item in expected[max(0, len(revisions) - 1):len(revisions) + 1]}
            if revision.revision_id not in candidates:
                raise HostProfileStoreError(_UNSAFE)
        return RecoveryPlan(action, expected[-1][1], marker_bytes, None, after, current,
                            tuple(revision.revision_id for revision in revisions), tuple(expected),
                            self._recovery_identities(local, history, revisions))

    def _validate_residue(self, local, history, marker, after, planned, *, allow_unpublished=False):
        for name, expected in ((self.paths.transaction_temporary.name, marker),
                               (self.paths.profile_temporary.name, after)):
            content = self._read(local, name, optional=True)
            if content is not None and not allow_unpublished and content != expected:
                raise HostProfileStoreError(_UNSAFE)
        content = self._read(history, self.paths.revision_temporary.name, optional=True)
        if content is not None and not allow_unpublished and parse_revision(content).revision_id != planned:
            raise HostProfileStoreError(_UNSAFE)

    @_bounded
    def inspect_recovery(self) -> RecoveryPlan | None:
        with self._session() as (local, history):
            return self._inspect(local, history)

    @_bounded
    def recover(self, expected_plan: RecoveryPlan) -> str | None:
        self._require_git(True)
        with self._session() as (local, history):
            actual = self._inspect(local, history)
            if not isinstance(expected_plan, RecoveryPlan) or actual is None or actual != expected_plan:
                raise HostProfileStoreError("private host profile recovery plan changed")
            self._verify_anchor(local, history)
            from .host_profile_recovery import RecoveryArchive
            RecoveryArchive(self, local, history).apply(actual)
            return None if actual.action == "remove-untouched-marker" else actual.revision_id
