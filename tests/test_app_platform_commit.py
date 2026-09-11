import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server.app_platform_commit import (
    AppGitState,
    AppPlatformCommitError,
    AppPlatformCommitter,
)
from local_web_server.app_update_models import AppUpdatePlan, FileChange
from local_web_server.app_update_transaction import UpdatePublisher


class AppPlatformCommitterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "application"
        self.repository.mkdir()
        self.write("index.html", b"old index\n")
        self.write("package.json", b'{"old":true}\n')
        self.write("unrelated.txt", b"preserve me\n")
        self.git("init", "--initial-branch=main")
        self.git("add", "--all")
        self.git("-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "baseline")
        self.original_head = self.git("rev-parse", "HEAD").strip()
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@localhost")
        self.update_plan = AppUpdatePlan(
            app_id="meals",
            mode="refresh",
            current_ui_version="1.0.0",
            target_ui_version="2.0.0",
            target_ui_sha256="a" * 64,
            capabilities=(),
            changes=(
                FileChange(Path("index.html"), b"old index\n", b"new index\n"),
                FileChange(Path("package.json"), b'{"old":true}\n', b'{"new":true}\n'),
                FileChange(Path("vendor/local-web-ui.tgz"), None, b"reviewed artifact\n"),
            ),
        )
        self.committer = AppPlatformCommitter()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def git(self, *arguments: str) -> str:
        return subprocess.run(
            ("git", *arguments),
            cwd=self.repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def write(self, path: str, content: bytes) -> None:
        target = self.repository / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    def publish(self, changes: tuple[FileChange, ...]) -> None:
        UpdatePublisher().publish(self.repository, changes, lambda: None)

    def assert_private_error(self, call) -> None:
        with self.assertRaises(AppPlatformCommitError) as raised:
            call()
        self.assertNotIn(str(self.repository), str(raised.exception))
        self.assertNotIn("private", str(raised.exception))

    @staticmethod
    def wait_for_process_exit(pid: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.01)
        return False

    def test_inspect_clean_main_returns_canonical_repository_and_exact_head(self):
        state = self.committer.inspect_clean_main(self.repository)

        self.assertEqual(
            state,
            AppGitState(repository=self.repository.resolve(), head=self.original_head),
        )

    def test_inspect_clean_main_reports_stable_block_reasons(self):
        missing = self.repository.parent / "missing"
        cases = (
            ("unavailable", missing, (), "repository-unavailable"),
            ("not main", self.repository, ("switch", "-c", "feature"), "repository-not-main"),
            ("detached", self.repository, ("checkout", "--detach"), "repository-not-main"),
            ("not clean", self.repository, (), "repository-not-clean"),
        )
        for name, repository, command, reason in cases:
            with self.subTest(name=name):
                self.git("switch", "main")
                self.git("reset", "--hard", self.original_head)
                self.git("clean", "-fdx")
                if command:
                    self.git(*command)
                if reason == "repository-not-clean":
                    self.write("private-untracked.txt", b"private\n")

                with self.assertRaises(AppPlatformCommitError) as raised:
                    self.committer.inspect_clean_main(repository)

                self.assertEqual(raised.exception.inspection_reason, reason)

    def test_inspection_fails_closed_when_git_output_exceeds_the_bound(self):
        for index in range(40):
            self.write(f"private-untracked-{index:02d}.txt", b"private\n")
        committer = AppPlatformCommitter(maximum_git_output_bytes=256)

        with self.assertRaises(AppPlatformCommitError) as raised:
            committer.inspect_clean_main(self.repository)

        self.assertEqual(
            raised.exception.inspection_reason, "repository-unavailable"
        )
        self.assertEqual(
            str(raised.exception), "application repository inspection failed"
        )

    def test_git_failures_reap_the_owned_group_without_signalling_an_unrelated_group(self):
        original_popen = subprocess.Popen
        for outcome in ("overflow", "timeout", "leader-exit", "decode-error"):
            with self.subTest(outcome=outcome):
                pid_file = Path(self.temporary.name) / f"{outcome}-descendant.pid"
                owned = []
                unrelated = original_popen(
                    (sys.executable, "-c", "import time; time.sleep(30)"),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    def launch(arguments, **kwargs):
                        self.assertEqual(arguments[:4], (
                            "git",
                            "-C",
                            str(self.repository.resolve()),
                            "--literal-pathspecs",
                        ))
                        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
                        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
                        self.assertTrue(kwargs["start_new_session"])
                        child_stdout = (
                            "None"
                            if outcome == "leader-exit"
                            else "subprocess.DEVNULL"
                        )
                        action = {
                            "overflow": "os.write(1, b'x' * 4096); time.sleep(30)",
                            "timeout": "time.sleep(30)",
                            "leader-exit": "pass",
                            "decode-error": "os.write(1, b'\\xff')",
                        }[outcome]
                        script = (
                            "import os,subprocess,sys,time;"
                            f"child=subprocess.Popen((sys.executable,'-c','import time; time.sleep(30)'),stdout={child_stdout},stderr=subprocess.DEVNULL);"
                            f"open({str(pid_file)!r},'w').write(str(child.pid));"
                            f"{action}"
                        )
                        process = original_popen(
                            (sys.executable, "-c", script),
                            **kwargs,
                        )
                        owned.append(process)
                        return process

                    committer = AppPlatformCommitter(
                        maximum_git_output_bytes=64,
                        git_timeout_seconds=0.2,
                        git_stop_seconds=0.5,
                    )
                    with patch(
                        "local_web_server.app_platform_commit.subprocess.Popen",
                        side_effect=launch,
                    ):
                        self.assert_private_error(
                            lambda: committer.inspect_clean_main(self.repository)
                        )

                    deadline = time.monotonic() + 1.0
                    while not pid_file.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(pid_file.exists())
                    descendant = int(pid_file.read_text())
                    self.assertTrue(all(process.poll() is not None for process in owned))
                    self.assertTrue(self.wait_for_process_exit(descendant))
                    self.assertIsNone(unrelated.poll())
                finally:
                    try:
                        os.killpg(unrelated.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    unrelated.wait(timeout=1)

    def test_git_reaps_non_pipe_descendants_after_every_leader_return(self):
        original_popen = subprocess.Popen
        cases = (
            ("zero-checked", 0, True, False),
            ("zero-unchecked", 0, False, False),
            ("nonzero-checked", 1, True, True),
            ("nonzero-unchecked", 1, False, False),
        )
        for name, return_code, check, expect_error in cases:
            with self.subTest(name=name):
                pid_file = Path(self.temporary.name) / f"{name}-descendant.pid"
                owned = []
                unrelated = original_popen(
                    (sys.executable, "-c", "import time; time.sleep(30)"),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    def launch(arguments, **kwargs):
                        self.assertEqual(arguments[:4], (
                            "git",
                            "-C",
                            str(self.repository),
                            "--literal-pathspecs",
                        ))
                        script = (
                            "import os,subprocess,sys;"
                            "child=subprocess.Popen((sys.executable,'-c','import time; time.sleep(30)'),"
                            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
                            f"open({str(pid_file)!r},'w').write(str(child.pid));"
                            "os.write(1,b'reviewed-output\\n');"
                            f"sys.exit({return_code})"
                        )
                        process = original_popen(
                            (sys.executable, "-c", script),
                            **kwargs,
                        )
                        owned.append(process)
                        return process

                    with patch(
                        "local_web_server.app_platform_commit.subprocess.Popen",
                        side_effect=launch,
                    ):
                        if expect_error:
                            self.assert_private_error(
                                lambda: self.committer._git(
                                    self.repository, "status", check=check
                                )
                            )
                        else:
                            result = self.committer._git(
                                self.repository, "status", check=check
                            )
                            self.assertEqual(result.returncode, return_code)
                            self.assertEqual(result.stdout, "reviewed-output\n")

                    self.assertTrue(pid_file.exists())
                    descendant = int(pid_file.read_text())
                    self.assertTrue(all(process.poll() is not None for process in owned))
                    self.assertTrue(self.wait_for_process_exit(descendant))
                    self.assertIsNone(unrelated.poll())
                finally:
                    for process in owned:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    try:
                        os.killpg(unrelated.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    unrelated.wait(timeout=1)

    def test_commit_update_creates_an_exact_path_only_commit(self):
        self.publish(self.update_plan.changes)

        commit = self.committer.commit_update(
            self.repository, self.update_plan, expected_head=self.original_head
        )

        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        self.assertEqual(
            self.git("diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines(),
            ["index.html", "package.json", "vendor/local-web-ui.tgz"],
        )
        self.assertEqual(
            self.git("show", "-s", "--format=%s", commit).strip(),
            "chore: refresh local-web platform for meals",
        )
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.repository / "unrelated.txt").read_bytes(), b"preserve me\n")

    def test_update_commit_fails_closed_when_git_output_exceeds_the_bound(self):
        before = b"private-before\n" * 32
        after = b"reviewed-after\n" * 32
        self.write("large-platform-file.txt", before)
        self.git("add", "large-platform-file.txt")
        self.git("commit", "-m", "large platform baseline")
        head = self.git("rev-parse", "HEAD").strip()
        plan = AppUpdatePlan(
            **{
                **self.update_plan.__dict__,
                "changes": (
                    FileChange(Path("large-platform-file.txt"), before, after),
                ),
            }
        )
        self.publish(plan.changes)
        committer = AppPlatformCommitter(maximum_git_output_bytes=256)

        self.assert_private_error(
            lambda: committer.commit_update(
                self.repository, plan, expected_head=head
            )
        )

        self.assertEqual(self.git("rev-parse", "HEAD").strip(), head)

    def test_commit_update_bypasses_hooks_and_global_signing(self):
        hook = self.repository / ".git/hooks/pre-commit"
        global_config = self.repository.parent / "private-global.gitconfig"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        global_config.write_text("[commit]\n\tgpgSign = true\n")
        self.publish(self.update_plan.changes)

        with patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(global_config)}):
            commit = self.committer.commit_update(
                self.repository, self.update_plan, expected_head=self.original_head
            )

        self.assertRegex(commit, r"^[0-9a-f]{40}$")

    def test_inspect_refuses_detached_wrong_branch_and_any_unrelated_visible_state(self):
        cases = (
            ("detached", lambda: self.git("checkout", "--detach")),
            ("wrong branch", lambda: self.git("switch", "-c", "private-feature")),
            ("modified", lambda: self.write("unrelated.txt", b"private modified\n")),
            ("staged", lambda: (self.write("unrelated.txt", b"private staged\n"), self.git("add", "unrelated.txt"))),
            ("untracked", lambda: self.write("private-untracked.txt", b"private\n")),
        )
        for name, prepare in cases:
            with self.subTest(name=name):
                self.git("switch", "main")
                self.git("reset", "--hard", self.original_head)
                self.git("clean", "-fdx")
                prepare()

                self.assert_private_error(
                    lambda: self.committer.inspect_clean_main(self.repository)
                )

    def test_inspect_allows_unrelated_ignored_files(self):
        self.write(".gitignore", b"*.private\n")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore private files")
        expected_head = self.git("rev-parse", "HEAD").strip()
        self.write("secret.private", b"preserve private ignored data\n")

        state = self.committer.inspect_clean_main(self.repository)

        self.assertEqual(state.head, expected_head)
        self.assertEqual(
            (self.repository / "secret.private").read_bytes(),
            b"preserve private ignored data\n",
        )

    def test_commit_refuses_a_changed_expected_head_or_unrelated_state(self):
        self.publish(self.update_plan.changes)
        self.write("unrelated.txt", b"private head changed\n")
        self.git("add", "unrelated.txt")
        self.git("commit", "-m", "private head changed")
        changed_head = self.git("rev-parse", "HEAD").strip()

        self.assert_private_error(
            lambda: self.committer.commit_update(
                self.repository, self.update_plan, expected_head=self.original_head
            )
        )
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), changed_head)

    def test_commit_refuses_unrelated_modified_staged_or_untracked_files(self):
        cases = (
            ("modified", lambda: self.write("unrelated.txt", b"private modified\n")),
            ("staged", lambda: (self.write("unrelated.txt", b"private staged\n"), self.git("add", "unrelated.txt"))),
            ("untracked", lambda: self.write("private-untracked.txt", b"private\n")),
        )
        for name, prepare in cases:
            with self.subTest(name=name):
                self.git("reset", "--hard", self.original_head)
                self.git("clean", "-fdx")
                self.publish(self.update_plan.changes)
                prepare()
                head = self.git("rev-parse", "HEAD").strip()

                self.assert_private_error(
                    lambda: self.committer.commit_update(
                        self.repository, self.update_plan, expected_head=head
                    )
                )
                self.assertEqual(self.git("rev-parse", "HEAD").strip(), head)

    def test_commit_preserves_unrelated_ignored_files(self):
        self.write(".gitignore", b"*.private\n")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore private files")
        expected_head = self.git("rev-parse", "HEAD").strip()
        self.write("secret.private", b"preserve private ignored data\n")
        self.publish(self.update_plan.changes)

        commit = self.committer.commit_update(
            self.repository, self.update_plan, expected_head=expected_head
        )

        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        self.assertEqual(
            (self.repository / "secret.private").read_bytes(),
            b"preserve private ignored data\n",
        )
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_commit_refuses_an_ignored_planned_target(self):
        self.write(".gitignore", b"vendor/\n")
        self.git("add", ".gitignore")
        self.git("commit", "-m", "ignore vendor output")
        expected_head = self.git("rev-parse", "HEAD").strip()
        self.publish(self.update_plan.changes)

        self.assert_private_error(
            lambda: self.committer.commit_update(
                self.repository, self.update_plan, expected_head=expected_head
            )
        )
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), expected_head)

    def test_commit_refuses_wrong_before_or_after_bytes_and_missing_or_extra_diff_paths(self):
        cases = (
            ("wrong before", lambda: self.write("index.html", b"wrong before\n")),
            ("wrong after", lambda: (self.publish(self.update_plan.changes), self.write("index.html", b"wrong after\n"))),
            ("missing diff", lambda: self.write("index.html", b"new index\n")),
            ("extra diff", lambda: (self.publish(self.update_plan.changes), self.write("unrelated.txt", b"private extra\n"))),
        )
        for name, prepare in cases:
            with self.subTest(name=name):
                self.git("reset", "--hard", self.original_head)
                self.git("clean", "-fdx")
                prepare()
                head = self.git("rev-parse", "HEAD").strip()

                self.assert_private_error(
                    lambda: self.committer.commit_update(
                        self.repository, self.update_plan, expected_head=head
                    )
                )
                self.assertEqual(self.git("rev-parse", "HEAD").strip(), head)

    def test_commit_handles_literal_pathspec_magic_names_and_refuses_nested_git_roots(self):
        magic_plan = AppUpdatePlan(
            **{
                **self.update_plan.__dict__,
                "changes": (FileChange(Path(":(top)literal.txt"), None, b"literal\n"),),
            }
        )
        self.publish(magic_plan.changes)

        commit = self.committer.commit_update(
            self.repository, magic_plan, expected_head=self.original_head
        )

        self.assertEqual(
            self.git("diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines(),
            [":(top)literal.txt"],
        )
        self.git("reset", "--hard", self.original_head)
        nested = self.repository / "nested"
        nested.mkdir()
        subprocess.run(("git", "init"), cwd=nested, check=True, capture_output=True)
        nested_plan = AppUpdatePlan(
            **{
                **self.update_plan.__dict__,
                "changes": (FileChange(Path("nested/target.txt"), None, b"private\n"),),
            }
        )
        self.publish(nested_plan.changes)

        self.assert_private_error(
            lambda: self.committer.commit_update(
                self.repository, nested_plan, expected_head=self.original_head
            )
        )

    def test_commit_restoration_creates_exact_inverse_commit(self):
        self.publish(self.update_plan.changes)
        update_commit = self.committer.commit_update(
            self.repository, self.update_plan, expected_head=self.original_head
        )
        inverse = tuple(
            FileChange(change.path, change.after, change.before)
            for change in self.update_plan.changes
        )
        self.publish(inverse)

        restoration = self.committer.commit_restoration(
            self.repository, self.update_plan, expected_head=update_commit
        )

        self.assertEqual(
            self.git("diff-tree", "--no-commit-id", "--name-only", "-r", restoration).splitlines(),
            ["index.html", "package.json", "vendor/local-web-ui.tgz"],
        )
        self.assertEqual(
            self.git("show", "-s", "--format=%s", restoration).strip(),
            "chore: restore local-web platform for meals",
        )
        self.git(
            "diff",
            "--quiet",
            self.original_head,
            restoration,
            "--",
            *(change.path.as_posix() for change in self.update_plan.changes),
        )

    def test_restoration_commit_fails_closed_when_git_output_exceeds_the_bound(self):
        before = b"private-before\n" * 32
        after = b"reviewed-after\n" * 32
        self.write("large-platform-file.txt", before)
        self.git("add", "large-platform-file.txt")
        self.git("commit", "-m", "large platform baseline")
        head = self.git("rev-parse", "HEAD").strip()
        plan = AppUpdatePlan(
            **{
                **self.update_plan.__dict__,
                "changes": (
                    FileChange(Path("large-platform-file.txt"), before, after),
                ),
            }
        )
        self.publish(plan.changes)
        update_commit = self.committer.commit_update(
            self.repository, plan, expected_head=head
        )
        inverse = tuple(
            FileChange(change.path, change.after, change.before)
            for change in plan.changes
        )
        self.publish(inverse)
        committer = AppPlatformCommitter(maximum_git_output_bytes=256)

        self.assert_private_error(
            lambda: committer.commit_restoration(
                self.repository, plan, expected_head=update_commit
            )
        )

        self.assertEqual(self.git("rev-parse", "HEAD").strip(), update_commit)

    def test_restoration_refuses_stale_head_unrelated_state_or_wrong_restored_bytes(self):
        self.publish(self.update_plan.changes)
        update_commit = self.committer.commit_update(
            self.repository, self.update_plan, expected_head=self.original_head
        )
        inverse = tuple(FileChange(change.path, change.after, change.before) for change in self.update_plan.changes)
        cases = (
            ("stale head", lambda: (self.publish(inverse), self.git("commit", "--allow-empty", "-m", "private head changed"))),
            ("unrelated", lambda: (self.publish(inverse), self.write("unrelated.txt", b"private state\n"))),
            ("wrong bytes", lambda: (self.publish(inverse), self.write("index.html", b"wrong restored\n"))),
        )
        for name, prepare in cases:
            with self.subTest(name=name):
                self.git("reset", "--hard", update_commit)
                self.git("clean", "-fdx")
                prepare()
                head = self.git("rev-parse", "HEAD").strip()

                self.assert_private_error(
                    lambda: self.committer.commit_restoration(
                        self.repository, self.update_plan, expected_head=update_commit
                    )
                )
                self.assertEqual(self.git("rev-parse", "HEAD").strip(), head)

    def test_commit_failure_does_not_claim_a_restoration(self):
        self.publish(self.update_plan.changes)
        update_commit = self.committer.commit_update(
            self.repository, self.update_plan, expected_head=self.original_head
        )
        inverse = tuple(FileChange(change.path, change.after, change.before) for change in self.update_plan.changes)
        self.publish(inverse)
        original_git = self.committer._git

        def fail_commit(repository, *arguments, **kwargs):
            if "commit" in arguments:
                raise AppPlatformCommitError("private commit detail")
            return original_git(repository, *arguments, **kwargs)

        with patch.object(self.committer, "_git", side_effect=fail_commit):
            self.assert_private_error(
                lambda: self.committer.commit_restoration(
                    self.repository, self.update_plan, expected_head=update_commit
                )
            )
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), update_commit)


if __name__ == "__main__":
    unittest.main()
