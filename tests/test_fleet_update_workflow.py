import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests.suites import acceptance


class FleetUpdateWorkflowTests(unittest.TestCase):

    def test_workflow_failure_cleans_owned_resources_and_sanitizes_output(self):
        import scripts.verify_fleet_update_workflow as verifier
        from unittest.mock import Mock

        roots, lines = [], []
        fleet = Mock()
        fleet.apply.side_effect = RuntimeError("PRIVATE /private/fixture")
        instance = verifier.FleetUpdateWorkflowVerifier(
            temporary_directory_factory=verifier.RecordingTemporaryDirectory(roots),
            fleet_factory=lambda *_args, **_kwargs: fleet,
            emit=lines.append,
        )
        self.assertEqual(instance.run(), 1)
        fleet.close.assert_called_once()
        self.assertEqual(len(roots), 1)
        self.assertFalse(roots[0].exists())
        report = "\n".join(lines)
        for private in ("PRIVATE", "/private/fixture", str(roots[0])):
            self.assertNotIn(private, report)


    @acceptance
    def test_real_disposable_coordinator_recovers_and_continues(self):
        """The acceptance uses the production updater/checker, not marker doubles."""
        import scripts.verify_fleet_update_workflow as verifier

        with tempfile.TemporaryDirectory() as temporary:
            fleet = verifier._DisposableFleet(Path(temporary))
            try:
                fleet.create()
                fleet.preview()
                self.assertEqual(
                    fleet.cli_preview_statuses(),
                    (
                        ("current-static", "CURRENT"),
                        ("ready-static", "READY"),
                        ("legacy", "SKIPPED"),
                    ),
                )
                result = fleet.apply()

                self.assertEqual(
                    [(item.app_id, item.status) for item in result.apps],
                    [
                        ("current-static", "CURRENT"),
                        ("ready-static", "UPDATED"),
                        ("ready-service", "UPDATED"),
                        ("failing-static", "FAILED_RECOVERED"),
                        ("later-static", "UPDATED"),
                        ("legacy", "SKIPPED"),
                    ],
                )
                self.assertTrue(fleet.checks_ran)
                self.assertTrue(fleet.releases_match_commits())
                self.assertTrue(fleet.legacy_is_untouched())
                process_group = fleet.service.process_group
                # Negative checks reuse this populated fleet rather than rebuilding
                # it for each verifier assertion.
                original = fleet.recovery_original()
                self.assertTrue(fleet.recovery_matches(original))
                recovered = fleet.apps / "failing-static"
                release_key = recovered.resolve()
                saved_release = fleet.releases[release_key]
                fleet.releases[release_key] = "wrong-live-release"
                self.assertFalse(fleet.recovery_matches(original))
                fleet.releases[release_key] = saved_release
                domain = recovered / "domain-note.txt"
                saved_domain = domain.read_bytes() if domain.exists() else None
                domain.write_text("changed domain data\n")
                self.assertFalse(fleet.recovery_matches(original))
                if saved_domain is None:
                    domain.unlink()
                else:
                    domain.write_bytes(saved_domain)

                service = fleet.apps / "ready-service"
                release_source = service / "release/server/service.py"
                saved_source = release_source.read_bytes()
                release_source.write_bytes(b"# mismatched release\n")
                with self.assertRaises(RuntimeError):
                    fleet.activator.activate(service)
                release_source.write_bytes(saved_source)
                manifest_path = service / "local-web.json"
                saved_manifest = manifest_path.read_bytes()
                manifest = json.loads(saved_manifest)
                manifest["service"]["startCommand"].insert(2, "-u")
                manifest_path.write_text(json.dumps(manifest))
                with self.assertRaises(RuntimeError):
                    fleet.activator.activate(service)
                manifest_path.write_bytes(saved_manifest)
                self.assertTrue(fleet.retry_is_noop())
            finally:
                fleet.close()
            self.assertIsNotNone(process_group)
            self.assertFalse(verifier._group_exists(process_group))


    @acceptance
    def test_bounded_runner_reaps_descendants_after_every_outcome(self):
        """A dead leader must not allow an inherited-pipe descendant to survive."""
        import scripts.verify_fleet_update_workflow as verifier

        for outcome in ("success", "nonzero", "timeout", "overflow", "failure"):
            with self.subTest(outcome=outcome):
                result = verifier.run_bounded_probe(outcome)
                self.assertEqual(result, outcome)
                self.assertTrue(verifier.probe_process_group_reaped())
                self.assertTrue(verifier.probe_descendant_reaped())


    @acceptance
    def test_disposable_service_allows_bounded_delayed_readiness(self):
        import scripts.verify_fleet_update_workflow as verifier

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            program = root / "service.py"
            program.write_text(
                "import argparse\n"
                "import time\n"
                "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
                "parser = argparse.ArgumentParser()\n"
                "parser.add_argument('--port', type=int, required=True)\n"
                "port = parser.parse_args().port\n"
                "time.sleep(5.2)\n"
                "class Health(BaseHTTPRequestHandler):\n"
                " def do_GET(self):\n"
                "  self.send_response(204 if self.path == '/healthz' else 404)\n"
                "  self.end_headers()\n"
                " def log_message(self, *args): pass\n"
                "ThreadingHTTPServer(('127.0.0.1', port), Health).serve_forever()\n",
                encoding="utf-8",
            )
            command = (
                "/usr/bin/env", "python3", str(program), "--port", "{port}"
            )
            identity = verifier._CheckedService(
                command,
                verifier.expand_service_command_template(
                    command,
                    port=verifier._CHECK_PORT,
                    release=root,
                    repository=root,
                ),
                root,
                "/healthz",
                verifier._CHECK_PORT,
            )
            service = verifier._PrivateHttpService()
            failure = None
            try:
                service.ensure_running(identity, root)
            except RuntimeError as error:
                failure = error
            finally:
                service.close()

        self.assertIsNone(failure)

    @acceptance
    def test_failed_disposable_service_start_retains_no_candidate_state(self):
        import scripts.verify_fleet_update_workflow as verifier

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = (
                "/usr/bin/env",
                "python3",
                "-c",
                "import time; time.sleep(60)",
                "{port}",
            )
            identity = verifier._CheckedService(
                command,
                verifier.expand_service_command_template(
                    command,
                    port=verifier._CHECK_PORT,
                    release=root,
                    repository=root,
                ),
                root,
                "/healthz",
                verifier._CHECK_PORT,
            )
            service = verifier._PrivateHttpService()
            try:
                with (
                    patch.object(
                        verifier, "_SERVICE_START_TIMEOUT_SECONDS", 0.5
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    service.ensure_running(identity, root)

                self.assertEqual(
                    (service.process, service.port, service._identity),
                    (None, None, None),
                )
                self.assertIsNone(service.process_group)
            finally:
                service.close()

    @acceptance
    def test_disposable_service_health_bypasses_ambient_url_openers(self):
        import scripts.verify_fleet_update_workflow as verifier

        class Health(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(204 if self.path == "/healthz" else 404)
                self.end_headers()

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Health)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        identity = verifier._CheckedService(
            (), (), Path.cwd(), "/healthz", verifier._CHECK_PORT
        )
        try:
            with patch(
                "urllib.request.urlopen",
                side_effect=AssertionError("ambient URL opener was used"),
            ):
                verifier._PrivateHttpService._check_port(
                    identity, server.server_address[1]
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

        self.assertFalse(thread.is_alive())
