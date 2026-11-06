import dataclasses
from pathlib import Path
import sys
import tempfile
import unittest

from local_web_server.native_build import (
    NativeDependencyCache, NativeDependencyRecipe, NativePlatformIdentity, probe_native_platform, dependency_input_export,
)
from local_web_server.process_runner import ProcessCommand


class NativeBuildTests(unittest.TestCase):
    def identity(self, **changes):
        value = dict(os='darwin', architecture='arm64', node_abi='137')
        value.update(changes)
        return NativePlatformIdentity(**value)

    def recipe(self, installer):
        return NativeDependencyRecipe(('package-lock.json',),
            (ProcessCommand('native dependencies', (sys.executable, '-c', installer)),), ('node_modules',))

    def source(self, root, name):
        source = root / name
        source.mkdir()
        (source / 'package-lock.json').write_text('locked v1')
        (source / 'source.ts').write_text('source')
        return source

    def test_native_probe_ignores_ambient_node_options(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {'NODE_OPTIONS': '--require=/nonexistent/private-module'}):
            identity = probe_native_platform()
        self.assertTrue(identity.node_abi.isdigit())
        self.assertIn(identity.os, ('darwin', 'linux'))
        self.assertIn(identity.architecture, ('arm64', 'x64'))

    def test_warm_source_change_installs_zero_dependencies_and_preserves_internal_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recipe = self.recipe("from pathlib import Path; "
                "Path('node_modules/.bin').mkdir(parents=True); "
                "Path('node_modules/tool').write_text('prepared'); "
                "Path('node_modules/.bin/tool').symlink_to('../tool'); "
                "Path('installer-ran').write_text('yes')")
            cache = NativeDependencyCache(root / 'cache')
            first = self.source(root, 'first')
            cold = cache.prepare(first, recipe, recipe_version='1', platform=self.identity())
            second = self.source(root, 'second')
            (second / 'source.ts').write_text('source changed')
            warm = cache.prepare(second, recipe, recipe_version='1', platform=self.identity())
            self.assertFalse(cold.cache_hit)
            self.assertTrue(warm.cache_hit)
            self.assertEqual(cold.key, warm.key)
            self.assertFalse((second / 'installer-ran').exists())
            self.assertTrue((second / 'node_modules/.bin/tool').is_symlink())
            self.assertEqual((second / 'node_modules/.bin/tool').read_text(), 'prepared')
            (second / 'node_modules/tool').write_text('local mutation')
            third = self.source(root, 'third')
            cache.prepare(third, recipe, recipe_version='1', platform=self.identity())
            self.assertEqual((third / 'node_modules/tool').read_text(), 'prepared')

    def test_lock_recipe_platform_and_node_abi_changes_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            recipe = self.recipe("from pathlib import Path; Path('node_modules').mkdir(); Path('node_modules/a').write_text('ready')")
            cache = NativeDependencyCache(root / 'cache')
            first = self.source(root, 'first')
            cold = cache.prepare(first, recipe, recipe_version='1', platform=self.identity())
            cases = ((dict(node_abi='138'), '1', 'locked v1'),
                     (dict(architecture='amd64'), '1', 'locked v1'),
                     (dict(os='linux'), '1', 'locked v1'),
                     ({}, '2', 'locked v1'), ({}, '1', 'locked v2'))
            for index, (changes, version, lock) in enumerate(cases):
                source = self.source(root, 'case' + str(index))
                (source / 'package-lock.json').write_text(lock)
                result = cache.prepare(source, recipe, recipe_version=version, platform=self.identity(**changes))
                self.assertFalse(result.cache_hit)
                self.assertNotEqual(cold.key, result.key)

    def test_unsafe_symlink_or_mutated_lock_never_becomes_a_cache_hit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, script in enumerate((
                "from pathlib import Path; Path('node_modules').mkdir(); Path('node_modules/link').symlink_to('/etc/passwd')",
                "from pathlib import Path; Path('node_modules').mkdir(); Path('package-lock.json').write_text('mutated')",
            )):
                source = self.source(root, 'source' + str(index))
                cache = NativeDependencyCache(root / ('cache' + str(index)))
                with self.assertRaises(ValueError):
                    cache.prepare(source, self.recipe(script), recipe_version='1', platform=self.identity())
                self.assertFalse(any(path.is_dir() and not path.name.startswith('.') for path in cache.root.iterdir()))

    def test_existing_dependency_output_is_never_overwritten_or_reinstalled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = NativeDependencyCache(root / 'cache')
            recipe = self.recipe("from pathlib import Path; Path('node_modules').mkdir(exist_ok=True); Path('node_modules/a').write_text('installed'); Path('installer-ran').write_text('yes')")
            cache.prepare(self.source(root, 'first'), recipe, recipe_version='1', platform=self.identity())
            second = self.source(root, 'second')
            (second / 'node_modules').mkdir()
            (second / 'node_modules/a').write_text('existing')
            with self.assertRaises(ValueError):
                cache.prepare(second, recipe, recipe_version='1', platform=self.identity())
            self.assertEqual((second / 'node_modules/a').read_text(), 'existing')
            self.assertFalse((second / 'installer-ran').exists())

    def test_undeclared_installer_helper_is_inaccessible_and_declared_helper_changes_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.source(root, 'undeclared')
            script = "from pathlib import Path; Path('node_modules').mkdir(); Path('node_modules/tool').write_text('v1')"
            (source / 'install.py').write_text(script)
            command = (ProcessCommand('install helper', (sys.executable, 'install.py')),)
            cache = NativeDependencyCache(root / 'cache')
            with self.assertRaises(ValueError):
                cache.prepare(source, NativeDependencyRecipe(('package-lock.json',), command, ('node_modules',)),
                              recipe_version='1', platform=self.identity())
            keys = []
            for index in (1, 2):
                selected = self.source(root, 'declared' + str(index))
                (selected / 'install.py').write_text(script.replace('v1', 'v' + str(index)))
                result = cache.prepare(selected, NativeDependencyRecipe(('package-lock.json', 'install.py'), command, ('node_modules',)),
                                       recipe_version='1', platform=self.identity())
                self.assertFalse(result.cache_hit)
                keys.append(result.key)
                self.assertEqual((selected / 'node_modules/tool').read_text(), 'v' + str(index))
            self.assertNotEqual(*keys)

    def test_installer_context_contains_only_declared_inputs_and_does_not_leak_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = self.source(root, 'source')
            (source / '.env').write_text('TOKEN=private')
            (source / 'state').mkdir()
            (source / 'state/store').write_text('live')
            recipe = self.recipe("from pathlib import Path; assert not Path('.env').exists(); assert not Path('state').exists(); assert not Path('source.ts').exists(); Path('node_modules').mkdir(); Path('node_modules/tool').write_text('prepared')")
            NativeDependencyCache(root / 'cache').prepare(source, recipe, recipe_version='1', platform=self.identity())
            self.assertEqual((source / 'node_modules/tool').read_text(), 'prepared')
            self.assertEqual((source / 'state/store').read_text(), 'live')
            with dependency_input_export(source, ('package-lock.json', 'package-lock.json')) as context:
                self.assertEqual([p.name for p in context.iterdir()], ['package-lock.json'])
                self.assertEqual((context / 'package-lock.json').stat().st_mode & 0o777, (source / 'package-lock.json').stat().st_mode & 0o777)
            self.assertFalse(context.exists())

    def test_recipe_rejects_overlapping_outputs_and_secret_or_traversal_inputs(self):
        command = (ProcessCommand('install', ('npm', 'ci')),)
        for lockfiles, outputs in ((('.env',), ('node_modules',)),
                                   (('../package-lock.json',), ('node_modules',)),
                                   (('package-lock.json',), ('node_modules', 'node_modules/nested')),
                                   (('package-lock.json',), ('../node_modules',))):
            with self.subTest(lockfiles=lockfiles, outputs=outputs), self.assertRaises(ValueError):
                NativeDependencyRecipe(lockfiles, command, outputs)


if __name__ == '__main__':
    unittest.main()
