"""Immutable, content-addressed production release and transfer contracts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any


_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_IMAGE = re.compile(r'^sha256:[0-9a-f]{64}$')
_COMMIT = re.compile(r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')
_APP = re.compile(r'^[a-z0-9]+(?:-[a-z0-9]+)*$')
_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _text(value: object) -> bool:
    return type(value) is str and bool(value) and not any(ord(c) < 32 for c in value)


def protected_name(name: str) -> bool:
    """Source examples are safe; Git metadata and actual env files are not."""
    return name == '.git' or (name.startswith('.env') and name != '.env.example')


def validate_relative_path(value: str) -> None:
    if (not _text(value) or '\\' in value or value.startswith('/')
            or any(part in ('', '.', '..') or protected_name(part) for part in value.split('/'))
            or str(PurePosixPath(value)) != value):
        raise ValueError('release path must be canonical and relative')


def _keys(value: object, expected: set[str]) -> dict:
    if type(value) is not dict or set(value) != expected:
        raise ValueError('invalid release descriptor fields')
    return value


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str
    sha256: str
    size: int
    mode: int

    def __post_init__(self) -> None:
        validate_relative_path(self.path)
        if (type(self.sha256) is not str or not _SHA256.fullmatch(self.sha256)
                or type(self.size) is not int or self.size < 0
                or type(self.mode) is not int or not 0 <= self.mode <= 0o777):
            raise ValueError('invalid release file metadata')

    def to_dict(self) -> dict[str, Any]:
        return dict(path=self.path, sha256=self.sha256, size=self.size, mode=self.mode)

    @classmethod
    def from_dict(cls, value: object) -> FileEntry:
        return cls(**_keys(value, {'path', 'sha256', 'size', 'mode'}))


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    image_digest: str
    dependency_digest: str
    os: str
    architecture: str
    abi: str
    lockfile_digest: str
    recipe_version: str

    def __post_init__(self) -> None:
        if (any(type(value) is not str or not _IMAGE.fullmatch(value)
                for value in (self.image_digest, self.dependency_digest))
                or type(self.lockfile_digest) is not str
                or not _SHA256.fullmatch(self.lockfile_digest)
                or not all(_text(value) for value in
                           (self.os, self.architecture, self.abi, self.recipe_version))):
            raise ValueError('invalid runtime identity')

    def to_dict(self) -> dict[str, str]:
        return dict(imageDigest=self.image_digest, dependencyDigest=self.dependency_digest,
                    os=self.os, architecture=self.architecture, abi=self.abi,
                    lockfileDigest=self.lockfile_digest, recipeVersion=self.recipe_version)

    @classmethod
    def from_dict(cls, value: object) -> RuntimeIdentity:
        value = _keys(value, {'imageDigest', 'dependencyDigest', 'os', 'architecture',
                             'abi', 'lockfileDigest', 'recipeVersion'})
        return cls(value['imageDigest'], value['dependencyDigest'], value['os'],
                   value['architecture'], value['abi'], value['lockfileDigest'],
                   value['recipeVersion'])


@dataclass(frozen=True, slots=True)
class ReleaseDescriptorV1:
    app_id: str
    source_commit: str
    recipe_version: str
    runtime: RuntimeIdentity | None
    files: tuple[FileEntry, ...]
    data_contract: str
    rollback_policy: str

    def __post_init__(self) -> None:
        if (type(self.app_id) is not str or not _APP.fullmatch(self.app_id)
                or type(self.source_commit) is not str or not _COMMIT.fullmatch(self.source_commit)
                or not _text(self.recipe_version) or not _text(self.data_contract)
                or self.rollback_policy not in ('compatible', 'migration-required')
                or self.runtime is not None and type(self.runtime) is not RuntimeIdentity
                or type(self.files) is not tuple or any(type(f) is not FileEntry for f in self.files)):
            raise ValueError('invalid release descriptor')
        ordered = tuple(sorted(self.files, key=lambda f: f.path))
        names: set[str] = set()
        blobs: dict[str, int] = {}
        for entry in ordered:
            if entry.path in names or any(str(parent) in names for parent in PurePosixPath(entry.path).parents):
                raise ValueError('duplicate or overlapping release paths')
            if entry.sha256 in blobs and blobs[entry.sha256] != entry.size:
                raise ValueError('inconsistent blob size')
            names.add(entry.path)
            blobs[entry.sha256] = entry.size
        object.__setattr__(self, 'files', ordered)

    def to_dict(self) -> dict[str, Any]:
        return dict(schemaVersion=1, appId=self.app_id, sourceCommit=self.source_commit,
                    recipeVersion=self.recipe_version,
                    runtime=self.runtime.to_dict() if self.runtime else None,
                    files=[f.to_dict() for f in self.files], dataContract=self.data_contract,
                    rollbackPolicy=self.rollback_policy)

    def canonical_bytes(self) -> bytes:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(',', ':'),
                          ensure_ascii=False, allow_nan=False).encode('utf-8')

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, value: object) -> ReleaseDescriptorV1:
        value = _keys(value, {'schemaVersion', 'appId', 'sourceCommit', 'recipeVersion',
                             'runtime', 'files', 'dataContract', 'rollbackPolicy'})
        if type(value['schemaVersion']) is not int or value['schemaVersion'] != 1 or type(value['files']) is not list:
            raise ValueError('unsupported release descriptor schema')
        return cls(value['appId'], value['sourceCommit'], value['recipeVersion'],
                   RuntimeIdentity.from_dict(value['runtime']) if value['runtime'] is not None else None,
                   tuple(FileEntry.from_dict(f) for f in value['files']),
                   value['dataContract'], value['rollbackPolicy'])


def snapshot_files(root: Path) -> tuple[FileEntry, ...]:
    """Hash a regular-file tree without following directory or file symlinks."""
    entries: list[FileEntry] = []

    def visit(directory: int, prefix: str) -> None:
        for name in sorted(os.listdir(directory)):
            relative = prefix + name
            validate_relative_path(relative)
            before = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, _DIRECTORY, dir_fd=directory)
                try:
                    if os.fstat(child).st_ino != before.st_ino:
                        raise ValueError('release tree changed during snapshot')
                    visit(child, relative + '/')
                finally:
                    os.close(child)
            elif stat.S_ISREG(before.st_mode):
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
                try:
                    opened = os.fstat(descriptor)
                    if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                        raise ValueError('release tree changed during snapshot')
                    digest = hashlib.sha256()
                    count = 0
                    while chunk := os.read(descriptor, 1024 * 1024):
                        digest.update(chunk)
                        count += len(chunk)
                    after = os.fstat(descriptor)
                    if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or count != opened.st_size:
                        raise ValueError('release file changed during snapshot')
                    entries.append(FileEntry(relative, digest.hexdigest(), count, stat.S_IMODE(opened.st_mode)))
                finally:
                    os.close(descriptor)
            else:
                raise ValueError('release tree contains a link or special file')

    try:
        descriptor = os.open(root, _DIRECTORY)
        try:
            visit(descriptor, '')
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ValueError('cannot snapshot release tree') from error
    return tuple(sorted(entries, key=lambda f: f.path))


@dataclass(frozen=True, slots=True)
class TransferPlan:
    missing_files: tuple[FileEntry, ...]
    missing_bytes: int
    runtime_required: bool


def plan_transfer(descriptor: ReleaseDescriptorV1, verified_blob_digests: frozenset[str],
                  cached_runtimes: tuple[RuntimeIdentity, ...]) -> TransferPlan:
    """Plan against verified completed objects only; deduplicate equal blob bytes."""
    seen = set(verified_blob_digests)
    missing = []
    for entry in descriptor.files:
        if entry.sha256 not in seen:
            missing.append(entry)
            seen.add(entry.sha256)
    return TransferPlan(tuple(missing), sum(entry.size for entry in missing),
                        descriptor.runtime is not None and descriptor.runtime not in cached_runtimes)
