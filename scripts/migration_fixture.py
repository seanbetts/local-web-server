"""Small POSIX fixture runner for the two migration acceptance journeys.

Workers own one private process group. Fixture children inherit it: no PID files,
process-table scans, detached jobs, or machine-wide cleanup are needed.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path


def private_environment(root: Path) -> dict[str, str]:
    environment = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
    }
    for name, directory in (
        ("HOME", "home"),
        ("TMPDIR", "tmp"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
    ):
        path = root / "process-environment" / directory
        path.mkdir(parents=True, mode=0o700, exist_ok=True)
        environment[name] = str(path)
    return environment


def stop_process(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
    process.wait(timeout=1)


def process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def run_bounded(
    arguments,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    maximum: int = 8192,
    merge_stderr: bool = True,
    worker: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Bound bytes and time; always reap the child and a worker's descendants."""
    with subprocess.Popen(
        arguments,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL,
        start_new_session=worker,
    ) as process:
        output = bytearray()
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise RuntimeError("disposable command exceeded its time bound")
                    if not selector.select(min(remaining, 0.1)):
                        continue
                    chunk = os.read(
                        process.stdout.fileno(), min(4096, maximum + 1 - len(output))
                    )
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > maximum:
                        raise RuntimeError(
                            "disposable command exceeded its output bound"
                        )
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
        finally:
            if worker:
                # Only this fixture-created group is signalled, even if its leader exited.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            stop_process(process)
        return subprocess.CompletedProcess(
            arguments, process.returncode, output.decode("utf-8"), ""
        )


def generate_service_repository(
    repository: Path, platform: Path, artifact: Path, *, app_id: str, route: str
) -> None:
    """Render the production template; app generation has its own integration test."""
    import hashlib
    import json
    import shutil
    from local_web_server.app_foundation import render_generated_repository
    from local_web_server.app_template import TemplateInputs
    from local_web_server.app_provenance import (
        AppProvenance,
        CURRENT_TEMPLATE_VERSION,
        UiArtifactReference,
        render_provenance,
    )
    from local_web_server.config import SUPPORTED_PLATFORM_CONTRACT

    version = json.loads((platform / "packages/ui/package.json").read_text())["version"]
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    inputs = TemplateInputs(
        app_id=app_id,
        title="Migration Fixture",
        route=route,
        icon="database",
        accent="#76D39B",
        ui_version=version,
        ui_sha256=digest,
        capabilities=(),
        kind="service",
    )
    for rendered in render_generated_repository(inputs):
        destination = repository / rendered.path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(rendered.content, encoding="utf-8")
    vendor = repository / "vendor/local-web-ui.tgz"
    vendor.parent.mkdir(exist_ok=True)
    shutil.copyfile(artifact, vendor)
    (repository / ".local-web-platform.json").write_bytes(
        render_provenance(
            AppProvenance(
                schema_version=1,
                template_version=CURRENT_TEMPLATE_VERSION,
                platform_contract_version=SUPPORTED_PLATFORM_CONTRACT,
                ui=UiArtifactReference(version, digest),
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


def inherit_worker_session():
    """Keep production runner children in the fixture's outer cleanup group.

    Their command/validation logic runs unchanged. Production process supervision
    has dedicated tests; inside this worker, the fixture owns group supervision.
    """
    from contextlib import ExitStack
    from unittest.mock import patch
    from local_web_server import app_platform_commit, git_runner, process_runner

    class SubprocessInWorker:
        def __getattr__(self, name):
            return getattr(subprocess, name)

        def Popen(self, *args, **kwargs):
            kwargs["start_new_session"] = False
            return subprocess.Popen(*args, **kwargs)

    stack = ExitStack()
    for module in (app_platform_commit, git_runner, process_runner):
        stack.enter_context(patch.object(module, "subprocess", SubprocessInWorker()))
    stack.enter_context(patch.object(git_runner, "_stop_and_reap", stop_process))
    stack.enter_context(
        patch.object(
            process_runner,
            "_stop_process_group",
            lambda process, *_args: stop_process(process),
        )
    )
    stack.enter_context(
        patch.object(
            app_platform_commit.AppPlatformCommitter,
            "_stop_git_process",
            lambda _self, process: stop_process(process),
        )
    )
    return stack
