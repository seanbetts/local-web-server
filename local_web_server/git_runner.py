"""Bounded, ambient-independent execution of fixed local Git inspections."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time


_GIT_CANDIDATES = (
    Path("/usr/bin/git"),
    Path("/opt/homebrew/bin/git"),
    Path("/usr/local/bin/git"),
)
_GIT_TIMEOUT_SECONDS = 15.0
_MAXIMUM_STDOUT = 1024 * 1024
_GIT_PREFIX = (
    "--no-pager",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
)


class GitRunnerError(RuntimeError):
    """A Git inspection failed without exposing paths or child output."""


@dataclass(frozen=True, slots=True)
class GitResult:
    returncode: int
    stdout: str


def _git_executable() -> str:
    for candidate in _GIT_CANDIDATES:
        try:
            if not candidate.is_absolute():
                continue
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if (
            stat.S_ISREG(metadata.st_mode)
            and not metadata.st_mode & 0o022
            and os.access(resolved, os.X_OK)
        ):
            return os.fspath(resolved)
    raise GitRunnerError("Git inspection failed")


def _environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_ALLOW_PROTOCOL": "",
        "GIT_ASKPASS": "/usr/bin/false",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "SSH_ASKPASS": "/usr/bin/false",
    }


def _stop_and_reap(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass


def run_git(
    repository: Path,
    arguments: tuple[str, ...],
    *,
    maximum_stdout: int,
) -> GitResult:
    """Run one Git inspection with fixed isolation, time and output bounds."""

    if (
        type(maximum_stdout) is not int
        or not 0 <= maximum_stdout <= _MAXIMUM_STDOUT
        or not isinstance(arguments, tuple)
        or not arguments
        or any(
            type(argument) is not str
            or not argument
            or "\0" in argument
            for argument in arguments
        )
    ):
        raise GitRunnerError("Git inspection failed")
    try:
        root = Path(repository).resolve(strict=True)
        if not root.is_dir():
            raise OSError
        process = subprocess.Popen(
            (_git_executable(), *_GIT_PREFIX, "-C", os.fspath(root), *arguments),
            cwd=root,
            env=_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise GitRunnerError("Git inspection failed") from error
    if process.stdout is None:
        _stop_and_reap(process)
        raise GitRunnerError("Git inspection failed")

    deadline = time.monotonic() + _GIT_TIMEOUT_SECONDS
    output = bytearray()
    selector = selectors.DefaultSelector()
    completed = False
    try:
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdout, selectors.EVENT_READ)
        reached_eof = False
        while not reached_eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitRunnerError("Git inspection failed")
            events = selector.select(min(remaining, 0.1))
            for key, _mask in events:
                chunk = os.read(
                    key.fileobj.fileno(),
                    min(65_536, maximum_stdout + 1 - len(output)),
                )
                if not chunk:
                    selector.unregister(key.fileobj)
                    reached_eof = True
                    break
                output.extend(chunk)
                if len(output) > maximum_stdout:
                    raise GitRunnerError("Git inspection failed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GitRunnerError("Git inspection failed")
        returncode = process.wait(timeout=remaining)
        try:
            stdout = bytes(output).decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise GitRunnerError("Git inspection failed") from error
        completed = True
        return GitResult(returncode, stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise GitRunnerError("Git inspection failed") from error
    finally:
        selector.close()
        if not completed:
            _stop_and_reap(process)
        process.stdout.close()
