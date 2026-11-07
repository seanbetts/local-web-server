"""Origin handoff contracts use disposable contexts and synthetic callbacks only."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

from local_web_server.models import HostRegistry
from local_web_server.nas_models import NasTarget
from local_web_server.nas_platform_installation import SavedNasInstallationContext, HostnameHandoff, canonical
from tests.test_nas_installation_plan import platform
from local_web_server.release_manifest import FileEntry


class OriginTransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()/'historical'; self.root.mkdir(mode=0o700)
        self.destination = Path(self.temp.name).resolve()/'production'
        self.source = HostRegistry('shadow.example.ts.net', Path('/volume2/docker/local-web'), (),
            schema_version=3, public_origin='https://shadow.example.ts.net', ingress_mode='tailscale-serve',
            target=NasTarget('nas-docker', ('/fixed/adapter',), '172.30.200.0/24', '172.30.200.2'))
        self.target = replace(self.source, host='local.example.ts.net', public_origin='https://local.example.ts.net')
        self.prepared = self.prepared_origin('https://local.example.ts.net')
        self.context = SavedNasInstallationContext(self.source.public_origin, '172.30.200.0/24', '172.30.200.2',
            'caddy@sha256:'+'a'*64, 'tailscale@sha256:'+'b'*64, (), HostnameHandoff('local','mac-retained','shadow'), (),
            (('c'*64,'d'*64),))
        self.context.save(self.root)

    def prepared_origin(self, origin):
        initial=platform()
        files=tuple(replace(f,content=f.content.replace(b'https://local.example.ts.net',origin.encode())) if f.path=='runtime-settings.json'
            else replace(f,content=canonical(dict(services=dict(caddy=dict(image='caddy@sha256:'+'a'*64), tailscale=dict(image='tailscale@sha256:'+'b'*64)))))
            if f.path=='compose.yaml' else f for f in initial.files)
        descriptor=replace(initial.descriptor,files=tuple(FileEntry(f.path,hashlib.sha256(f.content).hexdigest(),len(f.content),f.mode) for f in files))
        return replace(initial,files=files,descriptor=descriptor)

    def proof(self, plan, operation='original'):
        value=plan.to_dict()
        return dict(state='origin-ready', operationId=operation, planDigest=plan.digest,
            publicOrigin=value['toOrigin'], platformReleaseDigest=value['nextPlatformDescriptorDigest'],
            sourceConfigDigest=value['nextConfigDigest'], apps={}, healthy=True)

    def plan(self, **changes):
        from local_web_server.nas_origin_transition import preview_nas_origin_transition
        args = dict(source_registry=self.source, destination_registry=self.target, source_context=self.context,
            prepared=self.prepared, app_descriptors={}, current_platform_digest='c'*64, current_config_digest='d'*64,
            source_profile_digest='e'*64, destination_profile_digest='f'*64, installed_marker_digest='a'*64,
            client_root=self.root, destination_client_root=self.destination)
        args.update(changes)
        return preview_nas_origin_transition(**args)

    def test_plan_roundtrip_freezes_origin_and_provenance(self):
        from local_web_server.nas_origin_transition import NasOriginTransitionPlan
        plan = self.plan()
        self.assertEqual(plan.direction, 'forward')
        self.assertEqual(NasOriginTransitionPlan.from_dict(plan.to_dict()), plan)
        value=plan.to_dict(); value['apps']['injected']={}
        self.assertEqual(plan.to_dict()['apps'], {})
        self.assertEqual(plan.to_dict()['sourceContext'], self.context.to_dict())
        self.assertEqual(plan.next_context.public_origin, self.target.public_origin)
        for registry in (replace(self.target, public_origin='https://mac-retained.example.ts.net'),
                         replace(self.target, public_origin='https://local.other.ts.net'),
                         replace(self.target, target=replace(self.target.target, adapter_command=('/other',))),
                         replace(self.target, apps=(object(),))):
            with self.assertRaises(ValueError): self.plan(destination_registry=registry)

    def test_status_never_replays_uncertain_callback_and_promotion_binds_history(self):
        from local_web_server.nas_origin_transition import NasOriginTransitionSession, resolve_nas_origin_client_root
        plan=self.plan(); session=NasOriginTransitionSession(self.root)
        calls=[]
        def uncertain(plan, operation):
            calls.append(operation); raise TimeoutError('lost acknowledgement')
        with self.assertRaises(TimeoutError):
            session.apply(plan, operation_id='original', approved_digest=plan.digest,
                          transition=uncertain, validate_authority=lambda: None)
        with self.assertRaisesRegex(ValueError,'original.*status'):
            session.apply(plan, operation_id='original', approved_digest=plan.digest,
                          transition=uncertain, validate_authority=lambda: None)
        self.assertEqual(calls, ['original'])
        proof=dict(state='origin-ready', operationId='original', planDigest=plan.digest,
            publicOrigin=self.target.public_origin, platformReleaseDigest=plan.to_dict()['nextPlatformDescriptorDigest'],
            sourceConfigDigest=self.prepared.source_config_digest, apps={}, healthy=True)
        result=session.status('original', observe=lambda frozen, operation: proof)
        self.assertEqual(result, proof)
        # A fresh process promotes using only original receipt and exact observation.
        session=NasOriginTransitionSession(self.root)
        session.promote('original', proof)
        self.assertEqual(resolve_nas_origin_client_root(self.target, self.destination), self.root)
        self.assertEqual(SavedNasInstallationContext.load(self.root).public_origin, self.target.public_origin)
        self.assertFalse((self.destination/'installation-context.json').exists())
        session.promote('original', proof)  # idempotent local completion
        self.assertEqual(calls, ['original'])

    def test_collision_and_unhealthy_observation_preserve_source_context(self):
        from local_web_server.nas_origin_transition import NasOriginTransitionSession
        plan=self.plan(); session=NasOriginTransitionSession(self.root)
        session.begin(plan, operation_id='original', approved_digest=plan.digest)
        self.destination.mkdir(mode=0o700); (self.destination/'unrelated').write_text('preserve')
        with self.assertRaises(ValueError): session.promote('original', self.proof(plan))
        self.assertEqual(SavedNasInstallationContext.load(self.root), self.context)

    def test_reverse_preserves_latest_runtime_context_and_uses_original_root(self):
        forward=self.plan()
        reverse=self.plan(source_registry=self.target, destination_registry=self.source,
            source_context=forward.next_context, current_platform_digest=self.prepared.descriptor.digest, current_config_digest=self.prepared.source_config_digest,
            prepared=self.prepared_origin(self.source.public_origin),
            destination_client_root=self.root)
        self.assertEqual(reverse.direction,'reverse')
        self.assertEqual(reverse.next_context.known_runtime_versions, self.context.known_runtime_versions)


    def test_forward_reverse_forward_rebinds_only_exact_completed_lineage(self):
        from local_web_server.nas_origin_transition import NasOriginTransitionSession, resolve_nas_origin_client_root
        session=NasOriginTransitionSession(self.root); first=self.plan()
        session.begin(first,operation_id='forward',approved_digest=first.digest)
        session.promote('forward',self.proof(first,'forward'))
        reverse=self.plan(source_registry=self.target,destination_registry=self.source,source_context=first.next_context,
            current_platform_digest=self.prepared.descriptor.digest,current_config_digest=self.prepared.source_config_digest,
            prepared=self.prepared_origin(self.source.public_origin),destination_client_root=self.root)
        session.begin(reverse,operation_id='reverse',approved_digest=reverse.digest)
        session.promote('reverse',self.proof(reverse,'reverse'))
        with self.assertRaises(ValueError): resolve_nas_origin_client_root(self.target,self.destination)
        second=self.plan(source_context=reverse.next_context,
            current_platform_digest=reverse.to_dict()['nextPlatformDescriptorDigest'],current_config_digest=reverse.to_dict()['nextConfigDigest'])
        self.assertIsNotNone(second.to_dict()['previousBindingDigest'])
        session.begin(second,operation_id='again',approved_digest=second.digest)
        session.promote('again',self.proof(second,'again'))
        self.assertEqual(resolve_nas_origin_client_root(self.target,self.destination),self.root)
        with self.assertRaises(ValueError): session.promote('reverse',self.proof(reverse,'reverse'))

    def test_namespace_allows_monotonic_context_history_and_refuses_changed_baseline(self):
        from local_web_server.nas_origin_transition import NasOriginTransitionSession, resolve_nas_origin_client_root
        plan=self.plan(); session=NasOriginTransitionSession(self.root)
        session.begin(plan,operation_id='original',approved_digest=plan.digest)
        session.promote('original',self.proof(plan))
        newer=replace(plan.next_context,platform_releases=(*plan.next_context.platform_releases,('1'*64,'2'*64)))
        newer.save(self.root)
        self.assertEqual(resolve_nas_origin_client_root(self.target,self.destination),self.root)
        replace(newer,platform_releases=(('1'*64,'2'*64),)).save(self.root)
        with self.assertRaises(ValueError): resolve_nas_origin_client_root(self.target,self.destination)

    def test_rejects_prepared_image_change_and_strict_health_boolean(self):
        bad=self.prepared_origin(self.target.public_origin)
        files=tuple(replace(f,content=f.content.replace(b'caddy@sha256:',b'other@sha256:')) if f.path=='compose.yaml' else f for f in bad.files)
        bad=replace(bad,files=files,descriptor=replace(bad.descriptor,files=tuple(FileEntry(f.path,hashlib.sha256(f.content).hexdigest(),len(f.content),f.mode) for f in files)))
        with self.assertRaises(ValueError): self.plan(prepared=bad)
        from local_web_server.nas_origin_transition import NasOriginTransitionSession
        plan=self.plan(); session=NasOriginTransitionSession(self.root)
        session.begin(plan,operation_id='original',approved_digest=plan.digest)
        proof=self.proof(plan); proof['healthy']=1
        with self.assertRaises(ValueError): session.promote('original',proof)

    def test_interrupted_promotion_recovers_original_without_remote_replay(self):
        import local_web_server.nas_origin_transition as module
        for boundary in ('pending-binding','context','complete-binding','complete-receipt'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as directory:
                root=Path(directory).resolve()/'source'; root.mkdir(mode=0o700)
                destination=Path(directory).resolve()/'destination'; self.context.save(root)
                plan=self.plan(client_root=root,destination_client_root=destination)
                session=module.NasOriginTransitionSession(root)
                session.begin(plan,operation_id='original',approved_digest=plan.digest)
                original_write=module._write; original_save=SavedNasInstallationContext.save
                def interrupted_write(path,name,value):
                    original_write(path,name,value)
                    selected=(boundary=='pending-binding' and name=='origin-binding.json' and value['phase']=='pending'
                        or boundary=='complete-binding' and name=='origin-binding.json' and value['phase']=='complete'
                        or boundary=='complete-receipt' and name=='origin-transition-original.json' and value['phase']=='complete')
                    if selected: raise TimeoutError('crash after durable publication')
                def interrupted_save(context,path):
                    original_save(context,path)
                    if boundary=='context': raise TimeoutError('crash after context')
                with patch.object(module,'_write',interrupted_write), patch.object(SavedNasInstallationContext,'save',interrupted_save):
                    with self.assertRaises(TimeoutError): session.promote('original',self.proof(plan))
                session=module.NasOriginTransitionSession(root)
                session.promote('original',self.proof(plan))
                self.assertEqual(module.resolve_nas_origin_client_root(self.target,destination),root)
                self.assertFalse((root/'origin-transition-pending.json').exists())

    def test_changed_context_after_begin_prevents_transition_callback(self):
        from local_web_server.nas_origin_transition import NasOriginTransitionSession
        plan=self.plan(); session=NasOriginTransitionSession(self.root)
        session.begin(plan,operation_id='original',approved_digest=plan.digest)
        replace(self.context,platform_releases=(*self.context.platform_releases,('1'*64,'2'*64))).save(self.root)
        calls=[]
        with self.assertRaises(ValueError): session.apply(plan,operation_id='original',approved_digest=plan.digest,
            transition=lambda *args:calls.append(args),validate_authority=lambda:None)
        self.assertEqual(calls,[])

    def test_pending_transition_blocks_normal_namespace_resolution(self):
        from local_web_server.nas_origin_transition import NasOriginTransitionSession, resolve_nas_origin_client_root
        plan=self.plan(); session=NasOriginTransitionSession(self.root)
        session.begin(plan,operation_id='original',approved_digest=plan.digest)
        with self.assertRaises(ValueError): resolve_nas_origin_client_root(self.source,self.root)

    def test_destination_changed_after_review_refuses_callback(self):
        from local_web_server.nas_origin_transition import NasOriginTransitionSession
        plan=self.plan(); self.destination.mkdir(mode=0o700)
        (self.destination/'foreign').write_text('preserve')
        calls=[]; session=NasOriginTransitionSession(self.root)
        with self.assertRaises(ValueError): session.apply(plan,operation_id='original',approved_digest=plan.digest,
            transition=lambda *args:calls.append(args),validate_authority=lambda:None)
        self.assertEqual(calls,[])
        self.assertEqual((self.destination/'foreign').read_text(),'preserve')

    def test_runtime_metadata_is_frozen_and_cannot_be_removed_after_promotion(self):
        from dataclasses import asdict
        from local_web_server.nas_render import ContainerRuntimeSpec
        from local_web_server.nas_platform_installation import KnownRuntimeVersion
        from local_web_server.release_manifest import RuntimeIdentity, ReleaseDescriptorV1
        from local_web_server.nas_origin_transition import NasOriginTransitionSession, resolve_nas_origin_client_root
        spec=ContainerRuntimeSpec('notes','sha256:'+'a'*64,('node','/app/current/server.js','--origin','{publicOrigin}'),42001,1,512,health_runtime='node')
        metadata=(json.dumps(asdict(spec),sort_keys=True,separators=(',',':'))+'\n').encode()
        identity=RuntimeIdentity('sha256:'+'a'*64,'sha256:'+'b'*64,'linux','amd64','node24','c'*64,'v1')
        descriptor=ReleaseDescriptorV1('notes','d'*40,'v1',identity,
            (FileEntry('.local-web-runtime.json',hashlib.sha256(metadata).hexdigest(),len(metadata),0o644),),'notes-v1','compatible')
        context=replace(self.context,known_runtime_versions=(KnownRuntimeVersion(descriptor,spec),))
        context.save(self.root); plan=self.plan(source_context=context)
        self.assertEqual(plan.next_context.known_runtime_versions,context.known_runtime_versions)
        session=NasOriginTransitionSession(self.root); session.begin(plan,operation_id='original',approved_digest=plan.digest)
        session.promote('original',self.proof(plan))
        replace(plan.next_context,known_runtime_versions=()).save(self.root)
        with self.assertRaises(ValueError): resolve_nas_origin_client_root(self.target,self.destination)

    def test_lifecycle_namespace_resolves_promoted_history_and_keeps_unbound_default(self):
        from local_web_server.nas_lifecycle import nas_client_root
        from local_web_server.nas_origin_transition import NasOriginTransitionSession
        with patch.object(Path,'home',return_value=Path(self.temp.name).resolve()):
            unbound=nas_client_root(self.source)
            self.assertEqual(unbound.parent,Path.home()/'Library/Application Support/Local Web/nas')
            destination=nas_client_root(self.target); destination.parent.mkdir(parents=True,mode=0o700)
            plan=self.plan(destination_client_root=destination); session=NasOriginTransitionSession(self.root)
            session.begin(plan,operation_id='original',approved_digest=plan.digest)
            session.promote('original',self.proof(plan))
            self.assertEqual(nas_client_root(self.target),self.root)
