"""Preview-first orchestration for a service public-base-path migration."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .app_activation import verify_served_index_tile
from .app_platform_commit import AppPlatformCommitter
from .app_registration import AppRegistrar, PublicBasePathRegistrationPlan
from .config import load_registry
from .deploy import (
    DeploymentManager,
    DeploymentResult,
    RegisteredServiceRecoveryFailed,
    RegisteredServiceTransition,
)
from .git_build import GitRepository
from .host_profile import HostProfilePaths
from .host_profile_store import HostProfileStore
from .models import AppManifest, HostApp, HostRegistry
from .platform_installation import (
    install_platform,
    require_committed_installation_sources,
)
from .runtime import (
    AppLock,
    RuntimeAccess,
    RuntimeLayout,
    existing_release,
    prune_releases,
    read_release_commit,
)


_PLATFORM_ROOT = Path(__file__).resolve().parents[1]
_VALIDATION_FAILED = "public base path migration validation failed"
_REGISTRY_FAILED = "public base path migration registry update failed"
_DEPLOYMENT_FAILED = "public base path migration deployment failed"
_VERIFICATION_FAILED = "public base path migration verification failed"
_RECOVERY_FAILED = "public base path migration failed; recovery failed"
_MANAGED_NAME = "VITE_PUBLIC_BASE_PATH"
_COMMIT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class PublicBasePathMigrationError(RuntimeError):
    """A bounded public failure for the public-base-path migration workflow."""


class _PublicBasePathVerificationError(RuntimeError):
    """Keep candidate tile failures distinct without exposing their detail."""


@dataclass(frozen=True)
class PublicBasePathMigrationPlan:
    app_id: str
    route: str
    port: int
    public_base_path_status: str
    identity_status: str = "unchanged"
    repository_status: str = "unchanged"
    route_status: str = "unchanged"
    port_status: str = "unchanged"
    service_command_status: str = "unchanged"
    service_action: str = "redeploy and reload"
    _registration: PublicBasePathRegistrationPlan | None = field(
        repr=False, compare=False, default=None
    )
    _former_current: str | None = field(repr=False, compare=False, default=None)
    _former_previous: str | None = field(repr=False, compare=False, default=None)
    _runtime_root: Path | None = field(repr=False, compare=False, default=None)
    _target_commit: str | None = field(repr=False, compare=False, default=None)


@dataclass(frozen=True)
class PublicBasePathMigrationResult:
    plan: PublicBasePathMigrationPlan
    registry_revision: str | None = field(repr=False)
    deployment: DeploymentResult | None = field(repr=False)
    verified: bool


class PublicBasePathMigrator:
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

    def preview(self, repository: Path) -> PublicBasePathMigrationPlan:
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
            registration = self.registrar.plan_public_base_path_migration(
                app_repository, self.registry_path
            )
            if repository_access.main_commit() != target_commit:
                raise ValueError("application target changed during migration planning")
            self.profile_store.require_clean(require_main=False)
            host = self._validate_registration(
                app_repository, registry, registration
            )

            layout = RuntimeLayout(registry.runtime_root, registration.app_id)
            former_current = read_release_commit(layout.current)
            former_previous = read_release_commit(layout.previous)
            if former_current is None:
                raise ValueError("current release is absent")

            target = repository_access.manifest_at(target_commit)
            deployed = repository_access.manifest_at(former_current)
            self._validate_target_manifest_shape(
                target,
                host,
                app_repository,
                registry.runtime_root,
                registration,
            )
            self._validate_deployed_manifest_shape(
                deployed,
                host,
                registration,
            )

            changed = registration.changed
            if changed:
                if target_commit == former_current:
                    raise ValueError("migration requires a fresh source commit")
                if existing_release(layout, target_commit) is not None:
                    raise ValueError("migration target release already exists")

            return PublicBasePathMigrationPlan(
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
        except Exception as error:
            raise PublicBasePathMigrationError(_VALIDATION_FAILED) from error

    def migrate(self, repository: Path) -> PublicBasePathMigrationResult:
        plan = self.preview(repository)
        if plan.public_base_path_status == "current":
            return PublicBasePathMigrationResult(plan, None, None, False)

        try:
            self.profile_store.require_clean()
            refreshed = self.preview(repository)
            app_repository = Path(repository).resolve(strict=True)
            if not self._same_exact_plan(plan, refreshed):
                raise ValueError("migration plan changed")
            application_state = AppPlatformCommitter().inspect_clean_main(
                app_repository
            )
            if (
                application_state.repository != app_repository
                or application_state.head != refreshed._target_commit
            ):
                raise ValueError("application checkout changed after migration planning")
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
            repository_access = self.repository_factory(app_repository)
            if repository_access.main_commit() != refreshed._target_commit:
                raise ValueError("application target changed after migration planning")
            former_releases = self._release_commits(layout)
            if refreshed._target_commit in former_releases:
                raise ValueError("migration target release already exists")
        except Exception as error:
            raise PublicBasePathMigrationError(_VALIDATION_FAILED) from error

        target_commit = refreshed._target_commit
        expected_pointers = (
            refreshed._former_current,
            refreshed._former_previous,
        )

        def require_source_target() -> None:
            if repository_access.main_commit() != target_commit:
                raise ValueError("application target changed during migration")

        def require_former_pointers() -> None:
            actual = (
                read_release_commit(layout.current),
                read_release_commit(layout.previous),
            )
            if actual != expected_pointers:
                raise ValueError("release pointers changed during migration")

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

        try:
            registry_revision = self.profile_store.publish_public_base_path_migration(
                refreshed.app_id,
                registration._before,
                registration._candidate,
            )
        except Exception as error:
            try:
                require_former_pointers()
                self._restore_before_runtime(registration)
                require_former_pointers()
                require_source_target()
                if self._release_commits(layout) != former_releases:
                    raise ValueError("release state changed before migration recovery")
            except Exception as recovery_error:
                error.add_note(
                    "public base path migration recovery failed: "
                    f"{recovery_error}"
                )
                raise PublicBasePathMigrationError(_RECOVERY_FAILED) from error
            raise PublicBasePathMigrationError(_REGISTRY_FAILED) from error

        locked_validations = 0

        def validate_locked() -> None:
            nonlocal locked_validations
            require_candidate_registry()
            require_source_target()
            require_former_pointers()
            releases = self._release_commits(layout)
            if locked_validations == 0:
                if releases != former_releases:
                    raise ValueError("release state changed before migration build")
            else:
                if releases != former_releases | {target_commit}:
                    raise ValueError("migration build changed unrelated releases")
                if existing_release(layout, target_commit) != (
                    layout.releases / target_commit
                ):
                    raise ValueError("migration target release is incomplete")
            locked_validations += 1

        def install_candidate() -> None:
            require_candidate_registry()
            require_source_target()
            self.install(self.platform_repository, dry_run=False)
            require_candidate_registry()
            require_source_target()

        def verify_candidate() -> None:
            try:
                require_candidate_registry()
                require_source_target()
                candidate_registry = load_registry(self.registry_path)
                require_candidate_registry()
                self.verify(app_repository, candidate_registry, refreshed.app_id)
                require_candidate_registry()
                require_source_target()
            except Exception as error:
                raise _PublicBasePathVerificationError from error

        def restore() -> None:
            require_former_pointers()
            current = self.profile_store.read_current()
            if self.registry_path.read_bytes() != current:
                raise ValueError("registry changed during migration recovery")
            if current not in {registration._before, registration._candidate}:
                raise ValueError("registry changed during migration recovery")
            if current == registration._candidate:
                self.profile_store.publish_public_base_path_restoration(
                    refreshed.app_id,
                    registration._candidate,
                    registration._before,
                )
            require_before_registry()
            self.install(self.platform_repository, dry_run=False)
            require_before_registry()
            require_former_pointers()

        restored_verified = False

        def verify_restored() -> None:
            nonlocal restored_verified
            require_before_registry()
            require_former_pointers()
            restored_registry = load_registry(self.registry_path)
            require_before_registry()
            require_former_pointers()
            self.verify(app_repository, restored_registry, refreshed.app_id)
            require_before_registry()
            require_former_pointers()
            self._remove_attempt_release(
                layout,
                target_commit,
                former_releases,
            )
            require_before_registry()
            require_former_pointers()
            require_source_target()
            if self._release_commits(layout) != former_releases:
                raise ValueError("migration recovery changed release state")
            restored_verified = True

        def validate_final() -> None:
            require_candidate_registry()
            require_source_target()
            actual_pointers = (
                read_release_commit(layout.current),
                read_release_commit(layout.previous),
            )
            expected_final = (target_commit, refreshed._former_current)
            if actual_pointers != expected_final:
                raise ValueError("migration final release pointers changed")
            if existing_release(layout, target_commit) != (
                layout.releases / target_commit
            ):
                raise ValueError("migration target release is absent")
            if locked_validations < 2:
                raise ValueError("migration release was not validated under lock")

        transition = RegisteredServiceTransition(
            install=install_candidate,
            verify=verify_candidate,
            restore=restore,
            verify_restored=verify_restored,
            _expected_pointers=expected_pointers,
            _target_commit=target_commit,
            validate_final=validate_final,
        )
        try:
            require_candidate_registry()
            require_source_target()
            candidate_registry = load_registry(self.registry_path)
            require_candidate_registry()
            deployment = self.deployer_factory(candidate_registry).deploy(
                refreshed.app_id,
                service_command_transition=transition,
                expected_commit=target_commit,
                expected_current_commit=refreshed._former_current,
                validate_locked=validate_locked,
            )
            validate_final()
            if (
                deployment.app_id != refreshed.app_id
                or deployment.commit != target_commit
                or deployment.outcome != "deployed"
            ):
                raise ValueError("migration deployment result is inconsistent")
        except Exception as error:
            if isinstance(error, RegisteredServiceRecoveryFailed):
                raise PublicBasePathMigrationError(_RECOVERY_FAILED) from error
            if not restored_verified:
                try:
                    with AppLock(layout):
                        restore()
                        verify_restored()
                except Exception as recovery_error:
                    error.add_note(
                        "public base path migration recovery failed: "
                        f"{recovery_error}"
                    )
                    raise PublicBasePathMigrationError(_RECOVERY_FAILED) from error
            if isinstance(error, _PublicBasePathVerificationError):
                raise PublicBasePathMigrationError(_VERIFICATION_FAILED) from error
            raise PublicBasePathMigrationError(_DEPLOYMENT_FAILED) from error

        return PublicBasePathMigrationResult(
            refreshed,
            registry_revision,
            deployment,
            True,
        )

    @staticmethod
    def _same_exact_plan(
        planned: PublicBasePathMigrationPlan,
        refreshed: PublicBasePathMigrationPlan,
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
        registration: PublicBasePathRegistrationPlan,
    ) -> None:
        current = self.profile_store.read_current()
        if self.registry_path.read_bytes() != current:
            raise ValueError("registry changed before migration recovery")
        if current not in {registration._before, registration._candidate}:
            raise ValueError("registry changed before migration recovery")
        if current == registration._candidate:
            self.profile_store.publish_public_base_path_restoration(
                registration.app_id,
                registration._candidate,
                registration._before,
            )
        if self.registry_path.read_bytes() != registration._before:
            raise ValueError("registry recovery was incomplete")

    @staticmethod
    def _release_commits(layout: RuntimeLayout) -> frozenset[str]:
        with RuntimeAccess(layout, create=False) as access:
            commits = []
            for name in os.listdir(access.releases):
                if not _COMMIT_ID.fullmatch(name):
                    continue
                if not access.release_exists(name):
                    raise ValueError("release disappeared during migration")
                commits.append(name)
        return frozenset(commits)

    @classmethod
    def _remove_attempt_release(
        cls,
        layout: RuntimeLayout,
        target_commit: str,
        former_releases: frozenset[str],
    ) -> None:
        pointers = (
            read_release_commit(layout.current),
            read_release_commit(layout.previous),
        )
        if target_commit in pointers:
            raise ValueError("migration target release is still selected")
        releases = cls._release_commits(layout)
        if releases == former_releases:
            if existing_release(layout, target_commit) is not None:
                raise ValueError("migration target release marker is inconsistent")
            return
        if releases != former_releases | {target_commit}:
            raise ValueError("migration recovery found an unrelated release")
        if existing_release(layout, target_commit) != (
            layout.releases / target_commit
        ):
            raise ValueError("migration target release marker is inconsistent")
        prune_releases(layout, protected_commits=former_releases)
        if (
            existing_release(layout, target_commit) is not None
            or cls._release_commits(layout) != former_releases
        ):
            raise ValueError("migration target release cleanup was incomplete")

    def _validate_registration(
        self,
        app_repository: Path,
        registry: HostRegistry,
        registration: PublicBasePathRegistrationPlan,
    ) -> HostApp:
        registry_path = self.registry_path.resolve(strict=True)
        if (
            registration.registry.resolve(strict=True) != registry_path
            or registration._before != self.registry_path.read_bytes()
            or not isinstance(registration._candidate, bytes)
            or registration.changed
            != (registration._candidate != registration._before)
        ):
            raise ValueError("registration candidate is inconsistent")

        matching: list[tuple[int, HostApp, Path]] = []
        for index, host in enumerate(registry.apps):
            registered_repository = host.repository.resolve(strict=True)
            if (
                host.id == registration.app_id
                or registered_repository == app_repository
            ):
                matching.append((index, host, registered_repository))
        if len(matching) != 1:
            raise ValueError("registration match is not unique")
        index, host, registered_repository = matching[0]
        if (
            host.id != registration.app_id
            or registered_repository != app_repository
            or host.port != registration.port
            or host.start_command is None
        ):
            raise ValueError("registration identity changed")

        self._validate_candidate_bytes(registration, index)
        return host

    @staticmethod
    def _validate_candidate_bytes(
        registration: PublicBasePathRegistrationPlan, matching_index: int
    ) -> None:
        before = json.loads(registration._before.decode("utf-8"))
        candidate = json.loads(registration._candidate.decode("utf-8"))
        if (
            not isinstance(before, dict)
            or not isinstance(candidate, dict)
            or set(candidate) != set(before)
            or any(
                candidate[key] != before[key]
                for key in before
                if key != "apps"
            )
        ):
            raise ValueError("registration candidate changes platform state")
        before_apps = before.get("apps")
        candidate_apps = candidate.get("apps")
        if (
            not isinstance(before_apps, list)
            or not isinstance(candidate_apps, list)
            or len(before_apps) != len(candidate_apps)
            or not 0 <= matching_index < len(before_apps)
            or any(
                candidate_entry != before_entry
                for index, (before_entry, candidate_entry) in enumerate(
                    zip(before_apps, candidate_apps)
                )
                if index != matching_index
            )
        ):
            raise ValueError("registration candidate changes another application")

        before_entry = before_apps[matching_index]
        candidate_entry = candidate_apps[matching_index]
        if not isinstance(before_entry, dict) or not isinstance(candidate_entry, dict):
            raise ValueError("registration candidate is invalid")
        before_without_environment = {
            key: value for key, value in before_entry.items() if key != "environment"
        }
        candidate_without_environment = {
            key: value
            for key, value in candidate_entry.items()
            if key != "environment"
        }
        before_environment = before_entry.get("environment", {})
        candidate_environment = candidate_entry.get("environment", {})
        if (
            before_without_environment != candidate_without_environment
            or not isinstance(before_environment, dict)
            or not isinstance(candidate_environment, dict)
            or {
                key: value
                for key, value in before_environment.items()
                if key != _MANAGED_NAME
            }
            != {
                key: value
                for key, value in candidate_environment.items()
                if key != _MANAGED_NAME
            }
        ):
            raise ValueError("registration candidate is not bounded")
        if registration.changed:
            managed_value = candidate_environment.get(_MANAGED_NAME)
            if (
                not isinstance(managed_value, str)
                or not managed_value
                or before_environment.get(_MANAGED_NAME) == managed_value
            ):
                raise ValueError("registration candidate managed value is invalid")
        elif candidate != before:
            raise ValueError("current registration candidate changed")

    @staticmethod
    def _validate_deployed_manifest_shape(
        manifest: AppManifest,
        host: HostApp,
        registration: PublicBasePathRegistrationPlan,
    ) -> None:
        if (
            manifest.id != registration.app_id
            or manifest.kind != "service"
            or manifest.route != registration.route
            or host.port != registration.port
            or host.start_command is None
        ):
            raise ValueError("application identity changed")

    @classmethod
    def _validate_target_manifest_shape(
        cls,
        manifest: AppManifest,
        host: HostApp,
        app_repository: Path,
        runtime_root: Path,
        registration: PublicBasePathRegistrationPlan,
    ) -> None:
        cls._validate_deployed_manifest_shape(manifest, host, registration)
        command = AppRegistrar._service_command(
            manifest, app_repository, runtime_root, registration.port
        )
        if command != host.start_command.argv:
            raise ValueError("service command changed")
