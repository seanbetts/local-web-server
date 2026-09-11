"""Preview-first orchestration for an existing service command migration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .app_activation import verify_served_index_tile
from .app_registration import AppRegistrar, ServiceCommandRegistrationPlan
from .config import load_registry
from .deploy import (
    DeploymentManager,
    DeploymentResult,
    ServiceCommandRecoveryFailed,
    ServiceCommandTransition,
)
from .git_build import GitRepository
from .host_profile import HostProfilePaths
from .host_profile_store import HostProfileStore
from .models import HostRegistry
from .platform_installation import (
    install_platform,
    require_committed_installation_sources,
)
from .runtime import RuntimeLayout, read_release_commit


_PLATFORM_ROOT = Path(__file__).resolve().parents[1]
_VALIDATION_FAILED = "service command migration validation failed"
_REGISTRY_FAILED = "service command migration registry update failed"
_DEPLOYMENT_FAILED = "service command migration deployment failed"
_VERIFICATION_FAILED = "service command migration verification failed"
_RECOVERY_FAILED = "service command migration failed; recovery failed"


class ServiceCommandMigrationError(RuntimeError):
    """A bounded public failure for the service-command migration workflow."""


class _ServiceCommandVerificationError(RuntimeError):
    """Keep candidate tile failures distinct without publishing their detail."""


@dataclass(frozen=True)
class ServiceCommandMigrationPlan:
    app_id: str
    route: str
    port: int
    command_status: str
    identity_status: str = "unchanged"
    repository_status: str = "unchanged"
    route_status: str = "unchanged"
    port_status: str = "unchanged"
    service_action: str = "redeploy and reload"
    _registration: ServiceCommandRegistrationPlan | None = field(
        repr=False, compare=False, default=None
    )
    _former_current: str | None = field(repr=False, compare=False, default=None)
    _former_previous: str | None = field(repr=False, compare=False, default=None)
    _runtime_root: Path | None = field(repr=False, compare=False, default=None)
    _target_commit: str | None = field(repr=False, compare=False, default=None)


@dataclass(frozen=True)
class ServiceCommandMigrationResult:
    plan: ServiceCommandMigrationPlan
    registry_revision: str | None = field(repr=False)
    deployment: DeploymentResult | None = field(repr=False)
    verified: bool


class ServiceCommandMigrator:
    def __init__(
        self,
        *,
        platform_repository: Path = _PLATFORM_ROOT,
        registry_path: Path | None = None,
        registrar: AppRegistrar | None = None,
        profile_store: HostProfileStore | None = None,
        install: Callable[..., Any] = install_platform,
        deployer_factory: Callable[[HostRegistry], Any] = DeploymentManager,
        repository_factory: Callable[[Path], Any] = GitRepository,
        verify: Callable[[Path, Any, str], None] = verify_served_index_tile,
    ):
        self.platform_repository = Path(platform_repository)
        profile_paths = HostProfilePaths.for_repository(self.platform_repository)
        self.registry_path = Path(registry_path or profile_paths.profile)
        self.registrar = registrar or AppRegistrar()
        self.profile_store = profile_store or HostProfileStore(profile_paths)
        self.install = install
        self.deployer_factory = deployer_factory
        self.repository_factory = repository_factory
        self.verify = verify

    def preview(self, repository: Path) -> ServiceCommandMigrationPlan:
        try:
            require_committed_installation_sources(
                self.platform_repository, require_main=False
            )
            app_repository = Path(repository).resolve(strict=True)
            if not app_repository.is_dir():
                raise OSError
            registry = load_registry(self.registry_path)
            repository_access = self.repository_factory(app_repository)
            target_commit = repository_access.main_commit()
            registration = self.registrar.plan_service_command_migration(
                app_repository, self.registry_path
            )
            if repository_access.main_commit() != target_commit:
                raise ValueError("application target changed during migration planning")
            self.profile_store.require_clean(require_main=False)
            self._validate_registration(
                app_repository, registry, registration
            )

            layout = RuntimeLayout(registry.runtime_root, registration.app_id)
            former_current = read_release_commit(layout.current)
            former_previous = read_release_commit(layout.previous)
            if former_current is None:
                raise ValueError("current release is absent")
            target = repository_access.manifest_at(target_commit)
            deployed = repository_access.manifest_at(former_current)
            if (
                target.id != registration.app_id
                or target.kind != "service"
                or target.route != registration.route
            ):
                raise ValueError("target application identity changed")
            if (
                deployed.id != registration.app_id
                or deployed.kind != "service"
                or deployed.route != registration.route
            ):
                raise ValueError("deployed application identity changed")

            changed = registration.changed
            return ServiceCommandMigrationPlan(
                registration.app_id,
                registration.route,
                registration.port,
                "change" if changed else "current",
                service_action="redeploy and reload" if changed else "none",
                _registration=registration,
                _former_current=former_current,
                _former_previous=former_previous,
                _runtime_root=registry.runtime_root,
                _target_commit=target_commit,
            )
        except ServiceCommandMigrationError:
            raise
        except Exception as error:
            raise ServiceCommandMigrationError(_VALIDATION_FAILED) from error

    def migrate(self, repository: Path) -> ServiceCommandMigrationResult:
        plan = self.preview(repository)
        if plan.command_status == "current":
            return ServiceCommandMigrationResult(plan, None, None, False)

        try:
            self.profile_store.require_clean()
            refreshed = self.preview(repository)
            app_repository = Path(repository).resolve(strict=True)
            if not self._same_exact_plan(plan, refreshed):
                raise ValueError("migration plan changed")
            registration = refreshed._registration
            if (
                registration is None
                or not registration.changed
                or refreshed._runtime_root is None
                or refreshed._former_current is None
                or refreshed._target_commit is None
            ):
                raise ValueError("migration plan is incomplete")
            layout = RuntimeLayout(refreshed._runtime_root, refreshed.app_id)
        except ServiceCommandMigrationError:
            raise
        except Exception as error:
            raise ServiceCommandMigrationError(_VALIDATION_FAILED) from error

        expected_pointers = (
            refreshed._former_current,
            refreshed._former_previous,
        )

        def require_former_pointers() -> None:
            actual = (
                read_release_commit(layout.current),
                read_release_commit(layout.previous),
            )
            if actual != expected_pointers:
                raise ValueError("release pointers changed during migration")

        try:
            registry_revision = self.profile_store.publish_service_command_migration(
                refreshed.app_id,
                registration._before,
                registration._candidate,
            )
        except Exception as error:
            try:
                require_former_pointers()
                self._restore_before_runtime(registration)
            except Exception as recovery_error:
                error.add_note(
                    f"service command migration recovery failed: {recovery_error}"
                )
                raise ServiceCommandMigrationError(_RECOVERY_FAILED) from error
            raise ServiceCommandMigrationError(_REGISTRY_FAILED) from error

        def require_candidate_registry() -> None:
            current = self.profile_store.read_current()
            if (
                current != registration._candidate
                or self.registry_path.read_bytes() != current
            ):
                raise ValueError("registry changed during migration deployment")

        def require_before_registry() -> None:
            current = self.profile_store.read_current()
            if (
                current != registration._before
                or self.registry_path.read_bytes() != current
            ):
                raise ValueError("registry changed during migration recovery")

        def install_candidate() -> None:
            require_candidate_registry()
            self.install(self.platform_repository, dry_run=False)
            require_candidate_registry()

        def verify_candidate() -> None:
            try:
                require_candidate_registry()
                self.verify(app_repository, candidate_registry, refreshed.app_id)
                require_candidate_registry()
            except Exception as error:
                raise _ServiceCommandVerificationError from error

        def restore() -> None:
            require_former_pointers()
            current = self.profile_store.read_current()
            if self.registry_path.read_bytes() != current:
                raise ValueError("registry changed during migration recovery")
            if current not in {registration._before, registration._candidate}:
                raise ValueError("registry changed during migration recovery")
            if current == registration._candidate:
                self.profile_store.publish_service_command_restoration(
                    refreshed.app_id,
                    registration._candidate,
                    registration._before,
                )
            require_before_registry()
            self.install(self.platform_repository, dry_run=False)
            require_before_registry()

        restored_verified = False

        def verify_restored() -> None:
            nonlocal restored_verified
            require_before_registry()
            restored_registry = load_registry(self.registry_path)
            require_before_registry()
            self.verify(app_repository, restored_registry, refreshed.app_id)
            require_before_registry()
            restored_verified = True

        transition = ServiceCommandTransition(
            install=install_candidate,
            verify=verify_candidate,
            restore=restore,
            verify_restored=verify_restored,
            _expected_pointers=expected_pointers,
            _target_commit=refreshed._target_commit,
            validate_final=require_candidate_registry,
        )
        try:
            if self.registry_path.read_bytes() != registration._candidate:
                raise ValueError("registry changed before migration deployment")
            candidate_registry = load_registry(self.registry_path)
            deployment = self.deployer_factory(candidate_registry).deploy(
                refreshed.app_id,
                service_command_transition=transition,
            )
        except Exception as error:
            if isinstance(error, ServiceCommandRecoveryFailed):
                raise ServiceCommandMigrationError(_RECOVERY_FAILED) from error
            if not restored_verified:
                try:
                    restore()
                except Exception as recovery_error:
                    error.add_note(
                        f"service command migration recovery failed: {recovery_error}"
                    )
                    raise ServiceCommandMigrationError(_RECOVERY_FAILED) from error
            if isinstance(error, _ServiceCommandVerificationError):
                raise ServiceCommandMigrationError(_VERIFICATION_FAILED) from error
            raise ServiceCommandMigrationError(_DEPLOYMENT_FAILED) from error

        return ServiceCommandMigrationResult(
            refreshed, registry_revision, deployment, True
        )

    def _validate_registration(
        self,
        app_repository: Path,
        registry: HostRegistry,
        registration: ServiceCommandRegistrationPlan,
    ) -> None:
        registry_path = self.registry_path.resolve(strict=True)
        if (
            registration.registry.resolve(strict=True) != registry_path
            or registration._before != self.registry_path.read_bytes()
            or not isinstance(registration._candidate, bytes)
            or registration.changed
            != (registration._candidate != registration._before)
        ):
            raise ValueError("registration candidate is inconsistent")

        matching = []
        for host in registry.apps:
            registered_repository = host.repository.resolve(strict=True)
            if (
                host.id == registration.app_id
                or registered_repository == app_repository
            ):
                matching.append((host, registered_repository))
        if len(matching) != 1:
            raise ValueError("registration match is not unique")
        host, registered_repository = matching[0]
        if (
            host.id != registration.app_id
            or registered_repository != app_repository
            or host.port != registration.port
            or host.start_command is None
        ):
            raise ValueError("registration identity changed")

    @staticmethod
    def _same_exact_plan(
        planned: ServiceCommandMigrationPlan,
        refreshed: ServiceCommandMigrationPlan,
    ) -> bool:
        if planned != refreshed:
            return False
        if (
            planned._former_current != refreshed._former_current
            or planned._former_previous != refreshed._former_previous
            or planned._runtime_root != refreshed._runtime_root
            or planned._target_commit != refreshed._target_commit
        ):
            return False
        first = planned._registration
        second = refreshed._registration
        return (
            first is not None
            and second is not None
            and first.app_id == second.app_id
            and first.route == second.route
            and first.port == second.port
            and first.registry == second.registry
            and first.changed == second.changed
            and first._before == second._before
            and first._candidate == second._candidate
        )

    def _restore_before_runtime(
        self,
        registration: ServiceCommandRegistrationPlan,
    ) -> None:
        current = self.profile_store.read_current()
        if self.registry_path.read_bytes() != current:
            raise ValueError("registry changed before migration recovery")
        if current not in {registration._before, registration._candidate}:
            raise ValueError("registry changed before migration recovery")
        if current == registration._candidate:
            self.profile_store.publish_service_command_restoration(
                registration.app_id,
                registration._candidate,
                registration._before,
            )
        if self.registry_path.read_bytes() != registration._before:
            raise ValueError("registry recovery was incomplete")
