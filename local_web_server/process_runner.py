"""Bounded, isolated execution for fixed local-web process workflows."""

from __future__ import annotations

import os
import signal
import subprocess
import time
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


_COMMAND_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
_NPM_CACHE = Path.home() / ".npm"
_PLAYWRIGHT_CACHE = Path.home() / "Library" / "Caches" / "ms-playwright"


class ProcessRunError(ValueError):
    """A fixed process workflow failed without exposing child output."""


@dataclass(frozen=True)
class ProcessCommand:
    label: str
    argv: tuple[str, ...]


class ProcessWorkflowRunner(Protocol):
    """The fixed-command execution seam used by app workflows."""

    def run(self, repository: Path, commands: tuple[ProcessCommand, ...]) -> None: ...


def _environment(home: Path) -> dict[str, str]:
    return {
        "PATH": _COMMAND_PATH,
        "LANG": "C",
        "LC_ALL": "C",
        "CI": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": os.fspath(home),
        "TMPDIR": os.fspath(home),
        "NPM_CONFIG_USERCONFIG": os.fspath(home / ".npmrc-user-disabled"),
        "NPM_CONFIG_GLOBALCONFIG": os.fspath(home / ".npmrc-global-disabled"),
        "NPM_CONFIG_CACHE": os.fspath(_NPM_CACHE),
        "PLAYWRIGHT_BROWSERS_PATH": os.fspath(_PLAYWRIGHT_CACHE),
    }


def _group_alive(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        raise ProcessRunError("process recovery failed") from error


def _wait_for_group_exit(
    process: subprocess.Popen[bytes], deadline: float, poll_seconds: float
) -> bool:
    while True:
        process.poll()
        if not _group_alive(process.pid):
            if process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                try:
                    process.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    return False
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_seconds, remaining))


def _stop_process_group(
    process: subprocess.Popen[bytes], stop_seconds: float, poll_seconds: float
) -> None:
    process.poll()
    if not _group_alive(process.pid):
        if not _wait_for_group_exit(
            process, time.monotonic() + stop_seconds, poll_seconds
        ):
            raise ProcessRunError("process recovery failed")
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError as error:
        raise ProcessRunError("process recovery failed") from error
    if _wait_for_group_exit(
        process, time.monotonic() + stop_seconds, poll_seconds
    ):
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError as error:
        raise ProcessRunError("process recovery failed") from error
    if not _wait_for_group_exit(
        process, time.monotonic() + stop_seconds, poll_seconds
    ):
        raise ProcessRunError("process recovery failed")


class ProcessRunner:
    def __init__(
        self,
        *,
        timeout_seconds: float = 300,
        stop_seconds: float = 0.5,
        poll_seconds: float = 0.01,
    ) -> None:
        if timeout_seconds <= 0 or stop_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("process runner timing must be positive")
        self._timeout_seconds = timeout_seconds
        self._stop_seconds = stop_seconds
        self._poll_seconds = poll_seconds

    def run(self, repository: Path, commands: tuple[ProcessCommand, ...]) -> None:
        """Run immutable argv commands under one total deadline."""

        try:
            resolved = Path(repository).resolve(strict=True)
            if not resolved.is_dir() or not commands:
                raise OSError
        except OSError as error:
            raise ProcessRunError("process workflow failed") from error

        deadline = time.monotonic() + self._timeout_seconds
        with tempfile.TemporaryDirectory(prefix="local-web-process-home-") as text:
            home = Path(text)
            home.chmod(0o700)
            environment = _environment(home)
            for command in commands:
                self._run_command(resolved, command, environment, deadline)

    def _run_command(
        self,
        repository: Path,
        command: ProcessCommand,
        environment: dict[str, str],
        deadline: float,
    ) -> None:
        if (
            type(command) is not ProcessCommand
            or not command.label
            or not command.argv
            or any(type(argument) is not str or not argument for argument in command.argv)
        ):
            raise ProcessRunError("process workflow failed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProcessRunError(f"{command.label} failed")
        try:
            process = subprocess.Popen(
                command.argv,
                cwd=repository,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ProcessRunError(f"{command.label} failed") from error

        primary_error: BaseException | None = None
        return_code: int | None = None
        try:
            try:
                return_code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired as error:
                primary_error = ProcessRunError(f"{command.label} failed")
                primary_error.__cause__ = error
            except BaseException as error:
                primary_error = error
        finally:
            cleanup_error: ProcessRunError | None = None
            try:
                _stop_process_group(
                    process, self._stop_seconds, self._poll_seconds
                )
            except ProcessRunError as error:
                cleanup_error = error
            if primary_error is not None:
                if cleanup_error is not None:
                    raise primary_error from cleanup_error
                raise primary_error
            if cleanup_error is not None:
                raise ProcessRunError(f"{command.label} failed") from cleanup_error
        if return_code != 0:
            raise ProcessRunError(f"{command.label} failed")
