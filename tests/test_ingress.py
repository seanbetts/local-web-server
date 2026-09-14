import http.client
import os
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from local_web_server.ingress import IngressVerificationError, TailscaleServeIngressVerifier
from local_web_server._ingress_probe import request_status
from local_web_server.models import HostRegistry
from tests.ingress_fixture import HTTPSPeer, TLSIdentity, ingress_transport, ingress_worker


@contextmanager
def slow_header_server():
    """A real peer that stays below the socket timeout with partial headers."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(1)
    stopped = threading.Event()
    requests = []

    def serve():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(1)
                request = b""
                while b"\r\n\r\n" not in request:
                    received = connection.recv(4096)
                    if not received:
                        return
                    request += received
                requests.append(request)
                connection.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                for _ in range(6):
                    if stopped.wait(0.08):
                        return
                    connection.sendall(b"fragment ")
                connection.sendall(b"\r\nContent-Length: 0\r\n\r\n")
        except OSError:
            pass

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield listener.getsockname()[1], requests
    finally:
        stopped.set()
        listener.close()
        thread.join(2)
        if thread.is_alive():
            raise AssertionError("disposable header server did not stop")


class FakeHTTPSConnection:
    def __init__(self, status=200, *, failure=None, failure_stage="response"):
        self.status = status
        self.failure = failure
        self.failure_stage = failure_stage
        self.requests = []
        self.closed = False

    def request(self, method, path, *, headers):
        self.requests.append((method, path, headers))
        if self.failure_stage == "request" and self.failure:
            raise self.failure

    def getresponse(self):
        if self.failure_stage == "response" and self.failure:
            raise self.failure
        return self

    def read(self, *args):
        raise AssertionError("ingress probes must never read the response body")

    def close(self):
        self.closed = True
        if self.failure_stage == "close" and self.failure:
            raise self.failure


class IngressTests(unittest.TestCase):
    def setUp(self):
        self.registry = HostRegistry(
            "local.example.ts.net", Path("/unused"), (), 2,
            "https://local.example.ts.net", "tailscale-serve",
        )
        self.connection = FakeHTTPSConnection()
        self.connections = []
        self.commands = []
        self.output = "p4321\nf8\nn127.0.0.1:8080\nf9\nn127.0.0.1:2019\n"
        self.returncode = 0
        self.runner_error = None

    def connect(self, host, port, *, timeout, context):
        self.connections.append((host, port, timeout, context))
        return self.connection

    def run_command(self, argv, *, timeout):
        self.commands.append((list(argv), timeout))
        if self.runner_error:
            raise self.runner_error
        return subprocess.CompletedProcess(argv, self.returncode, self.output, "private stderr")

    def verifier(self, *, connection_factory=None, **kwargs):
        def probe(host, deadline, monotonic):
            # Unit tests exercise the worker's request semantics in this
            # process; elapsed deadline tests below run the real child.
            with patch("http.client.HTTPSConnection", connection_factory or self.connect):
                try:
                    return request_status(host, deadline - monotonic())
                except http.client.HTTPException:
                    raise OSError from None

        worker = patch("local_web_server.ingress._https_status", side_effect=probe)
        worker.start()
        self.addCleanup(worker.stop)
        return TailscaleServeIngressVerifier(run=self.run_command, **kwargs)

    def assert_sanitized(self, error, message):
        self.assertEqual(str(error), message)
        for sensitive in (self.registry.public_origin, self.registry.host, self.output, "private"):
            self.assertNotIn(sensitive, str(error))
            self.assertNotIn(sensitive, repr(error))

    def test_version_one_is_a_noop(self):
        registry = HostRegistry("mac.local", Path("/unused"), ())
        verifier = self.verifier()
        verifier.preflight(registry)
        verifier.verify_loaded(registry, 4321)
        self.assertEqual(self.connections, [])
        self.assertEqual(self.commands, [])

    def test_prepared_listener_is_allowed_only_in_explicit_preparation(self):
        self.output = "n*:80\nn127.0.0.1:8080\nn127.0.0.1:2019\n"
        self.verifier().verify_prepared(self.registry, 4321)
        with self.assertRaises(IngressVerificationError):
            self.verifier().verify_loaded(self.registry, 4321)

    def test_prepared_proof_rejects_incomplete_or_extra_listeners(self):
        # These test endpoint strings, not socket families: macOS reports the
        # permitted legacy wildcard as *:80 even when it is a dual-stack socket.
        for output in (
            "n*:80\n", "n127.0.0.1:8080\n", "n[::]:80\nn127.0.0.1:8080\n",
            "n*:80\nn127.0.0.1:8080\nn*:443\n",
            "n*:80\nn127.0.0.1:8080\nn127.0.0.1:8080\n",
            "n*:80\nn127.0.0.1:8080\nn192.168.1.2:2019\n",
            "n*:80\nn127.0.0.1:8080\nn127.0.0.1:80\n",
            "n*:80\nn127.0.0.1:8080\nn127.0.0.1:9999\n",
        ):
            with self.subTest(output=output):
                self.output = output
                with self.assertRaises(IngressVerificationError):
                    self.verifier().verify_prepared(self.registry, 4321)

    def test_preparation_preflight_requires_a_successful_https_response(self):
        self.connection = FakeHTTPSConnection(502)
        with self.assertRaises(IngressVerificationError):
            self.verifier().preflight(self.registry, require_success=True)

    def test_preflight_uses_one_bounded_validated_head_and_accepts_any_http_status(self):
        for status in (100, 199, 200, 299, 301, 404, 500, 599):
            with self.subTest(status=status):
                self.connection = FakeHTTPSConnection(status)
                self.verifier().preflight(self.registry)
                self.assertEqual(self.connection.requests, [
                    ("HEAD", "/_local-web/platform/index/registry-v1.json", {"Host": "local.example.ts.net"}),
                ])
                self.assertTrue(self.connection.closed)
                host, port, timeout, context = self.connections[-1]
                self.assertEqual((host, port), ("local.example.ts.net", 443))
                self.assertGreater(timeout, 0)
                self.assertLessEqual(timeout, 5)
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(len(self.connections), 8)
        self.assertEqual(self.commands, [])

    def test_default_connection_factory_keeps_certificate_validation(self):
        with patch("http.client.HTTPSConnection", self.connect):
            request_status(self.registry.host, 5)
        self.assertEqual(len(self.connections), 1)
        self.assertTrue(self.connections[0][3].check_hostname)
        self.assertEqual(self.connections[0][3].verify_mode, ssl.CERT_REQUIRED)

    def test_preflight_rejects_non_http_status(self):
        for status in (99, 600):
            with self.subTest(status=status):
                self.connection = FakeHTTPSConnection(status)
                with self.assertRaises(IngressVerificationError):
                    self.verifier().preflight(self.registry)
                self.assertTrue(self.connection.closed)

    def test_https_errors_are_sanitized_and_connections_closed(self):
        errors = (ssl.SSLCertVerificationError("private certificate"),
                  socket.gaierror("private DNS"), TimeoutError("private timeout"),
                  ConnectionError("private connection"), http.client.HTTPException("private HTTP"))
        for stage in ("request", "response", "close"):
            for error in errors:
                with self.subTest(stage=stage, error=type(error).__name__):
                    self.connection = FakeHTTPSConnection(failure=error, failure_stage=stage)
                    with self.assertRaises(IngressVerificationError) as caught:
                        self.verifier().preflight(self.registry)
                    self.assert_sanitized(caught.exception, "Tailscale HTTPS ingress is unavailable")
                    self.assertTrue(self.connection.closed)

    def test_connection_creation_errors_are_sanitized(self):
        def fail(*args, **kwargs):
            raise OSError("private provider error")
        with self.assertRaises(IngressVerificationError) as caught:
            self.verifier(connection_factory=fail).preflight(self.registry)
        self.assert_sanitized(caught.exception, "Tailscale HTTPS ingress is unavailable")

    def test_loaded_requires_success_without_following_redirects(self):
        for status in (200, 204, 299, 100, 199, 301, 404, 500):
            with self.subTest(status=status):
                self.connection = FakeHTTPSConnection(status)
                if 200 <= status <= 299:
                    self.verifier().verify_loaded(self.registry, 4321)
                else:
                    with self.assertRaises(IngressVerificationError):
                        self.verifier().verify_loaded(self.registry, 4321)
                self.assertEqual(len(self.connection.requests), 1)
                self.assertTrue(self.connection.closed)

    def test_listener_command_is_pid_scoped_and_accepts_admin_listener(self):
        self.verifier().verify_loaded(self.registry, 4321)
        self.assertEqual(self.commands, [([
            "/usr/sbin/lsof", "-nP", "-a", "-p", "4321", "-iTCP", "-sTCP:LISTEN", "-F", "n",
        ], 5)])

    def test_readiness_budget_counts_https_time_before_listener_inspection(self):
        now = [0.0]

        def connect(*args, **kwargs):
            now[0] += 0.75
            return self.connect(*args, **kwargs)

        verifier = self.verifier(connection_factory=connect, monotonic=lambda: now[0])
        verifier.verify_loaded(self.registry, 4321, timeout=2)
        self.assertEqual(self.connections[0][2], 2)
        self.assertEqual(self.commands[0][1], 1.25)

    def test_absolute_deadline_interrupts_real_slow_headers(self):
        # Per-recv inactivity timeouts allow a trickling peer to keep this
        # actual request alive well beyond the complete readiness budget.
        with slow_header_server() as (port, requests):
            with ingress_transport(port):
                verifier = TailscaleServeIngressVerifier(run=self.run_command)
                started = time.monotonic()
                with self.assertRaises(IngressVerificationError):
                    verifier.verify_loaded(self.registry, 4321, timeout=0.15)
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.35, f"absolute 0.15s deadline took {elapsed:.3f}s")
            self.assertEqual(len(requests), 1)
            self.assertTrue(requests[0].startswith(b"HEAD /_local-web/platform/index/registry-v1.json HTTP/1.1\r\n"))
            self.assertEqual(self.commands, [])

    def test_expired_readiness_cannot_accept_late_https_or_listener_success(self):
        for stage in ("https", "listener"):
            with self.subTest(stage=stage):
                now = [0.0]
                self.commands.clear()

                def connect(*args, **kwargs):
                    if stage == "https":
                        now[0] = 2.0
                    return self.connect(*args, **kwargs)

                def run(argv, **kwargs):
                    now[0] = 2.0
                    return self.run_command(argv, **kwargs)

                verifier = self.verifier(connection_factory=connect, monotonic=lambda: now[0])
                verifier.run = run
                with self.assertRaises(IngressVerificationError):
                    verifier.verify_loaded(self.registry, 4321, timeout=2)
                if stage == "https":
                    self.assertEqual(self.commands, [])

    def test_exposed_non_http_listener_cannot_pass_isolation(self):
        # Ignoring non-80 endpoints would leave Caddy admin or TLS exposed.
        for endpoint in (
            "*:2019", "*:443", "192.168.1.2:2019", "100.100.1.2:443",
            "[::]:2019", "[2001:db8::1]:443", "[fd7a:115c:a1e0::1]:2019",
        ):
            with self.subTest(endpoint=endpoint):
                self.output = f"n127.0.0.1:8080\nn{endpoint}\n"
                with self.assertRaises(IngressVerificationError) as caught:
                    self.verifier().verify_loaded(self.registry, 4321)
                self.assert_sanitized(caught.exception, "Caddy listener isolation could not be verified")

    def test_loopback_ipv4_and_ipv6_admin_listeners_remain_allowed(self):
        # Rejecting ::1 admin sockets would block the unchanged Caddy admin API.
        self.output = "n127.0.0.1:8080\nn127.0.0.1:2019\nn[::1]:2019\n"
        self.verifier().verify_loaded(self.registry, 4321)

    def test_listener_proof_rejects_wrong_duplicate_absent_or_malformed_listener(self):
        outputs = (
            "n*:80\n", "n192.168.1.2:80\n", "n100.100.1.2:80\n",
            "n[::]:80\n", "n[::1]:80\n", "n127.0.0.1:2019\n", "",
            "n127.0.0.1:8080\nn127.0.0.1:8080\n",
            "n127.0.0.1:8080\nn*:80\n", "n127.0.0.1:8080\nnmalformed\n",
            "n127.0.0.1:8080\nn127.0.0.1:80 \n", "n127.0.0.1:8080\nn\n",
        )
        for output in outputs:
            with self.subTest(output=output):
                self.output = output
                with self.assertRaises(IngressVerificationError) as caught:
                    self.verifier().verify_loaded(self.registry, 4321)
                # Empty output is not a meaningful privacy marker.
                self.output = output or "private empty output"
                self.assert_sanitized(caught.exception, "Caddy listener isolation could not be verified")

    def test_listener_command_failure_is_sanitized(self):
        for error in (None, OSError("private spawn"), subprocess.TimeoutExpired("private lsof", 5)):
            with self.subTest(error=type(error).__name__):
                self.returncode = 1
                self.runner_error = error
                with self.assertRaises(IngressVerificationError) as caught:
                    self.verifier().verify_loaded(self.registry, 4321)
                self.assert_sanitized(caught.exception, "Caddy listener isolation could not be verified")


class IngressDeadlineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.identity = TLSIdentity()
        cls.addClassCleanup(cls.identity.close)

    def setUp(self):
        self.registry = HostRegistry(
            "local.example.ts.net", Path("/unused"), (), 2,
            "https://local.example.ts.net", "tailscale-serve",
        )
        self.listener_timeouts = []
        self.verifier = TailscaleServeIngressVerifier(run=self.listener)

    def listener(self, argv, *, timeout):
        self.listener_timeouts.append(timeout)
        return subprocess.CompletedProcess(argv, 0, "n127.0.0.1:8080\n", "")

    def assert_reaped(self, processes):
        self.assertTrue(processes, "the production worker must have started")
        for process in processes:
            self.assertIsNotNone(process.returncode)
            self.assertTrue(process.stdout.closed)
            with self.assertRaises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)

    def test_real_tls_success_is_head_only_and_closes_without_reading_body(self):
        with HTTPSPeer(self.identity) as peer, ingress_transport(peer.port, tls=True) as processes:
            with patch.dict(os.environ, {"SSL_CERT_FILE": str(self.identity.certificate)}):
                started = time.monotonic()
                self.verifier.verify_loaded(self.registry, 4321, timeout=0.8)
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.5)
            self.assertEqual(len(peer.requests), 1)
            self.assertTrue(peer.requests[0].startswith(b"HEAD /_local-web/platform/index/registry-v1.json HTTP/1.1\r\n"))
            self.assertIn(b"\r\nHost: local.example.ts.net\r\n", peer.requests[0])
            self.assertTrue(peer.closed.wait(0.2), "HEAD must close without body data")
            self.assertEqual(len(self.listener_timeouts), 1)
            self.assertGreater(self.listener_timeouts[0], 0)
            self.assertLess(self.listener_timeouts[0], 0.8)
            self.assert_reaped(processes)

    def test_real_tls_rejects_untrusted_certificate_and_wrong_hostname(self):
        for failure in ("trust", "hostname"):
            with self.subTest(failure=failure):
                registry = self.registry
                if failure == "hostname":
                    registry = replace(registry, host="attacker.example", public_origin="https://attacker.example")
                certificate = self.identity.certificate if failure == "hostname" else self.identity.key
                with HTTPSPeer(self.identity) as peer, ingress_transport(peer.port, tls=True) as processes:
                    with patch.dict(os.environ, {"SSL_CERT_FILE": str(certificate)}):
                        with self.assertRaisesRegex(IngressVerificationError, "^Tailscale HTTPS ingress is unavailable$") as caught:
                            self.verifier.verify_loaded(registry, 4321, timeout=0.8)
                    self.assertIsNotNone(peer.connection, "certificate rejection must reach the TLS peer")
                    self.assertEqual(peer.requests, [])
                    self.assertEqual(self.listener_timeouts, [])
                    self.assertNotIn(registry.host, repr(caught.exception))
                    self.assertNotIn("certificate", repr(caught.exception))
                    self.assertIsNone(caught.exception.__cause__)
                    self.assert_reaped(processes)

    def test_real_tls_preflight_accepts_redirect_loaded_rejects_without_following(self):
        for preflight in (True, False):
            with self.subTest(preflight=preflight):
                with HTTPSPeer(self.identity, status=302) as peer, ingress_transport(peer.port, tls=True) as processes:
                    with patch.dict(os.environ, {"SSL_CERT_FILE": str(self.identity.certificate)}):
                        if preflight:
                            self.verifier.preflight(self.registry)
                        else:
                            with self.assertRaises(IngressVerificationError):
                                self.verifier.verify_loaded(self.registry, 4321, timeout=0.8)
                    self.assertEqual(len(peer.requests), 1)
                    self.assertEqual(self.listener_timeouts, [])
                    self.assert_reaped(processes)

    def test_real_tls_slow_headers_and_stalled_handshake_are_cancelled(self):
        for stage in ("headers", "handshake"):
            with self.subTest(stage=stage):
                with HTTPSPeer(self.identity, slow_headers=stage == "headers", handshake=stage == "handshake") as peer:
                    with ingress_transport(peer.port, tls=True) as processes:
                        with patch.dict(os.environ, {"SSL_CERT_FILE": str(self.identity.certificate)}):
                            started = time.monotonic()
                            with self.assertRaises(IngressVerificationError):
                                self.verifier.verify_loaded(self.registry, 4321, timeout=0.15)
                            elapsed = time.monotonic() - started
                        self.assertLess(elapsed, 0.35, f"{stage}: absolute 0.15s deadline took {elapsed:.3f}s")
                        self.assertTrue(peer.connected.is_set(), "real TLS boundary must be reached")
                        if stage == "headers":
                            self.assertEqual(len(peer.requests), 1)
                        self.assertTrue(peer.closed.wait(1), "deadline must close the peer socket")
                        self.assertEqual(processes[0].returncode, -signal.SIGKILL)
                        self.assertEqual(self.listener_timeouts, [])
                        self.assert_reaped(processes)

    def test_blocked_dns_connect_and_write_are_cancelled_without_lingering_workers(self):
        # A separate child owns even boundaries that ignore socket timeouts.
        # The marker proves execution reached the actual boundary before kill.
        boundaries = {
            "DNS": "socket.getaddrinfo = blocked",
            "connect": "socket.getaddrinfo = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 9))]\nsocket.socket.connect = blocked",
            "write": "http.client.HTTPConnection.send = blocked",
        }
        with tempfile.TemporaryDirectory() as directory:
            for stage, boundary in boundaries.items():
                with self.subTest(stage=stage):
                    marker = Path(directory) / stage
                    bootstrap = f"""
import http.client
import socket
import time
from pathlib import Path
def blocked(*args, **kwargs):
    Path({str(marker)!r}).touch()
    time.sleep(10)
{boundary}
"""
                    with ingress_worker(bootstrap) as processes:
                        started = time.monotonic()
                        with self.assertRaises(IngressVerificationError):
                            self.verifier.verify_loaded(self.registry, 4321, timeout=0.15)
                        elapsed = time.monotonic() - started
                        self.assertTrue(marker.is_file(), f"{stage} boundary was not reached")
                        self.assertLess(elapsed, 0.35, f"{stage}: absolute 0.15s deadline took {elapsed:.3f}s")
                        self.assertEqual(processes[0].returncode, -signal.SIGKILL)
                        self.assert_reaped(processes)
        self.assertEqual(self.listener_timeouts, [])

    def test_repeated_expiry_from_non_main_thread_leaks_no_processes_fds_or_threads(self):
        baseline_threads = set(threading.enumerate())
        baseline_fds = len(os.listdir("/dev/fd"))
        with ingress_worker("import time; time.sleep(10)") as processes:
            with ThreadPoolExecutor(max_workers=1) as caller:
                for _ in range(6):
                    future = caller.submit(self.verifier.verify_loaded, self.registry, 4321, timeout=0.05)
                    with self.assertRaises(IngressVerificationError):
                        future.result(timeout=0.35)
            self.assertEqual(len(processes), 6)
            self.assert_reaped(processes)
        self.assertEqual(set(threading.enumerate()), baseline_threads)
        self.assertEqual(len(os.listdir("/dev/fd")), baseline_fds)

    def test_caller_interruption_kills_and_reaps_the_worker(self):
        process_type = subprocess.Popen
        with ingress_worker("import time; time.sleep(10)") as processes:
            with patch.object(process_type, "communicate", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.verifier.verify_loaded(self.registry, 4321, timeout=0.8)
            self.assertEqual(processes[0].returncode, -signal.SIGKILL)
            self.assert_reaped(processes)

    def test_interruption_during_reaping_still_closes_the_parent_pipe(self):
        process_type = subprocess.Popen
        wait = process_type.wait

        def interrupted_wait(process, *args, **kwargs):
            # Popen.wait can finish reaping and then re-raise KeyboardInterrupt.
            wait(process, *args, **kwargs)
            raise KeyboardInterrupt

        with ingress_worker("import time; time.sleep(10)") as processes:
            with patch.object(process_type, "wait", interrupted_wait):
                with self.assertRaises(KeyboardInterrupt):
                    self.verifier.verify_loaded(self.registry, 4321, timeout=0.05)
            try:
                self.assert_reaped(processes)
            finally:
                for process in processes:
                    process.stdout.close()

    def test_child_startup_consumes_the_existing_absolute_deadline(self):
        with ingress_worker("import time; time.sleep(10)") as processes:
            spawn = subprocess.Popen

            def delayed_start(*args, **kwargs):
                process = spawn(*args, **kwargs)
                time.sleep(0.25)
                return process

            with patch("local_web_server.ingress.subprocess.Popen", side_effect=delayed_start):
                started = time.monotonic()
                with self.assertRaises(IngressVerificationError):
                    self.verifier.verify_loaded(self.registry, 4321, timeout=0.3)
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.42, f"startup reset the 0.3s deadline: {elapsed:.3f}s")
            self.assert_reaped(processes)

    def test_child_failure_discards_diagnostics_and_reaps(self):
        with ingress_worker("raise RuntimeError('private provider diagnostic')") as processes:
            with self.assertRaisesRegex(IngressVerificationError, "^Tailscale HTTPS ingress is unavailable$") as caught:
                self.verifier.verify_loaded(self.registry, 4321, timeout=0.8)
            self.assertIsNone(caught.exception.__cause__)
            self.assertNotIn("private", repr(caught.exception))
            self.assertEqual(self.listener_timeouts, [])
            self.assert_reaped(processes)
