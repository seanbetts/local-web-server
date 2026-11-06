import json
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

from local_web_server.models import Command, HostApp
from local_web_server.native_build import NativePlatformIdentity
from local_web_server.nas_workflow import NasReleasePreparer
from local_web_server.release_manifest import ReleaseDescriptorV1, snapshot_files
from tests.helpers import init_git_repo
from tests.test_production_build import docker_archive


class NasReleasePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / 'repository'
        self.repository.mkdir()

    def source(self, *, production=True):
        install = "import os; from pathlib import Path; assert 'VITE_PUBLIC_BASE_PATH' not in os.environ; Path('node_modules').mkdir(); Path('node_modules/prepared').write_text('cached')"
        build = "import os; from pathlib import Path; assert os.environ['VITE_PUBLIC_BASE_PATH']=='/example/'; assert Path('node_modules/prepared').read_text()=='cached'; Path('dist').mkdir(); Path('dist/index.html').write_text('compiled')"
        manifest = dict(schemaVersion=1, id='example', title='Example', route='/example', kind='static',
                        build=dict(commands=[[sys.executable, '-c', "raise RuntimeError('legacy installer must not run')"]],
                                   output='dist', environment=['VITE_PUBLIC_BASE_PATH']), healthPath='/example/')
        if production:
            manifest['production'] = dict(recipeVersion='1', dataContract='none', rollbackPolicy='compatible', runtime=None,
                buildCommands=[[sys.executable, '-c', build]], nativeDependencies=dict(
                    lockfiles=['package-lock.json'], installCommands=[[sys.executable, '-c', install]], outputs=['node_modules']))
        commit = init_git_repo(self.repository, {'local-web.json': json.dumps(manifest), 'package-lock.json': '{}'})
        host = HostApp('example', self.repository, False, None, (('VITE_PUBLIC_BASE_PATH', '/example/'),), None, None)
        return host, commit

    def test_preparation_runs_explicit_native_recipe_and_reuses_dependencies(self):
        host, commit = self.source()
        preparer = NasReleasePreparer(self.root/'cache', native_platform=NativePlatformIdentity('darwin', 'arm64', '137'))
        first = preparer.prepare(host, commit=commit, destination=self.root/'first')
        second = preparer.prepare(host, commit=commit, destination=self.root/'second')
        self.assertFalse(first.native_cache_hit)
        self.assertTrue(second.native_cache_hit)
        self.assertEqual(first.descriptor.digest, second.descriptor.digest)
        self.assertEqual((second.files_root/'index.html').read_text(), 'compiled')
        self.assertEqual(second.descriptor.files, snapshot_files(second.files_root))
        self.assertEqual(ReleaseDescriptorV1.from_dict(json.loads((second.root/'descriptor.json').read_text())), second.descriptor)
        self.assertFalse((self.repository/'node_modules').exists())
        self.assertFalse((self.repository/'dist').exists())
        self.assertIsNone(second.runtime_archive)
        self.assertIsNone(second.runtime_spec)

    def test_legacy_app_refuses_before_build_or_destination_creation(self):
        host, commit = self.source(production=False)
        target = self.root/'prepared'
        with self.assertRaisesRegex(ValueError, 'production recipe'):
            NasReleasePreparer(self.root/'cache').prepare(host, commit=commit, destination=target)
        self.assertFalse(target.exists())

    def test_replace_refs_cannot_substitute_a_different_build_recipe(self):
        host, original = self.source()
        import subprocess
        value = json.loads((self.repository/'local-web.json').read_text())
        value['production']['buildCommands'] = [[sys.executable, '-c', 'raise RuntimeError("replacement recipe")']]
        (self.repository/'local-web.json').write_text(json.dumps(value))
        subprocess.run(['git', '-C', str(self.repository), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repository), 'commit', '-qm', 'replacement'], check=True)
        replacement = subprocess.check_output(['git', '-C', str(self.repository), 'rev-parse', 'HEAD'], text=True).strip()
        subprocess.run(['git', '-C', str(self.repository), 'replace', original, replacement], check=True)
        prepared = NasReleasePreparer(self.root/'cache', native_platform=NativePlatformIdentity('darwin', 'arm64', '137')).prepare(
            host, commit=original, destination=self.root/'prepared')
        self.assertEqual(prepared.descriptor.source_commit, original)
        self.assertEqual((prepared.files_root/'index.html').read_text(), 'compiled')

    def test_failed_native_build_never_publishes_candidate(self):
        host, commit = self.source()
        value = json.loads((self.repository/'local-web.json').read_text())
        value['production']['buildCommands'] = [[sys.executable, '-c', 'raise RuntimeError("failed")']]
        (self.repository/'local-web.json').write_text(json.dumps(value))
        import subprocess
        subprocess.run(['git', '-C', str(self.repository), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repository), 'commit', '-qm', 'break fixture build'], check=True)
        commit = subprocess.check_output(['git', '-C', str(self.repository), 'rev-parse', 'HEAD'], text=True).strip()
        with self.assertRaises(ValueError):
            NasReleasePreparer(self.root/'cache', native_platform=NativePlatformIdentity('darwin', 'arm64', '137')).prepare(
                host, commit=commit, destination=self.root/'prepared')
        self.assertFalse((self.root/'prepared').exists())

    def test_linux_dependency_build_receives_only_keyed_inputs_and_is_reused(self):
        host, _ = self.source()
        manifest = json.loads((self.repository/'local-web.json').read_text())
        manifest.update(kind='service', healthPath='/example/healthz', service=dict(module='example', internalHealthPath='/healthz'))
        manifest['production'].update(dataContract='fixture-v1', runtime=dict(
            baseImage='python@sha256:'+'b'*64, abi='python314', lockfiles=['requirements.lock'],
            dockerfile='Dockerfile.runtime', command=['/bin/true'], cpuLimit=1, memoryLimitMib=256, secretFiles=[], healthRuntime='python3'))
        (self.repository/'local-web.json').write_text(json.dumps(manifest))
        (self.repository/'requirements.lock').write_text('locked fixture')
        (self.repository/'Dockerfile.runtime').write_text('ARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE}\n')
        import subprocess
        subprocess.run(['git', '-C', str(self.repository), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(self.repository), 'commit', '-qm', 'service fixture'], check=True)
        commit = subprocess.check_output(['git', '-C', str(self.repository), 'rev-parse', 'HEAD'], text=True).strip()
        host = replace(host, port=43123, start_command=Command(('/usr/bin/true',)))
        calls = []

        def install(source, dockerfile, key, archive, base_image):
            calls.append(key)
            self.assertEqual({str(path.relative_to(source)) for path in source.rglob('*') if path.is_file()},
                             {'requirements.lock', 'Dockerfile.runtime'})
            self.assertEqual(base_image, manifest['production']['runtime']['baseImage'])
            docker_archive(archive)

        preparer = NasReleasePreparer(self.root/'cache', dependency_installer=install,
                                     native_platform=NativePlatformIdentity('darwin', 'arm64', '137'))
        first = preparer.prepare(host, commit=commit, destination=self.root/'first')
        second = preparer.prepare(host, commit=commit, destination=self.root/'second')
        self.assertEqual(len(calls), 1)
        self.assertFalse(first.runtime_cache_hit)
        self.assertTrue(second.runtime_cache_hit)
        self.assertEqual(first.descriptor.digest, second.descriptor.digest)
        self.assertEqual(second.runtime_spec.image, second.descriptor.runtime.image_digest)
        metadata = json.loads((second.files_root/'.local-web-runtime.json').read_text())
        self.assertEqual(metadata['image'], second.runtime_spec.image)
        self.assertEqual(second.descriptor.files, snapshot_files(second.files_root))
