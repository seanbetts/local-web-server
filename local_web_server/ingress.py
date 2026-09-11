"""Bounded, body-free checks of the externally managed Tailscale ingress."""

import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from .models import HostRegistry
from .public_origin import TAILSCALE_CADDY_PORT, TAILSCALE_SERVE

if TYPE_CHECKING:
    from .install import CommandRunner


class IngressVerificationError(RuntimeError):
    """Ingress could not be verified without exposing provider diagnostics."""


def _https_status(host: str, deadline: float, monotonic: Callable[[], float]) -> int:
    """Cancel the entire exchange, including libc DNS, at one absolute deadline.

    The isolated stdlib worker creates no child processes and writes only its
    three-digit status. Killing and reaping it closes every socket and pipe;
    any resolver threads belong to that process, not this caller. Process
    startup consumes the same budget; only OS termination/reaping can follow it.
    """
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", str(Path(__file__).with_name("_ingress_probe.py")),
         host, str(remaining)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        output, _ = process.communicate(timeout=remaining)
        if monotonic() >= deadline:
            raise TimeoutError
        if process.returncode != 0 or re.fullmatch(rb"[0-9]{3}", output) is None:
            raise ValueError
        return int(output)
    finally:
        try:
            if process.poll() is None:
                process.kill()
            # The worker cannot pass a pipe to a descendant, so SIGKILL also
            # ends DNS/header work and permits reaping without a pipe wait.
            process.wait()
        finally:
            process.stdout.close()


class TailscaleServeIngressVerifier:
    def __init__(
        self,
        *,
        run: "CommandRunner",
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.run = run
        self.monotonic = monotonic

    def preflight(self, registry: HostRegistry, *, require_success: bool = False) -> None:
        if registry.ingress_mode == TAILSCALE_SERVE:
            self._probe(registry, require_success=require_success)

    def verify_loaded(
        self, registry: HostRegistry, caddy_pid: int, *, timeout: float | None = None,
    ) -> None:
        self._verify_process(registry, caddy_pid, timeout=timeout, prepared=False)

    def verify_prepared(
        self, registry: HostRegistry, caddy_pid: int, *, timeout: float | None = None,
    ) -> None:
        """Verify the explicitly requested, temporary dual-listener handover."""
        self._verify_process(registry, caddy_pid, timeout=timeout, prepared=True)

    def _verify_process(
        self, registry: HostRegistry, caddy_pid: int, *, timeout: float | None,
        prepared: bool,
    ) -> None:
        if registry.ingress_mode == TAILSCALE_SERVE:
            deadline = None if timeout is None else self.monotonic() + timeout
            self._probe(registry, require_success=True, deadline=deadline)
            self._verify_listener(
                caddy_pid, timeout=self._remaining_timeout(deadline), prepared=prepared,
            )
            self._remaining_timeout(deadline)

    def _remaining_timeout(self, deadline: float | None) -> float:
        remaining = 5.0 if deadline is None else min(5.0, deadline - self.monotonic())
        if remaining <= 0:
            raise IngressVerificationError("Caddy ingress readiness timed out")
        return remaining

    def _probe(
        self, registry: HostRegistry, *, require_success: bool, deadline: float | None = None,
    ) -> None:
        request_deadline = self.monotonic() + 5.0
        if deadline is not None:
            request_deadline = min(request_deadline, deadline)
        host = urlsplit(registry.public_origin).hostname
        try:
            status = _https_status(host, request_deadline, self.monotonic)
        except (OSError, subprocess.SubprocessError, ValueError):
            raise IngressVerificationError("Tailscale HTTPS ingress is unavailable") from None
        minimum, maximum = (200, 299) if require_success else (100, 599)
        if not minimum <= status <= maximum:
            raise IngressVerificationError("Tailscale HTTPS ingress is unavailable")

    def _verify_listener(
        self, caddy_pid: int, *, timeout: float = 5, prepared: bool = False,
    ) -> None:
        message = "Caddy listener isolation could not be verified"
        try:
            result = self.run(
                ["/usr/sbin/lsof", "-nP", "-a", "-p", str(caddy_pid),
                 "-iTCP", "-sTCP:LISTEN", "-F", "n"],
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            raise IngressVerificationError(message) from None
        if result.returncode != 0:
            raise IngressVerificationError(message)
        expected = (f"127.0.0.1:{TAILSCALE_CADDY_PORT}",)
        if prepared:
            expected = ("*:80", *expected)
        listeners = []
        for field in result.stdout.splitlines():
            if not field.startswith("n"):
                continue
            endpoint = field[1:]
            if not re.fullmatch(r"(?:\[[^\]\s]+\]|[^:\s]+):[0-9]+", endpoint):
                raise IngressVerificationError(message)
            if endpoint in ("127.0.0.1:2019", "[::1]:2019"):
                continue
            if endpoint not in expected:
                raise IngressVerificationError(message)
            listeners.append(endpoint)
        if tuple(sorted(listeners)) != expected:
            raise IngressVerificationError(message)
