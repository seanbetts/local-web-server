"""Deterministically render the public hosting configuration files."""

import json
import plistlib
import re
import shlex
from collections.abc import Mapping
from pathlib import Path

from .health import remote_backend_health_path
from .index_registry import INDEX_ASSET_ROUTE, INDEX_REGISTRY_ROUTE
from .models import AppManifest, HostApp, HostRegistry
from .public_origin import TAILSCALE_CADDY_PORT, TAILSCALE_SERVE, TRUSTED_LAN
from .runtime import RuntimeLayout
from .theme import THEME_ROUTE


_APP_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quoted_path(path: Path) -> str:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError(f"path must be absolute: {path}")
    return json.dumps(str(path))


def _manifest_for(app: HostApp, manifests: Mapping[str, AppManifest]) -> AppManifest:
    try:
        manifest = manifests[app.id]
    except KeyError as error:
        raise ValueError(f"missing manifest for registered app: {app.id}") from error
    if manifest.id != app.id:
        raise ValueError(f"manifest id does not match registered app: {app.id}")
    return manifest


def probe_environment_name(app_id: str, source_name: str) -> str:
    """Return the private Caddy environment name for one declared probe input."""
    if not _APP_ID.fullmatch(app_id):
        raise ValueError(f"invalid app id: {app_id}")
    if not _ENVIRONMENT_NAME.fullmatch(source_name):
        raise ValueError(f"invalid environment name: {source_name}")
    return f"LOCAL_WEB_PROBE_{app_id.replace('-', '_').upper()}_{source_name.upper()}"


def _render_backend_probe(lines: list[str], app: HostApp) -> None:
    probe = app.backend_probe
    if probe is None:
        return
    route = remote_backend_health_path(app.id)
    matcher = f"{app.id.replace('-', '_')}_backend_probe"
    origin = probe_environment_name(app.id, probe.base_url_environment)

    lines.extend(
        (
            f"    handle {route} {{",
            f"        @{matcher} method HEAD",
            f"        handle @{matcher} {{",
            f'            reverse_proxy "{{${origin}}}" {{',
            "                method GET",
            f"                rewrite {probe.path}",
            "                header_up -*",
        )
    )
    for header, source_name in probe.headers_from_environment:
        lines.append(
            f'                header_up {header} "{{${probe_environment_name(app.id, source_name)}}}"'
        )
    lines.extend(
        (
            "                header_down -*",
            "                header_down -Location",
            "                transport http {",
            "                    dial_timeout 5s",
            "                    response_header_timeout 5s",
            "                }",
            "                @probe_success status 2xx",
            "                handle_response @probe_success {",
            "                    respond 204",
            "                }",
            "                handle_response {",
            "                    respond 502",
            "                }",
            "            }",
            "        }",
            "        handle {",
            "            respond 405",
            "        }",
            "    }",
        )
    )


def _render_frontend_cache_headers(lines: list[str], app_id: str, indent: str) -> None:
    matcher = app_id.replace("-", "_")
    lines.extend(
        (
            f"{indent}@{matcher}_immutable_assets path /assets/*",
            f'{indent}header @{matcher}_immutable_assets Cache-Control "public, max-age=31536000, immutable"',
            f"{indent}@{matcher}_revalidated_files not path /assets/*",
            f'{indent}header @{matcher}_revalidated_files Cache-Control "no-cache"',
        )
    )


def _render_caddy_opening(registry: HostRegistry) -> list[str]:
    if registry.ingress_mode == TRUSTED_LAN:
        return [":80 {"]
    if registry.ingress_mode == TAILSCALE_SERVE:
        return [
            "{",
            "    servers {",
            "        trusted_proxies static 127.0.0.1/32",
            "        trusted_proxies_strict",
            "    }",
            "}",
            "",
            f"http://{registry.host}:{TAILSCALE_CADDY_PORT} {{",
            "    bind 127.0.0.1",
        ]
    raise ValueError("unsupported ingress mode")


def _frontend_content_security_policy(manifest: AppManifest) -> str:
    service = manifest.service
    security = service.frontend_security if service is not None else None
    connect_sources = ""
    img_sources = ""
    worker_directive = ""
    child_directive = ""
    if security is not None:
        if security.connect_sources:
            connect_sources = " " + " ".join(sorted(security.connect_sources))
        if security.img_sources:
            img_sources = " " + " ".join(sorted(security.img_sources))
        if security.worker_sources:
            worker_sources = " ".join(sorted(security.worker_sources))
            worker_directive = f"worker-src 'self' {worker_sources}; "
        if security.child_sources:
            child_sources = " ".join(sorted(security.child_sources))
            child_directive = f"child-src 'self' {child_sources}; "
    return (
        f"default-src 'self'; connect-src 'self'{connect_sources}; "
        f"img-src 'self' data:{img_sources}; {worker_directive}{child_directive}"
        "script-src 'self'; "
        "style-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'none'"
    )


def render_caddyfile(
    registry: HostRegistry, manifests: Mapping[str, AppManifest], *,
    prepare_tailscale_port_migration: bool = False,
) -> str:
    """Render the plain-HTTP Caddy configuration for all registered applications."""
    if prepare_tailscale_port_migration and registry.ingress_mode != TAILSCALE_SERVE:
        raise ValueError("port migration preparation requires Tailscale ingress")
    apps = [(_manifest_for(app, manifests), app) for app in registry.apps]
    apps.sort(key=lambda item: -len(item[0].route))

    opening = _render_caddy_opening(registry)
    lines = list(opening)
    theme_root = Path(registry.runtime_root) / "platform"
    lines.extend(
        (
            f"    handle {THEME_ROUTE} {{",
            "        rewrite * /theme.css",
            f"        root * {_quoted_path(theme_root)}",
            '        header Cache-Control "no-cache"',
            '        header X-Content-Type-Options "nosniff"',
            "        file_server",
            "    }",
        )
    )
    index_root = theme_root / "index"
    lines.extend(
        (
            f"    handle {INDEX_REGISTRY_ROUTE} {{",
            f"        rewrite * /registry-v1.json",
            f"        root * {_quoted_path(index_root)}",
            '        header Cache-Control "no-cache"',
            '        header X-Content-Type-Options "nosniff"',
            "        file_server",
            "    }",
            f"    handle_path {INDEX_ASSET_ROUTE}/* {{",
            f"        root * {_quoted_path(index_root)}",
            '        header X-Content-Type-Options "nosniff"',
            "        file_server",
            "    }",
        )
    )
    gallery_root = theme_root / "ui-gallery"
    lines.extend(
        (
            "    redir /_local-web/platform/ui-gallery /_local-web/platform/ui-gallery/ 308",
            "    handle_path /_local-web/platform/ui-gallery/* {",
            f"        root * {_quoted_path(gallery_root)}",
            '        header Cache-Control "no-store"',
            '        header X-Content-Type-Options "nosniff"',
            "        try_files {path} /index.html",
            "        file_server",
            "    }",
        )
    )
    for _manifest, app in apps:
        _render_backend_probe(lines, app)

    for manifest, app in apps:
        route = manifest.route
        service = manifest.service if manifest.kind == "service" else None
        split_service = service is not None and service.frontend_output is not None
        route_is_health = split_service and manifest.health_path == route
        if route_is_health:
            lines.extend(
                (
                    f"    handle_path {route} {{",
                    f"        reverse_proxy 127.0.0.1:{app.port}",
                    "    }",
                    f"    redir {route} {route}/ 308",
                )
            )
        else:
            lines.append(f"    redir {route} {route}/ 308")
        if manifest.kind == "static":
            lines.append(f"    handle_path {route}/* {{")
            root = RuntimeLayout(registry.runtime_root, app.id).current
            lines.append(f"        root * {_quoted_path(root)}")
            _render_frontend_cache_headers(lines, app.id, "        ")
            lines.append("        file_server")
            lines.append("    }")
        elif manifest.kind == "service":
            if app.port is None:
                raise ValueError(f"service app has no port: {app.id}")
            if not split_service:
                lines.append(f"    handle_path {route}/* {{")
                lines.append(f"        reverse_proxy 127.0.0.1:{app.port}")
                lines.append("    }")
            else:
                lines.append(f"    handle_path {route}/* {{")
                if not route_is_health:
                    health_path = manifest.health_path.removeprefix(route)
                    lines.append(f"        @{app.id}_health path {health_path}")
                    lines.append(f"        handle @{app.id}_health {{")
                    lines.append(f"            reverse_proxy 127.0.0.1:{app.port}")
                    lines.append("        }")
                for index, proxy_path in enumerate(service.proxy_paths):
                    matcher = f"{app.id.replace('-', '_')}_proxy_{index}"
                    lines.append(f"        @{matcher} path {proxy_path}*")
                    lines.append(f"        handle @{matcher} {{")
                    lines.append(f"            reverse_proxy 127.0.0.1:{app.port}")
                    lines.append("        }")
                release_root = RuntimeLayout(registry.runtime_root, app.id).current
                frontend_output = service.frontend_output
                if (
                    frontend_output.is_absolute()
                    or frontend_output == Path(".")
                    or ".." in frontend_output.parts
                ):
                    raise ValueError("split service frontend output must be a proper release descendant")
                root = release_root / frontend_output
                lines.append("        handle {")
                lines.append(f"            root * {_quoted_path(root)}")
                _render_frontend_cache_headers(lines, app.id, "            ")
                lines.append("            header {")
                lines.append('                X-Content-Type-Options "nosniff"')
                lines.append('                Referrer-Policy "no-referrer"')
                policy = _frontend_content_security_policy(manifest)
                lines.append(f'                Content-Security-Policy "{policy}"')
                lines.append("            }")
                lines.append("            try_files {path} /index.html")
                lines.append("            file_server")
                lines.append("        }")
                lines.append("    }")
        else:
            raise ValueError(f"unknown app kind: {manifest.kind}")

    lines.extend(
        (
            "    handle {",
            f"        root * {_quoted_path(Path(registry.runtime_root))}",
            "        rewrite * /home.html",
            "        file_server",
            "    }",
        )
    )
    route_lines = lines[len(opening) :]
    lines = ["    route {"]
    lines.extend(f"    {line}" for line in route_lines)
    lines.append("    }")
    probe_apps = [app for _manifest, app in apps if app.backend_probe is not None]
    if probe_apps:
        lines.append("    handle_errors {")
        for app in probe_apps:
            matcher = f"{app.id.replace('-', '_')}_backend_probe_error"
            route = remote_backend_health_path(app.id)
            lines.append(f"        @{matcher} path {route}")
            lines.append(f"        respond @{matcher} 502")
        lines.append("    }")
    lines.extend(("}", ""))
    site_body = lines
    lines = [*opening, *site_body]
    if prepare_tailscale_port_migration:
        lines.extend((":80 {", *site_body))
    if registry.ingress_mode == TAILSCALE_SERVE:
        # An unmatched host otherwise receives an empty 200 from Caddy, which
        # cannot distinguish a broken forwarded Host in a body-free HEAD probe.
        lines.extend((
            f"http://:{TAILSCALE_CADDY_PORT} {{",
            "    bind 127.0.0.1",
            "    respond 421",
            "}",
            "",
        ))
    return "\n".join(lines)


def _plist(value: dict[str, object]) -> bytes:
    return plistlib.dumps(value, fmt=plistlib.FMT_XML, sort_keys=True)


def render_caddy_plist(
    repository: Path, runtime_root: Path, environment: tuple[tuple[str, str], ...] = ()
) -> bytes:
    """Render Caddy's user LaunchAgent plist."""
    repository = Path(repository)
    runtime_root = Path(runtime_root)
    _quoted_path(repository)
    _quoted_path(runtime_root)
    values: dict[str, str] = {}
    for pair in environment:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise ValueError("invalid Caddy environment")
        name, value = pair
        if not isinstance(name, str) or not _ENVIRONMENT_NAME.fullmatch(name) or not isinstance(value, str):
            raise ValueError("invalid Caddy environment")
        if name in values:
            raise ValueError("duplicate Caddy environment name")
        values[name] = value
    payload: dict[str, object] = {
            "Label": "com.sean.local-web.caddy",
            "ProgramArguments": [
                "/opt/homebrew/bin/caddy",
                "run",
                "--config",
                str(runtime_root / "Caddyfile"),
            ],
            "RunAtLoad": True,
            "KeepAlive": True,
            "WorkingDirectory": str(repository),
            "StandardOutPath": str(runtime_root / "logs" / "caddy.out.log"),
            "StandardErrorPath": str(runtime_root / "logs" / "caddy.err.log"),
    }
    if environment:
        payload["EnvironmentVariables"] = dict(sorted(environment))
    return _plist(payload)


def render_service_plist(host: HostApp, manifest: AppManifest, layout: RuntimeLayout) -> bytes:
    """Render one service's LaunchAgent using its exact registered command."""
    if host.id != manifest.id or host.id != layout.app_id:
        raise ValueError("host, manifest, and runtime layout must name the same app")
    if manifest.kind != "service" or host.start_command is None:
        raise ValueError(f"service app requires a start command: {host.id}")
    if not Path(host.start_command.argv[0]).is_absolute():
        raise ValueError(f"service command must be absolute: {host.start_command.argv[0]}")
    _quoted_path(layout.current)
    return _plist(
        {
            "Label": f"com.sean.local-web.{host.id}",
            "ProgramArguments": list(host.start_command.argv),
            "EnvironmentVariables": {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            },
            "RunAtLoad": True,
            "KeepAlive": True,
            "WorkingDirectory": str(layout.current),
            "StandardOutPath": str(layout.service_logs / f"{host.id}.out.log"),
            "StandardErrorPath": str(layout.service_logs / f"{host.id}.err.log"),
        }
    )


def render_hook(app_id: str, platform_repository: Path) -> str:
    """Render a managed, non-blocking Git hook for one validated app ID."""
    if not _APP_ID.fullmatch(app_id):
        raise ValueError(f"invalid app id: {app_id}")
    command = Path(platform_repository) / "bin/local-web"
    if not command.is_absolute():
        raise ValueError("platform repository must be absolute")
    deploy_command = " ".join(
        (
            "/opt/homebrew/bin/python3",
            shlex.quote(str(command)),
            "deploy",
            app_id,
            "--from-hook",
        )
    )
    return "\n".join(
        (
            "#!/bin/sh",
            "# managed-by-local-web-server",
            "branch=$(/usr/bin/git symbolic-ref --quiet --short HEAD 2>/dev/null) || exit 0",
            '[ "$branch" = main ] || exit 0',
            deploy_command,
            "result=$?",
            'if [ "$result" -ne 0 ]; then',
            f'    echo "Local web deployment failed for {app_id}; the Git operation remains successful." >&2',
            "fi",
            "exit 0",
            "",
        )
    )
