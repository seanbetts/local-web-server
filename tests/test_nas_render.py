"""Source-only NAS Compose contracts; these do not start any containers."""
from dataclasses import replace
import json
from pathlib import Path
import unittest

from local_web_server.models import AppManifest, BuildSpec, HostApp, HostRegistry, ServiceSpec
from local_web_server.nas_models import NasTarget
from local_web_server.nas_render import ContainerRuntimeSpec, render_nas_compose


IMAGE = 'example.invalid/runtime@sha256:' + 'a' * 64
CADDY = 'example.invalid/caddy@sha256:' + 'b' * 64
TAILSCALE = 'example.invalid/tailscale@sha256:' + 'c' * 64


class NasRenderTests(unittest.TestCase):
    def setUp(self):
        self.registry = HostRegistry(
            host='local.example.ts.net', runtime_root=Path('/volume2/local-web'),
            apps=tuple(HostApp(name, Path('/Users/example/source') / name, False, None, (), port, None)
                       for name, port in [('notes', 4174), ('site', None)]),
            schema_version=3, public_origin='https://local.example.ts.net', ingress_mode='tailscale-serve',
            target=NasTarget('nas-docker', ('/usr/local/bin/adapter',), '172.30.200.0/24', '172.30.200.2'))
        build = BuildSpec((), Path('release'), ())
        self.manifests = {
            'notes': AppManifest(1, 'notes', 'Notes', '/notes', 'service', build,
                                 '/notes/healthz', ServiceSpec('server', '/healthz')),
            'site': AppManifest(1, 'site', 'Site', '/site', 'static', build, '/site/', None),
        }
        self.runtime = ContainerRuntimeSpec(
            app_id='notes', image=IMAGE,
            command=('node', '/app/current/server.mjs', '--port', '{port}', '--data-dir', '{state}',
                     '--bind', '{bindHost}', '--origin', '{publicOrigin}', '--proxy', '{proxyAddress}'),
            port=4174, cpu_limit=1.0, memory_limit_mib=512, secret_files=('provider-key',), health_runtime='node')

    def render(self, **overrides):
        values = dict(registry=self.registry, manifests=self.manifests, runtimes=(self.runtime,),
                      caddy_image=CADDY, tailscale_image=TAILSCALE)
        values.update(overrides)
        return json.loads(render_nas_compose(**values))

    def test_front_door_uses_userspace_persistent_identity_and_shared_namespace(self):
        document = self.render()
        services = document['services']
        sidecar, caddy = services['tailscale'], services['caddy']
        self.assertEqual(sidecar['environment']['TS_USERSPACE'], 'true')
        self.assertEqual(sidecar['environment']['TS_STATE_DIR'], '/var/lib/tailscale')
        self.assertEqual(sidecar['environment']['TS_SERVE_CONFIG'], '/etc/local-web/current/serve.json')
        self.assertEqual(caddy['network_mode'], 'service:tailscale')
        self.assertEqual(sidecar['networks']['local-web']['ipv4_address'], '172.30.200.2')
        self.assertEqual(document['networks']['local-web']['ipam']['config'],
                         [{'subnet': '172.30.200.0/24', 'gateway': '172.30.200.1'}])
        self.assertIn('/etc/local-web/current/Caddyfile', caddy['command'])
        self.assertTrue(any(m['target'] == '/var/lib/tailscale' and not m['read_only']
                            for m in sidecar['volumes']))
        self.assertNotIn('TS_AUTHKEY', sidecar['environment'])
        for service in services.values():
            self.assertNotIn('ports', service)
            self.assertNotIn('privileged', service)
            self.assertNotIn('devices', service)
            self.assertEqual(service['security_opt'], ['no-new-privileges:true'])
            self.assertEqual(service['cap_drop'], ['ALL'])

    def test_backend_has_stable_readonly_code_parent_and_only_own_state_secrets(self):
        backend = self.render()['services']['app-notes']
        self.assertEqual(backend['entrypoint'], ['node', '/app/current/server.mjs', '--port', '4174',
                         '--data-dir', '/state', '--bind', '0.0.0.0', '--origin', self.registry.public_origin,
                         '--proxy', self.registry.target.proxy_address])
        self.assertEqual(backend['command'], [])
        self.assertEqual(backend['working_dir'], '/app/current')
        self.assertEqual(backend['platform'], 'linux/amd64')
        self.assertTrue(backend['read_only'])
        self.assertEqual(backend['cpus'], 1.0)
        self.assertEqual(backend['mem_limit'], '512m')
        bindings = {m['target']: m for m in backend['volumes']}
        self.assertEqual(set(bindings), {'/app', '/state', '/run/secrets/provider-key'})
        self.assertEqual(bindings['/app']['source'], '/volume2/local-web/apps/notes')
        self.assertTrue(bindings['/app']['read_only'])
        self.assertEqual(bindings['/state']['source'], '/volume2/local-web/state/notes')
        self.assertFalse(bindings['/state']['read_only'])
        self.assertTrue(bindings['/run/secrets/provider-key']['read_only'])
        self.assertEqual(backend['user'], '10001:10001')
        rendered = render_nas_compose(self.registry, self.manifests, (self.runtime,),
                                     caddy_image=CADDY, tailscale_image=TAILSCALE)
        self.assertNotIn('/Users/', rendered)
        self.assertNotIn('docker.sock', rendered)

    def test_caddy_shares_readonly_generation_parents_without_state_or_secrets(self):
        document = self.render()
        self.assertNotIn('app-site', document['services'])
        bindings = document['services']['caddy']['volumes']
        self.assertEqual({m['source'] for m in bindings},
                         {'/volume2/local-web/apps', '/volume2/local-web/apps/local-web-platform'})
        self.assertEqual({m['target'] for m in bindings}, {'/srv/apps', '/srv/platform', '/etc/local-web'})
        self.assertTrue(all(m['read_only'] for m in bindings))
        self.assertTrue(all(not m['bind']['create_host_path'] for m in bindings))

    def test_output_is_deterministic_and_runtime_order_independent(self):
        second = replace(self.runtime, app_id='tasks', port=4175, secret_files=())
        registry = replace(self.registry, apps=self.registry.apps +
                           (replace(self.registry.apps[0], id='tasks', port=4175),))
        manifests = self.manifests | {'tasks': replace(self.manifests['notes'], id='tasks', route='/tasks')}
        arguments = dict(registry=registry, manifests=manifests, caddy_image=CADDY, tailscale_image=TAILSCALE)
        self.assertEqual(render_nas_compose(runtimes=(second, self.runtime), **arguments),
                         render_nas_compose(runtimes=(self.runtime, second), **arguments))

    def test_proxy_address_cannot_collide_with_bridge_gateway(self):
        registry = replace(self.registry, target=replace(self.registry.target, proxy_address='172.30.200.1'))
        document = self.render(registry=registry)
        self.assertEqual(document['networks']['local-web']['ipam']['config'][0]['gateway'], '172.30.200.2')

    def test_prepared_runtime_can_use_exact_runnable_docker_config_id(self):
        identity = 'sha256:' + 'd' * 64
        document = self.render(runtimes=(replace(self.runtime, image=identity),))
        self.assertEqual(document['services']['app-notes']['image'], identity)

    def test_only_public_probe_environment_is_forwarded_to_caddy(self):
        environment = (('LOCAL_WEB_PROBE_NOTES_VITE_PROVIDER_URL', 'https://provider.example'),)
        document = self.render(caddy_environment=environment)
        self.assertEqual(document['services']['caddy']['environment'], dict(environment))
        for bad in ((('SECRET_API_KEY', 'private'),), (('LOCAL_WEB_PROBE_NOTES_KEY', '$HOME'),),
                    environment + environment):
            with self.subTest(environment=bad), self.assertRaises(ValueError):
                self.render(caddy_environment=bad)

    def test_health_runtime_is_explicit_and_limited_to_supported_probe_binaries(self):
        with self.assertRaises(TypeError):
            ContainerRuntimeSpec('notes', IMAGE, ('node', '/app/current/server.mjs'), 4174, 1.0, 512)
        with self.assertRaises(ValueError):
            replace(self.runtime, health_runtime='shell')
        self.assertEqual(replace(self.runtime, health_runtime='python3').health_runtime, 'python3')

    def test_small_subnet_without_room_for_backends_is_refused(self):
        registry = replace(self.registry, target=replace(self.registry.target, network_subnet='172.30.200.0/30'))
        with self.assertRaises(ValueError):
            self.render(registry=registry)

    def test_macos_paths_unknown_placeholders_shell_expansion_and_unpinned_images_refused(self):
        for change in (
            dict(command=('python', '{repository}/server.py')),
            dict(command=('/Users/example/.venv/bin/python', 'server.py')),
            dict(command=('node', '{release}/service.mjs')),
            dict(command=('node', '$HOME/server.js')),
            dict(command=('node', '/app/current/{unknown}')),
            dict(image='example/runtime:latest'),
            dict(secret_files=('../other/key',)),
            dict(secret_files=('key', 'key')),
            dict(user='0:0'), dict(cpu_limit=0), dict(memory_limit_mib=0), dict(port=0),
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.render(runtimes=(replace(self.runtime, **change),))
        with self.assertRaises(ValueError):
            self.render(caddy_image='caddy:latest')
        with self.assertRaises(ValueError):
            self.render(tailscale_image='tailscale:latest')

    def test_missing_duplicate_unknown_or_static_backend_recipe_is_refused(self):
        for runtimes in ((), (self.runtime, self.runtime),
                         (replace(self.runtime, app_id='unknown'),),
                         (self.runtime, replace(self.runtime, app_id='site'))):
            with self.subTest(runtimes=runtimes), self.assertRaises(ValueError):
                self.render(runtimes=runtimes)
        with self.assertRaises(ValueError):
            self.render(manifests={'notes': self.manifests['notes']})

    def test_unsafe_target_or_noncanonical_origin_is_refused(self):
        for registry in (
            replace(self.registry, target=NasTarget()),
            replace(self.registry, ingress_mode='trusted-lan'),
            replace(self.registry, public_origin='http://local.example.ts.net'),
            replace(self.registry, public_origin='https://user:password@local.example.ts.net'),
            replace(self.registry, public_origin='https://other.example.test'),
            replace(self.registry, public_origin='https://local.example.ts.net#'),
            replace(self.registry, runtime_root=Path('/Users/example/runtime')),
            replace(self.registry, runtime_root=Path('/volume2/local-web/../other')),
            replace(self.registry, target=replace(self.registry.target, proxy_address='172.30.201.2')),
        ):
            with self.subTest(registry=registry), self.assertRaises(ValueError):
                self.render(registry=registry)


if __name__ == '__main__':
    unittest.main()
