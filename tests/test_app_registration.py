import json
import os
import shutil
import subprocess
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from local_web_server.app_doctor import Diagnostic, DoctorReport
from local_web_server.app_registration import (
    AppRegistrar,
    AppRegistrationError,
    AppRegistrationPlan,
)
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore, HostProfileStoreError
from tests.helpers import init_git_repo


class FixedDoctor:
    def __init__(self, report: DoctorReport | None = None):
        self.report = report or DoctorReport("recipe-notebook", True, ())
        self.repositories: list[Path] = []

    def inspect(self, repository: Path) -> DoctorReport:
        self.repositories.append(repository)
        return self.report


class AppRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.existing_repository = self.root / "existing-app"
        self.repository = self.root / "recipe-notebook"
        self.existing_repository.mkdir()
        self.repository.mkdir()
        self._write_manifest(
            self.existing_repository,
            app_id="existing-app",
            route="/existing-app",
            platform=False,
        )
        self._write_generated_repository(self.repository)
        init_git_repo(self.existing_repository, {})
        init_git_repo(self.repository, {})
        self.registry_payload = {
            "runtimeRoot": str(self.root / "private-runtime"),
            "apps": [
                {
                    "environment": {
                        "VITE_PUBLIC_BASE_PATH": "/existing-app/",
                    },
                    "autoDeploy": False,
                    "repository": str(self.existing_repository),
                    "id": "existing-app",
                }
            ],
            "host": "private-host.local",
            "schemaVersion": 1,
        }
        self.platform = self.root / "platform"
        self.platform.mkdir()
        init_git_repo(
            self.platform,
            {
                ".gitignore": "config/local/\n",
                "config/apps.example.json": "{}\n",
                "local_web_server/source.py": "# fixture\n",
            },
        )
        self.profile_paths = HostProfilePaths.for_repository(self.platform)
        self.registry = self.profile_paths.profile
        self.profile_store = HostProfileStore(self.profile_paths)
        self._write_registry(self.registry_payload)
        self.doctor = FixedDoctor()
        self.registrar = AppRegistrar(doctor=self.doctor)

    def tearDown(self):
        self.temporary.cleanup()

    def test_registrar_exposes_plans_but_no_direct_profile_write_escape_hatches(self):
        for name in (
            "publish_service_command_migration",
            "restore_service_command_migration",
            "publish_public_base_path_migration",
            "restore_public_base_path_migration",
            "_publish_registry",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(AppRegistrar, name))

    def _write_registry(self, payload) -> None:
        content = (json.dumps(payload, indent=4) + "\n").encode("utf-8")
        if self.profile_paths.local.exists():
            shutil.rmtree(self.profile_paths.local)
        self.profile_store = HostProfileStore(self.profile_paths)
        self.profile_store.initialise(content)

    def _write_raw_registry(self, payload, label: str) -> Path:
        """Write an explicit planner-only fixture outside the private store."""

        path = self.root / f"raw-{label}.json"
        path.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ("git", *arguments),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )

    def _commit_changes(self, repository: Path, message: str = "fixture update") -> None:
        if not self._git(repository, "status", "--porcelain").stdout:
            return
        self._git(repository, "add", "--all")
        self._git(repository, "commit", "-m", message)

    def _write_manifest(
        self,
        repository: Path,
        *,
        app_id: str = "recipe-notebook",
        route: str = "/recipe-notebook",
        kind: str = "static",
        environment: tuple[str, ...] = ("VITE_PUBLIC_BASE_PATH",),
        capabilities: tuple[str, ...] = (),
        template_version: int = 1,
        platform: bool = True,
        start_command: list[str] | None = None,
    ) -> None:
        manifest = {
            "schemaVersion": 1,
            "id": app_id,
            "title": "PRIVATE MANIFEST TITLE",
            "route": route,
            "kind": kind,
            "build": {
                "commands": [["npm", "ci"], ["npm", "run", "build"]],
                "output": "dist",
                "environment": list(environment),
            },
            "healthPath": f"{route}/" if kind == "static" else f"{route}/healthz",
            "home": {"icon": "book", "accent": "#8EA7C6"},
        }
        if kind == "service":
            manifest["service"] = {
                "module": "private_service",
                "internalHealthPath": "/healthz",
            }
            if start_command is not None:
                manifest["service"]["startCommand"] = start_command
        if platform:
            manifest["platform"] = {
                "contractVersion": 1,
                "templateVersion": template_version,
                "uiVersion": "0.4.0",
                "capabilities": list(capabilities),
            }
        (repository / "local-web.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def _write_provenance(
        self,
        repository: Path,
        *,
        capabilities: tuple[str, ...] = (),
        template_version: int = 1,
    ) -> None:
        provenance = {
            "schemaVersion": 1,
            "templateVersion": template_version,
            "platformContractVersion": 1,
            "ui": {"version": "0.4.0", "sha256": "a" * 64},
            "capabilities": list(capabilities),
            "domainPaletteTokens": [],
            "managedFiles": [],
        }
        (repository / ".local-web-platform.json").write_text(
            json.dumps(provenance), encoding="utf-8"
        )

    def _write_generated_repository(
        self,
        repository: Path,
        *,
        kind: str = "static",
        environment: tuple[str, ...] = ("VITE_PUBLIC_BASE_PATH",),
        manifest_capabilities: tuple[str, ...] = (),
        provenance_capabilities: tuple[str, ...] = (),
        manifest_template_version: int = 1,
        provenance_template_version: int = 1,
        start_command: list[str] | None = None,
    ) -> None:
        self._write_manifest(
            repository,
            kind=kind,
            environment=environment,
            capabilities=manifest_capabilities,
            template_version=manifest_template_version,
            start_command=start_command,
        )
        self._write_provenance(
            repository,
            capabilities=provenance_capabilities,
            template_version=provenance_template_version,
        )

    def _service_command_migration_fixture(self, name: str = "fixture-service"):
        repository = self.root / name
        repository.mkdir()
        declared_command = [
            "node",
            "{release}/server/service.mjs",
            "--port",
            "{port}",
            "--state-path",
            "{repository}/user-data/state.sqlite3",
        ]
        self._write_manifest(
            repository,
            app_id="fixture-service",
            route="/fixture",
            kind="service",
            start_command=declared_command,
        )
        self._write_provenance(repository)
        init_git_repo(repository, {})
        self.registry_payload["apps"] = [
            {
                "id": "fixture-service",
                "repository": str(repository.resolve()),
                "autoDeploy": True,
                "environment": {"VITE_PUBLIC_BASE_PATH": "/fixture/"},
                "port": 52700,
                "startCommand": [
                    "/usr/bin/env",
                    "node",
                    "old-service.mjs",
                    "--port",
                    "52700",
                ],
            }
        ]
        self._write_registry(self.registry_payload)
        return repository, AppRegistrar(
            doctor=FixedDoctor(DoctorReport("fixture-service", True, ()))
        )

    def _public_base_path_migration_fixture(self, name: str = "fixture-service"):
        repository = self.root / name
        repository.mkdir()
        declared_command = [
            "node",
            "{release}/server/service.mjs",
            "--port",
            "{port}",
        ]
        self._write_manifest(
            repository,
            app_id="fixture-service",
            route="/fixture",
            kind="service",
            environment=("VITE_PUBLIC_BASE_PATH",),
            start_command=declared_command,
        )
        self._write_provenance(repository)
        init_git_repo(repository, {})
        release = self.root / "private-runtime/apps/fixture-service/current"
        self.registry_payload["apps"] = [
            {
                "id": "fixture-service",
                "repository": str(repository.resolve()),
                "autoDeploy": True,
                "port": 52700,
                "startCommand": [
                    "/opt/homebrew/bin/node",
                    str(release / "server/service.mjs"),
                    "--port",
                    "52700",
                ],
            }
        ]
        self._write_registry(self.registry_payload)
        return repository, AppRegistrar(
            doctor=FixedDoctor(DoctorReport("fixture-service", True, ()))
        )

    def test_plans_a_public_base_path_only_migration_without_writing(self):
        repository, registrar = self._public_base_path_migration_fixture()
        before = self.registry.read_bytes()

        plan = registrar.plan_public_base_path_migration(repository, self.registry)

        self.assertEqual((plan.app_id, plan.route, plan.port),
                         ("fixture-service", "/fixture", 52700))
        self.assertTrue(plan.changed)
        self.assertEqual(self.registry.read_bytes(), before)
        candidate = json.loads(plan._candidate)
        original = json.loads(plan._before)
        self.assertEqual(candidate["apps"][0]["environment"],
                         {"VITE_PUBLIC_BASE_PATH": "/fixture/"})
        self.assertEqual(
            {key: value for key, value in candidate["apps"][0].items()
             if key != "environment"},
            {key: value for key, value in original["apps"][0].items()
             if key != "environment"},
        )
        self.assertNotIn("/fixture/", repr(plan))

    def test_preserves_fixed_environment_and_environment_file(self):
        repository, registrar = self._public_base_path_migration_fixture()
        environment_file = repository / ".env"
        environment_file.write_text("PUBLIC_ANALYTICS_MODE=from-file\n", encoding="utf-8")
        self._write_manifest(
            repository,
            app_id="fixture-service",
            route="/fixture",
            kind="service",
            environment=("VITE_PUBLIC_BASE_PATH", "PUBLIC_ANALYTICS_MODE"),
            start_command=["node", "{release}/server/service.mjs", "--port", "{port}"],
        )
        self._commit_changes(repository)
        payload = json.loads(self.registry.read_text())
        payload["apps"][0]["environmentFile"] = str(environment_file)
        payload["apps"][0]["environment"] = {"PUBLIC_ANALYTICS_MODE": "off"}
        self._write_registry(payload)

        plan = registrar.plan_public_base_path_migration(repository, self.registry)

        candidate = json.loads(plan._candidate)["apps"][0]
        self.assertEqual(candidate["environmentFile"], str(environment_file))
        self.assertEqual(candidate["environment"], {
            "PUBLIC_ANALYTICS_MODE": "off",
            "VITE_PUBLIC_BASE_PATH": "/fixture/",
        })

    def test_public_base_path_migration_rejects_invalid_registered_shapes_without_mutation(self):
        cases = (
            "missing registration", "static manifest", "missing allowlist",
            "incomplete service shape", "changed command", "changed port",
            "duplicate match", "conflicting route", "invalid environment",
        )
        for case in cases:
            with self.subTest(case=case):
                repository, registrar = self._public_base_path_migration_fixture(
                    f"fixture-{case.replace(' ', '-')}"
                )
                payload = json.loads(self.registry.read_text())
                entry = payload["apps"][0]
                if case == "missing registration":
                    payload["apps"] = []
                elif case == "static manifest":
                    self._write_manifest(
                        repository, app_id="fixture-service", route="/fixture", kind="static"
                    )
                    self._commit_changes(repository)
                elif case == "missing allowlist":
                    self._write_manifest(
                        repository, app_id="fixture-service", route="/fixture",
                        kind="service", environment=(),
                        start_command=["node", "{release}/server/service.mjs", "--port", "{port}"],
                    )
                    self._commit_changes(repository)
                elif case == "incomplete service shape":
                    entry.pop("startCommand")
                elif case == "changed command":
                    entry["startCommand"][-2] = "other.mjs"
                elif case == "changed port":
                    entry["port"] = 52701
                elif case == "duplicate match":
                    payload["apps"].append(deepcopy(entry))
                elif case == "conflicting route":
                    self._write_manifest(
                        self.existing_repository, app_id="existing-app", route="/fixture",
                        platform=False
                    )
                    self._commit_changes(self.existing_repository)
                    payload["apps"].append({
                        "id": "existing-app", "repository": str(self.existing_repository.resolve()),
                        "autoDeploy": False,
                    })
                elif case == "invalid environment":
                    entry["environment"] = ["VITE_PUBLIC_BASE_PATH=/bad"]
                registry = self._write_raw_registry(
                    payload, f"public-base-{case.replace(' ', '-')}"
                )
                before = registry.read_bytes()
                with self.assertRaises(AppRegistrationError):
                    registrar.plan_public_base_path_migration(repository, registry)
                self.assertEqual(registry.read_bytes(), before)

    def test_public_base_path_migration_publishes_and_restores_exact_candidate(self):
        repository, registrar = self._public_base_path_migration_fixture()
        plan = registrar.plan_public_base_path_migration(repository, self.registry)
        before = plan._before

        revision = self.profile_store.publish_public_base_path_migration(
            plan.app_id, plan._before, plan._candidate
        )

        self.assertRegex(revision or "", r"^[0-9a-f]{64}$")
        self.assertEqual(self.registry.read_bytes(), plan._candidate)
        restored = self.profile_store.publish_public_base_path_restoration(
            plan.app_id, plan._candidate, plan._before
        )
        self.assertRegex(restored or "", r"^[0-9a-f]{64}$")
        self.assertEqual(self.registry.read_bytes(), before)
        with self.assertRaises(HostProfileStoreError):
            self.profile_store.publish_public_base_path_restoration(
                plan.app_id, plan._candidate, plan._before
            )

    def test_public_base_path_migration_is_idempotent_when_already_current(self):
        repository, registrar = self._public_base_path_migration_fixture()
        payload = json.loads(self.registry.read_text())
        payload["apps"][0]["environment"] = {"VITE_PUBLIC_BASE_PATH": "/fixture/"}
        self._write_registry(payload)
        before = self.registry.read_bytes()

        plan = registrar.plan_public_base_path_migration(repository, self.registry)

        self.assertFalse(plan.changed)
        self.assertEqual(plan._before, before)
        self.assertEqual(plan._candidate, before)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_public_base_path_migration_rejects_stale_publication(self):
        repository, registrar = self._public_base_path_migration_fixture()
        plan = registrar.plan_public_base_path_migration(repository, self.registry)
        other = plan._before.replace(b"private-host.local", b"other-host.local")
        self.profile_store.publish_registration("other", plan._before, other)

        with self.assertRaises(HostProfileStoreError):
            self.profile_store.publish_public_base_path_migration(
                plan.app_id, plan._before, plan._candidate
            )

    def _assert_service_command_migration_rejected(
        self, registrar: AppRegistrar, repository: Path, registry: Path
    ) -> None:
        before = registry.read_bytes()
        with self.assertRaises(AppRegistrationError):
            registrar.plan_service_command_migration(repository, registry)
        self.assertEqual(registry.read_bytes(), before)

    def _assert_rejected_without_mutation(
        self,
        registrar: AppRegistrar | None = None,
        *,
        dry_run: bool = False,
    ) -> str:
        before = self.registry.read_bytes()
        with self.assertRaises(AppRegistrationError) as raised:
            (registrar or self.registrar).register(
                self.repository,
                self.registry,
                profile_store=self.profile_store,
                dry_run=dry_run,
            )
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertNotIn(str(self.root), str(raised.exception))
        self.assertNotIn("PRIVATE MANIFEST TITLE", str(raised.exception))
        self.assertNotIn("private-host.local", str(raised.exception))
        return str(raised.exception)

    def test_appends_only_the_generated_host_entry_after_existing_apps(self):
        original_top_level_order = list(self.registry_payload)
        original_apps = json.loads(json.dumps(self.registry_payload["apps"]))
        unrelated_registry = self.root / "unrelated-profile/apps.json"
        unrelated_registry.parent.mkdir()
        unrelated_registry.write_bytes(b"outside registration target\n")
        unrelated_before = unrelated_registry.read_bytes()

        result = self.registrar.register(
            self.repository, self.registry, profile_store=self.profile_store
        )

        registered = json.loads(self.registry.read_text(encoding="utf-8"))
        self.assertEqual(list(registered), original_top_level_order)
        self.assertEqual(registered["apps"][:-1], original_apps)
        self.assertEqual(
            registered["apps"][-1],
            {
                "id": "recipe-notebook",
                "repository": str(self.repository.resolve()),
                "autoDeploy": True,
                "environment": {
                    "VITE_PUBLIC_BASE_PATH": "/recipe-notebook/",
                },
            },
        )
        self.assertEqual(result.app_id, "recipe-notebook")
        self.assertEqual(result.route, "/recipe-notebook")
        self.assertEqual(result.registry, self.registry)
        self.assertTrue(result.changed)
        self.assertFalse(result.dry_run)
        self.assertTrue(self.registry.read_bytes().endswith(b"\n"))
        self.assertIn(b'  "apps": [\n', self.registry.read_bytes())
        self.assertEqual(unrelated_registry.read_bytes(), unrelated_before)

    def test_exact_existing_entry_is_an_idempotent_noop(self):
        first = self.registrar.register(
            self.repository, self.registry, profile_store=self.profile_store
        )
        before = self.registry.read_bytes()

        second = self.registrar.register(
            self.repository, self.registry, profile_store=self.profile_store
        )

        self.assertTrue(first.changed)
        self.assertFalse(second.changed)
        self.assertEqual(self.registry.read_bytes(), before)

    def test_plan_returns_exact_candidate_without_writing(self):
        before = self.registry.read_bytes()

        plan = self.registrar.plan(self.repository, self.registry)

        self.assertTrue(plan.changed)
        self.assertEqual(plan._before, before)
        self.assertEqual(self.registry.read_bytes(), before)
        candidate = json.loads(plan.candidate.decode("utf-8"))
        self.assertEqual(candidate["apps"][-1]["id"], "recipe-notebook")
        self.assertEqual(plan.app_id, "recipe-notebook")
        self.assertEqual(plan.kind, "static")
        self.assertIsNone(plan.port)

    def test_plans_a_service_command_only_migration_without_writing(self):
        repository, registrar = self._service_command_migration_fixture()
        before = self.registry.read_bytes()

        plan = registrar.plan_service_command_migration(repository, self.registry)

        self.assertEqual(plan.app_id, "fixture-service")
        self.assertEqual(plan.route, "/fixture")
        self.assertEqual(plan.port, 52700)
        self.assertTrue(plan.changed)
        self.assertEqual(self.registry.read_bytes(), before)

        candidate = json.loads(plan._candidate.decode("utf-8"))
        before_payload = json.loads(plan._before.decode("utf-8"))
        self.assertEqual(
            {key: value for key, value in candidate["apps"][0].items() if key != "startCommand"},
            {key: value for key, value in before_payload["apps"][0].items() if key != "startCommand"},
        )
        self.assertEqual(candidate["apps"][0]["port"], 52700)
        self.assertIn(str(repository.resolve()), candidate["apps"][0]["startCommand"][-1])
        self.assertNotIn("old-service.mjs", repr(plan))
        self.assertNotIn("service.mjs", repr(plan))
        self.assertEqual(
            {name for name in plan.__dataclass_fields__ if not name.startswith("_")},
            {"app_id", "route", "port", "registry", "changed"},
        )

        self.registry.write_bytes(plan._candidate)
        current = registrar.plan_service_command_migration(repository, self.registry)

        self.assertFalse(current.changed)
        self.assertEqual(current._before, plan._candidate)
        self.assertEqual(current._candidate, plan._candidate)

    def test_service_command_migration_is_not_default_registration(self):
        repository, registrar = self._service_command_migration_fixture()
        before = self.registry.read_bytes()

        with self.assertRaises(AppRegistrationError):
            registrar.plan(repository, self.registry, port=52700)
        with self.assertRaises(AppRegistrationError):
            registrar.register(
                repository,
                self.registry,
                profile_store=self.profile_store,
                port=52700,
            )

        self.assertEqual(self.registry.read_bytes(), before)

    def test_service_command_migration_rejects_invalid_registrations_without_mutation(self):
        cases = (
            "missing",
            "static",
            "incomplete port",
            "incomplete command",
            "duplicate",
            "mismatched id",
            "mismatched repository",
            "conflicting route",
            "conflicting port",
        )
        for case in cases:
            with self.subTest(case=case):
                repository, registrar = self._service_command_migration_fixture(
                    f"fixture-{case.replace(' ', '-')}"
                )
                entry = self.registry_payload["apps"][0]
                if case == "missing":
                    self.registry_payload["apps"].pop()
                elif case == "static":
                    self._write_manifest(
                        repository,
                        app_id="fixture-service",
                        route="/fixture",
                        kind="static",
                    )
                    self._commit_changes(repository)
                elif case == "incomplete port":
                    entry.pop("port")
                elif case == "incomplete command":
                    entry.pop("startCommand")
                elif case == "duplicate":
                    entry["repository"] = str(self.existing_repository.resolve())
                    duplicate = deepcopy(entry)
                    duplicate["id"] = "existing-app"
                    duplicate["repository"] = str(repository.resolve())
                    self.registry_payload["apps"].append(duplicate)
                elif case == "mismatched id":
                    entry["id"] = "other-service"
                elif case == "mismatched repository":
                    other = self.root / "other-service"
                    other.mkdir()
                    entry["repository"] = str(other.resolve())
                elif case == "conflicting route":
                    self._write_manifest(
                        self.existing_repository,
                        app_id="existing-app",
                        route="/fixture",
                        platform=False,
                    )
                    self._commit_changes(self.existing_repository)
                    self.registry_payload["apps"].append(
                        {
                            "id": "existing-app",
                            "repository": str(self.existing_repository.resolve()),
                            "autoDeploy": False,
                            "environment": {"VITE_PUBLIC_BASE_PATH": "/existing-app/"},
                        }
                    )
                elif case == "conflicting port":
                    self._write_manifest(
                        self.existing_repository,
                        app_id="existing-app",
                        route="/existing-app",
                        kind="service",
                        platform=False,
                    )
                    self._commit_changes(self.existing_repository)
                    self.registry_payload["apps"].append(
                        {
                            "id": "existing-app",
                            "repository": str(self.existing_repository.resolve()),
                            "autoDeploy": False,
                            "environment": {"VITE_PUBLIC_BASE_PATH": "/existing-app/"},
                            "port": 52700,
                            "startCommand": ["/usr/bin/true"],
                        }
                    )
                registry = self._write_raw_registry(
                    self.registry_payload, f"service-command-{case.replace(' ', '-')}"
                )

                self._assert_service_command_migration_rejected(
                    registrar, repository, registry
                )

    def test_service_command_migration_validates_and_publishes_exact_bytes(self):
        repository, registrar = self._service_command_migration_fixture()
        plan = registrar.plan_service_command_migration(repository, self.registry)
        candidate = json.loads(plan._candidate)
        changed_port = deepcopy(candidate["apps"][0])
        changed_port["port"] = 52701

        with self.assertRaises(AppRegistrationError):
            registrar._require_command_only_change(candidate["apps"][0], changed_port)

        revision = self.profile_store.publish_service_command_migration(
            plan.app_id, plan._before, plan._candidate
        )
        self.assertRegex(revision or "", r"^[0-9a-f]{64}$")
        self.assertEqual(self.registry.read_bytes(), plan._candidate)

    def test_service_command_migration_restores_only_its_exact_candidate(self):
        repository, registrar = self._service_command_migration_fixture()
        plan = registrar.plan_service_command_migration(repository, self.registry)

        self.profile_store.publish_service_command_migration(
            plan.app_id, plan._before, plan._candidate
        )
        self.profile_store.publish_service_command_restoration(
            plan.app_id, plan._candidate, plan._before
        )
        self.assertEqual(self.registry.read_bytes(), plan._before)

        self.assertIsNone(
            self.profile_store.publish_service_command_restoration(
                plan.app_id, plan._before, plan._before
            )
        )
        self.assertEqual(self.registry.read_bytes(), plan._before)

    def test_reserved_generic_identity_is_rejected_without_mutation(self):
        self._write_manifest(self.repository)
        payload = json.loads((self.repository / "local-web.json").read_text())
        payload["home"]["icon"] = "apps"
        (self.repository / "local-web.json").write_text(json.dumps(payload))
        self._commit_changes(self.repository)

        message = self._assert_rejected_without_mutation(dry_run=True)

        self.assertEqual(message, "application identity is not eligible for registration")

    def test_registration_publication_is_owned_by_the_real_profile_store(self):
        before_revisions = self.profile_store.revisions()

        result = self.registrar.register(
            self.repository, self.registry, profile_store=self.profile_store
        )

        revisions = self.profile_store.revisions()
        self.assertEqual(len(revisions), len(before_revisions) + 1)
        self.assertEqual(revisions[-1].revision_id, result.registry_revision)
        self.assertEqual(revisions[-1].operation, "registration")
        self.assertEqual(revisions[-1].app_id, "recipe-notebook")

    def test_registers_declared_service_command_with_explicit_unique_port(self):
        self._write_generated_repository(
            self.repository,
            kind="service",
            start_command=[
                "/usr/bin/env",
                "node",
                "{release}/server/app.mjs",
                "--port",
                "{port}",
            ],
        )
        self._commit_changes(self.repository)

        result = self.registrar.register(
            self.repository,
            self.registry,
            profile_store=self.profile_store,
            port=52700,
        )

        registered = json.loads(self.registry.read_text())["apps"][-1]
        current = self.root / "private-runtime/apps/recipe-notebook/current"
        self.assertEqual(registered["port"], 52700)
        self.assertEqual(
            registered["startCommand"],
            [
                "/usr/bin/env",
                "node",
                str(current / "server/app.mjs"),
                "--port",
                "52700",
            ],
        )
        self.assertEqual(result.app_id, "recipe-notebook")

    def test_expands_canonical_repository_as_one_registered_argument(self):
        """A repository placeholder must resolve to one canonical argv item."""
        repository = self.root / "Recipe Notebook Workspace"
        repository.mkdir()
        self._write_generated_repository(
            repository,
            kind="service",
            start_command=[
                "/usr/bin/env",
                "node",
                "{release}/server/app.mjs",
                "--port",
                "{port}",
                "--data-dir",
                "{repository}/data",
            ],
        )
        init_git_repo(repository, {})
        self._commit_changes(repository)

        plan = self.registrar.plan(repository, self.registry, port=52700)
        registered = json.loads(plan.candidate)["apps"][-1]
        release = self.root / "private-runtime/apps/recipe-notebook/current"

        self.assertEqual(registered["repository"], str(repository.resolve()))
        self.assertEqual(registered["startCommand"][2], str(release / "server/app.mjs"))
        self.assertEqual(registered["startCommand"][4], "52700")
        self.assertEqual(registered["startCommand"][-1], str(repository.resolve() / "data"))
        self.assertNotIn("{", json.dumps(registered["startCommand"]))

        self.registry.write_bytes(plan.candidate)
        repeated = self.registrar.plan(repository, self.registry, port=52700)

        self.assertFalse(repeated.changed)
        self.assertEqual(repeated.candidate, plan.candidate)

    def test_plans_committed_static_registration_as_an_in_place_service_transition(self):
        """Only the matching static entry may become its committed service entry."""
        static_entry = {
            "id": "recipe-notebook",
            "repository": str(self.repository.resolve()),
            "autoDeploy": True,
            "environmentFile": str((self.repository / ".env").resolve()),
            "environment": {"VITE_PUBLIC_BASE_PATH": "/recipe-notebook/"},
        }
        self.registry_payload["apps"].append(static_entry)
        self._write_registry(self.registry_payload)
        matching_index = 1
        unrelated_index = 0
        unrelated_before = deepcopy(self.registry_payload["apps"][unrelated_index])
        self._write_generated_repository(
            self.repository,
            kind="service",
            start_command=["/usr/bin/env", "node", "server.mjs", "--port", "{port}"],
        )
        self._commit_changes(self.repository)
        before = self.registry.read_bytes()

        plan = self.registrar.plan(self.repository, self.registry, port=52700)
        candidate = json.loads(plan.candidate)

        self.assertTrue(plan.changed)
        self.assertEqual(len(candidate["apps"]), len(self.registry_payload["apps"]))
        self.assertEqual(candidate["apps"][matching_index]["port"], 52700)
        self.assertEqual(
            candidate["apps"][matching_index]["environmentFile"],
            static_entry["environmentFile"],
        )
        self.assertEqual(candidate["apps"][unrelated_index], unrelated_before)
        self.assertEqual(self.registry.read_bytes(), before)

        self.registry.write_bytes(plan.candidate)
        repeated = self.registrar.plan(self.repository, self.registry, port=52700)

        self.assertFalse(repeated.changed)
        self.assertEqual(repeated.candidate, plan.candidate)

    def test_rejects_unsupported_existing_registration_transitions_without_mutation(self):
        """Only a matching static host can be replaced by a service entry."""
        alternate_repository = self.root / "other-notebook"
        alternate_repository.mkdir()
        static_entry = {
            "id": "recipe-notebook",
            "repository": str(self.repository.resolve()),
            "autoDeploy": True,
            "environment": {"VITE_PUBLIC_BASE_PATH": "/recipe-notebook/"},
        }
        service_entry = {
            **static_entry,
            "port": 52700,
            "startCommand": ["/usr/bin/env", "node", "old-server.mjs", "--port", "52700"],
        }

        def configure(entry, *, kind="service", app_id="recipe-notebook", route="/recipe-notebook"):
            self._write_generated_repository(
                self.repository,
                kind=kind,
                start_command=(
                    ["/usr/bin/env", "node", "server.mjs", "--port", "{port}"]
                    if kind == "service"
                    else None
                ),
            )
            if app_id != "recipe-notebook" or route != "/recipe-notebook":
                self._write_manifest(
                    self.repository,
                    app_id=app_id,
                    route=route,
                    kind=kind,
                    start_command=(
                        ["/usr/bin/env", "node", "server.mjs", "--port", "{port}"]
                        if kind == "service"
                        else None
                    ),
                )
            self._commit_changes(self.repository)
            self.registry_payload["apps"] = [
                deepcopy(self.registry_payload["apps"][0]),
                deepcopy(entry),
            ]
            self._write_registry(self.registry_payload)

        cases = (
            (
                "same id different repository",
                {**static_entry, "repository": str(alternate_repository.resolve())},
                {},
            ),
            (
                "same repository different id",
                {**static_entry, "id": "other-notebook"},
                {},
            ),
            (
                "changed service argv",
                service_entry,
                {},
            ),
            (
                "service to static conversion",
                service_entry,
                {"kind": "static"},
            ),
        )
        for label, entry, options in cases:
            with self.subTest(label=label):
                configure(entry, **options)
                before = self.registry.read_bytes()

                with self.assertRaisesRegex(AppRegistrationError, "application conflicts with host registry"):
                    self.registrar.plan(
                        self.repository,
                        self.registry,
                        port=None if options.get("kind") == "static" else 52700,
                    )

                self.assertEqual(self.registry.read_bytes(), before)

    def test_rejects_duplicate_matches_and_conflicting_route_or_port_without_mutation(self):
        """Ambiguous host entries and unrelated route or port use must not be rewritten."""
        static_entry = {
            "id": "recipe-notebook",
            "repository": str(self.repository.resolve()),
            "autoDeploy": True,
            "environment": {"VITE_PUBLIC_BASE_PATH": "/recipe-notebook/"},
        }
        self._write_generated_repository(
            self.repository,
            kind="service",
            start_command=["/usr/bin/env", "node", "server.mjs", "--port", "{port}"],
        )
        self._commit_changes(self.repository)

        cases = (
            (
                "duplicate matches",
                [
                    {
                        **deepcopy(self.registry_payload["apps"][0]),
                        "id": "recipe-notebook",
                    },
                    {**static_entry, "id": "recipe-notebook-alias"},
                ],
                "application conflicts with host registry",
            ),
            (
                "conflicting route",
                [deepcopy(self.registry_payload["apps"][0]), static_entry],
                "application conflicts with host registry",
            ),
            (
                "conflicting port",
                [
                    {
                        **self.registry_payload["apps"][0],
                        "port": 52700,
                        "startCommand": ["/usr/bin/true"],
                    },
                    static_entry,
                ],
                "application conflicts with host registry",
            ),
        )
        for label, apps, message in cases:
            with self.subTest(label=label):
                if label == "conflicting route":
                    self._write_manifest(
                        self.existing_repository,
                        app_id="existing-app",
                        route="/recipe-notebook",
                        platform=False,
                    )
                    self._commit_changes(self.existing_repository)
                self.registry_payload["apps"] = apps
                self._write_registry(self.registry_payload)
                before = self.registry.read_bytes()

                with self.assertRaisesRegex(AppRegistrationError, message):
                    self.registrar.plan(self.repository, self.registry, port=52700)

                self.assertEqual(self.registry.read_bytes(), before)

    def test_registers_existing_module_service_through_its_verified_virtualenv(self):
        self._write_generated_repository(self.repository, kind="service")
        python = self.repository / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text("#!/bin/sh\n")
        python.chmod(0o755)
        self._commit_changes(self.repository)

        self.registrar.register(
            self.repository,
            self.registry,
            profile_store=self.profile_store,
            port=52701,
        )

        registered = json.loads(self.registry.read_text())["apps"][-1]
        self.assertEqual(
            registered["startCommand"],
            [str(python.resolve()), "-m", "private_service", "--port", "52701"],
        )

    def test_service_requires_port_and_static_rejects_one_without_mutation(self):
        self._write_generated_repository(
            self.repository,
            kind="service",
            start_command=["/usr/bin/env", "node", "server.mjs", "--port", "{port}"],
        )
        self._commit_changes(self.repository)
        self.assertEqual(
            self._assert_rejected_without_mutation(),
            "service registration requires an explicit port",
        )

        self._write_generated_repository(self.repository)
        self._commit_changes(self.repository)
        before = self.registry.read_bytes()
        with self.assertRaisesRegex(AppRegistrationError, "static registration cannot declare a port"):
            self.registrar.register(
                self.repository,
                self.registry,
                profile_store=self.profile_store,
                port=52700,
            )
        self.assertEqual(self.registry.read_bytes(), before)

    def test_service_port_conflict_is_rejected_without_mutation(self):
        self.registry_payload["apps"][0].update(
            {
                "port": 52700,
                "startCommand": ["/usr/bin/true"],
            }
        )
        self._write_manifest(self.existing_repository, kind="service", platform=False)
        self._commit_changes(self.existing_repository)
        self._write_registry(self.registry_payload)
        self._write_generated_repository(
            self.repository,
            kind="service",
            start_command=["/usr/bin/env", "node", "server.mjs", "--port", "{port}"],
        )
        self._commit_changes(self.repository)

        before = self.registry.read_bytes()
        with self.assertRaisesRegex(AppRegistrationError, "application conflicts with host registry"):
            self.registrar.register(
                self.repository,
                self.registry,
                profile_store=self.profile_store,
                port=52700,
            )
        self.assertEqual(self.registry.read_bytes(), before)

    def test_accepts_supported_capabilities_and_additional_declared_build_inputs(self):
        self._write_generated_repository(
            self.repository,
            environment=("VITE_PUBLIC_BASE_PATH", "VITE_OPTIONAL_MODEL"),
            manifest_capabilities=("supabase",),
            provenance_capabilities=("supabase",),
        )
        self._commit_changes(self.repository)

        result = self.registrar.register(
            self.repository,
            self.registry,
            profile_store=self.profile_store,
            dry_run=True,
        )

        self.assertTrue(result.dry_run)

    def test_dry_run_performs_all_checks_without_opening_a_write_candidate(self):
        before = self.registry.read_bytes()

        with patch.object(
            HostProfileStore,
            "publish_registration",
            side_effect=AssertionError("dry run attempted profile publication"),
        ):
            result = self.registrar.register(
                self.repository,
                self.registry,
                profile_store=self.profile_store,
                dry_run=True,
            )

        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(self.doctor.repositories, [self.repository.resolve()])
        self.assertTrue(result.changed)
        self.assertTrue(result.dry_run)

    def test_dry_run_still_reads_registered_repository_manifests(self):
        (self.existing_repository / "local-web.json").unlink()
        self._commit_changes(self.existing_repository)

        self._assert_rejected_without_mutation(dry_run=True)

    def test_existing_dirty_worktree_route_is_ignored_in_favor_of_main(self):
        """A dirty route must not create a conflict the host will never deploy."""
        self._write_manifest(
            self.existing_repository,
            app_id="existing-app",
            route="/recipe-notebook",
            platform=False,
        )
        before = self.registry.read_bytes()

        result = self.registrar.register(
            self.repository,
            self.registry,
            profile_store=self.profile_store,
            dry_run=True,
        )

        self.assertEqual(result.route, "/recipe-notebook")
        self.assertEqual(self.registry.read_bytes(), before)

    def test_existing_non_main_branch_cannot_hide_a_main_route_conflict(self):
        """Registration must inspect the exact main manifest used by hosting."""
        self._write_manifest(
            self.existing_repository,
            app_id="existing-app",
            route="/recipe-notebook",
            platform=False,
        )
        self._commit_changes(self.existing_repository, "main route conflict")
        self._git(self.existing_repository, "switch", "-c", "feature-route")
        self._write_manifest(
            self.existing_repository,
            app_id="existing-app",
            route="/feature-only",
            platform=False,
        )
        self._commit_changes(self.existing_repository, "feature route divergence")

        message = self._assert_rejected_without_mutation(dry_run=True)

        self.assertEqual(message, "application conflicts with host registry")

    def test_candidate_dirty_manifest_must_agree_with_deployable_main(self):
        """A dirty candidate route must not become registry metadata."""
        self._write_manifest(self.repository, route="/dirty-private-route")

        message = self._assert_rejected_without_mutation(dry_run=True)

        self.assertEqual(message, "application is not eligible for registration")

    def test_candidate_non_main_manifest_must_agree_with_deployable_main(self):
        """A feature-branch route must not become registry metadata."""
        self._git(self.repository, "switch", "-c", "feature-route")
        self._write_manifest(self.repository, route="/feature-private-route")
        self._commit_changes(self.repository, "feature candidate divergence")

        message = self._assert_rejected_without_mutation(dry_run=True)

        self.assertEqual(message, "application is not eligible for registration")

    def test_rejects_invalid_schema_one_registry_before_mutation(self):
        payload = dict(self.registry_payload)
        payload["schemaVersion"] = 2
        registry = self._write_raw_registry(payload, "invalid-schema")
        before = registry.read_bytes()

        with self.assertRaises(AppRegistrationError) as raised:
            self.registrar.plan(self.repository, registry)

        self.assertEqual(str(raised.exception), "host registry is invalid")
        self.assertEqual(registry.read_bytes(), before)
        self.assertEqual(self.doctor.repositories, [])

    def test_rejects_id_repository_and_manifest_route_conflicts(self):
        cases = ("id", "repository", "route")
        baseline = deepcopy(self.registry_payload)
        for conflict in cases:
            with self.subTest(conflict=conflict):
                self.registry_payload = deepcopy(baseline)
                self._write_manifest(
                    self.existing_repository,
                    app_id="existing-app",
                    route="/existing-app",
                    platform=False,
                )
                if conflict == "id":
                    self.registry_payload["apps"][0]["id"] = "recipe-notebook"
                elif conflict == "repository":
                    self.registry_payload["apps"][0]["repository"] = str(
                        self.repository
                    )
                else:
                    self._write_manifest(
                        self.existing_repository,
                        app_id="existing-app",
                        route="/recipe-notebook",
                        platform=False,
                    )
                    self._commit_changes(self.existing_repository)
                self._write_registry(self.registry_payload)

                message = self._assert_rejected_without_mutation()

                self.assertEqual(message, "application conflicts with host registry")

    def test_rejects_existing_duplicate_resolved_repositories(self):
        duplicate = dict(self.registry_payload["apps"][0])
        duplicate["id"] = "existing-alias"
        duplicate["repository"] = str(self.existing_repository / ".." / "existing-app")
        self.registry_payload["apps"].append(duplicate)
        registry = self._write_raw_registry(
            self.registry_payload, "duplicate-repositories"
        )
        before = registry.read_bytes()

        with self.assertRaises(AppRegistrationError) as raised:
            self.registrar.plan(self.repository, registry)

        self.assertEqual(
            str(raised.exception), "host registry has conflicting repositories"
        )
        self.assertEqual(registry.read_bytes(), before)

    def test_rejects_every_ineligible_generated_app_before_mutation(self):
        diagnostic = Diagnostic(
            "git.managed-files-dirty", "error", "PRIVATE DIAGNOSTIC", "PRIVATE REMEDY"
        )
        cases = (
            (
                "doctor diagnostics",
                lambda: AppRegistrar(
                    doctor=FixedDoctor(
                        DoctorReport("recipe-notebook", False, (diagnostic,))
                    )
                ),
            ),
            (
                "missing provenance",
                lambda: (self.repository / ".local-web-platform.json").unlink(),
            ),
            (
                "manifest provenance disagreement",
                lambda: self._write_generated_repository(
                    self.repository,
                    manifest_template_version=2,
                    provenance_template_version=1,
                ),
            ),
        )
        for label, arrange in cases:
            with self.subTest(label=label):
                self._write_generated_repository(self.repository)
                arranged = arrange()
                registrar = arranged if isinstance(arranged, AppRegistrar) else None
                self._commit_changes(self.repository)

                message = self._assert_rejected_without_mutation(registrar)

                self.assertEqual(message, "application is not eligible for registration")

    def test_write_failure_preserves_exact_registry_bytes_and_removes_candidate(self):
        before = self.registry.read_bytes()
        history_before = tuple(
            (path.name, path.read_bytes())
            for path in sorted(self.profile_paths.history.iterdir())
        )

        with patch(
            "local_web_server.host_profile_store.os.replace",
            side_effect=OSError("PRIVATE WRITE FAILURE"),
        ):
            message = self._assert_rejected_without_mutation()

        self.assertEqual(message, "application registration failed")
        self.assertEqual(self.registry.read_bytes(), before)
        self.assertEqual(
            tuple(
                (path.name, path.read_bytes())
                for path in sorted(self.profile_paths.history.iterdir())
            ),
            history_before,
        )
        self.assertFalse(self.profile_paths.transaction.exists())
        self.assertFalse(self.profile_paths.transaction_temporary.exists())


class _FixtureRegistrationPlanner(AppRegistrar):
    """Exercise the real publication seam without app-specific validation."""

    def __init__(self):
        self.plan_calls = 0

    def plan(self, repository, registry_path, *, port=None):
        self.plan_calls += 1
        registry_path = Path(registry_path)
        before = registry_path.read_bytes()
        payload = json.loads(before)
        entry = {
            "id": "fixture-app",
            "repository": str(Path(repository).resolve()),
            "autoDeploy": True,
        }
        existing = next(
            (app for app in payload["apps"] if app["id"] == "fixture-app"),
            None,
        )
        changed = existing != entry
        if changed:
            payload["apps"].append(entry)
            candidate = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        else:
            candidate = before
        return AppRegistrationPlan(
            "fixture-app",
            "/fixture-app",
            "static",
            port,
            registry_path,
            changed,
            before,
            candidate,
        )


class AppRegistrationRevisionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.platform = self.root / "local-web-server"
        self.app = self.root / "fixture-app"
        self.platform.mkdir()
        self.app.mkdir()
        self.registry_bytes = (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "host": "fixture.local",
                    "runtimeRoot": str(self.root / "runtime"),
                    "apps": [],
                },
                indent=2,
            )
            + "\n"
        ).encode("utf-8")
        init_git_repo(
            self.platform,
            {
                ".gitignore": "config/local/\n",
                "config/apps.example.json": self.registry_bytes.decode("utf-8"),
                "tracked.txt": "clean\n",
            },
        )
        self.paths = HostProfilePaths.for_repository(self.platform)
        self.store = HostProfileStore(self.paths)
        self.store.initialise(self.registry_bytes)
        self.registrar = _FixtureRegistrationPlanner()

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _git(repository: Path, *arguments: str) -> None:
        subprocess.run(
            ("git", *arguments),
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        )

    def _profile_state(self):
        return (
            self.paths.profile.read_bytes(),
            tuple(
                (path.name, path.read_bytes())
                for path in sorted(self.paths.history.iterdir())
            ),
        )

    def _assert_apply_rejected_without_profile_mutation(self):
        before = self._profile_state()
        with self.assertRaisesRegex(
            AppRegistrationError, "^application registration failed$"
        ) as raised:
            self.registrar.register(
                self.app,
                self.paths.profile,
                profile_store=self.store,
            )
        self.assertEqual(self._profile_state(), before)
        self.assertNotIn(str(self.root), str(raised.exception))
        self.assertEqual(self.registrar.plan_calls, 0)

    def test_apply_publishes_one_registration_revision_and_noop_retry_publishes_none(self):
        initial_revisions = self.store.revisions()

        first = self.registrar.register(
            self.app,
            self.paths.profile,
            profile_store=self.store,
        )
        after_first = self.store.revisions()
        second = self.registrar.register(
            self.app,
            self.paths.profile,
            profile_store=self.store,
        )

        self.assertTrue(first.changed)
        self.assertRegex(first.registry_revision or "", r"^[0-9a-f]{64}$")
        self.assertEqual(len(after_first), len(initial_revisions) + 1)
        self.assertEqual(after_first[-1].revision_id, first.registry_revision)
        self.assertEqual(after_first[-1].operation, "registration")
        self.assertEqual(after_first[-1].app_id, "fixture-app")
        self.assertFalse(second.changed)
        self.assertIsNone(second.registry_revision)
        self.assertEqual(self.store.revisions(), after_first)

    def test_dry_run_prepares_exact_bytes_without_mutating_profile_or_history(self):
        before = self._profile_state()

        result = self.registrar.register(
            self.app,
            self.paths.profile,
            profile_store=self.store,
            dry_run=True,
        )

        self.assertTrue(result.changed)
        self.assertTrue(result.dry_run)
        self.assertIsNone(result.registry_revision)
        self.assertEqual(self._profile_state(), before)

    def test_dirty_platform_apply_fails_before_profile_mutation(self):
        source = self.platform / "local_web_server/private.py"
        source.parent.mkdir()
        source.write_text("PRIVATE = True\n", encoding="utf-8")

        self._assert_apply_rejected_without_profile_mutation()

    def test_non_main_platform_apply_fails_before_profile_mutation(self):
        self._git(self.platform, "switch", "-c", "fixture-branch")

        self._assert_apply_rejected_without_profile_mutation()

    def test_incomplete_transaction_apply_fails_before_profile_mutation(self):
        self.paths.transaction.write_bytes(b"{}\n")
        self.paths.transaction.chmod(0o600)

        self._assert_apply_rejected_without_profile_mutation()


if __name__ == "__main__":
    unittest.main()
