"""Prepared schema-3 configuration publication in disposable private stores."""
import json
from unittest.mock import patch

from local_web_server.host_commands import HostCommands, HostCommandError
from local_web_server.host_profile import canonical_json_bytes
from local_web_server.host_profile_backup import parse_host_backup
from tests.test_host_commands import forbid_writes
from tests import test_host_commands
from tests.test_host_profile_store import StoreFixture, BEFORE, CLOCK, digest


class HostConfigureTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.store.initialise(BEFORE)
        self.commands = HostCommands(self.root, clock=CLOCK)
        self.source = self.root / 'prepared.json'
        self.payload = dict(json.loads(BEFORE), schemaVersion=3, ingressMode='trusted-lan',
                            target={'kind': 'macos'}, services=[])
        self.write_source()

    def write_source(self):
        self.write_private(self.source, canonical_json_bytes(self.payload))

    def preview(self):
        return self.commands.preview_configuration(self.source)

    def test_configuration_preview_is_read_only_and_has_bounded_digests(self):
        with forbid_writes():
            plan = self.preview()
        self.assertEqual(plan.operation, 'configure')
        self.assertEqual(plan.revision_id, self.store.revisions()[-1].revision_id)
        self.assertEqual(plan.profile_sha256, digest(self.source.read_bytes()))
        self.assertNotIn(str(self.source), json.dumps(plan.as_dict()))
        self.assertEqual(self.store.read_current(), BEFORE)

    def test_configuration_applies_exact_profile_with_backup_and_new_audit_revision(self):
        plan = self.preview()
        result = self.commands.apply(plan)
        self.assertTrue(result.applied)
        self.assertEqual(parse_host_backup(result.backup_path).profile_bytes, BEFORE)
        self.assertEqual(self.store.read_current(), self.source.read_bytes())
        revisions = self.store.revisions()
        self.assertEqual(len(revisions), 2)
        self.assertEqual(revisions[-1].operation, 'profile-configuration')
        self.assertIsNone(revisions[-1].app_id)
        self.assertFalse(self.commands.apply(self.preview()).applied)
        self.assertEqual(len(self.store.revisions()), 2)

    def test_configuration_preserves_origin_ingress_and_app_records(self):
        for replacement in ({'host': 'changed.example'}, {'apps': [{'id': 'new-app', 'repository': '/tmp/new-app', 'autoDeploy': False}]}, {'schemaVersion': 1}):
            with self.subTest(replacement=replacement):
                self.payload = dict(json.loads(BEFORE), schemaVersion=3, ingressMode='trusted-lan', target={'kind': 'macos'}, services=[])
                self.payload.update(replacement)
                self.write_source()
                with self.assertRaises(HostCommandError):
                    self.preview()
        self.assertEqual(self.store.read_current(), BEFORE)

    def test_configuration_can_pause_and_restore_auto_deploy_without_changing_app_identity(self):
        before = canonical_json_bytes(dict(json.loads(BEFORE), apps=[dict(id='example', repository='/tmp/example', autoDeploy=True)]))
        self.store.publish_registration('example', BEFORE, before)
        self.payload['apps'] = [dict(id='example', repository='/tmp/example', autoDeploy=False)]
        self.write_source()
        self.commands.apply(self.preview())
        self.assertFalse(json.loads(self.store.read_current())['apps'][0]['autoDeploy'])
        self.payload['apps'][0]['autoDeploy'] = True
        self.write_source()
        self.commands.apply(self.preview())
        self.assertTrue(json.loads(self.store.read_current())['apps'][0]['autoDeploy'])
        self.payload['apps'][0]['repository'] = '/tmp/another-repository'
        self.write_source()
        with self.assertRaises(HostCommandError):
            self.preview()

    def test_configuration_allows_runtime_root_change_and_marks_it_without_paths(self):
        self.payload['runtimeRoot'] = '/tmp/new-runtime'
        self.write_source()
        plan = self.preview()
        self.assertTrue(plan.as_dict()['runtimeRootChanged'])
        self.assertNotIn('/tmp/new-runtime', json.dumps(plan.as_dict()))

    def test_configuration_revalidates_source_and_aborts_before_backup_if_changed(self):
        plan = self.preview()
        self.payload['runtimeRoot'] = '/tmp/changed'
        self.write_source()
        with self.assertRaises(HostCommandError):
            self.commands.apply(plan)
        self.assertEqual(self.store.read_current(), BEFORE)
        self.assertEqual(list(self.paths.backups.iterdir()), [])

    def test_configuration_backup_failure_never_publishes_candidate(self):
        plan = self.preview()
        with patch('local_web_server.host_commands.write_host_backup', side_effect=OSError('private-path')):
            with self.assertRaises(HostCommandError):
                self.commands.apply(plan)
        self.assertEqual(self.store.read_current(), BEFORE)
        self.assertEqual(len(self.store.revisions()), 1)

    def test_configuration_validates_target_and_services_and_rejects_unknown_fields(self):
        self.payload['services'] = [{'id': 'jellyfin', 'title': 'Jellyfin',
            'url': 'https://local.example.ts.net:8444', 'icon': 'video', 'accent': '#75A7FF'}]
        self.write_source()
        self.assertEqual(self.commands.apply(self.preview()).plan.operation, 'configure')
        current = self.store.read_current()
        self.payload['target'] = {'kind': 'unknown-target'}
        self.write_source()
        with self.assertRaises(HostCommandError):
            self.preview()
        self.assertEqual(self.store.read_current(), current)

    def test_configuration_rejects_a_stale_current_chain(self):
        plan = self.preview()
        changed = BEFORE + b' '
        self.store.publish_registration('example', BEFORE, changed)
        with self.assertRaises(HostCommandError):
            self.commands.apply(plan)
        self.assertEqual(self.store.read_current(), changed)

    def test_configuration_supports_schema_two_to_nas_without_changing_origin(self):
        before = canonical_json_bytes(dict(schemaVersion=2, publicOrigin='https://local.example.ts.net',
            ingressMode='tailscale-serve', runtimeRoot='/tmp/runtime', apps=[]))
        self.store.publish_registration('example', BEFORE, before)
        self.payload = dict(json.loads(before), schemaVersion=3, runtimeRoot='/tmp/nas-runtime',
            target=dict(kind='nas-docker', adapterCommand=['/usr/local/bin/local-web-nas'],
                networkSubnet='172.30.200.0/24', proxyAddress='172.30.200.2'), services=[])
        self.write_source()
        result = self.commands.apply(self.preview())
        self.assertTrue(result.applied)
        self.assertEqual(parse_host_backup(result.backup_path).profile_bytes, before)
        self.assertEqual(self.store.read_current(), self.source.read_bytes())


class HostConfigureCliTests(StoreFixture):
    run_cli = test_host_commands.HostCliIntegrationTests.run_cli

    def setUp(self):
        super().setUp()
        self.source = self.root / "prepared.json"

    def test_cli_configure_requires_exact_preview_revision_and_candidate_digest(self):
        self.store.initialise(BEFORE)
        candidate = canonical_json_bytes(dict(json.loads(BEFORE), schemaVersion=3,
            ingressMode='trusted-lan', target={'kind': 'macos'}, services=[]))
        self.write_private(self.source, candidate)
        args = ['host', 'configure', '--from', str(self.source)]
        with forbid_writes():
            code, output, error = self.run_cli(args)
        self.assertEqual((code, error), (0, ''))
        plan = json.loads(output)
        for flags in ([], ['--expected-revision', '0' * 64, '--expected-profile-sha256', digest(candidate)],
                      ['--expected-revision', plan['revisionId'], '--expected-profile-sha256', '0' * 64]):
            code, output, error = self.run_cli(args + ['--apply'] + flags)
            self.assertEqual((code, output), (2, ''))
            self.assertNotIn(str(self.source), error)
            self.assertEqual(self.store.read_current(), BEFORE)
        code, output, error = self.run_cli(args + ['--apply', '--expected-revision', plan['revisionId'], '--expected-profile-sha256', plan['profileSha256']])
        self.assertEqual((code, error), (0, ''))
        self.assertTrue(json.loads(output)['applied'])
