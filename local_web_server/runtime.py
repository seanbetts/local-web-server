"""Runtime paths, pinned directories, release pointers, and deployment locks."""

import errno
import fcntl
import os
import re
import stat
import uuid
from collections.abc import Collection
from pathlib import Path
from typing import TextIO


_COMMIT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_APP_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_POINTER_NAMES = {"current", "previous"}
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class DeploymentBusy(RuntimeError):
    """A deployment or rollback already holds this application's lock."""


class RuntimeLayoutError(ValueError):
    """The managed runtime tree is malformed or unsafe to access."""


class _RuntimePathAbsent(FileNotFoundError):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(str(path))


class RuntimeLayout:
    """The disposable runtime locations belonging to one validated app ID."""

    def __init__(self, runtime_root: Path, app_id: str):
        raw_runtime_root = Path(runtime_root)
        if not raw_runtime_root.is_absolute():
            raise RuntimeLayoutError("runtime root must be absolute")
        try:
            if raw_runtime_root.is_symlink():
                raise RuntimeLayoutError(
                    "runtime layout root must be real and non-symlink"
                )
            self.runtime_root = raw_runtime_root.resolve()
        except RuntimeLayoutError:
            raise
        except (OSError, RuntimeError) as error:
            raise RuntimeLayoutError("runtime layout root cannot be resolved safely") from error
        if not _APP_ID.fullmatch(app_id):
            raise RuntimeLayoutError("runtime app id is invalid")
        self.app_id = app_id
        self.app_root = self.runtime_root / "apps" / app_id
        self.releases = self.app_root / "releases"
        self.current = self.app_root / "current"
        self.previous = self.app_root / "previous"
        self.lock = self.app_root / "deploy.lock"
        self.deploy_logs = self.runtime_root / "logs" / "deploy"
        self.service_logs = self.runtime_root / "logs" / "services"


def _layout_for_pointer(link: Path) -> RuntimeLayout:
    link = Path(link)
    if (
        not link.is_absolute()
        or link.name not in _POINTER_NAMES
        or link.parent.parent.name != "apps"
    ):
        raise RuntimeLayoutError("release pointer must be an absolute current or previous path")
    layout = RuntimeLayout(link.parent.parent.parent, link.parent.name)
    if link not in {layout.current, layout.previous}:
        raise RuntimeLayoutError("release pointer path is outside its runtime layout")
    return layout


def _validate_layout_paths(layout: RuntimeLayout) -> None:
    if (
        layout.app_root != layout.runtime_root / "apps" / layout.app_id
        or layout.releases != layout.app_root / "releases"
        or layout.current != layout.app_root / "current"
        or layout.previous != layout.app_root / "previous"
        or layout.lock != layout.app_root / "deploy.lock"
        or layout.deploy_logs != layout.runtime_root / "logs" / "deploy"
        or layout.service_logs != layout.runtime_root / "logs" / "services"
    ):
        raise RuntimeLayoutError("runtime layout paths are invalid")


def _open_root(path: Path, *, create: bool) -> int:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        if not create:
            raise _RuntimePathAbsent(path) from None
        try:
            path.mkdir(parents=True, mode=0o700)
            path_stat = path.lstat()
        except (OSError, RuntimeError) as error:
            raise RuntimeLayoutError(f"cannot create real runtime directory: {path}") from error
    except (OSError, RuntimeError) as error:
        raise RuntimeLayoutError(f"cannot inspect runtime directory: {path}") from error
    if stat.S_ISLNK(path_stat.st_mode) or not stat.S_ISDIR(path_stat.st_mode):
        raise RuntimeLayoutError(
            f"runtime layout directory must be real and non-symlink: {path}"
        )
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS)
    except OSError as error:
        raise RuntimeLayoutError(f"cannot pin runtime directory: {path}") from error
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RuntimeLayoutError(f"runtime directory is not a directory: {path}")
    return descriptor


def _open_child_directory(parent: int, name: str, path: Path, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
        except OSError as error:
            raise RuntimeLayoutError(f"cannot create runtime directory: {path}") from error
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError:
        raise _RuntimePathAbsent(path) from None
    except OSError as error:
        raise RuntimeLayoutError(
            f"runtime layout directory must be real and non-symlink: {path}"
        ) from error
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise RuntimeLayoutError(f"runtime path is not a directory: {path}")
    return descriptor


class RuntimeAccess:
    """Pinned descriptors for managed directories below one runtime root."""

    def __init__(self, layout: RuntimeLayout, *, create: bool, include_logs: bool = False):
        _validate_layout_paths(layout)
        self.layout = layout
        self._descriptors: list[int] = []
        try:
            self.runtime_root = self._keep(_open_root(layout.runtime_root, create=create))
            self.apps = self._keep(
                _open_child_directory(
                    self.runtime_root,
                    "apps",
                    layout.runtime_root / "apps",
                    create=create,
                )
            )
            self.app_root = self._keep(
                _open_child_directory(
                    self.apps, layout.app_id, layout.app_root, create=create
                )
            )
            self.releases = self._keep(
                _open_child_directory(
                    self.app_root, "releases", layout.releases, create=create
                )
            )
            self.deploy_logs: int | None = None
            self.service_logs: int | None = None
            if include_logs:
                logs_path = layout.runtime_root / "logs"
                logs = self._keep(
                    _open_child_directory(
                        self.runtime_root, "logs", logs_path, create=create
                    )
                )
                self.deploy_logs = self._keep(
                    _open_child_directory(
                        logs, "deploy", layout.deploy_logs, create=create
                    )
                )
                self.service_logs = self._keep(
                    _open_child_directory(
                        logs, "services", layout.service_logs, create=create
                    )
                )
        except BaseException:
            self.close()
            raise

    def _keep(self, descriptor: int) -> int:
        self._descriptors.append(descriptor)
        return descriptor

    def close(self) -> None:
        while self._descriptors:
            os.close(self._descriptors.pop())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def release_exists(self, commit: str) -> bool:
        if not _COMMIT_ID.fullmatch(commit):
            raise RuntimeLayoutError("release name must be a full Git object ID")
        try:
            release_stat = os.stat(commit, dir_fd=self.releases, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as error:
            raise RuntimeLayoutError(f"cannot inspect release: {commit}") from error
        if stat.S_ISLNK(release_stat.st_mode) or not stat.S_ISDIR(release_stat.st_mode):
            raise RuntimeLayoutError(f"release must be a real directory: {commit}")
        return True


class AppLock:
    """A non-blocking advisory lock opened without following managed symlinks."""

    def __init__(self, layout: RuntimeLayout):
        self.path = layout.lock
        self.layout = layout
        self._file = None
        self._access: RuntimeAccess | None = None

    def __enter__(self):
        self._access = RuntimeAccess(self.layout, create=True, include_logs=True)
        try:
            descriptor = os.open(
                self.path.name,
                os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=self._access.app_root,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise RuntimeLayoutError(f"deployment lock must be a real file: {self.path}")
            self._file = os.fdopen(descriptor, "a+")
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.close()
            raise DeploymentBusy(f"deployment already in progress: {self.path}") from error
        except RuntimeLayoutError:
            self.close()
            raise
        except OSError as error:
            self.close()
            raise RuntimeLayoutError(
                f"deployment lock must be a real non-symlink file: {self.path}"
            ) from error
        return self

    def open_deploy_log(self, filename: str) -> TextIO:
        if (
            self._access is None
            or self._access.deploy_logs is None
            or "/" in filename
            or filename in {"", ".", ".."}
        ):
            raise RuntimeLayoutError("deployment log path is invalid")
        try:
            descriptor = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _NOFOLLOW | _CLOEXEC,
                0o600,
                dir_fd=self._access.deploy_logs,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise RuntimeLayoutError("deployment log must be a real file")
            return os.fdopen(descriptor, "w", encoding="utf-8")
        except RuntimeLayoutError:
            raise
        except OSError as error:
            raise RuntimeLayoutError(
                f"deployment log must be a real non-symlink file: {filename}"
            ) from error

    def close(self) -> None:
        if self._file is not None:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            finally:
                self._file.close()
                self._file = None
        if self._access is not None:
            self._access.close()
            self._access = None

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _pointer_commit(access: RuntimeAccess, name: str) -> str | None:
    try:
        pointer_stat = os.stat(name, dir_fd=access.app_root, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeLayoutError(f"cannot inspect release pointer: {name}") from error
    if not stat.S_ISLNK(pointer_stat.st_mode):
        raise RuntimeLayoutError(f"release pointer must be a symlink or absent: {name}")
    try:
        raw_target = os.readlink(name, dir_fd=access.app_root)
    except OSError as error:
        raise RuntimeLayoutError(f"cannot read release pointer: {name}") from error
    target = Path(raw_target)
    if target.is_absolute():
        if target.parent != access.layout.releases:
            raise RuntimeLayoutError(f"release pointer must remain in releases: {name}")
        commit = target.name
    else:
        if len(target.parts) != 2 or target.parts[0] != "releases":
            raise RuntimeLayoutError(f"release pointer must remain in releases: {name}")
        commit = target.parts[1]
    if not _COMMIT_ID.fullmatch(commit) or not access.release_exists(commit):
        raise RuntimeLayoutError(f"release pointer target is invalid: {name}")
    return commit


def atomic_symlink(link: Path, target: Path) -> None:
    """Atomically make a release pointer reference a pinned direct release."""
    link = Path(link)
    target = Path(target)
    layout = _layout_for_pointer(link)
    if not target.is_absolute():
        raise RuntimeLayoutError(
            "release target must be a direct child of the matching releases directory"
        )
    try:
        canonical_target = target.resolve(strict=True)
        canonical_releases = layout.releases.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RuntimeLayoutError("release target cannot be resolved safely") from error
    if (
        canonical_target.parent != canonical_releases
        or not _COMMIT_ID.fullmatch(canonical_target.name)
    ):
        raise RuntimeLayoutError(
            "release target must be a direct child of the matching releases directory"
        )

    with RuntimeAccess(layout, create=False) as access:
        if not access.release_exists(canonical_target.name):
            raise RuntimeLayoutError("release target is missing")
        temporary = f".{link.name}.{uuid.uuid4().hex}.tmp"
        try:
            os.symlink(
                f"releases/{canonical_target.name}", temporary, dir_fd=access.app_root
            )
            os.replace(
                temporary,
                link.name,
                src_dir_fd=access.app_root,
                dst_dir_fd=access.app_root,
            )
        except BaseException:
            try:
                os.unlink(temporary, dir_fd=access.app_root)
            except FileNotFoundError:
                pass
            raise


def _pointer_exists_when_releases_absent(layout: RuntimeLayout, link: Path) -> bool:
    try:
        app_stat = layout.app_root.lstat()
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError) as error:
        raise RuntimeLayoutError("cannot inspect incomplete runtime layout") from error
    if stat.S_ISLNK(app_stat.st_mode) or not stat.S_ISDIR(app_stat.st_mode):
        raise RuntimeLayoutError("runtime app directory must be real and non-symlink")
    try:
        link.lstat()
    except FileNotFoundError:
        return False
    except (OSError, RuntimeError) as error:
        raise RuntimeLayoutError("cannot inspect incomplete release pointer") from error
    return True


def read_release_commit(link: Path) -> str | None:
    """Return the selected commit, ``None`` only for genuinely absent state."""
    link = Path(link)
    layout = _layout_for_pointer(link)
    try:
        with RuntimeAccess(layout, create=False) as access:
            return _pointer_commit(access, link.name)
    except _RuntimePathAbsent as error:
        if error.path == layout.releases and _pointer_exists_when_releases_absent(layout, link):
            raise RuntimeLayoutError("release pointer exists without a valid releases directory")
        return None


def remove_release_pointer(link: Path, *, expected_commit: str | None = None) -> None:
    """Remove one valid pointer through its pinned app directory."""
    link = Path(link)
    layout = _layout_for_pointer(link)
    try:
        with RuntimeAccess(layout, create=False) as access:
            selected = _pointer_commit(access, link.name)
            if selected is None:
                if expected_commit is not None:
                    raise RuntimeLayoutError(
                        f"release pointer is absent instead of {expected_commit}: {link.name}"
                    )
                return
            if expected_commit is not None and selected != expected_commit:
                raise RuntimeLayoutError(
                    f"release pointer changed unexpectedly: {link.name}"
                )
            try:
                os.unlink(link.name, dir_fd=access.app_root)
            except OSError as error:
                raise RuntimeLayoutError(
                    f"cannot remove release pointer safely: {link.name}"
                ) from error
    except _RuntimePathAbsent:
        if expected_commit is not None:
            raise RuntimeLayoutError(
                f"release pointer is absent instead of {expected_commit}: {link.name}"
            ) from None


def existing_release(layout: RuntimeLayout, commit: str) -> Path | None:
    """Return a complete exact-SHA release without following a symlink."""
    try:
        with RuntimeAccess(layout, create=False) as access:
            return layout.releases / commit if access.release_exists(commit) else None
    except _RuntimePathAbsent:
        return None


def _remove_tree_at(parent: int, name: str) -> None:
    child = _open_child_directory(parent, name, Path(name), create=False)
    try:
        for entry in os.listdir(child):
            entry_stat = os.stat(entry, dir_fd=child, follow_symlinks=False)
            if stat.S_ISDIR(entry_stat.st_mode) and not stat.S_ISLNK(entry_stat.st_mode):
                _remove_tree_at(child, entry)
            else:
                os.unlink(entry, dir_fd=child)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=parent)


def prune_releases(
    layout: RuntimeLayout,
    *,
    protected_commits: Collection[str] = (),
) -> None:
    """Remove unreferenced direct commit directories through a pinned parent."""
    protected = frozenset(protected_commits)
    for commit in protected:
        if not isinstance(commit, str) or not _COMMIT_ID.fullmatch(commit):
            raise RuntimeLayoutError(
                "protected release name must be a full Git object ID"
            )
    try:
        with RuntimeAccess(layout, create=False) as access:
            for commit in protected:
                if not access.release_exists(commit):
                    raise RuntimeLayoutError(f"protected release is missing: {commit}")
            retained = set(protected)
            retained.update(
                commit
                for commit in (
                    _pointer_commit(access, "current"),
                    _pointer_commit(access, "previous"),
                )
                if commit is not None
            )
            for name in os.listdir(access.releases):
                if name in retained or not _COMMIT_ID.fullmatch(name):
                    continue
                release_stat = os.stat(
                    name, dir_fd=access.releases, follow_symlinks=False
                )
                if stat.S_ISDIR(release_stat.st_mode) and not stat.S_ISLNK(
                    release_stat.st_mode
                ):
                    _remove_tree_at(access.releases, name)
    except _RuntimePathAbsent:
        if protected:
            raise RuntimeLayoutError("protected release is missing") from None
        return
