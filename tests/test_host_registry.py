import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from local_web_server.config import (
    build_environment,
    load_manifest,
    load_registered_app,
    load_registry,
)
from local_web_server.render import render_caddyfile
from tests.helpers import init_git_repo


class HostRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / "runtime"
        self.catalogue = self.root / "repositories/catalogue"
        self.metrics = self.root / "repositories/metrics"
        self.events = self.root / "repositories/events"
        self.manifest_payloads = {
            "catalogue": {
                "schemaVersion": 1,
                "id": "catalogue",
                "title": "Fixture Catalogue",
                "route": "/catalogue",
                "kind": "static",
                "build": {
                    "commands": [["true"]],
                    "output": "dist",
                    "environment": ["VITE_PUBLIC_BASE_PATH"],
                },
                "healthPath": "/catalogue/",
            },
            "metrics": {
                "schemaVersion": 1,
                "id": "metrics",
                "title": "Fixture Metrics",
                "route": "/metrics",
                "kind": "service",
                "build": {
                    "commands": [["true"]],
                    "output": "release",
                    "environment": [],
                },
                "healthPath": "/metrics/healthz",
                "service": {
                    "module": "fixture_metrics",
                    "internalHealthPath": "/healthz",
                    "frontendOutput": "frontend",
                    "proxyPaths": ["/api"],
                },
            },
            "events": {
                "schemaVersion": 1,
                "id": "events",
                "title": "Fixture Events",
                "route": "/events",
                "kind": "service",
                "build": {
                    "commands": [["true"]],
                    "output": "public",
                    "environment": [],
                },
                "healthPath": "/events/healthz",
                "service": {
                    "module": "fixture_events",
                    "internalHealthPath": "/healthz",
                },
            },
        }
        for app_id, repository in (
            ("catalogue", self.catalogue),
            ("metrics", self.metrics),
            ("events", self.events),
        ):
            repository.mkdir(parents=True)
            init_git_repo(
                repository,
                {
                    "local-web.json": json.dumps(
                        self.manifest_payloads[app_id], sort_keys=True
                    )
                    + "\n"
                },
            )

        local = self.root / "config/local"
        local.mkdir(parents=True, mode=0o700)
        (local / "history").mkdir(mode=0o700)
        (local / "backups").mkdir(mode=0o700)
        self.registry_path = local / "apps.json"
        self.registry_path.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "publicOrigin": "https://local.example.ts.net",
                    "ingressMode": "tailscale-serve",
                    "runtimeRoot": str(self.runtime),
                    "apps": [
                        {
                            "id": "catalogue",
                            "repository": str(self.catalogue),
                            "autoDeploy": True,
                            "environment": {
                                "VITE_PUBLIC_BASE_PATH": "/catalogue/"
                            },
                        },
                        {
                            "id": "metrics",
                            "repository": str(self.metrics),
                            "autoDeploy": True,
                            "port": 53211,
                            "startCommand": [
                                "/usr/bin/python3",
                                "-m",
                                "fixture_metrics",
                                "--port",
                                "53211",
                            ],
                        },
                        {
                            "id": "events",
                            "repository": str(self.events),
                            "autoDeploy": True,
                            "port": 53212,
                            "startCommand": [
                                "/usr/bin/python3",
                                "-m",
                                "fixture_events",
                                "--port",
                                "53212",
                            ],
                        },
                    ],
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.registry_path.chmod(0o600)

    def test_private_registry_matches_committed_fixture_manifests(self):
        registry = load_registry(self.registry_path)

        self.assertEqual(registry.schema_version, 2)
        self.assertEqual(registry.host, "local.example.ts.net")
        self.assertEqual(
            registry.public_origin, "https://local.example.ts.net"
        )
        self.assertEqual(registry.ingress_mode, "tailscale-serve")
        self.assertTrue(registry.apps)
        self.assertEqual(len({app.id for app in registry.apps}), len(registry.apps))
        self.assertEqual(
            len({app.repository.resolve() for app in registry.apps}),
            len(registry.apps),
        )
        for host_app in registry.apps:
            manifest = load_manifest(host_app.repository / "local-web.json")
            committed = subprocess.run(
                ["git", "show", "HEAD:local-web.json"],
                cwd=host_app.repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(manifest.id, host_app.id)
            self.assertEqual(
                json.loads(committed), self.manifest_payloads[host_app.id]
            )
            self.assertEqual(
                json.loads(committed),
                json.loads(
                    (host_app.repository / "local-web.json").read_text(
                        encoding="utf-8"
                    )
                ),
            )

    def test_static_vite_app_receives_its_registered_hosted_base_path(self):
        registry = load_registry(self.registry_path)
        host, manifest = load_registered_app(registry, "catalogue")

        environment = build_environment(manifest, host, {"PATH": "/usr/bin"})

        self.assertEqual(environment["VITE_PUBLIC_BASE_PATH"], "/catalogue/")

    def test_service_ports_are_unique_and_static_apps_have_no_port(self):
        registry = load_registry(self.registry_path)
        service_ports = []
        for registered in registry.apps:
            _host, manifest = load_registered_app(registry, registered.id)
            if manifest.kind == "service":
                self.assertIsNotNone(registered.port)
                service_ports.append(registered.port)
            else:
                self.assertIsNone(registered.port)

        self.assertEqual(len(service_ports), len(set(service_ports)))

    def test_service_health_route_renders_and_real_caddy_accepts_configuration(self):
        registry = load_registry(self.registry_path)
        manifests = {
            registered.id: load_registered_app(registry, registered.id)[1]
            for registered in registry.apps
        }

        caddyfile = render_caddyfile(registry, manifests)

        self.assertIn("@metrics_health path /healthz", caddyfile)
        caddy_home = self.root / "caddy-home"
        caddy_data = self.root / "caddy-data"
        caddy_config = self.root / "caddy-config"
        for directory in (caddy_home, caddy_data, caddy_config):
            directory.mkdir()
        homebrew_caddy = Path("/opt/homebrew/bin/caddy")
        caddy = (
            str(homebrew_caddy)
            if homebrew_caddy.is_file()
            else shutil.which("caddy")
        )
        if caddy is None:
            self.skipTest("real Caddy is unavailable")
        result = subprocess.run(
            [
                caddy,
                "validate",
                "--adapter",
                "caddyfile",
                "--config",
                "-",
            ],
            input=caddyfile,
            capture_output=True,
            text=True,
            timeout=5,
            env={
                "HOME": str(caddy_home),
                "PATH": "/usr/bin:/bin",
                "XDG_CONFIG_HOME": str(caddy_config),
                "XDG_DATA_HOME": str(caddy_data),
            },
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
