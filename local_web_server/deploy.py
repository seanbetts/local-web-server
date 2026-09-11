"""Safe deployment and rollback orchestration for registered applications."""

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TextIO

from .config import (
    ConfigError,
    build_environment,
    registered_host,
    validate_registered_app,
)
from .git_build import Builder, GitRepository
from .health import frontend_health_path
from .models import AppManifest, HostApp, HostRegistry
from .public_origin import join_public_origin
from .runtime import (
    AppLock,
    RuntimeLayout,
    atomic_symlink,
    existing_release,
    prune_releases,
    read_release_commit,
    remove_release_pointer,
)
from .services import (
    HealthResult,
    HttpHealthChecker,
    LaunchctlServiceController,
    ServiceState,
)


SERVICE_READINESS_TIMEOUT_SECONDS = 15.0
SERVICE_READINESS_POLL_SECONDS = 0.25
_EXPECTATION_UNSET = object()


class Repository(Protocol):
    def main_commit(self) -> str: ...

    def current_branch(self) -> str | None: ...

    def manifest_at(self, commit: str) -> AppManifest: ...


class ReleaseBuilder(Protocol):
    def build(
        self,
        host: HostApp,
        manifest: AppManifest,
        commit: str,
        layout: RuntimeLayout,
        environment: Mapping[str, str],
        log: TextIO,
    ) -> Path: ...


class ServiceController(Protocol):
    def state(self, label: str) -> ServiceState: ...

    def ensure_running(self, label: str, plist_path: Path) -> None: ...

    def restart(self, label: str, plist_path: Path) -> None: ...

    def stop(self, label: str, plist_path: Path) -> None: ...

    def replace(self, label: str, plist_path: Path) -> None: ...


class HealthChecker(Protocol):
    def check(self, url: str, *, host: str | None = None) -> HealthResult: ...


class HealthCheckFailed(RuntimeError):
    """A newly selected release did not pass a readiness gate."""

    def __init__(self, app_id: str, url: str, result: HealthResult):
        self.app_id = app_id
        self.url = url
        self.result = result
        detail = result.error or (
            f"HTTP {result.status}" if result.status is not None else "unknown error"
        )
        super().__init__(f"release readiness failed for {app_id}: {detail}")


class RollbackUnavailable(RuntimeError):
    """An application has no complete current/previous release pair to swap."""


@dataclass(frozen=True)
class RegisteredServiceTransition:
    install: Callable[[], None] = field(repr=False)
    verify: Callable[[], None] = field(repr=False)
    restore: Callable[[], None] = field(repr=False)
    verify_restored: Callable[[], None] = field(repr=False)
    _expected_pointers: tuple[str | None, str | None] = field(repr=False)
    _target_commit: str = field(repr=False)
    validate_final: Callable[[], None] = field(repr=False)


class RegisteredServiceRecoveryFailed(RuntimeError):
    """A registered-service transition failed and exact recovery also failed."""


ServiceCommandTransition = RegisteredServiceTransition
ServiceCommandRecoveryFailed = RegisteredServiceRecoveryFailed


@dataclass(frozen=True)
class DeploymentResult:
    app_id: str
    commit: str | None
    outcome: str
    message: str
    log_path: Path | None


class DeploymentManager:
    """Deploy exact main revisions and safely exchange release pointers."""

    def __init__(
        self,
        registry: HostRegistry,
        *,
        builder: ReleaseBuilder | None = None,
        services: ServiceController | None = None,
        health: HealthChecker | None = None,
        launch_agents: Path | None = None,
        environment: Mapping[str, str] | None = None,
        repository_factory: Callable[[Path], Repository] = GitRepository,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.registry = registry
        self.builder = builder or Builder()
        self.services = services or LaunchctlServiceController()
        self.health = health or HttpHealthChecker()
        self.launch_agents = Path(launch_agents or (Path.home() / "Library" / "LaunchAgents"))
        self.environment = dict(os.environ if environment is None else environment)
        self.repository_factory = repository_factory
        self.monotonic = monotonic
        self.sleep = sleep

    def deploy(
        self,
        app_id: str,
        from_hook: bool = False,
        *,
        install: Callable[[], None] | None = None,
        service_command_transition: RegisteredServiceTransition | None = None,
        expected_commit: str | None = None,
        expected_current_commit: str | None | object = _EXPECTATION_UNSET,
        validate_locked: Callable[[], None] | None = None,
    ) -> DeploymentResult:
        if install is not None and service_command_transition is not None:
            raise ValueError(
                "install and registered-service transition are mutually exclusive"
            )
        layout = self._layout_for_registered_app(app_id)
        with AppLock(layout) as lock:
            def current_under_expectations() -> Path | None:
                if validate_locked is not None:
                    validate_locked()
                selected = self._release_target(layout, layout.current)
                if expected_current_commit is not _EXPECTATION_UNSET and (
                    selected.name if selected is not None else None
                ) != expected_current_commit:
                    raise ValueError("current release changed before deployment")
                return selected

            current = current_under_expectations()
            previous = self._release_target(layout, layout.previous)
            host = registered_host(self.registry, app_id)
            repository = self.repository_factory(host.repository)
            if service_command_transition is None:
                commit = repository.main_commit()
            else:
                commit = service_command_transition._target_commit
                self._require_transition_target(
                    repository,
                    service_command_transition,
                )
            if expected_commit is not None and commit != expected_commit:
                raise ValueError("deployment target changed")
            host, manifest = validate_registered_app(host, repository.manifest_at(commit))

            if from_hook and repository.current_branch() != "main":
                return DeploymentResult(
                    app_id,
                    commit,
                    "skipped",
                    f"{app_id} skipped: hook is not on main",
                    None,
                )
            if from_hook and not host.auto_deploy:
                return DeploymentResult(
                    app_id,
                    commit,
                    "skipped",
                    f"{app_id} skipped: automatic deployment is disabled",
                    None,
                )

            if service_command_transition is not None:
                actual_pointers = (
                    current.name if current is not None else None,
                    previous.name if previous is not None else None,
                )
                if actual_pointers != service_command_transition._expected_pointers:
                    raise ValueError(
                        "registered-service transition pointer state changed"
                    )
            former_manifest = None
            former_current_was_service = False
            if current is not None:
                former_manifest = repository.manifest_at(current.name)
                if former_manifest.id != app_id:
                    raise ConfigError("current release manifest does not match app id")
                former_current_was_service = former_manifest.kind == "service"
            defer_install = (
                install is not None
                and manifest.kind == "service"
                and not former_current_was_service
            )
            if install is not None and not defer_install:
                install()
            if current is not None and current.name == commit:
                try:
                    current_under_expectations()
                    if service_command_transition is not None:
                        service_command_transition.install()
                        self.services.replace(*self._service_target(app_id))
                    elif manifest.kind == "service":
                        self.services.ensure_running(*self._service_target(app_id))
                    url, result = self._check_release_health(host, manifest)
                    if not result.healthy:
                        raise HealthCheckFailed(app_id, url, result)
                    if service_command_transition is not None:
                        service_command_transition.verify()
                        service_command_transition.validate_final()
                        self._require_transition_target(
                            repository,
                            service_command_transition,
                        )
                except Exception as error:
                    if service_command_transition is None:
                        raise
                    self._recover_post_switch(
                        layout,
                        manifest,
                        current,
                        previous,
                        error,
                        operation="deployment",
                        former_current_was_service=former_current_was_service,
                        registered_service_transition=service_command_transition,
                        former_manifest=former_manifest,
                        host=host,
                        pointers_changed=False,
                    )
                    raise
                return DeploymentResult(
                    app_id,
                    commit,
                    "unchanged",
                    f"{app_id} unchanged at {commit}",
                    None,
                )

            log_path = layout.deploy_logs / f"{app_id}-{commit}.log"
            environment = build_environment(manifest, host, self.environment)
            try:
                with lock.open_deploy_log(log_path.name) as log:
                    release = existing_release(layout, commit)
                    if release is None:
                        release = self.builder.build(
                            host,
                            manifest,
                            commit,
                            layout,
                            environment,
                            log,
                        )
                    else:
                        log.write(f"reusing complete release {commit}\n")
                expected_release = layout.releases / commit
                if release != expected_release:
                    raise ValueError("builder returned an unexpected release path")

                current_under_expectations()
            except Exception as error:
                if service_command_transition is None:
                    raise
                self._recover_post_switch(
                    layout,
                    manifest,
                    current,
                    previous,
                    error,
                    operation="deployment",
                    former_current_was_service=former_current_was_service,
                    registered_service_transition=service_command_transition,
                    former_manifest=former_manifest,
                    host=host,
                    pointers_changed=False,
                )
                raise

            atomic_symlink(layout.current, release)
            try:
                if service_command_transition is not None:
                    service_command_transition.install()
                    self.services.replace(*self._service_target(app_id))
                elif defer_install:
                    install()
                if service_command_transition is None and manifest.kind == "service":
                    self.services.ensure_running(*self._service_target(app_id))
                url, result = self._check_release_health(host, manifest)
                if not result.healthy:
                    raise HealthCheckFailed(app_id, url, result)
                if service_command_transition is not None:
                    service_command_transition.verify()

                self._select_pointer(layout.previous, current)
                if service_command_transition is not None:
                    prune_releases(
                        layout,
                        protected_commits=(previous.name,)
                        if previous is not None
                        else (),
                    )
                    service_command_transition.validate_final()
                    self._require_transition_target(
                        repository,
                        service_command_transition,
                    )
            except Exception as error:
                self._recover_post_switch(
                    layout,
                    manifest,
                    current,
                    previous,
                    error,
                    operation="deployment",
                    former_current_was_service=former_current_was_service,
                    registered_service_transition=service_command_transition,
                    former_manifest=former_manifest,
                    host=host,
                    expected_selected_commit=(
                        commit
                        if expected_current_commit is not _EXPECTATION_UNSET
                        else _EXPECTATION_UNSET
                    ),
                )
                raise
            if service_command_transition is None:
                prune_releases(layout)
            return DeploymentResult(
                app_id,
                commit,
                "deployed",
                f"{app_id} deployed {commit}",
                log_path,
            )

    def rollback(self, app_id: str) -> DeploymentResult:
        layout = self._layout_for_registered_app(app_id)
        with AppLock(layout):
            host = registered_host(self.registry, app_id)
            repository = self.repository_factory(host.repository)
            commit = repository.main_commit()
            host, manifest = validate_registered_app(host, repository.manifest_at(commit))
            current = self._release_target(layout, layout.current)
            previous = self._release_target(layout, layout.previous)
            if current is None or previous is None:
                raise RollbackUnavailable(f"rollback unavailable for {app_id}")

            atomic_symlink(layout.current, previous)
            try:
                if manifest.kind == "service":
                    self.services.restart(*self._service_target(app_id))
                url, result = self._check_release_health(host, manifest)
                if not result.healthy:
                    raise HealthCheckFailed(app_id, url, result)
                atomic_symlink(layout.previous, current)
            except Exception as error:
                self._recover_post_switch(
                    layout,
                    manifest,
                    current,
                    previous,
                    error,
                    operation="rollback",
                    former_current_was_service=manifest.kind == "service",
                )
                raise
            prune_releases(layout)
            return DeploymentResult(
                app_id,
                previous.name,
                "rolled-back",
                f"{app_id} rolled back to {previous.name}",
                None,
            )

    def _layout_for_registered_app(self, app_id: str) -> RuntimeLayout:
        if not any(app.id == app_id for app in self.registry.apps):
            raise ConfigError(f"registered app not found: {app_id}")
        return RuntimeLayout(self.registry.runtime_root, app_id)

    @staticmethod
    def _require_transition_target(
        repository: Repository,
        transition: RegisteredServiceTransition,
    ) -> None:
        if repository.main_commit() != transition._target_commit:
            raise ValueError("registered-service transition target changed")

    @staticmethod
    def _release_target(layout: RuntimeLayout, pointer: Path) -> Path | None:
        commit = read_release_commit(pointer)
        if commit is not None:
            return layout.releases / commit
        if pointer.exists() or pointer.is_symlink():
            raise ValueError(f"invalid release pointer: {pointer}")
        return None

    def _check_release_health(
        self, host: HostApp, manifest: AppManifest
    ) -> tuple[str, HealthResult]:
        checks = [
            (
                join_public_origin(
                    self.registry.public_origin,
                    frontend_health_path(manifest),
                ),
                None,
            )
        ]
        if manifest.kind == "service":
            checks.extend(
                (
                    (
                        f"http://127.0.0.1:{host.port}"
                        f"{manifest.service.internal_health_path}",
                        self.registry.host,
                    ),
                    (
                        join_public_origin(
                            self.registry.public_origin,
                            manifest.health_path,
                        ),
                        None,
                    ),
                )
            )
        else:
            url, request_host = checks[0]
            return url, self._check_health(url, request_host)

        service_label = self._service_target(manifest.id)[0]
        deadline = self.monotonic() + SERVICE_READINESS_TIMEOUT_SECONDS
        while True:
            service_state = self.services.state(service_label)
            if service_state is not ServiceState.RUNNING:
                url = service_label
                result = HealthResult(
                    False,
                    None,
                    f"service is {service_state.value}",
                )
            else:
                for url, request_host in checks:
                    result = self._check_health(url, request_host)
                    if not result.healthy:
                        break
                else:
                    return url, result
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return url, result
            self.sleep(min(SERVICE_READINESS_POLL_SECONDS, remaining))

    def _check_health(self, url: str, request_host: str | None) -> HealthResult:
        if request_host is None:
            return self.health.check(url)
        return self.health.check(url, host=request_host)

    def _service_target(self, app_id: str) -> tuple[str, Path]:
        label = f"com.sean.local-web.{app_id}"
        return label, self.launch_agents / f"{label}.plist"

    @staticmethod
    def _select_pointer(pointer: Path, target: Path | None) -> None:
        if target is not None:
            atomic_symlink(pointer, target)
            return
        remove_release_pointer(pointer)

    def _recover_post_switch(
        self,
        layout: RuntimeLayout,
        manifest: AppManifest,
        former_current: Path | None,
        former_previous: Path | None,
        original_error: Exception,
        *,
        operation: str,
        former_current_was_service: bool,
        registered_service_transition: RegisteredServiceTransition | None = None,
        former_manifest: AppManifest | None = None,
        host: HostApp | None = None,
        pointers_changed: bool = True,
        expected_selected_commit: str | None | object = _EXPECTATION_UNSET,
    ) -> None:
        cleanup_errors: list[Exception] = []
        if pointers_changed and expected_selected_commit is not _EXPECTATION_UNSET:
            try:
                if read_release_commit(layout.current) != expected_selected_commit:
                    raise ValueError(
                        f"{operation} recovery cannot replace an external release"
                    )
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
                pointers_changed = False
        if pointers_changed:
            for pointer, target in (
                (layout.current, former_current),
                (layout.previous, former_previous),
            ):
                try:
                    self._select_pointer(pointer, target)
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)

        pointers_restored = False
        try:
            actual = (
                read_release_commit(layout.current),
                read_release_commit(layout.previous),
            )
            expected = (
                former_current.name if former_current is not None else None,
                former_previous.name if former_previous is not None else None,
            )
            if actual != expected:
                raise ValueError(
                    f"{operation} recovery did not restore the exact release pointer pair"
                )
            pointers_restored = True
        except Exception as cleanup_error:
            cleanup_errors.append(cleanup_error)

        if registered_service_transition is not None:
            transition_restored = False
            if pointers_restored:
                try:
                    registered_service_transition.restore()
                    transition_restored = True
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)

            old_job_replaced = False
            if transition_restored:
                try:
                    if former_manifest is None or host is None:
                        raise ValueError(
                            "registered-service recovery requires the former release manifest"
                        )
                    self.services.replace(*self._service_target(former_manifest.id))
                    old_job_replaced = True
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)

            old_release_healthy = False
            if old_job_replaced:
                try:
                    url, result = self._check_release_health(host, former_manifest)
                    if not result.healthy:
                        raise HealthCheckFailed(former_manifest.id, url, result)
                    old_release_healthy = True
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)

            if old_release_healthy:
                try:
                    registered_service_transition.verify_restored()
                except Exception as cleanup_error:
                    cleanup_errors.append(cleanup_error)

            if cleanup_errors:
                raise RegisteredServiceRecoveryFailed(
                    "registered-service transition recovery failed"
                ) from original_error
            return

        if pointers_restored and manifest.kind == "service":
            try:
                if former_current is None or not former_current_was_service:
                    self.services.stop(*self._service_target(manifest.id))
                else:
                    self.services.restart(*self._service_target(manifest.id))
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)

        if pointers_restored:
            try:
                prune_releases(layout)
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
        for cleanup_error in cleanup_errors:
            original_error.add_note(f"{operation} recovery failed: {cleanup_error}")
