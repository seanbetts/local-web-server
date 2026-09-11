import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from local_web_server.app_doctor import DoctorReport
from local_web_server.app_registration import AppRegistrar, AppRegistrationError
from local_web_server.deploy import (
    DeploymentManager,
    DeploymentResult,
    ServiceCommandRecoveryFailed,
)
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore, HostProfileStoreError
from local_web_server.runtime import RuntimeLayout, read_release_commit
from local_web_server.service_command_migration import (
    ServiceCommandMigrationError,
    ServiceCommandMigrator,
)
from tests.helpers import FakeHealthChecker, FakeServiceController, init_git_repo


PRIVATE_COMMAND = "PRIVATE_ARGV_VALUE"
PRIVATE_FAILURE = "PRIVATE_EXCEPTION_TEXT"


class FixedDoctor:
    def inspect(self, _repository: Path) -> DoctorReport:
        return DoctorReport("fixture-service", True, ())


class RecordingRegistrar(AppRegistrar):
    def __init__(self, *, fail: str | None = None):
        super().__init__(doctor=FixedDoctor())
        self.fail = fail
        self.plan_calls = 0
        self.publish_calls = 0
        self.restore_calls = 0

    def plan_service_command_migration(self, repository, registry_path):
        self.plan_calls += 1
        plan = super().plan_service_command_migration(repository, registry_path)
        if self.fail == "changed-port":
            return replace(plan, port=plan.port + 1)
        if self.fail == "changed-plan" and self.plan_calls == 2:
            return replace(plan, _candidate=plan._candidate + b" ")
        return plan

    def publish_service_command_migration(self, plan):
        self.publish_calls += 1
        raise AssertionError("the registrar must not publish host-profile bytes")

    def restore_service_command_migration(self, plan):
        self.restore_calls += 1
        raise AssertionError("the registrar must not restore host-profile bytes")


class RecordingProfileStore:
    def __init__(self, real: HostProfileStore, *, fail: str | None = None):
        self.real = real
        self.fail = fail
        self.clean_calls: list[bool] = []
        self.migration_calls = 0
        self.restoration_calls = 0

    def require_clean(self, *, require_main: bool = True):
        self.clean_calls.append(require_main)
        return self.real.require_clean(require_main=require_main)

    def read_current(self):
        return self.real.read_current()

    def publish_service_command_migration(self, app_id, before, after):
        self.migration_calls += 1
        if self.fail == "migration-publication-before":
            raise HostProfileStoreError(PRIVATE_FAILURE)
        revision = self.real.publish_service_command_migration(app_id, before, after)
        if self.fail == "migration-publication-after":
            raise HostProfileStoreError(PRIVATE_FAILURE)
        if self.fail == "post-publication-profile-race":
            payload = json.loads(self.real.paths.profile.read_text(encoding="utf-8"))
            payload["apps"][0]["autoDeploy"] = False
            self.real.paths.profile.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
        if self.fail == "post-publication-exact-before-race":
            self.real.paths.profile.write_bytes(before)
        return revision

    def publish_service_command_restoration(self, app_id, before, after):
        self.restoration_calls += 1
        if self.fail == "restoration-publication-before":
            raise HostProfileStoreError(PRIVATE_FAILURE)
        revision = self.real.publish_service_command_restoration(
            app_id, before, after
        )
        if self.fail == "restoration-publication-after":
            raise HostProfileStoreError(PRIVATE_FAILURE)
        return revision


class RecordingInstaller:
    def __init__(self, registry_path: Path, *, fail: str | None = None):
        self.registry_path = registry_path
        self.fail = fail
        self.calls: list[str] = []

    def __call__(self, _repository: Path, *, dry_run: bool):
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        command = payload["apps"][0]["startCommand"]
        state = "candidate" if PRIVATE_COMMAND in command else "former"
        self.calls.append(state)
        if dry_run:
            raise AssertionError("migration installation must not be a dry run")
        if self.fail == "install" and state == "candidate":
            raise RuntimeError(PRIVATE_FAILURE)
        if self.fail == "old-install" and state == "former":
            raise RuntimeError(PRIVATE_FAILURE)
        if (
            self.fail == "redundant-old-install"
            and state == "former"
            and self.calls.count("former") > 1
        ):
            raise RuntimeError(PRIVATE_FAILURE)
        if self.fail == "post-install-registry-race" and state == "candidate":
            payload["apps"][0]["autoDeploy"] = False
            self.registry_path.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
        if self.fail == "post-old-install-registry-race" and state == "former":
            payload["apps"][0]["autoDeploy"] = False
            self.registry_path.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
        return object()


class RecordingVerifier:
    def __init__(
        self,
        *,
        fail: str | None = None,
        registry_path: Path | None = None,
    ):
        self.fail = fail
        self.registry_path = registry_path
        self.calls: list[str] = []

    def __call__(self, _repository: Path, registry, app_id: str):
        command = next(app.start_command.argv for app in registry.apps if app.id == app_id)
        state = "candidate" if PRIVATE_COMMAND in command else "former"
        self.calls.append(state)
        if self.fail == "tile" and state == "candidate":
            raise RuntimeError(PRIVATE_FAILURE)
        if self.fail == "old-tile" and state == "former":
            raise RuntimeError(PRIVATE_FAILURE)
        if self.fail == f"post-{state}-tile-registry-race":
            if self.registry_path is None:
                raise AssertionError("registry path is required for a race fixture")
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
            payload["apps"][0]["autoDeploy"] = False
            self.registry_path.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )


class TransitionDeployer:
    def __init__(
        self,
        *,
        runtime: Path,
        target_commit: str,
        former_current: str,
        former_previous: str | None,
        fail: str | tuple[str, ...] | None = None,
        before_final=None,
    ):
        self.runtime = runtime
        self.target_commit = target_commit
        self.former_current = former_current
        self.former_previous = former_previous
        self.fail = fail
        self.before_final = before_final
        self.calls = 0
        self.transition = None
        self.registry = None

    def _fails(self, stage: str) -> bool:
        return stage == self.fail or (
            isinstance(self.fail, tuple) and stage in self.fail
        )

    def _select(self, name: str, commit: str | None):
        pointer = self.runtime / "apps/fixture-service" / name
        pointer.unlink(missing_ok=True)
        if commit is not None:
            os.symlink(f"releases/{commit}", pointer)

    def _recover(self, transition, primary: Exception, *, replacement_began: bool):
        cleanup_failed = False
        self._select("current", self.former_current)
        self._select("previous", self.former_previous)
        try:
            transition.restore()
        except Exception:
            cleanup_failed = True
        if replacement_began:
            if self._fails("old-replacement"):
                cleanup_failed = True
            elif self._fails("old-health"):
                cleanup_failed = True
            else:
                try:
                    transition.verify_restored()
                except Exception:
                    cleanup_failed = True
        if cleanup_failed:
            raise ServiceCommandRecoveryFailed(PRIVATE_FAILURE) from primary
        raise primary

    def deploy(self, app_id, *, service_command_transition=None):
        self.calls += 1
        self.transition = service_command_transition
        if app_id != "fixture-service" or service_command_transition is None:
            raise AssertionError("migration must use the command-transition deployment path")
        if service_command_transition._target_commit != self.target_commit:
            raise AssertionError("migration must pin the reviewed application target")
        layout = RuntimeLayout(self.runtime, "fixture-service")
        actual_pointers = (
            read_release_commit(layout.current),
            read_release_commit(layout.previous),
        )
        if actual_pointers != service_command_transition._expected_pointers:
            raise ValueError("service command transition pointer state changed")
        if self._fails("pointer-race-before-deploy"):
            (
                self.runtime
                / "apps/fixture-service/releases"
                / self.target_commit
            ).mkdir(parents=True, exist_ok=True)
            self._select("current", self.target_commit)
            raise RuntimeError(PRIVATE_FAILURE)
        if self._fails("build"):
            raise RuntimeError(PRIVATE_FAILURE)
        release = self.runtime / "apps/fixture-service/releases" / self.target_commit
        release.mkdir(parents=True, exist_ok=True)
        self._select("current", self.target_commit)
        replacement_began = False
        try:
            service_command_transition.install()
            replacement_began = True
            if self._fails("replacement"):
                raise RuntimeError(PRIVATE_FAILURE)
            if self._fails("health"):
                raise RuntimeError(PRIVATE_FAILURE)
            service_command_transition.verify()
            self._select("previous", self.former_current)
            if self.before_final is not None:
                self.before_final()
            service_command_transition.validate_final()
        except Exception as error:
            self._recover(
                service_command_transition,
                error,
                replacement_began=replacement_began,
            )
        return DeploymentResult(
            app_id,
            self.target_commit,
            "deployed",
            PRIVATE_COMMAND,
            Path("/private/deployment.log"),
        )


class RecordingReleaseBuilder:
    def __init__(self):
        self.calls: list[str] = []

    def build(self, host, manifest, commit, layout, environment, log):
        self.calls.append(commit)
        release = layout.releases / commit
        release.mkdir(parents=True)
        return release


class FailingReleaseBuilder(RecordingReleaseBuilder):
    def build(self, host, manifest, commit, layout, environment, log):
        self.calls.append(commit)
        raise RuntimeError(PRIVATE_FAILURE)


class FailingReplaceServiceController(FakeServiceController):
    def replace(self, label: str, plist_path: Path) -> None:
        super().replace(label, plist_path)
        raise RuntimeError(PRIVATE_FAILURE)


class RealTask3Deployer:
    def __init__(
        self,
        *,
        root: Path,
        builder=None,
        services=None,
        health=None,
    ):
        self.root = root
        self.builder = builder or RecordingReleaseBuilder()
        self.services = services or FakeServiceController()
        self.health = health or FakeHealthChecker()
        self.registry = None
        self.calls = 0

    def deploy(self, app_id, **kwargs):
        self.calls += 1
        manager = DeploymentManager(
            self.registry,
            builder=self.builder,
            services=self.services,
            health=self.health,
            launch_agents=self.root / "LaunchAgents",
            environment={"PATH": "/usr/bin:/bin"},
        )
        return manager.deploy(app_id, **kwargs)


class AdvancingMainDeployer:
    def __init__(self, application: Path, launch_agents: Path, builder):
        self.application = application
        self.launch_agents = launch_agents
        self.builder = builder
        self.registry = None
        self.calls = 0
        self.transition = None
        self.advanced_commit = None

    def deploy(self, app_id, *, service_command_transition=None):
        self.calls += 1
        self.transition = service_command_transition
        marker = self.application / "advanced-release-private-marker.txt"
        marker.write_text("PRIVATE_ADVANCED_RELEASE_VALUE", encoding="utf-8")
        subprocess.run(
            ("git", "add", marker.name),
            cwd=self.application,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ("git", "commit", "-m", "advance application main"),
            cwd=self.application,
            check=True,
            capture_output=True,
            text=True,
        )
        self.advanced_commit = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=self.application,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        manager = DeploymentManager(
            self.registry,
            builder=self.builder,
            services=FakeServiceController(),
            health=FakeHealthChecker(),
            launch_agents=self.launch_agents,
            environment={"PATH": "/usr/bin:/bin"},
        )
        return manager.deploy(
            app_id,
            service_command_transition=service_command_transition,
        )


def snapshot_tree(root: Path) -> tuple[tuple[str, str, bytes | str], ...]:
    snapshot = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts:
            continue
        if path.is_symlink():
            snapshot.append((str(relative), "link", os.readlink(path)))
        elif path.is_file():
            snapshot.append((str(relative), "file", path.read_bytes()))
        elif path.is_dir():
            snapshot.append((str(relative), "dir", b""))
    return tuple(snapshot)


class ServiceCommandMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.platform = self.root / "local-web-server"
        self.application = self.root / "fixture-service"
        self.runtime = self.root / "runtime"
        self.application.mkdir()
        self._write_manifest(command_variant="former")
        self._write_provenance()
        self.former_commit = init_git_repo(self.application, {})
        self._write_manifest(command_variant="candidate")
        self._git(self.application, "add", "local-web.json")
        self._git(self.application, "commit", "-m", "change command")
        self.target_commit = self._git(
            self.application, "rev-parse", "HEAD"
        ).stdout.strip()

        release = (
            self.runtime
            / "apps/fixture-service/releases"
            / self.former_commit
        )
        release.mkdir(parents=True)
        os.symlink(
            f"releases/{self.former_commit}",
            self.runtime / "apps/fixture-service/current",
        )
        self.registry_payload = {
            "schemaVersion": 1,
            "host": "fixture.local",
            "runtimeRoot": str(self.runtime),
            "apps": [
                {
                    "id": "fixture-service",
                    "repository": str(self.application.resolve()),
                    "autoDeploy": True,
                    "environment": {"VITE_PUBLIC_BASE_PATH": "/fixture/"},
                    "port": 52700,
                    "startCommand": [
                        "/usr/bin/env",
                        "node",
                        "former-service.mjs",
                        "--port",
                        "52700",
                    ],
                }
            ],
        }
        self.registry_bytes = self._registry_bytes()
        self.platform.mkdir()
        init_git_repo(
            self.platform,
            {
                ".gitignore": "config/local/\n",
                "config/apps.example.json": self.registry_bytes.decode("utf-8"),
                "unrelated.txt": "fixture\n",
            },
        )
        self.profile_paths = HostProfilePaths.for_repository(self.platform)
        self.profile_store = HostProfileStore(self.profile_paths)
        self.profile_store.initialise(self.registry_bytes)
        self.registry = self.profile_paths.profile
        self.platform_base = self._git(
            self.platform, "rev-parse", "HEAD"
        ).stdout.strip()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _git(repository: Path, *arguments: str):
        return subprocess.run(
            ("git", *arguments),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )

    def _write_manifest(
        self,
        *,
        command_variant: str,
        app_id: str = "fixture-service",
        kind: str = "service",
        route: str = "/fixture",
    ):
        manifest = {
            "schemaVersion": 1,
            "id": app_id,
            "title": "Fixture Service",
            "route": route,
            "kind": kind,
            "build": {
                "commands": [["true"]],
                "output": "dist",
                "environment": ["VITE_PUBLIC_BASE_PATH"],
            },
            "healthPath": f"{route}/healthz" if kind == "service" else f"{route}/",
            "home": {"icon": "book", "accent": "#8EA7C6"},
            "platform": {
                "contractVersion": 1,
                "templateVersion": 1,
                "uiVersion": "0.4.0",
                "capabilities": [],
            },
        }
        if kind == "service":
            command = (
                ["/usr/bin/env", "node", "former-service.mjs", "--port", "{port}"]
                if command_variant == "former"
                else [
                    "/usr/bin/env",
                    "node",
                    "{release}/service.mjs",
                    "--port",
                    "{port}",
                    "--repository-data",
                    "{repository}/data",
                    PRIVATE_COMMAND,
                ]
            )
            manifest["service"] = {
                "module": "fixture_service",
                "internalHealthPath": "/healthz",
                "startCommand": command,
            }
        (self.application / "local-web.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def _write_provenance(self):
        (self.application / ".local-web-platform.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "templateVersion": 1,
                    "platformContractVersion": 1,
                    "ui": {"version": "0.4.0", "sha256": "a" * 64},
                    "capabilities": [],
                    "domainPaletteTokens": [],
                    "managedFiles": [],
                }
            ),
            encoding="utf-8",
        )

    def _registry_bytes(self):
        return (json.dumps(self.registry_payload, indent=2) + "\n").encode("utf-8")

    def _publish_fixture_profile(self):
        candidate = self._registry_bytes()
        before = self.profile_store.read_current()
        if candidate != before:
            self.profile_store.publish_registration(
                "fixture-service", before, candidate
            )
        self.registry_bytes = candidate

    def _reset_profile_store(self):
        shutil.rmtree(self.profile_paths.local)
        self.profile_paths = HostProfilePaths.for_repository(self.platform)
        self.profile_store = HostProfileStore(self.profile_paths)
        self.profile_store.initialise(self.registry_bytes)
        self.registry = self.profile_paths.profile

    def _commit_deployed_manifest(
        self, *, app_id="fixture-service", kind="service", route="/fixture"
    ):
        self._write_manifest(
            command_variant="former", app_id=app_id, kind=kind, route=route
        )
        self._git(self.application, "add", "local-web.json")
        self._git(self.application, "commit", "-m", "invalid deployed manifest")
        deployed = self._git(self.application, "rev-parse", "HEAD").stdout.strip()
        (self.runtime / "apps/fixture-service/releases" / deployed).mkdir()
        current = self.runtime / "apps/fixture-service/current"
        current.unlink()
        os.symlink(f"releases/{deployed}", current)
        self._write_manifest(command_variant="candidate")
        self._git(self.application, "add", "local-web.json")
        self._git(self.application, "commit", "-m", "restore target manifest")

    def migrator(
        self,
        *,
        registrar=None,
        profile_store=None,
        install=None,
        deployer=None,
        verifier=None,
    ):
        registrar = registrar or RecordingRegistrar()
        profile_store = profile_store or RecordingProfileStore(self.profile_store)
        install = install or RecordingInstaller(self.registry)
        verifier = verifier or RecordingVerifier()
        deployer = deployer or TransitionDeployer(
            runtime=self.runtime,
            target_commit=self.target_commit,
            former_current=self.former_commit,
            former_previous=None,
        )
        migrator = ServiceCommandMigrator(
            platform_repository=self.platform,
            registry_path=self.registry,
            registrar=registrar,
            profile_store=profile_store,
            install=install,
            deployer_factory=lambda registry: self._capture_registry(deployer, registry),
            verify=verifier,
        )
        return migrator, registrar, profile_store, install, deployer, verifier

    @staticmethod
    def _capture_registry(deployer, registry):
        deployer.registry = registry
        return deployer

    def test_preview_is_bounded_read_only_and_feature_branch_eligible(self):
        self._git(self.platform, "switch", "-c", "feature/preview")
        migrator, registrar, profile_store, install, deployer, verifier = self.migrator()
        before = snapshot_tree(self.root)

        plan = migrator.preview(self.application)

        self.assertEqual(
            (
                plan.app_id,
                plan.route,
                plan.port,
                plan.command_status,
                plan.identity_status,
                plan.repository_status,
                plan.route_status,
                plan.port_status,
                plan.service_action,
            ),
            (
                "fixture-service",
                "/fixture",
                52700,
                "change",
                "unchanged",
                "unchanged",
                "unchanged",
                "unchanged",
                "redeploy and reload",
            ),
        )
        self.assertEqual(snapshot_tree(self.root), before)
        self.assertNotIn("startCommand", repr(plan))
        self.assertNotIn(str(self.application), repr(plan))
        self.assertEqual(registrar.plan_calls, 1)
        self.assertEqual(profile_store.clean_calls, [False])
        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])

    def test_feature_branch_preview_cannot_cross_the_apply_clean_main_gate(self):
        self._git(self.platform, "switch", "-c", "feature/apply")
        migrator, registrar, profile_store, install, deployer, verifier = self.migrator()
        before = snapshot_tree(self.root)

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration validation failed$",
        ):
            migrator.migrate(self.application)

        self.assertEqual(snapshot_tree(self.root), before)
        self.assertEqual(registrar.publish_calls, 0)
        self.assertEqual(profile_store.migration_calls, 0)
        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])

    def test_preview_rejects_missing_and_malformed_runtime_pointers(self):
        current = self.runtime / "apps/fixture-service/current"
        cases = ("missing", "outside", "missing-commit")
        for case in cases:
            with self.subTest(case=case):
                current.unlink(missing_ok=True)
                if case == "outside":
                    os.symlink(str(self.root), current)
                elif case == "missing-commit":
                    missing = "f" * 40
                    (self.runtime / "apps/fixture-service/releases" / missing).mkdir()
                    os.symlink(f"releases/{missing}", current)
                migrator, *_ = self.migrator()
                with self.assertRaisesRegex(
                    ServiceCommandMigrationError,
                    "^service command migration validation failed$",
                ) as raised:
                    migrator.preview(self.application)
                self.assertNotIn(str(self.root), str(raised.exception))
                if case == "missing-commit":
                    (self.runtime / "apps/fixture-service/releases" / ("f" * 40)).rmdir()
                current.unlink(missing_ok=True)
                os.symlink(f"releases/{self.former_commit}", current)

    def test_preview_rejects_mismatched_deployed_manifests(self):
        cases = (
            ("other-service", "service", "/fixture"),
            ("fixture-service", "static", "/fixture"),
            ("fixture-service", "service", "/former-route"),
        )
        for app_id, kind, route in cases:
            with self.subTest(app_id=app_id, kind=kind, route=route):
                self._commit_deployed_manifest(app_id=app_id, kind=kind, route=route)
                migrator, *_ = self.migrator()
                with self.assertRaisesRegex(
                    ServiceCommandMigrationError,
                    "^service command migration validation failed$",
                ):
                    migrator.preview(self.application)

    def test_preview_rejects_changed_port_duplicate_match_and_dirty_registry(self):
        changed_port, *_ = self.migrator(registrar=RecordingRegistrar(fail="changed-port"))
        with self.assertRaisesRegex(ServiceCommandMigrationError, "validation failed"):
            changed_port.preview(self.application)

        duplicate = dict(self.registry_payload["apps"][0])
        duplicate["id"] = "fixture-alias"
        self.registry_payload["apps"].append(duplicate)
        self.registry.write_bytes(self._registry_bytes())
        duplicate_migrator, *_ = self.migrator()
        with self.assertRaisesRegex(ServiceCommandMigrationError, "validation failed"):
            duplicate_migrator.preview(self.application)

        self.registry_payload["apps"].pop()
        self.registry.write_bytes(self.registry_bytes + b"\n")
        dirty_migrator, *_ = self.migrator()
        with self.assertRaisesRegex(ServiceCommandMigrationError, "validation failed"):
            dirty_migrator.preview(self.application)

    def test_apply_replans_publishes_deploys_transition_and_returns_bounded_result(self):
        migrator, registrar, profile_store, install, deployer, verifier = self.migrator()

        result = migrator.migrate(self.application)

        self.assertTrue(result.verified)
        self.assertEqual(result.plan.port, 52700)
        self.assertRegex(result.registry_revision or "", r"^[0-9a-f]{64}$")
        self.assertEqual(registrar.plan_calls, 2)
        self.assertEqual(profile_store.clean_calls, [False, True, False])
        self.assertEqual(profile_store.migration_calls, 1)
        self.assertEqual(profile_store.restoration_calls, 0)
        self.assertIsNotNone(deployer.transition)
        self.assertEqual(result.plan._target_commit, self.target_commit)
        self.assertEqual(deployer.transition._target_commit, self.target_commit)
        self.assertEqual(install.calls, ["candidate"])
        self.assertEqual(verifier.calls, ["candidate"])
        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(read_release_commit(layout.current), self.target_commit)
        self.assertEqual(read_release_commit(layout.previous), self.former_commit)
        self.assertNotIn(str(self.application), repr(result))
        self.assertNotIn("deployment.log", repr(result))
        self.assertNotIn(PRIVATE_COMMAND, repr(result))
        self.assertNotIn(self.target_commit, repr(result.plan))
        self.assertNotIn(self.target_commit, repr(deployer.transition))
        self.assertEqual(
            [revision.operation for revision in self.profile_store.revisions()],
            ["initialisation", "service-command-migration"],
        )

    def test_main_advance_between_replan_and_deploy_cannot_build_a_mismatched_release(self):
        builder = RecordingReleaseBuilder()
        deployer = AdvancingMainDeployer(
            self.application,
            self.root / "LaunchAgents",
            builder,
        )
        migrator, _, _, install, _, verifier = self.migrator(deployer=deployer)

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration deployment failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(builder.calls, [])
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertFalse(
            (layout.releases / (deployer.advanced_commit or "missing")).exists()
        )
        self.assertEqual(install.calls, ["former"])
        self.assertEqual(verifier.calls, [])
        self.assertNotIn("PRIVATE_ADVANCED_RELEASE_VALUE", str(raised.exception))
        self.assertNotIn(str(self.application), str(raised.exception))
        self.assertNotIn(deployer.advanced_commit or "missing", repr(deployer.transition))

    def test_apply_rejects_replanned_candidate_change_before_publication(self):
        registrar = RecordingRegistrar(fail="changed-plan")
        migrator, _, profile_store, install, deployer, verifier = self.migrator(
            registrar=registrar
        )

        with self.assertRaisesRegex(ServiceCommandMigrationError, "validation failed"):
            migrator.migrate(self.application)

        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(profile_store.migration_calls, 0)
        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])

    def test_post_publication_profile_race_is_rejected_before_deployment(self):
        profile_store = RecordingProfileStore(
            self.profile_store, fail="post-publication-profile-race"
        )
        migrator, _, _, install, deployer, verifier = self.migrator(
            profile_store=profile_store
        )

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration failed; recovery failed$",
        ):
            migrator.migrate(self.application)

        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])

    def test_exact_prior_profile_race_cannot_bypass_restoration_revision(self):
        profile_store = RecordingProfileStore(
            self.profile_store, fail="post-publication-exact-before-race"
        )
        migrator, _, _, install, deployer, verifier = self.migrator(
            profile_store=profile_store
        )

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration failed; recovery failed$",
        ):
            migrator.migrate(self.application)

        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])

    def test_post_install_registry_race_cannot_pass_candidate_verification(self):
        install = RecordingInstaller(
            self.registry, fail="post-install-registry-race"
        )
        migrator, _, _, _, deployer, verifier = self.migrator(install=install)

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration failed; recovery failed$",
        ):
            migrator.migrate(self.application)

        self.assertEqual(deployer.calls, 1)
        self.assertEqual(verifier.calls, [])

    def test_candidate_tile_verification_cannot_mutate_registry_and_pass(self):
        verifier = RecordingVerifier(
            fail="post-candidate-tile-registry-race",
            registry_path=self.registry,
        )
        migrator, _, _, install, deployer, _ = self.migrator(verifier=verifier)

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration failed; recovery failed$",
        ):
            migrator.migrate(self.application)

        self.assertEqual(install.calls, ["candidate"])
        self.assertEqual(deployer.calls, 1)
        self.assertEqual(verifier.calls, ["candidate"])

    def test_pointer_race_before_deploy_cannot_be_reported_as_recovered(self):
        deployer = TransitionDeployer(
            runtime=self.runtime,
            target_commit=self.target_commit,
            former_current=self.former_commit,
            former_previous=None,
            fail="pointer-race-before-deploy",
        )
        migrator, *_ = self.migrator(deployer=deployer)

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration failed; recovery failed$",
        ):
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(read_release_commit(layout.current), self.target_commit)

    def test_late_exact_prior_profile_race_is_a_bounded_recovery_failure(self):
        deployer = TransitionDeployer(
            runtime=self.runtime,
            target_commit=self.target_commit,
            former_current=self.former_commit,
            former_previous=None,
            before_final=lambda: self.registry.write_bytes(self.registry_bytes),
        )
        migrator, _, _, install, _, verifier = self.migrator(deployer=deployer)

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration failed; recovery failed$",
        ):
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertEqual(install.calls, ["candidate"])
        self.assertEqual(verifier.calls, ["candidate"])
        with self.assertRaises(HostProfileStoreError):
            self.profile_store.read_current()

    def test_real_task3_build_failure_restores_and_proves_former_once(self):
        builder = FailingReleaseBuilder()
        services = FakeServiceController()
        health = FakeHealthChecker()
        deployer = RealTask3Deployer(
            root=self.root,
            builder=builder,
            services=services,
            health=health,
        )
        install = RecordingInstaller(self.registry)
        verifier = RecordingVerifier()
        migrator, registrar, profile_store, *_ = self.migrator(
            install=install,
            deployer=deployer,
            verifier=verifier,
        )

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration deployment failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(builder.calls, [self.target_commit])
        self.assertEqual(registrar.restore_calls, 0)
        self.assertEqual(profile_store.restoration_calls, 1)
        self.assertEqual(install.calls, ["former"])
        self.assertEqual([call[0] for call in services.calls], ["replace", "state"])
        self.assertEqual(
            health.calls,
            [
                "http://fixture.local/fixture/",
                "http://127.0.0.1:52700/healthz",
                "http://fixture.local/fixture/healthz",
            ],
        )
        self.assertEqual(verifier.calls, ["former"])
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertFalse((layout.releases / self.target_commit).exists())
        self.assertNotIn(PRIVATE_FAILURE, str(raised.exception))

    def test_real_task3_candidate_install_failure_restores_and_proves_former_once(self):
        builder = RecordingReleaseBuilder()
        services = FakeServiceController()
        health = FakeHealthChecker()
        deployer = RealTask3Deployer(
            root=self.root,
            builder=builder,
            services=services,
            health=health,
        )
        install = RecordingInstaller(self.registry, fail="install")
        verifier = RecordingVerifier()
        migrator, registrar, profile_store, *_ = self.migrator(
            install=install,
            deployer=deployer,
            verifier=verifier,
        )

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration deployment failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(builder.calls, [self.target_commit])
        self.assertEqual(registrar.restore_calls, 0)
        self.assertEqual(profile_store.restoration_calls, 1)
        self.assertEqual(install.calls, ["candidate", "former"])
        self.assertEqual([call[0] for call in services.calls], ["replace", "state"])
        self.assertEqual(len(health.calls), 3)
        self.assertEqual(verifier.calls, ["former"])
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertTrue((layout.releases / self.target_commit).is_dir())
        self.assertNotIn(PRIVATE_FAILURE, str(raised.exception))

    def test_real_task3_proven_recovery_does_not_run_a_failing_second_install(self):
        deployer = RealTask3Deployer(
            root=self.root,
            builder=FailingReleaseBuilder(),
        )
        install = RecordingInstaller(
            self.registry,
            fail="redundant-old-install",
        )
        verifier = RecordingVerifier()
        migrator, registrar, profile_store, *_ = self.migrator(
            install=install,
            deployer=deployer,
            verifier=verifier,
        )

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration deployment failed$",
        ) as raised:
            migrator.migrate(self.application)

        self.assertEqual(registrar.restore_calls, 0)
        self.assertEqual(profile_store.restoration_calls, 1)
        self.assertEqual(install.calls, ["former"])
        self.assertEqual(verifier.calls, ["former"])
        self.assertNotIn(PRIVATE_FAILURE, str(raised.exception))

    def test_real_task3_recovery_failure_never_runs_the_weaker_outer_restore(self):
        services = FailingReplaceServiceController()
        health = FakeHealthChecker()
        deployer = RealTask3Deployer(
            root=self.root,
            services=services,
            health=health,
        )
        install = RecordingInstaller(self.registry, fail="install")
        verifier = RecordingVerifier()
        migrator, registrar, profile_store, *_ = self.migrator(
            install=install,
            deployer=deployer,
            verifier=verifier,
        )

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration failed; recovery failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(registrar.restore_calls, 0)
        self.assertEqual(profile_store.restoration_calls, 1)
        self.assertEqual(install.calls, ["candidate", "former"])
        self.assertEqual([call[0] for call in services.calls], ["replace"])
        self.assertEqual(health.calls, [])
        self.assertEqual(verifier.calls, [])
        self.assertTrue((layout.releases / self.target_commit).is_dir())
        self.assertIsInstance(raised.exception.__cause__, ServiceCommandRecoveryFailed)
        self.assertNotIn(PRIVATE_FAILURE, str(raised.exception))

    def test_current_command_is_an_exact_no_op(self):
        migrator, *_ = self.migrator()
        migrator.migrate(self.application)
        second, registrar, profile_store, install, deployer, verifier = self.migrator()
        before = snapshot_tree(self.root)

        result = second.migrate(self.application)

        self.assertEqual(snapshot_tree(self.root), before)
        self.assertEqual(result.plan.command_status, "current")
        self.assertEqual(result.plan.service_action, "none")
        self.assertIsNone(result.registry_revision)
        self.assertIsNone(result.deployment)
        self.assertFalse(result.verified)
        self.assertEqual(registrar.plan_calls, 1)
        self.assertEqual(profile_store.clean_calls, [False])
        self.assertEqual(profile_store.migration_calls, 0)
        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])

    def test_profile_publication_failures_restore_exact_bytes_without_runtime_calls(self):
        for publication_failure in (
            "migration-publication-before",
            "migration-publication-after",
        ):
            with self.subTest(profile_store=publication_failure):
                registrar = RecordingRegistrar()
                profile_store = RecordingProfileStore(
                    self.profile_store, fail=publication_failure
                )
                migrator, _, _, install, deployer, verifier = self.migrator(
                    registrar=registrar, profile_store=profile_store
                )
                with self.assertRaisesRegex(
                    ServiceCommandMigrationError,
                    "^service command migration registry update failed$",
                ) as raised:
                    migrator.migrate(self.application)
                self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
                self.assertEqual(install.calls, [])
                self.assertEqual(deployer.calls, 0)
                self.assertEqual(verifier.calls, [])
                self.assertNotIn(PRIVATE_FAILURE, str(raised.exception))
                self.assertNotIn(str(self.root), repr(raised.exception))
                expected_operations = ["initialisation"]
                if publication_failure == "migration-publication-after":
                    expected_operations.extend(
                        [
                            "service-command-migration",
                            "service-command-restoration",
                        ]
                    )
                self.assertEqual(
                    [
                        revision.operation
                        for revision in self.profile_store.revisions()
                    ],
                    expected_operations,
                )
                self._reset_profile_store()

    def test_post_publication_failures_restore_old_registry_and_pointer_pair(self):
        for failure, message in (
            ("build", "deployment failed"),
            ("install", "deployment failed"),
            ("replacement", "deployment failed"),
            ("health", "deployment failed"),
            ("tile", "verification failed"),
        ):
            with self.subTest(failure=failure):
                install = RecordingInstaller(self.registry, fail=failure)
                verifier = RecordingVerifier(fail=failure)
                deployer = TransitionDeployer(
                    runtime=self.runtime,
                    target_commit=self.target_commit,
                    former_current=self.former_commit,
                    former_previous=None,
                    fail=failure,
                )
                migrator, registrar, profile_store, *_ = self.migrator(
                    install=install, deployer=deployer, verifier=verifier
                )
                with self.assertRaisesRegex(
                    ServiceCommandMigrationError,
                    f"^service command migration {message}$",
                ) as raised:
                    migrator.migrate(self.application)
                self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
                layout = RuntimeLayout(self.runtime, "fixture-service")
                self.assertEqual(read_release_commit(layout.current), self.former_commit)
                self.assertIsNone(read_release_commit(layout.previous))
                self.assertEqual(registrar.restore_calls, 0)
                self.assertGreaterEqual(profile_store.restoration_calls, 1)
                self.assertNotIn(PRIVATE_FAILURE, str(raised.exception))
                self.assertNotIn(PRIVATE_COMMAND, repr(raised.exception))
                self._reset_profile_store()

    def test_each_recovery_boundary_has_one_distinct_public_failure(self):
        cases = (
            ("restoration-publication-before", "build"),
            ("old-install", "build"),
            ("old-replacement", "health"),
            ("old-health", "health"),
            ("old-tile", "health"),
        )
        for recovery_failure, primary_failure in cases:
            with self.subTest(recovery_failure=recovery_failure):
                registrar = RecordingRegistrar()
                profile_store = RecordingProfileStore(
                    self.profile_store,
                    fail=(
                        "restoration-publication-before"
                        if recovery_failure == "restoration-publication-before"
                        else None
                    ),
                )
                install = RecordingInstaller(
                    self.registry,
                    fail="old-install" if recovery_failure == "old-install" else None,
                )
                verifier = RecordingVerifier(
                    fail="old-tile" if recovery_failure == "old-tile" else None
                )
                deployer = TransitionDeployer(
                    runtime=self.runtime,
                    target_commit=self.target_commit,
                    former_current=self.former_commit,
                    former_previous=None,
                    fail=(primary_failure, recovery_failure),
                )
                migrator, *_ = self.migrator(
                    registrar=registrar,
                    profile_store=profile_store,
                    install=install,
                    deployer=deployer,
                    verifier=verifier,
                )
                with self.assertRaisesRegex(
                    ServiceCommandMigrationError,
                    "^service command migration failed; recovery failed$",
                ) as raised:
                    migrator.migrate(self.application)
                self.assertNotIn(PRIVATE_FAILURE, str(raised.exception))
                self.assertNotIn(PRIVATE_COMMAND, repr(raised.exception))
                self._reset_profile_store()
                current = self.runtime / "apps/fixture-service/current"
                current.unlink(missing_ok=True)
                os.symlink(f"releases/{self.former_commit}", current)
                (self.runtime / "apps/fixture-service/previous").unlink(missing_ok=True)

    def test_registry_mutation_during_old_install_is_a_recovery_failure(self):
        install = RecordingInstaller(
            self.registry,
            fail="post-old-install-registry-race",
        )
        deployer = TransitionDeployer(
            runtime=self.runtime,
            target_commit=self.target_commit,
            former_current=self.former_commit,
            former_previous=None,
            fail="build",
        )
        migrator, *_ = self.migrator(install=install, deployer=deployer)

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration failed; recovery failed$",
        ):
            migrator.migrate(self.application)

        self.assertEqual(install.calls, ["former"])

    def test_registry_mutation_during_restored_tile_check_is_classified_inside_recovery(self):
        verifier = RecordingVerifier(
            fail="post-former-tile-registry-race",
            registry_path=self.registry,
        )
        deployer = TransitionDeployer(
            runtime=self.runtime,
            target_commit=self.target_commit,
            former_current=self.former_commit,
            former_previous=None,
            fail="health",
        )
        migrator, *_ = self.migrator(deployer=deployer, verifier=verifier)

        with self.assertRaisesRegex(
            ServiceCommandMigrationError,
            "^service command migration failed; recovery failed$",
        ) as raised:
            migrator.migrate(self.application)

        self.assertIsInstance(raised.exception.__cause__, ServiceCommandRecoveryFailed)
        self.assertEqual(verifier.calls, ["former"])


if __name__ == "__main__":
    unittest.main()
