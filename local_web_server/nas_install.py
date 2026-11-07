"""Prepare a pinned NAS platform release on the Mac, without installing it.

The returned complete file/permission plan is reviewable input to later fixed
publication. It never creates NAS directories, activates code, installs a skill,
changes hooks or invokes launchctl. Image/runtime/identity and UID feasibility
still require the separately approved shadow-runtime validation.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat

from .config import probe_environment
from .git_runner import run_git
from .index_registry import render_index_registry, render_index_registry_v2
from .models import AppManifest, HostRegistry
from .nas_render import ContainerRuntimeSpec, render_nas_compose
from .process_runner import ProcessCommand, ProcessRunner, ProcessWorkflowRunner
from .release_manifest import FileEntry, ReleaseDescriptorV1
from .render import probe_environment_name, render_caddyfile
from .system_index_bundle import load_system_index_bundle, load_ui_gallery_bundle
from .theme import validate_theme


PLATFORM_APP_ID = 'local-web-platform'


@dataclass(frozen=True)
class PlatformFile:
    path: str
    content: bytes
    mode: int


@dataclass(frozen=True)
class PathPermission:
    """Proposed NAS path ownership; no mkdir/chmod/chown occurs in preparation."""
    path: str
    mode: int
    uid: int
    gid: int
    kind: str = 'directory'


@dataclass(frozen=True)
class PreparedNasPlatform:
    descriptor: ReleaseDescriptorV1
    files: tuple[PlatformFile, ...]
    permissions: tuple[PathPermission, ...]

    def verify(self) -> None:
        entries = tuple(FileEntry(file.path, hashlib.sha256(file.content).hexdigest(), len(file.content), file.mode)
                        for file in self.files)
        if entries != self.descriptor.files or len({file.path for file in self.files}) != len(self.files):
            raise ValueError('prepared platform files disagree with descriptor')
        paths = set()
        for entry in self.permissions:
            path = Path(entry.path)
            if ('\\' in entry.path or '\0' in entry.path or entry.path in paths or entry.path != '.' and (path.is_absolute() or path.as_posix() != entry.path
                    or '..' in path.parts) or entry.kind not in ('file', 'directory')
                    or any(type(value) is not int or value < 0 for value in (entry.mode, entry.uid, entry.gid)) or entry.mode > 0o777):
                raise ValueError('invalid platform permission plan')
            paths.add(entry.path)

    def write_files(self, root: Path) -> None:
        """Materialize only a new local publication artifact, never a NAS path."""
        self.verify()
        root = Path(root)
        root.mkdir(mode=0o700)
        for file in self.files:
            path = root / file.path
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open('xb') as output:
                output.write(file.content)
                output.flush(); os.fsync(output.fileno())
                os.fchmod(output.fileno(), file.mode)
            parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(parent)
            finally: os.close(parent)

    @property
    def source_config_digest(self) -> str:
        """Bind runtime approval to the complete fixed configuration, not UI bytes."""
        names = {'compose.yaml', 'Caddyfile', 'serve.json', 'runtime-settings.json'}
        digests = {file.path: hashlib.sha256(file.content).hexdigest()
                   for file in self.files if file.path in names}
        if set(digests) != names:
            raise ValueError('the complete runtime configuration is required')
        return hashlib.sha256(json.dumps(digests, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _require_source(repository: Path, source_commit: str) -> None:
    # Local import avoids a cycle when the existing installer dispatches here.
    from .platform_installation import require_committed_installation_sources
    from .install import InstallError
    try:
        require_committed_installation_sources(repository)
    except InstallError as error:
        raise ValueError('platform installation sources must be committed') from error
    head = run_git(repository, ('rev-parse', 'HEAD'), maximum_stdout=128)
    if head.returncode != 0 or head.stdout.strip() != source_commit:
        raise ValueError('platform source commit does not match the reviewed checkout')


def _theme(repository: Path, accents: tuple[str, ...]) -> bytes:
    path = repository / 'platform_assets' / 'theme.css'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
            raise ValueError('platform theme must be a bounded regular file')
        content = source.read(1024 * 1024 + 1)
    validate_theme(content, accents)
    return content


def _require_prebuilt_sources(repository: Path, paths: tuple[Path, ...]) -> None:
    """Without a build, only outputs stored in the exact source commit qualify."""
    tracked = run_git(repository, ('ls-files', '--error-unmatch', '--',
        *(':(literal)' + str(path.relative_to(repository)) for path in paths)), maximum_stdout=1024 * 1024)
    if tracked.returncode != 0:
        raise ValueError('prebuilt platform outputs need pinned provenance or a native build')


def _permissions(app_ids: tuple[str, ...], runtimes: tuple[ContainerRuntimeSpec, ...]) -> tuple[PathPermission, ...]:
    entries = [PathPermission('.', 0o700, 0, 0), PathPermission('apps', 0o755, 0, 0),
               PathPermission('state', 0o700, 0, 0), PathPermission('secrets', 0o700, 0, 0),
               PathPermission('tailscale', 0o700, 0, 0)]
    # Publishers must apply the same directory traversal mode to immutable
    # release descendants and generation children before binding them read-only.
    for app in sorted((PLATFORM_APP_ID, *app_ids)):
        for suffix in ('', '/releases', '/generations'):
            entries.append(PathPermission('apps/' + app + suffix, 0o755, 0, 0))
    for spec in sorted(runtimes, key=lambda value: value.app_id):
        uid, gid = (int(value) for value in spec.user.split(':'))
        entries.append(PathPermission('state/' + spec.app_id, 0o700, uid, gid))
        if spec.secret_files:
            entries.append(PathPermission('secrets/' + spec.app_id, 0o700, 0, 0))
        for secret in sorted(spec.secret_files):
            entries.append(PathPermission('secrets/' + spec.app_id + '/' + secret, 0o400, uid, gid, 'file'))
    return tuple(sorted(entries, key=lambda entry: entry.path))


def prepare_nas_platform(
    repository: Path,
    registry: HostRegistry,
    manifests: Mapping[str, AppManifest],
    runtimes: tuple[ContainerRuntimeSpec, ...],
    *,
    source_commit: str,
    caddy_image: str,
    tailscale_image: str,
    build: bool = False,
    runner: ProcessWorkflowRunner | None = None,
) -> PreparedNasPlatform:
    """Validate/build the selected source and return all immutable platform files.

    ``build=False`` consumes bundles tracked in the selected commit for fixture
    previews; ordinary ignored build outputs require the native build path.
    The default remains preparation only, with no target mutation or live claims.
    """
    repository = Path(repository).resolve(strict=True)
    # Validate source identity and descriptor metadata before running even builds.
    ReleaseDescriptorV1(PLATFORM_APP_ID, source_commit, 'nas-platform-v1', None, (),
                        'local-web-platform-v1', 'compatible')
    _require_source(repository, source_commit)
    if PLATFORM_APP_ID in manifests:
        raise ValueError('the platform release ID cannot be registered as an app')
    # Refuse an incomplete target/recipe inventory before spending build time.
    render_nas_compose(registry, manifests, runtimes, caddy_image=caddy_image,
                       tailscale_image=tailscale_image)
    if build:
        (runner or ProcessRunner()).run(repository, (
            ProcessCommand('system-index-build', ('npm', 'run', 'build:index')),
            ProcessCommand('ui-gallery-build', ('npm', 'run', 'build:gallery')),
        ))
    index = load_system_index_bundle(repository / 'apps' / 'system-index' / 'dist')
    gallery = load_ui_gallery_bundle(repository / 'examples' / 'ui-gallery' / 'dist')
    if not build:
        _require_prebuilt_sources(repository, (
            repository / 'apps/system-index/dist/index.html',
            *(repository / 'apps/system-index/dist' / str(path) for path, _ in index.assets),
            *(repository / 'examples/ui-gallery/dist' / str(path) for path, _ in gallery),
            repository / 'platform_assets/theme.css',
        ))
    accents = tuple(sorted({manifest.home.accent for manifest in manifests.values()} |
                           {service.accent for service in registry.services}))
    theme = _theme(repository, accents)
    environment = tuple(sorted(
        (probe_environment_name(host.id, name), value)
        for host in registry.apps if host.backend_probe is not None
        for name, value in probe_environment(manifests[host.id], host).items()))
    compose = render_nas_compose(registry, manifests, runtimes, caddy_image=caddy_image,
                                 tailscale_image=tailscale_image, caddy_environment=environment)
    serve_host = registry.host + ':443'
    serve = dict(TCP={'443': {'HTTPS': True}},
                 Web={serve_host: {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8080'}}}},
                 AllowFunnel={serve_host: False})
    specs = {spec.app_id: spec for spec in runtimes}
    settings_apps = {}
    for app_id, manifest in sorted(manifests.items()):
        spec = specs.get(app_id)
        if manifest.kind == 'service' and manifest.service is None:
            raise ValueError('service runtime settings require the app service contract')
        settings_apps[app_id] = dict(
            kind=manifest.kind, port=spec.port if spec else None,
            internalHealthPath=manifest.service.internal_health_path if spec else None,
            publicHealthPath=manifest.health_path, healthRuntime=spec.health_runtime if spec else None,
            user=spec.user if spec else None, cpuLimit=spec.cpu_limit if spec else None,
            memoryLimitMiB=spec.memory_limit_mib if spec else None,
            secretFiles=sorted(spec.secret_files) if spec else [])
    settings = dict(schemaVersion=1, publicOrigin=registry.public_origin,
                    proxyAddress=registry.target.proxy_address, networkSubnet=registry.target.network_subnet,
                    apps=settings_apps)
    files = [PlatformFile('compose.yaml', compose.encode(), 0o600),
             PlatformFile('Caddyfile', render_caddyfile(registry, manifests).encode(), 0o600),
             PlatformFile('serve.json', (json.dumps(serve, sort_keys=True, indent=2) + '\n').encode(), 0o600),
             PlatformFile('runtime-settings.json', (json.dumps(settings, sort_keys=True, indent=2) + '\n').encode(), 0o600),
             PlatformFile('home.html', index.home, 0o644), PlatformFile('theme.css', theme, 0o644),
             PlatformFile('index/registry-v1.json', render_index_registry(registry, manifests), 0o644),
             PlatformFile('index/registry-v2.json', render_index_registry_v2(registry, manifests), 0o644)]
    files.extend(PlatformFile('index/' + str(path), content, 0o644) for path, content in index.assets)
    files.extend(PlatformFile('ui-gallery/' + str(path), content, 0o644) for path, content in gallery)
    files = tuple(sorted(files, key=lambda file: file.path))
    if len({file.path for file in files}) != len(files):
        raise ValueError('duplicate platform output paths')
    _require_source(repository, source_commit)
    descriptor = ReleaseDescriptorV1(PLATFORM_APP_ID, source_commit, 'nas-platform-v1', None,
        tuple(FileEntry(file.path, hashlib.sha256(file.content).hexdigest(), len(file.content), file.mode)
              for file in files), 'local-web-platform-v1', 'compatible')
    return PreparedNasPlatform(descriptor, files, _permissions(tuple(manifests), runtimes))
