import dataclasses
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from local_web_server.release_manifest import (
    FileEntry, ReleaseDescriptorV1, RuntimeIdentity, plan_transfer, snapshot_files,
)


class ReleaseManifestTests(unittest.TestCase):
    def descriptor(self, files=(), runtime=None, **kwargs):
        return ReleaseDescriptorV1('example', 'a' * 40, '1', runtime, tuple(files),
                                   kwargs.get('data_contract', 'example-v1'),
                                   kwargs.get('rollback_policy', 'compatible'))

    def runtime(self, **kwargs):
        values = dict(image_digest='sha256:' + 'b' * 64,
                      dependency_digest='sha256:' + 'c' * 64, os='linux',
                      architecture='amd64', abi='python-3.14',
                      lockfile_digest='d' * 64, recipe_version='1')
        values.update(kwargs)
        return RuntimeIdentity(**values)

    def test_snapshot_records_exact_bytes_mode_and_sorted_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'z.js').write_bytes(b'abc')
            (root / 'z.js').chmod(0o755)
            (root / 'a.txt').write_bytes(b'')
            entries = snapshot_files(root)
        self.assertEqual([entry.path for entry in entries], ['a.txt', 'z.js'])
        self.assertEqual(entries[1], FileEntry('z.js',
            'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad', 3, 0o755))

    def test_descriptor_is_canonical_immutable_and_round_trips(self):
        first = FileEntry('z', 'f' * 64, 12, 0o644)
        second = FileEntry('a', 'e' * 64, 4, 0o755)
        descriptor = self.descriptor([first, second])
        payload = descriptor.to_dict()
        self.assertEqual(payload['schemaVersion'], 1)
        self.assertEqual([entry['path'] for entry in payload['files']], ['a', 'z'])
        expected = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        self.assertEqual(descriptor.canonical_bytes(), expected)
        self.assertEqual(descriptor.digest, hashlib.sha256(expected).hexdigest())
        self.assertEqual(ReleaseDescriptorV1.from_dict(payload), descriptor)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            descriptor.app_id = 'other'
        self.assertNotEqual(descriptor.digest, self.descriptor([first]).digest)
        self.assertNotEqual(descriptor.digest, self.descriptor([first, second],
                            rollback_policy='migration-required').digest)

    def test_rejects_alias_traversal_duplicate_and_unsafe_metadata(self):
        for path in ('../a', '/a', 'a/../b', 'a//b', './a', 'a\\b', '.git/config', '.env', 'x/.env.production'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                FileEntry(path, 'f' * 64, 1, 0o644)
        good = FileEntry('a', 'f' * 64, 1, 0o644)
        with self.assertRaises(ValueError):
            self.descriptor([good, good])
        with self.assertRaises(ValueError):
            self.descriptor([good, FileEntry('a/b', 'e' * 64, 2, 0o644)])
        for field, value in (('size', -1), ('size', True), ('mode', 0o4755), ('sha256', 'bad')):
            values = dict(path='a', sha256='f' * 64, size=1, mode=0o644)
            values[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                FileEntry(**values)
        payload = self.descriptor().to_dict()
        payload['unexpected'] = 'ignored?'
        with self.assertRaises(ValueError):
            ReleaseDescriptorV1.from_dict(payload)

    def test_snapshot_rejects_symlinks_and_protected_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'link').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):
                snapshot_files(root)
            (root / 'link').unlink()
            (root / '.env').write_text('TOKEN=secret')
            with self.assertRaises(ValueError):
                snapshot_files(root)

    def test_transfer_counts_unique_missing_blob_bytes_and_exact_runtime_identity(self):
        runtime = self.runtime()
        entries = (FileEntry('a', 'e' * 64, 5, 0o644),
                   FileEntry('b', 'e' * 64, 5, 0o755),
                   FileEntry('c', 'f' * 64, 7, 0o644))
        descriptor = self.descriptor(entries, runtime)
        plan = plan_transfer(descriptor, frozenset({'f' * 64}), (runtime,))
        self.assertEqual(tuple(entry.path for entry in plan.missing_files), ('a',))
        self.assertEqual(plan.missing_bytes, 5)
        self.assertFalse(plan.runtime_required)
        for key, value in (('abi', 'python-3.13'), ('architecture', 'arm64'),
                           ('os', 'darwin'), ('lockfile_digest', 'e' * 64),
                           ('image_digest', 'sha256:' + 'a' * 64), ('recipe_version', '2')):
            # A cache report for an incompatible object must never suppress transfer.
            candidate = dataclasses.replace(runtime, **{key: value})
            self.assertTrue(plan_transfer(descriptor, frozenset(), (candidate,)).runtime_required)
        self.assertFalse(plan_transfer(self.descriptor(), frozenset(), ()).runtime_required)


if __name__ == '__main__':
    unittest.main()
