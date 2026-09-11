"""Canonical paths and secure filesystem primitives for private host state."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .git_runner import GitRunnerError, run_git


class HostProfileError(ValueError):
    """The private host profile cannot be selected safely."""


_UNSAFE_STATE = "private host profile state is unsafe"
_MISSING_PROFILE = (
    "private host profile is missing; run local-web host init or "
    "local-web host restore"
)
_PUBLIC_SCAFFOLD_NAMES = frozenset((".gitignore", "README.md"))


@dataclass(frozen=True, slots=True)
class HostProfilePaths:
    """Framework-owned paths beneath one canonical platform repository."""

    repository: Path
    repository_device: int
    repository_inode: int
    config: Path
    local: Path
    profile: Path
    history: Path
    backups: Path
    transaction: Path
    profile_temporary: Path
    revision_temporary: Path
    transaction_temporary: Path
    backup_temporary: Path

    @classmethod
    def for_repository(
        cls, repository: Path, *, allow_public_scaffold: bool = False
    ) -> HostProfilePaths:
        if type(allow_public_scaffold) is not bool:
            raise HostProfileError(_UNSAFE_STATE)
        root = _resolve_repository(Path(repository))
        try:
            metadata = root.lstat()
        except OSError as error:
            raise HostProfileError("platform repository is invalid") from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise HostProfileError("platform repository is invalid")
        local = root / "config/local"
        paths = cls(
            repository=root,
            repository_device=metadata.st_dev,
            repository_inode=metadata.st_ino,
            config=root / "config",
            local=local,
            profile=local / "apps.json",
            history=local / "history",
            backups=local / "backups",
            transaction=local / ".host-profile-transaction.json",
            profile_temporary=local / ".apps.json.tmp",
            revision_temporary=local / "history/.revision.json.tmp",
            transaction_temporary=local / ".host-profile-transaction.json.tmp",
            backup_temporary=local / "backups/.host-profile-backup.json.tmp",
        )
        _validate_existing_components(
            paths, allow_public_scaffold=allow_public_scaffold
        )
        return paths

    @property
    def temporary_files(self) -> tuple[Path, ...]:
        return (
            self.profile_temporary,
            self.revision_temporary,
            self.transaction_temporary,
            self.backup_temporary,
        )

    @property
    def recovery_residue(self) -> Path:
        return self.local / ".host-profile-recovery"


def profile_digest(content: bytes) -> str:
    """Return the lowercase SHA-256 digest of the exact profile bytes."""

    return hashlib.sha256(content).hexdigest()


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    """Encode a mapping as stable compact UTF-8 JSON with one final newline."""

    return (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def validate_secure_directory(
    path: Path, *, root: Path, required: bool = True
) -> None:
    """Require an in-root, non-symlinked directory with mode ``0700``."""

    candidate = Path(path)
    boundary = Path(root)
    _validate_containment(candidate, boundary)
    _validate_existing_ancestors(candidate, boundary)
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        if required:
            raise HostProfileError(_UNSAFE_STATE) from None
        return
    except OSError as error:
        raise HostProfileError(_UNSAFE_STATE) from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
        raise HostProfileError(_UNSAFE_STATE)


def ensure_secure_directory(path: Path, *, root: Path) -> None:
    """Create an absent private directory as ``0700`` and revalidate it."""

    candidate = Path(path)
    boundary = Path(root)
    _validate_containment(candidate, boundary)
    try:
        parent_descriptor, name = _open_parent_directory(candidate, boundary)
    except OSError as error:
        raise HostProfileError(_UNSAFE_STATE) from error
    try:
        created = False
        try:
            metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_descriptor)
                created = True
            except FileExistsError:
                pass
            if created:
                os.chmod(
                    name,
                    0o700,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            metadata = os.stat(
                name, dir_fd=parent_descriptor, follow_symlinks=False
            )
        except OSError as error:
            raise HostProfileError(_UNSAFE_STATE) from error
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise HostProfileError(_UNSAFE_STATE)
        if created:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            try:
                descriptor = os.open(name, flags, dir_fd=parent_descriptor)
                try:
                    os.fchmod(descriptor, 0o700)
                    metadata = os.fstat(descriptor)
                finally:
                    os.close(descriptor)
            except OSError as error:
                raise HostProfileError(_UNSAFE_STATE) from error
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) != 0o700
            ):
                raise HostProfileError(_UNSAFE_STATE)
    finally:
        os.close(parent_descriptor)
    validate_secure_directory(candidate, root=boundary)


def validate_secure_file(path: Path, *, root: Path, required: bool = True) -> None:
    """Require an in-root, non-symlinked regular file with mode ``0600``."""

    candidate = Path(path)
    boundary = Path(root)
    _validate_containment(candidate, boundary)
    _validate_existing_ancestors(candidate, boundary)
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        if required:
            raise HostProfileError(_UNSAFE_STATE) from None
        return
    except OSError as error:
        raise HostProfileError(_UNSAFE_STATE) from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise HostProfileError(_UNSAFE_STATE)


def ensure_private_directories(paths: HostProfilePaths) -> None:
    """Create and verify all framework-owned private directories."""

    for directory in (paths.local, paths.history, paths.backups):
        ensure_secure_directory(directory, root=paths.repository)


def resolve_host_profile(repository: Path) -> Path:
    """Resolve and validate the canonical private profile for a repository."""

    paths = HostProfilePaths.for_repository(repository)
    try:
        paths.profile.lstat()
    except FileNotFoundError:
        raise HostProfileError(_MISSING_PROFILE) from None
    except OSError as error:
        raise HostProfileError(_UNSAFE_STATE) from error
    validate_secure_directory(paths.local, root=paths.repository)
    validate_secure_directory(paths.history, root=paths.repository)
    validate_secure_directory(paths.backups, root=paths.repository)
    validate_secure_file(paths.profile, root=paths.repository)
    return paths.profile


def _resolve_repository(repository: Path) -> Path:
    try:
        root = repository.resolve(strict=True)
        metadata = root.lstat()
    except OSError as error:
        raise HostProfileError("platform repository is invalid") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise HostProfileError("platform repository is invalid")
    try:
        result = run_git(
            root,
            ("rev-parse", "--show-toplevel"),
            maximum_stdout=8192,
        )
        if result.returncode != 0 or not result.stdout.endswith("\n"):
            raise GitRunnerError("Git inspection failed")
        git_root = Path(result.stdout.strip()).resolve(strict=True)
    except (OSError, GitRunnerError) as error:
        raise HostProfileError("platform repository is invalid") from error
    if git_root != root:
        raise HostProfileError("platform repository must be a repository root")
    return root


def _validate_existing_components(
    paths: HostProfilePaths, *, allow_public_scaffold: bool = False
) -> None:
    _validate_public_config(paths.config, paths.repository)
    try:
        local_metadata = paths.local.lstat()
    except FileNotFoundError:
        local_metadata = None
    except OSError as error:
        raise HostProfileError(_UNSAFE_STATE) from error
    if (
        allow_public_scaffold
        and local_metadata is not None
        and stat.S_IMODE(local_metadata.st_mode) != 0o700
    ):
        descriptor = _open_public_scaffold(paths.local, paths.repository)
        os.close(descriptor)
    else:
        validate_secure_directory(
            paths.local, root=paths.repository, required=False
        )
    for directory in (paths.history, paths.backups, paths.recovery_residue):
        validate_secure_directory(directory, root=paths.repository, required=False)
    for private_file in (
        paths.profile,
        paths.transaction,
        *paths.temporary_files,
    ):
        validate_secure_file(private_file, root=paths.repository, required=False)


def _open_public_scaffold(path: Path, root: Path) -> int:
    """Open an exact public-only scaffold without treating it as private state."""

    _validate_containment(path, root)
    descriptor = None
    try:
        parent, name = _open_parent_directory(path, root)
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent,
            )
        finally:
            os.close(parent)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise HostProfileError(_UNSAFE_STATE)
        with os.scandir(descriptor) as entries:
            names = []
            for entry in entries:
                names.append(entry.name)
                if len(names) > len(_PUBLIC_SCAFFOLD_NAMES):
                    raise HostProfileError(_UNSAFE_STATE)
        if not set(names).issubset(_PUBLIC_SCAFFOLD_NAMES):
            raise HostProfileError(_UNSAFE_STATE)
        for name in names:
            child = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (
                not stat.S_ISREG(child.st_mode)
                or child.st_uid != os.geteuid()
                or child.st_nlink != 1
                or stat.S_IMODE(child.st_mode) & 0o022
            ):
                raise HostProfileError(_UNSAFE_STATE)
        return descriptor
    except BaseException:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise


def _validate_public_config(path: Path, root: Path) -> None:
    _validate_containment(path, root)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise HostProfileError("platform repository configuration is invalid") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise HostProfileError("platform repository configuration is invalid")


def _validate_containment(path: Path, root: Path) -> None:
    if not path.is_absolute() or not root.is_absolute():
        raise HostProfileError(_UNSAFE_STATE)
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise HostProfileError(_UNSAFE_STATE) from None
    if not relative.parts or ".." in relative.parts:
        raise HostProfileError(_UNSAFE_STATE)


def _validate_existing_ancestors(path: Path, root: Path) -> None:
    relative = path.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise HostProfileError(_UNSAFE_STATE) from error
        if not stat.S_ISDIR(metadata.st_mode):
            raise HostProfileError(_UNSAFE_STATE)


def _open_parent_directory(path: Path, root: Path) -> tuple[int, str]:
    relative = path.relative_to(root)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, flags)
    try:
        for part in relative.parts[:-1]:
            child_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child_descriptor
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, relative.name
