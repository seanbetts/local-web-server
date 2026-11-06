"""NAS ingress shares the established Mac Caddy renderer and protections."""
from dataclasses import replace
import hashlib
from pathlib import Path
import unittest

from local_web_server.nas_models import NasTarget
from local_web_server.render import render_caddyfile
from tests import test_render


class NasIngressTests(unittest.TestCase):
    def setUp(self):
        fixture = test_render.RenderTests(); fixture.setUp()
        self.manifests = fixture.manifests
        self.mac = fixture.registry
        self.registry = replace(fixture.registry, host='local.example.ts.net',
                                public_origin='https://local.example.ts.net', ingress_mode='tailscale-serve',
                                runtime_root=Path('/volume2/local-web'),
                                target=NasTarget('nas-docker', ('/usr/local/bin/adapter',),
                                                 '172.30.200.0/24', '172.30.200.2'))

    def test_only_caddy_loopback_listener_in_sidecar_namespace(self):
        rendered = render_caddyfile(self.registry, self.manifests)
        self.assertIn('http://local.example.ts.net:8080 {\n    bind 127.0.0.1', rendered)
        self.assertIn('http://:8080 {\n    bind 127.0.0.1\n    respond 421', rendered)
        self.assertIn('trusted_proxies static 127.0.0.1/32', rendered)
        self.assertIn('trusted_proxies_strict', rendered)
        self.assertNotIn(':80 {', rendered)
        self.assertNotIn('reverse_proxy 127.0.0.1:', rendered)
        self.assertIn('reverse_proxy app-samplealpha:8765', rendered)

    def test_container_roots_share_atomic_platform_and_app_generations(self):
        rendered = render_caddyfile(self.registry, self.manifests)
        for path in ('/srv/apps/plotter/current', '/srv/apps/samplebeta/current',
                     '/srv/platform/current', '/srv/platform/current/index',
                     '/srv/platform/current/ui-gallery'):
            self.assertIn('root * "' + path + '"', rendered)
        self.assertNotIn('/volume2/', rendered)
        self.assertNotIn('/Users/', rendered)
        self.assertIn('rewrite * /home.html', rendered)

    def test_nas_reuses_same_security_cache_and_probe_directives_as_mac(self):
        rendered = render_caddyfile(self.registry, self.manifests)
        mac = render_caddyfile(self.mac, self.manifests)
        expected = ('header_up -*', 'header_down -*', 'header_down -Location',
                    'dial_timeout 5s', 'response_header_timeout 5s', 'Cache-Control "no-cache"',
                    'Cache-Control "public, max-age=31536000, immutable"', 'X-Content-Type-Options "nosniff"')
        for line in expected:
            self.assertIn(line, mac)
            self.assertIn(line, rendered)
        self.assertIn('LOCAL_WEB_PROBE_PLOTTER_VITE_SUPABASE_URL', rendered)

    def test_split_backend_preserves_csp_cache_and_private_upstream(self):
        split = replace(self.manifests['samplealpha'].service, frontend_output=Path('public'), proxy_paths=('/api',))
        manifests = self.manifests | {'samplealpha': replace(self.manifests['samplealpha'], service=split)}
        rendered = render_caddyfile(self.registry, manifests)
        self.assertIn('root * "/srv/apps/samplealpha/current/public"', rendered)
        self.assertIn('Content-Security-Policy "default-src', rendered)
        self.assertIn('Referrer-Policy "no-referrer"', rendered)
        self.assertIn('reverse_proxy app-samplealpha:8765', rendered)

    def test_mac_rendered_bytes_remain_unchanged(self):
        self.assertEqual(hashlib.sha256(render_caddyfile(self.mac, self.manifests).encode()).hexdigest(),
                         '477dce1f2d7c3458179927d3486c7343fd20107c026e4f84ed83fde598f7f4e1')
        tailscale = replace(self.mac, public_origin='https://local.example.ts.net',
                            host='local.example.ts.net', ingress_mode='tailscale-serve')
        self.assertEqual(hashlib.sha256(render_caddyfile(tailscale, self.manifests).encode()).hexdigest(),
                         'a67198c4fdad7cda745fcf4fd95de9496f446032952d7fac4a7dc9f40a668654')

    def test_nas_refuses_mac_port_migration_mode_and_lan_ingress(self):
        with self.assertRaises(ValueError):
            render_caddyfile(self.registry, self.manifests, prepare_tailscale_port_migration=True)
        with self.assertRaises(ValueError):
            render_caddyfile(replace(self.registry, ingress_mode='trusted-lan'), self.manifests)
