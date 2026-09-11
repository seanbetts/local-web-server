"""Map only the isolated ingress worker's outbound socket to a test server."""

import subprocess
import socket
import ssl
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


class TLSIdentity:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.certificate = Path(self.directory.name) / "certificate.pem"
        self.key = Path(self.directory.name) / "key.pem"
        subprocess.run([
            "/usr/bin/openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "1", "-subj", "/CN=local.example.ts.net",
            "-addext", "subjectAltName=DNS:local.example.ts.net",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign,digitalSignature,keyEncipherment",
            "-keyout", str(self.key), "-out", str(self.certificate),
        ], check=True, capture_output=True, timeout=5)

    def close(self):
        self.directory.cleanup()


class HTTPSPeer:
    """One real TLS request; keep its advertised body unavailable until EOF."""

    def __init__(self, identity, *, status=200, slow_headers=False, handshake=False):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(identity.certificate, identity.key)
        self.status = status
        self.slow_headers = slow_headers
        self.handshake = handshake
        self.requests = []
        self.stopped = threading.Event()
        self.closed = threading.Event()
        self.connected = threading.Event()
        self.connection = None

    def __enter__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(1)
        self.listener.settimeout(1)
        self.thread = threading.Thread(target=self.serve)
        self.thread.start()
        return self

    def serve(self):
        try:
            connection, _ = self.listener.accept()
            self.connection = connection
            with connection:
                connection.settimeout(1)
                if self.handshake:
                    # Receive ClientHello but never supply a ServerHello.
                    while connection.recv(4096):
                        self.connected.set()
                    self.closed.set()
                    return
                with self.context.wrap_socket(connection, server_side=True) as secured:
                    self.connection = secured
                    self.connected.set()
                    request = b""
                    while b"\r\n\r\n" not in request:
                        received = secured.recv(4096)
                        if not received:
                            return
                        request += received
                    self.requests.append(request)
                    secured.sendall(f"HTTP/1.1 {self.status} Result\r\n".encode())
                    if self.slow_headers:
                        secured.sendall(b"X-Slow: ")
                        for _ in range(6):
                            if self.stopped.wait(0.08):
                                return
                            secured.sendall(b"fragment ")
                        secured.sendall(b"\r\n")
                    secured.sendall(b"Location: https://private.invalid/finished\r\nContent-Length: 100000000\r\n\r\n")
                    # HEAD must finish and disconnect without waiting for body
                    # bytes. A redirect must not initiate another request.
                    while secured.recv(4096):
                        pass
                    self.closed.set()
        except OSError:
            self.closed.set()

    def __exit__(self, *exc):
        self.stopped.set()
        self.listener.close()
        if self.connection is not None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
        self.thread.join(2)
        if self.thread.is_alive():
            raise AssertionError("disposable HTTPS peer did not stop")


@contextmanager
def ingress_worker(bootstrap):
    """Run the real worker with a controlled external boundary in its child."""
    popen = subprocess.Popen
    processes = []
    bootstrap += """
import runpy
import sys
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""

    def spawn(argv, *args, **kwargs):
        command = list(argv)
        worker = len(command) == 6 and Path(command[3]).name == "_ingress_probe.py"
        if worker:
            command = [*command[:3], "-c", bootstrap, *command[3:]]
        process = popen(command, *args, **kwargs)
        if worker:
            processes.append(process)
        return process

    with patch("local_web_server.ingress.subprocess.Popen", side_effect=spawn):
        yield processes


@contextmanager
def ingress_transport(port, *, host_header=None, tls=False):
    if tls:
        bootstrap = f"""
import socket
getaddrinfo = socket.getaddrinfo
def mapped_address(host, port, *args, **kwargs):
    return getaddrinfo("127.0.0.1", {port!r}, *args, **kwargs)
socket.getaddrinfo = mapped_address
"""
    else:
        bootstrap = f"""
import http.client

class SocketConnection(http.client.HTTPConnection):
    def __init__(self, host, port=None, *, timeout, **kwargs):
        super().__init__("127.0.0.1", {port!r}, timeout=timeout)
    def request(self, method, path, *, headers):
        if {host_header!r} is not None:
            headers = {{"Host": {host_header!r}}}
        super().request(method, path, headers=headers)

http.client.HTTPSConnection = SocketConnection
"""
    with ingress_worker(bootstrap) as processes:
        yield processes
