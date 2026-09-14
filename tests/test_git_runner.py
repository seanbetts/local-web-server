import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from local_web_server.git_runner import GitRunnerError, run_git


class GitRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repository"
        subprocess.run(
            ("/usr/bin/git", "init", "-q", self.repository),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _executable(self, source: str) -> Path:
        executable = self.root / "git-fixture"
        executable.write_text("#!/bin/sh\n" + source, encoding="utf-8")
        executable.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        return executable

    @staticmethod
    def _wait_for_process_exit(pid: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.01)
        return False

    def test_ignores_ambient_path_and_git_configuration(self):
        malicious = self.root / "ambient"
        malicious.mkdir()
        marker = self.root / "ambient-git-ran"
        fake = malicious / "git"
        fake.write_text(f"#!/bin/sh\ntouch {marker!s}\nexit 91\n", encoding="utf-8")
        fake.chmod(0o700)

        with patch.dict(
            os.environ,
            {
                "PATH": os.fspath(malicious),
                "GIT_DIR": os.fspath(self.root / "wrong-git-dir"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "alias.rev-parse",
                "GIT_CONFIG_VALUE_0": "!false",
            },
        ):
            result = run_git(
                self.repository,
                ("rev-parse", "--show-toplevel"),
                maximum_stdout=4096,
            )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, os.fspath(self.repository.resolve()) + "\n")
        self.assertFalse(marker.exists())

    def test_timeout_terminates_and_reaps_its_process_group(self):
        executable = self._executable(
            "echo $$ > runner.pid\n"
            "sleep 30\n"
        )
        started = time.monotonic()
        with (
            patch("local_web_server.git_runner._GIT_CANDIDATES", (executable,)),
            patch("local_web_server.git_runner._GIT_TIMEOUT_SECONDS", 0.5),
            self.assertRaises(GitRunnerError),
        ):
            run_git(self.repository, ("status",), maximum_stdout=16)

        self.assertLess(time.monotonic() - started, 3)
        pid = int((self.repository / "runner.pid").read_text(encoding="ascii"))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_timeout_stops_descendant_when_git_leader_has_already_exited(self):
        executable = self._executable(
            "echo $$ > runner.pid\n"
            "sleep 30 &\n"
            "echo $! > descendant.pid\n"
            "exit 0\n"
        )
        child_pid = None
        try:
            with (
                patch("local_web_server.git_runner._GIT_CANDIDATES", (executable,)),
                patch("local_web_server.git_runner._GIT_TIMEOUT_SECONDS", 0.5),
                self.assertRaises(GitRunnerError),
            ):
                run_git(self.repository, ("status",), maximum_stdout=16)

            child_pid = int(
                (self.repository / "descendant.pid").read_text(encoding="ascii")
            )
            self.assertTrue(self._wait_for_process_exit(child_pid))
        finally:
            if child_pid is not None:
                try:
                    os.kill(child_pid, 9)
                except ProcessLookupError:
                    pass

    def test_stdout_flood_is_bounded_and_process_is_reaped(self):
        executable = self._executable(
            "echo $$ > runner.pid\n"
            "while :; do printf '0123456789abcdef'; done\n"
        )
        with (
            patch("local_web_server.git_runner._GIT_CANDIDATES", (executable,)),
            self.assertRaises(GitRunnerError),
        ):
            run_git(self.repository, ("status",), maximum_stdout=32)

        pid = int((self.repository / "runner.pid").read_text(encoding="ascii"))
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_non_utf8_stdout_is_rejected_with_a_bounded_error(self):
        executable = self._executable("printf '\\377'\n")
        with (
            patch("local_web_server.git_runner._GIT_CANDIDATES", (executable,)),
            self.assertRaises(GitRunnerError) as raised,
        ):
            run_git(self.repository, ("status",), maximum_stdout=16)

        self.assertEqual(str(raised.exception), "Git inspection failed")


if __name__ == "__main__":
    unittest.main()
