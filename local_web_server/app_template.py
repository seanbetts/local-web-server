"""Strict renderer for the canonical generated React application."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .icons import canonical_manifest_icon
from .nas_recipe import ContainerBuildRecipe, ProductionRecipe, production_declaration, parse_production_recipe
from .native_build import NativeDependencyRecipe
from .process_runner import ProcessCommand


_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates" / "react-app"
_MARKER = re.compile(r"__[A-Z0-9_]+__")
_APP_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_TITLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]*$")
_ROUTE = re.compile(r"^/[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*$")
_ACCENT = re.compile(r"^#[0-9A-F]{6}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_CAPABILITIES = frozenset({"supabase"})
_ALLOWED_MARKERS = frozenset(
    {
        "__APP_ID__",
        "__APP_TITLE__",
        "__APP_ROUTE__",
        "__APP_ICON__",
        "__APP_ACCENT__",
        "__UI_VERSION__",
        "__CAPABILITIES_JSON__",
        "__APP_KIND__",
        "__BUILD_OUTPUT__",
        "__BUILD_RELEASE__",
        "__HEALTH_PATH__",
        "__SERVICE_MANIFEST__",
        "__SERVICE_SCRIPT__",
        "__CHECK_SCRIPT__",
        "__APP_ARCHITECTURE__",
        "__SERVICE_DEVELOPMENT__",
        "__PRODUCTION_MANIFEST__",
    }
)


class TemplateError(ValueError):
    """Template inputs or sources violate the canonical contract."""


@dataclass(frozen=True)
class TemplateInputs:
    app_id: str
    title: str
    route: str
    icon: str
    accent: str
    ui_version: str
    ui_sha256: str
    capabilities: tuple[str, ...]
    kind: str = "static"
    production_recipe: ProductionRecipe | None = None


@dataclass(frozen=True)
class RenderedFile:
    path: Path
    content: str


def _reject_marker_characters(value: str) -> None:
    if "__" in value:
        raise TemplateError("template inputs cannot contain marker syntax")


def _validate(inputs: TemplateInputs) -> None:
    if type(inputs.capabilities) is not tuple:
        raise TemplateError("capabilities must be an immutable tuple")
    values = (
        inputs.app_id,
        inputs.title,
        inputs.route,
        inputs.icon,
        inputs.accent,
        inputs.ui_version,
        inputs.ui_sha256,
        *inputs.capabilities,
        inputs.kind,
    )
    if any(type(value) is not str for value in values):
        raise TemplateError("template inputs must be strings")
    for value in values:
        _reject_marker_characters(value)
    if not _APP_ID.fullmatch(inputs.app_id):
        raise TemplateError("app_id is invalid")
    if not _TITLE.fullmatch(inputs.title):
        raise TemplateError("title is invalid")
    if not _ROUTE.fullmatch(inputs.route):
        raise TemplateError("route is invalid")
    try:
        if canonical_manifest_icon(inputs.icon) != inputs.icon:
            raise ValueError
    except ValueError as error:
        raise TemplateError("icon is invalid") from error
    if not _ACCENT.fullmatch(inputs.accent):
        raise TemplateError("accent is invalid")
    if not _VERSION.fullmatch(inputs.ui_version):
        raise TemplateError("ui_version is invalid")
    if not _SHA256.fullmatch(inputs.ui_sha256):
        raise TemplateError("ui_sha256 is invalid")
    if inputs.kind not in {"static", "service"}:
        raise TemplateError("kind is invalid")
    if inputs.production_recipe is not None:
        try:
            if type(inputs.production_recipe) is not ProductionRecipe:
                raise ValueError
            inputs.production_recipe.validate_kind(inputs.kind)
        except ValueError as error:
            raise TemplateError("production recipe is invalid for the app kind") from error
    if (
        inputs.capabilities != tuple(sorted(inputs.capabilities))
        or len(inputs.capabilities) != len(set(inputs.capabilities))
        or any(item not in _SUPPORTED_CAPABILITIES for item in inputs.capabilities)
    ):
        raise TemplateError("capabilities are invalid")


def _destination(source: Path, suffix: str) -> Path:
    relative = source.relative_to(_TEMPLATE_ROOT).as_posix()
    if not relative.endswith(suffix):
        raise TemplateError("template source has an invalid name")
    return Path(relative.removesuffix(suffix))


def render_template(inputs: TemplateInputs) -> tuple[RenderedFile, ...]:
    """Render the complete template after strict input and marker validation."""

    if type(inputs) is not TemplateInputs:
        raise TemplateError("inputs must be TemplateInputs")
    _validate(inputs)
    service = inputs.kind == "service"
    native = NativeDependencyRecipe(
        ('package-lock.json', 'package.json', 'vendor/local-web-ui.tgz'),
        (ProcessCommand('native frontend dependencies', ('npm', 'ci')),), ('node_modules',))
    production = inputs.production_recipe
    if production is None:
        runtime = ContainerBuildRecipe(
            base_image='node@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9',
            abi='node-24-debian-bookworm-amd64', lockfiles=('runtime/dependencies.json',),
            dockerfile='runtime/Dockerfile',
            command=('node', '/app/current/server/service.mjs', '--runtime', 'nas-docker',
                     '--port', '{port}', '--bind-host', '{bindHost}',
                     '--public-origin', '{publicOrigin}', '--proxy-address', '{proxyAddress}'),
            cpu_limit=1, memory_limit_mib=256, health_runtime='node') if service else None
        production = parse_production_recipe(production_declaration(inputs.kind,
            data_contract='none', runtime=runtime,
            build_commands=(ProcessCommand('native frontend', ('npm', 'run', 'build')),),
            native_dependencies=native), kind=inputs.kind)
    replacements = {
        "__PRODUCTION_MANIFEST__": (
            '  "production": ' + json.dumps(production.to_dict(), indent=2).replace('\n', '\n  ') + ',\n'
            if production is not None else ''
        ),
        "__APP_ID__": inputs.app_id,
        "__APP_TITLE__": inputs.title,
        "__APP_ROUTE__": inputs.route,
        "__APP_ICON__": inputs.icon,
        "__APP_ACCENT__": inputs.accent,
        "__UI_VERSION__": inputs.ui_version,
        "__CAPABILITIES_JSON__": json.dumps(list(inputs.capabilities), separators=(",", ":")),
        "__APP_KIND__": inputs.kind,
        "__BUILD_OUTPUT__": "release" if service else "dist",
        "__BUILD_RELEASE__": (
            ',\n    "release": [\n'
            '      {"source": "dist", "target": "public"},\n'
            '      {"source": "server", "target": "server"}\n'
            "    ]"
            if service
            else ""
        ),
        "__HEALTH_PATH__": f"{inputs.route}/healthz" if service else f"{inputs.route}/",
        "__SERVICE_MANIFEST__": (
            ',\n  "service": {\n'
            '    "module": "server/service.mjs",\n'
            '    "internalHealthPath": "/healthz",\n'
            '    "frontendOutput": "public",\n'
            '    "proxyPaths": ["/api"],\n'
            '    "startCommand": ["/usr/bin/env", "node", "server/service.mjs", "--port", "{port}"]\n'
            "  }"
            if service
            else ""
        ),
        "__SERVICE_SCRIPT__": (
            ',\n    "test:service": "node --check server/service.mjs"'
            if service
            else ""
        ),
        "__CHECK_SCRIPT__": (
            "npm run lint && npm run test && npm run test:service && npm run build"
            if service
            else "npm run lint && npm run test && npm run build"
        ),
        "__APP_ARCHITECTURE__": (
            "a React, TypeScript, and Vite client with a small language-neutral local service contract"
            if service
            else "a static React, TypeScript, and Vite client"
        ),
        "__SERVICE_DEVELOPMENT__": (
            "The reference backend lives in `server/` and defaults to Mac loopback. The checked-in NAS production recipe supplies explicit bind, HTTPS origin and trusted-proxy flags; it uses a pinned Node 24 image with built-in modules only. Native frontend dependencies are cached separately. The declarative release composer assembles the frontend and server. Extend the app-owned recipe when adding runtime dependencies, secrets or durable data; code rollback never restores data.\n"
            if service
            else ""
        ),
    }
    rendered: list[RenderedFile] = []
    sources = [(source, ".template") for source in _TEMPLATE_ROOT.rglob("*.template")]
    if service:
        sources.extend(
            (source, ".service-template")
            for source in _TEMPLATE_ROOT.rglob("*.service-template")
        )
    for source, suffix in sorted(sources, key=lambda item: item[0].as_posix()):
        content = source.read_text(encoding="utf-8")
        source_markers = frozenset(_MARKER.findall(content))
        if not source_markers.issubset(_ALLOWED_MARKERS):
            raise TemplateError("template source contains an unsupported marker")
        for marker, value in replacements.items():
            content = content.replace(marker, value)
        if _MARKER.search(content):
            raise TemplateError("template rendering left an unresolved marker")
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        if not content.endswith("\n"):
            content += "\n"
        rendered.append(RenderedFile(_destination(source, suffix), content))
    return tuple(rendered)
