"""Recoverable publication for an immutable application update plan."""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .app_update_models import FileChange


_RECOVERY_PATH = "local-web/app-update-recovery.json"
_SCHEMA_VERSION = 1
_MAX_RECOVERY_BYTES = 32 * 1024 * 1024
_SAFE_GIT_ENV = {
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
}


class AppUpdateTransactionError(ValueError):
    """An application update could not be published or recovered safely."""


def _git(repository: Path, arguments: tuple[str, ...]) -> bytes:
    try:
        result = subprocess.run(
            (
                "git",
                "--literal-pathspecs",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.attributesFile=/dev/null",
                *arguments,
            ),
            cwd=repository,
            env=_SAFE_GIT_ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AppUpdateTransactionError("application update recovery failed") from error
    if result.returncode != 0:
        raise AppUpdateTransactionError("application update recovery failed")
    return result.stdout


def _repository_root(repository: Path) -> Path:
    if not isinstance(repository, Path):
        raise AppUpdateTransactionError("application update recovery failed")
    try:
        root = repository.resolve(strict=True)
        if not root.is_dir():
            raise OSError
        reported = Path(
            _git(root, ("rev-parse", "--show-toplevel")).decode("utf-8").strip()
        ).resolve(strict=True)
    except (OSError, UnicodeError) as error:
        raise AppUpdateTransactionError("application update recovery failed") from error
    if reported != root:
        raise AppUpdateTransactionError("application update recovery failed")
    return root


def _recovery_path(repository: Path) -> tuple[Path, Path]:
    root = _repository_root(repository)
    try:
        raw = _git(root, ("rev-parse", "--git-path", _RECOVERY_PATH))
        text = raw.decode("utf-8").strip()
        if not text:
            raise ValueError
        path = Path(text)
        if not path.is_absolute():
            path = root / path
        return root, path.resolve(strict=False)
    except (UnicodeError, ValueError, OSError) as error:
        raise AppUpdateTransactionError("application update recovery failed") from error


def _validate_relative_path(path: Path) -> Path:
    if (
        not isinstance(path, Path)
        or path.is_absolute()
        or not path.parts
        or path == Path(".")
        or any(part in ("", ".", "..") for part in path.parts)
        or Path(path.as_posix()) != path
    ):
        raise AppUpdateTransactionError("application update publication failed")
    return path


def _destination(repository: Path, relative: Path) -> Path:
    relative = _validate_relative_path(relative)
    destination = repository / relative
    try:
        if destination.resolve(strict=False) != destination:
            raise OSError
    except OSError as error:
        raise AppUpdateTransactionError("application update publication failed") from error
    return destination


def _canonical_json(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("utf-8")


def _write_temporary(
    descriptor: int, path: Path, content: bytes, mode: int
) -> None:
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _write_recovery(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_text = tempfile.mkstemp(
        prefix=".app-update-recovery-", dir=path.parent
    )
    temporary = Path(temporary_text)
    try:
        _write_temporary(descriptor, temporary, _canonical_json(state), 0o600)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _decode_state(content: bytes) -> dict[str, Any]:
    if type(content) is not bytes or len(content) > _MAX_RECOVERY_BYTES:
        raise ValueError
    value = json.loads(content.decode("utf-8"))
    if not isinstance(value, dict) or _canonical_json(value) != content:
        raise ValueError
    if set(value) != {
        "createdDirectories",
        "files",
        "schemaVersion",
        "temporaryFiles",
    }:
        raise ValueError
    if value["schemaVersion"] != _SCHEMA_VERSION:
        raise ValueError
    files = value["files"]
    directories = value["createdDirectories"]
    temporary_files = value["temporaryFiles"]
    if (
        not isinstance(files, list)
        or not isinstance(directories, list)
        or not isinstance(temporary_files, list)
    ):
        raise ValueError
    seen: set[Path] = set()
    for item in files:
        if not isinstance(item, dict) or set(item) not in (
            {"absent", "path"},
            {"bytes", "mode", "path"},
        ):
            raise ValueError
        relative = _validate_relative_path(Path(item.get("path", "")))
        if relative in seen:
            raise ValueError
        seen.add(relative)
        if "absent" in item:
            if item["absent"] is not True:
                raise ValueError
        elif (
            type(item["bytes"]) is not str
            or type(item["mode"]) is not int
            or not 0 <= item["mode"] <= 0o7777
        ):
            raise ValueError
        else:
            base64.b64decode(item["bytes"], validate=True)
    for collection in (directories, temporary_files):
        if any(type(item) is not str for item in collection):
            raise ValueError
        paths = [_validate_relative_path(Path(item)) for item in collection]
        if len(paths) != len(set(paths)):
            raise ValueError
    return value


def _remove_empty_directories(repository: Path, relative_paths: list[str]) -> None:
    for text in sorted(
        relative_paths, key=lambda item: len(Path(item).parts), reverse=True
    ):
        directory = _destination(repository, Path(text))
        try:
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError:
            if directory.exists():
                raise


class UpdatePublisher:
    """Publish planned files with exact rollback after failure or interruption."""

    def __init__(
        self,
        *,
        replace: Callable[[Path, Path], None] = os.replace,
    ) -> None:
        self._replace = replace

    def recovery_pending(self, repository: Path) -> bool:
        _, recovery = _recovery_path(repository)
        try:
            return recovery.exists() or recovery.is_symlink()
        except OSError as error:
            raise AppUpdateTransactionError(
                "application update recovery failed"
            ) from error

    def recover(self, repository: Path) -> bool:
        root, recovery = _recovery_path(repository)
        try:
            if not recovery.exists() and not recovery.is_symlink():
                return False
            if recovery.is_symlink() or not recovery.is_file():
                raise OSError
            state = _decode_state(recovery.read_bytes())
            self._restore(root, recovery, state)
            return True
        except AppUpdateTransactionError as error:
            raise AppUpdateTransactionError(
                "application update recovery failed"
            ) from error
        except (OSError, UnicodeError, ValueError, TypeError) as error:
            raise AppUpdateTransactionError("application update recovery failed") from error

    def publish(
        self,
        repository: Path,
        changes: tuple[FileChange, ...],
        validate: Callable[[], None],
    ) -> None:
        try:
            root, recovery = _recovery_path(repository)
            if type(changes) is not tuple or not callable(validate) or not changes:
                raise AppUpdateTransactionError("application update publication failed")
            if recovery.exists() or recovery.is_symlink():
                raise AppUpdateTransactionError("application update publication failed")
            normalized = self._validate_changes(root, changes)
            state, prepared = self._prepare(root, normalized)
        except AppUpdateTransactionError:
            raise
        except Exception as error:
            raise AppUpdateTransactionError("application update publication failed") from error

        marker_written = False
        try:
            _write_recovery(recovery, state)
            marker_written = True
            temporary_by_path = dict(prepared)
            for change in normalized:
                destination = _destination(root, change.path)
                if change.after is None:
                    destination.unlink()
                else:
                    self._replace(temporary_by_path[change.path], destination)
            validate()
            self._clear_recovery(recovery)
        except Exception as error:
            try:
                if marker_written or recovery.exists() or recovery.is_symlink():
                    self._restore(root, recovery, state)
                else:
                    self._clean_prepared(root, state)
            except Exception as recovery_error:
                raise AppUpdateTransactionError(
                    "application update publication failed; recovery required"
                ) from recovery_error
            raise AppUpdateTransactionError("application update publication failed") from error
        except BaseException:
            if (
                not marker_written
                and not recovery.exists()
                and not recovery.is_symlink()
            ):
                self._clean_prepared(root, state)
            raise

    @staticmethod
    def _validate_changes(
        repository: Path, changes: tuple[FileChange, ...]
    ) -> tuple[FileChange, ...]:
        seen: set[Path] = set()
        for change in changes:
            if (
                type(change) is not FileChange
                or type(change.before) not in (bytes, type(None))
                or type(change.after) not in (bytes, type(None))
                or change.before == change.after
            ):
                raise AppUpdateTransactionError("application update publication failed")
            relative = _validate_relative_path(change.path)
            if relative in seen:
                raise AppUpdateTransactionError("application update publication failed")
            seen.add(relative)
            destination = _destination(repository, relative)
            if destination.is_symlink():
                raise AppUpdateTransactionError("application update publication failed")
            if change.after is None:
                if (
                    not destination.is_file()
                    or destination.read_bytes() != change.before
                ):
                    raise AppUpdateTransactionError("application update publication failed")
            elif change.before is None:
                if destination.exists():
                    raise AppUpdateTransactionError("application update publication failed")
            elif not destination.is_file() or destination.read_bytes() != change.before:
                raise AppUpdateTransactionError("application update publication failed")
        return changes

    @staticmethod
    def _prepare(
        repository: Path, changes: tuple[FileChange, ...]
    ) -> tuple[dict[str, Any], tuple[tuple[Path, Path], ...]]:
        created_directories: list[Path] = []
        prepared: list[tuple[Path, Path]] = []
        files: list[dict[str, Any]] = []
        try:
            for change in changes:
                destination = _destination(repository, change.path)
                missing: list[Path] = []
                parent = destination.parent
                while parent != repository and not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                if parent.is_symlink() or not parent.is_dir():
                    raise OSError
                for directory in reversed(missing):
                    directory.mkdir()
                    created_directories.append(directory.relative_to(repository))

                mode = (
                    stat.S_IMODE(destination.stat().st_mode)
                    if change.before is not None
                    else 0o644
                )
                if change.before is None:
                    files.append({"absent": True, "path": change.path.as_posix()})
                else:
                    files.append(
                        {
                            "bytes": base64.b64encode(change.before).decode("ascii"),
                            "mode": mode,
                            "path": change.path.as_posix(),
                        }
                    )
                if change.after is None:
                    continue
                descriptor, temporary_text = tempfile.mkstemp(
                    prefix=f".{destination.name}.local-web-update-",
                    dir=destination.parent,
                )
                temporary = Path(temporary_text)
                _write_temporary(descriptor, temporary, change.after, mode)
                prepared.append((change.path, temporary))
            state = {
                "createdDirectories": [
                    path.as_posix() for path in created_directories
                ],
                "files": files,
                "schemaVersion": _SCHEMA_VERSION,
                "temporaryFiles": [
                    temporary.relative_to(repository).as_posix()
                    for _, temporary in prepared
                ],
            }
            return state, tuple(prepared)
        except BaseException:
            for _, temporary in prepared:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            _remove_empty_directories(
                repository,
                [path.as_posix() for path in created_directories],
            )
            raise

    @staticmethod
    def _clean_prepared(repository: Path, state: dict[str, Any]) -> None:
        for text in state["temporaryFiles"]:
            temporary = _destination(repository, Path(text))
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        _remove_empty_directories(repository, state["createdDirectories"])

    @classmethod
    def _restore(
        cls, repository: Path, recovery: Path, state: dict[str, Any]
    ) -> None:
        for item in state["files"]:
            destination = _destination(repository, Path(item["path"]))
            if "absent" in item:
                try:
                    destination.unlink()
                except FileNotFoundError:
                    pass
                continue
            content = base64.b64decode(item["bytes"], validate=True)
            descriptor, temporary_text = tempfile.mkstemp(
                prefix=f".{destination.name}.local-web-restore-",
                dir=destination.parent,
            )
            temporary = Path(temporary_text)
            try:
                _write_temporary(
                    descriptor, temporary, content, item["mode"]
                )
                os.replace(temporary, destination)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        cls._clean_prepared(repository, state)
        cls._clear_recovery(recovery)

    @staticmethod
    def _clear_recovery(recovery: Path) -> None:
        try:
            recovery.unlink()
        except FileNotFoundError:
            pass
        try:
            recovery.parent.rmdir()
        except OSError:
            pass
