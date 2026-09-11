#!/usr/bin/env python3
"""Verify static-to-service activation without publishing live host state."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

PLATFORM_REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_REPOSITORY))

from local_web_server.app_activation import AppActivationError, AppActivator
from local_web_server.app_registration import AppRegistrar
from local_web_server.config import load_manifest, load_registry
from local_web_server.deploy import DeploymentManager
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore
from local_web_server.runtime import RuntimeLayout
from local_web_server.services import HealthResult, ServiceState
from scripts.verify_new_app_workflow import (
    WorkflowCommand,
    _DisposableDeployer,
    _DisposableHttpServer,
    _DisposableInstaller,
    _process_group_exists,
    _registry_unchanged,
    _run_git,
    _stop_process_group,
    execute_command,
)
from scripts.disposable_workflow_support import (
    initialise_disposable_host_profile,
    sanitized_subprocess_environment,
)


CODING_ROOT = PLATFORM_REPOSITORY.parent
DATA_SENTINEL = b'{"state":"canonical"}\n'
ACTIVATION_PASS_LABELS = (
    "activate static",
    "commit service manifest",
    "preview transition without writes",
    "apply transition",
    "inspect expanded private argv",
    "retry activation without a second registry revision",
    "recover failed service redeployment",
    "retry recovered service deployment",
    "verify repository data sentinel",
)
_FAILURE_LABELS = frozenset(
    (
        "live registry guard",
        "create eligible static fixture",
        *ACTIVATION_PASS_LABELS,
        "repository service transition",
        "cleanup",
    )
)


@dataclass(frozen=True)
class FixtureCreationCommand:
    label: str
    source_platform: Path
    platform_repository: Path
    app_repository: Path


CommandExecutor = Callable[[FixtureCreationCommand], None]
ActivationExecutor = Callable[[Path, Path], tuple[str, ...]]
Emitter = Callable[[str], None]
_T = TypeVar("_T")
_MAX_SERVICE_EVIDENCE_BYTES = 8192
_SERVICE_START_TIMEOUT_SECONDS = 5.0


class TransitionAcceptanceError(RuntimeError):
    """A bounded disposable phase failed without exposing private paths."""

    def __init__(self, label: str, cause: BaseException | None = None):
        safe_label = (
            label if label in _FAILURE_LABELS else "repository service transition"
        )
        super().__init__(safe_label)
        if cause is not None:
            self.__cause__ = cause

    @property
    def label(self) -> str:
        return str(self)


def _phase(label: str, operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except TransitionAcceptanceError:
        raise
    except BaseException as error:
        raise TransitionAcceptanceError(label, error) from error


def _snapshot_disposable_tree(root: Path) -> tuple[tuple[object, ...], ...]:
    root = Path(root)
    if not os.path.lexists(root):
        return (("missing",),)
    paths = (root, *sorted(root.rglob("*")))
    snapshot: list[tuple[object, ...]] = []
    for path in paths:
        metadata = path.lstat()
        relative = Path(".") if path == root else path.relative_to(root)
        mode = stat.S_IMODE(metadata.st_mode)
        identity = (metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns)
        if stat.S_ISDIR(metadata.st_mode):
            snapshot.append((relative, "directory", mode, *identity))
        elif stat.S_ISREG(metadata.st_mode):
            snapshot.append((relative, "file", mode, *identity, path.read_bytes()))
        elif stat.S_ISLNK(metadata.st_mode):
            snapshot.append(
                (relative, "symlink", mode, *identity, os.readlink(path))
            )
        else:
            raise RuntimeError("disposable state cannot be inspected")
    return tuple(snapshot)


def _read_service_evidence(process: subprocess.Popen[bytes], url: str) -> object:
    deadline = time.monotonic() + _SERVICE_START_TIMEOUT_SECONDS
    while True:
        if process.poll() is not None:
            raise RuntimeError("disposable service exited before readiness")
        try:
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json"},
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=0.25) as response:
                if response.status != 200:
                    raise RuntimeError("disposable service evidence was unavailable")
                content = response.read(_MAX_SERVICE_EVIDENCE_BYTES + 1)
            if len(content) > _MAX_SERVICE_EVIDENCE_BYTES:
                raise RuntimeError("disposable service evidence was too large")
            return json.loads(content.decode("utf-8"))
        except (ConnectionError, TimeoutError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                raise RuntimeError("disposable service did not become ready")
            time.sleep(0.02)


class _DisposableLaunchAgentController:
    """Model one launchd job while executing the real registered service argv."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root)
        self.host = None
        self.process: subprocess.Popen[bytes] | None = None
        self.loaded = False
        self.throttled = False
        self.launches: list[Path] = []
        self.commands: list[tuple[str, ...]] = []
        self.errors: list[bytes] = []
        self.last_process_id: int | None = None

    def _require_label(self, label: str) -> None:
        if self.host is None or label != f"com.sean.local-web.{self.host.id}":
            raise RuntimeError("disposable service label did not match")

    def _capture_stderr(self, process: subprocess.Popen[bytes]) -> None:
        if process.stderr is None:
            return
        try:
            content = process.stderr.read(_MAX_SERVICE_EVIDENCE_BYTES + 1)
        finally:
            process.stderr.close()
        if len(content) > _MAX_SERVICE_EVIDENCE_BYTES:
            raise RuntimeError("disposable service error output was too large")
        if content:
            self.errors.append(content)

    def _observe_exit(self) -> None:
        process = self.process
        if process is None or process.poll() is None:
            return
        process.wait()
        try:
            self._capture_stderr(process)
        finally:
            self.process = None
        self.throttled = True

    def _stop_process(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            _stop_process_group(process)
        else:
            process.wait()
        try:
            self._capture_stderr(process)
        finally:
            self.process = None

    def _launch(self) -> None:
        if self.host is None or self.host.start_command is None:
            raise RuntimeError("disposable service command is missing")
        layout = RuntimeLayout(self.runtime_root, self.host.id)
        release = layout.current.resolve(strict=True)
        command = self.host.start_command.argv
        process = subprocess.Popen(
            command,
            cwd=release,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=sanitized_subprocess_environment(),
            close_fds=True,
            start_new_session=True,
        )
        self.process = process
        self.last_process_id = process.pid
        self.launches.append(release)
        self.commands.append(command)
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            return
        self._observe_exit()

    def install(self, registry) -> None:
        services = [app for app in registry.apps if app.start_command is not None]
        if not services:
            return
        if len(services) != 1:
            raise RuntimeError("disposable service registry is ambiguous")
        self.host = services[0]
        if not self.loaded:
            self.loaded = True
            self._launch()

    def state(self, label: str) -> ServiceState:
        self._require_label(label)
        if not self.loaded:
            return ServiceState.MISSING
        self._observe_exit()
        if self.process is None:
            return ServiceState.STOPPED
        return ServiceState.RUNNING

    def ensure_running(self, label: str, _plist_path: Path) -> None:
        self._require_label(label)
        state = self.state(label)
        if state is ServiceState.MISSING:
            self.loaded = True
            self._launch()
            return
        self.restart(label, _plist_path)

    def restart(self, label: str, _plist_path: Path) -> None:
        self._require_label(label)
        self._observe_exit()
        if self.throttled:
            return
        self._stop_process()
        self._launch()

    def stop(self, label: str, _plist_path: Path) -> None:
        self._require_label(label)
        self._stop_process()
        self.loaded = False
        self.throttled = False

    def close(self) -> None:
        self._stop_process()
        self.loaded = False
        self.throttled = False

    @property
    def module_not_found(self) -> bool:
        return any(b"MODULE_NOT_FOUND" in content for content in self.errors)


class _DisposableTransitionInstaller(_DisposableInstaller):
    def __init__(self, registry_path: Path, services: _DisposableLaunchAgentController):
        super().__init__(registry_path)
        self.services = services

    def __call__(
        self,
        repository: Path,
        *,
        dry_run: bool,
        recover_missing_caddy: bool = False,
    ) -> object:
        result = super().__call__(
            repository,
            dry_run=dry_run,
            recover_missing_caddy=recover_missing_caddy,
        )
        self.services.install(load_registry(self.registry_path))
        return result


class _DisposableTransitionHealthChecker:
    """Validate release files and proxy service checks without a live Caddy install."""

    def __init__(self, registry, failure_gate: list[int]):
        self.registry = registry
        self.failure_gate = failure_gate

    @staticmethod
    def _http_health(url: str, host: str | None) -> HealthResult:
        try:
            request = urllib.request.Request(
                url,
                headers={"Host": host} if host else {},
                method="HEAD",
            )
            with urllib.request.urlopen(request, timeout=1) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            return HealthResult(False, status, f"HTTP {status}")
        except (OSError, ValueError, urllib.error.URLError) as error:
            return HealthResult(False, None, str(error)[:300])
        return HealthResult(200 <= status < 300, status, None)

    def check(self, url: str, *, host: str | None = None) -> HealthResult:
        parsed = urlsplit(url)
        registered = self.registry.apps[0]
        manifest = load_manifest(registered.repository / "local-web.json")
        layout = RuntimeLayout(self.registry.runtime_root, registered.id)

        if parsed.netloc == self.registry.host:
            if parsed.path == f"{manifest.route}/":
                try:
                    current = layout.current.resolve(strict=True)
                    frontend = (
                        current / manifest.service.frontend_output / "index.html"
                        if manifest.kind == "service"
                        and manifest.service is not None
                        and manifest.service.frontend_output is not None
                        else current / "index.html"
                    )
                    healthy = frontend.is_file()
                except (OSError, RuntimeError):
                    healthy = False
                return HealthResult(healthy, 200 if healthy else 404, None)
            if manifest.kind == "service" and manifest.service is not None:
                if parsed.path == manifest.health_path:
                    if self.failure_gate[0] > 0:
                        self.failure_gate[0] -= 1
                        return HealthResult(False, 503, "HTTP 503")
                    internal_path = manifest.service.internal_health_path
                elif parsed.path == f"{manifest.route}/api/health":
                    internal_path = "/api/health"
                else:
                    return HealthResult(False, 404, "HTTP 404")
                return self._http_health(
                    f"http://127.0.0.1:{registered.port}{internal_path}",
                    self.registry.host,
                )
            return HealthResult(False, 404, "HTTP 404")

        return self._http_health(url, host)


def create_eligible_static_fixture(command: FixtureCreationCommand) -> None:
    """Generate the fixture from a clean disposable copy of committed inputs."""

    execute_command(
        WorkflowCommand(
            "clone disposable platform",
            (
                "/usr/bin/git",
                "clone",
                "--quiet",
                "--no-local",
                "--single-branch",
                str(command.source_platform),
                str(command.platform_repository),
            ),
            command.platform_repository.parent,
            60,
        )
    )
    _run_git(command.platform_repository, "switch", "-C", "main")
    exclude = command.platform_repository / ".git/info/exclude"
    exclude.write_text(
        exclude.read_text(encoding="utf-8") + "\nconfig/local/\n",
        encoding="utf-8",
    )
    local_web = str((command.platform_repository / "bin/local-web").resolve())
    execute_command(
        WorkflowCommand(
            command.label,
            (
                "/usr/bin/env",
                "PYTHONDONTWRITEBYTECODE=1",
                local_web,
                "app",
                "init",
                str(command.app_repository),
                "--title",
                "Repository Service Transition",
                "--icon",
                "database",
                "--accent",
                "#76D39B",
            ),
            command.platform_repository,
            600,
        )
    )


def _write_service_commit(app_repository: Path) -> None:
    manifest_path = app_repository / "local-web.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["kind"] = "service"
    manifest["healthPath"] = f'{manifest["route"]}/healthz'
    manifest["build"]["output"] = "release"
    manifest["build"]["release"] = [
        {"source": "dist", "target": "public"},
        {"source": "server", "target": "server"},
    ]
    manifest["service"] = {
        "module": "server/service.mjs",
        "internalHealthPath": "/healthz",
        "frontendOutput": "public",
        "proxyPaths": ["/api"],
        "startCommand": [
            "/usr/bin/env", "node", "{release}/server/service.mjs",
            "--port", "{port}", "--data-dir", "{repository}/data",
        ],
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    server = app_repository / "server/service.mjs"
    server.parent.mkdir()
    server.write_text(
        "import { readFile } from 'node:fs/promises';\n"
        "import { createServer } from 'node:http';\n"
        "import { resolve } from 'node:path';\n"
        "const args = process.argv.slice(2);\n"
        "if (args.length !== 4 || args[0] !== '--port' || args[2] !== '--data-dir') process.exit(64);\n"
        "const port = Number(args[1]);\n"
        "if (!Number.isInteger(port) || port < 1 || port > 65535) process.exit(64);\n"
        "const dataPath = resolve(args[3]);\n"
        "const sentinel = JSON.parse(await readFile(resolve(dataPath, 'state.json'), 'utf8'));\n"
        "const evidence = JSON.stringify({ releasePath: process.cwd(), dataPath, argv: process.argv.slice(1), sentinel });\n"
        "createServer((request, response) => {\n"
        "  if (request.url !== '/healthz' && request.url !== '/api/health') { response.statusCode = 404; response.end(); return; }\n"
        "  response.setHeader('Content-Type', 'application/json');\n"
        "  response.end(evidence);\n"
        "}).listen(port, '127.0.0.1');\n",
        encoding="utf-8",
    )
    gitignore = app_repository / ".gitignore"
    gitignore.write_text(
        gitignore.read_text(encoding="utf-8") + "data/\n",
        encoding="utf-8",
    )
    _run_git(
        app_repository,
        "add",
        "local-web.json",
        "server/service.mjs",
        ".gitignore",
    )
    _run_git(app_repository, "commit", "-m", "feat: convert fixture to service")


def execute_disposable_transition(
    app_repository: Path, temporary_root: Path
) -> tuple[str, ...]:
    """Exercise the real transition lifecycle against disposable host state."""

    app_repository = Path(app_repository).resolve(strict=True)
    temporary_root = Path(temporary_root).resolve(strict=True)
    platform = temporary_root / "disposable-platform"
    runtime = temporary_root / "activation-runtime"
    registry_path = platform / "config/local/apps.json"
    data_path = app_repository / "data/state.json"
    services = _DisposableLaunchAgentController(runtime)
    failure_gate = [0]

    def initialise_platform(index: _DisposableHttpServer) -> None:
        content = (
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
        if (
            initialise_disposable_host_profile(platform, content)
            != registry_path
        ):
            raise RuntimeError("disposable private profile contract failed")

    try:
        with _DisposableHttpServer(runtime) as index:
            _phase("activate static", lambda: initialise_platform(index))
            installer = _DisposableTransitionInstaller(registry_path, services)

            def activator() -> AppActivator:
                def deployer(registry):
                    return DeploymentManager(
                        registry,
                        services=services,
                        health=_DisposableTransitionHealthChecker(
                            registry, failure_gate
                        ),
                        launch_agents=temporary_root / "LaunchAgents",
                        environment={
                            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
                        },
                    )

                return AppActivator(
                    platform_repository=platform,
                    registry_path=registry_path,
                    registrar=AppRegistrar(),
                    profile_store=HostProfileStore(
                        HostProfilePaths.for_repository(platform)
                    ),
                    install=installer,
                    deployer_factory=deployer,
                )

            def activate_static() -> None:
                data_path.parent.mkdir()
                data_path.write_bytes(DATA_SENTINEL)
                result = activator().activate(app_repository)
                layout = RuntimeLayout(runtime, app_repository.name)
                current = layout.current.resolve(strict=True)
                if (
                    result.plan.kind != "static"
                    or result.registry_revision is None
                    or not result.verified
                    or not (current / "index.html").is_file()
                    or (current / "server/service.mjs").exists()
                ):
                    raise RuntimeError("disposable transition contract failed")

            _phase("activate static", activate_static)
            _phase(
                "commit service manifest",
                lambda: _write_service_commit(app_repository),
            )

            registry_before = registry_path.read_bytes()
            head_before = _run_git(platform, "rev-parse", "HEAD")
            runtime_before = _snapshot_disposable_tree(runtime)

            def preview_transition() -> None:
                plan = activator().preview(app_repository)
                if (
                    plan.kind != "service"
                    or not plan.registry_changed
                    or registry_path.read_bytes() != registry_before
                    or _run_git(platform, "rev-parse", "HEAD") != head_before
                    or _snapshot_disposable_tree(runtime) != runtime_before
                ):
                    raise RuntimeError("disposable transition contract failed")

            _phase("preview transition without writes", preview_transition)

            applied = _phase(
                "apply transition", lambda: activator().activate(app_repository)
            )
            if (
                applied.plan.kind != "service"
                or applied.registry_revision is None
                or not applied.verified
            ):
                raise TransitionAcceptanceError("apply transition")

            registry = _phase(
                "inspect expanded private argv", lambda: load_registry(registry_path)
            )
            host = next(
                (app for app in registry.apps if app.id == app_repository.name), None
            )
            command = (
                ()
                if host is None or host.start_command is None
                else host.start_command.argv
            )
            expected_release = (
                runtime.resolve() / "apps" / app_repository.name / "current"
            )
            expected_data = app_repository.resolve() / "data"
            if host is None or host.port is None:
                raise TransitionAcceptanceError("inspect expanded private argv")
            expected_command = (
                "/usr/bin/env",
                "node",
                str(expected_release / "server/service.mjs"),
                "--port",
                str(host.port),
                "--data-dir",
                str(expected_data),
            )
            service_release = expected_release.resolve(strict=True)
            if (
                command != expected_command
                or not services.launches
                or any(release != service_release for release in services.launches)
                or any(launch != expected_command for launch in services.commands)
                or services.module_not_found
                or services.state(f"com.sean.local-web.{host.id}")
                is not ServiceState.RUNNING
            ):
                raise TransitionAcceptanceError("inspect expanded private argv")

            evidence = _phase(
                "inspect expanded private argv",
                lambda: _read_service_evidence(
                    services.process,
                    f"http://127.0.0.1:{host.port}/healthz",
                ),
            )
            expected_evidence = {
                "releasePath": str(service_release),
                "dataPath": str(expected_data.resolve(strict=True)),
                "argv": list(expected_command[2:]),
                "sentinel": json.loads(DATA_SENTINEL),
            }
            proxy = _DisposableTransitionHealthChecker(registry, failure_gate).check(
                f"http://{registry.host}{load_manifest(app_repository / 'local-web.json').route}/api/health"
            )
            if evidence != expected_evidence or not proxy.healthy:
                raise TransitionAcceptanceError("inspect expanded private argv")

            transition_head = _run_git(platform, "rev-parse", "HEAD")
            retried = _phase(
                "retry activation without a second registry revision",
                lambda: activator().activate(app_repository),
            )
            if (
                retried.registry_revision is not None
                or not retried.verified
                or retried.plan.port != host.port
                or _run_git(platform, "rev-parse", "HEAD") != transition_head
            ):
                raise TransitionAcceptanceError(
                    "retry activation without a second registry revision"
                )

            stable_release = expected_release.resolve(strict=True)
            server = app_repository / "server/service.mjs"
            server.write_text(
                server.read_text(encoding="utf-8") + "// controlled redeployment\n",
                encoding="utf-8",
            )
            _run_git(app_repository, "add", "server/service.mjs")
            _run_git(app_repository, "commit", "-m", "test: controlled redeployment")
            failure_gate[0] = 100
            try:
                activator().activate(app_repository)
            except AppActivationError as error:
                remaining_failures = failure_gate[0]
                failure_gate[0] = 0
                if (
                    str(error) != "application deployment failed"
                    or remaining_failures >= 100
                ):
                    raise TransitionAcceptanceError(
                        "recover failed service redeployment"
                    ) from error
            else:
                raise TransitionAcceptanceError(
                    "recover failed service redeployment"
                )
            if (
                expected_release.resolve(strict=True) != stable_release
                or services.state(f"com.sean.local-web.{host.id}")
                is not ServiceState.RUNNING
                or _run_git(platform, "rev-parse", "HEAD") != transition_head
            ):
                raise TransitionAcceptanceError(
                    "recover failed service redeployment"
                )

            recovered = _phase(
                "retry recovered service deployment",
                lambda: activator().activate(app_repository),
            )
            if (
                recovered.registry_revision is not None
                or not recovered.verified
                or recovered.plan.port != host.port
                or expected_release.resolve(strict=True) == stable_release
                or _run_git(platform, "rev-parse", "HEAD") != transition_head
                or services.module_not_found
            ):
                raise TransitionAcceptanceError(
                    "retry recovered service deployment"
                )

            try:
                sentinel_unchanged = data_path.read_bytes() == DATA_SENTINEL
            except OSError as error:
                raise TransitionAcceptanceError(
                    "verify repository data sentinel", error
                ) from error
            if not sentinel_unchanged:
                raise TransitionAcceptanceError("verify repository data sentinel")
    finally:
        services.close()
        if (
            services.last_process_id is not None
            and _process_group_exists(services.last_process_id)
        ):
            raise TransitionAcceptanceError("cleanup")

    return ACTIVATION_PASS_LABELS


class RepositoryServiceTransitionVerifier:
    def __init__(
        self,
        *,
        platform_repository: Path = PLATFORM_REPOSITORY,
        coding_root: Path = CODING_ROOT,
        live_registry: Path | None = None,
        execute: CommandExecutor = create_eligible_static_fixture,
        activate: ActivationExecutor = execute_disposable_transition,
        emit: Emitter = print,
    ) -> None:
        self._platform_repository = Path(platform_repository).resolve()
        self._coding_root = Path(coding_root).resolve()
        self._live_registry = None if live_registry is None else Path(live_registry)
        self._execute = execute
        self._activate = activate
        self._emit = emit

    def _fixture_command(
        self, temporary_root: Path, repository: Path
    ) -> FixtureCreationCommand:
        return FixtureCreationCommand(
            "create eligible static fixture",
            self._platform_repository,
            temporary_root / "disposable-platform",
            repository,
        )

    def run(self) -> int:
        temporary: tempfile.TemporaryDirectory[str] | None = None
        result = 0
        live_content: bytes | None = None
        live_mode: int | None = None
        live_registry = self._live_registry
        owns_registry = live_registry is None
        try:
            temporary = tempfile.TemporaryDirectory(
                prefix=".local-web-repository-service-transition-",
                dir=self._coding_root,
            )
            temporary_root = Path(temporary.name)
            if live_registry is None:
                guard_platform = temporary_root / "guard-platform"
                guard_platform.mkdir()
                (guard_platform / "config").mkdir()
                (guard_platform / "local_web_server").mkdir()
                (guard_platform / "local_web_server/source.py").write_text(
                    "# disposable guard\n", encoding="utf-8"
                )
                (guard_platform / ".gitignore").write_text(
                    "config/local/\n", encoding="utf-8"
                )
                _run_git(guard_platform, "init", "-b", "main")
                _run_git(guard_platform, "add", ".")
                _run_git(
                    guard_platform,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgSign=false",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@invalid",
                    "commit",
                    "-m",
                    "fixture",
                )
                guard_content = (
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "host": "transition-guard.invalid",
                            "runtimeRoot": str(temporary_root / "guard-runtime"),
                            "apps": [],
                        }
                    )
                    + "\n"
                ).encode("utf-8")
                live_registry = initialise_disposable_host_profile(
                    guard_platform.resolve(), guard_content
                )
            live_content = live_registry.read_bytes()
            live_mode = stat.S_IMODE(live_registry.stat().st_mode)
            app_repository = temporary_root / "repository-service-transition"
            app_repository.mkdir()

            try:
                self._execute(self._fixture_command(temporary_root, app_repository))
                if not _registry_unchanged(
                    live_registry, live_content, live_mode
                ):
                    raise RuntimeError
            except BaseException:
                self._emit("create eligible static fixture FAIL")
                result = 1
            else:
                self._emit("create eligible static fixture PASS")

            if result == 0:
                try:
                    labels = self._activate(app_repository, temporary_root)
                    if labels != ACTIVATION_PASS_LABELS or not _registry_unchanged(
                        live_registry, live_content, live_mode
                    ):
                        raise TransitionAcceptanceError(
                            "repository service transition"
                        )
                except TransitionAcceptanceError as error:
                    self._emit(f"{error.label} FAIL")
                    result = 1
                except BaseException:
                    self._emit("repository service transition FAIL")
                    result = 1
                else:
                    for label in labels:
                        self._emit(f"{label} PASS")
        except BaseException:
            self._emit("live registry guard FAIL")
            result = 1
        finally:
            cleanup_ok = True
            registry_ok = True
            if (
                owns_registry
                and live_content is not None
                and live_mode is not None
            ):
                registry_ok = _registry_unchanged(
                    live_registry, live_content, live_mode
                )
            if temporary is not None:
                try:
                    temporary.cleanup()
                    cleanup_ok = not Path(temporary.name).exists()
                except BaseException:
                    cleanup_ok = False
            if (
                not owns_registry
                and live_content is not None
                and live_mode is not None
            ):
                registry_ok = _registry_unchanged(
                    live_registry, live_content, live_mode
                )
            cleanup_ok = cleanup_ok and registry_ok
            self._emit(f"cleanup {'PASS' if cleanup_ok else 'FAIL'}")
            if not cleanup_ok:
                result = 1
        return result


def main() -> int:
    return RepositoryServiceTransitionVerifier().run()


if __name__ == "__main__":
    raise SystemExit(main())
