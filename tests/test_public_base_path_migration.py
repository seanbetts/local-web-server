import json
import os
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from local_web_server.app_doctor import DoctorReport
from local_web_server.app_registration import AppRegistrar, AppRegistrationError
from local_web_server.deploy import (
    DeploymentManager,
    DeploymentResult,
    RegisteredServiceRecoveryFailed,
)
from local_web_server.public_base_path_migration import (
    PublicBasePathMigrationError,
    PublicBasePathMigrator,
)
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore, HostProfileStoreError
from local_web_server.runtime import (
    AppLock,
    DeploymentBusy,
    RuntimeLayout,
    prune_releases as prune_runtime_releases,
    read_release_commit,
)
from local_web_server.services import HealthResult, ServiceState
from tests.helpers import FakeHealthChecker, FakeServiceController, init_git_repo


PRIVATE_VALUE = "/fixture/"
PRIVATE_FAILURE = "PRIVATE_EXCEPTION_TEXT"
EXTERNAL_COMMIT = "e" * 40
UNRELATED_COMMIT = "f" * 40


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

    def plan_public_base_path_migration(self, repository, registry_path):
        self.plan_calls += 1
        if self.fail == "public-error":
            raise PublicBasePathMigrationError("PRIVATE_COLLABORATOR_FAILURE")
        plan = super().plan_public_base_path_migration(repository, registry_path)
        if self.fail == "changed-port":
            return replace(plan, port=plan.port + 1)
        if self.fail == "changed-command":
            candidate = json.loads(plan._candidate)
            candidate["apps"][0]["startCommand"][-1] = "PRIVATE_CHANGED_COMMAND"
            return replace(
                plan,
                _candidate=(json.dumps(candidate, indent=2) + "\n").encode("utf-8"),
            )
        if self.fail == "changed-plan" and self.plan_calls == 2:
            return replace(plan, _candidate=plan._candidate + b" ")
        return plan

    def publish_public_base_path_migration(self, plan):
        self.publish_calls += 1
        raise AssertionError("the registrar must not publish host-profile bytes")

    def restore_public_base_path_migration(self, plan):
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

    def publish_public_base_path_migration(self, app_id, before, after):
        self.migration_calls += 1
        if self.fail == "migration-publication-before":
            raise HostProfileStoreError(PRIVATE_FAILURE)
        revision = self.real.publish_public_base_path_migration(app_id, before, after)
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

    def publish_public_base_path_restoration(self, app_id, before, after):
        self.restoration_calls += 1
        if self.fail == "restoration-publication-before":
            raise HostProfileStoreError(PRIVATE_FAILURE)
        revision = self.real.publish_public_base_path_restoration(
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
        self.installed_state = "former"

    def __call__(self, _repository: Path, *, dry_run: bool):
        payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
        environment = payload["apps"][0].get("environment", {})
        state = (
            "candidate"
            if environment.get("VITE_PUBLIC_BASE_PATH") == PRIVATE_VALUE
            else "former"
        )
        self.calls.append(state)
        if dry_run:
            raise AssertionError("migration installation must not be a dry run")
        self.installed_state = state
        if self.fail == "install" and state == "candidate":
            raise RuntimeError(PRIVATE_FAILURE)
        if self.fail == "old-install" and state == "former":
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
        environment = next(
            app.environment for app in registry.apps if app.id == app_id
        )
        state = (
            "candidate"
            if dict(environment).get("VITE_PUBLIC_BASE_PATH") == PRIVATE_VALUE
            else "former"
        )
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
        self.service_state = "former"
        self.replacements: list[str] = []
        self.health_evidence: list[str] = []

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
        if self._fails("pointer-race-after-switch"):
            (self.runtime / "apps/fixture-service/releases" / EXTERNAL_COMMIT).mkdir(
                parents=True, exist_ok=True
            )
            self._select("current", EXTERNAL_COMMIT)
            cleanup_failed = True
        else:
            self._select("current", self.former_current)
            self._select("previous", self.former_previous)
        try:
            transition.restore()
        except Exception:
            cleanup_failed = True
        if replacement_began:
            if self._fails("old-replacement"):
                cleanup_failed = True
            else:
                self.replacements.append("former")
                self.service_state = "former"
                if self._fails("old-health"):
                    cleanup_failed = True
                else:
                    self.health_evidence.append("former")
                    if self._fails("unrelated-release-before-cleanup"):
                        (
                            self.runtime
                            / "apps/fixture-service/releases"
                            / UNRELATED_COMMIT
                        ).mkdir(parents=True, exist_ok=True)
                    try:
                        transition.verify_restored()
                    except Exception:
                        cleanup_failed = True
        if cleanup_failed:
            raise RegisteredServiceRecoveryFailed(PRIVATE_FAILURE) from primary
        raise primary

    def deploy(
        self,
        app_id,
        *,
        service_command_transition=None,
        expected_commit=None,
        expected_current_commit=None,
        validate_locked=None,
    ):
        self.calls += 1
        self.transition = service_command_transition
        if app_id != "fixture-service" or service_command_transition is None:
            raise AssertionError(
                "migration must use the registered-service transition deployment path"
            )
        if service_command_transition._target_commit != self.target_commit:
            raise AssertionError("migration must pin the reviewed application target")
        if expected_commit != self.target_commit:
            raise AssertionError("migration must pass the exact deployment target")
        if expected_current_commit != self.former_current:
            raise AssertionError("migration must pass the exact former current release")
        if validate_locked is None:
            raise AssertionError("migration must validate locked registry state")
        validate_locked()
        layout = RuntimeLayout(self.runtime, "fixture-service")
        actual_pointers = (
            read_release_commit(layout.current),
            read_release_commit(layout.previous),
        )
        if actual_pointers != service_command_transition._expected_pointers:
            raise ValueError("registered-service transition pointer state changed")
        if self._fails("pointer-race-before-deploy"):
            (layout.releases / self.target_commit).mkdir(parents=True, exist_ok=True)
            self._select("current", self.target_commit)
            raise RuntimeError(PRIVATE_FAILURE)
        if self._fails("build"):
            raise RuntimeError(PRIVATE_FAILURE)
        release = layout.releases / self.target_commit
        release.mkdir(parents=True, exist_ok=True)
        (release / "source-commit").write_text(
            self.target_commit, encoding="utf-8"
        )
        validate_locked()
        self._select("current", self.target_commit)
        replacement_began = False
        try:
            service_command_transition.install()
            replacement_began = True
            self.replacements.append("candidate")
            self.service_state = "candidate"
            if self._fails("replacement"):
                raise RuntimeError(PRIVATE_FAILURE)
            self.health_evidence.append("candidate")
            if self._fails("health") or self._fails("pointer-race-after-switch"):
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
            PRIVATE_FAILURE,
            Path("/private/deployment.log"),
        )


class RecordingReleaseBuilder:
    def __init__(self):
        self.calls: list[str] = []

    def build(self, host, manifest, commit, layout, environment, log):
        self.calls.append(commit)
        if environment.get("VITE_PUBLIC_BASE_PATH") != PRIVATE_VALUE:
            raise AssertionError("target release must use the candidate environment")
        release = layout.releases / commit
        release.mkdir(parents=True)
        (release / "source-commit").write_text(commit, encoding="utf-8")
        return release


class UnexpectedPathReleaseBuilder(RecordingReleaseBuilder):
    def build(self, host, manifest, commit, layout, environment, log):
        super().build(host, manifest, commit, layout, environment, log)
        return layout.releases / UNRELATED_COMMIT


class FailingReleaseBuilder(RecordingReleaseBuilder):
    def build(self, host, manifest, commit, layout, environment, log):
        self.calls.append(commit)
        raise RuntimeError(PRIVATE_FAILURE)


class StatefulServiceController:
    def __init__(self, *, fail_replace_calls: tuple[int, ...] = ()):
        self.fail_replace_calls = set(fail_replace_calls)
        self.calls: list[tuple[str, str]] = []
        self.replace_calls = 0
        self.active_state = "former"

    def state(self, label: str) -> ServiceState:
        self.calls.append(("state", label))
        return ServiceState.RUNNING

    def replace(self, label: str, _plist_path: Path) -> None:
        self.replace_calls += 1
        self.calls.append(("replace", label))
        if self.replace_calls in self.fail_replace_calls:
            raise RuntimeError(PRIVATE_FAILURE)
        self.active_state = "candidate" if self.replace_calls == 1 else "former"

    def ensure_running(self, _label: str, _plist_path: Path) -> None:
        raise AssertionError("transition deployment must replace the service")

    def restart(self, _label: str, _plist_path: Path) -> None:
        raise AssertionError("transition deployment must replace the service")

    def stop(self, _label: str, _plist_path: Path) -> None:
        raise AssertionError("transition deployment must replace the service")


class RecordingHealthChecker:
    def __init__(self, *, raise_on_calls: tuple[int, ...] = ()):
        self.raise_on_calls = set(raise_on_calls)
        self.calls: list[str] = []

    def check(self, url: str, *, host: str | None = None) -> HealthResult:
        del host
        self.calls.append(url)
        if len(self.calls) in self.raise_on_calls:
            raise RuntimeError(PRIVATE_FAILURE)
        return HealthResult(True, 200, None)


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
        self.services = services or StatefulServiceController()
        self.health = health or RecordingHealthChecker()
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

    def deploy(self, app_id, **kwargs):
        self.calls += 1
        self.transition = kwargs.get("service_command_transition")
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
        return manager.deploy(app_id, **kwargs)


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


class PublicBasePathMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.platform = self.root / "local-web-server"
        self.application = self.root / "fixture-service"
        self.runtime = self.root / "runtime"

        self.application.mkdir()
        self._write_manifest()
        self._write_provenance()
        self.former_commit = init_git_repo(self.application, {})
        (self.application / "compatible-change.txt").write_text(
            "newer compatible source\n", encoding="utf-8"
        )
        self._git(self.application, "add", "compatible-change.txt")
        self._git(self.application, "commit", "-m", "compatible target")
        self.target_commit = self._git(
            self.application, "rev-parse", "HEAD"
        ).stdout.strip()

        release = self.runtime / "apps/fixture-service/releases" / self.former_commit
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
                    "port": 52700,
                    "startCommand": self._service_command(),
                }
            ],
        }
        self.platform.mkdir()
        init_git_repo(
            self.platform,
            {
                ".gitignore": "config/local/\n",
                "config/apps.example.json": self._registry_bytes().decode("utf-8"),
                "unrelated.txt": "fixture\n",
            },
        )
        self.registry_bytes = self._registry_bytes()
        self.profile_paths = HostProfilePaths.for_repository(self.platform)
        self.profile_store = HostProfileStore(self.profile_paths)
        self.profile_store.initialise(self.registry_bytes)
        self.registry = self.profile_paths.profile

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
        app_id: str = "fixture-service",
        kind: str = "service",
        route: str = "/fixture",
        declare_start_command: bool = True,
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
            manifest["service"] = {
                "module": "fixture_service",
                "internalHealthPath": "/healthz",
            }
            if declare_start_command:
                manifest["service"]["startCommand"] = [
                    "/usr/bin/env",
                    "node",
                    "{release}/service.mjs",
                    "--port",
                    "{port}",
                ]
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

    def _service_command(self):
        return [
            "/usr/bin/env",
            "node",
            str(self.runtime / "apps/fixture-service/current/service.mjs"),
            "--port",
            "52700",
        ]

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

    def _select_current(self, commit: str):
        release = self.runtime / "apps/fixture-service/releases" / commit
        release.mkdir(parents=True, exist_ok=True)
        current = self.runtime / "apps/fixture-service/current"
        current.unlink(missing_ok=True)
        os.symlink(f"releases/{commit}", current)

    def _commit_deployed_manifest(self, *, app_id: str, kind: str, route: str):
        self._write_manifest(app_id=app_id, kind=kind, route=route)
        self._git(self.application, "add", "local-web.json")
        self._git(self.application, "commit", "-m", "invalid deployed manifest")
        deployed = self._git(self.application, "rev-parse", "HEAD").stdout.strip()
        self._select_current(deployed)
        self._write_manifest()
        self._git(self.application, "add", "local-web.json")
        self._git(self.application, "commit", "-m", "restore compatible target")

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
        deployer = deployer or TransitionDeployer(
            runtime=self.runtime,
            target_commit=self.target_commit,
            former_current=self.former_commit,
            former_previous=None,
        )
        verifier = verifier or RecordingVerifier()
        migrator = PublicBasePathMigrator(
            platform_repository=self.platform,
            registry_path=self.registry,
            registrar=registrar,
            profile_store=profile_store,
            install=install,
            deployer_factory=lambda registry: self._capture_registry(
                deployer, registry
            ),
            verify=verifier,
        )
        return migrator, registrar, profile_store, install, deployer, verifier

    @staticmethod
    def _capture_registry(deployer, registry):
        deployer.registry = registry
        return deployer

    def _reset_platform_and_runtime(self):
        shutil.rmtree(self.profile_paths.local)
        self.profile_paths = HostProfilePaths.for_repository(self.platform)
        self.profile_store = HostProfileStore(self.profile_paths)
        self.profile_store.initialise(self.registry_bytes)
        self.registry = self.profile_paths.profile
        layout = RuntimeLayout(self.runtime, "fixture-service")
        for pointer in (layout.current, layout.previous):
            pointer.unlink(missing_ok=True)
        os.symlink(f"releases/{self.former_commit}", layout.current)
        for commit in (
            self.target_commit,
            EXTERNAL_COMMIT,
            UNRELATED_COMMIT,
        ):
            release = layout.releases / commit
            if release.is_symlink():
                release.unlink()
            elif release.exists():
                shutil.rmtree(release)

    def _assert_private_failure_is_bounded(self, error):
        self.assertNotIn(PRIVATE_FAILURE, str(error))
        self.assertNotIn(PRIVATE_VALUE, str(error))
        self.assertNotIn(str(self.root), str(error))

    def _assert_recovered_state(
        self,
        install: RecordingInstaller,
        deployer,
        verifier: RecordingVerifier,
        *,
        expect_tile: bool = True,
    ):
        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertEqual(install.installed_state, "former")
        self.assertEqual(deployer.service_state, "former")
        if expect_tile:
            self.assertIn("former", verifier.calls)
        self.assertFalse((layout.releases / self.target_commit).exists())
        self.assertEqual(
            self._git(self.platform, "status", "--porcelain").stdout,
            "",
        )

    def _assert_changed_apply_rejects_application_checkout(self):
        migrator, registrar, profile_store, install, deployer, verifier = self.migrator()
        before = snapshot_tree(self.root)

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration validation failed$",
        ) as raised:
            migrator.migrate(self.application)

        self.assertEqual(snapshot_tree(self.root), before)
        self.assertEqual(registrar.plan_calls, 2)
        self.assertEqual(registrar.publish_calls, 0)
        self.assertEqual(profile_store.clean_calls, [False, True, False])
        self.assertEqual(profile_store.migration_calls, 0)
        self.assertEqual(profile_store.restoration_calls, 0)
        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])
        self._assert_private_failure_is_bounded(raised.exception)

    def assertValidationFailed(self, migrator):
        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration validation failed$",
        ) as raised:
            migrator.preview(self.application)
        self.assertNotIn(str(self.root), str(raised.exception))
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_preview_is_bounded_read_only_and_feature_branch_eligible(self):
        self._git(self.platform, "switch", "-c", "feature/preview")
        self._git(self.application, "switch", "-c", "feature/preview")
        migrator, registrar, profile_store, install, deployer, verifier = self.migrator()
        before = snapshot_tree(self.root)

        plan = migrator.preview(self.application)

        self.assertEqual(
            (
                plan.app_id,
                plan.route,
                plan.port,
                plan.public_base_path_status,
                plan.identity_status,
                plan.repository_status,
                plan.route_status,
                plan.port_status,
                plan.service_command_status,
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
                "unchanged",
                "redeploy and reload",
            ),
        )
        self.assertEqual(snapshot_tree(self.root), before)
        self.assertNotIn("VITE_PUBLIC_BASE_PATH", repr(plan))
        self.assertNotIn(PRIVATE_VALUE, repr(plan))
        self.assertNotIn(str(self.application), repr(plan))
        self.assertNotIn(self.target_commit, repr(plan))
        self.assertEqual(registrar.plan_calls, 1)
        self.assertEqual(profile_store.clean_calls, [False])
        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])

    def test_preview_accepts_legacy_deployed_command_when_target_matches_registration(self):
        self._write_manifest(declare_start_command=False)
        self._git(self.application, "add", "local-web.json")
        self._git(self.application, "commit", "-m", "legacy implicit service command")
        self.former_commit = self._git(
            self.application, "rev-parse", "HEAD"
        ).stdout.strip()
        self._select_current(self.former_commit)

        self._write_manifest()
        self._git(self.application, "add", "local-web.json")
        self._git(self.application, "commit", "-m", "declare registered command")
        self.target_commit = self._git(
            self.application, "rev-parse", "HEAD"
        ).stdout.strip()
        self.assertFalse((self.application / ".venv").exists())

        migrator, registrar, profile_store, install, deployer, verifier = self.migrator()
        before = snapshot_tree(self.root)

        plan = migrator.preview(self.application)

        self.assertEqual(plan.public_base_path_status, "change")
        self.assertEqual(plan.service_command_status, "unchanged")
        self.assertEqual(plan.service_action, "redeploy and reload")
        self.assertEqual(snapshot_tree(self.root), before)
        self.assertEqual(registrar.plan_calls, 1)
        self.assertEqual(profile_store.clean_calls, [False])
        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])

    def test_preview_sanitizes_public_errors_from_collaborators(self):
        migrator, *_ = self.migrator(
            registrar=RecordingRegistrar(fail="public-error")
        )

        with self.assertRaises(PublicBasePathMigrationError) as raised:
            migrator.preview(self.application)

        self.assertEqual(
            str(raised.exception),
            "public base path migration validation failed",
        )
        self.assertNotIn("PRIVATE_COLLABORATOR_FAILURE", str(raised.exception))

    def test_preview_rejects_missing_and_malformed_runtime_pointers(self):
        current = self.runtime / "apps/fixture-service/current"
        previous = self.runtime / "apps/fixture-service/previous"
        cases = ("missing-current", "outside-current", "outside-previous")
        for case in cases:
            with self.subTest(case=case):
                current.unlink(missing_ok=True)
                previous.unlink(missing_ok=True)
                os.symlink(f"releases/{self.former_commit}", current)
                if case == "missing-current":
                    current.unlink()
                elif case == "outside-current":
                    current.unlink()
                    os.symlink(str(self.root), current)
                else:
                    os.symlink(str(self.root), previous)
                migrator, *_ = self.migrator()
                self.assertValidationFailed(migrator)

    def test_preview_rejects_mismatched_deployed_identity_kind_and_route(self):
        cases = (
            ("other-service", "service", "/fixture"),
            ("fixture-service", "static", "/fixture"),
            ("fixture-service", "service", "/former-route"),
        )
        for app_id, kind, route in cases:
            with self.subTest(app_id=app_id, kind=kind, route=route):
                self._commit_deployed_manifest(app_id=app_id, kind=kind, route=route)
                migrator, *_ = self.migrator()
                self.assertValidationFailed(migrator)

    def test_preview_rejects_changed_command_port_duplicate_match_and_dirty_registry(self):
        for failure in ("changed-command", "changed-port"):
            with self.subTest(failure=failure):
                migrator, *_ = self.migrator(
                    registrar=RecordingRegistrar(fail=failure)
                )
                self.assertValidationFailed(migrator)

        duplicate = dict(self.registry_payload["apps"][0])
        duplicate["id"] = "fixture-alias"
        self.registry_payload["apps"].append(duplicate)
        self.registry.write_bytes(self._registry_bytes())
        duplicate_migrator, *_ = self.migrator()
        self.assertValidationFailed(duplicate_migrator)

        self.registry_payload["apps"].pop()
        self.registry.write_bytes(self.registry_bytes + b"\n")
        dirty_migrator, *_ = self.migrator()
        self.assertValidationFailed(dirty_migrator)

    def test_changed_preview_requires_a_fresh_unreleased_target(self):
        self._select_current(self.target_commit)
        equal_target, *_ = self.migrator()
        self.assertValidationFailed(equal_target)

        self._select_current(self.former_commit)
        existing_target, *_ = self.migrator()
        self.assertValidationFailed(existing_target)

    def test_current_registration_skips_fresh_release_gates_and_is_a_no_op(self):
        self.registry_payload["apps"][0]["environment"] = {
            "VITE_PUBLIC_BASE_PATH": PRIVATE_VALUE
        }
        self._publish_fixture_profile()
        self._select_current(self.target_commit)
        migrator, registrar, profile_store, install, deployer, verifier = self.migrator()
        before = snapshot_tree(self.root)

        plan = migrator.preview(self.application)

        self.assertEqual(snapshot_tree(self.root), before)
        self.assertEqual(plan.public_base_path_status, "current")
        self.assertEqual(plan.service_action, "none")
        self.assertNotIn(PRIVATE_VALUE, repr(plan))
        self.assertEqual(registrar.plan_calls, 1)
        self.assertEqual(profile_store.clean_calls, [False])
        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])

    def test_apply_replans_publishes_deploys_transition_and_returns_bounded_result(self):
        migrator, registrar, profile_store, install, deployer, verifier = self.migrator()

        result = migrator.migrate(self.application)

        self.assertTrue(result.verified)
        self.assertEqual(result.plan.port, 52700)
        self.assertRegex(result.registry_revision or "", r"^[0-9a-f]{64}$")
        self.assertEqual(registrar.plan_calls, 2)
        self.assertEqual(registrar.publish_calls, 0)
        self.assertEqual(profile_store.clean_calls, [False, True, False])
        self.assertEqual(profile_store.migration_calls, 1)
        self.assertEqual(profile_store.restoration_calls, 0)
        self.assertEqual(install.calls, ["candidate"])
        self.assertEqual(install.installed_state, "candidate")
        self.assertEqual(deployer.replacements, ["candidate"])
        self.assertEqual(deployer.service_state, "candidate")
        self.assertEqual(deployer.health_evidence, ["candidate"])
        self.assertEqual(verifier.calls, ["candidate"])
        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(read_release_commit(layout.current), self.target_commit)
        self.assertEqual(read_release_commit(layout.previous), self.former_commit)
        self.assertEqual(
            json.loads(self.registry.read_text(encoding="utf-8"))["apps"][0][
                "environment"
            ],
            {"VITE_PUBLIC_BASE_PATH": PRIVATE_VALUE},
        )
        self.assertEqual(
            [revision.operation for revision in self.profile_store.revisions()],
            ["initialisation", "public-base-path-migration"],
        )
        self.assertNotIn(str(self.application), repr(result))
        self.assertNotIn("deployment.log", repr(result))
        self.assertNotIn(PRIVATE_VALUE, repr(result))
        self.assertNotIn(PRIVATE_FAILURE, repr(result))
        self.assertNotIn(self.target_commit, repr(result.plan))
        self.assertNotIn(self.target_commit, repr(deployer.transition))

    def test_current_apply_is_an_exact_noop(self):
        self.registry_payload["apps"][0]["environment"] = {
            "VITE_PUBLIC_BASE_PATH": PRIVATE_VALUE
        }
        self._publish_fixture_profile()
        self._select_current(self.target_commit)
        migrator, registrar, profile_store, install, deployer, verifier = self.migrator()
        before = snapshot_tree(self.root)

        result = migrator.migrate(self.application)

        self.assertEqual(snapshot_tree(self.root), before)
        self.assertEqual(result.plan.public_base_path_status, "current")
        self.assertEqual(result.plan.service_action, "none")
        self.assertIsNone(result.registry_revision)
        self.assertIsNone(result.deployment)
        self.assertFalse(result.verified)
        self.assertEqual(registrar.plan_calls, 1)
        self.assertEqual(registrar.publish_calls, 0)
        self.assertEqual(profile_store.clean_calls, [False])
        self.assertEqual(profile_store.migration_calls, 0)
        self.assertEqual(profile_store.restoration_calls, 0)
        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])

    def test_feature_branch_apply_cannot_cross_the_clean_main_gate(self):
        self._git(self.platform, "switch", "-c", "feature/apply")
        migrator, registrar, profile_store, install, deployer, verifier = self.migrator()
        before = snapshot_tree(self.root)

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration validation failed$",
        ) as raised:
            migrator.migrate(self.application)

        self.assertEqual(snapshot_tree(self.root), before)
        self.assertEqual(registrar.plan_calls, 1)
        self.assertEqual(registrar.publish_calls, 0)
        self.assertEqual(profile_store.clean_calls, [False, True])
        self.assertEqual(profile_store.migration_calls, 0)
        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])
        self._assert_private_failure_is_bounded(raised.exception)

    def test_changed_apply_rejects_an_application_feature_branch(self):
        self._git(self.application, "switch", "-c", "feature/apply")

        self._assert_changed_apply_rejects_application_checkout()

        self.assertEqual(
            self._git(self.application, "branch", "--show-current").stdout.strip(),
            "feature/apply",
        )

    def test_changed_apply_rejects_staged_application_changes(self):
        staged = self.application / "staged-private.txt"
        staged.write_text("PRIVATE_STAGED_VALUE\n", encoding="utf-8")
        self._git(self.application, "add", staged.name)

        self._assert_changed_apply_rejects_application_checkout()

        self.assertIn(
            "A  staged-private.txt",
            self._git(self.application, "status", "--short").stdout,
        )

    def test_changed_apply_rejects_unstaged_application_changes(self):
        tracked = self.application / "compatible-change.txt"
        tracked.write_text("PRIVATE_UNSTAGED_VALUE\n", encoding="utf-8")

        self._assert_changed_apply_rejects_application_checkout()

        self.assertIn(
            " M compatible-change.txt",
            self._git(self.application, "status", "--short").stdout,
        )

    def test_changed_apply_rejects_untracked_application_files(self):
        untracked = self.application / "untracked-private.txt"
        untracked.write_text("PRIVATE_UNTRACKED_VALUE\n", encoding="utf-8")

        self._assert_changed_apply_rejects_application_checkout()

        self.assertIn(
            "?? untracked-private.txt",
            self._git(self.application, "status", "--short").stdout,
        )

    def test_apply_rejects_a_changed_exact_replan_before_publication(self):
        registrar = RecordingRegistrar(fail="changed-plan")
        migrator, _, profile_store, install, deployer, verifier = self.migrator(
            registrar=registrar
        )

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration validation failed$",
        ):
            migrator.migrate(self.application)

        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(registrar.plan_calls, 2)
        self.assertEqual(registrar.publish_calls, 0)
        self.assertEqual(profile_store.migration_calls, 0)
        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])

    def test_advancing_application_main_is_not_built_or_reported_as_recovered(self):
        builder = RecordingReleaseBuilder()
        deployer = AdvancingMainDeployer(
            self.application,
            self.root / "LaunchAgents",
            builder,
        )
        migrator, _, _, install, _, verifier = self.migrator(deployer=deployer)

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration failed; recovery failed$",
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
        self.assertEqual(install.installed_state, "former")
        self.assertIn("former", verifier.calls)
        self._assert_private_failure_is_bounded(raised.exception)

    def test_pointer_races_are_never_clobbered_or_reported_as_recovered(self):
        for failure, expected_current in (
            ("pointer-race-before-deploy", self.target_commit),
            ("pointer-race-after-switch", EXTERNAL_COMMIT),
        ):
            with self.subTest(failure=failure):
                deployer = TransitionDeployer(
                    runtime=self.runtime,
                    target_commit=self.target_commit,
                    former_current=self.former_commit,
                    former_previous=None,
                    fail=failure,
                )
                migrator, *_ = self.migrator(deployer=deployer)

                with self.assertRaisesRegex(
                    PublicBasePathMigrationError,
                    "^public base path migration failed; recovery failed$",
                ):
                    migrator.migrate(self.application)

                layout = RuntimeLayout(self.runtime, "fixture-service")
                self.assertEqual(read_release_commit(layout.current), expected_current)
                self.assertNotEqual(self.registry.read_bytes(), self.registry_bytes)
                self._reset_platform_and_runtime()

    def test_profile_publication_failures_restore_exact_bytes_and_audit_state(self):
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
                    registrar=registrar,
                    profile_store=profile_store,
                )

                with self.assertRaisesRegex(
                    PublicBasePathMigrationError,
                    "^public base path migration registry update failed$",
                ) as raised:
                    migrator.migrate(self.application)

                layout = RuntimeLayout(self.runtime, "fixture-service")
                self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
                self.assertEqual(read_release_commit(layout.current), self.former_commit)
                self.assertIsNone(read_release_commit(layout.previous))
                self.assertEqual(install.calls, [])
                self.assertEqual(deployer.calls, 0)
                self.assertEqual(verifier.calls, [])
                self.assertEqual(
                    self._git(self.platform, "status", "--porcelain").stdout,
                    "",
                )
                expected_operations = ["initialisation"]
                if publication_failure == "migration-publication-after":
                    expected_operations.extend(
                        [
                            "public-base-path-migration",
                            "public-base-path-restoration",
                        ]
                    )
                self.assertEqual(
                    [
                        revision.operation
                        for revision in self.profile_store.revisions()
                    ],
                    expected_operations,
                )
                self._assert_private_failure_is_bounded(raised.exception)
                self._reset_platform_and_runtime()

    def test_recoverable_post_publication_failures_restore_every_owned_state(self):
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
                    fail=failure if failure in {"build", "replacement", "health"} else None,
                )
                migrator, registrar, profile_store, *_ = self.migrator(
                    install=install,
                    deployer=deployer,
                    verifier=verifier,
                )

                with self.assertRaisesRegex(
                    PublicBasePathMigrationError,
                    f"^public base path migration {message}$",
                ) as raised:
                    migrator.migrate(self.application)

                self._assert_recovered_state(install, deployer, verifier)
                self.assertEqual(registrar.restore_calls, 0)
                self.assertGreaterEqual(profile_store.restoration_calls, 1)
                self.assertEqual(
                    [
                        revision.operation
                        for revision in self.profile_store.revisions()
                    ],
                    [
                        "initialisation",
                        "public-base-path-migration",
                        "public-base-path-restoration",
                    ],
                )
                self._assert_private_failure_is_bounded(raised.exception)
                self._reset_platform_and_runtime()

    def test_profile_races_after_publication_install_and_restored_install_are_recovery_failures(self):
        cases = (
            ("post-publication-profile-race", None, None),
            (None, "post-install-registry-race", None),
            (None, "post-old-install-registry-race", "health"),
        )
        for publication_failure, install_failure, primary_failure in cases:
            with self.subTest(
                publication=publication_failure,
                install=install_failure,
            ):
                profile_store = RecordingProfileStore(
                    self.profile_store,
                    fail=publication_failure,
                )
                install = RecordingInstaller(
                    self.registry,
                    fail=install_failure,
                )
                deployer = TransitionDeployer(
                    runtime=self.runtime,
                    target_commit=self.target_commit,
                    former_current=self.former_commit,
                    former_previous=None,
                    fail=primary_failure,
                )
                migrator, _, _, _, _, verifier = self.migrator(
                    profile_store=profile_store,
                    install=install,
                    deployer=deployer,
                )

                with self.assertRaisesRegex(
                    PublicBasePathMigrationError,
                    "^public base path migration failed; recovery failed$",
                ) as raised:
                    migrator.migrate(self.application)

                self.assertNotEqual(self.registry.read_bytes(), self.registry_bytes)
                self._assert_private_failure_is_bounded(raised.exception)
                self._reset_platform_and_runtime()

    def test_exact_prior_profile_race_cannot_bypass_restoration_revision(self):
        profile_store = RecordingProfileStore(
            self.profile_store, fail="post-publication-exact-before-race"
        )
        migrator, _, _, install, deployer, verifier = self.migrator(
            profile_store=profile_store
        )

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration failed; recovery failed$",
        ) as raised:
            migrator.migrate(self.application)

        self.assertEqual(install.calls, [])
        self.assertEqual(deployer.calls, 0)
        self.assertEqual(verifier.calls, [])
        self._assert_private_failure_is_bounded(raised.exception)

    def test_registry_races_during_candidate_and_restored_tile_checks_are_recovery_failures(self):
        for verifier_failure, primary_failure in (
            ("post-candidate-tile-registry-race", None),
            ("post-former-tile-registry-race", "health"),
        ):
            with self.subTest(verifier=verifier_failure):
                verifier = RecordingVerifier(
                    fail=verifier_failure,
                    registry_path=self.registry,
                )
                deployer = TransitionDeployer(
                    runtime=self.runtime,
                    target_commit=self.target_commit,
                    former_current=self.former_commit,
                    former_previous=None,
                    fail=primary_failure,
                )
                migrator, *_ = self.migrator(
                    deployer=deployer,
                    verifier=verifier,
                )

                with self.assertRaisesRegex(
                    PublicBasePathMigrationError,
                    "^public base path migration failed; recovery failed$",
                ) as raised:
                    migrator.migrate(self.application)

                self.assertNotEqual(self.registry.read_bytes(), self.registry_bytes)
                self._assert_private_failure_is_bounded(raised.exception)
                self._reset_platform_and_runtime()

    def test_real_task3_success_applies_install_service_health_and_tile(self):
        builder = RecordingReleaseBuilder()
        services = StatefulServiceController()
        health = RecordingHealthChecker()
        deployer = RealTask3Deployer(
            root=self.root,
            builder=builder,
            services=services,
            health=health,
        )
        install = RecordingInstaller(self.registry)
        verifier = RecordingVerifier()
        migrator, *_ = self.migrator(
            install=install,
            deployer=deployer,
            verifier=verifier,
        )

        result = migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertTrue(result.verified)
        self.assertEqual(builder.calls, [self.target_commit])
        self.assertEqual(install.calls, ["candidate"])
        self.assertEqual(services.replace_calls, 1)
        self.assertEqual(services.active_state, "candidate")
        self.assertEqual(len(health.calls), 3)
        self.assertEqual(verifier.calls, ["candidate"])
        self.assertEqual(read_release_commit(layout.current), self.target_commit)
        self.assertEqual(read_release_commit(layout.previous), self.former_commit)
        self.assertEqual(
            read_release_commit(layout.current),
            (layout.releases / self.target_commit / "source-commit").read_text(
                encoding="utf-8"
            ),
        )

    def test_real_task3_build_failure_reloads_and_proves_former_service(self):
        builder = FailingReleaseBuilder()
        services = StatefulServiceController()
        health = RecordingHealthChecker()
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
            PublicBasePathMigrationError,
            "^public base path migration deployment failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(builder.calls, [self.target_commit])
        self.assertEqual(registrar.restore_calls, 0)
        self.assertEqual(profile_store.restoration_calls, 1)
        self.assertEqual(install.calls, ["former"])
        self.assertEqual(services.replace_calls, 1)
        self.assertEqual(len(health.calls), 3)
        self.assertEqual(verifier.calls, ["former"])
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertFalse((layout.releases / self.target_commit).exists())
        self._assert_private_failure_is_bounded(raised.exception)

    def test_real_task3_install_failure_reloads_and_proves_former_service(self):
        builder = RecordingReleaseBuilder()
        services = StatefulServiceController()
        health = RecordingHealthChecker()
        deployer = RealTask3Deployer(
            root=self.root,
            builder=builder,
            services=services,
            health=health,
        )
        install = RecordingInstaller(self.registry, fail="install")
        verifier = RecordingVerifier()
        migrator, *_ = self.migrator(
            install=install,
            deployer=deployer,
            verifier=verifier,
        )

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration deployment failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(builder.calls, [self.target_commit])
        self.assertEqual(install.calls, ["candidate", "former"])
        self.assertEqual(install.installed_state, "former")
        self.assertEqual(services.replace_calls, 1)
        self.assertEqual(len(health.calls), 3)
        self.assertEqual(verifier.calls, ["former"])
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertFalse((layout.releases / self.target_commit).exists())
        self._assert_private_failure_is_bounded(raised.exception)

    def test_real_task3_failed_former_replacement_is_a_recovery_failure(self):
        services = StatefulServiceController(fail_replace_calls=(1,))
        health = RecordingHealthChecker()
        deployer = RealTask3Deployer(
            root=self.root,
            services=services,
            health=health,
        )
        install = RecordingInstaller(self.registry, fail="install")
        verifier = RecordingVerifier()
        migrator, *_ = self.migrator(
            install=install,
            deployer=deployer,
            verifier=verifier,
        )

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration failed; recovery failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(install.calls, ["candidate", "former"])
        self.assertEqual(services.replace_calls, 1)
        self.assertEqual(health.calls, [])
        self.assertEqual(verifier.calls, [])
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertTrue((layout.releases / self.target_commit).is_dir())
        self._assert_private_failure_is_bounded(raised.exception)

    def test_real_task3_failed_former_health_is_a_recovery_failure(self):
        services = StatefulServiceController()
        health = RecordingHealthChecker(raise_on_calls=(1,))
        deployer = RealTask3Deployer(
            root=self.root,
            services=services,
            health=health,
        )
        install = RecordingInstaller(self.registry, fail="install")
        verifier = RecordingVerifier()
        migrator, *_ = self.migrator(
            install=install,
            deployer=deployer,
            verifier=verifier,
        )

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration failed; recovery failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(install.calls, ["candidate", "former"])
        self.assertEqual(services.replace_calls, 1)
        self.assertEqual(len(health.calls), 1)
        self.assertEqual(verifier.calls, [])
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertTrue((layout.releases / self.target_commit).is_dir())
        self._assert_private_failure_is_bounded(raised.exception)

    def test_real_task3_recovery_failure_never_runs_weaker_outer_recovery(self):
        services = StatefulServiceController(fail_replace_calls=(2,))
        health = RecordingHealthChecker(raise_on_calls=(1,))
        deployer = RealTask3Deployer(
            root=self.root,
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
            PublicBasePathMigrationError,
            "^public base path migration failed; recovery failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertEqual(registrar.restore_calls, 0)
        self.assertEqual(profile_store.restoration_calls, 1)
        self.assertEqual(install.calls, ["candidate", "former"])
        self.assertEqual(services.replace_calls, 2)
        self.assertEqual(services.active_state, "candidate")
        self.assertEqual(len(health.calls), 1)
        self.assertEqual(verifier.calls, [])
        self.assertTrue((layout.releases / self.target_commit).is_dir())
        self.assertEqual(
            [revision.operation for revision in self.profile_store.revisions()],
            [
                "initialisation",
                "public-base-path-migration",
                "public-base-path-restoration",
            ],
        )
        self._assert_private_failure_is_bounded(raised.exception)

    def test_restoration_publication_before_effect_leaves_one_migration_checkpoint(self):
        registrar = RecordingRegistrar()
        profile_store = RecordingProfileStore(
            self.profile_store,
            fail="restoration-publication-before",
        )
        services = StatefulServiceController()
        health = RecordingHealthChecker(raise_on_calls=(1,))
        deployer = RealTask3Deployer(
            root=self.root,
            services=services,
            health=health,
        )
        install = RecordingInstaller(self.registry)
        verifier = RecordingVerifier()
        migrator, *_ = self.migrator(
            registrar=registrar,
            profile_store=profile_store,
            install=install,
            deployer=deployer,
            verifier=verifier,
        )

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration failed; recovery failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(registrar.restore_calls, 0)
        self.assertEqual(profile_store.migration_calls, 1)
        self.assertEqual(profile_store.restoration_calls, 1)
        self.assertEqual(install.calls, ["candidate"])
        self.assertEqual(services.replace_calls, 1)
        self.assertEqual(services.active_state, "candidate")
        self.assertEqual(len(health.calls), 1)
        self.assertEqual(verifier.calls, [])
        self.assertNotEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertTrue((layout.releases / self.target_commit).is_dir())
        self.assertEqual(
            [revision.operation for revision in self.profile_store.revisions()],
            ["initialisation", "public-base-path-migration"],
        )
        self._assert_private_failure_is_bounded(raised.exception)

    def test_restoration_publication_after_effect_is_not_compensated_twice(self):
        registrar = RecordingRegistrar()
        profile_store = RecordingProfileStore(
            self.profile_store,
            fail="restoration-publication-after",
        )
        services = StatefulServiceController()
        health = RecordingHealthChecker(raise_on_calls=(1,))
        deployer = RealTask3Deployer(
            root=self.root,
            services=services,
            health=health,
        )
        install = RecordingInstaller(self.registry)
        verifier = RecordingVerifier()
        migrator, *_ = self.migrator(
            registrar=registrar,
            profile_store=profile_store,
            install=install,
            deployer=deployer,
            verifier=verifier,
        )

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration failed; recovery failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(registrar.restore_calls, 0)
        self.assertEqual(profile_store.migration_calls, 1)
        self.assertEqual(profile_store.restoration_calls, 1)
        self.assertEqual(install.calls, ["candidate"])
        self.assertEqual(services.replace_calls, 1)
        self.assertEqual(services.active_state, "candidate")
        self.assertEqual(len(health.calls), 1)
        self.assertEqual(verifier.calls, [])
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertTrue((layout.releases / self.target_commit).is_dir())
        self.assertEqual(
            [revision.operation for revision in self.profile_store.revisions()],
            [
                "initialisation",
                "public-base-path-migration",
                "public-base-path-restoration",
            ],
        )
        self._assert_private_failure_is_bounded(raised.exception)

    def test_real_task3_post_switch_recovery_is_not_repeated_after_unwind(self):
        services = StatefulServiceController()
        health = RecordingHealthChecker(raise_on_calls=(1,))
        deployer = RealTask3Deployer(
            root=self.root,
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
            PublicBasePathMigrationError,
            "^public base path migration deployment failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertEqual(registrar.restore_calls, 0)
        self.assertEqual(profile_store.restoration_calls, 1)
        self.assertEqual(install.calls, ["candidate", "former"])
        self.assertEqual(services.replace_calls, 2)
        self.assertEqual(services.active_state, "former")
        self.assertEqual(len(health.calls), 4)
        self.assertEqual(verifier.calls, ["former"])
        self.assertFalse((layout.releases / self.target_commit).exists())
        self._assert_private_failure_is_bounded(raised.exception)

    def test_real_task3_pre_switch_cleanup_proof_and_prune_hold_the_app_lock(self):
        builder = UnexpectedPathReleaseBuilder()
        deployer = RealTask3Deployer(root=self.root, builder=builder)
        install = RecordingInstaller(self.registry)
        verifier = RecordingVerifier()
        migrator, *_ = self.migrator(
            install=install,
            deployer=deployer,
            verifier=verifier,
        )
        layout = RuntimeLayout(self.runtime, "fixture-service")
        cleanup_lock_outcomes: list[str] = []

        def race_prune(actual_layout, *, protected_commits=()):
            self.assertEqual(actual_layout.app_id, layout.app_id)
            try:
                with AppLock(layout):
                    cleanup_lock_outcomes.append("acquired")
                    concurrent = layout.releases / UNRELATED_COMMIT
                    concurrent.mkdir()
                    (concurrent / "source-commit").write_text(
                        UNRELATED_COMMIT,
                        encoding="utf-8",
                    )
            except DeploymentBusy:
                cleanup_lock_outcomes.append("blocked")
            return prune_runtime_releases(
                actual_layout,
                protected_commits=protected_commits,
            )

        with patch(
            "local_web_server.public_base_path_migration.prune_releases",
            side_effect=race_prune,
        ):
            with self.assertRaisesRegex(
                PublicBasePathMigrationError,
                "^public base path migration deployment failed$",
            ) as raised:
                migrator.migrate(self.application)

        self.assertEqual(builder.calls, [self.target_commit])
        self.assertEqual(cleanup_lock_outcomes, ["blocked"])
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(read_release_commit(layout.current), self.former_commit)
        self.assertIsNone(read_release_commit(layout.previous))
        self.assertEqual(install.calls, ["former"])
        self.assertEqual(verifier.calls, ["former"])
        self.assertFalse((layout.releases / self.target_commit).exists())
        self._assert_private_failure_is_bounded(raised.exception)

    def test_each_recovery_boundary_exposes_only_the_one_recovery_failure(self):
        cases = (
            (None, "restoration-publication-before", None, None, "build"),
            (None, None, "old-install", None, "build"),
            (None, None, None, None, ("health", "old-replacement")),
            (None, None, None, None, ("health", "old-health")),
            (None, None, None, "old-tile", "health"),
        )
        for (
            registrar_failure,
            publication_failure,
            install_failure,
            verifier_failure,
            deploy_failure,
        ) in cases:
            with self.subTest(
                registrar=registrar_failure,
                publication=publication_failure,
                install=install_failure,
                verifier=verifier_failure,
                deploy=deploy_failure,
            ):
                registrar = RecordingRegistrar(fail=registrar_failure)
                profile_store = RecordingProfileStore(
                    self.profile_store, fail=publication_failure
                )
                install = RecordingInstaller(self.registry, fail=install_failure)
                verifier = RecordingVerifier(fail=verifier_failure)
                deployer = TransitionDeployer(
                    runtime=self.runtime,
                    target_commit=self.target_commit,
                    former_current=self.former_commit,
                    former_previous=None,
                    fail=deploy_failure,
                )
                migrator, *_ = self.migrator(
                    registrar=registrar,
                    profile_store=profile_store,
                    install=install,
                    deployer=deployer,
                    verifier=verifier,
                )

                with self.assertRaisesRegex(
                    PublicBasePathMigrationError,
                    "^public base path migration failed; recovery failed$",
                ) as raised:
                    migrator.migrate(self.application)

                self._assert_private_failure_is_bounded(raised.exception)
                self._reset_platform_and_runtime()

    def test_recovery_refuses_to_prune_an_unrelated_release(self):
        deployer = TransitionDeployer(
            runtime=self.runtime,
            target_commit=self.target_commit,
            former_current=self.former_commit,
            former_previous=None,
            fail=("health", "unrelated-release-before-cleanup"),
        )
        migrator, *_ = self.migrator(deployer=deployer)

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration failed; recovery failed$",
        ) as raised:
            migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertTrue((layout.releases / UNRELATED_COMMIT).is_dir())
        self.assertTrue((layout.releases / self.target_commit).is_dir())
        self._assert_private_failure_is_bounded(raised.exception)

    def test_target_release_cleanup_failure_is_a_recovery_failure(self):
        deployer = TransitionDeployer(
            runtime=self.runtime,
            target_commit=self.target_commit,
            former_current=self.former_commit,
            former_previous=None,
            fail="health",
        )
        migrator, *_ = self.migrator(deployer=deployer)

        with patch(
            "local_web_server.public_base_path_migration.prune_releases",
            side_effect=RuntimeError(PRIVATE_FAILURE),
            create=True,
        ):
            with self.assertRaisesRegex(
                PublicBasePathMigrationError,
                "^public base path migration failed; recovery failed$",
            ) as raised:
                migrator.migrate(self.application)

        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertTrue((layout.releases / self.target_commit).is_dir())
        self._assert_private_failure_is_bounded(raised.exception)

    def test_recovery_removes_only_the_attempt_release_and_preserves_preexisting_releases(self):
        preexisting_commit = "d" * 40
        layout = RuntimeLayout(self.runtime, "fixture-service")
        preexisting = layout.releases / preexisting_commit
        preexisting.mkdir()
        (preexisting / "owned-before").write_text("unchanged\n", encoding="utf-8")
        install = RecordingInstaller(self.registry)
        verifier = RecordingVerifier()
        deployer = TransitionDeployer(
            runtime=self.runtime,
            target_commit=self.target_commit,
            former_current=self.former_commit,
            former_previous=None,
            fail="health",
        )
        migrator, *_ = self.migrator(
            install=install,
            deployer=deployer,
            verifier=verifier,
        )

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration deployment failed$",
        ):
            migrator.migrate(self.application)

        self._assert_recovered_state(install, deployer, verifier)
        self.assertTrue(preexisting.is_dir())
        self.assertEqual(
            (preexisting / "owned-before").read_text(encoding="utf-8"),
            "unchanged\n",
        )

    def test_post_build_failure_recovery_allows_a_fresh_successful_retry(self):
        failed_install = RecordingInstaller(self.registry)
        failed_verifier = RecordingVerifier()
        failed_deployer = TransitionDeployer(
            runtime=self.runtime,
            target_commit=self.target_commit,
            former_current=self.former_commit,
            former_previous=None,
            fail="health",
        )
        first, *_ = self.migrator(
            install=failed_install,
            deployer=failed_deployer,
            verifier=failed_verifier,
        )

        with self.assertRaisesRegex(
            PublicBasePathMigrationError,
            "^public base path migration deployment failed$",
        ):
            first.migrate(self.application)

        self._assert_recovered_state(
            failed_install,
            failed_deployer,
            failed_verifier,
        )
        retry, _, _, install, deployer, verifier = self.migrator()

        result = retry.migrate(self.application)

        self.assertTrue(result.verified)
        self.assertEqual(install.installed_state, "candidate")
        self.assertEqual(deployer.service_state, "candidate")
        self.assertEqual(verifier.calls, ["candidate"])
        layout = RuntimeLayout(self.runtime, "fixture-service")
        self.assertEqual(read_release_commit(layout.current), self.target_commit)
        self.assertEqual(read_release_commit(layout.previous), self.former_commit)
        self.assertEqual(
            [revision.operation for revision in self.profile_store.revisions()],
            [
                "initialisation",
                "public-base-path-migration",
                "public-base-path-restoration",
                "public-base-path-migration",
            ],
        )


if __name__ == "__main__":
    unittest.main()
