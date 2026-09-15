"""Real command migration plus shared fixture execution/cleanup boundaries."""

import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from local_web_server import git_runner
from scripts.migration_fixture import private_environment, process_exists, run_bounded
from scripts.verify_service_command_migration import (
    ServiceCommandMigrationWorkflowVerifier,
)
from tests.suites import acceptance


class MigrationFixtureTests(unittest.TestCase):
    def test_timeout_and_output_limit_reap_worker_and_its_child(self):
        # Exercise the common runner once, including a child ignoring SIGTERM.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for noisy in (False, True):
                with self.subTest(noisy=noisy):
                    pid_file = root / "child.pid"
                    program = (
                        "import os, subprocess, sys, time\n"
                        "from pathlib import Path\n"
                        "child = subprocess.Popen([sys.executable, '-c', "
                        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
                        f"Path({str(pid_file)!r}).write_text(str(child.pid))\n"
                        + ("os.write(1, b'x' * 65536)\n" if noisy else "")
                        + "time.sleep(30)\n"
                    )
                    started = time.monotonic()
                    try:
                        with self.assertRaisesRegex(
                            RuntimeError, "output bound" if noisy else "time bound"
                        ):
                            run_bounded(
                                (sys.executable, "-c", program),
                                cwd=root,
                                env=private_environment(root),
                                timeout=0.5,
                                maximum=1024,
                                worker=True,
                            )
                        self.assertLess(time.monotonic() - started, 3)
                        pid = int(pid_file.read_text())
                        deadline = time.monotonic() + 2
                        while process_exists(pid) and time.monotonic() < deadline:
                            time.sleep(0.02)
                        self.assertFalse(
                            process_exists(pid), "fixture descendant survived cleanup"
                        )
                    finally:
                        if pid_file.exists():
                            try:
                                os.kill(int(pid_file.read_text()), signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            pid_file.unlink()

    def test_production_git_child_inherits_worker_process_group(self):
        # The common runner test owns timeout cleanup; this checks that the
        # production Git child remains inside the group that runner will reap.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            fake_git = root / "git"
            fake_git.write_text(
                f"#!{sys.executable}\nimport os\nprint(os.getpgrp())\n"
            )
            fake_git.chmod(0o700)
            source = Path(__file__).resolve().parents[1]
            program = (
                f"import os, sys; sys.path.insert(0, {str(source)!r})\n"
                "from pathlib import Path\n"
                "from local_web_server import git_runner\n"
                "from scripts.migration_fixture import inherit_worker_session\n"
                f"git_runner._git_executable = lambda: {str(fake_git)!r}\n"
                "with inherit_worker_session():\n"
                f"    result = git_runner.run_git(Path({str(root)!r}), ('status',), maximum_stdout=1024)\n"
                "assert result.returncode == 0\n"
                "print(os.getpgrp(), result.stdout.strip())\n"
            )
            result = run_bounded(
                (sys.executable, "-c", program),
                cwd=root,
                env=private_environment(root),
                # Use the normal Git command budget for setup, not a deadline
                # intended to interrupt the worker before its imports complete.
                timeout=git_runner._GIT_TIMEOUT_SECONDS,
                worker=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            worker_group, child_group = map(int, result.stdout.split())
            self.assertNotEqual(worker_group, os.getpgrp())
            self.assertEqual(child_group, worker_group)

    def test_worker_environment_excludes_parent_secrets_and_uses_private_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = private_environment(root)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
            for name in ("HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
                self.assertTrue(Path(environment[name]).is_relative_to(root))
                self.assertEqual(Path(environment[name]).stat().st_mode & 0o777, 0o700)


class ServiceCommandMigrationWorkflowTests(unittest.TestCase):
    @acceptance
    def test_real_migration_preserves_data_recovers_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = []
            verifier = ServiceCommandMigrationWorkflowVerifier(
                coding_root=root, emit=output.append
            )
            self.assertEqual(verifier.run(), 0, "\n".join(output))
            self.assertEqual(list(root.iterdir()), [])
