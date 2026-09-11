import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import local_web_server.deploy as deploy_module
from local_web_server.config import load_manifest
from local_web_server.deploy import (
    DeploymentManager,
    HealthCheckFailed,
    RegisteredServiceRecoveryFailed,
    RegisteredServiceTransition,
    RollbackUnavailable,
    ServiceCommandRecoveryFailed,
    ServiceCommandTransition,
)
from local_web_server.git_build import BuildFailed, GitRepository
from local_web_server.models import BackendProbeSpec, Command, HostApp, HostRegistry
from local_web_server.runtime import (
    AppLock,
    DeploymentBusy,
    RuntimeLayout,
    atomic_symlink,
)
from local_web_server.services import HealthResult, LaunchctlServiceController, ServiceState
from tests.helpers import FakeHealthChecker, FakeServiceController, init_git_repo


class FakeBuilder:
    def __init__(self, commit: str):
        self.commit = commit
        self.failure: BaseException | None = None
        self.calls: list[str] = []
        self.manifests = []
        self.environments = []

    def build(self, host, manifest, commit, layout, environment, log):
        self.calls.append(commit)
        self.manifests.append(manifest)
        self.environments.append(dict(environment))
        if self.failure is not None:
            raise self.failure
        release = layout.releases / commit
        release.mkdir(parents=True)
        (release / "index.html").write_text(commit, encoding="utf-8")
        return release


class FakeRepository:
    def __init__(self, harness):
        self.harness = harness

    def main_commit(self):
        if self.harness.main_commits:
            return self.harness.main_commits.pop(0)
        return self.harness.builder.commit

    def current_branch(self):
        return self.harness.branch

    def manifest_at(self, commit):
        return self.harness.manifests_by_commit.get(
            commit, self.harness.committed_manifest
        )


class FailingStartServiceController(FakeServiceController):
    def __init__(self, start_failure: Exception, recovery_failure: Exception):
        super().__init__()
        self.start_failure = start_failure
        self.recovery_failure = recovery_failure

    def ensure_running(self, label, plist_path):
        super().ensure_running(label, plist_path)
        raise self.start_failure

    def restart(self, label, plist_path):
        super().restart(label, plist_path)
        raise self.recovery_failure

    def stop(self, label, plist_path):
        super().stop(label, plist_path)
        raise self.recovery_failure


class QueuedRestartServiceController(FakeServiceController):
    def __init__(self, failures):
        super().__init__()
        self.failures = list(failures)

    def restart(self, label, plist_path):
        super().restart(label, plist_path)
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure


class RaisingHealthChecker:
    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = []

    def check(self, url):
        self.calls.append(url)
        raise self.errors.pop(0)


class UrlHealthChecker:
    def __init__(self, results):
        self.results = dict(results)
        self.calls = []
        self.hosts = []

    def check(self, url, *, host=None):
        self.calls.append(url)
        self.hosts.append(host)
        return self.results[url]


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class TransitionRecorder:
    def __init__(self, candidate_failure=None, recovery_failure=None):
        self.candidate_failure = candidate_failure
        self.recovery_failure = recovery_failure
        self.primary = RuntimeError(f"{candidate_failure} failed")
        self.cleanup = RuntimeError(f"recovery {recovery_failure} failed")
        self.phase = "new"
        self.events = []
        self.replace_steps = []
        self.replace_labels = []
        self.require_lock = False
        self.locked = False

    def record(self, event):
        if self.require_lock and not self.locked:
            raise AssertionError(f"transition event escaped the app lock: {event}")
        self.events.append(event)

    def failing(self, stage):
        if self.phase == "new":
            return self.candidate_failure == stage
        return self.recovery_failure == stage

    def error(self):
        return self.primary if self.phase == "new" else self.cleanup


class TransitionBuilder(FakeBuilder):
    def __init__(self, commit, recorder):
        super().__init__(commit)
        self.recorder = recorder

    def build(self, host, manifest, commit, layout, environment, log):
        self.recorder.record("build")
        if self.recorder.failing("build"):
            raise self.recorder.error()
        return super().build(host, manifest, commit, layout, environment, log)


class TransitionServiceController:
    def __init__(self, recorder):
        self.recorder = recorder

    def state(self, label):
        self.recorder.record(f"state-{self.recorder.phase}")
        if self.recorder.failing("launchd-state"):
            return ServiceState.STOPPED
        return ServiceState.RUNNING

    def ensure_running(self, label, plist_path):
        raise AssertionError("registered-service transitions must not ensure a stale job")

    def restart(self, label, plist_path):
        raise AssertionError(
            "registered-service transitions must replace the exact target job"
        )

    def stop(self, label, plist_path):
        raise AssertionError(
            "registered-service transitions must replace the exact target job"
        )

    def replace(self, label, plist_path):
        phase = self.recorder.phase
        event = "replace-target" if phase == "new" else "replace-old-target"
        self.recorder.record(event)
        self.recorder.replace_labels.append(label)
        self.recorder.replace_steps.append((phase, "bootout", label, plist_path))
        if self.recorder.failing("bootout") or (
            phase == "old" and self.recorder.failing("replace")
        ):
            raise self.recorder.error()
        self.recorder.replace_steps.append((phase, "bootstrap", label, plist_path))
        if self.recorder.failing("bootstrap"):
            raise self.recorder.error()


class TransitionHealthChecker:
    def __init__(self, recorder):
        self.recorder = recorder
        self.calls = []
        self.hosts = []

    def check(self, url, *, host=None):
        if url == "http://fixture.test/fixture/":
            stage = "frontend"
        elif url.startswith("http://127.0.0.1:"):
            stage = "internal-http"
        else:
            stage = "public-http"
        self.recorder.record(f"{stage}-{self.recorder.phase}")
        self.calls.append(url)
        self.hosts.append(host)
        if self.recorder.failing(stage):
            return HealthResult(False, 503, f"{stage} failed")
        return HealthResult(True, 200, None)


class DeploymentHarness:
    def __init__(
        self,
        root: Path,
        kind: str,
        *,
        auto_deploy: bool = True,
        remote_backend: bool = False,
        split_service: bool = False,
    ):
        self.root = root
        self.repository = root / "repository"
        self.repository.mkdir()
        self.manifest = {
            "schemaVersion": 1,
            "id": "fixture",
            "title": "Fixture",
            "route": "/fixture",
            "kind": kind,
            "build": {
                "commands": [["python3", "-c", "pass"]],
                "output": "dist",
                "environment": [],
            },
            "healthPath": "/fixture/healthz",
        }
        if kind == "service":
            self.manifest["service"] = {
                "module": "fixture",
                "internalHealthPath": "/healthz",
            }
        self.main_hash = init_git_repo(
            self.repository,
            {"local-web.json": json.dumps(self.manifest)},
        )
        self.committed_manifest = load_manifest(self.repository / "local-web.json")
        if remote_backend:
            self.committed_manifest = replace(
                self.committed_manifest,
                build=replace(
                    self.committed_manifest.build,
                    environment=("PUBLIC_API_URL",),
                ),
            )
        if split_service:
            self.committed_manifest = replace(
                self.committed_manifest,
                service=replace(
                    self.committed_manifest.service,
                    frontend_output=Path("frontend"),
                    proxy_paths=("/api",),
                ),
            )
        self.manifests_by_commit = {self.main_hash: self.committed_manifest}
        self.runtime = root / "runtime"
        self.layout = RuntimeLayout(self.runtime, "fixture")
        self.launch_agents = root / "LaunchAgents"
        self.private_data = root / "app-data" / "private.sqlite"
        self.private_data.parent.mkdir()
        self.private_data.write_bytes(b"private")
        host = HostApp(
            id="fixture",
            repository=self.repository,
            auto_deploy=auto_deploy,
            environment_file=None,
            environment=(),
            port=8765 if kind == "service" else None,
            start_command=(
                Command(("python3", "-m", "fixture")) if kind == "service" else None
            ),
            backend_probe=(
                BackendProbeSpec(
                    public_environment=("PUBLIC_API_URL",),
                    base_url_environment="PUBLIC_API_URL",
                    path="/health",
                    headers_from_environment=(),
                )
                if remote_backend
                else None
            ),
        )
        self.registry = HostRegistry(
            host="fixture.test",
            runtime_root=self.runtime,
            apps=(host,),
        )
        if remote_backend:
            self.registry = replace(
                self.registry,
                apps=(
                    replace(
                        host,
                        environment=(("PUBLIC_API_URL", "https://example.invalid"),),
                    ),
                ),
            )
        self.builder = FakeBuilder(self.main_hash)
        self.main_commits = []
        self.health = FakeHealthChecker()
        self.services = FakeServiceController()
        self.clock = FakeClock()
        self.branch = "main"

    def manager(self, *, real_repository: bool = False):
        repository_factory = GitRepository if real_repository else lambda _path: FakeRepository(self)
        return DeploymentManager(
            self.registry,
            builder=self.builder,
            services=self.services,
            health=self.health,
            launch_agents=self.launch_agents,
            environment={"PATH": "/usr/bin:/bin"},
            repository_factory=repository_factory,
            monotonic=self.clock.monotonic,
            sleep=self.clock.sleep,
        )

    def seed_release(self, commit: str, *, current: bool = False, previous: bool = False):
        release = self.layout.releases / commit
        release.mkdir(parents=True, exist_ok=True)
        if current:
            atomic_symlink(self.layout.current, release)
        if previous:
            atomic_symlink(self.layout.previous, release)
        return release


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def assert_private_data_untouched(self, harness):
        self.assertEqual(harness.private_data.read_bytes(), b"private")

    def test_service_command_transition_names_remain_compatible(self):
        self.assertIs(ServiceCommandTransition, RegisteredServiceTransition)
        self.assertIs(
            ServiceCommandRecoveryFailed,
            RegisteredServiceRecoveryFailed,
        )

    def test_expected_live_commit_is_checked_under_lock_before_deployment(self):
        harness = DeploymentHarness(self.root, kind="static")
        former = harness.seed_release("a" * 40, current=True)
        external = harness.seed_release("b" * 40)
        validation_calls = 0

        def validate_locked() -> None:
            nonlocal validation_calls
            validation_calls += 1
            with self.assertRaises(DeploymentBusy):
                with AppLock(harness.layout):
                    pass
            atomic_symlink(harness.layout.current, external)

        with self.assertRaisesRegex(ValueError, "current release changed"):
            harness.manager().deploy(
                "fixture",
                expected_commit=harness.main_hash,
                expected_current_commit=former.name,
                validate_locked=validate_locked,
            )

        self.assertEqual(validation_calls, 1)
        self.assertEqual(harness.layout.current.resolve(), external)
        self.assertEqual(harness.builder.calls, [])

    def test_live_commit_change_during_build_is_not_overwritten(self):
        harness = DeploymentHarness(self.root, kind="static")
        former = harness.seed_release("a" * 40, current=True)
        external = harness.seed_release("b" * 40)

        class PointerRacingBuilder(FakeBuilder):
            def build(inner_self, *args, **kwargs):
                release = super().build(*args, **kwargs)
                atomic_symlink(harness.layout.current, external)
                return release

        harness.builder = PointerRacingBuilder(harness.main_hash)

        with self.assertRaisesRegex(ValueError, "current release changed"):
            harness.manager().deploy(
                "fixture",
                expected_commit=harness.main_hash,
                expected_current_commit=former.name,
                validate_locked=lambda: None,
            )

        self.assertEqual(harness.layout.current.resolve(), external)
        self.assertEqual(harness.builder.calls, [harness.main_hash])

    def test_live_commit_change_after_switch_is_not_overwritten_by_recovery(self):
        harness = DeploymentHarness(self.root, kind="static")
        former = harness.seed_release("a" * 40, current=True)
        external = harness.seed_release("b" * 40)

        class ExternalSelectionHealth:
            def check(inner_self, _url, *, host=None):
                atomic_symlink(harness.layout.current, external)
                return HealthResult(False, 503, "private failure")

        harness.health = ExternalSelectionHealth()

        with self.assertRaises(HealthCheckFailed):
            harness.manager().deploy(
                "fixture",
                expected_commit=harness.main_hash,
                expected_current_commit=former.name,
                validate_locked=lambda: None,
            )

        self.assertEqual(harness.layout.current.resolve(), external)

    def prepare_transition_harness(self, root, recorder, *, current_is_main=False):
        harness = DeploymentHarness(root, kind="service")
        current_commit = harness.main_hash if current_is_main else "a" * 40
        target_commit = harness.main_hash if current_is_main else "b" * 40
        current = harness.seed_release(current_commit, current=True)
        previous = harness.seed_release("c" * 40, previous=True)
        stale = harness.seed_release("d" * 40)
        if not current_is_main:
            harness.manifests_by_commit[current_commit] = replace(
                harness.committed_manifest,
                health_path="/fixture/old-healthz",
                service=replace(
                    harness.committed_manifest.service,
                    internal_health_path="/old-healthz",
                ),
            )
        harness.builder = TransitionBuilder(target_commit, recorder)
        harness.services = TransitionServiceController(recorder)
        harness.health = TransitionHealthChecker(recorder)
        harness.clock = FakeClock()
        sentinel = harness.repository / "data" / "transition-sentinel.bin"
        sentinel.parent.mkdir()
        sentinel.write_bytes(b"repository data must remain byte-identical\x00\xff")
        return harness, current, previous, stale, sentinel

    def registered_service_transition(
        self,
        harness,
        recorder,
        former_current,
        former_previous,
    ):
        def install():
            recorder.record("install-new")
            if recorder.failing("install"):
                raise recorder.error()

        def verify():
            recorder.record("verify-new-tile")
            if recorder.failing("tile-verification"):
                raise recorder.error()

        def restore():
            self.assertEqual(harness.layout.current.resolve(), former_current)
            self.assertEqual(harness.layout.previous.resolve(), former_previous)
            recorder.record("restore-old-registry-and-install")
            recorder.phase = "old"
            if recorder.failing("restore"):
                raise recorder.error()

        def verify_restored():
            recorder.record("verify-old-tile")
            if recorder.failing("verify-restored"):
                raise recorder.error()

        def validate_final():
            recorder.record("validate-final")
            if recorder.failing("final-validation"):
                raise recorder.error()

        return RegisteredServiceTransition(
            install=install,
            verify=verify,
            restore=restore,
            verify_restored=verify_restored,
            _expected_pointers=(former_current.name, former_previous.name),
            _target_commit=harness.builder.commit,
            validate_final=validate_final,
        )

    def assert_transition_sentinels_untouched(self, harness, sentinel):
        self.assertEqual(
            sentinel.read_bytes(),
            b"repository data must remain byte-identical\x00\xff",
        )
        self.assert_private_data_untouched(harness)

    def test_service_command_transition_orders_switch_replace_health_tile_and_prune(self):
        recorder = TransitionRecorder()
        harness, current, previous, stale, sentinel = self.prepare_transition_harness(
            self.root,
            recorder,
        )
        transition = self.registered_service_transition(
            harness,
            recorder,
            current,
            previous,
        )
        recorder.require_lock = True
        original_app_lock = deploy_module.AppLock
        original_atomic_symlink = deploy_module.atomic_symlink
        original_prune_releases = deploy_module.prune_releases
        protected_prunes = []

        class RecordingAppLock:
            def __init__(self, layout):
                self.real = original_app_lock(layout)

            def __enter__(self):
                self.real.__enter__()
                recorder.locked = True
                return self

            def open_deploy_log(self, filename):
                return self.real.open_deploy_log(filename)

            def __exit__(self, exc_type, exc_value, traceback):
                recorder.locked = False
                return self.real.__exit__(exc_type, exc_value, traceback)

        def select_and_record(pointer, target):
            self.assertTrue(recorder.locked)
            original_atomic_symlink(pointer, target)
            if pointer == harness.layout.current:
                recorder.record("current=new")
            elif pointer == harness.layout.previous:
                recorder.record("previous=old")

        def prune_and_record(layout, *, protected_commits=()):
            self.assertTrue(recorder.locked)
            protected_prunes.append(tuple(protected_commits))
            original_prune_releases(
                layout,
                protected_commits=protected_commits,
            )
            recorder.record("prune")

        with (
            patch("local_web_server.deploy.AppLock", RecordingAppLock),
            patch("local_web_server.deploy.atomic_symlink", select_and_record),
            patch("local_web_server.deploy.prune_releases", prune_and_record),
        ):
            result = harness.manager().deploy(
                "fixture",
                service_command_transition=transition,
            )

        self.assertEqual(result.outcome, "deployed")
        self.assertEqual(
            recorder.events,
            [
                "build",
                "current=new",
                "install-new",
                "replace-target",
                "state-new",
                "frontend-new",
                "internal-http-new",
                "public-http-new",
                "verify-new-tile",
                "previous=old",
                "prune",
                "validate-final",
            ],
        )
        self.assertEqual(harness.layout.current.resolve().name, "b" * 40)
        self.assertEqual(harness.layout.previous.resolve(), current)
        self.assertFalse(stale.exists())
        self.assertEqual(
            recorder.replace_labels,
            ["com.sean.local-web.fixture"],
        )
        self.assertEqual(protected_prunes, [(previous.name,)])
        self.assertEqual(
            [(phase, step) for phase, step, _label, _plist in recorder.replace_steps],
            [("new", "bootout"), ("new", "bootstrap")],
        )
        self.assert_transition_sentinels_untouched(harness, sentinel)

    def test_registered_service_transition_runs_for_a_fixed_environment_change(self):
        recorder = TransitionRecorder()
        harness, current, previous, stale, sentinel = self.prepare_transition_harness(
            self.root,
            recorder,
        )
        former_host = harness.registry.apps[0]
        candidate_host = replace(
            former_host,
            environment=(("VITE_PUBLIC_BASE_PATH", "/fixture/"),),
        )
        self.assertEqual(candidate_host.start_command, former_host.start_command)
        self.assertEqual(
            replace(candidate_host, environment=former_host.environment),
            former_host,
        )
        installed_host = former_host

        def install_candidate():
            nonlocal installed_host
            self.assertEqual(installed_host, former_host)
            installed_host = candidate_host
            recorder.record("install-fixed-environment")

        def verify_candidate():
            self.assertEqual(installed_host, candidate_host)
            recorder.record("verify-fixed-environment")

        transition = RegisteredServiceTransition(
            install=install_candidate,
            verify=verify_candidate,
            restore=lambda: None,
            verify_restored=lambda: None,
            _expected_pointers=(current.name, previous.name),
            _target_commit=harness.builder.commit,
            validate_final=lambda: recorder.record("validate-fixed-environment"),
        )
        original_atomic_symlink = deploy_module.atomic_symlink
        original_prune_releases = deploy_module.prune_releases

        def select_and_record(pointer, target):
            original_atomic_symlink(pointer, target)
            if pointer == harness.layout.current:
                recorder.record("switch-current")
            elif pointer == harness.layout.previous:
                recorder.record("switch-previous")

        def prune_and_record(layout, *, protected_commits=()):
            original_prune_releases(
                layout,
                protected_commits=protected_commits,
            )
            recorder.record("prune")

        with (
            patch("local_web_server.deploy.atomic_symlink", select_and_record),
            patch("local_web_server.deploy.prune_releases", prune_and_record),
        ):
            result = harness.manager().deploy(
                "fixture",
                service_command_transition=transition,
            )

        self.assertEqual(result.outcome, "deployed")
        self.assertEqual(
            recorder.events,
            [
                "build",
                "switch-current",
                "install-fixed-environment",
                "replace-target",
                "state-new",
                "frontend-new",
                "internal-http-new",
                "public-http-new",
                "verify-fixed-environment",
                "switch-previous",
                "prune",
                "validate-fixed-environment",
            ],
        )
        self.assertEqual(installed_host, candidate_host)
        self.assertFalse(stale.exists())
        self.assert_transition_sentinels_untouched(harness, sentinel)

    def test_service_command_transition_failure_recovers_exact_state_without_pruning(self):
        failure_stages = (
            "build",
            "install",
            "bootout",
            "bootstrap",
            "launchd-state",
            "frontend",
            "internal-http",
            "public-http",
            "tile-verification",
        )
        health_failures = {
            "launchd-state",
            "frontend",
            "internal-http",
            "public-http",
        }
        for failure_stage in failure_stages:
            with self.subTest(failure_stage=failure_stage):
                root = self.root / failure_stage
                root.mkdir()
                recorder = TransitionRecorder(candidate_failure=failure_stage)
                harness, current, previous, stale, sentinel = (
                    self.prepare_transition_harness(root, recorder)
                )
                transition = self.registered_service_transition(
                    harness,
                    recorder,
                    current,
                    previous,
                )
                expected_error = (
                    HealthCheckFailed
                    if failure_stage in health_failures
                    else RuntimeError
                )

                with self.assertRaises(expected_error) as raised:
                    harness.manager().deploy(
                        "fixture",
                        service_command_transition=transition,
                    )

                if failure_stage not in health_failures:
                    self.assertIs(raised.exception, recorder.primary)
                self.assertEqual(harness.layout.current.resolve(), current)
                self.assertEqual(harness.layout.previous.resolve(), previous)
                self.assertTrue(stale.exists())
                self.assertEqual(
                    (harness.layout.releases / ("b" * 40)).exists(),
                    failure_stage != "build",
                )
                self.assert_transition_sentinels_untouched(harness, sentinel)
                self.assertEqual(
                    set(recorder.replace_labels),
                    {"com.sean.local-web.fixture"}
                    if recorder.replace_labels
                    else set(),
                )
                restore_index = recorder.events.index(
                    "restore-old-registry-and-install"
                )
                old_replace_index = recorder.events.index("replace-old-target")
                self.assertLess(restore_index, old_replace_index)
                expected_restored_order = [
                    "replace-old-target",
                    "state-old",
                    "frontend-old",
                    "internal-http-old",
                    "public-http-old",
                    "verify-old-tile",
                ]
                old_events = [
                    event
                    for event in recorder.events[old_replace_index:]
                    if event in expected_restored_order
                ]
                self.assertEqual(old_events, expected_restored_order)
                self.assertIn(
                    "http://127.0.0.1:8765/old-healthz",
                    harness.health.calls,
                )
                self.assertIn(
                    "http://fixture.test/fixture/old-healthz",
                    harness.health.calls,
                )

    def test_pre_replacement_transition_recovery_failures_are_classified(self):
        for recovery_stage in ("replace", "frontend"):
            with self.subTest(recovery_stage=recovery_stage):
                root = self.root / f"pre-replacement-{recovery_stage}"
                root.mkdir()
                recorder = TransitionRecorder(
                    candidate_failure="install",
                    recovery_failure=recovery_stage,
                )
                harness, current, previous, stale, sentinel = (
                    self.prepare_transition_harness(root, recorder)
                )
                transition = self.registered_service_transition(
                    harness,
                    recorder,
                    current,
                    previous,
                )

                with self.assertRaises(RegisteredServiceRecoveryFailed) as raised:
                    harness.manager().deploy(
                        "fixture",
                        service_command_transition=transition,
                    )

                self.assertEqual(
                    str(raised.exception),
                    "registered-service transition recovery failed",
                )
                self.assertIs(raised.exception.__cause__, recorder.primary)
                self.assertEqual(harness.layout.current.resolve(), current)
                self.assertEqual(harness.layout.previous.resolve(), previous)
                self.assertTrue(stale.exists())
                self.assertTrue((harness.layout.releases / ("b" * 40)).exists())
                self.assertIn("restore-old-registry-and-install", recorder.events)
                self.assertIn("replace-old-target", recorder.events)
                self.assertNotIn("verify-old-tile", recorder.events)
                self.assert_transition_sentinels_untouched(harness, sentinel)

    def test_service_command_transition_recovery_failures_are_classified(self):
        recovery_stages = (
            "restore",
            "replace",
            "launchd-state",
            "frontend",
            "internal-http",
            "public-http",
            "verify-restored",
        )
        for recovery_stage in recovery_stages:
            with self.subTest(recovery_stage=recovery_stage):
                root = self.root / recovery_stage
                root.mkdir()
                recorder = TransitionRecorder(
                    candidate_failure="tile-verification",
                    recovery_failure=recovery_stage,
                )
                harness, current, previous, stale, sentinel = (
                    self.prepare_transition_harness(root, recorder)
                )
                transition = self.registered_service_transition(
                    harness,
                    recorder,
                    current,
                    previous,
                )

                with self.assertRaises(RegisteredServiceRecoveryFailed) as raised:
                    harness.manager().deploy(
                        "fixture",
                        service_command_transition=transition,
                    )

                self.assertEqual(
                    str(raised.exception),
                    "registered-service transition recovery failed",
                )
                self.assertIs(raised.exception.__cause__, recorder.primary)
                self.assertEqual(harness.layout.current.resolve(), current)
                self.assertEqual(harness.layout.previous.resolve(), previous)
                self.assertTrue(stale.exists())
                self.assertTrue((harness.layout.releases / ("b" * 40)).exists())
                self.assertTrue(
                    all(
                        label == "com.sean.local-web.fixture"
                        for label in recorder.replace_labels
                    )
                )
                self.assert_transition_sentinels_untouched(harness, sentinel)

    def test_service_command_transition_prune_failure_recovers_former_releases(self):
        recorder = TransitionRecorder()
        harness, current, previous, stale, sentinel = self.prepare_transition_harness(
            self.root,
            recorder,
        )
        transition = self.registered_service_transition(
            harness,
            recorder,
            current,
            previous,
        )
        primary = RuntimeError("prune failed after deleting stale releases")
        original_prune_releases = deploy_module.prune_releases
        protected_calls = []

        def prune_then_fail(layout, *, protected_commits=()):
            recorder.record("prune")
            protected_calls.append(tuple(protected_commits))
            original_prune_releases(
                layout,
                protected_commits=protected_commits,
            )
            raise primary

        with patch(
            "local_web_server.deploy.prune_releases",
            side_effect=prune_then_fail,
        ):
            with self.assertRaises(RuntimeError) as raised:
                harness.manager().deploy(
                    "fixture",
                    service_command_transition=transition,
                )

        self.assertIs(raised.exception, primary)
        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertTrue(current.exists())
        self.assertTrue(previous.exists())
        self.assertTrue((harness.layout.releases / ("b" * 40)).exists())
        self.assertFalse(stale.exists())
        self.assertEqual(protected_calls, [(previous.name,)])
        self.assertEqual(
            {path.name for path in harness.layout.releases.iterdir()},
            {current.name, previous.name, "b" * 40},
        )
        restore_index = recorder.events.index("restore-old-registry-and-install")
        old_replace_index = recorder.events.index("replace-old-target")
        self.assertLess(recorder.events.index("prune"), restore_index)
        self.assertLess(restore_index, old_replace_index)
        self.assertEqual(
            recorder.events[old_replace_index:],
            [
                "replace-old-target",
                "state-old",
                "frontend-old",
                "internal-http-old",
                "public-http-old",
                "verify-old-tile",
            ],
        )
        self.assert_transition_sentinels_untouched(harness, sentinel)

    def test_service_command_final_validation_failure_recovers_after_protected_prune(self):
        recorder = TransitionRecorder(candidate_failure="final-validation")
        harness, current, previous, stale, sentinel = self.prepare_transition_harness(
            self.root,
            recorder,
        )
        transition = self.registered_service_transition(
            harness,
            recorder,
            current,
            previous,
        )
        original_prune_releases = deploy_module.prune_releases

        def prune_and_record(layout, *, protected_commits=()):
            original_prune_releases(
                layout,
                protected_commits=protected_commits,
            )
            recorder.record("prune")

        with patch(
            "local_web_server.deploy.prune_releases",
            side_effect=prune_and_record,
        ):
            with self.assertRaises(RuntimeError) as raised:
                harness.manager().deploy(
                    "fixture",
                    service_command_transition=transition,
                )

        self.assertIs(raised.exception, recorder.primary)
        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertTrue(current.exists())
        self.assertTrue(previous.exists())
        self.assertTrue((harness.layout.releases / ("b" * 40)).exists())
        self.assertFalse(stale.exists())
        self.assertEqual(
            recorder.events,
            [
                "build",
                "install-new",
                "replace-target",
                "state-new",
                "frontend-new",
                "internal-http-new",
                "public-http-new",
                "verify-new-tile",
                "prune",
                "validate-final",
                "restore-old-registry-and-install",
                "replace-old-target",
                "state-old",
                "frontend-old",
                "internal-http-old",
                "public-http-old",
                "verify-old-tile",
            ],
        )
        self.assert_transition_sentinels_untouched(harness, sentinel)

    def test_service_command_late_main_advance_recovers_after_switch(self):
        recorder = TransitionRecorder()
        harness, current, previous, stale, sentinel = self.prepare_transition_harness(
            self.root,
            recorder,
        )
        transition = self.registered_service_transition(
            harness,
            recorder,
            current,
            previous,
        )
        target_commit = harness.builder.commit
        advanced_commit = "e" * 40
        harness.main_commits = [target_commit, advanced_commit]

        with self.assertRaisesRegex(ValueError, "target changed"):
            harness.manager().deploy(
                "fixture",
                service_command_transition=transition,
            )

        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertTrue(current.exists())
        self.assertTrue(previous.exists())
        self.assertTrue((harness.layout.releases / target_commit).exists())
        self.assertFalse(stale.exists())
        self.assertEqual(
            recorder.events,
            [
                "build",
                "install-new",
                "replace-target",
                "state-new",
                "frontend-new",
                "internal-http-new",
                "public-http-new",
                "verify-new-tile",
                "validate-final",
                "restore-old-registry-and-install",
                "replace-old-target",
                "state-old",
                "frontend-old",
                "internal-http-old",
                "public-http-old",
                "verify-old-tile",
            ],
        )
        self.assertNotIn(target_commit, repr(transition))
        self.assertNotIn(advanced_commit, repr(transition))
        self.assert_transition_sentinels_untouched(harness, sentinel)

    def test_service_command_transition_rejects_a_changed_expected_pointer_pair(self):
        recorder = TransitionRecorder()
        harness, current, previous, stale, sentinel = self.prepare_transition_harness(
            self.root,
            recorder,
        )
        transition = replace(
            self.registered_service_transition(
                harness,
                recorder,
                current,
                previous,
            ),
            _expected_pointers=("e" * 40, previous.name),
        )

        with self.assertRaisesRegex(ValueError, "pointer state changed"):
            harness.manager().deploy(
                "fixture",
                service_command_transition=transition,
            )

        self.assertEqual(recorder.events, [])
        self.assertEqual(harness.builder.calls, [])
        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertTrue(stale.exists())
        self.assertNotIn(current.name, repr(transition))
        self.assertNotIn(previous.name, repr(transition))
        self.assert_transition_sentinels_untouched(harness, sentinel)

    def test_current_release_command_transition_runs_without_moving_pointers(self):
        recorder = TransitionRecorder()
        harness, current, previous, stale, sentinel = self.prepare_transition_harness(
            self.root,
            recorder,
            current_is_main=True,
        )
        transition = self.registered_service_transition(
            harness,
            recorder,
            current,
            previous,
        )

        with (
            patch("local_web_server.deploy.atomic_symlink") as select_pointer,
            patch("local_web_server.deploy.prune_releases") as prune,
        ):
            result = harness.manager().deploy(
                "fixture",
                service_command_transition=transition,
            )

        self.assertEqual(result.outcome, "unchanged")
        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertTrue(stale.exists())
        self.assertEqual(harness.builder.calls, [])
        select_pointer.assert_not_called()
        prune.assert_not_called()
        self.assertEqual(
            recorder.events,
            [
                "install-new",
                "replace-target",
                "state-new",
                "frontend-new",
                "internal-http-new",
                "public-http-new",
                "verify-new-tile",
                "validate-final",
            ],
        )
        self.assert_transition_sentinels_untouched(harness, sentinel)

    def test_failed_current_release_transition_compensates_without_pointer_writes(self):
        recorder = TransitionRecorder(candidate_failure="tile-verification")
        harness, current, previous, stale, sentinel = self.prepare_transition_harness(
            self.root,
            recorder,
            current_is_main=True,
        )
        transition = self.registered_service_transition(
            harness,
            recorder,
            current,
            previous,
        )
        pointer_writes = []

        def reject_pointer_write(pointer, target):
            pointer_writes.append((pointer, target))
            raise RuntimeError("unchanged pointer was rewritten")

        with patch(
            "local_web_server.deploy.atomic_symlink",
            side_effect=reject_pointer_write,
        ):
            with self.assertRaises(RuntimeError) as raised:
                harness.manager().deploy(
                    "fixture",
                    service_command_transition=transition,
                )

        self.assertIs(raised.exception, recorder.primary)
        self.assertEqual(pointer_writes, [])
        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertTrue(stale.exists())
        self.assertEqual(
            recorder.events,
            [
                "install-new",
                "replace-target",
                "state-new",
                "frontend-new",
                "internal-http-new",
                "public-http-new",
                "verify-new-tile",
                "restore-old-registry-and-install",
                "replace-old-target",
                "state-old",
                "frontend-old",
                "internal-http-old",
                "public-http-old",
                "verify-old-tile",
            ],
        )
        self.assert_transition_sentinels_untouched(harness, sentinel)

    def test_install_and_service_command_transition_are_mutually_exclusive(self):
        harness = DeploymentHarness(self.root, kind="service")
        events = []
        transition = ServiceCommandTransition(
            install=lambda: events.append("install-new"),
            verify=lambda: events.append("verify-new-tile"),
            restore=lambda: events.append("restore-old-registry-and-install"),
            verify_restored=lambda: events.append("verify-old-tile"),
            _expected_pointers=(None, None),
            _target_commit=harness.main_hash,
            validate_final=lambda: events.append("validate-final"),
        )

        with self.assertRaisesRegex(ValueError, "install|transition"):
            harness.manager().deploy(
                "fixture",
                install=lambda: events.append("ordinary-install"),
                service_command_transition=transition,
            )

        self.assertEqual(events, [])

    def test_failed_build_never_switches_current(self):
        harness = DeploymentHarness(self.root, kind="static")
        old = harness.seed_release("a" * 40, current=True)
        harness.builder.failure = BuildFailed(("false",), 1)

        with self.assertRaises(BuildFailed):
            harness.manager().deploy("fixture")

        self.assertEqual(harness.layout.current.resolve(), old)
        self.assert_private_data_untouched(harness)

    def test_static_success_switches_current_without_service_restart(self):
        harness = DeploymentHarness(self.root, kind="static")
        old = harness.seed_release("a" * 40, current=True)
        harness.builder.commit = "b" * 40

        result = harness.manager().deploy("fixture")

        self.assertEqual(result.outcome, "deployed")
        self.assertEqual(harness.layout.current.resolve().name, "b" * 40)
        self.assertEqual(harness.layout.previous.resolve(), old)
        self.assertEqual(harness.services.calls, [])
        self.assert_private_data_untouched(harness)

    def test_remote_backed_static_deploy_gates_only_canonical_frontend(self):
        harness = DeploymentHarness(self.root, kind="static", remote_backend=True)
        old = harness.seed_release("a" * 40, current=True)
        harness.builder.commit = "b" * 40
        harness.health = UrlHealthChecker(
            {
                "http://fixture.test/fixture/": HealthResult(True, 200, None),
                "http://fixture.test/_local-web/health/fixture/backend": HealthResult(
                    False, 502, "provider unavailable"
                ),
            }
        )

        result = harness.manager().deploy("fixture")

        self.assertEqual(result.outcome, "deployed")
        self.assertEqual(harness.layout.current.resolve().name, "b" * 40)
        self.assertEqual(harness.layout.previous.resolve(), old)
        self.assertEqual(harness.health.calls, ["http://fixture.test/fixture/"])
        self.assertEqual(harness.services.calls, [])
        self.assert_private_data_untouched(harness)

    def test_service_success_switches_current_and_starts_only_its_label(self):
        harness = DeploymentHarness(self.root, kind="service")
        harness.builder.commit = "b" * 40

        harness.manager().deploy("fixture")

        self.assertEqual(
            harness.services.calls,
            [
                (
                    "ensure_running",
                    "com.sean.local-web.fixture",
                    harness.launch_agents / "com.sean.local-web.fixture.plist",
                ),
                ("state", "com.sean.local-web.fixture", None),
            ],
        )
        self.assert_private_data_untouched(harness)

    def test_static_to_service_selects_service_release_before_installation(self):
        """Installing while current is static can launch a nonexistent service module."""
        harness = DeploymentHarness(self.root, kind="service")
        static_commit = "a" * 40
        old = harness.seed_release(static_commit, current=True)
        harness.manifests_by_commit[static_commit] = replace(
            harness.committed_manifest,
            kind="static",
            service=None,
        )
        harness.builder.commit = "b" * 40
        installed_currents: list[Path] = []

        result = harness.manager().deploy(
            "fixture",
            install=lambda: installed_currents.append(
                harness.layout.current.resolve(strict=True)
            ),
        )

        service_release = harness.layout.releases / ("b" * 40)
        self.assertEqual(result.outcome, "deployed")
        self.assertEqual(installed_currents, [service_release])
        self.assertEqual(harness.layout.current.resolve(), service_release)
        self.assertEqual(harness.layout.previous.resolve(), old)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running", "state"],
        )

    def test_checkpointed_static_to_service_failure_stops_then_retries_cleanly(self):
        """Recovery must not restart a service against the restored static release."""
        harness = DeploymentHarness(self.root, kind="service")
        static_commit = "a" * 40
        old = harness.seed_release(static_commit, current=True)
        harness.manifests_by_commit[static_commit] = replace(
            harness.committed_manifest,
            kind="static",
            service=None,
        )
        harness.builder.commit = "b" * 40

        def fail_after_switch() -> None:
            self.assertEqual(
                harness.layout.current.resolve().name,
                harness.builder.commit,
            )
            raise RuntimeError("installation failed")

        with self.assertRaisesRegex(RuntimeError, "installation failed"):
            harness.manager().deploy("fixture", install=fail_after_switch)

        self.assertEqual(harness.layout.current.resolve(), old)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["stop"],
        )

        harness.services.calls.clear()
        result = harness.manager().deploy("fixture", install=lambda: None)

        self.assertEqual(result.outcome, "deployed")
        self.assertEqual(harness.layout.current.resolve().name, "b" * 40)
        self.assertEqual(harness.layout.previous.resolve(), old)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running", "state"],
        )

    def test_service_deploy_waits_through_transient_public_backend_failures(self):
        harness = DeploymentHarness(self.root, kind="service")
        old = harness.seed_release("a" * 40, current=True)
        harness.builder.commit = "b" * 40
        harness.health.results = [
            HealthResult(True, 200, None),
            HealthResult(True, 200, None),
            HealthResult(False, 502, "HTTP 502"),
            HealthResult(True, 200, None),
            HealthResult(True, 200, None),
            HealthResult(False, None, "connection refused"),
            HealthResult(True, 200, None),
            HealthResult(True, 200, None),
            HealthResult(True, 200, None),
        ]

        result = harness.manager().deploy("fixture")

        self.assertEqual(result.outcome, "deployed")
        self.assertEqual(harness.layout.current.resolve().name, "b" * 40)
        self.assertEqual(harness.layout.previous.resolve(), old)
        self.assertEqual(
            harness.health.calls,
            [
                "http://fixture.test/fixture/",
                "http://127.0.0.1:8765/healthz",
                "http://fixture.test/fixture/healthz",
            ]
            * 3,
        )
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running"] + ["state"] * 3,
        )
        self.assertEqual(harness.clock.sleeps, [0.25, 0.25])
        self.assert_private_data_untouched(harness)

    def test_service_deploy_waits_for_launchd_to_be_running_before_http_checks(self):
        harness = DeploymentHarness(self.root, kind="service")
        old = harness.seed_release("a" * 40, current=True)
        harness.builder.commit = "b" * 40
        harness.services = FakeServiceController(
            states=[ServiceState.STOPPED, ServiceState.RUNNING]
        )

        result = harness.manager().deploy("fixture")

        self.assertEqual(result.outcome, "deployed")
        self.assertEqual(harness.layout.current.resolve().name, "b" * 40)
        self.assertEqual(harness.layout.previous.resolve(), old)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running", "state", "state"],
        )
        self.assertEqual(harness.clock.sleeps, [0.25])
        self.assertEqual(len(harness.health.calls), 3)
        self.assert_private_data_untouched(harness)

    def test_service_deploy_waits_through_launchd_default_throttle(self):
        harness = DeploymentHarness(self.root, kind="service")
        old = harness.seed_release("a" * 40, current=True)
        harness.builder.commit = "b" * 40
        harness.services = FakeServiceController(
            states=[ServiceState.STOPPED] * 40 + [ServiceState.RUNNING]
        )

        result = harness.manager().deploy("fixture")

        self.assertEqual(result.outcome, "deployed")
        self.assertEqual(harness.layout.current.resolve().name, "b" * 40)
        self.assertEqual(harness.layout.previous.resolve(), old)
        self.assertEqual(harness.clock.sleeps, [0.25] * 40)
        self.assertEqual(len(harness.health.calls), 3)
        self.assert_private_data_untouched(harness)

    def test_persistently_stopped_service_deploy_is_bounded_and_restores_exact_state(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("c" * 40, previous=True)
        harness.builder.commit = "b" * 40
        harness.services = FakeServiceController(state=ServiceState.STOPPED)

        with self.assertRaises(HealthCheckFailed) as raised:
            harness.manager().deploy("fixture")

        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running"] + ["state"] * 61 + ["restart"],
        )
        self.assertEqual(harness.health.calls, [])
        self.assertEqual(harness.clock.sleeps, [0.25] * 60)
        self.assertIn("service is stopped", str(raised.exception))
        self.assert_private_data_untouched(harness)

    def test_public_health_failure_restores_old_release(self):
        harness = DeploymentHarness(self.root, kind="service")
        old = harness.seed_release("a" * 40, current=True)
        harness.builder.commit = "b" * 40
        harness.health = UrlHealthChecker(
            {
                "http://fixture.test/fixture/": HealthResult(True, 200, None),
                "http://127.0.0.1:8765/healthz": HealthResult(True, 200, None),
                "http://fixture.test/fixture/healthz": HealthResult(
                    False, 503, "HTTP 503"
                ),
            }
        )

        with self.assertRaises(HealthCheckFailed):
            harness.manager().deploy("fixture")

        self.assertEqual(harness.layout.current.resolve(), old)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running"] + ["state"] * 61 + ["restart"],
        )
        self.assert_private_data_untouched(harness)

    def test_split_service_backend_failure_restores_exact_release_pair_and_service(self):
        harness = DeploymentHarness(self.root, kind="service", split_service=True)
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("c" * 40, previous=True)
        harness.builder.commit = "b" * 40
        harness.health = UrlHealthChecker(
            {
                "http://fixture.test/fixture/": HealthResult(True, 200, None),
                "http://127.0.0.1:8765/healthz": HealthResult(True, 200, None),
                "http://fixture.test/fixture/healthz": HealthResult(
                    False, 503, "private backend detail"
                ),
            }
        )

        with self.assertRaises(HealthCheckFailed):
            harness.manager().deploy("fixture")

        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running"] + ["state"] * 61 + ["restart"],
        )
        self.assertEqual(harness.health.calls[:3], [
            "http://fixture.test/fixture/",
            "http://127.0.0.1:8765/healthz",
            "http://fixture.test/fixture/healthz",
        ])
        self.assertEqual(harness.health.hosts[:3], [None, "fixture.test", None])
        self.assertEqual(len(harness.health.calls), 183)
        self.assertEqual(harness.clock.sleeps, [0.25] * 60)
        self.assert_private_data_untouched(harness)

    def test_service_start_failure_restores_old_release_and_preserves_original_error(self):
        harness = DeploymentHarness(self.root, kind="service")
        old = harness.seed_release("a" * 40, current=True)
        prior = harness.seed_release("c" * 40, previous=True)
        harness.builder.commit = "b" * 40
        start_failure = OSError("start failed")
        harness.services = FailingStartServiceController(
            start_failure,
            RuntimeError("restart failed"),
        )

        with self.assertRaises(OSError) as raised:
            harness.manager().deploy("fixture")

        self.assertIs(raised.exception, start_failure)
        self.assertEqual(harness.layout.current.resolve(), old)
        self.assertEqual(harness.layout.previous.resolve(), prior)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running", "restart"],
        )
        self.assertFalse((harness.layout.releases / ("b" * 40)).exists())
        self.assertEqual(harness.health.calls, [])
        self.assert_private_data_untouched(harness)

    def test_first_service_start_failure_removes_current_and_preserves_original_error(self):
        harness = DeploymentHarness(self.root, kind="service")
        harness.builder.commit = "b" * 40
        start_failure = OSError("start failed")
        harness.services = FailingStartServiceController(
            start_failure,
            RuntimeError("stop failed"),
        )

        with self.assertRaises(OSError) as raised:
            harness.manager().deploy("fixture")

        self.assertIs(raised.exception, start_failure)
        self.assertFalse(harness.layout.current.exists())
        self.assertFalse(harness.layout.current.is_symlink())
        self.assertFalse(harness.layout.previous.exists())
        self.assertFalse(harness.layout.previous.is_symlink())
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running", "stop"],
        )
        self.assertFalse((harness.layout.releases / ("b" * 40)).exists())
        self.assertEqual(harness.health.calls, [])
        self.assert_private_data_untouched(harness)

    def test_first_service_health_failure_removes_pointer_and_stops_service(self):
        harness = DeploymentHarness(self.root, kind="service")
        harness.builder.commit = "b" * 40
        harness.health.result = HealthResult(False, 503, "HTTP 503")

        with self.assertRaises(HealthCheckFailed):
            harness.manager().deploy("fixture")

        self.assertFalse(harness.layout.current.exists())
        self.assertFalse(harness.layout.current.is_symlink())
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running"] + ["state"] * 61 + ["stop"],
        )
        self.assert_private_data_untouched(harness)

    def test_non_main_hook_invocation_is_a_noop(self):
        harness = DeploymentHarness(self.root, kind="static")
        old = harness.seed_release("a" * 40, current=True)
        prior = harness.seed_release("c" * 40, previous=True)
        harness.builder.commit = "b" * 40
        harness.branch = "feature/work"

        result = harness.manager().deploy("fixture", from_hook=True)

        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(len(harness.builder.calls), 0)
        self.assertEqual(harness.layout.current.resolve(), old)
        self.assertEqual(harness.layout.previous.resolve(), prior)
        self.assert_private_data_untouched(harness)

    def test_manual_deploy_uses_refs_heads_main_even_on_feature_branch(self):
        harness = DeploymentHarness(self.root, kind="static")
        subprocess.run(
            ["git", "switch", "-c", "feature/work"],
            cwd=harness.repository,
            check=True,
            capture_output=True,
        )
        (harness.repository / "feature.txt").write_text("feature", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=harness.repository, check=True)
        subprocess.run(
            ["git", "commit", "-m", "feature"],
            cwd=harness.repository,
            check=True,
            capture_output=True,
        )
        feature_hash = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=harness.repository,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()

        harness.manager(real_repository=True).deploy("fixture")

        self.assertEqual(harness.builder.calls, [harness.main_hash])
        self.assertNotEqual(harness.builder.calls[0], feature_hash)
        self.assertEqual(harness.layout.current.resolve().name, harness.main_hash)
        self.assert_private_data_untouched(harness)

    def test_deploy_uses_manifest_from_exact_main_when_working_tree_conflicts(self):
        harness = DeploymentHarness(self.root, kind="static")
        committed = dict(harness.manifest)
        committed["build"] = dict(committed["build"])
        committed["build"]["environment"] = ["COMMITTED_VALUE"]
        committed["healthPath"] = "/fixture/main-health"
        (harness.repository / "local-web.json").write_text(
            json.dumps(committed), encoding="utf-8"
        )
        subprocess.run(["git", "add", "local-web.json"], cwd=harness.repository, check=True)
        subprocess.run(
            ["git", "commit", "-m", "main manifest"],
            cwd=harness.repository,
            check=True,
            capture_output=True,
        )
        harness.main_hash = GitRepository(harness.repository).main_commit()
        harness.builder.commit = harness.main_hash

        dirty = dict(committed)
        dirty["build"] = dict(committed["build"])
        dirty["build"]["commands"] = [["false"]]
        dirty["build"]["output"] = "dirty-output"
        dirty["build"]["environment"] = ["DIRTY_VALUE"]
        dirty["healthPath"] = "/fixture/dirty-health"
        (harness.repository / "local-web.json").write_text(json.dumps(dirty), encoding="utf-8")
        host = replace(
            harness.registry.apps[0],
            environment=(("COMMITTED_VALUE", "from-main"), ("DIRTY_VALUE", "from-dirty")),
        )
        harness.registry = replace(harness.registry, apps=(host,))

        harness.manager(real_repository=True).deploy("fixture")

        self.assertEqual(harness.builder.manifests[0].build.output, Path("dist"))
        self.assertEqual(harness.builder.environments[0]["COMMITTED_VALUE"], "from-main")
        self.assertNotIn("DIRTY_VALUE", harness.builder.environments[0])
        self.assertEqual(harness.health.calls, ["http://fixture.test/fixture/"])
        self.assert_private_data_untouched(harness)

    def test_running_service_deployment_gracefully_replaces_the_composed_launchctl_adapter(self):
        harness = DeploymentHarness(self.root, kind="service")
        harness.builder.commit = "b" * 40
        harness.services = LaunchctlServiceController()
        label = "com.sean.local-web.fixture"
        target = f"gui/{os.getuid()}/{label}"
        domain = f"gui/{os.getuid()}"
        plist = harness.launch_agents / f"{label}.plist"
        with patch("local_web_server.services.subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(
                    ["launchctl", "print", target], 0, "\tstate = running\n", ""
                ),
                subprocess.CompletedProcess(
                    ["launchctl", "bootout", domain, str(plist)], 0, "", ""
                ),
                subprocess.CompletedProcess(
                    ["launchctl", "bootstrap", domain, str(plist)], 0, "", ""
                ),
                subprocess.CompletedProcess(
                    ["launchctl", "print", target], 0, "\tstate = running\n", ""
                ),
            ]

            harness.manager().deploy("fixture")

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["launchctl", "print", target],
                ["launchctl", "bootout", domain, str(plist)],
                ["launchctl", "bootstrap", domain, str(plist)],
                ["launchctl", "print", target],
            ],
        )
        self.assertEqual(harness.layout.current.resolve().name, "b" * 40)

    def test_second_success_moves_old_current_to_previous_and_prunes_older(self):
        harness = DeploymentHarness(self.root, kind="static")
        old = harness.seed_release("a" * 40, current=True)
        stale = harness.seed_release("c" * 40)
        harness.builder.commit = "b" * 40

        harness.manager().deploy("fixture")

        self.assertEqual(harness.layout.current.resolve().name, "b" * 40)
        self.assertEqual(harness.layout.previous.resolve(), old)
        self.assertFalse(stale.exists())
        self.assert_private_data_untouched(harness)

    def test_deploy_and_rollback_leave_repository_data_untouched(self):
        harness = DeploymentHarness(self.root, kind="static")
        data = harness.repository / "data/state.json"
        data.parent.mkdir()
        data.write_bytes(b'{"state":"canonical"}\n')
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("c" * 40, previous=True)
        harness.builder.commit = "b" * 40

        harness.manager().deploy("fixture")

        self.assertEqual(harness.layout.current.resolve().name, "b" * 40)
        self.assertEqual(harness.layout.previous.resolve(), current)

        harness.manager().rollback("fixture")

        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve().name, "b" * 40)
        self.assertNotEqual(harness.layout.previous.resolve(), previous)
        self.assertEqual(data.read_bytes(), b'{"state":"canonical"}\n')

    def test_rollback_swaps_pointers_and_checks_health(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("b" * 40, previous=True)

        result = harness.manager().rollback("fixture")

        self.assertEqual(harness.layout.current.resolve(), previous)
        self.assertEqual(harness.layout.previous.resolve(), current)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["restart", "state"],
        )
        self.assertEqual(
            harness.health.calls,
            [
                "http://fixture.test/fixture/",
                "http://127.0.0.1:8765/healthz",
                "http://fixture.test/fixture/healthz",
            ],
        )
        self.assertEqual(result.outcome, "rolled-back")
        self.assert_private_data_untouched(harness)

    def test_tailscale_mode_has_no_public_http_fallback(self):
        harness = DeploymentHarness(self.root, kind="service")
        original = harness.seed_release("a" * 40, current=True)
        harness.registry = replace(
            harness.registry,
            host="local.example.ts.net",
            schema_version=2,
            public_origin="https://local.example.ts.net",
            ingress_mode="tailscale-serve",
        )

        class HttpsOnlyPublicHealth(FakeHealthChecker):
            def check(inner_self, url, *, host=None):
                if url.startswith("http://") and not url.startswith(
                    "http://127.0.0.1:"
                ):
                    raise AssertionError(f"public HTTP fallback attempted: {url}")
                return super().check(url, host=host)

        harness.health = HttpsOnlyPublicHealth()

        deployed = harness.manager().deploy("fixture")
        rolled_back = harness.manager().rollback("fixture")

        self.assertEqual(deployed.outcome, "deployed")
        self.assertEqual(rolled_back.outcome, "rolled-back")
        self.assertEqual(harness.layout.current.resolve(), original)
        self.assertEqual(
            harness.health.calls,
            [
                "https://local.example.ts.net/fixture/",
                "http://127.0.0.1:8765/healthz",
                "https://local.example.ts.net/fixture/healthz",
            ]
            * 2,
        )
        self.assertEqual(
            harness.health.hosts,
            [None, "local.example.ts.net", None] * 2,
        )

    def test_remote_backed_static_rollback_gates_only_canonical_frontend(self):
        harness = DeploymentHarness(self.root, kind="static", remote_backend=True)
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("b" * 40, previous=True)
        harness.health = UrlHealthChecker(
            {
                "http://fixture.test/fixture/": HealthResult(True, 200, None),
                "http://fixture.test/_local-web/health/fixture/backend": HealthResult(
                    False, 502, "provider unavailable"
                ),
            }
        )

        result = harness.manager().rollback("fixture")

        self.assertEqual(result.outcome, "rolled-back")
        self.assertEqual(harness.layout.current.resolve(), previous)
        self.assertEqual(harness.layout.previous.resolve(), current)
        self.assertEqual(harness.health.calls, ["http://fixture.test/fixture/"])
        self.assertEqual(harness.services.calls, [])
        self.assert_private_data_untouched(harness)

    def test_service_rollback_waits_through_transient_public_backend_failure(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("b" * 40, previous=True)
        harness.health.results = [
            HealthResult(True, 200, None),
            HealthResult(True, 200, None),
            HealthResult(False, None, "connection refused"),
            HealthResult(True, 200, None),
            HealthResult(True, 200, None),
            HealthResult(True, 200, None),
        ]

        result = harness.manager().rollback("fixture")

        self.assertEqual(result.outcome, "rolled-back")
        self.assertEqual(harness.layout.current.resolve(), previous)
        self.assertEqual(harness.layout.previous.resolve(), current)
        self.assertEqual(
            harness.health.calls,
            [
                "http://fixture.test/fixture/",
                "http://127.0.0.1:8765/healthz",
                "http://fixture.test/fixture/healthz",
            ]
            * 2,
        )
        self.assertEqual(harness.clock.sleeps, [0.25])
        self.assert_private_data_untouched(harness)

    def test_service_rollback_waits_for_launchd_to_be_running_before_http_checks(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("b" * 40, previous=True)
        harness.services = FakeServiceController(
            states=[ServiceState.STOPPED, ServiceState.RUNNING]
        )

        result = harness.manager().rollback("fixture")

        self.assertEqual(result.outcome, "rolled-back")
        self.assertEqual(harness.layout.current.resolve(), previous)
        self.assertEqual(harness.layout.previous.resolve(), current)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["restart", "state", "state"],
        )
        self.assertEqual(harness.clock.sleeps, [0.25])
        self.assertEqual(len(harness.health.calls), 3)
        self.assert_private_data_untouched(harness)

    def test_persistently_stopped_service_rollback_is_bounded_and_restores_exact_state(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("b" * 40, previous=True)
        harness.services = FakeServiceController(state=ServiceState.STOPPED)

        with self.assertRaises(HealthCheckFailed) as raised:
            harness.manager().rollback("fixture")

        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["restart"] + ["state"] * 61 + ["restart"],
        )
        self.assertEqual(harness.health.calls, [])
        self.assertEqual(harness.clock.sleeps, [0.25] * 60)
        self.assertIn("service is stopped", str(raised.exception))
        self.assert_private_data_untouched(harness)

    def test_rollback_without_previous_is_rejected(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release("a" * 40, current=True)

        with self.assertRaises(RollbackUnavailable):
            harness.manager().rollback("fixture")

        self.assertEqual(harness.layout.current.resolve(), current)
        self.assert_private_data_untouched(harness)

    def test_deploy_of_current_main_recovers_service_and_rechecks_health(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release(harness.main_hash, current=True)

        result = harness.manager().deploy("fixture")

        self.assertEqual(result.outcome, "unchanged")
        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.builder.calls, [])
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running", "state"],
        )
        self.assertEqual(
            harness.health.calls,
            [
                "http://fixture.test/fixture/",
                "http://127.0.0.1:8765/healthz",
                "http://fixture.test/fixture/healthz",
            ],
        )
        self.assert_private_data_untouched(harness)

    def test_deploy_of_current_static_release_rejects_failed_health(self):
        harness = DeploymentHarness(self.root, kind="static")
        current = harness.seed_release(harness.main_hash, current=True)
        harness.health.result = HealthResult(False, 503, "PRIVATE HEALTH DETAIL")

        with self.assertRaises(HealthCheckFailed):
            harness.manager().deploy("fixture")

        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.builder.calls, [])
        self.assertEqual(harness.services.calls, [])
        self.assertEqual(harness.health.calls, ["http://fixture.test/fixture/"])

    def test_hook_skips_app_with_automatic_deployment_disabled(self):
        harness = DeploymentHarness(self.root, kind="static", auto_deploy=False)

        result = harness.manager().deploy("fixture", from_hook=True)

        self.assertEqual(result.outcome, "skipped")
        self.assertEqual(harness.builder.calls, [])
        self.assert_private_data_untouched(harness)

    def test_static_health_failure_restores_current_without_service_operations(self):
        harness = DeploymentHarness(self.root, kind="static")
        old = harness.seed_release("a" * 40, current=True)
        harness.builder.commit = "b" * 40
        harness.health.result = HealthResult(False, 503, "HTTP 503")

        with self.assertRaises(HealthCheckFailed):
            harness.manager().deploy("fixture")

        self.assertEqual(harness.layout.current.resolve(), old)
        self.assertEqual(harness.services.calls, [])
        self.assertEqual(harness.health.calls, ["http://fixture.test/fixture/"])
        self.assertEqual(harness.clock.sleeps, [])
        self.assert_private_data_untouched(harness)

    def test_persistent_service_health_failure_rolls_back_after_bounded_wait(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("c" * 40, previous=True)
        harness.builder.commit = "b" * 40
        harness.health.result = HealthResult(False, 503, "HTTP 503")

        with self.assertRaises(HealthCheckFailed):
            harness.manager().deploy("fixture")

        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertEqual(harness.health.calls, ["http://fixture.test/fixture/"] * 61)
        self.assertEqual(harness.clock.sleeps, [0.25] * 60)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running"] + ["state"] * 61 + ["restart"],
        )
        self.assert_private_data_untouched(harness)

    def test_failed_rollback_restores_both_pointers_and_old_service(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("b" * 40, previous=True)
        harness.health.result = HealthResult(False, 503, "HTTP 503")

        with self.assertRaises(HealthCheckFailed):
            harness.manager().rollback("fixture")

        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["restart"] + ["state"] * 61 + ["restart"],
        )
        self.assert_private_data_untouched(harness)

    def test_keyboard_interrupt_from_build_is_not_swallowed(self):
        harness = DeploymentHarness(self.root, kind="static")
        old = harness.seed_release("a" * 40, current=True)
        harness.builder.failure = KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            harness.manager().deploy("fixture")

        self.assertEqual(harness.layout.current.resolve(), old)
        self.assert_private_data_untouched(harness)

    def test_deploy_refuses_symlinked_log_directory_without_writing_external_target(self):
        harness = DeploymentHarness(self.root, kind="static")
        external = self.root / "external-logs"
        external.mkdir()
        harness.layout.deploy_logs.parent.mkdir(parents=True)
        harness.layout.deploy_logs.symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "runtime|log|symlink"):
            harness.manager().deploy("fixture")

        self.assertEqual(list(external.iterdir()), [])
        self.assertEqual(harness.builder.calls, [])
        self.assert_private_data_untouched(harness)

    def test_deploy_does_not_follow_a_symlinked_log_file(self):
        harness = DeploymentHarness(self.root, kind="static")
        harness.layout.deploy_logs.mkdir(parents=True)
        external = self.root / "external-log"
        external.write_bytes(b"sentinel log bytes")
        log_path = harness.layout.deploy_logs / f"fixture-{harness.builder.commit}.log"
        log_path.symlink_to(external)

        with self.assertRaisesRegex(ValueError, "log|symlink|runtime"):
            harness.manager().deploy("fixture")

        self.assertEqual(external.read_bytes(), b"sentinel log bytes")
        self.assertEqual(harness.builder.calls, [])
        self.assert_private_data_untouched(harness)

    def test_rollback_restart_failure_restores_exact_pointer_pair_and_original_service(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("b" * 40, previous=True)
        primary = OSError("candidate restart failed")
        cleanup = RuntimeError("original restart failed")
        harness.services = QueuedRestartServiceController([primary, cleanup])

        with self.assertRaises(OSError) as raised:
            harness.manager().rollback("fixture")

        self.assertIs(raised.exception, primary)
        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["restart", "restart"],
        )
        self.assertTrue(
            any("original restart failed" in note for note in raised.exception.__notes__)
        )
        self.assert_private_data_untouched(harness)

    def test_rollback_checker_exception_restores_exact_pointer_pair_and_service(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("b" * 40, previous=True)
        primary = OSError("checker crashed")
        harness.health = RaisingHealthChecker([primary])

        with self.assertRaises(OSError) as raised:
            harness.manager().rollback("fixture")

        self.assertIs(raised.exception, primary)
        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["restart", "state", "restart"],
        )
        self.assert_private_data_untouched(harness)

    def test_deploy_checker_exception_restores_exact_pointer_pair_and_service(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("c" * 40, previous=True)
        harness.builder.commit = "b" * 40
        primary = OSError("checker crashed")
        harness.health = RaisingHealthChecker([primary])

        with self.assertRaises(OSError) as raised:
            harness.manager().deploy("fixture")

        self.assertIs(raised.exception, primary)
        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertEqual(
            [call[0] for call in harness.services.calls],
            ["ensure_running", "state", "restart"],
        )
        self.assertFalse((harness.layout.releases / ("b" * 40)).exists())
        self.assert_private_data_untouched(harness)

    def test_health_failure_preserves_primary_when_recovery_restart_fails(self):
        harness = DeploymentHarness(self.root, kind="service")
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("c" * 40, previous=True)
        harness.builder.commit = "b" * 40
        harness.health.result = HealthResult(False, 503, "HTTP 503")
        cleanup = RuntimeError("original restart failed")
        harness.services = QueuedRestartServiceController([cleanup])

        with self.assertRaises(HealthCheckFailed) as raised:
            harness.manager().deploy("fixture")

        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertTrue(
            any("original restart failed" in note for note in raised.exception.__notes__)
        )
        self.assertFalse((harness.layout.releases / ("b" * 40)).exists())
        self.assert_private_data_untouched(harness)

    @patch("local_web_server.deploy.prune_releases", side_effect=OSError("prune failed"))
    def test_health_failure_preserves_primary_when_recovery_pruning_fails(self, prune):
        harness = DeploymentHarness(self.root, kind="static")
        current = harness.seed_release("a" * 40, current=True)
        previous = harness.seed_release("c" * 40, previous=True)
        harness.builder.commit = "b" * 40
        harness.health.results = [HealthResult(False, 503, "HTTP 503")]

        with self.assertRaises(HealthCheckFailed) as raised:
            harness.manager().deploy("fixture")

        self.assertEqual(harness.layout.current.resolve(), current)
        self.assertEqual(harness.layout.previous.resolve(), previous)
        self.assertTrue(any("prune failed" in note for note in raised.exception.__notes__))
        prune.assert_called_once()
        self.assertEqual(prune.call_args.args[0].app_root, harness.layout.app_root)
        self.assert_private_data_untouched(harness)

    def test_deploy_reuses_main_release_retained_as_previous(self):
        harness = DeploymentHarness(self.root, kind="static")
        main = harness.seed_release("a" * 40, previous=True)
        current = harness.seed_release("b" * 40, current=True)
        harness.builder.commit = "a" * 40

        result = harness.manager().deploy("fixture")

        self.assertEqual(result.outcome, "deployed")
        self.assertEqual(harness.layout.current.resolve(), main)
        self.assertEqual(harness.layout.previous.resolve(), current)
        self.assertEqual(harness.builder.calls, [])
        self.assertEqual(harness.health.calls, ["http://fixture.test/fixture/"])
        self.assert_private_data_untouched(harness)

    def test_retry_reuses_release_installed_before_pointer_switch(self):
        harness = DeploymentHarness(self.root, kind="static")
        harness.builder.commit = "b" * 40

        with patch(
            "local_web_server.deploy.atomic_symlink",
            side_effect=OSError("interrupted before pointer switch"),
        ):
            with self.assertRaisesRegex(OSError, "interrupted"):
                harness.manager().deploy("fixture")

        installed = harness.layout.releases / ("b" * 40)
        self.assertTrue(installed.is_dir())
        self.assertFalse(harness.layout.current.exists())

        result = harness.manager().deploy("fixture")

        self.assertEqual(result.outcome, "deployed")
        self.assertEqual(harness.layout.current.resolve(), installed)
        self.assertFalse(harness.layout.previous.exists())
        self.assertEqual(harness.builder.calls, ["b" * 40])
        self.assert_private_data_untouched(harness)


if __name__ == "__main__":
    unittest.main()
