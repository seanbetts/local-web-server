import json
import os
import subprocess
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from local_web_server.config import (
    ConfigError,
    RegistryRepositoryConflictError,
    build_environment,
    load_manifest,
    load_registered_app,
    load_registry,
    parse_manifest,
    probe_environment,
    validate_registered_app,
)
from local_web_server.models import Command, HomePresentation, ReleaseEntry


class ConfigTests(unittest.TestCase):
    def test_high_caddy_port_is_reserved_only_for_tailscale_registry(self):
        payload = {
            "schemaVersion": 2, "publicOrigin": "https://local.example.ts.net",
            "ingressMode": "tailscale-serve", "runtimeRoot": str(self.root / "runtime"),
            "apps": [{"id": "app", "repository": str(self.root), "autoDeploy": True,
                      "port": 8080}],
        }
        with self.assertRaisesRegex(ConfigError, "reserved for Caddy"):
            load_registry(self.write_json("v2.json", payload))
        payload.pop("publicOrigin")
        payload.pop("ingressMode")
        payload.update(schemaVersion=1, host="legacy.local")
        self.assertEqual(load_registry(self.write_json("v1.json", payload)).apps[0].port, 8080)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def static_manifest_payload(self):
        return {
            "schemaVersion": 1,
            "id": "plotter",
            "title": "Plotter",
            "route": "/plotter",
            "kind": "static",
            "build": {
                "commands": [["npm", "ci"], ["npm", "run", "build"]],
                "output": "dist",
                "environment": ["VITE_SUPABASE_URL"],
            },
            "healthPath": "/plotter/",
        }

    def service_manifest_payload(self):
        return {
            "schemaVersion": 1,
            "id": "example-service",
            "title": "Example Service",
            "route": "/example-service",
            "kind": "service",
            "build": {
                "commands": [["python3", "-m", "build"]],
                "output": "public",
                "environment": [],
            },
            "healthPath": "/example-service/healthz",
            "service": {
                "module": "example_service",
                "internalHealthPath": "/healthz",
            },
        }

    def backend_probe_payload(self):
        return {
            "publicEnvironment": [
                "VITE_SUPABASE_URL",
                "VITE_SUPABASE_PUBLISHABLE_KEY",
            ],
            "baseUrlEnvironment": "VITE_SUPABASE_URL",
            "path": "/auth/v1/health",
            "headersFromEnvironment": {
                "apikey": "VITE_SUPABASE_PUBLISHABLE_KEY",
            },
        }

    def static_manifest_with_probe_inputs(self):
        payload = self.static_manifest_payload()
        payload["build"]["environment"].append("VITE_SUPABASE_PUBLISHABLE_KEY")
        return payload

    def host_with_backend_probe(self, probe=None, environment=None):
        environment_file = self.root / "probe.env"
        if environment is not None:
            environment_file.write_text(environment, encoding="utf-8")
        registry_payload = {
            "schemaVersion": 1,
            "host": "example-mac.local",
            "runtimeRoot": str(self.root / "runtime"),
            "apps": [{
                "id": "plotter",
                "repository": str(self.root),
                "autoDeploy": True,
                "backendProbe": probe if probe is not None else self.backend_probe_payload(),
            }],
        }
        if environment is not None:
            registry_payload["apps"][0]["environmentFile"] = str(environment_file)
        return load_registry(self.write_json("probe-registry.json", registry_payload)).apps[0]

    def test_loads_static_manifest_with_direct_argv_commands(self):
        path = self.write_json("local-web.json", self.static_manifest_payload())

        manifest = load_manifest(path)

        self.assertEqual(manifest.id, "plotter")
        self.assertEqual(manifest.build.commands[0].argv, ("npm", "ci"))
        self.assertEqual(manifest.build.output, Path("dist"))

    def test_loads_declarative_release_entries_and_service_start_command(self):
        payload = self.service_manifest_payload()
        payload["build"]["output"] = "release"
        payload["build"]["release"] = [
            {"source": "dist", "target": "public"},
            {"source": "server/app.mjs", "target": "server/app.mjs"},
        ]
        payload["service"]["startCommand"] = [
            "/usr/bin/env",
            "node",
            "server/app.mjs",
            "--port",
            "{port}",
            "--data-dir",
            "{repository}/data",
        ]

        manifest = parse_manifest(json.dumps(payload))

        self.assertEqual(
            manifest.build.release_entries,
            (
                ReleaseEntry(Path("dist"), Path("public")),
                ReleaseEntry(Path("server/app.mjs"), Path("server/app.mjs")),
            ),
        )
        self.assertEqual(
            manifest.service.start_command.argv,
            (
                "/usr/bin/env",
                "node",
                "server/app.mjs",
                "--port",
                "{port}",
                "--data-dir",
                "{repository}/data",
            ),
        )

    def test_release_contract_must_cover_split_service_frontend(self):
        payload = self.service_manifest_payload()
        payload["build"]["output"] = "release"
        payload["service"].update(
            {"frontendOutput": "public", "proxyPaths": ["/api"]}
        )
        payload["build"]["release"] = [
            {"source": "dist/assets", "target": "public/assets"},
            {"source": "server/app.mjs", "target": "server/app.mjs"},
        ]

        with self.assertRaisesRegex(ConfigError, "service.frontendOutput"):
            parse_manifest(json.dumps(payload))

    def test_rejects_invalid_declarative_release_contracts(self):
        cases = (
            ([], "build.release must be a non-empty list"),
            ([{"source": "../private", "target": "public"}], "build.release.source"),
            ([{"source": "dist", "target": "."}], "build.release.target"),
            ([{"source": "release/source", "target": "public"}], "build.release.source"),
            (
                [
                    {"source": "dist", "target": "public"},
                    {"source": "data", "target": "public/data"},
                ],
                "build.release targets must not overlap",
            ),
            ([{"source": "dist", "target": "public", "private": True}], "unknown key"),
        )
        for release, message in cases:
            with self.subTest(release=release):
                payload = self.service_manifest_payload()
                payload["build"]["output"] = "release"
                payload["build"]["release"] = release
                with self.assertRaisesRegex(ConfigError, message):
                    parse_manifest(json.dumps(payload))

    def test_rejects_invalid_service_start_commands(self):
        cases = (
            (["/usr/bin/env", "node", "server.mjs"], "must contain {port}"),
            (["/usr/bin/env", "node", "server.mjs", "{private}"], "unsupported placeholder"),
            (["/usr/bin/env", "node", "server.mjs", "{{port}}"], "unsupported placeholder"),
            (
                ["/usr/bin/env", "node", "server.mjs", "path/{repository", "{port}"],
                "unsupported placeholder",
            ),
        )
        for command, message in cases:
            with self.subTest(command=command):
                payload = self.service_manifest_payload()
                payload["service"]["startCommand"] = command
                with self.assertRaisesRegex(ConfigError, message):
                    parse_manifest(json.dumps(payload))

    def test_parses_optional_platform_contract(self):
        payload = self.static_manifest_payload()
        payload["platform"] = {
            "contractVersion": 1,
            "templateVersion": 1,
            "uiVersion": "0.1.0",
            "capabilities": ["supabase"],
        }

        manifest = parse_manifest(json.dumps(payload))

        self.assertEqual(manifest.platform.contract_version, 1)
        self.assertEqual(manifest.platform.template_version, 1)
        self.assertEqual(manifest.platform.ui_version, "0.1.0")
        self.assertEqual(manifest.platform.capabilities, ("supabase",))

    def test_legacy_manifest_has_no_platform_contract(self):
        manifest = parse_manifest(json.dumps(self.static_manifest_payload()))

        self.assertIsNone(manifest.platform)

    def test_rejects_invalid_platform_contracts(self):
        cases = (
            (
                {
                    "contractVersion": 2,
                    "templateVersion": 1,
                    "uiVersion": "0.1.0",
                    "capabilities": [],
                },
                "platform.contractVersion is unsupported",
            ),
            (
                {
                    "contractVersion": True,
                    "templateVersion": 1,
                    "uiVersion": "0.1.0",
                    "capabilities": [],
                },
                "platform.contractVersion is unsupported",
            ),
            (
                {
                    "contractVersion": 1,
                    "templateVersion": 0,
                    "uiVersion": "0.1.0",
                    "capabilities": [],
                },
                "platform.templateVersion",
            ),
            (
                {
                    "contractVersion": 1,
                    "templateVersion": 1,
                    "uiVersion": "v0.1.0",
                    "capabilities": [],
                },
                "platform.uiVersion",
            ),
            (
                {
                    "contractVersion": 1,
                    "templateVersion": 1,
                    "uiVersion": "0.1.0",
                    "capabilities": ["supabase", "supabase"],
                },
                "platform.capabilities must be unique and lexically sorted",
            ),
            (
                {
                    "contractVersion": 1,
                    "templateVersion": 1,
                    "uiVersion": "0.1.0",
                    "capabilities": ["zebra", "supabase"],
                },
                "platform.capabilities must be unique and lexically sorted",
            ),
            (
                {
                    "contractVersion": 1,
                    "templateVersion": 1,
                    "uiVersion": "0.1.0",
                    "capabilities": ["other"],
                },
                "platform.capabilities contains unsupported capability",
            ),
            (
                {
                    "contractVersion": 1,
                    "templateVersion": 1,
                    "uiVersion": "0.1.0",
                    "capabilities": [],
                    "unexpected": True,
                },
                "platform contains unknown key",
            ),
        )
        for index, (platform, error) in enumerate(cases):
            with self.subTest(index=index):
                payload = self.static_manifest_payload()
                payload["platform"] = platform
                with self.assertRaisesRegex(ConfigError, error):
                    parse_manifest(json.dumps(payload))

    def test_home_presentation_defaults_when_omitted(self):
        manifest = load_manifest(self.write_json("default-home.json", self.static_manifest_payload()))

        self.assertEqual(manifest.home, HomePresentation())

    def test_loads_strict_home_presentation(self):
        payload = self.static_manifest_payload()
        payload["home"] = {"icon": "route", "accent": "#75A7FF"}

        manifest = load_manifest(self.write_json("home.json", payload))

        self.assertEqual(manifest.home, HomePresentation("route", "#75A7FF"))

    def test_accepts_canonical_and_legacy_home_icons(self):
        aliases = {
            "apps": "apps",
            "book": "book",
            "route": "route",
            "shirt-sport": "shirt-sport",
            "chart-line": "chart-line",
            "app": "apps",
            "shirt": "shirt-sport",
            "chart": "chart-line",
        }
        for index, selected in enumerate(aliases):
            with self.subTest(index=index):
                payload = self.static_manifest_payload()
                payload["home"] = {"icon": selected, "accent": "#75A7FF"}
                manifest = load_manifest(self.write_json(f"icon-{index}.json", payload))
                self.assertEqual(manifest.home.icon, selected)

    def test_rejects_invalid_home_presentation(self):
        cases = (
            {},
            {"icon": "route"},
            {"accent": "#75A7FF"},
            {"icon": "route", "accent": "#75A7FF", "svg": "<svg/>"},
            {"icon": "<svg/>", "accent": "#75A7FF"},
            {"icon": "not-a-catalogue-icon", "accent": "#75A7FF"},
            {"icon": "route", "accent": "#75a7ff"},
            {"icon": "route", "accent": "#FFF"},
            {"icon": "route", "accent": "75A7FF"},
            {"icon": "route", "accent": "#75A7FFFF"},
            {"icon": True, "accent": "#75A7FF"},
            {"icon": "route", "accent": False},
            7, [], None,
        )
        for index, value in enumerate(cases):
            with self.subTest(value=value):
                payload = self.static_manifest_payload()
                payload["home"] = value
                with self.assertRaisesRegex(ConfigError, "home"):
                    load_manifest(self.write_json(f"invalid-home-{index}.json", payload))

    def test_rejects_route_traversal_shell_commands_and_unknown_keys(self):
        invalid = self.write_json("invalid.json", {
            "schemaVersion": 1,
            "id": "bad",
            "title": "Bad",
            "route": "/../bad",
            "kind": "static",
            "build": {
                "commands": ["npm run build"],
                "output": "../outside",
                "environment": [],
            },
            "healthPath": "/bad/",
            "surprise": True,
        })

        with self.assertRaisesRegex(ConfigError, "route"):
            load_manifest(invalid)

    def test_registry_rejects_duplicate_ids_ports_and_secret_values(self):
        path = self.write_json("apps.json", {
            "schemaVersion": 1,
            "host": "example-mac.local",
            "runtimeRoot": str(self.root / "runtime"),
            "apps": [
                {"id": "one", "repository": "/tmp/one", "autoDeploy": True, "port": 8765},
                {"id": "one", "repository": "/tmp/two", "autoDeploy": True, "port": 8765,
                 "password": "not-allowed"},
            ],
        })

        with self.assertRaisesRegex(ConfigError, "duplicate app id"):
            load_registry(path)

    def test_registry_rejects_duplicate_canonical_repository_roots(self):
        repository = self.root / "application"
        repository.mkdir()
        symlink_alias = self.root / "application-alias"
        symlink_alias.symlink_to(repository, target_is_directory=True)
        for name, alias in (
            ("lexical", repository.parent / "." / repository.name),
            ("symlink", symlink_alias),
        ):
            with self.subTest(name=name):
                path = self.write_json(f"duplicate-repositories-{name}.json", {
                    "schemaVersion": 1,
                    "host": "example-mac.local",
                    "runtimeRoot": str(self.root / "runtime"),
                    "apps": [
                        {"id": "one", "repository": str(repository), "autoDeploy": True},
                        {"id": "two", "repository": str(alias), "autoDeploy": True},
                    ],
                })

                with self.assertRaises(RegistryRepositoryConflictError) as raised:
                    load_registry(path)
                self.assertEqual(str(raised.exception), "duplicate app repository")

    def test_loads_service_manifest(self):
        path = self.write_json("service.json", {
            "schemaVersion": 1,
            "id": "example-service",
            "title": "Example Service",
            "route": "/example-service",
            "kind": "service",
            "build": {
                "commands": [["python3", "-m", "build"]],
                "output": "public",
                "environment": [],
            },
            "healthPath": "/example-service/healthz",
            "service": {
                "module": "example_service",
                "internalHealthPath": "/healthz",
            },
        })

        service = load_manifest(path)

        self.assertEqual(service.service.internal_health_path, "/healthz")

    def test_legacy_service_defaults_to_all_proxy(self):
        manifest = parse_manifest(json.dumps(self.service_manifest_payload()))

        self.assertIsNone(manifest.service.frontend_output)
        self.assertEqual(manifest.service.proxy_paths, ())

    def test_loads_split_service_contract(self):
        payload = self.service_manifest_payload()
        payload["service"].update({"frontendOutput": "public", "proxyPaths": ["/api"]})

        service = parse_manifest(json.dumps(payload)).service

        self.assertEqual(service.frontend_output, Path("public"))
        self.assertEqual(service.proxy_paths, ("/api",))

    def test_loads_canonical_frontend_security_origins_for_split_service(self):
        payload = self.service_manifest_payload()
        payload["service"].update(
            {
                "frontendOutput": "public",
                "proxyPaths": ["/api"],
                "frontendSecurity": {
                    "connectSources": [
                        "https://api.openrouteservice.org",
                        "https://api.maptiler.com:443",
                    ],
                    "imgSources": [
                        "https://tiles.example.test:8443",
                        "blob:",
                    ],
                },
            }
        )

        try:
            security = parse_manifest(json.dumps(payload)).service.frontend_security
        except ConfigError as error:
            self.fail(f"valid frontend security declaration was rejected: {error}")

        self.assertEqual(
            security.connect_sources,
            (
                "https://api.maptiler.com",
                "https://api.openrouteservice.org",
            ),
        )
        self.assertEqual(
            security.img_sources,
            ("blob:", "https://tiles.example.test:8443"),
        )

    def test_loads_the_documented_map_frontend_security_sources(self):
        payload = self.service_manifest_payload()
        payload.update(
            {
                "id": "plotter",
                "title": "Plotter",
                "route": "/plotter",
                "healthPath": "/plotter/healthz",
            }
        )
        payload["service"]["module"] = "plotter"
        payload["service"].update(
            {
                "frontendOutput": "public",
                "proxyPaths": ["/api"],
                "frontendSecurity": {
                    "connectSources": [
                        "https://api.maptiler.com",
                        "https://api.openrouteservice.org",
                    ],
                    "imgSources": ["blob:"],
                    "workerSources": ["blob:"],
                    "childSources": ["blob:"],
                },
            }
        )

        security = parse_manifest(json.dumps(payload)).service.frontend_security

        self.assertEqual(
            security.connect_sources,
            (
                "https://api.maptiler.com",
                "https://api.openrouteservice.org",
            ),
        )
        self.assertEqual(security.img_sources, ("blob:",))
        self.assertEqual(security.worker_sources, ("blob:",))
        self.assertEqual(security.child_sources, ("blob:",))

    def test_worker_and_child_frontend_sources_are_independently_optional(self):
        for key, attribute in (
            ("workerSources", "worker_sources"),
            ("childSources", "child_sources"),
        ):
            with self.subTest(key=key):
                payload = self.service_manifest_payload()
                payload["service"].update(
                    {
                        "frontendOutput": "public",
                        "proxyPaths": ["/api"],
                        "frontendSecurity": {key: ["blob:"]},
                    }
                )

                security = parse_manifest(
                    json.dumps(payload)
                ).service.frontend_security

                self.assertEqual(getattr(security, attribute), ("blob:",))
                other = (
                    "child_sources"
                    if attribute == "worker_sources"
                    else "worker_sources"
                )
                self.assertEqual(getattr(security, other), ())

    def test_rejects_unsafe_img_frontend_security_sources(self):
        unsafe_sources = (
            None,
            "data:",
            "file:",
            "javascript:",
            "http://images.example.test",
            "https://*.example.test",
            "https://images.example.test/path",
            "https://images.example.test?size=2",
            "https://images.example.test#icon",
            "https://user:password@images.example.test",
            "'unsafe-inline'",
            "BLOB:",
        )
        for source in unsafe_sources:
            with self.subTest(source=source):
                payload = self.service_manifest_payload()
                payload["service"].update(
                    {
                        "frontendOutput": "public",
                        "proxyPaths": ["/api"],
                        "frontendSecurity": {"imgSources": [source]},
                    }
                )

                with self.assertRaisesRegex(
                    ConfigError,
                    "service.frontendSecurity.imgSources must contain exact HTTPS origins or blob:",
                ):
                    parse_manifest(json.dumps(payload))

    def test_rejects_non_blob_worker_and_child_frontend_security_sources(self):
        unsafe_sources = (
            None,
            "data:",
            "file:",
            "javascript:",
            "http://workers.example.test",
            "https://workers.example.test",
            "https://workers.example.test/path",
            "https://workers.example.test?type=module",
            "https://workers.example.test#worker",
            "https://user:password@workers.example.test",
            "*",
            "'self'",
            "'unsafe-eval'",
            "BLOB:",
        )
        for key in ("workerSources", "childSources"):
            for source in unsafe_sources:
                with self.subTest(key=key, source=source):
                    payload = self.service_manifest_payload()
                    payload["service"].update(
                        {
                            "frontendOutput": "public",
                            "proxyPaths": ["/api"],
                            "frontendSecurity": {key: [source]},
                        }
                    )

                    with self.assertRaisesRegex(
                        ConfigError,
                        f"service.frontendSecurity.{key} must contain only blob:",
                    ):
                        parse_manifest(json.dumps(payload))

    def test_rejects_empty_and_duplicate_new_frontend_security_sources(self):
        invalid_cases = (
            (
                {"workerSources": []},
                "service.frontendSecurity.workerSources must be a non-empty list",
            ),
            (
                {"childSources": []},
                "service.frontendSecurity.childSources must be a non-empty list",
            ),
            (
                {"imgSources": "blob:"},
                "service.frontendSecurity.imgSources must be a non-empty list",
            ),
            (
                {"workerSources": "blob:"},
                "service.frontendSecurity.workerSources must be a non-empty list",
            ),
            (
                {"childSources": "blob:"},
                "service.frontendSecurity.childSources must be a non-empty list",
            ),
            (
                {"imgSources": ["https://images.example.test", "https://images.example.test:443"]},
                "service.frontendSecurity.imgSources contains duplicate sources",
            ),
            (
                {"workerSources": ["blob:", "blob:"]},
                "service.frontendSecurity.workerSources contains duplicate sources",
            ),
            (
                {"childSources": ["blob:", "blob:"]},
                "service.frontendSecurity.childSources contains duplicate sources",
            ),
        )
        for security, message in invalid_cases:
            with self.subTest(security=security):
                payload = self.service_manifest_payload()
                payload["service"].update(
                    {
                        "frontendOutput": "public",
                        "proxyPaths": ["/api"],
                        "frontendSecurity": security,
                    }
                )

                with self.assertRaisesRegex(ConfigError, message):
                    parse_manifest(json.dumps(payload))

    def test_rejects_unknown_empty_malformed_duplicate_and_non_frontend_security(self):
        invalid_cases = (
            (
                {"frontendOutput": "public", "proxyPaths": ["/api"], "frontendSecurity": {}},
                "service.frontendSecurity must declare at least one source directive",
            ),
            (
                {
                    "frontendOutput": "public",
                    "proxyPaths": ["/api"],
                    "frontendSecurity": {"scriptSources": ["https://cdn.example.test"]},
                },
                "service.frontendSecurity contains unknown key: scriptSources",
            ),
            (
                {
                    "frontendOutput": "public",
                    "proxyPaths": ["/api"],
                    "frontendSecurity": {"connectSources": []},
                },
                "service.frontendSecurity.connectSources must be a non-empty list",
            ),
            (
                {
                    "frontendOutput": "public",
                    "proxyPaths": ["/api"],
                    "frontendSecurity": {"imgSources": []},
                },
                "service.frontendSecurity.imgSources must be a non-empty list",
            ),
            (
                {
                    "frontendOutput": "public",
                    "proxyPaths": ["/api"],
                    "frontendSecurity": {"connectSources": "https://api.example.test"},
                },
                "service.frontendSecurity.connectSources must be a non-empty list",
            ),
            (
                {
                    "frontendOutput": "public",
                    "proxyPaths": ["/api"],
                    "frontendSecurity": {
                        "connectSources": [
                            "https://api.example.test",
                            "https://api.example.test:443",
                        ]
                    },
                },
                "service.frontendSecurity.connectSources contains duplicate origins",
            ),
            (
                {"frontendSecurity": {"connectSources": ["https://api.example.test"]}},
                "service.frontendSecurity requires a split service frontend",
            ),
        )
        for service_fields, message in invalid_cases:
            with self.subTest(service_fields=service_fields):
                payload = self.service_manifest_payload()
                payload["service"].update(service_fields)
                with self.assertRaisesRegex(ConfigError, message):
                    parse_manifest(json.dumps(payload))

    def test_rejects_unsafe_frontend_security_origins(self):
        unsafe_origins = (
            None,
            "http://api.example.test",
            "https://",
            "https://api.example.test/",
            "https://api.example.test/maps",
            "https://api.example.test?key=value",
            "https://api.example.test#fragment",
            "https://user:password@api.example.test",
            "https://*.example.test",
            "https://-api.example.test",
            "https://api..example.test",
            "https://api.example.test:0",
            "https://api.example.test:65536",
            "https://api.example.test 'unsafe-inline'",
            "data:",
            "blob:",
        )
        for index, origin in enumerate(unsafe_origins):
            with self.subTest(origin=origin):
                payload = self.service_manifest_payload()
                payload["service"].update(
                    {
                        "frontendOutput": "public",
                        "proxyPaths": ["/api"],
                        "frontendSecurity": {"connectSources": [origin]},
                    }
                )
                with self.assertRaisesRegex(
                    ConfigError,
                    "service.frontendSecurity.connectSources must contain exact HTTPS origins",
                ):
                    parse_manifest(json.dumps(payload), source=f"unsafe-origin-{index}")

    def test_loads_remote_backend_probe(self):
        registry = load_registry(self.write_json("apps.json", {
            "schemaVersion": 1,
            "host": "example-mac.local",
            "runtimeRoot": str(self.root / "runtime"),
            "apps": [{
                "id": "plotter",
                "repository": str(self.root),
                "autoDeploy": True,
                "backendProbe": {
                    "publicEnvironment": [
                        "VITE_SUPABASE_URL",
                        "VITE_SUPABASE_PUBLISHABLE_KEY",
                    ],
                    "baseUrlEnvironment": "VITE_SUPABASE_URL",
                    "path": "/auth/v1/health",
                    "headersFromEnvironment": {
                        "apikey": "VITE_SUPABASE_PUBLISHABLE_KEY",
                    },
                },
            }],
        }))

        probe = registry.apps[0].backend_probe
        self.assertEqual(
            probe.public_environment,
            ("VITE_SUPABASE_URL", "VITE_SUPABASE_PUBLISHABLE_KEY"),
        )
        self.assertEqual(probe.base_url_environment, "VITE_SUPABASE_URL")
        self.assertEqual(probe.path, "/auth/v1/health")
        self.assertEqual(probe.headers_from_environment, (("apikey", "VITE_SUPABASE_PUBLISHABLE_KEY"),))

    def test_rejects_invalid_split_service_contracts(self):
        cases = (
            {"frontendOutput": "public"},
            {"proxyPaths": ["/api"]},
            {"frontendOutput": "/public", "proxyPaths": ["/api"]},
            {"frontendOutput": "../public", "proxyPaths": ["/api"]},
            {"frontendOutput": "", "proxyPaths": ["/api"]},
            {"frontendOutput": ".", "proxyPaths": ["/api"]},
            {"frontendOutput": "./public", "proxyPaths": ["/api"]},
            {"frontendOutput": "public/.", "proxyPaths": ["/api"]},
            {"frontendOutput": "public//assets", "proxyPaths": ["/api"]},
            {"frontendOutput": "public/\x01assets", "proxyPaths": ["/api"]},
            {"frontendOutput": "public", "proxyPaths": []},
            {"frontendOutput": "public", "proxyPaths": "/api"},
            {"frontendOutput": "public", "proxyPaths": ["/"]},
            {"frontendOutput": "public", "proxyPaths": ["//api"]},
            {"frontendOutput": "public", "proxyPaths": ["/api/"]},
            {"frontendOutput": "public", "proxyPaths": ["/api?version=1"]},
            {"frontendOutput": "public", "proxyPaths": ["/api#fragment"]},
            {"frontendOutput": "public", "proxyPaths": ["/../api"]},
            {"frontendOutput": "public", "proxyPaths": ["/%2e%2e/api"]},
            {"frontendOutput": "public", "proxyPaths": ["/api", "/api"]},
        )
        for index, service_fields in enumerate(cases):
            with self.subTest(index=index):
                payload = self.service_manifest_payload()
                payload["service"].update(service_fields)
                with self.assertRaisesRegex(ConfigError, "service"):
                    parse_manifest(json.dumps(payload))

    def test_rejects_proxy_path_that_captures_public_health_path(self):
        payload = self.service_manifest_payload()
        payload["service"].update({"frontendOutput": "public", "proxyPaths": ["/healthz"]})

        with self.assertRaisesRegex(ConfigError, "proxyPaths"):
            parse_manifest(json.dumps(payload))

    def test_rejects_invalid_backend_probe_shapes_and_headers(self):
        forbidden_headers = (
            "Authorization", "Cookie", "Host", "Connection", "Proxy-Connection", "Forwarded",
            "X-Forwarded-For", "TE", "Trailer", "Transfer-Encoding", "Upgrade", "Keep-Alive",
        )
        invalid_probes = [
            {},
            {**self.backend_probe_payload(), "extra": True},
            {**self.backend_probe_payload(), "publicEnvironment": "VITE_SUPABASE_URL"},
            {**self.backend_probe_payload(), "publicEnvironment": ["VITE_SUPABASE_URL", "VITE_SUPABASE_URL"]},
            {**self.backend_probe_payload(), "headersFromEnvironment": []},
            {**self.backend_probe_payload(), "headersFromEnvironment": {"bad header": "VITE_SUPABASE_PUBLISHABLE_KEY"}},
            {**self.backend_probe_payload(), "path": "/"},
            {**self.backend_probe_payload(), "path": "//auth"},
            {**self.backend_probe_payload(), "path": "/auth/"},
            {**self.backend_probe_payload(), "path": "/auth?check=1"},
            {**self.backend_probe_payload(), "path": "/auth#check"},
            {**self.backend_probe_payload(), "path": "/../auth"},
            {**self.backend_probe_payload(), "path": "/%2e%2e/auth"},
        ]
        invalid_probes.extend(
            {**self.backend_probe_payload(), "headersFromEnvironment": {name: "VITE_SUPABASE_PUBLISHABLE_KEY"}}
            for name in forbidden_headers
        )
        for index, probe in enumerate(invalid_probes):
            with self.subTest(index=index):
                with self.assertRaisesRegex(ConfigError, "backendProbe"):
                    self.host_with_backend_probe(probe)

    def test_backend_probe_requires_compatible_public_build_inputs(self):
        manifest = parse_manifest(json.dumps(self.static_manifest_payload()))
        host = self.host_with_backend_probe()

        with self.assertRaisesRegex(ConfigError, "backendProbe"):
            validate_registered_app(host, manifest)

    def test_backend_probe_requires_every_public_name_to_be_referenced(self):
        manifest = parse_manifest(json.dumps(self.static_manifest_with_probe_inputs()))
        probe = self.backend_probe_payload()
        probe["publicEnvironment"].append("VITE_UNUSED_PUBLIC_INPUT")
        manifest_payload = self.static_manifest_with_probe_inputs()
        manifest_payload["build"]["environment"].append("VITE_UNUSED_PUBLIC_INPUT")
        host = self.host_with_backend_probe(probe)

        with self.assertRaisesRegex(ConfigError, "backendProbe"):
            validate_registered_app(host, parse_manifest(json.dumps(manifest_payload)))

        probe = self.backend_probe_payload()
        probe["publicEnvironment"].remove("VITE_SUPABASE_PUBLISHABLE_KEY")
        host = self.host_with_backend_probe(probe)
        with self.assertRaisesRegex(ConfigError, "backendProbe"):
            validate_registered_app(host, manifest)

    def test_backend_probe_is_rejected_for_service_apps(self):
        payload = self.service_manifest_payload()
        payload["id"] = "plotter"
        manifest = parse_manifest(json.dumps(payload))
        host = self.host_with_backend_probe()
        host = replace(
            host,
            port=8765,
            start_command=Command(("python3", "-m", "example_service")),
        )

        with self.assertRaisesRegex(ConfigError, "backendProbe"):
            validate_registered_app(host, manifest)

    def test_validates_and_resolves_only_declared_remote_probe_inputs(self):
        manifest = parse_manifest(json.dumps(self.static_manifest_with_probe_inputs()))
        host = self.host_with_backend_probe(
            environment=(
                "VITE_SUPABASE_URL=https://example.test:8443\n"
                "VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_fixture\n"
                "UNRELATED_PRIVATE_INPUT=not-for-probe\n"
            ),
        )

        validate_registered_app(host, manifest)
        environment = probe_environment(manifest, host)

        self.assertEqual(set(environment), {"VITE_SUPABASE_URL", "VITE_SUPABASE_PUBLISHABLE_KEY"})
        self.assertTrue(all(environment.values()))

    def test_backend_probe_requires_resolved_base_url_and_header_inputs(self):
        manifest = parse_manifest(json.dumps(self.static_manifest_with_probe_inputs()))
        for index, environment in enumerate((
            "VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_fixture\n",
            "VITE_SUPABASE_URL=https://example.test\n",
        )):
            with self.subTest(index=index):
                host = self.host_with_backend_probe(environment=environment)
                with self.assertRaisesRegex(ConfigError, "backendProbe"):
                    validate_registered_app(host, manifest)

    def test_probe_environment_file_read_failure_does_not_disclose_values(self):
        manifest = parse_manifest(json.dumps(self.static_manifest_with_probe_inputs()))
        host = self.host_with_backend_probe(
            environment=(
                "VITE_SUPABASE_URL=https://probe-value.example.test\n"
                "VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_probe_value\n"
            ),
        )
        host.environment_file.unlink()

        with self.assertRaises(ConfigError) as caught:
            probe_environment(manifest, host)

        self.assertNotIn("https://probe-value.example.test", str(caught.exception))
        self.assertNotIn("sb_publishable_probe_value", str(caught.exception))

    def test_rejects_invalid_remote_probe_values_without_disclosure(self):
        manifest = parse_manifest(json.dumps(self.static_manifest_with_probe_inputs()))
        invalid_values = (
            ("http://example.test\n", "sb_publishable_fixture\n"),
            ("https://user@example.test\n", "sb_publishable_fixture\n"),
            ("https://example.test/not-origin\n", "sb_publishable_fixture\n"),
            ("https://example.test?query=1\n", "sb_publishable_fixture\n"),
            ("https://example.test#fragment\n", "sb_publishable_fixture\n"),
            ("https://example.test:invalid\n", "sb_publishable_fixture\n"),
            ("https://example.test:65536\n", "sb_publishable_fixture\n"),
            ("https://example.test\n", "sb_secret_fixture\n"),
            ("https://example.test\n", "not-a-jwt\n"),
            ("https://example.test\n", "eyJhbGciOiJub25lIn0.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.\n"),
            ("https://example.test\n", "eyJhbGciOiJub25lIn0.eyJyb2xlIjoiYXV0aGVudGljYXRlZCJ9.\n"),
        )
        for index, (base_url, api_key) in enumerate(invalid_values):
            with self.subTest(index=index):
                host = self.host_with_backend_probe(
                    environment=(
                        f"VITE_SUPABASE_URL={base_url}"
                        f"VITE_SUPABASE_PUBLISHABLE_KEY={api_key}"
                    ),
                )
                secret_value = api_key.strip()
                with self.assertRaises(ConfigError) as caught:
                    validate_registered_app(host, manifest)
                self.assertIn("backendProbe", str(caught.exception))
                self.assertNotIn(base_url.strip(), str(caught.exception))
                self.assertNotIn(secret_value, str(caught.exception))

    def test_rejects_publishable_keys_that_can_change_caddy_syntax_without_disclosure(self):
        manifest = parse_manifest(json.dumps(self.static_manifest_with_probe_inputs()))
        hostile_suffixes = (
            "two words",
            "tab\tvalue",
            "line\rbreak",
            "line\r\nbreak",
            'quote"value',
            "open{brace",
            "close}brace",
            "comment#value",
            "first second third",
            "a" * 5000,
        )
        for index, suffix in enumerate(hostile_suffixes):
            with self.subTest(index=index):
                marker = f"sb_publishable_{suffix}"
                host = self.host_with_backend_probe(
                    environment=(
                        "VITE_SUPABASE_URL=https://example.test\n"
                        "VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_fixture\n"
                    )
                )
                host = replace(
                    host,
                    environment_file=None,
                    environment=(
                        ("VITE_SUPABASE_URL", "https://example.test"),
                        ("VITE_SUPABASE_PUBLISHABLE_KEY", marker),
                    ),
                )

                with self.assertRaises(ConfigError) as caught:
                    validate_registered_app(host, manifest)

                self.assertIn("backendProbe", str(caught.exception))
                self.assertNotIn(marker, str(caught.exception))

    def test_rejects_unsafe_non_apikey_probe_header_values_without_disclosure(self):
        manifest_payload = self.static_manifest_with_probe_inputs()
        manifest_payload["build"]["environment"].append("VITE_PROBE_VALUE")
        manifest = parse_manifest(json.dumps(manifest_payload))
        probe = self.backend_probe_payload()
        probe["publicEnvironment"].append("VITE_PROBE_VALUE")
        probe["headersFromEnvironment"]["X-Probe"] = "VITE_PROBE_VALUE"
        host = self.host_with_backend_probe(
            probe,
            environment=(
                "VITE_SUPABASE_URL=https://example.test\n"
                "VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_fixture\n"
                "VITE_PROBE_VALUE=safe\n"
            ),
        )
        marker = 'unsafe header #{value}'
        host = replace(
            host,
            environment_file=None,
            environment=(
                ("VITE_SUPABASE_URL", "https://example.test"),
                ("VITE_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_fixture"),
                ("VITE_PROBE_VALUE", marker),
            ),
        )

        with self.assertRaises(ConfigError) as caught:
            validate_registered_app(host, manifest)

        self.assertIn("backendProbe", str(caught.exception))
        self.assertNotIn(marker, str(caught.exception))

    def test_rejects_noncanonical_remote_probe_authorities_without_disclosure(self):
        manifest = parse_manifest(json.dumps(self.static_manifest_with_probe_inputs()))
        authorities = (
            "https://example.test%2fhidden",
            "https://example.test%5chidden",
            "https://example.test%3a443",
            "https://example.test%0acontrol",
            "https://example.test\\hidden",
            "https://example.test\x01control",
            "https://example{.test",
            "https://example}.test",
            'https://example".test',
        )
        for index, authority in enumerate(authorities):
            with self.subTest(index=index):
                host = self.host_with_backend_probe(
                    environment=(
                        f"VITE_SUPABASE_URL={authority}\n"
                        "VITE_SUPABASE_PUBLISHABLE_KEY=sb_publishable_fixture\n"
                    ),
                )
                with self.assertRaises(ConfigError) as caught:
                    validate_registered_app(host, manifest)
                self.assertIn("backendProbe", str(caught.exception))
                self.assertNotIn(authority, str(caught.exception))

    def test_rejects_non_base64url_jwt_probe_key_forms(self):
        manifest = parse_manifest(json.dumps(self.static_manifest_with_probe_inputs()))
        forms = (
            "eyJhbGciOiJub25lIn0=.eyJyb2xlIjoiYW5vbiJ9.c2ln",
            "eyJhbGciOiJub25lIn0.eyJyb2xlIjoiYW5vbiIsIngiOjEyfQ==.c2ln",
            "eyJ4Ijoi8J+YgCJ9.eyJyb2xlIjoiYW5vbiJ9.c2ln",
            "eyJhbGciOiJub25lIn0.eyJyb2xlIjoiYW5vbiJ9.++++",
        )
        for index, key in enumerate(forms):
            with self.subTest(index=index):
                host = self.host_with_backend_probe(
                    environment=(
                        "VITE_SUPABASE_URL=https://example.test\n"
                        f"VITE_SUPABASE_PUBLISHABLE_KEY={key}\n"
                    ),
                )
                with self.assertRaisesRegex(ConfigError, "backendProbe"):
                    validate_registered_app(host, manifest)

    def test_accepts_a_structurally_valid_anon_jwt_probe_key(self):
        manifest = parse_manifest(json.dumps(self.static_manifest_with_probe_inputs()))
        host = self.host_with_backend_probe(
            environment=(
                "VITE_SUPABASE_URL=https://example.test\n"
                "VITE_SUPABASE_PUBLISHABLE_KEY=eyJhbGciOiJub25lIn0.eyJyb2xlIjoiYW5vbiJ9.c2ln\n"
            ),
        )

        validate_registered_app(host, manifest)

    def test_build_environment_uses_allowlist_and_fixed_values_override_file(self):
        manifest_payload = self.static_manifest_payload()
        manifest_payload["build"]["environment"].append("VITE_PUBLIC_BASE_PATH")
        manifest_path = self.write_json("local-web.json", manifest_payload)
        environment_file = self.root / ".env"
        environment_file.write_text(
            "VITE_SUPABASE_URL=https://example.supabase.co\n"
            "VITE_PUBLIC_BASE_PATH=\n"
            "SERPAPI_API_KEY=not-exported\n",
            encoding="utf-8",
        )
        registry_path = self.write_json("apps.json", {
            "schemaVersion": 1,
            "host": "example-mac.local",
            "runtimeRoot": str(self.root / "runtime"),
            "apps": [{
                "id": "plotter",
                "repository": str(self.root),
                "autoDeploy": True,
                "environmentFile": str(environment_file),
                "environment": {"VITE_PUBLIC_BASE_PATH": "/plotter/"},
            }],
        })

        manifest = load_manifest(manifest_path)
        registry = load_registry(registry_path)
        host, _ = load_registered_app(registry, "plotter")
        env = build_environment(manifest, host, {"PATH": os.environ["PATH"], "SECRET": "hidden"})

        self.assertEqual(env["VITE_SUPABASE_URL"], "https://example.supabase.co")
        self.assertEqual(env["VITE_PUBLIC_BASE_PATH"], "/plotter/")
        self.assertNotIn("SERPAPI_API_KEY", env)
        self.assertIn("PATH", env)
        self.assertNotIn("SECRET", env)

    def test_rejects_static_service_port_80_and_fixed_secret_environment_name(self):
        static_with_service = self.static_manifest_payload()
        static_with_service["service"] = {
            "module": "plotter",
            "internalHealthPath": "/healthz",
        }
        with self.assertRaisesRegex(ConfigError, "service"):
            load_manifest(self.write_json("static-service.json", static_with_service))

        service_registry = {
            "schemaVersion": 1,
            "host": "example-mac.local",
            "runtimeRoot": str(self.root / "runtime"),
            "apps": [{
                "id": "service",
                "repository": str(self.root),
                "autoDeploy": True,
                "port": 80,
                "startCommand": ["python3", "-m", "example_service"],
            }],
        }
        with self.assertRaisesRegex(ConfigError, "port 80"):
            load_registry(self.write_json("port-80.json", service_registry))

        secret_environment = {
            "schemaVersion": 1,
            "host": "example-mac.local",
            "runtimeRoot": str(self.root / "runtime"),
            "apps": [{
                "id": "plotter",
                "repository": str(self.root),
                "autoDeploy": True,
                "environment": {"API_TOKEN": "not-allowed"},
            }],
        }
        with self.assertRaisesRegex(ConfigError, "API_TOKEN"):
            load_registry(self.write_json("secret-environment.json", secret_environment))

    def test_rejects_static_manifest_service_key_when_value_is_null(self):
        static_with_null_service = self.static_manifest_payload()
        static_with_null_service["service"] = None

        with self.assertRaisesRegex(ConfigError, "service"):
            load_manifest(self.write_json("static-null-service.json", static_with_null_service))

    def test_registry_rejects_unhashable_app_id_with_config_error(self):
        path = self.write_json("unhashable-id.json", {
            "schemaVersion": 1,
            "host": "example-mac.local",
            "runtimeRoot": str(self.root / "runtime"),
            "apps": [{
                "id": [],
                "repository": str(self.root),
                "autoDeploy": True,
            }],
        })

        with self.assertRaisesRegex(ConfigError, "app.id"):
            load_registry(path)

    def test_rejects_public_health_path_query_string(self):
        manifest = self.static_manifest_payload()
        manifest["healthPath"] = "/plotter/?probe=1"

        with self.assertRaisesRegex(ConfigError, "healthPath"):
            load_manifest(self.write_json("public-health-query.json", manifest))

    def test_rejects_browser_canonical_encoded_dot_traversal_in_public_health_path(self):
        encoded_dot_segments = (
            "%2e%2e",
            "%2E%2E",
            "%2e%2E",
            ".%2e",
            "%2E.",
        )

        for index, segment in enumerate(encoded_dot_segments):
            with self.subTest(segment=segment):
                manifest = self.static_manifest_payload()
                manifest["healthPath"] = f"/plotter/{segment}/samplealpha/api"
                with self.assertRaisesRegex(ConfigError, "healthPath"):
                    load_manifest(self.write_json(f"encoded-dot-{index}.json", manifest))

    def test_accepts_only_public_health_paths_that_browser_resolves_within_route(self):
        route_health_pairs = (
            ("/plotter", "/plotter/"),
            ("/samplebeta", "/samplebeta/"),
            ("/samplealpha", "/samplealpha/healthz"),
        )
        accepted = []
        for index, (route, health_path) in enumerate(route_health_pairs):
            manifest = self.static_manifest_payload()
            manifest["route"] = route
            manifest["healthPath"] = health_path
            accepted.append(
                load_manifest(self.write_json(f"accepted-health-{index}.json", manifest)).health_path
            )

        result = subprocess.run(
            [
                "node",
                "-e",
                (
                    "const fs=require('node:fs');"
                    "const paths=JSON.parse(fs.readFileSync(0,'utf8'));"
                    "process.stdout.write(JSON.stringify(paths.map((path)=>"
                    "new URL(path,'http://catalogue.local/').pathname)));"
                ),
            ],
            input=json.dumps(accepted),
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        canonical_paths = json.loads(result.stdout)
        self.assertEqual(canonical_paths, accepted)
        for (route, _health_path), canonical_path in zip(route_health_pairs, canonical_paths):
            self.assertTrue(
                canonical_path == route or canonical_path.startswith(f"{route}/"),
                canonical_path,
            )

    def test_rejects_noncanonical_public_health_path_forms(self):
        health_paths = (
            "/plotter/./healthz",
            "/plotter/%68ealthz",
            "/plotter/health\\status",
            "/plotter/health status",
            "/plotter/h\N{LATIN SMALL LETTER E WITH ACUTE}alth",
        )

        for index, health_path in enumerate(health_paths):
            with self.subTest(health_path=health_path):
                manifest = self.static_manifest_payload()
                manifest["healthPath"] = health_path
                with self.assertRaisesRegex(ConfigError, "healthPath"):
                    load_manifest(self.write_json(f"noncanonical-health-{index}.json", manifest))

    def test_rejects_internal_health_path_fragment(self):
        manifest = {
            "schemaVersion": 1,
            "id": "example-service",
            "title": "Example Service",
            "route": "/example-service",
            "kind": "service",
            "build": {"commands": [["python3", "-m", "build"]], "output": "public", "environment": []},
            "healthPath": "/example-service/healthz",
            "service": {"module": "example_service", "internalHealthPath": "/healthz#section"},
        }

        with self.assertRaisesRegex(ConfigError, "internalHealthPath"):
            load_manifest(self.write_json("internal-health-fragment.json", manifest))

    def test_registry_version_one_normalizes_existing_host_contract(self):
        payload = {
            "schemaVersion": 1,
            "host": "example-mac.local",
            "runtimeRoot": str(self.root / "runtime"),
            "apps": [],
        }

        registry = load_registry(self.write_json("registry-v1.json", payload))

        self.assertEqual(registry.schema_version, 1)
        self.assertEqual(registry.host, "example-mac.local")
        self.assertEqual(registry.public_origin, "http://example-mac.local")
        self.assertEqual(registry.ingress_mode, "trusted-lan")

    def test_registry_version_two_loads_one_canonical_tailscale_origin(self):
        payload = {
            "schemaVersion": 2,
            "publicOrigin": "https://local.example.ts.net",
            "ingressMode": "tailscale-serve",
            "runtimeRoot": str(self.root / "runtime"),
            "apps": [],
        }

        registry = load_registry(self.write_json("registry-v2.json", payload))

        self.assertEqual(registry.schema_version, 2)
        self.assertEqual(registry.host, "local.example.ts.net")
        self.assertEqual(registry.public_origin, "https://local.example.ts.net")
        self.assertEqual(registry.ingress_mode, "tailscale-serve")

    def test_registry_version_two_rejects_overlong_dns_names_at_the_public_boundary(self):
        # Per-label validation alone accepts a hostname that cannot be a valid
        # DNS name, letting an unusable canonical ingress reach installation.
        for final_length, valid in ((54, True), (55, False)):
            host = ".".join(("a" * 63, "b" * 63, "c" * 63, "d" * final_length, "ts", "net"))
            with self.subTest(host_length=len(host)):
                payload = {
                    "schemaVersion": 2,
                    "publicOrigin": f"https://{host}",
                    "ingressMode": "tailscale-serve",
                    "runtimeRoot": str(self.root / "runtime"),
                    "apps": [],
                }
                path = self.write_json("registry-dns-boundary.json", payload)
                if valid:
                    registry = load_registry(path)
                    self.assertEqual(len(registry.host), 253)
                    self.assertEqual(registry.public_origin, f"https://{host}")
                else:
                    with self.assertRaisesRegex(ConfigError, r"^registry\.publicOrigin is invalid$"):
                        load_registry(path)

    def test_registry_version_two_rejects_noncanonical_origins_and_shapes(self):
        tailnet_suffix = ".ts." + "net"
        valid = {
            "schemaVersion": 2,
            "publicOrigin": "https://local.example.ts.net",
            "ingressMode": "tailscale-serve",
            "runtimeRoot": str(self.root / "runtime"),
            "apps": [],
        }
        invalid_origins = (
            "",
            None,
            42,
            "HTTPS://local.example.ts.net",
            "https://LOCAL.example.ts.net",
            "http://local.example.ts.net",
            "https://local.example.com",
            "https://100.64.0.1",
            "https://local.example.ts.net:443",
            "https://user:password@local.example.ts.net",
            "https://local.example.ts.net/",
            "https://local.example.ts.net/path",
            "https://local.example.ts.net?query=value",
            "https://local.example.ts.net#fragment",
            "https://local.example.ts.net.",
            "https://local..example" + tailnet_suffix,
            "https://local\\example" + tailnet_suffix,
            "https://local%2eexample" + tailnet_suffix,
            " https://local.example.ts.net",
            "https://local.example.ts.net\x00",
            "https://local.example.ts.net\x80",
            "https://loc\N{LATIN SMALL LETTER A WITH ACUTE}l.example" + tailnet_suffix,
            "https://local" + tailnet_suffix,
            "https://-local.example.ts.net",
            "https://local-.example" + tailnet_suffix,
            "https://local_name.example" + tailnet_suffix,
            f"https://{'a' * 64}.example{tailnet_suffix}",
        )

        for index, public_origin in enumerate(invalid_origins):
            with self.subTest(publicOrigin=public_origin):
                payload = deepcopy(valid)
                payload["publicOrigin"] = public_origin
                with self.assertRaisesRegex(
                    ConfigError,
                    r"^registry\.publicOrigin is invalid$",
                ):
                    load_registry(self.write_json(f"invalid-origin-{index}.json", payload))

        shape_cases = (
            (
                {**valid, "ingressMode": "trusted-lan"},
                r"^registry\.ingressMode must be tailscale-serve$",
            ),
            (
                {key: value for key, value in valid.items() if key != "schemaVersion"},
                r"^registry is missing required key: schemaVersion$",
            ),
            (
                {key: value for key, value in valid.items() if key != "publicOrigin"},
                r"^registry is missing required key: publicOrigin$",
            ),
            (
                {key: value for key, value in valid.items() if key != "ingressMode"},
                r"^registry is missing required key: ingressMode$",
            ),
            (
                {key: value for key, value in valid.items() if key != "runtimeRoot"},
                r"^registry is missing required key: runtimeRoot$",
            ),
            (
                {key: value for key, value in valid.items() if key != "apps"},
                r"^registry is missing required key: apps$",
            ),
            (
                {**valid, "host": "example-mac.local"},
                r"^registry contains unknown key: host$",
            ),
            (
                {
                    "schemaVersion": 1,
                    "host": "example-mac.local",
                    "publicOrigin": valid["publicOrigin"],
                    "runtimeRoot": valid["runtimeRoot"],
                    "apps": [],
                },
                r"^registry contains unknown key: publicOrigin$",
            ),
        )

        for index, (payload, message) in enumerate(shape_cases):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ConfigError, message):
                    load_registry(self.write_json(f"invalid-shape-{index}.json", payload))

    def test_registry_requires_exact_supported_integer_schema_version(self):
        valid = {
            "schemaVersion": 1,
            "host": "example-mac.local",
            "runtimeRoot": str(self.root / "runtime"),
            "apps": [],
        }

        for value in (0, 3, "1", True):
            with self.subTest(schemaVersion=value):
                payload = deepcopy(valid)
                payload["schemaVersion"] = value
                with self.assertRaisesRegex(ConfigError, "schemaVersion"):
                    load_registry(self.write_json(f"schema-{value!r}.json", payload))

    def test_registry_rejects_relative_and_non_string_required_host_paths(self):
        valid = {
            "schemaVersion": 1,
            "host": "example-mac.local",
            "runtimeRoot": str(self.root / "runtime"),
            "apps": [{
                "id": "plotter",
                "repository": str(self.root / "repository"),
                "autoDeploy": True,
                "environmentFile": str(self.root / ".env"),
            }],
        }
        cases = (
            (("runtimeRoot",), "runtime"),
            (("runtimeRoot",), 42),
            (("apps", 0, "repository"), "repository"),
            (("apps", 0, "repository"), []),
            (("apps", 0, "environmentFile"), ".env"),
            (("apps", 0, "environmentFile"), False),
        )

        for index, (path, value) in enumerate(cases):
            with self.subTest(path=path, value=value):
                payload = deepcopy(valid)
                selected = payload
                for component in path[:-1]:
                    selected = selected[component]
                selected[path[-1]] = value
                with self.assertRaisesRegex(ConfigError, "absolute|non-empty string"):
                    load_registry(self.write_json(f"invalid-path-{index}.json", payload))
