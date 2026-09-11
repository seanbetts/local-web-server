import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server.process_runner import ProcessCommand, ProcessRunError, ProcessRunner


class ProcessRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.repository = Path(self.temporary.name) / "repository"
        self.repository.mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def test_runs_fixed_argv_in_order_with_an_isolated_environment(self):
        first = self.repository / "first.json"
        second = self.repository / "second.txt"
        inspect_environment = (
            "import json,os,sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_text(json.dumps({"
            "'cwd':os.getcwd(),'home':os.environ['HOME'],'tmp':os.environ['TMPDIR'],"
            "'userconfig':os.environ['NPM_CONFIG_USERCONFIG'],"
            "'globalconfig':os.environ['NPM_CONFIG_GLOBALCONFIG'],"
            "'private':os.environ.get('LOCAL_WEB_PRIVATE_TEST'),"
            "'argument':sys.argv[2]}))"
        )
        write_second = (
            "import sys; from pathlib import Path; "
            "Path(sys.argv[1]).write_text('second')"
        )

        with patch.dict(os.environ, {"LOCAL_WEB_PRIVATE_TEST": "must-not-leak"}):
            ProcessRunner().run(
                self.repository,
                (
                    ProcessCommand(
                        "inspect",
                        (sys.executable, "-c", inspect_environment, str(first), "one value"),
                    ),
                    ProcessCommand(
                        "second", (sys.executable, "-c", write_second, str(second))
                    ),
                ),
            )

        payload = json.loads(first.read_text())
        self.assertEqual(payload["cwd"], str(self.repository.resolve()))
        self.assertEqual(payload["tmp"], payload["home"])
        self.assertNotEqual(payload["home"], str(Path.home()))
        self.assertEqual(payload["userconfig"], payload["home"] + "/.npmrc-user-disabled")
        self.assertEqual(payload["globalconfig"], payload["home"] + "/.npmrc-global-disabled")
        self.assertIsNone(payload["private"])
        self.assertEqual(payload["argument"], "one value")
        self.assertEqual(second.read_text(), "second")
        self.assertFalse(Path(payload["home"]).exists())

    def test_sanitises_nonzero_child_output(self):
        command = (
            sys.executable,
            "-c",
            "import sys; print('private output'); print('private error', file=sys.stderr); sys.exit(7)",
        )

        with self.assertRaisesRegex(ProcessRunError, "^verification failed$") as raised:
            ProcessRunner().run(
                self.repository, (ProcessCommand("verification", command),)
            )

        self.assertNotIn("private", str(raised.exception))

    def test_total_timeout_terminates_descendants(self):
        marker = self.repository / "leaked-child.txt"
        child = (
            "import sys,time; from pathlib import Path; "
            "time.sleep(0.6); Path(sys.argv[1]).write_text('leaked')"
        )
        parent = (
            "import subprocess,sys,time; "
            "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]); "
            "time.sleep(60)"
        )
        started = time.monotonic()

        with self.assertRaisesRegex(ProcessRunError, "^timeout-probe failed$"):
            ProcessRunner(timeout_seconds=0.1, stop_seconds=0.1).run(
                self.repository,
                (
                    ProcessCommand(
                        "timeout-probe",
                        (sys.executable, "-c", parent, child, str(marker)),
                    ),
                ),
            )

        self.assertLess(time.monotonic() - started, 1.0)
        time.sleep(0.8)
        self.assertFalse(marker.exists())

    def test_successful_and_failed_leaders_cannot_leave_background_children(self):
        child = (
            "import sys,time; from pathlib import Path; "
            "Path(sys.argv[1]).write_text('ready'); time.sleep(0.6); "
            "Path(sys.argv[2]).write_text('leaked')"
        )
        parent = (
            "import subprocess,sys,time\n"
            "from pathlib import Path\n"
            "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]])\n"
            "ready=Path(sys.argv[2])\n"
            "deadline=time.monotonic()+1\n"
            "while not ready.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "sys.exit(int(sys.argv[4]))\n"
        )
        for return_code in (0, 7):
            with self.subTest(return_code=return_code):
                ready = self.repository / f"ready-{return_code}.txt"
                marker = self.repository / f"leaked-{return_code}.txt"
                command = ProcessCommand(
                    f"leader-{return_code}",
                    (
                        sys.executable,
                        "-c",
                        parent,
                        child,
                        str(ready),
                        str(marker),
                        str(return_code),
                    ),
                )
                if return_code:
                    with self.assertRaisesRegex(
                        ProcessRunError, f"^leader-{return_code} failed$"
                    ):
                        ProcessRunner().run(self.repository, (command,))
                else:
                    ProcessRunner().run(self.repository, (command,))
                self.assertTrue(ready.exists())
                time.sleep(0.8)
                self.assertFalse(marker.exists())

    def test_sigterm_ignoring_descendant_is_killed(self):
        ready = self.repository / "sigkill-ready.txt"
        marker = self.repository / "sigkill-leaked.txt"
        child = (
            "import signal,sys,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "Path(sys.argv[1]).write_text('ready'); time.sleep(0.6); "
            "Path(sys.argv[2]).write_text('leaked')"
        )
        parent = (
            "import subprocess,sys,time\n"
            "from pathlib import Path\n"
            "subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]])\n"
            "ready=Path(sys.argv[2])\n"
            "deadline=time.monotonic()+1\n"
            "while not ready.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
        )

        ProcessRunner(stop_seconds=0.1).run(
            self.repository,
            (
                ProcessCommand(
                    "sigkill-probe",
                    (sys.executable, "-c", parent, child, str(ready), str(marker)),
                ),
            ),
        )

        self.assertTrue(ready.exists())
        time.sleep(0.8)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
