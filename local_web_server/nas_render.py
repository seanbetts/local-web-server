"""Pure, deterministic NAS Compose rendering; no installation or remote actions.

The publisher provides stable ``apps/<id>`` parents and atomic generation
pointers. Caddy and each backend follow the same current generation, avoiding
separate code/frontend switches. The reserved ``local-web-platform`` app holds
Compose, Caddyfile, serve.json and the platform UI as one immutable release.
The existing ingress renderer owns canonical Host, trusted headers and loopback
8080; no app data or secrets are mounted into the front door.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import ipaddress
import json
import math
from pathlib import Path, PurePosixPath
import re
from string import Formatter
from urllib.parse import urlsplit

from .models import AppManifest, HostRegistry


_TEMPLATE = Path(__file__).resolve().parents[1] / 'templates' / 'nas' / 'compose.yaml'
_ID = re.compile(r'^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$')
_IMAGE = re.compile(r'^(?:[a-z0-9][a-z0-9._:/-]*@)?sha256:[0-9a-f]{64}$')
_SECRET = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]*$')
_USER = re.compile(r'^[1-9][0-9]*:[0-9]+$')
_MAC_PATHS = ('/Users/', '/Volumes/', '/Applications/', '/System/', '/Library/')
_FIELDS = frozenset(('port', 'state', 'publicOrigin', 'bindHost', 'proxyAddress'))


def _text(value: object) -> bool:
    return type(value) is str and bool(value) and not any(ord(c) < 32 or c in '$\\' for c in value)


def _image(value: str) -> None:
    if type(value) is not str or not _IMAGE.fullmatch(value):
        raise ValueError('runtime image must be pinned by SHA-256 digest')


def _command(argv: tuple[str, ...], replacements: Mapping[str, str] | None = None) -> list[str]:
    if type(argv) is not tuple or not argv:
        raise ValueError('container command must be a nonempty direct-argument tuple')
    result = []
    for argument in argv:
        if not _text(argument) or any(path in argument for path in _MAC_PATHS):
            raise ValueError('container command contains an unsafe or Mac-only value')
        try:
            for _, field, spec, conversion in Formatter().parse(argument):
                if field is not None and (field not in _FIELDS or spec or conversion):
                    raise ValueError('unsupported container command placeholder')
            if replacements is not None:
                argument = argument.format_map(replacements)
                if '{' in argument or '}' in argument:
                    raise ValueError('literal braces are unsupported in container commands')
        except (KeyError, ValueError) as error:
            raise ValueError('unsupported container command placeholder') from error
        result.append(argument)
    return result


@dataclass(frozen=True)
class ContainerRuntimeSpec:
    """Prepared Linux runtime plus app-owned argv, limits and selected secret names.

    No dependency installation or build recipe is executed by this renderer.
    Callers select resource ceilings explicitly (initial Node 1 CPU/512 MiB,
    Python 2 CPU/1024 MiB from the design); ceilings do not reserve host RAM.
    Secret values are never accepted here, only individual binding names.
    """
    app_id: str
    image: str
    command: tuple[str, ...]
    port: int
    cpu_limit: float
    memory_limit_mib: int
    secret_files: tuple[str, ...] = ()
    user: str = '10001:10001'
    health_runtime: str = field(kw_only=True)

    def __post_init__(self) -> None:
        if type(self.app_id) is not str or not _ID.fullmatch(self.app_id):
            raise ValueError('invalid container app ID')
        _image(self.image)
        _command(self.command)
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError('invalid container port')
        if (type(self.cpu_limit) not in (int, float) or not math.isfinite(self.cpu_limit)
                or self.cpu_limit <= 0 or type(self.memory_limit_mib) is not int
                or self.memory_limit_mib <= 0):
            raise ValueError('container resource ceilings must be finite and positive')
        if (type(self.secret_files) is not tuple
                or any(type(name) is not str or not _SECRET.fullmatch(name) for name in self.secret_files)
                or len(set(self.secret_files)) != len(self.secret_files)):
            raise ValueError('secret bindings must be unique individual file names')
        if type(self.user) is not str or not _USER.fullmatch(self.user):
            raise ValueError('backend user must be a non-root numeric uid:gid')
        if self.health_runtime not in ('node', 'python3'):
            raise ValueError('health runtime must explicitly select node or python3')


def _bind(source: PurePosixPath, target: str, *, read_only: bool = True) -> dict:
    return dict(type='bind', source=str(source), target=target, read_only=read_only,
                bind=dict(create_host_path=False))


def _validate_target(registry: HostRegistry) -> PurePosixPath:
    if registry.target.kind != 'nas-docker' or registry.ingress_mode != 'tailscale-serve':
        raise ValueError('NAS Compose requires the private Tailscale target')
    origin = urlsplit(registry.public_origin)
    if (registry.public_origin != f'https://{registry.host}'
            or origin.scheme != 'https' or origin.netloc != registry.host or not registry.host.endswith('.ts.net')
            or origin.username is not None or origin.password is not None
            or origin.path or origin.query or origin.fragment or not _text(registry.public_origin)):
        raise ValueError('NAS origin must be the canonical Tailscale HTTPS host')
    try:
        network = ipaddress.IPv4Network(registry.target.network_subnet, strict=True)
        proxy = ipaddress.IPv4Address(registry.target.proxy_address)
        private = tuple(ipaddress.IPv4Network(block) for block in
                        ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
        if (not any(network.subnet_of(block) for block in private) or network.prefixlen > 30
                or proxy not in network or proxy in (network.network_address, network.broadcast_address)):
            raise ValueError('invalid private network')
    except (ValueError, TypeError) as error:
        raise ValueError('NAS target requires a private IPv4 subnet and usable proxy address') from error
    raw = str(registry.runtime_root)
    root = PurePosixPath(raw)
    if (not _text(raw) or not root.is_absolute() or raw != str(root) or '..' in root.parts
            or raw == '/' or any(raw == path.rstrip('/') or raw.startswith(path) for path in _MAC_PATHS)):
        raise ValueError('NAS runtime root must be a canonical Linux absolute path')
    return root


def render_nas_compose(
    registry: HostRegistry,
    manifests: Mapping[str, AppManifest],
    runtimes: tuple[ContainerRuntimeSpec, ...],
    *,
    caddy_image: str,
    tailscale_image: str,
    caddy_environment: tuple[tuple[str, str], ...] = (),
) -> str:
    """Return JSON-valid YAML for a dedicated stack with no published host ports.

    The dedicated bridge has ordinary Docker egress (providers and Tailscale need
    it); it is not an outbound firewall. This configuration never joins an
    external/media network. No Docker socket, Mac source checkout or home mount
    is allowed. Filesystem existence, identity enrollment, Caddy validation,
    schema compatibility and app eligibility are separate integration gates.
    """
    root = _validate_target(registry)
    _image(caddy_image)
    _image(tailscale_image)
    app_ids = [app.id for app in registry.apps]
    if (len(app_ids) != len(set(app_ids)) or set(manifests) != set(app_ids)
            or any(not _ID.fullmatch(name) or manifests[name].id != name
                   or manifests[name].kind not in ('static', 'service') for name in app_ids)):
        raise ValueError('manifest inventory must exactly match registered apps')
    if type(runtimes) is not tuple or any(type(spec) is not ContainerRuntimeSpec for spec in runtimes):
        raise ValueError('invalid container runtime inventory')
    runtime_ids = [spec.app_id for spec in runtimes]
    service_ids = {name for name in app_ids if manifests[name].kind == 'service'}
    if len(set(runtime_ids)) != len(runtime_ids) or set(runtime_ids) != service_ids:
        raise ValueError('every service requires exactly one container runtime; static apps require none')
    ports = {app.id: app.port for app in registry.apps}
    if any(ports[spec.app_id] != spec.port for spec in runtimes):
        raise ValueError('runtime port must match registered backend')
    network = ipaddress.IPv4Network(registry.target.network_subnet)
    if network.num_addresses - 2 < len(runtimes) + 2:
        raise ValueError('private subnet needs room for gateway, sidecar and all backends')
    gateway = network[1] if str(network[1]) != registry.target.proxy_address else network[2]

    document = json.loads(_TEMPLATE.read_text(encoding='utf-8'))
    services = document['services']
    platform_root = root / 'apps' / 'local-web-platform'
    sidecar = services['tailscale']
    sidecar['environment']['TS_SERVE_CONFIG'] = '/etc/local-web/current/serve.json'
    sidecar.update(image=tailscale_image, networks={'local-web': {'ipv4_address': registry.target.proxy_address}},
                   volumes=[_bind(root / 'tailscale', '/var/lib/tailscale', read_only=False),
                            _bind(platform_root, '/etc/local-web')])
    services['caddy'].update(image=caddy_image,
                             volumes=[_bind(root / 'apps', '/srv/apps'),
                                      _bind(platform_root, '/srv/platform'),
                                      _bind(platform_root, '/etc/local-web')])
    services['caddy']['command'] = ['run', '--config', '/etc/local-web/current/Caddyfile', '--adapter', 'caddyfile']
    if (type(caddy_environment) is not tuple
            or any(type(pair) is not tuple or len(pair) != 2
                   or type(pair[0]) is not str or not re.fullmatch(r'LOCAL_WEB_PROBE_[A-Z0-9_]+', pair[0])
                   or not _text(pair[1]) for pair in caddy_environment)
            or len({name for name, _ in caddy_environment}) != len(caddy_environment)):
        raise ValueError('Caddy environment must contain only declared public probe inputs')
    if caddy_environment:
        services['caddy']['environment'] = dict(sorted(caddy_environment))
    document['networks']['local-web']['ipam'] = {
        'config': [{'subnet': registry.target.network_subnet, 'gateway': str(gateway)}]}
    replacements = dict(state='/state', publicOrigin=registry.public_origin,
                        bindHost='0.0.0.0', proxyAddress=registry.target.proxy_address)
    for spec in sorted(runtimes, key=lambda value: value.app_id):
        services['app-' + spec.app_id] = dict(
            image=spec.image, platform='linux/amd64', restart='unless-stopped',
            entrypoint=_command(spec.command, replacements | {'port': str(spec.port)}), command=[],
            working_dir='/app/current', user=spec.user, read_only=True,
            cap_drop=['ALL'], security_opt=['no-new-privileges:true'], tmpfs=['/tmp'],
            networks={'local-web': {}}, cpus=spec.cpu_limit, mem_limit=f'{spec.memory_limit_mib}m',
            volumes=[_bind(root / 'apps' / spec.app_id, '/app'),
                     _bind(root / 'state' / spec.app_id, '/state', read_only=False)] +
                    [_bind(root / 'secrets' / spec.app_id / name, '/run/secrets/' + name)
                     for name in sorted(spec.secret_files)])
    return json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + '\n'
