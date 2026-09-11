import os
import json
import plistlib
import socket
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

import scripts.verify_service_command_migration as migration_verifier

from local_web_server.deploy import DeploymentResult
from local_web_server.service_command_migration import (
    ServiceCommandMigrationPlan,
    ServiceCommandMigrationResult,
)


EXPECTED_PHASES = (
    "create healthy registered service with old command",
    "commit manifest-derived command change",
    "preview migration without writes",
    "apply migration after release selection",
    "verify fixed port and all health surfaces",
    "verify external repository data is untouched",
    "repeat apply as no-op",
    "restore old command and service after injected failure",
    "retry migration successfully",
    "cleanup",
)
DATA_SENTINEL = b'{"state":"external-and-immutable"}\n'


class InjectedWorkflow:
    """Exercise verifier phase boundaries without replacing its cleanup owner."""

    def __init__(self, root: Path, *, failure_phase: str | None):
        self.root = Path(root)
        self.failure_phase = failure_phase
        self.platform = self.root / "host-profile-guard"
        self.registry = self.platform / "config/local/apps.json"
        self.runtime = self.root / "runtime"
        self.plists = self.root / "LaunchAgents"
        self.repository = self.root / "fixture-service"
        self.data = self.repository / "repository-data/state.json"
        self.process: subprocess.Popen[bytes] | None = None
        self.process_group: int | None = None
        self.completed: list[str] = []
        self.data_observations: list[bytes] = []

    def run_phase(self, phase: str) -> None:
        if not self.platform.exists():
            selected = migration_verifier._create_disposable_guard_profile(self.root)
            if selected != self.registry:
                raise RuntimeError("PRIVATE fixture profile mismatch")
            self.runtime.mkdir()
            self.plists.mkdir()
            self.data.parent.mkdir(parents=True)
            self.data.write_bytes(DATA_SENTINEL)
            self.process = subprocess.Popen(
                ("/usr/bin/env", "python3", "-c", "import time; time.sleep(60)"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            self.process_group = self.process.pid
        self.data_observations.append(self.data.read_bytes())
        if phase == self.failure_phase:
            raise RuntimeError(f"PRIVATE failure at {self.root}")
        self.completed.append(phase)

    def close(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
        self.process.wait(timeout=2)


class ServiceCommandMigrationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.coding_root = self.root / "Coding"
        self.coding_root.mkdir()
        self.live_registry = migration_verifier._create_disposable_guard_profile(
            self.root
        )
        self.live_content = self.live_registry.read_bytes()
        self.live_mode = stat.S_IMODE(self.live_registry.stat().st_mode)

    def tearDown(self):
        self.temporary.cleanup()

    def test_phase_contract_is_exact_and_ordered(self):
        """Dropping or reordering a lifecycle phase must fail acceptance."""
        self.assertEqual(migration_verifier.PHASES, EXPECTED_PHASES)

    def test_real_workflow_targets_only_the_disposable_private_profile(self):
        root = (self.coding_root / "private-profile-target").resolve()
        root.mkdir()
        workflow = migration_verifier.DisposableServiceCommandMigrationWorkflow(
            root, Path(__file__).parents[1]
        )
        try:
            self.assertEqual(
                workflow.registry_path,
                root / "disposable-platform/config/local/apps.json",
            )
        finally:
            workflow.close()

    def test_every_injected_phase_failure_is_bounded_and_cleans_all_resources(self):
        """Every failure boundary must preserve sentinels and reap private state."""
        for failure_phase in EXPECTED_PHASES:
            with self.subTest(phase=failure_phase):
                lines: list[str] = []
                workflows: list[InjectedWorkflow] = []

                def factory(root: Path) -> InjectedWorkflow:
                    workflow = InjectedWorkflow(
                        root,
                        failure_phase=(
                            failure_phase if failure_phase != "cleanup" else None
                        ),
                    )
                    workflows.append(workflow)
                    return workflow

                def inject_cleanup(phase: str) -> None:
                    if phase == failure_phase == "cleanup":
                        raise RuntimeError(f"PRIVATE cleanup at {self.root}")

                verifier = migration_verifier.ServiceCommandMigrationWorkflowVerifier(
                    platform_repository=Path(__file__).parents[1],
                    coding_root=self.coding_root,
                    live_registry=self.live_registry,
                    workflow_factory=factory,
                    cleanup_injector=inject_cleanup,
                    emit=lines.append,
                )

                self.assertEqual(verifier.run(), 1)
                self.assertEqual(len(workflows), 1)
                workflow = workflows[0]
                failure_index = EXPECTED_PHASES.index(failure_phase)
                expected_lines = [
                    *(f"{phase} PASS" for phase in EXPECTED_PHASES[:failure_index]),
                    f"{failure_phase} FAIL",
                ]
                if failure_phase != "cleanup":
                    expected_lines.append("cleanup PASS")
                self.assertEqual(lines, expected_lines)
                self.assertFalse(workflow.root.exists())
                self.assertFalse(workflow.platform.exists())
                self.assertFalse(workflow.registry.exists())
                self.assertFalse(workflow.runtime.exists())
                self.assertFalse(workflow.plists.exists())
                self.assertFalse(workflow.repository.exists())
                if workflow.process_group is not None:
                    with self.assertRaises(ProcessLookupError):
                        os.killpg(workflow.process_group, 0)
                self.assertTrue(workflow.data_observations)
                self.assertEqual(set(workflow.data_observations), {DATA_SENTINEL})
                self.assertEqual(self.live_registry.read_bytes(), self.live_content)
                self.assertEqual(
                    stat.S_IMODE(self.live_registry.stat().st_mode), self.live_mode
                )
                output = "\n".join(lines)
                self.assertNotIn("PRIVATE", output)
                self.assertNotIn(str(self.root), output)
                self.assertEqual(list(self.coding_root.iterdir()), [])

    def test_success_emits_exactly_one_pass_line_per_phase(self):
        """Duplicate or extra public output must fail the bounded output contract."""
        lines: list[str] = []
        workflows: list[InjectedWorkflow] = []

        def factory(root: Path) -> InjectedWorkflow:
            workflow = InjectedWorkflow(root, failure_phase=None)
            workflows.append(workflow)
            return workflow

        verifier = migration_verifier.ServiceCommandMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            live_registry=self.live_registry,
            workflow_factory=factory,
            emit=lines.append,
        )

        self.assertEqual(verifier.run(), 0)
        self.assertEqual(lines, [f"{phase} PASS" for phase in EXPECTED_PHASES])
        self.assertEqual(workflows[0].completed, list(EXPECTED_PHASES[:-1]))
        self.assertFalse(workflows[0].root.exists())

    def test_default_disposable_guard_is_checked_before_owned_root_cleanup(self):
        lines: list[str] = []

        def command_factory(_root: Path, *_args: Path) -> tuple[str, ...]:
            protocol = "".join(
                [
                    *(f"PASS {index}\\n" for index in range(len(EXPECTED_PHASES) - 1)),
                    "CLEAN PASS\\n",
                ]
            )
            return ("/usr/bin/printf", protocol)

        verifier = migration_verifier.ServiceCommandMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            supervised_command_factory=command_factory,
            emit=lines.append,
        )

        self.assertEqual(verifier.run(), 0)
        self.assertEqual(lines, [f"{phase} PASS" for phase in EXPECTED_PHASES])
        self.assertEqual(list(self.coding_root.iterdir()), [])

    def test_migration_invocation_uses_the_real_bounded_cli_entrypoint(self):
        """Bypassing CLI parsing/output must fail the acceptance seam."""
        repository = self.root / "PRIVATE application"
        repository.mkdir()
        plan = ServiceCommandMigrationPlan(
            "fixture-service", "/fixture-service", 52990, "change"
        )

        class FakeMigrator:
            def __init__(self):
                self.preview_calls: list[Path] = []
                self.apply_calls: list[Path] = []

            def preview(self, target: Path):
                self.preview_calls.append(target)
                return plan

            def migrate(self, target: Path):
                self.apply_calls.append(target)
                return ServiceCommandMigrationResult(
                    plan,
                    "a" * 40,
                    DeploymentResult(
                        "fixture-service",
                        "b" * 40,
                        "deployed",
                        "PRIVATE child detail",
                        repository / "PRIVATE.log",
                    ),
                    True,
                )

        migrator = FakeMigrator()
        migrator.registry_path = self.live_registry
        preview = migration_verifier._invoke_migration_cli(
            repository, migrator, apply=False
        )
        applied = migration_verifier._invoke_migration_cli(
            repository, migrator, apply=True
        )

        expected = {
            "application": "fixture-service",
            "serviceCommand": "change",
            "identity": "unchanged",
            "repository": "unchanged",
            "route": "unchanged",
            "port": 52990,
            "portStatus": "unchanged",
            "serviceAction": "redeploy and reload",
        }
        self.assertEqual(preview, expected)
        self.assertEqual(applied, expected | {"registryRevision": "a" * 40})
        self.assertEqual(migrator.preview_calls, [repository])
        self.assertEqual(migrator.apply_calls, [repository])
        self.assertNotIn("PRIVATE", str(preview) + str(applied))

    def test_controller_launches_the_exact_validated_plist_contract(self):
        runtime = self.root / "runtime"
        layout = migration_verifier.RuntimeLayout(runtime, "service-command-fixture")
        release = layout.releases / "release-a"
        (release / "server").mkdir(parents=True)
        (release / "public").mkdir()
        (release / "server/service.mjs").write_text("fixture\n")
        (release / "public/index.html").write_text("fixture\n")
        current = layout.current
        current.parent.mkdir(parents=True, exist_ok=True)
        current.symlink_to(release)
        command = (
            "/usr/bin/env",
            "python3",
            "-c",
            "import time; time.sleep(60)",
            "--phase",
            "candidate",
        )
        host = SimpleNamespace(
            id="service-command-fixture",
            start_command=SimpleNamespace(argv=command),
        )
        controller = migration_verifier._DisposableServiceController(runtime)
        controller.configure(SimpleNamespace(apps=(host,)))
        plist = self.root / "fixture.plist"
        plist.write_bytes(
            plistlib.dumps(
                {
                    "Label": "com.sean.local-web.service-command-fixture",
                    "ProgramArguments": list(command),
                    "WorkingDirectory": str(current),
                }
            )
        )
        try:
            controller.ensure_running(
                "com.sean.local-web.service-command-fixture", plist
            )
            self.assertEqual(controller.records[-1].command, command)
            self.assertEqual(controller.records[-1].release, release)
            running_pid = controller.process.pid
            bad = plistlib.loads(plist.read_bytes())
            bad["Label"] = "com.sean.local-web.wrong"
            plist.write_bytes(plistlib.dumps(bad))
            with self.assertRaises(RuntimeError):
                controller.replace(
                    "com.sean.local-web.service-command-fixture", plist
                )
            self.assertEqual(controller.process.pid, running_pid)
            self.assertIsNone(controller.process.poll())
            bad["Label"] = "com.sean.local-web.service-command-fixture"
            bad["ProgramArguments"] = ["/usr/bin/false"]
            plist.write_bytes(plistlib.dumps(bad))
            with self.assertRaises(RuntimeError):
                controller.replace(
                    "com.sean.local-web.service-command-fixture", plist
                )
            self.assertEqual(controller.process.pid, running_pid)
            self.assertIsNone(controller.process.poll())
        finally:
            controller.close()

    def test_public_server_closes_bound_socket_before_start(self):
        server = migration_verifier._DisposablePublicServer(
            self.root, SimpleNamespace()
        )
        port = server.server.server_port
        server.close()
        rebound = socket.socket()
        try:
            rebound.bind(("127.0.0.1", port))
        finally:
            rebound.close()

    def test_public_server_shutdown_is_bounded_when_http_shutdown_hangs(self):
        server = migration_verifier._DisposablePublicServer(
            self.root, SimpleNamespace(), shutdown_timeout=0.05
        )
        server.start()
        original_shutdown = server.server.shutdown
        blocker = threading.Event()
        server.server.shutdown = lambda: blocker.wait(60)
        started = time.monotonic()
        try:
            with self.assertRaises(RuntimeError):
                server.close()
            self.assertLess(time.monotonic() - started, 0.5)
        finally:
            server.server.shutdown = original_shutdown
            original_shutdown()
            blocker.set()
            server.thread.join(timeout=1)

    def test_outer_supervision_bounds_a_hung_workflow_process(self):
        lines: list[str] = []
        pid_file = self.root / "worker.pid"

        def command_factory(root: Path, *_args: Path) -> tuple[str, ...]:
            return (
                "/usr/bin/env",
                "python3",
                "-c",
                (
                    "import os,time; from pathlib import Path; "
                    f"Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                    "time.sleep(60)"
                ),
            )

        verifier = migration_verifier.ServiceCommandMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            live_registry=self.live_registry,
            supervised_command_factory=command_factory,
            supervision_timeout=0.1,
            emit=lines.append,
        )
        started = time.monotonic()
        self.assertEqual(verifier.run(), 1)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(lines, [f"{EXPECTED_PHASES[0]} FAIL", "cleanup PASS"])
        self.assertEqual(list(self.coding_root.iterdir()), [])
        with self.assertRaises(ProcessLookupError):
            os.killpg(int(pid_file.read_text()), 0)

    def test_successful_worker_cleanup_never_replays_the_pid_ledger(self):
        lines: list[str] = []

        def command_factory(root: Path, *_args: Path) -> tuple[str, ...]:
            protocol = "".join(
                [*(f"PASS {index}\\n" for index in range(9)), "CLEAN PASS\\n"]
            )
            return ("/usr/bin/printf", protocol)

        verifier = migration_verifier.ServiceCommandMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            live_registry=self.live_registry,
            supervised_command_factory=command_factory,
            emit=lines.append,
        )
        with mock.patch.object(
            migration_verifier,
            "_reap_recorded_process_groups",
            side_effect=AssertionError("historical ledger replayed"),
        ):
            self.assertEqual(verifier.run(), 0)
        self.assertEqual(lines, [f"{phase} PASS" for phase in EXPECTED_PHASES])

    def test_reaper_discards_reused_pid_identity_without_signalling(self):
        ledger = self.root / "process-groups.json"
        ledger.write_text(
            '{"groups":[{"pgid":4242,"identity":"original"}],"version":1}\n'
        )
        with (
            mock.patch.object(
                migration_verifier,
                "_process_identity",
                return_value="replacement",
            ),
            mock.patch.object(migration_verifier.os, "killpg") as killpg,
        ):
            self.assertTrue(
                migration_verifier._reap_recorded_process_groups(ledger)
            )
        killpg.assert_not_called()
        self.assertEqual(
            __import__("json").loads(ledger.read_text()),
            {"groups": [], "version": 1},
        )

    def test_worker_cleanup_failure_has_one_final_cleanup_failure(self):
        def command_factory(_root: Path, *_args: Path) -> tuple[str, ...]:
            statements = "; ".join(
                [*(f"print('PASS {index}')" for index in range(9)), "print('CLEAN FAIL')"]
            )
            return (
                "/usr/bin/env",
                "python3",
                "-c",
                f"import sys; {statements}; sys.exit(1)",
            )

        lines: list[str] = []
        verifier = migration_verifier.ServiceCommandMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            live_registry=self.live_registry,
            supervised_command_factory=command_factory,
            emit=lines.append,
        )
        self.assertEqual(verifier.run(), 1)
        self.assertEqual(
            lines,
            [*(f"{phase} PASS" for phase in EXPECTED_PHASES[:-1]), "cleanup FAIL"],
        )

    def test_operational_and_cleanup_failures_are_both_reported_once(self):
        def command_factory(_root: Path, *_args: Path) -> tuple[str, ...]:
            return (
                "/usr/bin/env",
                "python3",
                "-c",
                "import sys; print('PASS 0\\nCLEAN FAIL'); sys.exit(1)",
            )

        lines: list[str] = []
        verifier = migration_verifier.ServiceCommandMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            live_registry=self.live_registry,
            supervised_command_factory=command_factory,
            emit=lines.append,
        )
        self.assertEqual(verifier.run(), 1)
        self.assertEqual(
            lines,
            [EXPECTED_PHASES[0] + " PASS", EXPECTED_PHASES[1] + " FAIL", "cleanup FAIL"],
        )

    def test_reaper_finds_owned_descendant_after_group_leader_exits(self):
        token = "a" * 64
        child_pid_file = self.root / "descendant.pid"
        environment = {
            **os.environ,
            "LOCAL_WEB_SERVICE_MIGRATION_OWNER": token,
        }
        leader = subprocess.Popen(
            (
                "/usr/bin/env",
                "python3",
                "-c",
                (
                    "import os,sys; from pathlib import Path; "
                    "child_pid=os.spawnve(os.P_NOWAIT,sys.executable,"
                    "(sys.executable,'-c','import time; time.sleep(60)'),os.environ); "
                    f"Path({str(child_pid_file)!r}).write_text(str(child_pid))"
                ),
            ),
            env=environment,
            start_new_session=True,
            stderr=subprocess.PIPE,
        )
        group = leader.pid
        leader.wait(timeout=2)
        ledger = self.root / "process-groups"
        ledger.write_text(
            json.dumps(
                {
                    "groups": [{"pgid": group, "identity": token}],
                    "version": 1,
                }
            )
        )
        try:
            self.assertTrue(
                migration_verifier._reap_recorded_process_groups(
                    ledger, cleanup_timeout=0.2
                )
            )
            with self.assertRaises(ProcessLookupError):
                os.killpg(group, 0)
        finally:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            diagnostics = leader.stderr.read()
            leader.stderr.close()
            self.assertEqual(diagnostics, b"")

    def test_reaper_refuses_unverifiable_existing_group_without_signalling(self):
        ledger = self.root / "process-groups"
        ledger.write_text(
            json.dumps(
                {
                    "groups": [{"pgid": 4242, "identity": "a" * 64}],
                    "version": 1,
                }
            )
        )
        with (
            mock.patch.object(
                migration_verifier,
                "_process_identity",
                return_value=migration_verifier._UNVERIFIABLE_IDENTITY,
            ),
            mock.patch.object(migration_verifier, "_process_group_exists", return_value=True),
            mock.patch.object(migration_verifier.os, "killpg") as killpg,
        ):
            self.assertFalse(
                migration_verifier._reap_recorded_process_groups(
                    ledger, cleanup_timeout=0.001
                )
            )
        killpg.assert_not_called()

    def test_group_identity_rejects_mixed_member_ownership(self):
        first = b"a" * 64
        second = b"b" * 64
        with mock.patch.object(
            migration_verifier,
            "_bounded_ps_output",
            side_effect=(
                b"101 4242\n102 4242\n",
                b"python LOCAL_WEB_SERVICE_MIGRATION_OWNER=" + first + b"\n",
                b"python LOCAL_WEB_SERVICE_MIGRATION_OWNER=" + second + b"\n",
            ),
        ):
            self.assertEqual(
                migration_verifier._process_identity(4242),
                migration_verifier._UNVERIFIABLE_IDENTITY,
            )

    def test_reaper_revalidates_identity_before_sigkill_escalation(self):
        ledger = self.root / "process-groups"
        token = "a" * 64
        ledger.write_text(
            json.dumps(
                {
                    "groups": [{"pgid": 4242, "identity": token}],
                    "version": 1,
                }
            )
        )
        with (
            mock.patch.object(
                migration_verifier,
                "_process_identity",
                side_effect=(token, "b" * 64),
            ),
            mock.patch.object(migration_verifier, "_process_group_exists", return_value=True),
            mock.patch.object(migration_verifier.os, "killpg") as killpg,
        ):
            self.assertTrue(
                migration_verifier._reap_recorded_process_groups(
                    ledger, cleanup_timeout=0.001
                )
            )
        killpg.assert_called_once_with(4242, signal.SIGTERM)

    def test_supervisor_stops_noisy_worker_at_the_output_cap(self):
        lines: list[str] = []
        pid_file = self.root / "noisy-worker.pid"

        def command_factory(_root: Path, *_args: Path) -> tuple[str, ...]:
            return (
                "/usr/bin/env",
                "python3",
                "-c",
                (
                    "import os,time; from pathlib import Path; "
                    f"Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                    "os.write(1,b'x'*65536); time.sleep(60)"
                ),
            )

        verifier = migration_verifier.ServiceCommandMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            live_registry=self.live_registry,
            supervised_command_factory=command_factory,
            supervision_timeout=5,
            emit=lines.append,
        )
        started = time.monotonic()
        self.assertEqual(verifier.run(), 1)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(lines, [EXPECTED_PHASES[0] + " FAIL", "cleanup PASS"])
        with self.assertRaises(ProcessLookupError):
            os.killpg(int(pid_file.read_text()), 0)

    def test_supervisor_escalates_term_resistant_descendant_after_leader_exit(self):
        lines: list[str] = []
        child_pid_file = self.root / "resistant-child.pid"
        ready_file = self.root / "resistant-child.ready"

        def command_factory(_root: Path, *_args: Path) -> tuple[str, ...]:
            child_source = (
                "import os,signal,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"Path({str(child_pid_file)!r}).write_text(str(os.getpgrp())); "
                f"Path({str(ready_file)!r}).write_text('ready'); time.sleep(60)"
            )
            leader_source = (
                "import subprocess,sys,time; from pathlib import Path; "
                f"child=subprocess.Popen([sys.executable,'-c',{child_source!r}]); "
                f"ready=Path({str(ready_file)!r}); "
                "deadline=time.monotonic()+2; "
                "exec(\"while not ready.exists() and time.monotonic()<deadline: time.sleep(0.01)\")"
            )
            return ("/usr/bin/env", "python3", "-c", leader_source)

        verifier = migration_verifier.ServiceCommandMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            live_registry=self.live_registry,
            supervised_command_factory=command_factory,
            supervision_timeout=2,
            emit=lines.append,
        )
        try:
            self.assertEqual(verifier.run(), 1)
            self.assertEqual(lines, [EXPECTED_PHASES[0] + " FAIL", "cleanup PASS"])
            with self.assertRaises(ProcessLookupError):
                os.killpg(int(child_pid_file.read_text()), 0)
        finally:
            if child_pid_file.exists():
                try:
                    os.killpg(int(child_pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_exception_paths_reap_active_ledger_before_temporary_deletion(self):
        scenarios = ("non-ascii", "read", "termination")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                lines: list[str] = []
                owned_groups: list[int] = []

                def command_factory(root: Path, *_args: Path) -> tuple[str, ...]:
                    token = "d" * 64
                    group_file = self.root / f"{scenario}-owned-group.pid"
                    launcher_source = (
                        "import os,subprocess,sys; from pathlib import Path; "
                        f"token={token!r}; "
                        "child=subprocess.Popen([sys.executable,'-c',"
                        "'import time; time.sleep(60)'], start_new_session=True, "
                        "env={**os.environ,'LOCAL_WEB_SERVICE_MIGRATION_OWNER':token}); "
                        f"Path({str(group_file)!r}).write_text(str(child.pid))"
                    )
                    launcher = subprocess.Popen(
                        (
                            "/usr/bin/env",
                            "python3",
                            "-c",
                            launcher_source,
                        ),
                        stderr=subprocess.DEVNULL,
                    )
                    launcher.wait(timeout=2)
                    group = int(group_file.read_text())
                    owned_groups.append(group)
                    (root / "process-groups").write_text(
                        json.dumps(
                            {
                                "groups": [
                                    {"pgid": group, "identity": token}
                                ],
                                "version": 1,
                            }
                        )
                    )
                    if scenario == "non-ascii":
                        return (
                            "/usr/bin/env",
                            "python3",
                            "-c",
                            "import os; os.write(1,b'\\xff')",
                        )
                    if scenario == "read":
                        return ("/usr/bin/printf", "PASS 0\\n")
                    return ("/bin/sleep", "60")

                verifier = migration_verifier.ServiceCommandMigrationWorkflowVerifier(
                    platform_repository=Path(__file__).parents[1],
                    coding_root=self.coding_root,
                    live_registry=self.live_registry,
                    supervised_command_factory=command_factory,
                    supervision_timeout=0.05,
                    emit=lines.append,
                )
                patches: list[object] = []
                if scenario == "read":
                    patches.append(
                        mock.patch.object(
                            migration_verifier,
                            "_read_pipe",
                            side_effect=OSError("PRIVATE read failure"),
                        )
                    )
                elif scenario == "termination":
                    original = migration_verifier._terminate_supervised_process

                    def terminate_then_fail(process):
                        original(process)
                        raise RuntimeError("PRIVATE termination failure")

                    patches.append(
                        mock.patch.object(
                            migration_verifier,
                            "_terminate_supervised_process",
                            side_effect=terminate_then_fail,
                        )
                    )
                try:
                    for patcher in patches:
                        patcher.start()
                    self.assertEqual(verifier.run(), 1)
                    self.assertEqual(
                        lines,
                        [EXPECTED_PHASES[0] + " FAIL", "cleanup PASS"],
                    )
                    for group in owned_groups:
                        with self.assertRaises(ProcessLookupError):
                            os.killpg(group, 0)
                finally:
                    for patcher in reversed(patches):
                        patcher.stop()
                    for group in owned_groups:
                        try:
                            os.killpg(group, signal.SIGKILL)
                        except ProcessLookupError:
                            pass


if __name__ == "__main__":
    unittest.main()
