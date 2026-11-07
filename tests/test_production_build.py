import dataclasses
import json
import hashlib
import gzip
import io
import tarfile
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from local_web_server.git_build import GitRepository
from local_web_server.process_runner import ProcessCommand
from local_web_server.models import BuildSpec, ReleaseEntry
from local_web_server.production_build import (
    DependencyCache, DependencyKey, ProductionBuilder, dependency_build_command, production_source_export, verify_docker_archive,
)
from tests.helpers import init_git_repo


def docker_archive(path, label='prepared', *, architecture='amd64', os_name='linux',
                   layer_body=None, omit_layer=False, corrupt_layer=False, compression=None, truncate_gzip=False, damage_gzip_crc=False, additional_layer_body=None):
    bodies = ([] if layer_body is None else [layer_body]) + ([] if additional_layer_body is None else [additional_layer_body])
    names = ['layer/layer.tar' if index == 0 else 'layer2/layer.tar' for index in range(len(bodies))]
    config = json.dumps(dict(architecture=architecture, os=os_name,
        config=dict(Cmd=['/bin/true'], Labels={'fixture': label}),
        rootfs=dict(type='layers', diff_ids=['sha256:' + hashlib.sha256(body).hexdigest() for body in bodies])), sort_keys=True).encode()
    digest = hashlib.sha256(config).hexdigest()
    manifest = json.dumps([dict(Config=digest + '.json', RepoTags=['local-web-runtime:prepared'],
        Layers=names)]).encode()
    with tarfile.open(path, 'w') as archive:
        for name, body in ((digest + '.json', config), ('manifest.json', manifest)):
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            archive.addfile(entry, io.BytesIO(body))
        for name, layer in (() if omit_layer else zip(names, bodies)):
            body = b'corrupt' if corrupt_layer else layer
            if compression == 'gzip':
                body = gzip.compress(body, mtime=0)
                if truncate_gzip:
                    body = body[:-5]
                if damage_gzip_crc:
                    body = body[:-8] + bytes([body[-8] ^ 1]) + body[-7:]
            elif compression == 'zstd':
                body = bytes.fromhex('28b52ffd') + body
            entry = tarfile.TarInfo(name)
            entry.size = len(body)
            archive.addfile(entry, io.BytesIO(body))
    return 'sha256:' + digest


def indexed_docker_archive(path, *, compression='gzip', manifest_changes=None,
                           index_changes=None, legacy_tags=None, include_platform=True):
    config_id = docker_archive(path, layer_body=b'prepared Linux layer', compression=compression)
    with tarfile.open(path) as archive:
        files = {member.name: archive.extractfile(member).read() for member in archive if member.isfile()}
    legacy = json.loads(files['manifest.json'])[0]
    config = files[legacy['Config']]
    config_path = 'blobs/sha256/' + config_id.split(':', 1)[1]
    files[config_path] = config
    legacy['Config'] = config_path
    layers = []
    for name in legacy['Layers']:
        body = files.pop(name)
        digest = hashlib.sha256(body).hexdigest()
        blob = 'blobs/sha256/' + digest
        files[blob] = body
        layers.append(dict(mediaType='application/vnd.docker.image.rootfs.diff.tar.gzip' if compression == 'gzip' else 'application/vnd.docker.image.rootfs.diff.tar',
                           digest='sha256:' + digest, size=len(body)))
    legacy['Layers'] = ['blobs/sha256/' + entry['digest'].split(':', 1)[1] for entry in layers]
    legacy['RepoTags'] = legacy_tags
    files['manifest.json'] = json.dumps([legacy]).encode()
    manifest = dict(schemaVersion=2, mediaType='application/vnd.docker.distribution.manifest.v2+json',
                    config=dict(mediaType='application/vnd.docker.container.image.v1+json', digest=config_id, size=len(config)), layers=layers)
    if manifest_changes:
        manifest_changes(manifest)
    body = json.dumps(manifest, sort_keys=True).encode()
    manifest_id = 'sha256:' + hashlib.sha256(body).hexdigest()
    files['blobs/sha256/' + manifest_id.split(':', 1)[1]] = body
    reference = dict(mediaType=manifest['mediaType'], digest=manifest_id, size=len(body))
    if include_platform:
        reference['platform'] = dict(os='linux', architecture='amd64')
    index = dict(schemaVersion=2, mediaType='application/vnd.oci.image.index.v1+json', manifests=[reference])
    if index_changes:
        index_changes(index)
    files['index.json'] = json.dumps(index).encode()
    with tarfile.open(path, 'w') as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(name); info.size=len(body)
            archive.addfile(info, io.BytesIO(body))
    return config_id, manifest_id


def rewrite_archive(path, mutate):
    with tarfile.open(path) as archive:
        files = {member.name: archive.extractfile(member).read() for member in archive if member.isfile()}
    mutate(files)
    with tarfile.open(path, 'w') as archive:
        for name, body in files.items():
            info = tarfile.TarInfo(name); info.size = len(body)
            archive.addfile(info, io.BytesIO(body))


class ProductionBuildTests(unittest.TestCase):
    def test_indexed_buildx_archive_binds_both_config_and_manifest_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'image.tar'
            for platform in (True, False):
                config_id, manifest_id = indexed_docker_archive(path, include_platform=platform)
                verified = verify_docker_archive(path)
                self.assertEqual(verified.image_digest, config_id)
                self.assertEqual(verified.manifest_digest, manifest_id)
                self.assertEqual(verified.archive_digest, hashlib.sha256(path.read_bytes()).hexdigest())
                cache = DependencyCache(Path(temporary) / ('cache' + str(platform)))
                def install(target):
                    target.write_bytes(path.read_bytes())
                identity = cache.prepare(self.key(), install).runtime_identity()
                self.assertEqual(identity.manifest_digest, manifest_id)
                self.assertEqual(identity.image_digest, config_id)
            docker_archive(path)
            self.assertIsNone(verify_docker_archive(path).manifest_digest)

    def test_index_hash_size_platform_and_legacy_graph_disagreement_fail_before_load(self):
        cases = (
            ({}, lambda value: value['manifests'][0].update(size=1)),
            ({}, lambda value: value['manifests'][0].update(digest='sha256:' + 'a' * 64)),
            ({}, lambda value: value['manifests'][0].update(platform=dict(os='linux', architecture='arm64'))),
            ({}, lambda value: value['manifests'].append(value['manifests'][0])),
            ({'manifest_changes': lambda value: value['config'].update(digest='sha256:' + 'a' * 64)}, None),
            ({'manifest_changes': lambda value: value['config'].update(size=1)}, None),
            ({'manifest_changes': lambda value: value['layers'][0].update(digest='sha256:' + 'a' * 64)}, None),
            ({'manifest_changes': lambda value: value['layers'][0].update(size=1)}, None),
            ({'manifest_changes': lambda value: value.update(layers=[])}, None),
        )
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'image.tar'
            for index, (changes, mutate_index) in enumerate(cases):
                indexed_docker_archive(path, **changes, index_changes=mutate_index)
                with self.subTest(case=index), self.assertRaises(ValueError):
                    verify_docker_archive(path)

    def test_archive_rejects_tampered_manifest_bytes_and_noncanonical_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'image.tar'
            _, manifest_id = indexed_docker_archive(path, manifest_changes=lambda value: value.update(annotations={'fixture': 'a'}))
            blob = 'blobs/sha256/' + manifest_id.split(':', 1)[1]
            rewrite_archive(path, lambda files: files.update({blob: files[blob].replace(b'"fixture": "a"', b'"fixture": "b"')}))
            with self.assertRaises(ValueError):
                verify_docker_archive(path)
            indexed_docker_archive(path)
            rewrite_archive(path, lambda files: files.update({'index.json': files['index.json'].replace(
                b'"schemaVersion": 2', b'"schemaVersion": 2, "schemaVersion": 2')}))
            with self.assertRaises(ValueError):
                verify_docker_archive(path)
            docker_archive(path, label=float('nan'))
            with self.assertRaises(ValueError):
                verify_docker_archive(path)
            docker_archive(path)
            rewrite_archive(path, lambda files: files.update({'repositories': b'{"jellyfin":{"latest":"foreign"}}'}))
            with self.assertRaises(ValueError):
                verify_docker_archive(path)

    def test_archive_cannot_retag_foreign_images_during_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'image.tar'
            for tags in (['jellyfin:latest'], ['registry.example/local-web-runtime:tag'], ['local-web-runtime:bad tag'], ['local-web-runtime:tag'] * 2):
                indexed_docker_archive(path, legacy_tags=tags)
                with self.subTest(tags=tags), self.assertRaises(ValueError):
                    verify_docker_archive(path)
            for tags in (None, [], ['local-web-runtime:fixture']):
                indexed_docker_archive(path, legacy_tags=tags)
                verify_docker_archive(path)
            for alias_field in ('org.opencontainers.image.ref.name', 'io.containerd.image.name'):
                indexed_docker_archive(path, index_changes=lambda value: value['manifests'][0].update(
                    annotations={alias_field: 'jellyfin:latest'}))
                with self.subTest(alias=alias_field), self.assertRaises(ValueError):
                    verify_docker_archive(path)

    def test_exact_export_never_runs_checkout_hooks_or_copies_their_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root/'repository'
            repository.mkdir()
            commit = init_git_repo(repository, {'source.txt': 'committed'})
            hook = repository/'.git/hooks/post-checkout'
            hook.write_text('#!/bin/sh\nprintf state > generated.sqlite\nprintf mutated > source.txt\n')
            hook.chmod(0o700)
            with production_source_export(GitRepository(repository), commit) as source:
                self.assertFalse((source/'generated.sqlite').exists())
                self.assertEqual((source/'source.txt').read_text(), 'committed')

    def test_exact_export_ignores_archive_attributes_and_preserves_committed_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root/'repository'
            repository.mkdir()
            content = '$Format:%H$'
            commit = init_git_repo(repository, {
                '.gitattributes': 'source.txt export-ignore\nsubstitute.txt export-subst\n',
                'source.txt': 'committed', 'substitute.txt': content,
            })
            (repository/'.git/info/attributes').write_text('source.txt export-ignore\n')
            with production_source_export(GitRepository(repository), commit) as source:
                self.assertEqual((source/'source.txt').read_text(), 'committed')
                self.assertEqual((source/'substitute.txt').read_text(), content)

    def test_source_identity_requires_a_commit_object_not_only_a_hex_identifier(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commit = init_git_repo(root, {'source.txt': 'committed'})
            tree = subprocess.check_output(['git', '-C', str(root), 'rev-parse', commit+'^{tree}'], text=True).strip()
            with self.assertRaisesRegex(ValueError, 'commit'):
                with production_source_export(GitRepository(root), tree):
                    self.fail('tree object was accepted as a source commit')

    def key(self, **changes):
        values = dict(lockfile_digest='a' * 64, image_digest='sha256:' + 'b' * 64,
                      os='linux', architecture='amd64', abi='python-3.14', recipe_version='1')
        values.update(changes)
        return DependencyKey(**values)

    def test_docker_archive_identity_comes_from_config_and_rejects_wrong_target_or_unsafe_tar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'image.tar'
            expected = docker_archive(path)
            verified = verify_docker_archive(path)
            self.assertEqual(verified.image_digest, expected)
            self.assertEqual((verified.os, verified.architecture), ('linux', 'amd64'))
            for changes in (dict(architecture='arm64'), dict(os_name='darwin')):
                docker_archive(path, **changes)
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    verify_docker_archive(path)
            docker_archive(path)
            for name, kind in (('../escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE),
                               ('manifest.json', tarfile.REGTYPE)):
                docker_archive(path)
                with tarfile.open(path, 'a') as archive:
                    entry = tarfile.TarInfo(name)
                    entry.type = kind
                    entry.linkname = '/etc/passwd' if kind == tarfile.SYMTYPE else ''
                    archive.addfile(entry)
                with self.subTest(name=name), self.assertRaises(ValueError):
                    verify_docker_archive(path)
            path.write_bytes(b'not a Docker archive')
            with self.assertRaises(ValueError):
                verify_docker_archive(path)

    def test_docker_archive_checks_every_layer_and_rejects_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'image.tar'
            expected = docker_archive(path, layer_body=b'uncompressed layer')
            self.assertEqual(verify_docker_archive(path).image_digest, expected)
            for changes in (dict(omit_layer=True), dict(corrupt_layer=True)):
                docker_archive(path, layer_body=b'uncompressed layer', **changes)
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    verify_docker_archive(path)
            docker_archive(path, layer_body=b'uncompressed layer')
            path.write_bytes(path.read_bytes()[:1100])
            with self.assertRaises(ValueError):
                verify_docker_archive(path)

    def test_gzip_buildx_layers_hash_decompressed_bytes_and_preserve_archive_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'image.tar'
            body = b'Linux image layer contents' * 1000
            expected = docker_archive(path, layer_body=body, compression='gzip')
            transport_digest = hashlib.sha256(path.read_bytes()).hexdigest()
            verified = verify_docker_archive(path)
            self.assertEqual(verified.image_digest, expected)
            self.assertEqual(verified.archive_digest, transport_digest)
            self.assertNotEqual(transport_digest, hashlib.sha256(body).hexdigest())
            for changes in (dict(corrupt_layer=True), dict(truncate_gzip=True), dict(damage_gzip_crc=True)):
                docker_archive(path, layer_body=body, compression='gzip', **changes)
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    verify_docker_archive(path)
            docker_archive(path, layer_body=body, compression='zstd')
            with self.assertRaisesRegex(ValueError, 'zstd'):
                verify_docker_archive(path)

    def test_compressed_and_uncompressed_layers_obey_expanded_byte_limits(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'image.tar'
            for compression in (None, 'gzip'):
                docker_archive(path, layer_body=b'x' * 10000, compression=compression)
                with self.subTest(compression=compression), self.assertRaises(ValueError):
                    verify_docker_archive(path, maximum_layer_bytes=100)
                with self.subTest(total=compression), self.assertRaises(ValueError):
                    verify_docker_archive(path, maximum_total_layer_bytes=100)
                docker_archive(path, layer_body=b'a' * 100, additional_layer_body=b'b' * 100, compression=compression)
                with self.subTest(combined=compression), self.assertRaises(ValueError):
                    verify_docker_archive(path, maximum_layer_bytes=100, maximum_total_layer_bytes=150)

    def test_source_only_change_reuses_dependency_digest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = DependencyCache(root / 'cache')
            installed = []
            def install(path):
                installed.append(path)
                docker_archive(path)
            first = cache.prepare(self.key(), install)
            # A later source commit has no bearing on the locked dependency identity.
            (root / 'source.py').write_text('new source')
            second = cache.prepare(self.key(), install)
            self.assertEqual(first.digest, second.digest)
            self.assertEqual(verify_docker_archive(second.path).image_digest, second.runtime_identity().image_digest)
            self.assertNotEqual(second.runtime_identity().image_digest, self.key().image_digest)
            self.assertEqual(len(installed), 1)
            self.assertTrue(second.cache_hit)
            self.assertFalse(first.cache_hit)
            self.assertEqual(second.runtime_identity().dependency_digest, 'sha256:' + second.digest)

    def test_abi_change_invalidates_dependency_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = DependencyCache(Path(temporary))
            calls = []
            def install(path):
                calls.append(path)
                docker_archive(path, str(len(calls)))
            first = cache.prepare(self.key(), install)
            for changes in ({'abi': 'python-3.13'}, {'lockfile_digest': 'c' * 64},
                            {'image_digest': 'sha256:' + 'd' * 64}, {'recipe_version': '2'}):
                artifact = cache.prepare(self.key(**changes), install)
                self.assertNotEqual(first.digest, artifact.digest)
                self.assertFalse(artifact.cache_hit)
            self.assertEqual(len(calls), 5)
            with self.assertRaises(ValueError):
                cache.prepare(self.key(os='darwin'), install)
            with self.assertRaises(ValueError):
                cache.prepare(self.key(architecture='arm64'), install)

    def test_corrupt_cache_is_rebuilt_and_failed_install_never_becomes_cached(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = DependencyCache(Path(temporary))
            def install(path):
                docker_archive(path)
            first = cache.prepare(self.key(), install)
            first.path.chmod(0o600)
            first.path.write_bytes(b'corrupt')
            repaired = cache.prepare(self.key(), install)
            self.assertFalse(repaired.cache_hit)
            self.assertEqual(repaired.digest, first.digest)
            def fail(path):
                path.write_bytes(b'partial')
                raise RuntimeError('installer failed')
            with self.assertRaises(RuntimeError):
                cache.prepare(self.key(abi='other'), fail)
            completed = cache.prepare(self.key(abi='other'), install)
            self.assertFalse(completed.cache_hit)

    def test_build_uses_exact_export_native_commands_and_leaves_source_secrets_state_untouched(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / 'repository'
            repository.mkdir()
            commit = init_git_repo(repository, {
                'source.txt': 'committed',
                '.gitignore': '.env\nstate/\nnode_modules/\n',
                '.env.example': 'TOKEN=example',
            })
            (repository / 'source.txt').write_text('dirty')
            (repository / '.env').write_text('TOKEN=secret')
            (repository / 'state').mkdir()
            (repository / 'state' / 'store').write_text('live')
            (repository / 'node_modules').mkdir()
            (repository / 'node_modules' / 'addon.node').write_bytes(b'macos')
            command = ProcessCommand('native build', (sys.executable, '-c',
                "from pathlib import Path; "
                "assert not Path('.git').exists(); assert not Path('.env').exists(); "
                "assert not Path('state').exists(); assert not Path('node_modules').exists(); "
                "assert Path('.env.example').exists(); "
                "Path('dist').mkdir(); Path('dist/index.html').write_bytes(Path('source.txt').read_bytes())"))
            target = root / 'prepared'
            result = ProductionBuilder().build(GitRepository(repository), commit=commit,
                app_id='example', recipe_version='1', commands=(command,), output=Path('dist'),
                destination=target, data_contract='static-v1', rollback_policy='compatible')
            self.assertEqual((target / 'files/index.html').read_text(), 'committed')
            self.assertEqual(result.source_commit, commit)
            self.assertEqual(json.loads((target / 'descriptor.json').read_text()), result.to_dict())
            self.assertEqual((repository / 'source.txt').read_text(), 'dirty')
            self.assertEqual((repository / '.env').read_text(), 'TOKEN=secret')
            self.assertEqual((repository / 'state/store').read_text(), 'live')
            with self.assertRaises(FileExistsError):
                ProductionBuilder().build(GitRepository(repository), commit=commit,
                    app_id='example', recipe_version='1', commands=(command,), output=Path('dist'),
                    destination=target, data_contract='static-v1', rollback_policy='compatible')

    def test_source_preparation_restores_dependencies_into_the_same_build_export(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / 'repository'
            repository.mkdir()
            commit = init_git_repo(repository, {'source.txt': 'committed'})
            def prepare(source):
                self.assertEqual((source / 'source.txt').read_text(), 'committed')
                (source / 'node_modules').mkdir()
                (source / 'node_modules/tool').write_text('restored native dependency')
            command = ProcessCommand('native frontend', (sys.executable, '-c',
                "from pathlib import Path; Path('dist').mkdir(); "
                "Path('dist/index.html').write_text(Path('node_modules/tool').read_text())"))
            ProductionBuilder().build(GitRepository(repository), commit=commit,
                app_id='example', recipe_version='1', commands=(command,), output=Path('dist'),
                destination=root / 'prepared', data_contract='none', rollback_policy='compatible', prepare_source=prepare)
            self.assertEqual((root / 'prepared/files/index.html').read_text(), 'restored native dependency')
            self.assertFalse((root / 'prepared/files/node_modules').exists())
            self.assertFalse((repository / 'node_modules').exists())

    def test_composes_existing_build_release_entries_before_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / 'repository'
            repository.mkdir()
            commit = init_git_repo(repository, {'server.py': 'committed server'})
            build = BuildSpec((), Path('release'), (),
                (ReleaseEntry(Path('dist'), Path('public')), ReleaseEntry(Path('server.py'), Path('server.py'))))
            command = ProcessCommand('native frontend', (sys.executable, '-c',
                "from pathlib import Path; Path('dist').mkdir(); Path('dist/index.html').write_text('frontend')"))
            descriptor = ProductionBuilder().build(GitRepository(repository), commit=commit,
                app_id='example', recipe_version='1', commands=(command,), output=Path('release'),
                destination=root / 'prepared', data_contract='v1', rollback_policy='compatible', build_spec=build)
            self.assertEqual([entry.path for entry in descriptor.files], ['public/index.html', 'server.py'])
            self.assertEqual((root / 'prepared/files/server.py').read_text(), 'committed server')

    def test_dependency_export_is_exact_and_disposable_without_building_frontend(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / 'repository'
            repository.mkdir()
            commit = init_git_repo(repository, {'requirements.lock': 'exact', '.env': 'committed secret'})
            (repository / 'requirements.lock').write_text('dirty')
            with production_source_export(GitRepository(repository), commit) as exported:
                self.assertEqual((exported / 'requirements.lock').read_text(), 'exact')
                self.assertFalse((exported / '.git').exists())
                self.assertFalse((exported / '.env').exists())
            self.assertFalse(exported.exists())
            with self.assertRaises(ValueError):
                with production_source_export(GitRepository(repository), 'main'):
                    self.fail('mutable revision accepted')

    def test_failed_build_or_unsafe_output_does_not_publish_a_partial_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / 'repository'
            repository.mkdir()
            commit = init_git_repo(repository, {'source.txt': 'safe'})
            for script in ("raise SystemExit(1)", "pass",
                           "from pathlib import Path; Path('dist').mkdir(); Path('dist/link').symlink_to('/etc/passwd')",
                           "from pathlib import Path; Path('dist').mkdir(); Path('dist/native').write_bytes(bytes.fromhex('cffaedfe') + b'bad')"):
                target = root / 'prepared'
                with self.subTest(script=script), self.assertRaises(ValueError):
                    ProductionBuilder().build(GitRepository(repository), commit=commit,
                        app_id='example', recipe_version='1',
                        commands=(ProcessCommand('build', (sys.executable, '-c', script)),),
                        output=Path('dist'), destination=target, data_contract='v1', rollback_policy='compatible')
                self.assertFalse(target.exists())

    def test_output_cannot_escape_export_through_an_ancestor_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / 'repository'
            repository.mkdir()
            commit = init_git_repo(repository, {'source.txt': 'safe'})
            outside = root / 'outside'
            (outside / 'dist').mkdir(parents=True)
            (outside / 'dist' / 'secret').write_text('private')
            command = ProcessCommand('build', (sys.executable, '-c',
                "from pathlib import Path; Path('escape').symlink_to(" + repr(str(outside)) + ")"))
            with self.assertRaises(ValueError):
                ProductionBuilder().build(GitRepository(repository), commit=commit,
                    app_id='example', recipe_version='1', commands=(command,), output=Path('escape/dist'),
                    destination=root / 'prepared', data_contract='v1', rollback_policy='compatible')
            self.assertFalse((root / 'prepared').exists())

    def test_dependency_image_command_refuses_undeclared_build_context_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            (source / 'Dockerfile.runtime').write_text('FROM ${RUNTIME_IMAGE}')
            (source / 'requirements.lock').write_text('locked')
            (source / 'unkeyed-helper.py').write_text('changing helper')
            with self.assertRaises(ValueError):
                dependency_build_command(source, Path('Dockerfile.runtime'), root / 'archive.tar', self.key(),
                    root / 'cache', 'python@sha256:' + 'b' * 64, dependency_inputs=('requirements.lock',))

    def test_dependency_command_pins_linux_target_base_and_outputs_archive_outside_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source'
            source.mkdir()
            (source / 'Dockerfile.runtime').write_text('FROM ${RUNTIME_IMAGE}')
            (source / 'requirements.lock').write_text('locked')
            archive = root / 'archive.tar'
            command = dependency_build_command(source, Path('Dockerfile.runtime'), archive, self.key(), root / 'buildkit-cache', 'python@sha256:' + 'b' * 64, dependency_inputs=('requirements.lock',))
            self.assertIn('linux/amd64', command.argv)
            self.assertIn('RUNTIME_IMAGE=python@sha256:' + 'b' * 64, command.argv)
            self.assertIn('type=docker,dest=' + str(archive.resolve()), command.argv)
            self.assertNotIn('--load', command.argv)
            with self.assertRaises(ValueError):
                dependency_build_command(source, Path('../Dockerfile'), archive, self.key(), root / 'cache', 'python@sha256:' + 'b' * 64, dependency_inputs=('requirements.lock',))
            with self.assertRaises(ValueError):
                dependency_build_command(source, Path('Dockerfile.runtime'), source / 'out.tar', self.key(), root / 'cache', 'python@sha256:' + 'b' * 64, dependency_inputs=('requirements.lock',))


if __name__ == '__main__':
    unittest.main()
