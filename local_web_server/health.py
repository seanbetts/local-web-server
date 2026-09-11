"""Canonical public health paths shared by render and operations."""

from .models import AppManifest, HostApp


def frontend_health_path(manifest: AppManifest) -> str:
    """Return the canonical public root for an application's frontend."""
    return f"{manifest.route}/"


def remote_backend_health_path(app_id: str) -> str:
    """Return the core-owned public route for a remote backend probe."""
    return f"/_local-web/health/{app_id}/backend"


def backend_health_path(host: HostApp, manifest: AppManifest) -> str | None:
    """Return an application's public required-backend path, when it has one."""
    if host.backend_probe is not None:
        return remote_backend_health_path(host.id)
    if manifest.kind == "service":
        return manifest.health_path
    return None
