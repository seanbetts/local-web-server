#!/usr/bin/env python3
"""Run the complete generated-app lifecycle without publishing live state."""

from __future__ import annotations

import hashlib
import argparse
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PLATFORM_REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_REPOSITORY))

from local_web_server.app_provenance import CURRENT_TEMPLATE_VERSION, load_provenance
from local_web_server.app_activation import AppActivationError, AppActivator
from local_web_server.config import build_environment, load_manifest, load_registry
from local_web_server.deploy import DeploymentResult, ReleaseBuilder
from local_web_server.git_build import Builder, GitRepository
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore
from local_web_server.index_registry import INDEX_REGISTRY_NAME, render_index_registry
from local_web_server.install import load_main_manifests
from local_web_server.models import (
    AppManifest,
    FrontendSecuritySpec,
    HostApp,
    HostRegistry,
)
from local_web_server.render import render_caddyfile
from local_web_server.runtime import RuntimeLayout, atomic_symlink
from local_web_server.services import HttpHealthChecker
from local_web_server.ui_package import CURRENT_UI_PACKAGE_VERSION
from scripts.disposable_workflow_support import (
    initialise_disposable_host_profile,
    sanitized_subprocess_environment,
)


CODING_ROOT = PLATFORM_REPOSITORY.parent
_CONTEXT_EXPORT_PATH = Path("src/contextExport.ts")
_CONTEXT_EXPORT_MAX_BYTES = 64 * 1024
_CONTEXT_EXPORT_SCHEMA = "local-web-context/v1"
_CADDY = Path("/opt/homebrew/bin/caddy")
_PLAYWRIGHT = PLATFORM_REPOSITORY / "node_modules/playwright"
_SERVING_CONNECT_ORIGIN = "https://api.maptiler.com"
_SERVING_IMAGE_ORIGIN = "https://images.example.test"
_CLOSED_EXPORT_CSP = "default-src 'none'; connect-src 'none'; img-src 'none'; font-src 'none'; media-src 'none'; object-src 'none'; frame-src 'none'; script-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"
_DEFAULT_SERVING_CSP = "default-src 'self'; connect-src 'self'; img-src 'self' data:; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
_EXTENDED_SERVING_CSP = "default-src 'self'; connect-src 'self' https://api.maptiler.com; img-src 'self' data: https://images.example.test; script-src 'self'; style-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
_BROWSER_DOWNLOAD_CHECK = r"""
const { readFileSync } = require('node:fs');
const { chromium } = require(process.argv[1]);

(async () => {
  const [url, expectedServingCsp, expectedAssetPrefix, closedExportCsp, ...servingOrigins] = process.argv.slice(2);
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const response = await page.goto(url);
    if (!response || !response.ok() || response.headers()['content-security-policy'] !== expectedServingCsp) {
      throw new Error('generated service serving policy was not applied');
    }
    const generatedAssets = await page
      .locator('script[src*="/assets/"], link[href*="/assets/"]')
      .evaluateAll((elements) => elements.map((element) => element.src || element.href));
    if (
      generatedAssets.length === 0 ||
      generatedAssets.some((asset) => !new URL(asset).pathname.startsWith(expectedAssetPrefix))
    ) {
      throw new Error('generated service assets did not use the registered route prefix');
    }
    const assetResponses = await Promise.all(
      generatedAssets.map((asset) => page.request.get(asset)),
    );
    if (assetResponses.some((assetResponse) => !assetResponse.ok())) {
      throw new Error('generated service prefixed asset was unavailable');
    }
    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.getByRole('button', { name: 'Export context' }).click(),
    ]);
    const path = await download.path();
    if (!path) {
      throw new Error('generated service context download was unavailable');
    }
    const html = readFileSync(path, 'utf8');
    const closedPolicy = `<meta http-equiv="Content-Security-Policy" content="${closedExportCsp}">`;
    if (!html.includes(closedPolicy) || servingOrigins.some((origin) => html.includes(origin))) {
      throw new Error('downloaded context policy was not isolated from serving policy');
    }
  } finally {
    await browser.close();
  }
})().catch(() => process.exit(1));
"""


@dataclass(frozen=True)
class WorkflowCommand:
    label: str
    argv: tuple[str, ...]
    cwd: Path
    timeout: int


CommandExecutor = Callable[[WorkflowCommand], None]
ActivationExecutor = Callable[[Path, Path, str], None]
HostedCspExecutor = Callable[[Path, Path], None]
HostedPolicyExercise = Callable[[Path, Path, AppManifest, str], None]
Emitter = Callable[[str], None]


def _registry_unchanged(path: Path, content: bytes, mode: int) -> bool:
    try:
        return (
            path.read_bytes() == content
            and stat.S_IMODE(path.stat().st_mode) == mode
        )
    except Exception:
        return False


def _read_context_export_metadata(repository: Path) -> tuple[str, str] | None:
    """Read only the generated adapter literals; never expose its payload body."""

    path = repository / _CONTEXT_EXPORT_PATH
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            return None
        if metadata.st_size > _CONTEXT_EXPORT_MAX_BYTES:
            return None
        with path.open(encoding="utf-8") as handle:
            content = handle.read(_CONTEXT_EXPORT_MAX_BYTES + 1)
    except (OSError, UnicodeError):
        return None
    if len(content) > _CONTEXT_EXPORT_MAX_BYTES:
        return None
    if (
        "buildContextExport" not in content
        or "LocalWebContextV1" not in content
        or "schema: 'local-web-context/v1'" not in content
        or "classification: 'private'" not in content
    ):
        return None
    return (_CONTEXT_EXPORT_SCHEMA, "private")


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    if not _process_group_exists(process.pid):
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        process.poll()
        if not _process_group_exists(process.pid):
            break
        time.sleep(0.01)
    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            process.poll()
            if not _process_group_exists(process.pid):
                break
            time.sleep(0.01)
    if _process_group_exists(process.pid):
        raise RuntimeError("workflow command cleanup failed")
    process.wait()


def execute_command(command: WorkflowCommand) -> None:
    """Execute one fixed command without exposing child output."""

    process = subprocess.Popen(
        command.argv,
        cwd=command.cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=sanitized_subprocess_environment(
            overrides={"PYTHONDONTWRITEBYTECODE": "1"}
        ),
        close_fds=True,
        start_new_session=True,
    )
    failure: BaseException | None = None
    return_code: int | None = None
    try:
        try:
            return_code = process.wait(timeout=command.timeout)
        except BaseException as error:
            failure = error
    finally:
        try:
            _stop_process_group(process)
        except BaseException as cleanup_error:
            if failure is None:
                failure = cleanup_error
    if failure is not None:
        raise RuntimeError("workflow command failed") from failure
    if return_code != 0:
        raise RuntimeError("workflow command failed")


class _DisposableInstaller:
    def __init__(self, registry_path: Path):
        self.registry_path = registry_path

    def __call__(
        self,
        _repository: Path,
        *,
        dry_run: bool,
        recover_missing_caddy: bool = False,
    ) -> object:
        if dry_run or not recover_missing_caddy:
            raise RuntimeError("disposable activation contract failed")
        registry = load_registry(self.registry_path)
        manifests = load_main_manifests(registry)
        destination = (
            registry.runtime_root / "platform" / "index" / INDEX_REGISTRY_NAME
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(render_index_registry(registry, manifests))
        return object()


class _DisposableRequestHandler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        self.send_response(503 if self.path == "/unhealthy" else 200)
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != f"/_local-web/platform/index/{INDEX_REGISTRY_NAME}":
            self.send_response(404)
            self.end_headers()
            return
        try:
            content = (
                self.server.runtime_root
                / "platform/index"
                / INDEX_REGISTRY_NAME
            ).read_bytes()
        except OSError:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, _format: str, *args: object) -> None:
        return


class _DisposableHttpServer:
    def __init__(self, runtime_root: Path, *, port: int = 0):
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", port), _DisposableRequestHandler
        )
        self.server.runtime_root = Path(runtime_root)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "_DisposableHttpServer":
        self.thread.start()
        return self

    def __exit__(self, *_error: object) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        if self.thread.is_alive():
            raise RuntimeError("disposable HTTP cleanup failed")


class _DisposableDeployer:
    def __init__(self, registry, repository: Path, *, fail: bool):
        self.registry = registry
        self.repository = Path(repository)
        self.fail = fail

    def deploy(
        self,
        app_id: str,
        *,
        install: Callable[[], None] | None = None,
    ) -> DeploymentResult:
        if install is not None:
            install()
        health = HttpHealthChecker()
        if self.fail:
            result = health.check(f"http://{self.registry.host}/unhealthy")
            if result.healthy or result.status != 503:
                raise RuntimeError("disposable health contract failed")
            raise RuntimeError("disposable deployment failed")
        manifest = load_manifest(self.repository / "local-web.json")
        public_paths = [f"{manifest.route}/"]
        if manifest.kind == "service":
            public_paths.append(manifest.health_path)
        for path in public_paths:
            if not health.check(f"http://{self.registry.host}{path}").healthy:
                raise RuntimeError("disposable health contract failed")
        if manifest.kind == "service":
            host = next(app for app in self.registry.apps if app.id == app_id)
            with _DisposableHttpServer(
                self.registry.runtime_root, port=host.port
            ) as internal:
                result = health.check(
                    f"http://{internal.host}{manifest.service.internal_health_path}",
                    host=self.registry.host,
                )
                if not result.healthy:
                    raise RuntimeError("disposable health contract failed")
        return DeploymentResult(app_id, "a" * 40, "deployed", "deployed", None)


def _run_git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("/usr/bin/git", "-C", str(repository), *arguments),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=sanitized_subprocess_environment(),
        check=False,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError("disposable activation contract failed")
    return result.stdout.strip()


def _exclude_disposable_private_profile(platform: Path) -> None:
    exclude = Path(platform) / ".git/info/exclude"
    content = exclude.read_text(encoding="utf-8")
    if "config/local/" not in content.splitlines():
        exclude.write_text(content + "\nconfig/local/\n", encoding="utf-8")


def _clone_disposable_platform(source: Path, destination: Path) -> None:
    result = subprocess.run(
        (
            "/usr/bin/git",
            "clone",
            "--quiet",
            "--no-local",
            "--single-branch",
            str(source),
            str(destination),
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=sanitized_subprocess_environment(),
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError("disposable platform clone failed")
    _run_git(destination, "switch", "-C", "main")
    _exclude_disposable_private_profile(destination)


def execute_disposable_activation(
    app_repository: Path, temporary_root: Path, kind: str
) -> None:
    """Exercise failed and recovered activation without touching the live host."""

    platform = Path(temporary_root) / "activation-platform"
    runtime = Path(temporary_root) / "activation-runtime"
    platform.mkdir()
    (platform / "config").mkdir()
    (platform / "local_web_server").mkdir()
    (platform / "local_web_server/source.py").write_text(
        "# disposable activation\n", encoding="utf-8"
    )
    (platform / ".gitignore").write_text(
        "config/local/\n", encoding="utf-8"
    )
    _run_git(platform, "init", "-b", "main")
    _run_git(platform, "config", "user.name", "Disposable Activation")
    _run_git(platform, "config", "user.email", "activation@localhost")
    _run_git(platform, "add", ".")
    _run_git(platform, "commit", "-m", "fixture")
    with _DisposableHttpServer(runtime) as index:
        registry_content = (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "host": index.host,
                    "runtimeRoot": str(runtime),
                    "apps": [],
                },
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        registry_path = initialise_disposable_host_profile(
            platform, registry_content
        )
        platform_head = _run_git(platform, "rev-parse", "HEAD")

        installer = _DisposableInstaller(registry_path)

        def activator(*, fail: bool) -> AppActivator:
            return AppActivator(
                platform_repository=platform,
                registry_path=registry_path,
                profile_store=HostProfileStore(
                    HostProfilePaths.for_repository(platform)
                ),
                install=installer,
                deployer_factory=lambda registry: _DisposableDeployer(
                    registry, app_repository, fail=fail
                ),
            )

        try:
            activator(fail=True).activate(app_repository)
        except AppActivationError as error:
            if str(error) != "application deployment failed":
                raise RuntimeError("disposable activation contract failed") from error
        else:
            raise RuntimeError("disposable activation contract failed")

        if _run_git(platform, "status", "--short"):
            raise RuntimeError("disposable activation contract failed")
        if _run_git(platform, "rev-parse", "HEAD") != platform_head:
            raise RuntimeError("disposable activation contract failed")
        failed_registry = load_registry(registry_path)
        registered = next(
            (app for app in failed_registry.apps if app.id == app_repository.name),
            None,
        )
        if registered is None or (kind == "service") != (registered.port is not None):
            raise RuntimeError("disposable activation contract failed")

        checkpoint = tuple(
            revision.revision_id
            for revision in HostProfileStore(
                HostProfilePaths.for_repository(platform)
            ).revisions()
        )
        recovered = activator(fail=False).activate(app_repository)
        if (
            not recovered.verified
            or recovered.registry_revision is not None
            or tuple(
                revision.revision_id
                for revision in HostProfileStore(
                    HostProfilePaths.for_repository(platform)
                ).revisions()
            )
            != checkpoint
            or _run_git(platform, "rev-parse", "HEAD") != platform_head
            or _run_git(platform, "status", "--short")
        ):
            raise RuntimeError("disposable activation contract failed")


def _available_loopback_port() -> int:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        return reservation.getsockname()[1]


def _extended_service_manifest(manifest: AppManifest) -> AppManifest:
    service = manifest.service
    if service is None or service.frontend_security is not None:
        raise RuntimeError("hosted CSP acceptance contract failed")
    return replace(
        manifest,
        service=replace(
            service,
            frontend_security=FrontendSecuritySpec(
                connect_sources=(_SERVING_CONNECT_ORIGIN,),
                img_sources=(_SERVING_IMAGE_ORIGIN,),
            ),
        ),
    )


def _wait_for_hosted_frontend(url: str, process: subprocess.Popen[bytes]) -> None:
    checker = HttpHealthChecker()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if checker.check(url).healthy:
            return
        time.sleep(0.02)
    raise RuntimeError("hosted CSP acceptance contract failed")


def _exercise_hosted_policy(
    repository: Path,
    runtime_root: Path,
    manifest,
    expected_serving_csp: str,
) -> None:
    port = _available_loopback_port()
    registry = HostRegistry(
        host=f"127.0.0.1:{port}",
        runtime_root=runtime_root,
        apps=(
            HostApp(
                id=manifest.id,
                repository=repository,
                auto_deploy=False,
                environment_file=None,
                environment=(),
                port=9,
                start_command=None,
            ),
        ),
    )
    rendered = render_caddyfile(registry, {manifest.id: manifest})
    rendered = "{\n    admin off\n}\n" + rendered.replace(
        ":80 {", f"http://127.0.0.1:{port} {{", 1
    )
    caddyfile = runtime_root / f"Caddyfile-{port}"
    caddyfile.write_text(rendered, encoding="utf-8")
    process = subprocess.Popen(
        (_CADDY, "run", "--config", caddyfile, "--adapter", "caddyfile"),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=sanitized_subprocess_environment(),
        close_fds=True,
        start_new_session=True,
    )
    try:
        url = f"http://127.0.0.1:{port}{manifest.route}/"
        _wait_for_hosted_frontend(url, process)
        execute_command(
            WorkflowCommand(
                "generated service context download",
                (
                    "/usr/bin/env",
                    "node",
                    "-e",
                    _BROWSER_DOWNLOAD_CHECK,
                    str(_PLAYWRIGHT),
                    url,
                    expected_serving_csp,
                    f"{manifest.route}/assets/",
                    _CLOSED_EXPORT_CSP,
                    _SERVING_CONNECT_ORIGIN,
                    _SERVING_IMAGE_ORIGIN,
                ),
                PLATFORM_REPOSITORY,
                60,
            )
        )
    finally:
        _stop_process_group(process)


def execute_hosted_service_csp_acceptance(
    repository: Path,
    temporary_root: Path,
    *,
    builder: ReleaseBuilder | None = None,
    exercise: HostedPolicyExercise = _exercise_hosted_policy,
) -> None:
    """Host one generated split service under default and extended CSP."""

    git_repository = GitRepository(repository)
    commit = git_repository.main_commit()
    manifest = git_repository.manifest_at(commit)
    if (
        manifest.kind != "service"
        or manifest.service is None
        or manifest.service.frontend_output is None
        or manifest.service.frontend_security is not None
    ):
        raise RuntimeError("hosted CSP acceptance contract failed")
    runtime_root = Path(temporary_root) / "hosted-csp-runtime"
    runtime_root.mkdir(parents=True)
    layout = RuntimeLayout(runtime_root, manifest.id)
    host = HostApp(
        id=manifest.id,
        repository=repository,
        auto_deploy=False,
        environment_file=None,
        environment=(("VITE_PUBLIC_BASE_PATH", f"{manifest.route}/"),),
        port=9,
        start_command=None,
    )
    try:
        with (runtime_root / "hosted-build.log").open("w", encoding="utf-8") as log:
            release = (builder or Builder()).build(
                host,
                manifest,
                commit,
                layout,
                build_environment(manifest, host, os.environ),
                log,
            )
        atomic_symlink(layout.current, release)
    except Exception as error:
        raise RuntimeError("hosted CSP acceptance contract failed") from error

    exercise(repository, runtime_root, manifest, _DEFAULT_SERVING_CSP)
    exercise(
        repository,
        runtime_root,
        _extended_service_manifest(manifest),
        _EXTENDED_SERVING_CSP,
    )


class NewAppWorkflowVerifier:
    def __init__(
        self,
        *,
        platform_repository: Path = PLATFORM_REPOSITORY,
        coding_root: Path = CODING_ROOT,
        registry: Path | None = None,
        execute: CommandExecutor = execute_command,
        activate: ActivationExecutor = execute_disposable_activation,
        hosted_csp: HostedCspExecutor = execute_hosted_service_csp_acceptance,
        emit: Emitter = print,
        kind: str = "static",
    ) -> None:
        self._platform_repository = Path(platform_repository).resolve()
        self._coding_root = Path(coding_root).resolve()
        self._registry = None if registry is None else Path(registry)
        self._execute = execute
        self._activate = activate
        self._hosted_csp = hosted_csp
        self._emit = emit
        if kind not in {"static", "service"}:
            raise ValueError("workflow kind is invalid")
        self._kind = kind

    def _commands(
        self, repository: Path, platform_repository: Path | None = None
    ) -> tuple[WorkflowCommand, ...]:
        platform = self._platform_repository if platform_repository is None else Path(platform_repository)
        local_web = str((platform / "bin/local-web").resolve())
        repository_text = str(repository)
        init_arguments = [
            local_web,
            "app",
            "init",
            repository_text,
            "--title",
            "Disposable Workflow Verification",
            "--icon",
            "book",
            "--accent",
            "#8EA7C6",
        ]
        if self._kind == "service":
            init_arguments.extend(("--kind", "service"))
        return (
            WorkflowCommand(
                "app init",
                tuple(init_arguments),
                platform,
                600,
            ),
            WorkflowCommand(
                "app doctor",
                (local_web, "app", "doctor", "--repository", repository_text),
                platform,
                60,
            ),
            WorkflowCommand(
                "app-local dependency preparation",
                ("npm", "ci", "--ignore-scripts"),
                repository,
                300,
            ),
            WorkflowCommand(
                "app check",
                (local_web, "app", "check", "--repository", repository_text),
                platform,
                600,
            ),
            WorkflowCommand(
                "app activate preview",
                (local_web, "app", "activate", "--repository", repository_text),
                platform,
                60,
            ),
        )

    def run(self) -> int:
        temporary: tempfile.TemporaryDirectory[str] | None = None
        registry_before: bytes | None = None
        registry_mode: int | None = None
        last_command_label = "app activate preview"
        result = 0
        registry: Path | None = self._registry
        platform = self._platform_repository
        try:
            temporary = tempfile.TemporaryDirectory(
                prefix=".local-web-new-app-workflow-", dir=self._coding_root
            )
            repository = Path(temporary.name) / "disposable-workflow-verification"
            repository.mkdir()
            self._emit("create empty folder PASS")

            if registry is None:
                platform = Path(temporary.name) / "disposable-platform"
                _clone_disposable_platform(self._platform_repository, platform)
                registry_content = (
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "host": "new-app-workflow.invalid",
                            "runtimeRoot": str(Path(temporary.name) / "runtime"),
                            "apps": [],
                        }
                    )
                    + "\n"
                ).encode("utf-8")
                registry = initialise_disposable_host_profile(
                    platform, registry_content
                )

            try:
                registry_before = registry.read_bytes()
                registry_mode = stat.S_IMODE(registry.stat().st_mode)
            except Exception:
                self._emit("app activate preview FAIL")
                result = 1

            for command in () if result else self._commands(repository, platform):
                last_command_label = command.label
                failed = False
                provenance = None
                context_export = None
                try:
                    self._execute(command)
                    if command.label == "app init":
                        provenance = load_provenance(
                            repository / ".local-web-platform.json"
                        )
                        context_export = _read_context_export_metadata(repository)
                        if (
                            provenance.template_version != CURRENT_TEMPLATE_VERSION
                            or provenance.ui.version != CURRENT_UI_PACKAGE_VERSION
                            or context_export is None
                        ):
                            failed = True
                except Exception:
                    failed = True
                try:
                    if not _registry_unchanged(
                        registry, registry_before, registry_mode
                    ):
                        failed = True
                except Exception:
                    failed = True
                if failed:
                    self._emit(f"{command.label} FAIL")
                    result = 1
                    break

                self._emit(f"{command.label} PASS")
                if provenance is not None:
                    self._emit(
                        f"platform version {provenance.platform_contract_version}"
                    )
                    self._emit(f"template version {provenance.template_version}")
                    self._emit(f"UI version {provenance.ui.version}")
                    self._emit(f"UI digest {provenance.ui.sha256}")
                    self._emit(f"context export schema {context_export[0]}")
                    self._emit(f"context export sensitivity {context_export[1]}")
                if command.label == "app check" and self._kind == "service":
                    if not self._service_release_ready(repository):
                        self._emit("release contract FAIL")
                        result = 1
                        break
                    self._emit("release contract PASS")
            if result == 0 and self._kind == "service":
                last_command_label = "hosted generated service CSP acceptance"
                try:
                    self._hosted_csp(repository, Path(temporary.name))
                    if not _registry_unchanged(
                        registry, registry_before, registry_mode
                    ):
                        raise RuntimeError("live registry changed")
                except Exception:
                    self._emit("hosted generated service CSP acceptance FAIL")
                    result = 1
                else:
                    self._emit("hosted generated service CSP acceptance PASS")
            if result == 0:
                last_command_label = "app activate apply/recovery"
                try:
                    self._activate(repository, Path(temporary.name), self._kind)
                    if not _registry_unchanged(
                        registry, registry_before, registry_mode
                    ):
                        raise RuntimeError("live registry changed")
                except Exception:
                    self._emit("app activate apply/recovery FAIL")
                    result = 1
                else:
                    self._emit("app activate apply/recovery PASS")
                    self._emit(
                        f"registry digest {hashlib.sha256(registry_before).hexdigest()}"
                    )
        except Exception:
            self._emit("create empty folder FAIL")
            result = 1
        finally:
            if registry_before is not None and registry_mode is not None:
                try:
                    unchanged = _registry_unchanged(
                        registry, registry_before, registry_mode
                    )
                except Exception:
                    unchanged = False
                if not unchanged and result == 0:
                    self._emit(f"{last_command_label} FAIL")
                    result = 1
            try:
                if temporary is not None:
                    temporary.cleanup()
            except Exception:
                self._emit("cleanup FAIL")
                result = 1
            else:
                self._emit("cleanup PASS")
        return result

    @staticmethod
    def _service_release_ready(repository: Path) -> bool:
        try:
            manifest = load_manifest(repository / "local-web.json")
            if (
                manifest.kind != "service"
                or manifest.service is None
                or not manifest.build.release_entries
                or manifest.service.frontend_output is None
            ):
                return False
            output = repository / manifest.build.output
            return (
                output.is_dir()
                and (output / manifest.service.frontend_output).is_dir()
                and all((output / entry.target).exists() for entry in manifest.build.release_entries)
            )
        except Exception:
            return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=("static", "service"), default="static")
    args = parser.parse_args()
    return NewAppWorkflowVerifier(kind=args.kind).run()


if __name__ == "__main__":
    raise SystemExit(main())
