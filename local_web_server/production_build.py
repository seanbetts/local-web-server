"""Mac-side exact-source builds and digest-verified Linux dependency caches.

Native commands run directly in a disposable source export. The separate fixed
Docker command prepares Linux artifacts on a cache miss; nothing runs on a NAS.
"""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import gzip
import zlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Callable, Iterator, TYPE_CHECKING

from .git_build import GitRepository
from .process_runner import ProcessCommand, ProcessRunner, ProcessWorkflowRunner
from .release_manifest import (ReleaseDescriptorV1, RuntimeIdentity, protected_name,
                               snapshot_files, validate_relative_path)


if TYPE_CHECKING:
    from .models import BuildSpec


_MACHO = {bytes.fromhex(value) for value in
          ('feedface', 'feedfacf', 'cefaedfe', 'cffaedfe', 'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}


@dataclass(frozen=True, slots=True)
class DependencyKey:
    lockfile_digest: str
    image_digest: str
    os: str
    architecture: str
    abi: str
    recipe_version: str
    recipe_digest: str = '0' * 64

    def __post_init__(self) -> None:
        if type(self.recipe_digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', self.recipe_digest):
            raise ValueError('dependency recipe digest must be SHA-256')
        # Reuse the public runtime identity's digest and text validation.
        RuntimeIdentity(self.image_digest, 'sha256:' + '0' * 64, self.os,
                        self.architecture, self.abi, self.lockfile_digest, self.recipe_version)

    def to_dict(self) -> dict[str, str]:
        return dict(lockfileDigest=self.lockfile_digest, imageDigest=self.image_digest,
                    os=self.os, architecture=self.architecture, abi=self.abi,
                    recipeVersion=self.recipe_version, recipeDigest=self.recipe_digest, contextContractVersion=1)

    @property
    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True,
                             separators=(',', ':')).encode()).hexdigest()

    def require_target(self) -> None:
        if (self.os, self.architecture) != ('linux', 'amd64'):
            raise ValueError('production dependencies require linux/amd64')


@dataclass(frozen=True, slots=True)
class CachedDependency:
    key: DependencyKey
    digest: str
    path: Path
    cache_hit: bool

    def runtime_identity(self) -> RuntimeIdentity:
        verified = verify_docker_archive(self.path)
        if verified.archive_digest != self.digest:
            raise ValueError('cached dependency archive changed')
        return RuntimeIdentity(verified.image_digest, 'sha256:' + self.digest,
            self.key.os, self.key.architecture, self.key.abi,
            self.key.lockfile_digest, self.key.recipe_version, verified.manifest_digest)


def _hash_file(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('dependency artifact must be a regular file')
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class VerifiedDockerArchive:
    image_digest: str
    os: str
    architecture: str
    archive_digest: str
    manifest_digest: str | None = None


def _unique_json(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate Docker metadata field")
        result[name] = value
    return result


def _reject_json_constant(_):
    raise ValueError("invalid Docker metadata constant")


def _reserved_image_tag(value):
    if type(value) is not str or not re.fullmatch(r"local-web-runtime:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", value):
        raise ValueError("saved image aliases must use the reserved local-web-runtime namespace")


def _validate_image_annotations(value):
    if type(value) is not dict or any(type(item) is not str for item in value.values()):
        raise ValueError("invalid saved image annotations")
    for name in ("org.opencontainers.image.ref.name", "io.containerd.image.name"):
        if name in value:
            _reserved_image_tag(value[name])


def verify_docker_archive(path: Path, *, maximum_layer_bytes: int = 4 * 1024**3,
                          maximum_total_layer_bytes: int = 16 * 1024**3) -> VerifiedDockerArchive:
    """Verify a single Linux/amd64 Docker-save image and its raw or gzip layers.

    The config SHA is logical content identity, not a universally runnable
    Docker reference. A verified optional index manifest supplies a second
    engine candidate; engines must inspect candidates after load. Both differ
    from the base-image pin and archive transport checksum. Layer diff IDs hash
    uncompressed bytes; gzip transport bytes are streamed with expansion bounds.
    OCI-only layouts and zstd layers remain unsupported by this contract.
    This inspection does not replace starting the image in an isolated runtime.
    """
    if any(type(limit) is not int or limit <= 0 for limit in (maximum_layer_bytes, maximum_total_layer_bytes)):
        raise ValueError('Docker layer expansion bounds must be positive integers')
    path = Path(path)
    archive_digest = _hash_file(path)
    try:
        with path.open('rb') as raw:
            raw.seek(0, os.SEEK_END)
            size = raw.tell()
            if size < 1024 or size % 512:
                raise ValueError('Docker-save archive is truncated or unsupported')
            raw.seek(-1024, os.SEEK_END)
            if raw.read(1024) != b'\0' * 1024:
                raise ValueError('Docker-save archive has no complete end marker')
        with tarfile.open(path, 'r:') as archive:
            members = {}
            for index, member in enumerate(archive):
                if index >= 100000:
                    raise ValueError('Docker-save archive has too many members')
                name = member.name.rstrip('/') if member.isdir() else member.name
                validate_relative_path(name)
                if name in members or not (member.isfile() or member.isdir()):
                    raise ValueError('Docker-save archive contains duplicate paths, links or special entries')
                members[name] = member

            def metadata(name):
                if name not in members or not members[name].isfile() or members[name].size > 1024 * 1024:
                    raise ValueError('Docker-save metadata is missing or oversized')
                with archive.extractfile(members[name]) as stream:
                    content = stream.read()
                if len(content) != members[name].size:
                    raise ValueError('Docker-save metadata is truncated')
                return content, json.loads(content, object_pairs_hook=_unique_json, parse_constant=_reject_json_constant)

            _, manifest = metadata('manifest.json')
            if (type(manifest) is not list or len(manifest) != 1
                    or type(manifest[0]) is not dict
                    or not {'Config', 'Layers'} <= set(manifest[0])
                    or set(manifest[0]) - {'Config', 'Layers', 'RepoTags'}):
                raise ValueError('a single Docker-save image is required; OCI layouts are unsupported')
            image = manifest[0]
            tags = image.get('RepoTags')
            if tags is not None:
                if type(tags) is not list or len(tags) > 64 or any(type(tag) is not str for tag in tags) or len(tags) != len(set(tags)):
                    raise ValueError('saved image aliases must be unique bounded tags')
                for tag in tags:
                    _reserved_image_tag(tag)
            if 'repositories' in members:
                _, repositories = metadata('repositories')
                if type(repositories) is not dict:
                    raise ValueError('invalid legacy image aliases')
                for repository, repository_tags in repositories.items():
                    if type(repository_tags) is not dict:
                        raise ValueError('invalid legacy image aliases')
                    for tag in repository_tags:
                        _reserved_image_tag(repository + ':' + tag)
            config_path = image['Config']
            layers = image['Layers']
            if type(config_path) is not str or type(layers) is not list or any(type(name) is not str for name in layers):
                raise ValueError('invalid Docker-save image manifest')
            validate_relative_path(config_path)
            config_bytes, config = metadata(config_path)
            config_sha = hashlib.sha256(config_bytes).hexdigest()
            if config_path not in (config_sha + '.json', 'blobs/sha256/' + config_sha):
                raise ValueError('Docker-save config filename does not match its digest')
            if type(config) is not dict or (config.get('os'), config.get('architecture')) != ('linux', 'amd64'):
                raise ValueError('prepared Docker image must target linux/amd64')
            rootfs = config.get('rootfs')
            if type(rootfs) is not dict or rootfs.get('type') != 'layers' or type(rootfs.get('diff_ids')) is not list or len(rootfs['diff_ids']) != len(layers):
                raise ValueError('Docker-save rootfs does not match its layers')
            total_expanded = 0
            stored_layers = []
            for name, expected in zip(layers, rootfs['diff_ids']):
                validate_relative_path(name)
                if (type(expected) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', expected)
                        or name not in members or not members[name].isfile()):
                    raise ValueError('Docker-save layer is missing or invalid')
                stored_digest = hashlib.sha256()
                with archive.extractfile(members[name]) as stored:
                    while chunk := stored.read(1024 * 1024):
                        stored_digest.update(chunk)
                digest = hashlib.sha256()
                count = 0
                with archive.extractfile(members[name]) as raw:
                    magic = raw.read(4)
                    raw.seek(0)
                    if magic == bytes.fromhex('28b52ffd'):
                        raise ValueError('zstd Docker layers are unsupported')
                    compressed = magic.startswith(b'\x1f\x8b')
                    stream = gzip.GzipFile(fileobj=raw, mode='rb') if compressed else raw
                    try:
                        while True:
                            # Read at most one byte beyond either expansion bound.
                            bound = min(1024 * 1024, maximum_layer_bytes - count + 1,
                                        maximum_total_layer_bytes - total_expanded + 1)
                            chunk = stream.read(bound)
                            if not chunk:
                                break
                            count += len(chunk)
                            total_expanded += len(chunk)
                            if count > maximum_layer_bytes or total_expanded > maximum_total_layer_bytes:
                                raise ValueError('Docker layer expansion exceeds the configured bound')
                            digest.update(chunk)
                    finally:
                        if compressed:
                            stream.close()
                if (not compressed and count != members[name].size) or 'sha256:' + digest.hexdigest() != expected:
                    raise ValueError('Docker-save uncompressed layer checksum mismatch')
                stored_layers.append(('sha256:' + stored_digest.hexdigest(), members[name].size, compressed))
            manifest_digest = None
            if 'index.json' in members:
                _, index = metadata('index.json')
                if (type(index) is not dict or type(index.get('schemaVersion')) is not int or index['schemaVersion'] != 2
                        or index.get('mediaType') not in (None, 'application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json')
                        or type(index.get('manifests')) is not list or len(index['manifests']) != 1):
                    raise ValueError('Docker-save index must select one verified image manifest')
                if 'annotations' in index:
                    _validate_image_annotations(index['annotations'])
                reference = index['manifests'][0]
                supported_manifests = ('application/vnd.docker.distribution.manifest.v2+json', 'application/vnd.oci.image.manifest.v1+json')
                if type(reference) is not dict or reference.get('mediaType') not in supported_manifests:
                    raise ValueError('unsupported Docker-save index manifest')
                if 'annotations' in reference:
                    _validate_image_annotations(reference['annotations'])
                if 'platform' in reference and (type(reference['platform']) is not dict
                        or (reference['platform'].get('os'), reference['platform'].get('architecture')) != ('linux', 'amd64')):
                    raise ValueError('Docker-save index platform disagrees with the Linux/amd64 config')

                def descriptor_blob(value, expected_digest=None, expected_size=None):
                    if (type(value) is not dict or type(value.get('digest')) is not str
                            or not re.fullmatch(r'sha256:[0-9a-f]{64}', value['digest'])
                            or type(value.get('size')) is not int or value['size'] < 0):
                        raise ValueError('invalid Docker-save manifest blob descriptor')
                    if (expected_digest is not None and value['digest'] != expected_digest
                            or expected_size is not None and value['size'] != expected_size):
                        raise ValueError('Docker-save index and legacy image graph disagree')
                    name = 'blobs/sha256/' + value['digest'][7:]
                    if name not in members or not members[name].isfile() or members[name].size != value['size']:
                        raise ValueError('Docker-save manifest blob is missing or has the wrong size')
                    digest = hashlib.sha256()
                    with archive.extractfile(members[name]) as stream:
                        while chunk := stream.read(1024 * 1024):
                            digest.update(chunk)
                    if 'sha256:' + digest.hexdigest() != value['digest']:
                        raise ValueError('Docker-save manifest blob checksum mismatch')
                    return name

                manifest_path = descriptor_blob(reference)
                _, selected = metadata(manifest_path)
                if (type(selected) is not dict or type(selected.get('schemaVersion')) is not int or selected['schemaVersion'] != 2
                        or selected.get('mediaType') not in (None, reference['mediaType'])
                        or type(selected.get('layers')) is not list or len(selected['layers']) != len(layers)):
                    raise ValueError('Docker-save manifest and legacy image graph disagree')
                if 'annotations' in selected:
                    _validate_image_annotations(selected['annotations'])
                selected_config = selected.get('config')
                if type(selected_config) is not dict or selected_config.get('mediaType') not in (
                        'application/vnd.docker.container.image.v1+json', 'application/vnd.oci.image.config.v1+json'):
                    raise ValueError('invalid Docker-save manifest config type')
                descriptor_blob(selected_config, 'sha256:' + config_sha, len(config_bytes))
                for selected_layer, (stored_digest, stored_size, compressed) in zip(selected['layers'], stored_layers):
                    supported_layers = (('application/vnd.docker.image.rootfs.diff.tar.gzip', 'application/vnd.oci.image.layer.v1.tar+gzip') if compressed else
                                        ('application/vnd.docker.image.rootfs.diff.tar', 'application/vnd.oci.image.layer.v1.tar'))
                    if type(selected_layer) is not dict or selected_layer.get('mediaType') not in supported_layers:
                        raise ValueError('Docker-save manifest layer type disagrees with its stored bytes')
                    descriptor_blob(selected_layer, stored_digest, stored_size)
                manifest_digest = reference['digest']
        if _hash_file(path) != archive_digest:
            raise ValueError('Docker-save archive changed during verification')
        return VerifiedDockerArchive('sha256:' + config_sha, 'linux', 'amd64', archive_digest, manifest_digest)
    except (OSError, EOFError, zlib.error, tarfile.TarError, UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('invalid Docker-save archive') from error


class DependencyCache:
    """Persist prepared image archives outside source exports, verifying each hit."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def prepare(self, key: DependencyKey,
                installer: Callable[[Path], None]) -> CachedDependency:
        key.require_target()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError('dependency cache must be a private directory')
        lock_descriptor = os.open(self.root / (key.digest + '.lock'),
                                  os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            return self._prepare_locked(key, installer)
        finally:
            os.close(lock_descriptor)

    def _prepare_locked(self, key: DependencyKey,
                        installer: Callable[[Path], None]) -> CachedDependency:
        target = self.root / key.digest
        archive = target / 'artifact.tar'
        if target.is_symlink():
            raise ValueError('dependency cache entry must not be a link')
        try:
            metadata_path = target / 'metadata.json'
            if metadata_path.is_symlink():
                raise ValueError('dependency metadata must not be a link')
            metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
            digest = verify_docker_archive(archive).archive_digest
            if metadata == dict(key=key.to_dict(), sha256=digest):
                return CachedDependency(key, digest, archive, True)
        except (OSError, ValueError):
            pass
        with tempfile.TemporaryDirectory(prefix='.prepare-', dir=self.root) as temporary:
            staging = Path(temporary)
            prepared = staging / 'artifact.tar'
            installer(prepared)
            digest = verify_docker_archive(prepared).archive_digest
            if prepared.stat().st_size == 0:
                raise ValueError('prepared dependency archive is empty')
            prepared.chmod(0o444)
            (staging / 'metadata.json').write_text(
                json.dumps(dict(key=key.to_dict(), sha256=digest), sort_keys=True),
                encoding='utf-8')
            if target.exists():
                shutil.rmtree(target)
            # All readers use the same key lock; a failed installer leaves no hit.
            target.mkdir(mode=0o700)
            try:
                os.replace(prepared, archive)
                os.replace(staging / 'metadata.json', target / 'metadata.json')
            except BaseException:
                shutil.rmtree(target)
                raise
        return CachedDependency(key, digest, archive, False)


def dependency_build_command(source: Path, dockerfile: Path, archive: Path,
                             key: DependencyKey, buildkit_cache: Path,
                             base_image_reference: str, *,
                             dependency_inputs: tuple[str, ...]) -> ProcessCommand:
    """Plan a pinned dependency-stage build; callers run it only on cache misses.

    The app-owned Dockerfile consumes RUNTIME_IMAGE and installs only its locked
    Linux runtime dependencies. Frontend compilation stays in native commands.
    """
    key.require_target()
    validate_relative_path(dockerfile.as_posix())
    source = source.resolve(strict=True)
    recipe = source / dockerfile
    if type(dependency_inputs) is not tuple or not dependency_inputs or any(type(path) is not str for path in dependency_inputs):
        raise ValueError('Linux dependency preparation requires explicit input files')
    for path in dependency_inputs:
        validate_relative_path(path)
    context_files = snapshot_files(source)
    if {entry.path for entry in context_files} != set(dependency_inputs) | {dockerfile.as_posix()}:
        raise ValueError('Linux dependency context must contain only the complete declared input files and Dockerfile')
    if not recipe.is_file():
        raise ValueError('dependency Dockerfile is missing')
    archive = archive.resolve()
    buildkit_cache = buildkit_cache.resolve()
    if archive.is_relative_to(source) or buildkit_cache.is_relative_to(source):
        raise ValueError('dependency artifacts and caches must be outside the source export')
    if (type(base_image_reference) is not str
            or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}', base_image_reference)
            or base_image_reference.rsplit('@', 1)[1] != key.image_digest):
        raise ValueError('base image reference must match the pinned digest')
    return ProcessCommand('Linux dependency image', (
        'docker', 'buildx', 'build', '--platform', 'linux/amd64',
        '--file', str(recipe), '--build-arg', 'RUNTIME_IMAGE=' + base_image_reference,
        '--cache-from', 'type=local,src=' + str(buildkit_cache),
        '--cache-to', 'type=local,dest=' + str(buildkit_cache) + ',mode=max',
        '--output', 'type=docker,dest=' + str(archive), str(source)))


def _export_revision(repository: GitRepository, commit: str, destination: Path) -> None:
    """Copy canonical Git blobs, without checkout hooks or archive attributes."""
    object_type = repository._run('--no-replace-objects', 'cat-file', '-t', commit,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=60)
    if object_type.stdout != b'commit\n':
        raise ValueError('production source identity must name a commit object')
    result = repository._run('--no-replace-objects', 'ls-tree', '-rz', '--full-tree', commit,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=60)
    rows = []
    for raw in result.stdout.split(b'\0'):
        if not raw:
            continue
        metadata, encoded_name = raw.split(b'\t', 1)
        mode, kind, object_id = metadata.split()
        name = encoded_name.decode('utf-8', errors='strict')
        if any(protected_name(part) for part in name.split('/')):
            continue
        validate_relative_path(name)
        if kind != b'blob' or mode not in (b'100644', b'100755'):
            raise ValueError('source tree contains links, submodules or special files')
        rows.append((name, object_id, mode))
    destination.mkdir(mode=0o700)
    with tempfile.TemporaryFile() as spool:
        repository._run('--no-replace-objects', 'cat-file', '--batch',
                        input=b''.join(object_id+b'\n' for _, object_id, _ in rows),
                        stdout=spool, stderr=subprocess.DEVNULL, check=True, timeout=60)
        spool.seek(0)
        for name, object_id, mode in rows:
            header = spool.readline(256).rstrip(b'\n').split()
            if len(header) != 3 or header[:2] != [object_id, b'blob'] or not header[2].isdigit():
                raise ValueError('source object is unavailable')
            remaining = int(header[2])
            target = destination/name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with target.open('xb') as output:
                while remaining:
                    chunk = spool.read(min(remaining, 1024*1024))
                    if not chunk:
                        raise ValueError('source object is truncated')
                    output.write(chunk)
                    remaining -= len(chunk)
            if spool.read(1) != b'\n':
                raise ValueError('invalid source object boundary')
            target.chmod(0o755 if mode == b'100755' else 0o644)
        if spool.read(1):
            raise ValueError('unexpected source object bytes')


@contextmanager
def production_source_export(repository: GitRepository, commit: str) -> Iterator[Path]:
    """Provide only exact committed source for isolated dependency preparation."""
    if type(commit) is not str or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit):
        raise ValueError('production source requires a full Git object ID')
    with tempfile.TemporaryDirectory(prefix='local-web-production-source-') as temporary:
        exported = Path(temporary) / 'source'
        _export_revision(repository, commit, exported)
        yield exported


def _require_portable_files(root: Path) -> None:
    for entry in snapshot_files(root):
        parts = Path(entry.path).parts
        if any(part in ('.venv', 'venv', 'node_modules', '__pycache__') for part in parts):
            raise ValueError('installed dependencies must use the Linux dependency artifact')
        with (root / entry.path).open('rb') as stream:
            if stream.read(4) in _MACHO:
                raise ValueError('macOS binaries cannot become Linux runtime app files')


class ProductionBuilder:
    """Prepare app files and a complete descriptor without touching serving state."""

    def __init__(self, runner: ProcessWorkflowRunner | None = None):
        self.runner = runner or ProcessRunner()

    def build(self, repository: GitRepository, *, commit: str, app_id: str,
              recipe_version: str, commands: tuple[ProcessCommand, ...], output: Path,
              destination: Path, data_contract: str, rollback_policy: str,
              runtime: RuntimeIdentity | None = None,
              build_spec: BuildSpec | None = None,
              prepare_source: Callable[[Path], None] | None = None) -> ReleaseDescriptorV1:
        # Validate metadata and paths before executing any app code.
        ReleaseDescriptorV1(app_id, commit, recipe_version, runtime, (), data_contract, rollback_policy)
        validate_relative_path(output.as_posix())
        if build_spec is not None and build_spec.output != output:
            raise ValueError('build specification and declared output must agree')
        if runtime is not None and (runtime.os, runtime.architecture) != ('linux', 'amd64'):
            raise ValueError('production runtime requires linux/amd64')
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('prepared release destination already exists')
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.local-web-production-', dir=destination.parent) as temporary:
            staging = Path(temporary)
            source = staging / 'source'
            _export_revision(repository, commit, source)
            if prepare_source is not None:
                prepare_source(source)
            if commands:
                self.runner.run(source, commands)
            if build_spec is not None:
                from .release_composer import ReleaseComposer
                ReleaseComposer().compose(source, build_spec)
            built = source
            for part in output.parts:
                built = built / part
                if built.is_symlink():
                    raise ValueError('build output path cannot contain links')
            try:
                resolved_output = built.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError('build output is unavailable') from error
            if not resolved_output.is_relative_to(source.resolve(strict=True)):
                raise ValueError('build output must stay inside the source export')
            _require_portable_files(built)
            before = snapshot_files(built)
            shutil.copytree(built, staging / 'release' / 'files', symlinks=True)
            files = snapshot_files(staging / 'release' / 'files')
            if files != before:
                raise ValueError('build output changed while preparing the release')
            descriptor = ReleaseDescriptorV1(app_id, commit, recipe_version, runtime,
                                            files, data_contract, rollback_policy)
            (staging / 'release' / 'descriptor.json').write_bytes(descriptor.canonical_bytes())
            # Same-filesystem publication; no live release or pointer is involved.
            if destination.exists() or destination.is_symlink():
                raise FileExistsError('prepared release destination already exists')
            os.rename(staging / 'release', destination)
            return descriptor
