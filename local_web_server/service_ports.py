"""Deterministic bounded loopback port selection for local services."""

from __future__ import annotations

import socket
from collections.abc import Callable

from .models import HostRegistry


class ServicePortError(ValueError):
    """A service port cannot be selected safely."""


def _loopback_available(port: int) -> bool:
    candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        candidate.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        candidate.close()


class ServicePortAllocator:
    def __init__(
        self,
        first: int = 52000,
        last: int = 52999,
        *,
        available: Callable[[int], bool] | None = None,
    ):
        if (
            type(first) is not int
            or type(last) is not int
            or not 1024 <= first <= last <= 65535
        ):
            raise ServicePortError("service port range is invalid")
        self.first = first
        self.last = last
        self._available = available or _loopback_available

    def select(
        self,
        registry: HostRegistry,
        *,
        excluded: frozenset[int] = frozenset(),
    ) -> int:
        if not isinstance(registry, HostRegistry) or any(
            type(port) is not int for port in excluded
        ):
            raise ServicePortError("service port request is invalid")
        registered = {app.port for app in registry.apps if app.port is not None}
        for port in range(self.first, self.last + 1):
            if port in registered or port in excluded:
                continue
            try:
                if self._available(port):
                    return port
            except Exception as error:
                raise ServicePortError("service port availability check failed") from error
        raise ServicePortError("no service port is available")
