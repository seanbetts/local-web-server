"""Private Mac recovery artifacts for one NAS client, never a restore or NAS caller.

The existing host backup document stays byte-for-byte compatible. Only fixed
client metadata and descriptor-bound pending upload bytes enter this separate tar.
"""
from __future__ import annotations

from contextlib import ExitStack
from functools import wraps
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile
from types import SimpleNamespace

from . import host_profile_backup as host_backup
from .host_profile_backup import build_host_backup, parse_host_backup
from .config import load_registry_bytes
from .host_profile_store import parse_revision
from .nas_lifecycle import NasOperationStore, nas_client_root
from .nas_platform_installation import SavedNasInstallationContext, InstallationApproval, canonical
from .production_build import verify_docker_archive, DependencyKey, validate_tar_headers
from .release_manifest import ReleaseDescriptorV1, snapshot_files

MAX_PAYLOAD_BYTES = 2 * 1024**3
MAX_FILES = 100_000
MAX_MANIFEST_BYTES = 16 * 1024**2
MAX_METADATA_BYTES = 2 * 1024**2
_SCHEMA = 'local-web-nas-client-backup/v1'
_APP = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
_OPERATION = r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}'
_SHA = re.compile(r'[a-f0-9]{64}\Z')


class NasClientBackupError(ValueError):
    """Bounded error that does not expose profile, credentials or local paths."""


def _bounded(function):
    @wraps(function)
    def run(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError, RecursionError, tarfile.TarError):
            raise NasClientBackupError('NAS client recovery backup is invalid, busy or changed; preserve original state') from None
    return run


def _path(value):
    if type(value) is not str or '\\' in value or '\0' in value:
        raise ValueError
    path = PurePosixPath(value)
    if path.is_absolute() or str(path) != value or any(p in ('..', '.') for p in path.parts) or not path.parts:
        raise ValueError
    return value


def _absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts or str(path) != str(value):
        raise ValueError
    return path


def _open(root, relative):
    parts = PurePosixPath(_path(relative)).parts
    parent = host_backup._open_directory(root)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent); parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd); raise ValueError
    return fd


def _signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, stat.S_IMODE(info.st_mode)


def _read(root, relative, limit=MAX_METADATA_BYTES, *, private=True):
    with os.fdopen(_open(root, relative), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > limit or private and stat.S_IMODE(before.st_mode) != 0o600:
            raise ValueError
        data = stream.read(limit + 1)
        if len(data) > limit or _signature(before) != _signature(os.fstat(stream.fileno())):
            raise ValueError
        return data


def _receipt(raw, app, *, completed=False):
    """Use the maintained receipt parser on a fresh private validation view."""
    value = json.loads(raw)
    if canonical(value) != raw:
        raise ValueError
    result = value.pop('result', None) if completed else None
    if completed and (type(result) is not dict or set(result) != {'state', 'current'}
                      or result['state'] not in ('active', 'rolled-back') or not _SHA.fullmatch(result['current'])):
        raise ValueError
    with tempfile.TemporaryDirectory(prefix='local-web-receipt-check-') as text:
        directory = Path(text).resolve()
        path = directory/'nas-operation.json'; path.write_bytes(canonical(value)); path.chmod(0o600)
        fd = host_backup._open_directory(directory)
        try:
            reader = NasOperationStore(directory, app)
            # Its ordinary lock creates directories. This reader is already in a
            # private validation view and receives only the pinned app directory.
            reader._access = SimpleNamespace(app_root=fd)
            parsed = reader.pending()
        finally:
            os.close(fd)
    if completed:
        if parsed['operationId'] is None: raise ValueError
        parsed['result'] = result
    return parsed


def _context(root, relative, registry):
    raw = _read(root, relative)
    result = SavedNasInstallationContext.from_dict(json.loads(raw))
    if canonical(result.to_dict()) != raw: raise ValueError
    result.validate_target(registry)
    return result


def _inventory(root):
    """Read fixed receipt names only; never walk caches, logs or old code."""
    apps = root/'apps'
    if not apps.exists(): return ()
    fd = host_backup._open_directory(apps)
    try:
        result = []
        for app in sorted(os.listdir(fd)):
            if not _APP.fullmatch(app): raise ValueError
            child = os.open(app, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                names = tuple(sorted(name for name in os.listdir(child)
                    if name == 'nas-operation.json' or re.fullmatch('nas-' + _OPERATION + r'\.json', name)))
                result.append((app, names))
            finally: os.close(child)
        if len(result) > 1024: raise ValueError
        return tuple(result)
    finally: os.close(fd)


def _source_relative(value, original):
    path = _absolute(value)
    relative = path.relative_to(original).as_posix()
    return _path(relative)


def _release_files(root, prepared, descriptor, selected):
    if sum(entry.size for entry in descriptor.files) > MAX_PAYLOAD_BYTES: raise ValueError
    directory = host_backup._open_directory(root/prepared)
    try:
        if snapshot_files(root/prepared) != descriptor.files: raise ValueError
        host_backup._verify_parent(root/prepared, directory)
    finally: os.close(directory)
    selected.update(prepared + '/' + entry.path for entry in descriptor.files)


def _cache_metadata(root, metadata, descriptor):
    saved = json.loads(_read(root, metadata, 65536, private=False))
    if type(saved) is not dict or set(saved) != {'key', 'sha256'}: raise ValueError
    raw = saved['key']
    names = {'lockfileDigest','imageDigest','os','architecture','abi','recipeVersion','recipeDigest','contextContractVersion'}
    if type(raw) is not dict or set(raw) != names or type(raw['contextContractVersion']) is not int or raw['contextContractVersion'] != 1: raise ValueError
    key = DependencyKey(raw['lockfileDigest'],raw['imageDigest'],raw['os'],raw['architecture'],raw['abi'],raw['recipeVersion'],raw['recipeDigest'])
    key.require_target()
    if (key.to_dict() != raw or key.digest != PurePosixPath(metadata).parent.name
            or 'sha256:' + saved['sha256'] != descriptor.runtime.dependency_digest
            or any(getattr(key,name) != getattr(descriptor.runtime,name) for name in ('lockfile_digest','os','architecture','abi','recipe_version'))): raise ValueError
    return saved


def _runtime(root, archive, descriptor, selected):
    if not re.fullmatch(r'cache/linux/[a-f0-9]{64}/artifact\.tar', archive): raise ValueError
    metadata = str(PurePosixPath(archive).parent/'metadata.json')
    _cache_metadata(root, metadata, descriptor)
    fd = _open(root, archive)
    try:
        if os.fstat(fd).st_size > MAX_PAYLOAD_BYTES: raise ValueError
    finally: os.close(fd)
    identity = verify_docker_archive(root/archive, maximum_layer_bytes=MAX_PAYLOAD_BYTES, maximum_total_layer_bytes=MAX_PAYLOAD_BYTES)
    if ('sha256:' + identity.archive_digest != descriptor.runtime.dependency_digest or identity.image_digest != descriptor.runtime.image_digest
            or descriptor.runtime.manifest_digest is not None and identity.manifest_digest != descriptor.runtime.manifest_digest): raise ValueError
    selected.update((metadata, archive))


def _find_selected(root, descriptor, selected):
    app_root = 'prepared/' + descriptor.app_id
    fd = host_backup._open_directory(root/app_root)
    try: names = sorted(os.listdir(fd))
    finally: os.close(fd)
    if len(names) > 1024: raise ValueError
    prepared = None
    for operation in names:
        if not re.fullmatch(_OPERATION, operation): continue
        candidate = app_root + '/' + operation
        raw = _read(root, candidate + '/descriptor.json', private=False)
        if raw != descriptor.canonical_bytes(): continue
        prepared = candidate; break
    if prepared is None: raise ValueError
    selected.add(prepared + '/descriptor.json')
    _release_files(root, prepared + '/files', descriptor, selected)
    archive = None
    if descriptor.runtime is not None:
        directory = host_backup._open_directory(root/'cache/linux')
        try: names = sorted(os.listdir(directory))
        finally: os.close(directory)
        if len(names) > 4096: raise ValueError
        for key in names:
            if not _SHA.fullmatch(key): continue
            metadata = 'cache/linux/' + key + '/metadata.json'
            saved = json.loads(_read(root, metadata, 65536, private=False))
            if type(saved) is dict and set(saved) == {'key','sha256'} and 'sha256:' + saved['sha256'] == descriptor.runtime.dependency_digest:
                archive = 'cache/linux/' + key + '/artifact.tar'; break
        if archive is None: raise ValueError
        _runtime(root, archive, descriptor, selected)
    return dict(appId=descriptor.app_id, releaseDigest=descriptor.digest, preparedRoot=prepared, runtimeArchive=archive)


def _origin_metadata(root, original):
    """Select reviewed namespace copies without following archived absolute paths."""
    from .nas_origin_transition import NasOriginTransitionPlan
    directory=host_backup._open_directory(root)
    try: names=set(os.listdir(directory))
    finally: os.close(directory)
    receipts=sorted(name for name in names if re.fullmatch('origin-transition-'+_OPERATION+r'\.json',name)
                    and name!='origin-transition-pending.json')
    if len(receipts)>128: raise ValueError
    selected={}; plans={}; destinations={}
    for name in receipts:
        raw=_read(root,name); record=json.loads(raw)
        if (canonical(record)!=raw or set(record)!={'schemaVersion','operationId','plan','phase'}
                or type(record['schemaVersion']) is not int or record['schemaVersion']!=1
                or record['phase'] not in ('prepared','remote-intent','complete')
                or name!='origin-transition-'+record['operationId']+'.json'): raise ValueError
        plan=NasOriginTransitionPlan.from_dict(record['plan']); value=plan.to_dict()
        if value['clientRoot']!=str(original): raise ValueError
        plans[record['operationId']]=(plan,record['phase'])
        selected[name]=(root,name)
        destination=_absolute(value['destinationClientRoot'])
        if destination!=original: destinations[destination]=destinations.get(destination,False) or record['phase']=='complete'
    if 'origin-transition-pending.json' in names:
        raw=_read(root,'origin-transition-pending.json'); pending=json.loads(raw)
        if (canonical(pending)!=raw or set(pending)!={'operationId','planDigest'}
                or pending['operationId'] not in plans or pending['planDigest']!=plans[pending['operationId']][0].digest): raise ValueError
        selected['origin-transition-pending.json']=(root,'origin-transition-pending.json')
    if selected:
        if _read(root,'origin-transition.lock')!=b'': raise ValueError
        selected['origin-transition.lock']=(root,'origin-transition.lock')
    elif 'origin-transition.lock' in names:
        if _read(root,'origin-transition.lock')!=b'': raise ValueError
        selected['origin-transition.lock']=(root,'origin-transition.lock')
    for destination in sorted(destinations):
        key=hashlib.sha256(str(destination).encode()).hexdigest()
        relative='origin-namespaces/'+key
        source=destination if root==original else root/relative
        try: raw=_read(source,'origin-binding.json')
        except FileNotFoundError:
            if destinations[destination]: raise ValueError
            continue
        binding=json.loads(raw)
        if (canonical(binding)!=raw or type(binding) is not dict
                or set(binding)!={'schemaVersion','operationId','planDigest','clientRoot','contextDigest','publicOrigin','phase','installedMarkerDigest'}
                or type(binding['schemaVersion']) is not int or binding['schemaVersion']!=1
                or binding['operationId'] not in plans or binding['clientRoot']!=str(original)
                or binding['phase'] not in ('pending','complete')): raise ValueError
        plan,phase=plans[binding['operationId']]; value=plan.to_dict()
        if (value['destinationClientRoot']!=str(destination) or plan.digest!=binding['planDigest']
                or hashlib.sha256(canonical(value['nextContext'])).hexdigest()!=binding['contextDigest']
                or value['toOrigin']!=binding['publicOrigin'] or value['installedMarkerDigest']!=binding['installedMarkerDigest']
                or binding['phase']=='pending' and phase=='complete'): raise ValueError
        if _read(source,'origin-transition.lock')!=b'': raise ValueError
        for filename in ('origin-binding.json','origin-transition.lock'):
            selected[relative+'/'+filename]=(source,filename)
    return selected


def _plan(root, original, registry, profile_digests):
    _context(root, 'installation-context.json', registry)
    origin_sources=_origin_metadata(root,original)
    selected = {'installation-context.json',*origin_sources}
    pending = []
    if 'origin-transition-pending.json' in origin_sources:
        selector=json.loads(_read(root,'origin-transition-pending.json'))
        pending.append(('origin-transition',selector['operationId']))
    preparations = []
    for app, names in _inventory(root):
        for name in names:
            relative = f'apps/{app}/{name}'
            raw = _read(root, relative)
            receipt = _receipt(raw, app, completed=name != 'nas-operation.json')
            if app!='local-web-platform' and receipt['profileAuthority'] not in profile_digests: raise ValueError
            if name != 'nas-operation.json' and name != 'nas-' + receipt['operationId'] + '.json': raise ValueError
            selected.add(relative)
            if name != 'nas-operation.json': continue
            pending.append((app, receipt['operationId']))
            descriptor = ReleaseDescriptorV1.from_dict(receipt['descriptor'])
            if receipt['action'] == 'rollback':
                if receipt['preparedRoot'] is not None or receipt['runtimeArchive'] is not None: raise ValueError
                continue
            operation = receipt['operationId']
            prepared = _source_relative(receipt['preparedRoot'], original)
            if app == 'local-web-platform':
                expected = f'apps/{app}/installation-{operation}/files'
                if prepared != expected or receipt['runtimeArchive'] is not None: raise ValueError
                capsule = str(PurePosixPath(prepared).parent/'installation-approval.json')
                raw = _read(root, capsule)
                approval = InstallationApproval.from_dict(json.loads(raw))
                if (canonical(approval.to_dict()) != raw or approval.platform_release_digest != descriptor.digest
                        or approval.expected_current != receipt['expectedCurrent'] or approval.profile_revision not in profile_digests):
                    raise ValueError
                selected.add(capsule)
                context = str(PurePosixPath(prepared).parent/'installation-context.json')
                saved_context = _context(root, context, registry)
                if dict(saved_context.platform_releases).get(descriptor.digest) != approval.source_config_digest: raise ValueError
                selected.add(context)
                if approval.selected_descriptor is not None:
                    chosen = approval.selected_descriptor
                    if chosen.runtime is not None and not any(version.descriptor == chosen for version in saved_context.known_runtime_versions): raise ValueError
                    preparations.append(_find_selected(root, chosen, selected))
            else:
                expected = f'prepared/{app}/{operation}'
                if prepared != expected: raise ValueError
                metadata = prepared + '/descriptor.json'
                if _read(root, metadata, private=False) != descriptor.canonical_bytes(): raise ValueError
                selected.add(metadata); prepared += '/files'
            _release_files(root, prepared, descriptor, selected)
            if descriptor.runtime is not None:
                _runtime(root, _source_relative(receipt['runtimeArchive'], original), descriptor, selected)
            elif receipt['runtimeArchive'] is not None: raise ValueError
    if len(selected) > MAX_FILES: raise ValueError
    return tuple(sorted(selected)), tuple(pending), preparations, origin_sources


def _copy(root, relative, destination, *, expected_size=None):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with os.fdopen(_open(root, relative), 'rb') as source, destination.open('xb') as target:
        before = os.fstat(source.fileno())
        if expected_size is not None and before.st_size != expected_size: raise ValueError
        remaining = before.st_size
        while remaining:
            chunk = source.read(min(remaining, 1024 * 1024))
            if not chunk: raise ValueError
            target.write(chunk); remaining -= len(chunk)
        if source.read(1): raise ValueError
        if _signature(before) != _signature(os.fstat(source.fileno())): raise ValueError
        destination.chmod(stat.S_IMODE(before.st_mode))
        return _signature(before)


def _host(path):
    backup = parse_host_backup(path)
    registry = load_registry_bytes(backup.profile_bytes)
    if registry.target.kind != 'nas-docker': raise ValueError
    digests = {parse_revision(record.content).current_registry_sha256 for record in backup.revisions}
    return backup, registry, digests


def _verify_tree(directory, manifest):
    backup, registry, digests = _host(directory/'host-backup.json')
    if hashlib.sha256(backup.profile_bytes).hexdigest() != manifest['profileSha256'] or backup.created_at != manifest['createdAt']: raise ValueError
    selected, pending, preparations, origin_sources = _plan(directory/'client', _absolute(manifest['sourceClientRoot']), registry, digests)
    if preparations != manifest['selectedPreparations']: raise ValueError
    expected = {'host-backup.json', *('client/' + name for name in selected)}
    if expected != {row['path'] for row in manifest['files']}: raise ValueError
    return dict(verified=True, profileSha256=manifest['profileSha256'], pendingOperations=len(pending), files=len(expected))


@_bounded
def verify_nas_client_backup(artifact: Path) -> dict:
    """Verify and materialize only into a fresh disposable private directory."""
    artifact = _absolute(artifact)
    with host_backup._parent(artifact) as (parent, path):
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, 'rb') as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_size > MAX_PAYLOAD_BYTES + 64 * 1024**2): raise ValueError
            validate_tar_headers(source, info.st_size, maximum_members=MAX_FILES + 1)
            with tempfile.TemporaryDirectory(prefix='local-web-client-verify-') as text:
                root = Path(text).resolve()
                with tarfile.open(fileobj=source, mode='r:') as bundle:
                    first = bundle.next()
                    if first is None or first.name != 'manifest.json' or not first.isfile() or first.mode != 0o600 or first.pax_headers or first.size > MAX_MANIFEST_BYTES: raise ValueError
                    raw = bundle.extractfile(first).read(MAX_MANIFEST_BYTES + 1); manifest = json.loads(raw)
                    if (canonical(manifest) != raw or set(manifest) != {'schema','createdAt','profileSha256','sourceClientRoot','files','selectedPreparations'}
                            or manifest['schema'] != _SCHEMA or type(manifest['files']) is not list or len(manifest['files']) > MAX_FILES): raise ValueError
                    rows = {}; total = 0
                    for row in manifest['files']:
                        if (type(row) is not dict or set(row) != {'path','sha256','size','mode'} or not _SHA.fullmatch(row['sha256'])
                                or type(row['size']) is not int or row['size'] < 0 or type(row['mode']) is not int or not 0 <= row['mode'] <= 0o777): raise ValueError
                        name = _path(row['path'])
                        if name in rows or name != 'host-backup.json' and not name.startswith('client/'): raise ValueError
                        rows[name] = row; total += row['size']
                    if total > MAX_PAYLOAD_BYTES: raise ValueError
                    seen = set()
                    for member in iter(bundle.next, None):
                        name = _path(member.name)
                        if (name not in rows or name in seen or not member.isfile() or member.size != rows[name]['size']
                                or member.mode != 0o600 or member.pax_headers): raise ValueError
                        destination = root/name; destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        digest = hashlib.sha256()
                        with bundle.extractfile(member) as input_file, destination.open('xb') as output:
                            while chunk := input_file.read(1024 * 1024): output.write(chunk); digest.update(chunk)
                        destination.chmod(rows[name]['mode'])
                        if digest.hexdigest() != rows[name]['sha256']: raise ValueError
                        seen.add(name)
                    if seen != set(rows): raise ValueError
                result = _verify_tree(root, manifest)
            if _signature(info) != _signature(os.fstat(source.fileno())): raise ValueError
    return result


@_bounded
def write_nas_client_backup(paths, output: Path, *, client_root: Path | None = None, clock=None) -> dict:
    """Publish an explicit new private artifact; never write profile/client state."""
    backup = build_host_backup(paths, clock)
    registry = load_registry_bytes(backup.profile_bytes)
    if registry.target.kind != 'nas-docker': raise ValueError
    root = _absolute(client_root if client_root is not None else nas_client_root(registry))
    output = _absolute(output)
    if output.is_relative_to(root): raise ValueError
    with host_backup._parent(output) as (parent, output), ExitStack() as locks:
        host_backup._validate_output_parent(paths, parent, output)
        if output.name == host_backup._TEMPORARY or host_backup._metadata(parent, output.name) is not None: raise ValueError
        root_fd = host_backup._open_directory(root); locks.callback(os.close, root_fd)
        root_info = os.fstat(root_fd)
        if stat.S_IMODE(root_info.st_mode) != 0o700 or root_info.st_uid != os.getuid(): raise ValueError
        try: origin_lock=_open(root,'origin-transition.lock')
        except FileNotFoundError: origin_lock=None
        if origin_lock is not None:
            locks.callback(os.close,origin_lock); fcntl.flock(origin_lock,fcntl.LOCK_SH|fcntl.LOCK_NB)
        inventory = _inventory(root)
        for app, names in inventory:
            try: fd = _open(root, f'apps/{app}/deploy.lock')
            except FileNotFoundError:
                if 'nas-operation.json' in names: raise ValueError
                continue
            locks.callback(os.close, fd); fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        digests = {parse_revision(record.content).current_registry_sha256 for record in backup.revisions}
        selected, pending, preparations, origin_sources = _plan(root, root, registry, digests)
        for source,filename in set(origin_sources.values()):
            if filename=='origin-transition.lock' and source!=root:
                fd=_open(source,filename); locks.callback(os.close,fd); fcntl.flock(fd,fcntl.LOCK_SH|fcntl.LOCK_NB)
        # Namespace metadata must still match after all selected locks are held.
        if _origin_metadata(root,root)!=origin_sources: raise ValueError
        sizes = {}
        for name in selected:
            source,relative=origin_sources.get(name,(root,name))
            fd = _open(source, relative)
            try: sizes[name] = os.fstat(fd).st_size
            finally: os.close(fd)
        if sum(sizes.values()) + len(backup.document_bytes) > MAX_PAYLOAD_BYTES: raise ValueError
        with tempfile.TemporaryDirectory(prefix='local-web-client-stage-') as text:
            stage = Path(text).resolve(); host_file = stage/'host-backup.json'; host_file.write_bytes(backup.document_bytes); host_file.chmod(0o600)
            signatures = {}
            for name in selected:
                source,relative=origin_sources.get(name,(root,name))
                signatures[name] = _copy(source, relative, stage/'client'/name, expected_size=sizes[name])
            rows = []
            for name in ('host-backup.json', *('client/' + n for n in selected)):
                path = stage/name; digest = hashlib.sha256()
                with path.open('rb') as source:
                    while chunk := source.read(1024 * 1024): digest.update(chunk)
                rows.append(dict(path=name, size=path.stat().st_size, mode=stat.S_IMODE(path.stat().st_mode), sha256=digest.hexdigest()))
            manifest = dict(schema=_SCHEMA, createdAt=backup.created_at, profileSha256=hashlib.sha256(backup.profile_bytes).hexdigest(), sourceClientRoot=str(root), files=rows, selectedPreparations=preparations)
            _verify_tree(stage, manifest)
            latest = build_host_backup(paths, clock)
            if latest.profile != backup.profile or latest.revisions != backup.revisions or _inventory(root) != inventory: raise ValueError
            if _origin_metadata(root,root)!=origin_sources: raise ValueError
            for name, signature in signatures.items():
                source,relative=origin_sources.get(name,(root,name))
                fd = _open(source, relative)
                try:
                    if _signature(os.fstat(fd)) != signature: raise ValueError
                finally: os.close(fd)
            host_backup._verify_parent(root, root_fd)
            fd, provenance = host_backup._open_scratch(parent)
            try:
                os.ftruncate(fd, 0)
                with os.fdopen(fd, 'w+b', closefd=False) as out:
                    with tarfile.open(fileobj=out, mode='w:', format=tarfile.USTAR_FORMAT) as bundle:
                        raw = canonical(manifest)
                        if len(raw) > MAX_MANIFEST_BYTES: raise ValueError
                        import io
                        member = tarfile.TarInfo('manifest.json'); member.size=len(raw); member.mode=0o600; bundle.addfile(member,io.BytesIO(raw))
                        for row in rows:
                            member=tarfile.TarInfo(row['path']); member.size=row['size']; member.mode=0o600
                            with (stage/row['path']).open('rb') as source: bundle.addfile(member,source)
                    out.flush(); os.fsync(fd)
                host_backup._verify_parent(output.parent, parent)
                host_backup._publish_from_fd(fd, parent, output.name); os.fsync(parent)
                result = verify_nas_client_backup(output)
                final = os.open(output.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                try: host_backup._validate_private_metadata(final, provenance)
                finally: os.close(final)
                result.update(path=str(output), bytes=os.fstat(fd).st_size)
                return result
            finally:
                os.close(fd)
