"""Strict parsers for public app manifests and host-local configuration."""

import base64
import binascii
import ipaddress
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .icons import canonical_manifest_icon
from .models import (
    AppManifest,
    BackendProbeSpec,
    BuildSpec,
    Command,
    FrontendSecuritySpec,
    HomePresentation,
    HostApp,
    HostRegistry,
    PlatformSpec,
    ReleaseEntry,
    ServiceSpec,
)
from .public_origin import (
    TAILSCALE_CADDY_PORT,
    TAILSCALE_SERVE,
    TRUSTED_LAN,
    parse_tailscale_public_origin,
)
from .service_command import ServiceCommandTemplateError, validate_service_command_template


class ConfigError(ValueError):
    """A manifest or registry does not satisfy the local web server contract."""


class RegistryRepositoryConflictError(ConfigError):
    """A registry contains more than one entry for a canonical repository."""


_APP_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_ROUTE = re.compile(r"^/[a-z0-9][a-z0-9/-]*$")
_HEALTH_PATH = re.compile(r"^/[A-Za-z0-9._~/-]*$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_KEY_NAMES = {"password", "token", "secret", "key"}
_SECRET_ENV_COMPONENTS = ("PASSWORD", "TOKEN", "SECRET", "KEY")
_PROCESS_ENVIRONMENT = ("PATH", "TMPDIR", "LANG", "LC_ALL")
_HOME_ACCENT = re.compile(r"^#[0-9A-F]{6}$")
_UI_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_HEADER_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_BASE64URL_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")
_PUBLISHABLE_SUPABASE_KEY = re.compile(r"^sb_publishable_[A-Za-z0-9_-]{1,480}$")
_SAFE_PROBE_HEADER_VALUE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,4096}$")
_SAFE_ORIGIN_HOST = re.compile(r"^[A-Za-z0-9.-]+$")
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_FORBIDDEN_PROBE_HEADERS = {
    "authorization",
    "cookie",
    "host",
    "connection",
    "forwarded",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "keep-alive",
}
SUPPORTED_PLATFORM_CONTRACT = 1
_SUPPORTED_PLATFORM_CAPABILITIES = {"supabase"}


def _read_object(path: Path, context: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read {context}: {path}") from error
    return _object(value, context)


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{context} must be an object")
    return value


def _reject_unknown_keys(value: Mapping[str, Any], allowed: set[str], context: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"{context} contains unknown key: {sorted(unknown)[0]}")


def _require_keys(value: Mapping[str, Any], required: set[str], context: str) -> None:
    missing = required - set(value)
    if missing:
        raise ConfigError(f"{context} is missing required key: {sorted(missing)[0]}")


def _string(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{context} must be a non-empty string")
    return value


def _absolute_path(value: Any, context: str) -> Path:
    path = Path(_string(value, context))
    if not path.is_absolute():
        raise ConfigError(f"{context} must be an absolute path")
    return path


def _schema_version(value: Any, context: str) -> int:
    if type(value) is not int or value != 1:
        raise ConfigError(f"{context} must be 1")
    return value


def _registry_schema_version(value: Any) -> int:
    if type(value) is not int or value not in (1, 2):
        raise ConfigError("registry.schemaVersion must be 1 or 2")
    return value


def _app_id(value: Any, context: str) -> str:
    identifier = _string(value, context)
    if not _APP_ID.fullmatch(identifier):
        raise ConfigError(f"{context} must match {_APP_ID.pattern}")
    return identifier


def _route(value: Any, context: str) -> str:
    route = _string(value, context)
    if (
        not _ROUTE.fullmatch(route)
        or ".." in route
        or "//" in route
        or route.endswith("/")
        or "?" in route
        or "#" in route
    ):
        raise ConfigError(f"{context} is invalid")
    return route


def _health_path(value: Any, context: str) -> str:
    health_path = _string(value, context)
    if (
        not _HEALTH_PATH.fullmatch(health_path)
        or ".." in health_path
        or "//" in health_path
        or "." in health_path.split("/")
        or "?" in health_path
        or "#" in health_path
    ):
        raise ConfigError(f"{context} is invalid")
    return health_path


def _proxy_paths(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("service.proxyPaths must be a non-empty list")
    paths: list[str] = []
    for path in value:
        if (
            not isinstance(path, str)
            or not path
            or path == "/"
            or not _HEALTH_PATH.fullmatch(path)
            or ".." in path
            or "//" in path
            or path.endswith("/")
            or "." in path.split("/")
            or "%" in path
            or "?" in path
            or "#" in path
        ):
            raise ConfigError("service.proxyPaths is invalid")
        paths.append(path)
    if len(set(paths)) != len(paths):
        raise ConfigError("service.proxyPaths contains duplicate paths")
    return tuple(paths)


def _command(value: Any, context: str) -> Command:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{context} must be a non-empty list")
    if any(not isinstance(argument, str) or not argument for argument in value):
        raise ConfigError(f"{context} must contain non-empty strings")
    return Command(tuple(value))


def _relative_path(value: Any, context: str) -> Path:
    output = _string(value, context)
    candidate = Path(output)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigError(f"{context} must be relative and cannot contain ..")
    return candidate


def _relative_output(value: Any) -> Path:
    return _relative_path(value, "build.output")


def _canonical_release_path(value: Any, context: str) -> Path:
    text = _string(value, context)
    candidate = Path(text)
    if (
        candidate.is_absolute()
        or candidate == Path(".")
        or ".." in candidate.parts
        or "\\" in text
        or any(not segment or segment in {".", ".."} for segment in text.split("/"))
        or candidate.as_posix() != text
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        raise ConfigError(f"{context} must be a canonical relative path")
    return candidate


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _parse_release(value: Any, output: Path) -> tuple[ReleaseEntry, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("build.release must be a non-empty list")
    entries: list[ReleaseEntry] = []
    for item in value:
        entry = _object(item, "build.release entry")
        _reject_unknown_keys(entry, {"source", "target"}, "build.release entry")
        _require_keys(entry, {"source", "target"}, "build.release entry")
        source = _canonical_release_path(entry["source"], "build.release.source")
        target = _canonical_release_path(entry["target"], "build.release.target")
        if source == output or _is_relative_to(source, output):
            raise ConfigError("build.release.source cannot be inside build.output")
        entries.append(ReleaseEntry(source, target))
    targets = [entry.target for entry in entries]
    for index, target in enumerate(targets):
        if any(
            target == other
            or _is_relative_to(target, other)
            or _is_relative_to(other, target)
            for other in targets[index + 1 :]
        ):
            raise ConfigError("build.release targets must not overlap")
    return tuple(entries)


def _frontend_output(value: Any) -> Path:
    context = "service.frontendOutput"
    output = _string(value, context)
    candidate = Path(output)
    if (
        candidate.is_absolute()
        or candidate == Path(".")
        or ".." in candidate.parts
        or "\\" in output
        or any(not segment or segment in {".", ".."} for segment in output.split("/"))
        or candidate.as_posix() != output
        or any(ord(character) < 32 or ord(character) == 127 for character in output)
    ):
        raise ConfigError(
            f"{context} must be a canonical relative path and a proper release descendant"
        )
    return candidate


def _environment_names(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(name, str) for name in value):
        raise ConfigError(f"{context} must be a list of strings")
    if any(not _ENV_NAME.fullmatch(name) for name in value):
        raise ConfigError(f"{context} contains an invalid environment name")
    if len(set(value)) != len(value):
        raise ConfigError(f"{context} contains duplicate environment names")
    return tuple(value)


def _parse_build(value: Any) -> BuildSpec:
    build = _object(value, "build")
    _reject_unknown_keys(build, {"commands", "output", "environment", "release"}, "build")
    _require_keys(build, {"commands", "output", "environment"}, "build")
    commands = build["commands"]
    if not isinstance(commands, list) or not commands:
        raise ConfigError("build.commands must be a non-empty list")
    output = _relative_output(build["output"])
    return BuildSpec(
        commands=tuple(_command(command, "build.commands") for command in commands),
        output=output,
        environment=_environment_names(build["environment"], "build.environment"),
        release_entries=(
            _parse_release(build["release"], output) if "release" in build else ()
        ),
    )


def _service_start_command(value: Any) -> Command:
    command = _command(value, "service.startCommand")
    try:
        validate_service_command_template(command.argv)
    except ServiceCommandTemplateError as error:
        raise ConfigError(str(error)) from error
    return command


def _canonical_https_origin(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ConfigError(f"{context} must contain exact HTTPS origins")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ConfigError(f"{context} must contain exact HTTPS origins") from error
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or host is None
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(f"{context} must contain exact HTTPS origins")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if (
            host != host.lower()
            or len(host) > 253
            or not _SAFE_ORIGIN_HOST.fullmatch(host)
            or not all(_DNS_LABEL.fullmatch(label) for label in host.split("."))
            or (host.replace(".", "").isdigit() and "." in host)
        ):
            raise ConfigError(f"{context} must contain exact HTTPS origins")
        canonical_host = host
    else:
        canonical_host = address.compressed

    if ":" in canonical_host:
        canonical_host = f"[{canonical_host}]"
    canonical_port = "" if port in {None, 443} else f":{port}"
    return f"https://{canonical_host}{canonical_port}"


def _frontend_security_sources(
    value: Any,
    context: str,
    *,
    allow_blob: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{context} must be a non-empty list")
    sources = []
    for item in value:
        if allow_blob and item == "blob:":
            sources.append(item)
            continue
        try:
            sources.append(_canonical_https_origin(item, context))
        except ConfigError as error:
            if allow_blob:
                raise ConfigError(
                    f"{context} must contain exact HTTPS origins or blob:"
                ) from error
            raise
    if len(set(sources)) != len(sources):
        duplicate_kind = "sources" if allow_blob else "origins"
        raise ConfigError(f"{context} contains duplicate {duplicate_kind}")
    return tuple(sorted(sources))


def _frontend_blob_sources(value: Any, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{context} must be a non-empty list")
    if any(item != "blob:" for item in value):
        raise ConfigError(f"{context} must contain only blob:")
    if len(set(value)) != len(value):
        raise ConfigError(f"{context} contains duplicate sources")
    return tuple(sorted(value))


def _parse_frontend_security(value: Any) -> FrontendSecuritySpec:
    context = "service.frontendSecurity"
    security = _object(value, context)
    _reject_unknown_keys(
        security,
        {"connectSources", "imgSources", "workerSources", "childSources"},
        context,
    )
    if not security:
        raise ConfigError(f"{context} must declare at least one source directive")
    return FrontendSecuritySpec(
        connect_sources=(
            _frontend_security_sources(
                security["connectSources"], f"{context}.connectSources"
            )
            if "connectSources" in security
            else ()
        ),
        img_sources=(
            _frontend_security_sources(
                security["imgSources"],
                f"{context}.imgSources",
                allow_blob=True,
            )
            if "imgSources" in security
            else ()
        ),
        worker_sources=(
            _frontend_blob_sources(
                security["workerSources"], f"{context}.workerSources"
            )
            if "workerSources" in security
            else ()
        ),
        child_sources=(
            _frontend_blob_sources(
                security["childSources"], f"{context}.childSources"
            )
            if "childSources" in security
            else ()
        ),
    )


def _parse_service(value: Any, route: str, health_path: str) -> ServiceSpec:
    service = _object(value, "service")
    _reject_unknown_keys(
        service,
        {
            "module",
            "internalHealthPath",
            "frontendOutput",
            "proxyPaths",
            "startCommand",
            "frontendSecurity",
        },
        "service",
    )
    _require_keys(service, {"module", "internalHealthPath"}, "service")
    has_frontend_output = "frontendOutput" in service
    has_proxy_paths = "proxyPaths" in service
    if has_frontend_output != has_proxy_paths:
        raise ConfigError("service.frontendOutput and service.proxyPaths must be supplied together")
    if "frontendSecurity" in service and not has_frontend_output:
        raise ConfigError("service.frontendSecurity requires a split service frontend")
    proxy_paths = _proxy_paths(service["proxyPaths"]) if has_proxy_paths else ()
    for proxy_path in proxy_paths:
        public_prefix = f"{route}{proxy_path}"
        if health_path.startswith(public_prefix):
            raise ConfigError("service.proxyPaths collides with manifest.healthPath")
    return ServiceSpec(
        module=_string(service["module"], "service.module"),
        internal_health_path=_health_path(service["internalHealthPath"], "service.internalHealthPath"),
        frontend_output=(
            _frontend_output(service["frontendOutput"])
            if has_frontend_output
            else None
        ),
        proxy_paths=proxy_paths,
        start_command=(
            _service_start_command(service["startCommand"])
            if "startCommand" in service
            else None
        ),
        frontend_security=(
            _parse_frontend_security(service["frontendSecurity"])
            if "frontendSecurity" in service
            else None
        ),
    )


def _validate_service_release(build: BuildSpec, service: ServiceSpec | None) -> None:
    if (
        service is None
        or service.frontend_output is None
        or not build.release_entries
    ):
        return
    frontend = service.frontend_output
    if not any(
        entry.target == frontend or entry.target in frontend.parents
        for entry in build.release_entries
    ):
        raise ConfigError(
            "service.frontendOutput must be contained by a build.release target"
        )


def _parse_home(value: Any) -> HomePresentation:
    home = _object(value, "home")
    _reject_unknown_keys(home, {"icon", "accent"}, "home")
    _require_keys(home, {"icon", "accent"}, "home")
    icon = _string(home["icon"], "home.icon")
    try:
        canonical_manifest_icon(icon)
    except ValueError:
        raise ConfigError("home.icon is unknown")
    accent = _string(home["accent"], "home.accent")
    if not _HOME_ACCENT.fullmatch(accent):
        raise ConfigError("home.accent must match #RRGGBB")
    return HomePresentation(icon, accent)


def _parse_platform(value: Any) -> PlatformSpec:
    platform = _object(value, "platform")
    _reject_unknown_keys(
        platform,
        {"contractVersion", "templateVersion", "uiVersion", "capabilities"},
        "platform",
    )
    _require_keys(
        platform,
        {"contractVersion", "templateVersion", "uiVersion", "capabilities"},
        "platform",
    )
    if (
        type(platform["contractVersion"]) is not int
        or platform["contractVersion"] != SUPPORTED_PLATFORM_CONTRACT
    ):
        raise ConfigError("platform.contractVersion is unsupported")
    template_version = platform["templateVersion"]
    if type(template_version) is not int or template_version <= 0:
        raise ConfigError("platform.templateVersion must be a positive integer")
    ui_version = platform["uiVersion"]
    if not isinstance(ui_version, str) or not _UI_VERSION.fullmatch(ui_version):
        raise ConfigError("platform.uiVersion must match x.y.z")
    capabilities = platform["capabilities"]
    if not isinstance(capabilities, list) or any(
        not isinstance(capability, str) for capability in capabilities
    ):
        raise ConfigError("platform.capabilities must be a list of strings")
    if len(set(capabilities)) != len(capabilities) or capabilities != sorted(capabilities):
        raise ConfigError("platform.capabilities must be unique and lexically sorted")
    if any(capability not in _SUPPORTED_PLATFORM_CAPABILITIES for capability in capabilities):
        raise ConfigError("platform.capabilities contains unsupported capability")
    return PlatformSpec(
        contract_version=SUPPORTED_PLATFORM_CONTRACT,
        template_version=template_version,
        ui_version=ui_version,
        capabilities=tuple(capabilities),
    )


def load_manifest(path: Path) -> AppManifest:
    """Load a schema-version-one app manifest from *path*."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"cannot read manifest: {path}") from error
    return parse_manifest(content, source=str(path))


def parse_manifest(content: str, *, source: str = "manifest") -> AppManifest:
    """Parse and validate manifest JSON obtained from a pinned source."""
    try:
        manifest = _object(json.loads(content), "manifest")
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ConfigError(f"cannot read manifest: {source}") from error
    _require_keys(
        manifest,
        {"schemaVersion", "id", "title", "route", "kind", "build", "healthPath"},
        "manifest",
    )
    route = _route(manifest["route"], "manifest.route")
    _reject_unknown_keys(
        manifest,
        {
            "schemaVersion", "id", "title", "route", "kind", "build", "healthPath", "service",
            "home", "platform",
        },
        "manifest",
    )
    schema_version = _schema_version(manifest["schemaVersion"], "manifest.schemaVersion")
    kind = _string(manifest["kind"], "manifest.kind")
    if kind not in {"static", "service"}:
        raise ConfigError("manifest.kind must be static or service")
    health_path = _health_path(manifest["healthPath"], "manifest.healthPath")
    if health_path != route and not health_path.startswith(f"{route}/"):
        raise ConfigError("manifest.healthPath must equal route or begin with route + '/'")

    build = _parse_build(manifest["build"])
    service_value = manifest.get("service")
    if kind == "service":
        if service_value is None:
            raise ConfigError("service manifest requires service")
        service = _parse_service(service_value, route, health_path)
    else:
        if "service" in manifest:
            raise ConfigError("static manifest cannot declare service")
        service = None
    _validate_service_release(build, service)

    return AppManifest(
        schema_version=schema_version,
        id=_app_id(manifest["id"], "manifest.id"),
        title=_string(manifest["title"], "manifest.title"),
        route=route,
        kind=kind,
        build=build,
        health_path=health_path,
        service=service,
        home=_parse_home(manifest["home"]) if "home" in manifest else HomePresentation(),
        platform=(
            _parse_platform(manifest["platform"])
            if "platform" in manifest
            else None
        ),
    )


def _contains_secret_key(value: Mapping[str, Any]) -> str | None:
    for key in value:
        if key.lower() in _SECRET_KEY_NAMES:
            return key
    return None


def _fixed_environment(value: Any) -> tuple[tuple[str, str], ...]:
    environment = _object(value, "app.environment")
    secret_key = _contains_secret_key(environment)
    if secret_key is not None:
        raise ConfigError(f"registry cannot contain secret key: {secret_key}")
    pairs: list[tuple[str, str]] = []
    for name, fixed_value in environment.items():
        if not _ENV_NAME.fullmatch(name):
            raise ConfigError(f"app.environment contains invalid name: {name}")
        if any(component in name.upper() for component in _SECRET_ENV_COMPONENTS):
            raise ConfigError(f"app.environment cannot set secret name: {name}")
        if not isinstance(fixed_value, str):
            raise ConfigError(f"app.environment value for {name} must be a string")
        pairs.append((name, fixed_value))
    return tuple(pairs)


def _probe_path(value: Any) -> str:
    path = _string(value, "app.backendProbe.path")
    if (
        path == "/"
        or not _HEALTH_PATH.fullmatch(path)
        or ".." in path
        or "//" in path
        or path.endswith("/")
        or "." in path.split("/")
        or "%" in path
        or "?" in path
        or "#" in path
    ):
        raise ConfigError("app.backendProbe.path is invalid")
    return path


def _parse_probe_headers(value: Any) -> tuple[tuple[str, str], ...]:
    headers = _object(value, "app.backendProbe.headersFromEnvironment")
    pairs: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    for name, environment_name in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise ConfigError("app.backendProbe.headersFromEnvironment is invalid")
        normalized = name.lower()
        if (
            normalized in _FORBIDDEN_PROBE_HEADERS
            or normalized.startswith("proxy-")
            or normalized.startswith("x-forwarded-")
            or normalized in seen_names
        ):
            raise ConfigError("app.backendProbe.headersFromEnvironment is invalid")
        seen_names.add(normalized)
        pairs.append(
            (
                name,
                _environment_names([environment_name], "app.backendProbe.headersFromEnvironment")[0],
            )
        )
    return tuple(pairs)


def _parse_backend_probe(value: Any) -> BackendProbeSpec:
    probe = _object(value, "app.backendProbe")
    _reject_unknown_keys(
        probe,
        {"publicEnvironment", "baseUrlEnvironment", "path", "headersFromEnvironment"},
        "app.backendProbe",
    )
    _require_keys(
        probe,
        {"publicEnvironment", "baseUrlEnvironment", "path", "headersFromEnvironment"},
        "app.backendProbe",
    )
    return BackendProbeSpec(
        public_environment=_environment_names(
            probe["publicEnvironment"], "app.backendProbe.publicEnvironment"
        ),
        base_url_environment=_environment_names(
            [probe["baseUrlEnvironment"]], "app.backendProbe.baseUrlEnvironment"
        )[0],
        path=_probe_path(probe["path"]),
        headers_from_environment=_parse_probe_headers(probe["headersFromEnvironment"]),
    )


def _probe_references(probe: BackendProbeSpec) -> set[str]:
    return {probe.base_url_environment} | {
        name for _header, name in probe.headers_from_environment
    }


def _validate_backend_probe_declaration(host: HostApp, manifest: AppManifest) -> None:
    probe = host.backend_probe
    if probe is None:
        return
    if manifest.kind != "static":
        raise ConfigError("app.backendProbe is allowed only for static manifests")
    references = _probe_references(probe)
    public_names = set(probe.public_environment)
    if references - public_names:
        raise ConfigError("app.backendProbe.publicEnvironment is missing a referenced name")
    if public_names - references:
        raise ConfigError("app.backendProbe.publicEnvironment contains an unused name")
    if references - set(manifest.build.environment):
        raise ConfigError("app.backendProbe references a name outside build.environment")


def _decode_jwt_part(part: str) -> Any:
    padding = "=" * (-len(part) % 4)
    decoded = base64.b64decode(
        (part + padding).encode("ascii"), altchars=b"-_", validate=True
    )
    return json.loads(decoded.decode("utf-8"))


def _is_publishable_supabase_key(value: str) -> bool:
    if value.startswith("sb_publishable_"):
        return _PUBLISHABLE_SUPABASE_KEY.fullmatch(value) is not None
    if len(value) > 4096:
        return False
    parts = value.split(".")
    if len(parts) != 3 or any(not _BASE64URL_SEGMENT.fullmatch(part) for part in parts):
        return False
    try:
        header = _decode_jwt_part(parts[0])
        payload = _decode_jwt_part(parts[1])
        base64.b64decode(parts[2].encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeError, ValueError, json.JSONDecodeError, binascii.Error):
        return False
    return isinstance(header, dict) and isinstance(payload, dict) and payload.get("role") == "anon"


def _validate_probe_origin(value: str) -> None:
    try:
        origin = urlsplit(value)
        port = origin.port
    except ValueError:
        raise ConfigError("app.backendProbe.baseUrlEnvironment is invalid") from None
    authority = origin.netloc
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username is not None
        or origin.password is not None
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
        or not authority.isascii()
        or not _SAFE_ORIGIN_HOST.fullmatch(origin.hostname)
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in authority)
        or "%" in authority
        or "\\" in authority
        or origin.path not in {"", "/"}
        or origin.query
        or origin.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ConfigError("app.backendProbe.baseUrlEnvironment is invalid")


def _resolved_probe_environment(manifest: AppManifest, host: HostApp) -> dict[str, str]:
    probe = host.backend_probe
    if probe is None:
        return {}
    allowed_names = set(probe.public_environment)
    values: dict[str, str] = {}
    if host.environment_file is not None:
        values.update(_environment_file_values(host.environment_file, allowed_names))
    values.update({name: value for name, value in host.environment if name in allowed_names})
    for name in _probe_references(probe):
        value = values.get(name)
        if not isinstance(value, str) or not value:
            raise ConfigError("app.backendProbe is missing a resolved public input")
    _validate_probe_origin(values[probe.base_url_environment])
    for header_name, environment_name in probe.headers_from_environment:
        value = values[environment_name]
        if not _SAFE_PROBE_HEADER_VALUE.fullmatch(value):
            raise ConfigError("app.backendProbe.headersFromEnvironment is invalid")
        if header_name.lower() == "apikey" and not _is_publishable_supabase_key(value):
            raise ConfigError("app.backendProbe.headersFromEnvironment is invalid")
    return {name: values[name] for name in probe.public_environment}


def _parse_host_app(value: Any) -> HostApp:
    app = _object(value, "app")
    secret_key = _contains_secret_key(app)
    if secret_key is not None:
        raise ConfigError(f"registry cannot contain secret key: {secret_key}")
    _reject_unknown_keys(
        app,
        {
            "id",
            "repository",
            "autoDeploy",
            "environmentFile",
            "environment",
            "port",
            "startCommand",
            "backendProbe",
        },
        "app",
    )
    _require_keys(app, {"id", "repository", "autoDeploy"}, "app")
    if type(app["autoDeploy"]) is not bool:
        raise ConfigError("app.autoDeploy must be a boolean")

    port = app.get("port")
    if port is not None:
        if type(port) is not int or not 1 <= port <= 65535:
            raise ConfigError("app.port must be an integer between 1 and 65535")
        if port == 80:
            raise ConfigError("app.port 80 is reserved for Caddy")

    start_command_value = app.get("startCommand")
    return HostApp(
        id=_app_id(app["id"], "app.id"),
        repository=_absolute_path(app["repository"], "app.repository"),
        auto_deploy=app["autoDeploy"],
        environment_file=(
            _absolute_path(app["environmentFile"], "app.environmentFile")
            if "environmentFile" in app
            else None
        ),
        environment=_fixed_environment(app["environment"]) if "environment" in app else (),
        port=port,
        start_command=(
            _command(start_command_value, "app.startCommand")
            if start_command_value is not None
            else None
        ),
        backend_probe=(
            _parse_backend_probe(app["backendProbe"])
            if "backendProbe" in app
            else None
        ),
    )


def load_registry(path: Path) -> HostRegistry:
    """Load and normalize a non-secret host registry from *path*."""
    registry = _read_object(path, "registry")
    secret_key = _contains_secret_key(registry)
    if secret_key is not None:
        raise ConfigError(f"registry cannot contain secret key: {secret_key}")
    _require_keys(registry, {"schemaVersion"}, "registry")
    schema_version = _registry_schema_version(registry["schemaVersion"])

    if schema_version == 1:
        _reject_unknown_keys(
            registry,
            {"schemaVersion", "host", "runtimeRoot", "apps"},
            "registry",
        )
        _require_keys(
            registry,
            {"schemaVersion", "host", "runtimeRoot", "apps"},
            "registry",
        )
        host = _string(registry["host"], "registry.host")
        public_origin = f"http://{host}"
        ingress_mode = TRUSTED_LAN
    else:
        _reject_unknown_keys(
            registry,
            {"schemaVersion", "publicOrigin", "ingressMode", "runtimeRoot", "apps"},
            "registry",
        )
        _require_keys(
            registry,
            {"schemaVersion", "publicOrigin", "ingressMode", "runtimeRoot", "apps"},
            "registry",
        )
        if registry["ingressMode"] != TAILSCALE_SERVE:
            raise ConfigError("registry.ingressMode must be tailscale-serve")
        try:
            origin = parse_tailscale_public_origin(registry["publicOrigin"])
        except ValueError as error:
            raise ConfigError("registry.publicOrigin is invalid") from error
        host = origin.host
        public_origin = origin.value
        ingress_mode = TAILSCALE_SERVE

    if not isinstance(registry["apps"], list):
        raise ConfigError("registry.apps must be a list")

    raw_apps = tuple(_object(app, "app") for app in registry["apps"])
    ids = [_app_id(app.get("id"), "app.id") for app in raw_apps]
    if len(set(ids)) != len(ids):
        raise ConfigError("duplicate app id")
    apps = tuple(_parse_host_app(app) for app in raw_apps)
    repositories = [app.repository.resolve() for app in apps]
    if len(set(repositories)) != len(repositories):
        raise RegistryRepositoryConflictError("duplicate app repository")
    ports = [app.port for app in apps if app.port is not None]
    if ingress_mode == TAILSCALE_SERVE and TAILSCALE_CADDY_PORT in ports:
        raise ConfigError("app.port 8080 is reserved for Caddy")
    if len(set(ports)) != len(ports):
        raise ConfigError("duplicate service port")
    return HostRegistry(
        host=host,
        runtime_root=_absolute_path(registry["runtimeRoot"], "registry.runtimeRoot"),
        apps=apps,
        schema_version=schema_version,
        public_origin=public_origin,
        ingress_mode=ingress_mode,
    )


class _RegistryBytes:
    """Expose exact in-memory bytes through the registry reader interface."""

    def __init__(self, content: bytes):
        self._content = content

    def read_text(self, *, encoding: str) -> str:
        return self._content.decode(encoding, errors="strict")

    def __str__(self) -> str:
        return "private host registry"


def load_registry_bytes(content: bytes) -> HostRegistry:
    """Parse exact host-registry bytes without reopening a filesystem path."""

    if type(content) is not bytes:
        raise ConfigError("cannot read registry: private host registry")
    return load_registry(_RegistryBytes(content))


def registered_host(registry: HostRegistry, app_id: str) -> HostApp:
    """Return one registered host entry without consulting its working tree."""
    host = next((app for app in registry.apps if app.id == app_id), None)
    if host is None:
        raise ConfigError(f"registered app not found: {app_id}")
    return host


def validate_registered_app(host: HostApp, manifest: AppManifest) -> tuple[HostApp, AppManifest]:
    """Verify that a pinned manifest and its host entry form a valid app."""
    if manifest.id != host.id:
        raise ConfigError("registered app id does not match manifest id")
    if manifest.kind == "static" and (host.port is not None or host.start_command is not None):
        raise ConfigError("static host entry cannot declare port or startCommand")
    if manifest.kind == "service" and (host.port is None or host.start_command is None):
        raise ConfigError("service host entry requires port and startCommand")
    _validate_backend_probe_declaration(host, manifest)
    _resolved_probe_environment(manifest, host)
    return host, manifest


def load_registered_app(registry: HostRegistry, app_id: str) -> tuple[HostApp, AppManifest]:
    """Load and verify an app manifest from its current working tree."""
    host = registered_host(registry, app_id)
    return validate_registered_app(host, load_manifest(host.repository / "local-web.json"))


def _environment_file_values(path: Path, allowed_names: set[str]) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ConfigError(f"cannot read environment file: {path}") from error

    values: dict[str, str] = {}
    for number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in line:
            raise ConfigError(f"environment file line {number} is malformed")
        name, value = line.split("=", 1)
        if not _ENV_NAME.fullmatch(name):
            raise ConfigError(f"environment file line {number} has malformed name")
        if name in allowed_names:
            values[name] = value
    return values


def build_environment(manifest: AppManifest, host: HostApp, base: Mapping[str, str]) -> dict[str, str]:
    """Construct the minimal, allowlisted subprocess environment for an app build."""
    environment = {
        name: value
        for name in _PROCESS_ENVIRONMENT
        if isinstance(value := base.get(name), str)
    }
    allowed_names = set(manifest.build.environment)
    if host.environment_file is not None:
        environment.update(_environment_file_values(host.environment_file, allowed_names))
    environment.update({name: value for name, value in host.environment if name in allowed_names})
    return environment


def probe_environment(manifest: AppManifest, host: HostApp) -> dict[str, str]:
    """Resolve the validated public inputs of a declared remote backend probe."""
    validate_registered_app(host, manifest)
    return _resolved_probe_environment(manifest, host)
