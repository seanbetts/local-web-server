import hashlib
import json
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import scripts.verify_new_app_workflow as new_app_verifier
from scripts.disposable_workflow_support import initialise_disposable_host_profile
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore

from scripts.verify_new_app_workflow import (
    NewAppWorkflowVerifier,
    WorkflowCommand,
    _DisposableDeployer,
    _DisposableHttpServer,
    _stop_process_group,
    execute_command,
    execute_hosted_service_csp_acceptance,
)
from local_web_server.app_provenance import CURRENT_TEMPLATE_VERSION
from local_web_server.models import HostApp, HostRegistry
from local_web_server.ui_package import CURRENT_UI_PACKAGE_VERSION


ROOT = Path(__file__).parents[1]


class RecordingExecutor:
    def __init__(self, registry: Path, failure_label: str | None = None):
        self.registry = registry
        self.failure_label = failure_label
        self.commands: list[WorkflowCommand] = []
        self.generated_repository: Path | None = None

    def __call__(self, command: WorkflowCommand) -> None:
        self.commands.append(command)
        if command.label == "app init":
            repository = Path(command.argv[3])
            self.generated_repository = repository
            (repository / ".local-web-platform.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "templateVersion": CURRENT_TEMPLATE_VERSION,
                        "platformContractVersion": 1,
                        "ui": {"version": CURRENT_UI_PACKAGE_VERSION, "sha256": "a" * 64},
                        "capabilities": [],
                        "domainPaletteTokens": [],
                        "managedFiles": [],
                    }
                ),
                encoding="utf-8",
            )
            (repository / "src").mkdir()
            (repository / "src/contextExport.ts").write_text(
                "export const buildContextExport = async () => ({\n"
                "  schema: 'local-web-context/v1',\n"
                "  sensitivity: { classification: 'private' },\n"
                "} satisfies LocalWebContextV1);\n",
                encoding="utf-8",
            )
        if command.label == self.failure_label:
            raise subprocess.CalledProcessError(
                9,
                command.argv,
                output=b"PRIVATE CHILD OUTPUT",
                stderr=b"PRIVATE ENVIRONMENT=/private/path",
            )


class RecordingActivation:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = []

    def __call__(self, repository: Path, temporary_root: Path, kind: str) -> None:
        self.calls.append((repository, temporary_root, kind))
        if self.fail:
            raise RuntimeError("PRIVATE ACTIVATION DETAIL")


class RecordingHostedCsp:
    def __init__(self):
        self.calls = []

    def __call__(self, repository: Path, temporary_root: Path) -> None:
        self.calls.append((repository, temporary_root))


class NewAppWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.coding_root = Path(self.temporary.name) / "Coding"
        self.coding_root.mkdir()
        guard_platform = self.root_platform("host-profile-guard")
        guard_content = (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "host": "new-app-guard.invalid",
                    "runtimeRoot": str(Path(self.temporary.name) / "guard-runtime"),
                    "apps": [],
                }
            )
            + "\n"
        ).encode("utf-8")
        self.registry = initialise_disposable_host_profile(
            guard_platform, guard_content
        )
        self.lines: list[str] = []
        self.activation = RecordingActivation()
        self.hosted_csp = RecordingHostedCsp()

    def tearDown(self):
        self.temporary.cleanup()

    def verifier(self, executor: RecordingExecutor) -> NewAppWorkflowVerifier:
        return NewAppWorkflowVerifier(
            platform_repository=ROOT,
            coding_root=self.coding_root,
            registry=self.registry,
            execute=executor,
            activate=self.activation,
            hosted_csp=self.hosted_csp,
            emit=self.lines.append,
        )

    def test_disposable_profile_helper_initialises_one_private_revision(self):
        platform = self.root_platform("new-app-platform")
        content = (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "host": "new-app.invalid",
                    "runtimeRoot": str(self.coding_root / "runtime"),
                    "apps": [],
                }
            )
            + "\n"
        ).encode("utf-8")

        registry = initialise_disposable_host_profile(
            platform, content
        )
        paths = HostProfilePaths.for_repository(platform)
        store = HostProfileStore(paths)

        self.assertEqual(registry, paths.profile)
        self.assertEqual(store.read_current(), content)
        self.assertEqual(len(store.revisions()), 1)
        self.assertEqual(stat.S_IMODE(paths.local.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(paths.profile.stat().st_mode), 0o600)

    def test_default_registry_uses_the_owned_private_profile_path(self):
        class ProfileObservingExecutor(RecordingExecutor):
            observed_profile: Path | None = None
            observed_content: bytes | None = None
            observed_revision_count: int | None = None

            def __call__(inner_self, command: WorkflowCommand) -> None:
                if command.label == "app init":
                    paths = HostProfilePaths.for_repository(command.cwd)
                    store = HostProfileStore(paths)
                    inner_self.observed_profile = paths.profile
                    inner_self.observed_content = store.read_current()
                    inner_self.observed_revision_count = len(store.revisions())
                super().__call__(command)

        executor = ProfileObservingExecutor(Path("unused-explicit-registry"))
        verifier = NewAppWorkflowVerifier(
            platform_repository=ROOT,
            coding_root=self.coding_root,
            execute=executor,
            activate=self.activation,
            hosted_csp=self.hosted_csp,
            emit=self.lines.append,
        )

        self.assertEqual(verifier.run(), 0)
        repository = Path(executor.commands[0].argv[3])
        platform = repository.parent / "disposable-platform"
        self.assertEqual(executor.commands[0].cwd, platform)
        self.assertEqual(
            Path(executor.commands[0].argv[0]), platform / "bin/local-web"
        )
        self.assertEqual(
            executor.observed_profile,
            platform / "config/local/apps.json",
        )
        self.assertEqual(executor.observed_revision_count, 1)
        self.assertEqual(
            json.loads(executor.observed_content),
            {
                "schemaVersion": 1,
                "host": "new-app-workflow.invalid",
                "runtimeRoot": str(repository.parent / "runtime"),
                "apps": [],
            },
        )
        self.assertFalse(platform.exists())
        self.assertEqual(list(self.coding_root.iterdir()), [])

    def root_platform(self, name: str) -> Path:
        platform = Path(self.temporary.name) / name
        platform.mkdir()
        (platform / "config").mkdir()
        (platform / "local_web_server").mkdir()
        (platform / "local_web_server/source.py").write_text(
            "# disposable\n", encoding="utf-8"
        )
        (platform / ".gitignore").write_text(
            "config/local/\n", encoding="utf-8"
        )
        subprocess.run(
            ("/usr/bin/git", "init", "-q", "-b", "main"),
            cwd=platform,
            check=True,
        )
        subprocess.run(
            ("/usr/bin/git", "add", "."), cwd=platform, check=True
        )
        subprocess.run(
            (
                "/usr/bin/git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@invalid",
                "commit",
                "-qm",
                "fixture",
            ),
            cwd=platform,
            check=True,
        )
        return platform.resolve()

    def test_process_group_permission_probe_still_reaps_owned_leader(self):
        class Process:
            pid = 4242

            def __init__(self):
                self.wait_calls = 0

            def poll(self):
                return None

            def wait(self):
                self.wait_calls += 1

        process = Process()
        signal_calls: list[tuple[int, int]] = []

        def kill_group(process_group: int, signal_number: int) -> None:
            signal_calls.append((process_group, signal_number))
            if len(signal_calls) == 1:
                raise PermissionError("transient process-group probe denial")
            if signal_number == 0:
                raise ProcessLookupError

        with mock.patch(
            "scripts.verify_new_app_workflow.os.killpg", side_effect=kill_group
        ):
            try:
                _stop_process_group(process)
            except PermissionError:
                self.fail("a permission-denied probe abandoned owned process cleanup")

        self.assertEqual(
            signal_calls,
            [
                (4242, 0),
                (4242, signal.SIGTERM),
                (4242, 0),
                (4242, 0),
                (4242, 0),
            ],
        )
        self.assertEqual(process.wait_calls, 1)

    def test_process_group_disappearing_before_sigterm_still_reaps_owned_leader(self):
        class Process:
            pid = 4242

            def __init__(self):
                self.wait_calls = 0

            def poll(self):
                return None

            def wait(self):
                self.wait_calls += 1

        process = Process()
        signal_calls: list[tuple[int, int]] = []

        def kill_group(process_group: int, signal_number: int) -> None:
            signal_calls.append((process_group, signal_number))
            if signal_number == signal.SIGTERM:
                raise ProcessLookupError
            if len(signal_calls) > 2:
                raise ProcessLookupError

        with mock.patch(
            "scripts.verify_new_app_workflow.os.killpg", side_effect=kill_group
        ):
            try:
                _stop_process_group(process)
            except ProcessLookupError:
                self.fail("a group-exit race at SIGTERM abandoned process reaping")

        self.assertEqual(
            signal_calls[:2], [(4242, 0), (4242, signal.SIGTERM)]
        )
        self.assertEqual(process.wait_calls, 1)

    def test_process_group_disappearing_before_sigkill_still_reaps_owned_leader(self):
        class Process:
            pid = 4242

            def __init__(self):
                self.wait_calls = 0

            def poll(self):
                return None

            def wait(self):
                self.wait_calls += 1

        process = Process()
        signal_calls: list[tuple[int, int]] = []

        def kill_group(process_group: int, signal_number: int) -> None:
            signal_calls.append((process_group, signal_number))
            if signal_number == signal.SIGKILL:
                raise ProcessLookupError

        with (
            mock.patch(
                "scripts.verify_new_app_workflow._process_group_exists",
                side_effect=(True, True, False),
            ),
            mock.patch(
                "scripts.verify_new_app_workflow.time.monotonic",
                side_effect=(0, 2, 2, 4),
            ),
            mock.patch(
                "scripts.verify_new_app_workflow.os.killpg",
                side_effect=kill_group,
            ),
        ):
            try:
                _stop_process_group(process)
            except ProcessLookupError:
                self.fail("a group-exit race at SIGKILL abandoned process reaping")

        self.assertEqual(
            signal_calls,
            [(4242, signal.SIGTERM), (4242, signal.SIGKILL)],
        )
        self.assertEqual(process.wait_calls, 1)

    def test_disposable_deployer_runs_activation_install_callback(self):
        """The acceptance deployer must preserve AppActivator's install transaction."""
        repository = Path(self.temporary.name) / "fixture"
        repository.mkdir()
        (repository / "local-web.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "id": "fixture",
                    "title": "Fixture",
                    "route": "/fixture",
                    "kind": "static",
                    "build": {
                        "commands": [["true"]],
                        "output": "dist",
                        "environment": [],
                    },
                    "healthPath": "/fixture/",
                    "home": {"icon": "book", "accent": "#8EA7C6"},
                }
            ),
            encoding="utf-8",
        )
        runtime = Path(self.temporary.name) / "runtime"
        host = HostApp(
            id="fixture",
            repository=repository,
            auto_deploy=False,
            environment_file=None,
            environment=(),
            port=None,
            start_command=None,
        )
        events: list[str] = []
        with _DisposableHttpServer(runtime) as server:
            registry = HostRegistry(server.host, runtime, (host,))

            result = _DisposableDeployer(
                registry, repository, fail=False
            ).deploy("fixture", install=lambda: events.append("install"))

        self.assertEqual(result.outcome, "deployed")
        self.assertEqual(events, ["install"])

    def test_runs_the_complete_disposable_lifecycle_with_fixed_commands(self):
        """Dropping, reordering, or broadening a lifecycle command must fail."""
        before = self.registry.read_bytes()
        executor = RecordingExecutor(self.registry)

        code = self.verifier(executor).run()

        self.assertEqual(code, 0)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertIsNotNone(executor.generated_repository)
        self.assertFalse(executor.generated_repository.exists())
        self.assertEqual(list(self.coding_root.iterdir()), [])

        repository = Path(executor.commands[0].argv[3])
        local_web = str((ROOT / "bin/local-web").resolve())
        self.assertEqual(
            executor.commands,
            [
                WorkflowCommand(
                    "app init",
                    (
                        local_web,
                        "app",
                        "init",
                        str(repository),
                        "--title",
                        "Disposable Workflow Verification",
                        "--icon",
                        "book",
                        "--accent",
                        "#8EA7C6",
                    ),
                    ROOT,
                    600,
                ),
                WorkflowCommand(
                    "app doctor",
                    (local_web, "app", "doctor", "--repository", str(repository)),
                    ROOT,
                    60,
                ),
                WorkflowCommand(
                    "app-local dependency preparation",
                    ("npm", "ci", "--ignore-scripts"),
                    repository,
                    300,
                ),
                WorkflowCommand(
                    "app check",
                    (local_web, "app", "check", "--repository", str(repository)),
                    ROOT,
                    600,
                ),
                WorkflowCommand(
                    "app activate preview",
                    (
                        local_web,
                        "app",
                        "activate",
                        "--repository",
                        str(repository),
                    ),
                    ROOT,
                    60,
                ),
            ],
        )
        self.assertEqual(
            self.lines,
            [
                "create empty folder PASS",
                "app init PASS",
                "platform version 1",
                f"template version {CURRENT_TEMPLATE_VERSION}",
                f"UI version {CURRENT_UI_PACKAGE_VERSION}",
                f"UI digest {'a' * 64}",
                "context export schema local-web-context/v1",
                "context export sensitivity private",
                "app doctor PASS",
                "app-local dependency preparation PASS",
                "app check PASS",
                "app activate preview PASS",
                "app activate apply/recovery PASS",
                f"registry digest {hashlib.sha256(before).hexdigest()}",
                "cleanup PASS",
            ],
        )
        self.assertEqual(
            self.activation.calls,
            [(repository, repository.parent, "static")],
        )

    def test_failure_after_context_metadata_keeps_registry_root_and_process_cleanup(self):
        """A post-init failure must not leak the generated root or owned child group."""

        class ProcessFailingExecutor(RecordingExecutor):
            def __init__(inner_self, registry: Path, pid_path: Path):
                super().__init__(registry)
                inner_self.pid_path = pid_path

            def __call__(inner_self, command: WorkflowCommand) -> None:
                super().__call__(command)
                if command.label == "app doctor":
                    execute_command(
                        WorkflowCommand(
                            "injected process failure",
                            (
                                sys.executable,
                                "-c",
                                "from pathlib import Path; import os, time; "
                                f"Path({str(inner_self.pid_path)!r}).write_text(str(os.getpid())); "
                                "time.sleep(60)",
                            ),
                            ROOT,
                            1,
                        )
                    )

        before = self.registry.read_bytes()
        pid_path = Path(self.temporary.name) / "child-pid"
        executor = ProcessFailingExecutor(self.registry, pid_path)

        code = self.verifier(executor).run()

        self.assertEqual(code, 1)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertTrue(pid_path.is_file())
        process_id = int(pid_path.read_text(encoding="utf-8"))
        process = subprocess.run(
            ("ps", "-o", "stat=", "-p", str(process_id)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
        self.assertTrue(
            process.returncode != 0 or process.stdout.strip().startswith("Z"),
            "injected child process survived workflow cleanup",
        )
        self.assertEqual(self.lines[-2:], ["app doctor FAIL", "cleanup PASS"])
        self.assertIn("context export schema local-web-context/v1", self.lines)
        self.assertIn("context export sensitivity private", self.lines)
        self.assertEqual(list(self.coding_root.iterdir()), [])
        self.assertNotIn("local-web-context/v1',", "\n".join(self.lines))

    def test_workflow_commands_disable_python_bytecode_in_disposable_clones(self):
        marker = Path(self.temporary.name) / "bytecode-mode"

        execute_command(
            WorkflowCommand(
                "inspect bytecode mode",
                (
                    sys.executable,
                    "-c",
                    "import os, pathlib; "
                    f"pathlib.Path({str(marker)!r}).write_text("
                    "os.environ.get('PYTHONDONTWRITEBYTECODE', ''))",
                ),
                ROOT,
                5,
            )
        )

        self.assertEqual(marker.read_text(encoding="utf-8"), "1")

    def test_child_failure_is_labeled_without_output_environment_or_path_leakage(self):
        """Surfacing a child exception would disclose private process data."""
        executor = RecordingExecutor(self.registry, failure_label="app check")

        code = self.verifier(executor).run()

        self.assertEqual(code, 1)
        self.assertEqual(
            self.lines[-2:], ["app check FAIL", "cleanup PASS"]
        )
        report = "\n".join(self.lines)
        self.assertNotIn("PRIVATE", report)
        self.assertNotIn(str(self.temporary.name), report)
        self.assertNotIn("ENVIRONMENT", report)
        self.assertIsNotNone(executor.generated_repository)
        self.assertFalse(executor.generated_repository.exists())
        self.assertEqual(list(self.coding_root.iterdir()), [])

    def test_runs_service_lifecycle_with_automatic_port_and_release_contract(self):
        class ServiceExecutor(RecordingExecutor):
            def __call__(inner_self, command: WorkflowCommand) -> None:
                super().__call__(command)
                repository = Path(inner_self.commands[0].argv[3])
                if command.label == "app init":
                    (repository / "local-web.json").write_text(
                        json.dumps(
                            {
                                "schemaVersion": 1,
                                "id": "disposable-workflow-verification",
                                "title": "Disposable Workflow Verification",
                                "route": "/disposable-workflow-verification",
                                "kind": "service",
                                "build": {
                                    "commands": [["npm", "run", "build"]],
                                    "output": "release",
                                    "environment": ["VITE_PUBLIC_BASE_PATH"],
                                    "release": [
                                        {"source": "dist", "target": "public"},
                                        {"source": "server", "target": "server"},
                                    ],
                                },
                                "healthPath": "/disposable-workflow-verification/healthz",
                                "service": {
                                    "module": "server/service.mjs",
                                    "internalHealthPath": "/healthz",
                                    "frontendOutput": "public",
                                    "proxyPaths": ["/api"],
                                },
                            }
                        )
                    )
                if command.label == "app check":
                    (repository / "release/public").mkdir(parents=True)
                    (repository / "release/server").mkdir()

        executor = ServiceExecutor(self.registry)
        verifier = NewAppWorkflowVerifier(
            platform_repository=ROOT,
            coding_root=self.coding_root,
            registry=self.registry,
            execute=executor,
            activate=self.activation,
            hosted_csp=self.hosted_csp,
            emit=self.lines.append,
            kind="service",
        )

        code = verifier.run()

        self.assertEqual(code, 0)
        self.assertEqual(executor.commands[0].argv[-2:], ("--kind", "service"))
        self.assertNotIn("--port", tuple(arg for command in executor.commands for arg in command.argv))
        self.assertEqual(executor.commands[-1].label, "app activate preview")
        self.assertEqual(self.activation.calls[0][2], "service")
        self.assertEqual(
            self.hosted_csp.calls,
            [(Path(executor.commands[0].argv[3]), Path(executor.commands[0].argv[3]).parent)],
        )
        self.assertIn("release contract PASS", self.lines)
        for line in (
            "context export schema local-web-context/v1",
            "context export sensitivity private",
            "app check PASS",
            "app activate preview PASS",
            "hosted generated service CSP acceptance PASS",
            "app activate apply/recovery PASS",
            "cleanup PASS",
        ):
            with self.subTest(line=line):
                self.assertIn(line, self.lines)
        self.assertNotIn("foundation", "\n".join(self.lines))

    def test_hosted_service_csp_acceptance_builds_the_registered_route_prefix(self):
        repository = Path(self.temporary.name) / "generated-service"
        repository.mkdir()
        (repository / "local-web.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "id": "generated-service",
                    "title": "Generated Service",
                    "route": "/generated-service",
                    "kind": "service",
                    "build": {
                        "commands": [["npm", "run", "build"]],
                        "output": "release",
                        "environment": ["VITE_PUBLIC_BASE_PATH"],
                        "release": [
                            {"source": "dist", "target": "public"},
                            {"source": "server", "target": "server"},
                        ],
                    },
                    "healthPath": "/generated-service/healthz",
                    "service": {
                        "module": "server/service.mjs",
                        "internalHealthPath": "/healthz",
                        "frontendOutput": "public",
                        "proxyPaths": ["/api"],
                    },
                }
            ),
            encoding="utf-8",
        )
        subprocess.run(("git", "init", "-b", "main"), cwd=repository, check=True, capture_output=True)
        subprocess.run(
            ("git", "config", "user.name", "Hosted CSP Test"),
            cwd=repository,
            check=True,
        )
        subprocess.run(
            ("git", "config", "user.email", "hosted-csp@localhost"),
            cwd=repository,
            check=True,
        )
        subprocess.run(("git", "add", "local-web.json"), cwd=repository, check=True)
        subprocess.run(("git", "commit", "-m", "fixture"), cwd=repository, check=True, capture_output=True)

        build_calls = []

        class RecordingBuilder:
            def build(inner_self, host, manifest, commit, layout, environment, log):
                build_calls.append((host, manifest, commit, layout, environment))
                release = layout.releases / commit
                (release / "public/assets").mkdir(parents=True)
                (release / "public/index.html").write_text(
                    '<script src="/generated-service/assets/index.js"></script>',
                    encoding="utf-8",
                )
                (release / "public/assets/index.js").write_text("", encoding="utf-8")
                (release / "server").mkdir()
                return release

        policy_calls = []

        def exercise(_repository, runtime_root, manifest, expected_csp):
            current = runtime_root / "apps/generated-service/current"
            self.assertIn(
                'src="/generated-service/assets/index.js"',
                (current / "public/index.html").read_text(encoding="utf-8"),
            )
            policy_calls.append((manifest, expected_csp))

        execute_hosted_service_csp_acceptance(
            repository,
            Path(self.temporary.name) / "hosted",
            builder=RecordingBuilder(),
            exercise=exercise,
        )

        self.assertEqual(len(build_calls), 1)
        host, manifest, commit, _layout, environment = build_calls[0]
        self.assertEqual(host.environment, (("VITE_PUBLIC_BASE_PATH", "/generated-service/"),))
        self.assertEqual(environment["VITE_PUBLIC_BASE_PATH"], "/generated-service/")
        self.assertEqual(len(commit), 40)
        self.assertIsNone(manifest.service.frontend_security)
        self.assertEqual(len(policy_calls), 2)
        self.assertIsNone(policy_calls[0][0].service.frontend_security)
        self.assertEqual(
            policy_calls[1][0].service.frontend_security.connect_sources,
            ("https://api.maptiler.com",),
        )
        self.assertEqual(
            policy_calls[1][0].service.frontend_security.img_sources,
            ("https://images.example.test",),
        )

    def test_disposable_activation_failure_is_sanitised_and_preserves_live_registry(self):
        self.activation = RecordingActivation(fail=True)
        before = self.registry.read_bytes()

        code = self.verifier(RecordingExecutor(self.registry)).run()

        self.assertEqual(code, 1)
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(
            self.lines[-2:], ["app activate apply/recovery FAIL", "cleanup PASS"]
        )
        self.assertNotIn("PRIVATE", "\n".join(self.lines))

    def test_registry_mutation_fails_the_dry_run_step_and_still_cleans_up(self):
        """A regressed dry run must fail without hiding its live mutation."""

        class MutatingExecutor(RecordingExecutor):
            def __call__(inner_self, command: WorkflowCommand) -> None:
                super().__call__(command)
                if command.label == "app activate preview":
                    inner_self.registry.write_bytes(b"mutated private registry\n")

        executor = MutatingExecutor(self.registry)
        before = self.registry.read_bytes()

        code = self.verifier(executor).run()

        self.assertEqual(code, 1)
        self.assertNotEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.registry.read_bytes(), b"mutated private registry\n")
        self.assertEqual(
            self.lines[-2:], ["app activate preview FAIL", "cleanup PASS"]
        )
        self.assertNotIn("mutated", "\n".join(self.lines))
        self.assertEqual(list(self.coding_root.iterdir()), [])

    def _assert_failing_child_registry_mutation_is_preserved(self, label: str) -> None:
        """A failing child mutation must remain available as evidence."""

        class MutatingFailingExecutor(RecordingExecutor):
            def __call__(inner_self, command: WorkflowCommand) -> None:
                super().__call__(command)
                if command.label == label:
                    inner_self.registry.write_bytes(b"mutated private registry\n")
                    inner_self.registry.chmod(0o777)
                    raise subprocess.CalledProcessError(
                        9,
                        command.argv,
                        output=b"PRIVATE CHILD OUTPUT",
                        stderr=b"PRIVATE ENVIRONMENT=/private/path",
                    )

        self.registry.chmod(0o640)
        before = self.registry.read_bytes()
        before_mode = stat.S_IMODE(self.registry.stat().st_mode)
        executor = MutatingFailingExecutor(self.registry)

        code = self.verifier(executor).run()

        self.assertEqual(code, 1)
        self.assertNotEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.registry.read_bytes(), b"mutated private registry\n")
        self.assertNotEqual(stat.S_IMODE(self.registry.stat().st_mode), before_mode)
        self.assertEqual(stat.S_IMODE(self.registry.stat().st_mode), 0o777)
        self.assertEqual(self.lines[-2:], [f"{label} FAIL", "cleanup PASS"])
        report = "\n".join(self.lines)
        self.assertNotIn("PRIVATE", report)
        self.assertNotIn(str(self.temporary.name), report)
        self.assertEqual(list(self.coding_root.iterdir()), [])

    def test_registration_mutation_is_preserved_when_the_child_also_fails(self):
        """The verifier must not overwrite a failing registration's evidence."""

        self._assert_failing_child_registry_mutation_is_preserved(
            "app activate preview"
        )

    def test_earlier_child_mutation_is_preserved_when_that_child_fails(self):
        """The verifier must not overwrite an earlier child's evidence."""

        self._assert_failing_child_registry_mutation_is_preserved("app doctor")

    def test_registry_deletion_fails_immediately_without_recreating_the_file(self):
        """A child that deletes the registry must fail without verifier repair."""

        class DeletingExecutor(RecordingExecutor):
            def __call__(inner_self, command: WorkflowCommand) -> None:
                super().__call__(command)
                if command.label == "app doctor":
                    inner_self.registry.unlink()

        code = self.verifier(DeletingExecutor(self.registry)).run()

        self.assertEqual(code, 1)
        self.assertFalse(self.registry.exists())
        self.assertEqual(self.lines[-2:], ["app doctor FAIL", "cleanup PASS"])
        self.assertEqual(list(self.coding_root.iterdir()), [])

    def test_final_registry_guard_preserves_a_change_after_the_last_child_check(self):
        """A late concurrent edit must be detected and left untouched."""

        def emit(line: str) -> None:
            self.lines.append(line)
            if line == "app activate apply/recovery PASS":
                self.registry.write_bytes(b"late private registry change\n")

        verifier = NewAppWorkflowVerifier(
            platform_repository=ROOT,
            coding_root=self.coding_root,
            registry=self.registry,
            execute=RecordingExecutor(self.registry),
            activate=self.activation,
            emit=emit,
        )

        code = verifier.run()

        self.assertEqual(code, 1)
        self.assertEqual(self.registry.read_bytes(), b"late private registry change\n")
        self.assertEqual(
            self.lines[-2:], ["app activate apply/recovery FAIL", "cleanup PASS"]
        )
        self.assertEqual(list(self.coding_root.iterdir()), [])

    def test_successful_workflow_does_not_rewrite_an_unchanged_registry(self):
        """The recovery guard must remain read-only on the normal path."""
        before = self.registry.stat()
        executor = RecordingExecutor(self.registry)

        code = self.verifier(executor).run()

        after = self.registry.stat()
        self.assertEqual(code, 0)
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
        self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))


if __name__ == "__main__":
    unittest.main()
