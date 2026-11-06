"""Native frontend dependency preparation, separate from Linux runtime artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Iterator

from .process_runner import ProcessCommand, ProcessRunner, ProcessWorkflowRunner
from .release_manifest import validate_relative_path


@dataclass(frozen=True, slots=True)
class NativePlatformIdentity:
    os: str
    architecture: str
    node_abi: str

    def __post_init__(self) -> None:
        if any(type(value) is not str or not re.fullmatch(r'[a-zA-Z0-9._-]+', value)
               for value in (self.os, self.architecture, self.node_abi)):
            raise ValueError('native dependency identity requires OS, architecture and Node ABI')

    def to_dict(self) -> dict[str, str]:
        return dict(os=self.os, architecture=self.architecture, nodeAbi=self.node_abi)


def probe_native_platform() -> NativePlatformIdentity:
    """Read the installed Node ABI without ambient NODE_OPTIONS or package code."""
    for candidate in ('/opt/homebrew/bin/node', '/usr/local/bin/node', '/usr/bin/node'):
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            break
    else:
        raise ValueError('a supported native Node installation is required')
    try:
        result = subprocess.run((candidate, '-p',
            'JSON.stringify({os:process.platform,architecture:process.arch,nodeAbi:process.versions.modules})'),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'},
            timeout=5, check=True)
        if len(result.stdout) > 1024:
            raise ValueError('native Node metadata is oversized')
        value = json.loads(result.stdout)
        if type(value) is not dict or set(value) != {'os', 'architecture', 'nodeAbi'}:
            raise ValueError('invalid native Node metadata')
        return NativePlatformIdentity(value['os'], value['architecture'], value['nodeAbi'])
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise ValueError('native Node identity inspection failed') from error


@dataclass(frozen=True, slots=True)
class NativeDependencyRecipe:
    lockfiles: tuple[str, ...]
    install_commands: tuple[ProcessCommand, ...]
    outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        for values in (self.lockfiles, self.outputs):
            if type(values) is not tuple or not values or any(type(v) is not str for v in values) or len(set(values)) != len(values):
                raise ValueError('native dependency inputs and outputs must be unique nonempty paths')
            for value in values:
                validate_relative_path(value)
        names = set(self.outputs)
        if any(any(str(parent) in names for parent in Path(name).parents) for name in names):
            raise ValueError('native dependency outputs cannot overlap')
        if type(self.install_commands) is not tuple or not self.install_commands:
            raise ValueError('native dependency installation requires explicit direct commands')
        for command in self.install_commands:
            if (type(command) is not ProcessCommand or not command.argv
                    or any(type(arg) is not str or not arg or '\0' in arg for arg in command.argv)):
                raise ValueError('invalid native dependency command')

    def to_dict(self) -> dict[str, Any]:
        return dict(lockfiles=list(self.lockfiles), installCommands=[list(c.argv) for c in self.install_commands],
                    outputs=list(self.outputs))

    @classmethod
    def from_dict(cls, value: object) -> NativeDependencyRecipe:
        if type(value) is not dict or set(value) != {'lockfiles', 'installCommands', 'outputs'}:
            raise ValueError('invalid native dependency recipe fields')
        for field in ('lockfiles', 'outputs'):
            if type(value[field]) is not list or any(type(v) is not str for v in value[field]):
                raise ValueError('native dependency paths must be lists of strings')
        commands = parse_native_commands(value['installCommands'], label='native dependencies')
        return cls(tuple(value['lockfiles']), commands, tuple(value['outputs']))


def parse_native_commands(value: object, *, label: str = 'native build') -> tuple[ProcessCommand, ...]:
    if type(value) is not list:
        raise ValueError('native build commands must be direct argument lists')
    result = []
    for index, argv in enumerate(value):
        if type(argv) is not list or not argv or any(type(arg) is not str or not arg or '\0' in arg for arg in argv):
            raise ValueError('native build commands must be direct argument lists')
        result.append(ProcessCommand(f'{label} {index + 1}', tuple(argv)))
    return tuple(result)


@contextmanager
def dependency_input_export(source: Path, inputs: tuple[str, ...]) -> Iterator[Path]:
    """Expose only declared dependency inputs, never the complete app source/state."""
    if type(inputs) is not tuple or not inputs or any(type(path) is not str for path in inputs):
        raise ValueError('dependency preparation requires explicit input files')
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError('dependency inputs require an exact source export')
    with tempfile.TemporaryDirectory(prefix='local-web-dependency-inputs-') as temporary:
        context = Path(temporary)
        for relative in sorted(set(inputs)):
            validate_relative_path(relative)
            current = source
            for part in Path(relative).parts:
                current = current / part
                if current.is_symlink():
                    raise ValueError('dependency input paths cannot contain links')
            descriptor = os.open(current, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                before = os.fstat(descriptor)
                if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode) > 0o777:
                    raise ValueError('dependency inputs must be regular unprivileged files')
                target = context / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with os.fdopen(os.dup(descriptor), 'rb') as stream, target.open('xb') as output:
                    shutil.copyfileobj(stream, output)
                after = os.fstat(descriptor)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError('dependency input changed while exporting')
                target.chmod(stat.S_IMODE(before.st_mode))
            finally:
                os.close(descriptor)
        yield context


def _hash(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError('native dependency input must be a regular file')
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _key(source: Path, recipe: NativeDependencyRecipe, version: str,
         platform: NativePlatformIdentity) -> str:
    if type(version) is not str or not version:
        raise ValueError('native dependency recipe version is required')
    inputs = []
    for relative in sorted(recipe.lockfiles):
        current = source
        for part in Path(relative).parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('native lockfiles cannot contain links')
        inputs.append(dict(path=relative, sha256=_hash(current), mode=stat.S_IMODE(current.stat().st_mode)))
    return hashlib.sha256(json.dumps(dict(contextContractVersion=1, inputs=inputs, recipe=recipe.to_dict(), recipeVersion=version,
        platform=platform.to_dict()), sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _snapshot(tree: Path) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(tree.rglob('*')):
        relative = path.relative_to(tree).as_posix()
        validate_relative_path(relative)
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            try:
                resolved = path.resolve(strict=True)
            except (OSError, RuntimeError) as error:
                raise ValueError('native dependency link must resolve inside its artifact') from error
            if Path(target).is_absolute() or not resolved.is_relative_to(tree.resolve(strict=True)):
                raise ValueError('native dependency link escapes its artifact')
            entries.append(dict(path=relative, type='link', target=target))
        elif stat.S_ISDIR(metadata.st_mode):
            entries.append(dict(path=relative, type='directory', mode=stat.S_IMODE(metadata.st_mode)))
        elif stat.S_ISREG(metadata.st_mode):
            entries.append(dict(path=relative, type='file', mode=stat.S_IMODE(metadata.st_mode),
                                size=metadata.st_size, sha256=_hash(path)))
        else:
            raise ValueError('native dependencies cannot contain special files')
    return entries


def _copy_outputs(source: Path, destination: Path, outputs: tuple[str, ...]) -> None:
    for relative in outputs:
        original = source / relative
        target = destination / relative
        if original.is_symlink() or not original.is_dir():
            raise ValueError('native dependency outputs must be directories')
        if target.exists() or target.is_symlink():
            raise ValueError('native dependency restore must not replace existing source files')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(original, target, symlinks=True)


@dataclass(frozen=True, slots=True)
class NativeDependencyResult:
    key: str
    cache_hit: bool


class NativeDependencyCache:
    def __init__(self, root: Path, runner: ProcessWorkflowRunner | None = None):
        self.root = Path(root)
        self.runner = runner or ProcessRunner()

    def prepare(self, source: Path, recipe: NativeDependencyRecipe, *, recipe_version: str,
                platform: NativePlatformIdentity) -> NativeDependencyResult:
        source = Path(source)
        if source.is_symlink() or not source.is_dir():
            raise ValueError('native preparation requires an exact source export')
        for relative in recipe.outputs:
            target = source / relative
            if target.exists() or target.is_symlink():
                raise ValueError('native dependency outputs already exist in the source export')
        if self.root.resolve().is_relative_to(source.resolve()):
            raise ValueError('native dependency caches must be outside the source export')
        key = _key(source, recipe, recipe_version, platform)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise ValueError('native cache must not be a link')
        lock = os.open(self.root / (key + '.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._prepare_locked(source, recipe, recipe_version, platform, key)
        finally:
            os.close(lock)

    def _prepare_locked(self, source: Path, recipe: NativeDependencyRecipe, version: str,
                        platform: NativePlatformIdentity, key: str) -> NativeDependencyResult:
        target = self.root / key
        if target.is_symlink():
            raise ValueError('native dependency cache entry cannot be a link')
        cache_hit = False
        try:
            metadata = target / 'tree.json'
            tree = target / 'tree'
            if metadata.is_symlink() or tree.is_symlink():
                raise ValueError('native dependency cache metadata cannot be a link')
            expected = json.loads(metadata.read_text())
            cache_hit = _snapshot(tree) == expected
        except (OSError, ValueError):
            pass
        if cache_hit:
            _copy_outputs(tree, source, recipe.outputs)
            return NativeDependencyResult(key, True)
        with dependency_input_export(source, recipe.lockfiles) as installer_source:
            self.runner.run(installer_source, recipe.install_commands)
            if _key(installer_source, recipe, version, platform) != key:
                raise ValueError('native installer changed a locked dependency input')
            with tempfile.TemporaryDirectory(prefix='.native-prepare-', dir=self.root) as temporary:
                staging = Path(temporary)
                tree = staging / 'tree'
                tree.mkdir()
                _copy_outputs(installer_source, tree, recipe.outputs)
                snapshot = _snapshot(tree)
                (staging / 'tree.json').write_text(json.dumps(snapshot, sort_keys=True))
                if target.exists():
                    shutil.rmtree(target)
                target.mkdir(mode=0o700)
                os.rename(tree, target / 'tree')
                os.rename(staging / 'tree.json', target / 'tree.json')
        _copy_outputs(target / 'tree', source, recipe.outputs)
        return NativeDependencyResult(key, False)
