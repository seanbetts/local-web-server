import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from local_web_server.config import load_manifest
from local_web_server.git_build import GitRepository
from local_web_server.models import BackendProbeSpec, Command, HostApp, HostRegistry
from local_web_server.runtime import RuntimeLayout, atomic_symlink
from local_web_server.services import HealthResult, HttpHealthChecker, ServiceState
from tests.helpers import init_git_repo


class FakeRepository:
    def __init__(self, commit, manifest):
        self.commit = commit
        self.manifest = manifest

    def main_commit(self):
        return self.commit

    def manifest_at(self, commit):
        return self.manifest


class FakeServices:
    def __init__(self, state=ServiceState.RUNNING):
        self.state_value = state
        self.labels = []

    def state(self, label):
        self.labels.append(label)
        return self.state_value


class FakeHealth:
    def __init__(self, results):
        self.results = list(results)
        self.urls = []
        self.hosts = []

    def check(self, url, *, host=None):
        self.urls.append(url)
        self.hosts.append(host)
        return self.results.pop(0)


class StatusHarness:
    def __init__(self, root, *, service=False, remote_backend=False, split_service=False):
        self.root = root
        self.repository = root / "repository"
        self.repository.mkdir()
        self.main_commit = "a" * 40
        self.manifest = {
            "schemaVersion": 1,
            "id": "fixture",
            "title": "Fixture",
            "route": "/fixture",
            "kind": "service" if service else "static",
            "build": {"commands": [["python3", "-c", "pass"]], "output": "dist", "environment": []},
            "healthPath": "/fixture/healthz",
        }
        if service:
            self.manifest["service"] = {"module": "fixture", "internalHealthPath": "/healthz"}
        self.git_commit = init_git_repo(
            self.repository, {"local-web.json": json.dumps(self.manifest)}
        )
        self.committed_manifest = load_manifest(self.repository / "local-web.json")
        if remote_backend:
            self.committed_manifest = replace(
                self.committed_manifest,
                build=replace(
                    self.committed_manifest.build,
                    environment=("PUBLIC_API_URL",),
                ),
            )
        if split_service:
            self.committed_manifest = replace(
                self.committed_manifest,
                service=replace(
                    self.committed_manifest.service,
                    frontend_output=Path("frontend"),
                    proxy_paths=("/api",),
                ),
            )
        self.layout = RuntimeLayout(root / "runtime", "fixture")
        self.registry = HostRegistry(
            host="fixture.test",
            runtime_root=root / "runtime",
            apps=(HostApp(
                id="fixture", repository=self.repository, auto_deploy=True,
                environment_file=None, environment=(), port=8765 if service else None,
                start_command=Command(("python3", "-m", "fixture")) if service else None,
                backend_probe=(
                    BackendProbeSpec(
                        public_environment=("PUBLIC_API_URL",),
                        base_url_environment="PUBLIC_API_URL",
                        path="/health",
                        headers_from_environment=(),
                    )
                    if remote_backend
                    else None
                ),
            ),),
        )
        if remote_backend:
            self.registry = replace(
                self.registry,
                apps=(
                    replace(
                        self.registry.apps[0],
                        environment=(("PUBLIC_API_URL", "https://example.invalid"),),
                    ),
                ),
            )

    def deploy(self, commit):
        release = self.layout.releases / commit
        release.mkdir(parents=True)
        atomic_symlink(self.layout.current, release)


class StatusCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def collect(self, harness, health, services=None):
        from local_web_server.status import StatusCollector

        return StatusCollector(
            harness.registry,
            health=health,
            services=services or FakeServices(),
            repository_factory=lambda _path: FakeRepository(
                harness.main_commit, harness.committed_manifest
            ),
        ).collect()[0]

    def test_static_only_app_checks_canonical_frontend_root_without_backend(self):
        harness = StatusHarness(self.root)
        harness.deploy(harness.main_commit)

        health = FakeHealth([HealthResult(True, 200, None)])
        status = self.collect(harness, health)

        self.assertEqual(status.app_id, "fixture")
        self.assertEqual(status.main_commit, "a" * 40)
        self.assertEqual(status.deployed_commit, "a" * 40)
        self.assertFalse(status.stale)
        self.assertIsNone(status.service_state)
        self.assertIsNone(status.internal_health)
        self.assertTrue(status.frontend_health.healthy)
        self.assertIsNone(status.backend_health)
        self.assertEqual(health.urls, ["http://fixture.test/fixture/"])
        self.assertIsNone(status.latest_log)
        self.assertTrue(status.healthy)

    def test_deployed_commit_behind_main_is_stale_even_when_health_is_good(self):
        harness = StatusHarness(self.root)
        harness.deploy("b" * 40)

        status = self.collect(harness, FakeHealth([HealthResult(True, 200, None)]))

        self.assertTrue(status.stale)
        self.assertFalse(status.healthy)

    def test_remote_backed_static_app_checks_frontend_and_reserved_backend(self):
        harness = StatusHarness(self.root, remote_backend=True)
        harness.deploy(harness.main_commit)
        health = FakeHealth(
            [HealthResult(True, 200, None), HealthResult(False, 502, "provider detail")]
        )

        status = self.collect(harness, health)

        self.assertEqual(
            health.urls,
            [
                "http://fixture.test/fixture/",
                "http://fixture.test/_local-web/health/fixture/backend",
            ],
        )
        self.assertTrue(status.frontend_health.healthy)
        self.assertFalse(status.backend_health.healthy)
        self.assertFalse(status.healthy)

    def test_remote_backed_status_uses_real_head_checker_for_both_public_probes(self):
        harness = StatusHarness(self.root, remote_backend=True)
        harness.deploy(harness.main_commit)
        requests = []

        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        def open_request(request, *, timeout):
            requests.append((request.get_method(), request.full_url, timeout))
            return Response()

        with patch(
            "local_web_server.services.urllib.request.urlopen",
            side_effect=open_request,
        ):
            status = self.collect(harness, HttpHealthChecker())

        self.assertTrue(status.healthy)
        self.assertEqual(
            requests,
            [
                ("HEAD", "http://fixture.test/fixture/", 5),
                (
                    "HEAD",
                    "http://fixture.test/_local-web/health/fixture/backend",
                    5,
                ),
            ],
        )

    def test_missing_release_is_not_deployed(self):
        harness = StatusHarness(self.root)

        status = self.collect(harness, FakeHealth([HealthResult(False, 503, "HTTP 503")]))

        self.assertIsNone(status.deployed_commit)
        self.assertFalse(status.stale)
        self.assertFalse(status.healthy)

    def test_stopped_backend_is_reported_separately_from_health(self):
        harness = StatusHarness(self.root, service=True)
        harness.deploy(harness.main_commit)
        services = FakeServices(ServiceState.STOPPED)

        status = self.collect(
            harness,
            FakeHealth(
                [
                    HealthResult(True, 200, None),
                    HealthResult(True, 200, None),
                    HealthResult(True, 200, None),
                ]
            ),
            services,
        )

        self.assertEqual(status.service_state, ServiceState.STOPPED)
        self.assertTrue(status.internal_health.healthy)
        self.assertTrue(status.frontend_health.healthy)
        self.assertTrue(status.backend_health.healthy)
        self.assertFalse(status.healthy)
        self.assertEqual(
            services.labels,
            ["com.sean.local-web.caddy", "com.sean.local-web.fixture"],
        )

    def test_service_reports_public_failure_when_internal_health_succeeds(self):
        harness = StatusHarness(self.root, service=True)
        harness.deploy(harness.main_commit)

        status = self.collect(
            harness,
            FakeHealth(
                [
                    HealthResult(True, 200, None),
                    HealthResult(True, 200, None),
                    HealthResult(False, 502, "HTTP 502"),
                ]
            ),
            FakeServices(),
        )

        self.assertTrue(status.internal_health.healthy)
        self.assertTrue(status.frontend_health.healthy)
        self.assertFalse(status.backend_health.healthy)
        self.assertFalse(status.healthy)

    def test_service_internal_health_uses_public_host_with_loopback_url(self):
        harness = StatusHarness(self.root, service=True)
        harness.deploy(harness.main_commit)
        health = FakeHealth(
            [
                HealthResult(True, 200, None),
                HealthResult(True, 200, None),
                HealthResult(True, 200, None),
            ]
        )

        self.collect(harness, health, FakeServices())

        self.assertEqual(
            health.urls,
            [
                "http://fixture.test/fixture/",
                "http://127.0.0.1:8765/healthz",
                "http://fixture.test/fixture/healthz",
            ],
        )
        self.assertEqual(health.hosts, [None, "fixture.test", None])

    def test_tailscale_mode_uses_https_public_and_http_internal_health(self):
        harness = StatusHarness(self.root, service=True)
        harness.registry = replace(
            harness.registry,
            host="local.example.ts.net",
            schema_version=2,
            public_origin="https://local.example.ts.net",
            ingress_mode="tailscale-serve",
        )
        harness.deploy(harness.main_commit)
        health = FakeHealth(
            [
                HealthResult(True, 200, None),
                HealthResult(True, 200, None),
                HealthResult(True, 200, None),
            ]
        )

        self.collect(harness, health, FakeServices())

        self.assertEqual(
            health.urls,
            [
                "https://local.example.ts.net/fixture/",
                "http://127.0.0.1:8765/healthz",
                "https://local.example.ts.net/fixture/healthz",
            ],
        )
        self.assertEqual(
            health.hosts,
            [None, "local.example.ts.net", None],
        )

    def test_service_reports_both_internal_and_public_health_failures(self):
        harness = StatusHarness(self.root, service=True)
        harness.deploy(harness.main_commit)

        status = self.collect(
            harness,
            FakeHealth(
                [
                    HealthResult(True, 200, None),
                    HealthResult(False, None, "refused"),
                    HealthResult(False, 503, "HTTP 503"),
                ]
            ),
            FakeServices(),
        )

        self.assertFalse(status.internal_health.healthy)
        self.assertFalse(status.backend_health.healthy)

    def test_split_service_checks_frontend_internal_and_public_backend(self):
        harness = StatusHarness(self.root, service=True, split_service=True)
        harness.deploy(harness.main_commit)
        health = FakeHealth(
            [
                HealthResult(True, 200, None),
                HealthResult(True, 200, None),
                HealthResult(True, 204, None),
            ]
        )

        status = self.collect(harness, health, FakeServices())

        self.assertEqual(
            health.urls,
            [
                "http://fixture.test/fixture/",
                "http://127.0.0.1:8765/healthz",
                "http://fixture.test/fixture/healthz",
            ],
        )
        self.assertTrue(status.frontend_health.healthy)
        self.assertTrue(status.internal_health.healthy)
        self.assertTrue(status.backend_health.healthy)
        self.assertTrue(status.healthy)

    def test_status_uses_manifest_from_exact_main_when_working_tree_conflicts(self):
        harness = StatusHarness(self.root)
        harness.main_commit = harness.git_commit
        harness.deploy(harness.git_commit)
        dirty = dict(harness.manifest)
        dirty["healthPath"] = "/fixture/dirty-health"
        (harness.repository / "local-web.json").write_text(json.dumps(dirty), encoding="utf-8")
        health = FakeHealth([HealthResult(True, 200, None)])
        services = FakeServices()

        from local_web_server.status import StatusCollector

        status = StatusCollector(
            harness.registry,
            health=health,
            services=services,
            repository_factory=GitRepository,
        ).collect()[0]

        self.assertTrue(status.healthy)
        self.assertEqual(health.urls, ["http://fixture.test/fixture/"])

    def test_caddy_state_is_collected_once_and_stopped_caddy_is_unhealthy(self):
        harness = StatusHarness(self.root)
        harness.deploy(harness.main_commit)
        services = FakeServices(ServiceState.STOPPED)

        status = self.collect(
            harness,
            FakeHealth([HealthResult(True, 200, None)]),
            services,
        )

        self.assertEqual(status.caddy_state, ServiceState.STOPPED)
        self.assertEqual(services.labels, ["com.sean.local-web.caddy"])
        self.assertFalse(status.healthy)


if __name__ == "__main__":
    unittest.main()
