"""Render the sanitized public registry consumed by the System Index."""

import json
from collections.abc import Mapping

from .health import backend_health_path, frontend_health_path
from .icons import canonical_manifest_icon
from .models import AppManifest, HostApp, HostRegistry


INDEX_ASSET_ROUTE = "/_local-web/platform/index"
INDEX_REGISTRY_NAME = "registry-v1.json"
INDEX_REGISTRY_ROUTE = f"{INDEX_ASSET_ROUTE}/{INDEX_REGISTRY_NAME}"


def _manifest_for(app: HostApp, manifests: Mapping[str, AppManifest]) -> AppManifest:
    try:
        manifest = manifests[app.id]
    except KeyError as error:
        raise ValueError(f"missing manifest for registered app: {app.id}") from error
    if manifest.id != app.id:
        raise ValueError(f"manifest id does not match registered app: {app.id}")
    return manifest


def render_index_registry(
    registry: HostRegistry, manifests: Mapping[str, AppManifest]
) -> bytes:
    """Render the deterministic, explicitly public registry payload."""
    apps: list[dict[str, str | None]] = []
    for host in registry.apps:
        manifest = _manifest_for(host, manifests)
        frontend_path = frontend_health_path(manifest)
        apps.append(
            {
                "id": manifest.id,
                "title": manifest.title,
                "route": frontend_path,
                "icon": canonical_manifest_icon(manifest.home.icon),
                "accent": manifest.home.accent,
                "frontendHealthPath": frontend_path,
                "backendHealthPath": backend_health_path(host, manifest),
            }
        )
    payload = {"schemaVersion": 1, "apps": apps}
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
