"""Host-target and external-catalogue values, independent of app source code."""
from dataclasses import dataclass


@dataclass(frozen=True)
class NasTarget:
    kind: str = 'macos'
    adapter_command: tuple[str, ...] = ()
    network_subnet: str | None = None
    proxy_address: str | None = None
    build_context: str | None = None
    writes_enabled: bool = False


@dataclass(frozen=True)
class ExternalServiceLink:
    id: str
    title: str
    url: str
    icon: str
    accent: str
