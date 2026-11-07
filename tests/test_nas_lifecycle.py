import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from local_web_server.nas_lifecycle import NasOperationStore, NasHostDeploymentManager, NasStatusCollector
from local_web_server.models import HostApp, HostRegistry
from local_web_server.nas_models import NasTarget
from local_web_server.nas_transport import NasTransportError
from local_web_server.nas_deploy import PublicationResult
from local_web_server.release_manifest import FileEntry, ReleaseDescriptorV1, snapshot_files
from local_web_server.services import HealthResult
from tests.helpers import init_git_repo


class NasOperationStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = NasOperationStore(self.root/'local-client', 'example')
        self.descriptor = ReleaseDescriptorV1('example', 'a'*40, '1', None,
            (FileEntry('a', 'a'*64, 0, 0o644), FileEntry('b', 'b'*64, 0, 0o644)), 'none', 'compatible')

    def test_original_intent_is_durable_before_remote_work_and_cannot_be_overwritten(self):
        with self.store.lock():
            self.store.begin('original', self.descriptor, expected_current=None,
                             prepared_root=self.root/'prepared', runtime_archive=None, action='deploy')
            receipt = self.store.pending()
            self.assertEqual(receipt['operationId'], 'original')
            self.assertEqual(receipt['descriptor'], self.descriptor.to_dict())
            self.assertIsNone(receipt['expectedCurrent'])
            self.assertEqual(receipt['phase'], 'preparing')
            self.assertIsNone(receipt['uploadBlobs'])
            self.assertEqual(self.store.select_upload_blobs('original', ('a'*64, 'b'*64)), ('a'*64, 'b'*64))
            self.assertEqual(self.store.select_upload_blobs('original', ('b'*64,)), ('a'*64, 'b'*64))
            with self.assertRaises(ValueError):
                self.store.select_upload_blobs('original', ('c'*64,))
            with self.assertRaisesRegex(ValueError, 'original'):
                self.store.begin('another', self.descriptor, expected_current=None,
                                 prepared_root=self.root/'prepared', runtime_archive=None, action='deploy')
            self.store.before_transition('original', 'activate')
            self.assertEqual(self.store.pending()['phase'], 'activating')
            receipt = self.store.pending()
        another_process = NasOperationStore(self.root/'local-client', 'example')
        with another_process.lock():
            self.assertEqual(another_process.pending(), receipt)
            another_process.complete('original', state='active', current=self.descriptor.digest)
            self.assertIsNone(another_process.pending())
        archive = self.root/'local-client/apps/example/nas-original.json'
        self.assertEqual(json.loads(archive.read_text())['result']['current'], self.descriptor.digest)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)

    def test_malformed_or_foreign_receipt_fails_closed_without_removal(self):
        with self.store.lock():
            self.store.begin('original', self.descriptor, expected_current=None,
                             prepared_root=self.root/'prepared', runtime_archive=None, action='deploy')
        path = self.root/'local-client/apps/example/nas-operation.json'
        original = path.read_bytes()
        bad = json.loads(original)
        bad['descriptor']['appId'] = 'other'
        path.write_text(json.dumps(bad))
        with self.store.lock(), self.assertRaises(ValueError):
            self.store.pending()
        self.assertTrue(path.exists())

    def test_lock_is_required_and_symlink_receipt_is_not_followed(self):
        with self.assertRaises(ValueError):
            self.store.pending()
        with self.store.lock():
            victim = self.root/'victim'
            victim.write_text('keep')
            (self.root/'local-client/apps/example/nas-operation.json').symlink_to(victim)
            with self.assertRaises((ValueError, OSError)):
                self.store.pending()
        self.assertEqual(victim.read_text(), 'keep')


class NasHostLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root/'source'
        self.repository.mkdir()
        manifest = dict(schemaVersion=1, id='example', title='Example', route='/example', kind='static',
                        build=dict(commands=[['true']], output='dist', environment=[]), healthPath='/example/',
                        production=dict(recipeVersion='1', dataContract='none', rollbackPolicy='compatible',
                                        runtime=None, nativeDependencies=None, buildCommands=[['true']]))
        self.commit = init_git_repo(self.repository, {'local-web.json': json.dumps(manifest)})
        self.host = HostApp('example', self.repository, False, None, (), None, None)
        self.registry = HostRegistry('local.example.ts.net', self.root/'must-not-be-created', (self.host,),
            3, 'https://local.example.ts.net', 'tailscale-serve',
            NasTarget('nas-docker', ('/usr/local/bin/example-adapter',), '172.30.200.0/24', '172.30.200.2'))
        files = self.root/'prepared/files'
        files.mkdir(parents=True)
        (files/'index.html').write_text('compiled')
        self.descriptor = ReleaseDescriptorV1('example', self.commit, '1', None, snapshot_files(files), 'none', 'compatible')
        self.prepared = SimpleNamespace(root=files.parent, files_root=files, descriptor=self.descriptor, runtime_archive=None)
        self.preparer = Mock()
        self.preparer.prepare.return_value = self.prepared
        self.health = Mock()
        self.health.check.return_value = HealthResult(True, 200, None)
        self.calls = []
        self.active = None
        self.previous = None
        self.fail_prepare = False
        self.fail_activate = False
        self.fail_reconcile = False
        self.server_phase = 'absent'

        def request(action, *, operation_id, payload):
            self.calls.append((action, operation_id, payload))
            if action == 'status':
                if 'releaseDigest' in payload:
                    if self.fail_reconcile:
                        raise NasTransportError(action, operation_id, outcome_unknown=True)
                    return dict(state=self.server_phase, phase=self.server_phase,
                        current=self.active.digest if self.active else None,
                        healthy=self.server_phase == 'active', observedHealthy=self.server_phase == 'active')
                return dict(state='active' if self.active else 'idle', current=self.active.digest if self.active else None,
                    descriptor=self.active.to_dict() if self.active else None,
                    previous=self.previous.digest if self.previous else None,
                    previousDescriptor=self.previous.to_dict() if self.previous else None,
                    healthy=self.active is not None, observedHealthy=True if self.active else None, journalHealthy=False)
            receipt = self.root/'client/apps/example/nas-operation.json'
            self.assertTrue(receipt.exists(), 'intent must exist before any NAS mutation')
            if action == 'prepare':
                self.assertEqual(json.loads(receipt.read_text())['phase'], 'preparing')
                if self.fail_prepare:
                    raise NasTransportError(action, operation_id, outcome_unknown=True)
                self.server_phase = 'prepared'
                return dict(missingBlobs=[], runtimeRequired=False)
            if action in ('activate', 'rollback'):
                self.assertEqual(json.loads(receipt.read_text())['phase'], 'activating')
                self.active = ReleaseDescriptorV1.from_dict(payload.get('descriptor', self.descriptor.to_dict()))
                self.server_phase = 'active'
                if self.fail_activate:
                    raise NasTransportError(action, operation_id, outcome_unknown=True)
                return dict(state='active', current=self.active.digest, healthy=True)
            if action == 'finish':
                return dict(finished=True)
            raise AssertionError(action)
        self.transport = SimpleNamespace(request=request)

    def manager(self):
        return NasHostDeploymentManager(self.registry, client_root=self.root/'client',
            preparer=self.preparer, transport=self.transport, health=self.health)

    def test_deploy_builds_exact_main_on_mac_and_records_intent_before_publication(self):
        result = self.manager().deploy('example')
        self.assertEqual(result.outcome, 'deployed')
        self.assertEqual(result.commit, self.commit)
        self.preparer.prepare.assert_called_once()
        self.assertEqual(self.preparer.prepare.call_args.kwargs['commit'], self.commit)
        self.assertFalse(self.registry.runtime_root.exists())
        self.assertFalse((self.root/'client/apps/example/nas-operation.json').exists())
        self.assertEqual([c[0] for c in self.calls], ['status', 'prepare', 'activate', 'status', 'finish'])
        self.health.check.assert_called_with('https://local.example.ts.net/example/')

    def test_failed_publication_preserves_original_receipt_and_blocks_a_new_operation(self):
        self.fail_prepare = True
        with self.assertRaises(ValueError) as caught:
            self.manager().deploy('example')
        original = (self.root/'client/apps/example/nas-operation.json').read_bytes()
        self.assertIn('--recover ' + json.loads(original)['operationId'], str(caught.exception))
        self.fail_prepare = False
        before = len(self.calls)
        with self.assertRaisesRegex(ValueError, 'original-operation'):
            self.manager().deploy('example')
        self.assertEqual(len(self.calls), before)
        self.assertEqual((self.root/'client/apps/example/nas-operation.json').read_bytes(), original)

    def test_disabled_hook_skips_build_and_remote_access(self):
        result = self.manager().deploy('example', from_hook=True)
        self.assertEqual(result.outcome, 'skipped')
        self.assertEqual(self.calls, [])
        self.preparer.prepare.assert_not_called()

    def test_changed_data_contract_blocks_before_creating_an_impossible_pending_intent(self):
        self.active = ReleaseDescriptorV1('example', 'b'*40, '1', None, self.descriptor.files, 'old-contract', 'compatible')
        with self.assertRaisesRegex(ValueError, 'data contract'):
            self.manager().deploy('example')
        self.assertFalse((self.root/'client/apps/example/nas-operation.json').exists())
        self.assertEqual([action for action, _, _ in self.calls], ['status'])
        self.preparer.prepare.assert_not_called()

    def test_recovery_after_prepare_never_arrived_reuses_intent_and_prepared_bytes(self):
        self.fail_prepare = True
        with self.assertRaises(ValueError):
            self.manager().deploy('example')
        path = self.root/'client/apps/example/nas-operation.json'
        operation_id = json.loads(path.read_text())['operationId']
        self.fail_prepare = False
        result = self.manager().recover('example', operation_id)
        self.assertEqual(result.outcome, 'deployed')
        self.preparer.prepare.assert_called_once()
        self.assertEqual([op for action, op, _ in self.calls if action == 'prepare'], [operation_id, operation_id])
        self.assertEqual(len([c for c in self.calls if c[0] == 'activate']), 1)
        self.assertFalse(path.exists())

    def test_unknown_activation_recovers_original_status_without_another_transition(self):
        self.fail_activate = self.fail_reconcile = True
        with self.assertRaises(ValueError):
            self.manager().deploy('example')
        path = self.root/'client/apps/example/nas-operation.json'
        receipt = json.loads(path.read_text())
        self.assertEqual(receipt['phase'], 'activating')
        self.fail_activate = self.fail_reconcile = False
        result = self.manager().recover('example', receipt['operationId'])
        self.assertEqual(result.outcome, 'deployed')
        self.preparer.prepare.assert_called_once()
        self.assertEqual(len([c for c in self.calls if c[0] == 'activate']), 1)
        self.assertFalse(path.exists())

    def test_wrong_recovery_identity_does_not_touch_remote_state(self):
        self.fail_prepare = True
        with self.assertRaises(ValueError):
            self.manager().deploy('example')
        self.calls.clear()
        with self.assertRaises(ValueError):
            self.manager().recover('example', 'different')
        self.assertEqual(self.calls, [])

    def test_status_uses_remote_descriptor_and_fresh_health_without_mac_runtime_paths(self):
        self.active = self.descriptor
        collector = NasStatusCollector(self.registry, transport=self.transport, health=self.health)
        status, = collector.collect()
        self.assertTrue(status.healthy)
        self.assertEqual(status.deployed_commit, self.commit)
        self.assertFalse(self.registry.runtime_root.exists())
        self.assertFalse((self.root/'client').exists())

    def test_rollback_uses_retained_remote_descriptor_without_rebuilding_or_restoring_data(self):
        self.active = ReleaseDescriptorV1('example', 'b'*40, '1', None, self.descriptor.files, 'none', 'compatible')
        expected = self.active.digest
        self.previous = self.descriptor
        result = self.manager().rollback('example')
        self.assertEqual(result.commit, self.commit)
        self.preparer.prepare.assert_not_called()
        rollback, = [c for c in self.calls if c[0] == 'rollback']
        self.assertEqual(rollback[2]['descriptor'], self.descriptor.to_dict())
        self.assertEqual(rollback[2]['expectedCurrent'], expected)

    def test_failed_requested_rollback_reports_recovery_as_failure_after_finishing_its_receipt(self):
        self.active = ReleaseDescriptorV1('example', 'b'*40, '1', None, self.descriptor.files, 'none', 'compatible')
        expected = self.active.digest
        self.previous = self.descriptor
        manager = self.manager()
        publisher = Mock()
        publisher.rollback.side_effect = lambda *args, **kwargs: PublicationResult(
            kwargs['operation_id'], self.descriptor.digest, 'rolled-back', expected)
        manager._publisher = Mock(return_value=publisher)
        with self.assertRaisesRegex(ValueError, 'requested NAS rollback failed.*recovered release'):
            manager.rollback('example')
        self.assertFalse((self.root/'client/apps/example/nas-operation.json').exists())
