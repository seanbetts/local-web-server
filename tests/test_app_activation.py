import json
import threading
import subprocess
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from local_web_server.app_activation import (
    AppActivationError,
    AppActivator,
    verify_served_index_tile,
)
from local_web_server.config import load_registry
from local_web_server.app_registration import AppRegistrationPlan
from local_web_server.deploy import DeploymentResult
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore, HostProfileStoreError
from local_web_server.icons import canonical_manifest_icon
from tests.helpers import init_git_repo
from tests.redirect_fixture import RedirectServer


class FixtureRegistrar:
    def __init__(self, registry: Path):
        self.registry = registry

    def plan(self, repository, registry_path, *, port=None):
        payload = json.loads(self.registry.read_text())
        app_id = Path(repository).name
        existing = next((app for app in payload["apps"] if app["id"] == app_id), None)
        entry = {
            "id": app_id,
            "repository": str(Path(repository).resolve()),
            "autoDeploy": True,
            "environment": {"VITE_PUBLIC_BASE_PATH": f"/{app_id}/"},
        }
        if port is not None:
            entry["port"] = port
            entry["startCommand"] = ["/usr/bin/true", str(port)]
        if existing == entry:
            candidate = self.registry.read_bytes()
            changed = False
        elif (
            existing is not None
            and port is not None
            and "port" not in existing
            and "startCommand" not in existing
        ):
            payload["apps"][payload["apps"].index(existing)] = entry
            candidate = (json.dumps(payload, indent=2) + "\n").encode()
            changed = True
        elif existing is not None:
            raise ValueError("fixture conflict")
        else:
            payload["apps"].append(entry)
            candidate = (json.dumps(payload, indent=2) + "\n").encode()
            changed = True
        manifest = json.loads((Path(repository) / "local-web.json").read_text())
        return AppRegistrationPlan(
            app_id,
            manifest["route"],
            manifest["kind"],
            port,
            Path(registry_path),
            changed,
            self.registry.read_bytes(),
            candidate,
        )

    def register(self, repository, registry_path, *, dry_run=False, port=None):
        raise AssertionError("the registrar must not publish host-profile bytes")


class RecordingProfileStore:
    def __init__(self, real: HostProfileStore, *, fail: str | None = None, events=None):
        self.real = real
        self.fail = fail
        self.events = events
        self.clean_calls: list[bool] = []
        self.registration_calls = 0

    def require_clean(self, *, require_main: bool = True):
        self.clean_calls.append(require_main)
        return self.real.require_clean(require_main=require_main)

    def read_current(self):
        return self.real.read_current()

    def publish_registration(self, app_id, before, after):
        self.registration_calls += 1
        if self.events is not None:
            self.events.append("publish registration revision")
        if self.fail == "publication-before":
            raise HostProfileStoreError("PRIVATE PROFILE DETAIL")
        revision = self.real.publish_registration(app_id, before, after)
        if self.fail == "publication-after":
            raise HostProfileStoreError("PRIVATE PROFILE DETAIL")
        return revision


class FirstPort:
    def select(self, registry, *, excluded=frozenset()):
        return next(port for port in range(52000, 52010) if port not in excluded)


class SequencedPorts:
    def __init__(self, *ports):
        self.ports = iter(ports)
        self.calls = 0

    def select(self, registry, *, excluded=frozenset()):
        self.calls += 1
        return next(self.ports)


class RecordingInstallation:
    def __init__(self, platform, registry, *, fail=False, events=None):
        self.platform = platform
        self.registry = registry
        self.fail = fail
        self.calls = []
        self.events = events

    def __call__(self, repository, *, dry_run, recover_missing_caddy=False):
        if self.events is not None:
            self.events.append("install")
        self.calls.append(
            (Path(repository), dry_run, recover_missing_caddy)
        )
        if self.fail:
            raise RuntimeError("PRIVATE INSTALL DETAIL")
        registry = json.loads(self.registry.read_text())
        runtime = Path(registry["runtimeRoot"])
        destination = runtime / "platform/index/registry-v1.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        apps = []
        for host in registry["apps"]:
            manifest = json.loads((Path(host["repository"]) / "local-web.json").read_text())
            apps.append(
                {
                    "id": manifest["id"],
                    "title": manifest["title"],
                    "route": f'{manifest["route"]}/',
                    "icon": canonical_manifest_icon(manifest["home"]["icon"]),
                    "accent": manifest["home"]["accent"],
                    "frontendHealthPath": f'{manifest["route"]}/',
                    "backendHealthPath": None,
                }
            )
        destination.write_text(json.dumps({"schemaVersion": 1, "apps": apps}))
        return object()


class RecordingDeployer:
    def __init__(self, *, fail=False, events=None):
        self.fail = fail
        self.calls = []
        self.events = events

    def deploy(self, app_id, *, install=None):
        if self.events is not None:
            self.events.append("deploy")
        self.calls.append(app_id)
        if install is not None:
            install()
        if self.fail:
            raise RuntimeError("PRIVATE DEPLOY DETAIL")
        return DeploymentResult(app_id, "a" * 40, "deployed", "deployed", None)


def verify_installed_index_tile(repository, registry, app_id):
    manifest = json.loads((Path(repository) / "local-web.json").read_text())
    payload = json.loads(
        (
            Path(registry.runtime_root)
            / "platform/index/registry-v1.json"
        ).read_text()
    )
    expected = {
        "id": app_id,
        "title": manifest["title"],
        "route": f'{manifest["route"]}/',
        "icon": canonical_manifest_icon(manifest["home"]["icon"]),
        "accent": manifest["home"]["accent"],
    }
    matches = [app for app in payload["apps"] if app.get("id") == app_id]
    if len(matches) != 1 or any(
        matches[0].get(key) != value for key, value in expected.items()
    ):
        raise ValueError


class AppActivationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.platform = self.root / "local-web-server"
        self.app = self.root / "recipe-notebook"
        self.app.mkdir()
        (self.app / "local-web.json").write_text(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "id": "recipe-notebook",
                    "title": "Recipe Notebook",
                    "route": "/recipe-notebook",
                    "kind": "static",
                    "build": {
                        "commands": [["true"]],
                        "output": "dist",
                        "environment": ["VITE_PUBLIC_BASE_PATH"],
                    },
                    "healthPath": "/recipe-notebook/",
                    "home": {"icon": "book", "accent": "#8EA7C6"},
                }
            )
        )
        self.runtime = self.root / "runtime"
        registry = {
            "schemaVersion": 1,
            "host": "system-index.local",
            "runtimeRoot": str(self.runtime),
            "apps": [],
        }
        self.registry_bytes = (json.dumps(registry, indent=2) + "\n").encode()
        self.platform.mkdir()
        init_git_repo(
            self.platform,
            {
                ".gitignore": "config/local/\n",
                "config/apps.example.json": self.registry_bytes.decode(),
                "unrelated.txt": "base\n",
            },
        )
        self.profile_paths = HostProfilePaths.for_repository(self.platform)
        self.profile_store = HostProfileStore(self.profile_paths)
        self.profile_store.initialise(self.registry_bytes)
        self.registry = self.profile_paths.profile

    def tearDown(self):
        self.temporary.cleanup()

    def activator(
        self,
        *,
        deploy_fails=False,
        install_fails=False,
        allocator=None,
        profile_store=None,
    ):
        installation = RecordingInstallation(
            self.platform, self.registry, fail=install_fails
        )
        deployer = RecordingDeployer(fail=deploy_fails)
        profile_store = profile_store or RecordingProfileStore(self.profile_store)
        activator = AppActivator(
            platform_repository=self.platform,
            registry_path=self.registry,
            registrar=FixtureRegistrar(self.registry),
            allocator=allocator or FirstPort(),
            profile_store=profile_store,
            install=installation,
            deployer_factory=lambda _registry: deployer,
            verify=verify_installed_index_tile,
        )
        return activator, installation, deployer

    def make_service(self):
        manifest_path = self.app / "local-web.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["kind"] = "service"
        manifest["healthPath"] = "/recipe-notebook/healthz"
        manifest["service"] = {
            "module": "service",
            "internalHealthPath": "/healthz",
        }
        manifest_path.write_text(json.dumps(manifest))

    def git(self, *arguments):
        return subprocess.run(
            ("git", *arguments), cwd=self.platform, check=True,
            capture_output=True, text=True,
        ).stdout

    def test_preview_is_read_only_and_lists_the_complete_stage_contract(self):
        activator, installation, deployer = self.activator()
        head = self.git("rev-parse", "HEAD")

        plan = activator.preview(self.app)

        self.assertEqual(plan.app_id, "recipe-notebook")
        self.assertTrue(plan.registry_changed)
        self.assertIsNone(plan.port)
        self.assertEqual(
            plan.stages,
            ("validate", "register", "install", "deploy", "verify"),
        )
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(self.git("rev-parse", "HEAD"), head)
        self.assertEqual(installation.calls, [])
        self.assertEqual(deployer.calls, [])

    def test_feature_checkout_can_preview_but_cannot_publish_profile_revision(self):
        self.git("switch", "-c", "feature/preview")
        activator, _installation, _deployer = self.activator()

        plan = activator.preview(self.app)

        self.assertTrue(plan.registry_changed)
        with self.assertRaisesRegex(AppActivationError, "registration failed"):
            activator.activate(self.app)
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)

    def test_registry_read_race_is_sanitised_as_a_registration_failure(self):
        outer = self

        class RegistryRaceActivator(AppActivator):
            def preview(inner_self, repository):
                plan = super().preview(repository)
                outer.registry.unlink()
                return plan

        activator = RegistryRaceActivator(
            platform_repository=self.platform,
            registry_path=self.registry,
            registrar=FixtureRegistrar(self.registry),
            allocator=FirstPort(),
            profile_store=RecordingProfileStore(self.profile_store),
            install=RecordingInstallation(self.platform, self.registry),
            deployer_factory=lambda _registry: RecordingDeployer(),
            verify=verify_installed_index_tile,
        )

        with self.assertRaisesRegex(AppActivationError, "registration failed") as raised:
            activator.activate(self.app)

        self.assertNotIn(str(self.registry), str(raised.exception))

    def test_apply_publishes_registration_revision_installs_deploys_and_verifies_tile(self):
        activator, installation, deployer = self.activator()
        framework_head = self.git("rev-parse", "HEAD")

        result = activator.activate(self.app)

        self.assertTrue(result.verified)
        self.assertRegex(result.registry_revision or "", r"^[0-9a-f]{64}$")
        self.assertEqual(installation.calls, [(self.platform, False, True)])
        self.assertEqual(deployer.calls, ["recipe-notebook"])
        self.assertEqual(self.git("rev-parse", "HEAD"), framework_head)
        self.assertEqual(
            [revision.operation for revision in self.profile_store.revisions()],
            ["initialisation", "registration"],
        )

    def test_registration_revision_is_published_before_install_and_deploy(self):
        events: list[str] = []
        store = RecordingProfileStore(self.profile_store, events=events)
        installation = RecordingInstallation(
            self.platform, self.registry, events=events
        )
        deployer = RecordingDeployer(events=events)
        activator = AppActivator(
            platform_repository=self.platform,
            registry_path=self.registry,
            registrar=FixtureRegistrar(self.registry),
            allocator=FirstPort(),
            profile_store=store,
            install=installation,
            deployer_factory=lambda _registry: deployer,
            verify=verify_installed_index_tile,
        )

        result = activator.activate(self.app)

        self.assertTrue(result.verified)
        self.assertEqual(
            events,
            ["publish registration revision", "deploy", "install"],
        )

    def test_apply_delegates_installation_to_the_deployment_transaction(self):
        """Calling installation outside deploy reintroduces the release/start race."""
        events: list[str] = []
        installation = RecordingInstallation(
            self.platform, self.registry, events=events
        )
        deployer = RecordingDeployer(events=events)
        activator = AppActivator(
            platform_repository=self.platform,
            registry_path=self.registry,
            registrar=FixtureRegistrar(self.registry),
            allocator=FirstPort(),
            profile_store=RecordingProfileStore(self.profile_store),
            install=installation,
            deployer_factory=lambda _registry: deployer,
            verify=verify_installed_index_tile,
        )

        result = activator.activate(self.app)

        self.assertTrue(result.verified)
        self.assertEqual(events, ["deploy", "install"])

    def test_static_registration_transitions_to_a_service_without_preview_writes(self):
        activator, _installation, _deployer = self.activator()
        static_result = activator.activate(self.app)
        self.assertTrue(static_result.verified)

        self.make_service()
        before_preview = self.registry.read_bytes()
        plan = activator.preview(self.app)

        self.assertEqual(plan.kind, "service")
        self.assertEqual(plan.port, 52000)
        self.assertTrue(plan.registry_changed)
        self.assertEqual(self.registry.read_bytes(), before_preview)

        result = activator.activate(self.app)

        registered = json.loads(self.registry.read_text())["apps"][0]
        self.assertEqual(registered["port"], result.plan.port)
        self.assertIn("startCommand", registered)

    def test_stable_service_port_is_consistent_between_preview_and_apply(self):
        self.make_service()
        activator, _installation, _deployer = self.activator()

        preview = activator.preview(self.app)
        result = activator.activate(self.app)

        self.assertEqual(preview.port, 52000)
        self.assertEqual(result.plan.port, preview.port)
        registered = json.loads(self.registry.read_text())["apps"][0]
        self.assertEqual(registered["port"], preview.port)

    def test_service_apply_rechecks_and_stabilises_a_racing_port_before_publication(self):
        self.make_service()
        ports = SequencedPorts(52000, 52001, 52001)
        activator, _installation, _deployer = self.activator(allocator=ports)

        result = activator.activate(self.app)

        self.assertEqual(result.plan.port, 52001)
        self.assertEqual(ports.calls, 3)
        registered = json.loads(self.registry.read_text())["apps"][0]
        self.assertEqual(registered["port"], 52001)

    def test_apply_expected_plan_rejects_a_port_race_before_registry_publication(self):
        self.make_service()
        ports = SequencedPorts(52000, 52001)
        activator, installation, deployer = self.activator(allocator=ports)
        expected = activator.preview(self.app)

        with self.assertRaisesRegex(AppActivationError, "validation failed"):
            activator.activate(self.app, expected_plan=expected)

        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(installation.calls, [])
        self.assertEqual(deployer.calls, [])

    def test_service_port_churn_is_bounded_before_registry_publication(self):
        self.make_service()
        ports = SequencedPorts(52000, 52001, 52002, 52003)
        activator, installation, deployer = self.activator(allocator=ports)

        with self.assertRaisesRegex(AppActivationError, "registration failed"):
            activator.activate(self.app)

        self.assertEqual(ports.calls, 4)
        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(installation.calls, [])
        self.assertEqual(deployer.calls, [])

    def test_exact_second_service_activation_reuses_the_registered_port(self):
        self.make_service()
        activator, _installation, _deployer = self.activator()
        first = activator.activate(self.app)

        exact, _installation, _deployer = self.activator()
        preview = exact.preview(self.app)
        second = exact.activate(self.app)

        self.assertFalse(preview.registry_changed)
        self.assertEqual(preview.port, first.plan.port)
        self.assertFalse(second.plan.registry_changed)
        self.assertEqual(second.plan.port, first.plan.port)
        self.assertIsNone(second.registry_revision)

    def test_expected_exact_activation_uses_locked_registry_and_live_expectations(self):
        initial, _installation, _deployer = self.activator()
        initial.activate(self.app)
        registry_before = self.registry.read_bytes()

        class MutatingRegistrar(FixtureRegistrar):
            def __init__(inner_self, registry):
                super().__init__(registry)
                inner_self.register_calls = 0

            def register(inner_self, *args, **kwargs):
                inner_self.register_calls += 1
                inner_self.registry.write_bytes(b"private registrar mutation\n")
                return super().register(*args, **kwargs)

        class LockedExpectationDeployer:
            def __init__(inner_self):
                inner_self.calls = []

            def deploy(
                inner_self,
                app_id,
                *,
                install=None,
                expected_commit=None,
                expected_current_commit=None,
                validate_locked=None,
            ):
                inner_self.calls.append(
                    (app_id, expected_commit, expected_current_commit)
                )
                if validate_locked is None:
                    raise AssertionError("locked registry validation was omitted")
                validate_locked()
                if install is not None:
                    install()
                return DeploymentResult(
                    app_id, expected_commit, "deployed", "deployed", None
                )

        registrar = MutatingRegistrar(self.registry)
        deployer = LockedExpectationDeployer()
        activation = AppActivator(
            platform_repository=self.platform,
            registry_path=self.registry,
            registrar=registrar,
            allocator=FirstPort(),
            profile_store=RecordingProfileStore(self.profile_store),
            install=RecordingInstallation(self.platform, self.registry),
            deployer_factory=lambda _registry: deployer,
            verify=verify_installed_index_tile,
        )
        expected = activation.preview(self.app)

        result = activation.activate(
            self.app,
            expected_plan=expected,
            expected_source_commit="a" * 40,
            expected_former_live_commit="b" * 40,
        )

        self.assertTrue(result.verified)
        self.assertEqual(registrar.register_calls, 0)
        self.assertEqual(self.registry.read_bytes(), registry_before)
        self.assertEqual(self.git("status", "--short"), "")
        self.assertEqual(
            deployer.calls,
            [("recipe-notebook", "a" * 40, "b" * 40)],
        )

    def test_expected_activation_rejects_wrong_deployment_identity(self):
        initial, _installation, _deployer = self.activator()
        initial.activate(self.app)

        for field in ("app-id", "commit"):
            with self.subTest(field=field):
                class WrongIdentityDeployer:
                    def deploy(
                        inner_self,
                        app_id,
                        *,
                        install=None,
                        expected_commit=None,
                        expected_current_commit=None,
                        validate_locked=None,
                    ):
                        if validate_locked is not None:
                            validate_locked()
                        return DeploymentResult(
                            "private-wrong-app" if field == "app-id" else app_id,
                            "c" * 40 if field == "commit" else expected_commit,
                            "deployed",
                            "deployed",
                            None,
                        )

                activation = AppActivator(
                    platform_repository=self.platform,
                    registry_path=self.registry,
                    registrar=FixtureRegistrar(self.registry),
                    profile_store=RecordingProfileStore(self.profile_store),
                    install=RecordingInstallation(self.platform, self.registry),
                    deployer_factory=lambda _registry: WrongIdentityDeployer(),
                    verify=verify_installed_index_tile,
                )
                expected = activation.preview(self.app)

                with self.assertRaisesRegex(
                    AppActivationError, "deployment failed"
                ):
                    activation.activate(
                        self.app,
                        expected_plan=expected,
                        expected_source_commit="a" * 40,
                        expected_former_live_commit="b" * 40,
                    )

    def test_service_activation_rejects_same_id_from_a_different_repository(self):
        other = self.root / "other-recipe-notebook"
        other.mkdir()
        registry = json.loads(self.registry.read_text())
        registry["apps"].append(
            {
                "id": "recipe-notebook",
                "repository": str(other.resolve()),
                "autoDeploy": True,
                "environment": {"VITE_PUBLIC_BASE_PATH": "/recipe-notebook/"},
            }
        )
        self.registry.write_text(json.dumps(registry, indent=2) + "\n")
        self.make_service()
        activator, installation, deployer = self.activator()

        with self.assertRaisesRegex(AppActivationError, "validation failed"):
            activator.preview(self.app)

        self.assertEqual(installation.calls, [])
        self.assertEqual(deployer.calls, [])

    def test_failed_deployment_leaves_registration_revision_for_retry(self):
        failing, _installation, _deployer = self.activator(deploy_fails=True)

        with self.assertRaisesRegex(AppActivationError, "deployment failed") as raised:
            failing.activate(self.app)

        self.assertNotIn("PRIVATE", str(raised.exception))
        self.assertIn("recipe-notebook", self.registry.read_text())
        self.assertEqual(self.git("status", "--short"), "")
        checkpoint = self.profile_store.revisions()
        self.assertEqual(len(checkpoint), 2)
        self.assertEqual(checkpoint[-1].operation, "registration")

        recovered, _installation, _deployer = self.activator()
        result = recovered.activate(self.app)
        self.assertTrue(result.verified)
        self.assertIsNone(result.registry_revision)
        self.assertEqual(self.profile_store.revisions(), checkpoint)

    def test_failed_install_leaves_registration_revision_for_retry(self):
        failing, _installation, _deployer = self.activator(install_fails=True)

        with self.assertRaisesRegex(AppActivationError, "installation failed"):
            failing.activate(self.app)

        checkpoint = self.profile_store.revisions()
        self.assertEqual(len(checkpoint), 2)
        self.assertEqual(checkpoint[-1].operation, "registration")
        self.assertIn("recipe-notebook", self.registry.read_text())

        recovered, _installation, _deployer = self.activator()
        result = recovered.activate(self.app)

        self.assertTrue(result.verified)
        self.assertIsNone(result.registry_revision)
        self.assertEqual(self.profile_store.revisions(), checkpoint)

    def test_prepublication_store_failure_retains_exact_profile_and_history(self):
        before_history = self.profile_store.revisions()
        failing_store = RecordingProfileStore(
            self.profile_store, fail="publication-before"
        )
        activator, installation, deployer = self.activator(
            profile_store=failing_store
        )

        with self.assertRaisesRegex(AppActivationError, "registration failed") as raised:
            activator.activate(self.app)

        self.assertEqual(self.registry.read_bytes(), self.registry_bytes)
        self.assertEqual(self.profile_store.revisions(), before_history)
        self.assertEqual(installation.calls, [])
        self.assertEqual(deployer.calls, [])
        self.assertNotIn("PRIVATE", str(raised.exception))

    def test_failed_deployment_after_transition_retries_exact_registration(self):
        initial, _installation, _deployer = self.activator()
        initial.activate(self.app)
        self.make_service()
        failing, _installation, _deployer = self.activator(deploy_fails=True)

        with self.assertRaisesRegex(AppActivationError, "deployment failed"):
            failing.activate(self.app)

        registered = json.loads(self.registry.read_text())["apps"][0]
        self.assertEqual(registered["port"], 52000)
        self.assertIn("startCommand", registered)
        self.assertEqual(self.git("status", "--short"), "")

        recovered, _installation, _deployer = self.activator()
        result = recovered.activate(self.app)

        self.assertTrue(result.verified)
        self.assertFalse(result.plan.registry_changed)
        self.assertEqual(result.plan.port, 52000)
        self.assertIsNone(result.registry_revision)

    def test_exact_reactivation_rejects_a_dirty_registry(self):
        activator, _installation, _deployer = self.activator()
        activator.activate(self.app)
        self.registry.write_text(self.registry.read_text() + "\n")
        exact, installation, deployer = self.activator()

        with self.assertRaisesRegex(AppActivationError, "validation failed"):
            exact.preview(self.app)
        with self.assertRaisesRegex(AppActivationError, "validation failed"):
            exact.activate(self.app)

        self.assertEqual(installation.calls, [])
        self.assertEqual(deployer.calls, [])

    def test_exact_reactivation_can_preview_but_cannot_apply_from_feature_branch(self):
        activator, _installation, _deployer = self.activator()
        activator.activate(self.app)
        self.git("switch", "-c", "feature/exact-reactivation")
        exact, installation, deployer = self.activator()

        plan = exact.preview(self.app)

        self.assertFalse(plan.registry_changed)
        with self.assertRaisesRegex(AppActivationError, "registration failed"):
            exact.activate(self.app)
        self.assertEqual(installation.calls, [])
        self.assertEqual(deployer.calls, [])

    def test_existing_compatible_icon_alias_verifies_against_canonical_tile(self):
        manifest_path = self.app / "local-web.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["home"]["icon"] = "shirt"
        manifest_path.write_text(json.dumps(manifest))
        activator, _installation, _deployer = self.activator()

        result = activator.activate(self.app)

        self.assertTrue(result.verified)

    def test_default_tile_verifier_reads_the_served_registry_not_runtime_disk(self):
        activator, installation, _deployer = self.activator()
        activator.activate(self.app)
        registry = load_registry(self.registry)
        installed = (
            self.runtime / "platform/index/registry-v1.json"
        ).read_bytes()

        class Handler(BaseHTTPRequestHandler):
            payload = installed

            def do_GET(inner_self):
                inner_self.send_response(200)
                inner_self.send_header("Content-Type", "application/json")
                inner_self.end_headers()
                inner_self.wfile.write(inner_self.payload)

            def log_message(inner_self, _format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        served_registry = type(registry)(
            f"127.0.0.1:{server.server_port}",
            registry.runtime_root,
            registry.apps,
        )

        verify_served_index_tile(self.app, served_registry, "recipe-notebook")

        Handler.payload = b'{"schemaVersion":1,"apps":[]}'
        with self.assertRaises(ValueError):
            verify_served_index_tile(
                self.app, served_registry, "recipe-notebook"
            )

    def test_tile_verifier_uses_tailscale_public_origin(self):
        registry = replace(
            load_registry(self.registry),
            host="local.example.ts.net",
            schema_version=2,
            public_origin="https://local.example.ts.net",
            ingress_mode="tailscale-serve",
        )
        content = json.dumps(
            {
                "schemaVersion": 1,
                "apps": [
                    {
                        "id": "recipe-notebook",
                        "title": "Recipe Notebook",
                        "route": "/recipe-notebook/",
                        "icon": "book",
                        "accent": "#8EA7C6",
                    }
                ],
            }
        ).encode()
        with RedirectServer("/finished", body=content) as server:
            verify_served_index_tile(self.app, registry, "recipe-notebook")
        self.assertEqual(
            server.requests,
            [
                ("GET", "/_local-web/platform/index/registry-v1.json", "local.example.ts.net"),
                ("GET", "/finished", "local.example.ts.net"),
            ],
        )

    def test_https_tile_cannot_verify_a_registry_redirected_outside_canonical_origin(self):
        # A valid tile on another origin must not verify this host's catalogue.
        registry = replace(
            load_registry(self.registry), host="local.example.ts.net", schema_version=2,
            public_origin="https://local.example.ts.net", ingress_mode="tailscale-serve",
        )
        content = (
            b'{"schemaVersion":1,"apps":[{"id":"recipe-notebook","title":"Recipe Notebook",'
            b'"route":"/recipe-notebook/","icon":"book","accent":"#8EA7C6"}]}'
        )
        for location in (
            "http://local.example.ts.net/finished",
            "https://attacker.example/finished",
            "https://local.example.ts.net:444/finished",
            "https://local.example.ts.net:0/finished",
            "https://user@local.example.ts.net/finished",
            "//attacker.example/finished",
        ):
            with self.subTest(location=location), RedirectServer(location, body=content) as server:
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    verify_served_index_tile(self.app, registry, "recipe-notebook")
                self.assertEqual(caught.exception.code, 302)
                caught.exception.close()
                self.assertEqual(server.requests, [
                    ("GET", "/_local-web/platform/index/registry-v1.json", "local.example.ts.net"),
                ])

    def test_tile_redirects_preserve_same_https_origin_and_version_one_behavior(self):
        content = (
            b'{"schemaVersion":1,"apps":[{"id":"recipe-notebook","title":"Recipe Notebook",'
            b'"route":"/recipe-notebook/","icon":"book","accent":"#8EA7C6"}]}'
        )
        original = load_registry(self.registry)
        for registry, location in (
            (replace(original, host="local.example.ts.net", schema_version=2,
                     public_origin="https://local.example.ts.net", ingress_mode="tailscale-serve"),
             "/finished"),
            (original, "http://legacy-other.local/finished"),
        ):
            with self.subTest(origin=registry.public_origin), RedirectServer(location, body=content):
                verify_served_index_tile(self.app, registry, "recipe-notebook")


if __name__ == "__main__":
    unittest.main()
