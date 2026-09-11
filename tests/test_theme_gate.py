import os
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from local_web_server.theme_gate import ThemeReleaseError, ThemeReleaseGate


class FakeProcess:
    def __init__(self, *, returncode=0, timeout=False):
        self.pid = 42001
        self.returncode = returncode
        self.timeout = timeout
        self.communications = 0

    def communicate(self, timeout=None):
        self.communications += 1
        if self.timeout and self.communications == 1:
            raise subprocess.TimeoutExpired(["private", "argv"], timeout)
        return b"private stdout", b"private stderr"

    def poll(self):
        return self.returncode


class RecordingPopen:
    def __init__(self, processes, *, before_call=None):
        self.processes = list(processes)
        self.before_call = before_call
        self.calls = []
        self.candidates = []
        self.snapshot_contents = []
        self.ui_package_locations = []
        self.ui_package_contents = []
        self.working_directories = []

    def __call__(self, argv, **kwargs):
        if self.before_call is not None:
            self.before_call(len(self.calls))
        self.calls.append((tuple(argv), kwargs))
        self.working_directories.append(Path(kwargs["cwd"]).resolve(strict=True))
        candidate = Path(kwargs["env"]["LWP_THEME_CANDIDATE"])
        self.candidates.append(
            (candidate, candidate.read_bytes(), stat.S_IMODE(candidate.stat().st_mode))
        )
        snapshot_root = Path(kwargs["cwd"]) / "examples" / "ui-gallery" / "tests" / "snapshots"
        self.snapshot_contents.append(
            tuple(snapshot_root.joinpath(name).read_bytes() for name in ThemeReleaseGate.SNAPSHOT_NAMES)
        )
        ui_package = Path(kwargs["cwd"]) / "node_modules" / "@local-web" / "ui"
        self.ui_package_locations.append(ui_package.resolve(strict=True))
        self.ui_package_contents.append(ui_package.joinpath("source.txt").read_bytes())
        return self.processes.pop(0)


class ThemeReleaseGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.repository = self.root / "repository"
        self.repository.mkdir()
        snapshots = self.repository / "examples" / "ui-gallery" / "tests" / "snapshots"
        snapshots.mkdir(parents=True)
        for name in ThemeReleaseGate.SNAPSHOT_NAMES:
            snapshots.joinpath(name).write_bytes(b"png")
        self.ui_package = self.repository / "packages" / "ui"
        self.ui_package.mkdir(parents=True)
        self.ui_source = self.ui_package / "source.txt"
        self.ui_source.write_bytes(b"committed ui package")
        node_modules = self.repository / "node_modules"
        node_modules.mkdir()
        local_web_scope = node_modules / "@local-web"
        local_web_scope.mkdir()
        local_web_scope.joinpath("ui").symlink_to("../../packages/ui", target_is_directory=True)
        third_party = node_modules / "third-party"
        third_party.mkdir()
        third_party.joinpath("index.js").write_bytes(b"third party")
        self.repository.joinpath(".gitignore").write_text("node_modules/\n", encoding="utf-8")
        self._git("init", "-q")
        self._git("config", "user.email", "theme-gate@example.invalid")
        self._git("config", "user.name", "Theme Gate Test")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "authoritative snapshots")
        self.candidate = self.root / "candidate.css"
        self.candidate.write_bytes(
            (Path(__file__).parents[1] / "platform_assets" / "theme.css").read_bytes()
        )
        self.npm = self.root / "npm"
        self.npm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.npm.chmod(0o700)

    def tearDown(self):
        self.temp.cleanup()

    def _git(self, *arguments):
        return subprocess.run(
            ("/usr/bin/git", "-C", self.repository, *arguments),
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout

    def test_runs_fixed_contract_and_browser_commands_with_a_pinned_candidate_copy(self):
        popen = RecordingPopen([FakeProcess(), FakeProcess(), FakeProcess()])
        gate = ThemeReleaseGate(
            self.repository,
            npm=self.npm,
            popen=popen,
            timeout_seconds=30,
        )

        gate.verify(self.candidate)

        resolved_npm = str(self.npm.resolve())
        self.assertEqual(
            [call[0] for call in popen.calls],
            [
                (resolved_npm, "run", "build:gallery"),
                (resolved_npm, "run", "test:gallery", "--", "--run"),
                (resolved_npm, "run", "test:gallery:e2e"),
            ],
        )
        for index, (_argv, kwargs) in enumerate(popen.calls):
            with self.subTest(argv=_argv):
                self.assertNotEqual(kwargs["cwd"], self.repository.resolve())
                self.assertTrue(Path(kwargs["cwd"]).is_relative_to(Path(kwargs["env"]["TMPDIR"])))
                self.assertTrue(kwargs["start_new_session"])
                self.assertTrue(kwargs["close_fds"])
                self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
                self.assertEqual(kwargs["stdout"], subprocess.PIPE)
                self.assertEqual(kwargs["stderr"], subprocess.PIPE)
                self.assertEqual(
                    set(kwargs["env"]),
                    {"CI", "HOME", "LANG", "LC_ALL", "LWP_THEME_CANDIDATE", "PATH", "TMPDIR"},
                )
                candidate_copy, candidate_content, candidate_mode = popen.candidates[index]
                self.assertNotEqual(candidate_copy, self.candidate)
                self.assertEqual(candidate_content, self.candidate.read_bytes())
                self.assertEqual(candidate_mode, 0o600)
                self.assertNotIn("update-snapshots", " ".join(_argv))
                self.assertEqual(popen.snapshot_contents[index], (b"png",) * 4)
                self.assertEqual(
                    popen.ui_package_locations[index],
                    popen.working_directories[index] / "packages" / "ui",
                )

    def test_browser_cannot_observe_worktree_ui_package_changes_after_precheck(self):
        def replace_worktree_ui(call_index):
            if call_index == 0:
                self.ui_source.write_bytes(b"uncommitted worktree replacement")

        popen = RecordingPopen(
            [FakeProcess(), FakeProcess(), FakeProcess()],
            before_call=replace_worktree_ui,
        )
        gate = ThemeReleaseGate(self.repository, npm=self.npm, popen=popen)

        gate.verify(self.candidate)

        self.assertEqual(len(popen.calls), 3)
        self.assertEqual(popen.ui_package_contents, [b"committed ui package"] * 3)
        for index, (_argv, _kwargs) in enumerate(popen.calls):
            checkout = popen.working_directories[index]
            resolved_package = popen.ui_package_locations[index]
            self.assertEqual(resolved_package, checkout / "packages" / "ui")
            self.assertTrue(resolved_package.is_relative_to(checkout))

    def test_refuses_a_dirty_snapshot_before_starting_any_browser_command(self):
        snapshot = (
            self.repository
            / "examples"
            / "ui-gallery"
            / "tests"
            / "snapshots"
            / ThemeReleaseGate.SNAPSHOT_NAMES[0]
        )
        snapshot.write_bytes(b"valid but unreviewed png")
        popen = RecordingPopen([])
        gate = ThemeReleaseGate(self.repository, npm=self.npm, popen=popen)

        with self.assertRaisesRegex(ThemeReleaseError, "theme compatibility gate failed"):
            gate.verify(self.candidate)

        self.assertEqual(popen.calls, [])

    def test_browser_uses_authoritative_snapshots_when_worktree_path_changes_after_precheck(self):
        snapshot = (
            self.repository
            / "examples"
            / "ui-gallery"
            / "tests"
            / "snapshots"
            / ThemeReleaseGate.SNAPSHOT_NAMES[0]
        )

        def replace_worktree_snapshot(call_index):
            if call_index == 0:
                snapshot.write_bytes(b"replacement after initial path check")

        popen = RecordingPopen(
            [FakeProcess(), FakeProcess(), FakeProcess()],
            before_call=replace_worktree_snapshot,
        )
        gate = ThemeReleaseGate(self.repository, npm=self.npm, popen=popen)

        gate.verify(self.candidate)

        self.assertEqual(len(popen.calls), 3)
        self.assertEqual(popen.snapshot_contents, [(b"png",) * 4] * 3)

    def test_ignores_git_replace_refs_when_reading_authoritative_snapshots(self):
        original_commit = self._git("rev-parse", "HEAD").strip()
        snapshot = (
            self.repository
            / "examples"
            / "ui-gallery"
            / "tests"
            / "snapshots"
            / ThemeReleaseGate.SNAPSHOT_NAMES[0]
        )
        snapshot.write_bytes(b"replacement-ref png")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "replacement commit")
        replacement_commit = self._git("rev-parse", "HEAD").strip()
        self._git("update-ref", "HEAD", original_commit.decode("ascii"))
        self._git("read-tree", original_commit.decode("ascii"))
        snapshot.write_bytes(b"png")
        self._git("replace", original_commit.decode("ascii"), replacement_commit.decode("ascii"))
        popen = RecordingPopen([FakeProcess(), FakeProcess(), FakeProcess()])
        gate = ThemeReleaseGate(self.repository, npm=self.npm, popen=popen)

        gate.verify(self.candidate)

        self.assertEqual(len(popen.calls), 3)
        self.assertEqual(popen.snapshot_contents, [(b"png",) * 4] * 3)

    def test_refuses_candidate_and_snapshot_symlinks_without_starting_a_process(self):
        candidate_link = self.root / "candidate-link.css"
        candidate_link.symlink_to(self.candidate)
        popen = RecordingPopen([])
        gate = ThemeReleaseGate(self.repository, npm=self.npm, popen=popen)

        with self.assertRaisesRegex(ThemeReleaseError, "theme compatibility gate failed"):
            gate.verify(candidate_link)

        self.assertEqual(popen.calls, [])

        snapshot = (
            self.repository
            / "examples"
            / "ui-gallery"
            / "tests"
            / "snapshots"
            / ThemeReleaseGate.SNAPSHOT_NAMES[0]
        )
        target = self.root / "snapshot.png"
        target.write_bytes(b"png")
        snapshot.unlink()
        snapshot.symlink_to(target)

        with self.assertRaisesRegex(ThemeReleaseError, "theme compatibility gate failed"):
            gate.verify(self.candidate)

        self.assertEqual(popen.calls, [])

    def test_sanitises_process_failure_without_candidate_path_content_or_output(self):
        marker = b"PRIVATE-THEME-CONTENT"
        invalid = self.root / "private-theme-name.css"
        invalid.write_bytes(marker)
        gate = ThemeReleaseGate(
            self.repository,
            npm=self.npm,
            popen=RecordingPopen([]),
        )

        with self.assertRaises(ThemeReleaseError) as caught:
            gate.verify(invalid)

        message = str(caught.exception)
        self.assertEqual(message, "theme compatibility gate failed")
        self.assertNotIn(str(invalid), message)
        self.assertNotIn(marker.decode(), message)

        failed = ThemeReleaseGate(
            self.repository,
            npm=self.npm,
            popen=RecordingPopen([FakeProcess(returncode=9)]),
        )
        with self.assertRaises(ThemeReleaseError) as process_caught:
            failed.verify(self.candidate)
        self.assertEqual(str(process_caught.exception), "theme compatibility gate failed")
        self.assertNotIn("private stdout", str(process_caught.exception))
        self.assertNotIn("private stderr", str(process_caught.exception))

    def test_terminates_the_contained_process_group_on_timeout(self):
        process = FakeProcess(timeout=True)
        popen = RecordingPopen([process])
        gate = ThemeReleaseGate(
            self.repository,
            npm=self.npm,
            popen=popen,
            timeout_seconds=30,
        )

        with patch("local_web_server.theme_gate.os.killpg") as kill_group:
            with self.assertRaisesRegex(ThemeReleaseError, "theme compatibility gate failed"):
                gate.verify(self.candidate)

        self.assertIn((process.pid, signal.SIGTERM), [call.args for call in kill_group.call_args_list])
        self.assertNotIn(str(self.candidate), str(kill_group.call_args_list))

    def test_zero_exit_leader_cannot_leave_a_sigterm_ignoring_group_member(self):
        ready = self.root / "descendant-ready"
        marker = self.root / "late-descendant"
        child = self.root / "ignoring-child.py"
        child.write_text(
            "import signal, time\n"
            "from pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"Path({str(ready)!r}).touch()\n"
            "time.sleep(5)\n"
            f"Path({str(marker)!r}).touch()\n",
            encoding="utf-8",
        )
        leader = self.root / "successful-leader.py"
        leader.write_text(
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            f"ready = Path({str(ready)!r})\n"
            f"subprocess.Popen([sys.executable, {str(child)!r}], stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            "deadline = time.monotonic() + 2\n"
            "while not ready.exists() and time.monotonic() < deadline:\n"
            "    time.sleep(0.01)\n"
            "raise SystemExit(0 if ready.exists() else 2)\n",
            encoding="utf-8",
        )
        processes = []

        def launch(argv, **kwargs):
            process = subprocess.Popen(argv, **kwargs)
            processes.append(process)
            return process

        gate = ThemeReleaseGate(self.repository, npm=self.npm, popen=launch)
        process_group = None
        try:
            gate._run(
                (os.sys.executable, os.fspath(leader)),
                {"PATH": "/usr/bin:/bin"},
                time.monotonic() + 3,
                self.repository,
            )
            process_group = processes[0].pid
            with self.assertRaises(ProcessLookupError):
                os.killpg(process_group, 0)
            self.assertFalse(marker.exists())
        finally:
            if process_group is None and processes:
                process_group = processes[0].pid
            if process_group is not None:
                try:
                    os.killpg(process_group, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
