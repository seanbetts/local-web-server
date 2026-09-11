import json
import unittest
from dataclasses import replace
from pathlib import Path

from local_web_server.index_registry import render_index_registry
from local_web_server.models import (
    AppManifest,
    BackendProbeSpec,
    BuildSpec,
    Command,
    HomePresentation,
    HostApp,
    HostRegistry,
    ServiceSpec,
)


class IndexRegistryTests(unittest.TestCase):
    def setUp(self):
        build = BuildSpec((Command(("true",)),), Path("public"), ())
        self.registry = HostRegistry(
            host="test-mac.local",
            runtime_root=Path("/private/runtime-private-marker"),
            apps=(
                HostApp(
                    id="plotter",
                    repository=Path("/private/plotter-private-marker"),
                    auto_deploy=True,
                    environment_file=Path("/private/" + "plotter.env-private-marker"),
                    environment=(("PRIVATE_ENVIRONMENT_MARKER", "private-value-marker"),),
                    port=None,
                    start_command=None,
                    backend_probe=BackendProbeSpec(
                        public_environment=("PUBLIC_VALUE",),
                        base_url_environment="PRIVATE_ORIGIN_MARKER",
                        path="/private-probe-marker",
                        headers_from_environment=(("apikey", "PRIVATE_KEY_MARKER"),),
                    ),
                ),
                HostApp(
                    id="samplebeta",
                    repository=Path("/private/samplebeta-private-marker"),
                    auto_deploy=False,
                    environment_file=None,
                    environment=(),
                    port=None,
                    start_command=None,
                ),
            ),
        )
        self.manifests = {
            "plotter": AppManifest(
                1,
                "plotter",
                "Plotter",
                "/plotter",
                "static",
                build,
                "/plotter/",
                None,
                HomePresentation("route", "#75A7FF"),
            ),
            "samplebeta": AppManifest(
                1,
                "samplebeta",
                "Sample Workspace",
                "/samplebeta",
                "static",
                build,
                "/samplebeta/",
                None,
                HomePresentation("shirt", "#7A1735"),
            ),
        }

    def test_renders_the_public_schema_in_registry_order(self):
        self.assertEqual(
            json.loads(render_index_registry(self.registry, self.manifests)),
            {
                "schemaVersion": 1,
                "apps": [
                    {
                        "id": "plotter",
                        "title": "Plotter",
                        "route": "/plotter/",
                        "icon": "route",
                        "accent": "#75A7FF",
                        "frontendHealthPath": "/plotter/",
                        "backendHealthPath": "/_local-web/health/plotter/backend",
                    },
                    {
                        "id": "samplebeta",
                        "title": "Sample Workspace",
                        "route": "/samplebeta/",
                        "icon": "shirt-sport",
                        "accent": "#7A1735",
                        "frontendHealthPath": "/samplebeta/",
                        "backendHealthPath": None,
                    },
                ],
            },
        )

    def test_is_byte_identical_newline_terminated_and_uses_canonical_legacy_icons(self):
        first = render_index_registry(self.registry, self.manifests)

        self.assertEqual(first, render_index_registry(self.registry, self.manifests))
        self.assertTrue(first.endswith(b"\n"))
        self.assertEqual(json.loads(first)["apps"][1]["icon"], "shirt-sport")

    def test_rejects_manifest_ids_that_do_not_match_the_registered_app(self):
        manifests = dict(self.manifests)
        manifests["plotter"] = replace(manifests["plotter"], id="samplebeta")

        with self.assertRaisesRegex(ValueError, "manifest id does not match registered app: plotter"):
            render_index_registry(self.registry, manifests)

    def test_omits_private_registry_and_manifest_fixture_values(self):
        expanded_command = Command(
            (
                "/usr/bin/node",
                "/private/runtime-private-marker/apps/recipes/current/server/service.mjs",
                "--port",
                "52991",
                "--data-dir",
                "/private/Repository With Spaces/data",
            )
        )
        registry = replace(
            self.registry,
            apps=(
                replace(
                    self.registry.apps[0],
                    repository=Path("/private/Repository With Spaces"),
                    port=52991,
                    start_command=expanded_command,
                ),
                self.registry.apps[1],
            ),
        )
        manifests = dict(self.manifests)
        manifests["plotter"] = replace(
            manifests["plotter"],
            kind="service",
            health_path="/plotter/healthz",
            service=ServiceSpec("server/service.mjs", "/healthz"),
        )
        rendered = render_index_registry(registry, manifests)

        for marker in (
            "runtime-private-marker",
            "plotter-private-marker",
            "Repository With Spaces",
            "service.mjs",
            "52991",
            "env-private-marker",
            "PRIVATE_ENVIRONMENT_MARKER",
            "private-value-marker",
            "PRIVATE_ORIGIN_MARKER",
            "private-probe-marker",
            "PRIVATE_KEY_MARKER",
        ):
            with self.subTest(marker=marker):
                self.assertNotIn(marker.encode(), rendered)


if __name__ == "__main__":
    unittest.main()
