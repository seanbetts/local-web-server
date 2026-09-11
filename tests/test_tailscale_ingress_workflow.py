"""Exercise the rendered ingress contract through two disposable Caddy hops."""

import json
import queue
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from local_web_server.models import (
    AppManifest,
    BuildSpec,
    HostApp,
    HostRegistry,
    ServiceSpec,
)
from local_web_server.render import render_caddyfile


CADDY = Path("/opt/homebrew/bin/caddy")
CURL = Path("/usr/bin/curl")
PUBLIC_HOST = "local.example.ts.net"


@unittest.skipUnless(CADDY.is_file() and CURL.is_file(), "requires Caddy and curl")
class TailscaleIngressWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="tailscale-ingress-test-")
        self.root = Path(temporary.name)
        self.addCleanup(temporary.cleanup)
        self.logs = []

    def _stop_caddy(self, process):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def _start_caddy(self, name, configuration, port):
        state = self.root / name
        environment = {"PATH": "/usr/bin:/bin"}
        for variable, directory in (
            ("HOME", "home"),
            ("XDG_DATA_HOME", "data"),
            ("XDG_CONFIG_HOME", "config"),
        ):
            path = state / directory
            path.mkdir(parents=True)
            environment[variable] = str(path)
        caddyfile = state / "Caddyfile"
        caddyfile.write_text(configuration, encoding="utf-8")
        log_path = state / "output.log"
        self.logs.append(log_path)
        with log_path.open("wb") as output:
            process = subprocess.Popen(
                [str(CADDY), "run", "--config", str(caddyfile), "--adapter", "caddyfile"],
                cwd=state,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        self.addCleanup(self._stop_caddy, process)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            self.assertIsNone(process.poll(), f"{name} Caddy exited during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.02)
        self.fail(f"{name} Caddy did not listen within five seconds")

    def _stop_service(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive(), "disposable service thread did not stop")

    def test_rendered_proxy_preserves_https_host_and_rejects_other_hosts(self):
        self._exercise_rendered_proxy(prepared=False)

    def test_prepared_proxy_serves_both_ports_with_identical_application_headers(self):
        self._exercise_rendered_proxy(prepared=True)

    def _exercise_rendered_proxy(self, *, prepared):
        # Removing loopback trust would downgrade the scheme to http; removing
        # the canonical site matcher would route the attacker Host to the app.
        received = queue.Queue()

        class HeadersService(BaseHTTPRequestHandler):
            def do_GET(self):
                headers = {
                    "Host": self.headers.get("Host", "")[:256],
                    "X-Forwarded-Proto": self.headers.get("X-Forwarded-Proto", "")[:256],
                }
                received.put(headers)
                payload = json.dumps(headers).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), HeadersService)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(self._stop_service, server, thread)
        app_port = server.server_address[1]
        with socket.socket() as inner_reservation, socket.socket() as tls_reservation, socket.socket() as legacy_reservation:
            inner_reservation.bind(("127.0.0.1", 0))
            tls_reservation.bind(("127.0.0.1", 0))
            legacy_reservation.bind(("127.0.0.1", 0))
            inner_port = inner_reservation.getsockname()[1]
            https_port = tls_reservation.getsockname()[1]
            legacy_port = legacy_reservation.getsockname()[1]

        app = HostApp("echo", self.root / "repository", False, None, (), app_port, None)
        registry = HostRegistry(
            PUBLIC_HOST,
            self.root / "runtime",
            (app,),
            schema_version=2,
            public_origin="https://local.example.ts.net",
            ingress_mode="tailscale-serve",
        )
        manifest = AppManifest(
            1, "echo", "Disposable headers service", "/echo", "service",
            BuildSpec((), Path("public"), ()), "/echo/healthz", ServiceSpec("echo", "/healthz"),
        )
        index = registry.runtime_root / "platform" / "index" / "registry-v1.json"
        index.parent.mkdir(parents=True)
        index.write_bytes(b'{"apps":[]}\n')
        rendered = render_caddyfile(
            registry, {"echo": manifest}, prepare_tailscale_port_migration=prepared,
        )
        site_marker = "http://local.example.ts.net:8080 {"
        self.assertEqual(rendered.count(site_marker), 1)
        self.assertTrue(rendered.startswith("{\n"))
        inner_config = rendered.replace(site_marker, f"http://{PUBLIC_HOST}:{inner_port} {{", 1)
        inner_config = inner_config.replace("http://:8080 {", f"http://:{inner_port} {{")
        # Remap the legacy site too: disposable tests must never bind host port 80.
        inner_config = inner_config.replace("\n:80 {\n", f"\n:{legacy_port} {{\n    bind 127.0.0.1\n")
        inner_config = inner_config.replace("{\n", "{\n    admin off\n    skip_install_trust\n", 1)
        tls_config = (
            "{\n"
            "    admin off\n"
            "    skip_install_trust\n"
            "    auto_https disable_redirects\n"
            "}\n"
            f"https://{PUBLIC_HOST}:{https_port} {{\n"
            "    bind 127.0.0.1\n"
            "    tls internal\n"
            f"    reverse_proxy http://127.0.0.1:{inner_port} {{\n"
            "        header_up Host {http.request.header.X-Test-Forwarded-Host}\n"
            "        header_up X-Forwarded-Proto https\n"
            "    }\n"
            "}\n"
        )
        try:
            self._start_caddy("inner", inner_config, inner_port)
            self._start_caddy("tls", tls_config, https_port)
            command = [
                str(CURL), "--disable", "--silent", "--show-error", "--insecure",
                "--noproxy", "*", "--max-time", "2", "--max-filesize", "4096",
                "--fail", "--resolve", f"{PUBLIC_HOST}:{https_port}:127.0.0.1",
                "--header", "X-Test-Forwarded-Host: local.example.ts.net",
                f"https://{PUBLIC_HOST}:{https_port}/echo/headers",
            ]
            deadline = time.monotonic() + 5
            while True:
                response = subprocess.run(command, capture_output=True, text=True, timeout=3)
                # The listener can open before the disposable internal certificate
                # is ready. Retry only connection/TLS startup failures, not HTTP.
                if response.returncode not in (7, 35) or time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
            self.assertEqual(response.returncode, 0, response.stderr)
            expected = {"Host": "local.example.ts.net", "X-Forwarded-Proto": "https"}
            self.assertEqual(json.loads(response.stdout), expected)
            self.assertEqual(received.get(timeout=1), expected)

            if prepared:
                connection = HTTPConnection("127.0.0.1", legacy_port, timeout=2)
                try:
                    connection.request("GET", "/echo/headers", headers={
                        "Host": PUBLIC_HOST, "X-Forwarded-Proto": "https",
                    })
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read(4096)), expected)
                    self.assertEqual(received.get(timeout=1), expected)
                    connection.request("HEAD", "/_local-web/platform/index/registry-v1.json", headers={"Host": PUBLIC_HOST})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.getheader("Cache-Control"), "no-cache")
                    response.read()
                finally:
                    connection.close()

            # The same TLS ingress must reject a wrong forwarded Host on the
            # exact body-free platform route used by the installer.
            for forwarded_host, expected_status in (
                ("local.example.ts.net", "200"), ("attacker.example", "421"),
            ):
                head_command = [part for part in command[:-1] if part != "--fail"]
                head_command[head_command.index("X-Test-Forwarded-Host: local.example.ts.net")] = (
                    f"X-Test-Forwarded-Host: {forwarded_host}"
                )
                head_command.extend([
                    "--head", "--write-out", "\n%{http_code}",
                    f"https://{PUBLIC_HOST}:{https_port}/_local-web/platform/index/registry-v1.json",
                ])
                head = subprocess.run(head_command, capture_output=True, text=True, timeout=3)
                self.assertEqual(head.returncode, 0, head.stderr)
                self.assertEqual(head.stdout.splitlines()[-1], expected_status)

            connection = HTTPConnection("127.0.0.1", inner_port, timeout=2)
            try:
                connection.request("GET", "/echo/headers", headers={"Host": "attacker.example"})
                rejected = connection.getresponse()
                body = rejected.read(4096)
            finally:
                connection.close()
            # An empty 200 for an unmatched Host would falsely satisfy the
            # installer's body-free ingress health probe.
            self.assertEqual(rejected.status, 421)
            self.assertEqual(body, b"")
            self.assertTrue(received.empty(), "the noncanonical Host reached the application")
        except Exception as error:
            diagnostics = []
            for log_path in self.logs:
                with log_path.open("rb") as output:
                    output.seek(0, 2)
                    output.seek(max(0, output.tell() - 8192))
                    diagnostics.append(f"{log_path.parent.name}:\n{output.read().decode('utf-8', errors='replace')}")
            raise AssertionError(f"{error}\n" + "\n".join(diagnostics)) from error


if __name__ == "__main__":
    unittest.main()
