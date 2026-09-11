"""Real HTTP responses with only outbound TLS/socket connections substituted."""

import http.client
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from tests.ingress_fixture import ingress_transport


class RedirectServer:
    def __init__(self, location, *, body=b"healthy", response_status=None):
        self.location = location
        self.body = body
        self.requests = []
        self.response_status = response_status

    def __enter__(self):
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.respond(False)

            def do_GET(self):
                self.respond(True)

            def respond(self, include_body):
                fixture.requests.append((self.command, self.path, self.headers.get("Host")))
                redirect = fixture.location is not None and self.path != "/finished"
                status = (
                    fixture.response_status(len(fixture.requests))
                    if fixture.response_status is not None
                    else (302 if redirect else 200)
                )
                self.send_response(status)
                if redirect:
                    self.send_header("Location", fixture.location)
                self.send_header("Content-Length", str(len(fixture.body)))
                self.end_headers()
                if include_body:
                    self.wfile.write(fixture.body)

            def log_message(self, format, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.stack = ExitStack()
        self.stack.callback(server.server_close)
        self.stack.callback(thread.join, 2)
        self.stack.callback(server.shutdown)
        thread.start()
        connection = http.client.HTTPConnection

        class SocketConnection(connection):
            def __init__(self, host, port=None, *, timeout, **kwargs):
                super().__init__("127.0.0.1", server.server_port, timeout=timeout)

        # Keep URL parsing, HTTPSHandler, HTTPRedirectHandler and response
        # processing real. Only transport to the disposable socket is mapped.
        self.stack.enter_context(patch("urllib.request.getproxies", return_value={}))
        self.stack.enter_context(patch("urllib.request._opener", None))
        self.stack.enter_context(patch("http.client.HTTPConnection", SocketConnection))
        self.stack.enter_context(patch("http.client.HTTPSConnection", SocketConnection))
        self.stack.enter_context(ingress_transport(server.server_port))
        return self

    def __exit__(self, *exc):
        self.stack.close()
