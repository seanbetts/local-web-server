"""Mac-side prepared NAS bundles, with disposable pinned source fixtures."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from local_web_server.nas_install import prepare_nas_platform
from local_web_server.install import load_main_manifests
from local_web_server.config import ConfigError
from unittest.mock import Mock
from local_web_server.nas_render import ContainerRuntimeSpec
from local_web_server.models import AppManifest, BuildSpec, HostApp, HostRegistry, ServiceSpec
from local_web_server.nas_models import NasTarget
from tests.helpers import init_git_repo


ROOT = Path(__file__).resolve().parents[1]
CADDY = 'example.invalid/caddy@sha256:' + 'b' * 64
TAILSCALE = 'example.invalid/tailscale@sha256:' + 'c' * 64


class RecordingRunner:
    def __init__(self): self.calls = []
    def run(self, repository, commands): self.calls.append((repository, commands))


class NasPlatformInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.repository = Path(self.temp.name) / 'source'; self.repository.mkdir()
        self.commit = init_git_repo(self.repository, {
            'platform_assets/theme.css': (ROOT / 'platform_assets/theme.css').read_text(),
            'apps/system-index/dist/index.html': '<html><script src="/_local-web/platform/index/assets/index.js"></script></html>',
            'apps/system-index/dist/assets/index.js': 'console.log("synthetic index");',
            'apps/system-index/dist/assets/index.css': ':root { color: black; }',
            'examples/ui-gallery/dist/index.html': '<html>synthetic gallery</html>',
            'examples/ui-gallery/dist/assets/gallery.js': 'console.log("synthetic gallery");',
        })
        self.registry = HostRegistry(
            host='local.example.ts.net', runtime_root=Path('/volume2/local-web'),
            apps=(HostApp('notes', Path('/Users/example/source'), False, None, (), 4174, None),),
            public_origin='https://local.example.ts.net', ingress_mode='tailscale-serve', schema_version=3,
            target=NasTarget('nas-docker', ('/usr/local/bin/adapter',), '172.30.200.0/24', '172.30.200.2'))
        self.manifests = {'notes': AppManifest(1, 'notes', 'Notes', '/notes', 'service',
                    BuildSpec((), Path('release'), ()), '/notes/healthz',
                    ServiceSpec('server', '/healthz', frontend_output=Path('public'), proxy_paths=('/api',)))}
        self.runtimes = (ContainerRuntimeSpec('notes', 'sha256:' + 'a' * 64,
                       ('node', '/app/current/server.js', '--port', '{port}'), 4174, 1.0, 512,
                       secret_files=('provider-key',), health_runtime='node'),)

    def prepare(self, **overrides):
        arguments = dict(repository=self.repository, registry=self.registry, manifests=self.manifests,
                         runtimes=self.runtimes, source_commit=self.commit, caddy_image=CADDY,
                         tailscale_image=TAILSCALE, build=False)
        arguments.update(overrides)
        return prepare_nas_platform(**arguments)

    def test_pinned_nas_service_without_mac_command_prepares_the_platform(self):
        source = Mock()
        source.main_commit.return_value = 'a' * 40
        source.manifest_at.return_value = self.manifests['notes']
        manifests = load_main_manifests(self.registry, repository_factory=lambda _: source)
        self.assertEqual(manifests, self.manifests)
        prepared = self.prepare(manifests=manifests)
        self.assertIn('compose.yaml', {entry.path for entry in prepared.files})
        with self.assertRaisesRegex(ConfigError, 'startCommand'):
            load_main_manifests(replace(self.registry, target=NasTarget('macos')),
                                repository_factory=lambda _: source)
        no_port = replace(self.registry, apps=(replace(self.registry.apps[0], port=None),))
        with self.assertRaisesRegex(ConfigError, 'port'):
            load_main_manifests(no_port, repository_factory=lambda _: source)

    def test_prepares_complete_immutable_platform_descriptor_without_target_writes(self):
        with patch('subprocess.Popen', side_effect=AssertionError('no runtime process')):
            # Git verification is required even for preview and uses the retained Git runner.
            with patch('local_web_server.nas_install._require_source'):
                with patch('local_web_server.nas_install._require_prebuilt_sources'):
                    prepared = self.prepare()
        files = {file.path: file for file in prepared.files}
        self.assertEqual(set(files), {'compose.yaml', 'Caddyfile', 'serve.json', 'home.html', 'theme.css',
                    'index/assets/index.js', 'index/assets/index.css', 'index/registry-v1.json',
                    'index/registry-v2.json', 'ui-gallery/index.html', 'ui-gallery/assets/gallery.js',
                    'runtime-settings.json'})
        self.assertEqual(prepared.descriptor.app_id, 'local-web-platform')
        self.assertEqual(prepared.descriptor.source_commit, self.commit)
        self.assertEqual(prepared.descriptor.data_contract, 'local-web-platform-v1')
        for entry in prepared.descriptor.files:
            self.assertEqual(entry.sha256, hashlib.sha256(files[entry.path].content).hexdigest())
            self.assertEqual(entry.size, len(files[entry.path].content))
        self.assertNotIn('/Users/', files['Caddyfile'].content.decode())
        compose = json.loads(files['compose.yaml'].content)
        self.assertIn('/etc/local-web/current/Caddyfile', compose['services']['caddy']['command'])
        serve = json.loads(files['serve.json'].content)
        host = 'local.example.ts.net:443'
        self.assertEqual(serve['Web'][host]['Handlers']['/']['Proxy'], 'http://127.0.0.1:8080')
        self.assertEqual(serve['AllowFunnel'], {host: False})
        self.assertTrue(all(not file.path.endswith('.plist') for file in prepared.files))

    def test_permissions_plan_separates_code_state_and_selected_secrets(self):
        plan = {entry.path: entry for entry in self.prepare().permissions}
        self.assertEqual(plan['.'].mode, 0o700)
        self.assertEqual(plan['apps/notes'].mode, 0o755)
        self.assertEqual(plan['apps/notes/generations'].mode, 0o755)
        self.assertEqual((plan['state/notes'].mode, plan['state/notes'].uid, plan['state/notes'].gid),
                         (0o700, 10001, 10001))
        self.assertEqual((plan['secrets/notes/provider-key'].mode, plan['secrets/notes/provider-key'].uid),
                         (0o400, 10001))

    def test_native_build_calls_existing_index_and_gallery_workflows_only(self):
        runner = RecordingRunner()
        self.prepare(build=True, runner=runner)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(tuple(command.argv for command in runner.calls[0][1]),
                         (('npm', 'run', 'build:index'), ('npm', 'run', 'build:gallery')))

    def test_untracked_ignored_prebuilt_outputs_cannot_be_mislabelled_as_source_commit(self):
        import subprocess
        # Model a real ignored dist bundle with no build provenance.
        subprocess.run(['git', 'rm', '--cached', '-r', 'apps/system-index/dist'],
                       cwd=self.repository, check=True, capture_output=True)
        (self.repository / '.gitignore').write_text('apps/system-index/dist/\n')
        subprocess.run(['git', 'add', '.gitignore'], cwd=self.repository, check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'synthetic ignored outputs'],
                       cwd=self.repository, check=True, capture_output=True)
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repository,
                                check=True, capture_output=True, text=True).stdout.strip()
        with self.assertRaises(ValueError): self.prepare(source_commit=commit)

    def test_source_commit_mismatch_and_dirty_or_untracked_installation_code_refused(self):
        with self.assertRaises(ValueError): self.prepare(source_commit='d' * 40)
        (self.repository / 'local_web_server').mkdir()
        (self.repository / 'local_web_server/new.py').write_text('# untracked installation source')
        with self.assertRaises(ValueError): self.prepare()

    def test_theme_or_bundle_failure_preserves_originals_and_produces_no_installation(self):
        sentinel = self.repository.parent / 'prior-platform'; sentinel.write_bytes(b'prior immutable release')
        # A bad source theme is committed: source pinning must pass and validation must refuse it.
        path = self.repository / 'platform_assets/theme.css'; path.write_text('invalid-theme')
        import subprocess
        subprocess.run(['git', 'add', '.'], cwd=self.repository, check=True, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'synthetic invalid theme'], cwd=self.repository, check=True, capture_output=True)
        commit = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=self.repository, check=True, capture_output=True, text=True).stdout.strip()
        with self.assertRaises(ValueError): self.prepare(source_commit=commit)
        self.assertEqual(sentinel.read_bytes(), b'prior immutable release')
        self.assertFalse((self.repository / 'prepared-nas').exists())

    def test_output_is_deterministic_for_the_same_reviewed_inputs(self):
        self.assertEqual(self.prepare(), self.prepare())

    def test_runtime_settings_pin_explicit_service_constraints_and_config_digest(self):
        prepared = self.prepare()
        files = {file.path: file for file in prepared.files}
        settings = json.loads(files['runtime-settings.json'].content)
        self.assertEqual(settings, dict(schemaVersion=1, publicOrigin=self.registry.public_origin,
            proxyAddress='172.30.200.2', networkSubnet='172.30.200.0/24', apps={
                'notes': dict(kind='service', port=4174, internalHealthPath='/healthz',
                              publicHealthPath='/notes/healthz', healthRuntime='node', user='10001:10001',
                              cpuLimit=1.0, memoryLimitMiB=512, secretFiles=['provider-key'])}))
        expected = {name: hashlib.sha256(files[name].content).hexdigest()
                    for name in ('compose.yaml', 'Caddyfile', 'serve.json', 'runtime-settings.json')}
        expected_digest = hashlib.sha256(json.dumps(expected, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(prepared.source_config_digest, expected_digest)
        self.assertNotIn('/Users/', files['runtime-settings.json'].content.decode())

    def test_static_apps_have_no_backend_or_secret_constraints(self):
        static = replace(self.manifests['notes'], id='site', route='/site', kind='static',
                         service=None, health_path='/site/')
        registry = replace(self.registry, apps=self.registry.apps +
                           (replace(self.registry.apps[0], id='site', port=None),))
        prepared = self.prepare(registry=registry, manifests=self.manifests | {'site': static})
        settings = json.loads(next(file.content for file in prepared.files if file.path == 'runtime-settings.json'))
        self.assertEqual(settings['apps']['site'], dict(kind='static', port=None, internalHealthPath=None,
            publicHealthPath='/site/', healthRuntime=None, user=None, cpuLimit=None, memoryLimitMiB=None,
            secretFiles=[]))
        permissions = {entry.path: entry for entry in prepared.permissions}
        self.assertEqual(permissions['apps/site'].mode, 0o755)
        self.assertNotIn('state/site', permissions)
