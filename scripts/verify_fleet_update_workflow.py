#!/usr/bin/env python3
"""Exercise fleet-update boundaries in a disposable, host-private workspace."""

from __future__ import annotations

import json
import hashlib
import io
import os
import gzip
import selectors
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_PLATFORM_REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PLATFORM_REPOSITORY))

from local_web_server.app_activation import AppActivationPlan, AppActivationResult
from local_web_server.app_check import AppChecker
from local_web_server.app_provenance import (
    CURRENT_TEMPLATE_VERSION,
    AppProvenance,
    UiArtifactReference,
    render_provenance,
)
from local_web_server.app_update import AppUpdater, NpmLockfileBuilder
from local_web_server.deploy import DeploymentResult
from local_web_server.fleet_update import FleetUpdater
from local_web_server.release_outcome import validate_release_outcome
from local_web_server.config import load_manifest
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore
from local_web_server.service_command import expand_service_command_template
from local_web_server.runtime import RuntimeLayout, atomic_symlink
from local_web_server.ui_package import UiPackageArtifact, build_ui_package
from scripts.disposable_workflow_support import (
    initialise_disposable_host_profile,
    sanitized_subprocess_environment,
)


EXPECTED_PHASES = (
    "create disposable fleet PASS",
    "preview complete fleet PASS",
    "prove preview purity PASS",
    "apply compatible apps PASS",
    "verify focused app commits PASS",
    "verify exact live releases PASS",
    "classify unsupported app PASS",
    "recover injected app failure PASS",
    "prove aggregate result PASS",
    "cleanup PASS",
)

_MAX_OUTPUT = 4096
_TIMEOUT = 0.2
_CHECK_PORT = 43129
_PRIVATE_MARKER = "PRIVATE-FLEET-WORKFLOW-DETAIL"
_LAST_PROBE_PROCESS_GROUP: int | None = None
_LAST_PROBE_DESCENDANT: int | None = None
_STOPPED_SERVICE_PROCESS_GROUPS: list[int] = []
_PLATFORM_OWNED = (
    Path(".local-web-platform.json"),
    Path("index.html"),
    Path("local-web.json"),
    Path("package-lock.json"),
    Path("package.json"),
    Path("vendor/local-web-ui.tgz"),
)


@dataclass(frozen=True)
class _RecoverySnapshot:
    head: str
    tree: str
    platform: tuple[tuple[Path, bytes | None], ...]


@dataclass(frozen=True)
class _CheckedService:
    """The immutable service activation inputs accepted by the real checker."""

    command_template: tuple[str, ...]
    composed_command: tuple[str, ...]
    working_directory: Path
    health_path: str
    port: int


@dataclass(frozen=True)
class _CheckedRelease:
    """Checker evidence which an activation may consume exactly once unchanged."""

    head: str
    manifest_digest: str
    app_id: str
    route: str
    kind: str
    release: Path
    fingerprint: str
    service: _CheckedService | None


class RecordingTemporaryDirectory:
    """Factory adapter used by tests to retain only temporary-root evidence."""

    def __init__(self, roots: list[Path]) -> None:
        self._roots = roots

    def __call__(self, *args, **kwargs):
        temporary = tempfile.TemporaryDirectory(*args, **kwargs)
        self._roots.append(Path(temporary.name))
        return temporary


def _group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _stop_group(process: subprocess.Popen[bytes]) -> None:
    """Always terminate and reap the session, including after its leader exits."""
    process_group = process.pid
    for signal_value in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process_group, signal_value)
        except ProcessLookupError:
            break
        except PermissionError:
            # Darwin can reject a zero-signal probe for an already-reaped
            # session; a signal attempt remains the authoritative boundary.
            break
        deadline = time.monotonic() + 0.5
        while _group_exists(process_group) and time.monotonic() < deadline:
            try:
                process.wait(timeout=0.02)
            except subprocess.TimeoutExpired:
                pass
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass


@dataclass(frozen=True)
class _BoundedCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


def _run_bounded_command(
    command: tuple[str, ...], cwd: Path, *, timeout: float = 10.0, maximum: int = _MAX_OUTPUT,
    environment: dict[str, str] | None = None, on_started: Callable[[int], None] | None = None,
) -> _BoundedCommandResult:
    """Run a private CLI child with incremental bounded output and group cleanup."""
    process = subprocess.Popen(
        command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True,
        env=sanitized_subprocess_environment(environment),
    )
    if on_started is not None:
        on_started(process.pid)
    selector = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        if process.stdout is None or process.stderr is None:
            raise RuntimeError(_PRIVATE_MARKER)
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            for key, _event in selector.select(min(remaining, 0.05)):
                chunk = os.read(key.fileobj.fileno(), 1024)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output[key.data].extend(chunk)
                if sum(len(value) for value in output.values()) > maximum:
                    raise OverflowError
        process.wait(timeout=max(deadline - time.monotonic(), 0.01))
        return _BoundedCommandResult(process.returncode, bytes(output["stdout"]), bytes(output["stderr"]))
    finally:
        _stop_group(process)
        selector.close()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def run_bounded_probe(outcome: str) -> str:
    """Exercise the exact bounded runner with an inherited-pipe descendant."""

    with tempfile.TemporaryDirectory(prefix="local-web-fleet-probe-") as text:
        pid_file = Path(text) / "child.pid"
        child = "import time; time.sleep(60)"
        spawn = (
            "import subprocess,sys; from pathlib import Path; "
            f"p=subprocess.Popen([sys.executable,'-c',{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
            f"Path({str(pid_file)!r}).write_text(str(p.pid)); "
        )
        choices = {
            "success": (spawn + "sys.exit(0)", _TIMEOUT, _MAX_OUTPUT, None),
            "nonzero": (spawn + "sys.exit(7)", _TIMEOUT, _MAX_OUTPUT, 7),
            "timeout": (spawn + "import time; time.sleep(60)", _TIMEOUT, _MAX_OUTPUT, TimeoutError),
            "overflow": (spawn + "sys.stdout.write('x' * 8192); sys.stdout.flush()", _TIMEOUT, _MAX_OUTPUT, OverflowError),
            "failure": (spawn + "raise RuntimeError('private')", _TIMEOUT, _MAX_OUTPUT, 1),
        }
        if outcome not in choices:
            raise ValueError("invalid disposable probe")
        program, timeout, maximum, expected = choices[outcome]
        command = (sys.executable, "-c", program)
        try:
            result = _run_bounded_command(
                command, Path.cwd(), timeout=timeout, maximum=maximum,
                on_started=lambda process_group: _set_probe_process_group(process_group),
            )
        except (TimeoutError, OverflowError) as error:
            if expected is not type(error):
                raise RuntimeError("disposable child outcome was invalid") from error
        else:
            if expected is not None and result.returncode != expected:
                raise RuntimeError("disposable child outcome was invalid")
            if outcome == "success" and result.returncode != 0:
                raise RuntimeError("disposable child outcome was invalid")
        try:
            _set_probe_descendant(int(pid_file.read_text(encoding="utf-8")))
        except (OSError, ValueError) as error:
            raise RuntimeError("disposable child outcome was invalid") from error
    return outcome


def last_probe_process_group() -> int:
    if _LAST_PROBE_PROCESS_GROUP is None:
        raise RuntimeError("disposable process group was unavailable")
    return _LAST_PROBE_PROCESS_GROUP


def _set_probe_process_group(process_group: int) -> None:
    global _LAST_PROBE_PROCESS_GROUP
    _LAST_PROBE_PROCESS_GROUP = process_group


def _set_probe_descendant(process_identifier: int) -> None:
    global _LAST_PROBE_DESCENDANT
    _LAST_PROBE_DESCENDANT = process_identifier


def probe_process_group_reaped() -> bool:
    return not _group_exists(last_probe_process_group())


def probe_descendant_reaped() -> bool:
    if _LAST_PROBE_DESCENDANT is None:
        raise RuntimeError("disposable process was unavailable")
    deadline = time.monotonic() + 0.5
    while True:
        try:
            os.kill(_LAST_PROBE_DESCENDANT, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


class _RecordingChecker(AppChecker):
    """Real structural/app-local/release checking with private release evidence."""

    def __init__(self, checked: dict[Path, _CheckedRelease]) -> None:
        super().__init__()
        self._checked = checked

    def check(self, repository: Path):
        report = super().check(repository)
        root = Path(repository).resolve(strict=True)
        manifest_path = root / "local-web.json"
        manifest_bytes = manifest_path.read_bytes()
        manifest = load_manifest(manifest_path)
        release = root / manifest.build.output
        validate_release_outcome(root, manifest, release)
        if manifest_path.read_bytes() != manifest_bytes:
            raise RuntimeError(_PRIVATE_MARKER)
        self._checked[root] = _CheckedRelease(
            _git(root, "rev-parse", "HEAD"),
            hashlib.sha256(manifest_bytes).hexdigest(),
            manifest.id,
            manifest.route,
            manifest.kind,
            release,
            _release_fingerprint(release),
            _checked_service(root, release, manifest),
        )
        return report


class _PrivateHttpService:
    """A disposable loopback service with bounded startup and group cleanup."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[bytes] | None = None
        self.port: int | None = None
        self._identity: _CheckedService | None = None

    @property
    def process_group(self) -> int | None:
        return self.process.pid if self.process is not None else None

    def ensure_running(self, identity: _CheckedService, repository: Path) -> None:
        if self.process is not None and self.process.poll() is None:
            if self._identity != identity:
                raise RuntimeError(_PRIVATE_MARKER)
            self.check(identity)
            return
        port = _private_loopback_port()
        try:
            if expand_service_command_template(
                identity.command_template,
                port=identity.port,
                release=identity.working_directory,
                repository=repository,
            ) != identity.composed_command:
                raise RuntimeError(_PRIVATE_MARKER)
            rendered = expand_service_command_template(
                identity.command_template,
                port=port,
                release=identity.working_directory,
                repository=repository,
            )
        except ValueError:
            raise RuntimeError(_PRIVATE_MARKER)
        process = subprocess.Popen(
            rendered, cwd=identity.working_directory, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
            env=sanitized_subprocess_environment(),
        )
        selector = selectors.DefaultSelector()
        try:
            if process.stdout is None or process.stderr is None:
                raise RuntimeError(_PRIVATE_MARKER)
            selector.register(process.stdout, selectors.EVENT_READ)
            selector.register(process.stderr, selectors.EVENT_READ)
            deadline = time.monotonic() + 2
            output = bytearray()
            while time.monotonic() < deadline:
                for key, _event in selector.select(0.05):
                    output.extend(os.read(key.fileobj.fileno(), 1024))
                    if len(output) > _MAX_OUTPUT:
                        raise RuntimeError(_PRIVATE_MARKER)
                if process.poll() is not None:
                    raise RuntimeError(_PRIVATE_MARKER)
                self.process = process
                self.port = port
                self._identity = identity
                try:
                    self.check(identity)
                except (OSError, urllib.error.URLError):
                    continue
                return
            raise RuntimeError(_PRIVATE_MARKER)
        except BaseException:
            _stop_group(process)
            raise
        finally:
            selector.close()
            if process.stdout is not None and self.process is not process:
                process.stdout.close()
            if process.stderr is not None and self.process is not process:
                process.stderr.close()

    def check(self, identity: _CheckedService) -> None:
        if self.port is None or self._identity != identity:
            raise RuntimeError(_PRIVATE_MARKER)
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}{identity.health_path}", timeout=1
        ) as response:
            if response.status != 204:
                raise RuntimeError(_PRIVATE_MARKER)

    def close(self) -> None:
        if self.process is None:
            return
        _STOPPED_SERVICE_PROCESS_GROUPS.append(self.process.pid)
        _stop_group(self.process)
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()
        self.process = None
        self.port = None
        self._identity = None


def stopped_service_process_groups() -> tuple[int, ...]:
    return tuple(_STOPPED_SERVICE_PROCESS_GROUPS)


class _PrivateActivator:
    """Host-facing-only seam which proves it deploys the checked real release."""

    def __init__(self, *, fail_repository: Path, releases: dict[Path, str], checked: dict[Path, _CheckedRelease], service: _PrivateHttpService) -> None:
        self._fail_repository = fail_repository
        self._releases = releases
        self._checked = checked
        self._service = service
        self._failed = False

    def preview(self, repository: Path) -> AppActivationPlan:
        manifest = json.loads((repository / "local-web.json").read_text(encoding="utf-8"))
        return AppActivationPlan(manifest["id"], manifest["route"], manifest["kind"], None, False)

    def activate(
        self,
        repository: Path,
        *,
        expected_plan: AppActivationPlan | None = None,
        expected_source_commit: str | None = None,
        expected_former_live_commit: str | None = None,
    ) -> AppActivationResult:
        root = Path(repository).resolve(strict=True)
        checked = self._checked.get(root)
        if checked is None:
            raise RuntimeError(_PRIVATE_MARKER)
        manifest_path = root / "local-web.json"
        manifest_bytes = manifest_path.read_bytes()
        if hashlib.sha256(manifest_bytes).hexdigest() != checked.manifest_digest:
            raise RuntimeError(_PRIVATE_MARKER)
        manifest = load_manifest(manifest_path)
        release = root / manifest.build.output
        service = _checked_service(root, release, manifest)
        if (
            manifest_path.read_bytes() != manifest_bytes
            or _git(root, "rev-parse", "HEAD") != checked.head
            or manifest.id != checked.app_id
            or manifest.route != checked.route
            or manifest.kind != checked.kind
            or release != checked.release
            or _release_fingerprint(release) != checked.fingerprint
            or service != checked.service
        ):
            raise RuntimeError(_PRIVATE_MARKER)
        validate_release_outcome(root, manifest, release)
        plan = AppActivationPlan(
            checked.app_id, checked.route, checked.kind, None, False
        )
        if expected_plan is not None and plan != expected_plan:
            raise RuntimeError(_PRIVATE_MARKER)
        commit = _git(repository, "rev-parse", "HEAD")
        if (
            (expected_source_commit is not None and commit != expected_source_commit)
            or (
                expected_former_live_commit is not None
                and self._releases.get(root) != expected_former_live_commit
            )
        ):
            raise RuntimeError(_PRIVATE_MARKER)
        if root == self._fail_repository.resolve() and not self._failed:
            self._failed = True
            raise RuntimeError(_PRIVATE_MARKER)
        if checked.kind == "service":
            if service is None:
                raise RuntimeError(_PRIVATE_MARKER)
            self._service.ensure_running(service, root)
        self._releases[root] = commit
        return AppActivationResult(
            plan,
            None,
            DeploymentResult(plan.app_id, commit, "deployed", "deployed", None),
            True,
        )


class _PrivateLiveInspector:
    def __init__(self, apps: Path, releases: dict[Path, str]) -> None:
        self._apps = apps
        self._releases = releases

    def inspect(self, _runtime_root: Path, app_id: str) -> str | None:
        return self._releases.get((self._apps / app_id).resolve())


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", *arguments), cwd=repository, check=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        env=sanitized_subprocess_environment(),
    ).stdout.strip()


def _release_fingerprint(release: Path) -> str:
    """Fingerprint every immutable regular file in the composed release."""
    entries: list[bytes] = []
    try:
        for candidate in sorted(release.rglob("*"), key=lambda item: item.as_posix()):
            if candidate.is_dir():
                continue
            if candidate.is_symlink() or not candidate.is_file():
                raise OSError
            relative = candidate.relative_to(release).as_posix().encode("utf-8")
            entries.extend((relative, b"\0", hashlib.sha256(candidate.read_bytes()).hexdigest().encode("ascii"), b"\n"))
    except (OSError, ValueError):
        raise RuntimeError(_PRIVATE_MARKER) from None
    return hashlib.sha256(b"".join(entries)).hexdigest()


def _checked_service(root: Path, release: Path, manifest) -> _CheckedService | None:
    """Capture the exact checked command, cwd and health semantics for a service."""
    if manifest.kind != "service":
        return None
    service = manifest.service
    if service is None or service.start_command is None:
        raise RuntimeError(_PRIVATE_MARKER)
    try:
        composed = expand_service_command_template(
            service.start_command.argv,
            port=_CHECK_PORT,
            release=release,
            repository=root,
        )
    except ValueError:
        raise RuntimeError(_PRIVATE_MARKER) from None
    return _CheckedService(
        service.start_command.argv,
        composed,
        release,
        service.internal_health_path,
        _CHECK_PORT,
    )


def _private_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _DisposableFleet:
    """A real FleetUpdater run with only its host-facing seams private."""

    _READY = ("ready-static", "ready-service", "failing-static", "later-static")

    def __init__(self, root: Path, *, require_cli_preview: bool = True) -> None:
        self.root = Path(root).resolve()
        self.platform = self.root / "platform"
        self.registry = self.platform / "config/local/apps.json"
        self.runtime = self.root / "runtime"
        self.apps = self.root / "apps"
        self._snapshot: tuple[bytes, bytes] | None = None
        self.releases: dict[Path, str] = {}
        self.checked: dict[Path, _CheckedRelease] = {}
        self.service = _PrivateHttpService()
        self.coordinator: FleetUpdater | None = None
        self.result = None
        self._legacy_head = ""
        self._current_artifact: UiPackageArtifact | None = None
        self._current_artifact_bytes = b""
        self._current_lock = b""
        self._old_lock = b""
        self._cli_statuses: tuple[tuple[str, str], ...] = ()
        self._require_cli_preview = require_cli_preview
        self._recovery_original: _RecoverySnapshot | None = None
        self.activator: _PrivateActivator | None = None

    @property
    def checks_ran(self) -> bool:
        return len(self.checked) >= len(self._READY)

    def create(self) -> None:
        subprocess.run(
            (
                "git",
                "clone",
                "--no-local",
                str(_PLATFORM_REPOSITORY),
                str(self.platform),
            ),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=sanitized_subprocess_environment(),
        )
        _git(self.platform, "switch", "-C", "main")
        exclude = self.platform / ".git/info/exclude"
        exclude.write_text(
            exclude.read_text(encoding="utf-8") + "\nconfig/local/\n",
            encoding="utf-8",
        )
        self.runtime.mkdir()
        self.apps.mkdir()
        artifact_path = self.root / "current-ui.tgz"
        self._current_artifact = self._build_real_artifact(artifact_path)
        self._current_artifact_bytes = artifact_path.read_bytes()
        self._current_lock = NpmLockfileBuilder().build(self._package_bytes(), None, self._current_artifact_bytes)
        self._old_lock = NpmLockfileBuilder().build(self._package_bytes(), None, self._old_artifact())
        for app_id in ("current-static", *self._READY, "legacy"):
            self._create_app(app_id, service=app_id == "ready-service", legacy=app_id == "legacy")
        self._write_registry()
        self._snapshot = (self.registry.read_bytes(), (self.runtime / "sentinel").read_bytes())
        activator = _PrivateActivator(
            fail_repository=self.apps / "failing-static", releases=self.releases, checked=self.checked, service=self.service,
        )
        self.activator = activator
        self._recovery_original = self._snapshot_repository(
            self.apps / "failing-static"
        )
        self.coordinator = FleetUpdater(
            updater_factory=lambda: AppUpdater(platform_repository=self.platform, artifact_builder=self._build_cached_artifact),
            checker_factory=lambda: _RecordingChecker(self.checked),
            activator_factory=lambda _registry: activator,
            live_inspector=_PrivateLiveInspector(self.apps, self.releases),
        )

    def _build_cached_artifact(self, _repository: Path, output: Path) -> UiPackageArtifact:
        if self._current_artifact is None:
            raise RuntimeError(_PRIVATE_MARKER)
        shutil.copyfile(self._current_artifact.path, output)
        return UiPackageArtifact(self._current_artifact.version, self._current_artifact.sha256, output)

    def _build_real_artifact(self, output: Path) -> UiPackageArtifact:
        previous = tempfile.tempdir
        try:
            tempfile.tempdir = str(self.root)
            return build_ui_package(self.platform, output)
        finally:
            tempfile.tempdir = previous

    @staticmethod
    def _package_bytes() -> bytes:
        return (json.dumps({
            "name": "disposable-app", "private": True, "version": "0.0.0", "type": "module",
            "scripts": {name: "node -e \"\"" for name in ("dev", "build", "lint", "test", "test:e2e", "check")},
            "dependencies": {"@local-web/ui": "file:vendor/local-web-ui.tgz"},
        }, sort_keys=True, indent=2) + "\n").encode("utf-8")

    @staticmethod
    def _old_artifact() -> bytes:
        payload = json.dumps({"name": "@local-web/ui", "version": "0.6.0"}, sort_keys=True).encode("utf-8")
        buffer = io.BytesIO()
        with gzip.GzipFile(fileobj=buffer, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                member = tarfile.TarInfo("package/package.json")
                member.size = len(payload)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(payload))
        return buffer.getvalue()

    def _create_app(self, app_id: str, *, service: bool, legacy: bool) -> None:
        repository = self.apps / app_id
        repository.mkdir()
        manifest: dict[str, object] = {
            "schemaVersion": 1, "id": app_id, "title": app_id,
            "route": f"/{app_id}", "kind": "service" if service else "static",
            "build": {"commands": [["/usr/bin/true"]], "output": "release" if service else "dist", "environment": []},
            "healthPath": f"/{app_id}/healthz" if service else f"/{app_id}/",
            "home": {"icon": "chart-line", "accent": "#D9467A"},
        }
        if service:
            manifest["build"] = {
                "commands": [["/usr/bin/true"]], "output": "release", "environment": [],
                "release": [{"source": "dist", "target": "public"}, {"source": "server", "target": "server"}],
            }
            manifest["service"] = {
                "module": "server/service.py", "internalHealthPath": "/healthz",
                "frontendOutput": "public", "proxyPaths": ["/api"],
                "startCommand": ["/usr/bin/env", "python3", "{release}/server/service.py", "--port", "{port}"],
            }
            (repository / "server").mkdir()
            (repository / "server/service.py").write_text(
                "import argparse\n"
                "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('--port', type=int, required=True)\n"
                "port = parser.parse_args().port\n"
                "class Health(BaseHTTPRequestHandler):\n"
                " def do_GET(self):\n"
                "  self.send_response(204 if self.path == '/healthz' else 404)\n"
                "  self.end_headers()\n"
                " def log_message(self, *args): pass\n"
                "ThreadingHTTPServer(('127.0.0.1', port), Health).serve_forever()\n",
                encoding="utf-8",
            )
        if not legacy:
            current = app_id == "current-static"
            template_version = CURRENT_TEMPLATE_VERSION if current else 1
            artifact = self._current_artifact_bytes if current else self._old_artifact()
            version = self._current_artifact.version if current and self._current_artifact is not None else "0.6.0"
            digest = hashlib.sha256(artifact).hexdigest()
            manifest["platform"] = {
                "contractVersion": 1, "templateVersion": template_version, "uiVersion": version, "capabilities": [],
            }
            (repository / ".local-web-platform.json").write_bytes(render_provenance(AppProvenance(
                1, template_version, 1, UiArtifactReference(version, digest), (), (), (Path("local-web.json"), Path("vendor/local-web-ui.tgz")),
            )))
            (repository / "vendor").mkdir()
            (repository / "vendor/local-web-ui.tgz").write_bytes(artifact)
            (repository / "package.json").write_bytes(self._package_bytes())
            (repository / "package-lock.json").write_bytes(self._current_lock if current else self._old_lock)
            (repository / "index.html").write_text('<!doctype html><html><head><link rel="stylesheet" href="/_local-web/platform/theme.css"></head><body></body></html>\n', encoding="utf-8")
            frontend = repository / "dist"
            frontend.mkdir(parents=True)
            (frontend / "index.html").write_text('<!doctype html><html><head><link rel="stylesheet" href="/_local-web/platform/theme.css"></head><body></body></html>\n', encoding="utf-8")
            if service:
                release = repository / "release"
                (release / "public").mkdir(parents=True)
                (release / "public" / "index.html").write_bytes((repository / "dist" / "index.html").read_bytes())
                (release / "server").mkdir()
                (release / "server" / "service.py").write_bytes((repository / "server" / "service.py").read_bytes())
        (repository / "local-web.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        _git(repository, "init", "--initial-branch=main")
        _git(repository, "add", "--all")
        _git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@invalid", "commit", "-m", "fixture")
        head = _git(repository, "rev-parse", "HEAD")
        self.releases[repository.resolve()] = head
        layout = RuntimeLayout(self.runtime, app_id)
        release = layout.releases / head
        release.mkdir(parents=True, exist_ok=True)
        atomic_symlink(layout.current, release)
        if legacy:
            self._legacy_head = head

    def _write_registry(self) -> None:
        apps = [
            {"id": app_id, "repository": str(self.apps / app_id), "autoDeploy": True}
            for app_id in ("current-static", *self._READY, "legacy")
        ]
        content = (json.dumps({
            "schemaVersion": 1, "host": "fleet.test", "runtimeRoot": str(self.runtime), "apps": apps,
        }) + "\n").encode("utf-8")
        if (
            initialise_disposable_host_profile(self.platform, content)
            != self.registry
        ):
            raise RuntimeError(_PRIVATE_MARKER)
        (self.runtime / "sentinel").write_bytes(b"private runtime sentinel\n")

    def preview(self) -> None:
        expected = (
            ("current-static", "CURRENT"),
            ("ready-static", "READY"),
            ("legacy", "SKIPPED"),
        )
        if self._require_cli_preview:
            registry_before = self.registry.read_bytes()
            payload = json.loads(registry_before)
            payload["apps"] = [
                app
                for app in payload["apps"]
                if app["id"] in {"current-static", "ready-static", "legacy"}
            ]
            preview_registry = (json.dumps(payload) + "\n").encode("utf-8")
            profile = HostProfileStore(
                HostProfilePaths.for_repository(self.platform)
            )
            profile.publish_registration(
                "fleet-preview", registry_before, preview_registry
            )
            try:
                result = _run_bounded_command(
                    (str(self.platform / "bin/local-web"), "apps", "update", "--dry-run"), self.platform,
                    timeout=60.0,
                    environment={
                        **os.environ,
                        "PYTHONDONTWRITEBYTECODE": "1",
                        "TMPDIR": str(self.root),
                    },
                )
            finally:
                if profile.read_current() != preview_registry:
                    raise RuntimeError(_PRIVATE_MARKER)
                profile.publish_registration(
                    "fleet-preview", preview_registry, registry_before
                )
            statuses = tuple(
                (parts[0], parts[1])
                for line in result.stdout.decode("utf-8").splitlines()
                if (parts := line.split(maxsplit=2)) and len(parts) >= 2 and parts[1] in {"CURRENT", "READY", "SKIPPED", "BLOCKED"}
            )
            if (
                result.returncode != 0
                or result.stderr
                or statuses != expected
                or self.registry.read_bytes() != registry_before
            ):
                raise RuntimeError(_PRIVATE_MARKER)
            self._cli_statuses = statuses
        if self.coordinator is None:
            raise RuntimeError(_PRIVATE_MARKER)
        plan = self.coordinator.preview(self.registry)
        if [(app.app_id, app.status) for app in plan.apps] != [
            ("current-static", "CURRENT"), *( (app, "READY") for app in self._READY ), ("legacy", "SKIPPED"),
        ]:
            raise RuntimeError(_PRIVATE_MARKER)

    def assert_preview_pure(self) -> None:
        if self._snapshot != (self.registry.read_bytes(), (self.runtime / "sentinel").read_bytes()):
            raise RuntimeError(_PRIVATE_MARKER)
        if any(_git(self.apps / app, "status", "--porcelain=v1") for app in ("current-static", *self._READY, "legacy")):
            raise RuntimeError(_PRIVATE_MARKER)

    def apply(self):
        if self.coordinator is None:
            raise RuntimeError(_PRIVATE_MARKER)
        self.result = self.coordinator.apply(self.registry)
        return self.result

    def verify_commits(self) -> None:
        if self.result is None:
            raise RuntimeError(_PRIVATE_MARKER)
        for item in self.result.apps:
            if item.status != "UPDATED" or item.source_head is None:
                continue
            paths = _git(self.apps / item.app_id, "diff-tree", "--no-commit-id", "--name-only", "-r", item.source_head)
            if not paths or any(path not in {".local-web-platform.json", "local-web.json", "package-lock.json", "package.json", "vendor/local-web-ui.tgz"} for path in paths.splitlines()):
                raise RuntimeError(_PRIVATE_MARKER)

    def verify_releases(self) -> None:
        if not self.releases_match_commits():
            raise RuntimeError(_PRIVATE_MARKER)

    def verify_unsupported(self) -> None:
        if not self.legacy_is_untouched():
            raise RuntimeError(_PRIVATE_MARKER)

    def verify_recovery(self) -> None:
        if self.result is None or not any(
            item.app_id == "failing-static" and item.status == "FAILED_RECOVERED"
            for item in self.result.apps
        ) or not any(
            item.app_id == "later-static" and item.status == "UPDATED"
            for item in self.result.apps
        ) or not self.recovery_matches_original():
            raise RuntimeError(_PRIVATE_MARKER)

    @staticmethod
    def _snapshot_repository(repository: Path) -> _RecoverySnapshot:
        platform: list[tuple[Path, bytes | None]] = []
        try:
            for relative in _PLATFORM_OWNED:
                target = repository / relative
                if not target.exists():
                    platform.append((relative, None))
                elif target.is_symlink() or not target.is_file():
                    raise OSError
                else:
                    platform.append((relative, target.read_bytes()))
        except OSError:
            raise RuntimeError(_PRIVATE_MARKER) from None
        return _RecoverySnapshot(
            _git(repository, "rev-parse", "HEAD"),
            _git(repository, "rev-parse", "HEAD^{tree}"),
            tuple(platform),
        )

    def recovery_original(self) -> _RecoverySnapshot:
        if self._recovery_original is None:
            raise RuntimeError(_PRIVATE_MARKER)
        return self._recovery_original

    def recovery_matches(self, original: _RecoverySnapshot) -> bool:
        if self.result is None:
            return False
        item = next(
            (entry for entry in self.result.apps if entry.app_id == "failing-static"),
            None,
        )
        repository = self.apps / "failing-static"
        if (
            item is None
            or item.status != "FAILED_RECOVERED"
            or item.source_head is None
            or item.deployed_commit is None
            or item.source_head != item.deployed_commit
            or item.source_head == original.head
        ):
            return False
        try:
            if (
                _git(repository, "symbolic-ref", "--short", "HEAD") != "main"
                or _git(repository, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching")
                or _git(repository, "rev-parse", "HEAD") != item.source_head
                or _git(repository, "rev-parse", "HEAD^{tree}") != original.tree
                or self.releases.get(repository.resolve()) != item.deployed_commit
            ):
                return False
            return self._snapshot_repository(repository).platform == original.platform
        except (RuntimeError, subprocess.SubprocessError):
            return False

    def recovery_matches_original(self) -> bool:
        return self.recovery_matches(self.recovery_original())

    def verify_aggregate(self) -> None:
        if self.result is None or not any(
            item.status in {"SKIPPED", "FAILED_RECOVERED"} for item in self.result.apps
        ):
            raise RuntimeError(_PRIVATE_MARKER)

    def releases_match_commits(self) -> bool:
        if self.result is None:
            return False
        return all(
            item.status in {"CURRENT", "SKIPPED"}
            or (
                item.source_head is not None
                and item.source_head == item.deployed_commit
                and self.releases.get((self.apps / item.app_id).resolve())
                == item.deployed_commit
            )
            for item in self.result.apps
        )

    def legacy_is_untouched(self) -> bool:
        legacy = self.apps / "legacy"
        return _git(legacy, "rev-parse", "HEAD") == self._legacy_head and not _git(legacy, "status", "--porcelain=v1")

    def retry_is_noop(self) -> bool:
        if self.coordinator is None:
            return False
        retry = self.coordinator.apply(self.registry)
        if not any(
            item.app_id == "failing-static" and item.status == "UPDATED"
            for item in retry.apps
        ):
            return False
        final = self.coordinator.apply(self.registry)
        return all(item.status in {"CURRENT", "SKIPPED"} for item in final.apps)

    def close(self) -> None:
        self.service.close()

    def cli_preview_statuses(self) -> tuple[tuple[str, str], ...]:
        return self._cli_statuses


class FleetUpdateWorkflowVerifier:
    """Own a disposable fleet and render only fixed public phase labels."""

    def __init__(
        self,
        *,
        temporary_directory_factory: Callable[..., tempfile.TemporaryDirectory[str]] = tempfile.TemporaryDirectory,
        fail_after: str | None = None,
        emit: Callable[[str], None] = print,
    ) -> None:
        if fail_after is not None and fail_after not in EXPECTED_PHASES:
            raise ValueError("invalid disposable phase")
        self._temporary_directory_factory = temporary_directory_factory
        self._fail_after = fail_after
        self._emit = emit

    def run(self) -> int:
        temporary = None
        fleet = None
        completed: list[str] = []
        failed: str | None = None
        phases: tuple[tuple[str, Callable[[], None]], ...] = ()
        try:
            temporary = self._temporary_directory_factory(prefix="local-web-fleet-update-")
            fleet = _DisposableFleet(
                Path(temporary.name), require_cli_preview=self._fail_after is None
            )
            phases = (
                (EXPECTED_PHASES[0], fleet.create),
                (EXPECTED_PHASES[1], fleet.preview),
                (EXPECTED_PHASES[2], fleet.assert_preview_pure),
                (EXPECTED_PHASES[3], fleet.apply),
                (EXPECTED_PHASES[4], fleet.verify_commits),
                (EXPECTED_PHASES[5], fleet.verify_releases),
                (EXPECTED_PHASES[6], fleet.verify_unsupported),
                (EXPECTED_PHASES[7], fleet.verify_recovery),
                (EXPECTED_PHASES[8], fleet.verify_aggregate),
            )
            for label, action in phases:
                action()
                if label == self._fail_after:
                    raise RuntimeError(_PRIVATE_MARKER)
                completed.append(label)
        except BaseException:
            failed = self._fail_after or (EXPECTED_PHASES[len(completed)] if len(completed) < len(EXPECTED_PHASES) - 1 else EXPECTED_PHASES[-1])
        finally:
            cleanup_ok = False
            if fleet is not None:
                try:
                    fleet.close()
                except BaseException:
                    failed = EXPECTED_PHASES[-1]
            if temporary is not None:
                try:
                    temporary.cleanup()
                    cleanup_ok = not Path(temporary.name).exists()
                except BaseException:
                    cleanup_ok = False
            if self._fail_after == EXPECTED_PHASES[-1]:
                cleanup_ok = False
                failed = EXPECTED_PHASES[-1]
            if failed is None and not cleanup_ok:
                failed = EXPECTED_PHASES[-1]
        for label in completed:
            self._emit(label)
        if failed is not None:
            self._emit(failed.replace(" PASS", " FAIL"))
            if cleanup_ok and failed != EXPECTED_PHASES[-1]:
                self._emit(EXPECTED_PHASES[-1])
            return 1
        self._emit(EXPECTED_PHASES[-1])
        return 0


def verify(*, fail_after: str | None = None) -> tuple[str, ...]:
    lines: list[str] = []
    result = FleetUpdateWorkflowVerifier(fail_after=fail_after, emit=lines.append).run()
    if result != 0 or tuple(lines) != EXPECTED_PHASES:
        raise RuntimeError("fleet update workflow failed")
    return tuple(lines)


def main() -> int:
    try:
        for label in verify():
            print(label)
    except BaseException:
        print("fleet platform update workflow FAIL")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
