"""Reviewed NAS installation plans; no SSH, bootstrap or host service effects.

Apply consumes exact prepared bytes through an injected publication manager.
Initial privileged bootstrap and enabling production callbacks remain separate
installation work. Evidence is a caller-supplied observation identity, never a
live read or an inference from passing source tests.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import ClassVar, Mapping, TYPE_CHECKING

from .nas_install import PreparedNasPlatform, PLATFORM_APP_ID
from .nas_render import ContainerRuntimeSpec

if TYPE_CHECKING:
    from .nas_deploy import NasDeploymentManager, PublicationResult
    from .nas_workflow import PreparedNasRelease

STORE_ROOT = '/volume2/docker/local-web'
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_APP = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z')


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()


def _sha(value, *, optional=False):
    if optional and value is None: return
    if type(value) is not str or not _SHA.fullmatch(value): raise ValueError('full SHA-256 identity required')


@dataclass(frozen=True)
class InstallationApproval:
    platform_release_digest: str
    source_config_digest: str
    expected_current: str | None
    previous_config_digest: str | None
    profile_revision: str
    permissions_digest: str
    evidence_digest: str
    store_root: str = STORE_ROOT
    selected_app_id: str | None = None
    selected_release_digest: str | None = None
    selected_descriptor: object | None = None

    def __post_init__(self):
        for name in ('platform_release_digest', 'source_config_digest', 'profile_revision', 'permissions_digest', 'evidence_digest'):
            _sha(getattr(self, name))
        for name in ('expected_current', 'previous_config_digest', 'selected_release_digest'): _sha(getattr(self, name), optional=True)
        if self.selected_descriptor is not None:
            from .release_manifest import ReleaseDescriptorV1
            if (not isinstance(self.selected_descriptor,ReleaseDescriptorV1) or self.selected_descriptor.app_id!=self.selected_app_id
                    or self.selected_descriptor.digest!=self.selected_release_digest):raise ValueError('selected descriptor binding disagrees')
        if (self.selected_app_id is None)!=(self.selected_descriptor is None):raise ValueError('complete selected descriptor required')
        if (self.store_root != STORE_ROOT or (self.expected_current is None) != (self.previous_config_digest is None)
                or (self.selected_app_id is None) != (self.selected_release_digest is None)
                or self.selected_app_id is not None and (type(self.selected_app_id) is not str or not _APP.fullmatch(self.selected_app_id)
                                                        or self.selected_app_id == PLATFORM_APP_ID)):
            raise ValueError('invalid installation approval binding')

    def to_dict(self) -> dict:
        return dict(schemaVersion=1, storeRoot=self.store_root, platformReleaseDigest=self.platform_release_digest,
                    sourceConfigDigest=self.source_config_digest, expectedCurrent=self.expected_current,
                    previousConfigDigest=self.previous_config_digest, profileRevision=self.profile_revision,
                    permissionsDigest=self.permissions_digest, evidenceDigest=self.evidence_digest,
                    selectedApp=None if self.selected_app_id is None else dict(appId=self.selected_app_id, releaseDigest=self.selected_release_digest,descriptor=self.selected_descriptor.to_dict()))

    @classmethod
    def from_dict(cls, value) -> InstallationApproval:
        names = {'schemaVersion','storeRoot','platformReleaseDigest','sourceConfigDigest','expectedCurrent','previousConfigDigest',
                 'profileRevision','permissionsDigest','evidenceDigest','selectedApp'}
        if type(value) is not dict or set(value) != names or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1:
            raise ValueError('invalid installation approval fields')
        selected = value['selectedApp']
        if selected is not None and (type(selected) is not dict or set(selected) != {'appId','releaseDigest','descriptor'}):
            raise ValueError('invalid selected app binding')
        from .release_manifest import ReleaseDescriptorV1
        return cls(value['platformReleaseDigest'],value['sourceConfigDigest'],value['expectedCurrent'],value['previousConfigDigest'],
                   value['profileRevision'],value['permissionsDigest'],value['evidenceDigest'],value['storeRoot'],
                   selected['appId'] if selected else None,selected['releaseDigest'] if selected else None,
                   ReleaseDescriptorV1.from_dict(selected['descriptor']) if selected else None)

    @property
    def digest(self): return hashlib.sha256(canonical(self.to_dict())).hexdigest()


@dataclass(frozen=True)
class InstallationEvidence:
    name: str
    status: str
    observation_digest: str | None
    REQUIRED: ClassVar[tuple[str,...]] = ('runtime-features','subnet','storage','permissions','backup-coverage',
                                          'host-profile-export','hostname-inventory')

    def __post_init__(self):
        if self.name not in self.REQUIRED or self.status not in ('verified','missing','failed'):
            raise ValueError('invalid installation prerequisite')
        _sha(self.observation_digest, optional=self.status != 'verified')


@dataclass(frozen=True)
class HostnameHandoff:
    production_name: str
    retained_mac_name: str
    temporary_nas_name: str

    def __post_init__(self):
        names = (self.production_name,self.retained_mac_name,self.temporary_nas_name)
        if len(set(names)) != 3 or any(type(name) is not str or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', name) for name in names):
            raise ValueError('three exact distinct Tailscale names required')

    def steps(self):
        return (f'Inventory the current Mac node {self.production_name}, all grants, Serve routes, certificates and other uses; retain its identity.',
                f'Create a dedicated temporary Local Web node {self.temporary_nas_name}; leave existing NAS media identity and access unchanged.',
                f'Rehearse valid HTTPS, allowed clients, no Funnel, wrong Host/direct HTTP rejection and app backups on {self.temporary_nas_name}.',
                f'After separate cutover approval and writer fencing, rename Mac {self.production_name} to {self.retained_mac_name}; rename the dedicated NAS node {self.temporary_nas_name} to {self.production_name}.',
                f'Verify exact original HTTPS hostname/certificate, destination, grants, Serve loopback proxy and every public app health route before admitting writers.',
                f'On failure keep writers fenced: return NAS {self.production_name} to {self.temporary_nas_name}, return Mac {self.retained_mac_name} to {self.production_name}, restore reviewed Serve/grants and verify HTTPS.',
                'After NAS writes, transfer the latest consistent app data/media through its tested restore route before returning Mac authority; code rollback does not restore data.')


@dataclass(frozen=True)
class BackupRequirement:
    path: str
    strategy: str
    reason: str


@dataclass(frozen=True)
class NasInstallationPreview:
    prepared: PreparedNasPlatform
    approval: InstallationApproval
    evidence: tuple[InstallationEvidence,...]
    backup_requirements: tuple[BackupRequirement,...]
    hostname_steps: tuple[str,...]

    @property
    def missing_prerequisites(self):
        missing = [entry.name for entry in self.evidence if entry.status != 'verified']
        if not self.hostname_steps and 'hostname-inventory' not in missing: missing.append('hostname-inventory')
        return tuple(sorted(missing))

    @property
    def ready(self): return not self.missing_prerequisites


def select_runtime_specs(service_app_ids: tuple[str,...], observed_specs: Mapping[str,ContainerRuntimeSpec], *,
                         selected_candidate: PreparedNasRelease | None = None) -> tuple[ContainerRuntimeSpec,...]:
    """Use current observed specs, replacing only one exact prepared candidate.

    Never load main-branch recipes for unselected apps. The caller must derive
    observed_specs from the current immutable remote release metadata.
    """
    specs = dict(observed_specs)
    if any(key != spec.app_id for key,spec in specs.items()) or set(specs)-set(service_app_ids):
        raise ValueError('observed runtime inventory disagrees')
    if selected_candidate is not None:
        descriptor, spec = selected_candidate.descriptor, selected_candidate.runtime_spec
        if spec is not None:
            if (spec.app_id != descriptor.app_id or descriptor.app_id not in service_app_ids or descriptor.runtime is None
                    or spec.image != descriptor.runtime.image_digest): raise ValueError('selected runtime identity disagrees')
            current = specs.get(spec.app_id)
            policy = ('port','cpu_limit','memory_limit_mib','secret_files','user','health_runtime')
            if current is not None and any(getattr(current,key) != getattr(spec,key) for key in policy):
                raise ValueError('runtime policy change requires reviewed app migration; preserve rollback authorization')
            specs[spec.app_id] = spec
        elif descriptor.app_id in service_app_ids or descriptor.runtime is not None:
            raise ValueError('selected service is missing its exact runtime')
    if set(specs) != set(service_app_ids): raise ValueError('current unselected runtime observations required')
    return tuple(specs[key] for key in sorted(specs))


def preview_nas_installation(*, prepared: PreparedNasPlatform, store_root: str, profile_revision: str,
                             expected_current: str | None, previous_config_digest: str | None,
                             evidence: tuple[InstallationEvidence,...] = (), hostname_handoff: HostnameHandoff | None = None,
                             selected_candidate: PreparedNasRelease | None = None) -> NasInstallationPreview:
    if prepared.descriptor.app_id != PLATFORM_APP_ID or prepared.descriptor.runtime is not None:
        raise ValueError('installation requires reserved static platform release')
    prepared.verify()
    if len({entry.name for entry in evidence}) != len(evidence): raise ValueError('duplicate installation evidence')
    by_name = {entry.name:entry for entry in evidence}
    observations = tuple(by_name.get(name,InstallationEvidence(name,'missing',None)) for name in InstallationEvidence.REQUIRED)
    settings = json.loads(next(file.content for file in prepared.files if file.path=='runtime-settings.json'))
    if hostname_handoff is not None and settings['publicOrigin'].split('://')[1].split('.')[0] not in (
            hostname_handoff.production_name, hostname_handoff.temporary_nas_name):
        raise ValueError('hostname plan does not name the production or temporary NAS origin')
    selected = selected_candidate.descriptor if selected_candidate is not None else None
    hostname_steps = hostname_handoff.steps() if hostname_handoff else ()
    approval = InstallationApproval(prepared.descriptor.digest,prepared.source_config_digest,expected_current,previous_config_digest,
        profile_revision,hashlib.sha256(canonical([asdict(entry) for entry in prepared.permissions])).hexdigest(),
        hashlib.sha256(canonical(dict(evidence=[asdict(entry) for entry in observations],
                                     hostnameSteps=hostname_steps))).hexdigest(),
        store_root,selected.app_id if selected else None,selected.digest if selected else None,selected)
    backups = [BackupRequirement(path,'essentials',reason) for path,reason in (
        ('tailscale','Dedicated node identity and Serve state'),('secrets','Selected app credentials'),
        ('installation-approvals','Retained configuration authorization for recovery'),
        ('operations','Original-operation receipts and uncertain outcomes'),
        ('apps/*/descriptors','Immutable release identities'),('apps/*/generations','Current/previous recovery pointers'),
        ('apps/*/pointers','Atomic current/previous selection'),('host-profile/history','Mac local-web host backup export: exact profile and revision history'))]
    backups += [BackupRequirement('state/'+app,'app-consistent-backup','All authoritative app records, uploads/media, revision/locks and backup retention; test restore with media reference integrity') for app in sorted(settings['apps']) if settings['apps'][app]['kind']=='service']
    backups.append(BackupRequirement('blobs/releases/runtimes/build-caches','exclude-rebuildable','Verified code/image/build artifacts are not authoritative app data'))
    return NasInstallationPreview(prepared,approval,observations,tuple(backups),hostname_steps)


def apply_nas_installation(preview: NasInstallationPreview, *, approved_capsule_digest: str, profile_revision: str,
                           expected_current: str | None, manager: NasDeploymentManager, operation_id: str) -> PublicationResult:
    """Publish exact reviewed bytes only through an explicitly injected manager.

    This is not a privileged bootstrap command. The installation-owned adapter
    must already support capsule approval; its default callbacks fail closed.
    """
    _validate_review(preview,approved_capsule_digest,profile_revision,expected_current)
    with tempfile.TemporaryDirectory(prefix='local-web-nas-platform-') as directory:
        root = Path(directory)/'files'
        preview.prepared.write_files(root)
        return manager.deploy(preview.prepared.descriptor,root,operation_id=operation_id,expected_current=expected_current,
                              installation_approval=preview.approval)


def _validate_review(preview,approved_capsule_digest,profile_revision,expected_current):
    _sha(approved_capsule_digest)
    if (not preview.ready or approved_capsule_digest != preview.approval.digest
            or profile_revision != preview.approval.profile_revision or expected_current != preview.approval.expected_current):
        raise ValueError('installation review is missing, changed or stale')
    preview.prepared.verify()
    if hashlib.sha256(canonical(dict(evidence=[asdict(entry) for entry in preview.evidence],
                                    hostnameSteps=preview.hostname_steps))).hexdigest() != preview.approval.evidence_digest:
        raise ValueError('installation prerequisite evidence changed after review')
    if (preview.prepared.descriptor.digest != preview.approval.platform_release_digest
            or preview.prepared.source_config_digest != preview.approval.source_config_digest
            or hashlib.sha256(canonical([asdict(entry) for entry in preview.prepared.permissions])).hexdigest() != preview.approval.permissions_digest):
        raise ValueError('prepared installation changed after review')


class NasPlatformInstallationSession:
    """Separate durable platform intent, never stored in the selected app receipt.

    Transport/public health are injected by the approved installation route.
    No default SSH, live bootstrap or source-copy action exists here.
    """
    def __init__(self,client_root,transport,*,public_health,on_applied=None,validate_profile=None,profile_revision=None):
        from .nas_lifecycle import NasOperationStore
        self.store = NasOperationStore(Path(client_root),PLATFORM_APP_ID)
        self.transport,self.public_health,self.on_applied = transport,public_health,on_applied
        self.validate_profile=validate_profile
        _sha(profile_revision,optional=True)
        self.profile_revision=profile_revision

    def authority(self):
        if self.validate_profile is not None:self.validate_profile()

    def publisher(self,operation_id):
        from .nas_deploy import NasDeploymentManager
        session=self
        class AuthorizedTransport:
            def request(self,action,*,operation_id,payload):
                if action!='status':session.authority()
                return session.transport.request(action,operation_id=operation_id,payload=payload)
        def before_transition(action):
            self.authority()
            self.store.before_transition(operation_id,action)
        def public_health(descriptor):
            self.authority()
            healthy=self.public_health(descriptor)
            self.authority()
            return healthy
        return NasDeploymentManager(AuthorizedTransport(),public_health=public_health,
            before_transition=before_transition,
            select_upload_blobs=lambda requested:self.store.select_upload_blobs(operation_id,requested))

    def apply(self,preview,*,approved_capsule_digest,profile_revision,expected_current,operation_id,next_context=None):
        import os
        _validate_review(preview,approved_capsule_digest,profile_revision,expected_current)
        if type(operation_id) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',operation_id):
            raise ValueError('invalid platform operation identity')
        with self.store.lock():
            if self.store.pending() is not None: raise ValueError('platform installation requires original-operation recovery')
            directory = self.store.layout.app_root/('installation-'+operation_id)
            # A prior artifact is preserved rather than silently overwritten.
            os.mkdir(directory,0o700)
            preview.prepared.write_files(directory/'files')
            with (directory/'installation-approval.json').open('xb') as output:
                os.chmod(output.name,0o600)
                output.write(canonical(preview.approval.to_dict())); output.flush(); os.fsync(output.fileno())
            if next_context is not None:
                next_context.save(directory)
            # The durable receipt is established before the first remote request.
            self.authority()
            self.store.begin(operation_id,preview.prepared.descriptor,expected_current=expected_current,
                             prepared_root=directory/'files',runtime_archive=None,action='deploy')
            result = self.publisher(operation_id).deploy(preview.prepared.descriptor,directory/'files',operation_id=operation_id,
                        expected_current=expected_current,installation_approval=preview.approval)
            return self.complete(result,preview.approval)

    def complete(self,result,approval):
        self.authority()
        if result.state=='active' and result.current==approval.platform_release_digest:
            receipt=self.store.pending()
            directory=Path(receipt['preparedRoot']).parent
            try: context=SavedNasInstallationContext.load(directory)
            except FileNotFoundError: context=None
            if context is not None:
                if dict(context.platform_releases).get(approval.platform_release_digest)!=approval.source_config_digest:
                    raise ValueError('saved context disagrees with applied configuration')
                context.save(self.store.layout.runtime_root)
        if self.on_applied is not None and result.state=='active' and result.current==approval.platform_release_digest:
            self.on_applied(approval)
        self.store.complete(result.operation_id,state=result.state,current=result.current)
        if result.state!='active' or result.current!=approval.platform_release_digest:
            raise ValueError(f'requested NAS platform change failed health checks; recovered release {result.current}; operation {result.operation_id} is finished')
        return result

    def recover(self,operation_id):
        from .release_manifest import ReleaseDescriptorV1
        import os
        with self.store.lock():
            receipt = self.store.pending()
            if receipt is None or receipt['operationId']!=operation_id:
                raise ValueError('original platform operation required')
            descriptor = ReleaseDescriptorV1.from_dict(receipt['descriptor'])
            root = Path(receipt['preparedRoot'])
            expected_root = self.store.layout.app_root/('installation-'+operation_id)/'files'
            if root != expected_root: raise ValueError('platform recovery artifact path changed')
            fd = os.open(root.parent/'installation-approval.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
            with os.fdopen(fd,'rb') as source:
                raw = source.read(65537)
            if len(raw)>65536: raise ValueError('platform capsule limit')
            approval = InstallationApproval.from_dict(json.loads(raw))
            if (canonical(approval.to_dict())!=raw or approval.platform_release_digest!=descriptor.digest
                    or approval.expected_current!=receipt['expectedCurrent']):
                raise ValueError('original platform capsule changed')
            if self.profile_revision is not None and approval.profile_revision!=self.profile_revision:
                raise ValueError('original platform profile changed; preserve the operation for recovery')
            publisher = self.publisher(operation_id)
            status = self.transport.request('status',operation_id=operation_id,
                        payload=dict(appId=PLATFORM_APP_ID,releaseDigest=descriptor.digest))
            if (receipt['phase']=='preparing' and status.get('phase') in ('prepared','absent')
                    and status.get('current')==receipt['expectedCurrent']):
                result = publisher.deploy(descriptor,root,operation_id=operation_id,expected_current=receipt['expectedCurrent'],
                                          installation_approval=approval)
            else:
                # Reconcile consumes the already-observed acknowledgement; no
                # mutation is replayed following uncertain activation.
                result = publisher._complete_transition(descriptor,operation_id,receipt['expectedCurrent'],status)
            return self.complete(result,approval)


@dataclass(frozen=True)
class KnownRuntimeVersion:
    descriptor: object
    runtime_spec: ContainerRuntimeSpec

    def __post_init__(self):
        descriptor,spec=self.descriptor,self.runtime_spec
        metadata=(json.dumps(asdict(spec),sort_keys=True,separators=(',',':'))+'\n').encode()
        entries=[entry for entry in descriptor.files if entry.path=='.local-web-runtime.json']
        if (descriptor.app_id!=spec.app_id or descriptor.runtime is None or descriptor.runtime.image_digest!=spec.image
                or len(entries)!=1 or entries[0].sha256!=hashlib.sha256(metadata).hexdigest() or entries[0].size!=len(metadata)):
            raise ValueError('saved runtime metadata must match its exact release descriptor')

    def to_dict(self): return dict(descriptor=self.descriptor.to_dict(),runtimeSpec=asdict(self.runtime_spec))

    @classmethod
    def from_dict(cls,value):
        from .release_manifest import ReleaseDescriptorV1
        if type(value) is not dict or set(value)!={'descriptor','runtimeSpec'}: raise ValueError('invalid saved runtime version')
        raw=dict(value['runtimeSpec'])
        raw['command']=tuple(raw['command']); raw['secret_files']=tuple(raw['secret_files'])
        return cls(ReleaseDescriptorV1.from_dict(value['descriptor']),ContainerRuntimeSpec(**raw))


def _client_directory(root):
    """Open an existing private client directory, rejecting every symlink."""
    import os,stat
    root=Path(root)
    if not root.is_absolute() or '..' in root.parts: raise ValueError('absolute private client root required')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:
        for part in root.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        info=os.fstat(fd)
        if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700: raise ValueError('client context root must be private')
        return fd
    except BaseException:
        os.close(fd);raise


@dataclass(frozen=True)
class SavedNasInstallationContext:
    public_origin: str
    network_subnet: str
    proxy_address: str
    caddy_image: str
    tailscale_image: str
    evidence: tuple[InstallationEvidence,...]
    hostname_handoff: HostnameHandoff
    known_runtime_versions: tuple[KnownRuntimeVersion,...]
    platform_releases: tuple[tuple[str,str],...]

    def __post_init__(self):
        from .nas_render import _image
        from urllib.parse import urlsplit
        import ipaddress
        _image(self.caddy_image);_image(self.tailscale_image)
        origin=urlsplit(self.public_origin)
        subnet=ipaddress.IPv4Network(self.network_subnet,strict=True)
        proxy=ipaddress.IPv4Address(self.proxy_address)
        if (origin.scheme!='https' or origin.netloc!=origin.hostname or not origin.hostname.endswith('.ts.net')
                or origin.path or origin.query or origin.fragment or proxy not in subnet or not subnet.is_private):
            raise ValueError('invalid saved NAS target')
        if len({entry.name for entry in self.evidence})!=len(self.evidence): raise ValueError('duplicate saved prerequisite')
        if len({(v.descriptor.app_id,v.descriptor.digest) for v in self.known_runtime_versions})!=len(self.known_runtime_versions):
            raise ValueError('duplicate saved runtime version')
        for release,config in self.platform_releases: _sha(release);_sha(config)
        if len(dict(self.platform_releases))!=len(self.platform_releases): raise ValueError('duplicate platform identity')

    def to_dict(self):
        return dict(schemaVersion=1,storeRoot=STORE_ROOT,publicOrigin=self.public_origin,networkSubnet=self.network_subnet,
            proxyAddress=self.proxy_address,caddyImage=self.caddy_image,tailscaleImage=self.tailscale_image,
            evidence=[asdict(entry) for entry in self.evidence],hostnameHandoff=asdict(self.hostname_handoff),
            knownRuntimeVersions=[entry.to_dict() for entry in self.known_runtime_versions],platformReleases=dict(self.platform_releases))

    @classmethod
    def from_dict(cls,value):
        names={'schemaVersion','storeRoot','publicOrigin','networkSubnet','proxyAddress','caddyImage','tailscaleImage',
               'evidence','hostnameHandoff','knownRuntimeVersions','platformReleases'}
        if (type(value) is not dict or set(value)!=names or type(value['schemaVersion']) is not int
                or value['schemaVersion']!=1 or value['storeRoot']!=STORE_ROOT): raise ValueError('invalid saved NAS installation context')
        return cls(value['publicOrigin'],value['networkSubnet'],value['proxyAddress'],value['caddyImage'],value['tailscaleImage'],
            tuple(InstallationEvidence(**entry) for entry in value['evidence']),HostnameHandoff(**value['hostnameHandoff']),
            tuple(KnownRuntimeVersion.from_dict(entry) for entry in value['knownRuntimeVersions']),tuple(sorted(value['platformReleases'].items())))

    def validate_target(self,registry):
        if (registry.target.kind!='nas-docker' or str(registry.runtime_root)!=STORE_ROOT or registry.public_origin!=self.public_origin
                or registry.target.network_subnet!=self.network_subnet or registry.target.proxy_address!=self.proxy_address):
            raise ValueError('saved NAS context target changed; prepare a reviewed installation')

    def observed_runtime_specs(self,descriptors):
        known={(entry.descriptor.app_id,entry.descriptor.digest):entry for entry in self.known_runtime_versions}
        result={}
        for app,descriptor in descriptors.items():
            if descriptor is None or descriptor.runtime is None: continue
            version=known.get((app,descriptor.digest))
            if version is None or version.descriptor!=descriptor: raise ValueError('unobserved runtime metadata; prepare reviewed installation context')
            result[app]=version.runtime_spec
        return result

    def with_applied(self,preview,selected_candidate=None):
        from dataclasses import replace
        versions={(v.descriptor.app_id,v.descriptor.digest):v for v in self.known_runtime_versions}
        if selected_candidate is not None and selected_candidate.runtime_spec is not None:
            version=KnownRuntimeVersion(selected_candidate.descriptor,selected_candidate.runtime_spec)
            versions[(version.descriptor.app_id,version.descriptor.digest)]=version
        platforms=dict(self.platform_releases)
        platforms[preview.prepared.descriptor.digest]=preview.prepared.source_config_digest
        return replace(self,known_runtime_versions=tuple(versions[key] for key in sorted(versions)),platform_releases=tuple(sorted(platforms.items())))

    def save(self,client_root):
        import os,stat,uuid
        data=canonical(self.to_dict())
        if len(data)>2*1024*1024: raise ValueError('saved installation context limit')
        parent=_client_directory(client_root);temporary='.installation-context-'+uuid.uuid4().hex
        try:
            try:
                info=os.stat('installation-context.json',dir_fd=parent,follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
                    raise ValueError('unsafe existing installation context')
            except FileNotFoundError: pass
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
            with os.fdopen(fd,'wb') as output: output.write(data);output.flush();os.fsync(output.fileno())
            os.replace(temporary,'installation-context.json',src_dir_fd=parent,dst_dir_fd=parent);os.fsync(parent)
        finally:
            try:os.unlink(temporary,dir_fd=parent)
            except FileNotFoundError:pass
            os.close(parent)

    @classmethod
    def load(cls,client_root):
        import os,stat
        parent=_client_directory(client_root)
        try:fd=os.open('installation-context.json',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        finally:os.close(parent)
        with os.fdopen(fd,'rb') as source:
            info=os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
                raise ValueError('unsafe installation context')
            data=source.read(2*1024*1024+1)
        if len(data)>2*1024*1024:raise ValueError('installation context limit')
        result=cls.from_dict(json.loads(data))
        if canonical(result.to_dict())!=data:raise ValueError('installation context must be canonical')
        return result


def install_saved_nas_platform(repository,registry,*,dry_run,profile_revision,selected_candidate=None,
                               client_root=None,transport=None,health=None,prepare=None,observe=None,context_loader=None,validate_profile=None):
    """Normal post-bootstrap refresh, using current immutable metadata only.

    The bootstrap context is explicit; its absence never falls back to Mac
    installation or a source-build recipe for an unselected app.
    """
    import uuid
    from .nas_lifecycle import nas_client_root, _observe
    from .nas_transport import NasTransport
    from .services import HttpHealthChecker
    from .public_origin import join_public_origin
    from .git_build import GitRepository
    from .git_runner import run_git
    from .config import validate_registered_app
    from .nas_install import prepare_nas_platform
    from .render import THEME_ROUTE,INDEX_REGISTRY_V2_ROUTE
    client_root=Path(client_root) if client_root is not None else nas_client_root(registry)
    try:context=(context_loader or SavedNasInstallationContext.load)(client_root)
    except FileNotFoundError as error:
        raise ValueError('NAS bootstrap context unavailable; prepare and approve a dedicated NAS installation first') from error
    context.validate_target(registry)
    if selected_candidate is not None and selected_candidate.descriptor.rollback_policy!='compatible':
        raise ValueError('selected app requires its reviewed data migration')
    transport=transport or NasTransport(registry.target,timeout_seconds=450)
    health=health or HttpHealthChecker()
    observe=observe or _observe
    def public_health(_descriptor):
        live=observe(transport,PLATFORM_APP_ID)
        return (live.observed_healthy is True and all(health.check(join_public_origin(registry.public_origin,path)).healthy
            for path in ('/',THEME_ROUTE,INDEX_REGISTRY_V2_ROUTE,'/_local-web/platform/ui-gallery/')))
    session=NasPlatformInstallationSession(client_root,transport,public_health=public_health,validate_profile=validate_profile,
                                           profile_revision=profile_revision)
    with session.store.lock():pending=session.store.pending()
    if pending is not None:
        if dry_run:raise ValueError('platform installation requires original-operation recovery before a new preview')
        session.recover(pending['operationId'])
        context=(context_loader or SavedNasInstallationContext.load)(client_root)
    platform_live=observe(transport,PLATFORM_APP_ID)
    if platform_live.descriptor is None or platform_live.observed_healthy is not True:
        raise ValueError('current approved NAS platform must be freshly healthy')
    previous_config=dict(context.platform_releases).get(platform_live.descriptor.digest)
    if previous_config is None:raise ValueError('unrecognized current platform configuration; recover its original installation')
    observed={host.id:observe(transport,host.id).descriptor for host in registry.apps}
    specs=context.observed_runtime_specs(observed)
    manifests={}
    for host in registry.apps:
        live=observed[host.id]
        if selected_candidate is not None and host.id==selected_candidate.descriptor.app_id:
            if live is not None and live.data_contract!=selected_candidate.descriptor.data_contract:
                raise ValueError('existing app data contract change requires reviewed migration')
            commit=selected_candidate.descriptor.source_commit
        elif live is not None:commit=live.source_commit
        else:raise ValueError('unselected app has no observed release; prepare its explicit bootstrap installation')
        repository_view=GitRepository(host.repository)
        _,manifest=validate_registered_app(host,repository_view.manifest_at(commit))
        if selected_candidate is not None and host.id==selected_candidate.descriptor.app_id and live is not None:
            _,previous_manifest=validate_registered_app(host,repository_view.manifest_at(live.source_commit))
            def policy(value):
                return (value.kind,value.route,value.health_path,
                        value.service.internal_health_path if value.service else None,
                        value.service.proxy_paths if value.service else ())
            if policy(previous_manifest)!=policy(manifest):
                raise ValueError('app ingress/health policy change requires reviewed migration to preserve code rollback')
        if live is None and manifest.kind=='service':
            spec=selected_candidate.runtime_spec if selected_candidate is not None and selected_candidate.descriptor.app_id==host.id else None
            if (spec is None or selected_candidate.descriptor.data_contract!='none' or selected_candidate.descriptor.rollback_policy!='compatible'
                    or spec.user!='10001:10001' or spec.secret_files
                    or any(v.descriptor.app_id==host.id and v.descriptor.data_contract!='none' for v in context.known_runtime_versions)):
                raise ValueError('new data-bearing service permissions/backup coverage require explicit reviewed installation')
        manifests[host.id]=manifest
    runtime_specs=select_runtime_specs(tuple(sorted(app for app,m in manifests.items() if m.kind=='service')),specs,selected_candidate=selected_candidate)
    source=run_git(Path(repository),('rev-parse','HEAD'),maximum_stdout=128)
    if source.returncode:raise ValueError('platform source revision unavailable')
    prepared=(prepare or prepare_nas_platform)(repository,registry,manifests,runtime_specs,
        source_commit=source.stdout.strip(),caddy_image=context.caddy_image,tailscale_image=context.tailscale_image,build=True)
    if validate_profile is not None:validate_profile()
    preview=preview_nas_installation(prepared=prepared,store_root=str(registry.runtime_root),profile_revision=profile_revision,
        expected_current=platform_live.descriptor.digest,previous_config_digest=previous_config,evidence=context.evidence,
        hostname_handoff=context.hostname_handoff,selected_candidate=selected_candidate)
    if dry_run:return preview
    if not preview.ready:raise ValueError('saved NAS bootstrap prerequisite evidence is incomplete; prepare reviewed installation')
    next_context=context.with_applied(preview,selected_candidate)
    return session.apply(preview,approved_capsule_digest=preview.approval.digest,profile_revision=profile_revision,
        expected_current=platform_live.descriptor.digest,operation_id=uuid.uuid4().hex,next_context=next_context)
