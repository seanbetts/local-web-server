"""Synthetic Mac client recovery exports, with no NAS calls or real profile reads."""
from dataclasses import replace, asdict
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
from unittest.mock import patch

from tests.test_host_profile_store import StoreFixture, CLOCK
from tests.test_nas_installation_plan import platform
from tests.test_production_build import docker_archive
from local_web_server.config import load_registry_bytes
from local_web_server.nas_lifecycle import NasOperationStore
from local_web_server.nas_platform_installation import SavedNasInstallationContext, HostnameHandoff
from local_web_server.release_manifest import ReleaseDescriptorV1, snapshot_files, RuntimeIdentity
from local_web_server.nas_client_backup import write_nas_client_backup, verify_nas_client_backup, NasClientBackupError


class ClientBackupTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.profile = json.dumps(dict(schemaVersion=3, publicOrigin='https://local.example.ts.net',
            ingressMode='tailscale-serve', runtimeRoot='/volume2/docker/local-web', apps=[], services=[],
            target=dict(kind='nas-docker', adapterCommand=['/fixture/adapter'], networkSubnet='172.30.200.0/24', proxyAddress='172.30.200.2'))).encode()
        self.store.initialise(self.profile)
        self.client = self.root/'client';self.client.mkdir(mode=0o700)
        self.context = SavedNasInstallationContext('https://local.example.ts.net','172.30.200.0/24','172.30.200.2',
            'caddy@sha256:'+'a'*64,'tailscale/tailscale@sha256:'+'b'*64,(),HostnameHandoff('local','local-mac-retained','local-shadow'),(),())
        self.context.save(self.client)
        self.output = self.root/'recovery.tar'

    def pending(self, app='example', operation='original', runtime=False):
        root=self.client/'prepared'/app/operation;files=root/'files';files.mkdir(parents=True)
        (files/'server.py').write_bytes(b'original selected bytes\n');(files/'server.py').chmod(0o644)
        archive=None;identity=None
        if runtime:
            from local_web_server.production_build import DependencyKey
            key=DependencyKey('d'*64,'sha256:'+'e'*64,'linux','amd64','fixture','1')
            archive=self.client/'cache/linux'/key.digest/'artifact.tar';archive.parent.mkdir(parents=True)
            image=docker_archive(archive,layer_body=b'original selected Linux layer')
            digest=hashlib.sha256(archive.read_bytes()).hexdigest()
            (archive.parent/'metadata.json').write_text(json.dumps(dict(key=key.to_dict(),sha256=digest)))
            identity=RuntimeIdentity(image,'sha256:'+digest,'linux','amd64','fixture','d'*64,'1')
            from local_web_server.nas_render import ContainerRuntimeSpec
            spec=ContainerRuntimeSpec(app,image,('python3','/app/current/server.py'),43123,1,512,health_runtime='python3')
            (files/'.local-web-runtime.json').write_text(json.dumps(asdict(spec),sort_keys=True,separators=(',',':'))+'\n')
            (files/'.local-web-runtime.json').chmod(0o644)
        descriptor=ReleaseDescriptorV1(app,'a'*40,'1',identity,snapshot_files(files),'fixture-v1','compatible')
        (root/'descriptor.json').write_bytes(descriptor.canonical_bytes())
        store=NasOperationStore(self.client,app)
        with store.lock():store.begin(operation,descriptor,expected_current=None,prepared_root=root,runtime_archive=archive,action='deploy',
                                     profile_authority=hashlib.sha256(self.profile).hexdigest())
        return store,root,descriptor,archive

    def write(self):return write_nas_client_backup(self.paths,self.output,client_root=self.client,clock=CLOCK)

    def test_preserves_pending_original_code_runtime_profile_and_receipts_but_not_caches(self):
        store,root,descriptor,archive=self.pending(runtime=True)
        receipt=(store.layout.app_root/'nas-operation.json').read_bytes();runtime=archive.read_bytes()
        (self.client/'cache/unrelated').write_bytes(b'never export dependency cache')
        completed=self.client/'prepared/example/completed/files';completed.mkdir(parents=True);(completed/'old.py').write_bytes(b'completed rebuildable')
        result=self.write();self.assertEqual(result['pendingOperations'],1)
        self.assertEqual(self.output.stat().st_mode&0o777,0o600)
        with tarfile.open(self.output,'r:') as bundle:
            names=bundle.getnames();self.assertNotIn('client/cache/unrelated',names)
            self.assertNotIn('client/prepared/example/completed/files/old.py',names)
            self.assertEqual(bundle.extractfile('client/apps/example/nas-operation.json').read(),receipt)
            self.assertEqual(bundle.extractfile('client/'+archive.relative_to(self.client).as_posix()).read(),runtime)
            self.assertEqual(bundle.extractfile('client/prepared/example/original/files/server.py').read(),b'original selected bytes\n')
        verified=verify_nas_client_backup(self.output);self.assertEqual(verified['pendingOperations'],1)
        self.assertEqual((store.layout.app_root/'nas-operation.json').read_bytes(),receipt)

    def test_no_clobber_and_original_source_unchanged(self):
        self.pending();self.write();before=self.output.read_bytes()
        import local_web_server.nas_client_backup as module
        with patch.object(module,'_copy',side_effect=AssertionError('copied before collision check')):
            with self.assertRaises(NasClientBackupError):self.write()
        self.assertEqual(self.output.read_bytes(),before)

    def test_app_receipt_requires_original_authority_in_exported_profile_history(self):
        store,*_=self.pending()
        from local_web_server.nas_lifecycle import _canonical
        path=store.layout.app_root/'nas-operation.json';receipt=json.loads(path.read_bytes())
        for authority in (None,'f'*64):
            with self.subTest(authority=authority):
                path.write_bytes(_canonical(receipt|{'profileAuthority':authority}))
                with self.assertRaises(NasClientBackupError):self.write()
                self.assertFalse(self.output.exists())

    def test_busy_existing_lock_and_missing_pending_lock_refuse_without_creating_paths(self):
        store,*_=self.pending()
        with store.lock():
            with self.assertRaises(NasClientBackupError):self.write()
        store.layout.lock.unlink()
        with self.assertRaises(NasClientBackupError):self.write()
        self.assertFalse(store.layout.lock.exists());self.assertFalse(self.output.exists())

    def test_outside_pending_root_symlink_code_and_wrong_runtime_are_rejected(self):
        store,root,descriptor,archive=self.pending(runtime=True)
        path=store.layout.app_root/'nas-operation.json';original=path.read_bytes();bad=json.loads(original);bad['preparedRoot']=str(self.root/'outside')
        from local_web_server.nas_lifecycle import _canonical
        path.write_bytes(_canonical(bad))
        with self.assertRaises(NasClientBackupError):self.write()
        path.write_bytes(original);code=root/'files/server.py';body=code.read_bytes();code.unlink();code.symlink_to(self.root/'foreign')
        with self.assertRaises(NasClientBackupError):self.write()
        code.unlink();code.write_bytes(body);code.chmod(0o644);archive.write_bytes(b'not original runtime')
        with self.assertRaises(NasClientBackupError):self.write()

    def test_context_target_and_profile_drift_fail_before_publication(self):
        self.pending();replace(self.context,proxy_address='172.30.200.3').save(self.client)
        with self.assertRaises(NasClientBackupError):self.write()
        self.context.save(self.client)
        import local_web_server.nas_client_backup as module
        original=module.build_host_backup;calls=[]
        def drift(*args,**kwargs):
            backup=original(*args,**kwargs);calls.append(True)
            if len(calls)==1:self.store.publish_configuration(self.profile,self.profile+b' ')
            return backup
        with patch.object(module,'build_host_backup',side_effect=drift):
            with self.assertRaises(NasClientBackupError):self.write()
        self.assertFalse(self.output.exists())

    def platform_pending(self, selected=None):
        prepared=platform();app='local-web-platform';operation='platform-original';store=NasOperationStore(self.client,app)
        with store.lock():
            directory=store.layout.app_root/('installation-'+operation);directory.mkdir(mode=0o700);prepared.write_files(directory/'files')
            from local_web_server.nas_platform_installation import preview_nas_installation,canonical
            approval=preview_nas_installation(prepared=prepared,store_root='/volume2/docker/local-web',profile_revision=hashlib.sha256(self.profile).hexdigest(),expected_current=None,previous_config_digest=None,selected_candidate=selected).approval
            (directory/'installation-approval.json').write_bytes(canonical(approval.to_dict()));(directory/'installation-approval.json').chmod(0o600)
            next_context=replace(self.context,platform_releases=((prepared.descriptor.digest,approval.source_config_digest),))
            if selected is not None and selected.runtime_spec is not None:
                from local_web_server.nas_platform_installation import KnownRuntimeVersion
                next_context=replace(next_context,known_runtime_versions=(KnownRuntimeVersion(selected.descriptor,selected.runtime_spec),))
            next_context.save(directory)
            store.begin(operation,prepared.descriptor,expected_current=None,prepared_root=directory/'files',runtime_archive=None,action='deploy')
        return store

    def test_platform_pending_keeps_exact_capsule_and_context(self):
        self.platform_pending()
        self.write();self.assertTrue(verify_nas_client_backup(self.output)['verified'])
        with tarfile.open(self.output) as bundle:self.assertIn('client/apps/local-web-platform/installation-platform-original/installation-approval.json',bundle.getnames())

    def test_verify_rejects_tampered_member_and_traversal(self):
        self.pending();self.write()
        original=self.output.read_bytes()
        for name in ('client/prepared/example/original/files/server.py','../escaped'):
            out=io.BytesIO()
            with tarfile.open(fileobj=io.BytesIO(original)) as source,tarfile.open(fileobj=out,mode='w:') as target:
                for member in source:
                    content=source.extractfile(member).read()
                    if member.name=='client/prepared/example/original/files/server.py':
                        content=b'tampered';member.size=len(content);member.name=name
                    target.addfile(member,io.BytesIO(content))
            self.output.write_bytes(out.getvalue())
            with self.assertRaises(NasClientBackupError):verify_nas_client_backup(self.output)

    def test_platform_capsule_preserves_selected_original_before_app_receipt_exists(self):
        from types import SimpleNamespace
        from local_web_server.nas_render import ContainerRuntimeSpec
        store,root,descriptor,archive=self.pending(runtime=True)
        (store.layout.app_root/'nas-operation.json').unlink()
        spec=ContainerRuntimeSpec(**(json.loads((root/'files/.local-web-runtime.json').read_text())|{'command':('python3','/app/current/server.py'),'secret_files':()}))
        self.platform_pending(SimpleNamespace(descriptor=descriptor,runtime_spec=spec))
        self.write()
        with tarfile.open(self.output) as bundle:
            names=bundle.getnames();manifest=json.load(bundle.extractfile('manifest.json'))
            self.assertIn('client/prepared/example/original/descriptor.json',names)
            self.assertIn('client/'+archive.relative_to(self.client).as_posix(),names)
            self.assertEqual(manifest['selectedPreparations'][0]['preparedRoot'],'prepared/example/original')
        self.assertTrue(verify_nas_client_backup(self.output)['verified'])

    def test_missing_capsule_selected_original_cannot_claim_complete_recovery(self):
        from types import SimpleNamespace
        store,root,descriptor,archive=self.pending()
        (store.layout.app_root/'nas-operation.json').unlink()
        self.platform_pending(SimpleNamespace(descriptor=descriptor,runtime_spec=None))
        (root/'descriptor.json').unlink()
        with self.assertRaises(NasClientBackupError):self.write()

    def test_completed_metadata_and_context_only_export_does_not_create_locks_or_directories(self):
        result=self.write();self.assertEqual(result['pendingOperations'],0)
        self.assertFalse((self.client/'apps').exists())
        self.output.unlink()
        store,root,descriptor,_=self.pending()
        with store.lock():store.complete('original',state='active',current=descriptor.digest)
        import shutil
        shutil.rmtree(root);store.layout.lock.unlink()
        result=self.write()
        self.assertFalse(store.layout.lock.exists());self.assertEqual(result['pendingOperations'],0)
        with tarfile.open(self.output) as bundle:
            self.assertIn('client/apps/example/nas-original.json',bundle.getnames())
            self.assertNotIn('client/prepared/example/original/descriptor.json',bundle.getnames())

    def test_payload_limit_is_checked_before_copy_and_output_cannot_target_client_state(self):
        self.pending()
        import local_web_server.nas_client_backup as module
        with patch.object(module,'MAX_PAYLOAD_BYTES',8),patch.object(module,'_copy',side_effect=AssertionError('copy started')):
            with self.assertRaises(NasClientBackupError):self.write()
        with self.assertRaises(NasClientBackupError):
            write_nas_client_backup(self.paths,self.client/'export.tar',client_root=self.client,clock=CLOCK)
        self.assertFalse(self.output.exists());self.assertFalse((self.client/'export.tar').exists())

    def test_matching_cache_transport_digest_is_required_even_for_valid_image(self):
        store,root,descriptor,archive=self.pending(runtime=True)
        metadata=archive.parent/'metadata.json';value=json.loads(metadata.read_bytes());value['sha256']='0'*64;metadata.write_text(json.dumps(value))
        with self.assertRaises(NasClientBackupError):self.write()

    def test_pending_context_must_identify_its_original_capsule(self):
        self.platform_pending()
        path=self.client/'apps/local-web-platform/installation-platform-original/installation-context.json'
        self.context.save(path.parent)
        with self.assertRaises(NasClientBackupError):self.write()

    def test_context_preserves_both_known_runtime_versions_and_platform_releases(self):
        from local_web_server.nas_platform_installation import KnownRuntimeVersion
        from local_web_server.nas_render import ContainerRuntimeSpec
        store,root,descriptor,archive=self.pending(runtime=True)
        spec=ContainerRuntimeSpec(**(json.loads((root/'files/.local-web-runtime.json').read_text())|{'command':('python3','/app/current/server.py'),'secret_files':()}))
        current=KnownRuntimeVersion(descriptor,spec)
        previous=KnownRuntimeVersion(replace(descriptor,source_commit='b'*40),spec)
        context=replace(self.context,known_runtime_versions=(current,previous),platform_releases=(('a'*64,'b'*64),('c'*64,'d'*64)))
        context.save(self.client);original=(self.client/'installation-context.json').read_bytes()
        self.write()
        with tarfile.open(self.output) as bundle:
            self.assertEqual(bundle.extractfile('client/installation-context.json').read(),original)
        self.assertTrue(verify_nas_client_backup(self.output)['verified'])

    def test_copy_stops_at_observed_size_when_a_selected_file_grows(self):
        import local_web_server.nas_client_backup as module
        source=self.client/'growing';source.write_bytes(b'four');destination=self.root/'copied'
        original=module.os.fdopen;read_count=[0]
        class GrowingReader:
            def __init__(self,stream):self.stream=stream;self.grown=False
            def __enter__(self):return self
            def __exit__(self,*args):return self.stream.__exit__(*args)
            def fileno(self):return self.stream.fileno()
            def read(self,size=-1):
                if not self.grown:
                    self.grown=True
                    with source.open('ab') as out:out.write(b'x'*100000)
                data=self.stream.read(size);read_count[0]+=len(data);return data
        with patch.object(module.os,'fdopen',side_effect=lambda *args,**kwargs:GrowingReader(original(*args,**kwargs))):
            with self.assertRaises(ValueError):module._copy(self.client,'growing',destination)
        self.assertLessEqual(read_count[0],5)
        self.assertLessEqual(destination.stat().st_size,4)

    def test_selected_capsule_context_cannot_drop_its_selected_runtime_version(self):
        from types import SimpleNamespace
        from local_web_server.nas_render import ContainerRuntimeSpec
        store,root,descriptor,_=self.pending(runtime=True)
        (store.layout.app_root/'nas-operation.json').unlink()
        spec=ContainerRuntimeSpec(**(json.loads((root/'files/.local-web-runtime.json').read_text())|{'command':('python3','/app/current/server.py'),'secret_files':()}))
        self.platform_pending(SimpleNamespace(descriptor=descriptor,runtime_spec=spec))
        directory=self.client/'apps/local-web-platform/installation-platform-original'
        context=SavedNasInstallationContext.load(directory)
        replace(context,known_runtime_versions=()).save(directory)
        with self.assertRaises(NasClientBackupError):self.write()

    def test_sparse_large_extension_is_rejected_before_tarfile_allocates_its_payload(self):
        from tests.test_production_build import sparse_extension_archive
        sparse_extension_archive(self.output);self.output.chmod(0o600)
        with patch.object(tarfile.TarInfo,'_proc_gnulong',side_effect=AssertionError('allocating extension parser entered')) as parser:
            with self.assertRaises(NasClientBackupError):verify_nas_client_backup(self.output)
            parser.assert_not_called()

    def test_recovery_archive_refuses_directory_headers_before_tarfile_open(self):
        entry=tarfile.TarInfo('directory/');entry.type=tarfile.DIRTYPE
        self.output.write_bytes(entry.tobuf(format=tarfile.USTAR_FORMAT)+bytes(1024));self.output.chmod(0o600)
        with patch('local_web_server.nas_client_backup.tarfile.open',side_effect=AssertionError('tar parser entered')) as parser:
            with self.assertRaises(NasClientBackupError):verify_nas_client_backup(self.output)
            parser.assert_not_called()

    def test_pending_runtime_extension_never_reaches_nested_allocating_parser(self):
        from tests.test_production_build import sparse_extension_archive
        from local_web_server.nas_platform_installation import canonical
        store,root,descriptor,archive=self.pending(runtime=True)
        sparse_extension_archive(archive)
        with archive.open('rb') as source:digest=hashlib.file_digest(source,'sha256').hexdigest()
        metadata=archive.parent/'metadata.json';value=json.loads(metadata.read_bytes());value['sha256']=digest;metadata.write_text(json.dumps(value))
        descriptor=replace(descriptor,runtime=replace(descriptor.runtime,dependency_digest='sha256:'+digest))
        (root/'descriptor.json').write_bytes(descriptor.canonical_bytes())
        path=store.layout.app_root/'nas-operation.json';receipt=json.loads(path.read_bytes());receipt['descriptor']=descriptor.to_dict();path.write_bytes(canonical(receipt))
        with patch.object(tarfile.TarInfo,'_proc_gnulong',side_effect=AssertionError('nested extension parser entered')) as parser:
            with self.assertRaises(NasClientBackupError):self.write()
            parser.assert_not_called()
