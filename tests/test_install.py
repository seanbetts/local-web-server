import json
import os
import plistlib
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stderr
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from local_web_server.install import InstallError, InstallResult, Installer, _atomic_write
from local_web_server.ingress import IngressVerificationError, TailscaleServeIngressVerifier
from local_web_server.agent_skill import AgentSkillInstallResult
from local_web_server.index_registry import INDEX_REGISTRY_NAME, render_index_registry
from local_web_server.theme import ThemeError, ThemeStore
from local_web_server.models import (
    AppManifest,
    BackendProbeSpec,
    BuildSpec,
    Command,
    HostApp,
    HostRegistry,
    HomePresentation,
    ServiceSpec,
)
from local_web_server.render import (
    render_caddy_plist,
    render_caddyfile,
    render_hook,
)
from local_web_server.platform_installation import PlatformInstallationResult
from scripts.install_local_web import build_parser as build_install_parser
from scripts.install_local_web import main as install_main
from tests.helpers import TEST_CADDY, caddy_integration, init_git_repo
from tests.ingress_fixture import ingress_transport
from tests.redirect_fixture import RedirectServer


class RecordingRunner:
    def __init__(self, *, caddy_loaded=False, caddy_states=None, ac_sleep=0):
        self.caddy_loaded = caddy_loaded
        self.caddy_states = list(caddy_states or ())
        self.caddy_pid = 4321
        self.caddy_started = "Sat Aug 22 12:00:00 2026"
        self.process_states = []
        self.ac_sleep = ac_sleep
        self.launchctl_returncode = None
        self.launchctl_stderr = None
        self.launchctl_stdout = None
        self.calls = []
        self.environments = []
        self.validate = None
        self.adapt = None
        self.bootout = None
        self.bootstrap = None
        self.reload = None
        self.process = None
        self.before_call = None
        self.timeouts = []

    def __call__(self, argv, *, env=None, timeout=None):
        argv = list(argv)
        self.calls.append(argv)
        self.environments.append(None if env is None else dict(env))
        self.timeouts.append(timeout)
        if self.before_call is not None:
            self.before_call(argv, timeout)
        if argv == ["pmset", "-g", "custom"]:
            output = (
                "Battery Power:\n sleep              5\n"
                f"AC Power:\n sleep              {self.ac_sleep}\n"
            )
            return subprocess.CompletedProcess(argv, 0, output, "")
        if argv[:2] == ["launchctl", "print"]:
            if self.caddy_states:
                state = self.caddy_states.pop(0)
                if state in {"loaded", "missing"}:
                    self.caddy_loaded = state == "loaded"
                returncode = {"loaded": 0, "missing": 3}.get(state, 1)
                stderr = {
                    "loaded": "",
                    "missing": (
                        'Could not find service "com.sean.local-web.caddy" '
                        f"in domain for user gui: {os.getuid()}\n"
                    ),
                }.get(state, "inspection failed")
                return subprocess.CompletedProcess(
                    argv,
                    returncode,
                    (
                        self.launchctl_stdout
                        if state == "loaded" and self.launchctl_stdout is not None
                        else (
                            f"state = running\npid = {self.caddy_pid}\n"
                            if state == "loaded"
                            else ""
                        )
                    ),
                    stderr,
                )
            return subprocess.CompletedProcess(
                argv,
                self.launchctl_returncode
                if self.launchctl_returncode is not None
                else (0 if self.caddy_loaded else 3),
                (
                    self.launchctl_stdout
                    if self.caddy_loaded and self.launchctl_stdout is not None
                    else (
                        f"state = running\npid = {self.caddy_pid}\n"
                        if self.caddy_loaded
                        else ""
                    )
                ),
                self.launchctl_stderr
                if self.launchctl_stderr is not None
                else (
                    ""
                    if self.caddy_loaded
                    else (
                        'Could not find service "com.sean.local-web.caddy" '
                        f"in domain for user gui: {os.getuid()}\n"
                    )
                ),
            )
        if argv[:4] == ["/bin/ps", "-p", str(self.caddy_pid), "-o"]:
            if self.process is not None:
                return self.process(argv)
            if self.process_states:
                state = self.process_states.pop(0)
                alive = state == "alive"
            else:
                alive = self.caddy_loaded
            return subprocess.CompletedProcess(
                argv,
                0 if alive else 1,
                f"{self.caddy_started}\n" if alive else "",
                "",
            )
        if argv[:2] == ["/opt/homebrew/bin/caddy", "validate"]:
            if self.validate is not None:
                return self.validate(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["/opt/homebrew/bin/caddy", "adapt"]:
            if self.adapt is not None:
                return self.adapt(argv)
            # Adapt parses configuration without starting a server or activating it.
            # Use Caddy itself so migration tests cannot invent listener semantics.
            if TEST_CADDY is None:
                raise unittest.SkipTest("requires Caddy 2")
            return subprocess.run(
                [str(TEST_CADDY), *argv[1:]],
                env=env,
                timeout=timeout,
                capture_output=True,
                text=True,
            )
        if argv[:2] == ["/opt/homebrew/bin/caddy", "reload"]:
            if self.reload is not None:
                return self.reload(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["launchctl", "bootout"]:
            if self.bootout is not None:
                return self.bootout(argv)
            self.caddy_loaded = False
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[:2] == ["launchctl", "bootstrap"]:
            if self.bootstrap is not None:
                result = self.bootstrap(argv)
            else:
                result = subprocess.CompletedProcess(argv, 0, "", "")
            if result.returncode == 0:
                self.caddy_loaded = True
            return result
        raise AssertionError(f"unexpected command: {argv}")


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class RecordingThemeGate:
    def __init__(self):
        self.calls = []
        self.error = None

    def verify(self, candidate):
        self.calls.append((Path(candidate), Path(candidate).read_bytes()))
        if self.error is not None:
            raise self.error


class RecordingIngressVerifier:
    def __init__(self):
        self.calls = []
        self.preflight_error = None
        self.loaded_error = None
        self.on_call = None

    def preflight(self, registry, *, require_success=False):
        self.require_success = require_success
        self.calls.append(("preflight", registry))
        if self.on_call:
            self.on_call("preflight")
        if self.preflight_error:
            raise self.preflight_error

    def verify_loaded(self, registry, caddy_pid, *, timeout=None):
        self.calls.append(("loaded", registry, caddy_pid))
        if self.on_call:
            self.on_call("loaded")
        if self.loaded_error:
            raise self.loaded_error

    def verify_prepared(self, registry, caddy_pid, *, timeout=None):
        self.calls.append(("prepared", registry, caddy_pid))
        if self.on_call:
            self.on_call("prepared")
        if self.loaded_error:
            raise self.loaded_error


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.runtime = self.home / "Library" / "Application Support" / "LocalWebServer"
        self.hosting_repository = self.root / "local-web-server"
        self.hosting_repository.mkdir()
        theme_directory = self.hosting_repository / "platform_assets"
        theme_directory.mkdir()
        theme_directory.joinpath("theme.css").write_bytes(
            (Path(__file__).parents[1] / "platform_assets" / "theme.css").read_bytes()
        )
        self.index_dist = self.hosting_repository / "apps" / "system-index" / "dist"
        self.index_assets = self.index_dist / "assets"
        self.index_assets.mkdir(parents=True)
        self.index_home = (
            b"<!doctype html><link rel='stylesheet' href='/_local-web/platform/index/assets/index-old.css'>"
            b"<script type='module' src='/_local-web/platform/index/assets/index-old.js'></script>"
        )
        (self.index_dist / "index.html").write_bytes(self.index_home)
        (self.index_assets / "index-old.css").write_bytes(b"body { color: black; }")
        (self.index_assets / "index-old.js").write_bytes(b"console.log('old')")
        gallery_dist = self.hosting_repository / "examples" / "ui-gallery" / "dist"
        gallery_assets = gallery_dist / "assets"
        gallery_assets.mkdir(parents=True)
        (gallery_dist / "index.html").write_bytes(b"<script src='/assets/gallery.js'></script>")
        (gallery_assets / "gallery.js").write_bytes(b"console.log('gallery')")
        self.repositories = self.root / "repositories"
        self.repositories.mkdir()

        self.static_repository = self.repositories / "static-app"
        self.static_repository.mkdir()
        init_git_repo(self.static_repository, {"README.md": "static\n"})
        self.service_repository = self.repositories / "service-app"
        self.service_repository.mkdir()
        init_git_repo(self.service_repository, {"README.md": "service\n"})

        self.app_data = self.root / "app-owned" / "data.sqlite"
        self.app_data.parent.mkdir()
        self.app_data.write_bytes(b"private application bytes")
        self.environment_file = self.root / "app-owned" / ".env"
        self.environment_file.write_text("PRIVATE_VALUE=unchanged\n", encoding="utf-8")

        build = BuildSpec((Command(("true",)),), Path("public"), ())
        self.manifests = {
            "static-app": AppManifest(
                1, "static-app", "Static App", "/static", "static", build, "/static/", None
            ),
            "service-app": AppManifest(
                1,
                "service-app",
                "Service App",
                "/service",
                "service",
                build,
                "/service/",
                ServiceSpec("service_app", "/health"),
            ),
        }
        self.registry = HostRegistry(
            host="test-mac.local",
            runtime_root=self.runtime,
            apps=(
                HostApp(
                    "static-app",
                    self.static_repository,
                    True,
                    self.environment_file,
                    (),
                    None,
                    None,
                ),
                HostApp(
                    "service-app",
                    self.service_repository,
                    True,
                    None,
                    (),
                    8765,
                    Command(
                        (
                            "/opt/homebrew/bin/python3",
                            "-m",
                            "service_app",
                            "--db",
                            str(self.app_data),
                        )
                    ),
                ),
            ),
        )
        self.runner = RecordingRunner()
        self.theme_gate = RecordingThemeGate()

    def tearDown(self):
        self.temp.cleanup()

    def installer(self, runner=None, **kwargs):
        kwargs.setdefault("theme_gate", self.theme_gate)
        return Installer(
            self.registry,
            self.manifests,
            repository=self.hosting_repository,
            home=self.home,
            run=runner or self.runner,
            **kwargs,
        )

    def use_tailscale_registry(self, *, prepared_fixture=False):
        self.registry = replace(
            self.registry, host="local.example.ts.net", schema_version=2,
            public_origin="https://local.example.ts.net", ingress_mode="tailscale-serve",
        )
        if prepared_fixture:
            (self.runtime / "Caddyfile").write_text(render_caddyfile(
                self.registry, self.manifests, prepare_tailscale_port_migration=True,
            ))

    def write_prior_prepared_bundle(self):
        self.use_tailscale_registry()
        return self.write_prior_replacement_bundle(render_caddyfile(
            self.registry, self.manifests, prepare_tailscale_port_migration=True,
        ).encode())

    def runtime_snapshot(self):
        return {
            path: (path.read_bytes() if path.is_file() else None, stat.S_IMODE(path.stat().st_mode))
            for base in (self.runtime, self.home / "Library" / "LaunchAgents")
            if base.exists()
            for path in (base, *base.rglob("*"))
        }

    def test_high_port_handover_requires_explicit_preparation_before_final_install(self):
        self.installer().install()
        self.use_tailscale_registry()
        self.runner.caddy_loaded = True
        prior = self.runtime_snapshot()
        self.runner.calls.clear()
        verifier = RecordingIngressVerifier()
        with self.assertRaisesRegex(InstallError, "requires port migration preparation"):
            self.installer(ingress_verifier=verifier).install()
        self.assertEqual(self.runtime_snapshot(), prior)
        self.assertFalse(any(call[1] in ("reload", "bootout", "bootstrap") for call in self.runner.calls))

    def test_high_port_preparation_then_finalisation_and_idempotence(self):
        self.installer().install()
        self.use_tailscale_registry()
        self.runner.caddy_loaded = True
        verifier = RecordingIngressVerifier()
        installer = self.installer(ingress_verifier=verifier)
        for prepared in (True, False):
            with self.subTest(prepared=prepared):
                self.runner.calls.clear()
                verifier.calls.clear()
                result = installer.install(prepare_tailscale_port_migration=prepared)
                expected_phase = "prepared" if prepared else "loaded"
                self.assertEqual(verifier.calls[-1], (expected_phase, self.registry, 4321))
                self.assertEqual(verifier.require_success, prepared)
                self.assertTrue(any(call[1] == "bootout" for call in self.runner.calls))
                self.assertFalse(any(call[1] == "reload" for call in self.runner.calls))
                content = (self.runtime / "Caddyfile").read_text()
                self.assertIn("127.0.0.1", content)
                self.assertIn(":8080 {", content)
                self.assertEqual("\n:80 {\n" in content, prepared)
                if prepared:
                    self.assertTrue(any("not final security activation" in item for item in result.messages))
                self.runner.calls.clear()
                installer.install(prepare_tailscale_port_migration=prepared)
                self.assertFalse(any(call[1] in ("reload", "bootout", "bootstrap") for call in self.runner.calls))

    def test_high_port_preparation_rejects_version_one_and_final_state_without_writes(self):
        with self.assertRaisesRegex(InstallError, "requires Tailscale ingress"):
            self.installer().install(prepare_tailscale_port_migration=True, dry_run=True)
        self.assertFalse(self.runtime.exists())
        self.installer().install()
        self.use_tailscale_registry()
        (self.runtime / "Caddyfile").write_text(render_caddyfile(self.registry, self.manifests))
        self.runner.caddy_loaded = True
        prior = self.runtime_snapshot()
        with self.assertRaisesRegex(InstallError, "unsupported Caddy listener topology"):
            self.installer(ingress_verifier=RecordingIngressVerifier()).install(
                prepare_tailscale_port_migration=True,
            )
        self.assertEqual(self.runtime_snapshot(), prior)

    def test_high_port_unknown_topology_is_rejected_before_any_managed_mutation(self):
        self.installer().install()
        self.use_tailscale_registry(prepared_fixture=True)
        caddyfile = self.runtime / "Caddyfile"
        with caddyfile.open("a") as output:
            output.write("\nhttp://:54321 {\n    bind 127.0.0.1\n    respond 204\n}\n")
        self.runner.caddy_loaded = True
        prior = self.runtime_snapshot()
        for prepared in (False, True):
            self.runner.calls.clear()
            with self.assertRaisesRegex(InstallError, "unsupported Caddy listener topology"):
                self.installer(ingress_verifier=RecordingIngressVerifier()).install(
                    prepare_tailscale_port_migration=prepared,
                )
            self.assertEqual(self.runtime_snapshot(), prior)
            self.assertFalse(any(call[1] in ("reload", "bootout", "bootstrap") for call in self.runner.calls))

    def test_high_port_preparation_rejects_recovery_flag(self):
        self.use_tailscale_registry()
        with self.assertRaisesRegex(InstallError, "cannot recover missing Caddy"):
            self.installer().install(
                recover_missing_caddy=True, prepare_tailscale_port_migration=True,
            )
        self.assertFalse(self.runtime.exists())

    def test_high_port_each_phase_failure_restores_exact_prior_listener_bundle(self):
        self.installer().install()
        self.use_tailscale_registry()
        self.runner.caddy_loaded = True
        verifier = RecordingIngressVerifier()
        clock = FakeClock()
        installer = self.installer(ingress_verifier=verifier, monotonic=clock.monotonic, sleep=clock.sleep)
        for prepared in (True, False):
            for stage in ("validate", "bootstrap", "ingress"):
                with self.subTest(prepared=prepared, stage=stage):
                    prior = self.runtime_snapshot()
                    self.runner.calls.clear()
                    verifier.loaded_error = IngressVerificationError("private provider") if stage == "ingress" else None
                    self.runner.validate = (lambda argv: subprocess.CompletedProcess(argv, 1, "", "private path")) if stage == "validate" else None
                    calls = []
                    def bootstrap(argv):
                        calls.append(argv)
                        return subprocess.CompletedProcess(argv, 1 if len(calls) == 1 else 0, "", "private host")
                    self.runner.bootstrap = bootstrap if stage == "bootstrap" else None
                    with self.assertRaises(InstallError) as caught:
                        installer.install(prepare_tailscale_port_migration=prepared)
                    self.assertNotIn("private", str(caught.exception))
                    self.assertNotIn(str(self.root), str(caught.exception))
                    self.assertNotIn(self.registry.host, str(caught.exception))
                    self.assertEqual(self.runtime_snapshot(), prior)
                    self.assertTrue(self.runner.caddy_loaded)
                    if stage == "validate":
                        self.assertFalse(any(call[1] == "bootout" for call in self.runner.calls))
            self.runner.validate = self.runner.bootstrap = None
            verifier.loaded_error = None
            installer.install(prepare_tailscale_port_migration=prepared)

    def test_high_port_preparation_preview_is_private_and_does_not_inspect_live_state(self):
        self.use_tailscale_registry()
        verifier = RecordingIngressVerifier()
        result = self.installer(ingress_verifier=verifier).install(
            dry_run=True, prepare_tailscale_port_migration=True,
        )
        summary = " ".join(result.dry_run_summary)
        self.assertIn("127.0.0.1:8080", summary)
        self.assertIn("wildcard port 80", summary)
        self.assertNotIn(self.registry.host, summary)
        self.assertNotIn(str(self.root), summary)
        self.assertEqual(verifier.calls, [])
        self.assertEqual(self.runner.calls, [["pmset", "-g", "custom"]])
        self.assertFalse(self.runtime.exists())

    def test_tailscale_orders_preflight_validation_replacement_verification_then_platform_writes(self):
        self.installer().install()
        old_registry = (self.runtime / "platform" / "index" / INDEX_REGISTRY_NAME).read_bytes()
        self.manifests["static-app"] = replace(self.manifests["static-app"], route="/updated-static")
        self.use_tailscale_registry()
        self.runner.caddy_loaded = True
        verifier = RecordingIngressVerifier()
        events = []
        from local_web_server.install import _atomic_write

        def record_write(path, content, mode):
            events.append("caddy-write" if path.name == "Caddyfile" else "platform-write")
            _atomic_write(path, content, mode)

        def observe_command(argv, timeout):
            if argv[:2] == ["/opt/homebrew/bin/caddy", "validate"]:
                events.append("validate")
            elif argv[:2] == ["/opt/homebrew/bin/caddy", "adapt"]:
                events.append("adapt")
            elif argv[:2] == ["launchctl", "bootout"]:
                events.append("bootout")
            elif argv[:2] == ["launchctl", "bootstrap"]:
                events.append("bootstrap")

        def observe_verifier(phase):
            events.append(phase)
            self.assertEqual((self.runtime / "platform" / "index" / INDEX_REGISTRY_NAME).read_bytes(), old_registry)
            if phase == "prepared":
                self.assertIn(b"bind 127.0.0.1", (self.runtime / "Caddyfile").read_bytes())

        verifier.on_call = observe_verifier
        self.runner.before_call = observe_command
        with patch("local_web_server.install._atomic_write", side_effect=record_write):
            self.installer(ingress_verifier=verifier).install(prepare_tailscale_port_migration=True)
        self.assertEqual(
            events[:9],
            [
                "preflight",
                "adapt",
                "validate",
                "adapt",
                "adapt",
                "caddy-write",
                "bootout",
                "bootstrap",
                "prepared",
            ],
        )
        self.assertTrue(events[9:])
        self.assertEqual(set(events[9:]), {"platform-write"})
        self.assertEqual(verifier.calls, [("preflight", self.registry), ("prepared", self.registry, 4321)])
        self.assertFalse(
            any(call[:2] == ["/opt/homebrew/bin/caddy", "reload"] for call in self.runner.calls)
        )
        self.assertNotEqual((self.runtime / "platform" / "index" / INDEX_REGISTRY_NAME).read_bytes(), old_registry)

    def test_return_to_trusted_lan_replaces_loaded_job_for_listener_scope_change(self):
        self.installer().install()
        self.use_tailscale_registry(prepared_fixture=True)
        self.runner.caddy_loaded = True
        self.installer(ingress_verifier=RecordingIngressVerifier()).install()
        self.runner.calls.clear()
        self.registry = replace(
            self.registry,
            host="system-index.local",
            schema_version=1,
            public_origin="http://system-index.local",
            ingress_mode="trusted-lan",
        )

        self.installer().install()

        self.assertTrue(
            any(call[:2] == ["launchctl", "bootout"] for call in self.runner.calls)
        )
        self.assertTrue(
            any(call[:2] == ["launchctl", "bootstrap"] for call in self.runner.calls)
        )
        self.assertFalse(
            any(
                call[:2] == ["/opt/homebrew/bin/caddy", "reload"]
                for call in self.runner.calls
            )
        )
        self.assertNotIn(b"bind 127.0.0.1", (self.runtime / "Caddyfile").read_bytes())

    def test_listener_transition_uses_topology_not_an_incidental_bind_directive(self):
        self.installer().install()
        caddyfile = self.runtime / "Caddyfile"
        caddyfile.write_text(
            caddyfile.read_text(encoding="utf-8")
            + "http://127.0.0.1:54321 {\n"
            + "    bind 127.0.0.1\n"
            + "    respond 204\n"
            + "}\n",
            encoding="utf-8",
        )
        self.runner.caddy_loaded = True
        self.runner.calls.clear()

        self.installer(ingress_verifier=RecordingIngressVerifier()).install()

        self.assertTrue(
            any(call[:2] == ["launchctl", "bootout"] for call in self.runner.calls)
        )
        self.assertTrue(
            any(call[:2] == ["launchctl", "bootstrap"] for call in self.runner.calls)
        )
        self.assertFalse(
            any(
                call[:2] == ["/opt/homebrew/bin/caddy", "reload"]
                for call in self.runner.calls
            )
        )

    def test_listener_topology_failure_is_sanitised_and_write_free(self):
        self.installer().install()
        prior = self.runtime_snapshot()
        self.runner.caddy_loaded = True
        self.runner.calls.clear()
        self.manifests["static-app"] = replace(
            self.manifests["static-app"], route="/updated-static"
        )
        private_marker = "private-topology-provider-detail"
        self.runner.adapt = lambda argv: subprocess.CompletedProcess(
            argv, 1, "", private_marker
        )

        with self.assertRaisesRegex(
            InstallError, "Caddy listener topology could not be determined"
        ) as caught:
            self.installer().install()

        self.assertNotIn(private_marker, str(caught.exception))
        self.assertEqual(self.runtime_snapshot(), prior)
        self.assertFalse(
            any(
                call[:2]
                in (
                    ["launchctl", "bootout"],
                    ["launchctl", "bootstrap"],
                    ["/opt/homebrew/bin/caddy", "reload"],
                )
                for call in self.runner.calls
            )
        )

    def test_tailscale_dry_run_performs_no_ingress_or_caddy_inspection(self):
        self.use_tailscale_registry()
        verifier = RecordingIngressVerifier()
        result = self.installer(ingress_verifier=verifier).install(dry_run=True)
        self.assertTrue(result.dry_run)
        self.assertEqual(verifier.calls, [])
        self.assertEqual(self.runner.calls, [["pmset", "-g", "custom"]])
        self.assertFalse(self.runtime.exists())

    def test_tailscale_dry_run_summary_is_private_and_preserves_existing_runtime(self):
        self.installer().install()
        before = self.runtime_snapshot()
        self.use_tailscale_registry()
        self.runner.calls.clear()
        verifier = RecordingIngressVerifier()
        with patch("socket.create_connection", side_effect=AssertionError("unexpected network")):
            result = self.installer(ingress_verifier=verifier).install(dry_run=True)
        summary = "\n".join(result.dry_run_summary)
        self.assertIn("Tailscale Serve", summary)
        self.assertIn("tailscale-serve", summary)
        self.assertIn("loopback-only", summary)
        self.assertIn("127.0.0.1:8080", summary)
        self.assertNotIn(str(self.root), summary)
        self.assertNotIn(self.registry.host, summary)
        self.assertEqual(self.runtime_snapshot(), before)
        self.assertEqual(verifier.calls, [])
        self.assertEqual(self.runner.calls, [["pmset", "-g", "custom"]])

        with (
            patch(
                "scripts.install_local_web.install_platform",
                return_value=PlatformInstallationResult(
                    result,
                    AgentSkillInstallResult(
                        self.home / ".codex/skills/local-web-app-development", True, True,
                    ),
                ),
            ),
            patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            self.assertEqual(install_main(["--dry-run"]), 0)
        output = stdout.getvalue()
        self.assertIn("Tailscale Serve", output)
        self.assertIn("loopback-only", output)
        self.assertIn(f"Would write: {len(result.writes)} managed files", output)
        self.assertIn("Would link agent skill: local-web-app-development", output)
        self.assertNotIn(str(self.root), output)
        self.assertNotIn(self.registry.host, output)

    def test_tailscale_preflight_failure_preserves_all_runtime_bytes_modes_and_process(self):
        self.installer().install()
        (self.runtime / "Caddyfile").chmod(0o640)
        self.runtime.chmod(0o750)
        before = self.runtime_snapshot()
        self.use_tailscale_registry()
        self.runner.caddy_loaded = True
        self.runner.calls.clear()
        verifier = RecordingIngressVerifier()
        verifier.preflight_error = IngressVerificationError("private origin provider details")
        with self.assertRaisesRegex(InstallError, "Tailscale HTTPS ingress preflight failed") as caught:
            self.installer(ingress_verifier=verifier).install()
        self.assertNotIn("private", repr(caught.exception))
        self.assertEqual(self.runtime_snapshot(), before)
        self.assertTrue(self.runner.caddy_loaded)
        self.assertFalse(any(call[0] == "/opt/homebrew/bin/caddy" or call[:2] in
                             (["launchctl", "bootout"], ["launchctl", "bootstrap"])
                             for call in self.runner.calls))

    def test_tailscale_requires_preexisting_loaded_managed_job_even_with_recovery_requested(self):
        for recover in (False, True):
            with self.subTest(recover=recover):
                self.write_prior_caddy_bundle()
                before = self.runtime_snapshot()
                self.use_tailscale_registry()
                verifier = RecordingIngressVerifier()
                self.runner.calls.clear()
                with self.assertRaisesRegex(InstallError, "requires an already loaded managed Caddy"):
                    self.installer(ingress_verifier=verifier).install(recover_missing_caddy=recover)
                self.assertEqual(verifier.calls, [])
                self.assertEqual(self.runtime_snapshot(), before)
                self.assertFalse(self.runner.caddy_loaded)
                self.assertFalse(any(call[:2] == ["launchctl", "bootstrap"] for call in self.runner.calls))

    def test_tailscale_requires_running_process_before_preflight(self):
        self.write_prior_caddy_bundle()
        before = self.runtime_snapshot()
        self.use_tailscale_registry()
        self.runner.caddy_loaded = True
        self.runner.launchctl_stdout = "state = waiting\n"
        verifier = RecordingIngressVerifier()
        with self.assertRaisesRegex(InstallError, "requires an already loaded managed Caddy"):
            self.installer(ingress_verifier=verifier).install()
        self.assertEqual(verifier.calls, [])
        self.assertEqual(self.runtime_snapshot(), before)

    def test_tailscale_loaded_failure_restores_exact_pair_registry_and_running_reload_state(self):
        self.installer().install()
        self.use_tailscale_registry(prepared_fixture=True)
        self.runner.caddy_loaded = True
        self.installer(ingress_verifier=RecordingIngressVerifier()).install()
        self.runner.calls.clear()
        self.manifests["static-app"] = replace(self.manifests["static-app"], route="/updated-static")
        caddyfile = self.runtime / "Caddyfile"
        caddyfile.chmod(0o640)
        prior = self.runtime_snapshot()
        reloaded = []

        def observe_reload(argv):
            reloaded.append(caddyfile.read_bytes())
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.runner.reload = observe_reload
        verifier = RecordingIngressVerifier()
        verifier.loaded_error = IngressVerificationError("private HTTPS or listener failure")
        with self.assertRaisesRegex(InstallError, "Tailscale HTTPS ingress verification failed") as caught:
            self.installer(ingress_verifier=verifier).install()
        self.assertNotIn("private", repr(caught.exception))
        self.assertEqual(self.runtime_snapshot(), prior)
        self.assertEqual(len(reloaded), 2)
        self.assertIn(b"bind 127.0.0.1", reloaded[0])
        self.assertEqual(reloaded[1], prior[caddyfile][0])
        self.assertTrue(self.runner.caddy_loaded)
        self.assertEqual(self.runner.caddy_pid, 4321)
        self.assertFalse(any(call[:2] == ["launchctl", "bootout"] for call in self.runner.calls))

    @caddy_integration
    def test_wrong_forwarded_host_fails_loaded_probe_and_restores_exact_installation(self):
        # Caddy's unmatched-Host empty 200 must not commit a broken migration.
        # launchd and the TLS socket are external boundaries; the installer,
        # verifier, rendered route, HEAD response, and file recovery are real.
        self.installer().install()
        self.use_tailscale_registry(prepared_fixture=True)
        self.runner.caddy_loaded = True
        self.installer(ingress_verifier=RecordingIngressVerifier()).install()
        self.runner.calls.clear()
        caddyfile = self.runtime / "Caddyfile"
        caddyfile.chmod(0o640)
        prior = self.runtime_snapshot()
        self.manifests["static-app"] = replace(self.manifests["static-app"], route="/updated-static")
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        processes = []
        reloaded = []

        def stop(process):
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

        def load_disposable_caddy(argv=None):
            if processes:
                stop(processes[-1])
            state = self.root / f"caddy-socket-{len(processes)}"
            state.mkdir()
            content = caddyfile.read_text().replace(":8080 {", f":{port} {{")
            if content.startswith("{\n"):
                content = content.replace("{\n", "{\n    admin off\n    skip_install_trust\n", 1)
            else:
                content = "{\n    admin off\n    skip_install_trust\n}\n" + content.replace(
                    f":{port} {{", f":{port} {{\n    bind 127.0.0.1", 1,
                )
            config = state / "Caddyfile"
            config.write_text(content)
            with (state / "output.log").open("wb") as output:
                process = subprocess.Popen(
                    [str(TEST_CADDY), "run", "--config", str(config), "--adapter", "caddyfile"],
                    env={"PATH": "/usr/bin:/bin", "XDG_DATA_HOME": str(state / "data"),
                         "XDG_CONFIG_HOME": str(state / "config")},
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                )
            processes.append(process)
            self.addCleanup(stop, process)
            deadline = time.monotonic() + 5
            while True:
                self.assertIsNone(process.poll(), "disposable Caddy exited")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        break
                except OSError:
                    self.assertLess(time.monotonic(), deadline, "disposable Caddy did not start")
                    time.sleep(0.02)
            if argv is not None:
                reloaded.append(caddyfile.read_bytes())
            return subprocess.CompletedProcess(argv or [], 0, "", "")

        def inspect_listener(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, "n127.0.0.1:8080\n", "")

        load_disposable_caddy()
        self.runner.reload = load_disposable_caddy
        verifier = TailscaleServeIngressVerifier(run=inspect_listener)
        with ingress_transport(port, host_header="attacker.example"):
            with self.assertRaisesRegex(InstallError, "Tailscale HTTPS ingress verification failed"):
                self.installer(ingress_verifier=verifier).install()
            # The restored route responds, but the wrong Host cannot satisfy the 2xx gate.
            verifier.preflight(self.registry)
        self.assertEqual(self.runtime_snapshot(), prior)
        self.assertEqual(len(reloaded), 2)
        self.assertEqual(reloaded[1], prior[caddyfile][0])
        self.assertTrue(self.runner.caddy_loaded)
        self.assertEqual(self.runner.caddy_pid, 4321)

    def test_tailscale_loaded_failure_restores_exact_pair_and_prior_job_after_replacement(self):
        self.installer().install()
        self.manifests["static-app"] = replace(self.manifests["static-app"], route="/updated-static")
        caddyfile, plist = self.write_prior_prepared_bundle()
        caddyfile.chmod(0o640)
        plist.chmod(0o644)
        prior = self.runtime_snapshot()
        self.use_tailscale_registry()
        self.runner.caddy_loaded = True
        bootstrapped = []

        def observe_bootstrap(argv):
            bootstrapped.append((caddyfile.read_bytes(), plist.read_bytes()))
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.runner.bootstrap = observe_bootstrap
        verifier = RecordingIngressVerifier()
        verifier.loaded_error = IngressVerificationError("private verification failure")
        with self.assertRaisesRegex(InstallError, "Tailscale HTTPS ingress verification failed"):
            self.installer(ingress_verifier=verifier).install()
        self.assertEqual(self.runtime_snapshot(), prior)
        self.assertEqual(len(bootstrapped), 2)
        self.assertIn(b"bind 127.0.0.1", bootstrapped[0][0])
        self.assertEqual(bootstrapped[1], (prior[caddyfile][0], prior[plist][0]))
        self.assertTrue(self.runner.caddy_loaded)

    def test_tailscale_replacement_waits_for_running_process_before_healthy_head(self):
        # launchctl bootstrap can register a waiting job before its PID exists;
        # immediate PID readback would reject a candidate that starts normally.
        self.installer().install()
        plist = self.home / "Library" / "LaunchAgents" / "com.sean.local-web.caddy.plist"
        plist.write_bytes(render_caddy_plist(self.hosting_repository / "old-checkout", self.runtime))
        self.use_tailscale_registry(prepared_fixture=True)
        self.runner.caddy_loaded = True
        clock = FakeClock()
        bootstraps = []

        def bootstrap(argv):
            bootstraps.append(plist.read_bytes())
            self.runner.launchctl_stdout = "state = waiting\n" if len(bootstraps) == 1 else None
            return subprocess.CompletedProcess(argv, 0, "", "")

        def sleep(seconds):
            clock.sleep(seconds)
            if clock.now >= 0.1:
                self.runner.launchctl_stdout = None
                self.runner.caddy_pid = 4322
                self.runner.caddy_started = "Sat Sep 5 20:00:00 2026"

        def listener(argv, *, timeout):
            self.assertEqual(argv[4], "4322")
            return subprocess.CompletedProcess(argv, 0, "n127.0.0.1:8080\n", "")

        self.runner.bootstrap = bootstrap
        with RedirectServer(None) as server:
            verifier = TailscaleServeIngressVerifier(run=listener)
            try:
                result = self.installer(
                    ingress_verifier=verifier, monotonic=clock.monotonic, sleep=sleep,
                ).install()
            except InstallError as error:
                self.fail(f"normally starting candidate was rejected before readiness: {error}")
        self.assertFalse(result.dry_run)
        self.assertEqual(len(bootstraps), 1)
        self.assertEqual(clock.now, 0.1)
        self.assertEqual(server.requests, [
            ("HEAD", "/_local-web/platform/index/registry-v1.json", "local.example.ts.net"),
            ("HEAD", "/_local-web/platform/index/registry-v1.json", "local.example.ts.net"),
        ])
        self.assertEqual(self.runner.caddy_pid, 4322)

    def test_tailscale_replacement_waiting_deadline_restores_exact_prior_installation(self):
        # A registered candidate that never starts must exhaust a bounded phase,
        # restore the exact prior pair and verify the restored running process.
        self.installer().install()
        caddyfile = self.runtime / "Caddyfile"
        plist = self.home / "Library" / "LaunchAgents" / "com.sean.local-web.caddy.plist"
        plist.write_bytes(render_caddy_plist(self.hosting_repository / "old-checkout", self.runtime))
        self.use_tailscale_registry(prepared_fixture=True)
        caddyfile.chmod(0o640)
        plist.chmod(0o644)
        prior = self.runtime_snapshot()
        self.use_tailscale_registry()
        self.runner.caddy_loaded = True
        clock = FakeClock()
        bootstraps = []

        def bootstrap(argv):
            bootstraps.append((caddyfile.read_bytes(), plist.read_bytes()))
            self.runner.launchctl_stdout = "state = waiting\n" if len(bootstraps) == 1 else None
            return subprocess.CompletedProcess(argv, 0, "", "")

        def listener(argv, *, timeout):
            self.fail("a candidate without a running PID must not reach listener verification")

        self.runner.bootstrap = bootstrap
        with RedirectServer(None) as server:
            verifier = TailscaleServeIngressVerifier(run=listener)
            with self.assertRaises(InstallError):
                self.installer(
                    ingress_verifier=verifier, monotonic=clock.monotonic, sleep=clock.sleep,
                ).install()
        self.assertEqual(self.runtime_snapshot(), prior)
        self.assertEqual(clock.now, 2.0)
        self.assertEqual(len(bootstraps), 2)
        self.assertEqual(bootstraps[1], (prior[caddyfile][0], prior[plist][0]))
        self.assertTrue(self.runner.caddy_loaded)
        self.assertEqual(self.runner.caddy_pid, 4321)
        self.assertEqual(server.requests, [
            ("HEAD", "/_local-web/platform/index/registry-v1.json", "local.example.ts.net"),
        ])

    def test_tailscale_replacement_waits_for_healthy_ingress_after_pid_exists(self):
        # A running PID can precede the listener binding: do not roll back a
        # normally starting candidate on Serve's first real HTTP 502 response.
        self.installer().install()
        self.write_prior_prepared_bundle()
        self.manifests["static-app"] = replace(self.manifests["static-app"], title="Ready candidate")
        self.use_tailscale_registry()
        self.runner.caddy_loaded = True
        clock = FakeClock()
        bootstraps = []

        def bootstrap(argv):
            bootstraps.append(argv)
            self.runner.caddy_pid = 4322
            self.runner.caddy_started = "Sat Sep 5 20:00:00 2026"
            return subprocess.CompletedProcess(argv, 0, "", "")

        def listener(argv, *, timeout):
            self.assertEqual(argv[4], "4322")
            return subprocess.CompletedProcess(argv, 0, "n127.0.0.1:8080\n", "")

        self.runner.bootstrap = bootstrap
        with RedirectServer(None, response_status=lambda request: 502 if request == 2 else 200) as server:
            verifier = TailscaleServeIngressVerifier(run=listener)
            try:
                result = self.installer(
                    ingress_verifier=verifier, monotonic=clock.monotonic, sleep=clock.sleep,
                ).install()
            except InstallError as error:
                self.fail(f"candidate with a running PID was rejected on transient ingress: {error}")
        self.assertFalse(result.dry_run)
        self.assertEqual(len(bootstraps), 1)
        self.assertGreater(clock.now, 0)
        self.assertLessEqual(clock.now, 2)
        self.assertEqual(server.requests, [
            ("HEAD", "/_local-web/platform/index/registry-v1.json", "local.example.ts.net"),
        ] * 3)
        self.assertIn(b"Ready candidate", (self.runtime / "platform" / "index" / INDEX_REGISTRY_NAME).read_bytes())
        self.assertEqual(self.runner.caddy_pid, 4322)

    def test_tailscale_replacement_unavailable_ingress_deadline_restores_exact_installation(self):
        # Persistent 502 must receive a bounded readiness opportunity, then fail
        # before later platform writes and recover the prior bundle/process.
        self.installer().install()
        caddyfile, plist = self.write_prior_prepared_bundle()
        caddyfile.chmod(0o640)
        plist.chmod(0o644)
        prior = self.runtime_snapshot()
        self.use_tailscale_registry()
        self.runner.caddy_loaded = True
        clock = FakeClock()
        bootstraps = []
        requests_at = []
        writes = []

        def write(path, content, mode):
            writes.append(path)
            _atomic_write(path, content, mode)

        def bootstrap(argv):
            bootstraps.append((caddyfile.read_bytes(), plist.read_bytes()))
            self.runner.caddy_pid = 4322 if len(bootstraps) == 1 else 4323
            self.runner.caddy_started = "Sat Sep 5 20:00:00 2026"
            return subprocess.CompletedProcess(argv, 0, "", "")

        def status(request):
            requests_at.append(clock.now)
            return 200 if request == 1 else 502

        def listener(argv, *, timeout):
            self.fail("HTTP 502 must not pass loaded ingress verification")

        self.runner.bootstrap = bootstrap
        with RedirectServer(None, response_status=status) as server, patch(
            "local_web_server.install._atomic_write", side_effect=write,
        ):
            verifier = TailscaleServeIngressVerifier(run=listener)
            with self.assertRaisesRegex(InstallError, "Tailscale HTTPS ingress verification failed") as caught:
                self.installer(
                    ingress_verifier=verifier, monotonic=clock.monotonic, sleep=clock.sleep,
                ).install()
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(writes, [caddyfile, plist, caddyfile, plist])
        self.assertEqual(self.runtime_snapshot(), prior)
        self.assertEqual(len(bootstraps), 2)
        self.assertEqual(bootstraps[1], (prior[caddyfile][0], prior[plist][0]))
        self.assertTrue(self.runner.caddy_loaded)
        self.assertEqual(self.runner.caddy_pid, 4323)
        self.assertIn(["/bin/ps", "-p", "4323", "-o", "lstart="], self.runner.calls[-2:])
        self.assertGreater(len(server.requests), 2)
        self.assertEqual(clock.now, 2.0)
        self.assertLess(max(requests_at), 2.0)

    def test_tailscale_rerun_verifies_existing_version_two_without_reloading(self):
        self.installer().install()
        self.use_tailscale_registry(prepared_fixture=True)
        self.runner.caddy_loaded = True
        verifier = RecordingIngressVerifier()
        installer = self.installer(ingress_verifier=verifier)
        installer.install()
        self.runner.calls.clear()
        verifier.calls.clear()
        installer.install()
        self.assertEqual(verifier.calls, [("preflight", self.registry), ("loaded", self.registry, 4321)])
        self.assertFalse(any(call[:2] == ["/opt/homebrew/bin/caddy", "reload"] for call in self.runner.calls))

    def test_tailscale_replacement_caps_https_timeout_to_remaining_readiness(self):
        # A fixed per-probe timeout must not restart the readiness budget after
        # an earlier attempt has consumed most of it.
        self.installer().install()
        self.write_prior_prepared_bundle()
        self.use_tailscale_registry()
        self.runner.caddy_loaded = True
        clock = FakeClock()
        timeouts = []

        def status(request):
            if request == 2:
                clock.now += 1.5
                return 502
            return 200

        def listener(argv, *, timeout):
            self.assertLessEqual(timeout, 0.45)
            return subprocess.CompletedProcess(argv, 0, "n127.0.0.1:8080\n", "")

        with RedirectServer(None, response_status=status):
            from local_web_server.ingress import _https_status

            def probe(host, deadline, monotonic):
                timeouts.append(deadline - monotonic())
                return _https_status(host, deadline, monotonic)

            verifier = TailscaleServeIngressVerifier(run=listener)
            with patch("local_web_server.ingress._https_status", side_effect=probe):
                self.installer(
                    ingress_verifier=verifier, monotonic=clock.monotonic, sleep=clock.sleep,
                ).install()
        self.assertGreater(timeouts[0], 4)
        self.assertLessEqual(timeouts[0], 5)
        self.assertLessEqual(timeouts[1], 2)
        self.assertLessEqual(timeouts[2], 0.45)

    def test_tailscale_replacement_recovery_rejects_registered_job_without_running_pid(self):
        self.installer().install()
        self.write_prior_prepared_bundle()
        prior = self.runtime_snapshot()
        self.use_tailscale_registry()
        self.runner.caddy_loaded = True
        clock = FakeClock()
        bootstraps = 0
        recovery_started = []

        def leave_restored_job_waiting(argv):
            nonlocal bootstraps
            bootstraps += 1
            if bootstraps == 2:
                recovery_started.append(clock.now)
                self.runner.launchctl_stdout = "state = waiting\n"
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.runner.bootstrap = leave_restored_job_waiting
        verifier = RecordingIngressVerifier()
        verifier.loaded_error = IngressVerificationError("private verification failure")
        with self.assertRaisesRegex(InstallError, "installation failed; platform recovery failed"):
            self.installer(
                ingress_verifier=verifier, monotonic=clock.monotonic, sleep=clock.sleep,
            ).install()
        self.assertEqual(self.runtime_snapshot(), prior)
        self.assertEqual(bootstraps, 2)
        self.assertTrue(self.runner.caddy_loaded)
        self.assertGreater(clock.now, 0)
        self.assertLessEqual(clock.now - recovery_started[0], 2)

    def test_tailscale_recovery_rejects_changed_managed_process_identity_after_reload(self):
        self.installer().install()
        self.use_tailscale_registry(prepared_fixture=True)
        self.runner.caddy_loaded = True
        self.installer(ingress_verifier=RecordingIngressVerifier()).install()
        self.runner.calls.clear()
        self.manifests["static-app"] = replace(self.manifests["static-app"], route="/updated-static")
        verifier = RecordingIngressVerifier()
        verifier.loaded_error = IngressVerificationError("private verification failure")
        reloads = 0

        def replace_during_recovery(argv):
            nonlocal reloads
            reloads += 1
            if reloads == 2:
                self.runner.caddy_started = "Sun Aug 23 12:00:00 2026"
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.runner.reload = replace_during_recovery
        with self.assertRaisesRegex(InstallError, "installation failed; platform recovery failed"):
            self.installer(ingress_verifier=verifier).install()

    def test_tailscale_post_load_process_inspection_failure_restores_before_platform_writes(self):
        self.installer().install()
        self.use_tailscale_registry(prepared_fixture=True)
        self.runner.caddy_loaded = True
        self.installer(ingress_verifier=RecordingIngressVerifier()).install()
        self.runner.calls.clear()
        prior = self.runtime_snapshot()
        self.manifests["static-app"] = replace(self.manifests["static-app"], route="/updated-static")
        verifier = RecordingIngressVerifier()
        reloads = 0

        def lose_process_identity_then_restore(argv):
            nonlocal reloads
            reloads += 1
            self.runner.launchctl_stdout = "state = running\n" if reloads == 1 else None
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.runner.reload = lose_process_identity_then_restore
        with self.assertRaisesRegex(InstallError, "cannot identify loaded Caddy process"):
            self.installer(ingress_verifier=verifier).install()
        self.assertEqual(self.runtime_snapshot(), prior)
        self.assertEqual(reloads, 2)
        self.assertEqual(verifier.calls, [("preflight", self.registry)])

    def test_changed_theme_passes_gate_before_any_runtime_or_caddy_mutation(self):
        observations = []

        class ObservingGate:
            def verify(inner_self, candidate):
                observations.append(
                    (
                        Path(candidate).read_bytes(),
                        self.runtime.exists(),
                        tuple(self.runner.calls),
                    )
                )

        self.installer(theme_gate=ObservingGate()).install()

        self.assertEqual(
            observations,
            [(
                (self.hosting_repository / "platform_assets" / "theme.css").read_bytes(),
                False,
                (["pmset", "-g", "custom"],),
            )],
        )

    def test_dry_run_validates_but_does_not_run_browser_gate(self):
        self.installer().install(dry_run=True)

        self.assertEqual(self.theme_gate.calls, [])

    def test_byte_identical_reinstall_does_not_run_browser_gate_again(self):
        installer = self.installer()
        installer.install()
        self.assertEqual(
            [content for _path, content in self.theme_gate.calls],
            [(self.hosting_repository / "platform_assets" / "theme.css").read_bytes()],
        )
        self.theme_gate.calls.clear()

        installer.install()

        self.assertEqual(self.theme_gate.calls, [])

    def test_gate_failure_leaves_all_runtime_files_and_loaded_state_exact(self):
        installer = self.installer()
        first = installer.install()
        before = {
            path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            for path in first.writes
        }
        theme_path = self.hosting_repository / "platform_assets" / "theme.css"
        theme_path.write_bytes(theme_path.read_bytes().replace(b".14", b".15"))
        self.theme_gate.error = RuntimeError("private gate output")
        self.theme_gate.calls.clear()
        self.runner.caddy_loaded = True
        self.runner.calls.clear()

        with self.assertRaisesRegex(InstallError, "theme compatibility gate failed") as caught:
            installer.install()

        self.assertEqual(str(caught.exception), "theme compatibility gate failed")
        self.assertNotIn("private gate output", str(caught.exception))
        self.assertEqual(
            {
                path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in first.writes
            },
            before,
        )
        self.assertTrue(self.runner.caddy_loaded)
        self.assertEqual(self.runner.calls, [["pmset", "-g", "custom"]])

    def installer_with_remote_probe(self):
        probe = BackendProbeSpec(
            (
                "VITE_SUPABASE_URL",
                "VITE_SUPABASE_PUBLISHABLE_KEY",
            ),
            "VITE_SUPABASE_URL",
            "/auth/v1/health",
            (("apikey", "VITE_SUPABASE_PUBLISHABLE_KEY"),),
        )
        self.environment_file.write_text(
            "VITE_SUPABASE_URL=https://probe-value.example.test\n"
            "VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_probe_value\n"
            "UNRELATED_PRIVATE_INPUT=private-probe-value\n",
            encoding="utf-8",
        )
        static_manifest = self.manifests["static-app"]
        self.manifests["static-app"] = AppManifest(
            static_manifest.schema_version,
            static_manifest.id,
            static_manifest.title,
            static_manifest.route,
            static_manifest.kind,
            BuildSpec(
                static_manifest.build.commands,
                static_manifest.build.output,
                ("VITE_SUPABASE_URL", "VITE_SUPABASE_PUBLISHABLE_KEY"),
            ),
            static_manifest.health_path,
            static_manifest.service,
        )
        static_host, service_host = self.registry.apps
        self.registry = HostRegistry(
            self.registry.host,
            self.registry.runtime_root,
            (
                HostApp(
                    static_host.id,
                    static_host.repository,
                    static_host.auto_deploy,
                    static_host.environment_file,
                    static_host.environment,
                    static_host.port,
                    static_host.start_command,
                    probe,
                ),
                service_host,
            ),
        )
        return self.installer()

    def write_prior_caddy_bundle(self, caddy_bytes=b"prior config\n"):
        self.runtime.mkdir(parents=True, exist_ok=True)
        caddyfile = self.runtime / "Caddyfile"
        caddyfile.write_bytes(caddy_bytes)
        caddy_plist = (
            self.home / "Library" / "LaunchAgents" / "com.sean.local-web.caddy.plist"
        )
        caddy_plist.parent.mkdir(parents=True, exist_ok=True)
        caddy_plist.write_bytes(
            render_caddy_plist(self.hosting_repository, self.runtime)
        )
        return caddyfile, caddy_plist

    def write_prior_replacement_bundle(self, caddy_bytes=b"prior config\n"):
        caddyfile, caddy_plist = self.write_prior_caddy_bundle(caddy_bytes)
        caddy_plist.write_bytes(b"exact prior managed plist bytes\n")
        return caddyfile, caddy_plist

    def test_creates_private_runtime_home_log_directories_and_service_only_plists(self):
        result = self.installer().install()

        directories = (
            self.runtime,
            self.runtime / "apps",
            self.runtime / "platform" / "index",
            self.runtime / "platform" / "index" / "assets",
            self.runtime / "platform" / "ui-gallery",
            self.runtime / "platform" / "ui-gallery" / "assets",
            self.runtime / "logs" / "caddy",
            self.runtime / "logs" / "deploy",
            self.runtime / "logs" / "services",
            self.runtime / "apps" / "static-app" / "releases",
            self.runtime / "apps" / "service-app" / "releases",
            self.home / "Library" / "LaunchAgents",
        )
        for directory in directories:
            with self.subTest(directory=directory):
                self.assertTrue(directory.is_dir())
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

        caddyfile = self.runtime / "Caddyfile"
        home_page = self.runtime / "home.html"
        index_registry = self.runtime / "platform" / "index" / INDEX_REGISTRY_NAME
        caddy_plist = self.home / "Library" / "LaunchAgents" / "com.sean.local-web.caddy.plist"
        service_plist = self.home / "Library" / "LaunchAgents" / "com.sean.local-web.service-app.plist"
        self.assertEqual(
            caddyfile.read_text(encoding="utf-8"),
            render_caddyfile(self.registry, self.manifests),
        )
        self.assertEqual(home_page.read_bytes(), self.index_home)
        self.assertEqual(index_registry.read_bytes(), render_index_registry(self.registry, self.manifests))
        for private_value in (str(self.app_data), str(self.repositories), "DATABASE_PATH"):
            self.assertNotIn(private_value, home_page.read_text(encoding="utf-8"))
            self.assertNotIn(private_value.encode(), index_registry.read_bytes())
        self.assertTrue(caddy_plist.is_file())
        self.assertTrue(service_plist.is_file())
        self.assertFalse(
            (self.home / "Library" / "LaunchAgents" / "com.sean.local-web.static-app.plist").exists()
        )
        index_asset_paths = tuple(
            self.runtime / "platform" / "index" / "assets" / name
            for name in ("index-old.css", "index-old.js")
        )
        gallery_asset = self.runtime / "platform" / "ui-gallery" / "assets" / "gallery.js"
        for path in (
            caddyfile,
            home_page,
            index_registry,
            caddy_plist,
            service_plist,
            *index_asset_paths,
            gallery_asset,
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertIn(path, result.writes)
        theme = self.runtime / "platform" / "theme.css"
        self.assertEqual(
            theme.read_bytes(),
            (self.hosting_repository / "platform_assets" / "theme.css").read_bytes(),
        )
        self.assertEqual(stat.S_IMODE(theme.stat().st_mode), 0o600)
        self.assertIn(theme, result.writes)

    def test_theme_is_validated_against_every_registered_accent_before_activation(self):
        from local_web_server.theme import validate_theme

        self.manifests["static-app"] = AppManifest(
            1,
            "static-app",
            "Static App",
            "/static",
            "static",
            self.manifests["static-app"].build,
            "/static/",
            None,
            HomePresentation("apps", "#D98AA8"),
        )
        self.manifests["service-app"] = AppManifest(
            1,
            "service-app",
            "Service App",
            "/service",
            "service",
            self.manifests["service-app"].build,
            "/service/",
            self.manifests["service-app"].service,
            HomePresentation("apps", "#76D39B"),
        )

        with patch("local_web_server.theme.validate_theme", wraps=validate_theme) as validator:
            self.installer().install()

        self.assertEqual(validator.call_args_list[0].args[1], ("#D98AA8", "#76D39B"))

    def test_invalid_repository_theme_stops_before_commands_or_runtime_writes(self):
        (self.hosting_repository / "platform_assets" / "theme.css").write_bytes(
            b"private invalid theme payload"
        )

        with self.assertRaisesRegex(InstallError, "cannot install generated local web files") as caught:
            self.installer().install()

        self.assertFalse(self.runtime.exists())
        self.assertEqual(self.runner.calls, [])
        self.assertNotIn("private invalid theme payload", str(caught.exception))

    def test_theme_activation_failure_restores_exact_prior_caddy_bundle_and_loaded_job(self):
        prior_caddy = b"exact prior caddy bytes\n"
        caddyfile, caddy_plist = self.write_prior_caddy_bundle(prior_caddy)
        prior_plist = caddy_plist.read_bytes()
        ThemeStore(self.runtime).activate(
            (self.hosting_repository / "platform_assets" / "theme.css").read_bytes()
        )
        prior_theme = (self.runtime / "platform" / "theme.css").read_bytes()
        runner = RecordingRunner(caddy_loaded=True)

        with patch.object(
            ThemeStore, "activate", side_effect=ThemeError("theme transition failed")
        ):
            with self.assertRaisesRegex(InstallError, "cannot install generated local web files"):
                self.installer(runner).install()

        self.assertEqual(caddyfile.read_bytes(), prior_caddy)
        self.assertEqual(caddy_plist.read_bytes(), prior_plist)
        self.assertEqual((self.runtime / "platform" / "theme.css").read_bytes(), prior_theme)
        self.assertTrue(runner.caddy_loaded)

    def test_theme_failure_stops_candidate_job_loaded_during_caddy_validation(self):
        prior_caddy = b"exact prior race caddy bytes\n"
        caddyfile, caddy_plist = self.write_prior_caddy_bundle(prior_caddy)
        prior_plist = caddy_plist.read_bytes()
        store = ThemeStore(self.runtime)
        store.activate(
            (self.hosting_repository / "platform_assets" / "theme.css").read_bytes()
        )
        prior_theme = (
            store.current.read_bytes(),
            store.previous.read_bytes() if store.previous.exists() else None,
        )
        runner = RecordingRunner(caddy_states=["missing", "loaded"])
        bootstrapped_caddy = []

        def observe_candidate_bootstrap(argv):
            bootstrapped_caddy.append(caddyfile.read_bytes())
            return subprocess.CompletedProcess(argv, 0, "", "")

        runner.bootstrap = observe_candidate_bootstrap
        with patch.object(
            ThemeStore, "activate", side_effect=ThemeError("theme transition failed")
        ):
            with self.assertRaisesRegex(InstallError, "cannot install generated local web files"):
                self.installer(runner).install()

        self.assertEqual(caddyfile.read_bytes(), prior_caddy)
        self.assertEqual(caddy_plist.read_bytes(), prior_plist)
        self.assertEqual(store.current.read_bytes(), prior_theme[0])
        self.assertEqual(
            store.previous.read_bytes() if store.previous.exists() else None,
            prior_theme[1],
        )
        self.assertEqual(
            bootstrapped_caddy,
            [render_caddyfile(self.registry, self.manifests).encode()],
        )
        self.assertEqual(
            sum(call[:2] == ["launchctl", "bootout"] for call in runner.calls), 2
        )
        self.assertEqual(
            sum(call[:2] == ["launchctl", "bootstrap"] for call in runner.calls), 1
        )
        self.assertFalse(runner.caddy_loaded)

    def test_first_install_theme_failure_removes_new_caddy_publication(self):
        with patch.object(
            ThemeStore, "activate", side_effect=ThemeError("theme transition failed")
        ):
            with self.assertRaisesRegex(InstallError, "cannot install generated local web files"):
                self.installer().install()

        self.assertFalse((self.runtime / "Caddyfile").exists())
        self.assertFalse(
            (self.home / "Library" / "LaunchAgents" / "com.sean.local-web.caddy.plist").exists()
        )
        self.assertFalse((self.runtime / "platform" / "theme.css").exists())

    def test_later_file_failure_restores_exact_theme_caddy_pair_and_loaded_state(self):
        import local_web_server.install as install_module

        installer = self.installer()
        installer.install()
        caddyfile = self.runtime / "Caddyfile"
        caddy_plist = (
            self.home / "Library" / "LaunchAgents" / "com.sean.local-web.caddy.plist"
        )
        store = ThemeStore(self.runtime)
        prior = (
            caddyfile.read_bytes(),
            caddy_plist.read_bytes(),
            store.current.read_bytes(),
            store.previous.read_bytes() if store.previous.exists() else None,
        )
        static = self.manifests["static-app"]
        self.manifests["static-app"] = AppManifest(
            static.schema_version,
            static.id,
            static.title,
            "/static-new",
            static.kind,
            static.build,
            "/static-new/",
            static.service,
            static.home,
            static.platform,
        )
        theme_path = self.hosting_repository / "platform_assets" / "theme.css"
        theme_path.write_bytes(theme_path.read_bytes().replace(b".14", b".15"))
        self.runner.caddy_loaded = True
        real_atomic_write = install_module._atomic_write

        def fail_home(path, content, mode):
            if path == self.runtime / "home.html":
                raise InstallError("simulated later platform failure")
            return real_atomic_write(path, content, mode)

        with patch("local_web_server.install._atomic_write", side_effect=fail_home):
            with self.assertRaisesRegex(InstallError, "later platform failure"):
                self.installer().install()

        self.assertEqual(caddyfile.read_bytes(), prior[0])
        self.assertEqual(caddy_plist.read_bytes(), prior[1])
        self.assertEqual(store.current.read_bytes(), prior[2])
        self.assertEqual(
            store.previous.read_bytes() if store.previous.exists() else None, prior[3]
        )
        self.assertTrue(self.runner.caddy_loaded)

    def test_validates_temporary_candidate_before_replacing_existing_caddyfile(self):
        self.runtime.mkdir(parents=True)
        installed = self.runtime / "Caddyfile"
        installed.write_bytes(b"exact prior bytes\n")
        observations = []

        def validate(argv):
            candidate = Path(argv[argv.index("--config") + 1])
            observations.append(
                (candidate, installed.read_bytes(), candidate.read_text(encoding="utf-8"))
            )
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.runner.validate = validate

        self.installer().install()

        self.assertEqual(len(observations), 1)
        candidate, installed_during_validation, candidate_text = observations[0]
        self.assertEqual(candidate.parent, installed.parent)
        self.assertNotEqual(candidate, installed)
        self.assertEqual(installed_during_validation, b"exact prior bytes\n")
        self.assertEqual(candidate_text, render_caddyfile(self.registry, self.manifests))
        self.assertFalse(candidate.exists())

    def test_candidate_validation_explicitly_uses_caddyfile_adapter(self):
        def validate(argv):
            accepted = argv[-2:] == ["--adapter", "caddyfile"]
            return subprocess.CompletedProcess(argv, 0 if accepted else 1, "", "")

        self.runner.validate = validate

        self.installer().install()

        self.assertTrue((self.runtime / "Caddyfile").is_file())

    def test_failed_validation_preserves_existing_caddyfile(self):
        self.runtime.mkdir(parents=True)
        installed = self.runtime / "Caddyfile"
        installed.write_bytes(b"exact prior bytes\x00")
        self.runner.validate = lambda argv: subprocess.CompletedProcess(argv, 1, "", "invalid")

        with self.assertRaisesRegex(InstallError, "validation"):
            self.installer().install()

        self.assertEqual(installed.read_bytes(), b"exact prior bytes\x00")

    def test_caddyfile_only_change_with_long_lived_sse_reloads_without_replacement(self):
        installed, _caddy_plist = self.write_prior_caddy_bundle(b":80 {\n    respond 200\n}\n# old config\n")
        runner = RecordingRunner(caddy_loaded=True)
        reloaded_bytes = []

        def reload_caddy(argv):
            reloaded_bytes.append(installed.read_bytes())
            return subprocess.CompletedProcess(argv, 0, "", "")

        runner.reload = reload_caddy

        self.installer(runner).install()

        self.assertEqual(
            reloaded_bytes,
            [render_caddyfile(self.registry, self.manifests).encode()],
        )
        self.assertFalse(any(call[:2] == ["launchctl", "bootout"] for call in runner.calls))
        self.assertFalse(any(call[:2] == ["launchctl", "bootstrap"] for call in runner.calls))

    def test_failed_caddyfile_reload_restores_exact_prior_bytes_and_live_config(self):
        prior = b":80 {\n    respond 200\n}\n# exact prior reload bytes without a newline"
        installed, caddy_plist = self.write_prior_caddy_bundle(prior)
        prior_plist = caddy_plist.read_bytes()
        runner = RecordingRunner(caddy_loaded=True)
        private_marker = "private-reload-diagnostic-marker"
        reloaded = []

        def fail_candidate_then_restore(argv):
            reloaded.append(installed.read_bytes())
            return subprocess.CompletedProcess(
                argv,
                1 if len(reloaded) == 1 else 0,
                "",
                private_marker if len(reloaded) == 1 else "",
            )

        runner.reload = fail_candidate_then_restore

        with self.assertRaisesRegex(
            InstallError,
            "configuration activation failed.*restored prior configuration",
        ) as caught:
            self.installer(runner).install()

        self.assertEqual(
            reloaded,
            [render_caddyfile(self.registry, self.manifests).encode(), prior],
        )
        self.assertEqual(installed.read_bytes(), prior)
        self.assertEqual(caddy_plist.read_bytes(), prior_plist)
        self.assertTrue(runner.caddy_loaded)
        self.assertFalse(
            any(call[:2] == ["launchctl", "bootout"] for call in runner.calls)
        )
        disclosed = "\n".join(
            [str(caught.exception), *(argument for call in runner.calls for argument in call)]
        )
        self.assertNotIn(private_marker, disclosed)

    def test_reload_spawn_error_restores_prior_bytes_without_disclosure(self):
        prior = b":80 {\n    respond 200\n}\n# exact prior reload spawn-error bytes"
        installed, caddy_plist = self.write_prior_caddy_bundle(prior)
        prior_plist = caddy_plist.read_bytes()
        runner = RecordingRunner(caddy_loaded=True)
        private_marker = "private-reload-spawn-error-marker"
        reloads = 0

        def fail_candidate_spawn_then_restore(argv):
            nonlocal reloads
            reloads += 1
            if reloads == 1:
                raise OSError(private_marker)
            return subprocess.CompletedProcess(argv, 0, "", "")

        runner.reload = fail_candidate_spawn_then_restore

        with self.assertRaisesRegex(
            InstallError, "configuration activation failed.*restored prior configuration"
        ) as caught:
            self.installer(runner).install()

        self.assertEqual(installed.read_bytes(), prior)
        self.assertEqual(caddy_plist.read_bytes(), prior_plist)
        self.assertEqual(reloads, 2)
        self.assertNotIn(private_marker, str(caught.exception))

    def test_plist_change_waits_for_old_managed_process_exit_before_bootstrap(self):
        self.write_prior_caddy_bundle(render_caddyfile(self.registry, self.manifests).encode())
        runner = RecordingRunner(caddy_loaded=True)
        runner.process_states = ["alive", "alive", "alive", "missing", "alive"]
        clock = FakeClock()
        bootstrap_process_states = []

        def observe_bootstrap(argv):
            bootstrap_process_states.append(tuple(runner.process_states))
            return subprocess.CompletedProcess(argv, 0, "", "")

        runner.bootstrap = observe_bootstrap
        installer = self.installer_with_remote_probe()
        installer.run = runner
        installer.monotonic = clock.monotonic
        installer.sleep = clock.sleep

        result = installer.install()

        self.assertFalse(result.dry_run)
        self.assertEqual(bootstrap_process_states, [("alive",)])
        self.assertEqual(clock.sleeps, [0.05, 0.05])
        self.assertTrue(any(call[:2] == ["/bin/ps", "-p"] for call in runner.calls))

    def test_shutdown_timeout_uses_separate_recovery_phase_and_never_kills_by_port(self):
        prior_caddy = b"exact prior bounded recovery caddy bytes"
        caddyfile, caddy_plist = self.write_prior_replacement_bundle(prior_caddy)
        prior_plist = caddy_plist.read_bytes()
        runner = RecordingRunner(caddy_loaded=True)
        clock = FakeClock()
        bootstrapped = []

        def process_state(argv):
            old_bundle_restored = caddyfile.read_bytes() == prior_caddy
            alive = bool(bootstrapped) or not old_bundle_restored
            return subprocess.CompletedProcess(
                argv,
                0 if alive else 1,
                f"{runner.caddy_started}\n" if alive else "",
                "",
            )

        def observe_recovery_bootstrap(argv):
            bootstrapped.append((caddyfile.read_bytes(), caddy_plist.read_bytes()))
            return subprocess.CompletedProcess(argv, 0, "", "")

        runner.process = process_state
        runner.bootstrap = observe_recovery_bootstrap

        with self.assertRaisesRegex(
            InstallError,
            "LaunchAgent activation failed.*restored prior configuration",
        ):
            self.installer(
                runner,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            ).install()

        self.assertAlmostEqual(clock.now, 10.0)
        self.assertEqual(bootstrapped, [(prior_caddy, prior_plist)])
        self.assertEqual(caddyfile.read_bytes(), prior_caddy)
        self.assertEqual(caddy_plist.read_bytes(), prior_plist)
        self.assertTrue(runner.caddy_loaded)
        flattened = [argument for call in runner.calls for argument in call]
        self.assertNotIn("kill", flattened)
        self.assertNotIn("lsof", flattened)
        self.assertNotIn("80", flattened)

    def test_reused_pid_is_not_treated_as_the_captured_managed_process(self):
        self.write_prior_replacement_bundle(
            render_caddyfile(self.registry, self.manifests).encode()
        )
        runner = RecordingRunner(caddy_loaded=True)
        inspections = 0

        def process_state(argv):
            nonlocal inspections
            inspections += 1
            started = runner.caddy_started if inspections == 1 else "Fri Aug 21 12:00:00 2026"
            return subprocess.CompletedProcess(argv, 0, f"{started}\n", "")

        runner.process = process_state
        installer = self.installer_with_remote_probe()
        installer.run = runner

        result = installer.install()

        self.assertFalse(result.dry_run)
        self.assertEqual(
            sum(call[:2] == ["launchctl", "bootstrap"] for call in runner.calls),
            1,
        )
        flattened = [argument for call in runner.calls for argument in call]
        self.assertNotIn("kill", flattened)

    def test_ambiguous_managed_process_identity_fails_closed_before_bootout(self):
        cases = ("missing-pid", "ps-error")
        for case in cases:
            with self.subTest(case=case):
                caddyfile, caddy_plist = self.write_prior_replacement_bundle(
                    b"exact prior identity config"
                )
                prior = (caddyfile.read_bytes(), caddy_plist.read_bytes())
                runner = RecordingRunner(caddy_loaded=True)
                if case == "missing-pid":
                    runner.launchctl_stdout = "state = running\n"
                else:
                    runner.process = lambda argv: subprocess.CompletedProcess(
                        argv, 1, "", "private inspection failure"
                    )

                with self.assertRaisesRegex(
                    InstallError,
                    "restored prior configuration and running state",
                ):
                    self.installer(runner).install()

                self.assertEqual(
                    (caddyfile.read_bytes(), caddy_plist.read_bytes()), prior
                )
                self.assertFalse(
                    any(
                        call[:2] == ["launchctl", "bootout"]
                        for call in runner.calls
                    )
                )

    def test_transient_pre_bootout_process_error_does_not_restart_healthy_prior_job(self):
        prior_caddy = b"exact prior transient identity config"
        caddyfile, caddy_plist = self.write_prior_replacement_bundle(prior_caddy)
        prior = (caddyfile.read_bytes(), caddy_plist.read_bytes())
        runner = RecordingRunner(caddy_loaded=True)
        inspections = 0

        def process_state(argv):
            nonlocal inspections
            inspections += 1
            if inspections == 1:
                return subprocess.CompletedProcess(
                    argv, 1, "", "private transient inspection failure"
                )
            return subprocess.CompletedProcess(
                argv, 0, f"{runner.caddy_started}\n", ""
            )

        runner.process = process_state

        with self.assertRaisesRegex(
            InstallError, "restored prior configuration and running state"
        ):
            self.installer(runner).install()

        self.assertEqual((caddyfile.read_bytes(), caddy_plist.read_bytes()), prior)
        self.assertTrue(runner.caddy_loaded)
        self.assertFalse(
            any(call[:2] == ["launchctl", "bootout"] for call in runner.calls)
        )
        self.assertFalse(
            any(call[:2] == ["launchctl", "bootstrap"] for call in runner.calls)
        )

    def test_post_bootout_process_ambiguity_carries_identity_into_failed_recovery(self):
        prior_caddy = b"exact prior post-bootout identity config"
        caddyfile, caddy_plist = self.write_prior_replacement_bundle(prior_caddy)
        prior = (caddyfile.read_bytes(), caddy_plist.read_bytes())
        runner = RecordingRunner(caddy_loaded=True)
        private_marker = "private-post-bootout-ps-diagnostic"
        inspections = 0

        def process_state(argv):
            nonlocal inspections
            inspections += 1
            if inspections == 1:
                return subprocess.CompletedProcess(
                    argv, 0, f"{runner.caddy_started}\n", ""
                )
            return subprocess.CompletedProcess(argv, 1, "", private_marker)

        runner.process = process_state

        with self.assertRaisesRegex(
            InstallError, "restored prior files but recovery failed"
        ) as caught:
            self.installer(runner).install()

        self.assertEqual((caddyfile.read_bytes(), caddy_plist.read_bytes()), prior)
        self.assertEqual(
            sum(call[:2] == ["launchctl", "bootout"] for call in runner.calls),
            1,
        )
        self.assertFalse(
            any(call[:2] == ["launchctl", "bootstrap"] for call in runner.calls)
        )
        disclosed = "\n".join(
            [str(caught.exception), *(argument for call in runner.calls for argument in call)]
        )
        self.assertNotIn(private_marker, disclosed)

    def test_nonzero_bootout_that_removed_job_waits_for_identity_and_restores_prior(self):
        prior_caddy = b"exact prior nonzero bootout config"
        caddyfile, caddy_plist = self.write_prior_replacement_bundle(prior_caddy)
        prior = (caddyfile.read_bytes(), caddy_plist.read_bytes())
        runner = RecordingRunner(caddy_loaded=True)
        runner.process_states = ["alive", "missing", "alive"]
        bootstrapped = []

        def remove_job_but_report_failure(argv):
            runner.caddy_loaded = False
            return subprocess.CompletedProcess(argv, 1, "", "private bootout failure")

        def observe_recovery_bootstrap(argv):
            bootstrapped.append((caddyfile.read_bytes(), caddy_plist.read_bytes()))
            return subprocess.CompletedProcess(argv, 0, "", "")

        runner.bootout = remove_job_but_report_failure
        runner.bootstrap = observe_recovery_bootstrap

        with self.assertRaisesRegex(
            InstallError, "restored prior configuration and running state"
        ):
            self.installer(runner).install()

        self.assertEqual((caddyfile.read_bytes(), caddy_plist.read_bytes()), prior)
        self.assertEqual(bootstrapped, [prior])
        self.assertTrue(runner.caddy_loaded)

    def test_nonzero_bootout_leaving_same_job_loaded_does_not_restart_it(self):
        prior_caddy = b"exact prior unchanged bootout config"
        caddyfile, caddy_plist = self.write_prior_replacement_bundle(prior_caddy)
        prior = (caddyfile.read_bytes(), caddy_plist.read_bytes())
        runner = RecordingRunner(caddy_loaded=True)
        runner.bootout = lambda argv: subprocess.CompletedProcess(
            argv, 1, "", "private bootout failure"
        )

        with self.assertRaisesRegex(
            InstallError, "restored prior configuration and running state"
        ):
            self.installer(runner).install()

        self.assertEqual((caddyfile.read_bytes(), caddy_plist.read_bytes()), prior)
        self.assertTrue(runner.caddy_loaded)
        self.assertEqual(
            sum(call[:2] == ["launchctl", "bootout"] for call in runner.calls),
            1,
        )
        self.assertFalse(
            any(call[:2] == ["launchctl", "bootstrap"] for call in runner.calls)
        )

    def test_bootout_command_consumes_the_shutdown_deadline_before_recovery(self):
        prior_caddy = b"exact prior slow bootout config"
        caddyfile, caddy_plist = self.write_prior_replacement_bundle(prior_caddy)
        prior = (caddyfile.read_bytes(), caddy_plist.read_bytes())
        runner = RecordingRunner(caddy_loaded=True)
        clock = FakeClock()
        bootout_timeouts = []

        def time_out_bootout(argv, timeout):
            if argv[:2] != ["launchctl", "bootout"]:
                return
            bootout_timeouts.append(timeout)
            if timeout is None:
                return
            runner.caddy_loaded = False
            clock.sleep(timeout)
            raise subprocess.TimeoutExpired(argv, timeout)

        runner.before_call = time_out_bootout

        with self.assertRaisesRegex(
            InstallError, "restored prior configuration and running state"
        ):
            self.installer(
                runner,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            ).install()

        self.assertEqual((caddyfile.read_bytes(), caddy_plist.read_bytes()), prior)
        self.assertEqual(bootout_timeouts, [10.0])
        self.assertAlmostEqual(clock.now, 10.0)
        self.assertTrue(runner.caddy_loaded)

    def test_bootstrap_command_consumes_only_its_own_deadline_before_recovery(self):
        prior_caddy = b"exact prior slow bootstrap config"
        caddyfile, caddy_plist = self.write_prior_replacement_bundle(prior_caddy)
        prior = (caddyfile.read_bytes(), caddy_plist.read_bytes())
        runner = RecordingRunner(caddy_loaded=True)
        clock = FakeClock()
        bootstrap_timeouts = []

        def time_out_candidate_bootstrap(argv, timeout):
            if argv[:2] != ["launchctl", "bootstrap"]:
                return
            bootstrap_timeouts.append(timeout)
            if len(bootstrap_timeouts) == 1 and timeout is not None:
                clock.sleep(timeout)
                raise subprocess.TimeoutExpired(argv, timeout)

        runner.before_call = time_out_candidate_bootstrap

        with self.assertRaisesRegex(
            InstallError, "restored prior configuration and running state"
        ):
            self.installer(
                runner,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
            ).install()

        self.assertEqual((caddyfile.read_bytes(), caddy_plist.read_bytes()), prior)
        self.assertEqual(bootstrap_timeouts, [2.0, 2.0])
        self.assertAlmostEqual(clock.now, 2.0)
        self.assertTrue(runner.caddy_loaded)

    def test_opted_in_retry_bootstraps_complete_missing_managed_bundle(self):
        caddyfile, caddy_plist = self.write_prior_caddy_bundle(
            render_caddyfile(self.registry, self.manifests).encode()
        )
        caddyfile.chmod(0o600)
        caddy_plist.chmod(0o600)
        prior = (caddyfile.read_bytes(), caddy_plist.read_bytes())
        runner = RecordingRunner(caddy_loaded=False)

        result = self.installer(runner).install(recover_missing_caddy=True)

        self.assertFalse(result.dry_run)
        self.assertTrue(runner.caddy_loaded)
        self.assertEqual(
            sum(call[:2] == ["launchctl", "bootstrap"] for call in runner.calls),
            1,
        )
        self.assertEqual((caddyfile.read_bytes(), caddy_plist.read_bytes()), prior)
        self.assertFalse(
            any(call[:2] == ["/opt/homebrew/bin/caddy", "reload"] for call in runner.calls)
        )

    def test_opted_in_retry_rejects_partial_missing_managed_bundle(self):
        self.runtime.mkdir(parents=True)
        (self.runtime / "Caddyfile").write_bytes(b"partial managed state")
        runner = RecordingRunner(caddy_loaded=False)

        with self.assertRaisesRegex(InstallError, "complete managed Caddyfile"):
            self.installer(runner).install(recover_missing_caddy=True)

        self.assertFalse(
            any(call[:2] == ["launchctl", "bootstrap"] for call in runner.calls)
        )

    def test_standalone_rerun_with_complete_missing_bundle_does_not_autostart(self):
        self.write_prior_caddy_bundle(
            render_caddyfile(self.registry, self.manifests).encode()
        )
        runner = RecordingRunner(caddy_loaded=False)

        result = self.installer(runner).install()

        self.assertFalse(result.dry_run)
        self.assertFalse(runner.caddy_loaded)
        self.assertFalse(
            any(call[:2] == ["launchctl", "bootstrap"] for call in runner.calls)
        )

    def test_loaded_job_activation_waits_for_bootout_removal_before_bootstrap(self):
        installed, _caddy_plist = self.write_prior_replacement_bundle(b"old config\n")
        runner = RecordingRunner(
            caddy_states=[
                "loaded",
                "loaded",
                "loaded",
                "loaded",
                "missing",
                "loaded",
            ]
        )
        clock = FakeClock()

        result = self.installer(
            runner, monotonic=clock.monotonic, sleep=clock.sleep
        ).install()

        self.assertFalse(result.dry_run)
        self.assertEqual(
            installed.read_bytes(), render_caddyfile(self.registry, self.manifests).encode()
        )
        self.assertEqual(clock.sleeps, [0.05])
        self.assertEqual(runner.caddy_states, [])
        self.assertTrue(runner.caddy_loaded)

    def test_loaded_job_activation_waits_for_delayed_launchctl_visibility(self):
        installed, _caddy_plist = self.write_prior_replacement_bundle(b"old config\n")
        runner = RecordingRunner(
            caddy_states=[
                "loaded",
                "loaded",
                "loaded",
                "missing",
                "missing",
                "missing",
                "loaded",
            ]
        )
        clock = FakeClock()
        result = self.installer(
            runner, monotonic=clock.monotonic, sleep=clock.sleep
        ).install()

        self.assertFalse(result.dry_run)
        self.assertEqual(
            installed.read_bytes(), render_caddyfile(self.registry, self.manifests).encode()
        )
        self.assertEqual(clock.sleeps, [0.05, 0.05])
        self.assertEqual(
            sum(call[:2] == ["launchctl", "bootstrap"] for call in runner.calls), 1
        )
        self.assertTrue(runner.caddy_loaded)

    def test_loaded_job_activation_retries_transient_bootstrap_exit_5_while_missing(self):
        installed, _caddy_plist = self.write_prior_replacement_bundle(b"old config\n")
        runner = RecordingRunner(caddy_loaded=True)
        attempts = 0

        def fail_once_during_teardown(argv):
            nonlocal attempts
            attempts += 1
            return subprocess.CompletedProcess(
                argv, 5 if attempts == 1 else 0, "", "transient diagnostic"
            )

        runner.bootstrap = fail_once_during_teardown
        clock = FakeClock()

        result = self.installer(
            runner, monotonic=clock.monotonic, sleep=clock.sleep
        ).install()

        self.assertFalse(result.dry_run)
        self.assertEqual(
            installed.read_bytes(), render_caddyfile(self.registry, self.manifests).encode()
        )
        self.assertEqual(attempts, 2)
        self.assertEqual(clock.sleeps, [0.05])
        self.assertTrue(runner.caddy_loaded)

    def test_loaded_job_activation_retries_bootstrap_operation_in_progress_while_missing(self):
        installed, _caddy_plist = self.write_prior_replacement_bundle(b"old config\n")
        runner = RecordingRunner(caddy_loaded=True)
        attempts = 0

        def operation_in_progress_once(argv):
            nonlocal attempts
            attempts += 1
            return subprocess.CompletedProcess(
                argv, 37 if attempts == 1 else 0, "", "Operation already in progress"
            )

        runner.bootstrap = operation_in_progress_once
        clock = FakeClock()

        result = self.installer(
            runner, monotonic=clock.monotonic, sleep=clock.sleep
        ).install()

        self.assertFalse(result.dry_run)
        self.assertEqual(
            installed.read_bytes(), render_caddyfile(self.registry, self.manifests).encode()
        )
        self.assertEqual(attempts, 2)
        self.assertEqual(clock.sleeps, [0.05])
        self.assertTrue(runner.caddy_loaded)

    def test_persistent_bootstrap_exit_5_times_out_and_restores_exact_prior_bundle(self):
        prior_caddy = b"prior config with persistent bootstrap failure"
        caddyfile, caddy_plist = self.write_prior_replacement_bundle(prior_caddy)
        caddyfile.chmod(0o640)
        caddy_plist.chmod(0o640)
        prior_plist = caddy_plist.read_bytes()
        runner = RecordingRunner(caddy_loaded=True)
        marker = "private-bootstrap-diagnostic-marker"
        phases = []

        def fail_new_bundle_until_timeout(argv):
            restoring = caddyfile.read_bytes() == prior_caddy
            phases.append("restore" if restoring else "activate")
            return subprocess.CompletedProcess(
                argv, 0 if restoring else 5, "", "" if restoring else marker
            )

        runner.bootstrap = fail_new_bundle_until_timeout
        clock = FakeClock()

        with self.assertRaises(InstallError) as caught:
            self.installer(
                runner, monotonic=clock.monotonic, sleep=clock.sleep
            ).install()

        self.assertEqual(
            str(caught.exception),
            "Caddy LaunchAgent activation failed; restored prior configuration and running state",
        )
        self.assertEqual(caddyfile.read_bytes(), prior_caddy)
        self.assertEqual(caddy_plist.read_bytes(), prior_plist)
        self.assertEqual(stat.S_IMODE(caddyfile.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(caddy_plist.stat().st_mode), 0o640)
        self.assertGreater(phases.count("activate"), 1)
        self.assertEqual(phases.count("restore"), 1)
        self.assertAlmostEqual(clock.now, 2.0)
        self.assertTrue(clock.sleeps)
        self.assertLessEqual(max(clock.sleeps), 0.05)
        self.assertTrue(runner.caddy_loaded)
        disclosed = "\n".join(
            [str(caught.exception), *(argument for call in runner.calls for argument in call)]
        )
        self.assertNotIn(marker, disclosed)

    def test_prior_job_restoration_waits_for_delayed_launchctl_visibility(self):
        prior = b"prior config with delayed recovery"
        installed, caddy_plist = self.write_prior_replacement_bundle(prior)
        prior_plist = caddy_plist.read_bytes()
        runner = RecordingRunner(
            caddy_states=[
                "loaded",
                "loaded",
                "loaded",
                "missing",
                "missing",
                "missing",
                "loaded",
            ]
        )
        bootstraps = 0

        def fail_activation_then_restore(argv):
            nonlocal bootstraps
            bootstraps += 1
            return subprocess.CompletedProcess(
                argv, 1 if bootstraps == 1 else 0, "", ""
            )

        runner.bootstrap = fail_activation_then_restore
        clock = FakeClock()
        with self.assertRaisesRegex(
            InstallError, "restored prior configuration and running state"
        ):
            self.installer(
                runner, monotonic=clock.monotonic, sleep=clock.sleep
            ).install()

        self.assertEqual(installed.read_bytes(), prior)
        self.assertEqual(caddy_plist.read_bytes(), prior_plist)
        self.assertEqual(clock.sleeps, [0.05])
        self.assertEqual(bootstraps, 2)
        self.assertTrue(runner.caddy_loaded)

    def test_failed_loaded_job_replacement_restores_exact_prior_bytes_and_job(self):
        prior = b"prior config with no final newline"
        installed, caddy_plist = self.write_prior_replacement_bundle(prior)
        prior_plist = caddy_plist.read_bytes()
        runner = RecordingRunner(caddy_loaded=True)
        bootstrapped = []

        def fail_then_restore(argv):
            bootstrapped.append((installed.read_bytes(), caddy_plist.read_bytes()))
            return subprocess.CompletedProcess(
                argv, 1 if len(bootstrapped) == 1 else 0, "", ""
            )

        runner.bootstrap = fail_then_restore

        with self.assertRaisesRegex(InstallError, "restored"):
            self.installer(runner).install()

        self.assertEqual(bootstrapped[0][0], render_caddyfile(self.registry, self.manifests).encode())
        self.assertEqual(bootstrapped[1], (prior, prior_plist))
        self.assertEqual(installed.read_bytes(), prior)

    def test_first_install_never_bootstraps_or_reloads_caddy(self):
        self.installer().install()

        flattened = [argument for call in self.runner.calls for argument in call]
        self.assertNotIn("bootstrap", flattened)
        self.assertNotIn("reload", flattened)

    def test_refuses_loaded_caddy_without_prior_managed_config_before_any_runtime_write(self):
        runner = RecordingRunner(caddy_loaded=True)

        with self.assertRaisesRegex(
            InstallError, "Stop the loaded Caddy LaunchAgent before installing"
        ):
            self.installer(runner).install()

        self.assertFalse(self.runtime.exists())
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())
        self.assertFalse(
            any(
                call[:2] == ["/opt/homebrew/bin/caddy", "reload"]
                for call in runner.calls
            )
        )

    def test_replaces_job_when_caddy_becomes_loaded_during_validation(self):
        installed, _caddy_plist = self.write_prior_caddy_bundle()
        runner = RecordingRunner(caddy_states=["missing", "loaded"])
        bootstrapped_bytes = []

        def bootstrap_caddy(argv):
            bootstrapped_bytes.append(installed.read_bytes())
            return subprocess.CompletedProcess(argv, 0, "", "")

        runner.bootstrap = bootstrap_caddy

        self.installer(runner).install()

        self.assertEqual(
            bootstrapped_bytes, [render_caddyfile(self.registry, self.manifests).encode()]
        )

    def test_refuses_caddy_loaded_during_validation_without_prior_live_config(self):
        runner = RecordingRunner(caddy_states=["missing", "loaded"])

        with self.assertRaisesRegex(
            InstallError, "Stop the loaded Caddy LaunchAgent before installing"
        ):
            self.installer(runner).install()

        self.assertFalse((self.runtime / "Caddyfile").exists())
        self.assertFalse(
            any(
                call[:2] == ["/opt/homebrew/bin/caddy", "reload"]
                for call in runner.calls
            )
        )

    def test_second_caddy_state_inspection_error_preserves_prior_live_config(self):
        installed, _caddy_plist = self.write_prior_caddy_bundle()
        runner = RecordingRunner(caddy_states=["missing", "error"])

        with self.assertRaisesRegex(InstallError, "inspect loaded Caddy"):
            self.installer(runner).install()

        self.assertEqual(installed.read_bytes(), b"prior config\n")
        self.assertFalse(
            any(
                call[:2] == ["/opt/homebrew/bin/caddy", "reload"]
                for call in runner.calls
            )
        )

    def test_caddy_that_stops_during_validation_restores_exact_prior_loaded_job(self):
        installed, caddy_plist = self.write_prior_caddy_bundle()
        marker = "sb_publishable_prior_loaded_marker"
        caddy_plist.write_bytes(
            render_caddy_plist(
                self.hosting_repository,
                self.runtime,
                environment=(("LOCAL_WEB_PROBE_PRIOR_VALUE", marker),),
            )
        )
        prior_caddy = installed.read_bytes()
        prior_plist = caddy_plist.read_bytes()
        runner = RecordingRunner(caddy_states=["loaded", "missing"])
        bootstrapped = []

        def observe_restoration(argv):
            bootstrapped.append(Path(argv[-1]).read_bytes())
            return subprocess.CompletedProcess(argv, 0, "", "")

        runner.bootstrap = observe_restoration

        with self.assertRaisesRegex(InstallError, "state changed.*restored") as caught:
            self.installer(runner).install()

        self.assertEqual(installed.read_bytes(), prior_caddy)
        self.assertEqual(caddy_plist.read_bytes(), prior_plist)
        self.assertEqual(bootstrapped, [prior_plist])
        self.assertTrue(runner.caddy_loaded)
        self.assertFalse(any(call[:2] == ["launchctl", "bootout"] for call in runner.calls))
        disclosed = "\n".join(
            [str(caught.exception), *(argument for call in runner.calls for argument in call)]
        )
        self.assertNotIn(marker, disclosed)

    def test_caddy_loaded_state_race_reports_distinct_restoration_failure(self):
        installed, caddy_plist = self.write_prior_caddy_bundle()
        marker = "sb_publishable_failed_restoration_marker"
        caddy_plist.write_bytes(
            render_caddy_plist(
                self.hosting_repository,
                self.runtime,
                environment=(("LOCAL_WEB_PROBE_PRIOR_VALUE", marker),),
            )
        )
        prior_caddy = installed.read_bytes()
        prior_plist = caddy_plist.read_bytes()
        runner = RecordingRunner(caddy_states=["loaded", "missing"])
        runner.bootstrap = lambda argv: subprocess.CompletedProcess(argv, 1, "", marker)

        with self.assertRaisesRegex(
            InstallError, "state changed.*LaunchAgent recovery failed"
        ) as caught:
            self.installer(runner).install()

        self.assertEqual(installed.read_bytes(), prior_caddy)
        self.assertEqual(caddy_plist.read_bytes(), prior_plist)
        self.assertFalse(runner.caddy_loaded)
        self.assertEqual(
            sum(call[:2] == ["launchctl", "bootstrap"] for call in runner.calls), 1
        )
        disclosed = "\n".join(
            [str(caught.exception), *(argument for call in runner.calls for argument in call)]
        )
        self.assertNotIn(marker, disclosed)

    def test_launchctl_inspection_failure_preserves_existing_caddyfile(self):
        self.runtime.mkdir(parents=True)
        installed = self.runtime / "Caddyfile"
        installed.write_bytes(b"prior config\n")
        self.runner.launchctl_returncode = 1
        self.runner.launchctl_stderr = "permission failure"

        with self.assertRaisesRegex(InstallError, "inspect loaded Caddy"):
            self.installer().install()

        self.assertEqual(installed.read_bytes(), b"prior config\n")

    def test_launchctl_exit_113_missing_service_allows_install(self):
        self.runner.launchctl_returncode = 113
        self.runner.launchctl_stderr = (
            'Could not find service "com.sean.local-web.caddy" '
            f"in domain for user gui: {os.getuid()}\n"
        )

        self.installer().install()

        self.assertTrue((self.runtime / "Caddyfile").is_file())

    def test_launchctl_exit_113_bad_request_missing_service_allows_install(self):
        self.runner.launchctl_returncode = 113
        self.runner.launchctl_stderr = (
            "Bad request.\n"
            'Could not find service "com.sean.local-web.caddy" '
            f"in domain for user gui: {os.getuid()}\n"
        )

        self.installer().install()

        self.assertTrue((self.runtime / "Caddyfile").is_file())

    def test_launchctl_exit_113_without_missing_diagnostic_preserves_caddyfile(self):
        self.runtime.mkdir(parents=True)
        installed = self.runtime / "Caddyfile"
        installed.write_bytes(b"prior config\n")
        self.runner.launchctl_returncode = 113
        self.runner.launchctl_stderr = "Permission denied\n"

        with self.assertRaisesRegex(InstallError, "inspect loaded Caddy"):
            self.installer().install()

        self.assertEqual(installed.read_bytes(), b"prior config\n")

    def test_launchctl_missing_phrase_for_wrong_target_is_not_accepted(self):
        diagnostics = (
            (
                'Could not find service "com.sean.local-web.samplealpha" '
                f"in domain for user gui: {os.getuid()}\n"
            ),
            (
                'Could not find service "com.sean.local-web.caddy" '
                f"in domain for user gui: {os.getuid() + 1}\n"
            ),
        )

        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                self.runner.launchctl_returncode = 113
                self.runner.launchctl_stderr = diagnostic

                with self.assertRaisesRegex(InstallError, "inspect loaded Caddy"):
                    self.installer().install()

    def test_launchctl_missing_line_with_unrecognised_extra_text_is_not_accepted(self):
        missing = (
            'Could not find service "com.sean.local-web.caddy" '
            f"in domain for user gui: {os.getuid()}\n"
        )

        for diagnostic in (f"Unexpected prefix\n{missing}", f"{missing}Unexpected suffix\n"):
            with self.subTest(diagnostic=diagnostic):
                self.runner.launchctl_returncode = 113
                self.runner.launchctl_stderr = diagnostic

                with self.assertRaisesRegex(InstallError, "inspect loaded Caddy"):
                    self.installer().install()

    def test_installs_missing_managed_hooks_with_private_executable_mode(self):
        self.installer().install()

        for host in self.registry.apps:
            for name in ("post-commit", "post-merge"):
                hook = host.repository / ".git" / "hooks" / name
                self.assertEqual(
                    hook.read_text(encoding="utf-8"),
                    render_hook(host.id, self.hosting_repository),
                )
                self.assertEqual(stat.S_IMODE(hook.stat().st_mode), 0o700)

    def test_replaces_older_managed_hook(self):
        hook = self.static_repository / ".git" / "hooks" / "post-commit"
        hook.write_text("#!/bin/sh\n# managed-by-local-web-server\nold body\n", encoding="utf-8")
        hook.chmod(0o600)

        self.installer().install()

        self.assertEqual(
            hook.read_text(encoding="utf-8"),
            render_hook("static-app", self.hosting_repository),
        )
        self.assertEqual(stat.S_IMODE(hook.stat().st_mode), 0o700)

    def test_does_not_change_repository_hook_directory_permissions(self):
        hooks = self.static_repository / ".git" / "hooks"
        hooks.chmod(0o755)

        self.installer().install()

        self.assertEqual(stat.S_IMODE(hooks.stat().st_mode), 0o755)

    def test_refuses_a_symlinked_repository_hooks_directory(self):
        hooks = self.static_repository / ".git" / "hooks"
        shutil.rmtree(hooks)
        external = self.root / "app-owned-hooks"
        external.mkdir()
        hooks.symlink_to(external, target_is_directory=True)

        with self.assertRaisesRegex(InstallError, "hooks directory"):
            self.installer().install()

        self.assertEqual(list(external.iterdir()), [])
        self.assertFalse(self.runtime.exists())

    def test_refuses_unmanaged_hook_before_writing_runtime_files(self):
        hook = self.service_repository / ".git" / "hooks" / "post-merge"
        hook.write_text("#!/bin/sh\necho preserve-me\n", encoding="utf-8")

        with self.assertRaisesRegex(InstallError, "unmanaged Git hook"):
            self.installer().install()

        self.assertEqual(hook.read_text(encoding="utf-8"), "#!/bin/sh\necho preserve-me\n")
        self.assertFalse(self.runtime.exists())

    def test_leaves_application_data_and_environment_files_untouched(self):
        original_data = self.app_data.read_bytes()
        original_environment = self.environment_file.read_bytes()
        original_data_mode = stat.S_IMODE(self.app_data.stat().st_mode)
        original_environment_mode = stat.S_IMODE(self.environment_file.stat().st_mode)

        self.installer().install()

        self.assertEqual(self.app_data.read_bytes(), original_data)
        self.assertEqual(self.environment_file.read_bytes(), original_environment)
        self.assertEqual(stat.S_IMODE(self.app_data.stat().st_mode), original_data_mode)
        self.assertEqual(
            stat.S_IMODE(self.environment_file.stat().st_mode), original_environment_mode
        )

    def test_reports_ac_sleep_command_without_executing_sudo(self):
        runner = RecordingRunner(ac_sleep=1)

        result = self.installer(runner).install()

        self.assertIn(
            "System sleep is enabled on AC power. Run: sudo pmset -c sleep 0",
            result.messages,
        )
        self.assertFalse(any(call and call[0] == "sudo" for call in runner.calls))

    def test_dry_run_reports_targets_without_writing_or_running_services(self):
        result = self.installer().install(dry_run=True)

        self.assertTrue(result.dry_run)
        self.assertIn(self.runtime / "Caddyfile", result.writes)
        self.assertIn(self.runtime / "platform" / "index" / INDEX_REGISTRY_NAME, result.writes)
        self.assertIn(self.runtime / "platform" / "theme.css", result.writes)
        self.assertFalse(self.runtime.exists())
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())
        self.assertFalse(any(call and call[0] == "launchctl" for call in self.runner.calls))
        self.assertFalse(any(call and call[0] == "/opt/homebrew/bin/caddy" for call in self.runner.calls))

    def test_remote_probe_values_are_confined_to_private_caddy_plist(self):
        installer = self.installer_with_remote_probe()
        rendered = installer._rendered_files()
        caddy_plist = (
            self.home / "Library" / "LaunchAgents" / "com.sean.local-web.caddy.plist"
        )
        markers = (
            "https://probe-value.example.test",
            "sb_publishable_probe_value",
            "private-probe-value",
        )

        plist = plistlib.loads(rendered[caddy_plist][0])

        self.assertEqual(
            plist.get("EnvironmentVariables"),
            {
                "LOCAL_WEB_PROBE_STATIC_APP_VITE_SUPABASE_URL": markers[0],
                "LOCAL_WEB_PROBE_STATIC_APP_VITE_SUPABASE_PUBLISHABLE_KEY": markers[1],
            },
        )
        for path, (content, _mode) in rendered.items():
            if path == caddy_plist:
                continue
            with self.subTest(path=path):
                for marker in markers:
                    self.assertNotIn(marker.encode(), content)

    def test_remote_probe_dry_run_never_discloses_values(self):
        installer = self.installer_with_remote_probe()
        markers = (
            "https://probe-value.example.test",
            "sb_publishable_probe_value",
            "private-probe-value",
        )

        result = installer.install(dry_run=True)

        self.assertTrue(result.dry_run)
        self.assertIn(self.runtime / "Caddyfile", result.writes)
        self.assertIn("VITE_SUPABASE_URL", "\n".join(result.messages))
        self.assertIn("VITE_SUPABASE_PUBLISHABLE_KEY", "\n".join(result.messages))
        self.assertFalse(self.runtime.exists())
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())
        disclosed = "\n".join(
            [*result.messages, *(argument for call in self.runner.calls for argument in call)]
        )
        for marker in markers:
            self.assertNotIn(marker, disclosed)

    def test_caddy_candidate_validation_receives_probe_values_only_in_subprocess_environment(self):
        installer = self.installer_with_remote_probe()
        markers = (
            "https://probe-value.example.test",
            "sb_publishable_probe_value",
        )

        result = installer.install()

        validation_index = next(
            index
            for index, call in enumerate(self.runner.calls)
            if call[:2] == ["/opt/homebrew/bin/caddy", "validate"]
        )
        validation_environment = self.runner.environments[validation_index]
        self.assertEqual(
            {
                name: value
                for name, value in validation_environment.items()
                if name.startswith("LOCAL_WEB_PROBE_")
            },
            {
                "LOCAL_WEB_PROBE_STATIC_APP_VITE_SUPABASE_URL": markers[0],
                "LOCAL_WEB_PROBE_STATIC_APP_VITE_SUPABASE_PUBLISHABLE_KEY": markers[1],
            },
        )
        disclosed = "\n".join(
            [*result.messages, *(argument for call in self.runner.calls for argument in call)]
        )
        for marker in markers:
            self.assertNotIn(marker, disclosed)

    def test_first_loaded_probe_activation_bootstraps_generated_environment_names(self):
        self.installer().install()
        prior_caddy_plist = (
            self.home / "Library" / "LaunchAgents" / "com.sean.local-web.caddy.plist"
        )
        self.assertNotIn(
            "EnvironmentVariables", plistlib.loads(prior_caddy_plist.read_bytes())
        )
        installer = self.installer_with_remote_probe()
        self.runner.caddy_loaded = True
        bootstrapped = []

        def observe_bootstrap(argv):
            plist = plistlib.loads(Path(argv[-1]).read_bytes())
            bootstrapped.append(plist)
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.runner.bootstrap = observe_bootstrap

        installer.install()

        self.assertEqual(len(bootstrapped), 1)
        self.assertEqual(
            set(bootstrapped[0]["EnvironmentVariables"]),
            {
                "LOCAL_WEB_PROBE_STATIC_APP_VITE_SUPABASE_URL",
                "LOCAL_WEB_PROBE_STATIC_APP_VITE_SUPABASE_PUBLISHABLE_KEY",
            },
        )
        self.assertTrue(bootstrapped[0]["RunAtLoad"])
        self.assertTrue(bootstrapped[0]["KeepAlive"])
        self.assertTrue(self.runner.caddy_loaded)

    def test_value_only_probe_change_replaces_loaded_job_and_persists_for_restart(self):
        installer = self.installer_with_remote_probe()
        installer.install()
        caddyfile = self.runtime / "Caddyfile"
        prior_caddyfile = caddyfile.read_bytes()
        self.runner.caddy_loaded = True
        self.runner.calls.clear()
        self.runner.environments.clear()
        changed_url = "https://changed-probe-value.example.test"
        changed_key = "sb_publishable_changed_probe_value"
        self.environment_file.write_text(
            f"VITE_SUPABASE_URL={changed_url}\n"
            f"VITE_SUPABASE_PUBLISHABLE_KEY={changed_key}\n",
            encoding="utf-8",
        )
        bootstrapped = []

        def observe_bootstrap(argv):
            bootstrapped.append(plistlib.loads(Path(argv[-1]).read_bytes()))
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.runner.bootstrap = observe_bootstrap

        installer.install()

        self.assertEqual(caddyfile.read_bytes(), prior_caddyfile)
        self.assertTrue(any(call[:2] == ["launchctl", "bootout"] for call in self.runner.calls))
        self.assertTrue(any(call[:2] == ["launchctl", "bootstrap"] for call in self.runner.calls))
        self.assertEqual(len(bootstrapped), 1)
        environment = bootstrapped[0]["EnvironmentVariables"]
        self.assertEqual(
            environment["LOCAL_WEB_PROBE_STATIC_APP_VITE_SUPABASE_URL"], changed_url
        )
        self.assertEqual(
            environment["LOCAL_WEB_PROBE_STATIC_APP_VITE_SUPABASE_PUBLISHABLE_KEY"],
            changed_key,
        )
        persisted = plistlib.loads(
            (self.home / "Library" / "LaunchAgents" / "com.sean.local-web.caddy.plist").read_bytes()
        )
        self.assertEqual(persisted["EnvironmentVariables"], environment)
        self.assertTrue(persisted["KeepAlive"])

    def test_failed_loaded_job_activation_restores_exact_prior_files_modes_and_job(self):
        installer = self.installer_with_remote_probe()
        installer.install()
        caddyfile = self.runtime / "Caddyfile"
        caddy_plist = (
            self.home / "Library" / "LaunchAgents" / "com.sean.local-web.caddy.plist"
        )
        caddyfile.chmod(0o640)
        caddy_plist.chmod(0o640)
        prior_caddyfile = caddyfile.read_bytes()
        prior_plist = caddy_plist.read_bytes()
        self.runner.caddy_loaded = True
        marker_url = "https://rollback-marker.example.test"
        marker_key = "sb_publishable_rollback_marker"
        self.environment_file.write_text(
            f"VITE_SUPABASE_URL={marker_url}\n"
            f"VITE_SUPABASE_PUBLISHABLE_KEY={marker_key}\n",
            encoding="utf-8",
        )
        bootstrapped_plists = []

        def fail_then_restore(argv):
            bootstrapped_plists.append(Path(argv[-1]).read_bytes())
            return subprocess.CompletedProcess(
                argv, 1 if len(bootstrapped_plists) == 1 else 0, "", ""
            )

        self.runner.bootstrap = fail_then_restore

        with self.assertRaises(InstallError) as caught:
            installer.install()

        self.assertEqual(caddyfile.read_bytes(), prior_caddyfile)
        self.assertEqual(caddy_plist.read_bytes(), prior_plist)
        self.assertEqual(stat.S_IMODE(caddyfile.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(caddy_plist.stat().st_mode), 0o640)
        self.assertEqual(bootstrapped_plists[-1], prior_plist)
        self.assertTrue(self.runner.caddy_loaded)
        disclosed = "\n".join(
            [str(caught.exception), *(argument for call in self.runner.calls for argument in call)]
        )
        self.assertNotIn(marker_url, disclosed)
        self.assertNotIn(marker_key, disclosed)

    def test_probe_resolution_failure_never_writes_or_discloses_values(self):
        installer = self.installer_with_remote_probe()
        marker = "https://invalid-probe-value.example.test/path"
        self.environment_file.write_text(
            f"VITE_SUPABASE_URL={marker}\n"
            "VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_probe_value\n",
            encoding="utf-8",
        )

        with self.assertRaises(InstallError) as caught:
            installer.install()

        disclosed = "\n".join(
            [str(caught.exception), *(argument for call in self.runner.calls for argument in call)]
        )
        self.assertNotIn(marker, disclosed)
        self.assertFalse(self.runtime.exists())
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_missing_probe_environment_file_stops_before_commands_or_writes(self):
        installer = self.installer_with_remote_probe()
        self.environment_file.unlink()

        with self.assertRaises(InstallError) as caught:
            installer.install()

        self.assertEqual(str(caught.exception), "cannot install generated local web files")
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.runtime.exists())
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_missing_probe_input_stops_before_commands_or_writes(self):
        installer = self.installer_with_remote_probe()
        marker = "https://missing-input-probe-value.example.test"
        self.environment_file.write_text(
            f"VITE_SUPABASE_URL={marker}\n", encoding="utf-8"
        )

        with self.assertRaises(InstallError) as caught:
            installer.install()

        self.assertEqual(str(caught.exception), "cannot install generated local web files")
        self.assertNotIn(marker, str(caught.exception))
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.runtime.exists())
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_invalid_probe_url_stops_before_commands_or_writes(self):
        installer = self.installer_with_remote_probe()
        marker = "https://invalid-url-probe-value.example.test/path"
        self.environment_file.write_text(
            f"VITE_SUPABASE_URL={marker}\n"
            "VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_probe_value\n",
            encoding="utf-8",
        )

        with self.assertRaises(InstallError) as caught:
            installer.install()

        self.assertEqual(str(caught.exception), "cannot install generated local web files")
        self.assertNotIn(marker, str(caught.exception))
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.runtime.exists())
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_invalid_probe_key_stops_before_commands_or_writes(self):
        installer = self.installer_with_remote_probe()
        marker = "invalid-publishable-probe-value"
        self.environment_file.write_text(
            "VITE_SUPABASE_URL=https://valid-probe-value.example.test\n"
            f"VITE_SUPABASE_PUBLISHABLE_KEY={marker}\n",
            encoding="utf-8",
        )

        with self.assertRaises(InstallError) as caught:
            installer.install()

        self.assertEqual(str(caught.exception), "cannot install generated local web files")
        self.assertNotIn(marker, str(caught.exception))
        self.assertEqual(self.runner.calls, [])
        self.assertFalse(self.runtime.exists())
        self.assertFalse((self.home / "Library" / "LaunchAgents").exists())

    def test_changed_public_probe_value_replaces_only_the_private_plist(self):
        installer = self.installer_with_remote_probe()
        first = installer.install()
        caddyfile = self.runtime / "Caddyfile"
        caddy_plist = (
            self.home / "Library" / "LaunchAgents" / "com.sean.local-web.caddy.plist"
        )
        first_caddyfile = caddyfile.read_bytes()
        first_plist = caddy_plist.read_bytes()
        self.environment_file.write_text(
            "VITE_SUPABASE_URL=https://changed-probe-value.example.test\n"
            "VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_changed_probe_value\n"
            "UNRELATED_PRIVATE_INPUT=private-probe-value\n",
            encoding="utf-8",
        )

        second = installer.install()

        previous_home = self.runtime / "home.previous.html"
        self.assertEqual(
            tuple(path for path in second.writes if path != previous_home),
            first.writes,
        )
        self.assertEqual(caddyfile.read_bytes(), first_caddyfile)
        self.assertNotEqual(caddy_plist.read_bytes(), first_plist)
        self.assertEqual(stat.S_IMODE(caddy_plist.stat().st_mode), 0o600)
        changed_plist = caddy_plist.read_bytes()

        third = installer.install()

        self.assertEqual(third.writes, second.writes)
        self.assertEqual(caddy_plist.read_bytes(), changed_plist)

    def test_identical_caddyfile_rerun_repairs_private_mode_without_restarting_job(self):
        installer = self.installer()
        installer.install()
        caddyfile = self.runtime / "Caddyfile"
        original = caddyfile.read_bytes()
        caddyfile.chmod(0o644)
        self.runner.caddy_loaded = True
        self.runner.calls.clear()
        self.runner.environments.clear()

        installer.install()

        self.assertEqual(caddyfile.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(caddyfile.stat().st_mode), 0o600)
        self.assertFalse(any(call[:2] == ["launchctl", "bootout"] for call in self.runner.calls))
        self.assertFalse(any(call[:2] == ["launchctl", "bootstrap"] for call in self.runner.calls))
        self.assertFalse(
            any(call[:2] == ["/opt/homebrew/bin/caddy", "reload"] for call in self.runner.calls)
        )

    def test_caddy_activation_failure_preserves_private_plist_and_prior_live_config(self):
        installer = self.installer_with_remote_probe()
        self.runtime.mkdir(parents=True)
        caddyfile = self.runtime / "Caddyfile"
        caddyfile.write_bytes(b"prior live config")
        caddy_plist = (
            self.home / "Library" / "LaunchAgents" / "com.sean.local-web.caddy.plist"
        )
        caddy_plist.parent.mkdir(parents=True)
        prior_plist = b"prior private plist bytes"
        caddy_plist.write_bytes(prior_plist)
        runner = RecordingRunner(caddy_loaded=True)
        bootstraps: list[tuple[bytes, bytes]] = []

        def fail_then_restore(argv):
            bootstraps.append((caddyfile.read_bytes(), caddy_plist.read_bytes()))
            return subprocess.CompletedProcess(
                argv, 1 if len(bootstraps) == 1 else 0, "", ""
            )

        runner.bootstrap = fail_then_restore

        with self.assertRaisesRegex(InstallError, "restored"):
            Installer(
                self.registry,
                self.manifests,
                repository=self.hosting_repository,
                home=self.home,
                run=runner,
                theme_gate=self.theme_gate,
            ).install()

        self.assertEqual(caddyfile.read_bytes(), b"prior live config")
        self.assertEqual(caddy_plist.read_bytes(), prior_plist)
        self.assertEqual(bootstraps[-1], (b"prior live config", prior_plist))

    def test_rejects_probe_environment_names_that_collide_after_caddy_mapping(self):
        static_manifest = self.manifests["static-app"]
        self.manifests["static-app"] = AppManifest(
            static_manifest.schema_version,
            static_manifest.id,
            static_manifest.title,
            static_manifest.route,
            static_manifest.kind,
            BuildSpec(
                static_manifest.build.commands,
                static_manifest.build.output,
                ("PROBE_URL", "probe_url"),
            ),
            static_manifest.health_path,
            static_manifest.service,
        )
        self.environment_file.write_text(
            "PROBE_URL=https://probe.example.test\n"
            "probe_url=sb_publishable_probe_value\n",
            encoding="utf-8",
        )
        static_host, service_host = self.registry.apps
        collision_probe = BackendProbeSpec(
            ("PROBE_URL", "probe_url"),
            "PROBE_URL",
            "/auth/v1/health",
            (("apikey", "probe_url"),),
        )
        self.registry = HostRegistry(
            self.registry.host,
            self.registry.runtime_root,
            (
                HostApp(
                    static_host.id,
                    static_host.repository,
                    static_host.auto_deploy,
                    static_host.environment_file,
                    static_host.environment,
                    static_host.port,
                    static_host.start_command,
                    collision_probe,
                ),
                service_host,
            ),
        )

        with self.assertRaisesRegex(InstallError, "duplicate generated Caddy probe environment"):
            self.installer()._rendered_files()

    def test_file_updates_use_temporary_sibling_replacements(self):
        real_replace = os.replace
        replacements = []

        def record_replace(source, destination, **kwargs):
            source_path = Path(source)
            destination_path = Path(destination)
            replacements.append((source_path, destination_path))
            return real_replace(source, destination, **kwargs)

        with patch("local_web_server.install.os.replace", side_effect=record_replace):
            result = self.installer().install()

        destinations = [destination for _, destination in replacements]
        for path in result.writes:
            with self.subTest(path=path):
                self.assertTrue(path in destinations or Path(path.name) in destinations)
        for source, destination in replacements:
            self.assertEqual(source.parent, destination.parent)
            self.assertNotEqual(source, destination)

    def test_installer_renders_manifests_from_exact_main_not_dirty_worktrees(self):
        from local_web_server.install import load_main_manifests

        repository = self.root / "exact-main-app"
        repository.mkdir()
        committed = {
            "schemaVersion": 1,
            "id": "exact-main-app",
            "title": "Main App",
            "route": "/main-route",
            "kind": "static",
            "build": {"commands": [["true"]], "output": "public", "environment": []},
            "healthPath": "/main-route/",
        }
        init_git_repo(repository, {"local-web.json": json.dumps(committed)})
        dirty = dict(committed)
        dirty["title"] = "Dirty App"
        dirty["route"] = "/dirty-route"
        dirty["healthPath"] = "/dirty-route/"
        (repository / "local-web.json").write_text(json.dumps(dirty), encoding="utf-8")
        registry = HostRegistry(
            host="test-mac.local",
            runtime_root=self.runtime,
            apps=(HostApp(
                "exact-main-app", repository, True, None, (), None, None
            ),),
        )

        manifests = load_main_manifests(registry)
        Installer(
            registry,
            manifests,
            repository=self.hosting_repository,
            home=self.home,
            run=self.runner,
            theme_gate=self.theme_gate,
        ).install()

        caddyfile = (self.runtime / "Caddyfile").read_text(encoding="utf-8")
        self.assertIn("/main-route", caddyfile)
        self.assertNotIn("/dirty-route", caddyfile)

    def test_unchanged_rerun_preserves_distinct_previous_home_and_omits_its_write(self):
        installer = self.installer()
        installer.install()
        current_home = self.runtime / "home.html"
        previous_home = self.runtime / "home.previous.html"
        home_a = current_home.read_bytes()
        home_b = b"<!doctype html><script src='bundle-b'></script>"
        (self.index_dist / "index.html").write_bytes(home_b)

        changed = installer.install()

        self.assertEqual(current_home.read_bytes(), home_b)
        self.assertEqual(previous_home.read_bytes(), home_a)
        self.assertIn(previous_home, changed.writes)

        unchanged = installer.install()

        self.assertEqual(current_home.read_bytes(), home_b)
        self.assertEqual(previous_home.read_bytes(), home_a)
        self.assertNotIn(previous_home, unchanged.writes)
        self.assertEqual(stat.S_IMODE(previous_home.stat().st_mode), 0o600)
        self.assertEqual(
            (self.runtime / "platform" / "index" / INDEX_REGISTRY_NAME).read_bytes(),
            render_index_registry(self.registry, self.manifests),
        )

    def test_locked_interleaving_snapshots_actual_former_home_for_next_switch(self):
        installer = self.installer()
        installer.install()
        current_home = self.runtime / "home.html"
        previous_home = self.runtime / "home.previous.html"
        home_b = b"<!doctype html><script src='bundle-b'></script>"
        home_c = b"<!doctype html><script src='bundle-c'></script>"
        original_transaction = ThemeStore.transaction
        interleaved = False

        @contextmanager
        def interleaving_transaction(store):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                (self.index_dist / "index.html").write_bytes(home_b)
                self.installer().install()
                (self.index_dist / "index.html").write_bytes(home_c)
            with original_transaction(store):
                yield store

        (self.index_dist / "index.html").write_bytes(home_c)
        with patch.object(ThemeStore, "transaction", interleaving_transaction):
            result = installer.install()

        self.assertTrue(interleaved)
        self.assertEqual(current_home.read_bytes(), home_c)
        self.assertEqual(previous_home.read_bytes(), home_b)
        self.assertIn(previous_home, result.writes)
        self.assertLess(result.writes.index(previous_home), result.writes.index(current_home))

    def test_noop_still_rejects_unsafe_previous_home(self):
        installer = self.installer()
        installer.install()
        current_home = self.runtime / "home.html"
        previous_home = self.runtime / "home.previous.html"
        previous_home.symlink_to(self.environment_file)
        prior = current_home.read_bytes()

        with self.assertRaisesRegex(
            InstallError, "^cannot install generated local web files$"
        ):
            installer.install()

        self.assertEqual(current_home.read_bytes(), prior)
        self.assertTrue(previous_home.is_symlink())

    def test_dry_run_conditional_previous_target_is_only_a_read_only_snapshot(self):
        installer = self.installer()
        installer.install()
        current_home = self.runtime / "home.html"
        previous_home = self.runtime / "home.previous.html"
        home_a = current_home.read_bytes()
        home_b = b"<!doctype html><script src='bundle-b'></script>"
        (self.index_dist / "index.html").write_bytes(home_b)

        predicted_change = installer.install(dry_run=True)

        self.assertIn(previous_home, predicted_change.writes)
        self.assertEqual(current_home.read_bytes(), home_a)
        self.assertFalse(previous_home.exists())

        current_home.write_bytes(home_b)
        predicted_noop = installer.install(dry_run=True)

        self.assertNotIn(previous_home, predicted_noop.writes)
        self.assertEqual(current_home.read_bytes(), home_b)
        self.assertFalse(previous_home.exists())

    def test_index_assets_and_registry_publish_before_exact_home_switch(self):
        import local_web_server.install as install_module

        self.installer().install()
        previous_home = (self.runtime / "home.html").read_bytes()
        (self.index_assets / "index-old.css").unlink()
        (self.index_assets / "index-old.js").unlink()
        new_home = (
            b"<!doctype html><link rel='stylesheet' href='/_local-web/platform/index/assets/index-new.css'>"
            b"<script type='module' src='/_local-web/platform/index/assets/index-new.js'></script>"
        )
        (self.index_dist / "index.html").write_bytes(new_home)
        (self.index_assets / "index-new.css").write_bytes(b"body { color: white; }")
        (self.index_assets / "index-new.js").write_bytes(b"console.log('new')")
        writes = []
        real_atomic_write = install_module._atomic_write

        def record_write(path, content, mode):
            writes.append(Path(path))
            real_atomic_write(path, content, mode)

        with patch("local_web_server.install._atomic_write", side_effect=record_write):
            result = self.installer().install()

        home = self.runtime / "home.html"
        previous = self.runtime / "home.previous.html"
        registry = self.runtime / "platform" / "index" / INDEX_REGISTRY_NAME
        new_assets = tuple(
            self.runtime / "platform" / "index" / "assets" / name
            for name in ("index-new.css", "index-new.js")
        )
        for new_asset in new_assets:
            with self.subTest(new_asset=new_asset):
                self.assertLess(writes.index(new_asset), writes.index(registry))
                self.assertLess(writes.index(new_asset), writes.index(home))
        self.assertLess(writes.index(registry), writes.index(home))
        self.assertLess(writes.index(previous), writes.index(home))
        self.assertIn(previous, result.writes)
        self.assertLess(result.writes.index(previous), result.writes.index(home))
        self.assertEqual(home.read_bytes(), new_home)
        self.assertEqual(previous.read_bytes(), previous_home)
        for name in ("index-old.css", "index-old.js", "index-new.css", "index-new.js"):
            with self.subTest(name=name):
                self.assertTrue(
                    (self.runtime / "platform" / "index" / "assets" / name).is_file()
                )

    def test_asset_or_registry_failure_preserves_current_home_and_rerun_completes(self):
        import local_web_server.install as install_module

        for sequence, failed_relative in enumerate((
            Path("assets/index-new.js"),
            Path(INDEX_REGISTRY_NAME),
        )):
            with self.subTest(failed_relative=failed_relative):
                self.installer().install()
                current = self.runtime / "home.html"
                prior = current.read_bytes()
                new_home = (
                    f"<!doctype html><script src='new-{sequence}'></script>".encode()
                )
                (self.index_dist / "index.html").write_bytes(new_home)
                (self.index_assets / "index-new.js").write_bytes(b"console.log('new')")
                failed_target = self.runtime / "platform" / "index" / failed_relative
                real_atomic_write = install_module._atomic_write

                def fail_target(path, content, mode):
                    if Path(path) == failed_target:
                        raise InstallError("simulated index publication failure")
                    real_atomic_write(path, content, mode)

                with patch("local_web_server.install._atomic_write", side_effect=fail_target):
                    with self.assertRaisesRegex(InstallError, "index publication failure"):
                        self.installer().install()

                self.assertEqual(current.read_bytes(), prior)
                self.installer().install()
                self.assertEqual(current.read_bytes(), new_home)

    def test_failed_atomic_home_switch_preserves_prior_bytes_and_rerun_completes(self):
        import local_web_server.install as install_module

        self.installer().install()
        current = self.runtime / "home.html"
        prior = current.read_bytes()
        new_home = b"<!doctype html><script src='replacement'></script>"
        (self.index_dist / "index.html").write_bytes(new_home)
        real_atomic_write = install_module._atomic_write
        failed_once = False

        def fail_home_once(path, content, mode):
            nonlocal failed_once
            if Path(path) == current and not failed_once:
                failed_once = True
                raise InstallError("simulated home switch failure")
            real_atomic_write(path, content, mode)

        with patch("local_web_server.install._atomic_write", side_effect=fail_home_once):
            with self.assertRaisesRegex(InstallError, "home switch failure"):
                self.installer().install()

        self.assertEqual(current.read_bytes(), prior)
        self.installer().install()
        self.assertEqual(current.read_bytes(), new_home)

    def test_unsafe_current_or_previous_home_fails_before_publication_with_sanitized_error(self):
        for unsafe_name in ("home.html", "home.previous.html"):
            for unsafe_kind in ("symlink", "directory"):
                with self.subTest(unsafe_name=unsafe_name, unsafe_kind=unsafe_kind):
                    self.runtime.mkdir(parents=True, exist_ok=True)
                    unsafe = self.runtime / unsafe_name
                    if unsafe_kind == "symlink":
                        unsafe.symlink_to(self.environment_file)
                    else:
                        unsafe.mkdir()
                    with self.assertRaisesRegex(
                        InstallError, "^cannot install generated local web files$"
                    ) as caught:
                        self.installer().install()
                    self.assertNotIn(str(self.environment_file), str(caught.exception))
                    self.assertFalse((self.runtime / "Caddyfile").exists())
                    if unsafe_kind == "symlink":
                        unsafe.unlink()
                    else:
                        unsafe.rmdir()

    def test_rerun_repairs_a_partial_install_after_a_later_file_failure(self):
        import local_web_server.install as install_module

        real_atomic_write = install_module._atomic_write
        failed_target = (
            self.home
            / "Library"
            / "LaunchAgents"
            / "com.sean.local-web.caddy.plist"
        )
        failed_once = False

        def fail_later_file(path, content, mode):
            nonlocal failed_once
            if path == failed_target and not failed_once:
                failed_once = True
                raise InstallError("simulated later-file failure")
            real_atomic_write(path, content, mode)

        with patch("local_web_server.install._atomic_write", side_effect=fail_later_file):
            with self.assertRaisesRegex(InstallError, "later-file"):
                self.installer().install()

        self.assertFalse((self.runtime / "Caddyfile").exists())
        self.assertFalse((self.runtime / "home.html").exists())
        self.assertFalse(failed_target.exists())

        result = self.installer().install()

        self.assertTrue(all(path.is_file() for path in result.writes))


class InstallScriptTests(unittest.TestCase):
    def test_setup_parser_accepts_dry_run_and_explicit_preparation(self):
        self.assertFalse(build_install_parser().parse_args([]).dry_run)
        self.assertTrue(build_install_parser().parse_args(["--dry-run"]).dry_run)
        args = build_install_parser().parse_args(["--dry-run", "--prepare-tailscale-port-migration"])
        self.assertTrue(args.dry_run)
        self.assertTrue(args.prepare_tailscale_port_migration)
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit) as raised:
            build_install_parser().parse_args(["install"])
        self.assertEqual(raised.exception.code, 2)

    def test_setup_propagates_explicit_preparation_flag(self):
        result = PlatformInstallationResult(
            InstallResult((), (), True), AgentSkillInstallResult(Path("/tmp/skill"), True, True),
        )
        with patch("scripts.install_local_web.install_platform", return_value=result) as install, patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(install_main(["--dry-run", "--prepare-tailscale-port-migration"]), 0)
        self.assertEqual(install.call_args.kwargs, {
            "dry_run": True, "prepare_tailscale_port_migration": True,
        })

    def test_setup_returns_two_for_invalid_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("scripts.install_local_web.repository", Path(directory)):
                with redirect_stderr(StringIO()) as stderr:
                    result = install_main(["--dry-run"])

        self.assertEqual(result, 2)
        self.assertIn("install-local-web:", stderr.getvalue())

    def test_setup_delegates_platform_installation_and_formats_dry_run(self):
        expected_repository = Path("/private/tmp/local-web-server-build-fixture")
        result = PlatformInstallationResult(
            InstallResult((Path("/private/tmp/home.html"),), ("message",), True),
            AgentSkillInstallResult(
                Path("/private/tmp/profile/.codex/skills/local-web-app-development"),
                True,
                True,
            ),
        )

        with (
            patch("scripts.install_local_web.repository", expected_repository),
            patch(
                "scripts.install_local_web.install_platform", return_value=result
            ) as install,
            patch("sys.stdout", new_callable=StringIO) as stdout,
        ):
            code = install_main(["--dry-run"])

        self.assertEqual(code, 0)
        install.assert_called_once_with(expected_repository, dry_run=True)
        self.assertIn("Would write: /private/tmp/home.html", stdout.getvalue())
        self.assertIn("message", stdout.getvalue())
        self.assertIn(
            "Would link agent skill: /private/tmp/profile/.codex/skills/local-web-app-development",
            stdout.getvalue(),
        )

    def test_setup_sanitizes_platform_installation_failure(self):
        with (
            patch(
                "scripts.install_local_web.install_platform",
                side_effect=InstallError("agent skill installation failed"),
            ),
            redirect_stderr(StringIO()) as stderr,
        ):
            code = install_main([])

        self.assertEqual(code, 2)
        self.assertEqual(
            stderr.getvalue(), "install-local-web: agent skill installation failed\n"
        )


if __name__ == "__main__":
    unittest.main()
