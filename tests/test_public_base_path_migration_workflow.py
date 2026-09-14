import errno
import hashlib
import json
import os
import plistlib
import signal
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

import scripts.verify_public_base_path_migration as migration_verifier

from local_web_server.deploy import DeploymentResult
from local_web_server.public_base_path_migration import (
    PublicBasePathMigrationPlan,
    PublicBasePathMigrationResult,
)
from tests.suites import acceptance


EXPECTED_PHASES = (
    "create healthy legacy registered service",
    "commit hosted frontend release",
    "preview public base path migration without writes",
    "apply migration with fresh environment",
    "verify fixed port command assets and health",
    "repeat apply as no-op",
    "restore legacy registration and service after injected failure",
    "retry public base path migration successfully",
    "cleanup",
)
LIVE_SENTINEL = b'{"schemaVersion":1,"apps":[]}\n'
DATA_SENTINEL = b'{"state":"external-and-immutable"}\n'
PARENT_SECRET_NAME = "LOCAL_WEB_PUBLIC_BASE_PATH_PARENT_SECRET"


def _tree_state(root: Path) -> tuple[tuple[object, ...], ...]:
    entries = [root, *sorted(root.rglob("*"))]
    state = []
    for path in entries:
        metadata = path.lstat()
        state.append(
            (
                str(path.relative_to(root)),
                metadata.st_dev,
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                path.read_bytes() if path.is_file() else None,
            )
        )
    return tuple(state)


def _port_2019_listener_state() -> bytes:
    result = subprocess.run(
        (
            "/usr/sbin/lsof",
            "-nP",
            "-iTCP:2019",
            "-sTCP:LISTEN",
            "-FpcfnT",
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=2,
    )
    if result.returncode != 0 or not result.stdout or len(result.stdout) > 8192:
        raise AssertionError("port 2019 listener identity was unavailable")
    process_ids = tuple(
        sorted(
            {
                int(line[1:])
                for line in result.stdout.splitlines()
                if line.startswith(b"p") and line[1:].isdigit()
            }
        )
    )
    if not process_ids or len(process_ids) > 16:
        raise AssertionError("port 2019 listener identity was unavailable")
    identities = bytearray(result.stdout)
    for process_id in process_ids:
        process = subprocess.run(
            (
                "/bin/ps",
                "-p",
                str(process_id),
                "-o",
                "pid=,lstart=,command=",
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
            check=False,
            timeout=2,
        )
        if (
            process.returncode != 0
            or not process.stdout
            or len(process.stdout) > 8192
        ):
            raise AssertionError("port 2019 listener identity was unavailable")
        identities.extend(process.stdout)
    if len(identities) > 32768:
        raise AssertionError("port 2019 listener identity was unavailable")
    return bytes(identities)


class PublicBasePathMigrationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.coding_root = self.root / "Coding"
        self.coding_root.mkdir()
        self.live_registry = migration_verifier._create_disposable_guard_profile(
            self.root
        )
        self.external_data = self.root / "external-state.json"
        self.external_data.write_bytes(DATA_SENTINEL)
        self.external_data.chmod(0o600)
        self.external_mode = stat.S_IMODE(self.external_data.stat().st_mode)

    def tearDown(self):
        self.temporary.cleanup()

    def test_phase_contract_is_exact_and_ordered(self):
        """Dropping or reordering a lifecycle phase must fail acceptance."""
        self.assertEqual(migration_verifier.PHASES, EXPECTED_PHASES)

    def test_real_workflow_targets_only_the_disposable_private_profile(self):
        root = (self.coding_root / "private-profile-target").resolve()
        root.mkdir()
        workflow = migration_verifier.DisposablePublicBasePathMigrationWorkflow(
            root,
            Path(__file__).parents[1],
            self.external_data,
            scenario="normal",
        )
        try:
            self.assertEqual(
                workflow.registry_path,
                root / "disposable-platform/config/local/apps.json",
            )
        finally:
            workflow.close()

    @acceptance
    def test_fixture_builds_its_ui_package_from_public_source_on_demand(self):
        root = (self.coding_root / "on-demand-ui-package").resolve()
        root.mkdir()
        workflow = migration_verifier.DisposablePublicBasePathMigrationWorkflow(
            root,
            Path(__file__).parents[1],
            self.external_data,
            scenario="normal",
        )
        try:
            migration_verifier.build_ui_package(
                Path(__file__).parents[1], root / "local-web-ui.tgz"
            )
            workflow._clone_platform_and_generate_app()

            artifact = workflow.repository / "vendor/local-web-ui.tgz"
            provenance = json.loads(
                (workflow.repository / ".local-web-platform.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(artifact.is_file())
            self.assertEqual(
                provenance["ui"]["sha256"],
                hashlib.sha256(artifact.read_bytes()).hexdigest(),
            )
            self.assertFalse(
                (
                    workflow.platform
                    / "artifacts"
                    / f"local-web-ui-{provenance['ui']['version']}.tgz"
                ).exists()
            )
        finally:
            workflow.close()

    def test_managed_file_recovery_compares_exact_bytes_and_mode(self):
        """Atomic rollback may replace inode identity without changing state."""
        managed = self.root / "managed.json"
        managed.write_bytes(LIVE_SENTINEL)
        managed.chmod(0o640)
        expected = migration_verifier._managed_file_state(managed)
        former_identity = migration_verifier._file_state(managed)

        replacement = self.root / "replacement.json"
        replacement.write_bytes(LIVE_SENTINEL)
        replacement.chmod(0o640)
        os.replace(replacement, managed)

        self.assertNotEqual(migration_verifier._file_state(managed), former_identity)
        self.assertEqual(migration_verifier._managed_file_state(managed), expected)
        managed.chmod(0o600)
        self.assertNotEqual(migration_verifier._managed_file_state(managed), expected)

    def test_every_injected_phase_failure_is_bounded_and_cleans_all_resources(self):
        """Every protocol failure is attributed without rebuilding the fixture."""
        live_state = migration_verifier._file_state(self.live_registry)
        external_state = migration_verifier._file_state(self.external_data)
        for failure_phase in EXPECTED_PHASES:
            with self.subTest(phase=failure_phase):
                lines: list[str] = []
                failure_index = EXPECTED_PHASES.index(failure_phase)

                def command_factory(_root: Path, *_args: Path) -> tuple[str, ...]:
                    protocol = "".join(
                        f"PASS {index}\\n" for index in range(failure_index)
                    )
                    if failure_phase == EXPECTED_PHASES[-1]:
                        protocol += "CLEAN FAIL\\n"
                    return ("/usr/bin/printf", protocol)

                verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
                    platform_repository=Path(__file__).parents[1],
                    coding_root=self.coding_root,
                    live_registry=self.live_registry,
                    external_data=self.external_data,
                    supervised_command_factory=command_factory,
                    emit=lines.append,
                )

                started = time.monotonic()
                self.assertEqual(verifier.run(), 1)
                self.assertLess(time.monotonic() - started, 2)
                expected_lines = [
                    *(f"{phase} PASS" for phase in EXPECTED_PHASES[:failure_index]),
                    f"{failure_phase} FAIL",
                ]
                if failure_phase != "cleanup":
                    expected_lines.append("cleanup PASS")
                self.assertEqual(lines, expected_lines)
                self.assertEqual(list(self.coding_root.iterdir()), [])
                self.assertEqual(
                    migration_verifier._file_state(self.live_registry), live_state
                )
                self.assertEqual(
                    migration_verifier._file_state(self.external_data),
                    external_state,
                )
                output = "\n".join(lines)
                self.assertNotIn("PRIVATE", output)
                self.assertNotIn(str(self.root), output)
                self.assertTrue(all(len(line.encode("ascii")) <= 128 for line in lines))
                self.assertLessEqual(len(output.encode("ascii")), 2048)
                self.assertEqual(list(self.coding_root.iterdir()), [])

    @acceptance
    def test_real_migration_recovers_retries_and_cleans_all_resources(self):
        lines: list[str] = []
        live_state = migration_verifier._file_state(self.live_registry)
        external_state = migration_verifier._file_state(self.external_data)
        verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            live_registry=self.live_registry,
            external_data=self.external_data,
            emit=lines.append,
        )

        self.assertEqual(verifier.run(), 0)
        self.assertEqual(lines, [f"{phase} PASS" for phase in EXPECTED_PHASES])
        self.assertEqual(list(self.coding_root.iterdir()), [])
        self.assertEqual(
            migration_verifier._file_state(self.live_registry), live_state
        )
        self.assertEqual(
            migration_verifier._file_state(self.external_data), external_state
        )

    def test_success_emits_exactly_one_pass_line_per_phase(self):
        """Duplicate or extra public output must fail the bounded output contract."""
        lines: list[str] = []

        def command_factory(_root: Path, *_args: Path) -> tuple[str, ...]:
            protocol = "".join(
                [
                    *(f"PASS {index}\\n" for index in range(len(EXPECTED_PHASES) - 1)),
                    "CLEAN PASS\\n",
                ]
            )
            return ("/usr/bin/printf", protocol)

        verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            live_registry=self.live_registry,
            external_data=self.external_data,
            supervised_command_factory=command_factory,
            emit=lines.append,
        )

        self.assertEqual(verifier.run(), 0)
        self.assertEqual(lines, [f"{phase} PASS" for phase in EXPECTED_PHASES])
        self.assertEqual(list(self.coding_root.iterdir()), [])
        self.assertEqual(self.external_data.read_bytes(), DATA_SENTINEL)
        self.assertEqual(
            stat.S_IMODE(self.external_data.stat().st_mode), self.external_mode
        )

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

        verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            external_data=self.external_data,
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
        plan = PublicBasePathMigrationPlan(
            "public-base-path-fixture", "/fixture", 52990, "change"
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
                return PublicBasePathMigrationResult(
                    plan,
                    "a" * 40,
                    DeploymentResult(
                        "public-base-path-fixture",
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
            "application": "public-base-path-fixture",
            "publicBasePath": "change",
            "identity": "unchanged",
            "repository": "unchanged",
            "route": "unchanged",
            "port": 52990,
            "portStatus": "unchanged",
            "serviceCommand": "unchanged",
            "serviceAction": "redeploy and reload",
        }
        self.assertEqual(preview, expected)
        self.assertEqual(applied, expected | {"registryRevision": "a" * 40})
        self.assertEqual(migrator.preview_calls, [repository])
        self.assertEqual(migrator.apply_calls, [repository])
        self.assertNotIn("PRIVATE", str(preview) + str(applied))

    @acceptance
    def test_corrupted_generated_caddy_route_fails_the_real_workflow(self):
        lines: list[str] = []
        live_state = migration_verifier._file_state(self.live_registry)
        external_state = migration_verifier._file_state(self.external_data)
        verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            live_registry=self.live_registry,
            external_data=self.external_data,
            scenario="corrupt-caddy-route",
            emit=lines.append,
        )

        started = time.monotonic()
        self.assertEqual(verifier.run(), 1)
        self.assertLess(time.monotonic() - started, 60)
        self.assertEqual(lines, [EXPECTED_PHASES[0] + " FAIL", "cleanup PASS"])
        self.assertEqual(list(self.coding_root.iterdir()), [])
        self.assertEqual(
            migration_verifier._file_state(self.live_registry), live_state
        )
        self.assertEqual(
            migration_verifier._file_state(self.external_data), external_state
        )

    @acceptance
    def test_real_caddy_disables_admin_and_preserves_external_home_state(self):
        external_state_root = self.root / "external-caddy-state"
        external_home = external_state_root / "home"
        external_config = external_state_root / "config"
        external_data_home = external_state_root / "data"
        for directory in (external_home, external_config, external_data_home):
            directory.mkdir(parents=True)
            directory.chmod(0o700)
            sentinel = directory / "sentinel.bin"
            sentinel.write_bytes(b"external caddy state\x00\xff\n")
            sentinel.chmod(0o600)
        external_state = _tree_state(external_state_root)
        live_state = migration_verifier._file_state(self.live_registry)
        data_state = migration_verifier._file_state(self.external_data)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        owned_listener = False
        try:
            try:
                listener.bind(("127.0.0.1", 2019))
            except OSError as error:
                listener.close()
                if error.errno != errno.EADDRINUSE:
                    raise
            else:
                listener.listen(1)
                owned_listener = True
            listener_state = _port_2019_listener_state()

            worker_root = self.coding_root / "real-caddy-isolation"
            worker_root.mkdir(mode=0o700)
            worker_root = worker_root.resolve(strict=True)
            migration_verifier.build_ui_package(
                Path(__file__).parents[1], worker_root / "local-web-ui.tgz"
            )
            environment = {
                **os.environ,
                "HOME": str(external_home),
                "XDG_CONFIG_HOME": str(external_config),
                "XDG_DATA_HOME": str(external_data_home),
                "GIT_AUTHOR_NAME": "Disposable Migration",
                "GIT_AUTHOR_EMAIL": "public-base-path-migration@localhost",
                "GIT_COMMITTER_NAME": "Disposable Migration",
                "GIT_COMMITTER_EMAIL": "public-base-path-migration@localhost",
                "LOCAL_WEB_PUBLIC_BASE_PATH_MIGRATION_OWNER": "c" * 64,
                PARENT_SECRET_NAME: "must-never-reach-a-fixture-process",
            }
            worker = subprocess.run(
                (
                    os.path.realpath(__import__("sys").executable),
                    str(Path(migration_verifier.__file__).resolve()),
                    "--worker",
                    str(worker_root),
                    str(Path(__file__).parents[1]),
                    str(self.live_registry),
                    str(self.external_data),
                    EXPECTED_PHASES[1],
                    "normal",
                ),
                cwd=Path(__file__).parents[1],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=environment,
                check=False,
                timeout=120,
            )
            self.assertEqual(worker.returncode, 1)
            self.assertEqual(worker.stdout, b"PASS 0\nCLEAN PASS\n")

            execution = json.loads(
                (worker_root / "runtime/Caddyfile.disposable.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(execution.get("admin"), {"disabled": True})
            for path in (
                worker_root / "runtime/caddy-environment/home",
                worker_root / "runtime/caddy-environment/config",
                worker_root / "runtime/caddy-environment/data",
            ):
                self.assertTrue(path.is_dir())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
            self.assertEqual(_tree_state(external_state_root), external_state)
            self.assertEqual(_port_2019_listener_state(), listener_state)
            self.assertEqual(
                migration_verifier._file_state(self.live_registry), live_state
            )
            self.assertEqual(
                migration_verifier._file_state(self.external_data), data_state
            )
            self.assertEqual(
                json.loads((worker_root / "process-groups").read_text()),
                {"groups": [], "version": 1},
            )
            if owned_listener:
                # A TCP handshake is portable across supported macOS runners;
                # SO_ACCEPTCONN is not implemented by every hosted kernel.
                with socket.create_connection(listener.getsockname(), timeout=1):
                    pass
        finally:
            if owned_listener:
                listener.close()

    @acceptance
    def test_noisy_production_build_is_capped_and_fails_in_finite_time(self):
        lines: list[str] = []
        live_state = migration_verifier._file_state(self.live_registry)
        external_state = migration_verifier._file_state(self.external_data)
        verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
            platform_repository=Path(__file__).parents[1],
            coding_root=self.coding_root,
            live_registry=self.live_registry,
            external_data=self.external_data,
            scenario="noisy-build",
            emit=lines.append,
        )

        started = time.monotonic()
        self.assertEqual(verifier.run(), 1)
        self.assertLess(time.monotonic() - started, 60)
        self.assertEqual(
            lines,
            [
                *(phase + " PASS" for phase in EXPECTED_PHASES[:3]),
                EXPECTED_PHASES[3] + " FAIL",
                "cleanup PASS",
            ],
        )
        self.assertEqual(list(self.coding_root.iterdir()), [])
        self.assertEqual(
            migration_verifier._file_state(self.live_registry), live_state
        )
        self.assertEqual(
            migration_verifier._file_state(self.external_data), external_state
        )

    @acceptance
    def test_controller_launches_the_exact_validated_plist_contract(self):
        runtime = self.root / "runtime"
        layout = migration_verifier.RuntimeLayout(runtime, "public-base-path-fixture")
        release = layout.releases / "release-a"
        (release / "server").mkdir(parents=True)
        (release / "public").mkdir()
        (release / "server/service.mjs").write_text("fixture\n")
        (release / "public/index.html").write_text("fixture\n")
        (release / "public/build.json").write_text(
            '{"publicBasePath":"/fixture/","version":"candidate"}\n'
        )
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
            id="public-base-path-fixture",
            start_command=SimpleNamespace(argv=command),
        )
        controller = migration_verifier._DisposableServiceController(runtime)
        controller.configure(SimpleNamespace(apps=(host,)))
        plist = self.root / "fixture.plist"
        plist.write_bytes(
            plistlib.dumps(
                {
                    "Label": "com.sean.local-web.public-base-path-fixture",
                    "ProgramArguments": list(command),
                    "WorkingDirectory": str(current),
                    "EnvironmentVariables": {
                        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
                    },
                }
            )
        )
        private_root = self.root / "controller-private-environment"
        private_environment = {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": str(private_root / "tmp"),
            "HOME": str(private_root / "home"),
            "XDG_CONFIG_HOME": str(private_root / "config"),
            "XDG_DATA_HOME": str(private_root / "data"),
        }
        with (
            mock.patch.object(
                migration_verifier,
                "_PRIVATE_EXECUTION_ENVIRONMENT",
                private_environment,
            ),
            mock.patch.object(
                migration_verifier,
                "_PROCESS_OWNER_TOKEN",
                "c" * 64,
            ),
        ):
            try:
                controller.ensure_running(
                    "com.sean.local-web.public-base-path-fixture", plist
                )
                self.assertEqual(controller.records[-1].command, command)
                self.assertEqual(controller.records[-1].release, release)
                self.assertEqual(
                    controller.records[-1].public_base_path, "/fixture/"
                )
                self.assertEqual(controller.records[-1].version, "candidate")
                running_pid = controller.process.pid
                bad = plistlib.loads(plist.read_bytes())
                bad["Label"] = "com.sean.local-web.wrong"
                plist.write_bytes(plistlib.dumps(bad))
                with self.assertRaises(RuntimeError):
                    controller.replace(
                        "com.sean.local-web.public-base-path-fixture", plist
                    )
                self.assertEqual(controller.process.pid, running_pid)
                self.assertIsNone(controller.process.poll())
                bad["Label"] = "com.sean.local-web.public-base-path-fixture"
                bad["ProgramArguments"] = ["/usr/bin/false"]
                plist.write_bytes(plistlib.dumps(bad))
                with self.assertRaises(RuntimeError):
                    controller.replace(
                        "com.sean.local-web.public-base-path-fixture", plist
                    )
                self.assertEqual(controller.process.pid, running_pid)
                self.assertIsNone(controller.process.poll())
            finally:
                controller.close()

    @acceptance
    def test_controller_launches_with_the_generated_plist_environment(self):
        runtime = self.root / "runtime"
        layout = migration_verifier.RuntimeLayout(runtime, "public-base-path-fixture")
        release = layout.releases / "release-a"
        (release / "server").mkdir(parents=True)
        (release / "public").mkdir()
        (release / "server/service.mjs").write_text("fixture\n")
        (release / "public/index.html").write_text("fixture\n")
        (release / "public/build.json").write_text(
            '{"publicBasePath":"/fixture/","version":"candidate"}\n'
        )
        layout.current.parent.mkdir(parents=True, exist_ok=True)
        layout.current.symlink_to(release)
        observation = self.root / "service-environment.json"
        expected_path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
        private_root = self.root / "private-process-environment"
        private_environment = {
            "PATH": expected_path,
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": str(private_root / "tmp"),
            "HOME": str(private_root / "home"),
            "XDG_CONFIG_HOME": str(private_root / "config"),
            "XDG_DATA_HOME": str(private_root / "data"),
        }
        for value in private_environment.values():
            if value.startswith(str(private_root)):
                Path(value).mkdir(parents=True, mode=0o700)
        owner = "d" * 64
        expected_child_environment = {
            **private_environment,
            "LOCAL_WEB_PUBLIC_BASE_PATH_MIGRATION_OWNER": owner,
        }
        command = (
            os.path.realpath(__import__("sys").executable),
            "-c",
            (
                "import json,os,time; from pathlib import Path; "
                f"names={tuple(sorted(expected_child_environment))!r}; "
                f"secret={PARENT_SECRET_NAME!r}; "
                "observed={'environment': {name: os.environ.get(name) for name in names}, "
                "'parentSecretPresent': secret in os.environ}; "
                f"Path({str(observation)!r}).write_text(json.dumps(observed, sort_keys=True)); "
                "time.sleep(60)"
            ),
        )
        host = SimpleNamespace(
            id="public-base-path-fixture",
            start_command=SimpleNamespace(argv=command),
        )
        controller = migration_verifier._DisposableServiceController(runtime)
        controller.configure(SimpleNamespace(apps=(host,)))
        plist = self.root / "fixture-environment.plist"
        plist.write_bytes(
            plistlib.dumps(
                {
                    "Label": "com.sean.local-web.public-base-path-fixture",
                    "ProgramArguments": list(command),
                    "WorkingDirectory": str(layout.current),
                    "EnvironmentVariables": {"PATH": expected_path},
                }
            )
        )
        with (
            mock.patch.dict(os.environ, {PARENT_SECRET_NAME: "private"}),
            mock.patch.object(
                migration_verifier,
                "_PRIVATE_EXECUTION_ENVIRONMENT",
                private_environment,
                create=True,
            ),
            mock.patch.object(migration_verifier, "_PROCESS_OWNER_TOKEN", owner),
        ):
            try:
                controller.ensure_running(
                    "com.sean.local-web.public-base-path-fixture", plist
                )
                deadline = time.monotonic() + 1
                while not observation.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(observation.is_file())
                self.assertEqual(
                    json.loads(observation.read_text()),
                    {
                        "environment": expected_child_environment,
                        "parentSecretPresent": False,
                    },
                )
            finally:
                controller.close()

    def test_owned_environment_is_explicit_and_drops_parent_secrets(self):
        private_root = self.root / "explicit-environment"
        expected = {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": str(private_root / "tmp"),
            "HOME": str(private_root / "home"),
            "XDG_CONFIG_HOME": str(private_root / "config"),
            "XDG_DATA_HOME": str(private_root / "data"),
        }
        owner = "e" * 64
        with (
            mock.patch.dict(os.environ, {PARENT_SECRET_NAME: "private"}),
            mock.patch.object(
                migration_verifier,
                "_PRIVATE_EXECUTION_ENVIRONMENT",
                expected,
                create=True,
            ),
            mock.patch.object(migration_verifier, "_PROCESS_OWNER_TOKEN", owner),
        ):
            self.assertEqual(
                migration_verifier._owned_environment(),
                {
                    **expected,
                    "LOCAL_WEB_PUBLIC_BASE_PATH_MIGRATION_OWNER": owner,
                },
            )

    def test_caddy_environment_is_minimal_private_and_plist_derived(self):
        runtime = self.root / "caddy-runtime"
        repository = self.root / "caddy-repository"
        repository.mkdir()
        private_root = self.root / "base-environment"
        base = {
            "PATH": "/base/path",
            "LANG": "C",
            "LC_ALL": "C",
            "TMPDIR": str(private_root / "tmp"),
            "HOME": str(private_root / "home"),
            "XDG_CONFIG_HOME": str(private_root / "config"),
            "XDG_DATA_HOME": str(private_root / "data"),
        }
        owner = "f" * 64
        controller = migration_verifier._DisposableCaddyController(
            runtime,
            repository,
            54321,
            corrupt_route=False,
        )
        spec = migration_verifier._CaddyLaunchSpec(
            repository,
            (("PATH", "/generated/plist/path"),),
        )
        with (
            mock.patch.dict(os.environ, {PARENT_SECRET_NAME: "private"}),
            mock.patch.object(
                migration_verifier,
                "_PRIVATE_EXECUTION_ENVIRONMENT",
                base,
                create=True,
            ),
            mock.patch.object(migration_verifier, "_PROCESS_OWNER_TOKEN", owner),
        ):
            environment = controller._execution_environment(spec)
        self.assertEqual(
            environment,
            {
                **base,
                "PATH": "/generated/plist/path",
                "HOME": str(controller.environment_home),
                "XDG_CONFIG_HOME": str(controller.environment_config),
                "XDG_DATA_HOME": str(controller.environment_data),
                "LOCAL_WEB_PUBLIC_BASE_PATH_MIGRATION_OWNER": owner,
            },
        )
        self.assertNotIn(PARENT_SECRET_NAME, environment)

    @acceptance
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

        verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
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

    @acceptance
    def test_successful_worker_cleanup_never_replays_the_pid_ledger(self):
        lines: list[str] = []

        def command_factory(root: Path, *_args: Path) -> tuple[str, ...]:
            protocol = "".join(
                [*(f"PASS {index}\\n" for index in range(len(EXPECTED_PHASES) - 1)), "CLEAN PASS\\n"]
            )
            return ("/usr/bin/printf", protocol)

        verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
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
                [*(f"print('PASS {index}')" for index in range(len(EXPECTED_PHASES) - 1)), "print('CLEAN FAIL')"]
            )
            return (
                "/usr/bin/env",
                "python3",
                "-c",
                f"import sys; {statements}; sys.exit(1)",
            )

        lines: list[str] = []
        verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
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
        verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
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

    @acceptance
    def test_reaper_finds_owned_descendant_after_group_leader_exits(self):
        token = "a" * 64
        child_pid_file = self.root / "descendant.pid"
        environment = {
            **os.environ,
            "LOCAL_WEB_PUBLIC_BASE_PATH_MIGRATION_OWNER": token,
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
                b"python LOCAL_WEB_PUBLIC_BASE_PATH_MIGRATION_OWNER=" + first + b"\n",
                b"python LOCAL_WEB_PUBLIC_BASE_PATH_MIGRATION_OWNER=" + second + b"\n",
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

    def test_token_reaper_drops_reused_pid_before_term_without_signalling(self):
        owner = "a" * 64
        discovered = migration_verifier._OwnedProcess(
            4242,
            "Mon Jan  1 00:00:00 2024",
            owner,
        )
        replacement = migration_verifier._OwnedProcess(
            4242,
            "Tue Jan  2 00:00:00 2024",
            "b" * 64,
        )
        with (
            mock.patch.object(
                migration_verifier,
                "_discover_owned_processes",
                side_effect=((discovered,), (), ()),
            ),
            mock.patch.object(
                migration_verifier,
                "_owned_process_identity",
                return_value=replacement,
            ),
            mock.patch.object(migration_verifier.os, "kill") as kill,
        ):
            self.assertTrue(
                migration_verifier._reap_owned_processes(
                    owner,
                    cleanup_timeout=0,
                )
            )
        kill.assert_not_called()

    def test_token_reaper_revalidates_before_kill_and_drops_unverifiable_pid(self):
        owner = "a" * 64
        discovered = migration_verifier._OwnedProcess(
            4242,
            "Mon Jan  1 00:00:00 2024",
            owner,
        )
        with (
            mock.patch.object(
                migration_verifier,
                "_discover_owned_processes",
                side_effect=((discovered,), (discovered,), ()),
            ),
            mock.patch.object(
                migration_verifier,
                "_owned_process_identity",
                side_effect=(discovered, None),
            ),
            mock.patch.object(migration_verifier.os, "kill") as kill,
        ):
            self.assertTrue(
                migration_verifier._reap_owned_processes(
                    owner,
                    cleanup_timeout=0,
                )
            )
        kill.assert_called_once_with(4242, signal.SIGTERM)

    @acceptance
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

        verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
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

    @acceptance
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

        verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
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

    @acceptance
    def test_supervisor_reaps_term_resistant_detached_setsid_descendant(self):
        lines: list[str] = []
        child_pid_file = self.root / "detached-child.pid"
        ready_file = self.root / "detached-child.ready"

        def command_factory(_root: Path, *_args: Path) -> tuple[str, ...]:
            child_source = (
                "import os,signal,time; from pathlib import Path; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"Path({str(child_pid_file)!r}).write_text(str(os.getpid())); "
                f"Path({str(ready_file)!r}).write_text('ready'); time.sleep(60)"
            )
            leader_source = (
                "import subprocess,sys,time; from pathlib import Path; "
                f"subprocess.Popen([sys.executable,'-c',{child_source!r}], start_new_session=True); "
                f"ready=Path({str(ready_file)!r}); "
                "deadline=time.monotonic()+2; "
                "exec(\"while not ready.exists() and time.monotonic()<deadline: time.sleep(0.01)\")"
            )
            return ("/usr/bin/env", "python3", "-c", leader_source)

        verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
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
                os.kill(int(child_pid_file.read_text()), 0)
        finally:
            if child_pid_file.exists():
                try:
                    os.kill(int(child_pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @acceptance
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
                        "env={**os.environ,'LOCAL_WEB_PUBLIC_BASE_PATH_MIGRATION_OWNER':token}); "
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

                verifier = migration_verifier.PublicBasePathMigrationWorkflowVerifier(
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
