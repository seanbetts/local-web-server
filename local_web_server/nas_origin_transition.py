"""Exact origin handoff plans and local recovery context publication.

No transport, hostname rename, profile apply, service or data operation exists
here. The private driver owns those separately authorized effects. Its one-shot
callback must journal its own individual effects before performing them.
"""
from dataclasses import dataclass, replace
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit
import uuid
from contextlib import contextmanager

from .nas_platform_installation import SavedNasInstallationContext, canonical, _client_directory, _sha, STORE_ROOT
from .release_manifest import ReleaseDescriptorV1


def _digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _path(value):
    path=Path(value)
    if not path.is_absolute() or '..' in path.parts or str(path)!=str(value):
        raise ValueError('exact absolute private client root required')
    return path


def _origin(value):
    if type(value) is not str: raise ValueError('canonical Tailscale HTTPS origin required')
    parsed=urlsplit(value)
    if (parsed.scheme!='https' or parsed.netloc!=parsed.hostname or parsed.path or parsed.query or parsed.fragment
            or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.ts\.net', parsed.hostname or '')
            or value!='https://'+parsed.hostname):
        raise ValueError('canonical Tailscale HTTPS origin required')
    return parsed.hostname.split('.',1)


def _read(root, name):
    parent=_client_directory(root)
    try: fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
    finally: os.close(parent)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600
                or info.st_nlink!=1): raise ValueError('unsafe transition record')
        data=source.read(4*1024*1024+1)
    if len(data)>4*1024*1024: raise ValueError('transition record limit')
    result=json.loads(data)
    if canonical(result)!=data: raise ValueError('noncanonical transition record')
    return result


def _write(root, name, value):
    parent=_client_directory(root); temporary='.origin-'+uuid.uuid4().hex
    try:
        try:
            info=os.stat(name,dir_fd=parent,follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600
                    or info.st_nlink!=1): raise ValueError('unsafe transition record')
        except FileNotFoundError: pass
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=parent)
        with os.fdopen(fd,'wb') as output:
            output.write(canonical(value)); output.flush(); os.fsync(output.fileno())
        os.replace(temporary,name,src_dir_fd=parent,dst_dir_fd=parent); os.fsync(parent)
    finally:
        try: os.unlink(temporary,dir_fd=parent)
        except FileNotFoundError: pass
        os.close(parent)


def _binding(root):
    try: value=_read(root,'origin-binding.json')
    except FileNotFoundError: return None
    if (type(value) is not dict or set(value)!={'schemaVersion','operationId','planDigest','clientRoot','contextDigest','publicOrigin','phase','installedMarkerDigest'}
            or type(value['schemaVersion']) is not int or value['schemaVersion']!=1
            or value['phase'] not in ('pending','complete')): raise ValueError('invalid origin binding')
    _path(value['clientRoot']); _origin(value['publicOrigin']); _operation(value['operationId'])
    for key in ('planDigest','contextDigest','installedMarkerDigest'): _sha(value[key])
    return value


def _operation(value):
    if type(value) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}',value):
        raise ValueError('original transition operation required')


@dataclass(frozen=True)
class NasOriginTransitionPlan:
    """Canonical bytes ensure callers cannot mutate nested approved inputs."""
    payload: bytes

    def __post_init__(self):
        value=json.loads(self.payload)
        names={'schemaVersion','direction','fromOrigin','toOrigin','sourceProfileDigest','destinationProfileDigest',
            'installedMarkerDigest','clientRoot','destinationClientRoot','previousBindingDigest','target',
            'sourceContext','nextContext','currentPlatformDigest','currentConfigDigest','nextPlatformDescriptor',
            'nextPlatformDescriptorDigest','nextConfigDigest','apps'}
        if (type(value) is not dict or set(value)!=names or type(value['schemaVersion']) is not int
                or value['schemaVersion']!=1 or canonical(value)!=self.payload): raise ValueError('invalid origin transition plan')
        for key in ('sourceProfileDigest','destinationProfileDigest','installedMarkerDigest','currentPlatformDigest',
                    'currentConfigDigest','nextPlatformDescriptorDigest','nextConfigDigest'):
            _sha(value[key])
        _sha(value['previousBindingDigest'],optional=True)
        _path(value['clientRoot']); _path(value['destinationClientRoot'])
        old=SavedNasInstallationContext.from_dict(value['sourceContext'])
        new=SavedNasInstallationContext.from_dict(value['nextContext'])
        oldname,suffix=_origin(value['fromOrigin']); newname,newsuffix=_origin(value['toOrigin'])
        handoff=old.hostname_handoff
        direction=('forward' if (oldname,newname)==(handoff.temporary_nas_name,handoff.production_name)
                   else 'reverse' if (oldname,newname)==(handoff.production_name,handoff.temporary_nas_name) else None)
        if direction is None or direction!=value['direction'] or suffix!=newsuffix or old.public_origin!=value['fromOrigin']:
            raise ValueError('origin transition must use the recorded NAS handoff pair')
        descriptor=ReleaseDescriptorV1.from_dict(value['nextPlatformDescriptor'])
        if descriptor.app_id!='local-web-platform' or descriptor.digest!=value['nextPlatformDescriptorDigest']:
            raise ValueError('next platform identity changed')
        expected=replace(old,public_origin=value['toOrigin'],platform_releases=tuple(sorted({**dict(old.platform_releases),
            descriptor.digest:value['nextConfigDigest']}.items())))
        if new!=expected or dict(old.platform_releases).get(value['currentPlatformDigest'])!=value['currentConfigDigest']:
            raise ValueError('origin context provenance changed')
        target=value['target']
        if (type(target) is not dict or set(target)!={'adapterCommand','storeRoot','networkSubnet','proxyAddress'}
                or target['storeRoot']!=STORE_ROOT or target['networkSubnet']!=old.network_subnet or target['proxyAddress']!=old.proxy_address
                or type(target['adapterCommand']) is not list or not target['adapterCommand']
                or any(type(part) is not str or not part or '\x00' in part for part in target['adapterCommand'])):
            raise ValueError('origin target changed')
        if type(value['apps']) is not dict: raise ValueError('exact app observations required')
        observed={app:ReleaseDescriptorV1.from_dict(entry) for app,entry in value['apps'].items()}
        if any(app!=entry.app_id or app=='local-web-platform' for app,entry in observed.items()):
            raise ValueError('app observation identity changed')
        old.observed_runtime_specs(observed)

    @classmethod
    def from_dict(cls,value): return cls(canonical(value))
    def to_dict(self): return json.loads(self.payload)
    @property
    def digest(self): return hashlib.sha256(self.payload).hexdigest()
    @property
    def direction(self): return self.to_dict()['direction']
    @property
    def next_context(self): return SavedNasInstallationContext.from_dict(self.to_dict()['nextContext'])


def preview_nas_origin_transition(*,source_registry,destination_registry,source_context,prepared,app_descriptors,
        current_platform_digest,current_config_digest,source_profile_digest,destination_profile_digest,
        installed_marker_digest,client_root,destination_client_root):
    source_context.validate_target(source_registry)
    if (source_registry.target.writes_enabled or destination_registry.target.writes_enabled
            or source_registry.ingress_mode!='tailscale-serve'
            or replace(source_registry,host=destination_registry.host,public_origin=destination_registry.public_origin)!=destination_registry
            or destination_registry.host!=urlsplit(destination_registry.public_origin).hostname):
        raise ValueError('origin transition changes only origin with writers fenced')
    if set(app_descriptors)!={app.id for app in source_registry.apps}: raise ValueError('complete exact app observations required')
    settings=json.loads(next(entry.content for entry in prepared.files if entry.path=='runtime-settings.json'))
    if (settings['publicOrigin']!=destination_registry.public_origin or settings['networkSubnet']!=source_context.network_subnet
            or settings['proxyAddress']!=source_context.proxy_address or set(settings['apps'])!=set(app_descriptors)):
        raise ValueError('prepared origin configuration changed')
    compose=json.loads(next(entry.content for entry in prepared.files if entry.path=='compose.yaml'))
    if {name:compose['services'][name]['image'] for name in ('caddy','tailscale')}!={'caddy':source_context.caddy_image,'tailscale':source_context.tailscale_image}:
        raise ValueError('origin transition frontdoor images changed')
    for entry in prepared.files:
        matches=[f for f in prepared.descriptor.files if f.path==entry.path]
        if len(matches)!=1 or matches[0].sha256!=hashlib.sha256(entry.content).hexdigest() or matches[0].size!=len(entry.content) or matches[0].mode!=entry.mode:
            raise ValueError('prepared platform bytes changed')
    if len(prepared.files)!=len(prepared.descriptor.files): raise ValueError('prepared platform inventory changed')
    next_context=replace(source_context,public_origin=destination_registry.public_origin,
        platform_releases=tuple(sorted({**dict(source_context.platform_releases),prepared.descriptor.digest:prepared.source_config_digest}.items())))
    oldname,_=_origin(source_registry.public_origin)
    binding=None
    if Path(destination_client_root)!=Path(client_root):
        binding=_binding(destination_client_root)
        if binding is not None and (binding['phase']!='complete' or binding['clientRoot']!=str(client_root)
                or binding['publicOrigin']!=destination_registry.public_origin or binding['installedMarkerDigest']!=installed_marker_digest):
            raise ValueError('foreign or pending destination namespace')
    return NasOriginTransitionPlan.from_dict(dict(schemaVersion=1,direction='forward' if oldname==source_context.hostname_handoff.temporary_nas_name else 'reverse',
        fromOrigin=source_registry.public_origin,toOrigin=destination_registry.public_origin,sourceProfileDigest=source_profile_digest,
        destinationProfileDigest=destination_profile_digest,installedMarkerDigest=installed_marker_digest,clientRoot=str(client_root),
        destinationClientRoot=str(destination_client_root),previousBindingDigest=_digest(binding) if binding else None,
        target=dict(adapterCommand=list(source_registry.target.adapter_command),storeRoot=str(source_registry.runtime_root),
            networkSubnet=source_context.network_subnet,proxyAddress=source_context.proxy_address),
        sourceContext=source_context.to_dict(),nextContext=next_context.to_dict(),currentPlatformDigest=current_platform_digest,
        currentConfigDigest=current_config_digest,nextPlatformDescriptor=prepared.descriptor.to_dict(),
        nextPlatformDescriptorDigest=prepared.descriptor.digest,nextConfigDigest=prepared.source_config_digest,
        apps={app:descriptor.to_dict() for app,descriptor in sorted(app_descriptors.items())}))


class NasOriginTransitionSession:
    """Original-only receipt. No callback is retried after its durable intent."""
    def __init__(self,client_root): self.root=_path(client_root)

    @contextmanager
    def lock(self):
        parent=_client_directory(self.root)
        try: fd=os.open('origin-transition.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=parent)
        finally: os.close(parent)
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1:
                raise ValueError('unsafe origin transition lock')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); yield
        finally: os.close(fd)

    def _load(self,operation):
        _operation(operation); record=_read(self.root,'origin-transition-'+operation+'.json')
        if (type(record) is not dict or set(record)!={'schemaVersion','operationId','plan','phase'}
                or type(record['schemaVersion']) is not int or record['schemaVersion']!=1 or record['operationId']!=operation
                or record['phase'] not in ('prepared','remote-intent','complete')): raise ValueError('invalid original transition receipt')
        plan=NasOriginTransitionPlan.from_dict(record['plan'])
        if plan.to_dict()['clientRoot']!=str(self.root): raise ValueError('original transition root changed')
        return record,plan

    def _begin(self,plan,operation_id,approved_digest):
        _operation(operation_id)
        if approved_digest!=plan.digest or plan.to_dict()['clientRoot']!=str(self.root): raise ValueError('exact approved origin plan required')
        try:
            record,existing=self._load(operation_id)
            if existing!=plan: raise ValueError('original transition plan changed')
            return record
        except FileNotFoundError: pass
        try: pending=_read(self.root,'origin-transition-pending.json')
        except FileNotFoundError: pending=None
        if pending is not None and pending!={'operationId':operation_id,'planDigest':plan.digest}: raise ValueError('original pending transition requires status')
        if canonical(SavedNasInstallationContext.load(self.root).to_dict())!=canonical(plan.to_dict()['sourceContext']):
            raise ValueError('source context changed')
        record=dict(schemaVersion=1,operationId=operation_id,plan=plan.to_dict(),phase='prepared')
        _write(self.root,'origin-transition-pending.json',dict(operationId=operation_id,planDigest=plan.digest))
        _write(self.root,'origin-transition-'+operation_id+'.json',record)
        return record

    def begin(self,plan,*,operation_id,approved_digest):
        with self.lock(): return self._begin(plan,operation_id,approved_digest)

    def apply(self,plan,*,operation_id,approved_digest,transition,validate_authority):
        with self.lock():
            record=self._begin(plan,operation_id,approved_digest)
            if record['phase']!='prepared': raise ValueError('original transition requires status; never replay activation')
            if canonical(SavedNasInstallationContext.load(self.root).to_dict())!=canonical(plan.to_dict()['sourceContext']):
                raise ValueError('source context changed')
            _check_destination(plan)
            validate_authority()
            record['phase']='remote-intent'; _write(self.root,'origin-transition-'+operation_id+'.json',record)
            return transition(plan,operation_id)

    def status(self,operation_id,*,observe):
        with self.lock():
            _,plan=self._load(operation_id)
            return observe(plan,operation_id)

    def promote(self,operation_id,observation):
        with self.lock():
            record,plan=self._load(operation_id); value=plan.to_dict()
            expected=dict(state='origin-ready',operationId=operation_id,planDigest=plan.digest,publicOrigin=value['toOrigin'],
                platformReleaseDigest=value['nextPlatformDescriptorDigest'],sourceConfigDigest=value['nextConfigDigest'],
                apps={app:ReleaseDescriptorV1.from_dict(desc).digest for app,desc in value['apps'].items()},healthy=True)
            if canonical(observation)!=canonical(expected): raise ValueError('exact healthy original origin observation required')
            current=SavedNasInstallationContext.load(self.root).to_dict()
            if record['phase']=='complete':
                if not _extends_context(plan.next_context,SavedNasInstallationContext.from_dict(current)):
                    raise ValueError('completed original context no longer current')
                self._retire_pending(operation_id,plan)
                return SavedNasInstallationContext.from_dict(current)
            if canonical(current) not in (canonical(value['sourceContext']),canonical(value['nextContext'])): raise ValueError('original context changed during promotion')
            try: pending=_read(self.root,'origin-transition-pending.json')
            except FileNotFoundError: pending=None
            if pending!={'operationId':operation_id,'planDigest':plan.digest}: raise ValueError('original pending transition required')
            destination=_path(value['destinationClientRoot'])
            binding=dict(schemaVersion=1,operationId=operation_id,planDigest=plan.digest,clientRoot=str(self.root),
                contextDigest=_digest(value['nextContext']),publicOrigin=value['toOrigin'],phase='pending',installedMarkerDigest=value['installedMarkerDigest'])
            if destination!=self.root:
                try: os.mkdir(destination,0o700)
                except FileExistsError: pass
                with NasOriginTransitionSession(destination).lock():
                    old=_binding(destination)
                    accepted=(old==binding or old==dict(binding,phase='complete') or _digest(old)==value['previousBindingDigest'])
                    if old is None: accepted=value['previousBindingDigest'] is None
                    parent=_client_directory(destination)
                    try: entries=set(os.listdir(parent))
                    finally: os.close(parent)
                    if not accepted or entries-{'origin-binding.json','origin-transition.lock'}: raise ValueError('destination namespace collision')
                    if old!=dict(binding,phase='complete'): _write(destination,'origin-binding.json',binding)
            current=SavedNasInstallationContext.load(self.root).to_dict()
            if canonical(current) not in (canonical(value['sourceContext']),canonical(value['nextContext'])): raise ValueError('original context changed during promotion')
            plan.next_context.save(self.root)
            if destination!=self.root: _write(destination,'origin-binding.json',dict(binding,phase='complete'))
            record['phase']='complete'; _write(self.root,'origin-transition-'+operation_id+'.json',record)
            self._retire_pending(operation_id,plan)
            return plan.next_context

    def _retire_pending(self,operation_id,plan):
        # Retain originals; only retire this exact pending selector.
        try: pending=_read(self.root,'origin-transition-pending.json')
        except FileNotFoundError: pending=None
        if pending=={'operationId':operation_id,'planDigest':plan.digest}:
            parent=_client_directory(self.root)
            try: os.unlink('origin-transition-pending.json',dir_fd=parent); os.fsync(parent)
            finally: os.close(parent)


def resolve_nas_origin_client_root(registry,namespace_root):
    """Resolve one completed direct binding; chains and stale contexts refuse."""
    namespace=_path(namespace_root)
    binding=_binding(namespace)
    if binding is None:
        _require_no_pending(namespace)
        return namespace
    historical=_path(binding['clientRoot'])
    if binding['phase']!='complete' or historical==namespace or binding['publicOrigin']!=registry.public_origin:
        raise ValueError('origin namespace requires original transition recovery')
    if _binding(historical) is not None: raise ValueError('origin namespace chains refused')
    _require_no_pending(historical)
    context=SavedNasInstallationContext.load(historical); context.validate_target(registry)
    record,plan=NasOriginTransitionSession(historical)._load(binding['operationId'])
    if (record['phase']!='complete' or plan.digest!=binding['planDigest']
            or _digest(plan.next_context.to_dict())!=binding['contextDigest']
            or plan.to_dict()['destinationClientRoot']!=str(namespace)
            or plan.to_dict()['installedMarkerDigest']!=binding['installedMarkerDigest']
            or not _extends_context(plan.next_context,context)):
        raise ValueError('origin namespace context changed')
    return historical


def _extends_context(baseline,current):
    """Only ordinary with_applied metadata additions may follow promotion."""
    if replace(current,known_runtime_versions=baseline.known_runtime_versions,
               platform_releases=baseline.platform_releases)!=baseline: return False
    versions={(entry.descriptor.app_id,entry.descriptor.digest):entry for entry in current.known_runtime_versions}
    return (all(versions.get((entry.descriptor.app_id,entry.descriptor.digest))==entry for entry in baseline.known_runtime_versions)
        and all(dict(current.platform_releases).get(key)==value for key,value in baseline.platform_releases))


def _require_no_pending(root):
    try: _read(root,'origin-transition-pending.json')
    except FileNotFoundError: return
    raise ValueError('original pending origin transition requires status')


def _check_destination(plan):
    value=plan.to_dict(); destination=_path(value['destinationClientRoot'])
    if destination==Path(value['clientRoot']): return
    try: parent=_client_directory(destination)
    except FileNotFoundError:
        if value['previousBindingDigest'] is not None: raise ValueError('reviewed destination binding missing')
        return
    try: entries=set(os.listdir(parent))
    finally: os.close(parent)
    binding=_binding(destination)
    if (entries-{'origin-binding.json','origin-transition.lock'}
            or (None if binding is None else _digest(binding))!=value['previousBindingDigest']):
        raise ValueError('destination namespace changed since review')
