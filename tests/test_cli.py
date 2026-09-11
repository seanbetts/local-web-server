import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from local_web_server.deploy import DeploymentResult
from local_web_server.config import ConfigError
from local_web_server.app_activation import AppActivationPlan, AppActivationResult
from local_web_server.app_identity import (
    AccentIdentity,
    IdentityAssignment,
    IdentityReport,
    ManifestIconIdentity,
)
from local_web_server.services import HealthResult, ServiceState
from local_web_server.app_generator import CreateAppPreview, CreateAppResult
from local_web_server.app_doctor import Diagnostic, DoctorReport
from local_web_server.app_registration import AppRegistrationResult
from local_web_server.service_command_migration import (
    ServiceCommandMigrationError,
    ServiceCommandMigrationPlan,
    ServiceCommandMigrationResult,
)
from local_web_server.public_base_path_migration import (
    PublicBasePathMigrationError,
    PublicBasePathMigrationPlan,
    PublicBasePathMigrationResult,
)
from local_web_server.app_update import AppUpdateError
from local_web_server.app_update_models import (
    AppUpdatePlan,
    AppUpdateRequest,
    AppUpdateResult,
    FileChange,
)
from local_web_server.fleet_update_models import (
    FleetAppPlan,
    FleetAppResult,
    FleetUpdatePlan,
    FleetUpdateResult,
)
from local_web_server.cli import build_parser
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore
from tests.helpers import init_git_repo


_PLATFORM_REPOSITORY = Path(__file__).resolve().parents[1]
_PRIVATE_HOST_PROFILE = _PLATFORM_REPOSITORY / "config/local/apps.json"
_INVALID_PRIVATE_PROFILE_ERROR = (
    "local-web: private host profile is invalid; run local-web host restore\n"
)
_PRIVATE_HOST_PROFILE_BYTES = b"fixture-private-profile-bytes"
_REGISTRY_BACKED_COMMANDS = (
    ["deploy", "fixture"],
    ["rollback", "fixture"],
    ["theme", "rollback"],
    ["status"],
    ["app", "identity"],
    ["app", "check", "--repository", "/tmp/fixture"],
    ["app", "register", "--repository", "/tmp/fixture", "--dry-run"],
    ["app", "register", "--repository", "/tmp/fixture", "--apply"],
    ["app", "activate", "--repository", "/tmp/fixture"],
    ["app", "migrate-service-command", "--repository", "/tmp/fixture"],
    ["app", "migrate-public-base-path", "--repository", "/tmp/fixture"],
    ["apps", "update", "--dry-run"],
    ["apps", "update", "--apply"],
)


def write_private_profile(repository: Path, content: str) -> Path:
    local = repository / "config/local"
    local.mkdir(mode=0o700)
    (local / "history").mkdir(mode=0o700)
    (local / "backups").mkdir(mode=0o700)
    profile = local / "apps.json"
    profile.write_text(content, encoding="utf-8")
    profile.chmod(0o600)
    return profile


class FakeStatus:
    def __init__(
        self,
        *,
        stale=False,
        healthy=True,
        backend=True,
        caddy_state=ServiceState.RUNNING,
    ):
        self.app_id = "plotter"
        self.main_commit = "a" * 40
        self.deployed_commit = "b" * 40
        self.stale = stale
        self.service_state = None
        self.internal_health = None
        self.frontend_health = HealthResult(healthy, 200 if healthy else 503, None)
        self.backend_health = (
            HealthResult(backend, 200 if backend else 503, "private provider detail")
            if backend is not None
            else None
        )
        self.latest_log = Path("/tmp/plotter.log")
        self.caddy_state = caddy_state
        self.healthy = healthy and not stale and caddy_state is ServiceState.RUNNING


def update_plan(
    *, mode="adopt", capabilities=("supabase",), changes=None, foundation=None
):
    if changes is None:
        changes = (
            FileChange(Path(".local-web-platform.json"), None, b"provenance"),
            FileChange(Path("vendor/local-web-ui.tgz"), None, b"artifact"),
        )
    return AppUpdatePlan(
        app_id="plotter",
        mode=mode,
        current_ui_version=None,
        target_ui_version="0.5.2",
        target_ui_sha256="a" * 64,
        capabilities=capabilities,
        changes=changes,
        foundation=foundation,
        current_platform_contract_version=None,
        current_template_version=None,
        target_platform_contract_version=1 if foundation else None,
        target_template_version=1 if foundation else None,
    )


class CliTests(unittest.TestCase):
    def setUp(self):
        self.host_profile_patcher = patch(
            "local_web_server.cli.resolve_host_profile",
            return_value=_PRIVATE_HOST_PROFILE,
        )
        self.host_profile_resolver = self.host_profile_patcher.start()
        self.addCleanup(self.host_profile_patcher.stop)
        self.registry_loader_patcher = patch(
            "local_web_server.cli.load_registry_bytes", return_value=object()
        )
        self.registry_loader = self.registry_loader_patcher.start()
        self.addCleanup(self.registry_loader_patcher.stop)
        self.profile_store_patcher = patch("local_web_server.cli.HostProfileStore")
        self.profile_store = self.profile_store_patcher.start()
        self.profile_store.return_value.read_snapshot.return_value = SimpleNamespace(
            profile_bytes=_PRIVATE_HOST_PROFILE_BYTES
        )
        self.addCleanup(self.profile_store_patcher.stop)

    def test_registry_backed_command_families_fail_closed_when_profile_is_missing(self):
        self.host_profile_patcher.stop()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        platform_repository = Path(temporary.name)
        init_git_repo(
            platform_repository,
            {
                "config/apps.json": "{}\n",
                "config/apps.example.json": "{}\n",
            },
        )
        with patch(
            "local_web_server.cli._PLATFORM_REPOSITORY", platform_repository
        ):
            for argv in _REGISTRY_BACKED_COMMANDS:
                with self.subTest(argv=argv):
                    with (
                        patch(
                            "local_web_server.cli.load_registry",
                            side_effect=AssertionError("legacy registry was opened"),
                        ) as load_registry,
                        patch(
                            "local_web_server.cli.identity_report",
                            side_effect=AssertionError(
                                "identity discovery was started"
                            ),
                        ) as identity_report,
                        patch(
                            "local_web_server.cli.AppRegistrar",
                            side_effect=AssertionError("registration was started"),
                        ) as registrar,
                        patch(
                            "local_web_server.cli.AppActivator",
                            side_effect=AssertionError("activation was started"),
                        ) as activator,
                        patch(
                            "local_web_server.cli.ServiceCommandMigrator",
                            side_effect=AssertionError(
                                "service migration was started"
                            ),
                        ) as service_migrator,
                        patch(
                            "local_web_server.cli.PublicBasePathMigrator",
                            side_effect=AssertionError(
                                "base-path migration was started"
                            ),
                        ) as base_path_migrator,
                        patch(
                            "local_web_server.cli.FleetUpdater",
                            side_effect=AssertionError("fleet update was started"),
                        ) as fleet_updater,
                        patch(
                            "local_web_server.cli.AppChecker",
                            side_effect=AssertionError("app checks were started"),
                        ) as app_checker,
                    ):
                        code, stdout, stderr = self.run_main(argv)

                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertEqual(
                        stderr,
                        "local-web: private host profile is missing; run local-web "
                        "host init or local-web host restore\n",
                    )
                    load_registry.assert_not_called()
                    identity_report.assert_not_called()
                    registrar.assert_not_called()
                    activator.assert_not_called()
                    service_migrator.assert_not_called()
                    base_path_migrator.assert_not_called()
                    fleet_updater.assert_not_called()
                    app_checker.assert_not_called()

    def test_pending_host_store_stops_deploy_before_manager_construction(self):
        self.host_profile_patcher.stop()
        self.registry_loader_patcher.stop()
        self.profile_store_patcher.stop()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        platform_repository = Path(temporary.name)
        registry_bytes = (
            json.dumps(
                {
                    "schemaVersion": 1,
                    "host": "fixture.local",
                    "runtimeRoot": str(platform_repository / "runtime"),
                    "apps": [],
                }
            )
            + "\n"
        ).encode("utf-8")
        init_git_repo(
            platform_repository,
            {
                ".gitignore": "config/local/\n",
                "config/apps.example.json": "{}\n",
                "local_web_server/source.py": "# fixture\n",
            },
        )
        paths = HostProfilePaths.for_repository(platform_repository)
        HostProfileStore(paths).initialise(registry_bytes)
        paths.transaction.write_bytes(b"marker-only-private-state")
        paths.transaction.chmod(0o600)

        with (
            patch("local_web_server.cli._PLATFORM_REPOSITORY", platform_repository),
            patch(
                "local_web_server.cli.DeploymentManager",
                side_effect=AssertionError("deployment was constructed"),
            ) as deployment,
        ):
            code, stdout, stderr = self.run_main(["deploy", "fixture"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, _INVALID_PRIVATE_PROFILE_ERROR)
        deployment.assert_not_called()

    def test_registry_backed_command_families_sanitise_malformed_private_profile_before_side_effects(self):
        self.host_profile_patcher.stop()
        self.registry_loader_patcher.stop()
        self.profile_store_patcher.stop()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        platform_repository = Path(temporary.name)
        init_git_repo(
            platform_repository,
            {
                "config/apps.json": "{}\n",
                "config/apps.example.json": "{}\n",
            },
        )
        profile = write_private_profile(
            platform_repository, '{"schemaVersion": 2, "PRIVATE_MARKER": '
        )

        with patch(
            "local_web_server.cli._PLATFORM_REPOSITORY", platform_repository
        ):
            for argv in _REGISTRY_BACKED_COMMANDS:
                with self.subTest(argv=argv):
                    with (
                        patch(
                            "local_web_server.cli.identity_report",
                            side_effect=AssertionError(
                                "identity discovery was started"
                            ),
                        ) as identity_report,
                        patch(
                            "local_web_server.cli.AppRegistrar",
                            side_effect=AssertionError("registration was started"),
                        ) as registrar,
                        patch(
                            "local_web_server.cli.AppActivator",
                            side_effect=AssertionError("activation was started"),
                        ) as activator,
                        patch(
                            "local_web_server.cli.ServiceCommandMigrator",
                            side_effect=AssertionError(
                                "service migration was started"
                            ),
                        ) as service_migrator,
                        patch(
                            "local_web_server.cli.PublicBasePathMigrator",
                            side_effect=AssertionError(
                                "base-path migration was started"
                            ),
                        ) as base_path_migrator,
                        patch(
                            "local_web_server.cli.FleetUpdater",
                            side_effect=AssertionError("fleet update was started"),
                        ) as fleet_updater,
                        patch(
                            "local_web_server.cli.AppChecker",
                            side_effect=AssertionError("app checks were started"),
                        ) as app_checker,
                        patch(
                            "local_web_server.cli.DeploymentManager",
                            side_effect=AssertionError("deployment was started"),
                        ) as deployment,
                        patch(
                            "local_web_server.cli.StatusCollector",
                            side_effect=AssertionError("status collection was started"),
                        ) as status_collector,
                    ):
                        code, stdout, stderr = self.run_main(argv)

                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertEqual(stderr, _INVALID_PRIVATE_PROFILE_ERROR)
                    self.assertNotIn(str(profile), stderr)
                    self.assertNotIn("PRIVATE_MARKER", stderr)
                    identity_report.assert_not_called()
                    registrar.assert_not_called()
                    activator.assert_not_called()
                    service_migrator.assert_not_called()
                    base_path_migrator.assert_not_called()
                    fleet_updater.assert_not_called()
                    app_checker.assert_not_called()
                    deployment.assert_not_called()
                    status_collector.assert_not_called()

    def test_semantically_invalid_private_profile_uses_the_same_bounded_error(self):
        self.host_profile_patcher.stop()
        self.registry_loader_patcher.stop()
        self.profile_store_patcher.stop()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        platform_repository = Path(temporary.name)
        init_git_repo(platform_repository, {"config/apps.example.json": "{}\n"})
        profile = write_private_profile(
            platform_repository,
            json.dumps(
                {
                    "schemaVersion": 2,
                    "publicOrigin": "https://local.example.ts.net",
                    "ingressMode": "tailscale-serve",
                    "runtimeRoot": str(platform_repository / "runtime"),
                    "apps": [],
                    "PRIVATE_VALIDATION_MARKER": True,
                }
            )
            + "\n",
        )

        with patch(
            "local_web_server.cli._PLATFORM_REPOSITORY", platform_repository
        ):
            code, stdout, stderr = self.run_main(["status"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, _INVALID_PRIVATE_PROFILE_ERROR)
        self.assertNotIn(str(profile), stderr)
        self.assertNotIn("PRIVATE_VALIDATION_MARKER", stderr)

    def test_invalid_utf8_private_profile_is_bounded_before_app_check_side_effects(self):
        self.host_profile_patcher.stop()
        self.registry_loader_patcher.stop()
        self.profile_store_patcher.stop()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        platform_repository = Path(temporary.name)
        init_git_repo(platform_repository, {"config/apps.example.json": "{}\n"})
        profile = write_private_profile(platform_repository, "{}\n")
        profile.write_bytes(
            b'{"schemaVersion":2}\xffPRIVATE_INVALID_BYTE_MARKER'
        )
        profile.chmod(0o600)

        with (
            patch(
                "local_web_server.cli._PLATFORM_REPOSITORY", platform_repository
            ),
            patch(
                "local_web_server.cli.AppChecker",
                side_effect=AssertionError("app checks were started"),
            ) as app_checker,
        ):
            code, stdout, stderr = self.run_main(
                ["app", "check", "--repository", "/tmp/fixture"]
            )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, _INVALID_PRIVATE_PROFILE_ERROR)
        self.assertNotIn(str(profile), stderr)
        self.assertNotIn("PRIVATE_INVALID_BYTE_MARKER", stderr)
        app_checker.assert_not_called()

    def test_host_lifecycle_parser_requires_explicit_inputs_and_apply(self):
        cases = (
            (["host", "init", "--from", "prepared.json"], "init", False),
            (["host", "migrate-registry", "--platform-repository", ".", "--apply"], "migrate-registry", True),
            (["host", "restore", "--from", "backup.json", "--replace"], "restore", False),
            (["host", "recover"], "recover", False),
        )
        for argv, command, apply in cases:
            args = build_parser().parse_args(argv)
            self.assertEqual(args.host_command, command)
            self.assertEqual(args.apply, apply)

    def test_host_parse_failures_never_echo_caller_arguments(self):
        for argv in (["host", "init"], ["host", "restore"],
                     ["host", "status", "--platform-repository", "/private/secret"],
                     ["host", "init", "--from", "x", "--registry", "/private/secret"],
                     ["host", "unknown-" + "private" * 1000]):
            code, stdout, stderr = self.run_main(argv)
            self.assertEqual(code, 2)
            self.assertEqual(stdout, "")
            self.assertLess(len(stderr), 180)
            self.assertNotIn("secret", stderr)
            self.assertNotIn("privateprivate", stderr)

    def run_main(self, argv):
        from local_web_server.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def run_main_parse_failure(self, argv):
        from local_web_server.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(argv)
        return raised.exception.code, stdout.getvalue(), stderr.getvalue()

    def test_ordinary_parse_failures_keep_actionable_argparse_diagnostics(self):
        cases = (
            (
                ["app", "create", "recipes", "--title", "Recipe Collection"],
                ("usage: local-web app create", "required", "--icon", "--accent"),
            ),
            (
                ["app", "update", "--foundation", "browser-native"],
                (
                    "usage: local-web app update",
                    "--foundation",
                    "browser-native",
                    "react-vite",
                ),
            ),
            (
                ["app", "register", "--port", "not-a-port"],
                (
                    "usage: local-web app register",
                    "--port",
                    "invalid int value",
                    "not-a-port",
                ),
            ),
            (
                ["deply", "plotter"],
                ("usage: local-web", "invalid choice", "deply"),
            ),
        )
        for arguments, expected_fragments in cases:
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.run_main_parse_failure(arguments)

                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                for fragment in expected_fragments:
                    self.assertIn(fragment, stderr)
                self.assertNotEqual(stderr, "local-web: invalid command arguments\n")

    def test_update_parser_accepts_only_the_react_vite_foundation(self):
        args = build_parser().parse_args(
            [
                "app", "update", "--repository", ".",
                "--foundation", "react-vite", "--dry-run",
            ]
        )

        self.assertEqual(args.foundation, "react-vite")
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["app", "update", "--foundation", "browser-native"]
            )

    @patch("local_web_server.cli.load_registry_bytes")
    @patch("local_web_server.cli.DeploymentManager")
    def test_deploy_prints_successful_operation(self, manager, load_registry):
        manager.return_value.deploy.return_value = DeploymentResult(
            "plotter", "a" * 40, "deployed", "plotter deployed " + "a" * 40, None
        )

        code, stdout, stderr = self.run_main(["deploy", "plotter"])

        self.assertEqual(code, 0)
        self.assertIn("plotter deployed", stdout)
        self.assertEqual(stderr, "")
        manager.return_value.deploy.assert_called_once_with("plotter", from_hook=False)
        load_registry.assert_called_once_with(_PRIVATE_HOST_PROFILE_BYTES)
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)

    @patch("local_web_server.cli.load_registry_bytes")
    @patch("local_web_server.cli.DeploymentManager")
    def test_rendered_hook_deploy_invocation_forwards_hook_context(self, manager, load_registry):
        manager.return_value.deploy.return_value = DeploymentResult(
            "plotter", "a" * 40, "deployed", "plotter deployed " + "a" * 40, None
        )

        code, stdout, stderr = self.run_main(["deploy", "plotter", "--from-hook"])

        self.assertEqual(code, 0)
        self.assertIn("plotter deployed", stdout)
        self.assertEqual(stderr, "")
        manager.return_value.deploy.assert_called_once_with("plotter", from_hook=True)
        load_registry.assert_called_once_with(_PRIVATE_HOST_PROFILE_BYTES)
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)

    @patch("local_web_server.cli.load_registry_bytes")
    @patch("local_web_server.cli.DeploymentManager")
    def test_rollback_prints_successful_operation(self, manager, load_registry):
        manager.return_value.rollback.return_value = DeploymentResult(
            "plotter", "a" * 40, "rolled-back", "plotter rolled back to " + "a" * 40, None
        )

        code, stdout, _stderr = self.run_main(["rollback", "plotter"])

        self.assertEqual(code, 0)
        self.assertIn("plotter rolled back", stdout)
        load_registry.assert_called_once_with(_PRIVATE_HOST_PROFILE_BYTES)
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)

    @patch("local_web_server.cli.load_registry_bytes")
    @patch("local_web_server.cli.StatusCollector")
    def test_status_returns_one_and_prints_stale_app(self, collector, load_registry):
        collector.return_value.caddy_state = ServiceState.RUNNING
        collector.return_value.collect.return_value = (FakeStatus(stale=True),)

        code, stdout, stderr = self.run_main(["status"])

        self.assertEqual(code, 1)
        self.assertIn("STALE", stdout)
        self.assertIn("frontend=OK backend=OK", stdout)
        self.assertNotIn("public=", stdout)
        self.assertEqual(stderr, "")
        load_registry.assert_called_once_with(_PRIVATE_HOST_PROFILE_BYTES)
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)

    @patch("local_web_server.cli.load_registry_bytes")
    @patch("local_web_server.cli.StatusCollector")
    def test_status_exposes_stopped_caddy_without_health_response_details(
        self, collector, load_registry
    ):
        status = FakeStatus(healthy=True, backend=False, caddy_state=ServiceState.STOPPED)
        status.backend_health = HealthResult(False, 503, "private response body")
        collector.return_value.caddy_state = ServiceState.STOPPED
        collector.return_value.collect.return_value = (status,)

        code, stdout, stderr = self.run_main(["status"])

        self.assertEqual(code, 1)
        self.assertIn("caddy: stopped", stdout)
        self.assertIn("frontend=OK backend=FAIL", stdout)
        self.assertNotIn("private response body", stdout)
        self.assertNotIn("503", stdout)
        self.assertEqual(stderr, "")

    @patch("local_web_server.cli.load_registry_bytes")
    @patch("local_web_server.cli.StatusCollector")
    def test_status_uses_dash_for_apps_without_a_backend(self, collector, load_registry):
        collector.return_value.caddy_state = ServiceState.RUNNING
        collector.return_value.collect.return_value = (FakeStatus(backend=None),)

        code, stdout, stderr = self.run_main(["status"])

        self.assertEqual(code, 0)
        self.assertIn("frontend=OK backend=-", stdout)
        self.assertEqual(stderr, "")

    @patch("local_web_server.cli.load_registry_bytes")
    @patch("local_web_server.cli.StatusCollector")
    def test_status_reports_missing_caddy_and_fails_with_no_registered_apps(
        self, collector, load_registry
    ):
        collector.return_value.caddy_state = ServiceState.MISSING
        collector.return_value.collect.return_value = ()

        code, stdout, stderr = self.run_main(["status"])

        self.assertEqual(code, 1)
        self.assertIn("caddy: missing", stdout)
        self.assertEqual(stderr, "")

    @patch("local_web_server.cli.load_registry_bytes")
    @patch("local_web_server.cli.DeploymentManager")
    def test_unknown_app_returns_operational_error(self, manager, load_registry):
        from local_web_server.config import ConfigError

        manager.return_value.deploy.side_effect = ConfigError("registered app not found: unknown")

        code, _stdout, stderr = self.run_main(["deploy", "unknown"])

        self.assertEqual(code, 2)
        self.assertIn("registered app not found", stderr)

    def test_parser_exposes_only_everyday_commands(self):
        from local_web_server.cli import build_parser

        parser = build_parser()
        subparsers = next(action for action in parser._actions if action.dest == "command")

        self.assertEqual(
            set(subparsers.choices),
            {"app", "apps", "host", "deploy", "rollback", "status", "theme"},
        )

    def test_apps_update_parser_requires_exactly_one_mode(self):
        parser = build_parser()

        dry_run = parser.parse_args(["apps", "update", "--dry-run"])
        apply = parser.parse_args(["apps", "update", "--apply"])

        self.assertEqual((dry_run.command, dry_run.apps_command), ("apps", "update"))
        self.assertTrue(dry_run.dry_run)
        self.assertFalse(dry_run.apply)
        self.assertFalse(apply.dry_run)
        self.assertTrue(apply.apply)
        with self.assertRaises(SystemExit):
            parser.parse_args(["apps", "update"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["apps", "update", "--dry-run", "--apply"])

    @patch("local_web_server.cli.FleetUpdater", create=True)
    def test_apps_update_dry_run_renders_ordered_bounded_plan_rows(self, fleet_updater):
        fleet_updater.return_value.preview.return_value = FleetUpdatePlan(
            apps=(
                FleetAppPlan(
                    "samplebeta",
                    "READY",
                    "0.6.0",
                    "0.6.1",
                    "d630e294ecff" + "a" * 52,
                    "0123456789abcdef" + "b" * 24,
                    "fedcba9876543210" + "c" * 24,
                    (
                        Path(".local-web-platform.json"),
                        Path("vendor/local-web-ui.tgz"),
                    ),
                    None,
                    repository=Path("/private/samplebeta"),
                    _update_plan=None,
                ),
                FleetAppPlan(
                    "samplealpha",
                    "SKIPPED",
                    None,
                    None,
                    None,
                    None,
                    None,
                    (),
                    "legacy-adoption-required",
                    repository=Path("/private/samplealpha"),
                    _update_plan=None,
                ),
            )
        )

        code, stdout, stderr = self.run_main(["apps", "update", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertEqual(
            stdout,
            "samplebeta READY ui=0.6.0->0.6.1 source=0123456789ab deployed=fedcba987654 "
            "paths=.local-web-platform.json,vendor/local-web-ui.tgz "
            "digest=d630e294ecff\n"
            "samplealpha SKIPPED ui=-->- source=- deployed=- paths=- digest=-\n",
        )
        self.assertEqual(stderr, "")
        self.assertNotIn("/private", stdout + stderr)
        fleet_updater.return_value.preview.assert_called_once_with(
            _PRIVATE_HOST_PROFILE
        )
        fleet_updater.return_value.apply.assert_not_called()
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)

    @patch("local_web_server.cli.FleetUpdater", create=True)
    def test_apps_update_apply_renders_bounded_results_and_summary(self, fleet_updater):
        results = (
            FleetAppResult(
                "samplebeta",
                "UPDATED",
                "0.6.0",
                "0.6.1",
                "0123456789abcdef" + "b" * 24,
                "fedcba9876543210" + "c" * 24,
                (Path(".local-web-platform.json"), Path("vendor/local-web-ui.tgz")),
                True,
                None,
            ),
            FleetAppResult("current", "CURRENT", "0.6.1", "0.6.1", "a" * 40, "a" * 40, (), True, None),
            FleetAppResult("legacy", "SKIPPED", None, None, None, None, (), False, "legacy-adoption-required"),
            FleetAppResult("dirty", "BLOCKED", None, None, None, None, (), False, "repository-dirty"),
            FleetAppResult("recovered", "FAILED_RECOVERED", "0.6.0", "0.6.1", "c" * 40, "c" * 40, (), True, "check-failed"),
            FleetAppResult("failed", "RECOVERY_FAILED", "0.6.0", "0.6.1", "d" * 40, "e" * 40, (), False, "recovery-failed"),
        )
        fleet_updater.return_value.apply.return_value = FleetUpdateResult(results)

        code, stdout, stderr = self.run_main(["apps", "update", "--apply"])

        self.assertEqual(code, 1)
        self.assertEqual(
            stdout,
            "samplebeta UPDATED ui=0.6.0->0.6.1 source=0123456789ab deployed=fedcba987654 "
            "paths=.local-web-platform.json,vendor/local-web-ui.tgz verified=yes\n"
            "current CURRENT ui=0.6.1->0.6.1 source=aaaaaaaaaaaa deployed=aaaaaaaaaaaa paths=- verified=yes\n"
            "legacy SKIPPED ui=-->- source=- deployed=- paths=- verified=no\n"
            "dirty BLOCKED ui=-->- source=- deployed=- paths=- verified=no\n"
            "recovered FAILED_RECOVERED ui=0.6.0->0.6.1 source=cccccccccccc deployed=cccccccccccc paths=- verified=yes\n"
            "failed RECOVERY_FAILED ui=0.6.0->0.6.1 source=dddddddddddd deployed=eeeeeeeeeeee paths=- verified=no\n"
            "summary updated=1 current=1 skipped=1 blocked=1 failed-recovered=1 recovery-failed=1\n",
        )
        self.assertEqual(stderr, "")
        fleet_updater.return_value.apply.assert_called_once_with(
            _PRIVATE_HOST_PROFILE
        )
        fleet_updater.return_value.preview.assert_not_called()
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)

    @patch("local_web_server.cli.FleetUpdater", create=True)
    def test_apps_update_apply_exits_zero_only_for_updated_or_current_results(
        self, fleet_updater
    ):
        for status, expected_code in (
            ("UPDATED", 0),
            ("CURRENT", 0),
            ("SKIPPED", 1),
            ("BLOCKED", 1),
            ("FAILED_RECOVERED", 1),
            ("RECOVERY_FAILED", 1),
        ):
            with self.subTest(status=status):
                fleet_updater.return_value.apply.return_value = FleetUpdateResult(
                    (
                        FleetAppResult(
                            "fixture",
                            status,
                            None,
                            None,
                            None,
                            None,
                            (),
                            False,
                            None,
                        ),
                    )
                )

                code, _stdout, stderr = self.run_main(["apps", "update", "--apply"])

                self.assertEqual(code, expected_code)
                self.assertEqual(stderr, "")

    @patch("local_web_server.cli.FleetUpdater", create=True)
    def test_apps_update_sanitises_planning_and_apply_failures(self, fleet_updater):
        injected = "INJECTED-FLEET-FAILURE /private/registered-app"
        fleet_updater.return_value.preview.side_effect = RuntimeError(injected)

        preview_code, preview_stdout, preview_stderr = self.run_main(
            ["apps", "update", "--dry-run"]
        )

        fleet_updater.return_value.preview.side_effect = None
        fleet_updater.return_value.apply.side_effect = RuntimeError(injected)
        apply_code, apply_stdout, apply_stderr = self.run_main(
            ["apps", "update", "--apply"]
        )

        invalid_code, invalid_stdout, invalid_stderr = self.run_main(
            ["apps", "update", "--apply", injected]
        )

        self.assertEqual((preview_code, apply_code, invalid_code), (2, 2, 2))
        self.assertEqual(preview_stdout + apply_stdout + invalid_stdout, "")
        self.assertEqual(
            preview_stderr + apply_stderr + invalid_stderr,
            "local-web: fleet application update failed\n"
            "local-web: fleet application update failed\n"
            "local-web: invalid command arguments\n",
        )
        output = (
            preview_stdout
            + preview_stderr
            + apply_stdout
            + apply_stderr
            + invalid_stdout
            + invalid_stderr
        )
        self.assertNotIn(injected, output)
        self.assertNotIn("/private", output)

    @patch("local_web_server.cli.identity_report")
    def test_app_identity_search_prints_semantic_choices_and_assignments(self, report):
        report.return_value = IdentityReport(
            icons=(
                ManifestIconIdentity(
                    "tools-kitchen-2",
                    "tools-kitchen-2",
                    "Kitchen tools",
                    ("food", "meal", "planning"),
                ),
            ),
            accents=(AccentIdentity("claret", "#7A1735", "#7A1735", "#A33D59"),),
            assignments=(
                IdentityAssignment("samplebeta", "Sample Workspace", "shirt-sport", "#7A1735"),
            ),
            reserved_icons=("apps",),
        )

        code, stdout, stderr = self.run_main(
            ["app", "identity", "--search", "meal food planning"]
        )

        self.assertEqual(code, 0)
        self.assertIn("tools-kitchen-2", stdout)
        self.assertIn("#7A1735", stdout)
        self.assertIn("samplebeta", stdout)
        self.assertNotIn("repository", stdout.lower())
        self.assertEqual(stderr, "")
        report.assert_called_once_with("meal food planning", _PRIVATE_HOST_PROFILE)
        self.registry_loader.assert_called_once_with(_PRIVATE_HOST_PROFILE_BYTES)
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)

    @patch("local_web_server.cli.identity_report")
    def test_app_identity_json_is_stable_and_sanitized(self, report):
        report.return_value = IdentityReport((), (), (), ("apps",))

        code, stdout, stderr = self.run_main(["app", "identity", "--json"])

        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(stdout),
            {"icons": [], "accents": [], "assignments": [], "reservedIcons": ["apps"]},
        )
        self.assertEqual(stderr, "")

    @patch("local_web_server.cli.load_registry")
    @patch("local_web_server.cli.load_main_manifests")
    def test_app_identity_sanitises_registered_manifest_failures(
        self, manifests, load_registry
    ):
        private_path = "/Users/" + "private"
        load_registry.return_value.apps = ()
        manifests.side_effect = ConfigError(
            f"cannot read manifest: {private_path}/app@deadbeef:local-web.json"
        )

        code, stdout, stderr = self.run_main(["app", "identity"])

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "local-web: application identity discovery failed\n")
        self.assertNotIn(private_path, stderr)
        self.assertEqual(
            load_registry.call_args_list,
            [call(_PRIVATE_HOST_PROFILE)],
        )
        self.registry_loader.assert_called_once_with(_PRIVATE_HOST_PROFILE_BYTES)
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)

    @patch("local_web_server.cli.AppActivator")
    def test_app_activate_previews_by_default_and_applies_only_when_requested(self, activator):
        plan = AppActivationPlan(
            "recipes", "/recipes", "service", 52000, True
        )
        activation = AppActivationResult(
            plan,
            "a" * 64,
            DeploymentResult("recipes", "b" * 40, "deployed", "deployed", None),
            True,
        )
        activator.return_value.preview.return_value = plan
        activator.return_value.activate.return_value = activation

        preview_code, preview_stdout, preview_stderr = self.run_main(
            ["app", "activate", "--repository", "/tmp/recipes"]
        )
        apply_code, apply_stdout, apply_stderr = self.run_main(
            ["app", "activate", "--repository", "/tmp/recipes", "--apply"]
        )

        self.assertEqual((preview_code, apply_code), (0, 0))
        self.assertIn("would activate recipes", preview_stdout)
        self.assertIn("port: 52000", preview_stdout)
        self.assertIn("activated recipes", apply_stdout)
        self.assertIn("registry revision: " + "a" * 64, apply_stdout)
        self.assertEqual(preview_stderr + apply_stderr, "")
        activator.return_value.preview.assert_called_once_with(Path("/tmp/recipes"))
        activator.return_value.activate.assert_called_once_with(Path("/tmp/recipes"))
        self.assertEqual(
            activator.call_args_list,
            [
                call(registry_path=_PRIVATE_HOST_PROFILE),
                call(registry_path=_PRIVATE_HOST_PROFILE),
            ],
        )
        self.assertEqual(
            self.host_profile_resolver.call_args_list,
            [call(_PLATFORM_REPOSITORY), call(_PLATFORM_REPOSITORY)],
        )

    @patch("local_web_server.cli.AppActivator")
    def test_app_activation_json_names_only_the_profile_revision(self, activator):
        plan = AppActivationPlan("recipes", "/recipes", "static", None, True)
        activator.return_value.activate.return_value = AppActivationResult(
            plan,
            "a" * 64,
            DeploymentResult("recipes", "b" * 40, "deployed", "deployed", None),
            True,
        )

        code, stdout, stderr = self.run_main(
            ["app", "activate", "--repository", "/tmp/recipes", "--apply", "--json"]
        )

        payload = json.loads(stdout)
        self.assertEqual(code, 0)
        self.assertEqual(payload["registryRevision"], "a" * 64)
        self.assertNotIn("registryCommit", payload)
        self.assertEqual(stderr, "")

    @patch("local_web_server.cli.ServiceCommandMigrator")
    def test_app_migrate_service_command_previews_and_applies_with_exact_bounded_output(
        self, migrator
    ):
        plan = ServiceCommandMigrationPlan(
            "fixture-service", "/fixture", 52700, "change"
        )
        migrator.return_value.preview.return_value = plan
        migrator.return_value.migrate.return_value = ServiceCommandMigrationResult(
            plan,
            "a" * 64,
            DeploymentResult(
                "fixture-service",
                "b" * 40,
                "deployed",
                "PRIVATE COMMAND OUTPUT",
                Path("/private/deployment.log"),
            ),
            True,
        )

        preview_code, preview_stdout, preview_stderr = self.run_main(
            ["app", "migrate-service-command", "--repository", "/tmp/private-app"]
        )
        apply_code, apply_stdout, apply_stderr = self.run_main(
            [
                "app",
                "migrate-service-command",
                "--repository",
                "/tmp/private-app",
                "--apply",
            ]
        )

        expected = (
            "application: fixture-service\n"
            "service command: change\n"
            "identity: unchanged\n"
            "repository: unchanged\n"
            "route: unchanged\n"
            "port: 52700 unchanged\n"
            "service action: redeploy and reload\n"
        )
        self.assertEqual((preview_code, apply_code), (0, 0))
        self.assertEqual(preview_stdout, expected)
        self.assertEqual(apply_stdout, expected)
        self.assertEqual(preview_stderr + apply_stderr, "")
        self.assertNotIn("private", preview_stdout + apply_stdout)
        self.assertNotIn("PRIVATE", preview_stdout + apply_stdout)
        self.assertNotIn("aaaa", preview_stdout + apply_stdout)
        migrator.return_value.preview.assert_called_once_with(Path("/tmp/private-app"))
        migrator.return_value.migrate.assert_called_once_with(Path("/tmp/private-app"))
        self.assertEqual(
            migrator.call_args_list,
            [
                call(registry_path=_PRIVATE_HOST_PROFILE),
                call(registry_path=_PRIVATE_HOST_PROFILE),
            ],
        )
        self.assertEqual(
            self.host_profile_resolver.call_args_list,
            [call(_PLATFORM_REPOSITORY), call(_PLATFORM_REPOSITORY)],
        )

    @patch("local_web_server.cli.ServiceCommandMigrator")
    def test_app_migrate_service_command_json_reports_revision_only_on_apply(
        self, migrator
    ):
        plan = ServiceCommandMigrationPlan(
            "fixture-service", "/fixture", 52700, "current", service_action="none"
        )
        migrator.return_value.preview.return_value = plan
        migrator.return_value.migrate.return_value = ServiceCommandMigrationResult(
            plan, "a" * 64, None, False
        )

        preview_code, preview_stdout, preview_stderr = self.run_main(
            ["app", "migrate-service-command", "--json"]
        )
        apply_code, apply_stdout, apply_stderr = self.run_main(
            ["app", "migrate-service-command", "--apply", "--json"]
        )

        expected = {
            "application": "fixture-service",
            "serviceCommand": "current",
            "identity": "unchanged",
            "repository": "unchanged",
            "route": "unchanged",
            "port": 52700,
            "portStatus": "unchanged",
            "serviceAction": "none",
        }
        self.assertEqual((preview_code, apply_code), (0, 0))
        self.assertEqual(json.loads(preview_stdout), expected)
        applied = json.loads(apply_stdout)
        self.assertEqual(applied, {**expected, "registryRevision": "a" * 64})
        self.assertNotIn("registryCommit", applied)
        self.assertEqual(preview_stderr + apply_stderr, "")

    def test_app_migrate_service_command_rejects_caller_controlled_values(self):
        for arguments in (
            ["--command", "SENSITIVE_ARGV_MARKER"],
            ["--port", "SENSITIVE_PORT_MARKER"],
            ["--route", "/tmp/SENSITIVE_ROUTE_MARKER"],
            ["/tmp/SENSITIVE_POSITIONAL_MARKER"],
        ):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.run_main(
                    ["app", "migrate-service-command", *arguments]
                )

                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "local-web: invalid command arguments\n")
                self.assertNotIn("SENSITIVE_", stderr)
                self.assertNotIn("/tmp/", stderr)

    @patch("local_web_server.cli.ServiceCommandMigrator")
    def test_app_migrate_service_command_sanitises_operational_errors(self, migrator):
        migrator.return_value.preview.side_effect = ServiceCommandMigrationError(
            "service command migration validation failed"
        )

        code, stdout, stderr = self.run_main(
            [
                "app",
                "migrate-service-command",
                "--repository",
                "/tmp/private-repository",
            ]
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "local-web: service command migration validation failed\n",
        )
        self.assertNotIn("private-repository", stderr)

    @patch("local_web_server.cli.PublicBasePathMigrator")
    def test_app_migrate_public_base_path_previews_and_applies_with_exact_bounded_output(
        self, migrator
    ):
        plan = PublicBasePathMigrationPlan(
            "fixture-service", "/fixture", 52700, "change"
        )
        migrator.return_value.preview.return_value = plan
        migrator.return_value.migrate.return_value = PublicBasePathMigrationResult(
            plan,
            "a" * 64,
            DeploymentResult(
                "fixture-service",
                "b" * 40,
                "deployed",
                "PRIVATE ENVIRONMENT OUTPUT",
                Path("/private/deployment.log"),
            ),
            True,
        )

        preview_code, preview_stdout, preview_stderr = self.run_main(
            ["app", "migrate-public-base-path", "--repository", "/tmp/private-app"]
        )
        apply_code, apply_stdout, apply_stderr = self.run_main(
            [
                "app",
                "migrate-public-base-path",
                "--repository",
                "/tmp/private-app",
                "--apply",
            ]
        )

        expected = (
            "application: fixture-service\n"
            "public base path: change\n"
            "identity: unchanged\n"
            "repository: unchanged\n"
            "route: unchanged\n"
            "port: 52700 unchanged\n"
            "service command: unchanged\n"
            "service action: redeploy and reload\n"
        )
        self.assertEqual((preview_code, apply_code), (0, 0))
        self.assertEqual(preview_stdout, expected)
        self.assertEqual(apply_stdout, expected)
        self.assertEqual(preview_stderr + apply_stderr, "")
        output = preview_stdout + apply_stdout
        self.assertNotIn("private", output)
        self.assertNotIn("PRIVATE", output)
        self.assertNotIn("aaaa", output)
        migrator.return_value.preview.assert_called_once_with(Path("/tmp/private-app"))
        migrator.return_value.migrate.assert_called_once_with(Path("/tmp/private-app"))
        self.assertEqual(
            migrator.call_args_list,
            [
                call(registry_path=_PRIVATE_HOST_PROFILE),
                call(registry_path=_PRIVATE_HOST_PROFILE),
            ],
        )
        self.assertEqual(
            self.host_profile_resolver.call_args_list,
            [call(_PLATFORM_REPOSITORY), call(_PLATFORM_REPOSITORY)],
        )

    @patch("local_web_server.cli.PublicBasePathMigrator")
    def test_app_migrate_public_base_path_json_reports_revision_only_on_apply(
        self, migrator
    ):
        plan = PublicBasePathMigrationPlan(
            "fixture-service", "/fixture", 52700, "current", service_action="none"
        )
        migrator.return_value.preview.return_value = plan
        migrator.return_value.migrate.return_value = PublicBasePathMigrationResult(
            plan, "a" * 64, None, False
        )

        preview_code, preview_stdout, preview_stderr = self.run_main(
            ["app", "migrate-public-base-path", "--json"]
        )
        apply_code, apply_stdout, apply_stderr = self.run_main(
            ["app", "migrate-public-base-path", "--apply", "--json"]
        )

        expected = {
            "application": "fixture-service",
            "publicBasePath": "current",
            "identity": "unchanged",
            "repository": "unchanged",
            "route": "unchanged",
            "port": 52700,
            "portStatus": "unchanged",
            "serviceCommand": "unchanged",
            "serviceAction": "none",
        }
        self.assertEqual((preview_code, apply_code), (0, 0))
        self.assertEqual(json.loads(preview_stdout), expected)
        applied = json.loads(apply_stdout)
        self.assertEqual(applied, {**expected, "registryRevision": "a" * 64})
        self.assertNotIn("registryCommit", applied)
        self.assertEqual(preview_stderr + apply_stderr, "")

    def test_app_migrate_public_base_path_rejects_caller_controlled_values(self):
        for arguments in (
            ["--value", "SENSITIVE_VALUE_MARKER"],
            ["--environment", "SENSITIVE_ENVIRONMENT_MARKER"],
            ["--route", "/tmp/SENSITIVE_ROUTE_MARKER"],
            ["--port", "SENSITIVE_PORT_MARKER"],
            ["/tmp/SENSITIVE_POSITIONAL_MARKER"],
        ):
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.run_main(
                    ["app", "migrate-public-base-path", *arguments]
                )

                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "local-web: invalid command arguments\n")
                self.assertNotIn("SENSITIVE_", stderr)
                self.assertNotIn("/tmp/", stderr)

    @patch("local_web_server.cli.PublicBasePathMigrator")
    def test_app_migrate_public_base_path_sanitises_operational_errors(self, migrator):
        migrator.return_value.preview.side_effect = PublicBasePathMigrationError(
            "public base path migration validation failed"
        )

        code, stdout, stderr = self.run_main(
            [
                "app",
                "migrate-public-base-path",
                "--repository",
                "/tmp/private-repository",
            ]
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "local-web: public base path migration validation failed\n",
        )
        self.assertNotIn("private-repository", stderr)

    def test_app_generation_requires_an_explicit_icon_and_accent(self):
        from local_web_server.cli import build_parser

        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(
                ["app", "create", "recipes", "--title", "Recipe Collection"]
            )
        with self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "app",
                    "init",
                    "/tmp/recipes",
                    "--title",
                    "Recipe Collection",
                    "--icon",
                    "book",
                ]
            )

    @patch("local_web_server.cli.AppGenerator")
    def test_app_create_dry_run_reports_explicit_identity_without_creating_anything(self, generator):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "recipes"
            generator.return_value.preview.return_value = CreateAppPreview(
                destination=destination,
                route="/recipes",
                icon="book",
                accent="#8EA7C6",
                capabilities=(),
                ui_version="0.1.0",
                template_version=1,
            )

            with patch("pathlib.Path.cwd", return_value=Path(temporary)):
                code, stdout, stderr = self.run_main(
                    [
                        "app", "create", "recipes", "--title", "Recipe Collection",
                        "--icon", "book", "--accent", "#8EA7C6", "--dry-run",
                    ]
                )

            self.assertEqual(code, 0)
            self.assertIn(f"destination: {destination}", stdout)
            self.assertIn("route: /recipes", stdout)
            self.assertIn("ui: 0.1.0", stdout)
            self.assertIn("template: 1", stdout)
            self.assertEqual(stderr, "")
            self.assertFalse(destination.exists())
            forwarded = generator.return_value.preview.call_args.args[0]
            self.assertEqual(forwarded.icon, "book")
            self.assertEqual(forwarded.accent, "#8EA7C6")
            self.assertEqual(forwarded.kind, "static")

    @patch("local_web_server.cli.AppGenerator")
    def test_app_create_forwards_explicit_values_and_reports_commit(self, generator):
        destination = Path("/tmp/recipes-explicit")
        generator.return_value.create.return_value = CreateAppResult(
            destination=destination,
            ui_version="0.1.0",
            ui_sha256="a" * 64,
            commit="b" * 40,
        )

        code, stdout, stderr = self.run_main(
            [
                "app", "create", "recipes", "--title", "Recipe Collection",
                "--destination", str(destination), "--route", "/food/recipes",
                "--icon", "book", "--accent", "#8EA7C6",
                "--capability", "supabase",
                "--kind", "service",
            ]
        )

        self.assertEqual(code, 0)
        self.assertIn("created recipes", stdout)
        self.assertIn("commit=" + "b" * 40, stdout)
        self.assertEqual(stderr, "")
        forwarded = generator.return_value.create.call_args.args[0]
        self.assertEqual(forwarded.destination, destination)
        self.assertEqual(forwarded.route, "/food/recipes")
        self.assertEqual(forwarded.capabilities, ("supabase",))
        self.assertEqual(forwarded.kind, "service")

    @patch("local_web_server.cli.AppGenerator")
    def test_app_init_forwards_derived_defaults_and_reports_initialisation(self, generator):
        destination = Path("/tmp/recipe-notebook")
        generator.return_value.init.return_value = CreateAppResult(
            destination=destination,
            ui_version="0.1.0",
            ui_sha256="a" * 64,
            commit="b" * 40,
        )

        code, stdout, stderr = self.run_main(
            [
                "app", "init", str(destination), "--title", "Recipe Notebook",
                "--icon", "book", "--accent", "#8EA7C6",
                "--capability", "supabase",
            ]
        )

        self.assertEqual(code, 0)
        self.assertIn("initialised recipe-notebook", stdout)
        self.assertIn("commit=" + "b" * 40, stdout)
        self.assertEqual(stderr, "")
        forwarded = generator.return_value.init.call_args.args[0]
        self.assertEqual(forwarded.app_id, "recipe-notebook")
        self.assertEqual(forwarded.destination, destination)
        self.assertEqual(forwarded.route, "/recipe-notebook")
        self.assertEqual(forwarded.icon, "book")
        self.assertEqual(forwarded.accent, "#8EA7C6")
        self.assertEqual(forwarded.capabilities, ("supabase",))

    @patch("local_web_server.cli.AppGenerator")
    def test_app_init_forwards_relative_directory_as_an_absolute_path(self, generator):
        """A relative CLI input must not make publication depend on child cwd."""
        destination = (Path.cwd() / "relative" / "recipe-notebook").absolute()
        generator.return_value.init.return_value = CreateAppResult(
            destination=destination,
            ui_version="0.1.0",
            ui_sha256="a" * 64,
            commit="b" * 40,
        )

        code, _stdout, stderr = self.run_main(
            [
                "app",
                "init",
                "relative/recipe-notebook",
                "--title",
                "Recipe Notebook",
                "--icon",
                "book",
                "--accent",
                "#8EA7C6",
            ]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        forwarded = generator.return_value.init.call_args.args[0]
        self.assertTrue(forwarded.destination.is_absolute())
        self.assertEqual(forwarded.destination, destination)
        self.assertEqual(forwarded.app_id, "recipe-notebook")

    @patch("local_web_server.cli.AppGenerator")
    def test_app_init_dry_run_previews_without_initialising(self, generator):
        destination = Path("/tmp/recipe-notebook")
        generator.return_value.preview_init.return_value = CreateAppPreview(
            destination=destination,
            route="/recipe-notebook",
            icon="book",
            accent="#8EA7C6",
            capabilities=(),
            ui_version="0.1.0",
            template_version=1,
        )

        code, stdout, stderr = self.run_main(
            [
                "app", "init", str(destination), "--title", "Recipe Notebook",
                "--icon", "book", "--accent", "#8EA7C6", "--dry-run",
            ]
        )

        self.assertEqual(code, 0)
        self.assertIn(f"destination: {destination}", stdout)
        self.assertIn("route: /recipe-notebook", stdout)
        self.assertEqual(stderr, "")
        generator.return_value.preview_init.assert_called_once()
        generator.return_value.init.assert_not_called()

    @patch("local_web_server.cli.AppUpdater")
    def test_app_update_dry_run_previews_repository_without_writing(self, updater):
        updater.return_value.preview.return_value = update_plan()

        code, stdout, stderr = self.run_main(
            [
                "app",
                "update",
                "--repository",
                "/tmp/private-plotter",
                "--capability",
                "supabase",
                "--dry-run",
            ]
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            stdout,
            "mode: adopt\n"
            "target ui: 0.5.2\n"
            f"digest: {'a' * 64}\n"
            "capabilities: supabase\n"
            "would update:\n"
            "  .local-web-platform.json\n"
            "  vendor/local-web-ui.tgz\n",
        )
        self.assertNotIn("private-plotter", stdout)
        self.assertEqual(stderr, "")
        updater.return_value.preview.assert_called_once_with(
            AppUpdateRequest(Path("/tmp/private-plotter"), ("supabase",))
        )
        updater.return_value.update.assert_not_called()

    @patch("local_web_server.cli.AppUpdater")
    def test_app_update_foundation_preview_reports_explicit_version_transition(
        self, updater
    ):
        updater.return_value.preview.return_value = update_plan(
            foundation="react-vite"
        )

        code, stdout, stderr = self.run_main(
            [
                "app", "update", "--repository", "/tmp/private-samplealpha",
                "--foundation", "react-vite", "--capability", "supabase",
                "--dry-run",
            ]
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            stdout,
            "mode: adopt\n"
            "foundation: react-vite\n"
            "current platform contract: -\n"
            "target platform contract: 1\n"
            "current template: -\n"
            "target template: 1\n"
            "current ui: -\n"
            "target ui: 0.5.2\n"
            f"digest: {'a' * 64}\n"
            "capabilities: supabase\n"
            "would update:\n"
            "  .local-web-platform.json\n"
            "  vendor/local-web-ui.tgz\n",
        )
        self.assertEqual(stderr, "")
        self.assertNotIn("private-samplealpha", stdout)
        updater.return_value.preview.assert_called_once_with(
            AppUpdateRequest(
                Path("/tmp/private-samplealpha"),
                ("supabase",),
                foundation="react-vite",
            )
        )

    @patch("local_web_server.cli.AppUpdater")
    def test_app_update_defaults_to_current_repository(self, updater):
        updater.return_value.preview.return_value = update_plan(capabilities=())

        code, _stdout, stderr = self.run_main(["app", "update", "--dry-run"])

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        updater.return_value.preview.assert_called_once_with(
            AppUpdateRequest(Path("."), ())
        )

    @patch("local_web_server.cli.AppUpdater")
    def test_app_update_canonicalises_repeated_capabilities(self, updater):
        updater.return_value.preview.return_value = update_plan()

        code, _stdout, stderr = self.run_main(
            ["app", "update", "--capability", "supabase", "--dry-run"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        updater.return_value.preview.assert_called_once_with(
            AppUpdateRequest(Path("."), ("supabase",))
        )

    @patch("local_web_server.cli.AppUpdater")
    def test_app_update_rejects_duplicate_and_unsupported_capabilities(self, updater):
        for capabilities in (("supabase", "supabase"), ("private",)):
            with self.subTest(capabilities=capabilities):
                arguments = ["app", "update", "--dry-run"]
                for capability in capabilities:
                    arguments.extend(("--capability", capability))

                code, stdout, stderr = self.run_main(arguments)

                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(
                    stderr,
                    "local-web: application update request is invalid\n",
                )
        updater.assert_not_called()

    @patch("local_web_server.cli.AppUpdater")
    def test_app_update_executes_real_update_and_reports_recovery(self, updater):
        plan = update_plan(mode="refresh", capabilities=())
        updater.return_value.update.return_value = AppUpdateResult(plan, recovered=True)

        code, stdout, stderr = self.run_main(
            ["app", "update", "--repository", "/tmp/private-plotter"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            stdout,
            "mode: refresh\n"
            "target ui: 0.5.2\n"
            f"digest: {'a' * 64}\n"
            "capabilities: -\n"
            "updated:\n"
            "  .local-web-platform.json\n"
            "  vendor/local-web-ui.tgz\n"
            "recovery: completed\n",
        )
        self.assertNotIn("private-plotter", stdout)
        self.assertEqual(stderr, "")
        updater.return_value.update.assert_called_once_with(
            AppUpdateRequest(Path("/tmp/private-plotter"), ())
        )
        updater.return_value.preview.assert_not_called()

    @patch("local_web_server.cli.AppUpdater")
    def test_app_update_reports_current_no_op(self, updater):
        plan = update_plan(mode="current", capabilities=(), changes=())
        updater.return_value.update.return_value = AppUpdateResult(plan, recovered=False)

        code, stdout, stderr = self.run_main(["app", "update"])

        self.assertEqual(code, 0)
        self.assertEqual(
            stdout,
            "mode: current\n"
            "target ui: 0.5.2\n"
            f"digest: {'a' * 64}\n"
            "capabilities: -\n"
            "updated: none\n"
            "recovery: not required\n",
        )
        self.assertEqual(stderr, "")

    @patch("local_web_server.cli.AppUpdater")
    def test_app_update_sanitises_operational_failures(self, updater):
        updater.return_value.preview.side_effect = AppUpdateError(
            "application update planning failed"
        )

        code, stdout, stderr = self.run_main(
            [
                "app",
                "update",
                "--repository",
                "/tmp/private-plotter",
                "--dry-run",
            ]
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "local-web: application update planning failed\n")
        self.assertNotIn("private-plotter", stderr)

    @patch("local_web_server.cli.load_registry_bytes")
    @patch("local_web_server.cli.DeploymentManager")
    @patch("local_web_server.cli.AppGenerator")
    @patch("local_web_server.cli._registration_profile_store", create=True)
    @patch("local_web_server.cli.AppRegistrar")
    def test_app_register_uses_only_default_registry_and_reports_registration(
        self, registrar, profile_store, generator, deployment, load_registry
    ):
        repository = Path("/tmp/recipe-notebook")
        registrar.return_value.register.return_value = AppRegistrationResult(
            app_id="recipe-notebook",
            route="/recipe-notebook",
            registry=_PRIVATE_HOST_PROFILE,
            changed=True,
            dry_run=False,
            registry_revision="a" * 64,
        )

        code, stdout, stderr = self.run_main(
            ["app", "register", "--repository", str(repository), "--apply"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "registered recipe-notebook at /recipe-notebook\n")
        self.assertEqual(stderr, "")
        registrar.return_value.register.assert_called_once_with(
            repository,
            _PRIVATE_HOST_PROFILE,
            profile_store=profile_store.return_value,
            dry_run=False,
            port=None,
        )
        profile_store.assert_called_once_with(_PRIVATE_HOST_PROFILE)
        generator.assert_not_called()
        deployment.assert_not_called()
        load_registry.assert_called_once_with(_PRIVATE_HOST_PROFILE_BYTES)
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)

    @patch("local_web_server.cli._registration_profile_store", create=True)
    @patch("local_web_server.cli.AppRegistrar")
    def test_app_register_dry_run_performs_only_registration_preview(
        self, registrar, profile_store
    ):
        repository = Path("/tmp/recipe-notebook")
        registrar.return_value.register.return_value = AppRegistrationResult(
            app_id="recipe-notebook",
            route="/recipe-notebook",
            registry=_PRIVATE_HOST_PROFILE,
            changed=True,
            dry_run=True,
        )

        code, stdout, stderr = self.run_main(
            ["app", "register", "--repository", str(repository), "--dry-run"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "would register recipe-notebook at /recipe-notebook\n")
        self.assertEqual(stderr, "")
        registrar.return_value.register.assert_called_once_with(
            repository,
            _PRIVATE_HOST_PROFILE,
            profile_store=profile_store.return_value,
            dry_run=True,
            port=None,
        )
        profile_store.assert_called_once_with(_PRIVATE_HOST_PROFILE)
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)

    @patch("local_web_server.cli._registration_profile_store", create=True)
    @patch("local_web_server.cli.AppRegistrar")
    def test_app_register_forwards_explicit_service_port(
        self, registrar, profile_store
    ):
        repository = Path("/tmp/service-app")
        registrar.return_value.register.return_value = AppRegistrationResult(
            app_id="service-app",
            route="/service-app",
            registry=_PRIVATE_HOST_PROFILE,
            changed=True,
            dry_run=True,
        )

        code, _stdout, stderr = self.run_main(
            [
                "app",
                "register",
                "--repository",
                str(repository),
                "--port",
                "52700",
                "--dry-run",
            ]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        registrar.return_value.register.assert_called_once_with(
            repository,
            _PRIVATE_HOST_PROFILE,
            profile_store=profile_store.return_value,
            dry_run=True,
            port=52700,
        )
        profile_store.assert_called_once_with(_PRIVATE_HOST_PROFILE)
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)

    @patch("local_web_server.cli._registration_profile_store", create=True)
    @patch("local_web_server.cli.AppRegistrar")
    def test_app_register_routes_privacy_safe_errors_through_cli_handler(
        self, registrar, profile_store
    ):
        from local_web_server.app_registration import AppRegistrationError

        registrar.return_value.register.side_effect = AppRegistrationError(
            "application is not eligible for registration"
        )

        code, stdout, stderr = self.run_main(
            ["app", "register", "--repository", "/tmp/private-recipe-notebook"]
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(
            stderr,
            "local-web: application is not eligible for registration\n",
        )
        self.assertNotIn("private-recipe-notebook", stderr)
        profile_store.assert_called_once_with(_PRIVATE_HOST_PROFILE)

    def test_app_register_accepts_explicit_apply_and_rejects_conflicting_modes(self):
        parser = build_parser()

        apply = parser.parse_args(["app", "register", "--apply"])

        self.assertTrue(apply.apply)
        self.assertFalse(apply.dry_run)
        with self.assertRaises(SystemExit):
            parser.parse_args(["app", "register", "--dry-run", "--apply"])

    @patch("local_web_server.cli.AppGenerator")
    def test_app_create_sanitises_generator_errors(self, generator):
        from local_web_server.app_generator import AppGenerationError

        generator.return_value.create.side_effect = AppGenerationError("app checks failed")

        code, stdout, stderr = self.run_main(
            [
                "app", "create", "recipes", "--title", "Recipe Collection",
                "--icon", "book", "--accent", "#8EA7C6",
            ]
        )

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "local-web: app checks failed\n")

    @patch("local_web_server.cli.AppDoctor")
    def test_app_doctor_json_emits_only_stable_machine_schema(self, doctor):
        doctor.return_value.inspect.return_value = DoctorReport(
            app="recipes",
            compatible=False,
            diagnostics=(
                Diagnostic(
                    code="ui.digest-mismatch",
                    severity="error",
                    message="Vendored UI package does not match provenance.",
                    remedy="Run local-web app update recipes.",
                ),
            ),
        )

        code, stdout, stderr = self.run_main(
            ["app", "doctor", "--repository", "/tmp/recipes", "--json"]
        )

        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(stdout),
            {
                "app": "recipes",
                "compatible": False,
                "diagnostics": [
                    {
                        "code": "ui.digest-mismatch",
                        "severity": "error",
                        "message": "Vendored UI package does not match provenance.",
                        "remedy": "Run local-web app update recipes.",
                    }
                ],
            },
        )
        self.assertEqual(stderr, "")
        doctor.return_value.inspect.assert_called_once_with(Path("/tmp/recipes"))

    @patch("local_web_server.cli.AppDoctor")
    def test_app_doctor_human_output_is_one_diagnostic_per_line(self, doctor):
        doctor.return_value.inspect.return_value = DoctorReport(
            "recipes",
            True,
            (Diagnostic("platform.legacy", "warning", "Legacy app.", "Migrate it."),),
        )

        code, stdout, stderr = self.run_main(
            ["app", "doctor", "--repository", "/tmp/recipes"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "WARNING platform.legacy: Legacy app. Migrate it.\n")
        self.assertEqual(stderr, "")

    @patch("local_web_server.cli.AppChecker")
    def test_app_check_runs_composed_checker_and_reports_success(self, checker):
        checker.return_value.check.return_value = DoctorReport("recipes", True, ())

        code, stdout, stderr = self.run_main(
            ["app", "check", "--repository", "/tmp/recipes"]
        )

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "recipes: checks passed\n")
        self.assertEqual(stderr, "")
        checker.return_value.check.assert_called_once_with(Path("/tmp/recipes"))
        self.registry_loader.assert_called_once_with(_PRIVATE_HOST_PROFILE_BYTES)
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)

    @patch("local_web_server.cli.load_registry_bytes")
    @patch("local_web_server.theme.ThemeStore")
    def test_theme_rollback_prints_only_the_restored_digest(self, store, load_registry):
        load_registry.return_value.runtime_root = Path("/runtime")
        digest = "a" * 64
        store.return_value.rollback.return_value = digest

        code, stdout, stderr = self.run_main(["theme", "rollback"])

        self.assertEqual(code, 0)
        self.assertEqual(stdout, f"platform theme rolled back to {digest}\n")
        self.assertEqual(stderr, "")
        load_registry.assert_called_once_with(_PRIVATE_HOST_PROFILE_BYTES)
        self.host_profile_resolver.assert_called_once_with(_PLATFORM_REPOSITORY)
        store.assert_called_once_with(Path("/runtime"))
        store.return_value.rollback.assert_called_once_with()

    def test_deploy_help_does_not_expose_hook_integration_flag(self):
        from local_web_server.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["deploy", "--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn("--from-hook", stdout.getvalue())

    def test_migration_help_keeps_normal_argparse_output(self):
        from local_web_server.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["app", "migrate-service-command", "--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn(
            "usage: local-web app migrate-service-command",
            stdout.getvalue(),
        )
        self.assertIn("--repository", stdout.getvalue())

    def test_public_base_path_migration_help_exposes_only_bounded_options(self):
        from local_web_server.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            redirect_stdout(stdout),
            redirect_stderr(stderr),
            self.assertRaises(SystemExit) as raised,
        ):
            main(["app", "migrate-public-base-path", "--help"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(stderr.getvalue(), "")
        help_output = stdout.getvalue()
        self.assertIn(
            "usage: local-web app migrate-public-base-path", help_output
        )
        for option in ("--repository", "--apply", "--json"):
            self.assertIn(option, help_output)
        for option in ("--value", "--environment", "--route", "--port"):
            self.assertNotIn(option, help_output)


if __name__ == "__main__":
    unittest.main()
