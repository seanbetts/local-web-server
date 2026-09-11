"""Safe, deterministic creation of complete platform application repositories."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .app_doctor import AppDoctor, AppInspector
from .app_foundation import render_generated_repository
from .app_identity import AppIdentityCatalogue
from .app_initialization import AppInitializationError, ExistingEmptyDestination
from .app_provenance import (
    CURRENT_TEMPLATE_VERSION,
    PROVENANCE_FILENAME,
    AppProvenance,
    UiArtifactReference,
    render_provenance,
)
from .app_template import TemplateError, TemplateInputs, render_template
from .config import ConfigError, SUPPORTED_PLATFORM_CONTRACT, load_manifest
from .icons import canonical_manifest_icon
from .process_runner import (
    ProcessCommand,
    ProcessRunError,
    ProcessRunner,
    ProcessWorkflowRunner,
)
from .ui_package import UiPackageArtifact, UiPackageError, build_ui_package
from .release_composer import AppReleaseComposer, ReleaseComposer, ReleaseCompositionError


_PLATFORM_ROOT = Path(__file__).resolve().parents[1]
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_RENAME_EXCL = 0x00000004
_RENAME_NOREPLACE = 0x00000001
_LIBC = ctypes.CDLL(None, use_errno=True)


class AppGenerationError(ValueError):
    """A creation request could not be completed without unsafe partial state."""


@dataclass(frozen=True)
class CreateAppRequest:
    app_id: str
    title: str
    destination: Path
    route: str
    icon: str
    accent: str
    capabilities: tuple[str, ...]
    kind: str = "static"


@dataclass(frozen=True)
class CreateAppPreview:
    destination: Path
    route: str
    icon: str
    accent: str
    capabilities: tuple[str, ...]
    ui_version: str
    template_version: int
    kind: str = "static"


@dataclass(frozen=True)
class CreateAppResult:
    destination: Path
    ui_version: str
    ui_sha256: str
    commit: str


ArtifactBuilder = Callable[[Path, Path], UiPackageArtifact]


def _ui_version(repository: Path) -> str:
    try:
        payload = json.loads(
            (repository / "packages/ui/package.json").read_text(encoding="utf-8")
        )
        version = payload["version"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise AppGenerationError("platform UI metadata is unavailable") from error
    if type(version) is not str or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+", version
    ):
        raise AppGenerationError("platform UI metadata is unavailable")
    return version


def _validate_request(request: CreateAppRequest, ui_version: str) -> None:
    if type(request) is not CreateAppRequest or not isinstance(request.destination, Path):
        raise AppGenerationError("app creation request is invalid")
    try:
        if AppIdentityCatalogue.load().is_reserved(
            canonical_manifest_icon(request.icon)
        ):
            raise ValueError
        render_template(
            TemplateInputs(
                app_id=request.app_id,
                title=request.title,
                route=request.route,
                icon=request.icon,
                accent=request.accent,
                ui_version=ui_version,
                ui_sha256="0" * 64,
                capabilities=request.capabilities,
                kind=request.kind,
            )
        )
    except (TemplateError, TypeError, ValueError) as error:
        raise AppGenerationError("app creation request is invalid") from error


def _lexical_destination(destination: Path) -> Path:
    try:
        lexical = Path(os.path.abspath(os.fspath(destination)))
    except (OSError, TypeError, ValueError) as error:
        raise AppGenerationError("destination is unsafe") from error
    if lexical == Path(lexical.anchor) or lexical.name in {"", ".", ".."}:
        raise AppGenerationError("destination is unsafe")
    return lexical


def _require_real_directory(path: Path) -> None:
    current = Path(path.anchor)
    try:
        for component in path.parts[1:]:
            current /= component
            metadata = os.lstat(current)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                raise AppGenerationError("destination is unsafe")
    except AppGenerationError:
        raise
    except OSError as error:
        raise AppGenerationError("destination parent is unavailable") from error


def _resolve_destination(destination: Path) -> Path:
    lexical = _lexical_destination(destination)
    _require_real_directory(lexical.parent)
    try:
        physical_parent = lexical.parent.resolve(strict=True)
    except OSError as error:
        raise AppGenerationError("destination parent is unavailable") from error
    if physical_parent != lexical.parent:
        raise AppGenerationError("destination is unsafe")
    if os.path.lexists(lexical):
        raise AppGenerationError("destination is unavailable")
    return physical_parent / lexical.name


def _write_generated_tree(stage: Path, inputs: TemplateInputs) -> None:
    for rendered in render_generated_repository(inputs):
        destination = stage / rendered.path
        try:
            destination.relative_to(stage)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered.content, encoding="utf-8")
        except OSError as error:
            raise AppGenerationError("app staging failed") from error


def _write_provenance(
    stage: Path, request: CreateAppRequest, artifact: UiPackageArtifact
) -> None:
    provenance = AppProvenance(
        schema_version=1,
        template_version=CURRENT_TEMPLATE_VERSION,
        platform_contract_version=SUPPORTED_PLATFORM_CONTRACT,
        ui=UiArtifactReference(version=artifact.version, sha256=artifact.sha256),
        capabilities=request.capabilities,
        domain_palette_tokens=(),
        managed_files=(
            Path("AGENTS.md"),
            Path("local-web.json"),
            Path("vendor/local-web-ui.tgz"),
        ),
    )
    try:
        (stage / PROVENANCE_FILENAME).write_bytes(render_provenance(provenance))
    except OSError as error:
        raise AppGenerationError("app staging failed") from error


def _copy_artifact(stage: Path, artifact: UiPackageArtifact) -> None:
    try:
        if artifact.path.is_symlink() or not artifact.path.is_file():
            raise OSError
        destination = stage / "vendor/local-web-ui.tgz"
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(artifact.path, destination)
    except OSError as error:
        raise AppGenerationError("platform UI build failed") from error


def _git_commands(app_id: str) -> tuple[ProcessCommand, ...]:
    common = (
        "git",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "commit.gpgSign=false",
        "-c",
        "user.name=Local Web Generator",
        "-c",
        "user.email=local-web-generator@localhost",
    )
    return (
        ProcessCommand("git-init", common + ("init", "--initial-branch=main")),
        ProcessCommand("git-add", common + ("add", "--all")),
        ProcessCommand(
            "git-commit", common + ("commit", "-m", f"chore: scaffold {app_id}")
        ),
    )


def _read_commit(repository: Path) -> str:
    try:
        result = subprocess.run(
            ("git", "rev-parse", "--verify", "HEAD"),
            cwd=repository,
            env={
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
        commit = result.stdout.decode("ascii").strip()
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        raise AppGenerationError("Git verification failed") from error
    if result.returncode != 0 or not _COMMIT.fullmatch(commit):
        raise AppGenerationError("Git verification failed")
    return commit


def _publish_no_replace(stage: Path, destination: Path) -> None:
    try:
        parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        raise AppGenerationError("app publication failed") from error
    try:
        if hasattr(_LIBC, "renameatx_np"):
            rename = _LIBC.renameatx_np
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename.restype = ctypes.c_int
            flags = _RENAME_EXCL
        elif hasattr(_LIBC, "renameat2"):
            rename = _LIBC.renameat2
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            rename.restype = ctypes.c_int
            flags = _RENAME_NOREPLACE
        else:
            raise AppGenerationError("atomic publication is unavailable")
        ctypes.set_errno(0)
        result = rename(
            parent_fd,
            os.fsencode(stage.name),
            parent_fd,
            os.fsencode(destination.name),
            flags,
        )
        if result == 0:
            return
        if ctypes.get_errno() in {errno.EEXIST, errno.ENOTEMPTY}:
            raise AppGenerationError("destination is unavailable")
        raise AppGenerationError("app publication failed")
    finally:
        os.close(parent_fd)


class AppGenerator:
    def __init__(
        self,
        platform_repository: Path = _PLATFORM_ROOT,
        *,
        artifact_builder: ArtifactBuilder = build_ui_package,
        doctor: AppInspector | None = None,
        process_runner: ProcessWorkflowRunner | None = None,
        release_composer: AppReleaseComposer | None = None,
    ) -> None:
        self._platform_repository = platform_repository
        self._artifact_builder = artifact_builder
        self._doctor = doctor if doctor is not None else AppDoctor()
        self._process_runner = process_runner if process_runner is not None else ProcessRunner()
        self._release_composer = (
            release_composer if release_composer is not None else ReleaseComposer()
        )

    def preview(self, request: CreateAppRequest) -> CreateAppPreview:
        ui_version = _ui_version(self._platform_repository)
        _validate_request(request, ui_version)
        destination = _resolve_destination(request.destination)
        return CreateAppPreview(
            destination=destination,
            route=request.route,
            icon=request.icon,
            accent=request.accent,
            capabilities=request.capabilities,
            ui_version=ui_version,
            template_version=CURRENT_TEMPLATE_VERSION,
            kind=request.kind,
        )

    def preview_init(self, request: CreateAppRequest) -> CreateAppPreview:
        return self._preview_init(request, recover=False)

    def create(self, request: CreateAppRequest) -> CreateAppResult:
        preview = self.preview(request)
        try:
            stage = Path(
                tempfile.mkdtemp(
                    prefix=f".{preview.destination.name}.local-web-",
                    dir=preview.destination.parent,
                )
            )
        except OSError as error:
            raise AppGenerationError("app creation failed") from error
        published = False
        try:
            result = self._build_verified_stage(
                request, preview.destination, stage
            )
            _publish_no_replace(stage, preview.destination)
            published = True
            return result
        except (
            AppGenerationError,
            ProcessRunError,
            UiPackageError,
            OSError,
            TemplateError,
            ValueError,
            subprocess.SubprocessError,
        ) as error:
            if isinstance(error, AppGenerationError) and str(error) in {
                "destination is unavailable",
                "destination is unsafe",
                "destination parent is unavailable",
            }:
                raise
            raise AppGenerationError("app creation failed") from error
        finally:
            if not published:
                try:
                    shutil.rmtree(stage)
                except OSError as error:
                    cleanup_error = AppGenerationError("app staging cleanup failed")
                    cleanup_error.__cause__ = error
                    raise AppGenerationError("app creation failed") from cleanup_error

    def init(self, request: CreateAppRequest) -> CreateAppResult:
        publisher = ExistingEmptyDestination()
        preview = self._preview_init(request, recover=True, publisher=publisher)
        try:
            paths = publisher.paths(preview.destination)
            paths.stage.mkdir(mode=0o700)
        except (AppInitializationError, OSError) as error:
            raise AppGenerationError("app creation failed") from error
        published = False
        try:
            result = self._build_verified_stage(
                request, preview.destination, paths.stage
            )
            publisher.publish(paths.stage, preview.destination)
            published = True
            return result
        except (
            AppGenerationError,
            AppInitializationError,
            ProcessRunError,
            UiPackageError,
            OSError,
            TemplateError,
            ValueError,
            subprocess.SubprocessError,
        ) as error:
            raise AppGenerationError("app creation failed") from error
        finally:
            if not published and os.path.lexists(paths.stage):
                try:
                    shutil.rmtree(paths.stage)
                except OSError as error:
                    cleanup_error = AppGenerationError("app staging cleanup failed")
                    cleanup_error.__cause__ = error
                    raise AppGenerationError("app creation failed") from cleanup_error

    def _preview_init(
        self,
        request: CreateAppRequest,
        *,
        recover: bool,
        publisher: ExistingEmptyDestination | None = None,
    ) -> CreateAppPreview:
        ui_version = _ui_version(self._platform_repository)
        _validate_request(request, ui_version)
        destination_handler = (
            publisher if publisher is not None else ExistingEmptyDestination()
        )
        try:
            destination = destination_handler.resolve(
                request.destination, recover=recover
            )
        except AppInitializationError as error:
            raise AppGenerationError(str(error)) from error
        return CreateAppPreview(
            destination=destination,
            route=request.route,
            icon=request.icon,
            accent=request.accent,
            capabilities=request.capabilities,
            ui_version=ui_version,
            template_version=CURRENT_TEMPLATE_VERSION,
            kind=request.kind,
        )

    def _build_verified_stage(
        self,
        request: CreateAppRequest,
        destination: Path,
        stage: Path,
    ) -> CreateAppResult:
        with tempfile.TemporaryDirectory(
            prefix=".local-web-app-artifact-", dir=self._platform_repository.parent
        ) as artifact_text:
            artifact_path = Path(artifact_text) / "local-web-ui.tgz"
            artifact = self._artifact_builder(self._platform_repository, artifact_path)
            if artifact.path != artifact_path:
                raise AppGenerationError("platform UI build failed")
            _copy_artifact(stage, artifact)
            _write_generated_tree(
                stage,
                TemplateInputs(
                    app_id=request.app_id,
                    title=request.title,
                    route=request.route,
                    icon=request.icon,
                    accent=request.accent,
                    ui_version=artifact.version,
                    ui_sha256=artifact.sha256,
                    capabilities=request.capabilities,
                    kind=request.kind,
                ),
            )
            _write_provenance(stage, request, artifact)

            git_init, git_add, git_commit = _git_commands(request.app_id)
            self._process_runner.run(
                stage,
                (
                    ProcessCommand(
                        "npm-lock",
                        ("npm", "install", "--package-lock-only", "--ignore-scripts"),
                    ),
                    ProcessCommand(
                        "npm-install", ("npm", "ci", "--ignore-scripts")
                    ),
                    git_init,
                    git_add,
                ),
            )
            report = self._doctor.inspect(stage)
            if report.diagnostics:
                raise AppGenerationError("app creation failed")
            self._process_runner.run(
                stage,
                (
                    ProcessCommand("npm-check", ("npm", "run", "check")),
                    ProcessCommand("npm-e2e", ("npm", "run", "test:e2e")),
                ),
            )
            try:
                manifest = load_manifest(stage / "local-web.json")
                self._release_composer.compose(stage, manifest.build)
            except (ConfigError, ReleaseCompositionError) as error:
                raise AppGenerationError("app creation failed") from error
            shutil.rmtree(stage / "node_modules")
            self._process_runner.run(stage, (git_commit,))
            commit = _read_commit(stage)
            return CreateAppResult(
                destination=destination,
                ui_version=artifact.version,
                ui_sha256=artifact.sha256,
                commit=commit,
            )
