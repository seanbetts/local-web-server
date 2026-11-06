import io
import json
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from local_web_server.data_target import DataTargetError, data_target_for_repository
from local_web_server.models import HostApp, HostRegistry
from local_web_server.nas_models import NasTarget
from tests.helpers import init_git_repo


class DataTargetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repository = self.root/'app'; self.repository.mkdir()
        init_git_repo(self.repository, {'local-web.json': json.dumps(dict(
            schemaVersion=1, id='example', title='Example', route='/example', kind='static',
            build=dict(commands=[['true']], output='dist', environment=[]), healthPath='/example/'))})
        self.registry = HostRegistry('local.example.ts.net', self.root/'runtime', (
            HostApp('example', self.repository, False, None, (), None, None),),
            schema_version=3, public_origin='https://local.example.ts.net', ingress_mode='tailscale-serve')
        self.snapshot = SimpleNamespace(revisions=(SimpleNamespace(revision_id='a'*64),))

    def test_exact_registered_repository_resolves_a_revision_bound_full_app_url(self):
        target = data_target_for_repository(self.registry, self.snapshot, self.repository)
        self.assertEqual(target, dict(schemaVersion=1, appId='example', targetKind='macos',
                                      baseUrl='https://local.example.ts.net/example', profileRevision='a'*64))

    def test_nas_and_legacy_lan_preserve_explicit_host_authority(self):
        nas = replace(self.registry, target=NasTarget(kind='nas-docker'))
        self.assertEqual(data_target_for_repository(nas, self.snapshot, self.repository)['targetKind'], 'nas-docker')
        lan = replace(self.registry, host='example.local', public_origin='http://example.local', ingress_mode='trusted-lan')
        self.assertEqual(data_target_for_repository(lan, self.snapshot, self.repository)['baseUrl'], 'http://example.local/example')
        with self.assertRaises(DataTargetError):
            data_target_for_repository(replace(lan, target=NasTarget(kind='nas-docker')), self.snapshot, self.repository)

    def test_unregistered_or_unhealthy_profile_never_supplies_a_local_fallback(self):
        other = self.root/'other'; other.mkdir()
        for registry, snapshot, repository in ((self.registry, self.snapshot, other),
                (self.registry, SimpleNamespace(revisions=()), self.repository),
                (replace(self.registry, public_origin='https://user:password@example.test'), self.snapshot, self.repository)):
            with self.subTest(repository=repository), self.assertRaises(DataTargetError) as raised:
                data_target_for_repository(registry, snapshot, repository)
            self.assertNotIn(str(self.root), str(raised.exception))

    def test_cli_publishes_only_bounded_data_target_metadata(self):
        from local_web_server import cli
        output = io.StringIO()
        with patch.object(cli, '_load_default_host_profile_snapshot', return_value=(self.root/'private.json', self.registry, self.snapshot)), redirect_stdout(output):
            self.assertEqual(cli.main(['app', 'data-target', '--repository', str(self.repository)]), 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['baseUrl'], 'https://local.example.ts.net/example')
        self.assertNotIn(str(self.root), output.getvalue())

    def test_cli_nas_doctor_uses_explicit_source_target_without_host_effects(self):
        from local_web_server import cli
        report = SimpleNamespace(compatible=True, app='example', diagnostics=())
        with patch.object(cli, 'AppDoctor') as doctor, redirect_stdout(io.StringIO()):
            doctor.return_value.inspect.return_value = report
            self.assertEqual(cli.main(['app', 'doctor', '--target', 'nas-docker', '--repository', str(self.repository)]), 0)
        doctor.assert_called_once_with(target_kind='nas-docker')
