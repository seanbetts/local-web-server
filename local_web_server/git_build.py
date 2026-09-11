"""Build exact Git revisions into disposable runtime releases."""

import errno
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO

from .app_provenance import AppProvenance, parse_provenance
from .config import ConfigError, parse_manifest
from .models import AppManifest, HostApp
from .runtime import RuntimeAccess, RuntimeLayout, RuntimeLayoutError
from .release_composer import ReleaseComposer, ReleaseCompositionError


_COMMIT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class BuildFailed(RuntimeError):
    """A build command completed with a non-zero exit status."""

    def __init__(self, argv: tuple[str, ...], returncode: int):
        self.argv = argv
        self.returncode = returncode
        super().__init__(f"build command exited {returncode}: {argv!r}")


class BuildOutputInvalid(ValueError):
    """The declared build output cannot safely become a release."""


class GitRepository:
    """Read Git state and create detached worktrees for one repository."""

    def __init__(self, repository: Path):
        self.repository = Path(repository).resolve()
        environment = os.environ.copy()
        local_variables = subprocess.run(
            ["git", "rev-parse", "--local-env-vars"],
            check=True,
            text=True,
            capture_output=True,
        ).stdout.splitlines()
        for variable in local_variables:
            environment.pop(variable, None)
        self._environment = environment

    def _run(self, *arguments: str, **kwargs):
        return subprocess.run(
            ["git", "-C", str(self.repository), *arguments],
            env=self._environment,
            **kwargs,
        )

    def main_commit(self) -> str:
        result = self._run(
            "rev-parse",
            "refs/heads/main",
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout.strip()

    def current_branch(self) -> str | None:
        result = self._run(
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        if result.returncode == 1:
            return None
        raise subprocess.CalledProcessError(
            result.returncode,
            result.args,
            output=result.stdout,
            stderr=result.stderr,
        )

    def manifest_at(self, commit: str) -> AppManifest:
        """Load ``local-web.json`` from the exact full Git object ID."""
        if not _COMMIT_ID.fullmatch(commit):
            raise ConfigError("manifest revision must be a full Git object ID")
        result = self._run(
            "show",
            f"{commit}:local-web.json",
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise ConfigError(f"cannot read manifest from exact revision {commit}")
        return parse_manifest(
            result.stdout,
            source=f"{self.repository}@{commit}:local-web.json",
        )

    def provenance_at(self, commit: str) -> AppProvenance:
        """Load platform provenance from the exact full Git object ID."""
        if not _COMMIT_ID.fullmatch(commit):
            raise ConfigError("provenance revision must be a full Git object ID")
        result = self._run(
            "show",
            f"{commit}:.local-web-platform.json",
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode != 0:
            raise ConfigError("cannot read provenance from exact revision")
        return parse_provenance(result.stdout)

    @contextmanager
    def detached_worktree(self, commit: str) -> Iterator[Path]:
        """Provide an isolated detached worktree for the exact *commit*."""
        with tempfile.TemporaryDirectory(prefix="local-web-build-") as temporary:
            path = Path(temporary) / "worktree"
            added = False
            try:
                self._run(
                    "worktree", "add", "--quiet", "--detach", str(path), commit,
                    check=True,
                )
                added = True
                yield path
            finally:
                if added:
                    self._run(
                        "worktree", "remove", "--force", str(path),
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                self._run(
                    "worktree", "prune",
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_protected_name(path: Path) -> bool:
    return path.name == ".git" or path.name.startswith(".env")


def _validated_output(worktree: Path, output: Path) -> Path:
    root = worktree.resolve(strict=True)
    try:
        resolved = (worktree / output).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise BuildOutputInvalid(f"build output is missing: {output}") from error
    if resolved == root or not resolved.is_dir() or not _is_within(resolved, root):
        raise BuildOutputInvalid(f"build output must be a directory within its worktree: {output}")
    if _is_protected_name(resolved):
        raise BuildOutputInvalid(f"build output contains protected source data: {resolved}")
    for entry in resolved.rglob("*"):
        if entry.is_symlink():
            raise BuildOutputInvalid(f"build output contains a symlink: {entry}")
        if _is_protected_name(entry):
            raise BuildOutputInvalid(f"build output contains protected source data: {entry}")
    return resolved


def _same_entry(expected: os.stat_result, actual: os.stat_result) -> bool:
    return (expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino)


def _require_staging_binding(
    releases: int,
    staging: str,
    expected: os.stat_result,
    descriptor: int | None,
) -> None:
    try:
        named_stat = os.stat(staging, dir_fd=releases, follow_symlinks=False)
        descriptor_stat = os.fstat(descriptor) if descriptor is not None else expected
    except OSError as error:
        raise RuntimeLayoutError("pinned release staging directory was detached") from error
    if (
        not stat.S_ISDIR(named_stat.st_mode)
        or not stat.S_ISDIR(descriptor_stat.st_mode)
        or not _same_entry(expected, named_stat)
        or not _same_entry(expected, descriptor_stat)
    ):
        raise RuntimeLayoutError("pinned release staging directory was replaced")


def _copy_file_at(
    source: int,
    destination: int,
    name: str,
    source_stat: os.stat_result,
    destination_guard: Callable[[], None],
) -> None:
    source_file = os.open(name, os.O_RDONLY | _NOFOLLOW | _CLOEXEC, dir_fd=source)
    try:
        opened_stat = os.fstat(source_file)
        if not stat.S_ISREG(opened_stat.st_mode) or not _same_entry(source_stat, opened_stat):
            raise BuildOutputInvalid(f"build output entry changed during publication: {name}")
        destination_guard()
        destination_file = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC,
            0o600,
            dir_fd=destination,
        )
        try:
            destination_guard()
            with (
                os.fdopen(source_file, "rb", closefd=False) as source_stream,
                os.fdopen(destination_file, "wb", closefd=False) as destination_stream,
            ):
                shutil.copyfileobj(source_stream, destination_stream)
            destination_guard()
            os.fchmod(destination_file, stat.S_IMODE(opened_stat.st_mode))
        finally:
            os.close(destination_file)
    finally:
        os.close(source_file)


def _copy_directory_contents(
    source: int,
    destination: int,
    destination_guard: Callable[[], None],
) -> None:
    for name in os.listdir(source):
        if _is_protected_name(Path(name)):
            raise BuildOutputInvalid(f"build output contains protected source data: {name}")
        source_stat = os.stat(name, dir_fd=source, follow_symlinks=False)
        if stat.S_ISLNK(source_stat.st_mode):
            raise BuildOutputInvalid(f"build output contains a symlink: {name}")
        if stat.S_ISREG(source_stat.st_mode):
            _copy_file_at(source, destination, name, source_stat, destination_guard)
            continue
        if not stat.S_ISDIR(source_stat.st_mode):
            raise BuildOutputInvalid(f"build output contains an unsupported entry: {name}")

        source_child = os.open(name, _DIRECTORY_FLAGS, dir_fd=source)
        try:
            opened_stat = os.fstat(source_child)
            if not stat.S_ISDIR(opened_stat.st_mode) or not _same_entry(source_stat, opened_stat):
                raise BuildOutputInvalid(f"build output entry changed during publication: {name}")
            destination_guard()
            os.mkdir(name, 0o700, dir_fd=destination)
            destination_child = os.open(name, _DIRECTORY_FLAGS, dir_fd=destination)
            try:
                destination_guard()
                _copy_directory_contents(source_child, destination_child, destination_guard)
                destination_guard()
                os.fchmod(destination_child, stat.S_IMODE(opened_stat.st_mode))
            finally:
                os.close(destination_child)
        finally:
            os.close(source_child)


def _copy_tree_at(
    source: Path,
    destination_parent: int,
    destination_name: str,
    destination_guard: Callable[[], None],
) -> None:
    try:
        source_stat = os.stat(source, follow_symlinks=False)
        if not stat.S_ISDIR(source_stat.st_mode):
            raise BuildOutputInvalid(f"build output is not a real directory: {source}")
        source_descriptor = os.open(source, _DIRECTORY_FLAGS)
    except BuildOutputInvalid:
        raise
    except OSError as error:
        raise BuildOutputInvalid(f"build output cannot be pinned safely: {source}") from error
    try:
        opened_stat = os.fstat(source_descriptor)
        if not stat.S_ISDIR(opened_stat.st_mode) or not _same_entry(source_stat, opened_stat):
            raise BuildOutputInvalid(f"build output changed during publication: {source}")
        destination_guard()
        os.mkdir(destination_name, 0o700, dir_fd=destination_parent)
        destination_descriptor = os.open(
            destination_name,
            _DIRECTORY_FLAGS,
            dir_fd=destination_parent,
        )
        try:
            destination_guard()
            _copy_directory_contents(
                source_descriptor,
                destination_descriptor,
                destination_guard,
            )
            destination_guard()
            os.fchmod(destination_descriptor, stat.S_IMODE(opened_stat.st_mode))
        finally:
            os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)


def _remove_tree_at(parent: int, name: str, staging_guard: Callable[[], None]) -> None:
    staging_guard()
    entry_stat = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISDIR(entry_stat.st_mode):
        staging_guard()
        os.unlink(name, dir_fd=parent)
        return

    staging_guard()
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        opened_stat = os.fstat(descriptor)
        if not stat.S_ISDIR(opened_stat.st_mode) or not _same_entry(entry_stat, opened_stat):
            raise RuntimeLayoutError(f"staging entry changed during cleanup: {name}")
        for child in os.listdir(descriptor):
            _remove_tree_at(descriptor, child, staging_guard)
    finally:
        os.close(descriptor)
    staging_guard()
    os.rmdir(name, dir_fd=parent)


def _cleanup_staging(
    releases: int,
    staging: str,
    staging_descriptor: int,
    expected_staging: os.stat_result | None,
) -> None:
    cleanup_error: BaseException | None = None
    if staging_descriptor != -1:
        try:
            if expected_staging is None:
                raise RuntimeLayoutError("pinned release staging identity is unavailable")

            def staging_guard() -> None:
                _require_staging_binding(
                    releases,
                    staging,
                    expected_staging,
                    staging_descriptor,
                )

            staging_guard()
            for entry in os.listdir(staging_descriptor):
                _remove_tree_at(staging_descriptor, entry, staging_guard)
        except BaseException as error:
            cleanup_error = error
        finally:
            os.close(staging_descriptor)
    if expected_staging is None:
        if cleanup_error is None:
            cleanup_error = RuntimeLayoutError("pinned release staging identity is unavailable")
    else:
        try:
            _require_staging_binding(releases, staging, expected_staging, None)
            os.rmdir(staging, dir_fd=releases)
        except BaseException as error:
            if cleanup_error is None:
                cleanup_error = error
            else:
                cleanup_error.add_note(f"staging directory removal also failed: {error!r}")
    if cleanup_error is not None:
        raise cleanup_error


class Builder:
    """Run direct build commands in an exact detached revision."""

    def build(
        self,
        host: HostApp,
        manifest: AppManifest,
        commit: str,
        layout: RuntimeLayout,
        environment: Mapping[str, str],
        log: TextIO,
    ) -> Path:
        if host.id != manifest.id or layout.app_id != manifest.id:
            raise ValueError("host, manifest, and runtime layout must name the same app")
        if not _COMMIT_ID.fullmatch(commit):
            raise ValueError("commit must be a full Git object ID")

        release = layout.releases / commit
        with RuntimeAccess(layout, create=True) as access:
            if access.release_exists(commit):
                raise FileExistsError(f"release already exists: {release}")
            repository = GitRepository(host.repository)
            with repository.detached_worktree(commit) as worktree:
                for command in manifest.build.commands:
                    result = subprocess.run(
                        command.argv,
                        cwd=worktree,
                        env=dict(environment),
                        text=True,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                    if result.returncode != 0:
                        raise BuildFailed(command.argv, result.returncode)
                try:
                    ReleaseComposer().compose(worktree, manifest.build)
                except ReleaseCompositionError as error:
                    raise BuildOutputInvalid(str(error)) from error
                output = _validated_output(worktree, manifest.build.output)
                return self._install_release(output, commit, layout, access)

    @staticmethod
    def _install_release(
        output: Path,
        commit: str,
        layout: RuntimeLayout,
        access: RuntimeAccess,
    ) -> Path:
        staging = f".staging-{uuid.uuid4().hex}"
        try:
            os.mkdir(staging, 0o700, dir_fd=access.releases)
        except OSError as error:
            raise RuntimeLayoutError("cannot create pinned release staging directory") from error
        staging_descriptor = -1
        expected_staging: os.stat_result | None = None
        primary_error: BaseException | None = None
        try:
            expected_staging = os.stat(
                staging,
                dir_fd=access.releases,
                follow_symlinks=False,
            )
            staging_descriptor = os.open(
                staging,
                _DIRECTORY_FLAGS,
                dir_fd=access.releases,
            )

            def staging_guard() -> None:
                if expected_staging is None:
                    raise RuntimeLayoutError("pinned release staging identity is unavailable")
                _require_staging_binding(
                    access.releases,
                    staging,
                    expected_staging,
                    staging_descriptor,
                )

            staging_guard()
            try:
                os.rename(output, "release", dst_dir_fd=staging_descriptor)
            except OSError as error:
                if error.errno != errno.EXDEV:
                    raise
                staging_guard()
                _copy_tree_at(output, staging_descriptor, "release", staging_guard)
                shutil.rmtree(output)
            staging_guard()
            if access.release_exists(commit):
                raise FileExistsError(f"release already exists: {layout.releases / commit}")
            staging_guard()
            os.rename(
                "release",
                commit,
                src_dir_fd=staging_descriptor,
                dst_dir_fd=access.releases,
            )
        except BaseException as error:
            primary_error = error
            raise
        finally:
            try:
                _cleanup_staging(
                    access.releases,
                    staging,
                    staging_descriptor,
                    expected_staging,
                )
            except BaseException as cleanup_error:
                if primary_error is None:
                    raise
                primary_error.add_note(f"staging cleanup failed: {cleanup_error!r}")
        return layout.releases / commit
