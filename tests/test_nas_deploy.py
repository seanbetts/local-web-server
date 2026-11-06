import dataclasses
import hashlib
import sys
from pathlib import Path
import tarfile
import tempfile
import unittest

from local_web_server.nas_deploy import NasDeploymentManager, NasPublicationError
from local_web_server.nas_transport import NasTransport, NasTransportError
from local_web_server.nas_models import NasTarget
from local_web_server.release_manifest import FileEntry, ReleaseDescriptorV1, RuntimeIdentity


class AdapterBoundary:
    def __init__(self, responses, inspect=None):
        self.responses = responses
        self.calls = []
        self.inspect = inspect

    def request(self, action, *, operation_id, payload):
        self.calls.append((action, operation_id, payload))
        if self.inspect:
            self.inspect(action, payload)
        value = self.responses[action]
        if isinstance(value, Exception):
            raise value
        return value


class NasDeploymentTests(unittest.TestCase):
    def fixture(self, root):
        files = root / 'files'
        files.mkdir()
        (files / 'a').write_bytes(b'new')
        (files / 'b').write_bytes(b'unchanged')
        (files / 'a').chmod(0o644)
        (files / 'b').chmod(0o644)
        entries = tuple(FileEntry(name, hashlib.sha256(body).hexdigest(), len(body), 0o644)
                        for name, body in (('a', b'new'), ('b', b'unchanged')))
        return files, ReleaseDescriptorV1('example', 'a' * 40, '1', None, entries, 'example-v1', 'compatible')

    def responses(self, descriptor, missing=()):
        return dict(prepare=dict(missingBlobs=list(missing), runtimeRequired=False),
                    upload=dict(uploaded=True),
                    activate=dict(state='active', current=descriptor.digest, healthy=True),
                    status=dict(state='active', current=descriptor.digest, healthy=True),
                    finish=dict(finished=True),
                    rollback=dict(state='active', current=descriptor.digest, healthy=True))

    def test_upload_bundle_contains_only_missing_unique_bytes_and_no_removed_asset(self):
        with tempfile.TemporaryDirectory() as temporary:
            files, descriptor = self.fixture(Path(temporary))
            missing = descriptor.files[0].sha256
            bundles = []
            def inspect(action, payload):
                if action == 'prepare':
                    self.assertEqual(payload['descriptor'], descriptor.to_dict())
                    self.assertEqual(payload['expectedCurrent'], 'b' * 64)
                if action == 'upload':
                    bundle = payload['bundle']
                    bundles.append(Path(bundle['path']))
                    self.assertEqual(hashlib.sha256(Path(bundle['path']).read_bytes()).hexdigest(), bundle['sha256'])
                    with tarfile.open(bundle['path'], 'r:gz') as archive:
                        self.assertEqual(archive.getnames(), ['blobs/' + missing])
                        self.assertEqual(archive.extractfile('blobs/' + missing).read(), b'new')
                    self.assertEqual(bundle['blobs'], [missing])
            adapter = AdapterBoundary(self.responses(descriptor, [missing]), inspect)
            result = NasDeploymentManager(adapter).deploy(descriptor, files, operation_id='op', expected_current='b' * 64)
            self.assertEqual(result.state, 'active')
            self.assertEqual(result.current, descriptor.digest)
            self.assertEqual([call[0] for call in adapter.calls], ['prepare', 'upload', 'activate', 'finish'])
            self.assertFalse(bundles[0].exists())

    def test_warm_publication_skips_all_artifact_uploads(self):
        with tempfile.TemporaryDirectory() as temporary:
            files, descriptor = self.fixture(Path(temporary))
            adapter = AdapterBoundary(self.responses(descriptor))
            result = NasDeploymentManager(adapter).deploy(descriptor, files, operation_id='op', expected_current=None)
            self.assertEqual(result.state, 'active')
            self.assertEqual([call[0] for call in adapter.calls], ['prepare', 'activate', 'finish'])

    def test_lost_activation_ack_uses_status_without_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            files, descriptor = self.fixture(Path(temporary))
            responses = self.responses(descriptor)
            responses['activate'] = NasTransportError('activate', 'op', outcome_unknown=True)
            adapter = AdapterBoundary(responses)
            result = NasDeploymentManager(adapter).deploy(descriptor, files, operation_id='op', expected_current=None)
            self.assertEqual(result.state, 'active')
            self.assertEqual([call[0] for call in adapter.calls], ['prepare', 'activate', 'status', 'finish'])
            self.assertEqual({call[1] for call in adapter.calls}, {'op'})

    def test_interrupted_upload_keeps_current_by_never_requesting_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            files, descriptor = self.fixture(Path(temporary))
            responses = self.responses(descriptor, [descriptor.files[0].sha256])
            responses['upload'] = NasTransportError('upload', 'op', outcome_unknown=True)
            adapter = AdapterBoundary(responses)
            with self.assertRaises(NasTransportError):
                NasDeploymentManager(adapter).deploy(descriptor, files, operation_id='op', expected_current='b' * 64)
            self.assertEqual([call[0] for call in adapter.calls], ['prepare', 'upload'])

    def test_unknown_or_unhealthy_status_retains_recovery_without_finish(self):
        with tempfile.TemporaryDirectory() as temporary:
            files, descriptor = self.fixture(Path(temporary))
            for state in ('pending', 'recovery-required', 'active'):
                responses = self.responses(descriptor)
                responses['activate'] = NasTransportError('activate', 'op', outcome_unknown=True)
                responses['status'] = dict(state=state, current=descriptor.digest, healthy=False)
                adapter = AdapterBoundary(responses)
                with self.subTest(state=state), self.assertRaises(NasPublicationError) as caught:
                    NasDeploymentManager(adapter).deploy(descriptor, files, operation_id='op', expected_current='b' * 64)
                self.assertEqual(caught.exception.operation_id, 'op')
                self.assertEqual(caught.exception.release_digest, descriptor.digest)
                self.assertEqual([call[0] for call in adapter.calls], ['prepare', 'activate', 'status'])

    def test_migration_required_refuses_ordinary_deploy_and_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            files, descriptor = self.fixture(Path(temporary))
            descriptor = dataclasses.replace(descriptor, rollback_policy='migration-required')
            adapter = AdapterBoundary({})
            manager = NasDeploymentManager(adapter)
            with self.assertRaises(ValueError):
                manager.deploy(descriptor, files, operation_id='op', expected_current=None)
            with self.assertRaises(ValueError):
                manager.rollback(descriptor, operation_id='op', expected_current='b' * 64)
            self.assertEqual(adapter.calls, [])

    def test_changed_source_invalid_missing_blob_and_runtime_miss_are_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            files, descriptor = self.fixture(Path(temporary))
            (files / 'a').write_bytes(b'changed after build')
            adapter = AdapterBoundary(self.responses(descriptor))
            with self.assertRaises(ValueError):
                NasDeploymentManager(adapter).deploy(descriptor, files, operation_id='op', expected_current=None)
            self.assertEqual(adapter.calls, [])
            (files / 'a').write_bytes(b'new')
            responses = self.responses(descriptor, ['f' * 64])
            adapter = AdapterBoundary(responses)
            with self.assertRaises(ValueError):
                NasDeploymentManager(adapter).deploy(descriptor, files, operation_id='op', expected_current=None)
            self.assertEqual([call[0] for call in adapter.calls], ['prepare'])
            responses = self.responses(descriptor)
            responses['prepare']['runtimeRequired'] = True
            adapter = AdapterBoundary(responses)
            with self.assertRaises(ValueError):
                NasDeploymentManager(adapter).deploy(descriptor, files, operation_id='op', expected_current=None)
            self.assertEqual([call[0] for call in adapter.calls], ['prepare'])

    def test_runtime_archive_is_checksum_bound_and_uploaded_only_on_cache_miss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files, descriptor = self.fixture(root)
            archive = root / 'image.tar'
            archive.write_bytes(b'prepared Linux image')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            runtime = RuntimeIdentity('sha256:' + 'b' * 64, 'sha256:' + digest, 'linux', 'amd64', 'node-24', 'c' * 64, '1')
            descriptor = dataclasses.replace(descriptor, runtime=runtime)
            responses = self.responses(descriptor)
            responses['prepare']['runtimeRequired'] = True
            adapter = AdapterBoundary(responses)
            NasDeploymentManager(adapter).deploy(descriptor, files, operation_id='op', expected_current=None, runtime_archive=archive)
            upload = next(payload for action, _, payload in adapter.calls if action == 'upload')
            self.assertIsNone(upload['bundle'])
            self.assertEqual(upload['runtimeArtifact']['sha256'], digest)
            archive.write_bytes(b'corrupt')
            adapter = AdapterBoundary(responses)
            with self.assertRaises(ValueError):
                NasDeploymentManager(adapter).deploy(descriptor, files, operation_id='op', expected_current=None, runtime_archive=archive)
            self.assertEqual([call[0] for call in adapter.calls], ['prepare'])

    def test_explicit_code_rollback_sends_contract_and_expected_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            _, descriptor = self.fixture(Path(temporary))
            adapter = AdapterBoundary(self.responses(descriptor))
            result = NasDeploymentManager(adapter).rollback(descriptor, operation_id='op', expected_current='b' * 64)
            self.assertEqual(result.current, descriptor.digest)
            self.assertEqual(adapter.calls[0][2]['descriptor']['dataContract'], 'example-v1')
            self.assertEqual(adapter.calls[0][2]['expectedCurrent'], 'b' * 64)
            self.assertEqual([call[0] for call in adapter.calls], ['rollback', 'finish'])

    def test_explicit_unknown_transition_ack_reconciles_original_status_without_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files, descriptor = self.fixture(root)
            calls = root / 'calls'
            script = root / 'adapter.py'
            script.write_text(
                'import json,sys\nfrom pathlib import Path\nrequest=json.load(sys.stdin)\n'
                + 'with Path(' + repr(str(calls)) + ').open("a") as output: output.write(request["action"] + " " + request["operationId"] + "\\n")\n'
                + 'ack={key:request[key] for key in ("schemaVersion","action","operationId")}\n'
                + 'if request["action"]=="prepare": ack.update(ok=True,result={"missingBlobs":[],"runtimeRequired":False})\n'
                + 'elif request["action"]=="activate": ack.update(ok=False,error="transition_requires_status",outcomeUnknown=True)\n'
                + 'else: ack.update(ok=True,result={"state":"recovery-required","current":request["payload"]["releaseDigest"],"healthy":False})\n'
                + 'json.dump(ack,sys.stdout)\n')
            transport = NasTransport(NasTarget(kind='nas-docker', adapter_command=(sys.executable, str(script))))
            with self.assertRaises(NasPublicationError) as caught:
                NasDeploymentManager(transport).deploy(descriptor, files, operation_id='original', expected_current=None)
            self.assertEqual(caught.exception.operation_id, 'original')
            self.assertEqual(caught.exception.release_digest, descriptor.digest)
            self.assertEqual(calls.read_text().splitlines(), ['prepare original', 'activate original', 'status original'])


if __name__ == '__main__':
    unittest.main()
