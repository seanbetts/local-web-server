"""Platform-side immutable release publication over the fixed NAS adapter.

This module prepares bytes and requests transitions. The installation-owned
adapter verifies/materializes complete descriptors, fences writers, enforces
expected-current/data-contract compatibility, and owns atomic serving pointers.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
import hashlib
import gzip
import os
from pathlib import Path
import re
import stat
import tarfile
import tempfile
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from .nas_platform_installation import InstallationApproval

from .nas_transport import NasTransportError
from .release_manifest import FileEntry, ReleaseDescriptorV1, snapshot_files


_DIGEST = re.compile(r'^[0-9a-f]{64}$')


class PublicationTransport(Protocol):
    def request(self, action: str, *, operation_id: str,
                payload: dict[str, Any]) -> dict[str, Any]: ...


class NasPublicationError(ValueError):
    """Unresolved publication retains the exact descriptor for recovery/status."""

    def __init__(self, descriptor: ReleaseDescriptorV1, operation_id: str):
        self.descriptor = descriptor
        self.release_digest = descriptor.digest
        self.operation_id = operation_id
        super().__init__('NAS publication requires status reconciliation or app recovery')


@dataclass(frozen=True, slots=True)
class PublicationResult:
    operation_id: str
    release_digest: str
    state: str
    current: str | None


def _artifact_metadata(path: Path) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('upload artifact must be a regular file')
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns) or size != before.st_size:
            raise ValueError('upload artifact changed during verification')
        return dict(path=str(path.resolve(strict=True)), sha256=digest.hexdigest(), size=size)
    finally:
        os.close(descriptor)


def _bundle(root: Path, files: tuple[FileEntry, ...], path: Path) -> dict[str, Any]:
    with (path.open('wb') as raw,
          gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed,
          tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive):
        for entry in files:
            source = root / entry.path
            actual = _artifact_metadata(source)
            if (actual['sha256'], actual['size']) != (entry.sha256, entry.size):
                raise ValueError('app blob does not match the release descriptor')
            info = tarfile.TarInfo('blobs/' + entry.sha256)
            info.size = entry.size
            info.mode = 0o444
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(descriptor, 'rb') as stream:
                archive.addfile(info, stream)
    metadata = _artifact_metadata(path)
    metadata['blobs'] = [entry.sha256 for entry in files]
    return metadata


class NasDeploymentManager:
    def __init__(self, transport: PublicationTransport, *,
                 public_health: Callable[[ReleaseDescriptorV1], bool],
                 before_transition: Callable[[str], None] | None = None,
                 select_upload_blobs: Callable[[tuple[str, ...]], tuple[str, ...]] | None = None):
        if not callable(public_health):
            raise ValueError('NAS publication requires a public HTTPS health gate')
        self.transport = transport
        self.public_health = public_health
        self.before_transition = before_transition
        self.select_upload_blobs = select_upload_blobs

    @staticmethod
    def _validate(descriptor: ReleaseDescriptorV1, expected_current: str | None) -> None:
        if descriptor.rollback_policy != 'compatible':
            raise ValueError('release requires an explicit app migration procedure')
        if expected_current is not None and (type(expected_current) is not str or not _DIGEST.fullmatch(expected_current)):
            raise ValueError('expected current release must be a full descriptor digest')
        if descriptor.runtime is not None and (descriptor.runtime.os, descriptor.runtime.architecture) != ('linux', 'amd64'):
            raise ValueError('NAS releases require linux/amd64 runtime artifacts')

    def deploy(self, descriptor: ReleaseDescriptorV1, files_root: Path, *,
               operation_id: str, expected_current: str | None,
               runtime_archive: Path | None = None,
               installation_approval: InstallationApproval | None = None) -> PublicationResult:
        self._validate(descriptor, expected_current)
        files_root = Path(files_root)
        if snapshot_files(files_root) != descriptor.files:
            raise ValueError('prepared app files do not match the complete release descriptor')
        identity = dict(appId=descriptor.app_id, releaseDigest=descriptor.digest)
        payload = dict(**identity, descriptor=descriptor.to_dict(), expectedCurrent=expected_current)
        if installation_approval is not None:
            from .nas_platform_installation import InstallationApproval
            if (not isinstance(installation_approval, InstallationApproval) or descriptor.app_id != 'local-web-platform'
                    or installation_approval.platform_release_digest != descriptor.digest
                    or installation_approval.expected_current != expected_current):
                raise ValueError('installation approval requires its exact reserved platform release')
            payload['installationApproval'] = installation_approval.to_dict()
        prepared = self.transport.request('prepare', operation_id=operation_id, payload=payload)
        if (type(prepared) is not dict or set(prepared) != {'missingBlobs', 'runtimeRequired'}
                or type(prepared['missingBlobs']) is not list
                or type(prepared['runtimeRequired']) is not bool
                or any(type(digest) is not str for digest in prepared['missingBlobs'])):
            raise ValueError('invalid NAS prepare acknowledgement')
        known = {entry.sha256: entry for entry in reversed(descriptor.files)}
        missing = prepared['missingBlobs']
        if len(set(missing)) != len(missing) or any(digest not in known for digest in missing):
            raise ValueError('NAS requested blobs outside the release descriptor')
        selection = tuple(sorted(missing))
        if self.select_upload_blobs is not None:
            selection = self.select_upload_blobs(selection)
            if (type(selection) is not tuple or any(type(digest) is not str or digest not in known for digest in selection)
                    or len(set(selection)) != len(selection) or not set(missing) <= set(selection)):
                raise ValueError('invalid original NAS upload selection')
        runtime = None
        if prepared['runtimeRequired']:
            if descriptor.runtime is None or runtime_archive is None:
                raise ValueError('NAS requires a prepared Linux dependency artifact')
            runtime = _artifact_metadata(Path(runtime_archive))
            if 'sha256:' + runtime['sha256'] != descriptor.runtime.dependency_digest:
                raise ValueError('Linux dependency archive does not match the descriptor')
        with tempfile.TemporaryDirectory(prefix='local-web-nas-upload-') as temporary:
            bundle = _bundle(files_root, tuple(known[digest] for digest in sorted(selection)),
                             Path(temporary) / 'blobs.tar.gz') if selection else None
            if bundle is not None or runtime is not None:
                uploaded = self.transport.request('upload', operation_id=operation_id,
                    payload=dict(**identity, bundle=bundle, runtimeArtifact=runtime))
                if uploaded != {'uploaded': True} or type(uploaded.get('uploaded')) is not bool:
                    raise ValueError('NAS did not acknowledge completed artifact verification')
            return self._transition('activate', descriptor, operation_id, expected_current,
                                    dict(**identity, expectedCurrent=expected_current))

    def rollback(self, descriptor: ReleaseDescriptorV1, *, operation_id: str,
                 expected_current: str) -> PublicationResult:
        self._validate(descriptor, expected_current)
        # The adapter must compare this descriptor's dataContract with the current
        # descriptor before switching code. Code rollback never restores data.
        payload = dict(appId=descriptor.app_id, releaseDigest=descriptor.digest,
                       descriptor=descriptor.to_dict(), expectedCurrent=expected_current)
        return self._transition('rollback', descriptor, operation_id, expected_current, payload)

    def _transition(self, action: str, descriptor: ReleaseDescriptorV1,
                    operation_id: str, expected_current: str | None,
                    payload: dict[str, Any]) -> PublicationResult:
        acknowledgement = self._request_transition(action, descriptor, operation_id, payload)
        return self._complete_transition(descriptor, operation_id, expected_current, acknowledgement)

    def reconcile(self, descriptor: ReleaseDescriptorV1, *, operation_id: str,
                  expected_current: str | None) -> PublicationResult:
        """Observe an original transition; never resend its activation request."""
        self._validate(descriptor, expected_current)
        try:
            acknowledgement = self.transport.request('status', operation_id=operation_id,
                payload=dict(appId=descriptor.app_id, releaseDigest=descriptor.digest))
        except NasTransportError as error:
            raise NasPublicationError(descriptor, operation_id) from error
        return self._complete_transition(descriptor, operation_id, expected_current, acknowledgement)

    def _complete_transition(self, descriptor: ReleaseDescriptorV1, operation_id: str,
                             expected_current: str | None, acknowledgement: dict) -> PublicationResult:
        identity = dict(appId=descriptor.app_id, releaseDigest=descriptor.digest)
        if not self._terminal(acknowledgement, descriptor, expected_current):
            raise NasPublicationError(descriptor, operation_id)
        public_healthy = self._public_healthy(descriptor, operation_id)
        if not public_healthy and acknowledgement['state'] == 'active':
            # Recover this operation; a second operation would bypass its fence.
            try:
                acknowledgement = self._request_transition('rollback', descriptor, operation_id,
                    dict(**identity, expectedCurrent=descriptor.digest, publicHealthFailed=True))
            except NasTransportError as error:
                raise NasPublicationError(descriptor, operation_id) from error
            if (not self._terminal(acknowledgement, descriptor, expected_current)
                    or acknowledgement['state'] != 'rolled-back'):
                raise NasPublicationError(descriptor, operation_id)
            public_healthy = self._public_healthy(descriptor, operation_id)
        if not public_healthy:
            raise NasPublicationError(descriptor, operation_id)
        state, current = acknowledgement['state'], acknowledgement['current']
        result = PublicationResult(operation_id, descriptor.digest, state, current)
        try:
            finished = self.transport.request('finish', operation_id=operation_id,
                payload=dict(**identity, state=state, current=current))
        except NasTransportError as error:
            raise NasPublicationError(descriptor, operation_id) from error
        if finished != {'finished': True} or type(finished.get('finished')) is not bool:
            raise NasPublicationError(descriptor, operation_id)
        return result

    def _public_healthy(self, descriptor: ReleaseDescriptorV1, operation_id: str) -> bool:
        try:
            result = self.public_health(descriptor)
        except Exception as error:
            raise NasPublicationError(descriptor, operation_id) from error
        if type(result) is not bool:
            raise NasPublicationError(descriptor, operation_id)
        return result

    @staticmethod
    def _terminal(acknowledgement: dict, descriptor: ReleaseDescriptorV1,
                  expected_current: str | None) -> bool:
        return (type(acknowledgement) is dict and acknowledgement.get('healthy') is True and (
            acknowledgement.get('state') == 'active' and acknowledgement.get('current') == descriptor.digest
            or acknowledgement.get('state') == 'rolled-back' and expected_current is not None
            and acknowledgement.get('current') == expected_current))

    def _request_transition(self, action: str, descriptor: ReleaseDescriptorV1,
                            operation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        identity = dict(appId=descriptor.app_id, releaseDigest=descriptor.digest)
        if self.before_transition is not None:
            self.before_transition(action)
        try:
            acknowledgement = self.transport.request(action, operation_id=operation_id, payload=payload)
        except NasTransportError as error:
            if not error.outcome_unknown:
                raise
            # Status is the only permitted reconciliation. In particular, a lost
            # activation acknowledgement never causes another activation request.
            try:
                acknowledgement = self.transport.request('status', operation_id=operation_id, payload=identity)
            except NasTransportError as status_error:
                raise NasPublicationError(descriptor, operation_id) from status_error
        return acknowledgement
