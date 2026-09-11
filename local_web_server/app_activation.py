"""Idempotent registration, installation, deployment, and tile verification."""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .app_registration import (
    AppRegistrar,
    AppRegistrationError,
    AppRegistrationPlan,
)
from .config import ConfigError, load_manifest, load_registry
from .deploy import DeploymentManager, DeploymentResult
from .health import frontend_health_path
from .index_registry import INDEX_REGISTRY_ROUTE
from .install import InstallError
from .icons import canonical_manifest_icon
from .host_profile import HostProfilePaths
from .host_profile_store import HostProfileStore, HostProfileStoreError
from .public_origin import join_public_origin
from .public_http import open_public_request
from .platform_installation import (
    install_platform,
    require_committed_installation_sources,
)
from .service_ports import ServicePortAllocator, ServicePortError


_PLATFORM_ROOT = Path(__file__).resolve().parents[1]
_STAGES = ("validate", "register", "install", "deploy", "verify")
_PORT_STABILITY_ATTEMPTS = 3
_MAX_INDEX_BYTES = 1024 * 1024


class AppActivationError(RuntimeError):
    """An activation stage failed without exposing private application state."""


class _ActivationInstallationError(RuntimeError):
    """Keep installation failure classification across deployment recovery."""


@dataclass(frozen=True)
class AppActivationPlan:
    app_id: str
    route: str
    kind: str
    port: int | None
    registry_changed: bool
    stages: tuple[str, ...] = _STAGES
    _registration: AppRegistrationPlan | None = field(
        repr=False, compare=True, default=None
    )


@dataclass(frozen=True)
class AppActivationResult:
    plan: AppActivationPlan
    registry_revision: str | None
    deployment: DeploymentResult
    verified: bool


def verify_served_index_tile(repository: Path, registry, app_id: str) -> None:
    """Verify the tile through the same public registry route used by browsers."""

    request = urllib.request.Request(
        join_public_origin(registry.public_origin, INDEX_REGISTRY_ROUTE),
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
        method="GET",
    )
    with open_public_request(request, timeout=5) as response:
        if not 200 <= response.status < 300:
            raise ValueError
        content = response.read(_MAX_INDEX_BYTES + 1)
    if len(content) > _MAX_INDEX_BYTES:
        raise ValueError
    payload = json.loads(content.decode("utf-8"))
    manifest = load_manifest(Path(repository) / "local-web.json")
    if not isinstance(payload, dict) or not isinstance(payload.get("apps"), list):
        raise ValueError
    expected = {
        "id": app_id,
        "title": manifest.title,
        "route": frontend_health_path(manifest),
        "icon": canonical_manifest_icon(manifest.home.icon),
        "accent": manifest.home.accent,
    }
    matches = [
        app
        for app in payload["apps"]
        if isinstance(app, dict) and app.get("id") == app_id
    ]
    if len(matches) != 1 or any(
        matches[0].get(key) != value for key, value in expected.items()
    ):
        raise ValueError


class AppActivator:
    def __init__(
        self,
        *,
        platform_repository: Path = _PLATFORM_ROOT,
        registry_path: Path | None = None,
        registrar: AppRegistrar | None = None,
        allocator: ServicePortAllocator | None = None,
        profile_store: HostProfileStore | None = None,
        install: Callable[..., Any] = install_platform,
        deployer_factory: Callable[[Any], Any] = DeploymentManager,
        verify: Callable[[Path, Any, str], None] = verify_served_index_tile,
    ):
        self.platform_repository = Path(platform_repository)
        profile_paths = HostProfilePaths.for_repository(self.platform_repository)
        self.registry_path = Path(registry_path or profile_paths.profile)
        self.registrar = registrar or AppRegistrar()
        self.allocator = allocator or ServicePortAllocator()
        self.profile_store = profile_store or HostProfileStore(profile_paths)
        self.install = install
        self.deployer_factory = deployer_factory
        self.verify = verify

    def preview(self, repository: Path) -> AppActivationPlan:
        try:
            require_committed_installation_sources(
                self.platform_repository, require_main=False
            )
            app_repository = Path(repository).resolve(strict=True)
            if not app_repository.is_dir():
                raise OSError
            manifest = load_manifest(app_repository / "local-web.json")
            registry = load_registry(self.registry_path)
            port = None
            if manifest.kind == "service":
                matching = [
                    host
                    for host in registry.apps
                    if host.id == manifest.id
                    or host.repository.resolve(strict=True) == app_repository
                ]
                if len(matching) > 1:
                    raise AppActivationError("application activation validation failed")
                if matching:
                    host = matching[0]
                    if (
                        host.id != manifest.id
                        or host.repository.resolve(strict=True) != app_repository
                    ):
                        raise AppActivationError(
                            "application activation validation failed"
                        )
                    if host.port is None:
                        if host.start_command is not None:
                            raise AppActivationError(
                                "application activation validation failed"
                            )
                        port = self.allocator.select(registry)
                    else:
                        if host.start_command is None:
                            raise AppActivationError(
                                "application activation validation failed"
                            )
                        port = host.port
                else:
                    port = self.allocator.select(registry)
            registration = self.registrar.plan(
                app_repository,
                self.registry_path,
                port=port,
            )
            self.profile_store.require_clean(require_main=False)
            return AppActivationPlan(
                registration.app_id,
                registration.route,
                registration.kind,
                registration.port,
                registration.changed,
                _registration=registration,
            )
        except AppActivationError:
            raise
        except (
            AppRegistrationError,
            ConfigError,
            InstallError,
            HostProfileStoreError,
            ServicePortError,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:
            raise AppActivationError(
                "application activation validation failed"
            ) from error

    def activate(
        self,
        repository: Path,
        *,
        expected_plan: AppActivationPlan | None = None,
        expected_source_commit: str | None = None,
        expected_former_live_commit: str | None = None,
    ) -> AppActivationResult:
        plan = self.preview(repository)
        if expected_plan is not None and plan != expected_plan:
            raise AppActivationError("application activation validation failed")
        if plan.kind == "service" and plan.registry_changed:
            try:
                plan = self._stabilise_service_port(repository, plan)
            except (
                AppRegistrationError,
                ConfigError,
                ServicePortError,
                OSError,
                RuntimeError,
                ValueError,
            ) as error:
                raise AppActivationError("application registration failed") from error
        registration = plan._registration
        if registration is None:
            raise AppActivationError("application activation validation failed")
        registry_revision = None
        try:
            self.profile_store.require_clean()
        except HostProfileStoreError as error:
            raise AppActivationError("application registration failed") from error
        exact_registered_plan = expected_plan is not None and not plan.registry_changed
        if exact_registered_plan:
            try:
                if self.registry_path.read_bytes() != registration.candidate:
                    raise AppRegistrationError("application activation plan changed")
            except (AppRegistrationError, OSError) as error:
                raise AppActivationError("application registration failed") from error
        else:
            try:
                registry_before = self.profile_store.read_current()
                if self.registry_path.read_bytes() != registry_before:
                    raise AppRegistrationError("application activation plan changed")
                refreshed_registration = self.registrar.plan(
                    repository,
                    self.registry_path,
                    port=plan.port,
                )
                if refreshed_registration != registration:
                    raise AppRegistrationError("application activation plan changed")
                if refreshed_registration.changed:
                    registry_revision = self.profile_store.publish_registration(
                        plan.app_id,
                        registry_before,
                        refreshed_registration.candidate,
                    )
                    if self.registry_path.read_bytes() != refreshed_registration.candidate:
                        raise AppRegistrationError("application activation plan changed")
            except (AppRegistrationError, HostProfileStoreError, OSError, ValueError) as error:
                raise AppActivationError("application registration failed") from error

        def install() -> None:
            try:
                self.install(
                    self.platform_repository,
                    dry_run=False,
                    recover_missing_caddy=True,
                )
            except (
                ConfigError,
                InstallError,
                OSError,
                RuntimeError,
                ValueError,
            ) as error:
                raise _ActivationInstallationError from error

        try:
            registry = load_registry(self.registry_path)
            deployer = self.deployer_factory(registry)
            if exact_registered_plan:
                def validate_locked() -> None:
                    if self.registry_path.read_bytes() != registration.candidate:
                        raise AppRegistrationError(
                            "application activation plan changed"
                        )
                    self.profile_store.require_clean()

                deployment = deployer.deploy(
                    plan.app_id,
                    install=install,
                    expected_commit=expected_source_commit,
                    expected_current_commit=expected_former_live_commit,
                    validate_locked=validate_locked,
                )
            else:
                deployment = deployer.deploy(
                    plan.app_id,
                    install=install,
                )
            if (
                deployment.app_id != plan.app_id
                or (
                    expected_source_commit is not None
                    and deployment.commit != expected_source_commit
                )
            ):
                raise ValueError
        except _ActivationInstallationError as error:
            raise AppActivationError("application installation failed") from error
        except Exception as error:
            raise AppActivationError("application deployment failed") from error

        try:
            self.verify(Path(repository), registry, plan.app_id)
        except Exception as error:
            raise AppActivationError("application verification failed") from error
        return AppActivationResult(plan, registry_revision, deployment, True)

    def _stabilise_service_port(
        self, repository: Path, plan: AppActivationPlan
    ) -> AppActivationPlan:
        current = plan
        for _attempt in range(_PORT_STABILITY_ATTEMPTS):
            registry = load_registry(self.registry_path)
            port = self.allocator.select(registry)
            registration = self.registrar.plan(
                repository,
                self.registry_path,
                port=port,
            )
            refreshed = AppActivationPlan(
                registration.app_id,
                registration.route,
                registration.kind,
                registration.port,
                registration.changed,
                _registration=registration,
            )
            if port == current.port:
                return refreshed
            current = refreshed
        raise ServicePortError("service port did not stabilise")
