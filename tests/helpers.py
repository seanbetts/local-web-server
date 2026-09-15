import os
import subprocess
import unittest
from collections.abc import Callable
from pathlib import Path

from local_web_server.services import HealthResult, ServiceState
from scripts import verify_host_profile_workflow as profile_verifier
from tests.suites import acceptance


def select_test_caddy(
    *,
    select: Callable[[], Path] = profile_verifier.select_caddy,
    required: bool | None = None,
) -> Path | None:
    if required is None:
        required = os.environ.get("LOCAL_WEB_REQUIRE_CADDY_INTEGRATION") == "1"
    try:
        return select()
    except profile_verifier.HostProfileWorkflowError as error:
        if required:
            raise RuntimeError("required Caddy integration is unavailable") from error
        return None


TEST_CADDY = select_test_caddy()
def caddy_integration(test):
    return acceptance(unittest.skipUnless(TEST_CADDY is not None, "requires Caddy 2")(test))


class FakeServiceController:
    """A recording service-controller fake for deployment and status tests."""

    def __init__(
        self,
        state: ServiceState = ServiceState.RUNNING,
        states: list[ServiceState] | None = None,
    ):
        self.state_result = state
        self.state_results = list(states or ())
        self.calls: list[tuple[str, str, Path | None]] = []

    def state(self, label: str) -> ServiceState:
        self.calls.append(("state", label, None))
        if self.state_results:
            return self.state_results.pop(0)
        return self.state_result

    def ensure_running(self, label: str, plist_path: Path) -> None:
        self.calls.append(("ensure_running", label, plist_path))

    def restart(self, label: str, plist_path: Path) -> None:
        self.calls.append(("restart", label, plist_path))

    def stop(self, label: str, plist_path: Path) -> None:
        self.calls.append(("stop", label, plist_path))

    def replace(self, label: str, plist_path: Path) -> None:
        self.calls.append(("replace", label, plist_path))


class FakeHealthChecker:
    """A recording health-checker fake with queued and fallback results."""

    def __init__(
        self,
        result: HealthResult = HealthResult(True, 200, None),
        results: list[HealthResult] | None = None,
    ):
        self.result = result
        self.results = list(results or ())
        self.calls: list[str] = []
        self.hosts: list[str | None] = []

    def check(self, url: str, *, host: str | None = None) -> HealthResult:
        self.calls.append(url)
        self.hosts.append(host)
        if self.results:
            return self.results.pop(0)
        return self.result


def init_git_repo(root: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Local Web Tests"], cwd=root, check=True)
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()
