"""Read-only, machine-readable structural checks for platform applications."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Protocol

from .app_provenance import (
    PROVENANCE_FILENAME,
    AppProvenance,
    ProvenanceError,
    parse_provenance,
    validate_compatibility,
)
from .config import ConfigError, SUPPORTED_PLATFORM_CONTRACT, parse_manifest
from .icons import canonical_manifest_icon


_MANIFEST = Path("local-web.json")
_PROVENANCE = Path(PROVENANCE_FILENAME)
_PACKAGE = Path("package.json")
_INDEX = Path("index.html")
_ARTIFACT = Path("vendor/local-web-ui.tgz")
_THEME_ROUTE = "/_local-web/platform/theme.css"
_REQUIRED_SCRIPTS = frozenset({"dev", "build", "lint", "test", "test:e2e", "check"})
_NPM_RUN = re.compile(
    r"(?<![A-Za-z0-9_-])npm"
    r"(?:\s+-{1,2}[A-Za-z0-9][A-Za-z0-9-]*(?:=[^\s;&|()]+)?)*"
    r"\s+run(?:-script)?"
    r"(?:\s+-{1,2}[A-Za-z0-9][A-Za-z0-9-]*(?:=[^\s;&|()]+)?)*"
    r"\s+([A-Za-z0-9][A-Za-z0-9:_-]*)"
)
_NPM_TEST = re.compile(
    r"(?<![A-Za-z0-9_-])npm"
    r"(?:\s+-{1,2}[A-Za-z0-9][A-Za-z0-9-]*(?:=[^\s;&|()]+)?)*"
    r"\s+test(?:\s|$|[;&|()])"
)
_LOCAL_WEB_COMMAND = re.compile(r"(?<![A-Za-z0-9_-])local-web(?![A-Za-z0-9_-])")
_SAFE_GIT_ENV = {
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
}


class _InspectionError(ValueError):
    """A structural artifact could not be inspected safely."""


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str
    message: str
    remedy: str


@dataclass(frozen=True)
class DoctorReport:
    app: str
    compatible: bool
    diagnostics: tuple[Diagnostic, ...]

    def as_json(self) -> str:
        payload = {
            "app": self.app,
            "compatible": self.compatible,
            "diagnostics": [asdict(item) for item in self.diagnostics],
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


class AppInspector(Protocol):
    """The narrow structural-inspection seam used by app workflows."""

    def inspect(self, repository: Path) -> DoctorReport: ...


class CandidateAppInspector(Protocol):
    """Structural inspection for a planned, not-yet-written app candidate."""

    def inspect_candidate(
        self,
        repository: Path,
        replacements: Mapping[Path, bytes],
        added_tracked: frozenset[Path],
    ) -> DoctorReport: ...


class _ThemeLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.routes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "link":
            return
        values = {name.lower(): value for name, value in attrs}
        rel = (values.get("rel") or "").lower().split()
        href = values.get("href")
        if "stylesheet" in rel and href is not None:
            self.routes.append(href)


def _diagnostic(code: str, app: str) -> Diagnostic:
    values = {
        "repository.invalid": (
            "error",
            "Repository is not a readable Git worktree.",
            "Run the command from a Git application repository.",
        ),
        "manifest.invalid": (
            "error",
            "Application manifest is missing or invalid.",
            "Repair the tracked local-web.json manifest.",
        ),
        "platform.legacy": (
            "warning",
            "Application uses the legacy hosting contract.",
            "Run local-web app update --repository .",
        ),
        "provenance.missing": (
            "error",
            "Platform provenance is missing.",
            "Restore the tracked .local-web-platform.json file.",
        ),
        "provenance.invalid": (
            "error",
            "Platform provenance is invalid.",
            "Regenerate or safely update platform provenance.",
        ),
        "platform.contract-version-mismatch": (
            "error",
            "Platform contract versions disagree.",
            "Restore manifest and provenance to the supported platform contract.",
        ),
        "platform.template-version-mismatch": (
            "error",
            "Template versions disagree.",
            "Run local-web app update --repository .",
        ),
        "ui.version-mismatch": (
            "error",
            "UI package versions disagree.",
            "Run local-web app update --repository .",
        ),
        "platform.capabilities-mismatch": (
            "error",
            "Capability declarations disagree.",
            "Run local-web app update --repository .",
        ),
        "ui.artifact-missing": (
            "error",
            "Vendored UI package is missing.",
            "Run local-web app update --repository .",
        ),
        "ui.digest-mismatch": (
            "error",
            "Vendored UI package does not match provenance.",
            "Run local-web app update --repository .",
        ),
        "git.managed-files-dirty": (
            "error",
            "Platform-managed files have uncommitted changes.",
            "Commit or restore the managed files before checking the app.",
        ),
        "git.managed-file-untracked": (
            "error",
            "A platform-managed file is not tracked by Git.",
            "Restore and commit every provenance-managed file.",
        ),
        "package.invalid": (
            "error",
            "Package metadata is missing or invalid.",
            "Restore the tracked package.json file.",
        ),
        "package.script-missing": (
            "error",
            "A required npm script is missing.",
            "Restore the standard platform npm scripts.",
        ),
        "package.script-recursive": (
            "error",
            "The app-local verification script graph invokes npm run check recursively.",
            "Keep lint, test, build, and test:e2e independent from npm run check.",
        ),
        "package.script-platform-coupled": (
            "error",
            "The app-local verification script graph invokes local-web.",
            "Keep verification app-local and use local-web app check for orchestration.",
        ),
        "ui.dependency-invalid": (
            "error",
            "The UI dependency is not the vendored artifact.",
            "Pin @local-web/ui to file:vendor/local-web-ui.tgz.",
        ),
        "theme.link-invalid": (
            "error",
            "The reserved platform theme link is missing or invalid.",
            f"Link the stylesheet at {_THEME_ROUTE}.",
        ),
        "icon.unsupported": (
            "error",
            "Application manifest uses an unsupported platform icon.",
            "Use an icon from the platform manifest allowlist.",
        ),
    }
    severity, message, remedy = values[code]
    return Diagnostic(code, severity, message, remedy)


def _repository_name(repository: Path) -> str:
    name = repository.name
    return name if re.fullmatch(r"[a-z][a-z0-9-]*", name) else "unknown"


def _git(repository: Path, argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            (
                "git",
                "--literal-pathspecs",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.attributesFile=/dev/null",
                *argv,
            ),
            cwd=repository,
            env=_SAFE_GIT_ENV,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise _InspectionError("repository inspection failed") from error


def _tracked_files(repository: Path) -> tuple[Path, ...]:
    result = _git(repository, ("ls-files", "-z", "--cached"))
    if result.returncode != 0:
        raise _InspectionError("repository inspection failed")
    paths: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            value = raw.decode("utf-8")
        except UnicodeError as error:
            raise _InspectionError("repository inspection failed") from error
        pure = PurePosixPath(value)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != value:
            raise _InspectionError("repository inspection failed")
        paths.append(Path(*pure.parts))
    return tuple(sorted(paths, key=lambda item: item.as_posix()))


def _canonical_relative_path(path: Path) -> Path:
    if not isinstance(path, Path):
        raise _InspectionError("repository inspection failed")
    value = path.as_posix()
    pure = PurePosixPath(value)
    if (
        not value
        or value == "."
        or pure.is_absolute()
        or ".." in pure.parts
        or pure.as_posix() != value
    ):
        raise _InspectionError("repository inspection failed")
    return Path(*pure.parts)


def _read_known(
    repository: Path,
    relative: Path,
    maximum: int = 2 * 1024 * 1024,
    replacements: Mapping[Path, bytes] | None = None,
) -> bytes:
    if replacements is not None and relative in replacements:
        content = replacements[relative]
        if len(content) > maximum:
            raise _InspectionError("structural file inspection failed")
        return content
    path = repository / relative
    try:
        current = repository
        for component in relative.parts:
            current = current / component
            if current.is_symlink():
                raise OSError
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise OSError
        content = path.read_bytes()
        if len(content) > maximum:
            raise OSError
        return content
    except OSError as error:
        raise _InspectionError("structural file inspection failed") from error


def _text(
    repository: Path,
    relative: Path,
    replacements: Mapping[Path, bytes] | None = None,
) -> str:
    try:
        return _read_known(repository, relative, replacements=replacements).decode("utf-8")
    except (UnicodeError, _InspectionError) as error:
        raise _InspectionError("structural file inspection failed") from error


def _managed_dirty(repository: Path, managed: tuple[Path, ...]) -> bool:
    arguments = tuple(path.as_posix() for path in managed)
    prefixes = [("diff", "--quiet", "--")]
    head = _git(repository, ("rev-parse", "--verify", "HEAD"))
    if head.returncode == 0:
        prefixes.append(("diff", "--cached", "--quiet", "--"))
    elif head.returncode != 128:
        raise _InspectionError("repository inspection failed")
    for prefix in prefixes:
        result = _git(repository, prefix + arguments)
        if result.returncode == 1:
            return True
        if result.returncode != 0:
            raise _InspectionError("repository inspection failed")
    return False


class AppDoctor:
    def inspect(self, repository: Path) -> DoctorReport:
        requested = Path(repository)
        app = _repository_name(requested)
        try:
            resolved = requested.resolve(strict=True)
            if not resolved.is_dir():
                raise OSError
            top = _git(resolved, ("rev-parse", "--show-toplevel"))
            if top.returncode != 0 or Path(top.stdout.decode("utf-8").strip()) != resolved:
                raise _InspectionError("repository inspection failed")
            tracked = _tracked_files(resolved)
        except (OSError, UnicodeError, _InspectionError):
            return DoctorReport(app, False, (_diagnostic("repository.invalid", app),))

        return self._inspect_structural(resolved, set(tracked), app)

    def inspect_candidate(
        self,
        repository: Path,
        replacements: Mapping[Path, bytes],
        added_tracked: frozenset[Path],
    ) -> DoctorReport:
        requested = Path(repository)
        app = _repository_name(requested)
        try:
            resolved = requested.resolve(strict=True)
            if not resolved.is_dir():
                raise OSError
            top = _git(resolved, ("rev-parse", "--show-toplevel"))
            if top.returncode != 0 or Path(top.stdout.decode("utf-8").strip()) != resolved:
                raise _InspectionError("repository inspection failed")
            tracked = set(_tracked_files(resolved))
            candidate_added = frozenset(
                _canonical_relative_path(path) for path in added_tracked
            )
            candidate_replacements = {
                _canonical_relative_path(path): content
                for path, content in replacements.items()
            }
            if any(not isinstance(content, bytes) for content in candidate_replacements.values()):
                raise _InspectionError("repository inspection failed")
            if any(path not in tracked | candidate_added for path in candidate_replacements):
                raise _InspectionError("repository inspection failed")
        except (OSError, TypeError, UnicodeError, _InspectionError):
            return DoctorReport(app, False, (_diagnostic("repository.invalid", app),))

        return self._inspect_structural(
            resolved,
            tracked | candidate_added,
            app,
            candidate_replacements,
        )

    def _inspect_structural(
        self,
        repository: Path,
        tracked: set[Path],
        app: str,
        replacements: Mapping[Path, bytes] | None = None,
    ) -> DoctorReport:
        diagnostics: list[Diagnostic] = []
        if _MANIFEST not in tracked:
            return DoctorReport(app, False, (_diagnostic("manifest.invalid", app),))

        manifest_content = ""
        try:
            manifest_content = _text(repository, _MANIFEST, replacements)
            manifest = parse_manifest(manifest_content, source="manifest")
            app = manifest.id
        except (ConfigError, _InspectionError):
            try:
                payload = json.loads(manifest_content)
                if isinstance(payload, dict):
                    candidate = payload.get("id")
                    if isinstance(candidate, str) and re.fullmatch(
                        r"[a-z][a-z0-9-]*", candidate
                    ):
                        app = candidate
                    platform = payload.get("platform")
                    if (
                        isinstance(platform, dict)
                        and type(platform.get("contractVersion")) is int
                        and platform["contractVersion"] != SUPPORTED_PLATFORM_CONTRACT
                    ):
                        return DoctorReport(
                            app,
                            False,
                            (_diagnostic("platform.contract-version-mismatch", app),),
                        )
                    home = payload.get("home")
                    if isinstance(home, dict) and isinstance(home.get("icon"), str):
                        try:
                            canonical_manifest_icon(home["icon"])
                        except ValueError:
                            return DoctorReport(
                                app, False, (_diagnostic("icon.unsupported", app),)
                            )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            return DoctorReport(app, False, (_diagnostic("manifest.invalid", app),))

        if manifest.platform is None:
            diagnostics.append(
                _diagnostic(
                    "provenance.invalid" if _PROVENANCE in tracked else "platform.legacy",
                    app,
                )
            )
            return self._report(app, diagnostics)

        provenance: AppProvenance | None = None
        if _PROVENANCE not in tracked:
            diagnostics.append(_diagnostic("provenance.missing", app))
        else:
            try:
                provenance = parse_provenance(_text(repository, _PROVENANCE, replacements))
            except (ProvenanceError, _InspectionError):
                diagnostics.append(_diagnostic("provenance.invalid", app))
        if provenance is None:
            return self._report(app, diagnostics)

        for code in validate_compatibility(provenance, manifest.platform):
            diagnostics.append(_diagnostic(code, app))

        if any(path not in tracked for path in provenance.managed_files):
            diagnostics.append(_diagnostic("git.managed-file-untracked", app))
        try:
            managed_to_check = tuple(
                path
                for path in provenance.managed_files
                if replacements is None or path not in replacements
            )
            if managed_to_check and _managed_dirty(repository, managed_to_check):
                diagnostics.append(_diagnostic("git.managed-files-dirty", app))
        except _InspectionError:
            diagnostics.append(_diagnostic("repository.invalid", app))

        if _ARTIFACT not in tracked:
            diagnostics.append(_diagnostic("ui.artifact-missing", app))
        else:
            try:
                digest = hashlib.sha256(
                    _read_known(repository, _ARTIFACT, 8 * 1024 * 1024, replacements)
                ).hexdigest()
                if digest != provenance.ui.sha256:
                    diagnostics.append(_diagnostic("ui.digest-mismatch", app))
            except _InspectionError:
                diagnostics.append(_diagnostic("ui.artifact-missing", app))

        package_content = None
        if _PACKAGE in tracked:
            try:
                package_content = _text(repository, _PACKAGE, replacements)
            except _InspectionError:
                pass
        self._inspect_package(package_content, diagnostics, app)

        html = ""
        if _INDEX in tracked:
            try:
                html = _text(repository, _INDEX, replacements)
            except _InspectionError:
                pass
        links = _ThemeLinkParser()
        links.feed(html)
        if links.routes.count(_THEME_ROUTE) != 1:
            diagnostics.append(_diagnostic("theme.link-invalid", app))

        return self._report(app, diagnostics)

    @staticmethod
    def _report(app: str, diagnostics: list[Diagnostic]) -> DoctorReport:
        unique = {item.code: item for item in diagnostics}
        ordered = tuple(unique[code] for code in sorted(unique))
        return DoctorReport(
            app, not any(item.severity == "error" for item in ordered), ordered
        )

    @staticmethod
    def _inspect_package(
        content: str | None, diagnostics: list[Diagnostic], app: str
    ) -> None:
        try:
            package = json.loads(content or "")
            if not isinstance(package, dict) or not isinstance(
                package.get("scripts"), dict
            ):
                raise ValueError
            scripts = package["scripts"]
            if any(
                name not in scripts
                or not isinstance(scripts[name], str)
                or not scripts[name].strip()
                for name in _REQUIRED_SCRIPTS
            ):
                diagnostics.append(_diagnostic("package.script-missing", app))

            reachable = AppDoctor._reachable_scripts(scripts, ("check", "test:e2e"))
            if any(
                isinstance(scripts.get(name), str)
                and "check" in _NPM_RUN.findall(scripts[name])
                for name in reachable
            ):
                diagnostics.append(_diagnostic("package.script-recursive", app))
            if any(
                isinstance(scripts.get(name), str)
                and _LOCAL_WEB_COMMAND.search(scripts[name])
                for name in reachable
            ):
                diagnostics.append(_diagnostic("package.script-platform-coupled", app))
            dependencies = package.get("dependencies")
            if (
                not isinstance(dependencies, dict)
                or dependencies.get("@local-web/ui") != "file:vendor/local-web-ui.tgz"
            ):
                diagnostics.append(_diagnostic("ui.dependency-invalid", app))
        except (TypeError, ValueError, json.JSONDecodeError):
            diagnostics.append(_diagnostic("package.invalid", app))

    @staticmethod
    def _reachable_scripts(
        scripts: dict[str, object], roots: tuple[str, ...]
    ) -> set[str]:
        reachable: set[str] = set()
        pending: list[str] = []

        def enqueue_invocation(name: str) -> None:
            pending.extend((f"pre{name}", name, f"post{name}"))

        for root in roots:
            enqueue_invocation(root)

        while pending:
            name = pending.pop()
            if name in reachable:
                continue
            reachable.add(name)
            command = scripts.get(name)
            if not isinstance(command, str):
                continue
            for target in _NPM_RUN.findall(command):
                enqueue_invocation(target)
            if _NPM_TEST.search(command):
                enqueue_invocation("test")

        return reachable
