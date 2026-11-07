"""Build Linux dependency archives against an explicit local Mac Docker daemon."""

from collections.abc import Callable
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

from .process_runner import ProcessCommand, ProcessRunner, ProcessWorkflowRunner
from .production_build import DependencyKey, dependency_build_command
from .release_manifest import snapshot_files


def _installed(candidates: tuple[Path, ...]) -> Path:
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
    raise ValueError('the Mac Linux builder requires installed Docker and Buildx tools')


class MacDockerDependencyBuilder:
    """A dependency-cache installer; it never selects a remote Docker endpoint.

    A disposable client configuration discovers the installed Buildx plugin,
    without editing the user's Docker settings or copying registry credentials.
    The selected local daemon retains BuildKit layers; archive/cache bytes stay
    on the Mac. App Dockerfiles receive only the already restricted input tree.
    """
    def __init__(self, context: str, cache_root: Path, *, runner: ProcessWorkflowRunner | None = None,
                 inspect_context: Callable[[str], str] | None = None,
                 docker_executable: Path | None = None, plugin_directory: Path | None = None):
        if type(context) is not str or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', context):
            raise ValueError('an explicit local Docker context name is required')
        self.context, self.cache_root = context, Path(cache_root)
        # Cold scientific-wheel downloads exceeded twenty minutes on the
        # measured isolated Mac VM; warm builds never invoke this installer.
        self.runner = runner or ProcessRunner(timeout_seconds=3600)
        self.inspect_context = inspect_context
        self.docker_executable, self.plugin_directory = docker_executable, plugin_directory

    def _docker(self) -> Path:
        return _installed((self.docker_executable,) if self.docker_executable else (
            Path('/opt/homebrew/bin/docker'), Path('/usr/local/bin/docker'), Path('/usr/bin/docker')))

    def local_endpoint(self) -> str:
        if self.inspect_context is not None:
            endpoint = self.inspect_context(self.context)
        else:
            result = subprocess.run((str(self._docker()), '--config', str(Path.home()/'.docker'),
                                     'context', 'inspect', self.context),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env={'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'},
                timeout=5, check=True)
            if len(result.stdout) > 64*1024:
                raise ValueError('Docker context metadata is oversized')
            value = json.loads(result.stdout)
            if type(value) is not list or len(value) != 1:
                raise ValueError('Docker context is unavailable')
            endpoint = value[0].get('Endpoints', {}).get('docker', {}).get('Host')
        if type(endpoint) is not str or any(c.isspace() or ord(c) < 32 for c in endpoint):
            raise ValueError('the build context must select a local Unix socket')
        parsed = urlsplit(endpoint)
        if (parsed.scheme != 'unix' or parsed.netloc or not parsed.path.startswith('/')
                or parsed.query or parsed.fragment or not endpoint.startswith('unix:///')):
            raise ValueError('the build context must select a local Unix socket')
        return endpoint

    def __call__(self, source: Path, dockerfile: Path, key: DependencyKey,
                 archive: Path, base_image: str) -> None:
        endpoint = self.local_endpoint()
        docker = self._docker()
        plugin = _installed((self.plugin_directory/'docker-buildx',) if self.plugin_directory else (
            Path('/opt/homebrew/lib/docker/cli-plugins/docker-buildx'),
            Path('/usr/local/lib/docker/cli-plugins/docker-buildx'),
            Path.home()/'.docker/cli-plugins/docker-buildx'))
        build_cache = self.cache_root/key.digest
        build_cache.mkdir(parents=True, exist_ok=True)
        inputs = tuple(entry.path for entry in snapshot_files(source) if entry.path != dockerfile.as_posix())
        planned = dependency_build_command(source, dockerfile, archive, key, build_cache, base_image,
                                           dependency_inputs=inputs)
        with tempfile.TemporaryDirectory(prefix='local-web-docker-client-') as temporary:
            configuration = Path(temporary)
            (configuration/'config.json').write_text(json.dumps({'cliPluginsExtraDirs': [str(plugin.parent)]}))
            command = ProcessCommand(planned.label, (str(docker), '--config', str(configuration), '--host', endpoint,
                *planned.argv[1:3], '--builder', 'default', *planned.argv[3:]))
            self.runner.run(source, (command,))
