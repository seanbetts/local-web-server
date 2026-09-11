"""Renderer partition for the shared React and Vite foundation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .app_template import RenderedFile, TemplateError, TemplateInputs, render_template


_FRONTEND_PATHS = frozenset({
    Path("eslint.config.js"), Path("index.html"), Path("package.json"),
    Path("playwright.config.ts"), Path("tsconfig.app.json"),
    Path("tsconfig.json"), Path("tsconfig.node.json"), Path("vite.config.ts"),
    Path("src/App.tsx"), Path("src/App.test.tsx"), Path("src/app.css"),
    Path("src/contextExport.ts"), Path("src/contextExport.test.ts"),
    Path("src/main.tsx"), Path("src/platform.ts"),
    Path("src/platform.test.ts"), Path("src/test/setup.ts"),
    Path("tests/app.spec.ts"),
})


@dataclass(frozen=True)
class RenderedReactFoundation:
    files: tuple[RenderedFile, ...]
    agent_guidance: RenderedFile


def _by_path(inputs: TemplateInputs) -> dict[Path, RenderedFile]:
    return {item.path: item for item in render_template(inputs)}


def render_react_vite_foundation(
    inputs: TemplateInputs, *, include_reference_service: bool
) -> RenderedReactFoundation:
    frontend = _by_path(inputs)
    if not include_reference_service:
        frontend[Path("package.json")] = _by_path(
            replace(inputs, kind="static")
        )[Path("package.json")]
    identity = _by_path(inputs)
    if set(frontend).issuperset(_FRONTEND_PATHS) is False:
        raise TemplateError("foundation template is incomplete")
    return RenderedReactFoundation(
        files=tuple(frontend[path] for path in sorted(_FRONTEND_PATHS)),
        agent_guidance=identity[Path("AGENTS.md")],
    )


def render_generated_repository(inputs: TemplateInputs) -> tuple[RenderedFile, ...]:
    foundation = render_react_vite_foundation(
        inputs, include_reference_service=inputs.kind == "service"
    )
    complete = _by_path(inputs)
    selected = {item.path: item for item in foundation.files}
    selected[foundation.agent_guidance.path] = foundation.agent_guidance
    for path, item in complete.items():
        if path not in _FRONTEND_PATHS and path != Path("AGENTS.md"):
            selected[path] = item
    return tuple(selected[path] for path in sorted(selected))
