"""Mac-side preparation of complete NAS releases from app-owned recipes.

Preparation writes only a new local artifact and shared build caches. It cannot
publish a host profile, touch a serving pointer or start a NAS service.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import tempfile

from .config import build_environment, validate_registered_app
from .git_build import GitRepository
from .models import HostApp
from .native_build import (
    NativeDependencyCache, NativePlatformIdentity, dependency_input_export, probe_native_platform,
)
from .nas_recipe import require_nas_recipe
from .nas_render import ContainerRuntimeSpec
from .process_runner import ProcessRunner, ProcessWorkflowRunner
from .production_build import DependencyCache, DependencyKey, ProductionBuilder, production_source_export
from .release_manifest import ReleaseDescriptorV1, snapshot_files


DependencyInstaller = Callable[[Path, Path, DependencyKey, Path, str], None]


@dataclass(frozen=True)
class PreparedNasRelease:
    root: Path
    descriptor: ReleaseDescriptorV1
    runtime_archive: Path | None
    runtime_spec: ContainerRuntimeSpec | None
    native_cache_hit: bool | None
    runtime_cache_hit: bool | None

    @property
    def files_root(self) -> Path:
        return self.root / 'files'


class NasReleasePreparer:
    def __init__(self, cache_root: Path, *, native_platform: NativePlatformIdentity | None = None,
                 dependency_installer: DependencyInstaller | None = None,
                 runner: ProcessWorkflowRunner | None = None):
        self.cache_root = Path(cache_root)
        self.native_platform = native_platform
        self.dependency_installer = dependency_installer
        self.runner = runner

    def prepare(self, host: HostApp, *, commit: str, destination: Path) -> PreparedNasRelease:
        repository = GitRepository(host.repository)
        host, manifest = validate_registered_app(host, repository.manifest_at(commit), target_kind="nas-docker")
        recipe = require_nas_recipe(manifest.production, kind=manifest.kind)
        if recipe.rollback_policy != 'compatible':
            raise ValueError('app migration requires its reviewed data recovery procedure')
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError('prepared NAS release already exists')
        public = build_environment(manifest, host, {})
        runner = self.runner or ProcessRunner(public_environment={**public, 'PUBLIC_LOCAL_WEB_SOURCE_COMMIT': commit})
        runtime_archive = None
        runtime_identity = None
        runtime_spec = None
        runtime_hit = None
        if recipe.runtime is not None:
            with production_source_export(repository, commit) as source:
                key = recipe.dependency_key(source)

                def install(archive: Path) -> None:
                    if self.dependency_installer is None:
                        raise ValueError('a configured isolated Mac Linux dependency builder is required')
                    with dependency_input_export(source, recipe.runtime.lockfiles + (recipe.runtime.dockerfile,)) as inputs:
                        self.dependency_installer(inputs, Path(recipe.runtime.dockerfile), key,
                                                  archive, recipe.runtime.base_image)

                cached = DependencyCache(self.cache_root/'linux').prepare(key, install)
                runtime_identity = cached.runtime_identity()
                runtime_archive, runtime_hit = cached.path, cached.cache_hit
            if host.port is None:
                raise ValueError('NAS service requires a registered port')
            runtime_spec = ContainerRuntimeSpec(
                manifest.id, runtime_identity.image_digest, recipe.runtime.command, host.port,
                recipe.runtime.cpu_limit, recipe.runtime.memory_limit_mib,
                recipe.runtime.secret_files, recipe.runtime.user, health_runtime=recipe.runtime.health_runtime)
        native_hit = None

        def prepare_source(source: Path) -> None:
            nonlocal native_hit
            if recipe.native_dependencies is not None:
                platform = self.native_platform or probe_native_platform()
                # Dependency artifacts depend only on their keyed inputs and
                # native identity, never on host-specific public build values.
                prepared = NativeDependencyCache(self.cache_root/'native').prepare(
                    source, recipe.native_dependencies, recipe_version=recipe.recipe_version, platform=platform)
                native_hit = prepared.cache_hit

        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.local-web-nas-release-', dir=destination.parent) as temporary:
            candidate = Path(temporary)/'candidate'
            descriptor = ProductionBuilder(runner).build(
                repository, commit=commit, app_id=manifest.id, recipe_version=recipe.recipe_version,
                commands=recipe.build_commands, output=manifest.build.output, destination=candidate,
                data_contract=recipe.data_contract, rollback_policy=recipe.rollback_policy,
                runtime=runtime_identity, build_spec=manifest.build, prepare_source=prepare_source)
            if runtime_spec is not None:
                # This selected-app runtime configuration is part of the hashed
                # release. It contains secret names, never secret values/host paths.
                metadata = candidate/'files'/'.local-web-runtime.json'
                if metadata.exists() or metadata.is_symlink():
                    raise ValueError('app release collides with reserved runtime metadata')
                metadata.write_text(json.dumps(asdict(runtime_spec), sort_keys=True, separators=(',', ':'))+'\n')
                metadata.chmod(0o644)
                descriptor = replace(descriptor, files=snapshot_files(candidate/'files'))
                (candidate/'descriptor.json').write_bytes(descriptor.canonical_bytes())
            if destination.exists() or destination.is_symlink():
                raise FileExistsError('prepared NAS release already exists')
            os.rename(candidate, destination)
        return PreparedNasRelease(destination, descriptor, runtime_archive, runtime_spec,
                                  native_hit, runtime_hit)
