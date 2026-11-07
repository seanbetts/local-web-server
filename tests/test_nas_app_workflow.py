"""Focused verifier boundaries; Docker/Colima are replaced with deterministic results."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from local_web_server.release_manifest import RuntimeIdentity
from unittest.mock import patch

from scripts import verify_nas_app_workflow as workflow


class NasAppWorkflowTests(unittest.TestCase):
    def test_service_requires_explicit_guarded_context_before_creating_an_app(self):
        with self.assertRaisesRegex(ValueError, 'explicit.*docker-context'):
            workflow.verify(kind='service')

    def test_guard_refuses_remote_endpoint_or_shared_vm_mounts_before_docker_effects(self):
        calls = []
        def execute(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'colima':
                return '10 1 0:1 / /Users rw - virtiofs host rw\n'
            return json.dumps([{'Endpoints': {'docker': {'Host': self.endpoint}}}])
        self.endpoint = 'ssh://remote.example.test'
        with self.assertRaises(ValueError): workflow.guard_context(workflow.CONTEXT, execute=execute)
        self.endpoint = 'unix://' + str(Path.home()/'.colima'/workflow.PROFILE/'docker.sock')
        with self.assertRaisesRegex(ValueError, 'shared'):
            workflow.guard_context(workflow.CONTEXT, execute=execute)
        self.assertFalse(any('run' in argv or 'load' in argv for argv in calls))

    def test_guard_reads_actual_mounts_and_linux_type_using_only_explicit_context(self):
        calls = []
        def execute(argv, **kwargs):
            calls.append(argv)
            if argv[0] == 'colima': return '10 1 0:1 / / rw - ext4 /dev/vda rw\n'
            if 'info' in argv: return 'linux\n'
            return json.dumps([{'Endpoints': {'docker': {'Host': 'unix://' + str(Path.home()/'.colima'/workflow.PROFILE/'docker.sock')}}}])
        self.assertEqual(workflow.guard_context(workflow.CONTEXT, execute=execute), ('docker', '--context', workflow.CONTEXT))
        self.assertTrue(all(argv[0] == 'colima' or workflow.CONTEXT in argv for argv in calls))

    def test_runtime_refuses_wrong_architecture_before_creating_resources(self):
        # The actual runtime entrypoint must consume the verifier's archive graph,
        # never trust a caller-provided image tag or unverified descriptor.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            prepared = type('Prepared', (), {'runtime_archive': root/'missing.tar'})()
            with patch.object(workflow, 'verify_docker_archive', side_effect=ValueError('invalid archive')), \
                 patch.object(workflow, 'checked') as checked:
                with self.assertRaises(ValueError): workflow.verify_runtime(prepared, workflow.CONTEXT)
                checked.assert_not_called()

    def test_failed_proxy_probe_cleans_only_owned_resources_and_preserves_images(self):
        identity = RuntimeIdentity('sha256:'+'a'*64,'sha256:'+'b'*64,'linux','amd64','node24','c'*64,'1')
        verified = SimpleNamespace(archive_digest='b'*64,image_digest=identity.image_digest,manifest_digest=None,os='linux',architecture='amd64')
        spec = SimpleNamespace(command=('node','/app/current/server/service.mjs'),cpu_limit=1,memory_limit_mib=256,user='10001:10001')
        prepared = SimpleNamespace(runtime_archive=Path('/not-read/archive.tar'),descriptor=SimpleNamespace(runtime=identity),
                                   files_root=Path('/not-read/files'),runtime_spec=spec)
        commands = []
        network = None
        def execute(argv, **kwargs):
            nonlocal network
            args = argv[3:]
            commands.append(args)
            if args[:2] == ['image','inspect']:
                return json.dumps([dict(Id=identity.image_digest,Os='linux',Architecture='amd64')])
            if args[:2] == ['network','create']:
                network = args[-1]
            if len(args)>1 and args[1] == 'inspect' and args[0] in ('container','volume','network'):
                labels={workflow.OWNER_LABEL:args[-1].rsplit('-',1)[0]}
                return json.dumps([dict(Config=dict(Labels=labels),Labels=labels)])
            if args[0] == 'inspect':
                return json.dumps([dict(Image=identity.image_digest,HostConfig=dict(ReadonlyRootfs=True,PortBindings={}),
                    Config=dict(User='10001:10001'),Mounts=[dict(Type='volume',RW=False)],
                    NetworkSettings=dict(Networks={network:dict(IPAddress='172.29.117.3')}))])
            if args[0] == 'exec' and '-p' in args:
                return json.dumps(dict(version='24.15.0',modules='137',os='linux',architecture='x64'))
            if args[0] == 'run' and workflow._CLIENT in args:
                raise ValueError('synthetic failed proxy probe')
            return ''
        with patch.object(workflow,'verify_docker_archive',return_value=verified), \
             patch.object(workflow,'guard_context',return_value=('docker','--context',workflow.CONTEXT)), \
             patch.object(workflow,'checked',side_effect=execute):
            with self.assertRaisesRegex(ValueError,'synthetic failed proxy probe'):
                workflow.verify_runtime(prepared,workflow.CONTEXT)
        removals = [args for args in commands if args[0] in ('rm','volume','network') and 'rm' in args]
        self.assertTrue(any(args[:2] == ['volume','rm'] for args in removals))
        self.assertTrue(any(args[:2] == ['network','rm'] for args in removals))
        self.assertTrue(all(args[-1].startswith('local-web-nas-acceptance-') for args in removals))
        self.assertFalse(any('prune' in args or args[:2] == ['image','rm'] for args in commands))

    def test_uncertain_volume_create_is_cleaned_only_with_matching_ownership_label(self):
        identity = RuntimeIdentity('sha256:'+'a'*64,'sha256:'+'b'*64,'linux','amd64','node24','c'*64,'1')
        verified = SimpleNamespace(archive_digest='b'*64,image_digest=identity.image_digest,manifest_digest=None,os='linux',architecture='amd64')
        prepared = SimpleNamespace(runtime_archive=Path('/not-read/archive.tar'),descriptor=SimpleNamespace(runtime=identity))
        for foreign in (False,True):
            commands=[]
            def execute(argv, **kwargs):
                args=argv[3:];commands.append(args)
                if args[:2] == ['image','inspect']:
                    return json.dumps([dict(Id=identity.image_digest,Os='linux',Architecture='amd64')])
                if args[:2] == ['volume','create']:
                    raise subprocess.TimeoutExpired('owned volume create',1)
                if args[:2] == ['volume','inspect']:
                    return json.dumps([dict(Labels={workflow.OWNER_LABEL:'foreign' if foreign else args[-1].rsplit('-',1)[0]})])
                return ''
            with self.subTest(foreign=foreign), \
                 patch.object(workflow,'verify_docker_archive',return_value=verified), \
                 patch.object(workflow,'guard_context',return_value=('docker','--context',workflow.CONTEXT)), \
                 patch.object(workflow,'checked',side_effect=execute):
                with self.assertRaises((subprocess.TimeoutExpired,ValueError)):
                    workflow.verify_runtime(prepared,workflow.CONTEXT)
            self.assertEqual(any(args[:2] == ['volume','rm'] for args in commands),not foreign)
            self.assertFalse(any('prune' in args or args[:2] == ['image','rm'] for args in commands))

    def test_command_failures_do_not_echo_child_output_or_inherit_docker_overrides(self):
        completed = subprocess.CompletedProcess(['docker'], 1, stdout=b'private output', stderr=b'secret')
        with patch.object(workflow.subprocess, 'run', return_value=completed) as run:
            with self.assertRaisesRegex(ValueError, '^acceptance command failed$'):
                workflow.checked(['docker','info'])
        self.assertNotIn('DOCKER_HOST', run.call_args.kwargs['env'])
        self.assertEqual(run.call_args.kwargs['env']['GIT_CONFIG_GLOBAL'],workflow.os.devnull)
        self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)


if __name__ == '__main__': unittest.main()
