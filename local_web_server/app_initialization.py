"""Recoverable publication into an existing empty application directory."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .app_provenance import (
    PROVENANCE_FILENAME,
    ProvenanceError,
    load_provenance,
    validate_compatibility,
)
from .config import ConfigError, load_manifest


_SAFE_TOKEN = r"[A-Za-z0-9_-]+"


class AppInitializationError(ValueError):
    """An existing destination cannot be safely initialised or recovered."""


def _operation_token() -> str:
    return secrets.token_hex(16)


@dataclass(frozen=True)
class InitPaths:
    destination: Path
    stage: Path
    empty_backup: Path


def _lexical_destination(destination: Path) -> Path:
    try:
        lexical = Path(os.path.abspath(os.fspath(destination)))
    except (OSError, TypeError, ValueError) as error:
        raise AppInitializationError("destination is unsafe") from error
    if lexical == Path(lexical.anchor) or lexical.name in {"", ".", ".."}:
        raise AppInitializationError("destination is unsafe")
    return lexical


def _require_real_directory(path: Path) -> None:
    current = Path(path.anchor)
    try:
        for component in path.parts[1:]:
            current /= component
            metadata = os.lstat(current)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise AppInitializationError("destination is unsafe")
    except AppInitializationError:
        raise
    except OSError as error:
        raise AppInitializationError("destination parent is unavailable") from error


def _is_empty_real_directory(path: Path) -> bool:
    try:
        metadata = os.lstat(path)
        return (
            stat.S_ISDIR(metadata.st_mode)
            and not stat.S_ISLNK(metadata.st_mode)
            and next(path.iterdir(), None) is None
        )
    except OSError as error:
        raise AppInitializationError("recovery is unsafe") from error


def _require_exact_stage_provenance(stage: Path, destination: Path) -> None:
    try:
        manifest = load_manifest(stage / "local-web.json")
        provenance = load_provenance(stage / PROVENANCE_FILENAME)
        if (
            manifest.id != destination.name
            or validate_compatibility(provenance, manifest.platform)
        ):
            raise AppInitializationError("initialisation recovery is unsafe")
    except AppInitializationError:
        raise
    except (ConfigError, ProvenanceError, OSError, RuntimeError, ValueError) as error:
        raise AppInitializationError("initialisation recovery is unsafe") from error


class ExistingEmptyDestination:
    def __init__(self, token_factory: Callable[[], str] = _operation_token):
        self._token_factory = token_factory

    def resolve(self, destination: Path, *, recover: bool = False) -> Path:
        lexical = _lexical_destination(destination)
        _require_real_directory(lexical.parent)
        if recover:
            self.recover(lexical)
        try:
            metadata = os.lstat(lexical)
        except FileNotFoundError as error:
            raise AppInitializationError(
                "destination must be an existing directory"
            ) from error
        except OSError as error:
            raise AppInitializationError("destination is unsafe") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AppInitializationError("destination must be an existing directory")
        try:
            if next(lexical.iterdir(), None) is not None:
                raise AppInitializationError("destination must be empty")
        except AppInitializationError:
            raise
        except OSError as error:
            raise AppInitializationError("destination is unsafe") from error
        return lexical.resolve(strict=True)

    def paths(self, destination: Path) -> InitPaths:
        lexical = _lexical_destination(destination)
        _require_real_directory(lexical.parent)
        try:
            token = self._token_factory()
        except Exception as error:
            raise AppInitializationError("initialisation token is unavailable") from error
        if type(token) is not str or not re.fullmatch(_SAFE_TOKEN, token):
            raise AppInitializationError("initialisation token is unavailable")
        stem = f".{lexical.name}.local-web-init-{token}"
        return InitPaths(
            destination=lexical,
            stage=lexical.parent / f"{stem}.stage",
            empty_backup=lexical.parent / f"{stem}.empty",
        )

    def recover(self, destination: Path) -> None:
        lexical = _lexical_destination(destination)
        _require_real_directory(lexical.parent)
        operations = self._recovery_operations(lexical)
        if not operations:
            return
        if len(operations) != 1:
            raise AppInitializationError("initialisation recovery is ambiguous")
        stage, backup = next(iter(operations.values()))
        destination_exists = os.path.lexists(lexical)
        if destination_exists:
            try:
                metadata = os.lstat(lexical)
            except OSError as error:
                raise AppInitializationError("initialisation recovery is unsafe") from error
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise AppInitializationError("initialisation recovery is unsafe")
        if backup is not None and not _is_empty_real_directory(backup):
            raise AppInitializationError("initialisation recovery is unsafe")
        if stage is not None:
            try:
                stage_metadata = os.lstat(stage)
            except OSError as error:
                raise AppInitializationError("initialisation recovery is unsafe") from error
            if stat.S_ISLNK(stage_metadata.st_mode) or not stat.S_ISDIR(
                stage_metadata.st_mode
            ):
                raise AppInitializationError("initialisation recovery is unsafe")
            _require_exact_stage_provenance(stage, lexical)

        try:
            if stage is not None and backup is not None and not destination_exists:
                shutil.rmtree(stage)
                backup.rename(lexical)
                return
            if stage is None and backup is not None:
                if destination_exists:
                    backup.rmdir()
                else:
                    backup.rename(lexical)
                return
            if stage is not None and backup is None and destination_exists:
                if not _is_empty_real_directory(lexical):
                    raise AppInitializationError("initialisation recovery is unsafe")
                shutil.rmtree(stage)
                return
        except AppInitializationError:
            raise
        except OSError as error:
            raise AppInitializationError("initialisation recovery failed") from error
        raise AppInitializationError("initialisation recovery is unsafe")

    def publish(self, stage: Path, destination: Path) -> None:
        resolved = self.resolve(destination)
        paths = self._paths_for_stage(resolved, stage)
        try:
            paths.destination.rename(paths.empty_backup)
        except OSError as error:
            raise AppInitializationError("app publication failed") from error
        try:
            paths.stage.rename(paths.destination)
        except OSError as error:
            try:
                paths.empty_backup.rename(paths.destination)
            except OSError as recovery_error:
                raise AppInitializationError("app publication recovery failed") from recovery_error
            raise AppInitializationError("app publication failed") from error
        try:
            paths.empty_backup.rmdir()
        except OSError as error:
            try:
                paths.destination.rename(paths.stage)
                paths.empty_backup.rename(paths.destination)
            except OSError as recovery_error:
                raise AppInitializationError("app publication recovery failed") from recovery_error
            raise AppInitializationError("app publication failed") from error

    @staticmethod
    def _recovery_operations(destination: Path) -> dict[str, tuple[Path | None, Path | None]]:
        pattern = re.compile(
            rf"^\.{re.escape(destination.name)}\.local-web-init-"
            rf"({_SAFE_TOKEN})\.(stage|empty)$"
        )
        operations: dict[str, list[Path | None]] = {}
        try:
            siblings = tuple(destination.parent.iterdir())
        except OSError as error:
            raise AppInitializationError("initialisation recovery failed") from error
        for sibling in siblings:
            match = pattern.fullmatch(sibling.name)
            if match is None:
                continue
            token, kind = match.groups()
            values = operations.setdefault(token, [None, None])
            values[0 if kind == "stage" else 1] = sibling
        return {token: (values[0], values[1]) for token, values in operations.items()}

    @staticmethod
    def _paths_for_stage(destination: Path, stage: Path) -> InitPaths:
        try:
            lexical_stage = Path(os.path.abspath(os.fspath(stage)))
        except (OSError, TypeError, ValueError) as error:
            raise AppInitializationError("app publication failed") from error
        pattern = re.compile(
            rf"^\.{re.escape(destination.name)}\.local-web-init-"
            rf"({_SAFE_TOKEN})\.stage$"
        )
        match = pattern.fullmatch(lexical_stage.name)
        if lexical_stage.parent != destination.parent or match is None:
            raise AppInitializationError("app publication failed")
        backup = destination.parent / (
            f".{destination.name}.local-web-init-{match.group(1)}.empty"
        )
        if os.path.lexists(backup):
            raise AppInitializationError("app publication failed")
        try:
            metadata = os.lstat(lexical_stage)
        except OSError as error:
            raise AppInitializationError("app publication failed") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise AppInitializationError("app publication failed")
        return InitPaths(destination, lexical_stage, backup)
