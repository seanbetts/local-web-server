#!/usr/bin/env python3
"""Verify existing-service React foundation adoption in disposable Git state."""

from __future__ import annotations

import json
import os
import pwd
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypeVar


PLATFORM_REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLATFORM_REPOSITORY))

from local_web_server.app_activation import AppActivationError, AppActivator
from local_web_server.app_check import AppCheckError, AppChecker
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore
from scripts.disposable_workflow_support import (
    initialise_disposable_host_profile,
    sanitized_subprocess_environment,
)


PASS_LABELS = (
    "create generic legacy service fixture",
    "preview React foundation without writes",
    "apply exact platform-owned foundation files",
    "preserve Python service legacy frontend and repository data",
    "commit adopted foundation and pass Doctor",
    "run foundation-local check and browser suite",
    "reject legacy release outcome",
    "integrate app-owned React release",
    "pass full app check and activation eligibility",
    "cleanup",
)

_INJECTABLE_LABELS = frozenset(
    {
        "preview React foundation without writes",
        "apply exact platform-owned foundation files",
        "reject legacy release outcome",
        "pass full app check and activation eligibility",
    }
)
_FOUNDATION_PATHS = frozenset(
    {
        Path(".local-web-platform.json"),
        Path("eslint.config.js"),
        Path("index.html"),
        Path("local-web.json"),
        Path("package-lock.json"),
        Path("package.json"),
        Path("playwright.config.ts"),
        Path("src/App.test.tsx"),
        Path("src/App.tsx"),
        Path("src/app.css"),
        Path("src/contextExport.test.ts"),
        Path("src/contextExport.ts"),
        Path("src/main.tsx"),
        Path("src/platform.test.ts"),
        Path("src/platform.ts"),
        Path("src/test/setup.ts"),
        Path("tests/app.spec.ts"),
        Path("tsconfig.app.json"),
        Path("tsconfig.json"),
        Path("tsconfig.node.json"),
        Path("vendor/local-web-ui.tgz"),
        Path("vite.config.ts"),
    }
)
_FOUNDATION_PARENT_PATHS = frozenset(
    {
        Path("src"),
        Path("src/test"),
        Path("tests"),
        Path("vendor"),
    }
)
_PRESERVED_PATHS = (
    Path(".gitignore"),
    Path("AGENTS.md"),
    Path("build_release.py"),
    Path("data/sentinel.txt"),
    Path("legacy_service_fixture/service.py"),
    Path("legacy_service/web_assets/index.html"),
    Path("notes/private.md"),
)
_THEME_ROUTE = "/_local-web/platform/theme.css"
_MAX_CAPTURE_BYTES = 64 * 1024
_MAX_PROCESS_LEDGER_BYTES = 16 * 1024
_MAX_LEDGER_PROCESSES = 128
_PROCESS_POLL_SECONDS = 0.01
_COMMAND_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
_TRUSTED_HOST_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
_NPM_CACHE = _TRUSTED_HOST_HOME / ".npm"
_PLAYWRIGHT_CACHE = (
    _TRUSTED_HOST_HOME / "Library" / "Caches" / "ms-playwright"
)
_FOUNDATION_CHECK_COMMANDS = frozenset({"npm-ci", "npm-check", "npm-e2e"})
_FOUNDATION_CHECK_OUTCOMES = frozenset(
    {"nonzero", "timeout", "output-bound", "cleanup"}
)
_T = TypeVar("_T")


class FoundationAdoptionError(RuntimeError):
    """One bounded acceptance phase failed without exposing child details."""

    def __init__(self, label: str, cause: BaseException | None = None):
        safe_label = label if label in PASS_LABELS else "foundation adoption workflow"
        super().__init__(safe_label)
        if cause is not None:
            self.__cause__ = cause


class _FoundationCheckDiagnostic(RuntimeError):
    """Private bounded attribution for one foundation-local command failure."""

    def __init__(self, command: str, outcome: str):
        if (
            command not in _FOUNDATION_CHECK_COMMANDS
            or outcome not in _FOUNDATION_CHECK_OUTCOMES
        ):
            raise ValueError("foundation check diagnostic was invalid")
        self.command = command
        self.outcome = outcome
        super().__init__(f"{command}:{outcome}")


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _stop_process_group_id(process_group: int) -> None:
    if process_group <= 0 or process_group == os.getpgrp():
        raise RuntimeError("disposable process cleanup failed")
    if not _process_group_exists(process_group):
        return
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _process_group_exists(process_group):
        time.sleep(_PROCESS_POLL_SECONDS)
    if _process_group_exists(process_group):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and _process_group_exists(process_group):
            time.sleep(_PROCESS_POLL_SECONDS)
    if _process_group_exists(process_group):
        raise RuntimeError("disposable process cleanup failed")


def _read_process_ledger(
    path: Path, *, allow_empty: bool = False
) -> dict[str, object] | None:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > (
        _MAX_PROCESS_LEDGER_BYTES
    ):
        raise RuntimeError("disposable process ledger was invalid")
    if metadata.st_size == 0 and allow_empty:
        return None
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("disposable process ledger was invalid") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schemaVersion", "workerPgid", "processes"}
        or payload.get("schemaVersion") != 1
        or type(payload.get("workerPgid")) is not int
        or payload["workerPgid"] <= 0
        or not isinstance(payload.get("processes"), list)
        or len(payload["processes"]) > _MAX_LEDGER_PROCESSES
    ):
        raise RuntimeError("disposable process ledger was invalid")
    for process in payload["processes"]:
        if (
            not isinstance(process, dict)
            or set(process) != {"pid", "pgid", "startNewSession"}
            or type(process.get("pid")) is not int
            or process["pid"] <= 0
            or type(process.get("pgid")) is not int
            or process["pgid"] <= 0
            or type(process.get("startNewSession")) is not bool
        ):
            raise RuntimeError("disposable process ledger was invalid")
    return payload


def _write_process_ledger(
    path: Path, worker_group: int, processes: list[dict[str, object]]
) -> None:
    if len(processes) > _MAX_LEDGER_PROCESSES:
        raise RuntimeError("disposable process ledger was invalid")
    content = json.dumps(
        {
            "schemaVersion": 1,
            "workerPgid": worker_group,
            "processes": processes,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    if len(content) > _MAX_PROCESS_LEDGER_BYTES:
        raise RuntimeError("disposable process ledger was invalid")
    temporary = path.with_name(f".{path.name}.next")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _stop_process_group(process: subprocess.Popen[bytes]) -> None:
    if not _process_group_exists(process.pid):
        process.wait()
        return
    os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        process.poll()
        if not _process_group_exists(process.pid):
            break
        time.sleep(0.01)
    if _process_group_exists(process.pid):
        os.killpg(process.pid, signal.SIGKILL)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            process.poll()
            if not _process_group_exists(process.pid):
                break
            time.sleep(0.01)
    if _process_group_exists(process.pid):
        raise RuntimeError("disposable process cleanup failed")
    process.wait()


def _ledgered_process_groups(path: Path) -> frozenset[int]:
    payload = _read_process_ledger(path, allow_empty=True)
    if payload is None:
        return frozenset()
    return frozenset(
        {
            payload["workerPgid"],
            *(process["pgid"] for process in payload["processes"]),
        }
    )


def _stop_worker_and_ledgered_groups(
    process: subprocess.Popen[bytes], ledger: Path
) -> None:
    failure: BaseException | None = None
    groups: set[int] = set()
    try:
        groups.update(_ledgered_process_groups(ledger))
    except BaseException as error:
        failure = error
    for process_group in sorted(groups - {process.pid}):
        try:
            _stop_process_group_id(process_group)
        except BaseException as error:
            if failure is None:
                failure = error
    try:
        _stop_process_group(process)
    except BaseException as error:
        if failure is None:
            failure = error
    try:
        groups.update(_ledgered_process_groups(ledger))
    except BaseException as error:
        if failure is None:
            failure = error
    for process_group in sorted(groups):
        try:
            _stop_process_group_id(process_group)
        except BaseException as error:
            if failure is None:
                failure = error
    if failure is not None:
        raise RuntimeError("disposable process cleanup failed") from failure


def _command_environment(home: Path) -> dict[str, str]:
    return sanitized_subprocess_environment(
        {},
        overrides={
            "PATH": _COMMAND_PATH,
            "LANG": "C",
            "LC_ALL": "C",
            "CI": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
            "HOME": os.fspath(home),
            "TMPDIR": os.fspath(home),
            "NPM_CONFIG_USERCONFIG": os.fspath(home / ".npmrc-user-disabled"),
            "NPM_CONFIG_GLOBALCONFIG": os.fspath(
                home / ".npmrc-global-disabled"
            ),
            "NPM_CONFIG_CACHE": os.fspath(_NPM_CACHE),
            "PLAYWRIGHT_BROWSERS_PATH": os.fspath(_PLAYWRIGHT_CACHE),
        },
    )


def _prepare_command_home(home: Path) -> None:
    try:
        resolved_home = home.resolve(strict=True)
        targets: list[Path] = []
        for target in (_NPM_CACHE, _PLAYWRIGHT_CACHE):
            metadata = target.lstat()
            resolved = target.resolve(strict=True)
            if (
                not target.is_absolute()
                or stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or not resolved.is_dir()
                or resolved == resolved_home
                or resolved_home in resolved.parents
                or resolved in resolved_home.parents
            ):
                raise OSError
            targets.append(resolved)

        library = home / "Library"
        caches = library / "Caches"
        library.mkdir(mode=0o700)
        caches.mkdir(mode=0o700)
        library.chmod(0o700)
        caches.chmod(0o700)
        (home / ".npm").symlink_to(targets[0], target_is_directory=True)
        (caches / "ms-playwright").symlink_to(
            targets[1], target_is_directory=True
        )
    except (OSError, RuntimeError) as error:
        raise RuntimeError("disposable cache mounts were invalid") from error


def _run(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout: float,
    capture: bool = False,
    process_ledger: Path | None = None,
) -> tuple[str, str]:
    """Run fixed argv in a child group and retain only bounded requested output."""

    with (
        tempfile.TemporaryDirectory(
            prefix="local-web-foundation-command-home-"
        ) as command_home_text,
        tempfile.TemporaryFile() as stdout,
        tempfile.TemporaryFile() as stderr,
    ):
        command_home = Path(command_home_text)
        command_home.chmod(0o700)
        _prepare_command_home(command_home)
        environment = _command_environment(command_home)
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=stdout if capture else subprocess.DEVNULL,
            stderr=stderr if capture else subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
        failure: BaseException | None = None
        return_code: int | None = None
        try:
            deadline = time.monotonic() + timeout
            while True:
                if capture and (
                    os.fstat(stdout.fileno()).st_size > _MAX_CAPTURE_BYTES
                    or os.fstat(stderr.fileno()).st_size > _MAX_CAPTURE_BYTES
                ):
                    failure = RuntimeError(
                        "disposable child output exceeded its bound"
                    )
                    break
                return_code = process.poll()
                if return_code is not None:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    failure = subprocess.TimeoutExpired(argv, timeout)
                    break
                time.sleep(min(_PROCESS_POLL_SECONDS, remaining))
        except BaseException as error:
            failure = error
        finally:
            try:
                if process_ledger is None:
                    _stop_process_group(process)
                else:
                    _stop_worker_and_ledgered_groups(process, process_ledger)
            except BaseException as cleanup_error:
                if failure is None:
                    failure = cleanup_error
        if isinstance(failure, RuntimeError) and str(failure) == (
            "disposable child output exceeded its bound"
        ):
            raise failure
        if failure is not None or return_code != 0:
            raise RuntimeError("disposable child command failed") from failure
        if not capture:
            return "", ""
        if (
            os.fstat(stdout.fileno()).st_size > _MAX_CAPTURE_BYTES
            or os.fstat(stderr.fileno()).st_size > _MAX_CAPTURE_BYTES
        ):
            raise RuntimeError("disposable child output exceeded its bound")
        stdout.seek(0)
        stderr.seek(0)
        return (
            stdout.read().decode("utf-8"),
            stderr.read().decode("utf-8"),
        )


def _git(repository: Path, *arguments: str) -> str:
    stdout, _stderr = _run(
        ("/usr/bin/git", "-C", str(repository), "--literal-pathspecs", *arguments),
        cwd=repository,
        timeout=60,
        capture=True,
    )
    return stdout.strip()


def _phase(
    label: str,
    operation: Callable[[], _T],
    labels: list[str],
    fail_after: str | None,
) -> _T:
    try:
        result = operation()
    except FoundationAdoptionError:
        raise
    except BaseException as error:
        raise FoundationAdoptionError(label, error) from error
    labels.append(label)
    if fail_after == label:
        raise FoundationAdoptionError(label)
    return result


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def _fixture_manifest() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "id": "foundation-fixture",
        "title": "Example Service Fixture",
        "route": "/foundation-fixture",
        "kind": "service",
        "build": {
            "commands": [["python3", "build_release.py"]],
            "output": ".local-web-dist",
            "environment": ["VITE_PUBLIC_BASE_PATH"],
        },
        "healthPath": "/foundation-fixture/healthz",
        "service": {
            "module": "legacy_service_fixture.service",
            "internalHealthPath": "/healthz",
            "frontendOutput": "public",
            "proxyPaths": ["/api"],
            "startCommand": [
                "/usr/bin/env",
                "python3",
                "{release}/server/service.py",
                "--port",
                "{port}",
                "--data-dir",
                "{repository}/data",
            ],
        },
        "home": {"icon": "chart-line", "accent": "#74D3A4"},
    }


def _write_fixture(repository: Path) -> None:
    files = {
        Path("local-web.json"): _json_bytes(_fixture_manifest()),
        Path("AGENTS.md"): (
            b"# Existing service fixture\n\n"
            b"Preserve the Python service, legacy frontend and repository data.\n"
        ),
        Path(".gitignore"): (
            b".local-web-dist/\ndist/\nnode_modules/\nplaywright-report/\n"
            b"test-results/\n*.tsbuildinfo\n"
        ),
        Path("legacy_service_fixture/service.py"): (
            b"import argparse\n"
            b"from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            b"from pathlib import Path\n\n"
            b"parser = argparse.ArgumentParser()\n"
            b"parser.add_argument('--port', type=int, required=True)\n"
            b"parser.add_argument('--data-dir', type=Path, required=True)\n"
            b"args = parser.parse_args()\n"
            b"sentinel = args.data_dir.joinpath('sentinel.txt').read_text()\n"
            b"if sentinel != 'repository data sentinel\\n':\n"
            b"    raise SystemExit(1)\n\n"
            b"class Handler(BaseHTTPRequestHandler):\n"
            b"    def do_GET(self):\n"
            b"        self.send_response(200 if self.path == '/healthz' else 404)\n"
            b"        self.end_headers()\n"
            b"    def log_message(self, _format, *args):\n"
            b"        pass\n\n"
            b"HTTPServer(('127.0.0.1', args.port), Handler).serve_forever()\n"
        ),
        Path("legacy_service/web_assets/index.html"): (
            b"<!doctype html><html><head><title>Legacy Service</title></head>"
            b"<body><main>Legacy service frontend</main></body></html>\n"
        ),
        Path("build_release.py"): (
            b"from pathlib import Path\n"
            b"import shutil\n\n"
            b"root = Path(__file__).resolve().parent\n"
            b"output = root / '.local-web-dist'\n"
            b"shutil.rmtree(output, ignore_errors=True)\n"
            b"(output / 'public').mkdir(parents=True)\n"
            b"(output / 'server').mkdir()\n"
            b"legacy = root / 'legacy_service/web_assets/index.html'\n"
            b"shutil.copy2(legacy, output / 'public/index.html')\n"
            b"service = root / 'legacy_service_fixture/service.py'\n"
            b"shutil.copy2(service, output / 'server/service.py')\n"
        ),
        Path("data/sentinel.txt"): b"repository data sentinel\n",
    }
    repository.mkdir()
    for relative, content in files.items():
        destination = repository / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    _git(repository, "init", "--initial-branch=main")
    _git(repository, "add", "--all")
    _git(
        repository,
        "-c",
        "user.name=Foundation Adoption Verification",
        "-c",
        "user.email=foundation-adoption@localhost",
        "commit",
        "-m",
        "Create legacy service fixture",
    )
    _git(repository, "switch", "-c", "foundation-adoption")
    note = repository / "notes/private.md"
    note.parent.mkdir()
    note.write_bytes(b"unrelated dirty note sentinel\n")


def _snapshot_tree(
    repository: Path,
) -> dict[Path, tuple[int, int, str | None, bytes | None]]:
    """Record every non-Git entry's type, mode, link target and file bytes."""

    snapshot: dict[Path, tuple[int, int, str | None, bytes | None]] = {}
    for current, directory_names, file_names in os.walk(
        repository, topdown=True, followlinks=False
    ):
        directory_names[:] = sorted(
            name for name in directory_names if name != ".git"
        )
        current_path = Path(current)
        for name in (*directory_names, *sorted(file_names)):
            path = current_path / name
            relative = path.relative_to(repository)
            metadata = path.lstat()
            entry_type = stat.S_IFMT(metadata.st_mode)
            mode = stat.S_IMODE(metadata.st_mode)
            link_target = os.readlink(path) if stat.S_ISLNK(metadata.st_mode) else None
            content = path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None
            snapshot[relative] = (entry_type, mode, link_target, content)
    return snapshot


def _changed_paths(
    before: dict[Path, object], after: dict[Path, object]
) -> frozenset[Path]:
    return frozenset(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


def _planned_paths(output: str, heading: str) -> frozenset[Path]:
    lines = output.splitlines()
    if lines.count(heading) != 1:
        raise RuntimeError("foundation update output was incomplete")
    start = lines.index(heading) + 1
    paths: list[Path] = []
    for line in lines[start:]:
        if not line.startswith("  "):
            break
        paths.append(Path(line.strip()))
    if not paths or len(paths) != len(set(paths)):
        raise RuntimeError("foundation update paths were invalid")
    return frozenset(paths)


def _run_cli(platform: Path, *arguments: str) -> tuple[str, str]:
    return _run(
        (str((platform / "bin/local-web").resolve()), *arguments),
        cwd=platform,
        timeout=600,
        capture=True,
    )


def _assert_cli_success(output: tuple[str, str]) -> str:
    stdout, stderr = output
    if stderr:
        raise RuntimeError("local-web emitted unexpected diagnostics")
    return stdout


def _commit_exact(repository: Path, paths: frozenset[Path], message: str) -> None:
    ordered = tuple(path.as_posix() for path in sorted(paths))
    _git(repository, "add", "--", *ordered)
    staged_output = _git(
        repository, "diff", "--cached", "--name-only", "--", *ordered
    )
    staged = frozenset(
        Path(line)
        for line in staged_output.splitlines()
        if line
    )
    if staged != paths:
        raise RuntimeError("fixture commit scope was invalid")
    _git(
        repository,
        "-c",
        "user.name=Foundation Adoption Verification",
        "-c",
        "user.email=foundation-adoption@localhost",
        "commit",
        "-m",
        message,
        "--",
        *ordered,
    )


def _write_disposable_registry(platform: Path, runtime: Path) -> tuple[Path, bytes]:
    content = _json_bytes(
        {
            "schemaVersion": 1,
            "host": "disposable.invalid",
            "runtimeRoot": str(runtime),
            "apps": [],
        }
    )
    return initialise_disposable_host_profile(platform, content), content


def _activation_preview(platform: Path, repository: Path, registry: Path):
    return AppActivator(
        platform_repository=platform,
        registry_path=registry,
        profile_store=HostProfileStore(
            HostProfilePaths.for_repository(platform)
        ),
    ).preview(repository)


def _direct_seam_payload(
    operation: str, platform: Path, repository: Path, registry: Path
) -> dict[str, object]:
    if operation == "check-legacy-release":
        try:
            AppChecker().check(repository)
        except AppCheckError as error:
            return {"outcome": "rejected", "error": str(error)}
        return {"outcome": "accepted"}
    if operation == "preview-activation":
        try:
            plan = _activation_preview(platform, repository, registry)
        except AppActivationError as error:
            return {"outcome": "rejected", "error": str(error)}
        return {
            "outcome": "eligible",
            "appId": plan.app_id,
            "kind": plan.kind,
            "portAssigned": plan.port is not None,
            "registryChanged": plan.registry_changed,
            "stages": list(plan.stages),
        }
    raise ValueError("direct seam operation was invalid")


def _direct_seam_worker_main(arguments: tuple[str, ...]) -> int:
    """Run real platform seams while recording their unchanged child groups."""

    if len(arguments) != 5 or os.getpgrp() != os.getpid():
        return 2
    operation, platform_text, repository_text, registry_text, ledger_text = arguments
    try:
        platform = Path(platform_text).resolve(strict=True)
        repository = Path(repository_text).resolve(strict=True)
        registry = Path(registry_text).resolve(strict=True)
        ledger = Path(ledger_text).resolve(strict=True)
        root = repository.parent
        if (
            platform.parent != root
            or root not in registry.parents
            or ledger.parent != root
            or not ledger.name.startswith(".direct-seam-ledger-")
            or ledger.suffix != ".json"
            or not platform.is_dir()
            or not repository.is_dir()
            or not registry.is_file()
            or not ledger.is_file()
            or ledger.stat().st_size != 0
        ):
            raise OSError
    except (OSError, RuntimeError):
        return 2

    original_popen = subprocess.Popen
    worker_group = os.getpgrp()
    processes: list[dict[str, object]] = []
    try:
        _write_process_ledger(ledger, worker_group, processes)
    except (OSError, RuntimeError):
        return 2

    def recording_popen(*args, **kwargs):
        if len(processes) >= _MAX_LEDGER_PROCESSES:
            raise RuntimeError("disposable process ledger was invalid")
        process = original_popen(*args, **kwargs)
        process_group = os.getpgid(process.pid)
        record = {
            "pid": process.pid,
            "pgid": process_group,
            "startNewSession": kwargs.get("start_new_session") is True,
        }
        try:
            processes.append(record)
            _write_process_ledger(ledger, worker_group, processes)
        except BaseException:
            processes.pop()
            if process_group == worker_group:
                process.terminate()
            else:
                _stop_process_group_id(process_group)
            process.wait()
            raise
        return process

    subprocess.Popen = recording_popen
    try:
        payload = _direct_seam_payload(
            operation, platform, repository, registry
        )
    except BaseException:
        return 1
    finally:
        subprocess.Popen = original_popen
    if not processes:
        return 1
    sys.stdout.write(json.dumps(payload, separators=(",", ":")))
    return 0


def _run_direct_seam(
    operation: str, platform: Path, repository: Path, registry: Path
) -> dict[str, object]:
    descriptor, ledger_text = tempfile.mkstemp(
        prefix=".direct-seam-ledger-",
        suffix=".json",
        dir=repository.parent,
    )
    os.close(descriptor)
    ledger = Path(ledger_text)
    stdout, stderr = _run(
        (
            sys.executable,
            str(Path(__file__).resolve()),
            "--direct-seam-worker",
            operation,
            str(platform),
            str(repository),
            str(registry),
            str(ledger),
        ),
        cwd=repository,
        timeout=600,
        capture=True,
        process_ledger=ledger,
    )
    try:
        payload = json.loads(stdout)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("direct seam evidence was invalid") from error
    if stderr or not isinstance(payload, dict):
        raise RuntimeError("direct seam evidence was invalid")
    process_ledger = _read_process_ledger(ledger)
    if process_ledger is None or not process_ledger["processes"]:
        raise RuntimeError("direct seam evidence was invalid")
    for process_group in _ledgered_process_groups(ledger):
        if _process_group_exists(process_group):
            raise RuntimeError("direct seam process cleanup was incomplete")
    return payload


def _create_fixture_phase(root: Path) -> tuple[Path, Path, dict[Path, bytes]]:
    platform = root / "disposable-platform"
    repository = root / "foundation-fixture"
    _run(
        (
            "/usr/bin/git",
            "clone",
            "--quiet",
            "--no-local",
            "--single-branch",
            str(PLATFORM_REPOSITORY),
            str(platform),
        ),
        cwd=root,
        timeout=60,
    )
    _git(platform, "switch", "-C", "main")
    exclude = platform / ".git/info/exclude"
    exclude.write_text(
        exclude.read_text(encoding="utf-8") + "\nconfig/local/\n",
        encoding="utf-8",
    )
    _run(("npm", "ci", "--ignore-scripts"), cwd=platform, timeout=300)
    _write_fixture(repository)
    preserved = {path: (repository / path).read_bytes() for path in _PRESERVED_PATHS}
    if _git(repository, "status", "--porcelain=v1", "--untracked-files=all") != (
        "?? notes/private.md"
    ):
        raise RuntimeError("fixture dirty-note contract was invalid")
    return platform, repository, preserved


def _preview_phase(platform: Path, repository: Path) -> None:
    before = _snapshot_tree(repository)
    status_before = _git(
        repository, "status", "--porcelain=v1", "--untracked-files=all"
    )
    stdout = _assert_cli_success(
        _run_cli(
            platform,
            "app",
            "update",
            "--repository",
            str(repository),
            "--foundation",
            "react-vite",
            "--dry-run",
        )
    )
    if (
        "mode: adopt\n" not in stdout
        or "foundation: react-vite\n" not in stdout
        or _planned_paths(stdout, "would update:") != _FOUNDATION_PATHS
        or _snapshot_tree(repository) != before
        or _git(repository, "status", "--porcelain=v1", "--untracked-files=all")
        != status_before
        or str(repository) in stdout
    ):
        raise RuntimeError("foundation preview contract was invalid")


def _publication_phase(platform: Path, repository: Path) -> None:
    before = _snapshot_tree(repository)
    stdout = _assert_cli_success(
        _run_cli(
            platform,
            "app",
            "update",
            "--repository",
            str(repository),
            "--foundation",
            "react-vite",
        )
    )
    if (
        _planned_paths(stdout, "updated:") != _FOUNDATION_PATHS
        or _changed_paths(before, _snapshot_tree(repository))
        != _FOUNDATION_PATHS | _FOUNDATION_PARENT_PATHS
        or str(repository) in stdout
    ):
        raise RuntimeError("foundation publication contract was invalid")


def _preservation_phase(
    repository: Path, preserved: dict[Path, bytes]
) -> None:
    if any(
        (repository / path).read_bytes() != content
        for path, content in preserved.items()
    ):
        raise RuntimeError("application-owned fixture bytes changed")
    expected_manifest = _fixture_manifest()
    adopted_manifest = json.loads((repository / "local-web.json").read_bytes())
    platform = adopted_manifest.pop("platform", None)
    if adopted_manifest != expected_manifest or not isinstance(platform, dict):
        raise RuntimeError("service hosting contract changed")


def _adopted_commit_phase(platform: Path, repository: Path) -> None:
    _commit_exact(repository, _FOUNDATION_PATHS, "Adopt React foundation")
    stdout = _assert_cli_success(
        _run_cli(platform, "app", "doctor", "--repository", str(repository))
    )
    if stdout.strip() != "foundation-fixture: compatible":
        raise RuntimeError("Doctor did not accept the adopted foundation")
    if _git(repository, "status", "--porcelain=v1", "--untracked-files=all") != (
        "?? notes/private.md"
    ):
        raise RuntimeError("adopted fixture status was invalid")


def _foundation_check_outcome(error: RuntimeError) -> str:
    if str(error) == "disposable child output exceeded its bound":
        return "output-bound"
    cause = error.__cause__
    if isinstance(cause, subprocess.TimeoutExpired):
        return "timeout"
    if (
        isinstance(cause, RuntimeError)
        and str(cause) == "disposable process cleanup failed"
    ):
        return "cleanup"
    return "nonzero"


def _foundation_checks_phase(repository: Path) -> None:
    commands = (
        ("npm-ci", ("npm", "ci", "--ignore-scripts")),
        ("npm-check", ("npm", "run", "check")),
        ("npm-e2e", ("npm", "run", "test:e2e")),
    )
    for command, argv in commands:
        diagnostic: _FoundationCheckDiagnostic | None = None
        try:
            _run(argv, cwd=repository, timeout=300)
        except RuntimeError as error:
            diagnostic = _FoundationCheckDiagnostic(
                command, _foundation_check_outcome(error)
            )
        if diagnostic is not None:
            raise diagnostic


def _legacy_rejection_phase(
    platform: Path, repository: Path, registry: Path
) -> None:
    _run(("/usr/bin/env", "python3", "build_release.py"), cwd=repository, timeout=60)
    legacy_entry = repository / ".local-web-dist/public/index.html"
    if not legacy_entry.is_file() or _THEME_ROUTE in legacy_entry.read_text(
        encoding="utf-8"
    ):
        raise RuntimeError("legacy release fixture was invalid")
    check_evidence = _run_direct_seam(
        "check-legacy-release", platform, repository, registry
    )
    if check_evidence != {
        "outcome": "rejected",
        "error": "app release contract failed",
    }:
        raise RuntimeError("app check failure was not sanitized")
    registry_before = registry.read_bytes()
    activation_evidence = _run_direct_seam(
        "preview-activation", platform, repository, registry
    )
    if activation_evidence != {
        "outcome": "rejected",
        "error": "application activation validation failed",
    }:
        raise RuntimeError("incomplete feature branch was activation eligible")
    if registry.read_bytes() != registry_before:
        raise RuntimeError("activation rejection changed the registry")


def _integration_phase(repository: Path) -> None:
    manifest_path = repository / "local-web.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["build"]["commands"] = [["npm", "run", "build"]]
    manifest["build"]["release"] = [
        {"source": "dist", "target": "public"},
        {"source": "legacy_service_fixture", "target": "server"},
    ]
    manifest_path.write_bytes(_json_bytes(manifest))
    _run(("npm", "run", "build"), cwd=repository, timeout=300)
    _commit_exact(
        repository,
        frozenset({Path("local-web.json")}),
        "Integrate React service release",
    )


def _full_check_and_activation_phase(
    platform: Path, repository: Path, registry: Path, runtime: Path
) -> None:
    stdout = _assert_cli_success(
        _run_cli(platform, "app", "check", "--repository", str(repository))
    )
    if stdout.strip() != "foundation-fixture: checks passed":
        raise RuntimeError("full application check was incomplete")
    note = repository / "notes/private.md"
    note.unlink()
    note.parent.rmdir()
    if _git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("fixture was not clean after note cleanup")
    _git(repository, "switch", "main")
    _git(repository, "merge", "--ff-only", "foundation-adoption")
    if _git(repository, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("merged fixture was not clean")
    registry_before = registry.read_bytes()
    activation_evidence = _run_direct_seam(
        "preview-activation", platform, repository, registry
    )
    if (
        activation_evidence
        != {
            "outcome": "eligible",
            "appId": "foundation-fixture",
            "kind": "service",
            "portAssigned": True,
            "registryChanged": True,
            "stages": ["validate", "register", "install", "deploy", "verify"],
        }
        or registry.read_bytes() != registry_before
        or runtime.exists()
    ):
        raise RuntimeError("activation preview eligibility was invalid")


def verify(*, fail_after: str | None = None) -> tuple[str, ...]:
    """Run the real disposable acceptance workflow and return bounded labels."""

    if fail_after is not None and fail_after not in _INJECTABLE_LABELS:
        raise ValueError("foundation adoption injection point is invalid")
    labels: list[str] = []
    temporary: TemporaryDirectory[str] | None = None
    error: FoundationAdoptionError | None = None
    try:
        temporary = TemporaryDirectory(prefix="local-web-foundation-adoption-")
        root = Path(temporary.name)
        platform, repository, preserved = _phase(
            PASS_LABELS[0],
            lambda: _create_fixture_phase(root),
            labels,
            fail_after,
        )
        runtime = root / "disposable-host/runtime"
        registry, _registry_content = _write_disposable_registry(
            platform, runtime
        )
        _phase(
            PASS_LABELS[1],
            lambda: _preview_phase(platform, repository),
            labels,
            fail_after,
        )
        _phase(
            PASS_LABELS[2],
            lambda: _publication_phase(platform, repository),
            labels,
            fail_after,
        )
        _phase(
            PASS_LABELS[3],
            lambda: _preservation_phase(repository, preserved),
            labels,
            fail_after,
        )
        _phase(
            PASS_LABELS[4],
            lambda: _adopted_commit_phase(platform, repository),
            labels,
            fail_after,
        )
        _phase(
            PASS_LABELS[5],
            lambda: _foundation_checks_phase(repository),
            labels,
            fail_after,
        )
        _phase(
            PASS_LABELS[6],
            lambda: _legacy_rejection_phase(platform, repository, registry),
            labels,
            fail_after,
        )
        _phase(
            PASS_LABELS[7],
            lambda: _integration_phase(repository),
            labels,
            fail_after,
        )
        _phase(
            PASS_LABELS[8],
            lambda: _full_check_and_activation_phase(
                platform, repository, registry, runtime
            ),
            labels,
            fail_after,
        )
    except FoundationAdoptionError as caught:
        error = caught
    except BaseException as caught:
        error = FoundationAdoptionError("foundation adoption workflow", caught)
    finally:
        if temporary is not None:
            try:
                temporary.cleanup()
                if Path(temporary.name).exists():
                    raise OSError
            except BaseException as cleanup_error:
                if error is None:
                    error = FoundationAdoptionError("cleanup", cleanup_error)
        if error is None:
            labels.append("cleanup")
    if error is not None:
        raise error
    result = tuple(labels)
    if result != PASS_LABELS:
        raise FoundationAdoptionError("foundation adoption workflow")
    return result


def main() -> int:
    try:
        labels = verify()
    except FoundationAdoptionError as error:
        print(f"{error} FAIL")
        return 1
    for label in labels:
        print(f"{label} PASS")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] != "--direct-seam-worker":
            raise SystemExit(2)
        raise SystemExit(_direct_seam_worker_main(tuple(sys.argv[2:])))
    raise SystemExit(main())
