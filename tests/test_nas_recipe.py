import copy
import json
from pathlib import Path
import tempfile
import unittest

from local_web_server.config import ConfigError, parse_manifest
from local_web_server.nas_recipe import (
    ContainerBuildRecipe, ProductionRecipe, parse_production_recipe,
    production_declaration, require_nas_recipe,
)


class NasRecipeTests(unittest.TestCase):
    def service(self):
        return dict(recipeVersion='1', dataContract='example-v1', rollbackPolicy='compatible',
                    buildCommands=[['npm', 'run', 'build']], nativeDependencies=dict(
                        lockfiles=['package-lock.json'], installCommands=[['npm', 'ci']], outputs=['node_modules']),
                    runtime=dict(baseImage='python@sha256:' + 'a' * 64, abi='cp314-linux-amd64', healthRuntime='python3',
                                 lockfiles=['requirements.lock'], dockerfile='Dockerfile.runtime',
                                 command=['python', '-m', 'example', '--port', '{port}', '--state', '{state}',
                                          '--bind', '{bindHost}', '--proxy', '{proxyAddress}', '--origin', '{publicOrigin}'],
                                 cpuLimit=2, memoryLimitMib=1024, secretFiles=['provider.key'], user='10001:10001'))

    def test_explicit_static_and_service_contracts_round_trip(self):
        static = production_declaration('static')
        self.assertEqual(static, dict(recipeVersion='1', dataContract='none', rollbackPolicy='compatible', runtime=None,
                              buildCommands=[], nativeDependencies=None))
        self.assertIsNone(parse_production_recipe(static, kind='static').runtime)
        value = self.service()
        recipe = parse_production_recipe(value, kind='service')
        self.assertEqual(recipe.to_dict(), value)
        self.assertEqual(require_nas_recipe(recipe, kind='service'), recipe)
        with self.assertRaises(ValueError):
            require_nas_recipe(None, kind='static')
        with self.assertRaises(ValueError):
            production_declaration('service')

    def test_health_runtime_is_explicit_and_never_inferred_from_image_or_command(self):
        payload = self.service()
        recipe = parse_production_recipe(payload, kind='service')
        self.assertEqual(recipe.runtime.health_runtime, 'python3')
        del payload['runtime']['healthRuntime']
        with self.assertRaises(ValueError):
            parse_production_recipe(payload, kind='service')
        payload['runtime']['healthRuntime'] = 'node'
        # Explicit selection is the contract; parsing image/command names is forbidden.
        self.assertEqual(parse_production_recipe(payload, kind='service').runtime.health_runtime, 'node')

    def test_production_native_recipe_requires_explicit_nonfallback_commands(self):
        value = self.service()
        recipe = parse_production_recipe(value, kind='service')
        self.assertEqual(recipe.build_commands[0].argv, ('npm', 'run', 'build'))
        self.assertEqual(recipe.native_dependencies.install_commands[0].argv, ('npm', 'ci'))
        for field in ('buildCommands', 'nativeDependencies'):
            incomplete = self.service()
            del incomplete[field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                parse_production_recipe(incomplete, kind='service')

    def test_rejects_unpinned_unsafe_or_incomplete_runtime_recipe(self):
        cases = [('healthRuntime', 'python'), ('healthRuntime', ''), ('healthRuntime', True), ('baseImage', 'python:3.14'), ('abi', ''), ('lockfiles', []),
                 ('lockfiles', ['../state.sqlite']), ('lockfiles', ['.env']),
                 ('lockfiles', ['requirements.lock', 'requirements.lock']),
                 ('dockerfile', '/Users/example/Dockerfile'),
                 ('command', ['python', '{repository}/main.py']),
                 ('command', ['python', '{port!r}']), ('command', ['python', '${TOKEN}']),
                 ('command', ['/Users/example/.venv/python', 'main.py']),
                 ('cpuLimit', float('nan')), ('memoryLimitMib', True),
                 ('secretFiles', ['../secret']), ('secretFiles', ['key', 'key']), ('user', '0:0')]
        for field, value in cases:
            payload = self.service()
            payload['runtime'][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                parse_production_recipe(payload, kind='service')
        payload = self.service()
        payload['runtime']['unexpected'] = True
        with self.assertRaises(ValueError):
            parse_production_recipe(payload, kind='service')
        with self.assertRaises(ValueError):
            parse_production_recipe(self.service(), kind='static')
        with self.assertRaises(ValueError):
            parse_production_recipe(production_declaration('static'), kind='service')

    def test_dependency_key_ignores_source_but_invalidates_lockfile_and_recipe_edits(self):
        recipe = parse_production_recipe(self.service(), kind='service')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'requirements.lock').write_text('package==1.0 --hash=sha256:abc')
            (root / 'Dockerfile.runtime').write_text('ARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE}\n')
            (root / 'source.py').write_text('old')
            first = recipe.dependency_key(root)
            (root / 'source.py').write_text('new')
            self.assertEqual(first, recipe.dependency_key(root))
            (root / 'requirements.lock').write_text('package==2.0 --hash=sha256:def')
            self.assertNotEqual(first.digest, recipe.dependency_key(root).digest)
            (root / 'requirements.lock').write_text('package==1.0 --hash=sha256:abc')
            (root / 'Dockerfile.runtime').write_text('ARG RUNTIME_IMAGE\nFROM ${RUNTIME_IMAGE}\nRUN true\n')
            self.assertNotEqual(first.digest, recipe.dependency_key(root).digest)
            (root / 'requirements.lock').unlink()
            (root / 'requirements.lock').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                recipe.dependency_key(root)

    def test_manifest_preserves_legacy_and_accepts_committed_production_contract(self):
        manifest = dict(schemaVersion=1, id='example', title='Example', route='/example', kind='static',
                        build=dict(commands=[['npm', 'run', 'build']], output='dist', environment=[]), healthPath='/example')
        self.assertIsNone(parse_manifest(json.dumps(manifest)).production)
        manifest['production'] = production_declaration('static')
        self.assertEqual(parse_manifest(json.dumps(manifest)).production.data_contract, 'none')
        manifest['production']['runtime'] = self.service()['runtime']
        with self.assertRaises(ConfigError):
            parse_manifest(json.dumps(manifest))


if __name__ == '__main__':
    unittest.main()
