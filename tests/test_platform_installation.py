from contextlib import redirect_stderr
from io import StringIO
import tempfile
import subprocess
import unittest
from pathlib import Path
from unittest.mock import call, patch

from local_web_server.agent_skill import AgentSkillInstallResult
from local_web_server.config import ConfigError
from local_web_server.install import InstallError, InstallResult
from local_web_server.host_profile import HostProfileError
from local_web_server.host_profile import HostProfilePaths
from local_web_server.host_profile_store import HostProfileStore
from local_web_server.platform_installation import (
    install_platform,
    require_committed_installation_sources,
)
from scripts.install_local_web import main as install_main
from tests.helpers import init_git_repo


class RecordingProcessRunner:
    events = []

    def run(self, repository, commands):
        self.events.append(
            (
                "builds",
                Path(repository),
                tuple(command.label for command in commands),
            )
        )


class RecordingInstaller:
    events = []

    def __init__(self, registry, manifests, *, repository):
        self.repository = Path(repository)

    def install(self, dry_run=False, *, recover_missing_caddy=False, prepare_tailscale_port_migration=False):
        self.events.append(
            ("install", self.repository, dry_run, recover_missing_caddy)
        )
        if prepare_tailscale_port_migration:
            self.events.append(("prepare",))
        return InstallResult((Path("/tmp/home.html"),), (), dry_run)


class RecordingSkillInstaller:
    events = []

    def __init__(self, *, repository):
        self.repository = Path(repository)

    def install(self, dry_run=False):
        self.events.append(("skill", self.repository, dry_run))
        return AgentSkillInstallResult(Path("/tmp/skill"), True, dry_run)


class PlatformInstallationTests(unittest.TestCase):
    def setUp(self):
        RecordingProcessRunner.events = []
        RecordingInstaller.events = []
        RecordingSkillInstaller.events = []

    @staticmethod
    def _initialise_profile(repository: Path) -> tuple[Path, bytes]:
        registry_bytes = (
            '{"schemaVersion":1,"host":"fixture.local",'
            f'"runtimeRoot":"{repository / "runtime"}","apps":[]}}\n'
        ).encode("utf-8")
        init_git_repo(
            repository,
            {
                ".gitignore": "config/local/\n",
                "config/apps.example.json": "{}\n",
                "local_web_server/source.py": "# fixture\n",
            },
        )
        paths = HostProfilePaths.for_repository(repository)
        HostProfileStore(paths).initialise(registry_bytes)
        return paths.profile, registry_bytes

    def test_composes_build_manifest_install_and_skill_boundaries_in_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            private_profile, registry_bytes = self._initialise_profile(repository)
            events = []

            def load_registry(content):
                events.append(("registry", content))
                return object()

            def load_manifests(registry):
                events.append(("manifests", registry))
                return {}

            with (
                patch(
                    "local_web_server.platform_installation.require_committed_installation_sources"
                ),
                patch(
                    "local_web_server.platform_installation.resolve_host_profile",
                    create=True,
                    return_value=private_profile,
                ) as resolve_host_profile,
                patch(
                    "local_web_server.platform_installation.load_registry_bytes",
                    side_effect=load_registry,
                ),
                patch(
                    "local_web_server.platform_installation.load_main_manifests",
                    side_effect=load_manifests,
                ),
                patch(
                    "local_web_server.platform_installation.ProcessRunner",
                    RecordingProcessRunner,
                ),
                patch(
                    "local_web_server.platform_installation.Installer",
                    RecordingInstaller,
                ),
                patch(
                    "local_web_server.platform_installation.AgentSkillInstaller",
                    RecordingSkillInstaller,
                ),
            ):
                result = install_platform(repository, dry_run=True)

        self.assertEqual(
            RecordingProcessRunner.events,
            [
                (
                    "builds",
                    repository,
                    ("system-index-build", "ui-gallery-build"),
                )
            ],
        )
        self.assertEqual(events[0], ("registry", registry_bytes))
        self.assertEqual(events[1][0], "manifests")
        resolve_host_profile.assert_called_once_with(repository)
        self.assertEqual(
            RecordingInstaller.events,
            [("install", repository, True, False)],
        )
        self.assertEqual(RecordingSkillInstaller.events, [("skill", repository, True)])
        self.assertTrue(result.install.dry_run)
        self.assertTrue(result.agent_skill.dry_run)

    def test_propagates_explicit_missing_caddy_recovery_to_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            private_profile, _registry_bytes = self._initialise_profile(repository)
            with (
                patch(
                    "local_web_server.platform_installation.require_committed_installation_sources"
                ),
                patch(
                    "local_web_server.platform_installation.resolve_host_profile",
                    create=True,
                    return_value=private_profile,
                ) as resolve_host_profile,
                patch(
                    "local_web_server.platform_installation.load_registry_bytes",
                    return_value=object(),
                ),
                patch(
                    "local_web_server.platform_installation.load_main_manifests",
                    return_value={},
                ),
                patch(
                    "local_web_server.platform_installation.ProcessRunner",
                    RecordingProcessRunner,
                ),
                patch(
                    "local_web_server.platform_installation.Installer",
                    RecordingInstaller,
                ),
                patch(
                    "local_web_server.platform_installation.AgentSkillInstaller",
                    RecordingSkillInstaller,
                ),
            ):
                install_platform(
                    repository,
                    dry_run=False,
                    recover_missing_caddy=True,
                )
                install_platform(repository, dry_run=True, prepare_tailscale_port_migration=True)

        self.assertEqual(
            resolve_host_profile.call_args_list,
            [call(repository), call(repository)],
        )
        self.assertEqual(
            RecordingInstaller.events,
            [("install", repository, False, True), ("install", repository, True, False), ("prepare",)],
        )

    def test_explicit_disposable_registry_path_bypasses_private_profile_resolution(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "config").mkdir()
            disposable_registry = repository / "fixtures/registry.json"
            with (
                patch(
                    "local_web_server.platform_installation.require_committed_installation_sources"
                ),
                patch(
                    "local_web_server.platform_installation.resolve_host_profile",
                    create=True,
                    side_effect=AssertionError("default profile resolution was used"),
                ) as resolve_host_profile,
                patch(
                    "local_web_server.platform_installation.load_registry",
                    return_value=object(),
                ) as load_registry,
                patch(
                    "local_web_server.platform_installation.load_main_manifests",
                    return_value={},
                ),
                patch(
                    "local_web_server.platform_installation.ProcessRunner",
                    RecordingProcessRunner,
                ),
                patch(
                    "local_web_server.platform_installation.Installer",
                    RecordingInstaller,
                ),
                patch(
                    "local_web_server.platform_installation.AgentSkillInstaller",
                    RecordingSkillInstaller,
                ),
            ):
                install_platform(
                    repository,
                    dry_run=True,
                    registry_path=disposable_registry,
                )

        resolve_host_profile.assert_not_called()
        load_registry.assert_called_once_with(disposable_registry)

    def test_missing_private_profile_stops_install_before_build_or_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "config").mkdir()
            with (
                patch(
                    "local_web_server.platform_installation.require_committed_installation_sources"
                ),
                patch(
                    "local_web_server.platform_installation.resolve_host_profile",
                    create=True,
                    side_effect=HostProfileError(
                        "private host profile is missing; run local-web host init "
                        "or local-web host restore"
                    ),
                ) as resolve_host_profile,
                patch(
                    "local_web_server.platform_installation.load_registry",
                    side_effect=AssertionError("a public registry was opened"),
                ) as load_registry,
                patch(
                    "local_web_server.platform_installation.ProcessRunner",
                    side_effect=AssertionError("platform builds started"),
                ) as process_runner,
                patch(
                    "local_web_server.platform_installation.Installer",
                    side_effect=AssertionError("platform install started"),
                ) as installer,
            ):
                with self.assertRaisesRegex(
                    HostProfileError, "host init.*host restore"
                ):
                    install_platform(repository, dry_run=True)

        resolve_host_profile.assert_called_once_with(repository)
        load_registry.assert_not_called()
        process_runner.assert_not_called()
        installer.assert_not_called()

    def test_pending_host_store_stops_install_before_build_or_installer_construction(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            registry_bytes = (
                '{"schemaVersion":1,"host":"fixture.local",'
                f'"runtimeRoot":"{repository / "runtime"}","apps":[]}}\n'
            ).encode("utf-8")
            init_git_repo(
                repository,
                {
                    ".gitignore": "config/local/\n",
                    "config/apps.example.json": "{}\n",
                    "local_web_server/source.py": "# fixture\n",
                },
            )
            paths = HostProfilePaths.for_repository(repository)
            HostProfileStore(paths).initialise(registry_bytes)
            paths.transaction.write_bytes(b"marker-only-private-state")
            paths.transaction.chmod(0o600)

            with (
                patch(
                    "local_web_server.platform_installation.ProcessRunner",
                    side_effect=AssertionError("platform builds started"),
                ) as process_runner,
                patch(
                    "local_web_server.platform_installation.Installer",
                    side_effect=AssertionError("platform install started"),
                ) as installer,
            ):
                with self.assertRaisesRegex(
                    HostProfileError,
                    "^private host profile is invalid; run local-web host restore$",
                ):
                    install_platform(repository, dry_run=True)

        process_runner.assert_not_called()
        installer.assert_not_called()

    def test_invalid_default_private_profile_is_bounded_before_build_or_install(self):
        failures = (
            ConfigError(
                "cannot read registry: /private/PRIVATE_MALFORMED_MARKER/apps.json"
            ),
            ConfigError("registry contains unknown key: PRIVATE_SCHEMA_MARKER"),
            UnicodeDecodeError(
                "utf-8", b"\xffPRIVATE_BYTE_MARKER", 0, 1, "invalid start byte"
            ),
        )
        for failure in failures:
            with self.subTest(failure=str(failure)):
                with tempfile.TemporaryDirectory() as temporary:
                    repository = Path(temporary)
                    private_profile, registry_bytes = self._initialise_profile(repository)
                    with (
                        patch(
                            "local_web_server.platform_installation.require_committed_installation_sources"
                        ),
                        patch(
                            "local_web_server.platform_installation.resolve_host_profile",
                            return_value=private_profile,
                        ) as resolve_host_profile,
                        patch(
                            "local_web_server.platform_installation.load_registry_bytes",
                            side_effect=failure,
                        ) as load_registry,
                        patch(
                            "local_web_server.platform_installation.ProcessRunner",
                            side_effect=AssertionError("platform builds started"),
                        ) as process_runner,
                        patch(
                            "local_web_server.platform_installation.Installer",
                            side_effect=AssertionError("platform install started"),
                        ) as installer,
                    ):
                        with self.assertRaisesRegex(
                            HostProfileError,
                            "^private host profile is invalid; run local-web host restore$",
                        ) as raised:
                            install_platform(repository, dry_run=True)

                self.assertNotIn("PRIVATE_", str(raised.exception))
                self.assertNotIn("/private", str(raised.exception))
                resolve_host_profile.assert_called_once_with(repository)
                load_registry.assert_called_once_with(registry_bytes)
                process_runner.assert_not_called()
                installer.assert_not_called()

    def test_explicit_registry_validation_errors_preserve_library_types(self):
        failures = (
            ConfigError("fixture registry is invalid"),
            UnicodeDecodeError(
                "utf-8", b"\xffPRIVATE_BYTE_MARKER", 0, 1, "invalid start byte"
            ),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with tempfile.TemporaryDirectory() as temporary:
                    repository = Path(temporary)
                    (repository / "config").mkdir()
                    disposable_registry = repository / "fixtures/registry.json"
                    with (
                        patch(
                            "local_web_server.platform_installation.require_committed_installation_sources"
                        ),
                        patch(
                            "local_web_server.platform_installation.load_registry",
                            side_effect=failure,
                        ),
                    ):
                        with self.assertRaises(type(failure)) as raised:
                            install_platform(
                                repository,
                                dry_run=True,
                                registry_path=disposable_registry,
                            )

                self.assertIs(raised.exception, failure)

    def test_unexpected_default_registry_loader_errors_are_not_reclassified(self):
        failure = RuntimeError("unexpected loader failure")
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            private_profile, _registry_bytes = self._initialise_profile(repository)
            with (
                patch(
                    "local_web_server.platform_installation.require_committed_installation_sources"
                ),
                patch(
                    "local_web_server.platform_installation.resolve_host_profile",
                    return_value=private_profile,
                ),
                patch(
                    "local_web_server.platform_installation.load_registry_bytes",
                    side_effect=failure,
                ),
            ):
                with self.assertRaises(RuntimeError) as raised:
                    install_platform(repository, dry_run=True)

        self.assertIs(raised.exception, failure)

    def test_install_script_reports_missing_profile_without_a_traceback(self):
        with (
            patch(
                "scripts.install_local_web.install_platform",
                side_effect=HostProfileError(
                    "private host profile is missing; run local-web host init or "
                    "local-web host restore"
                ),
            ),
            redirect_stderr(StringIO()) as stderr,
        ):
            code = install_main(["--dry-run"])

        self.assertEqual(code, 2)
        self.assertEqual(
            stderr.getvalue(),
            "install-local-web: private host profile is missing; run local-web "
            "host init or local-web host restore\n",
        )

    def test_install_script_sanitises_invalid_default_profile_without_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            init_git_repo(repository, {"config/apps.example.json": "{}\n"})
            local = repository / "config/local"
            local.mkdir(mode=0o700)
            (local / "history").mkdir(mode=0o700)
            (local / "backups").mkdir(mode=0o700)
            profile = local / "apps.json"
            payloads = (
                '{"schemaVersion": 2, "PRIVATE_MALFORMED_MARKER": ',
                '{"schemaVersion":2,"publicOrigin":"https://local.example.ts.net",'
                '"ingressMode":"tailscale-serve","runtimeRoot":"/tmp/runtime",'
                '"apps":[],"PRIVATE_SCHEMA_MARKER":true}\n',
            )
            for payload in payloads:
                with self.subTest(payload=payload[:25]):
                    profile.write_text(payload, encoding="utf-8")
                    profile.chmod(0o600)
                    with (
                        patch("scripts.install_local_web.repository", repository),
                        patch(
                            "local_web_server.platform_installation.require_committed_installation_sources"
                        ),
                        patch(
                            "local_web_server.platform_installation.ProcessRunner",
                            side_effect=AssertionError("platform builds started"),
                        ) as process_runner,
                        patch(
                            "local_web_server.platform_installation.Installer",
                            side_effect=AssertionError("platform install started"),
                        ) as installer,
                        redirect_stderr(StringIO()) as stderr,
                    ):
                        code = install_main(["--dry-run"])

                    self.assertEqual(code, 2)
                    self.assertEqual(
                        stderr.getvalue(),
                        "install-local-web: private host profile is invalid; "
                        "run local-web host restore\n",
                    )
                    self.assertNotIn(str(profile), stderr.getvalue())
                    self.assertNotIn("PRIVATE_", stderr.getvalue())
                    process_runner.assert_not_called()
                    installer.assert_not_called()

    def test_install_script_sanitises_invalid_utf8_profile_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            init_git_repo(repository, {"config/apps.example.json": "{}\n"})
            local = repository / "config/local"
            local.mkdir(mode=0o700)
            (local / "history").mkdir(mode=0o700)
            (local / "backups").mkdir(mode=0o700)
            profile = local / "apps.json"
            profile.write_bytes(
                b'{"schemaVersion":2}\xffPRIVATE_INVALID_BYTE_MARKER'
            )
            profile.chmod(0o600)
            with (
                patch("scripts.install_local_web.repository", repository),
                patch(
                    "local_web_server.platform_installation.require_committed_installation_sources"
                ),
                patch(
                    "local_web_server.platform_installation.ProcessRunner",
                    side_effect=AssertionError("platform builds started"),
                ) as process_runner,
                patch(
                    "local_web_server.platform_installation.Installer",
                    side_effect=AssertionError("platform install started"),
                ) as installer,
                redirect_stderr(StringIO()) as stderr,
            ):
                code = install_main(["--dry-run"])

        self.assertEqual(code, 2)
        self.assertEqual(
            stderr.getvalue(),
            "install-local-web: private host profile is invalid; "
            "run local-web host restore\n",
        )
        self.assertNotIn(str(profile), stderr.getvalue())
        self.assertNotIn("PRIVATE_INVALID_BYTE_MARKER", stderr.getvalue())
        process_runner.assert_not_called()
        installer.assert_not_called()

    def test_source_guard_allows_unrelated_docs_but_rejects_installation_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            init_git_repo(
                repository,
                {
                    ".gitignore": "apps/*/test-results/\n",
                    "docs/note.md": "committed\n",
                    "local_web_server/module.py": "committed\n",
                },
            )
            (repository / "docs/note.md").write_text("unrelated draft\n")
            result = repository / "apps/system-index/test-results/.last-run.json"
            result.parent.mkdir(parents=True)
            result.write_text("generated\n")

            require_committed_installation_sources(repository)

            source = repository / "local_web_server/module.py"
            source.write_text("uncommitted source\n")
            with self.assertRaisesRegex(
                InstallError, "installation sources have uncommitted changes"
            ):
                require_committed_installation_sources(repository)

            subprocess.run(
                ("git", "restore", "local_web_server/module.py"),
                cwd=repository,
                check=True,
            )
            require_committed_installation_sources(repository)

    def test_source_guard_requires_main_only_for_a_real_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            init_git_repo(
                repository,
                {"local_web_server/module.py": "committed\n"},
            )
            subprocess.run(
                ("git", "switch", "-c", "feature/install"),
                cwd=repository,
                check=True,
                capture_output=True,
            )

            require_committed_installation_sources(
                repository, require_main=False
            )
            with self.assertRaisesRegex(InstallError, "cannot be verified"):
                require_committed_installation_sources(
                    repository, require_main=True
                )


if __name__ == "__main__":
    unittest.main()
