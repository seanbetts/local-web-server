#!/usr/bin/env python3
"""Verify public-base-path migration against disposable platform state."""

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
import time
import urllib.error
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLATFORM_REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_REPOSITORY))

from local_web_server import cli
import local_web_server.git_build as git_build_module
from local_web_server.app_activation import AppActivator, verify_served_index_tile
from local_web_server.app_registration import AppRegistrar
from local_web_server.config import (
    ConfigError,
    load_manifest,
    load_registry,
)
from local_web_server.deploy import DeploymentManager
from local_web_server.health import frontend_health_path
from local_web_server.git_build import Builder
from local_web_server.index_registry import INDEX_REGISTRY_NAME, INDEX_REGISTRY_ROUTE
from local_web_server.install import Installer, load_main_manifests
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import (
    PUBLIC_BASE_PATH_RESTORATION,
    HostProfileStore,
)
from local_web_server.runtime import RuntimeLayout, read_release_commit
from local_web_server.public_base_path_migration import PublicBasePathMigrator
from local_web_server.service_ports import ServicePortAllocator
from local_web_server.services import HttpHealthChecker, ServiceState
from local_web_server.ui_package import build_ui_package
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
    "create healthy legacy registered service",
    "commit hosted frontend release",
    "preview public base path migration without writes",
    "apply migration with fresh environment",
    "verify fixed port command assets and health",
    "repeat apply as no-op",
    "restore legacy registration and service after injected failure",
    "retry public base path migration successfully",
    "cleanup",
)


_MAX_CLI_OUTPUT = 8192
_MAX_HTTP_BODY = 8192
_MAX_CADDY_OUTPUT = 262144
_CADDY_COMMAND_TIMEOUT = 10.0
_MAX_BUILD_LOG = 8192
_BUILD_COMMAND_TIMEOUT = 20.0
_CADDY = Path("/opt/homebrew/bin/caddy")
_HTTP_READY_TIMEOUT = 5.0
_DATA_SENTINEL = b'{"state":"external-and-immutable"}\n'
_APP_ID = "public-base-path-fixture"
_ROUTE = "/fixture"
_SERVICE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
_PARENT_SECRET_ENVIRONMENT_NAME = "LOCAL_WEB_PUBLIC_BASE_PATH_PARENT_SECRET"


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
    original = cli.PublicBasePathMigrator
    original_loader = cli._load_default_host_profile

    def migrator_factory(*, registry_path: Path):
        if Path(registry_path) != Path(migrator.registry_path):
            raise RuntimeError("disposable migration CLI failed")
        return migrator

    cli.PublicBasePathMigrator = migrator_factory
    cli._load_default_host_profile = lambda: (registry_path, registry)
    arguments = [
        "app",
        "migrate-public-base-path",
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
        cli.PublicBasePathMigrator = original
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
                "host": "public-base-path-guard.invalid",
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


@dataclass(frozen=True)
class _LaunchRecord:
    command: tuple[str, ...]
    release: Path
    release_ready: bool
    version: str
    public_base_path: str
    process_group: int


@dataclass(frozen=True)
class _LaunchSpec:
    command: tuple[str, ...]
    working_directory: Path
    environment: tuple[tuple[str, str], ...]


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
        return self.records[-1].version if self.records else None

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
            raise RuntimeError("disposable public base path was unavailable")
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
        environment = (
            payload.get("EnvironmentVariables") if isinstance(payload, dict) else None
        )
        if (
            not isinstance(payload, dict)
            or payload.get("Label") != label
            or not isinstance(arguments, list)
            or not arguments
            or any(not isinstance(value, str) for value in arguments)
            or tuple(arguments) != self.host.start_command.argv
            or not isinstance(working_directory, str)
            or Path(working_directory) != expected_directory
            or environment != {"PATH": _SERVICE_PATH}
        ):
            raise RuntimeError("disposable service plist contract was invalid")
        return _LaunchSpec(
            tuple(arguments),
            Path(working_directory),
            tuple(sorted(environment.items())),
        )

    def _launch(self, spec: _LaunchSpec) -> None:
        release = spec.working_directory.resolve(strict=True)
        command = spec.command
        metadata_path = release / "public/build.json"
        release_ready = (
            release.is_dir()
            and (release / "server/service.mjs").is_file()
            and (release / "public/index.html").is_file()
            and metadata_path.is_file()
        )
        if not release_ready:
            raise RuntimeError("disposable release was not selected before launch")
        try:
            content = metadata_path.read_bytes()
            if len(content) > _MAX_HTTP_BODY:
                raise ValueError
            metadata = json.loads(content.decode("utf-8"))
            version = metadata.get("version")
            public_base_path = metadata.get("publicBasePath")
            if (
                not isinstance(version, str)
                or version not in {"old", "candidate", "retry"}
                or not isinstance(public_base_path, str)
                or not public_base_path.startswith("/")
                or not public_base_path.endswith("/")
            ):
                raise ValueError
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise RuntimeError("disposable release metadata was invalid") from error
        environment = _owned_environment()
        if environment is None:
            raise RuntimeError("disposable process ownership was unavailable")
        environment.update(dict(spec.environment))
        environment = sanitized_subprocess_environment(environment)
        process = subprocess.Popen(
            command,
            cwd=spec.working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=environment,
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
                version,
                public_base_path,
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
        caddy: _DisposableCaddyController,
    ) -> None:
        self.registry_path = Path(registry_path)
        self.platform_repository = Path(platform_repository)
        self.home = Path(home)
        self.services = services
        self.caddy = caddy
        self.runner = _DisposableInstallRunner(
            self.platform_repository,
        )
        self.results: list[object] = []

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
        result = Installer(
            registry,
            manifests,
            repository=self.platform_repository,
            home=self.home,
            run=self.runner,
            theme_gate=_DisposableThemeGate(),
        ).install(recover_missing_caddy=recover_missing_caddy)
        self.results.append(result)
        self.services.configure(registry)
        self.caddy.replace(
            self.home / "Library/LaunchAgents/com.sean.local-web.caddy.plist"
        )
        return result


class _DisposableInstallRunner:
    def __init__(self, repository: Path) -> None:
        self.repository = Path(repository)

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
        if command[:2] == [str(_CADDY), "validate"]:
            environment = _owned_environment()
            if environment is None or env is None:
                raise RuntimeError("disposable process ownership was unavailable")
            environment.update(dict(env))
            return _run_bounded_process(
                tuple(command),
                cwd=self.repository,
                env=environment,
                timeout=min(
                    float(timeout or _CADDY_COMMAND_TIMEOUT), _CADDY_COMMAND_TIMEOUT
                ),
                maximum=_MAX_CADDY_OUTPUT,
                merge_stderr=True,
            )
        raise RuntimeError("disposable installer attempted an unexpected command")


class _DisposableThemeGate:
    def verify(self, candidate: Path) -> None:
        if not Path(candidate).is_file():
            raise RuntimeError("disposable theme candidate was unavailable")


class _BuildSubprocessProxy:
    """Bound actual production build commands using the common fixture runner."""

    def __init__(self, original, destination):
        self.original, self.destination = original, destination

    def __getattr__(self, name):
        return getattr(self.original, name)

    def run(self, *args, **kwargs):
        if kwargs.get("stdout") is not self.destination:
            return self.original.run(*args, **kwargs)
        result = _run_bounded_process(
            args[0],
            cwd=Path(kwargs["cwd"]),
            env=kwargs["env"],
            timeout=_BUILD_COMMAND_TIMEOUT,
            maximum=_MAX_BUILD_LOG,
        )
        self.destination.write(result.stdout)
        return result


class _BoundedProductionBuilder:
    """Retain Builder semantics while bounding its command output and duration."""

    def build(self, host, manifest, commit, layout, environment, log):
        original = git_build_module.subprocess
        git_build_module.subprocess = _BuildSubprocessProxy(original, log)
        try:
            return Builder().build(
                host,
                manifest,
                commit,
                layout,
                environment,
                log,
            )
        finally:
            git_build_module.subprocess = original


@dataclass(frozen=True)
class _CaddyLaunchSpec:
    working_directory: Path
    environment: tuple[tuple[str, str], ...]


class _DisposableCaddyController:
    """Execute the generated Caddy routes on one private loopback listener."""

    def __init__(
        self,
        runtime_root: Path,
        repository: Path,
        port: int,
        *,
        corrupt_route: bool,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.repository = Path(repository)
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None
        self.process_groups: list[int] = []
        self.execution_config = self.runtime_root / "Caddyfile.disposable.json"
        self.environment_root = self.runtime_root / "caddy-environment"
        self.environment_home = self.environment_root / "home"
        self.environment_config = self.environment_root / "config"
        self.environment_data = self.environment_root / "data"
        self.corrupt_route = corrupt_route

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.port}"

    def _launch_spec(self, plist_path: Path) -> _CaddyLaunchSpec:
        try:
            payload = plistlib.loads(Path(plist_path).read_bytes())
        except (OSError, plistlib.InvalidFileException) as error:
            raise RuntimeError("disposable Caddy plist was unavailable") from error
        expected_arguments = [
            str(_CADDY),
            "run",
            "--config",
            str(self.runtime_root / "Caddyfile"),
        ]
        environment = (
            payload.get("EnvironmentVariables", {})
            if isinstance(payload, dict)
            else None
        )
        if (
            not isinstance(payload, dict)
            or payload.get("Label") != "com.sean.local-web.caddy"
            or payload.get("ProgramArguments") != expected_arguments
            or payload.get("WorkingDirectory") != str(self.repository)
            or not isinstance(environment, dict)
            or any(
                not isinstance(name, str) or not isinstance(value, str)
                for name, value in environment.items()
            )
        ):
            raise RuntimeError("disposable Caddy plist contract was invalid")
        return _CaddyLaunchSpec(
            self.repository,
            tuple(sorted(environment.items())),
        )

    def _execution_environment(self, spec: _CaddyLaunchSpec) -> dict[str, str]:
        environment = _owned_environment()
        if environment is None:
            raise RuntimeError("disposable process ownership was unavailable")
        protected = {
            "HOME",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
        }
        if any(name in protected for name, _value in spec.environment):
            raise RuntimeError("disposable Caddy plist environment was invalid")
        for directory in (
            self.environment_home,
            self.environment_config,
            self.environment_data,
        ):
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        environment.update(dict(spec.environment))
        environment.update(
            {
                "HOME": str(self.environment_home),
                "XDG_CONFIG_HOME": str(self.environment_config),
                "XDG_DATA_HOME": str(self.environment_data),
            }
        )
        return environment

    def _adapt_private_listener(self, environment: dict[str, str]) -> None:
        result = _run_bounded_process(
            (
                str(_CADDY),
                "adapt",
                "--config",
                str(self.runtime_root / "Caddyfile"),
                "--adapter",
                "caddyfile",
            ),
            cwd=self.repository,
            env=sanitized_subprocess_environment(environment),
            timeout=_CADDY_COMMAND_TIMEOUT,
            maximum=_MAX_CADDY_OUTPUT,
            merge_stderr=False,
        )
        if result.returncode != 0:
            raise RuntimeError("generated disposable Caddy routing was invalid")
        try:
            payload = json.loads(result.stdout)
            servers = payload["apps"]["http"]["servers"]
            if not isinstance(servers, dict) or len(servers) != 1:
                raise ValueError
            server = next(iter(servers.values()))
            if not isinstance(server, dict) or server.get("listen") != [":80"]:
                raise ValueError
            server["listen"] = [f"127.0.0.1:{self.port}"]
            payload["admin"] = {"disabled": True}
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(
                "generated disposable Caddy routing was invalid"
            ) from error
        content = (
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            + b"\n"
        )
        if len(content) > _MAX_CADDY_OUTPUT:
            raise RuntimeError("generated disposable Caddy routing exceeded its bound")
        self.execution_config.write_bytes(content)
        self.execution_config.chmod(0o600)

    def _validate_generated_routing(self, environment: dict[str, str]) -> None:
        caddyfile = self.runtime_root / "Caddyfile"
        if self.corrupt_route:
            content = caddyfile.read_text(encoding="utf-8")
            expected = f"handle_path {_ROUTE}/* {{"
            if content.count(expected) != 1:
                raise RuntimeError("disposable Caddy route corruption was unavailable")
            caddyfile.write_text(
                content.replace(
                    expected,
                    "handle_path /corrupted-fixture/* {",
                    1,
                ),
                encoding="utf-8",
            )
        result = _run_bounded_process(
            (
                str(_CADDY),
                "validate",
                "--config",
                str(caddyfile),
                "--adapter",
                "caddyfile",
            ),
            cwd=self.repository,
            env=environment,
            timeout=_CADDY_COMMAND_TIMEOUT,
            maximum=_MAX_CADDY_OUTPUT,
            merge_stderr=True,
        )
        if result.returncode != 0:
            raise RuntimeError("generated disposable Caddy routing was invalid")

    def _stop(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            _stop_process(process)
        finally:
            self.process = None

    def replace(self, plist_path: Path) -> None:
        spec = self._launch_spec(plist_path)
        environment = self._execution_environment(spec)
        self._validate_generated_routing(environment)
        self._adapt_private_listener(environment)
        self._stop()
        process = subprocess.Popen(
            (
                str(_CADDY),
                "run",
                "--config",
                str(self.execution_config),
            ),
            cwd=spec.working_directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            close_fds=True,
            start_new_session=False,
        )
        self.process = process

        self.process_groups.append(process.pid)
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            pass
        else:
            self._stop()
            raise RuntimeError("disposable Caddy process failed to start")
        _read_http_bytes(f"http://{self.host}{INDEX_REGISTRY_ROUTE}")

    def close(self) -> None:
        self._stop()


def _bounded_file_bytes(path: Path) -> bytes:
    try:
        content = Path(path).read_bytes()
    except OSError as error:
        raise RuntimeError("disposable file evidence was unavailable") from error
    if len(content) > _MAX_HTTP_BODY:
        raise RuntimeError("disposable file evidence exceeded its bound")
    return content


def _file_state(path: Path) -> tuple[object, ...]:
    metadata = Path(path).lstat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        _bounded_file_bytes(path),
    )


def _immutable_unchanged(path: Path, expected: tuple[object, ...]) -> bool:
    try:
        return _file_state(path) == expected
    except (OSError, RuntimeError):
        return False


def _managed_file_state(path: Path) -> tuple[object, ...]:
    metadata = Path(path).lstat()
    return (stat.S_IMODE(metadata.st_mode), _bounded_file_bytes(path))


def _read_http_bytes(url: str) -> bytes:
    deadline = time.monotonic() + _HTTP_READY_TIMEOUT
    while True:
        try:
            request = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(request, timeout=0.5) as response:
                if response.status != 200:
                    raise RuntimeError("disposable HTTP evidence was unavailable")
                content = response.read(_MAX_HTTP_BODY + 1)
            if len(content) > _MAX_HTTP_BODY:
                raise RuntimeError("disposable HTTP evidence exceeded its bound")
            return content
        except urllib.error.HTTPError as error:
            error.close()
            raise RuntimeError("disposable HTTP evidence was unavailable") from error
        except (ConnectionError, TimeoutError, urllib.error.URLError) as error:
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "disposable HTTP endpoint did not become ready"
                ) from error
            time.sleep(0.02)


def _fixture_builder_source() -> str:
    return (
        "import json\n"
        "import os\n"
        "import shutil\n"
        "from pathlib import Path\n"
        "base = os.environ.get('VITE_PUBLIC_BASE_PATH')\n"
        "version = Path('build-version.txt').read_text(encoding='utf-8').strip()\n"
        "if base != '/fixture/' or version not in {'old', 'candidate', 'retry'}:\n"
        "    raise SystemExit(64)\n"
        "if version == 'candidate' and Path('noisy-build.txt').exists():\n"
        "    os.write(1, b'x' * 65536)\n"
        "output = Path('fixture-release')\n"
        "if output.exists():\n"
        "    shutil.rmtree(output)\n"
        "(output / 'public/assets').mkdir(parents=True)\n"
        "(output / 'server').mkdir()\n"
        "index = ('<!doctype html><html data-public-base=\\\"' + base + '\\\">'\n"
        "         '<body data-build-version=\\\"' + version + '\\\">'\n"
        "         '<script type=\\\"module\\\" src=\\\"' + base + 'assets/app.js\\\"></script>'\n"
        "         '</body></html>\\n')\n"
        "asset = ('window.__fixturePublicBasePath = ' + json.dumps(base) + ';\\n'\n"
        "         'window.__fixtureBuildVersion = ' + json.dumps(version) + ';\\n')\n"
        "metadata = {\n"
        "    'environmentFileRetained': os.environ.get('FIXTURE_ENVIRONMENT_FILE_ONLY') == 'from-file',\n"
        "    'fixedEnvironmentWon': base == '/fixture/',\n"
        f"    'parentSecretAbsent': {_PARENT_SECRET_ENVIRONMENT_NAME!r} not in os.environ,\n"
        "    'publicBasePath': base,\n"
        "    'version': version,\n"
        "}\n"
        "(output / 'public/index.html').write_text(index, encoding='utf-8')\n"
        "(output / 'public/assets/app.js').write_text(asset, encoding='utf-8')\n"
        "(output / 'public/build.json').write_text(json.dumps(metadata, sort_keys=True) + '\\n', encoding='utf-8')\n"
        "shutil.copyfile('server/service.mjs', output / 'server/service.mjs')\n"
    )


def _fixture_service_source() -> str:
    return (
        "import { access, readFile, realpath } from 'node:fs/promises';\n"
        "import { createServer } from 'node:http';\n"
        "import { dirname, resolve } from 'node:path';\n"
        "import { fileURLToPath } from 'node:url';\n"
        "const args = process.argv.slice(2);\n"
        "const value = (flag) => { const index = args.indexOf(flag); return index < 0 ? null : args[index + 1]; };\n"
        "const port = Number(value('--port'));\n"
        "const dataPath = value('--data-file');\n"
        "const failurePath = value('--failure-file');\n"
        "if (!Number.isInteger(port) || port < 1 || port > 65535 || dataPath === null || failurePath === null) process.exit(64);\n"
        "const script = await realpath(fileURLToPath(import.meta.url));\n"
        "const release = dirname(dirname(script));\n"
        "await access(resolve(release, 'public/index.html'));\n"
        "const metadata = JSON.parse(await readFile(resolve(release, 'public/build.json'), 'utf8'));\n"
        "const data = JSON.parse(await readFile(dataPath, 'utf8'));\n"
        f"const parentSecretAbsent = process.env[{json.dumps(_PARENT_SECRET_ENVIRONMENT_NAME)}] === undefined;\n"
        "const evidence = JSON.stringify({ buildVersion: metadata.version, publicBasePath: metadata.publicBasePath, releaseReady: true, dataReady: data.state === 'external-and-immutable', environmentFileRetained: metadata.environmentFileRetained, fixedEnvironmentWon: metadata.fixedEnvironmentWon, parentSecretAbsent: metadata.parentSecretAbsent, serviceParentSecretAbsent: parentSecretAbsent });\n"
        "createServer(async (request, response) => {\n"
        "  if (request.url !== '/healthz' && request.url !== '/api/health') { response.writeHead(404).end(); return; }\n"
        "  const failed = metadata.version === 'retry' && await access(failurePath).then(() => true, () => false);\n"
        "  if (failed && request.url === '/healthz') { response.writeHead(503).end(); return; }\n"
        "  response.setHeader('Content-Type', 'application/json');\n"
        "  response.setHeader('Content-Length', String(Buffer.byteLength(evidence)));\n"
        "  response.writeHead(200);\n"
        "  response.end(request.method === 'HEAD' ? undefined : evidence);\n"
        "}).listen(port, '127.0.0.1');\n"
    )


class DisposablePublicBasePathMigrationWorkflow:
    """Drive the real migration transaction only inside one disposable host."""

    def __init__(
        self,
        root: Path,
        platform_repository: Path,
        external_data: Path,
        *,
        scenario: str,
    ):
        self.root = Path(root).resolve(strict=True)
        self.source_platform = Path(platform_repository).resolve(strict=True)
        self.platform = self.root / "disposable-platform"
        self.repository = self.root / _APP_ID
        self.runtime = self.root / "runtime"
        self.home = self.root / "home"
        self.launch_agents = self.home / "Library/LaunchAgents"
        self.registry_path = self.platform / "config/local/apps.json"
        self.data_path = Path(external_data).resolve(strict=True)
        try:
            self.data_path.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise RuntimeError("external sentinel entered the workflow root")
        self.data_link = self.repository / "external-data/state.json"
        self.failure_marker = self.repository / ".failure/fail-retry"
        self.environment_file = self.repository / ".env.fixture"
        self.services = _DisposableServiceController(self.runtime)
        self.public_port = _allocate_fixture_port()
        self.caddy = _DisposableCaddyController(
            self.runtime,
            self.platform,
            self.public_port,
            corrupt_route=scenario == "corrupt-caddy-route",
        )
        self.scenario = scenario
        self.installer = _DisposableInstaller(
            self.registry_path,
            self.platform,
            self.home,
            self.services,
            self.caddy,
        )
        self.port: int | None = None
        self.data_snapshot: tuple[object, ...] | None = None
        self.environment_snapshot: tuple[object, ...] | None = None
        self.old_commit: str | None = None
        self.candidate_commit: str | None = None
        self.retry_commit: str | None = None
        self.old_command: tuple[str, ...] | None = None
        self.legacy_registry: bytes | None = None
        self._phase_index = 0
        self._closed = False

    @property
    def layout(self) -> RuntimeLayout:
        return RuntimeLayout(self.runtime, _APP_ID)

    @property
    def plist(self) -> Path:
        return self.launch_agents / f"com.sean.local-web.{_APP_ID}.plist"

    @property
    def caddy_plist(self) -> Path:
        return self.launch_agents / "com.sean.local-web.caddy.plist"

    @property
    def served_registry(self) -> Path:
        return self.runtime / "platform/index" / INDEX_REGISTRY_NAME

    def run_phase(self, phase: str) -> None:
        if self._closed or self._phase_index >= len(PHASES) - 1:
            raise RuntimeError("disposable workflow phase was invalid")
        if phase != PHASES[self._phase_index]:
            raise RuntimeError("disposable workflow phase order was invalid")
        operations = (
            self._create_legacy_service,
            self._commit_hosted_frontend_release,
            self._preview_migration,
            self._apply_migration,
            self._verify_fixed_command_assets_and_health,
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
            "public-base-path-migration@localhost",
        )
        self.repository.mkdir()
        _run_git(self.repository, "init", "--initial-branch=main")
        _run_git(self.repository, "config", "user.name", "Disposable Migration")
        _run_git(
            self.repository,
            "config",
            "user.email",
            "public-base-path-migration@localhost",
        )
        generate_service_repository(
            self.repository,
            self.platform,
            self.root / "local-web-ui.tgz",
            app_id=_APP_ID,
            route=_ROUTE,
        )

    def _prepare_old_service_commit(self) -> None:
        manifest_path = self.repository / "local-web.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["route"] = _ROUTE
        manifest["healthPath"] = f"{_ROUTE}/healthz"
        manifest["build"] = {
            "commands": [["/usr/bin/env", "python3", "scripts/build_fixture.py"]],
            "output": "fixture-release",
            "environment": [
                "FIXTURE_ENVIRONMENT_FILE_ONLY",
                "VITE_PUBLIC_BASE_PATH",
            ],
        }
        manifest["service"] = {
            "module": "server/service.mjs",
            "internalHealthPath": "/healthz",
            "frontendOutput": "public",
            "proxyPaths": ["/api"],
            "startCommand": [
                "/usr/bin/env",
                "node",
                "{release}/server/service.mjs",
                "--port",
                "{port}",
                "--data-file",
                "{repository}/external-data/state.json",
                "--failure-file",
                "{repository}/.failure/fail-retry",
            ],
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        scripts = self.repository / "scripts"
        scripts.mkdir(exist_ok=True)
        (scripts / "build_fixture.py").write_text(
            _fixture_builder_source(), encoding="utf-8"
        )
        (self.repository / "server/service.mjs").write_text(
            _fixture_service_source(), encoding="utf-8"
        )
        (self.repository / "build-version.txt").write_text("old\n", encoding="utf-8")
        gitignore = self.repository / ".gitignore"
        ignored = gitignore.read_text(encoding="utf-8")
        if not ignored.endswith("\n"):
            ignored += "\n"
        for entry in (
            "fixture-release/",
            "external-data/",
            ".failure/",
            ".env.fixture",
        ):
            line = f"{entry}\n"
            if line not in ignored:
                ignored += line
        gitignore.write_text(ignored, encoding="utf-8")
        _run_git(self.repository, "add", "--all")
        _run_git(
            self.repository,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "commit",
            "-m",
            "test: prepare disposable public base path fixture",
        )
        self.environment_file.write_bytes(
            b"VITE_PUBLIC_BASE_PATH=/legacy-fixture/\n"
            b"FIXTURE_ENVIRONMENT_FILE_ONLY=from-file\n"
        )
        self.environment_file.chmod(0o600)
        self.data_link.parent.mkdir()
        self.data_link.symlink_to(self.data_path)
        self.data_snapshot = _file_state(self.data_path)
        self.environment_snapshot = _file_state(self.environment_file)
        if _read_git(self.repository, "status", "--short"):
            raise RuntimeError("disposable application was not clean")

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
                    "host": self.caddy.host,
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
        environment = _owned_environment()
        if environment is None:
            raise RuntimeError("disposable process ownership was unavailable")
        return DeploymentManager(
            registry,
            builder=_BoundedProductionBuilder(),
            services=self.services,
            health=HttpHealthChecker(),
            launch_agents=self.launch_agents,
            environment=environment,
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

    def _migrator(self) -> PublicBasePathMigrator:
        return PublicBasePathMigrator(
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

    def _publish_legacy_registration(self) -> bytes:
        before = self.registry_path.read_bytes()
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        apps = payload.get("apps")
        if not isinstance(apps, list) or len(apps) != 1:
            raise RuntimeError("disposable registry fixture was invalid")
        entry = apps[0]
        if not isinstance(entry, dict) or entry.get("id") != _APP_ID:
            raise RuntimeError("disposable registry fixture was invalid")
        environment = entry.pop("environment", None)
        if environment not in (
            None,
            {"VITE_PUBLIC_BASE_PATH": f"{_ROUTE}/"},
        ):
            raise RuntimeError("disposable fixed environment was invalid")
        entry["environmentFile"] = str(self.environment_file.resolve(strict=True))
        content = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        HostProfileStore(
            HostProfilePaths.for_repository(self.platform)
        ).publish_public_base_path_restoration(_APP_ID, before, content)
        self.installer(self.platform, dry_run=False)
        self.services.configure(load_registry(self.registry_path))
        if _read_git(self.platform, "status", "--short"):
            raise RuntimeError("disposable platform was not clean")
        return content

    def _create_legacy_service(self) -> None:
        self._clone_platform_and_generate_app()
        self._prepare_old_service_commit()
        self.port = _allocate_fixture_port()
        while self.port == self.public_port:
            self.port = _allocate_fixture_port()
        self._initialise_platform_registry()

        result = self._activator().activate(self.repository)
        registered = load_registry(self.registry_path).apps[0]
        self.old_commit = _read_git(self.repository, "rev-parse", "refs/heads/main")
        self.old_command = self._expected_command()
        if (
            result.registry_revision is None
            or not result.verified
            or registered.id != _APP_ID
            or registered.port != self.port
            or registered.start_command is None
            or registered.start_command.argv != self.old_command
            or dict(registered.environment) != {"VITE_PUBLIC_BASE_PATH": f"{_ROUTE}/"}
        ):
            raise RuntimeError("disposable activation fixture was invalid")
        self.legacy_registry = self._publish_legacy_registration()
        registry = load_registry(self.registry_path)
        host = registry.apps[0]
        raw = json.loads(self.legacy_registry.decode("utf-8"))["apps"][0]
        if (
            "environment" in raw
            or raw.get("environmentFile")
            != str(self.environment_file.resolve(strict=True))
            or host.port != self.port
            or host.start_command is None
            or host.start_command.argv != self.old_command
            or read_release_commit(self.layout.current) != self.old_commit
            or read_release_commit(self.layout.previous) is not None
            or self.services.state(f"com.sean.local-web.{_APP_ID}")
            is not ServiceState.RUNNING
            or self.services.records[-1].version != "old"
            or self.services.records[-1].public_base_path != f"{_ROUTE}/"
            or not self.plist.is_file()
            or not (self.runtime / "Caddyfile").is_file()
            or not self.caddy_plist.is_file()
            or not self.served_registry.is_file()
            or _read_git(self.platform, "status", "--short")
            or _read_git(self.repository, "status", "--short")
        ):
            raise RuntimeError("disposable legacy service was invalid")
        self._require_all_health("old")
        self._require_sentinels()

    def _commit_version(self, version: str, message: str) -> str:
        if version not in {"candidate", "retry"}:
            raise RuntimeError("disposable build version was invalid")
        (self.repository / "build-version.txt").write_text(
            f"{version}\n", encoding="utf-8"
        )
        _run_git(self.repository, "add", "build-version.txt")
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

    def _commit_hosted_frontend_release(self) -> None:
        if self.scenario == "noisy-build":
            (self.repository / "noisy-build.txt").write_text(
                "exercise bounded production build output\n", encoding="utf-8"
            )
            _run_git(self.repository, "add", "noisy-build.txt")
        self.candidate_commit = self._commit_version(
            "candidate", "test: commit disposable hosted frontend"
        )
        if self.candidate_commit == self.old_commit:
            raise RuntimeError("disposable target commit did not change")
        target = self.layout.releases / self.candidate_commit
        if target.exists():
            raise RuntimeError("disposable target release existed before migration")
        self._require_sentinels()

    @staticmethod
    def _require_plan(
        payload: dict[str, object], *, public_base_path: str, action: str
    ) -> None:
        expected = {
            "application": _APP_ID,
            "publicBasePath": public_base_path,
            "identity": "unchanged",
            "repository": "unchanged",
            "route": "unchanged",
            "portStatus": "unchanged",
            "serviceCommand": "unchanged",
            "serviceAction": action,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise RuntimeError("disposable migration plan was invalid")
        port = payload.get("port")
        if type(port) is not int:
            raise RuntimeError("disposable migration port was invalid")

    def _pointer_pair(self) -> tuple[str | None, str | None]:
        return (
            read_release_commit(self.layout.current),
            read_release_commit(self.layout.previous),
        )

    def _runtime_snapshot(self) -> tuple[object, ...]:
        process = self.services.process
        return (
            self.registry_path.read_bytes(),
            stat.S_IMODE(self.registry_path.stat().st_mode),
            _read_git(self.platform, "rev-parse", "HEAD"),
            self._pointer_pair(),
            _file_state(self.plist),
            _file_state(self.runtime / "Caddyfile"),
            _file_state(self.caddy.execution_config),
            _file_state(self.caddy_plist),
            _file_state(self.served_registry),
            process.pid if process is not None else None,
            len(self.services.records),
            tuple(sorted(path.name for path in self.layout.releases.iterdir())),
            len(self.installer.results),
            _file_state(self.environment_file),
            _file_state(self.data_path),
        )

    def _preview_migration(self) -> None:
        before = self._runtime_snapshot()
        payload = _invoke_migration_cli(self.repository, self._migrator(), apply=False)
        self._require_plan(
            payload, public_base_path="change", action="redeploy and reload"
        )
        if (
            before != self._runtime_snapshot()
            or self.candidate_commit is None
            or (self.layout.releases / self.candidate_commit).exists()
        ):
            raise RuntimeError("disposable migration preview wrote state")
        self._require_sentinels()

    def _expected_command(self) -> tuple[str, ...]:
        if self.port is None:
            raise RuntimeError("disposable service port was unavailable")
        return (
            "/usr/bin/env",
            "node",
            str(self.layout.current / "server/service.mjs"),
            "--port",
            str(self.port),
            "--data-file",
            str(self.data_link),
            "--failure-file",
            str(self.failure_marker),
        )

    def _apply_migration(self) -> None:
        if self.candidate_commit is None or self.old_commit is None:
            raise RuntimeError("disposable migration commits were unavailable")
        before_pid = (
            self.services.process.pid if self.services.process is not None else None
        )
        records_before = len(self.services.records)
        payload = _invoke_migration_cli(self.repository, self._migrator(), apply=True)
        self._require_plan(
            payload, public_base_path="change", action="redeploy and reload"
        )
        registry = load_registry(self.registry_path)
        host = registry.apps[0]
        record = self.services.records[-1]
        current = self.layout.current.resolve(strict=True)
        if (
            payload.get("port") != self.port
            or host.port != self.port
            or host.start_command is None
            or host.start_command.argv != self.old_command
            or dict(host.environment) != {"VITE_PUBLIC_BASE_PATH": f"{_ROUTE}/"}
            or host.environment_file != self.environment_file.resolve(strict=True)
            or read_release_commit(self.layout.current) != self.candidate_commit
            or read_release_commit(self.layout.previous) != self.old_commit
            or len(self.services.records) != records_before + 1
            or record.command != self.old_command
            or record.release != current
            or record.release.name != self.candidate_commit
            or not record.release_ready
            or record.version != "candidate"
            or record.public_base_path != f"{_ROUTE}/"
            or self.services.process is None
            or self.services.process.pid == before_pid
            or _read_git(self.platform, "status", "--short")
        ):
            raise RuntimeError("disposable migration application was invalid")
        self._verify_hosted_build("candidate", self.candidate_commit)
        self._require_sentinels()

    def _verify_hosted_build(self, version: str, commit: str) -> None:
        release = self.layout.releases / commit
        metadata = json.loads(
            _bounded_file_bytes(release / "public/build.json").decode("utf-8")
        )
        index = _bounded_file_bytes(release / "public/index.html").decode("utf-8")
        asset = _bounded_file_bytes(release / "public/assets/app.js").decode("utf-8")
        if (
            metadata
            != {
                "environmentFileRetained": True,
                "fixedEnvironmentWon": True,
                "parentSecretAbsent": True,
                "publicBasePath": f"{_ROUTE}/",
                "version": version,
            }
            or f'data-public-base="{_ROUTE}/"' not in index
            or f'data-build-version="{version}"' not in index
            or f'src="{_ROUTE}/assets/app.js"' not in index
            or f'window.__fixturePublicBasePath = "{_ROUTE}/";' not in asset
            or f'window.__fixtureBuildVersion = "{version}";' not in asset
        ):
            raise RuntimeError("disposable hosted build did not receive its base path")

    def _verify_served_assets(self, version: str) -> None:
        index = _read_http_bytes(f"http://{self.caddy.host}{_ROUTE}/").decode("utf-8")
        asset = _read_http_bytes(
            f"http://{self.caddy.host}{_ROUTE}/assets/app.js"
        ).decode("utf-8")
        if (
            f'data-public-base="{_ROUTE}/"' not in index
            or f'data-build-version="{version}"' not in index
            or f'src="{_ROUTE}/assets/app.js"' not in index
            or f'window.__fixturePublicBasePath = "{_ROUTE}/";' not in asset
            or f'window.__fixtureBuildVersion = "{version}";' not in asset
        ):
            raise RuntimeError("disposable hosted assets were invalid")

    def _service_evidence(self) -> dict[str, object]:
        return _read_json(f"http://{self.caddy.host}{_ROUTE}/api/health")

    def _require_all_health(self, version: str) -> None:
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
        if evidence != {
            "buildVersion": version,
            "publicBasePath": f"{_ROUTE}/",
            "releaseReady": True,
            "dataReady": True,
            "environmentFileRetained": version != "old",
            "fixedEnvironmentWon": True,
            "parentSecretAbsent": True,
            "serviceParentSecretAbsent": True,
        }:
            raise RuntimeError("disposable service evidence was invalid")
        self._verify_served_assets(version)

    def _verify_fixed_command_assets_and_health(self) -> None:
        if (
            self.candidate_commit is None
            or self.legacy_registry is None
            or self.old_command is None
        ):
            raise RuntimeError("disposable candidate evidence was unavailable")
        registry = load_registry(self.registry_path)
        host = registry.apps[0]
        before = json.loads(self.legacy_registry.decode("utf-8"))
        after = json.loads(self.registry_path.read_text(encoding="utf-8"))
        candidate_entry = dict(after["apps"][0])
        environment = candidate_entry.pop("environment", None)
        if (
            after.keys() != before.keys()
            or {**after, "apps": [candidate_entry]} != before
            or environment != {"VITE_PUBLIC_BASE_PATH": f"{_ROUTE}/"}
            or host.port != self.port
            or host.start_command is None
            or host.start_command.argv != self.old_command
            or host.environment_file != self.environment_file.resolve(strict=True)
        ):
            raise RuntimeError("disposable registry migration was not bounded")
        for path in (
            self.registry_path,
            self.runtime,
            self.runtime / "Caddyfile",
            self.caddy.execution_config,
            self.launch_agents,
            self.plist,
            self.caddy_plist,
            self.environment_file,
        ):
            try:
                path.resolve(strict=True).relative_to(self.root)
            except (OSError, ValueError) as error:
                raise RuntimeError(
                    "disposable state escaped its private root"
                ) from error
        self._verify_hosted_build("candidate", self.candidate_commit)
        self._require_all_health("candidate")
        self._require_sentinels()

    def _require_sentinels(self) -> None:
        if (
            self.data_snapshot is None
            or self.environment_snapshot is None
            or _file_state(self.data_path) != self.data_snapshot
            or _file_state(self.environment_file) != self.environment_snapshot
        ):
            raise RuntimeError("disposable external state changed")
        resolved_runtime = self.runtime.resolve()
        for external in (self.data_path, self.environment_file):
            try:
                external.resolve(strict=True).relative_to(resolved_runtime)
            except ValueError:
                pass
            else:
                raise RuntimeError("disposable external state entered the runtime")
        try:
            self.data_path.resolve(strict=True).relative_to(self.root)
        except ValueError:
            pass
        else:
            raise RuntimeError("immutable external state entered the workflow root")
        if (
            not self.data_link.is_symlink()
            or self.data_link.resolve(strict=True) != self.data_path
        ):
            raise RuntimeError("disposable external state link changed")
        for release in (
            self.layout.releases.glob("*") if self.layout.releases.exists() else ()
        ):
            if (release / "external-data").exists() or (
                release / ".env.fixture"
            ).exists():
                raise RuntimeError("disposable external state entered a release")

    def _verify_noop_apply(self) -> None:
        before = self._runtime_snapshot()
        payload = _invoke_migration_cli(self.repository, self._migrator(), apply=True)
        self._require_plan(payload, public_base_path="current", action="none")
        if before != self._runtime_snapshot():
            raise RuntimeError("disposable migration no-op changed state")
        self._require_all_health("candidate")
        self._require_sentinels()

    def _verify_failed_migration_recovery(self) -> None:
        self.retry_commit = self._commit_version(
            "retry", "test: prepare disposable failed public base path migration"
        )
        former_registry = self._publish_legacy_registration()
        former_registry_mode = stat.S_IMODE(self.registry_path.stat().st_mode)
        former_pointers = self._pointer_pair()
        former_plist = _managed_file_state(self.plist)
        former_caddy = _managed_file_state(self.runtime / "Caddyfile")
        former_caddy_execution = _managed_file_state(self.caddy.execution_config)
        former_caddy_plist = _managed_file_state(self.caddy_plist)
        former_served_index = _managed_file_state(self.served_registry)
        former_releases = tuple(
            sorted(path.name for path in self.layout.releases.iterdir())
        )
        records_before = len(self.services.records)
        self.failure_marker.parent.mkdir(exist_ok=True)
        self.failure_marker.write_bytes(b"fail retry health\n")
        failed = False
        try:
            _invoke_migration_cli(self.repository, self._migrator(), apply=True)
        except RuntimeError:
            failed = True
        finally:
            self.failure_marker.unlink(missing_ok=True)
            try:
                self.failure_marker.parent.rmdir()
            except FileNotFoundError:
                pass
        new_records = self.services.records[records_before:]
        profile_revisions = HostProfileStore(
            HostProfilePaths.for_repository(self.platform)
        ).revisions()
        if (
            not failed
            or [record.version for record in new_records] != ["retry", "candidate"]
            or not new_records[0].release_ready
            or new_records[0].public_base_path != f"{_ROUTE}/"
        ):
            raise RuntimeError("disposable post-build failure was not exercised")
        if _process_exists(new_records[0].process_group):
            raise RuntimeError("failed disposable process group remained alive")
        if (
            self.registry_path.read_bytes() != former_registry
            or stat.S_IMODE(self.registry_path.stat().st_mode) != former_registry_mode
            or self._pointer_pair() != former_pointers
            or _managed_file_state(self.plist) != former_plist
            or _managed_file_state(self.runtime / "Caddyfile") != former_caddy
            or _managed_file_state(self.caddy.execution_config)
            != former_caddy_execution
            or _managed_file_state(self.caddy_plist) != former_caddy_plist
            or _managed_file_state(self.served_registry) != former_served_index
            or tuple(sorted(path.name for path in self.layout.releases.iterdir()))
            != former_releases
            or self.retry_commit is None
            or (self.layout.releases / self.retry_commit).exists()
            or self.services.records[-1].command != self.old_command
            or self.services.records[-1].version != "candidate"
            or self.services.state(f"com.sean.local-web.{_APP_ID}")
            is not ServiceState.RUNNING
            or self.caddy.process is None
            or self.caddy.process.poll() is not None
            or _read_git(self.platform, "status", "--short")
            or not profile_revisions
            or profile_revisions[-1].operation != PUBLIC_BASE_PATH_RESTORATION
            or profile_revisions[-1].app_id != _APP_ID
            or profile_revisions[-1].registry_bytes != former_registry
        ):
            raise RuntimeError("disposable failed migration recovery was inexact")
        self._require_all_health("candidate")
        self._require_sentinels()

    def _retry_migration(self) -> None:
        if self.retry_commit is None or self.candidate_commit is None:
            raise RuntimeError("disposable retry commits were unavailable")
        if (self.layout.releases / self.retry_commit).exists():
            raise RuntimeError("disposable retry release survived recovery")
        payload = _invoke_migration_cli(self.repository, self._migrator(), apply=True)
        self._require_plan(
            payload, public_base_path="change", action="redeploy and reload"
        )
        registry = load_registry(self.registry_path)
        host = registry.apps[0]
        record = self.services.records[-1]
        if (
            payload.get("port") != self.port
            or host.port != self.port
            or host.start_command is None
            or host.start_command.argv != self.old_command
            or dict(host.environment) != {"VITE_PUBLIC_BASE_PATH": f"{_ROUTE}/"}
            or host.environment_file != self.environment_file.resolve(strict=True)
            or self._pointer_pair() != (self.retry_commit, self.candidate_commit)
            or record.command != self.old_command
            or record.release.name != self.retry_commit
            or not record.release_ready
            or record.version != "retry"
            or record.public_base_path != f"{_ROUTE}/"
            or self.services.state(f"com.sean.local-web.{_APP_ID}")
            is not ServiceState.RUNNING
            or _read_git(self.platform, "status", "--short")
        ):
            raise RuntimeError("disposable migration retry was invalid")
        self._verify_hosted_build("retry", self.retry_commit)
        self._require_all_health("retry")
        self._require_sentinels()

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
            self.caddy.close()
        except BaseException as error:
            failures.append(error)
        if any(
            _process_exists(group)
            for group in (*self.services.process_groups, *self.caddy.process_groups)
        ):
            failures.append(RuntimeError("disposable process cleanup was incomplete"))
        if failures:
            raise RuntimeError("disposable workflow cleanup failed") from failures[0]


class PublicBasePathMigrationWorkflowVerifier:
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
                external = (
                    Path(self.external_data)
                    if self.external_data
                    else parent / "state.json"
                )
                if self.external_data is None:
                    external.write_bytes(_DATA_SENTINEL)
                external_before = _file_state(external)
                build_ui_package(self.platform, root / "local-web-ui.tgz")
                command = (
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(root),
                    str(self.platform),
                    str(external),
                    self.scenario,
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
                if _file_state(external) != external_before:
                    raise RuntimeError("external application data changed")
                self.emit(result.stdout.rstrip())
            return 0
        except Exception as error:
            self.emit(f"migration FAIL: {error}")
            return 1


def _worker_main(root: Path, platform: Path, external: Path, scenario: str) -> int:
    os.environ.clear()
    os.environ.update(_private_execution_environment(root))
    tempfile.tempdir = os.environ["TMPDIR"]
    with inherit_worker_session():
        workflow = DisposablePublicBasePathMigrationWorkflow(
            root, platform, external, scenario=scenario
        )
        try:
            for phase in PHASES[:-1]:
                workflow.run_phase(phase)
                print(f"{phase} PASS", flush=True)
        finally:
            workflow.close()
        return 0


def main() -> int:
    if len(sys.argv) == 6 and sys.argv[1] == "--worker":
        return _worker_main(
            Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5]
        )
    return PublicBasePathMigrationWorkflowVerifier().run()


if __name__ == "__main__":
    raise SystemExit(main())
