"""Immutable planning for adopting and refreshing existing platform apps."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .app_doctor import AppDoctor, CandidateAppInspector
from .app_foundation import render_react_vite_foundation
from .app_provenance import (
    CURRENT_TEMPLATE_VERSION,
    PROVENANCE_FILENAME,
    AppProvenance,
    ProvenanceError,
    UiArtifactReference,
    parse_provenance,
    render_provenance,
    validate_compatibility,
)
from .app_update_models import (
    AppUpdatePlan,
    AppUpdateRequest,
    AppUpdateResult,
    FileChange,
)
from .app_update_transaction import AppUpdateTransactionError, UpdatePublisher
from .app_template import TemplateError, TemplateInputs
from .config import ConfigError, SUPPORTED_PLATFORM_CONTRACT, parse_manifest
from .models import AppManifest
from .process_runner import (
    ProcessCommand,
    ProcessRunError,
    ProcessRunner,
    ProcessWorkflowRunner,
)
from .ui_package import UiPackageArtifact, build_ui_package


_PROVENANCE = Path(PROVENANCE_FILENAME)
_MANIFEST = Path("local-web.json")
_PACKAGE = Path("package.json")
_LOCKFILE = Path("package-lock.json")
_INDEX = Path("index.html")
_ARTIFACT = Path("vendor/local-web-ui.tgz")
_AGENT_GUIDANCE = Path("AGENTS.md")
_TARGETS = (_PROVENANCE, _INDEX, _MANIFEST, _LOCKFILE, _PACKAGE, _ARTIFACT)
_DEPENDENCY = "file:vendor/local-web-ui.tgz"
_CHECK = "npm run lint && npm run test && npm run build"
_THEME_ROUTE = "/_local-web/platform/theme.css"
_THEME_LINK = '<link rel="stylesheet" href="/_local-web/platform/theme.css">'
_THEME_LINKS = (_THEME_LINK, _THEME_LINK[:-1] + " />")
_LEGACY_COLOUR_MODE_BOOTSTRAP = (
    b"    <script>\n"
    b"      (() => {\n"
    b"        try {\n"
    b"          const mode = localStorage.getItem('local-web:colour-mode');\n"
    b"          if (mode === 'light' || mode === 'dark') {\n"
    b"            document.documentElement.dataset.lwpColourMode = mode;\n"
    b"          } else {\n"
    b"            delete document.documentElement.dataset.lwpColourMode;\n"
    b"          }\n"
    b"        } catch {\n"
    b"          delete document.documentElement.dataset.lwpColourMode;\n"
    b"        }\n"
    b"      })();\n"
    b"    </script>\n"
)
_HEAD_OPEN = re.compile(r"<head(?:\s[^>]*)?>", re.IGNORECASE)
_HEAD_CLOSE = re.compile(r"</head\s*>", re.IGNORECASE)
_SUPPORTED_CAPABILITIES = frozenset({"supabase"})
_REFRESHABLE_COMPATIBILITY = frozenset(
    {
        "platform.template-version-mismatch",
        "ui.version-mismatch",
        "platform.capabilities-mismatch",
    }
)
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_MAX_ARTIFACT_PACKAGE_BYTES = 64 * 1024
_UI_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
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
_FOUNDATION_COMMANDS = (
    ProcessCommand("npm-install", ("npm", "ci", "--ignore-scripts")),
    ProcessCommand("npm-check", ("npm", "run", "check")),
)


class AppUpdateError(ValueError):
    """An existing application cannot be planned for a safe platform update."""


class AppUpdatePlanChangedError(AppUpdateError):
    """The final update plan no longer matches its caller-approved plan."""


class _GitInspectionError(ValueError):
    """The target repository cannot be inspected through the fixed Git policy."""


class LockfileBuilder(Protocol):
    def build(
        self,
        package: bytes,
        current_lockfile: bytes | None,
        artifact: bytes,
    ) -> bytes: ...


ArtifactBuilder = Callable[[Path, Path], UiPackageArtifact]


class UpdatePublisherProtocol(Protocol):
    def recovery_pending(self, repository: Path) -> bool: ...

    def recover(self, repository: Path) -> bool: ...

    def publish(
        self,
        repository: Path,
        changes: tuple[FileChange, ...],
        validate: Callable[[], None],
    ) -> None: ...


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _decode_json(content: bytes) -> dict[str, Any]:
    if type(content) is not bytes or len(content) > _MAX_JSON_BYTES:
        raise ValueError("invalid json")
    value = json.loads(
        content.decode("utf-8"), object_pairs_hook=_duplicate_rejecting_object
    )
    if not isinstance(value, dict):
        raise ValueError("invalid json")
    return value


def _render_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def _read_required(repository: Path, relative: Path) -> bytes:
    path = repository / relative
    try:
        if path.is_symlink() or not path.is_file():
            raise OSError
        content = path.read_bytes()
    except OSError as error:
        raise AppUpdateError("application update planning failed") from error
    if len(content) > _MAX_JSON_BYTES:
        raise AppUpdateError("application update planning failed")
    return content


def _read_optional(repository: Path, relative: Path, maximum: int) -> bytes | None:
    path = repository / relative
    try:
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise OSError
        content = path.read_bytes()
    except OSError as error:
        raise AppUpdateError("application update planning failed") from error
    if len(content) > maximum:
        raise AppUpdateError("application update planning failed")
    return content


def _git(
    repository: Path, arguments: tuple[str, ...]
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            (
                "git",
                "--literal-pathspecs",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.attributesFile=/dev/null",
                *arguments,
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
        raise _GitInspectionError("repository inspection failed") from error


def _require_repository_root_with_head(repository: Path) -> None:
    top = _git(repository, ("rev-parse", "--show-toplevel"))
    head = _git(repository, ("rev-parse", "--verify", "HEAD^{commit}"))
    try:
        top_level = Path(top.stdout.decode("utf-8").strip())
    except UnicodeError as error:
        raise _GitInspectionError("repository inspection failed") from error
    if top.returncode != 0 or top_level != repository or head.returncode != 0:
        raise _GitInspectionError("repository inspection failed")


def _paths_are_dirty(repository: Path, paths: tuple[Path, ...]) -> bool:
    if not paths:
        return False
    result = _git(
        repository,
        (
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--",
            *(path.as_posix() for path in paths),
        ),
    )
    if result.returncode != 0:
        raise _GitInspectionError("repository inspection failed")
    if result.stdout:
        return True
    return any(
        _path_exists(repository, path) and not _is_tracked(repository, path)
        for path in paths
    )


def _path_exists(repository: Path, path: Path) -> bool:
    try:
        candidate = repository / path
        return candidate.exists() or candidate.is_symlink()
    except OSError as error:
        raise _GitInspectionError("repository inspection failed") from error


def _is_tracked(repository: Path, path: Path) -> bool:
    result = _git(repository, ("ls-files", "--error-unmatch", "--", path.as_posix()))
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise _GitInspectionError("repository inspection failed")


def _validate_request(request: AppUpdateRequest) -> None:
    if (
        type(request) is not AppUpdateRequest
        or not isinstance(request.repository, Path)
        or type(request.capabilities) is not tuple
        or any(type(item) is not str for item in request.capabilities)
        or request.capabilities != tuple(sorted(request.capabilities))
        or len(request.capabilities) != len(set(request.capabilities))
        or any(item not in _SUPPORTED_CAPABILITIES for item in request.capabilities)
        or request.foundation not in (None, "react-vite")
    ):
        raise AppUpdateError("application update request is invalid")


def _parse_update_manifest(content: bytes) -> AppManifest:
    try:
        _decode_json(content)
        return parse_manifest(content.decode("utf-8"), source="manifest")
    except (UnicodeError, ValueError, ConfigError) as error:
        raise AppUpdateError("application update planning failed") from error


def _target_has_unsafe_parent(repository: Path, relative: Path) -> bool:
    parent = repository
    try:
        for part in relative.parts[:-1]:
            parent /= part
            if not parent.exists() and not parent.is_symlink():
                return False
            if parent.is_symlink() or not parent.is_dir():
                return True
        return False
    except OSError as error:
        raise _GitInspectionError("repository inspection failed") from error


def _require_absent_foundation_targets(
    repository: Path, targets: tuple[Path, ...]
) -> None:
    try:
        for target in targets:
            exists = _path_exists(repository, target)
            tracked = _is_tracked(repository, target)
            if tracked and _paths_are_dirty(repository, (target,)):
                raise AppUpdateError("target files are not clean")
            if exists or tracked or _target_has_unsafe_parent(repository, target):
                raise AppUpdateError("application contract conflicts with update")
        if _paths_are_dirty(repository, targets):
            raise AppUpdateError("target files are not clean")
    except AppUpdateError:
        raise
    except _GitInspectionError as error:
        raise AppUpdateError("application update planning failed") from error


def _verify_temporary_foundation(
    workspace: Path,
    files: tuple[FileChange, ...],
    workspace_paths: frozenset[Path],
    runner: ProcessWorkflowRunner,
) -> None:
    for change in files:
        if type(change.after) is not bytes:
            raise AppUpdateError("application update planning failed")
        if change.path in workspace_paths:
            destination = workspace / change.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(change.after)
    runner.run(workspace, _FOUNDATION_COMMANDS)


def _manifest_candidate(
    content: bytes, ui_version: str, capabilities: tuple[str, ...]
) -> tuple[bytes, str, str | None, bool]:
    try:
        payload = _decode_json(content)
        manifest = parse_manifest(content.decode("utf-8"), source="manifest")
    except (UnicodeError, ValueError, ConfigError) as error:
        raise AppUpdateError("application update planning failed") from error

    existing = manifest.platform is not None
    current_ui_version = manifest.platform.ui_version if manifest.platform else None
    payload["platform"] = {
        "contractVersion": SUPPORTED_PLATFORM_CONTRACT,
        "templateVersion": CURRENT_TEMPLATE_VERSION,
        "uiVersion": ui_version,
        "capabilities": list(capabilities),
    }
    try:
        candidate = _render_json(payload)
        parse_manifest(candidate.decode("utf-8"), source="manifest")
    except (UnicodeError, ConfigError, TypeError, ValueError) as error:
        raise AppUpdateError("application update planning failed") from error
    return candidate, manifest.id, current_ui_version, existing


def _package_candidate(content: bytes) -> bytes:
    try:
        package = _decode_json(content)
        scripts = package.get("scripts")
        if not isinstance(scripts, dict):
            raise ValueError
        for bucket in (
            "devDependencies",
            "optionalDependencies",
            "peerDependencies",
        ):
            declarations = package.get(bucket)
            if isinstance(declarations, dict) and "@local-web/ui" in declarations:
                raise AppUpdateError("application contract conflicts with update")
        dependencies = package.get("dependencies")
        if dependencies is None:
            dependencies = {}
            package["dependencies"] = dependencies
        if not isinstance(dependencies, dict):
            raise ValueError
        current_dependency = dependencies.get("@local-web/ui")
        if current_dependency not in (None, _DEPENDENCY):
            raise AppUpdateError("application contract conflicts with update")
        dependencies["@local-web/ui"] = _DEPENDENCY
        if "check" not in scripts:
            if any(
                type(scripts.get(name)) is not str or not scripts[name].strip()
                for name in ("lint", "test", "build")
            ):
                raise AppUpdateError("application contract conflicts with update")
            scripts["check"] = _CHECK
        elif type(scripts["check"]) is not str or not scripts["check"].strip():
            raise AppUpdateError("application contract conflicts with update")
        return _render_json(package)
    except AppUpdateError:
        raise
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise AppUpdateError("application update planning failed") from error


def _index_candidate(content: bytes) -> bytes:
    legacy_bootstrap_count = content.count(_LEGACY_COLOUR_MODE_BOOTSTRAP)
    if legacy_bootstrap_count > 1:
        raise AppUpdateError("application contract conflicts with update")
    if legacy_bootstrap_count:
        content = content.replace(_LEGACY_COLOUR_MODE_BOOTSTRAP, b"")
    try:
        html = content.decode("utf-8")
    except UnicodeError as error:
        raise AppUpdateError("application update planning failed") from error
    openings = tuple(_HEAD_OPEN.finditer(html))
    closings = tuple(_HEAD_CLOSE.finditer(html))
    if (
        len(openings) != 1
        or len(closings) != 1
        or openings[0].end() > closings[0].start()
    ):
        raise AppUpdateError("application contract conflicts with update")
    head = html[openings[0].end() : closings[0].start()]
    route_count = html.count(_THEME_ROUTE)
    exact_count = sum(html.count(link) for link in _THEME_LINKS)
    if route_count:
        if (
            exact_count != 1
            or route_count != 1
            or not any(link in head for link in _THEME_LINKS)
        ):
            raise AppUpdateError("application contract conflicts with update")
        return content
    insertion = _THEME_LINK + "\n"
    candidate = html[: closings[0].start()] + insertion + html[closings[0].start() :]
    return candidate.encode("utf-8")


def _artifact_lock_metadata(artifact: bytes) -> tuple[str, str]:
    try:
        with tarfile.open(fileobj=io.BytesIO(artifact), mode="r:gz") as archive:
            matches = [
                member
                for member in archive.getmembers()
                if member.name == "package/package.json"
            ]
            if (
                len(matches) != 1
                or not matches[0].isfile()
                or matches[0].size > _MAX_ARTIFACT_PACKAGE_BYTES
            ):
                raise ValueError
            extracted = archive.extractfile(matches[0])
            if extracted is None:
                raise ValueError
            package = _decode_json(extracted.read(_MAX_ARTIFACT_PACKAGE_BYTES + 1))
            version = package.get("version")
            if package.get("name") != "@local-web/ui" or not isinstance(
                version, str
            ) or not _UI_VERSION.fullmatch(version):
                raise ValueError
    except (OSError, tarfile.TarError, UnicodeError, ValueError, TypeError) as error:
        raise AppUpdateError("application lockfile generation failed") from error
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(artifact).digest()).decode(
        "ascii"
    )
    return version, integrity


def _invalidate_ui_resolution(lockfile: bytes) -> bytes:
    try:
        payload = _decode_json(lockfile)
        packages = payload.get("packages")
        if isinstance(packages, dict):
            packages.pop("node_modules/@local-web/ui", None)
        dependencies = payload.get("dependencies")
        if isinstance(dependencies, dict):
            dependencies.pop("@local-web/ui", None)
        return _render_json(payload)
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise AppUpdateError("application lockfile generation failed") from error


class NpmLockfileBuilder:
    """Generate a package lock without exposing the target app to npm execution."""

    def __init__(self, process_runner: ProcessWorkflowRunner | None = None) -> None:
        self._process_runner = process_runner or ProcessRunner()

    def build(
        self,
        package: bytes,
        current_lockfile: bytes | None,
        artifact: bytes,
    ) -> bytes:
        if (
            type(package) is not bytes
            or (current_lockfile is not None and type(current_lockfile) is not bytes)
            or type(artifact) is not bytes
        ):
            raise AppUpdateError("application lockfile generation failed")
        try:
            expected_version, expected_integrity = _artifact_lock_metadata(artifact)
            with tempfile.TemporaryDirectory(prefix="local-web-app-update-lock-") as text:
                workspace = Path(text)
                vendor = workspace / "vendor"
                vendor.mkdir()
                (workspace / "package.json").write_bytes(package)
                if current_lockfile is not None:
                    (workspace / "package-lock.json").write_bytes(
                        _invalidate_ui_resolution(current_lockfile)
                    )
                (vendor / "local-web-ui.tgz").write_bytes(artifact)
                self._process_runner.run(
                    workspace,
                    (
                        ProcessCommand(
                            "npm-lock",
                            ("npm", "install", "--package-lock-only", "--ignore-scripts"),
                        ),
                    ),
                )
                lockfile = workspace / "package-lock.json"
                if lockfile.is_symlink() or not lockfile.is_file():
                    raise OSError
                content = lockfile.read_bytes()
                payload = _decode_json(content)
                packages = payload.get("packages")
                root = packages.get("") if isinstance(packages, dict) else None
                dependencies = root.get("dependencies") if isinstance(root, dict) else None
                ui_package = (
                    packages.get("node_modules/@local-web/ui")
                    if isinstance(packages, dict)
                    else None
                )
                if (
                    not isinstance(dependencies, dict)
                    or dependencies.get("@local-web/ui") != _DEPENDENCY
                    or not isinstance(ui_package, dict)
                    or ui_package.get("version") != expected_version
                    or ui_package.get("resolved") != _DEPENDENCY
                    or ui_package.get("integrity") != expected_integrity
                ):
                    raise ValueError
                return content
        except AppUpdateError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError, ProcessRunError) as error:
            raise AppUpdateError("application lockfile generation failed") from error


class AppUpdater:
    def __init__(
        self,
        platform_repository: Path = Path(__file__).resolve().parents[1],
        *,
        artifact_builder: ArtifactBuilder = build_ui_package,
        lockfile_builder: LockfileBuilder | None = None,
        inspector: CandidateAppInspector | None = None,
        publisher: UpdatePublisherProtocol | None = None,
        process_runner: ProcessWorkflowRunner | None = None,
    ) -> None:
        self._platform_repository = platform_repository
        self._artifact_builder = artifact_builder
        self._lockfile_builder = lockfile_builder or NpmLockfileBuilder()
        self._inspector = inspector or AppDoctor()
        self._publisher = publisher if publisher is not None else UpdatePublisher()
        self._process_runner = process_runner or ProcessRunner()

    def preview(self, request: AppUpdateRequest) -> AppUpdatePlan:
        _validate_request(request)
        if request.foundation == "react-vite":
            repository = self._resolve_foundation_repository(request)
            manifest_before = _read_required(repository, _MANIFEST)
            manifest = _parse_update_manifest(manifest_before)
            return self._preview_react_vite_foundation(
                repository, manifest_before, manifest, request
            )
        return self._preview_existing_foundation(request)

    def _resolve_foundation_repository(self, request: AppUpdateRequest) -> Path:
        try:
            repository = request.repository.resolve(strict=True)
            if not repository.is_dir():
                raise OSError
        except OSError as error:
            raise AppUpdateError("application update planning failed") from error
        try:
            if self._publisher.recovery_pending(repository):
                raise AppUpdateError("recovery required")
        except AppUpdateError:
            raise
        except AppUpdateTransactionError as error:
            raise AppUpdateError("application update planning failed") from error
        try:
            _require_repository_root_with_head(repository)
        except _GitInspectionError as error:
            raise AppUpdateError("application update planning failed") from error
        return repository

    def _preview_react_vite_foundation(
        self,
        repository: Path,
        manifest_before: bytes,
        manifest: AppManifest,
        request: AppUpdateRequest,
    ) -> AppUpdatePlan:
        if manifest.platform is not None:
            raise AppUpdateError("application contract conflicts with update")
        try:
            if _paths_are_dirty(repository, (_MANIFEST,)):
                raise AppUpdateError("target files are not clean")
        except AppUpdateError:
            raise
        except _GitInspectionError as error:
            raise AppUpdateError("application update planning failed") from error

        try:
            with tempfile.TemporaryDirectory(
                prefix="local-web-app-foundation-artifact-",
                dir=self._platform_repository.resolve(strict=True).parent,
            ) as text:
                output = Path(text) / "local-web-ui.tgz"
                artifact = self._artifact_builder(self._platform_repository, output)
                if type(artifact) is not UiPackageArtifact:
                    raise ValueError
                artifact_bytes = _read_optional(
                    artifact.path.parent,
                    Path(artifact.path.name),
                    _MAX_ARTIFACT_BYTES,
                )
                if artifact_bytes is None:
                    raise ValueError
                if hashlib.sha256(artifact_bytes).hexdigest() != artifact.sha256:
                    raise ValueError
                UiArtifactReference(artifact.version, artifact.sha256)
        except Exception as error:
            raise AppUpdateError("application update planning failed") from error

        try:
            rendered = render_react_vite_foundation(
                TemplateInputs(
                    app_id=manifest.id,
                    title=manifest.title,
                    route=manifest.route,
                    icon=manifest.home.icon,
                    accent=manifest.home.accent,
                    ui_version=artifact.version,
                    ui_sha256=artifact.sha256,
                    capabilities=request.capabilities,
                    kind=manifest.kind,
                ),
                include_reference_service=False,
            )
            candidates = {
                item.path: item.content.encode("utf-8") for item in rendered.files
            }
        except (TemplateError, UnicodeError, TypeError, ValueError) as error:
            raise AppUpdateError("application update planning failed") from error

        try:
            create_agent_guidance = not _path_exists(
                repository, _AGENT_GUIDANCE
            )
        except _GitInspectionError as error:
            raise AppUpdateError("application update planning failed") from error
        if create_agent_guidance:
            candidates[_AGENT_GUIDANCE] = rendered.agent_guidance.content.encode(
                "utf-8"
            )
        prospective_targets = tuple(
            sorted(
                (*candidates, _PROVENANCE, _LOCKFILE, _ARTIFACT),
                key=lambda path: path.as_posix(),
            )
        )
        _require_absent_foundation_targets(repository, prospective_targets)

        try:
            package_after = candidates[_PACKAGE]
            lockfile_after = self._lockfile_builder.build(
                package_after, None, artifact_bytes
            )
            if type(lockfile_after) is not bytes:
                raise ValueError
        except Exception as error:
            raise AppUpdateError("application update planning failed") from error

        manifest_after, app_id, current_ui_version, existing = _manifest_candidate(
            manifest_before, artifact.version, request.capabilities
        )
        if existing or current_ui_version is not None or app_id != manifest.id:
            raise AppUpdateError("application update planning failed")
        managed_files = (
            ((_AGENT_GUIDANCE,) if create_agent_guidance else ())
            + (_MANIFEST, _ARTIFACT)
        )
        try:
            provenance_after = render_provenance(
                AppProvenance(
                    schema_version=1,
                    template_version=CURRENT_TEMPLATE_VERSION,
                    platform_contract_version=SUPPORTED_PLATFORM_CONTRACT,
                    ui=UiArtifactReference(artifact.version, artifact.sha256),
                    capabilities=request.capabilities,
                    domain_palette_tokens=(),
                    managed_files=managed_files,
                )
            )
        except (ProvenanceError, TypeError, ValueError) as error:
            raise AppUpdateError("application update planning failed") from error

        candidates.update(
            {
                _PROVENANCE: provenance_after,
                _MANIFEST: manifest_after,
                _LOCKFILE: lockfile_after,
                _ARTIFACT: artifact_bytes,
            }
        )
        before = {
            path: manifest_before if path == _MANIFEST else None
            for path in candidates
        }
        changes = tuple(
            FileChange(path, before[path], candidates[path])
            for path in sorted(candidates, key=lambda item: item.as_posix())
        )
        workspace_paths = frozenset(
            (
                *(item.path for item in rendered.files),
                _MANIFEST,
                _LOCKFILE,
                _ARTIFACT,
            )
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix="local-web-app-foundation-verify-",
                dir=self._platform_repository.resolve(strict=True).parent,
            ) as text:
                _verify_temporary_foundation(
                    Path(text), changes, workspace_paths, self._process_runner
                )
        except Exception as error:
            raise AppUpdateError("application update planning failed") from error

        try:
            report = self._inspector.inspect_candidate(
                repository,
                candidates,
                frozenset(
                    change.path for change in changes if change.before is None
                ),
            )
        except Exception as error:
            raise AppUpdateError("application update planning failed") from error
        if any(item.severity == "error" for item in report.diagnostics):
            raise AppUpdateError("application update planning failed")

        return AppUpdatePlan(
            app_id=manifest.id,
            mode="adopt",
            current_ui_version=None,
            target_ui_version=artifact.version,
            target_ui_sha256=artifact.sha256,
            capabilities=request.capabilities,
            changes=changes,
            foundation="react-vite",
            current_platform_contract_version=None,
            current_template_version=None,
            target_platform_contract_version=SUPPORTED_PLATFORM_CONTRACT,
            target_template_version=CURRENT_TEMPLATE_VERSION,
        )

    def _preview_existing_foundation(
        self, request: AppUpdateRequest
    ) -> AppUpdatePlan:
        try:
            repository = request.repository.resolve(strict=True)
            if not repository.is_dir():
                raise OSError
        except OSError as error:
            raise AppUpdateError("application update planning failed") from error

        try:
            if self._publisher.recovery_pending(repository):
                raise AppUpdateError("recovery required")
        except AppUpdateError:
            raise
        except AppUpdateTransactionError as error:
            raise AppUpdateError("application update planning failed") from error

        try:
            _require_repository_root_with_head(repository)
            target_files_dirty = _paths_are_dirty(repository, _TARGETS)
        except _GitInspectionError as error:
            raise AppUpdateError("application update planning failed") from error
        if target_files_dirty:
            raise AppUpdateError("target files are not clean")

        manifest_before = _read_required(repository, _MANIFEST)
        package_before = _read_required(repository, _PACKAGE)
        index_before = _read_required(repository, _INDEX)
        lockfile_before = _read_optional(repository, _LOCKFILE, _MAX_JSON_BYTES)
        provenance_before = _read_optional(repository, _PROVENANCE, _MAX_JSON_BYTES)
        artifact_before = _read_optional(repository, _ARTIFACT, _MAX_ARTIFACT_BYTES)

        try:
            _decode_json(manifest_before)
            manifest_value = parse_manifest(
                manifest_before.decode("utf-8"), source="manifest"
            )
        except (UnicodeError, ValueError, ConfigError) as error:
            raise AppUpdateError("application update planning failed") from error

        if manifest_value.platform is None:
            if provenance_before is not None or artifact_before is not None:
                raise AppUpdateError("application contract conflicts with update")
            capabilities = request.capabilities
            current_provenance = None
        else:
            if request.capabilities:
                raise AppUpdateError("application contract conflicts with update")
            if provenance_before is None:
                raise AppUpdateError("application contract conflicts with update")
            try:
                current_provenance = parse_provenance(
                    provenance_before.decode("utf-8")
                )
            except (UnicodeError, ProvenanceError) as error:
                raise AppUpdateError("application contract conflicts with update") from error
            compatibility = validate_compatibility(
                current_provenance, manifest_value.platform
            )
            if any(
                code not in _REFRESHABLE_COMPATIBILITY for code in compatibility
            ):
                raise AppUpdateError("application contract conflicts with update")
            try:
                if not _is_tracked(repository, _PROVENANCE):
                    raise AppUpdateError("application contract conflicts with update")
                if _paths_are_dirty(repository, current_provenance.managed_files):
                    raise AppUpdateError("managed files are not clean")
            except AppUpdateError:
                raise
            except _GitInspectionError as error:
                raise AppUpdateError("application update planning failed") from error
            capabilities = manifest_value.platform.capabilities

        package_after = _package_candidate(package_before)
        index_after = _index_candidate(index_before)

        try:
            with tempfile.TemporaryDirectory(
                prefix="local-web-app-update-plan-",
                dir=self._platform_repository.resolve(strict=True).parent,
            ) as text:
                output = Path(text) / "local-web-ui.tgz"
                artifact = self._artifact_builder(self._platform_repository, output)
                if type(artifact) is not UiPackageArtifact:
                    raise ValueError
                artifact_bytes = _read_optional(
                    artifact.path.parent, Path(artifact.path.name), _MAX_ARTIFACT_BYTES
                )
                if artifact_bytes is None:
                    raise ValueError
                digest = hashlib.sha256(artifact_bytes).hexdigest()
                if digest != artifact.sha256:
                    raise ValueError
                UiArtifactReference(artifact.version, artifact.sha256)
        except Exception as error:
            raise AppUpdateError("application update planning failed") from error

        manifest_after, app_id, current_ui_version, existing = _manifest_candidate(
            manifest_before, artifact.version, capabilities
        )
        if existing != (manifest_value.platform is not None):
            raise AppUpdateError("application update planning failed")
        if current_provenance is None:
            domain_palette_tokens: tuple[str, ...] = ()
            managed_files = (_MANIFEST, _ARTIFACT)
        else:
            domain_palette_tokens = current_provenance.domain_palette_tokens
            managed_files = current_provenance.managed_files
        provenance_after = render_provenance(
            AppProvenance(
                schema_version=1,
                template_version=CURRENT_TEMPLATE_VERSION,
                platform_contract_version=SUPPORTED_PLATFORM_CONTRACT,
                ui=UiArtifactReference(artifact.version, artifact.sha256),
                capabilities=capabilities,
                domain_palette_tokens=domain_palette_tokens,
                managed_files=managed_files,
            )
        )
        try:
            lockfile_after = self._lockfile_builder.build(
                package_after, lockfile_before, artifact_bytes
            )
        except AppUpdateError:
            raise
        except Exception as error:
            raise AppUpdateError("application update planning failed") from error
        if type(lockfile_after) is not bytes:
            raise AppUpdateError("application update planning failed")

        candidates = {
            _PROVENANCE: provenance_after,
            _INDEX: index_after,
            _MANIFEST: manifest_after,
            _LOCKFILE: lockfile_after,
            _PACKAGE: package_after,
            _ARTIFACT: artifact_bytes,
        }
        before = {
            _PROVENANCE: provenance_before,
            _INDEX: index_before,
            _MANIFEST: manifest_before,
            _LOCKFILE: lockfile_before,
            _PACKAGE: package_before,
            _ARTIFACT: artifact_before,
        }
        try:
            report = self._inspector.inspect_candidate(
                repository,
                candidates,
                frozenset(path for path, content in before.items() if content is None),
            )
        except Exception as error:
            raise AppUpdateError("application update planning failed") from error
        if any(item.severity == "error" for item in report.diagnostics):
            raise AppUpdateError("application update planning failed")

        changes = tuple(
            FileChange(path, before[path], candidates[path])
            for path in sorted(_TARGETS, key=lambda item: item.as_posix())
            if before[path] != candidates[path]
        )
        mode = (
            "adopt"
            if manifest_value.platform is None
            else ("refresh" if changes else "current")
        )
        return AppUpdatePlan(
            app_id=app_id,
            mode=mode,
            current_ui_version=current_ui_version,
            target_ui_version=artifact.version,
            target_ui_sha256=artifact.sha256,
            capabilities=capabilities,
            changes=changes,
        )

    def update(
        self,
        request: AppUpdateRequest,
        *,
        expected_plan: AppUpdatePlan | None = None,
    ) -> AppUpdateResult:
        _validate_request(request)
        try:
            if expected_plan is not None:
                if type(expected_plan) is not AppUpdatePlan:
                    raise AppUpdatePlanChangedError(
                        "application update plan changed"
                    )
                if self._publisher.recovery_pending(request.repository):
                    raise AppUpdatePlanChangedError(
                        "application update plan changed"
                    )
                recovered = False
            else:
                recovered = self._publisher.recover(request.repository)
            plan = self.preview(request)
            if expected_plan is not None and plan != expected_plan:
                raise AppUpdatePlanChangedError("application update plan changed")
            if plan.changes:
                self._publisher.publish(
                    request.repository,
                    plan.changes,
                    lambda: self._verify_published_candidate(
                        request.repository.resolve(strict=True), plan
                    ),
                )
            return AppUpdateResult(plan=plan, recovered=recovered)
        except AppUpdateError:
            raise
        except AppUpdateTransactionError as error:
            raise AppUpdateError("application update failed") from error
        except (OSError, RuntimeError, ValueError) as error:
            raise AppUpdateError("application update failed") from error

    def _verify_published_candidate(
        self, repository: Path, plan: AppUpdatePlan
    ) -> None:
        for change in plan.changes:
            if type(change.after) is not bytes:
                raise AppUpdateError("application update planning failed")
        replacements = {change.path: change.after for change in plan.changes}
        try:
            for change in plan.changes:
                destination = repository / change.path
                if (
                    destination.is_symlink()
                    or not destination.is_file()
                    or destination.read_bytes() != change.after
                ):
                    raise OSError
            report = self._inspector.inspect_candidate(
                repository,
                replacements,
                frozenset(
                    change.path for change in plan.changes if change.before is None
                ),
            )
        except Exception as error:
            raise AppUpdateError("application update verification failed") from error
        if any(item.severity == "error" for item in report.diagnostics):
            raise AppUpdateError("application update verification failed")
