"""Strict renderer for the canonical generated React application."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .icons import canonical_manifest_icon


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
    replacements = {
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
            "The reference backend lives in `server/`, binds only to loopback, and is assembled with the frontend by the platform's declarative release contract.\n"
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
