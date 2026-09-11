import hashlib
import json
import os
import plistlib
import socket
import subprocess
import tempfile
import time
import unittest
from http.client import HTTPConnection
from dataclasses import replace
from pathlib import Path

from local_web_server.index_registry import INDEX_ASSET_ROUTE, INDEX_REGISTRY_ROUTE
from local_web_server.config import ConfigError, parse_manifest
from local_web_server.models import (
    AppManifest,
    BackendProbeSpec,
    BuildSpec,
    Command,
    HostApp,
    HostRegistry,
    ServiceSpec,
)
from local_web_server.runtime import RuntimeLayout
from local_web_server.theme import THEME_ROUTE, ThemeStore
from local_web_server.render import (
    render_caddy_plist,
    render_caddyfile,
    render_hook,
    render_service_plist,
)
from local_web_server.public_origin import TAILSCALE_SERVE
from tests.helpers import TEST_CADDY, caddy_integration


CADDY = TEST_CADDY


class RenderTests(unittest.TestCase):
    def setUp(self):
        self.runtime_root = Path("/Users/example/Library/Application Support/LocalWebServer")
        self.registry = HostRegistry(
            host="example-mac.local",
            runtime_root=self.runtime_root,
            apps=(
                HostApp(
                    id="plotter",
                    repository=Path("/Users/example/Coding/plotter-private"),
                    auto_deploy=True,
                    environment_file=Path("/Users/example/Coding/plotter-private/.env"),
                    environment=(),
                    port=None,
                    start_command=None,
                    backend_probe=BackendProbeSpec(
                        public_environment=(
                            "VITE_SUPABASE_URL",
                            "VITE_SUPABASE_PUBLISHABLE_KEY",
                        ),
                        base_url_environment="VITE_SUPABASE_URL",
                        path="/auth/v1/health",
                        headers_from_environment=(("apikey", "VITE_SUPABASE_PUBLISHABLE_KEY"),),
                    ),
                ),
                HostApp(
                    id="samplealpha",
                    repository=Path("/Users/example/Coding/example-planning"),
                    auto_deploy=True,
                    environment_file=Path("/Users/example/Coding/example-planning/.env"),
                    environment=(("DATABASE_PATH", "/private/samplealpha.sqlite"),),
                    port=8765,
                    start_command=Command(("/opt/homebrew/bin/python3", "-m", "samplealpha", "--port", "8765")),
                ),
                HostApp(
                    id="samplebeta",
                    repository=Path("/Users/example/Coding/villa-shirt-collection"),
                    auto_deploy=True,
                    environment_file=None,
                    environment=(),
                    port=None,
                    start_command=None,
                ),
            ),
        )
        build = BuildSpec(
            commands=(Command(("true",)),),
            output=Path("public"),
            environment=("VITE_SUPABASE_URL", "VITE_SUPABASE_PUBLISHABLE_KEY"),
        )
        self.manifests = {
            "plotter": AppManifest(1, "plotter", "Plotter <public>", "/plotter", "static", build, "/plotter/", None),
            "samplealpha": AppManifest(
                1,
                "samplealpha",
                "Example Dashboard",
                "/samplealpha",
                "service",
                build,
                "/samplealpha/healthz",
                ServiceSpec(
                    module="samplealpha",
                    internal_health_path="/healthz",
                    frontend_output=Path("public"),
                    proxy_paths=("/api",),
                ),
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
            ),
        }

    def test_schema_v1_caddy_rendering_digest_is_pinned_for_generic_fixture(self):
        rendered = render_caddyfile(self.registry, self.manifests)

        self.assertEqual(
            hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            "3b58e5ae1ff942b154fc1b5c06a77efa274e3682b841a73498e1c07dcb4c0f42",
        )

    @caddy_integration
    def test_high_port_and_prepared_topologies_are_real_caddy_listeners(self):
        registry = replace(
            self.registry, host="local.example.ts.net", schema_version=2,
            public_origin="https://local.example.ts.net", ingress_mode=TAILSCALE_SERVE,
        )
        for prepared, expected in ((False, ["127.0.0.1:8080"]),
                                   (True, [":80", "127.0.0.1:8080"])):
            with self.subTest(prepared=prepared):
                content = render_caddyfile(
                    registry, self.manifests, prepare_tailscale_port_migration=prepared,
                )
                result = subprocess.run(
                    [str(CADDY), "adapt", "--adapter", "caddyfile", "--config", "-"],
                    input=content, capture_output=True, text=True, timeout=5,
                    env={**os.environ,
                         "LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_URL": "https://example.test",
                         "LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_fixture"},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                servers = json.loads(result.stdout)["apps"]["http"]["servers"]
                self.assertEqual(sorted(item for server in servers.values() for item in server["listen"]),
                                 sorted(expected))

    def test_preparation_cannot_render_for_trusted_lan(self):
        with self.assertRaisesRegex(ValueError, "Tailscale"):
            render_caddyfile(self.registry, self.manifests, prepare_tailscale_port_migration=True)

    def test_tailscale_ingress_binds_only_canonical_loopback_site(self):
        registry = replace(
            self.registry,
            host="local.example.ts.net",
            schema_version=2,
            public_origin="https://local.example.ts.net",
            ingress_mode=TAILSCALE_SERVE,
        )

        rendered = render_caddyfile(registry, self.manifests)

        self.assertTrue(
            rendered.startswith(
                "{\n"
                "    servers {\n"
                "        trusted_proxies static 127.0.0.1/32\n"
                "        trusted_proxies_strict\n"
                "    }\n"
                "}\n"
                "\n"
                "http://local.example.ts.net:8080 {\n"
                "    bind 127.0.0.1\n"
            )
        )
        self.assertEqual(rendered.count("http://local.example.ts.net:8080 {"), 1)
        self.assertNotIn("\n:80 {\n", rendered)
        for private_range in ("::1/128", "fc00::/7", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"):
            self.assertNotIn(private_range, rendered)
        self.assertIn("        handle_path /plotter/* {", rendered)

    def test_tailscale_ingress_keeps_external_probe_headers_stripped(self):
        registry = replace(
            self.registry,
            host="local.example.ts.net",
            schema_version=2,
            public_origin="https://local.example.ts.net",
            ingress_mode=TAILSCALE_SERVE,
        )

        rendered = render_caddyfile(registry, self.manifests)
        probe_start = rendered.index("handle /_local-web/health/plotter/backend")
        probe_end = rendered.index("    handle_path /plotter/*", probe_start)
        probe = rendered[probe_start:probe_end]

        self.assertIn("header_up -*", probe)
        self.assertIn("header_up apikey", probe)
        self.assertLess(probe.index("header_up -*"), probe.index("header_up apikey"))

    @staticmethod
    def service_manifest_with_frontend_security(security):
        return parse_manifest(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "id": "samplealpha",
                    "title": "Example Dashboard",
                    "route": "/samplealpha",
                    "kind": "service",
                    "build": {
                        "commands": [["true"]],
                        "output": "public",
                        "environment": [],
                    },
                    "healthPath": "/samplealpha/healthz",
                    "service": {
                        "module": "samplealpha",
                        "internalHealthPath": "/healthz",
                        "frontendOutput": "public",
                        "proxyPaths": ["/api"],
                        "frontendSecurity": security,
                    },
                }
            )
        )

    def test_caddy_serves_the_index_registry_before_assets_apps_and_home_fallback(self):
        rendered = render_caddyfile(self.registry, self.manifests)
        registry_handler = (
            f"        handle {INDEX_REGISTRY_ROUTE} {{\n"
            "            rewrite * /registry-v1.json\n"
            '            root * "/Users/example/Library/Application Support/LocalWebServer/platform/index"\n'
            '            header Cache-Control "no-cache"\n'
            '            header X-Content-Type-Options "nosniff"\n'
            "            file_server\n"
            "        }"
        )
        assets_handler = (
            f"        handle_path {INDEX_ASSET_ROUTE}/* {{\n"
            '            root * "/Users/example/Library/Application Support/LocalWebServer/platform/index"\n'
            '            header X-Content-Type-Options "nosniff"\n'
            "            file_server\n"
            "        }"
        )

        self.assertIn(registry_handler, rendered)
        self.assertIn(assets_handler, rendered)
        self.assertIn("        handle_path /plotter/* {", rendered)
        self.assertIn("        handle_path /samplebeta/* {", rendered)
        self.assertIn("        handle_path /samplealpha/* {", rendered)
        self.assertLess(rendered.index(registry_handler), rendered.index(assets_handler))
        self.assertLess(rendered.index(assets_handler), rendered.index("handle_path /plotter/*"))
        self.assertLess(rendered.index(assets_handler), rendered.index("handle_path /samplebeta/*"))
        self.assertLess(rendered.index(assets_handler), rendered.index("handle_path /samplealpha/*"))
        self.assertLess(rendered.index("handle_path /plotter/*"), rendered.rindex("        handle {\n"))
        self.assertLess(rendered.index("handle_path /samplebeta/*"), rendered.rindex("        handle {\n"))
        self.assertLess(rendered.index("handle_path /samplealpha/*"), rendered.rindex("        handle {\n"))

    def test_caddy_serves_ui_gallery_before_registered_apps_with_spa_fallback(self):
        rendered = render_caddyfile(self.registry, self.manifests)

        self.assertIn(
            '        handle_path /_local-web/platform/ui-gallery/* {\n'
            '            root * "/Users/example/Library/Application Support/LocalWebServer/platform/ui-gallery"\n'
            '            header Cache-Control "no-store"\n'
            '            header X-Content-Type-Options "nosniff"\n'
            '            try_files {path} /index.html\n'
            '            file_server\n'
            '        }',
            rendered,
        )
        self.assertLess(rendered.index("platform/ui-gallery"), rendered.index("handle_path /plotter/*"))

    def test_caddy_orders_split_service_handlers_before_the_global_home_handler(self):
        rendered = render_caddyfile(self.registry, self.manifests)

        self.assertIn(":80 {\n", rendered)
        self.assertIn("    redir /samplealpha /samplealpha/ 308\n", rendered)
        self.assertIn("    handle_path /samplealpha/* {\n", rendered)
        self.assertIn("        @samplealpha_health path /healthz\n", rendered)
        self.assertIn("        @samplealpha_proxy_0 path /api*\n", rendered)
        self.assertIn(
            '        root * "/Users/example/Library/Application Support/LocalWebServer/apps/samplealpha/current/public"\n',
            rendered,
        )
        self.assertIn("        try_files {path} /index.html\n", rendered)
        self.assertIn("        file_server\n", rendered)
        self.assertIn("        reverse_proxy 127.0.0.1:8765\n", rendered)
        self.assertLess(rendered.index("@samplealpha_health path /healthz"), rendered.index("@samplealpha_proxy_0 path /api*"))
        self.assertLess(rendered.index("@samplealpha_proxy_0 path /api*"), rendered.index("root * \"/Users/example/Library/Application Support/LocalWebServer/apps/samplealpha/current/public\""))
        self.assertLess(rendered.index("root * \"/Users/example/Library/Application Support/LocalWebServer/apps/samplealpha/current/public\""), rendered.rindex("    handle {\n"))

    def test_caddy_serves_the_exact_live_theme_file_before_every_app_route(self):
        rendered = render_caddyfile(self.registry, self.manifests)
        store = ThemeStore(self.runtime_root)
        handler = (
            f"handle {THEME_ROUTE} {{\n"
            "            rewrite * /theme.css\n"
            f'            root * "{store.platform_root}"\n'
            '            header Cache-Control "no-cache"\n'
            '            header X-Content-Type-Options "nosniff"\n'
            "            file_server\n"
            "        }"
        )

        self.assertIn(handler, rendered)
        self.assertEqual(store.platform_root / "theme.css", store.current)
        self.assertLess(rendered.index(f"handle {THEME_ROUTE}"), rendered.index("handle_path /plotter/*"))
        self.assertNotIn("plotter-private", rendered[rendered.index(f"handle {THEME_ROUTE}"):rendered.index("handle_path /plotter/*")])
        self.assertNotIn("--lwp-", rendered)

    @caddy_integration
    def test_disposable_caddy_serves_theme_store_current_at_the_public_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime = root / "runtime"
            content = (Path(__file__).parents[1] / "platform_assets" / "theme.css").read_bytes()
            store = ThemeStore(runtime)
            store.activate(content)
            runtime.joinpath("home.html").write_text("home", encoding="utf-8")
            registry = replace(self.registry, runtime_root=runtime)
            rendered = render_caddyfile(registry, self.manifests)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            rendered = "{\n    admin off\n}\n" + rendered.replace(
                ":80 {", f"http://127.0.0.1:{port} {{", 1
            )
            caddyfile = root / "Caddyfile"
            caddyfile.write_text(rendered, encoding="utf-8")
            environment = {
                **os.environ,
                "LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_URL": "https://example.test",
                "LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_fixture",
            }
            process = subprocess.Popen(
                [
                    str(CADDY),
                    "run",
                    "--config",
                    str(caddyfile),
                    "--adapter",
                    "caddyfile",
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                response = None
                body = b""
                for _attempt in range(50):
                    connection = None
                    try:
                        connection = HTTPConnection("127.0.0.1", port, timeout=0.2)
                        connection.request("GET", THEME_ROUTE)
                        response = connection.getresponse()
                        body = response.read()
                        break
                    except OSError:
                        time.sleep(0.02)
                    finally:
                        if connection is not None:
                            connection.close()
                self.assertIsNotNone(response)
                self.assertEqual(response.status, 200)
                self.assertEqual(body, store.current.read_bytes())
                self.assertEqual(response.getheader("Cache-Control"), "no-cache")
                self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    @caddy_integration
    def test_registered_frontends_revalidate_content_and_cache_hashed_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            runtime = root / "runtime"
            static_root = runtime / "apps" / "samplebeta" / "current"
            service_root = runtime / "apps" / "samplealpha" / "current" / "public"
            for frontend_root in (static_root, service_root):
                frontend_root.joinpath("assets").mkdir(parents=True)
                frontend_root.joinpath("index.html").write_text("app shell", encoding="utf-8")
                frontend_root.joinpath("assets", "app-abc123.js").write_text(
                    "console.log('asset')", encoding="utf-8"
                )
            static_root.joinpath("samplebeta.json").write_text("[]", encoding="utf-8")
            runtime.joinpath("home.html").write_text("home", encoding="utf-8")
            registry = replace(self.registry, runtime_root=runtime)
            rendered = render_caddyfile(registry, self.manifests)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            rendered = "{\n    admin off\n}\n" + rendered.replace(
                ":80 {", f"http://127.0.0.1:{port} {{", 1
            )
            caddyfile = root / "Caddyfile"
            caddyfile.write_text(rendered, encoding="utf-8")
            environment = {
                **os.environ,
                "LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_URL": "https://example.test",
                "LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_fixture",
            }
            process = subprocess.Popen(
                [
                    str(CADDY),
                    "run",
                    "--config",
                    str(caddyfile),
                    "--adapter",
                    "caddyfile",
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                expected_cache_control = {
                    "/samplebeta/": "no-cache",
                    "/samplebeta/samplebeta.json": "no-cache",
                    "/samplebeta/assets/app-abc123.js": "public, max-age=31536000, immutable",
                    "/samplealpha/": "no-cache",
                    "/samplealpha/assets/app-abc123.js": "public, max-age=31536000, immutable",
                }
                for path, expected in expected_cache_control.items():
                    response = None
                    for _attempt in range(50):
                        connection = None
                        try:
                            connection = HTTPConnection("127.0.0.1", port, timeout=0.2)
                            connection.request("HEAD", path)
                            response = connection.getresponse()
                            response.read()
                            break
                        except OSError:
                            time.sleep(0.02)
                        finally:
                            if connection is not None:
                                connection.close()
                    self.assertIsNotNone(response, path)
                    self.assertEqual(response.status, 200, path)
                    self.assertEqual(response.getheader("Cache-Control"), expected, path)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    def test_split_frontend_has_the_samplealpha_shell_security_policy_only_in_static_handler(self):
        rendered = render_caddyfile(self.registry, self.manifests)
        static_start = rendered.index('root * "/Users/example/Library/Application Support/LocalWebServer/apps/samplealpha/current/public"')
        static_end = rendered.index("            file_server", static_start)
        static_handler = rendered[static_start:static_end]
        api_handler = rendered[
            rendered.index("@samplealpha_proxy_0 path /api*"):static_start
        ]

        self.assertIn('X-Content-Type-Options "nosniff"', static_handler)
        self.assertIn('Referrer-Policy "no-referrer"', static_handler)
        self.assertIn("default-src 'self'", static_handler)
        self.assertIn("base-uri 'none'", static_handler)
        self.assertIn("frame-ancestors 'none'", static_handler)
        self.assertIn("form-action 'none'", static_handler)
        self.assertIn(
            'Content-Security-Policy "default-src \'self\'; connect-src \'self\'; '
            "img-src 'self' data:; script-src 'self'; style-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'\"",
            static_handler,
        )
        self.assertNotIn("Content-Security-Policy", api_handler)

    def test_split_frontend_security_extends_only_the_requested_directive_in_stable_order(self):
        payload = {
            "schemaVersion": 1,
            "id": "samplealpha",
            "title": "Example Dashboard",
            "route": "/samplealpha",
            "kind": "service",
            "build": {
                "commands": [["true"]],
                "output": "public",
                "environment": [],
            },
            "healthPath": "/samplealpha/healthz",
            "service": {
                "module": "samplealpha",
                "internalHealthPath": "/healthz",
                "frontendOutput": "public",
                "proxyPaths": ["/api"],
                "frontendSecurity": {
                    "connectSources": [
                        "https://api.openrouteservice.org",
                        "https://api.maptiler.com",
                    ]
                },
            },
        }
        try:
            samplealpha = parse_manifest(json.dumps(payload))
        except ConfigError as error:
            self.fail(f"valid frontend security declaration was rejected: {error}")
        manifests = {**self.manifests, "samplealpha": samplealpha}

        baseline = render_caddyfile(self.registry, self.manifests)
        rendered = render_caddyfile(self.registry, manifests)
        default_policy = (
            'Content-Security-Policy "default-src \'self\'; connect-src \'self\'; '
            "img-src 'self' data:; script-src 'self'; style-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'\""
        )
        extended_policy = (
            'Content-Security-Policy "default-src \'self\'; connect-src \'self\' '
            "https://api.maptiler.com https://api.openrouteservice.org; "
            "img-src 'self' data:; script-src 'self'; style-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'\""
        )

        self.assertEqual(rendered, baseline.replace(default_policy, extended_policy, 1))
        static_start = rendered.index(
            'root * "/Users/example/Library/Application Support/LocalWebServer/apps/samplealpha/current/public"'
        )
        static_end = rendered.index("            file_server", static_start)
        api_handler = rendered[rendered.index("@samplealpha_proxy_0 path /api*"):static_start]
        self.assertNotIn("Content-Security-Policy", api_handler)
        self.assertNotIn("api.maptiler.com", api_handler)
        self.assertEqual(rendered.count("https://api.maptiler.com"), 1)
        self.assertLess(
            rendered.index("https://api.maptiler.com", static_start, static_end),
            rendered.index("https://api.openrouteservice.org", static_start, static_end),
        )

    def test_split_frontend_security_can_extend_img_src_without_extending_connect_src(self):
        payload = {
            "schemaVersion": 1,
            "id": "samplealpha",
            "title": "Example Dashboard",
            "route": "/samplealpha",
            "kind": "service",
            "build": {
                "commands": [["true"]],
                "output": "public",
                "environment": [],
            },
            "healthPath": "/samplealpha/healthz",
            "service": {
                "module": "samplealpha",
                "internalHealthPath": "/healthz",
                "frontendOutput": "public",
                "proxyPaths": ["/api"],
                "frontendSecurity": {
                    "imgSources": ["https://images.example.test"]
                },
            },
        }
        try:
            samplealpha = parse_manifest(json.dumps(payload))
        except ConfigError as error:
            self.fail(f"valid frontend security declaration was rejected: {error}")

        rendered = render_caddyfile(
            self.registry,
            {**self.manifests, "samplealpha": samplealpha},
        )

        self.assertIn("connect-src 'self';", rendered)
        self.assertIn("img-src 'self' data: https://images.example.test;", rendered)
        self.assertNotIn("connect-src 'self' https://images.example.test", rendered)

    def test_map_frontend_security_renders_each_directive_in_stable_order(self):
        samplealpha = self.service_manifest_with_frontend_security(
            {
                "connectSources": [
                    "https://api.openrouteservice.org",
                    "https://api.maptiler.com",
                ],
                "imgSources": ["blob:"],
                "workerSources": ["blob:"],
                "childSources": ["blob:"],
            }
        )

        rendered = render_caddyfile(
            self.registry,
            {**self.manifests, "samplealpha": samplealpha},
        )
        policy = (
            'Content-Security-Policy "default-src \'self\'; connect-src \'self\' '
            "https://api.maptiler.com https://api.openrouteservice.org; "
            "img-src 'self' data: blob:; worker-src 'self' blob:; "
            "child-src 'self' blob:; script-src 'self'; style-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'\""
        )

        self.assertIn(policy, rendered)
        static_start = rendered.index(
            'root * "/Users/example/Library/Application Support/LocalWebServer/apps/samplealpha/current/public"'
        )
        api_handler = rendered[
            rendered.index("@samplealpha_proxy_0 path /api*"):static_start
        ]
        self.assertNotIn("Content-Security-Policy", api_handler)
        self.assertNotIn("blob:", api_handler)

    def test_worker_and_child_frontend_directives_render_independently(self):
        cases = (
            (
                {"workerSources": ["blob:"]},
                "img-src 'self' data:; worker-src 'self' blob:; script-src 'self';",
                "child-src",
            ),
            (
                {"childSources": ["blob:"]},
                "img-src 'self' data:; child-src 'self' blob:; script-src 'self';",
                "worker-src",
            ),
        )
        for security, expected, absent in cases:
            with self.subTest(security=security):
                samplealpha = self.service_manifest_with_frontend_security(security)

                rendered = render_caddyfile(
                    self.registry,
                    {**self.manifests, "samplealpha": samplealpha},
                )
                static_start = rendered.index(
                    'root * "/Users/example/Library/Application Support/LocalWebServer/apps/samplealpha/current/public"'
                )
                static_end = rendered.index("            file_server", static_start)
                static_handler = rendered[static_start:static_end]

                self.assertIn(expected, static_handler)
                self.assertNotIn(absent, static_handler)

    def test_existing_frontend_security_fields_keep_their_exact_rendered_policy(self):
        samplealpha = self.service_manifest_with_frontend_security(
            {
                "connectSources": ["https://api.example.test"],
                "imgSources": ["https://images.example.test"],
            }
        )

        rendered = render_caddyfile(
            self.registry,
            {**self.manifests, "samplealpha": samplealpha},
        )

        self.assertIn(
            'Content-Security-Policy "default-src \'self\'; connect-src \'self\' '
            "https://api.example.test; img-src 'self' data: "
            "https://images.example.test; script-src 'self'; style-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'\"",
            rendered,
        )
        self.assertNotIn("worker-src", rendered)
        self.assertNotIn("child-src", rendered)

    @caddy_integration
    def test_caddy_validates_the_map_frontend_security_policy(self):
        samplealpha = self.service_manifest_with_frontend_security(
            {
                "connectSources": [
                    "https://api.maptiler.com",
                    "https://api.openrouteservice.org",
                ],
                "imgSources": ["blob:"],
                "workerSources": ["blob:"],
                "childSources": ["blob:"],
            }
        )
        rendered = render_caddyfile(
            self.registry,
            {**self.manifests, "samplealpha": samplealpha},
        )

        with tempfile.NamedTemporaryFile("w", suffix=".caddyfile") as candidate:
            candidate.write(rendered)
            candidate.flush()
            result = subprocess.run(
                [
                    str(CADDY),
                    "validate",
                    "--config",
                    candidate.name,
                    "--adapter",
                    "caddyfile",
                ],
                env={
                    **os.environ,
                    "LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_URL": "https://example.test",
                    "LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_fixture",
                },
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)

    @caddy_integration
    def test_distinct_proxy_paths_have_collision_free_matchers_and_validate(self):
        samplealpha = self.manifests["samplealpha"]
        manifests = dict(self.manifests)
        manifests["samplealpha"] = replace(
            samplealpha,
            service=replace(samplealpha.service, proxy_paths=("/api-v1", "/api_v1")),
        )
        rendered = render_caddyfile(self.registry, manifests)

        self.assertIn("@samplealpha_proxy_0 path /api-v1*", rendered)
        self.assertIn("@samplealpha_proxy_1 path /api_v1*", rendered)
        with tempfile.NamedTemporaryFile("w", suffix=".caddyfile") as candidate:
            candidate.write(rendered)
            candidate.flush()
            environment = {
                **os.environ,
                "LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_URL": "https://example.test",
                "LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_fixture",
            }
            result = subprocess.run(
                [
                    str(CADDY),
                    "validate",
                    "--config",
                    candidate.name,
                    "--adapter",
                    "caddyfile",
                ],
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    @caddy_integration
    def test_disposable_split_frontend_is_body_free_secure_and_runtime_confined(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            public = runtime / "apps" / "samplealpha" / "current" / "public"
            package = runtime / "apps" / "samplealpha" / "current" / "samplealpha_tracker"
            public.mkdir(parents=True)
            package.mkdir()
            index = b"public-shell"
            public.joinpath("index.html").write_bytes(index)
            package.joinpath("private.py").write_bytes(b"runtime-package-private-marker")
            registry = replace(self.registry, runtime_root=runtime)
            rendered = render_caddyfile(registry, self.manifests)
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                port = reservation.getsockname()[1]
            rendered = "{\n    admin off\n}\n" + rendered.replace(
                ":80 {", f"http://127.0.0.1:{port} {{", 1
            )
            caddyfile = root / "Caddyfile"
            caddyfile.write_text(rendered, encoding="utf-8")
            environment = {
                **os.environ,
                "LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_URL": "https://example.test",
                "LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_PUBLISHABLE_KEY": "sb_publishable_fixture",
            }
            process = subprocess.Popen(
                [
                    str(CADDY),
                    "run",
                    "--config",
                    str(caddyfile),
                    "--adapter",
                    "caddyfile",
                ],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            try:
                response = None
                for _attempt in range(50):
                    connection = None
                    try:
                        connection = HTTPConnection("127.0.0.1", port, timeout=0.2)
                        connection.request("HEAD", "/samplealpha/samplealpha_tracker/private.py")
                        response = connection.getresponse()
                        response.read()
                        break
                    except OSError:
                        time.sleep(0.02)
                    finally:
                        if connection is not None:
                            connection.close()
                self.assertIsNotNone(response)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Length"), str(len(index)))
                self.assertEqual(response.getheader("Cache-Control"), "no-cache")
                self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
                self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")
                policy = response.getheader("Content-Security-Policy")
                self.assertIn("frame-ancestors 'none'", policy)
                self.assertIn("base-uri 'none'", policy)
                self.assertIn("form-action 'none'", policy)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

    def test_caddy_renders_the_reserved_probe_without_resolved_values_or_upstream_disclosure(self):
        resolved_url = "https://project.supabase.co"
        resolved_key = "sb_publishable_test_value"
        rendered = render_caddyfile(self.registry, self.manifests)

        self.assertIn("@plotter_backend_probe method HEAD", rendered)
        self.assertIn("/_local-web/health/plotter/backend", rendered)
        self.assertIn("method GET", rendered)
        self.assertIn("handle_response @probe_success", rendered)
        self.assertIn("respond 204", rendered)
        self.assertIn("respond 502", rendered)
        self.assertIn("handle_errors {", rendered)
        self.assertIn("respond @plotter_backend_probe_error 502", rendered)
        self.assertIn("respond 405", rendered)
        self.assertIn("{$LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_URL}", rendered)
        self.assertNotIn(resolved_url, rendered)
        self.assertNotIn(resolved_key, rendered)
        self.assertIn("header_up -*", rendered)
        self.assertLess(rendered.index("header_up -*"), rendered.index("header_up apikey"))
        self.assertEqual(rendered.count("header_up apikey"), 1)
        self.assertIn("header_down -Location", rendered)
        self.assertIn("header_down -*", rendered)
        self.assertLess(rendered.index("@plotter_backend_probe"), rendered.index("handle_path /plotter/*"))

    def test_caddy_routes_route_equal_split_service_health_before_frontend_redirect(self):
        manifests = dict(self.manifests)
        samplealpha = self.manifests["samplealpha"]
        manifests["samplealpha"] = AppManifest(
            samplealpha.schema_version,
            samplealpha.id,
            samplealpha.title,
            samplealpha.route,
            samplealpha.kind,
            samplealpha.build,
            samplealpha.route,
            samplealpha.service,
        )

        rendered = render_caddyfile(self.registry, manifests)

        self.assertIn("    route {\n", rendered)
        self.assertIn("        handle_path /samplealpha {\n", rendered)
        self.assertIn("        redir /samplealpha /samplealpha/ 308\n", rendered)
        self.assertLess(rendered.index("handle_path /samplealpha {"), rendered.index("redir /samplealpha /samplealpha/ 308"))
        self.assertNotIn("@samplealpha_health path \n", rendered)
        self.assertIn("    handle_path /samplealpha/* {\n", rendered)

    def test_caddy_wraps_handlers_in_an_order_preserving_route(self):
        rendered = render_caddyfile(self.registry, self.manifests)

        self.assertIn(
            "    route {\n"
            "        handle /_local-web/platform/theme.css {\n",
            rendered,
        )
        self.assertIn(
            "        }\n"
            "        handle /_local-web/health/plotter/backend {\n",
            rendered,
        )
        self.assertLess(
            rendered.index("handle /_local-web/platform/theme.css"),
            rendered.index("handle /_local-web/health/plotter/backend"),
        )
        self.assertLess(
            rendered.index("handle /_local-web/health/plotter/backend"),
            rendered.rindex("        handle {\n"),
        )

    def test_caddy_preserves_legacy_service_proxy_and_static_file_server(self):
        legacy_manifests = dict(self.manifests)
        build = self.manifests["samplealpha"].build
        legacy_manifests["samplealpha"] = AppManifest(
            1,
            "samplealpha",
            "Example Dashboard",
            "/samplealpha",
            "service",
            build,
            "/samplealpha/healthz",
            ServiceSpec(module="samplealpha", internal_health_path="/healthz"),
        )

        rendered = render_caddyfile(self.registry, legacy_manifests)

        self.assertIn("    handle_path /samplealpha/* {", rendered)
        self.assertEqual(rendered.count("reverse_proxy 127.0.0.1:8765"), 1)
        self.assertIn("    handle_path /plotter/* {", rendered)
        self.assertIn(
            'root * "/Users/example/Library/Application Support/LocalWebServer/apps/plotter/current"',
            rendered,
        )
        self.assertIn("        file_server\n", rendered)

    def test_renderer_rejects_split_frontend_root_that_is_not_a_release_descendant(self):
        samplealpha = self.manifests["samplealpha"]
        manifests = dict(self.manifests)
        manifests["samplealpha"] = replace(
            samplealpha,
            service=replace(samplealpha.service, frontend_output=Path(".")),
        )

        with self.assertRaisesRegex(ValueError, "frontend output"):
            render_caddyfile(self.registry, manifests)

    def test_caddy_plist_keeps_probe_values_exclusively_in_environment_variables(self):
        repository = Path("/Users/example/Coding/local-web-server")
        environment = (
            ("LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_URL", "https://project.supabase.co"),
            ("LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_PUBLISHABLE_KEY", "sb_publishable_test_value"),
        )
        try:
            plist_bytes = render_caddy_plist(repository, self.runtime_root, environment)
        except TypeError as error:
            self.fail(f"Caddy plist does not accept private environment values: {error}")
        rendered = plistlib.loads(plist_bytes)
        caddyfile = render_caddyfile(self.registry, self.manifests)
        self.assertEqual(rendered["EnvironmentVariables"], dict(environment))
        self.assertIn(b"https://project.supabase.co", plist_bytes)
        self.assertIn(b"sb_publishable_test_value", plist_bytes)
        self.assertNotIn("https://project.supabase.co", caddyfile)
        self.assertNotIn("sb_publishable_test_value", caddyfile)
        with self.assertRaises(ValueError):
            render_caddy_plist(repository, self.runtime_root, environment + (environment[0],))
        with self.assertRaises(ValueError):
            render_caddy_plist(repository, self.runtime_root, (("bad-name", "value"),))

    def test_caddy_routes_apps_before_a_final_home_handler_without_private_paths(self):
        rendered = render_caddyfile(self.registry, self.manifests)

        self.assertNotIn("browse", rendered)
        self.assertNotIn("plotter-private", rendered)
        self.assertNotIn("Example Planning", rendered)
        self.assertNotIn(".env", rendered)
        self.assertNotIn("samplealpha.sqlite", rendered)

    def test_caddy_plist_uses_absolute_caddy_command_and_stable_repository_directory(self):
        repository = Path("/Users/example/Coding/local-web-server")
        rendered = plistlib.loads(render_caddy_plist(repository, self.runtime_root))

        self.assertEqual(rendered["Label"], "com.sean.local-web.caddy")
        self.assertEqual(
            rendered["ProgramArguments"],
            ["/opt/homebrew/bin/caddy", "run", "--config", str(self.runtime_root / "Caddyfile")],
        )
        self.assertTrue(rendered["RunAtLoad"])
        self.assertTrue(rendered["KeepAlive"])
        self.assertEqual(rendered["WorkingDirectory"], str(repository))
        self.assertEqual(rendered["StandardOutPath"], str(self.runtime_root / "logs" / "caddy.out.log"))
        self.assertEqual(rendered["StandardErrorPath"], str(self.runtime_root / "logs" / "caddy.err.log"))

    def test_service_plist_uses_registered_command_current_release_and_app_logs(self):
        host = self.registry.apps[1]
        rendered = plistlib.loads(
            render_service_plist(host, self.manifests["samplealpha"], RuntimeLayout(self.runtime_root, "samplealpha"))
        )

        self.assertEqual(rendered["Label"], "com.sean.local-web.samplealpha")
        self.assertEqual(rendered["ProgramArguments"], list(host.start_command.argv))
        self.assertTrue(all(Path(argument).is_absolute() for argument in rendered["ProgramArguments"][:1]))
        self.assertTrue(rendered["RunAtLoad"])
        self.assertTrue(rendered["KeepAlive"])
        self.assertEqual(
            rendered["EnvironmentVariables"],
            {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        )
        self.assertEqual(rendered["WorkingDirectory"], str(self.runtime_root / "apps" / "samplealpha" / "current"))
        self.assertEqual(rendered["StandardOutPath"], str(self.runtime_root / "logs" / "services" / "samplealpha.out.log"))
        self.assertEqual(rendered["StandardErrorPath"], str(self.runtime_root / "logs" / "services" / "samplealpha.err.log"))

    def test_service_plist_preserves_expanded_repository_data_command_privately(self):
        layout = RuntimeLayout(self.runtime_root, "repository-data")
        host = HostApp(
            id="repository-data",
            repository=Path("/private/Repository With Spaces"),
            auto_deploy=True,
            environment_file=None,
            environment=(),
            port=52991,
            start_command=Command(
                (
                    "/usr/bin/node",
                    str(layout.current / "server/service.mjs"),
                    "--port",
                    "52991",
                    "--data-dir",
                    "/private/Repository With Spaces/data",
                )
            ),
        )
        manifest = replace(self.manifests["samplealpha"], id="repository-data")

        rendered = plistlib.loads(render_service_plist(host, manifest, layout))

        self.assertEqual(
            rendered["ProgramArguments"],
            [
                "/usr/bin/node",
                str(layout.current / "server/service.mjs"),
                "--port",
                "52991",
                "--data-dir",
                "/private/Repository With Spaces/data",
            ],
        )
        self.assertEqual(rendered["WorkingDirectory"], str(layout.current))
        self.assertEqual(
            rendered["EnvironmentVariables"],
            {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        )

    def test_hook_is_managed_main_only_safe_and_never_rejects_git(self):
        rendered = render_hook("example-service", Path("/Users/example/Coding/local-web-server"))

        self.assertIn("# managed-by-local-web-server", rendered)
        self.assertIn("branch=$(/usr/bin/git symbolic-ref --quiet --short HEAD 2>/dev/null) || exit 0", rendered)
        self.assertIn('[ "$branch" = main ] || exit 0', rendered)
        self.assertIn(
            "/opt/homebrew/bin/python3 /Users/example/Coding/local-web-server/bin/local-web deploy example-service --from-hook",
            rendered,
        )
        self.assertIn("Local web deployment failed for example-service; the Git operation remains successful.", rendered)
        self.assertTrue(rendered.endswith("exit 0\n"))
        self.assertNotIn("$APP_ID", rendered)
        self.assertNotIn("eval", rendered)

    def test_hook_rejects_invalid_app_id_before_interpolation(self):
        with self.assertRaises(ValueError):
            render_hook("example-service; rm -rf /", Path("/Users/example/Coding/local-web-server"))

    def test_hook_quotes_the_selected_platform_repository(self):
        rendered = render_hook(
            "example-service",
            Path("/Users/example/Coding/Local Web Server"),
        )

        self.assertIn(
            "/opt/homebrew/bin/python3 '/Users/example/Coding/Local Web Server/bin/local-web' "
            "deploy example-service --from-hook",
            rendered,
        )

    def test_hook_rejects_a_relative_platform_repository(self):
        with self.assertRaises(ValueError):
            render_hook("example-service", Path("local-web-server"))


if __name__ == "__main__":
    unittest.main()
