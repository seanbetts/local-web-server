"""Contained browser compatibility gate for a candidate cosmetic theme."""

import os
import re
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from .theme import ThemeError, validate_theme


_DEFAULT_NPM = Path("/opt/homebrew/bin/npm")
_DEFAULT_GIT = Path("/usr/bin/git")
_MAX_THEME_BYTES = 1024 * 1024
_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
_PROCESS_STOP_SECONDS = 0.1
_PROCESS_POLL_SECONDS = 0.02
_COMMIT_PATTERN = re.compile(rb"[0-9a-f]{40,64}\n?")


class ThemeReleaseError(RuntimeError):
    """The candidate did not pass the complete compatibility gate."""


class ThemeGate(Protocol):
    def verify(self, candidate: Path) -> None: ...


class _Process(Protocol):
    pid: int
    returncode: int | None

    def communicate(self, timeout: float | None = None) -> tuple[bytes, bytes]: ...

    def poll(self) -> int | None: ...


PopenFactory = Callable[..., _Process]


class ThemeReleaseGate:
    """Build and browser-test a pinned candidate without exposing diagnostics."""

    SNAPSHOT_NAMES = (
        "gallery-light-desktop.png",
        "gallery-dark-desktop.png",
        "gallery-light-mobile.png",
        "gallery-dark-mobile.png",
    )
    _COMMANDS = (
        ("run", "build:gallery"),
        ("run", "test:gallery", "--", "--run"),
        ("run", "test:gallery:e2e"),
    )

    def __init__(
        self,
        repository: Path,
        *,
        npm: Path = _DEFAULT_NPM,
        git: Path = _DEFAULT_GIT,
        popen: PopenFactory = subprocess.Popen,
        timeout_seconds: float = 180.0,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        try:
            self.repository = Path(repository).resolve(strict=True)
            self.npm = Path(npm).resolve(strict=True)
            self.git = Path(git).resolve(strict=True)
            npm_metadata = self.npm.stat()
            git_metadata = self.git.stat()
            if (
                not self.repository.is_dir()
                or not stat.S_ISREG(npm_metadata.st_mode)
                or not stat.S_ISREG(git_metadata.st_mode)
                or not os.access(self.npm, os.X_OK)
                or not os.access(self.git, os.X_OK)
                or timeout_seconds <= 0
            ):
                raise ThemeReleaseError("theme compatibility gate failed")
        except ThemeReleaseError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise ThemeReleaseError("theme compatibility gate failed") from error
        self.popen = popen
        self.timeout_seconds = timeout_seconds
        self.monotonic = monotonic

    @staticmethod
    def _read_candidate(candidate: Path) -> bytes:
        descriptor = -1
        try:
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ThemeReleaseError("theme compatibility gate failed")
            descriptor = os.open(
                candidate,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            pinned = os.fstat(descriptor)
            if (
                not stat.S_ISREG(pinned.st_mode)
                or (metadata.st_dev, metadata.st_ino) != (pinned.st_dev, pinned.st_ino)
            ):
                raise ThemeReleaseError("theme compatibility gate failed")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                content = source.read(_MAX_THEME_BYTES + 1)
            if len(content) > _MAX_THEME_BYTES:
                raise ThemeReleaseError("theme compatibility gate failed")
            validate_theme(content)
            return content
        except ThemeReleaseError:
            raise
        except (OSError, ThemeError, ValueError) as error:
            raise ThemeReleaseError("theme compatibility gate failed") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)

    @staticmethod
    def _read_regular_file(path: Path, maximum_bytes: int) -> bytes:
        descriptor = -1
        try:
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise ThemeReleaseError("theme compatibility gate failed")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            pinned = os.fstat(descriptor)
            if (
                not stat.S_ISREG(pinned.st_mode)
                or (metadata.st_dev, metadata.st_ino) != (pinned.st_dev, pinned.st_ino)
            ):
                raise ThemeReleaseError("theme compatibility gate failed")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                content = source.read(maximum_bytes + 1)
            if not content or len(content) > maximum_bytes:
                raise ThemeReleaseError("theme compatibility gate failed")
            return content
        except ThemeReleaseError:
            raise
        except OSError as error:
            raise ThemeReleaseError("theme compatibility gate failed") from error
        finally:
            if descriptor != -1:
                os.close(descriptor)

    @staticmethod
    def _git_environment() -> dict[str, str]:
        return {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "HOME": os.fspath(Path.home()),
            "LANG": "en_GB.UTF-8",
            "LC_ALL": "en_GB.UTF-8",
            "PATH": "/usr/bin:/bin",
        }

    def _run_git(self, arguments: Sequence[str], deadline: float) -> bytes:
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise ThemeReleaseError("theme compatibility gate failed")
        try:
            result = subprocess.run(
                (os.fspath(self.git), "-C", os.fspath(self.repository), *arguments),
                cwd=self.repository,
                env=self._git_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=remaining,
                check=False,
            )
            if result.returncode != 0:
                raise ThemeReleaseError("theme compatibility gate failed")
            return result.stdout
        except ThemeReleaseError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ThemeReleaseError("theme compatibility gate failed") from error

    def _verify_snapshots(self, deadline: float) -> tuple[str, dict[str, bytes]]:
        root = self.repository / "examples" / "ui-gallery" / "tests" / "snapshots"
        try:
            commit_output = self._run_git(("rev-parse", "--verify", "HEAD^{commit}"), deadline)
            if not _COMMIT_PATTERN.fullmatch(commit_output):
                raise ThemeReleaseError("theme compatibility gate failed")
            commit = commit_output.strip().decode("ascii")
            authoritative: dict[str, bytes] = {}
            for name in self.SNAPSHOT_NAMES:
                path = root / name
                relative = path.relative_to(self.repository).as_posix()
                committed = self._run_git(("cat-file", "blob", f"{commit}:{relative}"), deadline)
                if not committed or len(committed) > _MAX_SNAPSHOT_BYTES:
                    raise ThemeReleaseError("theme compatibility gate failed")
                if self._read_regular_file(path, _MAX_SNAPSHOT_BYTES) != committed:
                    raise ThemeReleaseError("theme compatibility gate failed")
                authoritative[name] = committed
            return commit, authoritative
        except ThemeReleaseError:
            raise
        except (OSError, UnicodeError, ValueError) as error:
            raise ThemeReleaseError("theme compatibility gate failed") from error

    def _prepare_checkout(
        self,
        temporary_root: Path,
        commit: str,
        authoritative: dict[str, bytes],
        deadline: float,
    ) -> Path:
        archive = temporary_root / "repository.tar"
        checkout = temporary_root / "repository"
        checkout.mkdir(mode=0o700)
        self._run_git(
            ("archive", "--format=tar", f"--output={archive}", commit),
            deadline,
        )
        try:
            with tarfile.open(archive, mode="r:") as source:
                source.extractall(checkout, filter="data")
            if self.monotonic() >= deadline:
                raise ThemeReleaseError("theme compatibility gate failed")
            snapshot_root = checkout / "examples" / "ui-gallery" / "tests" / "snapshots"
            for name, expected in authoritative.items():
                if self._read_regular_file(snapshot_root / name, _MAX_SNAPSHOT_BYTES) != expected:
                    raise ThemeReleaseError("theme compatibility gate failed")
            shared_dependencies = self.repository.joinpath("node_modules").resolve(strict=True)
            private_dependencies = checkout / "node_modules"
            if not shared_dependencies.is_dir() or private_dependencies.exists():
                raise ThemeReleaseError("theme compatibility gate failed")
            private_dependencies.mkdir(mode=0o700)
            for dependency in shared_dependencies.iterdir():
                if dependency.name == "@local-web":
                    continue
                private_dependencies.joinpath(dependency.name).symlink_to(
                    dependency,
                    target_is_directory=dependency.is_dir(),
                )
            private_scope = private_dependencies / "@local-web"
            private_scope.mkdir(mode=0o700)
            private_ui = private_scope / "ui"
            private_ui.symlink_to("../../packages/ui", target_is_directory=True)
            archived_ui = checkout.joinpath("packages", "ui").resolve(strict=True)
            if not archived_ui.is_dir() or private_ui.resolve(strict=True) != archived_ui:
                raise ThemeReleaseError("theme compatibility gate failed")
            return checkout
        except ThemeReleaseError:
            raise
        except (OSError, tarfile.TarError, ValueError) as error:
            raise ThemeReleaseError("theme compatibility gate failed") from error

    @staticmethod
    def _minimal_environment(candidate: Path, temporary_root: Path) -> dict[str, str]:
        return {
            "CI": "1",
            "HOME": os.fspath(Path.home()),
            "LANG": "en_GB.UTF-8",
            "LC_ALL": "en_GB.UTF-8",
            "LWP_THEME_CANDIDATE": os.fspath(candidate),
            "PATH": "/opt/homebrew/bin:/usr/bin:/bin",
            "TMPDIR": os.fspath(temporary_root),
        }

    @staticmethod
    def _process_group_alive(process_group: int) -> bool:
        try:
            os.killpg(process_group, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError as error:
            raise ThemeReleaseError("theme compatibility gate failed") from error

    def _poll_process_group(self, process: _Process, deadline: float) -> bool:
        while True:
            process.poll()
            if not self._process_group_alive(process.pid):
                return True
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(_PROCESS_POLL_SECONDS, remaining))

    def _stop_group(self, process: _Process, deadline: float) -> None:
        process.poll()
        if not self._process_group_alive(process.pid):
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError as error:
            raise ThemeReleaseError("theme compatibility gate failed") from error
        term_deadline = min(deadline, self.monotonic() + _PROCESS_STOP_SECONDS)
        if self._poll_process_group(process, term_deadline):
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError as error:
            raise ThemeReleaseError("theme compatibility gate failed") from error
        kill_deadline = min(deadline, self.monotonic() + _PROCESS_STOP_SECONDS)
        if not self._poll_process_group(process, kill_deadline):
            raise ThemeReleaseError("theme compatibility gate failed")

    def _run(
        self,
        argv: Sequence[str],
        environment: dict[str, str],
        deadline: float,
        repository: Path,
    ) -> None:
        remaining = deadline - self.monotonic()
        cleanup_reserve = _PROCESS_STOP_SECONDS * 2
        if remaining <= cleanup_reserve:
            raise ThemeReleaseError("theme compatibility gate failed")
        try:
            process = self.popen(
                tuple(argv),
                cwd=repository,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                close_fds=True,
                start_new_session=True,
            )
            command_error: BaseException | None = None
            try:
                process.communicate(timeout=remaining - cleanup_reserve)
            except (OSError, subprocess.SubprocessError, ValueError) as error:
                command_error = error
            cleanup_error: ThemeReleaseError | None = None
            try:
                self._stop_group(process, deadline)
            except ThemeReleaseError as error:
                cleanup_error = error
            if command_error is not None or process.returncode != 0 or cleanup_error is not None:
                raise ThemeReleaseError("theme compatibility gate failed") from (
                    command_error or cleanup_error
                )
        except ThemeReleaseError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            raise ThemeReleaseError("theme compatibility gate failed") from error

    def verify(self, candidate: Path) -> None:
        """Require contract, axe, and four reviewed visual baselines to pass."""
        deadline = self.monotonic() + self.timeout_seconds
        content = self._read_candidate(Path(candidate))
        commit, authoritative = self._verify_snapshots(deadline)
        try:
            with tempfile.TemporaryDirectory(prefix="local-web-theme-gate-") as directory:
                temporary_root = Path(directory)
                temporary_root.chmod(0o700)
                checkout = self._prepare_checkout(
                    temporary_root,
                    commit,
                    authoritative,
                    deadline,
                )
                pinned = temporary_root / "candidate.css"
                descriptor = os.open(
                    pinned,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                try:
                    with os.fdopen(descriptor, "wb") as output:
                        descriptor = -1
                        output.write(content)
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    if descriptor != -1:
                        os.close(descriptor)
                environment = self._minimal_environment(pinned, temporary_root)
                for command in self._COMMANDS:
                    self._run(
                        (os.fspath(self.npm), *command),
                        environment,
                        deadline,
                        checkout,
                    )
        except ThemeReleaseError:
            raise
        except (OSError, RuntimeError, ValueError) as error:
            raise ThemeReleaseError("theme compatibility gate failed") from error
