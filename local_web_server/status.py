"""Read-only deployment, service, and health status collection."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .config import registered_host, validate_registered_app
from .git_build import GitRepository
from .health import backend_health_path, frontend_health_path
from .models import HostRegistry
from .public_origin import join_public_origin
from .runtime import RuntimeLayout, read_release_commit
from .services import HealthResult, HttpHealthChecker, LaunchctlServiceController, ServiceState


class Repository(Protocol):
    def main_commit(self) -> str: ...

    def manifest_at(self, commit: str): ...


class ServiceController(Protocol):
    def state(self, label: str) -> ServiceState: ...


class HealthChecker(Protocol):
    def check(self, url: str, *, host: str | None = None) -> HealthResult: ...


@dataclass(frozen=True)
class AppStatus:
    app_id: str
    main_commit: str
    deployed_commit: str | None
    stale: bool
    caddy_state: ServiceState
    service_state: ServiceState | None
    internal_health: HealthResult | None
    frontend_health: HealthResult
    backend_health: HealthResult | None
    latest_log: Path | None

    @property
    def healthy(self) -> bool:
        return (
            self.deployed_commit is not None
            and not self.stale
            and self.caddy_state is ServiceState.RUNNING
            and (self.service_state is None or self.service_state is ServiceState.RUNNING)
            and (self.internal_health is None or self.internal_health.healthy)
            and self.frontend_health.healthy
            and (self.backend_health is None or self.backend_health.healthy)
        )


class StatusCollector:
    """Collect each registered app's current state without changing it."""

    def __init__(
        self,
        registry: HostRegistry,
        *,
        services: ServiceController | None = None,
        health: HealthChecker | None = None,
        repository_factory: Callable[[Path], Repository] = GitRepository,
    ):
        self.registry = registry
        self.services = services or LaunchctlServiceController()
        self.health = health or HttpHealthChecker()
        self.repository_factory = repository_factory
        self.caddy_state: ServiceState | None = None

    def collect(self) -> tuple[AppStatus, ...]:
        self.caddy_state = self.services.state("com.sean.local-web.caddy")
        return tuple(
            self._collect_app(app.id, self.caddy_state) for app in self.registry.apps
        )

    def _collect_app(self, app_id: str, caddy_state: ServiceState) -> AppStatus:
        host = registered_host(self.registry, app_id)
        repository = self.repository_factory(host.repository)
        main_commit = repository.main_commit()
        host, manifest = validate_registered_app(host, repository.manifest_at(main_commit))
        layout = RuntimeLayout(self.registry.runtime_root, app_id)
        deployed_commit = read_release_commit(layout.current)
        if deployed_commit is None and (layout.current.exists() or layout.current.is_symlink()):
            raise ValueError(f"invalid release pointer: {layout.current}")

        frontend_health = self.health.check(
            join_public_origin(
                self.registry.public_origin,
                frontend_health_path(manifest),
            )
        )
        service_state = None
        internal_health = None
        if manifest.kind == "service":
            service_state = self.services.state(f"com.sean.local-web.{app_id}")
            internal_health = self.health.check(
                f"http://127.0.0.1:{host.port}{manifest.service.internal_health_path}",
                host=self.registry.host,
            )

        backend_path = backend_health_path(host, manifest)
        backend_health = (
            self.health.check(
                join_public_origin(self.registry.public_origin, backend_path)
            )
            if backend_path is not None
            else None
        )
        return AppStatus(
            app_id=app_id,
            main_commit=main_commit,
            deployed_commit=deployed_commit,
            stale=deployed_commit is not None and deployed_commit != main_commit,
            caddy_state=caddy_state,
            service_state=service_state,
            internal_health=internal_health,
            frontend_health=frontend_health,
            backend_health=backend_health,
            latest_log=self._latest_log(app_id, layout),
        )

    @staticmethod
    def _latest_log(app_id: str, layout: RuntimeLayout) -> Path | None:
        try:
            return max(
                layout.deploy_logs.glob(f"{app_id}-*.log"),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
                default=None,
            )
        except OSError as error:
            raise OSError(f"cannot inspect deployment logs for {app_id}") from error
