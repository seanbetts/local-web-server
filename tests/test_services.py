import os
import subprocess
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import call, patch

from local_web_server.services import (
    HealthResult,
    HttpHealthChecker,
    LaunchctlServiceController,
    ServiceState,
)
from tests.helpers import FakeHealthChecker, FakeServiceController
from tests.redirect_fixture import RedirectServer


class HealthHandler(BaseHTTPRequestHandler):
    status = 200
    body = b"ok"
    required_host = None
    methods = []

    def do_GET(self):
        self.methods.append("GET")
        self._respond(include_body=True)

    def do_HEAD(self):
        self.methods.append("HEAD")
        self._respond(include_body=False)

    def _respond(self, *, include_body):
        status = self.status
        if (
            self.required_host is not None
            and self.headers.get("Host") != self.required_host
        ):
            status = 403
        self.send_response(status)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        if include_body:
            self.wfile.write(self.body)

    def log_message(self, format, *args):
        pass


class HttpServer:
    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/healthz"

    def __exit__(self, exc_type, exc_value, traceback):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


class LaunchctlServiceControllerTests(unittest.TestCase):
    def setUp(self):
        self.controller = LaunchctlServiceController()
        self.label = "com.sean.local-web.samplealpha"
        self.target = f"gui/{os.getuid()}/{self.label}"
        self.domain = f"gui/{os.getuid()}"
        self.plist = Path("/tmp/com.sean.local-web.samplealpha.plist")

    def completed(self, argv, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(argv, returncode, stdout, stderr)

    @patch("local_web_server.services.subprocess.run")
    def test_state_parses_running_job(self, run):
        run.return_value = self.completed(
            ["launchctl", "print", self.target], stdout="\tstate = running\n"
        )

        self.assertEqual(self.controller.state(self.label), ServiceState.RUNNING)
        run.assert_called_once_with(
            ["launchctl", "print", self.target],
            capture_output=True,
            check=False,
            text=True,
        )

    @patch("local_web_server.services.subprocess.run")
    def test_state_parses_loaded_but_stopped_job(self, run):
        run.return_value = self.completed(
            ["launchctl", "print", self.target], stdout="\tstate = exited\n"
        )

        self.assertEqual(self.controller.state(self.label), ServiceState.STOPPED)

    @patch("local_web_server.services.subprocess.run")
    def test_state_recognises_only_documented_missing_service_result(self, run):
        run.return_value = self.completed(
            ["launchctl", "print", self.target],
            returncode=3,
            stderr=(
                'Could not find service "com.sean.local-web.samplealpha" '
                f"in domain for user gui: {os.getuid()}\n"
            ),
        )

        self.assertEqual(self.controller.state(self.label), ServiceState.MISSING)

    @patch("local_web_server.services.subprocess.run")
    def test_state_recognises_macos_exit_113_missing_service_result(self, run):
        run.return_value = self.completed(
            ["launchctl", "print", self.target],
            returncode=113,
            stderr=(
                'Could not find service "com.sean.local-web.samplealpha" '
                f"in domain for user gui: {os.getuid()}\n"
            ),
        )

        self.assertEqual(self.controller.state(self.label), ServiceState.MISSING)

    @patch("local_web_server.services.subprocess.run")
    def test_state_recognises_macos_bad_request_missing_service_result(self, run):
        run.return_value = self.completed(
            ["launchctl", "print", self.target],
            returncode=113,
            stderr=(
                "Bad request.\n"
                'Could not find service "com.sean.local-web.samplealpha" '
                f"in domain for user gui: {os.getuid()}\n"
            ),
        )

        self.assertEqual(self.controller.state(self.label), ServiceState.MISSING)

    @patch("local_web_server.services.subprocess.run")
    def test_state_surfaces_exit_113_without_missing_service_diagnostic(self, run):
        run.return_value = self.completed(
            ["launchctl", "print", self.target],
            returncode=113,
            stderr="Permission denied\n",
        )

        with self.assertRaises(subprocess.CalledProcessError):
            self.controller.state(self.label)

    @patch("local_web_server.services.subprocess.run")
    def test_state_rejects_missing_phrase_for_wrong_target(self, run):
        diagnostics = (
            (
                'Could not find service "com.sean.local-web.caddy" '
                f"in domain for user gui: {os.getuid()}\n"
            ),
            (
                'Could not find service "com.sean.local-web.samplealpha" '
                f"in domain for user gui: {os.getuid() + 1}\n"
            ),
        )

        for diagnostic in diagnostics:
            with self.subTest(diagnostic=diagnostic):
                run.return_value = self.completed(
                    ["launchctl", "print", self.target],
                    returncode=113,
                    stderr=diagnostic,
                )

                with self.assertRaises(subprocess.CalledProcessError):
                    self.controller.state(self.label)

    @patch("local_web_server.services.subprocess.run")
    def test_state_rejects_missing_line_with_unrecognised_extra_text(self, run):
        missing = (
            'Could not find service "com.sean.local-web.samplealpha" '
            f"in domain for user gui: {os.getuid()}\n"
        )

        for diagnostic in (f"Unexpected prefix\n{missing}", f"{missing}Unexpected suffix\n"):
            with self.subTest(diagnostic=diagnostic):
                run.return_value = self.completed(
                    ["launchctl", "print", self.target],
                    returncode=113,
                    stderr=diagnostic,
                )

                with self.assertRaises(subprocess.CalledProcessError):
                    self.controller.state(self.label)

    @patch("local_web_server.services.subprocess.run")
    def test_state_surfaces_non_missing_launchctl_failure(self, run):
        run.return_value = self.completed(
            ["launchctl", "print", self.target], returncode=5, stderr="Input/output error\n"
        )

        with self.assertRaises(subprocess.CalledProcessError):
            self.controller.state(self.label)

    @patch("local_web_server.services.subprocess.run")
    def test_ensure_running_bootstraps_missing_job_without_kickstarting_it(self, run):
        run.side_effect = [
            self.completed(
                ["launchctl", "print", self.target],
                returncode=3,
                stderr=(
                    'Could not find service "com.sean.local-web.samplealpha" '
                    f"in domain for user gui: {os.getuid()}\n"
                ),
            ),
            self.completed(["launchctl", "bootstrap", self.domain, str(self.plist)]),
            self.completed(["launchctl", "kickstart", "-k", self.target]),
        ]

        self.controller.ensure_running(self.label, self.plist)

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["launchctl", "print", self.target],
                ["launchctl", "bootstrap", self.domain, str(self.plist)],
            ],
        )

    @patch("local_web_server.services.subprocess.run")
    def test_ensure_running_surfaces_bootstrap_failure(self, run):
        run.side_effect = [
            self.completed(
                ["launchctl", "print", self.target],
                returncode=3,
                stderr=(
                    'Could not find service "com.sean.local-web.samplealpha" '
                    f"in domain for user gui: {os.getuid()}\n"
                ),
            ),
            self.completed(
                ["launchctl", "bootstrap", self.domain, str(self.plist)],
                returncode=5,
                stderr="Bootstrap failed: 5: Input/output error\n",
            ),
        ]

        with self.assertRaises(subprocess.CalledProcessError):
            self.controller.ensure_running(self.label, self.plist)

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["launchctl", "print", self.target],
                ["launchctl", "bootstrap", self.domain, str(self.plist)],
            ],
        )

    @patch("local_web_server.services.subprocess.run")
    def test_ensure_running_gracefully_replaces_an_already_running_job(self, run):
        run.side_effect = [
            self.completed(
                ["launchctl", "print", self.target], stdout="\tstate = running\n"
            ),
            self.completed(["launchctl", "bootout", self.domain, str(self.plist)]),
            self.completed(["launchctl", "bootstrap", self.domain, str(self.plist)]),
        ]

        self.controller.ensure_running(self.label, self.plist)

        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [
                ["launchctl", "print", self.target],
                ["launchctl", "bootout", self.domain, str(self.plist)],
                ["launchctl", "bootstrap", self.domain, str(self.plist)],
            ],
        )

    @patch("local_web_server.services.subprocess.run")
    def test_restart_gracefully_replaces_only_named_app_service(self, run):
        run.side_effect = [
            self.completed(["launchctl", "bootout", self.domain, str(self.plist)]),
            self.completed(["launchctl", "bootstrap", self.domain, str(self.plist)]),
        ]

        self.controller.restart(self.label, self.plist)

        self.assertEqual(
            [record.args[0] for record in run.call_args_list],
            [
                ["launchctl", "bootout", self.domain, str(self.plist)],
                ["launchctl", "bootstrap", self.domain, str(self.plist)],
            ],
        )

    @patch("local_web_server.services.subprocess.run")
    def test_stop_boots_out_named_service(self, run):
        run.return_value = self.completed(["launchctl", "bootout", self.domain, str(self.plist)])

        self.controller.stop(self.label, self.plist)

        run.assert_called_once_with(
            ["launchctl", "bootout", self.domain, str(self.plist)],
            capture_output=True,
            check=False,
            text=True,
        )

    @patch("local_web_server.services.subprocess.run")
    def test_stop_treats_only_missing_service_as_already_stopped(self, run):
        run.return_value = self.completed(
            ["launchctl", "bootout", self.domain, str(self.plist)],
            returncode=3,
            stderr="Boot-out failed: 3: No such process\n",
        )

        self.controller.stop(self.label, self.plist)

    @patch("local_web_server.services.subprocess.run")
    def test_stop_surfaces_other_launchctl_failure(self, run):
        run.return_value = self.completed(
            ["launchctl", "bootout", self.domain, str(self.plist)],
            returncode=5,
            stderr="Boot-out failed: 5: Input/output error\n",
        )

        with self.assertRaises(subprocess.CalledProcessError):
            self.controller.stop(self.label, self.plist)

    @patch("local_web_server.services._run_launchctl")
    def test_replace_boots_out_then_bootstraps_only_the_supplied_job(self, run):
        run.side_effect = [
            self.completed(
                ["launchctl", "bootout", self.domain, str(self.plist)],
                returncode=3,
                stderr="Boot-out failed: 3: No such process\n",
            ),
            self.completed(["launchctl", "bootstrap", self.domain, str(self.plist)]),
        ]

        self.controller.replace(self.label, self.plist)

        self.assertEqual(
            [record.args[0] for record in run.call_args_list],
            [
                ["launchctl", "bootout", f"gui/{os.getuid()}", str(self.plist)],
                ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(self.plist)],
            ],
        )
        self.assertEqual(
            run.call_args_list,
            [
                call(["launchctl", "bootout", self.domain, str(self.plist)]),
                call(["launchctl", "bootstrap", self.domain, str(self.plist)]),
            ],
        )

    @patch("local_web_server.services._run_launchctl")
    def test_replace_surfaces_bootout_failure_without_bootstrapping(self, run):
        run.return_value = self.completed(
            ["launchctl", "bootout", self.domain, str(self.plist)],
            returncode=5,
            stderr="Boot-out failed: 5: Input/output error\n",
        )

        with self.assertRaises(subprocess.CalledProcessError):
            self.controller.replace(self.label, self.plist)

        self.assertEqual(
            [record.args[0] for record in run.call_args_list],
            [["launchctl", "bootout", self.domain, str(self.plist)]],
        )

    @patch("local_web_server.services._run_launchctl")
    def test_replace_surfaces_bootstrap_failure_after_bootout(self, run):
        run.side_effect = [
            self.completed(["launchctl", "bootout", self.domain, str(self.plist)]),
            self.completed(
                ["launchctl", "bootstrap", self.domain, str(self.plist)],
                returncode=5,
                stderr="Bootstrap failed: 5: Input/output error\n",
            ),
        ]

        with self.assertRaises(subprocess.CalledProcessError):
            self.controller.replace(self.label, self.plist)

        self.assertEqual(
            [record.args[0] for record in run.call_args_list],
            [
                ["launchctl", "bootout", self.domain, str(self.plist)],
                ["launchctl", "bootstrap", self.domain, str(self.plist)],
            ],
        )


class HttpHealthCheckerTests(unittest.TestCase):
    def test_https_health_cannot_succeed_after_redirecting_outside_canonical_origin(self):
        # A downgrade or different authority must fail before the destination
        # can turn the public HTTPS health gate green.
        for location in (
            "http://local.example.ts.net/finished",
            "https://attacker.example/finished",
            "https://local.example.ts.net:444/finished",
            "https://local.example.ts.net:0/finished",
            "https://user@local.example.ts.net/finished",
            "//attacker.example/finished",
        ):
            with self.subTest(location=location), RedirectServer(location) as server:
                result = HttpHealthChecker().check("https://local.example.ts.net/healthz")
                self.assertEqual(result, HealthResult(False, 302, "HTTP 302"))
                self.assertEqual(server.requests, [("HEAD", "/healthz", "local.example.ts.net")])

    def test_same_origin_https_health_redirect_keeps_head_method(self):
        # Losing HEAD on an allowed redirect would fetch a health response body.
        with RedirectServer("/finished") as server:
            result = HttpHealthChecker().check("https://local.example.ts.net/healthz")
        self.assertEqual(result, HealthResult(True, 200, None))
        self.assertEqual(server.requests, [
            ("HEAD", "/healthz", "local.example.ts.net"),
            ("HEAD", "/finished", "local.example.ts.net"),
        ])

    def test_explicit_default_https_port_remains_the_same_origin(self):
        with RedirectServer("https://local.example.ts.net:443/finished"):
            result = HttpHealthChecker().check("https://local.example.ts.net/healthz")
        self.assertEqual(result, HealthResult(True, 200, None))

    def test_version_one_http_health_preserves_existing_redirect_behavior(self):
        with RedirectServer("http://legacy-other.local/finished"):
            result = HttpHealthChecker().check("http://legacy.local/healthz")
        self.assertEqual(result, HealthResult(True, 200, None))

    def test_check_uses_head_without_reading_a_response_body(self):
        HealthHandler.methods = []
        HealthHandler.body = b"body must not be requested"
        self.addCleanup(setattr, HealthHandler, "methods", [])
        self.addCleanup(setattr, HealthHandler, "body", b"ok")

        with HttpServer() as url:
            result = HttpHealthChecker().check(url)

        self.assertTrue(result.healthy)
        self.assertEqual(HealthHandler.methods, ["HEAD"])

    def test_check_accepts_http_200(self):
        with HttpServer() as url:
            result = HttpHealthChecker().check(url)

        self.assertTrue(result.healthy)
        self.assertEqual(result.status, 200)
        self.assertIsNone(result.error)

    def test_check_can_send_public_host_while_connecting_to_loopback(self):
        HealthHandler.required_host = "samplealpha.example.test"
        self.addCleanup(setattr, HealthHandler, "required_host", None)

        with HttpServer() as url:
            result = HttpHealthChecker().check(url, host="samplealpha.example.test")

        self.assertTrue(result.healthy)
        self.assertEqual(result.status, 200)
        self.assertIsNone(result.error)

    def test_check_returns_bounded_connection_error(self):
        result = HttpHealthChecker().check("http://127.0.0.1:1/healthz")

        self.assertFalse(result.healthy)
        self.assertIsNone(result.status)
        self.assertIsNotNone(result.error)
        self.assertLessEqual(len(result.error), 300)

    def test_check_returns_bounded_result_for_malformed_url(self):
        result = HttpHealthChecker().check("http://[::1/healthz")

        self.assertFalse(result.healthy)
        self.assertIsNone(result.status)
        self.assertIsNotNone(result.error)
        self.assertLessEqual(len(result.error), 300)

    def test_check_returns_status_without_response_body_for_http_error(self):
        HealthHandler.status = 500
        HealthHandler.body = b"do not expose this health response body"
        self.addCleanup(setattr, HealthHandler, "status", 200)
        self.addCleanup(setattr, HealthHandler, "body", b"ok")

        with HttpServer() as url:
            result = HttpHealthChecker().check(url)

        self.assertFalse(result.healthy)
        self.assertEqual(result.status, 500)
        self.assertEqual(result.error, "HTTP 500")


class ServiceAndHealthFakesTests(unittest.TestCase):
    def test_fake_service_controller_records_each_interface_call(self):
        controller = FakeServiceController(state=ServiceState.STOPPED)
        label = "com.sean.local-web.samplealpha"
        plist = Path("/tmp/com.sean.local-web.samplealpha.plist")

        self.assertEqual(controller.state(label), ServiceState.STOPPED)
        controller.ensure_running(label, plist)
        controller.restart(label, plist)
        controller.stop(label, plist)
        controller.replace(label, plist)

        self.assertEqual(
            controller.calls,
            [
                ("state", label, None),
                ("ensure_running", label, plist),
                ("restart", label, plist),
                ("stop", label, plist),
                ("replace", label, plist),
            ],
        )

    def test_fake_service_controller_can_queue_readiness_states(self):
        controller = FakeServiceController(
            state=ServiceState.RUNNING,
            states=[ServiceState.STOPPED, ServiceState.MISSING],
        )

        self.assertEqual(controller.state("fixture"), ServiceState.STOPPED)
        self.assertEqual(controller.state("fixture"), ServiceState.MISSING)
        self.assertEqual(controller.state("fixture"), ServiceState.RUNNING)

    def test_fake_health_checker_records_urls_and_returns_configured_results(self):
        first = HealthResult(False, 503, "HTTP 503")
        fallback = HealthResult(True, 200, None)
        health = FakeHealthChecker(result=fallback, results=[first])

        self.assertEqual(health.check("http://example.test/first"), first)
        self.assertEqual(health.check("http://example.test/second"), fallback)
        self.assertEqual(
            health.calls,
            ["http://example.test/first", "http://example.test/second"],
        )


if __name__ == "__main__":
    unittest.main()
