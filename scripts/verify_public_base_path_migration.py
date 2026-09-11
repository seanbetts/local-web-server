#!/usr/bin/env python3
"""Verify public-base-path migration against disposable platform state."""

# ruff: noqa: E402

from __future__ import annotations

import codecs
import hashlib
import io
import json
import os
import plistlib
import re
import secrets
import selectors
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLATFORM_REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_REPOSITORY))

from local_web_server import cli
import local_web_server.git_build as git_build_module
from local_web_server.app_activation import AppActivator, verify_served_index_tile
from local_web_server.app_foundation import render_generated_repository
from local_web_server.app_provenance import (
    CURRENT_TEMPLATE_VERSION,
    AppProvenance,
    UiArtifactReference,
    render_provenance,
)
from local_web_server.app_registration import AppRegistrar
from local_web_server.app_template import TemplateInputs
from local_web_server.config import (
    ConfigError,
    SUPPORTED_PLATFORM_CONTRACT,
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
    _process_group_exists,
    _stop_process_group,
)
from scripts.disposable_workflow_support import (
    initialise_disposable_host_profile,
    sanitized_subprocess_environment,
)


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


SupervisedCommandFactory = Callable[[Path, Path, Path], tuple[str, ...]]
Emitter = Callable[[str], None]
_MAX_CLI_OUTPUT = 8192
_MAX_HTTP_BODY = 8192
_MAX_GIT_OUTPUT = 8192
_MAX_CADDY_OUTPUT = 262144
_CADDY_COMMAND_TIMEOUT = 10.0
_MAX_BUILD_LOG = 8192
_BUILD_COMMAND_TIMEOUT = 20.0
_CADDY = Path("/opt/homebrew/bin/caddy")
_SCENARIOS = {"normal", "corrupt-caddy-route", "noisy-build"}
_HTTP_READY_TIMEOUT = 5.0
_DATA_SENTINEL = b'{"state":"external-and-immutable"}\n'
_APP_ID = "public-base-path-fixture"
_ROUTE = "/fixture"
_SERVICE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
_ACTIVE_PROCESS_LEDGER: Path | None = None
_ACTIVE_PROCESS_GROUPS: dict[int, str] = {}
_PROCESS_OWNER_TOKEN: str | None = None
_PRIVATE_EXECUTION_ENVIRONMENT: dict[str, str] | None = None
_OWNER_ENVIRONMENT_NAME = "LOCAL_WEB_PUBLIC_BASE_PATH_MIGRATION_OWNER"
_PARENT_SECRET_ENVIRONMENT_NAME = "LOCAL_WEB_PUBLIC_BASE_PATH_PARENT_SECRET"
_OWNER_PATTERN = re.compile(
    rb"(?:^| )LOCAL_WEB_PUBLIC_BASE_PATH_MIGRATION_OWNER=([0-9a-f]{64})(?: |$)"
)
_UNVERIFIABLE_IDENTITY = "<unverifiable>"
_MAX_PROCESS_TABLE_OUTPUT = 8 * 1024 * 1024


def _group_exists(process_group: int) -> bool:
    try:
        return _process_group_exists(process_group)
    except PermissionError:
        return True


def _owned_environment() -> dict[str, str] | None:
    if (
        _PROCESS_OWNER_TOKEN is None
        or _PRIVATE_EXECUTION_ENVIRONMENT is None
        or re.fullmatch(r"[0-9a-f]{64}", _PROCESS_OWNER_TOKEN) is None
    ):
        return None
    return {
        **_PRIVATE_EXECUTION_ENVIRONMENT,
        _OWNER_ENVIRONMENT_NAME: _PROCESS_OWNER_TOKEN,
    }


def _private_execution_environment(root: Path) -> dict[str, str]:
    resolved_root = Path(root).resolve(strict=True)
    environment_root = resolved_root / "process-environment"
    paths = {
        "TMPDIR": environment_root / "tmp",
        "HOME": environment_root / "home",
        "XDG_CONFIG_HOME": environment_root / "config",
        "XDG_DATA_HOME": environment_root / "data",
    }
    for directory in paths.values():
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        directory.chmod(0o700)
    return {
        "PATH": _SERVICE_PATH,
        "LANG": "C",
        "LC_ALL": "C",
        **{name: str(path) for name, path in paths.items()},
    }


def _bounded_ps_output(arguments: tuple[str, ...], maximum: int) -> bytes | None:
    environment = {
        "PATH": _SERVICE_PATH,
        "LANG": "C",
        "LC_ALL": "C",
    }
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                env=sanitized_subprocess_environment(environment),
                close_fds=True,
                start_new_session=True,
            )
            try:
                return_code = process.wait(timeout=1)
            finally:
                _stop_process_group(process)
        except (OSError, subprocess.TimeoutExpired, RuntimeError):
            return None
        size = output.tell()
        if return_code != 0 or size > maximum:
            return None
        output.seek(0)
        return output.read()


@dataclass(frozen=True)
class _OwnedProcess:
    process_id: int
    started: str
    owner: str


def _parse_owned_process_line(line: bytes) -> _OwnedProcess | None:
    fields = line.split(maxsplit=6)
    if len(fields) != 7:
        return None
    match = _OWNER_PATTERN.search(fields[6])
    if match is None:
        return None
    try:
        process_id = int(fields[0])
        owner = match.group(1).decode("ascii")
        started = b" ".join(fields[1:6]).decode("ascii")
    except (ValueError, UnicodeError):
        return None
    if process_id <= 0:
        return None
    return _OwnedProcess(process_id, started, owner)


def _owned_process_identity(process_id: int) -> _OwnedProcess | None:
    output = _bounded_ps_output(
        (
            "/bin/ps",
            "eww",
            "-p",
            str(process_id),
            "-o",
            "pid=,lstart=,command=",
        ),
        65536,
    )
    if output is None:
        return None
    lines = output.splitlines()
    return _parse_owned_process_line(lines[0]) if len(lines) == 1 else None


def _discover_owned_processes(owner: str) -> tuple[_OwnedProcess, ...] | None:
    if not re.fullmatch(r"[0-9a-f]{64}", owner):
        return None
    output = _bounded_ps_output(
        ("/bin/ps", "eww", "-axo", "pid=,lstart=,command="),
        _MAX_PROCESS_TABLE_OUTPUT,
    )
    if output is None:
        return None
    lines = output.splitlines()
    if len(lines) > 16384:
        return None
    processes = []
    for line in lines:
        process = _parse_owned_process_line(line)
        if (
            process is not None
            and process.owner == owner
            and process.process_id != os.getpid()
        ):
            processes.append(process)
    return tuple(processes)


def _reap_owned_processes(owner: str, *, cleanup_timeout: float = 1.0) -> bool:
    targeted: dict[int, _OwnedProcess] = {}
    for action in (signal.SIGTERM, signal.SIGKILL):
        deadline = time.monotonic() + cleanup_timeout
        while True:
            processes = _discover_owned_processes(owner)
            if processes is None:
                return False
            if not processes:
                break
            for process in processes:
                targeted[process.process_id] = process
                if _owned_process_identity(process.process_id) != process:
                    targeted.pop(process.process_id, None)
                    continue
                try:
                    os.kill(process.process_id, action)
                except ProcessLookupError:
                    targeted.pop(process.process_id, None)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        if not processes:
            break
    if _discover_owned_processes(owner) not in ((),):
        return False
    deadline = time.monotonic() + cleanup_timeout
    while targeted and time.monotonic() < deadline:
        for process_id, expected in tuple(targeted.items()):
            if _owned_process_identity(process_id) != expected:
                targeted.pop(process_id, None)
                continue
            try:
                os.kill(process_id, 0)
            except ProcessLookupError:
                targeted.pop(process_id, None)
        if targeted:
            time.sleep(0.01)
    return not targeted


def _pid_identity(process_id: int) -> str | None:
    output = _bounded_ps_output(
        ("/bin/ps", "eww", "-p", str(process_id), "-o", "command="),
        65536,
    )
    if output is None:
        return None
    match = _OWNER_PATTERN.search(output)
    return match.group(1).decode("ascii") if match is not None else None


def _process_identity(process_group: int) -> str | None:
    output = _bounded_ps_output(("/bin/ps", "-axo", "pid=,pgid="), 65536)
    if output is None:
        return _UNVERIFIABLE_IDENTITY if _group_exists(process_group) else None
    members: list[int] = []
    try:
        for line in output.splitlines():
            fields = line.split()
            if len(fields) == 2 and int(fields[1]) == process_group:
                members.append(int(fields[0]))
    except ValueError:
        return _UNVERIFIABLE_IDENTITY
    if not members:
        return None
    if len(members) > 1024:
        return _UNVERIFIABLE_IDENTITY
    identities = [_pid_identity(member) for member in members]
    if any(identity is None for identity in identities):
        return _UNVERIFIABLE_IDENTITY
    unique = set(identities)
    return unique.pop() if len(unique) == 1 else _UNVERIFIABLE_IDENTITY


def _publish_process_groups(ledger: Path, groups: dict[int, str]) -> None:
    payload = json.dumps(
        {
            "groups": [
                {"pgid": group, "identity": groups[group]}
                for group in sorted(groups)
            ],
            "version": 1,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii") + b"\n"
    candidate = ledger.with_name(f".{ledger.name}.{os.getpid()}.tmp")
    descriptor = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            written = 0
            while written < len(payload):
                written += os.write(descriptor, payload[written:])
            os.fsync(descriptor)
        except BaseException:
            candidate.unlink(missing_ok=True)
            raise
    finally:
        os.close(descriptor)
    os.replace(candidate, ledger)


def _record_process_group(process: subprocess.Popen[bytes]) -> None:
    if _ACTIVE_PROCESS_LEDGER is None:
        return
    process_group = process.pid
    process.poll()
    if not _group_exists(process_group):
        return
    if _PROCESS_OWNER_TOKEN is None:
        raise RuntimeError("disposable process ownership was unavailable")
    _ACTIVE_PROCESS_GROUPS[process_group] = _PROCESS_OWNER_TOKEN
    _publish_process_groups(_ACTIVE_PROCESS_LEDGER, _ACTIVE_PROCESS_GROUPS)


def _forget_process_group(process_group: int) -> None:
    if _ACTIVE_PROCESS_LEDGER is None:
        return
    _ACTIVE_PROCESS_GROUPS.pop(process_group, None)
    _publish_process_groups(_ACTIVE_PROCESS_LEDGER, _ACTIVE_PROCESS_GROUPS)


def _execute_workflow_command(command: WorkflowCommand) -> None:
    process = subprocess.Popen(
        command.argv,
        cwd=command.cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=sanitized_subprocess_environment(_owned_environment()),
        close_fds=True,
        start_new_session=True,
    )
    failure: BaseException | None = None
    try:
        try:
            _record_process_group(process)
            return_code = process.wait(timeout=command.timeout)
        except BaseException as error:
            failure = error
            return_code = None
    finally:
        stopped = False
        try:
            _stop_process_group(process)
            stopped = True
        except BaseException as error:
            failure = failure or error
        if stopped:
            _forget_process_group(process.pid)
    if failure is not None or return_code != 0:
        raise RuntimeError("disposable workflow command failed") from failure


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


def _read_git(repository: Path, *arguments: str) -> str:
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            ("/usr/bin/git", "-C", str(repository), *arguments),
            cwd=repository,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            env=sanitized_subprocess_environment(_owned_environment()),
            close_fds=True,
            start_new_session=True,
        )
        failure: BaseException | None = None
        return_code: int | None = None
        try:
            try:
                _record_process_group(process)
                return_code = process.wait(timeout=30)
            except BaseException as error:
                failure = error
        finally:
            stopped = False
            try:
                _stop_process_group(process)
                stopped = True
            except BaseException as cleanup_error:
                if failure is None:
                    failure = cleanup_error
            if stopped:
                _forget_process_group(process.pid)
        if failure is not None or return_code != 0:
            raise RuntimeError("disposable Git query failed") from failure
        size = output.tell()
        if size > _MAX_GIT_OUTPUT:
            raise RuntimeError("disposable Git output exceeded its bound")
        output.seek(0)
        try:
            return output.read().decode("utf-8").strip()
        except UnicodeError as error:
            raise RuntimeError("disposable Git output was invalid") from error


def _create_disposable_guard_profile(root: Path) -> Path:
    platform = Path(root) / "host-profile-guard"
    platform.mkdir()
    (platform / "config").mkdir()
    (platform / "local_web_server").mkdir()
    (platform / "local_web_server/source.py").write_text(
        "# disposable guard\n", encoding="utf-8"
    )
    (platform / ".gitignore").write_text(
        "config/local/\n", encoding="utf-8"
    )
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
            if not _group_exists(process.pid):
                _forget_process_group(process.pid)
            self.process = None

    def _stop_process(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                _stop_process_group(process)
            else:
                process.wait()
            self._capture_error(process)
        finally:
            if not _group_exists(process.pid):
                _forget_process_group(process.pid)
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
        arguments = payload.get("ProgramArguments") if isinstance(payload, dict) else None
        working_directory = payload.get("WorkingDirectory") if isinstance(payload, dict) else None
        environment = payload.get("EnvironmentVariables") if isinstance(payload, dict) else None
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
            start_new_session=True,
        )
        self.process = process
        _record_process_group(process)
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
            self.home
            / "Library/LaunchAgents/com.sean.local-web.caddy.plist"
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
                timeout=min(float(timeout or _CADDY_COMMAND_TIMEOUT), _CADDY_COMMAND_TIMEOUT),
                maximum=_MAX_CADDY_OUTPUT,
                merge_stderr=True,
            )
        raise RuntimeError("disposable installer attempted an unexpected command")


class _DisposableThemeGate:
    def verify(self, candidate: Path) -> None:
        if not Path(candidate).is_file():
            raise RuntimeError("disposable theme candidate was unavailable")


class _BoundedBuildLog:
    def __init__(self, destination, maximum: int) -> None:
        self.destination = destination
        self.maximum = maximum
        self.written = 0
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")

    def write(self, content: bytes) -> bool:
        remaining = self.maximum - self.written
        accepted = content[:remaining]
        if accepted:
            try:
                text = self.decoder.decode(accepted, final=False)
            except UnicodeError as error:
                raise RuntimeError("disposable build output was invalid") from error
            self.destination.write(text)
            self.destination.flush()
            self.written += len(accepted)
        return len(content) <= remaining

    def finish(self) -> None:
        try:
            text = self.decoder.decode(b"", final=True)
        except UnicodeError as error:
            raise RuntimeError("disposable build output was invalid") from error
        if text:
            self.destination.write(text)
            self.destination.flush()
        self.decoder = codecs.getincrementaldecoder("utf-8")("strict")


def _run_bounded_build_command(
    arguments,
    *,
    cwd: Path,
    environment: dict[str, str],
    log: _BoundedBuildLog,
) -> subprocess.CompletedProcess[str]:
    owned_environment = sanitized_subprocess_environment(environment)
    if _PROCESS_OWNER_TOKEN is not None:
        owned_environment[_OWNER_ENVIRONMENT_NAME] = _PROCESS_OWNER_TOKEN
    process = subprocess.Popen(
        arguments,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=owned_environment,
        close_fds=True,
        start_new_session=True,
    )
    if process.stdout is None:
        raise RuntimeError("disposable build output was unavailable")
    _record_process_group(process)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout.fileno(), selectors.EVENT_READ)
    deadline = time.monotonic() + _BUILD_COMMAND_TIMEOUT
    failure: BaseException | None = None
    try:
        reached_eof = False
        while not reached_eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError("disposable build exceeded its time bound")
            events = selector.select(min(remaining, 0.1))
            if not events:
                continue
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                reached_eof = True
                continue
            if not log.write(chunk):
                raise RuntimeError("disposable build exceeded its output bound")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("disposable build exceeded its time bound")
        process.wait(timeout=remaining)
        log.finish()
    except BaseException as error:
        failure = error
    finally:
        selector.close()
        process.stdout.close()
        try:
            _stop_process_group(process)
        except BaseException as cleanup_error:
            if failure is None:
                failure = cleanup_error
        if not _group_exists(process.pid):
            _forget_process_group(process.pid)
    if failure is not None:
        raise RuntimeError("disposable production build was bounded") from failure
    return subprocess.CompletedProcess(arguments, process.returncode)


class _BuildSubprocessProxy:
    def __init__(self, original, destination) -> None:
        self.original = original
        self.destination = destination
        self.log = _BoundedBuildLog(destination, _MAX_BUILD_LOG)

    def __getattr__(self, name: str):
        return getattr(self.original, name)

    def run(self, *args, **kwargs):
        if kwargs.get("stdout") is not self.destination:
            return self.original.run(*args, **kwargs)
        if (
            len(args) != 1
            or kwargs.get("stderr") is not self.original.STDOUT
            or kwargs.get("text") is not True
            or kwargs.get("check") is not False
            or not isinstance(kwargs.get("env"), dict)
            or kwargs.get("cwd") is None
        ):
            raise RuntimeError("production builder subprocess contract changed")
        return _run_bounded_build_command(
            args[0],
            cwd=Path(kwargs["cwd"]),
            environment=kwargs["env"],
            log=self.log,
        )


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
        environment = payload.get("EnvironmentVariables", {}) if isinstance(payload, dict) else None
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

    def _execution_environment(
        self, spec: _CaddyLaunchSpec
    ) -> dict[str, str]:
        environment = _owned_environment()
        if environment is None:
            raise RuntimeError("disposable process ownership was unavailable")
        protected = {
            "HOME",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            _OWNER_ENVIRONMENT_NAME,
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
            raise RuntimeError("generated disposable Caddy routing was invalid") from error
        content = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        ) + b"\n"
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
            _stop_process_group(process)
        finally:
            if not _group_exists(process.pid):
                _forget_process_group(process.pid)
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
            start_new_session=True,
        )
        self.process = process
        _record_process_group(process)
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


def _registry_unchanged(path: Path, content: bytes, mode: int) -> bool:
    try:
        return (
            path.read_bytes() == content and stat.S_IMODE(path.stat().st_mode) == mode
        )
    except Exception:
        return False


def _reap_recorded_process_groups(
    ledger: Path, *, cleanup_timeout: float = 1.0
) -> bool:
    try:
        content = ledger.read_bytes() if ledger.exists() else b'{"groups":[],"version":1}'
        if len(content) > 8192:
            return False
        payload = json.loads(content)
        if not isinstance(payload, dict):
            return False
        entries = payload.get("groups")
        if payload.get("version") != 1 or not isinstance(entries, list):
            return False
        groups: dict[int, str] = {}
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("pgid"), int)
                or entry["pgid"] <= 0
                or not isinstance(entry.get("identity"), str)
                or not entry["identity"]
            ):
                return False
            groups[entry["pgid"]] = entry["identity"]
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    cleanup_ok = True
    for group, identity in tuple(groups.items()):
        observed = _process_identity(group)
        if observed is None:
            groups.pop(group)
            continue
        if observed == _UNVERIFIABLE_IDENTITY:
            cleanup_ok = False
            continue
        if observed != identity:
            groups.pop(group)
            continue
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            groups.pop(group)
            continue
        deadline = time.monotonic() + cleanup_timeout
        while _group_exists(group) and time.monotonic() < deadline:
            time.sleep(0.01)
        if _group_exists(group):
            observed = _process_identity(group)
            if observed is None:
                groups.pop(group)
                continue
            if observed == _UNVERIFIABLE_IDENTITY:
                cleanup_ok = False
                continue
            if observed != identity:
                groups.pop(group)
                continue
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                groups.pop(group)
                continue
            deadline = time.monotonic() + cleanup_timeout
            while _group_exists(group) and time.monotonic() < deadline:
                time.sleep(0.01)
        if _group_exists(group):
            cleanup_ok = False
        else:
            groups.pop(group)
    try:
        _publish_process_groups(ledger, groups)
    except OSError:
        return False
    return cleanup_ok


def _terminate_supervised_process(process: subprocess.Popen[bytes]) -> None:
    process_group = process.pid

    def wait_for_group(timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while _group_exists(process_group) and time.monotonic() < deadline:
            process.poll()
            time.sleep(0.01)
        return not _group_exists(process_group)

    if _group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGTERM)
        except (PermissionError, ProcessLookupError):
            pass
        if not wait_for_group(1):
            try:
                os.killpg(process_group, signal.SIGKILL)
            except (PermissionError, ProcessLookupError):
                pass
            if not wait_for_group(1):
                raise RuntimeError("disposable supervisor process cleanup failed")
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("disposable supervisor process cleanup failed") from error


def _read_pipe(descriptor: int, maximum: int) -> bytes:
    return os.read(descriptor, maximum)


def _read_supervised_output(
    process: subprocess.Popen[bytes], *, timeout: float, maximum: int
) -> tuple[bytes, bool, bool]:
    if process.stdout is None:
        raise RuntimeError("disposable supervisor output was unavailable")
    descriptor = process.stdout.fileno()
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    output = bytearray()
    timed_out = False
    overflow = False
    reached_eof = False
    try:
        while not reached_eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_supervised_process(process)
                break
            events = selector.select(min(remaining, 0.1))
            if not events:
                continue
            chunk = _read_pipe(
                descriptor, min(4096, maximum + 1 - len(output))
            )
            if not chunk:
                reached_eof = True
                break
            output.extend(chunk)
            if len(output) > maximum:
                overflow = True
                _terminate_supervised_process(process)
                break
        if not timed_out and not overflow:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _terminate_supervised_process(process)
            else:
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _terminate_supervised_process(process)
    finally:
        selector.close()
        try:
            _terminate_supervised_process(process)
        finally:
            process.stdout.close()
    return bytes(output), timed_out, overflow


def _run_bounded_process(
    arguments: tuple[str, ...],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout: float,
    maximum: int,
    merge_stderr: bool,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        arguments,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
        env=sanitized_subprocess_environment(env),
        close_fds=True,
        start_new_session=True,
    )
    _record_process_group(process)
    try:
        output, timed_out, overflow = _read_supervised_output(
            process,
            timeout=timeout,
            maximum=maximum,
        )
    finally:
        if not _group_exists(process.pid):
            _forget_process_group(process.pid)
    if timed_out:
        raise RuntimeError("disposable subprocess exceeded its time bound")
    if overflow:
        raise RuntimeError("disposable subprocess exceeded its output bound")
    try:
        text = output.decode("utf-8")
    except UnicodeError as error:
        raise RuntimeError("disposable subprocess output was invalid") from error
    return subprocess.CompletedProcess(arguments, process.returncode, text, "")


class PublicBasePathMigrationWorkflowVerifier:
    """Own one private workflow root and expose only bounded phase results."""

    def __init__(
        self,
        *,
        platform_repository: Path = PLATFORM_REPOSITORY,
        coding_root: Path = CODING_ROOT,
        live_registry: Path | None = None,
        supervised_command_factory: SupervisedCommandFactory | None = None,
        external_data: Path | None = None,
        failure_phase: str | None = None,
        scenario: str = "normal",
        supervision_timeout: float = 900.0,
        emit: Emitter = print,
    ) -> None:
        self._platform_repository = Path(platform_repository).resolve()
        self._coding_root = Path(coding_root).resolve()
        self._live_registry = None if live_registry is None else Path(live_registry)
        self._supervised_command_factory = supervised_command_factory
        self._external_data = (
            Path(external_data).resolve(strict=True)
            if external_data is not None
            else None
        )
        if failure_phase is not None and failure_phase not in PHASES:
            raise ValueError("disposable failure phase was invalid")
        self._failure_phase = failure_phase
        if scenario not in _SCENARIOS:
            raise ValueError("disposable scenario was invalid")
        self._scenario = scenario
        self._supervision_timeout = supervision_timeout
        self._emit = emit

    def run(self) -> int:
        temporary: tempfile.TemporaryDirectory[str] | None = None
        external_temporary: tempfile.TemporaryDirectory[str] | None = None
        live_state: tuple[object, ...] | None = None
        external_state: tuple[object, ...] | None = None
        external_data = self._external_data
        result = 0
        process_ledger: Path | None = None
        worker_cleanup_ok: bool | None = None
        supervisor_needs_reap = False
        owner_token: str | None = None
        live_registry = self._live_registry
        owns_registry = live_registry is None
        try:
            temporary = tempfile.TemporaryDirectory(
                prefix=".local-web-public-base-path-migration-",
                dir=self._coding_root,
            )
            root = Path(temporary.name).resolve(strict=True)
            if live_registry is None:
                live_registry = _create_disposable_guard_profile(root)
            live_state = _file_state(live_registry)
            if external_data is None:
                external_temporary = tempfile.TemporaryDirectory(
                    prefix=".local-web-public-base-path-external-",
                    dir=self._coding_root,
                )
                external_data = Path(external_temporary.name) / "state.json"
                external_data.write_bytes(_DATA_SENTINEL)
                external_data.chmod(0o640)
            external_state = _file_state(external_data)
            process_ledger = root / "process-groups"
            supervisor_needs_reap = True
            if self._supervised_command_factory is None:
                artifact_path = root / "local-web-ui.tgz"
                artifact = build_ui_package(
                    self._platform_repository, artifact_path
                )
                if artifact.path != artifact_path:
                    raise RuntimeError(
                        "disposable UI fixture artifact was invalid"
                    )
                command = (
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    str(root),
                    str(self._platform_repository),
                    str(live_registry),
                    str(external_data),
                    self._failure_phase or "-",
                    self._scenario,
                )
            else:
                command = self._supervised_command_factory(
                    root, self._platform_repository, live_registry
                )
            owner_token = secrets.token_hex(32)
            worker_environment = {
                **_private_execution_environment(root),
                _OWNER_ENVIRONMENT_NAME: owner_token,
            }
            process = subprocess.Popen(
                command,
                cwd=self._platform_repository,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=sanitized_subprocess_environment(worker_environment),
                close_fds=True,
                start_new_session=True,
            )
            output, timed_out, output_overflow = _read_supervised_output(
                process,
                timeout=self._supervision_timeout,
                maximum=_MAX_CLI_OUTPUT,
            )
            protocol = (
                [] if output_overflow else output.decode("ascii").splitlines()
            )
            completed = 0
            protocol_valid = not output_overflow
            for line in protocol:
                if (
                    worker_cleanup_ok is None
                    and line == f"PASS {completed}"
                    and completed < len(PHASES) - 1
                ):
                    if not _immutable_unchanged(
                        live_registry, live_state
                    ) or not _immutable_unchanged(external_data, external_state):
                        protocol_valid = False
                        break
                    self._emit(f"{PHASES[completed]} PASS")
                    completed += 1
                elif worker_cleanup_ok is None and line in {
                    "CLEAN PASS",
                    "CLEAN FAIL",
                }:
                    worker_cleanup_ok = line == "CLEAN PASS"
                else:
                    protocol_valid = False
                    break
            operational_failed = (
                timed_out
                or not protocol_valid
                or completed != len(PHASES) - 1
            )
            if operational_failed:
                self._emit(f"{PHASES[completed]} FAIL")
                result = 1
            elif process.returncode != 0 and worker_cleanup_ok is not False:
                worker_cleanup_ok = False
            if worker_cleanup_ok is False:
                result = 1
            supervisor_needs_reap = not (
                worker_cleanup_ok is True
                and protocol_valid
                and not timed_out
            )
        except BaseException:
            self._emit(f"{PHASES[0]} FAIL")
            result = 1
        finally:
            cleanup_ok = True
            registry_ok = False
            if owns_registry and live_state is not None:
                registry_ok = _immutable_unchanged(live_registry, live_state)
            if worker_cleanup_ok is False:
                cleanup_ok = False
            if temporary is not None:
                if process_ledger is not None and supervisor_needs_reap:
                    cleanup_ok = (
                        _reap_recorded_process_groups(process_ledger) and cleanup_ok
                    )
                if owner_token is not None:
                    cleanup_ok = _reap_owned_processes(owner_token) and cleanup_ok
                try:
                    temporary.cleanup()
                    cleanup_ok = cleanup_ok and not Path(temporary.name).exists()
                except BaseException:
                    cleanup_ok = False
            if live_state is None or external_state is None or external_data is None:
                cleanup_ok = False
            else:
                if not owns_registry:
                    registry_ok = _immutable_unchanged(
                        live_registry, live_state
                    )
                cleanup_ok = (
                    cleanup_ok
                    and registry_ok
                    and _immutable_unchanged(external_data, external_state)
                )
            if external_temporary is not None:
                try:
                    external_temporary.cleanup()
                    cleanup_ok = cleanup_ok and not Path(
                        external_temporary.name
                    ).exists()
                except BaseException:
                    cleanup_ok = False
            self._emit(f"{PHASES[-1]} {'PASS' if cleanup_ok else 'FAIL'}")
            if not cleanup_ok:
                result = 1
        return result


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
        ui_metadata = json.loads(
            (self.platform / "packages/ui/package.json").read_text(encoding="utf-8")
        )
        ui_version = ui_metadata.get("version")
        if not isinstance(ui_version, str):
            raise RuntimeError("disposable UI fixture version was unavailable")
        artifact = self.root / "local-web-ui.tgz"
        artifact_content = artifact.read_bytes()
        artifact_digest = hashlib.sha256(artifact_content).hexdigest()
        inputs = TemplateInputs(
            app_id=_APP_ID,
            title="Public Base Path Fixture",
            route=_ROUTE,
            icon="database",
            accent="#76D39B",
            ui_version=ui_version,
            ui_sha256=artifact_digest,
            capabilities=(),
            kind="service",
        )
        for rendered in render_generated_repository(inputs):
            destination = self.repository / rendered.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered.content, encoding="utf-8")
        vendor = self.repository / "vendor/local-web-ui.tgz"
        vendor.parent.mkdir(exist_ok=True)
        shutil.copyfile(artifact, vendor)
        (self.repository / ".local-web-platform.json").write_bytes(
            render_provenance(
                AppProvenance(
                    schema_version=1,
                    template_version=CURRENT_TEMPLATE_VERSION,
                    platform_contract_version=SUPPORTED_PLATFORM_CONTRACT,
                    ui=UiArtifactReference(ui_version, artifact_digest),
                    capabilities=(),
                    domain_palette_tokens=(),
                    managed_files=(
                        Path("AGENTS.md"),
                        Path("local-web.json"),
                        Path("vendor/local-web-ui.tgz"),
                    ),
                )
            )
        )

    def _prepare_old_service_commit(self) -> None:
        manifest_path = self.repository / "local-web.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["route"] = _ROUTE
        manifest["healthPath"] = f"{_ROUTE}/healthz"
        manifest["build"] = {
            "commands": [
                ["/usr/bin/env", "python3", "scripts/build_fixture.py"]
            ],
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
        (self.repository / "build-version.txt").write_text(
            "old\n", encoding="utf-8"
        )
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
        ).publish_public_base_path_restoration(
            _APP_ID, before, content
        )
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
            or dict(registered.environment)
            != {"VITE_PUBLIC_BASE_PATH": f"{_ROUTE}/"}
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
            or dict(host.environment)
            != {"VITE_PUBLIC_BASE_PATH": f"{_ROUTE}/"}
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
            raise RuntimeError(
                "disposable hosted build did not receive its base path"
            )

    def _verify_served_assets(self, version: str) -> None:
        index = _read_http_bytes(f"http://{self.caddy.host}{_ROUTE}/").decode(
            "utf-8"
        )
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
            or [record.version for record in new_records]
            != ["retry", "candidate"]
            or not new_records[0].release_ready
            or new_records[0].public_base_path != f"{_ROUTE}/"
        ):
            raise RuntimeError("disposable post-build failure was not exercised")
        if _group_exists(new_records[0].process_group):
            raise RuntimeError("failed disposable process group remained alive")
        if (
            self.registry_path.read_bytes() != former_registry
            or stat.S_IMODE(self.registry_path.stat().st_mode)
            != former_registry_mode
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
            or profile_revisions[-1].operation
            != PUBLIC_BASE_PATH_RESTORATION
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
            or dict(host.environment)
            != {"VITE_PUBLIC_BASE_PATH": f"{_ROUTE}/"}
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
            _group_exists(group)
            for group in (*self.services.process_groups, *self.caddy.process_groups)
        ):
            failures.append(RuntimeError("disposable process cleanup was incomplete"))
        if failures:
            raise RuntimeError("disposable workflow cleanup failed") from failures[0]


def _worker_main(
    root: Path,
    platform: Path,
    live_registry: Path,
    external_data: Path,
    failure_phase: str | None,
    scenario: str,
) -> int:
    global _ACTIVE_PROCESS_LEDGER, _ACTIVE_PROCESS_GROUPS
    global _PRIVATE_EXECUTION_ENVIRONMENT, _PROCESS_OWNER_TOKEN
    owner = os.environ.get(_OWNER_ENVIRONMENT_NAME)
    if owner is None or re.fullmatch(r"[0-9a-f]{64}", owner) is None:
        return 1
    try:
        root = root.resolve(strict=True)
        private_environment = _private_execution_environment(root)
    except OSError:
        return 1
    os.environ.clear()
    os.environ.update(
        {
            **private_environment,
            _OWNER_ENVIRONMENT_NAME: owner,
        }
    )
    _PRIVATE_EXECUTION_ENVIRONMENT = private_environment
    tempfile.tempdir = private_environment["TMPDIR"]
    _ACTIVE_PROCESS_LEDGER = root / "process-groups"
    _ACTIVE_PROCESS_GROUPS = {}
    _PROCESS_OWNER_TOKEN = owner
    _publish_process_groups(_ACTIVE_PROCESS_LEDGER, _ACTIVE_PROCESS_GROUPS)
    workflow: DisposablePublicBasePathMigrationWorkflow | None = None
    cleanup_ok = True
    try:
        live_state = _file_state(live_registry)
        external_state = _file_state(external_data)
        workflow = DisposablePublicBasePathMigrationWorkflow(
            root,
            platform,
            external_data,
            scenario=scenario,
        )
        for index, phase in enumerate(PHASES[:-1]):
            workflow.run_phase(phase)
            if not _immutable_unchanged(
                live_registry, live_state
            ) or not _immutable_unchanged(external_data, external_state):
                raise RuntimeError("immutable sentinel changed")
            if failure_phase == phase:
                raise RuntimeError("injected disposable phase failure")
            print(f"PASS {index}", flush=True)
    except BaseException:
        return_code = 1
    else:
        return_code = 0
    finally:
        if workflow is not None:
            try:
                workflow.close()
            except BaseException:
                cleanup_ok = False
        cleanup_ok = cleanup_ok and not _ACTIVE_PROCESS_GROUPS
        if failure_phase == PHASES[-1]:
            cleanup_ok = False
        print(f"CLEAN {'PASS' if cleanup_ok else 'FAIL'}", flush=True)
    return return_code if cleanup_ok else 1


def main() -> int:
    if len(sys.argv) == 8 and sys.argv[1] == "--worker":
        failure_phase = None if sys.argv[6] == "-" else sys.argv[6]
        if failure_phase is not None and failure_phase not in PHASES:
            return 1
        if sys.argv[7] not in _SCENARIOS:
            return 1
        return _worker_main(
            Path(sys.argv[2]),
            Path(sys.argv[3]),
            Path(sys.argv[4]),
            Path(sys.argv[5]),
            failure_phase,
            sys.argv[7],
        )
    return PublicBasePathMigrationWorkflowVerifier().run()


if __name__ == "__main__":
    raise SystemExit(main())
