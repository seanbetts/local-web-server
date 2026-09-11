"""Narrow adapters for launchd service control and HTTP health checks."""

import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .public_http import open_public_request


class ServiceState(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    MISSING = "missing"


@dataclass(frozen=True)
class HealthResult:
    healthy: bool
    status: int | None
    error: str | None


def _run_launchctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, check=False, text=True)


def _missing_print_service(
    result: subprocess.CompletedProcess[str], label: str
) -> bool:
    missing = (
        f'Could not find service "{label}" '
        f"in domain for user gui: {os.getuid()}"
    )
    accepted_missing = (
        missing,
        f"{missing}\n",
        f"Bad request.\n{missing}",
        f"Bad request.\n{missing}\n",
    )
    return result.returncode in (3, 113) and result.stderr in accepted_missing


def _missing_bootout_service(result: subprocess.CompletedProcess[str]) -> bool:
    return result.returncode == 3 and "Boot-out failed: 3: No such process" in result.stderr


class LaunchctlServiceController:
    """Manage a single user LaunchAgent without touching unrelated jobs."""

    @staticmethod
    def _domain() -> str:
        return f"gui/{os.getuid()}"

    def _target(self, label: str) -> str:
        return f"{self._domain()}/{label}"

    def state(self, label: str) -> ServiceState:
        result = _run_launchctl(["launchctl", "print", self._target(label)])
        if _missing_print_service(result, label):
            return ServiceState.MISSING
        result.check_returncode()
        if "state = running" in result.stdout:
            return ServiceState.RUNNING
        return ServiceState.STOPPED

    def ensure_running(self, label: str, plist_path: Path) -> None:
        state = self.state(label)
        if state is ServiceState.MISSING:
            bootstrap = _run_launchctl(
                ["launchctl", "bootstrap", self._domain(), str(plist_path)]
            )
            bootstrap.check_returncode()
            return
        self.restart(label, plist_path)

    def restart(self, label: str, plist_path: Path) -> None:
        self.replace(label, plist_path)

    def stop(self, label: str, plist_path: Path) -> None:
        result = _run_launchctl(["launchctl", "bootout", self._domain(), str(plist_path)])
        if not _missing_bootout_service(result):
            result.check_returncode()

    def replace(self, label: str, plist_path: Path) -> None:
        self.stop(label, plist_path)
        result = _run_launchctl(
            ["launchctl", "bootstrap", self._domain(), str(plist_path)]
        )
        result.check_returncode()


class HttpHealthChecker:
    """Perform bounded, body-free HTTP health checks."""

    def check(self, url: str, *, host: str | None = None) -> HealthResult:
        try:
            request = urllib.request.Request(
                url,
                headers={"Host": host} if host else {},
                method="HEAD",
            )
            with open_public_request(request, timeout=5) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            return HealthResult(False, status, f"HTTP {status}")
        except (OSError, ValueError, urllib.error.URLError) as error:
            return HealthResult(False, None, str(error)[:300])
        if 200 <= status < 300:
            return HealthResult(True, status, None)
        return HealthResult(False, status, f"HTTP {status}")
