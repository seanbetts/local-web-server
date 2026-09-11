"""Bounded exact-byte private host backups; no extraction or restore writes.

The only payloads are apps.json and the complete canonical revision chain.
Names are identities, never paths to extract. SHA-256 detects corruption, not
authenticity; these unencrypted documents must remain private.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import ctypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
import json
import errno
import fcntl
import os
from pathlib import Path
import re
import stat
import sys
from typing import Callable

from .config import ConfigError, load_registry
from .host_profile import HostProfileError, HostProfilePaths, canonical_json_bytes, profile_digest
from .host_profile_store import HostProfileStore, HostProfileStoreError, INITIALISATION, parse_revision


MAX_BACKUP_BYTES = 64 * 1024 * 1024
MAX_PROFILE_BYTES = 5 * 1024 * 1024
MAX_REVISION_BYTES = 8 * 1024 * 1024
MAX_REVISIONS = 10_000
_SCHEMA = "local-web-host-backup/v1"
_UNSAFE = "private host backup is invalid or unsafe"
_TEMPORARY = ".host-profile-backup.json.tmp"
_PROVENANCE = b"com.apple.provenance"
_REVISION_NAME = re.compile(r"([0-9]{20})-([0-9a-f]{64})\.json\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{6})?Z\Z")


class HostProfileBackupError(RuntimeError):
    """A bounded diagnostic that never carries decoded data or user paths."""


def _bounded(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except HostProfileBackupError:
            raise
        except (OSError, ValueError, TypeError, KeyError, OverflowError,
                RecursionError, ConfigError, HostProfileError, HostProfileStoreError):
            raise HostProfileBackupError(_UNSAFE) from None
    return call


@dataclass(frozen=True, slots=True)
class BackupRecord:
    filename: str
    sha256: str
    content: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class HostProfileBackup:
    created_at: str
    profile: BackupRecord
    revisions: tuple[BackupRecord, ...]
    document_bytes: bytes = field(repr=False)

    @property
    def profile_bytes(self) -> bytes:
        return self.profile.content


class _RegistryText:
    """Read-only text source for the existing registry loader, without a file.

    load_registry consumes read_text only. Keep this adapter narrow so backup
    parsing applies the real registry semantics without temporary-file writes.
    """

    def __init__(self, content: bytes):
        self._content = content

    def read_text(self, *, encoding: str) -> str:
        return self._content.decode(encoding)

    def __str__(self) -> str:
        return "private backup registry"


def _registry(content):
    if len(content) > MAX_PROFILE_BYTES:
        raise HostProfileBackupError(_UNSAFE)
    load_registry(_RegistryText(content))


def _keys(value, keys):
    if not isinstance(value, dict) or set(value) != keys:
        raise HostProfileBackupError(_UNSAFE)


def _record(value, limit):
    _keys(value, {"filename", "sha256", "base64"})
    name, digest, encoded = value["filename"], value["sha256"], value["base64"]
    if (not isinstance(name, str) or len(name) > 90
            or not isinstance(digest, str) or not _SHA256.fullmatch(digest)
            or not isinstance(encoded, str) or len(encoded) > 4 * ((limit + 2) // 3)):
        raise HostProfileBackupError(_UNSAFE)
    content = base64.b64decode(encoded, validate=True)
    if (len(content) > limit or base64.b64encode(content).decode("ascii") != encoded
            or profile_digest(content) != digest):
        raise HostProfileBackupError(_UNSAFE)
    return BackupRecord(name, digest, content)


def _parse(content):
    if len(content) > MAX_BACKUP_BYTES:
        raise HostProfileBackupError(_UNSAFE)
    value = json.loads(content)
    _keys(value, {"schema", "createdAt", "profile", "revisions"})
    if value["schema"] != _SCHEMA or canonical_json_bytes(value) != content:
        raise HostProfileBackupError(_UNSAFE)
    created = value["createdAt"]
    if not isinstance(created, str) or not _TIMESTAMP.fullmatch(created):
        raise HostProfileBackupError(_UNSAFE)
    datetime.fromisoformat(created.replace("Z", "+00:00"))
    raw_revisions = value["revisions"]
    if not isinstance(raw_revisions, list) or not 1 <= len(raw_revisions) <= MAX_REVISIONS:
        raise HostProfileBackupError(_UNSAFE)
    profile = _record(value["profile"], MAX_PROFILE_BYTES)
    if profile.filename != "apps.json":
        raise HostProfileBackupError(_UNSAFE)
    _registry(profile.content)
    revisions, seen, previous, tip = [], set(), None, None
    for sequence, raw in enumerate(raw_revisions, 1):
        record = _record(raw, MAX_REVISION_BYTES)
        match = _REVISION_NAME.fullmatch(record.filename)
        if (not match or int(match[1]) != sequence or match[2] != record.sha256
                or record.sha256 in seen):
            raise HostProfileBackupError(_UNSAFE)
        revision = parse_revision(record.content)
        if (revision.previous_registry_sha256 != previous
                or (sequence == 1) != (revision.operation == INITIALISATION)):
            raise HostProfileBackupError(_UNSAFE)
        _registry(revision.registry_bytes)
        previous, tip = revision.current_registry_sha256, revision.registry_bytes
        seen.add(record.sha256)
        revisions.append(record)
    if tip != profile.content:
        raise HostProfileBackupError(_UNSAFE)
    return HostProfileBackup(created, profile, tuple(revisions), content)


def _read(directory, name, limit, *, private_metadata=False, provenance=None):
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(descriptor, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > limit):
            raise HostProfileBackupError(_UNSAFE)
        if private_metadata:
            _validate_private_metadata(handle.fileno(), provenance)
            os.fsync(handle.fileno())
        content = handle.read(limit + 1)
        if len(content) > limit:
            raise HostProfileBackupError(_UNSAFE)
        return content


def _open_directory(path):
    """Walk every component from / with no-follow and descriptor-relative opens."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _identity(metadata):
    return metadata.st_dev, metadata.st_ino


def _verify_parent(path, descriptor):
    reopened = _open_directory(path)
    try:
        if _identity(os.fstat(reopened)) != _identity(os.fstat(descriptor)):
            raise HostProfileBackupError(_UNSAFE)
    finally:
        os.close(reopened)


@contextmanager
def _parent(path):
    path = Path(path).absolute()
    if ".." in path.parts or path.name in ("", ".", ".."):
        raise HostProfileBackupError(_UNSAFE)
    descriptor = _open_directory(path.parent)
    try:
        yield descriptor, path
        _verify_parent(path.parent, descriptor)
    finally:
        os.close(descriptor)


@_bounded
def parse_host_backup(path: Path) -> HostProfileBackup:
    """Read one bounded private regular file, validate fully, and write nothing."""
    with _parent(path) as (directory, selected):
        return _parse(_read(directory, selected.name, MAX_BACKUP_BYTES))


def _encoded(name, content):
    return {"filename": name, "sha256": profile_digest(content),
            "base64": base64.b64encode(content).decode("ascii")}


@_bounded
def build_host_backup(
    paths: HostProfilePaths, clock: Callable[[], datetime] | None = None,
) -> HostProfileBackup:
    """Build from a bounded coherent validated store snapshot."""
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise HostProfileBackupError(_UNSAFE)
    created = now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    snapshot = HostProfileStore(paths).read_snapshot(
        max_profile_bytes=MAX_PROFILE_BYTES, max_revision_bytes=MAX_REVISION_BYTES,
        max_revisions=MAX_REVISIONS, max_total_bytes=MAX_BACKUP_BYTES)
    profile = _encoded("apps.json", snapshot.profile_bytes)
    records = []
    size = len(canonical_json_bytes(profile))
    for sequence, revision in enumerate(snapshot.revisions, 1):
        record = _encoded(f"{sequence:020d}-{revision.revision_id}.json", revision.envelope_bytes)
        size += len(canonical_json_bytes(record))
        if size > MAX_BACKUP_BYTES:
            raise HostProfileBackupError(_UNSAFE)
        records.append(record)
    content = canonical_json_bytes({"schema": _SCHEMA, "createdAt": created,
                                    "profile": profile, "revisions": records})
    return _parse(content)


def _metadata(directory, name):
    try:
        metadata = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise HostProfileBackupError(_UNSAFE)
    return metadata


def _native(name, result, arguments):
    if sys.platform != "darwin":
        raise OSError(errno.ENOTSUP, "private descriptor operation unsupported")
    try:
        function = getattr(ctypes.CDLL(None, use_errno=True), name)
    except AttributeError:
        raise OSError(errno.ENOTSUP, "private descriptor operation unsupported") from None
    function.restype = result
    function.argtypes = arguments
    return function


def _publish_from_fd(source: int, directory: int, name: str) -> None:
    """Atomically clone an owned fd to a new name; never resolve a source path.

    macOS fclonefileat guarantees all-or-none creation and EEXIST for an
    existing destination. Unsupported systems/filesystems fail closed.
    """
    clone = _native("fclonefileat", ctypes.c_int,
                    (ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32))
    if clone(source, directory, os.fsencode(name), 0) != 0:
        raise OSError(ctypes.get_errno(), "descriptor publication failed")


def _list_xattrs(descriptor):
    """List names only, with a 64 KiB allocation cap; never read attribute values."""
    listing = _native("flistxattr", ctypes.c_ssize_t,
                      (ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int))
    size = listing(descriptor, None, 0, 0)
    if size < 0 or size > 65536:
        raise OSError(errno.EIO, "private metadata inspection failed")
    if not size:
        return ()
    buffer = ctypes.create_string_buffer(size)
    used = listing(descriptor, buffer, size, 0)
    if used <= 0 or used > size or buffer.raw[used - 1] != 0:
        raise OSError(errno.EIO, "private metadata inspection failed")
    names = tuple(buffer.raw[:used - 1].split(b"\0"))
    if not all(names) or len(names) > 1024:
        raise OSError(errno.EIO, "private metadata inspection failed")
    return names


def _remove_xattr(descriptor, name):
    remove = _native("fremovexattr", ctypes.c_int,
                     (ctypes.c_int, ctypes.c_char_p, ctypes.c_int))
    if remove(descriptor, name, 0) != 0:
        raise OSError(ctypes.get_errno(), "private metadata sanitization failed")


def _read_xattr(descriptor, name):
    reading = _native("fgetxattr", ctypes.c_ssize_t, (
        ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_uint32, ctypes.c_int))
    size = reading(descriptor, name, None, 0, 0, 0)
    if size < 0 or size > 256:
        raise OSError(errno.EIO, "private provenance inspection failed")
    if not size:
        return b""
    buffer = ctypes.create_string_buffer(size)
    if reading(descriptor, name, buffer, size, 0, 0) != size:
        raise OSError(errno.EIO, "private provenance inspection failed")
    return buffer.raw


def _provenance(descriptor):
    names = _list_xattrs(descriptor)
    if not names:
        return None
    if names != (_PROVENANCE,):
        raise HostProfileBackupError(_UNSAFE)
    return _read_xattr(descriptor, _PROVENANCE)


def _require_empty_acl(descriptor):
    # Darwin ACL_TYPE_EXTENDED=0x100, ACL_FIRST_ENTRY=0. Unlike Linux,
    # acl_get_entry returns 0 for an entry and -1/EINVAL for an empty ACL.
    get_acl = _native("acl_get_fd_np", ctypes.c_void_p, (ctypes.c_int, ctypes.c_int))
    get_entry = _native("acl_get_entry", ctypes.c_int,
                       (ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)))
    free_acl = _native("acl_free", ctypes.c_int, (ctypes.c_void_p,))
    acl = get_acl(descriptor, 0x100)
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:
            return  # Darwin reports an absent extended ACL as ENOENT.
        raise OSError(ctypes.get_errno(), "private ACL inspection failed")
    try:
        entry = ctypes.c_void_p()
        ctypes.set_errno(0)
        result = get_entry(acl, 0, ctypes.byref(entry))
        if result != -1 or ctypes.get_errno() != errno.EINVAL:
            raise HostProfileBackupError(_UNSAFE)
    finally:
        if free_acl(acl) != 0:
            raise OSError(ctypes.get_errno(), "private ACL release failed")


def _validate_private_inode(descriptor):
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.geteuid() or metadata.st_nlink != 1
            or getattr(metadata, "st_flags", None) != 0):
        raise HostProfileBackupError(_UNSAFE)


def _validate_private_metadata(descriptor, provenance):
    _validate_private_inode(descriptor)
    _require_empty_acl(descriptor)
    if _provenance(descriptor) != provenance:
        raise HostProfileBackupError(_UNSAFE)


def _sanitize_scratch(descriptor):
    _validate_private_inode(descriptor)
    _require_empty_acl(descriptor)
    for name in _list_xattrs(descriptor):
        try:
            _remove_xattr(descriptor, name)
        except OSError as error:
            if name != _PROVENANCE or error.errno not in (errno.EACCES, errno.EPERM):
                raise
    # The sole exception is OS-managed provenance that survives a removal
    # attempt. Capture its bounded value and require an exact final match.
    provenance = _provenance(descriptor)
    _validate_private_metadata(descriptor, provenance)
    return provenance


def _validate_output_parent(paths, directory, selected):
    if HostProfilePaths.for_repository(paths.repository) != paths:
        raise HostProfileBackupError(_UNSAFE)
    protected = {path.name.casefold() for path in (
        paths.profile, paths.transaction, *paths.temporary_files)}
    if selected.name.casefold() in protected:
        raise HostProfileBackupError(_UNSAFE)
    output_identity = _identity(os.fstat(directory))
    identities = {}
    for path in (paths.local, paths.history, paths.backups):
        descriptor = _open_directory(path)
        try:
            identities[path] = _identity(os.fstat(descriptor))
        finally:
            os.close(descriptor)
    if output_identity in (identities[paths.local], identities[paths.history]):
        raise HostProfileBackupError(_UNSAFE)
    if (selected.is_relative_to(paths.local)
            and output_identity != identities[paths.backups]):
        raise HostProfileBackupError(_UNSAFE)


def _open_scratch(directory):
    """Open a locked private scratch inode, never unlinking its directory entry."""
    flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(_TEMPORARY, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        created = True
    except FileExistsError:
        descriptor = os.open(_TEMPORARY, flags, dir_fd=directory)
        created = False
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink > 1
                or (not created and stat.S_IMODE(metadata.st_mode) != 0o600)):
            raise HostProfileBackupError(_UNSAFE)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if created:
            os.fchmod(descriptor, 0o600)
        provenance = _sanitize_scratch(descriptor)
        return descriptor, provenance
    except BaseException:
        os.close(descriptor)
        raise


@_bounded
def write_host_backup(
    paths: HostProfilePaths, output: Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> Path:
    """Atomically publish a new 0600 backup; every final-name collision fails.

    Both default and explicit outputs are no-clobber. No output may target
    profile/history/transaction state or the framework temporary.
    """
    backup = build_host_backup(paths, clock)
    default = output is None
    if default:
        timestamp = datetime.fromisoformat(backup.created_at.replace("Z", "+00:00"))
        output = paths.backups / f"local-web-host-{timestamp.strftime('%Y%m%dT%H%M%S.%fZ')}.json"
    selected = Path(output).absolute()
    with _parent(selected) as (directory, selected):
        _validate_output_parent(paths, directory, selected)
        existing = _metadata(directory, selected.name)
        if existing is not None:
            raise HostProfileBackupError(_UNSAFE)
        descriptor, provenance = _open_scratch(directory)
        try:
            # The scratch path is non-authoritative after open. Truncate, write,
            # sync, and publication all use the locked owned inode. Never unlink
            # by name: another writer may have replaced the directory entry.
            os.ftruncate(descriptor, 0)
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(backup.document_bytes)
                handle.flush()
                os.fsync(descriptor)
            _verify_parent(selected.parent, directory)
            _publish_from_fd(descriptor, directory, selected.name)
            os.fsync(directory)
            readback = _read(directory, selected.name, MAX_BACKUP_BYTES,
                             private_metadata=True, provenance=provenance)
            if readback != backup.document_bytes or _parse(readback) != backup:
                raise HostProfileBackupError(_UNSAFE)
        finally:
            os.close(descriptor)
    return selected
