"""Mac client lifecycle for a NAS target, retaining one original operation receipt.

Client recovery records live on the Mac. A NAS runtime root is never interpreted
as a local serving directory, and this module never invokes launchctl.
"""

from contextlib import contextmanager
import json
import hashlib
import os
from pathlib import Path
import re
import stat
import uuid
from dataclasses import dataclass

from .config import registered_host, validate_registered_app
from .deploy import DeploymentResult, RollbackUnavailable
from .git_build import GitRepository
from .health import backend_health_path, frontend_health_path
from .mac_dependency_builder import MacDockerDependencyBuilder
from .nas_deploy import NasDeploymentManager
from .nas_recipe import require_nas_recipe
from .nas_transport import NasTransport
from .nas_workflow import NasReleasePreparer
from .public_origin import join_public_origin
from .release_manifest import ReleaseDescriptorV1
from .runtime import AppLock, RuntimeAccess, RuntimeLayout
from .services import HealthResult, HttpHealthChecker, ServiceState
from .status import AppStatus


_OPERATION = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_LIMIT = 2 * 1024 * 1024
_UNSET = object()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class NasOperationStore:
    """A private local receipt and nonblocking app lock, separate from NAS state."""

    def __init__(self, client_root: Path, app_id: str):
        self.layout = RuntimeLayout(client_root, app_id)
        self._access = None

    @contextmanager
    def lock(self):
        if self._access is not None:
            raise ValueError('NAS operation store is already locked')
        with AppLock(self.layout), RuntimeAccess(self.layout, create=True) as access:
            self._access = access
            try:
                yield self
            finally:
                self._access = None

    def _directory(self):
        if self._access is None:
            raise ValueError('NAS operation store requires its app lock')
        return self._access.app_root

    def pending(self):
        directory = self._directory()
        try:
            descriptor = os.open('nas-operation.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > _LIMIT or info.st_uid != os.getuid():
                raise ValueError('invalid NAS operation receipt')
            content = stream.read(_LIMIT + 1)
        try:
            value = json.loads(content)
            if type(value) is not dict or _canonical(value) != content:
                raise ValueError
            required = {'schemaVersion', 'operationId', 'descriptor', 'expectedCurrent',
                        'preparedRoot', 'runtimeArchive', 'action', 'phase', 'uploadBlobs'}
            if set(value) != required or type(value['schemaVersion']) is not int or value['schemaVersion'] != 1:
                raise ValueError
            release = ReleaseDescriptorV1.from_dict(value['descriptor'])
            if (release.app_id != self.layout.app_id
                    or type(value['operationId']) is not str or not _OPERATION.fullmatch(value['operationId'])
                    or value['action'] not in ('deploy', 'rollback')
                    or value['phase'] not in ('preparing', 'activating')
                    or value['expectedCurrent'] is not None and (
                        type(value['expectedCurrent']) is not str or not _DIGEST.fullmatch(value['expectedCurrent']))):
                raise ValueError
            for name in ('preparedRoot', 'runtimeArchive'):
                if value[name] is not None and (type(value[name]) is not str or not Path(value[name]).is_absolute()):
                    raise ValueError
            if value['action'] == 'deploy' and value['preparedRoot'] is None:
                raise ValueError
            blobs = value['uploadBlobs']
            if blobs is not None and (type(blobs) is not list or any(type(digest) is not str for digest in blobs)
                    or len(set(blobs)) != len(blobs) or not set(blobs) <= {entry.sha256 for entry in release.files}):
                raise ValueError
            return value
        except (ValueError, TypeError, KeyError, UnicodeError, RecursionError) as error:
            raise ValueError('invalid NAS operation receipt; preserve it for recovery') from error

    def begin(self, operation_id, descriptor, *, expected_current, prepared_root, runtime_archive, action):
        existing = self.pending()
        if existing is not None:
            raise ValueError(f"NAS operation {existing['operationId']} requires original-operation recovery")
        if (type(operation_id) is not str or not _OPERATION.fullmatch(operation_id)
                or descriptor.app_id != self.layout.app_id or action not in ('deploy', 'rollback')
                or expected_current is not None and (type(expected_current) is not str or not _DIGEST.fullmatch(expected_current))):
            raise ValueError('invalid NAS operation intent')
        value = dict(schemaVersion=1, operationId=operation_id, descriptor=descriptor.to_dict(),
                     expectedCurrent=expected_current, action=action, phase='preparing', uploadBlobs=None,
                     preparedRoot=str(Path(prepared_root).absolute()) if prepared_root is not None else None,
                     runtimeArchive=str(Path(runtime_archive).absolute()) if runtime_archive is not None else None)
        self._write('nas-operation.json', value)
        self.pending()

    def before_transition(self, operation_id, action):
        receipt = self.pending()
        if receipt is None or receipt['operationId'] != operation_id or action not in ('activate', 'rollback'):
            raise ValueError('transition requires the original NAS operation')
        receipt['phase'] = 'activating'
        self._write('nas-operation.json', receipt)

    def select_upload_blobs(self, operation_id, requested):
        receipt = self.pending()
        if receipt is None or receipt['operationId'] != operation_id:
            raise ValueError('upload requires the original NAS operation')
        descriptor = ReleaseDescriptorV1.from_dict(receipt['descriptor'])
        known = {entry.sha256 for entry in descriptor.files}
        if (type(requested) is not tuple or any(type(digest) is not str for digest in requested)
                or len(set(requested)) != len(requested) or not set(requested) <= known):
            raise ValueError('invalid original upload selection')
        if receipt['uploadBlobs'] is None:
            receipt['uploadBlobs'] = list(requested)
            self._write('nas-operation.json', receipt)
        if not set(requested) <= set(receipt['uploadBlobs']):
            raise ValueError('original upload selection changed; preserve the operation for recovery')
        return tuple(receipt['uploadBlobs'])

    def _write(self, name, value):
        directory = self._directory()
        temporary = '.nas-receipt-' + uuid.uuid4().hex
        content = _canonical(value)
        if len(content) > _LIMIT:
            raise ValueError('NAS operation receipt exceeds its bound')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=directory)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass

    def complete(self, operation_id, *, state, current):
        receipt = self.pending()
        if receipt is None or receipt['operationId'] != operation_id:
            raise ValueError('completion requires the original NAS operation')
        if (state not in ('active', 'rolled-back') or type(current) is not str
                or not _DIGEST.fullmatch(current)):
            raise ValueError('invalid NAS operation completion')
        archive = 'nas-' + operation_id + '.json'
        # The archive is durable before the active fence is removed. A crash in
        # between leaves a conservative original receipt for reconciliation.
        receipt['result'] = dict(state=state, current=current)
        self._write(archive, receipt)
        os.unlink('nas-operation.json', dir_fd=self._directory())
        os.fsync(self._directory())


@dataclass(frozen=True)
class NasLiveRelease:
    descriptor: ReleaseDescriptorV1 | None
    previous: ReleaseDescriptorV1 | None
    observed_healthy: bool | None

    @classmethod
    def parse(cls, app_id, response):
        try:
            if type(response) is not dict or response.get('state') not in ('idle', 'active', 'recovery-required'):
                raise ValueError
            current = response['current']
            descriptor = (ReleaseDescriptorV1.from_dict(response['descriptor'])
                          if response['descriptor'] is not None else None)
            previous = (ReleaseDescriptorV1.from_dict(response['previousDescriptor'])
                        if response.get('previousDescriptor') is not None else None)
            for value, digest in ((descriptor, current), (previous, response['previous'])):
                if value is None:
                    if digest is not None:
                        raise ValueError
                elif value.app_id != app_id or value.digest != digest:
                    raise ValueError
            observed = response.get('observedHealthy')
            if observed is not None and type(observed) is not bool:
                raise ValueError
            if descriptor is None and (previous is not None or observed is True):
                raise ValueError
            return cls(descriptor, previous, observed)
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError('invalid NAS live-release observation') from error


def _observe(transport, app_id):
    return NasLiveRelease.parse(app_id, transport.request('status', operation_id=uuid.uuid4().hex,
                                                        payload=dict(appId=app_id)))


def nas_client_root(registry):
    identity = dict(adapter=registry.target.adapter_command, runtimeRoot=str(registry.runtime_root),
                    publicOrigin=registry.public_origin)
    digest = hashlib.sha256(_canonical(identity)).hexdigest()
    return Path.home()/'Library/Application Support/Local Web/nas'/digest


class NasHostDeploymentManager:
    """Existing app operations using Mac builds and the fixed NAS adapter."""

    def __init__(self, registry, *, client_root=None, preparer=None, transport=None, health=None,
                 repository_factory=GitRepository):
        if registry.target.kind != 'nas-docker':
            raise ValueError('NAS lifecycle requires a NAS target')
        self.registry = registry
        self.client_root = Path(client_root) if client_root is not None else nas_client_root(registry)
        self.transport = transport or NasTransport(registry.target, timeout_seconds=450)
        self.health = health or HttpHealthChecker()
        self.repository_factory = repository_factory

        def install(*arguments):
            if registry.target.build_context is None:
                raise ValueError('a service dependency cache miss requires target.buildContext on the Mac')
            MacDockerDependencyBuilder(registry.target.build_context, self.client_root/'cache/buildkit')(*arguments)

        self.preparer = preparer or NasReleasePreparer(self.client_root/'cache', dependency_installer=install)

    def _publisher(self, store, operation_id, host):
        def public_health(_descriptor):
            # Automatic recovery may have selected previous code. Probe the
            # manifest belonging to that observed release rather than guessing
            # its route/health contract from the failed candidate.
            observed = _observe(self.transport, host.id)
            if observed.descriptor is None or observed.observed_healthy is not True:
                return False
            repository = self.repository_factory(host.repository)
            _, manifest = validate_registered_app(host, repository.manifest_at(observed.descriptor.source_commit))
            paths = [frontend_health_path(manifest)]
            backend = backend_health_path(host, manifest)
            if backend is not None:
                paths.append(backend)
            return all(self.health.check(join_public_origin(self.registry.public_origin, path)).healthy for path in paths)
        return NasDeploymentManager(self.transport, public_health=public_health,
            before_transition=lambda action: store.before_transition(operation_id, action),
            select_upload_blobs=lambda requested: store.select_upload_blobs(operation_id, requested))

    @staticmethod
    def _attempt(app_id, operation_id, operation):
        try:
            return operation()
        except Exception as error:
            raise ValueError(f'NAS operation {operation_id} requires reconciliation; '
                             f'use local-web deploy {app_id} --recover {operation_id}') from error

    @staticmethod
    def _complete(store, result, descriptor, action):
        store.complete(result.operation_id, state=result.state, current=result.current)
        if result.state != 'active' or result.current != descriptor.digest:
            raise ValueError(f'{descriptor.app_id} requested NAS {action} failed health checks; '
                             f'recovered release {result.current}; operation {result.operation_id} is finished')
        outcome = 'deployed' if action == 'deploy' else 'rolled-back'
        return DeploymentResult(descriptor.app_id, descriptor.source_commit, outcome,
            f'{descriptor.app_id} {outcome} on NAS; operation {result.operation_id}', None)

    def deploy(self, app_id, from_hook=False, *, expected_commit=None,
               expected_current_commit=_UNSET, validate_locked=None, **unsupported):
        if unsupported:
            raise ValueError('NAS deployment cannot use a macOS service transition')
        host = registered_host(self.registry, app_id)
        repository = self.repository_factory(host.repository)
        commit = repository.main_commit()
        if from_hook and (not host.auto_deploy or repository.current_branch() != 'main'):
            return DeploymentResult(app_id, commit, 'skipped', f'{app_id} skipped: automatic deployment is disabled or hook is not on main', None)
        store = NasOperationStore(self.client_root, app_id)
        with store.lock():
            pending = store.pending()
            if pending is not None:
                raise ValueError(f"NAS operation {pending['operationId']} requires original-operation recovery")
            if validate_locked is not None:
                validate_locked()
            if expected_commit is not None and commit != expected_commit:
                raise ValueError('deployment target changed')
            host, manifest = validate_registered_app(host, repository.manifest_at(commit))
            recipe = require_nas_recipe(manifest.production, kind=manifest.kind)
            if recipe.rollback_policy != 'compatible':
                raise ValueError('app migration requires its reviewed data recovery procedure')
            observed = _observe(self.transport, app_id)
            current = observed.descriptor
            if current is not None and (current.data_contract != recipe.data_contract
                    or current.rollback_policy != 'compatible'):
                raise ValueError('NAS deployment requires the same compatible data contract')
            if expected_current_commit is not _UNSET and (current.source_commit if current else None) != expected_current_commit:
                raise ValueError('current NAS release changed before deployment')
            operation_id = uuid.uuid4().hex
            prepared = self.preparer.prepare(host, commit=commit,
                destination=self.client_root/'prepared'/app_id/operation_id)
            if prepared.descriptor.source_commit != commit or prepared.descriptor.app_id != app_id:
                raise ValueError('prepared NAS release does not match its exact source')
            store.begin(operation_id, prepared.descriptor, expected_current=current.digest if current else None,
                        prepared_root=prepared.root, runtime_archive=prepared.runtime_archive, action='deploy')
            result = self._attempt(app_id, operation_id, lambda: self._publisher(store, operation_id, host).deploy(prepared.descriptor, prepared.files_root,
                operation_id=operation_id, expected_current=current.digest if current else None,
                runtime_archive=prepared.runtime_archive))
            return self._complete(store, result, prepared.descriptor, 'deploy')

    def rollback(self, app_id):
        host = registered_host(self.registry, app_id)
        store = NasOperationStore(self.client_root, app_id)
        with store.lock():
            pending = store.pending()
            if pending is not None:
                raise ValueError(f"NAS operation {pending['operationId']} requires original-operation recovery")
            observed = _observe(self.transport, app_id)
            current, previous = observed.descriptor, observed.previous
            if current is None or previous is None:
                raise RollbackUnavailable(f'{app_id} has no retained NAS current/previous pair')
            if (current.data_contract != previous.data_contract
                    or current.rollback_policy != 'compatible' or previous.rollback_policy != 'compatible'):
                raise ValueError('NAS code rollback requires the same compatible data contract')
            operation_id = uuid.uuid4().hex
            store.begin(operation_id, previous, expected_current=current.digest,
                        prepared_root=None, runtime_archive=None, action='rollback')
            result = self._attempt(app_id, operation_id, lambda: self._publisher(store, operation_id, host).rollback(previous,
                operation_id=operation_id, expected_current=current.digest))
            return self._complete(store, result, previous, 'rollback')

    def recover(self, app_id, operation_id):
        host = registered_host(self.registry, app_id)
        store = NasOperationStore(self.client_root, app_id)
        with store.lock():
            receipt = store.pending()
            if receipt is None or receipt['operationId'] != operation_id:
                raise ValueError('recovery requires the exact original NAS operation')
            descriptor = ReleaseDescriptorV1.from_dict(receipt['descriptor'])
            result = self._attempt(app_id, operation_id, lambda: self._resume(host, store, receipt, descriptor))
            return self._complete(store, result, descriptor, receipt['action'])

    def _resume(self, host, store, receipt, descriptor):
        operation_id = receipt['operationId']
        publisher = self._publisher(store, operation_id, host)
        observed = self.transport.request('status', operation_id=operation_id,
            payload=dict(appId=host.id, releaseDigest=descriptor.digest))
        if (receipt['phase'] == 'preparing' and type(observed) is dict
                and observed.get('phase') in ('absent', 'prepared')):
            if receipt['action'] == 'deploy':
                root = Path(receipt['preparedRoot'])
                artifact = Path(receipt['runtimeArchive']) if receipt['runtimeArchive'] else None
                return publisher.deploy(descriptor, root/'files', operation_id=operation_id,
                    expected_current=receipt['expectedCurrent'], runtime_archive=artifact)
            return publisher.rollback(descriptor, operation_id=operation_id,
                                      expected_current=receipt['expectedCurrent'])
        # This re-observes status and can finish or recover a confirmed active
        # result, but it never replays activation.
        return publisher.reconcile(descriptor, operation_id=operation_id,
                                   expected_current=receipt['expectedCurrent'])


class NasStatusCollector:
    """Read selected remote descriptors and fresh health; never inspect Mac state."""

    def __init__(self, registry, *, transport=None, health=None, repository_factory=GitRepository):
        if registry.target.kind != 'nas-docker':
            raise ValueError('NAS status requires a NAS target')
        self.registry = registry
        self.transport = transport or NasTransport(registry.target, timeout_seconds=450)
        self.health = health or HttpHealthChecker()
        self.repository_factory = repository_factory
        self.caddy_state = None

    def collect(self):
        frontdoor = self.health.check(join_public_origin(self.registry.public_origin, '/'))
        self.caddy_state = ServiceState.RUNNING if frontdoor.healthy else ServiceState.STOPPED
        results = []
        for host in self.registry.apps:
            repository = self.repository_factory(host.repository)
            main = repository.main_commit()
            observed = _observe(self.transport, host.id)
            deployed = observed.descriptor.source_commit if observed.descriptor else None
            _, manifest = validate_registered_app(host, repository.manifest_at(deployed or main))
            frontend = self.health.check(join_public_origin(self.registry.public_origin, frontend_health_path(manifest)))
            backend_path = backend_health_path(host, manifest)
            backend = self.health.check(join_public_origin(self.registry.public_origin, backend_path)) if backend_path else None
            internal = HealthResult(observed.observed_healthy is True, None,
                                    None if observed.observed_healthy is True else 'NAS runtime readiness is false or unconfirmed')
            service_state = (ServiceState.RUNNING if observed.observed_healthy is True else ServiceState.STOPPED) if manifest.kind == 'service' else None
            results.append(AppStatus(host.id, main, deployed, deployed is not None and deployed != main,
                self.caddy_state, service_state, internal, frontend, backend, None))
        return tuple(results)
