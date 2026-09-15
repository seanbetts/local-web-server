#!/usr/bin/env python3
"""Verify service-command migration against disposable platform state."""

# ruff: noqa: E402

from __future__ import annotations

import io
import json
import os
import plistlib
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PLATFORM_REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_REPOSITORY))

from local_web_server import cli
from local_web_server.ui_package import build_ui_package
from local_web_server.app_activation import AppActivator, verify_served_index_tile
from local_web_server.app_registration import AppRegistrar
from local_web_server.config import ConfigError, load_manifest, load_registry
from local_web_server.deploy import DeploymentManager
from local_web_server.health import frontend_health_path
from local_web_server.index_registry import INDEX_REGISTRY_NAME, INDEX_REGISTRY_ROUTE
from local_web_server.install import Installer, load_main_manifests
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import (
    SERVICE_COMMAND_RESTORATION,
    HostProfileStore,
)
from local_web_server.runtime import RuntimeLayout, read_release_commit
from local_web_server.service_command_migration import ServiceCommandMigrator
from local_web_server.service_ports import ServicePortAllocator
from local_web_server.services import HttpHealthChecker, ServiceState
from scripts.verify_new_app_workflow import (
    WorkflowCommand,
)
from scripts.disposable_workflow_support import (
    initialise_disposable_host_profile,
    sanitized_subprocess_environment,
)


from scripts.migration_fixture import (
    generate_service_repository,
    inherit_worker_session,
    private_environment as _private_execution_environment,
    process_exists as _process_exists,
    run_bounded as _run_bounded_process,
    stop_process as _stop_process,
)


def _owned_environment() -> dict[str, str]:
    return sanitized_subprocess_environment()


def _execute_workflow_command(command: WorkflowCommand) -> None:
    result = _run_bounded_process(
        command.argv, cwd=command.cwd, env=_owned_environment(), timeout=command.timeout
    )
    if result.returncode:
        raise RuntimeError(f"disposable command failed: {command.label}")


def _read_git(repository: Path, *arguments: str) -> str:
    result = _run_bounded_process(
        ("/usr/bin/git", "-C", str(repository), *arguments),
        cwd=repository,
        env=_owned_environment(),
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("disposable Git query failed")
    return result.stdout.strip()


CODING_ROOT = PLATFORM_REPOSITORY.parent

PHASES = (
    "create healthy registered service with old command",
    "commit manifest-derived command change",
    "preview migration without writes",
    "apply migration after release selection",
    "verify fixed port and all health surfaces",
    "verify external repository data is untouched",
    "repeat apply as no-op",
    "restore old command and service after injected failure",
    "retry migration successfully",
    "cleanup",
)


_MAX_CLI_OUTPUT = 8192
_MAX_HTTP_BODY = 8192
_HTTP_READY_TIMEOUT = 5.0
_DATA_SENTINEL = b'{"state":"external-and-immutable"}\n'
_APP_ID = "service-command-fixture"
_ROUTE = f"/{_APP_ID}"


class _BoundedTextSink(io.StringIO):
    def write(self, text: str) -> int:
        if self.tell() + len(text) > _MAX_CLI_OUTPUT:
            raise RuntimeError("disposable CLI output exceeded its bound")
        return super().write(text)


def _invoke_migration_cli(
    repository: Path,
    migrator: Any,
    *,
    apply: bool,
) -> dict[str, object]:
    """Drive the public parser and renderer with a disposable migrator instance."""

    stdout = _BoundedTextSink()
    stderr = _BoundedTextSink()
    try:
        registry_path = Path(migrator.registry_path)
        registry = load_registry(registry_path)
    except (AttributeError, OSError, ValueError, ConfigError) as error:
        raise RuntimeError("disposable migration CLI failed") from error
    original = cli.ServiceCommandMigrator
    original_loader = cli._load_default_host_profile

    def migrator_factory(*, registry_path: Path):
        if Path(registry_path) != Path(migrator.registry_path):
            raise RuntimeError("disposable migration CLI failed")
        return migrator

    cli.ServiceCommandMigrator = migrator_factory
    cli._load_default_host_profile = lambda: (registry_path, registry)
    arguments = [
        "app",
        "migrate-service-command",
        "--repository",
        str(repository),
        "--json",
    ]
    if apply:
        arguments.append("--apply")
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli.main(arguments)
    finally:
        cli.ServiceCommandMigrator = original
        cli._load_default_host_profile = original_loader
    if result != 0 or stderr.getvalue():
        raise RuntimeError("disposable migration CLI failed")
    try:
        payload = json.loads(stdout.getvalue())
    except (json.JSONDecodeError, UnicodeError) as error:
        raise RuntimeError("disposable migration CLI output was invalid") from error
    if not isinstance(payload, dict):
        raise RuntimeError("disposable migration CLI output was invalid")
    return payload


def _run_git(repository: Path, *arguments: str) -> None:
    _execute_workflow_command(
        WorkflowCommand(
            "disposable git operation",
            ("/usr/bin/git", "-C", str(repository), *arguments),
            repository,
            60,
        )
    )


def _create_disposable_guard_profile(root: Path) -> Path:
    platform = Path(root) / "host-profile-guard"
    platform.mkdir()
    (platform / "config").mkdir()
    (platform / "local_web_server").mkdir()
    (platform / "local_web_server/source.py").write_text(
        "# disposable guard\n", encoding="utf-8"
    )
    (platform / ".gitignore").write_text("config/local/\n", encoding="utf-8")
    _run_git(platform, "init", "-b", "main")
    _run_git(platform, "add", ".")
    _run_git(
        platform,
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
    content = (
        json.dumps(
            {
                "schemaVersion": 1,
                "host": "service-command-guard.invalid",
                "runtimeRoot": str(Path(root) / "guard-runtime"),
                "apps": [],
            }
        )
        + "\n"
    ).encode("utf-8")
    return initialise_disposable_host_profile(platform.resolve(), content)


def _allocate_fixture_port() -> int:
    candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    finally:
        candidate.close()
    if type(port) is not int or not 1024 <= port <= 65535:
        raise RuntimeError("disposable service port was invalid")
    return port


def _read_json(url: str) -> dict[str, object]:
    deadline = time.monotonic() + _HTTP_READY_TIMEOUT
    while True:
        try:
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/json"},
                method="GET",
            )
            with urllib.request.urlopen(request, timeout=0.5) as response:
                if response.status != 200:
                    raise RuntimeError("disposable HTTP evidence was unavailable")
                content = response.read(_MAX_HTTP_BODY + 1)
            if len(content) > _MAX_HTTP_BODY:
                raise RuntimeError("disposable HTTP evidence exceeded its bound")
            payload = json.loads(content.decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError("disposable HTTP evidence was invalid")
            return payload
        except (ConnectionError, TimeoutError, urllib.error.URLError):
            if time.monotonic() >= deadline:
                raise RuntimeError("disposable HTTP endpoint did not become ready")
            time.sleep(0.02)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise RuntimeError("disposable HTTP evidence was invalid") from error


def _phase_from_command(command: tuple[str, ...]) -> str:
    try:
        index = command.index("--phase")
        phase = command[index + 1]
    except (ValueError, IndexError):
        return "old"
    return phase if phase in {"candidate", "retry"} else "invalid"


@dataclass(frozen=True)
class _LaunchRecord:
    command: tuple[str, ...]
    release: Path
    release_ready: bool
    phase: str
    process_group: int


@dataclass(frozen=True)
class _LaunchSpec:
    command: tuple[str, ...]
    working_directory: Path


class _DisposableServiceController:
    """Run one fixture job across real process-group stop/start boundaries."""

    def __init__(self, runtime_root: Path):
        self.runtime_root = Path(runtime_root)
        self.host = None
        self.process: subprocess.Popen[bytes] | None = None
        self.loaded = False
        self.records: list[_LaunchRecord] = []
        self.errors: list[bytes] = []
        self.process_groups: list[int] = []

    @property
    def phase(self) -> str | None:
        return self.records[-1].phase if self.records else None

    def configure(self, registry) -> None:
        services = [app for app in registry.apps if app.start_command is not None]
        if len(services) != 1 or services[0].id != _APP_ID:
            raise RuntimeError("disposable service registry was invalid")
        self.host = services[0]

    def _require_label(self, label: str) -> None:
        if self.host is None or label != f"com.sean.local-web.{self.host.id}":
            raise RuntimeError("disposable service label was invalid")

    def _capture_error(self, process: subprocess.Popen[bytes]) -> None:
        if process.stderr is None:
            return
        try:
            content = process.stderr.read(_MAX_HTTP_BODY + 1)
        finally:
            process.stderr.close()
        if len(content) > _MAX_HTTP_BODY:
            raise RuntimeError("disposable service output exceeded its bound")
        if content:
            self.errors.append(content)

    def _observe_exit(self) -> None:
        process = self.process
        if process is None or process.poll() is None:
            return
        try:
            process.wait()
            self._capture_error(process)
        finally:
            self.process = None

    def _stop_process(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                _stop_process(process)
            else:
                process.wait()
            self._capture_error(process)
        finally:
            self.process = None

    def _load_launch_spec(self, label: str, plist_path: Path) -> _LaunchSpec:
        if self.host is None or self.host.start_command is None:
            raise RuntimeError("disposable service command was unavailable")
        try:
            payload = plistlib.loads(Path(plist_path).read_bytes())
        except (OSError, plistlib.InvalidFileException) as error:
            raise RuntimeError("disposable service plist was unavailable") from error
        layout = RuntimeLayout(self.runtime_root, self.host.id)
        expected_directory = layout.current
        arguments = (
            payload.get("ProgramArguments") if isinstance(payload, dict) else None
        )
        working_directory = (
            payload.get("WorkingDirectory") if isinstance(payload, dict) else None
        )
        if (
            payload.get("Label") != label
            or not isinstance(arguments, list)
            or not arguments
            or any(not isinstance(value, str) for value in arguments)
            or tuple(arguments) != self.host.start_command.argv
            or not isinstance(working_directory, str)
            or Path(working_directory) != expected_directory
        ):
            raise RuntimeError("disposable service plist contract was invalid")
        return _LaunchSpec(tuple(arguments), Path(working_directory))

    def _launch(self, spec: _LaunchSpec) -> None:
        release = spec.working_directory.resolve(strict=True)
        command = spec.command
        release_ready = (
            release.is_dir()
            and (release / "server/service.mjs").is_file()
            and (release / "public/index.html").is_file()
        )
        if not release_ready:
            raise RuntimeError("disposable release was not selected before launch")
        process = subprocess.Popen(
            command,
            cwd=spec.working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=_owned_environment(),
            close_fds=True,
            start_new_session=False,
        )
        self.process = process

        self.process_groups.append(process.pid)
        self.records.append(
            _LaunchRecord(
                command,
                release,
                release_ready,
                _phase_from_command(command),
                process.pid,
            )
        )
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            return
        self._observe_exit()

    def state(self, label: str) -> ServiceState:
        self._require_label(label)
        if not self.loaded:
            return ServiceState.MISSING
        self._observe_exit()
        return (
            ServiceState.RUNNING if self.process is not None else ServiceState.STOPPED
        )

    def ensure_running(self, label: str, plist_path: Path) -> None:
        self._require_label(label)
        spec = self._load_launch_spec(label, plist_path)
        if self.state(label) is ServiceState.RUNNING:
            return
        self.loaded = True
        self._launch(spec)

    def restart(self, label: str, plist_path: Path) -> None:
        self._require_label(label)
        spec = self._load_launch_spec(label, plist_path)
        self._stop_process()
        self.loaded = True
        self._launch(spec)

    def stop(self, label: str, _plist_path: Path) -> None:
        self._require_label(label)
        self._stop_process()
        self.loaded = False

    def replace(self, label: str, plist_path: Path) -> None:
        self._require_label(label)
        spec = self._load_launch_spec(label, plist_path)
        self.stop(label, plist_path)
        self.loaded = True
        self._launch(spec)

    def close(self) -> None:
        self._stop_process()
        self.loaded = False


class _DisposableInstaller:
    """Run the real installer with every host-owned target redirected privately."""

    def __init__(
        self,
        registry_path: Path,
        platform_repository: Path,
        home: Path,
        services: _DisposableServiceController,
    ) -> None:
        self.registry_path = Path(registry_path)
        self.platform_repository = Path(platform_repository)
        self.home = Path(home)
        self.services = services

    def __call__(
        self,
        _repository: Path,
        *,
        dry_run: bool,
        recover_missing_caddy: bool = False,
    ) -> object:
        if dry_run:
            raise RuntimeError("disposable installer cannot mutate in preview")
        registry = load_registry(self.registry_path)
        manifests = load_main_manifests(registry)
        Installer(
            registry,
            manifests,
            repository=self.platform_repository,
            home=self.home,
            run=_DisposableInstallRunner(),
            theme_gate=_DisposableThemeGate(),
        ).install(recover_missing_caddy=recover_missing_caddy)
        self.services.configure(registry)
        return object()


class _DisposableInstallRunner:
    def __call__(self, argv, *, env=None, timeout=None):
        command = list(argv)
        if command == ["pmset", "-g", "custom"]:
            return subprocess.CompletedProcess(
                command,
                0,
                "Battery Power:\n sleep 5\nAC Power:\n sleep 0\n",
                "",
            )
        if command[:2] == ["launchctl", "print"]:
            missing = (
                'Could not find service "com.sean.local-web.caddy" '
                f"in domain for user gui: {os.getuid()}\n"
            )
            return subprocess.CompletedProcess(command, 3, "", missing)
        if command[:2] == ["/opt/homebrew/bin/caddy", "validate"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise RuntimeError("disposable installer attempted an unexpected command")


class _DisposableThemeGate:
    def verify(self, candidate: Path) -> None:
        if not Path(candidate).is_file():
            raise RuntimeError("disposable theme candidate was unavailable")


class _DisposablePublicServer:
    """Serve the public tile and proxy fixture health without Caddy."""

    def __init__(
        self,
        runtime_root: Path,
        services: _DisposableServiceController,
        *,
        shutdown_timeout: float = 2.0,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.services = services
        self.shutdown_timeout = shutdown_timeout
        self.failure_phase: str | None = None
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _DisposablePublicRequestHandler
        )
        self.server.runtime_root = self.runtime_root
        self.server.services = services
        self.server.failure_source = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._started = False
        self._closed = False

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def start(self) -> None:
        if self._closed or self._started:
            raise RuntimeError("disposable public HTTP lifecycle was invalid")
        self._started = True
        self.thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[str] = []
        if self._started:
            shutdown = threading.Thread(target=self.server.shutdown, daemon=True)
            shutdown.start()
            shutdown.join(timeout=self.shutdown_timeout)
            if shutdown.is_alive():
                failures.append("shutdown")
        closer = threading.Thread(target=self.server.server_close, daemon=True)
        closer.start()
        closer.join(timeout=self.shutdown_timeout)
        if closer.is_alive():
            failures.append("socket")
        if self._started:
            self.thread.join(timeout=self.shutdown_timeout)
            if self.thread.is_alive():
                failures.append("thread")
        if failures:
            raise RuntimeError("disposable public HTTP cleanup failed")


class _DisposablePublicRequestHandler(BaseHTTPRequestHandler):
    def do_HEAD(self) -> None:
        self._respond(body=False)

    def do_GET(self) -> None:
        self._respond(body=True)

    def _respond(self, *, body: bool) -> None:
        runtime = self.server.runtime_root
        services = self.server.services
        if self.path == INDEX_REGISTRY_ROUTE:
            try:
                content = (
                    runtime / "platform/index" / INDEX_REGISTRY_NAME
                ).read_bytes()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            if len(content) > _MAX_HTTP_BODY:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content) if body else 0))
            self.end_headers()
            if body:
                self.wfile.write(content)
            return
        if self.path == f"{_ROUTE}/":
            try:
                current = RuntimeLayout(runtime, _APP_ID).current.resolve(strict=True)
                healthy = (current / "public/index.html").is_file()
            except (OSError, RuntimeError):
                healthy = False
            self.send_response(204 if healthy else 404)
            self.end_headers()
            return
        if self.path not in {f"{_ROUTE}/healthz", f"{_ROUTE}/api/health"}:
            self.send_response(404)
            self.end_headers()
            return
        failure_source = self.server.failure_source
        if (
            self.path == f"{_ROUTE}/healthz"
            and failure_source.failure_phase is not None
            and services.phase == failure_source.failure_phase
        ):
            self.send_response(503)
            self.end_headers()
            return
        if services.host is None or services.host.port is None:
            self.send_response(503)
            self.end_headers()
            return
        internal_path = "/healthz" if self.path.endswith("/healthz") else "/api/health"
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{services.host.port}{internal_path}",
                method="GET" if body else "HEAD",
            )
            with urllib.request.urlopen(request, timeout=0.5) as response:
                content = response.read(_MAX_HTTP_BODY + 1) if body else b""
                status = response.status
                content_type = response.headers.get("Content-Type")
            if len(content) > _MAX_HTTP_BODY:
                raise RuntimeError
        except urllib.error.HTTPError as error:
            status = error.code
            content = b""
            content_type = None
            error.close()
        except (OSError, RuntimeError, ValueError, urllib.error.URLError):
            status = 503
            content = b""
            content_type = None
        self.send_response(status)
        if content_type is not None:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if body and content:
            self.wfile.write(content)

    def log_message(self, _format: str, *args: object) -> None:
        return


def _fixture_service_source() -> str:
    return (
        "import { access, readFile, realpath } from 'node:fs/promises';\n"
        "import { createServer } from 'node:http';\n"
        "import { dirname, resolve } from 'node:path';\n"
        "import { fileURLToPath } from 'node:url';\n"
        "const args = process.argv.slice(2);\n"
        "const value = (flag) => { const index = args.indexOf(flag); return index < 0 ? null : args[index + 1]; };\n"
        "const port = Number(value('--port'));\n"
        "const phase = value('--phase') ?? 'old';\n"
        "const dataPath = value('--data-dir');\n"
        "if (!Number.isInteger(port) || port < 1 || port > 65535 || !['old', 'candidate', 'retry'].includes(phase)) process.exit(64);\n"
        "const script = await realpath(fileURLToPath(import.meta.url));\n"
        "const release = dirname(dirname(script));\n"
        "await access(resolve(release, 'public/index.html'));\n"
        "let dataReady = dataPath === null;\n"
        "if (dataPath !== null) { const data = JSON.parse(await readFile(resolve(dataPath, 'state.json'), 'utf8')); dataReady = data.state === 'external-and-immutable'; }\n"
        "const evidence = JSON.stringify({ phase, releaseReady: true, dataReady });\n"
        "createServer((request, response) => {\n"
        "  if (request.url !== '/healthz' && request.url !== '/api/health') { response.writeHead(404).end(); return; }\n"
        "  response.setHeader('Content-Type', 'application/json');\n"
        "  response.setHeader('Content-Length', String(Buffer.byteLength(evidence)));\n"
        "  response.writeHead(200);\n"
        "  response.end(request.method === 'HEAD' ? undefined : evidence);\n"
        "}).listen(port, '127.0.0.1');\n"
    )


class DisposableServiceCommandMigrationWorkflow:
    """Drive the real migration transaction only inside one disposable host."""

    def __init__(self, root: Path, platform_repository: Path):
        self.root = Path(root).resolve(strict=True)
        self.source_platform = Path(platform_repository).resolve(strict=True)
        self.platform = self.root / "disposable-platform"
        self.repository = self.root / _APP_ID
        self.runtime = self.root / "runtime"
        self.home = self.root / "home"
        self.launch_agents = self.home / "Library/LaunchAgents"
        self.registry_path = self.platform / "config/local/apps.json"
        self.data_path = self.repository / "repository-data/state.json"
        self.services = _DisposableServiceController(self.runtime)
        self.public = _DisposablePublicServer(self.runtime, self.services)
        self.installer = _DisposableInstaller(
            self.registry_path,
            self.platform,
            self.home,
            self.services,
        )
        self.port: int | None = None
        self.data_snapshot: tuple[object, ...] | None = None
        self.old_commit: str | None = None
        self.candidate_commit: str | None = None
        self.retry_commit: str | None = None
        self.candidate_registry: bytes | None = None
        self.candidate_plist: bytes | None = None
        self._phase_index = 0
        self._public_started = False
        self._closed = False

    @property
    def layout(self) -> RuntimeLayout:
        return RuntimeLayout(self.runtime, _APP_ID)

    @property
    def plist(self) -> Path:
        return self.launch_agents / f"com.sean.local-web.{_APP_ID}.plist"

    def run_phase(self, phase: str) -> None:
        if self._closed or self._phase_index >= len(PHASES) - 1:
            raise RuntimeError("disposable workflow phase was invalid")
        if phase != PHASES[self._phase_index]:
            raise RuntimeError("disposable workflow phase order was invalid")
        operations = (
            self._create_old_service,
            self._commit_candidate_command,
            self._preview_migration,
            self._apply_migration,
            self._verify_health_surfaces,
            self._verify_repository_data,
            self._verify_noop_apply,
            self._verify_failed_migration_recovery,
            self._retry_migration,
        )
        operations[self._phase_index]()
        self._phase_index += 1

    def _clone_platform_and_generate_app(self) -> None:
        _execute_workflow_command(
            WorkflowCommand(
                "clone disposable platform",
                (
                    "/usr/bin/git",
                    "clone",
                    "--quiet",
                    "--no-local",
                    "--single-branch",
                    str(self.source_platform),
                    str(self.platform),
                ),
                self.root,
                60,
            )
        )
        _run_git(self.platform, "switch", "-C", "main")
        exclude = self.platform / ".git/info/exclude"
        exclude.write_text(
            exclude.read_text(encoding="utf-8") + "\nconfig/local/\n",
            encoding="utf-8",
        )
        _run_git(self.platform, "config", "user.name", "Disposable Migration")
        _run_git(
            self.platform,
            "config",
            "user.email",
            "service-command-migration@localhost",
        )
        self.repository.mkdir()
        _run_git(self.repository, "init", "--initial-branch=main")
        _run_git(self.repository, "config", "user.name", "Disposable Migration")
        _run_git(self.repository, "config", "user.email", "fixture@localhost")
        generate_service_repository(
            self.repository,
            self.platform,
            self.root / "local-web-ui.tgz",
            app_id=_APP_ID,
            route=_ROUTE,
        )
        _run_git(self.repository, "add", "--all")
        _run_git(self.repository, "commit", "-m", "Create fixture")

    def _prepare_old_service_commit(self) -> None:
        manifest_path = self.repository / "local-web.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["build"] = {
            "commands": [["/usr/bin/true"]],
            "output": "fixture-release",
            "environment": ["VITE_PUBLIC_BASE_PATH"],
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        server = self.repository / "server/service.mjs"
        service_source = _fixture_service_source()
        server.write_text(service_source, encoding="utf-8")
        release = self.repository / "fixture-release"
        (release / "public").mkdir(parents=True)
        (release / "server").mkdir()
        (release / "public/index.html").write_text(
            "<!doctype html><title>Disposable service</title>\n",
            encoding="utf-8",
        )
        (release / "server/service.mjs").write_text(service_source, encoding="utf-8")
        gitignore = self.repository / ".gitignore"
        ignored = gitignore.read_text(encoding="utf-8")
        if not ignored.endswith("\n"):
            ignored += "\n"
        gitignore.write_text(ignored + "repository-data/\n", encoding="utf-8")
        _run_git(
            self.repository,
            "add",
            "local-web.json",
            "server/service.mjs",
            "fixture-release",
            ".gitignore",
        )
        _run_git(
            self.repository,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-m",
            "test: prepare disposable service command fixture",
        )
        self.data_path.parent.mkdir()
        self.data_path.write_bytes(_DATA_SENTINEL)
        self.data_snapshot = self._data_state()

    def _initialise_platform_registry(self) -> None:
        index_assets = self.platform / "apps/system-index/dist/assets"
        index_assets.mkdir(parents=True)
        (index_assets.parent / "index.html").write_text(
            "<!doctype html><script src='/assets/index.js'></script>\n",
            encoding="utf-8",
        )
        (index_assets / "index.js").write_text(
            "console.log('disposable index');\n", encoding="utf-8"
        )
        gallery_assets = self.platform / "examples/ui-gallery/dist/assets"
        gallery_assets.mkdir(parents=True)
        (gallery_assets.parent / "index.html").write_text(
            "<!doctype html><script src='/assets/gallery.js'></script>\n",
            encoding="utf-8",
        )
        (gallery_assets / "gallery.js").write_text(
            "console.log('disposable gallery');\n", encoding="utf-8"
        )
        content = (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "host": self.public.host,
                    "runtimeRoot": str(self.runtime),
                    "apps": [],
                },
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        _run_git(
            self.platform,
            "add",
            "--force",
            "apps/system-index/dist",
            "examples/ui-gallery/dist",
        )
        _run_git(
            self.platform,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-m",
            "test: initialise disposable host registry",
        )
        if (
            initialise_disposable_host_profile(self.platform, content)
            != self.registry_path
        ):
            raise RuntimeError("disposable private profile was invalid")
        if _read_git(self.platform, "status", "--short"):
            raise RuntimeError("disposable platform was not clean")

    def _deployer(self, registry):
        return DeploymentManager(
            registry,
            services=self.services,
            health=HttpHealthChecker(),
            launch_agents=self.launch_agents,
        )

    def _activator(self) -> AppActivator:
        if self.port is None:
            raise RuntimeError("disposable service port was unavailable")
        return AppActivator(
            platform_repository=self.platform,
            registry_path=self.registry_path,
            registrar=AppRegistrar(),
            allocator=ServicePortAllocator(self.port, self.port),
            profile_store=HostProfileStore(
                HostProfilePaths.for_repository(self.platform)
            ),
            install=self.installer,
            deployer_factory=self._deployer,
        )

    def _migrator(self) -> ServiceCommandMigrator:
        return ServiceCommandMigrator(
            platform_repository=self.platform,
            registry_path=self.registry_path,
            registrar=AppRegistrar(),
            profile_store=HostProfileStore(
                HostProfilePaths.for_repository(self.platform)
            ),
            install=self.installer,
            deployer_factory=self._deployer,
            verify=verify_served_index_tile,
        )

    def _create_old_service(self) -> None:
        self._clone_platform_and_generate_app()
        self._prepare_old_service_commit()
        self.port = _allocate_fixture_port()
        self.public.start()
        self._public_started = True
        self._initialise_platform_registry()

        result = self._activator().activate(self.repository)
        registry = load_registry(self.registry_path)
        host = registry.apps[0]
        self.old_commit = _read_git(self.repository, "rev-parse", "refs/heads/main")
        if (
            result.registry_revision is None
            or not result.verified
            or host.id != _APP_ID
            or host.port != self.port
            or host.start_command is None
            or "--phase" in host.start_command.argv
            or read_release_commit(self.layout.current) != self.old_commit
            or read_release_commit(self.layout.previous) is not None
            or self.services.state(f"com.sean.local-web.{_APP_ID}")
            is not ServiceState.RUNNING
            or not self.plist.is_file()
            or _read_git(self.platform, "status", "--short")
            or _read_git(self.repository, "status", "--short")
        ):
            raise RuntimeError("disposable old service was invalid")
        evidence = self._service_evidence()
        if evidence != {"phase": "old", "releaseReady": True, "dataReady": True}:
            raise RuntimeError("disposable old service evidence was invalid")
        self._require_data_unchanged()

    def _commit_manifest_command(self, phase: str, message: str) -> str:
        manifest_path = self.repository / "local-web.json"
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["service"]["startCommand"] = [
            "/usr/bin/env",
            "node",
            "{release}/server/service.mjs",
            "--port",
            "{port}",
            "--data-dir",
            "{repository}/repository-data",
            "--phase",
            phase,
        ]
        manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        _run_git(self.repository, "add", "local-web.json")
        _run_git(
            self.repository,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-m",
            message,
        )
        if _read_git(self.repository, "status", "--short"):
            raise RuntimeError("disposable application was not clean")
        return _read_git(self.repository, "rev-parse", "refs/heads/main")

    def _commit_candidate_command(self) -> None:
        self.candidate_commit = self._commit_manifest_command(
            "candidate", "test: change disposable service command"
        )
        if self.candidate_commit == self.old_commit:
            raise RuntimeError("disposable target commit did not change")
        target = self.layout.releases / self.candidate_commit
        if target.exists():
            raise RuntimeError("disposable target release existed before migration")
        self._require_data_unchanged()

    @staticmethod
    def _require_plan(payload: dict[str, object], *, command: str, action: str) -> None:
        expected = {
            "application": _APP_ID,
            "serviceCommand": command,
            "identity": "unchanged",
            "repository": "unchanged",
            "route": "unchanged",
            "portStatus": "unchanged",
            "serviceAction": action,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise RuntimeError("disposable migration plan was invalid")
        port = payload.get("port")
        if type(port) is not int:
            raise RuntimeError("disposable migration port was invalid")

    def _preview_migration(self) -> None:
        before = (
            self.registry_path.read_bytes(),
            _read_git(self.platform, "rev-parse", "HEAD"),
            read_release_commit(self.layout.current),
            read_release_commit(self.layout.previous),
            self.plist.read_bytes(),
            self.services.process.pid if self.services.process is not None else None,
            len(self.services.records),
            tuple(sorted(path.name for path in self.layout.releases.iterdir())),
        )
        payload = _invoke_migration_cli(self.repository, self._migrator(), apply=False)
        self._require_plan(payload, command="change", action="redeploy and reload")
        after = (
            self.registry_path.read_bytes(),
            _read_git(self.platform, "rev-parse", "HEAD"),
            read_release_commit(self.layout.current),
            read_release_commit(self.layout.previous),
            self.plist.read_bytes(),
            self.services.process.pid if self.services.process is not None else None,
            len(self.services.records),
            tuple(sorted(path.name for path in self.layout.releases.iterdir())),
        )
        if before != after or (self.layout.releases / self.candidate_commit).exists():
            raise RuntimeError("disposable migration preview wrote state")
        self._require_data_unchanged()

    def _expected_command(self, phase: str) -> tuple[str, ...]:
        if self.port is None:
            raise RuntimeError("disposable service port was unavailable")
        return (
            "/usr/bin/env",
            "node",
            str(self.layout.current / "server/service.mjs"),
            "--port",
            str(self.port),
            "--data-dir",
            str(self.repository.resolve(strict=True) / "repository-data"),
            "--phase",
            phase,
        )

    def _apply_migration(self) -> None:
        if self.candidate_commit is None or self.old_commit is None:
            raise RuntimeError("disposable migration commits were unavailable")
        before_pid = (
            self.services.process.pid if self.services.process is not None else None
        )
        payload = _invoke_migration_cli(self.repository, self._migrator(), apply=True)
        self._require_plan(payload, command="change", action="redeploy and reload")
        registry = load_registry(self.registry_path)
        host = registry.apps[0]
        record = self.services.records[-1]
        current = self.layout.current.resolve(strict=True)
        if (
            payload.get("port") != self.port
            or host.port != self.port
            or host.start_command is None
            or host.start_command.argv != self._expected_command("candidate")
            or read_release_commit(self.layout.current) != self.candidate_commit
            or read_release_commit(self.layout.previous) != self.old_commit
            or record.command != self._expected_command("candidate")
            or record.release != current
            or record.release.name != self.candidate_commit
            or not record.release_ready
            or record.phase != "candidate"
            or self.services.process is None
            or self.services.process.pid == before_pid
            or _read_git(self.platform, "status", "--short")
        ):
            raise RuntimeError("disposable migration application was invalid")
        self.candidate_registry = self.registry_path.read_bytes()
        self.candidate_plist = self.plist.read_bytes()
        evidence = self._service_evidence()
        if evidence != {
            "phase": "candidate",
            "releaseReady": True,
            "dataReady": True,
        }:
            raise RuntimeError("disposable candidate service evidence was invalid")
        self._require_data_unchanged()

    def _service_evidence(self) -> dict[str, object]:
        if self.port is None:
            raise RuntimeError("disposable service port was unavailable")
        return _read_json(f"http://127.0.0.1:{self.port}/healthz")

    def _require_all_health(self, phase: str) -> None:
        if self.port is None:
            raise RuntimeError("disposable service port was unavailable")
        registry = load_registry(self.registry_path)
        manifest = load_manifest(self.repository / "local-web.json")
        health = HttpHealthChecker()
        checks = (
            health.check(
                f"http://127.0.0.1:{self.port}{manifest.service.internal_health_path}",
                host=registry.host,
            ),
            health.check(f"http://{registry.host}{frontend_health_path(manifest)}"),
            health.check(f"http://{registry.host}{manifest.health_path}"),
            health.check(f"http://{registry.host}{_ROUTE}/api/health"),
        )
        if any(
            not result.healthy or result.status not in {200, 204} for result in checks
        ):
            raise RuntimeError("disposable health surface was unavailable")
        verify_served_index_tile(self.repository, registry, _APP_ID)
        evidence = self._service_evidence()
        if evidence != {"phase": phase, "releaseReady": True, "dataReady": True}:
            raise RuntimeError("disposable service evidence was invalid")

    def _verify_health_surfaces(self) -> None:
        registry = load_registry(self.registry_path)
        host = registry.apps[0]
        if (
            host.port != self.port
            or host.start_command is None
            or host.start_command.argv != self._expected_command("candidate")
        ):
            raise RuntimeError("disposable fixed-port contract was invalid")
        self._require_all_health("candidate")

    def _data_state(self) -> tuple[object, ...]:
        metadata = self.data_path.lstat()
        return (
            metadata.st_dev,
            metadata.st_ino,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            self.data_path.read_bytes(),
        )

    def _require_data_unchanged(self) -> None:
        if self.data_snapshot is None or self._data_state() != self.data_snapshot:
            raise RuntimeError("repository data changed during disposable migration")
        resolved_data = self.data_path.resolve(strict=True)
        resolved_runtime = self.runtime.resolve()
        try:
            resolved_data.relative_to(resolved_runtime)
        except ValueError:
            pass
        else:
            raise RuntimeError("repository data was placed in the runtime root")
        for release in (
            self.layout.releases.glob("*") if self.layout.releases.exists() else ()
        ):
            if (release / "repository-data").exists():
                raise RuntimeError("repository data was copied into a release")

    def _verify_repository_data(self) -> None:
        self._require_data_unchanged()
        if self.services.records[-1].command != self._expected_command("candidate"):
            raise RuntimeError("repository data command expansion was invalid")

    def _pointer_pair(self) -> tuple[str | None, str | None]:
        return (
            read_release_commit(self.layout.current),
            read_release_commit(self.layout.previous),
        )

    def _verify_noop_apply(self) -> None:
        before = (
            self.registry_path.read_bytes(),
            _read_git(self.platform, "rev-parse", "HEAD"),
            self._pointer_pair(),
            self.plist.read_bytes(),
            self.services.process.pid if self.services.process is not None else None,
            len(self.services.records),
            tuple(sorted(path.name for path in self.layout.releases.iterdir())),
            self._data_state(),
        )
        payload = _invoke_migration_cli(self.repository, self._migrator(), apply=True)
        self._require_plan(payload, command="current", action="none")
        after = (
            self.registry_path.read_bytes(),
            _read_git(self.platform, "rev-parse", "HEAD"),
            self._pointer_pair(),
            self.plist.read_bytes(),
            self.services.process.pid if self.services.process is not None else None,
            len(self.services.records),
            tuple(sorted(path.name for path in self.layout.releases.iterdir())),
            self._data_state(),
        )
        if before != after:
            raise RuntimeError("disposable migration no-op changed state")

    def _verify_failed_migration_recovery(self) -> None:
        if self.candidate_registry is None or self.candidate_plist is None:
            raise RuntimeError("disposable candidate state was unavailable")
        self.retry_commit = self._commit_manifest_command(
            "retry", "test: prepare disposable failed migration"
        )
        former_registry = self.registry_path.read_bytes()
        former_pointers = self._pointer_pair()
        former_plist = self.plist.read_bytes()
        served_index = (
            self.runtime / "platform/index" / INDEX_REGISTRY_NAME
        ).read_bytes()
        records_before = len(self.services.records)
        self.public.failure_phase = "retry"
        failed = False
        try:
            _invoke_migration_cli(self.repository, self._migrator(), apply=True)
        except RuntimeError:
            failed = True
        finally:
            self.public.failure_phase = None
        new_records = self.services.records[records_before:]
        profile_revisions = HostProfileStore(
            HostProfilePaths.for_repository(self.platform)
        ).revisions()
        if not failed or [record.phase for record in new_records] != [
            "retry",
            "candidate",
        ]:
            raise RuntimeError("disposable failure injection was not exercised")
        if _process_exists(new_records[0].process_group):
            raise RuntimeError("failed disposable process group remained alive")
        if (
            self.registry_path.read_bytes() != former_registry
            or self.registry_path.read_bytes() != self.candidate_registry
            or self._pointer_pair() != former_pointers
            or self.plist.read_bytes() != former_plist
            or self.plist.read_bytes() != self.candidate_plist
            or (self.runtime / "platform/index" / INDEX_REGISTRY_NAME).read_bytes()
            != served_index
            or self.services.records[-1].command != self._expected_command("candidate")
            or self.services.state(f"com.sean.local-web.{_APP_ID}")
            is not ServiceState.RUNNING
            or _read_git(self.platform, "status", "--short")
            or not profile_revisions
            or profile_revisions[-1].operation != SERVICE_COMMAND_RESTORATION
            or profile_revisions[-1].app_id != _APP_ID
            or profile_revisions[-1].registry_bytes != former_registry
        ):
            raise RuntimeError("disposable failed migration recovery was inexact")
        self._require_all_health("candidate")
        self._require_data_unchanged()

    def _retry_migration(self) -> None:
        if self.retry_commit is None or self.candidate_commit is None:
            raise RuntimeError("disposable retry commits were unavailable")
        payload = _invoke_migration_cli(self.repository, self._migrator(), apply=True)
        self._require_plan(payload, command="change", action="redeploy and reload")
        registry = load_registry(self.registry_path)
        host = registry.apps[0]
        if (
            payload.get("port") != self.port
            or host.port != self.port
            or host.start_command is None
            or host.start_command.argv != self._expected_command("retry")
            or self._pointer_pair() != (self.retry_commit, self.candidate_commit)
            or self.services.records[-1].command != self._expected_command("retry")
            or self.services.records[-1].release.name != self.retry_commit
            or not self.services.records[-1].release_ready
            or self.services.state(f"com.sean.local-web.{_APP_ID}")
            is not ServiceState.RUNNING
            or _read_git(self.platform, "status", "--short")
        ):
            raise RuntimeError("disposable migration retry was invalid")
        self._require_all_health("retry")
        self._require_data_unchanged()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        failures: list[BaseException] = []
        try:
            self.services.close()
        except BaseException as error:
            failures.append(error)
        try:
            self.public.close()
        except BaseException as error:
            failures.append(error)
        if any(_process_exists(group) for group in self.services.process_groups):
            failures.append(RuntimeError("disposable process cleanup was incomplete"))
        if failures:
            raise RuntimeError("disposable workflow cleanup failed") from failures[0]


class ServiceCommandMigrationWorkflowVerifier:
    """Run the real migration inside private directories and a bounded worker."""

    def __init__(
        self,
        *,
        platform_repository=PLATFORM_REPOSITORY,
        coding_root=CODING_ROOT,
        emit=print,
        supervision_timeout=300,
        live_registry=None,
        external_data=None,
        scenario="normal",
    ):
        self.platform = Path(platform_repository).resolve()
        self.coding_root = Path(coding_root)
        self.emit, self.timeout = emit, supervision_timeout
        self.live_registry, self.external_data, self.scenario = (
            live_registry,
            external_data,
            scenario,
        )

    def run(self) -> int:
        try:
            with tempfile.TemporaryDirectory(
                prefix=".migration-", dir=self.coding_root
            ) as directory:
                parent = Path(directory).resolve(strict=True)
                root = parent / "workflow"
                root.mkdir()
                guard = self.live_registry or _create_disposable_guard_profile(parent)
                before = (Path(guard).read_bytes(), Path(guard).stat().st_mode)
                build_ui_package(self.platform, root / "local-web-ui.tgz")
                command = (
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(root),
                    str(self.platform),
                )
                result = _run_bounded_process(
                    command,
                    cwd=self.platform,
                    env=_private_execution_environment(root),
                    timeout=self.timeout,
                    worker=True,
                )
                if result.returncode:
                    raise RuntimeError(result.stdout[-2000:])
                if before != (Path(guard).read_bytes(), Path(guard).stat().st_mode):
                    raise RuntimeError("disposable guard changed")

                self.emit(result.stdout.rstrip())
            return 0
        except Exception as error:
            self.emit(f"migration FAIL: {error}")
            return 1


def _worker_main(root: Path, platform: Path) -> int:
    os.environ.clear()
    os.environ.update(_private_execution_environment(root))
    tempfile.tempdir = os.environ["TMPDIR"]
    with inherit_worker_session():
        workflow = DisposableServiceCommandMigrationWorkflow(root, platform)
        try:
            for phase in PHASES[:-1]:
                workflow.run_phase(phase)
                print(f"{phase} PASS", flush=True)
        finally:
            workflow.close()
        return 0


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--worker":
        return _worker_main(Path(sys.argv[2]), Path(sys.argv[3]))
    return ServiceCommandMigrationWorkflowVerifier().run()


if __name__ == "__main__":
    raise SystemExit(main())
