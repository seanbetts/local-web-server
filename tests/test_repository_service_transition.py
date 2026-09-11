import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import scripts.verify_repository_service_transition as transition_verifier

from scripts.verify_repository_service_transition import (
    ACTIVATION_PASS_LABELS,
    FixtureCreationCommand,
    RepositoryServiceTransitionVerifier,
    TransitionAcceptanceError,
)
from local_web_server.runtime import RuntimeLayout, atomic_symlink
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore
from scripts.disposable_workflow_support import initialise_disposable_host_profile


ROOT = Path(__file__).parents[1]
REAL_TEMPORARY_DIRECTORY = tempfile.TemporaryDirectory


class ReadFailingBytesIO(io.BytesIO):
    def read(self, _size: int = -1) -> bytes:
        raise OSError("read failed")


class CleanupFailingTemporaryDirectory:
    def __init__(self, *args, **kwargs):
        self.temporary = REAL_TEMPORARY_DIRECTORY(*args, **kwargs)
        self.name = self.temporary.name

    def cleanup(self) -> None:
        self.temporary.cleanup()
        raise OSError("PRIVATE CLEANUP DETAIL")


class RecordingCommandExecutor:
    def __init__(self, events: list[str], *, fail: bool = False):
        self.events = events
        self.fail = fail
        self.commands: list[FixtureCreationCommand] = []
        self.repository: Path | None = None

    def __call__(self, command: FixtureCreationCommand) -> None:
        self.events.append(command.label)
        self.commands.append(command)
        self.repository = command.app_repository
        if self.fail:
            raise RuntimeError(f"PRIVATE COMMAND DETAIL {self.repository}")


class RecordingActivationExecutor:
    def __init__(
        self,
        events: list[str],
        *,
        failure_label: str | None = None,
    ):
        self.events = events
        self.failure_label = failure_label
        self.calls: list[tuple[Path, Path]] = []

    def __call__(self, repository: Path, temporary_root: Path) -> tuple[str, ...]:
        self.events.extend(ACTIVATION_PASS_LABELS)
        self.calls.append((repository, temporary_root))
        if self.failure_label is not None:
            raise TransitionAcceptanceError(
                self.failure_label,
                RuntimeError(f"PRIVATE ACTIVATION DETAIL {temporary_root}"),
            )
        return ACTIVATION_PASS_LABELS


class RepositoryServiceTransitionVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.coding_root = self.root / "Coding"
        self.coding_root.mkdir()
        self.live_registry = self._create_private_profile("host-profile-guard")
        self.live_content = self.live_registry.read_bytes()
        self.live_registry_mode = stat.S_IMODE(self.live_registry.stat().st_mode)
        self.lines: list[str] = []
        self.events: list[str] = []

    def _create_private_profile(self, name: str) -> Path:
        platform = self.root / name
        platform.mkdir()
        (platform / "config").mkdir()
        (platform / "local_web_server").mkdir()
        (platform / "local_web_server/source.py").write_text(
            "# disposable\n", encoding="utf-8"
        )
        (platform / ".gitignore").write_text(
            "config/local/\n", encoding="utf-8"
        )
        transition_verifier._run_git(platform, "init", "-b", "main")
        transition_verifier._run_git(platform, "add", ".")
        transition_verifier._run_git(
            platform,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgSign=false",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@invalid",
            "commit",
            "-m",
            "fixture",
        )
        content = (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "host": "transition.invalid",
                    "runtimeRoot": str(self.root / f"{name}-runtime"),
                    "apps": [],
                }
            )
            + "\n"
        ).encode("utf-8")
        return initialise_disposable_host_profile(
            platform.resolve(), content
        )

    def tearDown(self):
        self.temporary.cleanup()

    def verifier(
        self,
        command: RecordingCommandExecutor,
        activation: RecordingActivationExecutor,
    ) -> RepositoryServiceTransitionVerifier:
        return RepositoryServiceTransitionVerifier(
            platform_repository=ROOT,
            coding_root=self.coding_root,
            live_registry=self.live_registry,
            execute=command,
            activate=activation,
            emit=self.lines.append,
        )

    def _start_process(self, source: str) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            (sys.executable, "-c", source),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

    def test_normal_exit_closes_owned_stderr_after_capture(self):
        controller = transition_verifier._DisposableLaunchAgentController(self.root)
        process = self._start_process(
            "import sys; sys.stderr.buffer.write(b'captured error')"
        )
        controller.process = process
        try:
            process.wait(timeout=2)

            controller._observe_exit()

            self.assertIsNone(controller.process)
            self.assertTrue(controller.throttled)
            self.assertEqual(controller.errors, [b"captured error"])
            self.assertIsNotNone(process.stderr)
            self.assertTrue(process.stderr.closed)
        finally:
            if process.poll() is None:
                transition_verifier._stop_process_group(process)
            if process.stderr is not None and not process.stderr.closed:
                process.stderr.close()

    def test_explicit_stop_closes_owned_stderr_after_capture(self):
        controller = transition_verifier._DisposableLaunchAgentController(self.root)
        process = self._start_process("import time; time.sleep(60)")
        controller.process = process
        try:
            controller._stop_process()

            self.assertIsNone(controller.process)
            self.assertIsNotNone(process.stderr)
            self.assertTrue(process.stderr.closed)
        finally:
            if process.poll() is None:
                transition_verifier._stop_process_group(process)
            if process.stderr is not None and not process.stderr.closed:
                process.stderr.close()

    def test_capture_failure_closes_stderr_releases_process_and_propagates(self):
        controller = transition_verifier._DisposableLaunchAgentController(self.root)
        process = self._start_process("")
        process.wait(timeout=2)
        self.assertIsNotNone(process.stderr)
        process.stderr.close()
        stderr = ReadFailingBytesIO()
        process.stderr = stderr
        controller.process = process
        try:
            with self.assertRaisesRegex(OSError, "read failed"):
                controller._observe_exit()

            self.assertTrue(stderr.closed)
            self.assertIsNone(controller.process)
            controller._observe_exit()
        finally:
            if not stderr.closed:
                stderr.close()
            controller.process = None

    def test_disposable_profile_helper_initialises_one_private_revision(self):
        registry = self._create_private_profile("transition-platform")
        platform = self.root / "transition-platform"
        content = registry.read_bytes()
        paths = HostProfilePaths.for_repository(platform.resolve())
        store = HostProfileStore(paths)

        self.assertEqual(registry, paths.profile)
        self.assertEqual(store.read_current(), content)
        self.assertEqual(len(store.revisions()), 1)
        self.assertEqual(stat.S_IMODE(paths.local.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(paths.profile.stat().st_mode), 0o600)

    def execute_fake_transition(
        self,
        *,
        preview_writes_runtime: bool = False,
        mutate_command: Callable[[tuple[str, ...]], tuple[str, ...]] = lambda value: value,
    ) -> tuple[str, ...]:
        with REAL_TEMPORARY_DIRECTORY(dir=self.root) as temporary_text:
            temporary_root = Path(temporary_text)
            platform = temporary_root / "disposable-platform"
            platform.mkdir()
            (platform / "config").mkdir()
            (platform / "local_web_server").mkdir()
            (platform / "local_web_server/source.py").write_text(
                "# disposable\n", encoding="utf-8"
            )
            (platform / ".gitignore").write_text(
                "config/local/\n", encoding="utf-8"
            )
            app_repository = temporary_root / "repository-service-transition"
            app_repository.mkdir()
            runtime = temporary_root / "activation-runtime"
            transition_verifier._run_git(platform, "init", "-b", "main")
            transition_verifier._run_git(platform, "add", ".")
            transition_verifier._run_git(
                platform,
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@invalid",
                "commit",
                "-m",
                "fixture",
            )
            activation_count = 0

            class FakeActivator:
                def __init__(self, **_kwargs):
                    pass

                def activate(self, _repository: Path):
                    nonlocal activation_count
                    activation_count += 1
                    if activation_count == 1 and preview_writes_runtime:
                        runtime.mkdir(parents=True, exist_ok=True)
                        (runtime / "served-index.json").write_bytes(b"stable\n")
                    kind = "static" if activation_count == 1 else "service"
                    if activation_count == 1:
                        release = (
                            runtime
                            / "apps/repository-service-transition/releases"
                            / ("a" * 40)
                        )
                        release.mkdir(parents=True)
                        (release / "index.html").write_text(
                            "<!doctype html>\n", encoding="utf-8"
                        )
                        atomic_symlink(
                            RuntimeLayout(runtime, "repository-service-transition").current,
                            release.resolve(),
                        )
                    registry_revision = None if activation_count == 3 else "a" * 64
                    return SimpleNamespace(
                        plan=SimpleNamespace(kind=kind),
                        registry_revision=registry_revision,
                        verified=True,
                    )

                def preview(self, _repository: Path):
                    if preview_writes_runtime:
                        state = runtime / "served-index.json"
                        state.write_bytes(b"PRIVATE TRANSIENT STATE\n")
                        state.write_bytes(b"stable\n")
                    return SimpleNamespace(kind="service", registry_changed=True)

            expected_command = (
                "/usr/bin/env",
                "node",
                str(
                    runtime.resolve()
                    / "apps/repository-service-transition/current/server/service.mjs"
                ),
                "--port",
                "52991",
                "--data-dir",
                str(app_repository.resolve() / "data"),
            )
            registry = SimpleNamespace(
                apps=(
                    SimpleNamespace(
                        id=app_repository.name,
                        port=52991,
                        start_command=SimpleNamespace(
                            argv=mutate_command(expected_command)
                        ),
                    ),
                )
            )

            with (
                patch.object(transition_verifier, "AppActivator", FakeActivator),
                patch.object(
                    transition_verifier, "load_registry", return_value=registry
                ),
                patch.object(transition_verifier, "_write_service_commit"),
            ):
                return transition_verifier.execute_disposable_transition(
                    app_repository, temporary_root
                )

    def test_runs_the_ordered_static_to_repository_service_lifecycle(self):
        """Dropping or reordering a lifecycle phase must fail this contract."""
        command = RecordingCommandExecutor(self.events)
        activation = RecordingActivationExecutor(self.events)

        code = self.verifier(command, activation).run()

        self.assertEqual(code, 0)
        self.assertEqual(
            self.events,
            ["create eligible static fixture", *ACTIVATION_PASS_LABELS],
        )
        self.assertEqual(
            self.lines,
            [
                "create eligible static fixture PASS",
                *(f"{label} PASS" for label in ACTIVATION_PASS_LABELS),
                "cleanup PASS",
            ],
        )
        self.assertEqual(self.live_registry.read_bytes(), self.live_content)
        self.assertEqual(
            stat.S_IMODE(self.live_registry.stat().st_mode), self.live_registry_mode
        )
        self.assertEqual(len(command.commands), 1)
        self.assertEqual(len(activation.calls), 1)
        repository, temporary_root = activation.calls[0]
        fixture = command.commands[0]
        self.assertEqual(fixture.source_platform, ROOT)
        self.assertEqual(fixture.platform_repository.parent, temporary_root)
        self.assertNotEqual(fixture.platform_repository, ROOT)
        self.assertEqual(fixture.app_repository, repository)
        self.assertEqual(repository, command.repository)
        self.assertEqual(repository.parent, temporary_root)
        self.assertFalse(temporary_root.exists())
        self.assertEqual(list(self.coding_root.iterdir()), [])
        self.assertNotIn(str(self.root), "\n".join(self.lines))

    def test_default_disposable_guard_is_checked_before_owned_root_cleanup(self):
        command = RecordingCommandExecutor(self.events)
        activation = RecordingActivationExecutor(self.events)
        verifier = RepositoryServiceTransitionVerifier(
            platform_repository=ROOT,
            coding_root=self.coding_root,
            execute=command,
            activate=activation,
            emit=self.lines.append,
        )

        self.assertEqual(verifier.run(), 0)
        self.assertEqual(self.lines[-1], "cleanup PASS")
        self.assertEqual(list(self.coding_root.iterdir()), [])

    def test_command_failure_is_sanitized_and_always_cleans_up(self):
        command = RecordingCommandExecutor(self.events, fail=True)
        activation = RecordingActivationExecutor(self.events)

        code = self.verifier(command, activation).run()

        self.assertEqual(code, 1)
        self.assertEqual(
            self.lines,
            ["create eligible static fixture FAIL", "cleanup PASS"],
        )
        self.assertEqual(activation.calls, [])
        self.assertEqual(self.live_registry.read_bytes(), self.live_content)
        self.assertEqual(
            stat.S_IMODE(self.live_registry.stat().st_mode), self.live_registry_mode
        )
        self.assertIsNotNone(command.repository)
        self.assertFalse(command.repository.parent.exists())
        output = "\n".join(self.lines)
        self.assertNotIn("PRIVATE", output)
        self.assertNotIn(str(self.root), output)

    def test_activation_failure_emits_only_its_bounded_label_and_cleans_up(self):
        command = RecordingCommandExecutor(self.events)
        activation = RecordingActivationExecutor(
            self.events, failure_label="apply transition"
        )

        code = self.verifier(command, activation).run()

        self.assertEqual(code, 1)
        self.assertEqual(
            self.lines,
            [
                "create eligible static fixture PASS",
                "apply transition FAIL",
                "cleanup PASS",
            ],
        )
        self.assertEqual(self.live_registry.read_bytes(), self.live_content)
        self.assertEqual(
            stat.S_IMODE(self.live_registry.stat().st_mode), self.live_registry_mode
        )
        repository, temporary_root = activation.calls[0]
        self.assertFalse(repository.exists())
        self.assertFalse(temporary_root.exists())
        output = "\n".join(self.lines)
        self.assertNotIn("PRIVATE", output)
        self.assertNotIn(str(self.root), output)

    def test_activation_and_cleanup_failure_still_checks_live_registry(self):
        """Cleanup failure must not short-circuit the final registry guard."""
        command = RecordingCommandExecutor(self.events)
        activation = RecordingActivationExecutor(
            self.events, failure_label="apply transition"
        )
        registry_checks: list[Path] = []
        real_registry_unchanged = transition_verifier._registry_unchanged

        def recording_registry_guard(path: Path, content: bytes, mode: int) -> bool:
            registry_checks.append(path)
            return real_registry_unchanged(path, content, mode)

        with (
            patch.object(
                transition_verifier.tempfile,
                "TemporaryDirectory",
                CleanupFailingTemporaryDirectory,
            ),
            patch.object(
                transition_verifier,
                "_registry_unchanged",
                side_effect=recording_registry_guard,
            ),
        ):
            code = self.verifier(command, activation).run()

        self.assertEqual(code, 1)
        self.assertEqual(
            self.lines,
            [
                "create eligible static fixture PASS",
                "apply transition FAIL",
                "cleanup FAIL",
            ],
        )
        self.assertEqual(registry_checks, [self.live_registry, self.live_registry])
        self.assertEqual(self.live_registry.read_bytes(), self.live_content)

    def test_preview_runtime_write_fails_the_read_only_transition_phase(self):
        """Installer-like runtime writes during preview must fail acceptance."""
        with self.assertRaisesRegex(
            TransitionAcceptanceError, "preview transition without writes"
        ):
            self.execute_fake_transition(preview_writes_runtime=True)

    def test_private_argv_rejects_every_wrong_fixed_or_port_argument(self):
        """Partial argv checks must not accept the wrong executable or port."""
        cases = (
            (0, "/usr/bin/false"),
            (1, "python3"),
            (3, "--listen"),
            (4, "52992"),
        )
        for index, replacement in cases:
            with self.subTest(index=index, replacement=replacement):

                def mutate(command: tuple[str, ...]) -> tuple[str, ...]:
                    changed = list(command)
                    changed[index] = replacement
                    return tuple(changed)

                with self.assertRaisesRegex(
                    TransitionAcceptanceError, "inspect expanded private argv"
                ):
                    self.execute_fake_transition(mutate_command=mutate)

if __name__ == "__main__":
    unittest.main()
