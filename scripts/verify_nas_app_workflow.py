#!/usr/bin/env python3
"""Disposable NAS app source acceptance; protocol checks are explicitly simulated.

Service runtime proof requires the existing no-host-mount Mac test VM. No NAS
connection, host profile installation, published ports or host bind mounts occur.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from local_web_server.app_check import AppChecker
from local_web_server.app_doctor import AppDoctor
from local_web_server.app_generator import AppGenerator, CreateAppRequest
from local_web_server.config import load_manifest
from local_web_server.git_build import GitRepository
from local_web_server.mac_dependency_builder import MacDockerDependencyBuilder
from local_web_server.models import HostApp, HostRegistry
from local_web_server.nas_lifecycle import NasHostDeploymentManager
from local_web_server.nas_models import NasTarget
from local_web_server.nas_transport import NasTransportError
from local_web_server.nas_workflow import NasReleasePreparer
from local_web_server.native_build import probe_native_platform
from local_web_server.process_runner import ProcessCommand, ProcessRunner
from local_web_server.production_build import verify_docker_archive
from local_web_server.release_manifest import candidate_image_references, plan_transfer

PROFILE = 'nas-media-worker-test'
CONTEXT = 'colima-' + PROFILE
ORIGIN = 'https://local.example.ts.net'
OWNER_LABEL = 'org.local-web.nas-app-acceptance'


def checked(argv, *, stdin=None, timeout=30):
    """Fixed argv, bounded captured output, no ambient endpoint or secret values."""
    environment = {'PATH':'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin',
                   'HOME':str(Path.home()), 'LANG':'C', 'LC_ALL':'C',
                   'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,
                   'GIT_NO_REPLACE_OBJECTS':'1','GIT_TERMINAL_PROMPT':'0'}
    with tempfile.TemporaryFile() as output:
        result = subprocess.run(argv, stdin=stdin if stdin is not None else subprocess.DEVNULL,
            stdout=output, stderr=subprocess.DEVNULL, env=environment, timeout=timeout, check=False)
        if result.returncode: raise ValueError('acceptance command failed')
        output.seek(0)
        content = output.read(1024*1024+1)
    if len(content) > 1024*1024: raise ValueError('acceptance output exceeded its bound')
    return content.decode('utf-8')


def guard_context(context, *, execute=checked):
    if context != CONTEXT: raise ValueError('only the explicit dedicated test Docker context is supported')
    rows = json.loads(execute(['docker','context','inspect',context]))
    expected = 'unix://' + str(Path.home()/'.colima'/PROFILE/'docker.sock')
    if (type(rows) is not list or len(rows) != 1
            or rows[0].get('Endpoints',{}).get('docker',{}).get('Host') != expected):
        raise ValueError('test context must select its exact local Unix socket')
    mounts = execute(['colima','--profile',PROFILE,'ssh','--','cat','/proc/self/mountinfo'])
    records = [line.partition(' - ') for line in mounts.splitlines()]
    if not records or any(not separator or len(before.split()) < 5 or not after.split() for before,separator,after in records):
        raise ValueError('cannot verify actual test VM mounts')
    for before, _, after in records:
        if (after.split()[0] in ('9p','virtiofs','nfs','nfs4','cifs','smb3','fuse.sshfs')
                or before.split()[4].startswith(('/Users','/Volumes','/volume','/mnt/host'))):
            raise ValueError('test VM has shared host mounts')
    docker = ('docker','--context',context)
    if execute([*docker,'info','--format','{{.OSType}}']).strip() != 'linux':
        raise ValueError('the test daemon must be Linux')
    return docker


# Fixed generated-foundation probes, not a domain API or a production proxy.
_CLIENT = r'''
const http=require('http');
const host=process.argv[1], port=43123;
function request(path,extra={},method='GET'){return new Promise((resolve,reject)=>{
 const r=http.request({host,port,path,method,headers:{Host:'local.example.ts.net','X-Forwarded-Proto':'https',...extra}},s=>{let b='';s.on('data',d=>b+=d);s.on('end',()=>resolve([s.statusCode,b]));});
 r.setTimeout(2000,()=>r.destroy(new Error('timeout')));r.on('error',reject);r.end();});}
(async()=>{
 let good;for(let i=0;i<50;i++){try{good=await request('/api/hello');break;}catch{await new Promise(r=>setTimeout(r,100));}}
 if(!good||good[0]!==200||JSON.parse(good[1]).message!=='Hello from the local service.')throw Error('hello');
 if((await request('/healthz',{},'HEAD'))[0]!==204)throw Error('health');
 for(const extra of [{Host:'other.example.test'},{Origin:'https://other.example.test'},{'X-Forwarded-Proto':'http'}]){
  if((await request('/api/hello',extra))[0]!==403)throw Error('trust');}
 console.log(JSON.stringify({hello:200,healthHead:204,rejectedHostOriginProto:true}));
})().catch(()=>process.exit(1));
'''


def verify_runtime(prepared, context):
    verified = verify_docker_archive(prepared.runtime_archive)
    identity = prepared.descriptor.runtime
    if (identity is None or 'sha256:' + verified.archive_digest != identity.dependency_digest
            or verified.image_digest != identity.image_digest
            or verified.manifest_digest != identity.manifest_digest
            or (verified.os, verified.architecture) != ('linux','amd64')):
        raise ValueError('runtime archive differs from the verified release')
    docker = guard_context(context)
    def run(*args, **kwargs): return checked([*docker,*args], **kwargs)
    candidates = candidate_image_references(identity)
    def resolve():
        for reference in candidates:
            try: row = json.loads(run('image','inspect','--platform','linux/amd64',reference))[0]
            except ValueError: continue
            if row.get('Id') not in candidates or (row.get('Os'),row.get('Architecture')) != ('linux','amd64'):
                raise ValueError('actual Linux image identity disagrees with the archive')
            return row['Id']
        return None
    image = resolve()
    if image is None:
        with prepared.runtime_archive.open('rb') as stream: run('image','load',stdin=stream,timeout=120)
        image = resolve()
    if image is None: raise ValueError('verified runtime image unavailable after load')
    prefix = 'local-web-nas-acceptance-' + uuid.uuid4().hex[:12]
    code, network, backend, staging, client = [prefix+'-'+name for name in ('code','network','backend','staging','client')]
    containers, volumes, networks = [], [], []
    label = OWNER_LABEL + '=' + prefix
    def retire(kind, name):
        rows = json.loads(run(kind,'inspect',name))
        if type(rows) is not list or len(rows) != 1: raise ValueError('selected resource identity unconfirmed')
        labels = rows[0].get('Config',{}).get('Labels',{}) if kind == 'container' else rows[0].get('Labels',{})
        if type(labels) is not dict or labels.get(OWNER_LABEL) != prefix:
            raise ValueError('selected resource belongs to another owner')
        action = ('rm','--force') if kind == 'container' else (kind,'rm')
        run(*action,name)
    try:
        volumes.append(code)
        run('volume','create','--label',label,code)
        normalize = "const f=require('fs');function walk(p){f.chmodSync(p,0o755);for(const e of f.readdirSync(p,{withFileTypes:true}))if(e.isDirectory())walk(p+'/'+e.name)}walk('/app/current')"
        containers.append(staging)
        run('create','--pull','never','--label',label,'--name',staging,'--platform','linux/amd64','--network','none','--read-only','--user','0:0',
            '--cap-drop','ALL','--cap-add','FOWNER','--security-opt','no-new-privileges',
            '--mount',f'type=volume,src={code},dst=/app/current','--entrypoint','node',image,'-e',normalize)
        run('cp',str(prepared.files_root)+'/.',staging+':/app/current',timeout=60)
        run('start','--attach',staging)
        retire('container',staging); containers.remove(staging)
        # A unique bridge has no external route and no published host ports.
        networks.append(network)
        run('network','create','--label',label,'--internal','--subnet','172.29.117.0/24',network)
        spec = prepared.runtime_spec
        values = dict(port='43123',state='/state',publicOrigin=ORIGIN,bindHost='0.0.0.0',proxyAddress='172.29.117.2')
        command = [argument.format_map(values) for argument in spec.command]
        containers.append(backend)
        run('run','--pull','never','--label',label,'--detach','--name',backend,'--platform','linux/amd64','--network',network,'--ip','172.29.117.3',
            '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--cpus',str(spec.cpu_limit),'--memory',str(spec.memory_limit_mib)+'m','--user',spec.user,
            '--mount',f'type=volume,src={code},dst=/app/current,readonly','--entrypoint',command[0],image,*command[1:])
        row = json.loads(run('inspect',backend))[0]
        if (row['HostConfig']['ReadonlyRootfs'] is not True or row['HostConfig'].get('PortBindings')
                or row['Config']['User'] != spec.user or row['Image'] != image
                or any(m['Type'] != 'volume' or m['RW'] for m in row['Mounts'])
                or set(row['NetworkSettings']['Networks']) != {network}
                or row['NetworkSettings']['Networks'][network]['IPAddress'] != '172.29.117.3'):
            raise ValueError('actual backend runtime differs from read-only isolated contract')
        actual = json.loads(run('exec',backend,'node','-p',
            'JSON.stringify({version:process.versions.node,modules:process.versions.modules,os:process.platform,architecture:process.arch})'))
        if (not actual['version'].startswith('24.') or actual['modules'] != '137'
                or (actual['os'],actual['architecture']) != ('linux','x64')):
            raise ValueError('actual Node version or Linux/amd64 ABI differs')
        run('exec',backend,'node','-e',"try{require('fs').writeFileSync('/app/current/unexpected-write','x');process.exit(1)}catch(e){if(e.code!=='EROFS')process.exit(1)}")
        containers.append(client)
        result = json.loads(run('run','--pull','never','--label',label,'--name',client,'--platform','linux/amd64','--network',network,
            '--ip','172.29.117.2','--read-only','--user',spec.user,'--cap-drop','ALL','--security-opt','no-new-privileges',
            '--entrypoint','node',image,'-e',_CLIENT,'172.29.117.3',timeout=30))
        retire('container',client); containers.remove(client)
        if json.loads(run('network','inspect',network))[0].get('Internal') is not True:
            raise ValueError('test bridge is not internal')
        return dict(evidence='actual guarded Mac VM Linux runtime',node=actual,**result,readOnlyCode=True,noHostPorts=True,noBindMounts=True)
    finally:
        failures = []
        for kind, names in (('container',containers),('volume',volumes),('network',networks)):
            for name in reversed(names):
                try: retire(kind,name)
                except Exception: failures.append(name)
        if failures: raise ValueError('owned test runtime cleanup unconfirmed: ' + ','.join(failures))


def _commit(repository, path, message):
    checked(['git','-C',str(repository),'add','--',path])
    checked(['git','-C',str(repository),'-c','core.hooksPath=/dev/null','-c','user.name=Acceptance Fixture',
             '-c','user.email=fixture@example.test','commit','-qm',message])
    return GitRepository(repository).main_commit()


def verify(*, kind='static', docker_context=None):
    if kind not in ('static','service'): raise ValueError('unsupported workflow kind')
    if kind == 'service' and docker_context is None: raise ValueError('service requires explicit --docker-context')
    if docker_context is not None: guard_context(docker_context)
    with tempfile.TemporaryDirectory(prefix='local-web-nas-app-') as directory:
        root = Path(directory).resolve()
        repository = root/'acceptance-app'
        generated = AppGenerator(ROOT).create(CreateAppRequest('acceptance-app','Acceptance App',repository,
            '/acceptance-app','book','#8EA7C6',(),kind))
        doctor = AppDoctor(target_kind='nas-docker')
        if doctor.inspect(repository).diagnostics: raise ValueError('generated NAS Doctor failed')
        # Publication renames the generated tree; refresh local tooling using the
        # same maintained preparation step as verify_new_app_workflow.py.
        ProcessRunner().run(repository,(ProcessCommand('app-local dependencies',('npm','ci','--ignore-scripts')),))
        AppChecker(doctor=doctor).check(repository)
        manifest = load_manifest(repository/'local-web.json')
        host = HostApp(manifest.id,repository,False,None,(),43123 if kind=='service' else None,
                       manifest.service.start_command if manifest.service else None)
        commands = []
        original = ProcessRunner.run
        def observe(self, source, planned):
            commands.extend(tuple(command.argv) for command in planned)
            return original(self,source,planned)
        installer = MacDockerDependencyBuilder(docker_context,root/'buildkit') if kind=='service' else None
        preparer = NasReleasePreparer(root/'cache',dependency_installer=installer)
        with patch.object(ProcessRunner,'run',observe):
            started = time.monotonic()
            cold = preparer.prepare(host,commit=generated.commit,destination=root/'cold')
            cold_seconds = time.monotonic() - started
            source = repository/'src/App.tsx'
            text = source.read_text()
            changed_text = text.replace("Start building this view's domain experience here.", "Source-only acceptance change.")
            if changed_text == text: raise ValueError('generated source fixture marker unavailable')
            source.write_text(changed_text)
            changed = _commit(repository,'src/App.tsx','Disposable source-only acceptance change')
            commands.clear()
            started = time.monotonic()
            warm = preparer.prepare(host,commit=changed,destination=root/'warm')
            warm_seconds = time.monotonic() - started
        installer_calls = sum(argv[:2] in (('npm','ci'),('npm','install')) for argv in commands)
        runtime_builds = sum('buildx' in argv for argv in commands)
        if (warm.native_cache_hit is not True or kind=='service' and warm.runtime_cache_hit is not True
                or installer_calls or runtime_builds
                or cold.descriptor.runtime != warm.descriptor.runtime or cold.descriptor.digest == warm.descriptor.digest):
            raise ValueError('source-only warm preparation did not reuse dependency caches')
        transfer = plan_transfer(warm.descriptor,frozenset(entry.sha256 for entry in cold.descriptor.files),
                                 (cold.descriptor.runtime,) if cold.descriptor.runtime else ())
        if not transfer.missing_files or transfer.missing_bytes >= sum(entry.size for entry in warm.descriptor.files):
            raise ValueError('source update was not an incremental blob plan')
        class Unavailable:
            calls = 0
            def request(self, action, *, operation_id, payload):
                self.calls += 1
                raise NasTransportError(action,operation_id,outcome_unknown=True)
        unavailable = Unavailable()
        registry = HostRegistry('local.example.ts.net',root/'nas-runtime-must-not-exist',(host,),3,ORIGIN,'tailscale-serve',
            NasTarget('nas-docker',('/not-executed/protocol-fixture',),'172.30.200.0/24','172.30.200.2',docker_context))
        manager = NasHostDeploymentManager(registry,client_root=root/'client',transport=unavailable)
        try: manager.deploy(host.id)
        except NasTransportError: pass
        else: raise ValueError('unavailable protocol fixture unexpectedly deployed')
        if unavailable.calls != 1 or registry.runtime_root.exists(): raise ValueError('NAS request fell back to local runtime')
        runtime = verify_runtime(warm,docker_context) if kind=='service' else dict(evidence='not applicable: static release has no service runtime')
        content = json.loads((repository/'local-web.json').read_text()); content.pop('production')
        (repository/'local-web.json').write_text(json.dumps(content))
        _commit(repository,'local-web.json','Disposable unsupported legacy recipe fixture')
        try: manager.deploy(host.id)
        except ValueError as error:
            if 'committed production recipe' not in str(error): raise
        else: raise ValueError('legacy fixture unexpectedly became NAS eligible')
        if unavailable.calls != 1 or registry.runtime_root.exists(): raise ValueError('legacy recipe used a local fallback')
        return dict(kind=kind,sourceEvidence='actual generator, NAS Doctor, AppChecker and Mac native preparation',
            protocolEvidence='in-process unavailable-adapter simulation; no NAS connection',
            coldCommit=generated.commit,warmCommit=changed,coldRelease=cold.descriptor.digest,warmRelease=warm.descriptor.digest,
            nativePlatform=probe_native_platform().to_dict(),warmInstallerCalls=installer_calls,warmRuntimeBuilds=runtime_builds,
            coldPrepareSeconds=round(cold_seconds,3),warmPrepareSeconds=round(warm_seconds,3),
            warmNativeCacheHit=warm.native_cache_hit,warmRuntimeCacheHit=warm.runtime_cache_hit,
            incrementalBlobs=len(transfer.missing_files),incrementalBytes=transfer.missing_bytes,
            legacyRefused=True,remoteUnavailableRefused=True,noMacFallback=True,runtime=runtime)


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kind',choices=('static','service'),default='static')
    parser.add_argument('--docker-context')
    options = parser.parse_args(arguments)
    try:
        print(json.dumps(verify(kind=options.kind,docker_context=options.docker_context),sort_keys=True))
        return 0
    except (ValueError,OSError,subprocess.SubprocessError) as error:
        cause = error
        labels = []
        while cause is not None and len(labels) < 4:
            labels.append(type(cause).__name__ + ': ' + str(cause)[:160])
            cause = cause.__cause__
        print('NAS app workflow verification failed: ' + ' <- '.join(labels),file=sys.stderr)
        return 1


if __name__ == '__main__': raise SystemExit(main())
