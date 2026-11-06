"""App-owned production recipes, independent of host configuration and models."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from string import Formatter
from typing import TYPE_CHECKING, Any

from .release_manifest import validate_relative_path
from .native_build import NativeDependencyRecipe, parse_native_commands
from .process_runner import ProcessCommand

if TYPE_CHECKING:
    from .production_build import DependencyKey

_IMAGE = re.compile(r'^[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64}$')
_SECRET = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$')
_USER = re.compile(r'^[1-9][0-9]*:[0-9]+$')
_FIELDS = frozenset({'port', 'state', 'publicOrigin', 'bindHost', 'proxyAddress'})
_MAC_PATHS = ('/Users/', '/Volumes/', '/Applications/', '/System/', '/Library/')


def _text(value: object) -> bool:
    return type(value) is str and bool(value) and not any(ord(c) < 32 for c in value)


def _fields(value: object, required: set[str], optional: set[str] | None = None) -> dict:
    if type(value) is not dict or not required <= set(value) or set(value) - required - (optional or set()):
        raise ValueError('invalid production recipe fields')
    return value


def validate_container_command(command: tuple[str, ...]) -> None:
    if type(command) is not tuple or not command:
        raise ValueError('container command requires direct arguments')
    for argument in command:
        if (not _text(argument) or '$' in argument or '\\' in argument
                or any(path in argument for path in _MAC_PATHS)):
            raise ValueError('container command contains an unsafe or Mac-only value')
        try:
            for _, field, spec, conversion in Formatter().parse(argument):
                if field is not None and (field not in _FIELDS or spec or conversion):
                    raise ValueError('unsupported container command placeholder')
            expanded = argument.format_map({name: 'value' for name in _FIELDS})
            if '{' in expanded or '}' in expanded:
                raise ValueError('container command cannot contain literal braces')
        except (ValueError, KeyError) as error:
            raise ValueError('unsupported container command placeholder') from error


@dataclass(frozen=True, slots=True)
class ContainerBuildRecipe:
    base_image: str
    abi: str
    lockfiles: tuple[str, ...]
    dockerfile: str
    command: tuple[str, ...]
    cpu_limit: float
    memory_limit_mib: int
    secret_files: tuple[str, ...] = ()
    user: str = '10001:10001'
    health_runtime: str = field(kw_only=True)

    def __post_init__(self) -> None:
        if type(self.health_runtime) is not str or self.health_runtime not in ('node', 'python3'):
            raise ValueError('health runtime must explicitly select node or python3')
        if type(self.base_image) is not str or not _IMAGE.fullmatch(self.base_image) or not _text(self.abi):
            raise ValueError('runtime recipe requires a pinned base image and explicit ABI')
        if type(self.lockfiles) is not tuple or not self.lockfiles or len(set(self.lockfiles)) != len(self.lockfiles):
            raise ValueError('runtime recipe requires unique dependency lockfiles')
        for path in self.lockfiles:
            validate_relative_path(path)
        validate_relative_path(self.dockerfile)
        validate_container_command(self.command)
        if (type(self.cpu_limit) not in (int, float) or not math.isfinite(self.cpu_limit)
                or self.cpu_limit <= 0 or type(self.memory_limit_mib) is not int or self.memory_limit_mib <= 0):
            raise ValueError('container resource ceilings must be finite and positive')
        if (type(self.secret_files) is not tuple or any(type(name) is not str or not _SECRET.fullmatch(name)
                for name in self.secret_files) or len(set(self.secret_files)) != len(self.secret_files)):
            raise ValueError('secret bindings require unique individual filenames')
        if type(self.user) is not str or not _USER.fullmatch(self.user):
            raise ValueError('container user must be a non-root numeric uid:gid')

    def to_dict(self) -> dict[str, Any]:
        return dict(baseImage=self.base_image, abi=self.abi, lockfiles=list(self.lockfiles),
                    dockerfile=self.dockerfile, command=list(self.command), cpuLimit=self.cpu_limit,
                    memoryLimitMib=self.memory_limit_mib, secretFiles=list(self.secret_files), user=self.user, healthRuntime=self.health_runtime)

    @classmethod
    def from_dict(cls, value: object) -> ContainerBuildRecipe:
        value = _fields(value, {'baseImage', 'abi', 'lockfiles', 'dockerfile', 'command',
                                'cpuLimit', 'memoryLimitMib', 'secretFiles', 'healthRuntime'}, {'user'})
        for field in ('lockfiles', 'command', 'secretFiles'):
            if type(value[field]) is not list or any(type(item) is not str for item in value[field]):
                raise ValueError('runtime recipe list fields must contain strings')
        return cls(value['baseImage'], value['abi'], tuple(value['lockfiles']), value['dockerfile'],
                   tuple(value['command']), value['cpuLimit'], value['memoryLimitMib'],
                   tuple(value['secretFiles']), value.get('user', '10001:10001'), health_runtime=value['healthRuntime'])


def _source_digest(root: Path, relative: str) -> tuple[str, int]:
    candidate = root
    for part in Path(relative).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError('recipe input cannot contain links')
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('recipe inputs must be regular committed files')
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(descriptor)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('recipe input changed during verification')
            return digest.hexdigest(), after.st_size
        finally:
            os.close(descriptor)
    except OSError as error:
        raise ValueError('committed recipe input is unavailable') from error


@dataclass(frozen=True, slots=True)
class ProductionRecipe:
    recipe_version: str
    data_contract: str
    rollback_policy: str
    runtime: ContainerBuildRecipe | None
    build_commands: tuple[ProcessCommand, ...] = ()
    native_dependencies: NativeDependencyRecipe | None = None

    def __post_init__(self) -> None:
        if (not _text(self.recipe_version) or not _text(self.data_contract)
                or self.rollback_policy not in ('compatible', 'migration-required')
                or self.runtime is not None and type(self.runtime) is not ContainerBuildRecipe):
            raise ValueError('invalid production recipe')
        if type(self.build_commands) is not tuple:
            raise ValueError('native build commands must be immutable direct arguments')
        parse_native_commands([list(command.argv) for command in self.build_commands
                               if type(command) is ProcessCommand])
        if any(type(command) is not ProcessCommand for command in self.build_commands):
            raise ValueError('invalid native build command')
        if self.native_dependencies is not None and type(self.native_dependencies) is not NativeDependencyRecipe:
            raise ValueError('invalid native dependency recipe')

    def validate_kind(self, kind: str) -> None:
        if kind == 'static':
            if self.runtime is not None or self.data_contract != 'none' or self.rollback_policy != 'compatible':
                raise ValueError('static production recipe requires compatible none-data contract and no runtime')
        elif kind == 'service':
            if self.runtime is None:
                raise ValueError('service production recipe requires an explicit Linux runtime')
        else:
            raise ValueError('unsupported production app kind')

    def to_dict(self) -> dict[str, Any]:
        return dict(recipeVersion=self.recipe_version, dataContract=self.data_contract,
                    rollbackPolicy=self.rollback_policy,
                    runtime=self.runtime.to_dict() if self.runtime is not None else None,
                    buildCommands=[list(command.argv) for command in self.build_commands],
                    nativeDependencies=self.native_dependencies.to_dict() if self.native_dependencies is not None else None)

    def dependency_key(self, source_root: Path) -> DependencyKey:
        if self.runtime is None:
            raise ValueError('static apps have no dependency artifact')
        # Lazy import preserves models -> recipe -> validation independence.
        from .production_build import DependencyKey
        root = Path(source_root)
        if root.is_symlink() or not root.is_dir():
            raise ValueError('recipe inputs require an exact source export')
        inputs = [dict(path=path, sha256=digest, size=size, mode=stat.S_IMODE((root / path).stat().st_mode))
                  for path in sorted(self.runtime.lockfiles)
                  for digest, size in [_source_digest(root, path)]]
        lock_digest = hashlib.sha256(json.dumps(inputs, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()
        recipe_digest, _ = _source_digest(root, self.runtime.dockerfile)
        return DependencyKey(lock_digest, self.runtime.base_image.rsplit('@', 1)[1],
                             'linux', 'amd64', self.runtime.abi, self.recipe_version,
                             recipe_digest=recipe_digest)


def parse_production_recipe(value: object, *, kind: str) -> ProductionRecipe:
    value = _fields(value, {'recipeVersion', 'dataContract', 'rollbackPolicy', 'runtime', 'buildCommands', 'nativeDependencies'})
    recipe = ProductionRecipe(value['recipeVersion'], value['dataContract'], value['rollbackPolicy'],
                              ContainerBuildRecipe.from_dict(value['runtime']) if value['runtime'] is not None else None,
                              parse_native_commands(value['buildCommands']),
                              NativeDependencyRecipe.from_dict(value['nativeDependencies']) if value['nativeDependencies'] is not None else None)
    recipe.validate_kind(kind)
    return recipe


def require_nas_recipe(recipe: ProductionRecipe | None, *, kind: str) -> ProductionRecipe:
    if recipe is None:
        raise ValueError('app requires a committed production recipe before NAS publication')
    recipe.validate_kind(kind)
    return recipe


def production_declaration(kind: str, *, recipe_version: str = '1', data_contract: str | None = None,
                           rollback_policy: str = 'compatible',
                           runtime: ContainerBuildRecipe | None = None,
                           build_commands: tuple[ProcessCommand, ...] = (),
                           native_dependencies: NativeDependencyRecipe | None = None) -> dict[str, Any]:
    """Generate declarations from supplied app contracts; never invent image pins."""
    if data_contract is None:
        if kind != 'static':
            raise ValueError('service generation requires an explicit app data contract')
        data_contract = 'none'
    recipe = ProductionRecipe(recipe_version, data_contract, rollback_policy, runtime, build_commands, native_dependencies)
    recipe.validate_kind(kind)
    return recipe.to_dict()
