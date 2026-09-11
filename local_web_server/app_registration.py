"""Strict, atomic registration for generated platform applications."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .app_doctor import AppDoctor, AppInspector
from .app_identity import AppIdentityCatalogue
from .app_provenance import (
    PROVENANCE_FILENAME,
    ProvenanceError,
    load_provenance,
    validate_compatibility,
)
from .config import (
    build_environment,
    ConfigError,
    RegistryRepositoryConflictError,
    load_manifest,
    load_registry,
    validate_registered_app,
)
from .git_build import GitRepository
from .host_profile_store import HostProfileStore, HostProfileStoreError
from .icons import canonical_manifest_icon
from .models import AppManifest, Command
from .service_command import ServiceCommandTemplateError, expand_service_command_template


_MANIFEST_FILENAME = "local-web.json"
_SERVICE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


class AppRegistrationError(ValueError):
    """A privacy-safe failure to register an application."""


@dataclass(frozen=True)
class AppRegistrationResult:
    app_id: str
    route: str
    registry: Path
    changed: bool
    dry_run: bool
    registry_revision: str | None = None


@dataclass(frozen=True)
class AppRegistrationPlan:
    app_id: str
    route: str
    kind: str
    port: int | None
    registry: Path
    changed: bool
    _before: bytes = field(repr=False, compare=True)
    candidate: bytes = field(repr=False, compare=True)


@dataclass(frozen=True)
class ServiceCommandRegistrationPlan:
    app_id: str
    route: str
    port: int
    registry: Path
    changed: bool
    _before: bytes = field(repr=False, compare=False)
    _candidate: bytes = field(repr=False, compare=False)


@dataclass(frozen=True)
class PublicBasePathRegistrationPlan:
    app_id: str
    route: str
    port: int
    registry: Path
    changed: bool
    _before: bytes = field(repr=False, compare=False)
    _candidate: bytes = field(repr=False, compare=False)


def _managed_public_base_path(manifest: AppManifest) -> str | None:
    if "VITE_PUBLIC_BASE_PATH" not in manifest.build.environment:
        return None
    return f"{manifest.route}/"


class AppRegistrar:
    def __init__(
        self,
        doctor: AppInspector | None = None,
        repository_factory: Callable[[Path], GitRepository] = GitRepository,
    ):
        self.doctor = doctor or AppDoctor()
        self._repository_factory = repository_factory

    def register(
        self,
        repository: Path,
        registry_path: Path,
        *,
        profile_store: HostProfileStore,
        dry_run: bool = False,
        port: int | None = None,
    ) -> AppRegistrationResult:
        try:
            if Path(registry_path) != profile_store.paths.profile:
                raise AppRegistrationError("application registration failed")
            profile_store.require_clean(require_main=not dry_run)
            expected_before = profile_store.read_current()
        except AppRegistrationError:
            raise
        except (AttributeError, HostProfileStoreError, OSError, TypeError) as error:
            raise AppRegistrationError("application registration failed") from error
        plan = self.plan(repository, registry_path, port=port)
        if (
            plan.registry != Path(registry_path)
            or plan._before != expected_before
            or plan.changed != (plan._before != plan.candidate)
        ):
            raise AppRegistrationError("application registration failed")
        result = AppRegistrationResult(
            app_id=plan.app_id,
            route=plan.route,
            registry=plan.registry,
            changed=plan.changed,
            dry_run=dry_run,
        )
        if dry_run:
            return result
        try:
            revision = profile_store.publish_registration(
                plan.app_id,
                plan._before,
                plan.candidate,
            )
        except AppRegistrationError:
            raise
        except (HostProfileStoreError, OSError, TypeError, ValueError) as error:
            raise AppRegistrationError("application registration failed") from error
        return replace(result, registry_revision=revision)

    def plan(
        self,
        repository: Path,
        registry_path: Path,
        *,
        port: int | None = None,
    ) -> AppRegistrationPlan:
        registry_path = Path(registry_path)
        registry, raw_registry, registry_bytes = self._load_registry(registry_path)
        resolved_repository = self._resolve_repository(repository)
        manifest = self._eligible_manifest(resolved_repository)
        if manifest.kind == "service" and port is None:
            raise AppRegistrationError("service registration requires an explicit port")
        if manifest.kind == "static" and port is not None:
            raise AppRegistrationError("static registration cannot declare a port")
        if port is not None and (type(port) is not int or not 1 <= port <= 65535 or port == 80):
            raise AppRegistrationError("service registration port is invalid")
        start_command = self._service_command(
            manifest, resolved_repository, registry.runtime_root, port
        )
        matching_index = self._check_conflicts(
            registry.apps, resolved_repository, manifest.id, manifest.route, port
        )
        entry: dict[str, Any] = {
            "id": manifest.id,
            "repository": str(resolved_repository),
            "autoDeploy": True,
        }
        expected_public_base_path = _managed_public_base_path(manifest)
        if expected_public_base_path is not None:
            entry["environment"] = {
                "VITE_PUBLIC_BASE_PATH": expected_public_base_path,
            }
        if port is not None and start_command is not None:
            entry["port"] = port
            entry["startCommand"] = list(start_command)
        if matching_index is not None:
            current = registry.apps[matching_index]
            existing = raw_registry["apps"][matching_index]
            transitioning_to_service = (
                manifest.kind == "service"
                and current.port is None
                and current.start_command is None
            )
            if "environmentFile" in existing:
                entry["environmentFile"] = existing["environmentFile"]
            if existing == entry:
                return AppRegistrationPlan(
                    manifest.id,
                    manifest.route,
                    manifest.kind,
                    port,
                    registry_path,
                    False,
                    registry_bytes,
                    registry_bytes,
                )
            if not transitioning_to_service:
                raise AppRegistrationError("application conflicts with host registry")
            apps = list(raw_registry["apps"])
            apps[matching_index] = entry
        else:
            apps = list(raw_registry["apps"])
            apps.append(entry)
        candidate = dict(raw_registry)
        candidate["apps"] = apps
        content = (json.dumps(candidate, indent=2) + "\n").encode("utf-8")
        return AppRegistrationPlan(
            manifest.id,
            manifest.route,
            manifest.kind,
            port,
            registry_path,
            True,
            registry_bytes,
            content,
        )

    def plan_service_command_migration(
        self,
        repository: Path,
        registry_path: Path,
    ) -> ServiceCommandRegistrationPlan:
        registry_path = Path(registry_path)
        registry, raw_registry, registry_bytes = self._load_registry(registry_path)
        resolved_repository = self._resolve_repository(repository)
        manifest = self._eligible_manifest(resolved_repository)
        if manifest.kind != "service":
            raise AppRegistrationError("application is not eligible for registration")

        matching_index = self._check_conflicts(
            registry.apps, resolved_repository, manifest.id, manifest.route, None
        )
        if matching_index is None:
            raise AppRegistrationError("application conflicts with host registry")
        existing_host = registry.apps[matching_index]
        if existing_host.port is None or existing_host.start_command is None:
            raise AppRegistrationError("application conflicts with host registry")

        port = existing_host.port
        command = self._service_command(
            manifest, resolved_repository, registry.runtime_root, port
        )
        if command is None:
            raise AppRegistrationError("application is not eligible for registration")
        self._check_conflicts(
            registry.apps, resolved_repository, manifest.id, manifest.route, port
        )

        existing = raw_registry["apps"][matching_index]
        candidate_entry = dict(existing)
        candidate_entry["startCommand"] = list(command)
        self._require_command_only_change(existing, candidate_entry)
        validate_registered_app(
            replace(existing_host, start_command=Command(command)), manifest
        )

        if candidate_entry == existing:
            return ServiceCommandRegistrationPlan(
                manifest.id,
                manifest.route,
                port,
                registry_path,
                False,
                registry_bytes,
                registry_bytes,
            )

        apps = list(raw_registry["apps"])
        apps[matching_index] = candidate_entry
        candidate = dict(raw_registry)
        candidate["apps"] = apps
        content = (json.dumps(candidate, indent=2) + "\n").encode("utf-8")
        return ServiceCommandRegistrationPlan(
            manifest.id,
            manifest.route,
            port,
            registry_path,
            True,
            registry_bytes,
            content,
        )

    def plan_public_base_path_migration(
        self,
        repository: Path,
        registry_path: Path,
    ) -> PublicBasePathRegistrationPlan:
        registry_path = Path(registry_path)
        registry, raw_registry, registry_bytes = self._load_registry(registry_path)
        resolved_repository = self._resolve_repository(repository)
        manifest = self._eligible_manifest(resolved_repository)
        if manifest.kind != "service":
            raise AppRegistrationError("application is not eligible for registration")
        expected_public_base_path = _managed_public_base_path(manifest)
        if expected_public_base_path is None:
            raise AppRegistrationError("application is not eligible for registration")

        matching_index = self._check_conflicts(
            registry.apps, resolved_repository, manifest.id, manifest.route, None
        )
        if matching_index is None:
            raise AppRegistrationError("application conflicts with host registry")
        existing_host = registry.apps[matching_index]
        if existing_host.port is None or existing_host.start_command is None:
            raise AppRegistrationError("application conflicts with host registry")
        port = existing_host.port
        command = self._service_command(
            manifest, resolved_repository, registry.runtime_root, port
        )
        if command is None or command != existing_host.start_command.argv:
            raise AppRegistrationError("application conflicts with host registry")
        self._check_conflicts(
            registry.apps, resolved_repository, manifest.id, manifest.route, port
        )

        existing = raw_registry["apps"][matching_index]
        candidate_entry = deepcopy(existing)
        candidate_environment = dict(candidate_entry.get("environment", {}))
        candidate_environment["VITE_PUBLIC_BASE_PATH"] = expected_public_base_path
        candidate_entry["environment"] = candidate_environment
        candidate_host = replace(
            existing_host, environment=tuple(candidate_environment.items())
        )
        try:
            validate_registered_app(candidate_host, manifest)
            resolved_environment = build_environment(manifest, candidate_host, {})
        except (ConfigError, OSError, ValueError) as error:
            raise AppRegistrationError("application is not eligible for registration") from error
        if resolved_environment.get("VITE_PUBLIC_BASE_PATH") != expected_public_base_path:
            raise AppRegistrationError("application is not eligible for registration")

        if candidate_entry == existing:
            return PublicBasePathRegistrationPlan(
                manifest.id, manifest.route, port, registry_path, False,
                registry_bytes, registry_bytes
            )
        apps = list(raw_registry["apps"])
        apps[matching_index] = candidate_entry
        candidate = dict(raw_registry)
        candidate["apps"] = apps
        content = (json.dumps(candidate, indent=2) + "\n").encode("utf-8")
        return PublicBasePathRegistrationPlan(
            manifest.id, manifest.route, port, registry_path, True,
            registry_bytes, content
        )

    @staticmethod
    def _require_command_only_change(existing: dict[str, Any], candidate: dict[str, Any]) -> None:
        if set(existing) != set(candidate):
            raise AppRegistrationError("application conflicts with host registry")
        if any(
            existing[key] != candidate[key]
            for key in existing
            if key != "startCommand"
        ):
            raise AppRegistrationError("application conflicts with host registry")

    @staticmethod
    def _load_registry(registry_path: Path):
        try:
            if registry_path.is_symlink() or not registry_path.is_file():
                raise OSError
            registry = load_registry(registry_path)
            content = registry_path.read_bytes()
            raw_registry: Any = json.loads(content.decode("utf-8"))
            if not isinstance(raw_registry, dict) or not isinstance(
                raw_registry.get("apps"), list
            ):
                raise ValueError
            return registry, raw_registry, content
        except RegistryRepositoryConflictError as error:
            raise AppRegistrationError(
                "host registry has conflicting repositories"
            ) from error
        except (ConfigError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise AppRegistrationError("host registry is invalid") from error

    @staticmethod
    def _resolve_repository(repository: Path) -> Path:
        try:
            resolved = Path(repository).resolve(strict=True)
            if not resolved.is_dir():
                raise OSError
            return resolved
        except (OSError, RuntimeError) as error:
            raise AppRegistrationError(
                "application is not eligible for registration"
            ) from error

    def _eligible_manifest(self, repository: Path):
        try:
            report = self.doctor.inspect(repository)
            if not report.compatible or report.diagnostics:
                raise AppRegistrationError(
                    "application is not eligible for registration"
                )
            working_manifest = load_manifest(repository / _MANIFEST_FILENAME)
            working_provenance = load_provenance(repository / PROVENANCE_FILENAME)
            git_repository = self._repository_factory(repository)
            main_commit = git_repository.main_commit()
            manifest = git_repository.manifest_at(main_commit)
            provenance = git_repository.provenance_at(main_commit)
            if (
                report.app != manifest.id
                or working_manifest != manifest
                or working_provenance != provenance
                or validate_compatibility(provenance, manifest.platform)
                or manifest.platform is None
            ):
                raise AppRegistrationError(
                    "application is not eligible for registration"
                )
            if AppIdentityCatalogue.load().is_reserved(
                canonical_manifest_icon(manifest.home.icon)
            ):
                raise AppRegistrationError(
                    "application identity is not eligible for registration"
                )
            return manifest
        except AppRegistrationError:
            raise
        except (
            ConfigError,
            ProvenanceError,
            OSError,
            RuntimeError,
            ValueError,
            subprocess.CalledProcessError,
        ) as error:
            raise AppRegistrationError(
                "application is not eligible for registration"
            ) from error

    @staticmethod
    def _service_command(
        manifest: AppManifest,
        repository: Path,
        runtime_root: Path,
        port: int | None,
    ) -> tuple[str, ...] | None:
        if manifest.kind == "static":
            return None
        if manifest.service is None or port is None:
            raise AppRegistrationError("application is not eligible for registration")
        declared = manifest.service.start_command
        if declared is None:
            executable = repository / ".venv/bin/python"
            if not executable.is_file() or not os.access(executable, os.X_OK):
                raise AppRegistrationError("application is not eligible for registration")
            return (
                str(executable),
                "-m",
                manifest.service.module,
                "--port",
                str(port),
            )

        try:
            command = expand_service_command_template(
                declared.argv,
                port=port,
                release=runtime_root / "apps" / manifest.id / "current",
                repository=repository,
            )
        except ServiceCommandTemplateError as error:
            raise AppRegistrationError(
                "application is not eligible for registration"
            ) from error

        executable = Path(command[0])
        if not executable.is_absolute():
            from shutil import which

            resolved = which(command[0], path=_SERVICE_PATH)
            if resolved is None:
                raise AppRegistrationError("application is not eligible for registration")
            command = (resolved, *command[1:])
        return command

    def _check_conflicts(
        self,
        registered_apps,
        repository: Path,
        app_id: str,
        route: str,
        port: int | None,
    ) -> int | None:
        resolved_registered: list[Path] = []
        try:
            for app in registered_apps:
                resolved_registered.append(app.repository.resolve(strict=True))
        except (OSError, RuntimeError) as error:
            raise AppRegistrationError(
                "registered applications cannot be inspected"
            ) from error

        if len(set(resolved_registered)) != len(resolved_registered):
            raise AppRegistrationError("host registry has conflicting repositories")
        matching = [
            index
            for index, (app, registered_repository) in enumerate(
                zip(registered_apps, resolved_registered)
            )
            if app.id == app_id or registered_repository == repository
        ]
        if len(matching) > 1:
            raise AppRegistrationError("application conflicts with host registry")
        matching_index = matching[0] if matching else None
        if matching_index is not None and (
            registered_apps[matching_index].id != app_id
            or resolved_registered[matching_index] != repository
        ):
            raise AppRegistrationError("application conflicts with host registry")
        if port is not None and any(
            app.port == port and index != matching_index
            for index, app in enumerate(registered_apps)
        ):
            raise AppRegistrationError("application conflicts with host registry")

        registered_routes: set[str] = set()
        try:
            for index, (app, app_repository) in enumerate(
                zip(registered_apps, resolved_registered)
            ):
                if index == matching_index:
                    continue
                git_repository = self._repository_factory(app_repository)
                main_commit = git_repository.main_commit()
                _host, registered_manifest = validate_registered_app(
                    app, git_repository.manifest_at(main_commit)
                )
                if registered_manifest.route in registered_routes:
                    raise AppRegistrationError(
                        "host registry has conflicting application routes"
                    )
                registered_routes.add(registered_manifest.route)
        except AppRegistrationError:
            raise
        except (
            ConfigError,
            OSError,
            RuntimeError,
            ValueError,
            subprocess.CalledProcessError,
        ) as error:
            raise AppRegistrationError(
                "registered applications cannot be inspected"
            ) from error
        if route in registered_routes:
            raise AppRegistrationError("application conflicts with host registry")
        return matching_index
