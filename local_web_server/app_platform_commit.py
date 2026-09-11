"""Exact Git commits for already-published application platform updates."""

from __future__ import annotations

import os
import re
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .app_update_models import AppUpdatePlan, FileChange


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_GIT_OUTPUT_LIMIT_BYTES = 16 * 1024 * 1024
_GIT_READ_BYTES = 64 * 1024
_GIT_TIMEOUT_SECONDS = 15.0
_GIT_STOP_SECONDS = 0.5
_GIT_POLL_SECONDS = 0.01
_ENVIRONMENT = {
    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_NO_REPLACE_OBJECTS": "1",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_TERMINAL_PROMPT": "0",
}
RepositoryInspectionReason = Literal[
    "repository-unavailable",
    "repository-not-main",
    "repository-not-clean",
]


@dataclass(frozen=True)
class AppGitState:
    repository: Path
    head: str


class AppPlatformCommitError(RuntimeError):
    """An application platform commit cannot be made safely."""

    def __init__(
        self,
        message: str,
        *,
        inspection_reason: RepositoryInspectionReason | None = None,
    ) -> None:
        super().__init__(message)
        self.inspection_reason = inspection_reason


class _RepositoryInspectionFailure(ValueError):
    def __init__(self, reason: RepositoryInspectionReason) -> None:
        super().__init__()
        self.reason = reason


@dataclass(frozen=True)
class _Target:
    path: Path
    before: bytes | None
    after: bytes | None

    @property
    def text(self) -> str:
        return self.path.as_posix()


class AppPlatformCommitter:
    """Commit only the exact files and bytes described by an update plan."""

    def __init__(
        self,
        *,
        maximum_git_output_bytes: int = _GIT_OUTPUT_LIMIT_BYTES,
        git_timeout_seconds: float = _GIT_TIMEOUT_SECONDS,
        git_stop_seconds: float = _GIT_STOP_SECONDS,
    ) -> None:
        if (
            type(maximum_git_output_bytes) is not int
            or maximum_git_output_bytes <= 0
        ):
            raise ValueError("Git output bound must be positive")
        if git_timeout_seconds <= 0 or git_stop_seconds <= 0:
            raise ValueError("Git process timing must be positive")
        self._maximum_git_output_bytes = maximum_git_output_bytes
        self._git_timeout_seconds = git_timeout_seconds
        self._git_stop_seconds = git_stop_seconds

    def inspect_clean_main(self, repository: Path) -> AppGitState:
        try:
            root, head = self._repository_state(repository, inspection=True)
            self._require_clean(root, inspection=True)
            return AppGitState(repository=root, head=head)
        except _RepositoryInspectionFailure as error:
            raise AppPlatformCommitError(
                "application repository inspection failed",
                inspection_reason=error.reason,
            ) from error
        except AppPlatformCommitError:
            raise AppPlatformCommitError(
                "application repository inspection failed",
                inspection_reason="repository-unavailable",
            ) from None
        except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
            raise AppPlatformCommitError(
                "application repository inspection failed",
                inspection_reason="repository-unavailable",
            ) from error

    def commit_update(
        self, repository: Path, plan: AppUpdatePlan, expected_head: str
    ) -> str:
        return self._commit(
            repository,
            plan,
            expected_head,
            restoring=False,
        )

    def commit_restoration(
        self, repository: Path, plan: AppUpdatePlan, expected_head: str
    ) -> str:
        return self._commit(
            repository,
            plan,
            expected_head,
            restoring=True,
        )

    def _commit(
        self,
        repository: Path,
        plan: AppUpdatePlan,
        expected_head: str,
        *,
        restoring: bool,
    ) -> str:
        try:
            root, head = self._repository_state(repository)
            targets = self._targets(root, plan)
            message = (
                f"chore: restore local-web platform for {plan.app_id}"
                if restoring
                else f"chore: refresh local-web platform for {plan.app_id}"
            )
            if not isinstance(expected_head, str) or not _COMMIT.fullmatch(expected_head):
                raise ValueError
            if head != expected_head:
                raise ValueError
            self._require_expected_worktree(root, head, targets, restoring=restoring)
            if restoring:
                baseline = self._git(root, "rev-parse", "--verify", "HEAD^").stdout.strip()
                self._require_target_head_bytes(
                    root, baseline, targets, restoring=False
                )
            intent_paths = self._intent_paths(targets, restoring=restoring)
            if intent_paths:
                self._git(root, "add", "--intent-to-add", "--", *intent_paths)
            try:
                self._git(
                    root,
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "commit.gpgSign=false",
                    "-c",
                    "user.name=Local Web Activation",
                    "-c",
                    "user.email=local-web-activation@localhost",
                    "commit",
                    "--only",
                    "--no-verify",
                    "-m",
                    message,
                    "--",
                    *(target.text for target in targets),
                )
            except AppPlatformCommitError as error:
                self._clear_intent_to_add(root, intent_paths)
                raise ValueError from error
            return self._verify_commit(
                root,
                expected_head,
                targets,
                restoring=restoring,
            )
        except AppPlatformCommitError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, subprocess.SubprocessError) as error:
            raise AppPlatformCommitError("application platform commit failed") from error

    @staticmethod
    def _intent_paths(targets: tuple[_Target, ...], *, restoring: bool) -> tuple[str, ...]:
        return tuple(
            target.text
            for target in targets
            if (target.after if restoring else target.before) is None
            and (target.before if restoring else target.after) is not None
        )

    def _clear_intent_to_add(self, repository: Path, paths: tuple[str, ...]) -> None:
        if paths:
            self._git(repository, "reset", "--quiet", "HEAD", "--", *paths, check=False)

    def _repository_state(
        self, repository: Path, *, inspection: bool = False
    ) -> tuple[Path, str]:
        try:
            root = Path(repository).resolve(strict=True)
            if not root.is_dir():
                raise ValueError
        except (OSError, RuntimeError, TypeError, ValueError):
            self._raise_repository_failure(inspection, "repository-unavailable")
        top = self._git(root, "rev-parse", "--show-toplevel").stdout.strip()
        branch_result = self._git(
            root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
        )
        if branch_result.returncode == 1:
            self._raise_repository_failure(inspection, "repository-not-main")
        if branch_result.returncode != 0:
            raise AppPlatformCommitError("application Git operation failed")
        branch = branch_result.stdout.strip()
        head = self._git(root, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        try:
            top_matches_root = Path(top).resolve(strict=True) == root
        except (OSError, RuntimeError, ValueError):
            top_matches_root = False
        if not top_matches_root or not _COMMIT.fullmatch(head):
            self._raise_repository_failure(inspection, "repository-unavailable")
        if branch != "main":
            self._raise_repository_failure(inspection, "repository-not-main")
        return root, head

    def _git(
        self,
        repository: Path,
        *arguments: str,
        text: bool = True,
        check: bool = True,
    ) -> subprocess.CompletedProcess:
        argv = (
            "git",
            "-C",
            str(repository),
            "--literal-pathspecs",
            *arguments,
        )
        process: subprocess.Popen[bytes] | None = None
        selector: selectors.BaseSelector | None = None
        cleanup_required = True
        try:
            process = subprocess.Popen(
                argv,
                env=_ENVIRONMENT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
            if process.stdout is None:
                raise OSError
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            output = bytearray()
            deadline = time.monotonic() + self._git_timeout_seconds
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, self._git_timeout_seconds)
                events = selector.select(remaining)
                if not events:
                    raise subprocess.TimeoutExpired(argv, self._git_timeout_seconds)
                for key, _ in events:
                    available = self._maximum_git_output_bytes + 1 - len(output)
                    chunk = os.read(
                        key.fd,
                        min(_GIT_READ_BYTES, max(1, available)),
                    )
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) > self._maximum_git_output_bytes:
                        raise AppPlatformCommitError(
                            "application Git operation failed"
                        )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, self._git_timeout_seconds)
            return_code = process.wait(timeout=remaining)
            stdout: str | bytes = bytes(output)
            if text:
                stdout = stdout.decode("utf-8")
            result = subprocess.CompletedProcess(argv, return_code, stdout, None)
            self._stop_git_process(process)
            cleanup_required = False
        except AppPlatformCommitError:
            raise
        except (OSError, UnicodeError, subprocess.SubprocessError) as error:
            raise AppPlatformCommitError(
                "application Git operation failed"
            ) from error
        finally:
            cleanup_error: AppPlatformCommitError | None = None
            if process is not None and cleanup_required:
                try:
                    self._stop_git_process(process)
                except AppPlatformCommitError as error:
                    cleanup_error = error
            if selector is not None:
                selector.close()
            if process is not None and process.stdout is not None:
                process.stdout.close()
            if cleanup_error is not None:
                raise cleanup_error
        if check and result.returncode != 0:
            raise AppPlatformCommitError("application Git operation failed")
        return result

    @staticmethod
    def _git_group_alive(process: subprocess.Popen[bytes]) -> bool:
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Darwin can report EPERM for a just-exited session leader before
            # its Popen object has observed the exit.  A live owned group is
            # same-user and remains signalable even after its leader exits.
            return process.poll() is None
        except OSError as error:
            raise AppPlatformCommitError(
                "application Git operation failed"
            ) from error

    def _wait_for_git_group(
        self, process: subprocess.Popen[bytes], deadline: float
    ) -> bool:
        while True:
            process.poll()
            if not self._git_group_alive(process):
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
            time.sleep(min(_GIT_POLL_SECONDS, remaining))

    def _stop_git_process(self, process: subprocess.Popen[bytes]) -> None:
        try:
            process.poll()
            if not self._git_group_alive(process):
                if process.poll() is None:
                    process.wait(timeout=self._git_stop_seconds)
                return
            for signal_number in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, signal_number)
                except ProcessLookupError:
                    pass
                if self._wait_for_git_group(
                    process, time.monotonic() + self._git_stop_seconds
                ):
                    return
            raise AppPlatformCommitError("application Git operation failed")
        except (OSError, subprocess.SubprocessError) as error:
            self._reap_git_leader(process)
            raise AppPlatformCommitError(
                "application Git operation failed"
            ) from error

    def _reap_git_leader(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        try:
            process.wait(timeout=self._git_stop_seconds)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=self._git_stop_seconds)

    def _targets(self, repository: Path, plan: AppUpdatePlan) -> tuple[_Target, ...]:
        if type(plan) is not AppUpdatePlan or type(plan.app_id) is not str or not plan.app_id:
            raise ValueError
        if type(plan.changes) is not tuple or not plan.changes:
            raise ValueError
        targets: list[_Target] = []
        seen: set[Path] = set()
        for change in plan.changes:
            if (
                type(change) is not FileChange
                or type(change.before) not in (bytes, type(None))
                or type(change.after) not in (bytes, type(None))
                or change.before == change.after
            ):
                raise ValueError
            path = change.path
            if (
                not isinstance(path, Path)
                or path.is_absolute()
                or not path.parts
                or any(part in ("", ".", "..") for part in path.parts)
                or path in seen
            ):
                raise ValueError
            seen.add(path)
            self._require_safe_target(repository, path)
            targets.append(_Target(path, change.before, change.after))
        return tuple(sorted(targets, key=lambda target: target.text))

    @staticmethod
    def _require_safe_target(repository: Path, path: Path) -> None:
        candidate = repository
        for part in path.parts[:-1]:
            candidate = candidate / part
            if candidate.is_symlink() or (candidate / ".git").exists():
                raise ValueError
        target = repository / path
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ValueError

    def _require_clean(self, repository: Path, *, inspection: bool = False) -> None:
        status = self._git(
            repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ).stdout
        if status:
            self._raise_repository_failure(inspection, "repository-not-clean")

    @staticmethod
    def _raise_repository_failure(
        inspection: bool, reason: RepositoryInspectionReason
    ) -> None:
        if inspection:
            raise _RepositoryInspectionFailure(reason)
        raise ValueError

    def _require_expected_worktree(
        self,
        repository: Path,
        head: str,
        targets: tuple[_Target, ...],
        *,
        restoring: bool,
    ) -> None:
        self._require_target_head_bytes(repository, head, targets, restoring=restoring)
        desired = {
            target.path: target.before if restoring else target.after
            for target in targets
        }
        for target in targets:
            self._require_target_bytes(repository, target.path, desired[target.path])
        staged = self._git(repository, "diff", "--cached", "--name-only", "--no-renames").stdout.splitlines()
        if staged:
            raise ValueError
        expected_tracked = sorted(
            target.text
            for target in targets
            if (target.after if restoring else target.before) is not None
        )
        changed = self._git(repository, "diff", "--name-only", "--no-renames").stdout.splitlines()
        if changed != expected_tracked:
            raise ValueError
        expected_untracked = sorted(
            target.text
            for target in targets
            if (target.after if restoring else target.before) is None
            and desired[target.path] is not None
        )
        untracked = self._git(
            repository, "ls-files", "--others", "--exclude-standard"
        ).stdout.splitlines()
        if untracked != expected_untracked:
            raise ValueError

    def _require_target_head_bytes(
        self,
        repository: Path,
        head: str,
        targets: tuple[_Target, ...],
        *,
        restoring: bool,
    ) -> None:
        for target in targets:
            expected = target.after if restoring else target.before
            result = self._git(
                repository,
                "show",
                f"{head}:{target.text}",
                text=False,
                check=False,
            )
            if expected is None:
                if result.returncode == 0:
                    raise ValueError
            elif result.returncode != 0 or result.stdout != expected:
                raise ValueError

    @staticmethod
    def _require_target_bytes(repository: Path, path: Path, expected: bytes | None) -> None:
        target = repository / path
        if expected is None:
            if target.exists() or target.is_symlink():
                raise ValueError
            return
        if target.is_symlink() or not target.is_file() or target.read_bytes() != expected:
            raise ValueError

    def _verify_commit(
        self,
        repository: Path,
        expected_head: str,
        targets: tuple[_Target, ...],
        *,
        restoring: bool,
    ) -> str:
        root, commit = self._repository_state(repository)
        if root != repository:
            raise ValueError
        parent = self._git(root, "rev-parse", "--verify", "HEAD^").stdout.strip()
        paths = self._git(
            root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit
        ).stdout.splitlines()
        desired = {
            target.path: target.before if restoring else target.after
            for target in targets
        }
        if (
            parent != expected_head
            or paths != [target.text for target in targets]
            or not _COMMIT.fullmatch(commit)
        ):
            raise ValueError
        for target in targets:
            result = self._git(
                root, "show", f"{commit}:{target.text}", text=False, check=False
            )
            if desired[target.path] is None:
                if result.returncode == 0:
                    raise ValueError
            elif result.returncode != 0 or result.stdout != desired[target.path]:
                raise ValueError
        if restoring:
            baseline = self._git(
                root, "rev-parse", "--verify", f"{expected_head}^"
            ).stdout.strip()
            restored = self._git(
                root,
                "diff",
                "--quiet",
                baseline,
                commit,
                "--",
                *(target.text for target in targets),
                check=False,
            )
            if restored.returncode != 0:
                raise ValueError
        self._require_clean(root)
        return commit
