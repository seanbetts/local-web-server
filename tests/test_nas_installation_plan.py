"""Source-only installation authorization; all publication is injected."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from local_web_server.nas_install import PreparedNasPlatform, PlatformFile, PathPermission
from local_web_server.nas_platform_installation import (
    InstallationApproval, InstallationEvidence, HostnameHandoff,
    preview_nas_installation, apply_nas_installation, select_runtime_specs,
)
from local_web_server.nas_render import ContainerRuntimeSpec
from local_web_server.release_manifest import ReleaseDescriptorV1, FileEntry
import hashlib


def sha(data): return hashlib.sha256(data).hexdigest()


def platform():
    settings = dict(schemaVersion=1, publicOrigin='https://local.example.ts.net', proxyAddress='172.30.200.2', networkSubnet='172.30.200.0/24', apps={})
    files = tuple(PlatformFile(name, data, 0o600) for name, data in sorted({
        'compose.yaml': b'{}', 'Caddyfile': b'127.0.0.1:8080 {}', 'serve.json': b'{}',
        'runtime-settings.json': json.dumps(settings).encode()}.items()))
    desc = ReleaseDescriptorV1('local-web-platform', 'a'*40, 'nas-platform-v1', None,
        tuple(FileEntry(f.path, sha(f.content), len(f.content), f.mode) for f in files), 'local-web-platform-v1', 'compatible')
    return PreparedNasPlatform(desc, files, (PathPermission('.', 0o700, 0, 0),))


class InstallationPlanTests(unittest.TestCase):
    def preview(self, **changes):
        args = dict(prepared=platform(), store_root='/volume2/docker/local-web', profile_revision='b'*64,
            expected_current=None, previous_config_digest=None, evidence=(), hostname_handoff=None)
        args.update(changes)
        return preview_nas_installation(**args)

    def test_missing_prerequisites_are_explicit_and_apply_cannot_publish(self):
        plan = self.preview()
        self.assertFalse(plan.ready)
        self.assertIn('backup-coverage', plan.missing_prerequisites)
        self.assertIn('hostname-inventory', plan.missing_prerequisites)
        self.assertIn('host-profile-export', plan.missing_prerequisites)
        manager = Mock()
        with self.assertRaises(ValueError):
            apply_nas_installation(plan, approved_capsule_digest=plan.approval.digest,
                profile_revision='b'*64, expected_current=None, manager=manager, operation_id='install')
        manager.deploy.assert_not_called()
        backup = {entry.path: entry.strategy for entry in plan.backup_requirements}
        self.assertEqual(backup['tailscale'], 'essentials')
        self.assertEqual(backup['installation-approvals'], 'essentials')
        self.assertIn('host-profile/history', backup)

    def test_exact_capsule_apply_uses_existing_six_action_manager_and_rejects_stale_review(self):
        evidence = tuple(InstallationEvidence(name, 'verified', 'c'*64) for name in InstallationEvidence.REQUIRED)
        handoff = HostnameHandoff('local', 'local-mac-retained', 'local-shadow')
        plan = self.preview(evidence=evidence, hostname_handoff=handoff)
        self.assertTrue(plan.ready)
        self.assertEqual(InstallationApproval.from_dict(plan.approval.to_dict()), plan.approval)
        manager = Mock(); manager.deploy.return_value = 'published'
        for changes in (dict(approved_capsule_digest='d'*64), dict(profile_revision='d'*64), dict(expected_current='d'*64)):
            args = dict(approved_capsule_digest=plan.approval.digest, profile_revision='b'*64, expected_current=None)
            args.update(changes)
            with self.assertRaises(ValueError): apply_nas_installation(plan, manager=manager, operation_id='install', **args)
        manager.deploy.assert_not_called()
        self.assertEqual(apply_nas_installation(plan, approved_capsule_digest=plan.approval.digest,
            profile_revision='b'*64, expected_current=None, manager=manager, operation_id='install'), 'published')
        call = manager.deploy.call_args
        self.assertEqual(call.kwargs['installation_approval'], plan.approval)
        self.assertEqual(call.kwargs['expected_current'], None)
        self.assertEqual(call.args[0], plan.prepared.descriptor)
        self.assertFalse(call.args[1].exists())  # temporary local artifact retired after exact publication
        self.assertTrue(any('local-mac-retained' in step for step in plan.hostname_steps))

    def test_approval_rejects_extra_keys_wrong_release_root_and_permission_changes(self):
        approval = self.preview().approval
        for change in ({'extra': True}, {'storeRoot': '/tmp/other'}, {'schemaVersion': True}, {'expectedCurrent': 'x'}):
            raw = approval.to_dict(); raw.update(change)
            with self.assertRaises(ValueError): InstallationApproval.from_dict(raw)
        self.assertNotEqual(replace(approval, profile_revision='d'*64).digest, approval.digest)
        with self.assertRaises(ValueError): self.preview(prepared=replace(platform(), descriptor=replace(platform().descriptor, app_id='other')))

    def test_shadow_preview_keeps_original_handoff_names_without_authorizing_retained_mac(self):
        # Fixture origin is local.example.ts.net: here "local" is deliberately
        # the temporary node, while the original production node is different.
        handoff = HostnameHandoff('production', 'retained-mac', 'local')
        plan = self.preview(hostname_handoff=handoff)
        self.assertEqual(plan.hostname_steps, handoff.steps())
        self.assertEqual(plan.prepared.source_config_digest, platform().source_config_digest)
        with self.assertRaises(ValueError):
            self.preview(hostname_handoff=HostnameHandoff('production', 'local', 'shadow'))

    def test_unselected_specs_remain_observed_and_selected_candidate_is_exact(self):
        a = ContainerRuntimeSpec('a', 'sha256:'+'a'*64, ('node','/app/current/server.js'), 42001, 1, 512, health_runtime='node')
        b = replace(a, app_id='b', port=42002)
        candidate = Mock(runtime_spec=replace(a, image='sha256:'+'c'*64), descriptor=Mock(app_id='a', runtime=Mock(image_digest='sha256:'+'c'*64)))
        result = select_runtime_specs(('a','b'), {'a':a,'b':b}, selected_candidate=candidate)
        self.assertEqual(result, (candidate.runtime_spec,b))
        with self.assertRaises(ValueError): select_runtime_specs(('a','b'), {'a':a}, selected_candidate=candidate)
        for change in (dict(cpu_limit=2),dict(memory_limit_mib=1024),dict(user='10002:10002'),dict(secret_files=('new-key',)),dict(health_runtime='python3')):
            candidate.runtime_spec = replace(a,image='sha256:'+'c'*64,**change)
            with self.assertRaises(ValueError): select_runtime_specs(('a','b'),{'a':a,'b':b},selected_candidate=candidate)
        candidate.runtime_spec = replace(a, app_id='b')
        with self.assertRaises(ValueError): select_runtime_specs(('a','b'), {'a':a,'b':b}, selected_candidate=candidate)

    def test_evidence_cannot_be_called_verified_without_pinned_observation(self):
        with self.assertRaises(ValueError): InstallationEvidence('backup-coverage', 'verified', None)
        with self.assertRaises(ValueError): InstallationEvidence('invented', 'verified', 'a'*64)
        with self.assertRaises(ValueError): self.preview(evidence=(InstallationEvidence('storage','missing',None),)*2)
        with self.assertRaises(ValueError): HostnameHandoff('local','local','local-shadow')

    def test_reserved_prepare_is_typed_and_platform_session_retains_original_intent_for_recovery(self):
        from local_web_server.nas_deploy import NasDeploymentManager
        from local_web_server.nas_transport import NasTransportError
        from local_web_server.nas_platform_installation import NasPlatformInstallationSession
        from tests.test_nas_deploy import AdapterBoundary
        plan = self.preview(evidence=tuple(InstallationEvidence(name,'verified','c'*64) for name in InstallationEvidence.REQUIRED),
                            hostname_handoff=HostnameHandoff('local','local-mac-retained','local-shadow'))
        release = plan.prepared.descriptor.digest
        responses = dict(prepare=dict(missingBlobs=[],runtimeRequired=False),
            activate=NasTransportError('activate','install',outcome_unknown=True),
            status=NasTransportError('status','install',outcome_unknown=True),finish=dict(finished=True))
        adapter = AdapterBoundary(responses)
        with tempfile.TemporaryDirectory() as directory:
            session = NasPlatformInstallationSession(Path(directory)/'client',adapter,public_health=lambda _:True)
            with self.assertRaises(Exception):
                session.apply(plan,approved_capsule_digest=plan.approval.digest,profile_revision='b'*64,expected_current=None,operation_id='install')
            with session.store.lock():
                receipt = session.store.pending()
                self.assertEqual(receipt['operationId'],'install')
                self.assertEqual(receipt['descriptor']['appId'],'local-web-platform')
                self.assertTrue(Path(receipt['preparedRoot']).is_dir())
            self.assertEqual(adapter.calls[0][2]['installationApproval'],plan.approval.to_dict())
            adapter.calls.clear()
            responses['status']=dict(state='active',current=release,healthy=True,phase='active')
            changed=NasPlatformInstallationSession(Path(directory)/'client',adapter,public_health=lambda _:True,
                                                  profile_revision='d'*64)
            with self.assertRaisesRegex(ValueError,'original.*profile'):
                changed.recover('install')
            self.assertEqual(adapter.calls,[])
            self.assertEqual(session.recover('install').current,release)
            self.assertEqual([call[0] for call in adapter.calls],['status','finish'])
            with session.store.lock(): self.assertIsNone(session.store.pending())

    def test_saved_context_verifies_metadata_versions_and_refuses_symlink_or_target_drift(self):
        from local_web_server.nas_platform_installation import SavedNasInstallationContext, KnownRuntimeVersion
        from local_web_server.nas_install import PathPermission
        from dataclasses import asdict
        from local_web_server.release_manifest import RuntimeIdentity
        spec=ContainerRuntimeSpec('notes','sha256:'+'a'*64,('node','/app/current/server.js'),42001,1,512,health_runtime='node')
        metadata=(json.dumps(asdict(spec),sort_keys=True,separators=(',',':'))+'\n').encode()
        identity=RuntimeIdentity('sha256:'+'a'*64,'sha256:'+'b'*64,'linux','amd64','node24','c'*64,'v1')
        desc=ReleaseDescriptorV1('notes','d'*40,'v1',identity,(FileEntry('.local-web-runtime.json',sha(metadata),len(metadata),0o644),),'notes-v1','compatible')
        known=KnownRuntimeVersion(desc,spec)
        with self.assertRaises(ValueError): KnownRuntimeVersion(desc,replace(spec,cpu_limit=2))
        ctx=SavedNasInstallationContext('https://local.example.ts.net','172.30.200.0/24','172.30.200.2',
            'caddy@sha256:'+'a'*64,'tailscale@sha256:'+'b'*64,(),HostnameHandoff('local','local-mac','local-shadow'),(known,),
            ((platform().descriptor.digest,platform().source_config_digest),))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            ctx.save(root)
            loaded=SavedNasInstallationContext.load(root)
            self.assertEqual(loaded,ctx)
            self.assertEqual(loaded.observed_runtime_specs({'notes':desc}),{'notes':spec})
            with self.assertRaises(ValueError): loaded.observed_runtime_specs({'notes':replace(desc,source_commit='e'*40)})
            path=root/'installation-context.json'
            path.unlink(); path.symlink_to(root/'elsewhere')
            with self.assertRaises((ValueError,OSError)): SavedNasInstallationContext.load(root)

    def test_platform_installation_dispatches_nas_before_any_mac_build_or_service_path(self):
        from local_web_server.platform_installation import install_platform
        from tests.test_nas_platform_installation import NasPlatformInstallationTests
        from unittest.mock import patch
        fixture=NasPlatformInstallationTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        profile=fixture.repository/'profile.json';profile.write_text('{}')
        selected=Mock()
        with patch('local_web_server.platform_installation.require_committed_installation_sources'), \
             patch('local_web_server.platform_installation.resolve_host_profile',return_value=profile), \
             patch('local_web_server.platform_installation.load_registry',return_value=fixture.registry), \
             patch('local_web_server.platform_installation.load_registry_bytes',return_value=fixture.registry), \
             patch('local_web_server.nas_platform_installation.install_saved_nas_platform',return_value='nas') as nas, \
             patch('local_web_server.platform_installation.ProcessRunner.run',side_effect=AssertionError('Mac build path')), \
             patch('local_web_server.platform_installation.Installer',side_effect=AssertionError('Mac installer')):
            self.assertEqual(install_platform(fixture.repository,dry_run=False,registry_path=profile,selected_candidate=selected),'nas')
        self.assertIs(nas.call_args.kwargs['selected_candidate'],selected)
        self.assertEqual(nas.call_args.kwargs['profile_revision'],sha(b'{}'))

        def change_binding(*args,**kwargs):
            binding.return_value=profile.with_name('other-profile.json')
            kwargs['validate_profile']()
        with patch('local_web_server.platform_installation.require_committed_installation_sources'), \
             patch('local_web_server.platform_installation.resolve_host_profile',return_value=profile) as binding, \
             patch('local_web_server.platform_installation.load_registry',return_value=fixture.registry), \
             patch('local_web_server.platform_installation.load_registry_bytes',return_value=fixture.registry), \
             patch('local_web_server.nas_platform_installation.install_saved_nas_platform',side_effect=change_binding):
            from local_web_server.install import InstallError
            with self.assertRaisesRegex(InstallError,'profile changed'):
                install_platform(fixture.repository,dry_run=False,registry_path=profile,selected_candidate=selected)

    def test_nas_apply_refuses_feature_branch_before_publication(self):
        from local_web_server.platform_installation import install_platform
        from local_web_server.install import InstallError
        from tests.test_nas_platform_installation import NasPlatformInstallationTests
        from unittest.mock import patch
        import subprocess
        fixture=NasPlatformInstallationTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        subprocess.run(['git','checkout','-b','source-feature'],cwd=fixture.repository,check=True,capture_output=True)
        profile=fixture.repository/'profile.json';profile.write_text('{}')
        with patch('local_web_server.platform_installation.resolve_host_profile',return_value=profile), \
             patch('local_web_server.platform_installation.load_registry',return_value=fixture.registry), \
             patch('local_web_server.platform_installation.load_registry_bytes',return_value=fixture.registry), \
             patch('local_web_server.nas_platform_installation.install_saved_nas_platform') as publish:
            with self.assertRaises(InstallError):install_platform(fixture.repository,dry_run=False,registry_path=profile)
            publish.assert_not_called()

    def test_saved_default_refresh_uses_selected_candidate_and_observed_unselected_metadata(self):
        from local_web_server.nas_platform_installation import SavedNasInstallationContext,KnownRuntimeVersion,install_saved_nas_platform
        from local_web_server.release_manifest import RuntimeIdentity
        from tests.test_nas_platform_installation import NasPlatformInstallationTests
        from types import SimpleNamespace
        from dataclasses import asdict
        from unittest.mock import patch
        fixture=NasPlatformInstallationTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        registry=replace(fixture.registry,runtime_root=Path('/volume2/docker/local-web'),
            apps=(replace(fixture.registry.apps[0],start_command=('node','server.js')),))
        oldspec=fixture.runtimes[0]
        def release(spec,commit):
            meta=(json.dumps(asdict(spec),sort_keys=True,separators=(',',':'))+'\n').encode()
            identity=RuntimeIdentity(spec.image,'sha256:'+'f'*64,'linux','amd64','node24','e'*64,'v1')
            return ReleaseDescriptorV1(spec.app_id,commit,'v1',identity,(FileEntry('.local-web-runtime.json',sha(meta),len(meta),0o644),),'notes-v1','compatible')
        old=release(oldspec,'a'*40)
        candidate=SimpleNamespace(runtime_spec=replace(oldspec,image='sha256:'+'d'*64),descriptor=None)
        candidate.descriptor=release(candidate.runtime_spec,'b'*40)
        prior=fixture.prepare(registry=registry)
        context=SavedNasInstallationContext(registry.public_origin,registry.target.network_subnet,registry.target.proxy_address,
            'caddy@sha256:'+'b'*64,'tailscale@sha256:'+'c'*64,
            tuple(InstallationEvidence(name,'verified','c'*64) for name in InstallationEvidence.REQUIRED),
            HostnameHandoff('local','local-mac','local-shadow'),(KnownRuntimeVersion(old,oldspec),),((prior.descriptor.digest,prior.source_config_digest),))
        live=SimpleNamespace(descriptor=prior.descriptor,observed_healthy=True)
        observe=lambda _transport,app:live if app=='local-web-platform' else SimpleNamespace(descriptor=old,observed_healthy=True)
        built=Mock(return_value=prior)
        repository=Mock();repository.manifest_at.return_value=fixture.manifests['notes']
        with tempfile.TemporaryDirectory() as directory:
            client=Path(directory).resolve();context.save(client)
            with patch('local_web_server.git_build.GitRepository',return_value=repository):
                result=install_saved_nas_platform(fixture.repository,registry,dry_run=True,profile_revision='c'*64,
                    selected_candidate=candidate,client_root=client,transport=Mock(),prepare=built,observe=observe)
            self.assertTrue(result.ready)
            self.assertEqual(built.call_args.args[3],(candidate.runtime_spec,))
            self.assertEqual([call.args[0] for call in repository.manifest_at.call_args_list],['b'*40,'a'*40])
            next_context=context.with_applied(result,candidate)
            self.assertEqual(next_context.observed_runtime_specs({'notes':old})['notes'],oldspec)
            self.assertEqual(next_context.observed_runtime_specs({'notes':candidate.descriptor})['notes'],candidate.runtime_spec)
            from tests.test_nas_deploy import AdapterBoundary
            updated=fixture.prepare(registry=registry,runtimes=(candidate.runtime_spec,))
            built.return_value=updated
            responses=dict(prepare=dict(missingBlobs=[],runtimeRequired=False),
                activate=dict(state='active',current=updated.descriptor.digest,healthy=True),finish=dict(finished=True))
            adapter=AdapterBoundary(responses)
            health=Mock();health.check.return_value=SimpleNamespace(healthy=True)
            with patch('local_web_server.git_build.GitRepository',return_value=repository):
                published=install_saved_nas_platform(fixture.repository,registry,dry_run=False,profile_revision='c'*64,
                    selected_candidate=candidate,client_root=client,transport=adapter,prepare=built,observe=observe,health=health)
            self.assertEqual(published.current,updated.descriptor.digest)
            saved=SavedNasInstallationContext.load(client)
            self.assertEqual(dict(saved.platform_releases)[updated.descriptor.digest],updated.source_config_digest)
            self.assertEqual(saved.observed_runtime_specs({'notes':old})['notes'],oldspec)
            self.assertEqual(saved.observed_runtime_specs({'notes':candidate.descriptor})['notes'],candidate.runtime_spec)
            self.assertEqual([call[0] for call in adapter.calls],['prepare','activate','finish'])
            self.assertEqual({call[2]['appId'] for call in adapter.calls},{'local-web-platform'})
            # A normal newly registered generated service uses the saved
            # bootstrap context with its exact prepared none-data candidate.
            new_spec=replace(candidate.runtime_spec,app_id='new-service',port=4175,secret_files=())
            new_candidate=SimpleNamespace(runtime_spec=new_spec,descriptor=replace(release(new_spec,'c'*40),data_contract='none'))
            new_host=replace(registry.apps[0],id='new-service',repository=Path('/Users/example/new-source'),port=4175)
            new_registry=replace(registry,apps=(*registry.apps,new_host))
            new_manifest=replace(fixture.manifests['notes'],id='new-service',title='New service',route='/new-service',health_path='/new-service/healthz')
            final=fixture.prepare(registry=new_registry,manifests=dict(fixture.manifests,**{'new-service':new_manifest}),runtimes=(candidate.runtime_spec,new_spec))
            new_repo=Mock();new_repo.manifest_at.return_value=new_manifest
            repository.reset_mock()
            def observe_new(_transport,app):
                return SimpleNamespace(descriptor=updated.descriptor if app=='local-web-platform' else candidate.descriptor if app=='notes' else None,observed_healthy=True)
            final_adapter=AdapterBoundary(dict(prepare=dict(missingBlobs=[],runtimeRequired=False),activate=dict(state='active',current=final.descriptor.digest,healthy=True),finish=dict(finished=True)))
            with patch('local_web_server.git_build.GitRepository',side_effect=lambda path:new_repo if path==new_host.repository else repository):
                outcome=install_saved_nas_platform(fixture.repository,new_registry,dry_run=False,profile_revision='d'*64,
                    selected_candidate=new_candidate,client_root=client,transport=final_adapter,prepare=Mock(return_value=final),observe=observe_new,health=health)
            self.assertEqual(outcome.current,final.descriptor.digest)
            approved=final_adapter.calls[0][2]['installationApproval']['selectedApp']
            self.assertEqual(approved['descriptor']['dataContract'],'none')
            self.assertEqual(approved['releaseDigest'],new_candidate.descriptor.digest)
            retained=SavedNasInstallationContext.load(client)
            self.assertEqual(retained.observed_runtime_specs({'new-service':new_candidate.descriptor})['new-service'],new_spec)
            repository.manifest_at.assert_called_once_with('b'*40)
            new_repo.manifest_at.assert_called_once_with('c'*40)
            repository.main_commit.assert_not_called();new_repo.main_commit.assert_not_called()

    def test_profile_change_during_platform_upload_stops_activation_and_preserves_original_receipt(self):
        from local_web_server.nas_platform_installation import NasPlatformInstallationSession
        from tests.test_nas_deploy import AdapterBoundary
        plan=self.preview(evidence=tuple(InstallationEvidence(name,'verified','c'*64) for name in InstallationEvidence.REQUIRED),
                          hostname_handoff=HostnameHandoff('local','local-mac','local-shadow'))
        valid=[True]
        def authority():
            if not valid[0]:raise ValueError('synthetic clean profile revision changed')
        def changed(action,payload):
            if action=='upload':valid[0]=False
        responses=dict(prepare=dict(missingBlobs=list({entry.sha256 for entry in plan.prepared.descriptor.files}),runtimeRequired=False),
            upload=dict(uploaded=True),status=dict(state='recovery-required',phase='prepared',current=None,healthy=False))
        adapter=AdapterBoundary(responses,changed)
        with tempfile.TemporaryDirectory() as directory:
            session=NasPlatformInstallationSession(Path(directory)/'client',adapter,public_health=lambda _:True,validate_profile=authority)
            with self.assertRaises(ValueError):session.apply(plan,approved_capsule_digest=plan.approval.digest,
                profile_revision='b'*64,expected_current=None,operation_id='original')
            self.assertEqual([call[0] for call in adapter.calls],['prepare','upload'])
            with session.store.lock():
                self.assertEqual(session.store.pending()['phase'],'preparing')
            adapter.calls.clear()
            with self.assertRaises(ValueError):session.recover('original')
            self.assertEqual([call[0] for call in adapter.calls],['status'])
            with session.store.lock():self.assertEqual(session.store.pending()['operationId'],'original')

    def test_install_entrypoint_formats_nas_results_and_failed_platform_change_never_returns_success(self):
        from contextlib import redirect_stdout,redirect_stderr
        from io import StringIO
        from unittest.mock import patch
        from scripts.install_local_web import main
        from local_web_server.nas_deploy import PublicationResult
        plan=self.preview(evidence=tuple(InstallationEvidence(name,'verified','c'*64) for name in InstallationEvidence.REQUIRED),
                          hostname_handoff=HostnameHandoff('local','local-mac','local-shadow'))
        with patch('scripts.install_local_web.install_platform',return_value=plan),redirect_stdout(StringIO()) as output:
            self.assertEqual(main(['--dry-run']),0)
        self.assertIn('Would publish NAS platform',output.getvalue())
        result=PublicationResult('install',plan.prepared.descriptor.digest,'active',plan.prepared.descriptor.digest)
        with patch('scripts.install_local_web.install_platform',return_value=result),redirect_stdout(StringIO()) as output:
            self.assertEqual(main([]),0)
        self.assertIn('Published NAS platform',output.getvalue())
        from local_web_server.nas_platform_installation import NasPlatformInstallationSession
        from tests.test_nas_deploy import AdapterBoundary
        previous='e'*64
        rollback_plan=self.preview(expected_current=previous,previous_config_digest='f'*64,evidence=plan.evidence,
                                  hostname_handoff=HostnameHandoff('local','local-mac','local-shadow'))
        adapter=AdapterBoundary(dict(prepare=dict(missingBlobs=[],runtimeRequired=False),
            activate=dict(state='rolled-back',current=previous,healthy=True),finish=dict(finished=True)))
        applied=Mock()
        with tempfile.TemporaryDirectory() as directory:
            session=NasPlatformInstallationSession(Path(directory)/'client',adapter,public_health=lambda _:True,on_applied=applied)
            with self.assertRaisesRegex(ValueError,'finished'):
                session.apply(rollback_plan,approved_capsule_digest=rollback_plan.approval.digest,profile_revision='b'*64,
                              expected_current=previous,operation_id='install')
            with session.store.lock():self.assertIsNone(session.store.pending())
            applied.assert_not_called()
        self.assertEqual([call[0] for call in adapter.calls],['prepare','activate','finish'])
