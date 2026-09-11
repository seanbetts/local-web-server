"""Build a deterministic, public-only npm package for the shared UI library."""

from __future__ import annotations

import ctypes
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, NoReturn


_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOSE_ON_EXEC = getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | _NO_FOLLOW | _CLOSE_ON_EXEC
_REGULAR_FLAGS = os.O_RDONLY | _NO_FOLLOW | _CLOSE_ON_EXEC
_ARTIFACT_PARTS = ("artifacts", "local-web-ui-0.7.0.tgz")
_REPRODUCIBILITY_ARTIFACT_PARTS = ("artifacts", "local-web-ui-0.7.0-second.tgz")
_LEGACY_ARTIFACT_PARTS = ("artifacts", "local-web-ui.tgz")
CURRENT_UI_PACKAGE_VERSION = "0.7.0"
_NPM_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
_BUILD_TIMEOUT_SECONDS = 60
_PROCESS_GROUP_GRACE_SECONDS = 0.1
_GIT_TIMEOUT_SECONDS = 15
_MAX_GIT_FILE_BYTES = 2 * 1024 * 1024
_MAX_GIT_TREE_BYTES = 256 * 1024
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAME_EXCL = 0x00000004
_AT_REMOVEDIR = 0x0080
_PUBLIC_DIST_FILES = frozenset(
    {
        "AppShell.d.ts",
        "ContextExportButton.d.ts",
        "Icon.d.ts",
        "InteractiveExportControl.d.ts",
        "PlatformShell.d.ts",
        "SegmentedControl.d.ts",
        "ThemeControl.d.ts",
        "actions.d.ts",
        "colour.d.ts",
        "colourMode.d.ts",
        "content.d.ts",
        "contextExportDocument.d.ts",
        "contextExportModel.d.ts",
        "feedback.d.ts",
        "fallback-theme.css",
        "focus.d.ts",
        "forms.d.ts",
        "index.d.ts",
        "index.js",
        "interactiveExportDocument.js",
        "interactiveExportDocument.d.ts",
        "interactiveExportEnvironment.d.ts",
        "interactiveExportModel.d.ts",
        "interactiveExportVite.d.ts",
        "layout.d.ts",
        "overlays.d.ts",
        "styles.css",
        "vite.d.ts",
        "vite-env.d.ts",
        "vite.js",
    }
)
_PUBLIC_METADATA_KEYS = frozenset(
    {"name", "version", "private", "type", "exports", "files", "peerDependencies", "peerDependenciesMeta"}
)
_PUBLIC_PEERS = {"react": "19.2.8", "react-dom": "19.2.8", "vite": "^8.2.0"}
_PUBLIC_PEER_METADATA = {"vite": {"optional": True}}


class UiPackageError(ValueError):
    """The public UI package could not be built safely."""


@dataclass(frozen=True)
class UiPackageArtifact:
    """The immutable result of one UI-package build."""

    version: str
    sha256: str
    path: Path


def _before_descriptor_close() -> None:
    """Fault-injection boundary that runs before, and cannot receive, the fd."""


def _native_close(descriptor: int) -> None:
    """Make the sole authoritative close attempt for an owned descriptor."""
    close_function = _LIBC.close
    close_function.argtypes = [ctypes.c_int]
    close_function.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = close_function(descriptor)
    if result != 0:
        error = OSError(ctypes.get_errno(), "native descriptor close failed")
        raise _cleanup_error() from error


def _close_descriptor(descriptor: int) -> None:
    """Relinquish one owned descriptor after exactly one native close attempt."""
    hook_failure: UiPackageError | None = None
    close_failure: UiPackageError | None = None
    try:
        _before_descriptor_close()
    except Exception as error:
        hook_failure = _cleanup_error()
        hook_failure.__cause__ = error
    finally:
        try:
            _native_close(descriptor)
        except UiPackageError as error:
            close_failure = error
    if hook_failure is not None:
        if close_failure is not None:
            raise hook_failure from close_failure
        raise hook_failure
    if close_failure is not None:
        raise close_failure


def _close_owned_descriptors(descriptors: list[int]) -> None:
    """Relinquish every descriptor in reverse ownership order exactly once."""
    failure: UiPackageError | None = None
    while descriptors:
        descriptor = descriptors.pop()
        try:
            _close_descriptor(descriptor)
        except UiPackageError as error:
            if failure is None:
                failure = error
    if failure is not None:
        raise failure


def _run_bounded_cleanup(cleanup: Callable[[], None]) -> None:
    try:
        cleanup()
    except UiPackageError:
        raise
    except Exception as error:
        raise _cleanup_error() from error


def _raise_primary_after_cleanup(
    primary: BaseException, cleanup: Callable[[], None]
) -> NoReturn:
    try:
        _run_bounded_cleanup(cleanup)
    except UiPackageError as cleanup_error:
        raise primary from cleanup_error
    raise primary


@contextmanager
def _cleanup_preserving_primary(cleanup: Callable[[], None]) -> Iterator[None]:
    try:
        yield
    except BaseException as primary:
        _raise_primary_after_cleanup(primary, cleanup)
    else:
        _run_bounded_cleanup(cleanup)


@dataclass
class _PinnedDirectory:
    path: Path
    descriptors: list[int]

    @property
    def fd(self) -> int:
        return self.descriptors[-1]

    def close(self) -> None:
        _close_owned_descriptors(self.descriptors)


@dataclass
class _PinnedUiTree:
    repository_fd: int
    packages_fd: int
    package_fd: int
    dist_fd: int

    @classmethod
    def open(cls, repository_fd: int) -> "_PinnedUiTree":
        packages_fd = _open_directory_at(repository_fd, "packages")
        try:
            package_fd = _open_directory_at(packages_fd, "ui")
            try:
                dist_fd = _open_directory_at(package_fd, "dist")
                return cls(repository_fd, packages_fd, package_fd, dist_fd)
            except Exception as primary:
                _raise_primary_after_cleanup(
                    primary, lambda: _close_descriptor(package_fd)
                )
        except Exception as primary:
            _raise_primary_after_cleanup(
                primary, lambda: _close_descriptor(packages_fd)
            )

    def ensure_bound(self) -> None:
        try:
            packages = os.stat("packages", dir_fd=self.repository_fd, follow_symlinks=False)
            package = os.stat("ui", dir_fd=self.packages_fd, follow_symlinks=False)
            dist = os.stat("dist", dir_fd=self.package_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(packages.st_mode)
                or not stat.S_ISDIR(package.st_mode)
                or not stat.S_ISDIR(dist.st_mode)
                or not _same_entry(packages, os.fstat(self.packages_fd))
                or not _same_entry(package, os.fstat(self.package_fd))
                or not _same_entry(dist, os.fstat(self.dist_fd))
            ):
                raise _validation_error()
        except UiPackageError:
            raise
        except OSError as error:
            raise _validation_error() from error

    def close(self) -> None:
        descriptors: list[int] = []
        for attribute in ("packages_fd", "package_fd", "dist_fd"):
            descriptor = getattr(self, attribute)
            if descriptor != -1:
                setattr(self, attribute, -1)
                descriptors.append(descriptor)
        _close_owned_descriptors(descriptors)


@dataclass(frozen=True)
class _ArchiveEntry:
    path: str
    is_directory: bool
    content: bytes = b""


@dataclass
class _OutputTarget:
    path: Path
    name: str
    parent_fd: int
    owned_directory: _PinnedDirectory | None

    def close(self) -> None:
        if self.owned_directory is not None:
            owned_directory = self.owned_directory
            self.owned_directory = None
            self.parent_fd = -1
            owned_directory.close()
            return
        if self.parent_fd != -1:
            descriptor = self.parent_fd
            self.parent_fd = -1
            _close_descriptor(descriptor)


def _validation_error() -> UiPackageError:
    return UiPackageError("UI package validation failed")


def _build_error() -> UiPackageError:
    return UiPackageError("UI package build failed")


def _output_error() -> UiPackageError:
    return UiPackageError("UI package output failed")


def _cleanup_error() -> UiPackageError:
    return UiPackageError("UI package cleanup failed")


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _same_entry(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _require_directory(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise _validation_error()


def _open_directory_at(parent_fd: int, name: str) -> int:
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _require_directory(expected)
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        if not _same_entry(expected, os.fstat(descriptor)):
            _raise_primary_after_cleanup(
                _validation_error(), lambda: _close_descriptor(descriptor)
            )
        return descriptor
    except UiPackageError:
        raise
    except OSError as error:
        raise _validation_error() from error


def _pin_directory(path: Path) -> _PinnedDirectory:
    """Descriptor-walk an absolute path without trusting any ancestor pathname."""
    absolute = _absolute(path)
    descriptors: list[int] = []
    try:
        root = os.open(absolute.anchor, _DIRECTORY_FLAGS)
        descriptors.append(root)
        current = root
        for component in absolute.parts[1:]:
            current = _open_directory_at(current, component)
            descriptors.append(current)
        return _PinnedDirectory(absolute, descriptors)
    except UiPackageError as primary:
        try:
            _close_owned_descriptors(descriptors)
        except UiPackageError as cleanup:
            raise primary from cleanup
        raise
    except OSError as error:
        primary = _validation_error()
        try:
            _close_owned_descriptors(descriptors)
        except UiPackageError as cleanup:
            raise primary from cleanup
        raise primary from error


def _read_regular(parent_fd: int, name: str) -> bytes:
    try:
        expected = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(expected.st_mode) or not stat.S_ISREG(expected.st_mode):
            raise _validation_error()
        descriptor = os.open(name, _REGULAR_FLAGS, dir_fd=parent_fd)
        with _cleanup_preserving_primary(lambda: _close_descriptor(descriptor)):
            opened = os.fstat(descriptor)
            if not _same_entry(expected, opened) or opened.st_nlink != 1:
                raise _validation_error()
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            content = b"".join(chunks)
            final = os.fstat(descriptor)
            if (
                not _same_entry(opened, final)
                or final.st_nlink != 1
                or final.st_size != len(content)
            ):
                raise _validation_error()
            return content
    except UiPackageError:
        raise
    except OSError as error:
        raise _validation_error() from error


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise _validation_error()
        result[key] = value
    return result


def _canonical_metadata(content: bytes) -> tuple[str, bytes]:
    try:
        metadata = json.loads(content.decode("utf-8"), object_pairs_hook=_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        if isinstance(error, UiPackageError):
            raise
        raise _validation_error() from error
    if not isinstance(metadata, dict) or set(metadata) != _PUBLIC_METADATA_KEYS:
        raise _validation_error()
    if metadata["name"] != "@local-web/ui" or metadata["version"] != CURRENT_UI_PACKAGE_VERSION:
        raise _validation_error()
    if metadata["private"] is not True or metadata["type"] != "module":
        raise _validation_error()
    if metadata["files"] != ["dist"] or metadata["peerDependencies"] != _PUBLIC_PEERS:
        raise _validation_error()
    if metadata.get("peerDependenciesMeta", {}) != _PUBLIC_PEER_METADATA:
        raise _validation_error()
    exports = metadata["exports"]
    expected_exports = {
        ".": {"types": "./dist/index.d.ts", "import": "./dist/index.js"},
        "./styles.css": "./dist/styles.css",
        "./vite": {"types": "./dist/vite.d.ts", "import": "./dist/vite.js"},
    }
    if exports != expected_exports:
        raise _validation_error()
    canonical = json.dumps(
        metadata,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii") + b"\n"
    return metadata["version"], canonical


def _snapshot_dist(dist_fd: int) -> list[_ArchiveEntry]:
    try:
        listed = {entry.name: entry.stat(follow_symlinks=False) for entry in os.scandir(dist_fd)}
    except OSError as error:
        raise _validation_error() from error
    if set(listed) != _PUBLIC_DIST_FILES:
        raise _validation_error()
    entries = [_ArchiveEntry("package/dist", is_directory=True)]
    for name in sorted(_PUBLIC_DIST_FILES):
        info = listed[name]
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise _validation_error()
        content = _read_regular(dist_fd, name)
        entries.append(_ArchiveEntry(f"package/dist/{name}", is_directory=False, content=content))
    return entries


def _snapshot_package(tree: _PinnedUiTree) -> tuple[str, bytes, list[_ArchiveEntry]]:
    tree.ensure_bound()
    metadata = _read_regular(tree.package_fd, "package.json")
    version, canonical_metadata = _canonical_metadata(metadata)
    entries = [_ArchiveEntry("package", is_directory=True)]
    entries.append(_ArchiveEntry("package/package.json", is_directory=False, content=canonical_metadata))
    entries.extend(_snapshot_dist(tree.dist_fd))
    return version, canonical_metadata, sorted(entries, key=lambda entry: entry.path)


_GIT_OBJECT = re.compile(rb"^[0-9a-f]{40,64}$")
_REQUIRED_BUILD_PATHS = frozenset(
    {
        "package.json",
        "package-lock.json",
        "packages/ui/package.json",
        "packages/ui/tsconfig.json",
        "packages/ui/vite.config.ts",
    }
)
_SOURCE_PREFIX = "packages/ui/src/"
_SOURCE_SUFFIXES = (".ts", ".tsx", ".css")


def _is_build_source(path: str) -> bool:
    if not path.startswith(_SOURCE_PREFIX) or not path.endswith(_SOURCE_SUFFIXES):
        return False
    relative = path[len(_SOURCE_PREFIX) :]
    return (
        not relative.startswith("test/")
        and not relative.endswith(".test.ts")
        and not relative.endswith(".test.tsx")
    )


def _resolve_executable(name: str) -> str:
    candidate = shutil.which(name, path=_NPM_PATH)
    if candidate is None:
        raise _build_error()
    try:
        resolved = Path(candidate).resolve(strict=True)
        info = os.stat(resolved)
    except OSError as error:
        raise _build_error() from error
    if not stat.S_ISREG(info.st_mode) or not (info.st_mode & stat.S_IXUSR):
        raise _build_error()
    return str(resolved)


def _git_environment() -> dict[str, str]:
    return {
        "PATH": _NPM_PATH,
        "HOME": "/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_DIR": ".",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }


def _exec_from_descriptor_argv(directory_fd: int, argv: list[str]) -> list[str]:
    helper = (
        "import os,sys; "
        "descriptor=int(sys.argv[1]); "
        "os.fchdir(descriptor); "
        "os.close(descriptor); "
        "os.execvpe(sys.argv[2], sys.argv[2:], os.environ)"
    )
    return [sys.executable, "-c", helper, str(directory_fd), *argv]


def _process_group_alive(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _poll_process_group(process_group: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _process_group_alive(process_group):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    """Stop a whole process group even after its leader has already exited."""
    process_group = process.pid
    if not _process_group_alive(process_group):
        return
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as error:
        raise _build_error() from error
    if process.poll() is None:
        try:
            process.wait(timeout=_PROCESS_GROUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    if _poll_process_group(process_group, _PROCESS_GROUP_GRACE_SECONDS):
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError as error:
        raise _build_error() from error
    if process.poll() is None:
        try:
            process.wait(timeout=_PROCESS_GROUP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    if not _poll_process_group(process_group, _PROCESS_GROUP_GRACE_SECONDS):
        raise _build_error()


def _run_captured(directory_fd: int, argv: list[str], maximum: int) -> bytes:
    try:
        process = subprocess.Popen(
            _exec_from_descriptor_argv(directory_fd, argv),
            close_fds=True,
            env=_git_environment(),
            stderr=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            pass_fds=(directory_fd,),
            start_new_session=True,
        )
        try:
            output, _ = process.communicate(timeout=_GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as error:
            _stop_process_group(process)
            raise _validation_error() from error
        _stop_process_group(process)
        if process.returncode != 0 or len(output) > maximum:
            raise _validation_error()
        return output
    except UiPackageError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise _validation_error() from error


def _pin_git_directory(repository: _PinnedDirectory) -> _PinnedDirectory:
    try:
        info = os.stat(".git", dir_fd=repository.fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            descriptor = _open_directory_at(repository.fd, ".git")
            return _PinnedDirectory(repository.path / ".git", [descriptor])
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
            raise _validation_error()
        pointer = _read_regular(repository.fd, ".git").decode("utf-8")
        if not pointer.startswith("gitdir: "):
            raise _validation_error()
        target_text = pointer[8:].strip()
        if not target_text or "\x00" in target_text:
            raise _validation_error()
        target = Path(target_text)
        if not target.is_absolute():
            target = repository.path / target
        return _pin_directory(target)
    except (UnicodeDecodeError, OSError) as error:
        raise _validation_error() from error


@dataclass
class _GitAuthority:
    directory: _PinnedDirectory
    commit: str
    tree: str
    sources: dict[str, bytes]

    def close(self) -> None:
        self.directory.close()


def _load_git_authority(repository: _PinnedDirectory) -> _GitAuthority:
    git_directory = _pin_git_directory(repository)
    git = _resolve_executable("git")
    try:
        commit_bytes = _run_captured(
            git_directory.fd,
            [
                git,
                "--no-replace-objects",
                "--shallow-file",
                "",
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ],
            128,
        ).strip()
        if not _GIT_OBJECT.fullmatch(commit_bytes):
            raise _validation_error()
        commit = commit_bytes.decode("ascii")
        tree_bytes = _run_captured(
            git_directory.fd,
            [
                git,
                "--no-replace-objects",
                "--shallow-file",
                "",
                "rev-parse",
                "--verify",
                f"{commit}^{{tree}}",
            ],
            128,
        ).strip()
        if not _GIT_OBJECT.fullmatch(tree_bytes):
            raise _validation_error()
        tree_object = tree_bytes.decode("ascii")
        tree = _run_captured(
            git_directory.fd,
            [
                git,
                "--no-replace-objects",
                "--shallow-file",
                "",
                "ls-tree",
                "-r",
                "-z",
                tree_object,
                "--",
                "package.json",
                "package-lock.json",
                "packages/ui",
            ],
            _MAX_GIT_TREE_BYTES,
        )
        objects: dict[str, str] = {}
        for record in tree.rstrip(b"\0").split(b"\0") if tree else []:
            header, separator, path_bytes = record.partition(b"\t")
            parts = header.split(b" ")
            if not separator or len(parts) != 3:
                raise _validation_error()
            mode, kind, object_name = parts
            try:
                path = path_bytes.decode("utf-8")
            except UnicodeDecodeError as error:
                raise _validation_error() from error
            pure = PurePosixPath(path)
            if (
                mode != b"100644"
                or kind != b"blob"
                or not _GIT_OBJECT.fullmatch(object_name)
                or pure.is_absolute()
                or ".." in pure.parts
            ):
                raise _validation_error()
            if path in _REQUIRED_BUILD_PATHS or _is_build_source(path):
                objects[path] = object_name.decode("ascii")
        if not _REQUIRED_BUILD_PATHS.issubset(objects) or not any(
            _is_build_source(path) for path in objects
        ):
            raise _validation_error()
        sources: dict[str, bytes] = {}
        for path, object_name in sorted(objects.items()):
            sources[path] = _run_captured(
                git_directory.fd,
                [
                    git,
                    "--no-replace-objects",
                    "--shallow-file",
                    "",
                    "cat-file",
                    "blob",
                    object_name,
                ],
                _MAX_GIT_FILE_BYTES,
            )
        _canonical_metadata(sources["packages/ui/package.json"])
        return _GitAuthority(git_directory, commit, tree_object, sources)
    except Exception as primary:
        _raise_primary_after_cleanup(primary, git_directory.close)


def _read_path_at(root_fd: int, path: str) -> bytes:
    parts = PurePosixPath(path).parts
    descriptors: list[int] = []
    current = root_fd
    with _cleanup_preserving_primary(
        lambda: _close_owned_descriptors(descriptors)
    ):
        for component in parts[:-1]:
            current = _open_directory_at(current, component)
            descriptors.append(current)
        return _read_regular(current, parts[-1])


def _list_live_sources(directory_fd: int, prefix: str = _SOURCE_PREFIX) -> set[str]:
    paths: set[str] = set()
    try:
        children = sorted(os.scandir(directory_fd), key=lambda child: child.name)
        for child in children:
            path = f"{prefix}{child.name}"
            info = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                if path == f"{_SOURCE_PREFIX}test":
                    continue
                child_fd = _open_directory_at(directory_fd, child.name)
                with _cleanup_preserving_primary(
                    lambda: _close_descriptor(child_fd)
                ):
                    paths.update(_list_live_sources(child_fd, f"{path}/"))
            elif _is_build_source(path):
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise _validation_error()
                paths.add(path)
        return paths
    except UiPackageError:
        raise
    except OSError as error:
        raise _validation_error() from error


def _verify_worktree_inputs(repository_fd: int, sources: dict[str, bytes]) -> None:
    """Require every executable worktree input to equal the pinned commit."""
    for path, expected in sources.items():
        if _read_path_at(repository_fd, path) != expected:
            raise _validation_error()
    source_fd = repository_fd
    opened: list[int] = []
    with _cleanup_preserving_primary(lambda: _close_owned_descriptors(opened)):
        for component in ("packages", "ui", "src"):
            source_fd = _open_directory_at(source_fd, component)
            opened.append(source_fd)
        live_sources = _list_live_sources(source_fd)
    committed_sources = {path for path in sources if _is_build_source(path)}
    if live_sources != committed_sources:
        raise _validation_error()


def _ensure_stage_directory(parent_fd: int, name: str) -> int:
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        return _open_directory_at(parent_fd, name)
    except UiPackageError:
        raise
    except OSError as error:
        raise _validation_error() from error


def _write_stage_file(parent_fd: int, name: str, content: bytes) -> None:
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NO_FOLLOW | _CLOSE_ON_EXEC,
            0o600,
            dir_fd=parent_fd,
        )
        with _cleanup_preserving_primary(lambda: _close_descriptor(descriptor)):
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
    except UiPackageError as error:
        raise _validation_error() from error
    except OSError as error:
        raise _validation_error() from error


def _write_sources_to_stage(stage_fd: int, sources: dict[str, bytes]) -> None:
    directories: dict[tuple[str, ...], int] = {(): stage_fd}
    owned: list[int] = []
    with _cleanup_preserving_primary(lambda: _close_owned_descriptors(owned)):
        for path, content in sorted(sources.items()):
            parts = PurePosixPath(path).parts
            prefix: tuple[str, ...] = ()
            parent_fd = stage_fd
            for component in parts[:-1]:
                next_prefix = prefix + (component,)
                if next_prefix not in directories:
                    directories[next_prefix] = _ensure_stage_directory(parent_fd, component)
                    owned.append(directories[next_prefix])
                parent_fd = directories[next_prefix]
                prefix = next_prefix
            _write_stage_file(parent_fd, parts[-1], content)


def _native_rename_exclusive(parent_fd: int, old_name: str, new_name: str) -> None:
    """Atomically capture a same-parent name without replacing another entry."""
    if (
        not old_name
        or not new_name
        or "/" in old_name
        or "/" in new_name
        or old_name in {".", ".."}
        or new_name in {".", ".."}
    ):
        raise _cleanup_error()
    function = getattr(_LIBC, "renameatx_np", None)
    if function is None:
        raise _cleanup_error()
    function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    result = function(
        parent_fd,
        os.fsencode(old_name),
        parent_fd,
        os.fsencode(new_name),
        _RENAME_EXCL,
    )
    if result != 0:
        raise _cleanup_error()


def _native_terminal_unlink(parent_fd: int, name: str, *, is_directory: bool) -> None:
    function = _LIBC.unlinkat
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    function.restype = ctypes.c_int
    ctypes.set_errno(0)
    flags = _AT_REMOVEDIR if is_directory else 0
    if function(parent_fd, os.fsencode(name), flags) != 0:
        raise _cleanup_error()


def _terminal_remove_at(
    parent_fd: int,
    name: str,
    expected: os.stat_result,
    *,
    is_directory: bool,
) -> None:
    """Capture, verify, then remove one namespace entry.

    The exclusive rename is the authorization boundary: a replacement present
    at that instant is captured but never deleted. It is restored when the
    original name remains free, otherwise preserved under the random quarantine
    name and reported as a bounded cleanup failure.
    """
    quarantine = f".local-web-ui-delete-{uuid.uuid4().hex}"
    _native_rename_exclusive(parent_fd, name, quarantine)
    try:
        captured = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        raise _cleanup_error() from error
    captured_is_directory = stat.S_ISDIR(captured.st_mode) and not stat.S_ISLNK(
        captured.st_mode
    )
    if not _same_entry(captured, expected) or captured_is_directory != is_directory:
        try:
            _native_rename_exclusive(parent_fd, quarantine, name)
        except UiPackageError as restore_error:
            raise _cleanup_error() from restore_error
        raise _cleanup_error()
    _native_terminal_unlink(parent_fd, quarantine, is_directory=is_directory)


def _remove_tree_fd(directory_fd: int) -> None:
    """Remove children through already-retained, identity-verified descriptors."""
    try:
        children = sorted(os.scandir(directory_fd), key=lambda child: child.name)
        for child in children:
            expected = child.stat(follow_symlinks=False)
            if stat.S_ISDIR(expected.st_mode) and not stat.S_ISLNK(expected.st_mode):
                child_fd = _open_directory_at(directory_fd, child.name)
                with _cleanup_preserving_primary(
                    lambda: _close_descriptor(child_fd)
                ):
                    if not _same_entry(expected, os.fstat(child_fd)):
                        raise _cleanup_error()
                    _remove_tree_fd(child_fd)
                _terminal_remove_at(
                    directory_fd, child.name, expected, is_directory=True
                )
            else:
                _terminal_remove_at(
                    directory_fd, child.name, expected, is_directory=False
                )
    except UiPackageError:
        raise
    except OSError as error:
        raise _cleanup_error() from error


@dataclass
class _BuildSnapshot:
    parent: _PinnedDirectory
    name: str
    path: Path
    stage_fd: int
    identity: os.stat_result
    closed: bool = False

    def cleanup(self) -> None:
        if self.closed:
            return
        failure: UiPackageError | None = None
        try:
            _remove_tree_fd(self.stage_fd)
            _terminal_remove_at(
                self.parent.fd,
                self.name,
                self.identity,
                is_directory=True,
            )
        except UiPackageError as error:
            failure = error
        except OSError as error:
            failure = _cleanup_error()
            failure.__cause__ = error
        if self.stage_fd != -1:
            descriptor = self.stage_fd
            self.stage_fd = -1
            try:
                _close_descriptor(descriptor)
            except UiPackageError as error:
                if failure is None:
                    failure = error
        try:
            self.parent.close()
        except UiPackageError as error:
            if failure is None:
                failure = error
        self.closed = True
        if failure is not None:
            raise failure


def _make_build_snapshot(sources: dict[str, bytes]) -> _BuildSnapshot:
    temporary_parent = Path(tempfile.gettempdir()).resolve()
    parent = _pin_directory(temporary_parent)
    stage_name = f"local-web-ui-build-{uuid.uuid4().hex}"
    try:
        os.mkdir(stage_name, 0o700, dir_fd=parent.fd)
        stage_fd = _open_directory_at(parent.fd, stage_name)
    except Exception as primary:
        try:
            os.rmdir(stage_name, dir_fd=parent.fd)
        except OSError:
            pass
        if isinstance(primary, UiPackageError):
            domain_primary = primary
        else:
            domain_primary = _validation_error()
            domain_primary.__cause__ = primary
        _raise_primary_after_cleanup(domain_primary, parent.close)
    stage_path = temporary_parent / stage_name
    snapshot = _BuildSnapshot(
        parent=parent,
        name=stage_name,
        path=stage_path,
        stage_fd=stage_fd,
        identity=os.fstat(stage_fd),
    )
    try:
        _write_sources_to_stage(stage_fd, sources)
        return snapshot
    except Exception as primary:
        try:
            snapshot.cleanup()
        except UiPackageError as cleanup:
            if isinstance(primary, UiPackageError):
                raise primary from cleanup
            raise _validation_error() from cleanup
        if isinstance(primary, UiPackageError):
            raise
        raise _validation_error() from primary


def _validate_ustar_name(name: str) -> None:
    try:
        encoded = name.encode("ascii")
    except UnicodeEncodeError as error:
        raise _validation_error() from error
    if not name or name.startswith("/") or ".." in name.split("/"):
        raise _validation_error()
    if len(encoded) <= 100:
        return
    prefix, separator, leaf = name.rpartition("/")
    if not separator or len(prefix.encode("ascii")) > 155 or len(leaf.encode("ascii")) > 100:
        raise _validation_error()


def _write_tarball(entries: list[_ArchiveEntry], destination_fd: int) -> str:
    for entry in entries:
        _validate_ustar_name(entry.path)
    try:
        with os.fdopen(os.dup(destination_fd), "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                    for entry in entries:
                        member = tarfile.TarInfo(entry.path)
                        member.uid = member.gid = member.mtime = 0
                        member.uname = member.gname = ""
                        if entry.is_directory:
                            member.type = tarfile.DIRTYPE
                            member.mode = 0o755
                            archive.addfile(member)
                        else:
                            member.mode = 0o644
                            member.size = len(entry.content)
                            archive.addfile(member, io.BytesIO(entry.content))
        os.fsync(destination_fd)
        os.lseek(destination_fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(destination_fd, 1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
    except UiPackageError:
        raise
    except (OSError, ValueError, UnicodeError, tarfile.TarError) as error:
        raise _output_error() from error


def _expected_archive_names(entries: list[_ArchiveEntry]) -> list[str]:
    return [entry.path for entry in entries]


def _existing_output_is_safe(target: _OutputTarget, metadata: bytes, entries: list[_ArchiveEntry]) -> bool:
    try:
        info = os.stat(target.name, dir_fd=target.parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (stat.S_IMODE(info.st_mode) != 0o644)
        or not 0 < info.st_size <= _MAX_ARTIFACT_BYTES
    ):
        return False
    try:
        content = _read_regular(target.parent_fd, target.name)
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            members = archive.getmembers()
            if [member.name for member in members] != _expected_archive_names(entries):
                return False
            for member in members:
                if (
                    member.issym()
                    or member.islnk()
                    or member.uid != 0
                    or member.gid != 0
                    or member.uname != ""
                    or member.gname != ""
                    or member.mtime != 0
                ):
                    return False
                if member.isdir() and member.mode != 0o755:
                    return False
                if member.isfile() and member.mode != 0o644:
                    return False
            package = archive.extractfile("package/package.json")
            return package is not None and package.read() == metadata
    except (OSError, ValueError, UnicodeError, tarfile.TarError):
        return False


def _open_repository_artifacts(repository_fd: int) -> int:
    try:
        info = os.stat("artifacts", dir_fd=repository_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            os.mkdir("artifacts", 0o755, dir_fd=repository_fd)
        except OSError as error:
            raise _output_error() from error
    except OSError as error:
        raise _output_error() from error
    try:
        return _open_directory_at(repository_fd, "artifacts")
    except UiPackageError as error:
        raise _output_error() from error


def _prepare_output(repository: _PinnedDirectory, output: Path) -> _OutputTarget:
    output_path = _absolute(output)
    try:
        relative = output_path.relative_to(repository.path)
    except ValueError:
        relative = None
    if relative is not None:
        if relative.parts not in {
            _ARTIFACT_PARTS,
            _REPRODUCIBILITY_ARTIFACT_PARTS,
            _LEGACY_ARTIFACT_PARTS,
        }:
            raise _output_error()
        artifact_fd = _open_repository_artifacts(repository.fd)
        return _OutputTarget(output_path, relative.name, artifact_fd, None)
    parent = output_path.parent
    try:
        pinned_parent = _pin_directory(parent)
    except UiPackageError as error:
        raise _output_error() from error
    if output_path.name in {"", ".", ".."}:
        _raise_primary_after_cleanup(_output_error(), pinned_parent.close)
    return _OutputTarget(output_path, output_path.name, pinned_parent.fd, pinned_parent)


def _unlink_output_temporary(parent_fd: int, name: str) -> None:
    os.unlink(name, dir_fd=parent_fd)


def _cleanup_temporary(parent_fd: int, name: str) -> None:
    """Remove a failed output without leaving a partial artifact beside it.

    A transient unlink failure is retried.  If the parent remains hostile, move
    the file into the private build directory before attempting a final unlink;
    callers receive a bounded generic cleanup error if that cannot complete.
    """
    for operation in (
        lambda: _unlink_output_temporary(parent_fd, name),
        lambda: os.unlink(name, dir_fd=parent_fd),
        lambda: os.remove(name, dir_fd=parent_fd),
    ):
        try:
            operation()
            return
        except FileNotFoundError:
            return
        except OSError:
            continue
    recovery_name = f".local-web-ui-quarantine-{uuid.uuid4().hex}"
    try:
        os.replace(
            name,
            recovery_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except FileNotFoundError:
        return
    except OSError as error:
        raise _cleanup_error() from error
    try:
        try:
            _unlink_output_temporary(parent_fd, recovery_name)
        except OSError:
            try:
                os.unlink(recovery_name, dir_fd=parent_fd)
            except OSError:
                os.remove(recovery_name, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    except OSError as error:
        raise _cleanup_error() from error


def _replace_output(
    target: _OutputTarget,
    metadata: bytes,
    entries: list[_ArchiveEntry],
) -> str:
    if not _existing_output_is_safe(target, metadata, entries):
        raise _output_error()
    temporary_name = f".local-web-ui-{uuid.uuid4().hex}"
    temporary_fd = -1
    published = False
    primary_error: UiPackageError | None = None
    digest = ""
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | _NO_FOLLOW | _CLOSE_ON_EXEC,
            0o600,
            dir_fd=target.parent_fd,
        )
        os.fchmod(temporary_fd, 0o600)
        digest = _write_tarball(entries, temporary_fd)
        os.fchmod(temporary_fd, 0o644)
        descriptor = temporary_fd
        temporary_fd = -1
        _close_descriptor(descriptor)
        os.replace(
            temporary_name,
            target.name,
            src_dir_fd=target.parent_fd,
            dst_dir_fd=target.parent_fd,
        )
        published = True
        return digest
    except UiPackageError as error:
        primary_error = error
    except (OSError, ValueError, UnicodeError, tarfile.TarError) as error:
        primary_error = _output_error()
        primary_error.__cause__ = error
    cleanup_failure: UiPackageError | None = None
    if not published:
        if temporary_fd != -1:
            descriptor = temporary_fd
            temporary_fd = -1
            try:
                _close_descriptor(descriptor)
            except UiPackageError as error:
                cleanup_failure = error
        try:
            _cleanup_temporary(target.parent_fd, temporary_name)
        except UiPackageError as error:
            cleanup_failure = error
    if primary_error is not None:
        if cleanup_failure is not None:
            raise primary_error from cleanup_failure
        raise primary_error
    if cleanup_failure is not None:
        raise cleanup_failure
    return digest


def _resolve_npm() -> str:
    return _resolve_executable("npm")


def _build_environment() -> dict[str, str]:
    cache = Path(os.environ.get("NPM_CONFIG_CACHE", Path.home() / ".npm"))
    try:
        cache = cache.resolve(strict=True)
        info = os.stat(cache)
    except OSError as error:
        raise _build_error() from error
    if not stat.S_ISDIR(info.st_mode):
        raise _build_error()
    return {
        "PATH": _NPM_PATH,
        "HOME": "/var/empty",
        "LANG": "C",
        "LC_ALL": "C",
        "TZ": "UTC",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_CACHE": os.fspath(cache),
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_IGNORE_SCRIPTS": "true",
        "NPM_CONFIG_OFFLINE": "true",
        "NPM_CONFIG_REGISTRY": "https://registry.invalid",
        "NPM_CONFIG_UPDATE_NOTIFIER": "false",
        "NPM_CONFIG_USERCONFIG": "/dev/null",
        "NO_UPDATE_NOTIFIER": "1",
    }


def _run_supervised(
    directory_fd: int,
    argv: list[str],
    *,
    timeout: float,
    environment: dict[str, str] | None = None,
) -> None:
    """Run fixed argv in a descriptor-pinned cwd and contain its process group."""
    if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
        raise _build_error()
    try:
        process = subprocess.Popen(
            _exec_from_descriptor_argv(directory_fd, argv),
            close_fds=True,
            env=environment or _build_environment(),
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            pass_fds=(directory_fd,),
            start_new_session=True,
        )
        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            try:
                _stop_process_group(process)
            except UiPackageError:
                pass
            raise _build_error() from error
        group_error: UiPackageError | None = None
        try:
            _stop_process_group(process)
        except UiPackageError as error:
            group_error = error
        if returncode != 0 or group_error is not None:
            if group_error is not None:
                raise _build_error() from group_error
            raise _build_error()
    except UiPackageError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise _build_error() from error


def _run_build(snapshot: _BuildSnapshot) -> None:
    npm = _resolve_npm()
    node = _resolve_executable("node")
    environment = _build_environment()
    _run_supervised(
        snapshot.stage_fd,
        [
            npm,
            "ci",
            "--offline",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
        ],
        timeout=_BUILD_TIMEOUT_SECONDS,
        environment=environment,
    )
    _run_supervised(
        snapshot.stage_fd,
        [
            node,
            "node_modules/vite/bin/vite.js",
            "build",
            "--config",
            "packages/ui/vite.config.ts",
        ],
        timeout=_BUILD_TIMEOUT_SECONDS,
        environment=environment,
    )
    _run_supervised(
        snapshot.stage_fd,
        [
            node,
            "node_modules/typescript/bin/tsc",
            "--project",
            "packages/ui/tsconfig.json",
            "--emitDeclarationOnly",
        ],
        timeout=_BUILD_TIMEOUT_SECONDS,
        environment=environment,
    )


def build_ui_package(repository: Path, output: Path) -> UiPackageArtifact:
    """Build and atomically publish a deterministic public UI-package tarball."""
    pinned_repository = _pin_directory(repository)
    output_target: _OutputTarget | None = None
    snapshot: _BuildSnapshot | None = None
    authority: _GitAuthority | None = None
    primary_error: UiPackageError | None = None
    cleanup_failure: UiPackageError | None = None
    artifact: UiPackageArtifact | None = None
    try:
        output_target = _prepare_output(pinned_repository, output)
        authority = _load_git_authority(pinned_repository)
        _verify_worktree_inputs(pinned_repository.fd, authority.sources)
        snapshot = _make_build_snapshot(authority.sources)
        _run_build(snapshot)
        staged_tree = _PinnedUiTree.open(snapshot.stage_fd)
        with _cleanup_preserving_primary(staged_tree.close):
            version, metadata, entries = _snapshot_package(staged_tree)
        snapshot.cleanup()
        snapshot = None
        digest = _replace_output(output_target, metadata, entries)
        artifact = UiPackageArtifact(version=version, sha256=digest, path=output_target.path)
    except UiPackageError as error:
        primary_error = error
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError) as error:
        primary_error = _validation_error()
        primary_error.__cause__ = error
    finally:
        if snapshot is not None:
            try:
                snapshot.cleanup()
            except UiPackageError as error:
                cleanup_failure = error
        for resource in (output_target, authority, pinned_repository):
            if resource is None:
                continue
            try:
                resource.close()
            except UiPackageError as error:
                if cleanup_failure is None:
                    cleanup_failure = error
    if primary_error is not None:
        if cleanup_failure is not None:
            raise primary_error from cleanup_failure
        raise primary_error
    if cleanup_failure is not None:
        raise cleanup_failure
    if artifact is None:
        raise _validation_error()
    return artifact
