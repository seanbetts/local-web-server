"""Small, outcome-focused composition of declared application releases."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Protocol

from .models import BuildSpec


class ReleaseCompositionError(ValueError):
    """A declared release could not be assembled without replacing prior output."""


class AppReleaseComposer(Protocol):
    def compose(self, repository: Path, build: BuildSpec) -> Path: ...


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _protected(path: Path) -> bool:
    return any(part == ".git" or part.startswith(".env") for part in path.parts)


def _validate_source(repository: Path, relative: Path) -> Path:
    if relative.is_absolute() or ".." in relative.parts or _protected(relative):
        raise ReleaseCompositionError("release input is unavailable")
    source = repository
    metadata = None
    for index, component in enumerate(relative.parts):
        source /= component
        try:
            metadata = source.lstat()
        except OSError as error:
            raise ReleaseCompositionError("release input is unavailable") from error
        is_last = index == len(relative.parts) - 1
        if stat.S_ISLNK(metadata.st_mode) or (
            not is_last and not stat.S_ISDIR(metadata.st_mode)
        ):
            raise ReleaseCompositionError("release input is unavailable")
    if metadata is None or not (
        stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)
    ):
        raise ReleaseCompositionError("release input is unavailable")
    if stat.S_ISDIR(metadata.st_mode):
        try:
            descendants = tuple(source.rglob("*"))
        except OSError as error:
            raise ReleaseCompositionError("release input is unavailable") from error
        for descendant in descendants:
            try:
                child = descendant.lstat()
            except OSError as error:
                raise ReleaseCompositionError("release input is unavailable") from error
            if (
                _protected(descendant.relative_to(repository))
                or stat.S_ISLNK(child.st_mode)
                or not (stat.S_ISREG(child.st_mode) or stat.S_ISDIR(child.st_mode))
            ):
                raise ReleaseCompositionError("release input is unavailable")
    return source


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    source_mode = source.stat(follow_symlinks=False).st_mode
    destination.chmod(0o755 if source_mode & 0o111 else 0o644)


def _copy_source(source: Path, destination: Path) -> None:
    if source.is_file():
        _copy_file(source, destination)
        return
    destination.mkdir(parents=True, exist_ok=False)
    destination.chmod(0o755)
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        target = destination / child.name
        if child.is_dir():
            _copy_source(child, target)
        else:
            _copy_file(child, target)


class ReleaseComposer:
    """Assemble exact manifest entries and atomically replace a build output."""

    def compose(self, repository: Path, build: BuildSpec) -> Path:
        try:
            root = Path(repository).resolve(strict=True)
            if not root.is_dir():
                raise OSError
        except (OSError, RuntimeError) as error:
            raise ReleaseCompositionError("release repository is unavailable") from error

        output = root / build.output
        if not build.release_entries:
            return output
        if not _inside(output, root) or output == root:
            raise ReleaseCompositionError("release output is invalid")

        sources = [(_validate_source(root, entry.source), entry.target) for entry in build.release_entries]
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(
                tempfile.mkdtemp(prefix=f".{output.name}-stage-", dir=output.parent)
            )
        except OSError as error:
            raise ReleaseCompositionError("release staging failed") from error

        backup: Path | None = None
        try:
            for source, target in sources:
                _copy_source(source, stage / target)

            if os.path.lexists(output):
                if output.is_symlink() or not output.is_dir():
                    raise ReleaseCompositionError("release output is invalid")
                backup = Path(
                    tempfile.mkdtemp(prefix=f".{output.name}-backup-", dir=output.parent)
                )
                backup.rmdir()
                os.replace(output, backup)
            try:
                os.replace(stage, output)
            except BaseException:
                if backup is not None and not os.path.lexists(output):
                    os.replace(backup, output)
                    backup = None
                raise
            if backup is not None:
                try:
                    shutil.rmtree(backup)
                except OSError:
                    # The new output is already active. A stale backup is safer
                    # than reporting a failed composition after committing it.
                    pass
                else:
                    backup = None
            return output
        except ReleaseCompositionError:
            raise
        except (OSError, RuntimeError) as error:
            raise ReleaseCompositionError("release assembly failed") from error
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            if backup is not None and backup.exists() and not output.exists():
                try:
                    os.replace(backup, output)
                except OSError:
                    pass
