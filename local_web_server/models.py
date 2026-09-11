from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Command:
    argv: tuple[str, ...]


@dataclass(frozen=True)
class ReleaseEntry:
    source: Path
    target: Path


@dataclass(frozen=True)
class BuildSpec:
    commands: tuple[Command, ...]
    output: Path
    environment: tuple[str, ...]
    release_entries: tuple[ReleaseEntry, ...] = ()


@dataclass(frozen=True)
class FrontendSecuritySpec:
    connect_sources: tuple[str, ...] = ()
    img_sources: tuple[str, ...] = ()
    worker_sources: tuple[str, ...] = ()
    child_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceSpec:
    module: str
    internal_health_path: str
    frontend_output: Path | None = None
    proxy_paths: tuple[str, ...] = ()
    start_command: Command | None = None
    frontend_security: FrontendSecuritySpec | None = None


@dataclass(frozen=True)
class HomePresentation:
    icon: str = "app"
    accent: str = "#8EA7C6"


@dataclass(frozen=True)
class PlatformSpec:
    contract_version: int
    template_version: int
    ui_version: str
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppManifest:
    schema_version: int
    id: str
    title: str
    route: str
    kind: str
    build: BuildSpec
    health_path: str
    service: ServiceSpec | None
    home: HomePresentation = field(default_factory=HomePresentation)
    platform: PlatformSpec | None = None


@dataclass(frozen=True)
class HostApp:
    id: str
    repository: Path
    auto_deploy: bool
    environment_file: Path | None
    environment: tuple[tuple[str, str], ...]
    port: int | None
    start_command: Command | None
    backend_probe: "BackendProbeSpec | None" = None


@dataclass(frozen=True)
class BackendProbeSpec:
    public_environment: tuple[str, ...]
    base_url_environment: str
    path: str
    headers_from_environment: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class HostRegistry:
    host: str
    runtime_root: Path
    apps: tuple[HostApp, ...]
    schema_version: int = 1
    public_origin: str = ""
    ingress_mode: str = "trusted-lan"

    def __post_init__(self) -> None:
        if not self.public_origin:
            object.__setattr__(self, "public_origin", f"http://{self.host}")
