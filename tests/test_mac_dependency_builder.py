import json
from pathlib import Path
import tempfile
import unittest

from local_web_server.mac_dependency_builder import MacDockerDependencyBuilder
from local_web_server.production_build import DependencyKey


class MacDependencyBuilderTests(unittest.TestCase):
    def test_build_uses_explicit_local_daemon_isolated_client_and_persistent_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'source'; source.mkdir()
            (source/'Dockerfile').write_text('ARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE}\n')
            (source/'requirements.lock').write_text('locked')
            plugins = root/'plugins'; plugins.mkdir()
            plugin = plugins/'docker-buildx'; plugin.write_text('#!/bin/sh\n'); plugin.chmod(0o700)
            calls = []

            class Runner:
                def run(self, repository, commands):
                    calls.extend(commands)
                    argv = commands[0].argv
                    configuration = Path(argv[argv.index('--config')+1])
                    self_config = json.loads((configuration/'config.json').read_text())
                    self.assertion = self_config
                    assert set(self_config) == {'cliPluginsExtraDirs'}
                    assert argv[argv.index('--host')+1] == 'unix:///tmp/local-build.sock'
                    assert 'linux/amd64' in argv and '--builder' in argv

            builder = MacDockerDependencyBuilder('local-web-build', root/'cache', runner=Runner(),
                inspect_context=lambda _: 'unix:///tmp/local-build.sock',
                docker_executable=Path('/usr/bin/true'), plugin_directory=plugins)
            key = DependencyKey('a'*64, 'sha256:'+'b'*64, 'linux', 'amd64', 'python314', '1')
            builder(source, Path('Dockerfile'), key, root/'image.tar', 'python@sha256:'+'b'*64)
            self.assertEqual(len(calls), 1)
            self.assertIn(str(root/'cache'/key.digest), ' '.join(calls[0].argv))

    def test_remote_daemon_or_invalid_context_is_refused_before_a_build(self):
        for endpoint in ('ssh://nas', 'tcp://192.0.2.1:2375', 'unix://host/tmp/docker.sock', 'unix:///tmp/socket?bad'):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                MacDockerDependencyBuilder('builder', Path('/tmp/cache'), inspect_context=lambda _: endpoint).local_endpoint()
        for context in ('', '-host', 'builder;touch-file', 'two words'):
            with self.subTest(context=context), self.assertRaises(ValueError):
                MacDockerDependencyBuilder(context, Path('/tmp/cache'))
